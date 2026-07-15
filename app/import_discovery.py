"""Download client discovery: poll qBittorrent/SABnzbd for completed downloads."""

import asyncio

import httpx

from shared import get_cfg, get_db
from events import add_history, log_event
from routers.download_clients import get_client_for_protocol, apply_remote_path_mapping
from import_queue import _queue_import


# Single-flight guard for check_download_status. Evidence from issue #31
# follow-up A: the function's body takes 7-38s per run and was being
# spawned concurrently (up to 4× at once) from:
#   - status_loop (every 5 min)
#   - /api/check-downloads button
#   - /api/backfill-packs / system endpoints
# Overlapping runs amplify event-loop blocking and DB write contention.
# When one run is in flight, additional invocations are no-ops — the
# in-flight run will pick up whatever new state the caller cared about.
_CHECK_DOWNLOAD_STATUS_LOCK = asyncio.Lock()


async def check_download_status():
    """Poll download clients for completed downloads and queue them for import review.

    Skips if another invocation is still running (single-flight). Callers
    that need guaranteed execution should await a completed call instead
    of firing-and-forgetting via asyncio.create_task.
    """
    from shared import timed_block as _tb

    if _CHECK_DOWNLOAD_STATUS_LOCK.locked():
        return
    async with _CHECK_DOWNLOAD_STATUS_LOCK:
        with _tb("check_download_status"):
            return await _check_download_status_impl()


async def _check_download_status_impl():
    """Inner body (wrapped for timing instrumentation)."""
    from routers import suwayomi_ as _swy_router

    # Clean up stale imported/failed entries older than 7 days
    with get_db() as _cdb:
        _cdb.execute(
            "DELETE FROM import_queue_files WHERE queue_id IN ("
            "  SELECT id FROM import_queue WHERE status IN ('imported','skipped')"
            "  AND created_at < datetime('now', '-7 days'))"
        )
        _cdb.execute(
            "DELETE FROM import_queue WHERE status IN ('imported','skipped')"
            " AND created_at < datetime('now', '-7 days')"
        )

    # Auto-prune expired blocklist entries
    _bl_ttl = max(0, int(get_cfg("blocklist_ttl_days", "90") or "90"))
    if _bl_ttl > 0:
        with get_db() as _bldb:
            _bl_deleted = _bldb.execute(
                "DELETE FROM blocklist WHERE added_at < datetime('now', ? || ' days')",
                (f"-{_bl_ttl}",),
            ).rowcount
            if _bl_deleted > 0:
                log_event(
                    "info",
                    f"Auto-pruned {_bl_deleted} expired blocklist entr{'ies' if _bl_deleted != 1 else 'y'}",
                    db=_bldb,
                )

    # Auto-reset grabbed volumes that are stuck (no activity for >2 days)
    with get_db() as _stuckdb:
        _stuck_count = _stuckdb.execute(
            "UPDATE volumes SET status='wanted', grabbed_at=NULL, download_id=NULL,"
            " source_url=NULL, torrent_name=NULL, indexer=NULL, protocol=NULL,"
            " client=NULL, release_group=NULL, import_path=NULL"
            " WHERE status='grabbed'"
            "   AND grabbed_at < datetime('now', '-2 days')"
            "   AND NOT EXISTS ("
            "     SELECT 1 FROM import_queue iq WHERE iq.download_id = volumes.download_id"
            "     AND iq.status IN ('pending','partial')"
            "   )"
        ).rowcount
        if _stuck_count > 0:
            log_event(
                "info",
                f"Auto-reset {_stuck_count} stuck grabbed volume(s) back to wanted",
                db=_stuckdb,
            )

    # Auto-retry import_queue entries stuck in pending/partial > 2 hours
    with get_db() as _iq_db:
        stuck_pending = _iq_db.execute(
            "SELECT id FROM import_queue"
            " WHERE status IN ('pending','partial')"
            " AND created_at < datetime('now', '-2 hours')"
            " AND NOT EXISTS ("
            "   SELECT 1 FROM import_queue_files f"
            "   WHERE f.queue_id=import_queue.id AND f.status='needs_review'"
            " )"
        ).fetchall()
        stuck_ids = [r["id"] for r in stuck_pending]
    if stuck_ids:
        for _sid in stuck_ids:
            asyncio.create_task(_process_auto_import(_sid))

    # ── qBittorrent ──────────────────────────────────────────────────────────
    with get_db() as _cdb:
        _qc = get_client_for_protocol(_cdb, "torrent")
    host = ((_qc or {}).get("host") or "").rstrip("/")
    user = (_qc or {}).get("username") or ""
    pw = (_qc or {}).get("password") or ""
    cat = (_qc or {}).get("category") or get_cfg("category")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{host}/api/v2/auth/login", data={"username": user, "password": pw}
            )
            if "Ok" in r.text:
                r2 = await client.get(
                    f"{host}/api/v2/torrents/info", params={"category": cat}
                )
                if r2.status_code == 200:
                    all_torrents = r2.json()
                    all_hashes = {t["hash"].lower() for t in all_torrents}
                    completed = [t for t in all_torrents if t.get("progress", 0) >= 1.0]
                    torrent_by_hash = {t["hash"].lower(): t for t in completed}
                    completed_names = {normalize(t["name"]): t for t in completed}

                    def _process_qbit_completed():
                        with get_db() as db:
                            rows = db.execute(
                                "SELECT torrent_url, torrent_name, series_id, volume_num, download_id "
                                "FROM seen WHERE client='qbittorrent' AND protocol='torrent'"
                            ).fetchall()

                        matched = _deduplicate_qbit_matches(
                            rows, torrent_by_hash, completed_names
                        )

                        _new_imports = []
                        for row, torrent, dl_id in matched:
                            content_path = torrent.get("content_path") or torrent.get(
                                "save_path", ""
                            )
                            with get_db() as db:
                                content_path = apply_remote_path_mapping(
                                    db, content_path, host
                                )
                                q_id, needs_review = _queue_import(
                                    db,
                                    row["series_id"],
                                    dl_id,
                                    row["torrent_name"] or "",
                                    row["torrent_url"] or "",
                                    row["volume_num"],
                                    content_path,
                                )
                            if q_id and not needs_review:
                                _new_imports.append(q_id)
                        return _new_imports

                    _new_imports = await asyncio.to_thread(_process_qbit_completed)
                    for _imp_id in _new_imports:
                        asyncio.create_task(_process_auto_import(_imp_id))

                    def _qbit_orphan_cleanup_sync():
                        # Phase A (quick): bulk-reset grabbed-without-download_id
                        with get_db() as db:
                            db.execute(
                                "UPDATE volumes SET status='wanted', grabbed_at=NULL, source_url=NULL,"
                                " download_id=NULL, torrent_name=NULL, indexer=NULL, protocol=NULL,"
                                " client=NULL, release_group=NULL"
                                " WHERE status='grabbed' AND download_id IS NULL AND volume_num IS NOT NULL"
                            )
                            db.execute(
                                "DELETE FROM volumes WHERE status='grabbed'"
                                " AND download_id IS NULL AND volume_num IS NULL"
                            )

                        # Phase B (enumerate): find orphans at qBittorrent
                        with get_db() as db:
                            orphaned = db.execute(
                                "SELECT DISTINCT v.download_id, v.series_id,"
                                " COALESCE(sv.torrent_name, v.torrent_name) as torrent_name "
                                "FROM volumes v "
                                "LEFT JOIN seen sv ON sv.download_id = v.download_id "
                                "WHERE v.status='grabbed' "
                                "  AND v.client='qbittorrent' "
                                "  AND v.download_id IS NOT NULL "
                                "  AND v.download_id NOT IN ("
                                "    SELECT download_id FROM import_queue"
                                "    WHERE status='pending' AND download_id IS NOT NULL)"
                            ).fetchall()
                            orphaned = [dict(r) for r in orphaned]

                        # Phase C (per-orphan): individual processing
                        for gs in orphaned:
                            if (gs["download_id"] or "").lower() in all_hashes:
                                continue
                            h = gs["download_id"]
                            with get_db() as db:
                                orphan_vol_ids = [
                                    r[0]
                                    for r in db.execute(
                                        "SELECT id FROM volumes WHERE series_id=? AND download_id=?"
                                        " AND status='grabbed' AND volume_num IS NOT NULL",
                                        (gs["series_id"], h),
                                    ).fetchall()
                                ]
                                db.execute(
                                    "DELETE FROM volumes WHERE series_id=? AND download_id=?"
                                    " AND status='grabbed' AND volume_num IS NULL",
                                    (gs["series_id"], h),
                                )
                                db.execute(
                                    "UPDATE volumes SET status='wanted', download_id=NULL,"
                                    " torrent_name=NULL, indexer=NULL, protocol=NULL, client=NULL,"
                                    " grabbed_at=NULL, source_url=NULL, release_group=NULL "
                                    "WHERE series_id=? AND download_id=? AND status='grabbed'",
                                    (gs["series_id"], h),
                                )
                                if orphan_vol_ids:
                                    from volumes import _cascade_chapters

                                    _cascade_chapters(
                                        db,
                                        gs["series_id"],
                                        orphan_vol_ids,
                                        "wanted",
                                        grabbed_at=None,
                                        torrent_name=None,
                                        torrent_url=None,
                                        indexer=None,
                                        protocol=None,
                                        client=None,
                                        download_id=None,
                                        release_group=None,
                                    )
                                db.execute(
                                    "UPDATE import_queue SET status='skipped' "
                                    "WHERE download_id=? AND status='pending'",
                                    (h,),
                                )
                                db.execute(
                                    "UPDATE import_queue_files SET status='skipped' "
                                    "WHERE queue_id IN "
                                    "(SELECT id FROM import_queue WHERE download_id=?)",
                                    (h,),
                                )
                                db.execute("DELETE FROM seen WHERE download_id=?", (h,))
                                log_event(
                                    "warning",
                                    f"Grab lost (removed from client): {gs['torrent_name']}",
                                    gs["series_id"],
                                    db=db,
                                )
                                _sr = db.execute(
                                    "SELECT title FROM series WHERE id=?",
                                    (gs["series_id"],),
                                ).fetchone()
                                add_history(
                                    db,
                                    "grab_failed",
                                    gs["series_id"],
                                    _sr["title"] if _sr else "",
                                    "",
                                    source_title=gs["torrent_name"] or "",
                                    download_id=h,
                                    data={"reason": "removed_from_client"},
                                )

                    await asyncio.to_thread(_qbit_orphan_cleanup_sync)

                    if get_cfg("failed_download_handling", "0") == "1":
                        all_torrent_by_hash = {
                            t["hash"].lower(): t for t in all_torrents
                        }
                        error_states = {"error", "missingFiles", "stalledDL"}
                        with get_db() as _fdb:
                            seen_rows = _fdb.execute(
                                "SELECT download_id, series_id, torrent_name, torrent_url"
                                " FROM seen WHERE client='qbittorrent' AND protocol='torrent'"
                            ).fetchall()
                        for row in seen_rows:
                            h_fail = (row["download_id"] or "").lower()
                            if not h_fail:
                                continue
                            torrent_fail = all_torrent_by_hash.get(h_fail)
                            if (
                                torrent_fail
                                and torrent_fail.get("state", "") in error_states
                            ):

                                def _mark_failed_sync(r=row, tf=torrent_fail, h=h_fail):
                                    with get_db() as db:
                                        db.execute(
                                            "INSERT OR IGNORE INTO blocklist(series_id, torrent_url, torrent_name, reason)"
                                            " VALUES(?,?,?,?)",
                                            (
                                                r["series_id"],
                                                r["torrent_url"] or "",
                                                r["torrent_name"] or "",
                                                f"Download failed: {tf.get('state', 'error')}",
                                            ),
                                        )
                                        db.execute(
                                            "DELETE FROM volumes"
                                            " WHERE download_id=? AND status='grabbed'"
                                            "   AND volume_num IS NULL",
                                            (h,),
                                        )
                                        db.execute(
                                            "UPDATE volumes SET status='wanted', download_id=NULL, grabbed_at=NULL,"
                                            " source_url=NULL, torrent_name=NULL "
                                            "WHERE download_id=? AND status='grabbed'"
                                            "  AND volume_num IS NOT NULL",
                                            (h,),
                                        )
                                        db.execute(
                                            "DELETE FROM seen WHERE download_id=?", (h,)
                                        )

                                await asyncio.to_thread(_mark_failed_sync)
                                if (_qc or {}).get("remove_failed"):
                                    from clients import qbit_remove

                                    await qbit_remove(h_fail, delete_files=True)
                                log_event(
                                    "grab_failed",
                                    f"Auto-blacklisted failed download: {row['torrent_name']}",
                                    row["series_id"],
                                )
                                if get_cfg("redownload_failed_interactive", "0") != "1":
                                    from grab import grab_existing

                                    with get_db() as _rsdb:
                                        _rs = _rsdb.execute(
                                            "SELECT title, search_pattern FROM series WHERE id=?",
                                            (row["series_id"],),
                                        ).fetchone()
                                    if _rs:
                                        asyncio.create_task(
                                            grab_existing(
                                                row["series_id"],
                                                _rs["title"],
                                                _rs["search_pattern"] or "",
                                            )
                                        )
    except Exception as e:
        log_event("error", f"qBit status check failed: {e}")

    # ── SABnzbd ───────────────────────────────────────────────────────────────
    with get_db() as _cdb:
        _sc = get_client_for_protocol(_cdb, "nzb")
    sab_host = ((_sc or {}).get("host") or "").rstrip("/")
    sab_apikey = (_sc or {}).get("password") or ""
    if sab_apikey:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r_hist = await client.get(
                    f"{sab_host}/api",
                    params={
                        "mode": "history",
                        "limit": 100,
                        "apikey": sab_apikey,
                        "output": "json",
                    },
                )
                r_queue = await client.get(
                    f"{sab_host}/api",
                    params={
                        "mode": "queue",
                        "limit": 100,
                        "apikey": sab_apikey,
                        "output": "json",
                    },
                )

                sab_history_slots = []
                sab_queue_slots = []
                if r_hist.status_code == 200:
                    sab_history_slots = (
                        r_hist.json().get("history", {}).get("slots", [])
                    )
                if r_queue.status_code == 200:
                    sab_queue_slots = r_queue.json().get("queue", {}).get("slots", [])

                all_sab_nzo_ids: set[str] = {
                    s["nzo_id"] for s in sab_history_slots if s.get("nzo_id")
                } | {s["nzo_id"] for s in sab_queue_slots if s.get("nzo_id")}

                sab_by_nzo = {
                    s["nzo_id"]: s
                    for s in sab_history_slots
                    if s.get("status") == "Completed" and s.get("nzo_id")
                }

                _sab_new_queue_ids: list[int] = []

                def _sab_process_sync():
                    with get_db() as db:
                        rows = db.execute(
                            "SELECT torrent_url, torrent_name, series_id, volume_num, download_id "
                            "FROM seen WHERE client='sabnzbd'"
                        ).fetchall()
                        for row in rows:
                            if not row["download_id"]:
                                continue
                            slot = sab_by_nzo.get(row["download_id"])
                            if not slot:
                                continue
                            content_path = slot.get("storage", "")
                            content_path = apply_remote_path_mapping(
                                db, content_path, sab_host
                            )
                            q_id, needs_review = _queue_import(
                                db,
                                row["series_id"],
                                row["download_id"],
                                row["torrent_name"] or "",
                                row["torrent_url"] or "",
                                row["volume_num"],
                                content_path,
                            )
                            if q_id and not needs_review:
                                _sab_new_queue_ids.append(q_id)

                        sab_orphaned = db.execute(
                            "SELECT DISTINCT v.download_id, v.series_id,"
                            " COALESCE(sv.torrent_name, v.torrent_name) as torrent_name "
                            "FROM volumes v "
                            "LEFT JOIN seen sv ON sv.download_id = v.download_id "
                            "WHERE v.status='grabbed' "
                            "  AND v.client='sabnzbd' "
                            "  AND v.download_id IS NOT NULL "
                            "  AND v.download_id NOT IN ("
                            "    SELECT download_id FROM import_queue"
                            "    WHERE status='pending' AND download_id IS NOT NULL)"
                        ).fetchall()
                        for gs in sab_orphaned:
                            if gs["download_id"] in all_sab_nzo_ids:
                                continue
                            h_id = gs["download_id"]
                            orphan_vol_ids = [
                                r[0]
                                for r in db.execute(
                                    "SELECT id FROM volumes WHERE series_id=? AND download_id=?"
                                    " AND status='grabbed' AND volume_num IS NOT NULL",
                                    (gs["series_id"], h_id),
                                ).fetchall()
                            ]
                            db.execute(
                                "DELETE FROM volumes WHERE series_id=? AND download_id=?"
                                " AND status='grabbed' AND volume_num IS NULL",
                                (gs["series_id"], h_id),
                            )
                            db.execute(
                                "UPDATE volumes SET status='wanted', download_id=NULL,"
                                " torrent_name=NULL, indexer=NULL, protocol=NULL, client=NULL,"
                                " grabbed_at=NULL, source_url=NULL, release_group=NULL "
                                "WHERE series_id=? AND download_id=? AND status='grabbed'",
                                (gs["series_id"], h_id),
                            )
                            if orphan_vol_ids:
                                from volumes import _cascade_chapters

                                _cascade_chapters(
                                    db,
                                    gs["series_id"],
                                    orphan_vol_ids,
                                    "wanted",
                                    grabbed_at=None,
                                    torrent_name=None,
                                    torrent_url=None,
                                    indexer=None,
                                    protocol=None,
                                    client=None,
                                    download_id=None,
                                    release_group=None,
                                )
                            db.execute(
                                "UPDATE import_queue SET status='skipped' "
                                "WHERE download_id=? AND status='pending'",
                                (h_id,),
                            )
                            db.execute(
                                "UPDATE import_queue_files SET status='skipped' "
                                "WHERE queue_id IN "
                                "(SELECT id FROM import_queue WHERE download_id=?)",
                                (h_id,),
                            )
                            db.execute("DELETE FROM seen WHERE download_id=?", (h_id,))
                            log_event(
                                "warning",
                                f"SAB grab lost (removed from client): {gs['torrent_name']}",
                                gs["series_id"],
                            )
                            _sr = db.execute(
                                "SELECT title FROM series WHERE id=?",
                                (gs["series_id"],),
                            ).fetchone()
                            add_history(
                                db,
                                "grab_failed",
                                gs["series_id"],
                                _sr["title"] if _sr else "",
                                "",
                                source_title=gs["torrent_name"] or "",
                                download_id=h_id,
                                data={"reason": "removed_from_client"},
                            )

                await asyncio.to_thread(_sab_process_sync)
                for _sqid in _sab_new_queue_ids:
                    asyncio.create_task(_process_auto_import(_sqid))
        except Exception as e:
            log_event("error", f"SABnzbd status check failed: {e}")

    # ── Suwayomi ─────────────────────────────────────────────────────────────
    try:
        await _swy_router.check_suwayomi_jobs()
    except Exception as e:
        log_event("error", f"Suwayomi status check failed: {e}")


async def _process_auto_import(queue_id: int):
    """Auto-import a queue item where all files mapped cleanly (no review needed).

    This is re-exported from import_execute but kept here for backwards compatibility
    with import_pipeline.
    """
    from import_execute import _process_auto_import as _pap

    return await _pap(queue_id)


def normalize(text: str) -> str:
    """Normalize text for comparison (lowercase, strip)."""
    return (text or "").lower().strip()


def _deduplicate_qbit_matches(rows, torrent_by_hash, completed_names):
    """Match seen rows to completed torrents once per series and hash."""
    matched = []
    matched_keys = set()
    for row in rows:
        seen_download_id = (row["download_id"] or "").lower()
        name_norm = normalize(row["torrent_name"] or "")
        torrent = torrent_by_hash.get(seen_download_id) or completed_names.get(
            name_norm
        )
        if not torrent:
            continue
        download_id = str(torrent.get("hash") or seen_download_id).lower()
        identity = download_id or normalize(torrent.get("name") or "")
        key = (row["series_id"], identity)
        if key in matched_keys:
            continue
        matched_keys.add(key)
        matched.append((row, torrent, download_id))
    return matched
