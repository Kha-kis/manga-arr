"""Background-task loops and the scheduler harness.

Seventeenth module extracted from main.py. Pulls out every long-
running asyncio loop plus the task-lifecycle harness, the one-shot
"Run Now" entry points the task router calls, and the shared
cleanup / backoff helpers:

  periodic loops
    rss_loop                       — poll enabled indexers
    status_loop                    — check download completion
    refresh_ongoing_loop           — daily AniList refresh for
                                     RELEASING / HIATUS series
    _backfill_metadata_loop        — startup-only backfill of
                                     MangaDex / MAL / MU ids +
                                     chapter manifests, rate-aware
    _stuck_state_cleanup_loop      — hourly reconcile of stranded
                                     volume / queue / pending rows
    backlog_search_loop            — daily active search for
                                     every wanted volume
    rescan_loop                    — periodic library rescan
    _import_list_loop              — periodic import-list sync
    _backup_loop                   — auto-backup of the DB

  one-shot "Run Now" entry points
    backlog_search                 — single-pass backlog sweep
    import_list_sync               — single-pass list sync

  MangaDex backoff state
    _MDX_BACKOFF_UNTIL             — in-process deadline
    _mdx_backoff_active
    _mdx_set_backoff
    _maybe_backoff_from_exception  — extract Retry-After from a 429
    _parse_retry_after_seconds

  reconciliation
    cleanup_stuck_state            — three-phase sweep used both
                                     on startup and by the hourly
                                     cleanup loop

  task lifecycle harness
    _BACKGROUND_TASKS              — strong-ref registry
    create_background_task         — spawn + register + done-callback
    _cancel_background_tasks       — await graceful shutdown

main.log_event / main.poll_rss / main.check_download_status /
main.grab_existing / main.DB_PATH are imported lazily inside function
bodies to avoid import cycles — same pattern as prior extractions.
routers.* imports are lazy for the same reason.

Note: rescan_loop previously referenced `_rescan_all_impl` by bare
name, which resolved through main's module globals — except that
name was never actually imported into main, so the loop would have
raised NameError the first time it fired after the 12h startup
delay. The extraction adds the `from routers.series_ import
_rescan_all_impl` lazy import that was always needed.
"""
from __future__ import annotations

import asyncio
import os
import zipfile
from datetime import datetime, timedelta, timezone

from events import log_event
from grab import grab_existing, poll_rss
from import_pipeline import check_download_status
from metadata import anilist_search
from metadata_enrichment import _NON_STANDARD_STUB_EDITIONS, refresh_mangadex_map
from shared import get_cfg, get_db
from volumes import create_volume_stubs


async def rss_loop():
    from routers.system import update_task_state
    await asyncio.sleep(5)  # brief startup delay to let lifespan complete
    while True:
        try:
            await poll_rss()
        except Exception as e:
            log_event('error', f"RSS poll error: {e}")
        interval = max(60, int(get_cfg('rss_interval', '900')))
        now = datetime.now(timezone.utc)
        update_task_state('RssSyncAll', last_run=now,
                          next_run=datetime.fromtimestamp(now.timestamp() + interval, tz=timezone.utc))
        await asyncio.sleep(interval)


async def status_loop():
    """Check download completion every 5 minutes."""
    from routers.system import update_task_state
    await asyncio.sleep(60)  # initial delay
    while True:
        try:
            await check_download_status()
        except Exception as e:
            log_event('error', f"Download status check error: {e}")
        now = datetime.now(timezone.utc)
        update_task_state('CheckDownloads', last_run=now,
                          next_run=datetime.fromtimestamp(now.timestamp() + 300, tz=timezone.utc))
        await asyncio.sleep(300)


_THROTTLED_REFRESH_DAYS = 7   # how many days between refreshes for 'throttled' series


async def refresh_ongoing_loop():
    """Daily: check AniList for new volumes on RELEASING series, respecting per-series update_strategy."""
    await asyncio.sleep(300)  # initial delay
    while True:
        try:
            interval = max(3600, int(get_cfg('refresh_interval', '86400')))
            with get_db() as db:
                # Include RELEASING and any series explicitly set to 'always' or 'throttled'
                # ('once' series are auto-skipped below)
                candidates = db.execute(
                    "SELECT * FROM series WHERE UPPER(status) IN ('RELEASING','HIATUS')"
                    " AND anilist_id IS NOT NULL AND monitored=1"
                    " AND deleted_at IS NULL"
                ).fetchall()
            updated = 0
            now_utc = datetime.utcnow()
            for s in candidates:
                strategy = (s['update_strategy'] or 'always') if 'update_strategy' in s.keys() else 'always'

                # ── Update strategy filter ────────────────────────────────────
                if strategy == 'once':
                    # 'once' = manual-only; skip auto-refresh entirely
                    continue
                elif strategy == 'throttled':
                    last_refresh = s['last_metadata_refresh'] if 'last_metadata_refresh' in s.keys() else None
                    if last_refresh:
                        try:
                            last_dt = datetime.fromisoformat(last_refresh)
                            if (now_utc - last_dt).days < _THROTTLED_REFRESH_DAYS:
                                continue   # too soon
                        except ValueError:
                            pass
                # 'always' → fall through

                results = await anilist_search(s['title'])
                match = next((r for r in results if r['anilist_id'] == s['anilist_id']), None)
                if not match:
                    continue
                new_vols   = match.get('volumes') or 0
                old_vols   = s['total_volumes'] or 0
                new_status = match.get('status', s['status'])
                with get_db() as db:
                    # Always stamp last_metadata_refresh even if no data changed
                    db.execute(
                        "UPDATE series SET last_metadata_refresh=? WHERE id=?",
                        (now_utc.isoformat(), s['id'])
                    )
                    if new_vols > old_vols or new_status != s['status']:
                        db.execute(
                            "UPDATE series SET total_volumes=?, status=?,"
                            " vol_count_source=CASE WHEN COALESCE(vol_count_source,'anilist')"
                            " IN ('google_books','wikipedia','manual') THEN vol_count_source ELSE 'anilist' END"
                            " WHERE id=?",
                            (new_vols or None, new_status, s['id'])
                        )
                        if new_vols > old_vols and (s['edition_type'] or 'standard') not in _NON_STANDARD_STUB_EDITIONS:
                            create_volume_stubs(db, s['id'], new_vols)
                        # Auto-switch to 'once' when a series finishes — no need to keep polling
                        if new_status in ('FINISHED', 'CANCELLED') and strategy == 'always':
                            db.execute(
                                "UPDATE series SET update_strategy='once' WHERE id=?", (s['id'],)
                            )
                        log_event('refresh',
                                  f"Auto-refresh: {old_vols}→{new_vols} vols, status={new_status}",
                                  s['id'])
                        updated += 1
                await asyncio.sleep(1)  # rate-limit AniList requests
            if updated:
                log_event('refresh', f"Auto-refresh complete: {updated} series updated")
        except Exception as e:
            print(f"[Refresh] Error: {e}")
        from routers.system import update_task_state
        now = datetime.now(timezone.utc)
        update_task_state('RefreshMetadata', last_run=now,
                          next_run=datetime.fromtimestamp(now.timestamp() + interval, tz=timezone.utc))
        await asyncio.sleep(interval)


_MDX_BACKOFF_UNTIL: float = 0.0


def _mdx_backoff_active() -> bool:
    import time as _t
    return _t.time() < _MDX_BACKOFF_UNTIL


def _mdx_set_backoff(seconds: float, reason: str) -> None:
    """Extend the MangaDex backoff deadline. Persisted only in-process —
    a restart resets it, which is fine because the backfill loop re-runs
    from scratch at startup anyway and will re-hit any ongoing rate limit
    immediately."""
    global _MDX_BACKOFF_UNTIL
    import time as _t
    deadline = _t.time() + max(seconds, 1.0)
    if deadline > _MDX_BACKOFF_UNTIL:
        _MDX_BACKOFF_UNTIL = deadline
        print(f"[Backfill] MangaDex backoff set: {int(seconds)}s ({reason})")


async def _backfill_metadata_loop():
    """
    At startup, backfill MangaDex ID + cross-references (MAL/MU) for series missing them.
    Runs once, with a small delay between each to respect MangaDex rate limits (~5 req/s).
    When upstream signals rate-limiting (via an httpx.HTTPStatusError from a 429),
    respect the Retry-After value and defer remaining work until the deadline
    elapses so we don't burn through IP-ban thresholds.
    """
    from routers import mangadex_ as _mdx_router  # noqa: WPS433 (lazy to avoid cycle)
    await asyncio.sleep(10)  # let startup settle first
    with get_db() as db:
        missing = db.execute(
            "SELECT id FROM series WHERE deleted_at IS NULL AND ("
            " mangadex_id IS NULL OR mal_id IS NULL OR mu_id IS NULL"
            " OR (mangadex_id IS NOT NULL AND chapter_vol_map IS NULL))"
        ).fetchall()
    for row in missing:
        # If we've been rate-limited recently, hold off until the deadline
        while _mdx_backoff_active():
            import time as _t
            wait = max(1.0, _MDX_BACKOFF_UNTIL - _t.time())
            await asyncio.sleep(min(wait, 30))
        try:
            await refresh_mangadex_map(row['id'])
        except Exception as e:
            print(f"[Startup] metadata backfill error for series {row['id']}: {e}")
            _maybe_backoff_from_exception(e)
        await asyncio.sleep(2)  # ~0.5 req/s — well under MangaDex limit

    # Sync MangaDex chapter manifests for series that have mangadex_id but no chapter rows
    with get_db() as db:
        needs_sync = db.execute(
            "SELECT id FROM series WHERE mangadex_id IS NOT NULL"
            " AND deleted_at IS NULL"
            " AND NOT EXISTS (SELECT 1 FROM mangadex_chapters m WHERE m.series_id=series.id)"
        ).fetchall()
    for row in needs_sync:
        while _mdx_backoff_active():
            import time as _t
            wait = max(1.0, _MDX_BACKOFF_UNTIL - _t.time())
            await asyncio.sleep(min(wait, 30))
        try:
            await _mdx_router.sync_mangadex_chapters(row['id'])
        except Exception as e:
            print(f"[Startup] MangaDex chapter sync error for series {row['id']}: {e}")
            _maybe_backoff_from_exception(e)
        await asyncio.sleep(1.5)


def _maybe_backoff_from_exception(exc: Exception) -> None:
    """If an httpx exception carries a 429 response with Retry-After, honour
    it. Otherwise this is a no-op — the caller already handled the error."""
    resp = getattr(exc, 'response', None)
    if resp is None:
        return
    try:
        status = getattr(resp, 'status_code', None)
        if status == 429:
            ra = resp.headers.get('Retry-After') if hasattr(resp, 'headers') else None
            seconds = _parse_retry_after_seconds(ra) if ra else 60.0
            _mdx_set_backoff(seconds or 60.0, f'Retry-After={ra!r}')
    except Exception:
        pass


def _parse_retry_after_seconds(raw: str | None) -> float | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        import datetime as _dt
        dt = parsedate_to_datetime(raw)
        if dt is None:
            return None
        return max(0.0, (dt - _dt.datetime.now(_dt.timezone.utc)).total_seconds())
    except Exception:
        return None


def cleanup_stuck_state(*, grabbed_stale_hours: int = 6,
                        queue_stale_days: int = 30,
                        max_rows_per_sweep: int = 500) -> dict:
    """Reconcile the three stuck-state patterns the app can otherwise
    accumulate indefinitely:

      1. Volumes in status='grabbed' whose grabbed_at is older than
         ``grabbed_stale_hours`` and whose download_id is NULL. These
         got stranded when a client crash lost the download_id before
         the volume row was fully updated. Reset to 'wanted' so the
         series can pick them back up.

      2. pending_releases whose series has been deleted or unmonitored
         since the release was queued. No legitimate grab will fire
         for these, but the auto-prune only removes rows >7 days old
         — leaving a long tail of junk in the queue UI.

      3. import_queue rows stuck in status='pending' or 'partial' for
         more than ``queue_stale_days`` days. Mark them failed so the
         next periodic reconcile can return the associated volumes
         to 'wanted'.

    Every destructive action is logged via `log_event` so operators
    can see what moved. The ``max_rows_per_sweep`` cap exists as a
    safety valve against a bad filter matching the whole table — if
    we ever hit it, the next sweep picks up the rest.

    Returns a dict of counts for visibility in tests and logs.
    """
    # One transaction per phase — not one big transaction for all three.
    # Each phase might process hundreds of rows; keeping each phase its
    # own transaction lets other writers slot in between. The stats dict
    # is accumulated across phases at function scope.
    stats = {
        'volumes_reset':   0,
        'pending_deleted': 0,
        'queue_failed':    0,
    }

    # ── Phase 1: stale grabbed volumes ──
    with get_db() as db:
        # (1) Stale grabbed volumes with no download_id
        stale = db.execute(
            "SELECT v.id, v.series_id, v.volume_num, s.title"
            "  FROM volumes v LEFT JOIN series s ON s.id=v.series_id"
            " WHERE v.status='grabbed' AND v.download_id IS NULL"
            "   AND v.grabbed_at IS NOT NULL"
            "   AND v.grabbed_at < datetime('now', ?)"
            "   AND (v.client IS NULL OR v.client != 'suwayomi')"
            " LIMIT ?",
            (f'-{int(grabbed_stale_hours)} hours', max_rows_per_sweep)
        ).fetchall()
        for row in stale:
            db.execute(
                "UPDATE volumes SET status='wanted', grabbed_at=NULL,"
                " source_url=NULL, download_id=NULL, torrent_name=NULL,"
                " indexer=NULL, protocol=NULL, client=NULL, release_group=NULL,"
                " imported_at=NULL WHERE id=?",
                (row['id'],)
            )
            stats['volumes_reset'] += 1
        if stale:
            log_event(
                'stuck_cleanup',
                f'reset {len(stale)} stale grabbed-with-no-download_id volume(s) '
                f'(older than {grabbed_stale_hours}h)',
                db=db,
            )

    # ── Phase 2: pending_releases orphans ──
    with get_db() as db:
        orphans = db.execute(
            "SELECT pr.id, pr.series_id, pr.title, s.monitored"
            "  FROM pending_releases pr"
            "  LEFT JOIN series s ON s.id=pr.series_id"
            " WHERE s.id IS NULL OR s.monitored=0"
            " LIMIT ?",
            (max_rows_per_sweep,)
        ).fetchall()
        if orphans:
            db.execute(
                "DELETE FROM pending_releases WHERE id IN ("
                + ','.join('?' * len(orphans)) + ")",
                tuple(o['id'] for o in orphans)
            )
            stats['pending_deleted'] = len(orphans)
            log_event(
                'stuck_cleanup',
                f'deleted {len(orphans)} pending_release(s) for deleted or '
                f'unmonitored series',
                db=db,
            )

    # ── Phase 3: import_queue stuck in pending/partial ──
    with get_db() as db:
        stale_queue = db.execute(
            "SELECT id, series_id, torrent_name"
            "  FROM import_queue"
            " WHERE status IN ('pending', 'partial')"
            "   AND created_at < datetime('now', ?)"
            " LIMIT ?",
            (f'-{int(queue_stale_days)} days', max_rows_per_sweep)
        ).fetchall()
        for row in stale_queue:
            db.execute(
                "UPDATE import_queue SET status='failed' WHERE id=?",
                (row['id'],)
            )
            # Return any grabbed volumes associated via download_id back to wanted
            db.execute(
                "UPDATE volumes SET status='wanted', grabbed_at=NULL,"
                " download_id=NULL, torrent_name=NULL, indexer=NULL,"
                " protocol=NULL, client=NULL, release_group=NULL"
                " WHERE download_id IN ("
                "   SELECT download_id FROM import_queue WHERE id=?"
                " ) AND status='grabbed'",
                (row['id'],)
            )
            stats['queue_failed'] += 1
        if stale_queue:
            log_event(
                'stuck_cleanup',
                f'failed {len(stale_queue)} import_queue row(s) stuck in '
                f'pending/partial for >{queue_stale_days} days',
                db=db,
            )

    return stats


async def _stuck_state_cleanup_loop():
    """Run cleanup_stuck_state hourly. Kept separate from backlog_search_loop
    so a failure in one doesn't hide the other."""
    await asyncio.sleep(300)   # let startup settle and the boot-time one-shot finish
    while True:
        try:
            stats = cleanup_stuck_state()
            if any(stats.values()):
                print(f"[stuck-cleanup] {stats}")
        except Exception as e:
            print(f"[stuck-cleanup] error: {e}")
        await asyncio.sleep(3600)   # 1 hour


async def backlog_search_loop():
    """Daily: actively search for all wanted volumes that RSS may have missed."""
    await asyncio.sleep(600)   # initial delay — let startup settle
    while True:
        try:
            interval = 86400  # 24 hours
            ddl_only  = get_cfg('ddl_grab_mode', 'fallback') == 'only'
            with get_db() as db:
                wanted_series = db.execute(
                    "SELECT DISTINCT s.id, s.title, s.search_pattern, s.mangadex_id FROM series s"
                    " JOIN volumes v ON v.series_id=s.id"
                    " WHERE s.monitored=1 AND v.status='wanted'"
                    " AND s.deleted_at IS NULL"
                ).fetchall()
            searched = 0
            if ddl_only:
                from routers.suwayomi_ import _get_series_source
            for s in wanted_series:
                # In DDL-only mode, skip indexer search for series tracked via Suwayomi/MangaDex
                if ddl_only and _get_series_source(s['id'], dict(s)):
                    continue
                try:
                    grabbed = await grab_existing(s['id'], s['title'], s['search_pattern'])
                    if grabbed:
                        searched += grabbed
                except Exception as e:
                    import traceback
                    print(f"[Backlog] Error searching {s['title']}: {e}")
                    print(traceback.format_exc())
                await asyncio.sleep(2)  # rate-limit: ~0.5 series/sec
            if wanted_series:
                log_event('backlog_search', f"Backlog search complete: {len(wanted_series)} series, {searched} grabbed")
        except Exception as e:
            print(f"[Backlog] Error: {e}")
        from routers.system import update_task_state
        now = datetime.now(timezone.utc)
        update_task_state('BacklogSearch', last_run=now,
                          next_run=datetime.fromtimestamp(now.timestamp() + interval, tz=timezone.utc))
        await asyncio.sleep(interval)


async def backlog_search():
    """One-shot backlog search — search all wanted volumes once. Used by task scheduler 'Run Now'."""
    with get_db() as db:
        wanted_series = db.execute(
            "SELECT DISTINCT s.id, s.title, s.search_pattern FROM series s"
            " JOIN volumes v ON v.series_id=s.id"
            " WHERE s.monitored=1 AND v.status='wanted'"
            " AND s.deleted_at IS NULL"
        ).fetchall()
    searched = 0
    for s in wanted_series:
        try:
            grabbed = await grab_existing(s['id'], s['title'], s['search_pattern'])
            if grabbed:
                searched += grabbed
        except Exception as e:
            print(f"[Backlog] Error searching {s['title']}: {e}")
        await asyncio.sleep(2)
    if wanted_series:
        log_event('backlog_search', f"Backlog search: {len(wanted_series)} series, {searched} grabbed")


async def import_list_sync():
    """One-shot import list sync — sync all enabled import lists once. Used by task scheduler 'Run Now'."""
    try:
        from routers.import_lists import _sync_all_lists as _do_sync
        await _do_sync()
        log_event('import_list_sync', "Import list sync completed")
    except Exception as e:
        log_event('error', f"Import list sync failed: {e}")
        print(f"[ImportListSync] {e}")


async def rescan_loop():
    """Periodic library rescan — walks all series folders and reconciles on-disk state."""
    from routers.series_ import _rescan_all_impl  # noqa: WPS433 (lazy to avoid cycle)
    interval_h = int(get_cfg('rescan_interval_hours', '12'))
    # Delay first run so startup tasks finish before hammering disk
    await asyncio.sleep(interval_h * 3600)
    while True:
        try:
            await _rescan_all_impl()
        except Exception as e:
            log_event('error', f"Periodic rescan error: {e}")
        await asyncio.sleep(interval_h * 3600)


async def _import_list_loop():
    """Periodic import list sync — runs every 12 hours."""
    await asyncio.sleep(300)  # 5 min delay after startup
    while True:
        try:
            from routers.import_lists import _sync_all_lists
            await _sync_all_lists()
            log_event('import_list_sync', "Scheduled import list sync completed")
        except Exception as e:
            log_event('error', f"Import list sync error: {e}")
        from routers.system import update_task_state
        now = datetime.now(timezone.utc)
        update_task_state('ImportListSync', last_run=now,
                          next_run=datetime.fromtimestamp(now.timestamp() + 43200, tz=timezone.utc))
        await asyncio.sleep(43200)  # 12 hours


async def _backup_loop():
    """Auto-backup — interval and retention controlled by settings."""
    from main import DB_PATH  # noqa: WPS433 (lazy to avoid cycle)
    from routers.system import BACKUP_DIR, update_task_state
    await asyncio.sleep(3600)  # 1h delay after startup
    while True:
        interval_days = max(1, min(30, int(get_cfg('backup_interval_days', '1') or 1)))
        retention     = max(1, min(30, int(get_cfg('backup_retention',     '7') or 7)))
        try:
            os.makedirs(BACKUP_DIR, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fname = f"mangarr_auto_{ts}.zip"
            fpath = os.path.join(BACKUP_DIR, fname)
            with zipfile.ZipFile(fpath, 'w', zipfile.ZIP_DEFLATED) as zf:
                zf.write(DB_PATH, "mangarr.db")
            # Keep only last N auto backups
            auto_backups = sorted(
                [f for f in os.listdir(BACKUP_DIR)
                 if f.startswith('mangarr_auto_') and f.endswith('.zip')],
                reverse=True
            )
            for old in auto_backups[retention:]:
                try:
                    os.remove(os.path.join(BACKUP_DIR, old))
                except Exception:
                    pass
            now = datetime.now(timezone.utc)
            update_task_state('Backup', last_run=now,
                              next_run=datetime.fromtimestamp(now.timestamp() + interval_days * 86400, tz=timezone.utc))
            log_event('backup', f"Auto-backup created: {fname} (retaining last {retention})")
        except Exception as e:
            log_event('error', f"Auto-backup failed: {e}")
        await asyncio.sleep(interval_days * 86400)


# ── Recycle-bin reaper (PR-3 of the recycle-bin epic) ────────────────────────
# Hard-deletes series whose `deleted_at` is older than the configured
# retention period. Runs every 6 hours so the "X days remaining" UI on
# the recycle-bin page stays roughly accurate without spamming.

def _run_recycle_bin_purge_once(*, retention_days: int | None = None) -> int:
    """Hard-delete every series whose deleted_at is older than the
    retention period. Returns the count of purged series. Extracted
    from the loop body so the reaper can be exercised synchronously
    in tests without spinning up the asyncio task."""
    from shared import get_db
    from routers.series_ import _hard_delete_series
    if retention_days is None:
        try:
            retention_days = max(1, int(get_cfg('recycle_bin_retention_days', '30')))
        except (TypeError, ValueError):
            retention_days = 30
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    purged = 0
    with get_db() as db:
        expired = db.execute(
            "SELECT id, title FROM series"
            " WHERE deleted_at IS NOT NULL AND deleted_at < ?",
            (cutoff.isoformat(sep=' ', timespec='seconds'),)
        ).fetchall()
        for row in expired:
            try:
                _hard_delete_series(db, row['id'], log_history=True)
                purged += 1
            except Exception as e:
                log_event('error', f"Recycle-bin purge failed for series {row['id']}: {e}")
    return purged


async def _recycle_bin_reaper_loop():
    """Background loop: every 6 hours, purge any soft-deleted series
    older than the retention window. Slot into the existing background
    scheduler in `main.py`."""
    from routers.system import update_task_state
    await asyncio.sleep(900)  # 15 min after startup
    while True:
        try:
            purged = _run_recycle_bin_purge_once()
            if purged:
                log_event('recycle_bin_purge', f"Purged {purged} series from recycle bin")
            now = datetime.now(timezone.utc)
            update_task_state(
                'RecycleBinPurge',
                last_run=now,
                next_run=datetime.fromtimestamp(now.timestamp() + 6 * 3600, tz=timezone.utc),
            )
        except Exception as e:
            log_event('error', f"Recycle-bin reaper error: {e}")
        await asyncio.sleep(6 * 3600)  # 6h


# ── Background task lifecycle ────────────────────────────────────────────────
# All long-running asyncio loops (rss, status, refresh, backfill, backlog,
# suwayomi, rescan, import-list, backup, stuck-retry) are registered here so
# lifespan shutdown can cancel them, and so an unexpected exit from one
# surfaces in the log instead of silently dying.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def create_background_task(coro, name: str) -> asyncio.Task:
    """Start a long-running background task and track its lifecycle.

    - Names the task (visible in `asyncio.all_tasks()`).
    - Stores a strong reference so Python's GC doesn't collect it mid-run
      (raw asyncio.create_task() emits a "Task was destroyed but it is
      pending" warning if the return value isn't held).
    - Removes the reference when the task finishes.
    - Logs (warning-level) if the task exited via an uncaught exception.
      Clean cancellation on shutdown is silent.
    """
    import logging as _logging
    log = _logging.getLogger(__name__)

    task = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _BACKGROUND_TASKS.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            log.error("background task %r exited with exception: %r",
                      t.get_name(), exc, exc_info=exc)

    task.add_done_callback(_on_done)
    return task


async def _cancel_background_tasks() -> None:
    """Cancel every registered background task and await graceful exit.

    Called from lifespan shutdown. Uses return_exceptions so one slow task
    doesn't starve the others; each task's final state is logged by its
    own done-callback.
    """
    tasks = list(_BACKGROUND_TASKS)
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
