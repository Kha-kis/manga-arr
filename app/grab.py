"""Grab layer: send releases to download clients and record the state.

Sixteenth module extracted from main.py. Pulls out the synchronous
"decide, persist, notify" pipeline that sits between indexer results
and the download client:

  rejection logging
    _log_grab_rejection         — writes a `rejected_release` event
                                  for the filter decisions operators
                                  care about; silently skips high-
                                  frequency guards (dedup, coverage)
                                  so the events table doesn't flood.

  in-flight dedup
    _GRABBING_URLS              — module-level set of URLs currently
                                  between the `seen` check and the
                                  `INSERT INTO seen` — asyncio is
                                  single-threaded so plain set ops
                                  are safe between awaits. Prevents
                                  duplicate grabs when RSS poll and
                                  backlog search both pass the
                                  `seen` check before either COMMIT
                                  completes.

  core pipeline
    grab_item                   — full grab: dedup, blocklist, monitor
                                  mode, edition filter, min-seeders,
                                  coverage check, quality cutoff,
                                  repack/proper handling, send to
                                  client, persist, cascade to
                                  chapters, fire notifications.
    _collect_and_score          — dedupe + score a batch of items,
                                  enforces quality-definition size
                                  bounds.
    _search_all                 — run all enabled indexers for a
                                  title, dedupe, sort by score desc.

  backlog search
    grab_existing               — thin exception-safe wrapper around
                                  _grab_existing_inner.
    _grab_existing_inner        — full-title backlog search: tries a
                                  complete-pack-first path for
                                  FINISHED series missing ≥50% of
                                  volumes, then falls back to per-
                                  release matching across the main
                                  title and aliases.
    _select_covering_packs      — greedy non-overlapping selection
                                  that maximises coverage of the
                                  missing-volumes set. Sorted by
                                  coverage desc, then seeders.
    search_complete_pack        — complete-pack search with alias
                                  widening and volume gap-fill.

  RSS polling
    poll_rss                    — pull every enabled indexer's RSS,
                                  match against series patterns /
                                  aliases, apply delay profiles,
                                  and grab on the fly.

Cross-module deps that would form cycles (main.log_event,
main.add_history, main.broadcast_queue_event) are imported lazily
inside function bodies — same pattern as the prior extractions.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import re
from datetime import datetime

from clients import grab_url
from files import (
    build_volume_label,
    detect_edition_type,
    detect_language,
    detect_quality_from_title,
    parse_release_group,
    parse_revision,
    quality_from_filename,
    quality_rank,
)
from metadata_enrichment import _coverage_already_grabbed, chapters_to_volume_set
from notifications import make_grab_embed, notify_discord
from parsing import (
    detect_pack_type,
    extract_volume_num,
    extract_volume_range,
    is_complete_pack,
    is_foreign_language,
    matches,
    normalize,
)
from evaluation import score_release
from shared import get_cfg, get_db
from volumes import _cascade_chapters


# URLs currently in-flight to a download client. asyncio is single-threaded so
# plain set ops are safe between awaits. Prevents duplicate grabs when RSS poll
# and backlog search both pass the `seen` check before either INSERT completes.
_GRABBING_URLS: set[str] = set()


def _log_grab_rejection(series_id: int, title: str, reason: str) -> None:
    """Surface a grab rejection as a `rejected_release` event.

    Called from `grab_item` on rejection paths that represent real
    filtering decisions (blocklist, edition mismatch, cross-group
    repack, quality cutoff). Normal-flow deduplication (seen, in-flight
    dedup) is NOT logged here — those aren't rejections, just guards,
    and logging them would flood the events table on every RSS poll.

    Also skipped: high-frequency informational rejections that repeat
    every poll for a stable reason (coverage-already-satisfied,
    score-too-low, not-an-upgrade). Those would drown the debugging
    signal from the rare rejections operators actually care about.
    """
    from main import log_event  # noqa: WPS433 (lazy to avoid cycle)
    try:
        log_event('rejected_release',
                  f'{reason}: {title[:120]}',
                  series_id)
    except Exception:
        pass  # logging must never break grab


async def grab_item(item: dict, series_id: int, respect_monitoring: bool = True) -> bool:
    """
    Send item to download client and record. Returns True on success.
    respect_monitoring=False bypasses per-volume and series monitor_mode checks
    (used for manual interactive grabs).
    """
    from main import add_history, log_event  # noqa: WPS433 (lazy to avoid cycle)
    from main import broadcast_queue_event  # noqa: WPS433 (lazy to avoid cycle)

    title    = item['title']
    indexer  = item.get('indexer', 'Unknown')
    protocol = item.get('protocol', 'torrent')

    # Check seen (already grabbed) — must be a live DB query, not a cached set,
    # to guard against concurrent grabs (RSS poll + manual, or overlapping polls).
    with get_db() as db:
        if db.execute("SELECT 1 FROM seen WHERE torrent_url=?", (item['url'],)).fetchone():
            return False

    # In-flight dedup: all code between here and `await grab_url` is synchronous,
    # so a second coroutine that also passed the seen check above will see this
    # entry and bail before it can send a duplicate to the download client.
    if item['url'] in _GRABBING_URLS:
        return False
    _GRABBING_URLS.add(item['url'])

    # Check blocklist
    with get_db() as db:
        if db.execute("SELECT 1 FROM blocklist WHERE torrent_url=?", (item['url'],)).fetchone():
            _GRABBING_URLS.discard(item['url'])
            _log_grab_rejection(series_id, title, 'blocklisted')
            return False

    if respect_monitoring:
        # Check series monitor mode and edition type in one query
        with get_db() as db:
            s_mode_row = db.execute(
                "SELECT monitor_mode, edition_type FROM series WHERE id=?", (series_id,)
            ).fetchone()
        mode = (s_mode_row['monitor_mode'] if s_mode_row else None) or 'all'
        if mode == 'none':
            return False
        # Edition-type filter: series must only grab releases of its own edition.
        # Standard series skips colored/omnibus/deluxe etc. Non-standard series
        # skips standard (B&W) and other editions.
        _series_edition  = (s_mode_row['edition_type'] if s_mode_row else None) or 'standard'
        _release_edition = detect_edition_type(title) or 'standard'
        if _series_edition != _release_edition:
            _log_grab_rejection(
                series_id, title,
                f'edition mismatch (series={_series_edition}, release={_release_edition})'
            )
            return False

    # Minimum seeders check for torrents
    if item.get('protocol') == 'torrent':
        _min_seeds = int(get_cfg('min_seeders', '0') or '0')
        if _min_seeds > 0 and (item.get('seeders') or 0) < _min_seeds:
            return False

    # Parse item type early so we can pass volume_num into score_release
    vol_num = extract_volume_num(title)
    vol_rng = extract_volume_range(title)  # always check for range
    if vol_rng is not None:
        vol_num = None  # multi-volume range takes precedence over single number

    # Fetch pub_year for year-match bonus in scoring
    _pub_year: int | None = None
    try:
        with get_db() as _py_db:
            _py_row = _py_db.execute(
                "SELECT pub_year FROM series WHERE id=?", (series_id,)
            ).fetchone()
            if _py_row and _py_row['pub_year']:
                _pub_year = int(_py_row['pub_year'])
    except Exception:
        pass

    # Per-series scoring: apply language profile, release profiles, CF scoring,
    # and volume/year match bonuses for better disambiguation.
    _series_sc = score_release(title, series_id,
                               release_group=item.get('release_group', ''),
                               indexer=item.get('indexer', ''),
                               volume_num=vol_num,
                               pub_year=_pub_year)
    if _series_sc <= -900:
        return False

    # Per-volume monitoring check (single volume)
    if respect_monitoring and vol_num is not None:
        with get_db() as db:
            vol_mon = db.execute(
                "SELECT monitored FROM volumes WHERE series_id=? AND volume_num=?",
                (series_id, vol_num)
            ).fetchone()
        if vol_mon and vol_mon['monitored'] == 0:
            return False

    # Pack monitoring check: reject the entire pack (RSS sync) if no
    # MAINLINE wanted+monitored volumes are in the range — mirrors Sonarr's
    # MonitoredEpisodeSpecification. Specials don't count for a mainline
    # pack grab (a side-story marked monitored shouldn't make us grab an
    # unrelated mainline pack).
    if respect_monitoring and vol_num is None and vol_rng is not None:
        with get_db() as db:
            has_monitored = db.execute(
                "SELECT 1 FROM volumes WHERE series_id=? AND status='wanted' AND monitored=1"
                " AND volume_num >= ? AND volume_num <= ?"
                " AND COALESCE(is_special, 0) = 0 LIMIT 1",
                (series_id, vol_rng[0], vol_rng[1])
            ).fetchone()
        if not has_monitored:
            return False

    # Fetch series context (needed for coverage check and pack detection)
    with get_db() as db:
        s_row = db.execute(
            "SELECT title, total_volumes, total_chapters, chapter_vol_map, cover_url,"
            " root_folder_id, update_strategy FROM series WHERE id=?", (series_id,)
        ).fetchone()
        rf_row = db.execute(
            "SELECT rf.path FROM root_folders rf WHERE rf.id=?",
            (s_row['root_folder_id'],)
        ).fetchone() if s_row and s_row['root_folder_id'] else None

    total_vols = s_row['total_volumes'] if s_row else None
    total_chs  = s_row['total_chapters'] if s_row else None
    cover_url  = (s_row['cover_url'] or '') if s_row else ''
    # Let the download client use its own configured directory.
    # We query content_path from the client after completion for importing.
    save_path  = None
    ch_map: dict = {}
    if s_row and s_row['chapter_vol_map']:
        try:
            ch_map = json.loads(s_row['chapter_vol_map'])
        except Exception as e:
            print(f"[grab_item] chapter_vol_map parse failed: {e}")

    pack_type = detect_pack_type(title, vol_rng, total_vols) if vol_num is None else None
    complete  = (pack_type == 'complete')

    # ── Coverage check: skip if content already fully grabbed ─────────────────
    if vol_num is None and pack_type:
        # Determine chapter range for chapter packs
        ch_range = vol_rng if pack_type == 'chapter' else None
        if not ch_range and pack_type == 'chapter':
            m = re.search(r'(?:ch(?:apter)?s?\.?\s*|#\s*)(\d{1,4}(?:\.\d+)?)\b', title, re.IGNORECASE)
            if not m:
                m = re.search(r'(?:^|[\s\[({])(\d{2,4})(?:[\s\])}]|$)', title)
            if m:
                ch = float(m.group(1))
                ch_range = (ch, ch)
        if _coverage_already_grabbed(series_id, pack_type, vol_rng, ch_range, ch_map, total_chs, total_vols):
            print(f"[Grab] Skipping '{title[:60]}' — coverage already satisfied")
            return False
    elif vol_num is not None:
        # Single volume — skip if already grabbed or downloaded, UNLESS this is a quality upgrade
        with get_db() as db:
            existing_vol = db.execute(
                "SELECT status, torrent_name, quality, release_group FROM volumes "
                "WHERE series_id=? AND volume_num=? AND status != 'wanted'",
                (series_id, vol_num)
            ).fetchone()
        if existing_vol:
            if existing_vol['status'] == 'grabbed':
                return False  # already in flight
            # 'once' strategy: never upgrade — grab once and stop
            _strategy = (s_row['update_strategy'] or 'always') if s_row else 'always'
            if _strategy == 'once':
                return False  # already have it; no upgrades for 'once' series
            # ── Repack / Proper handling (Sonarr RepackSpecification) ──────────
            _revision   = parse_revision(title)
            _prop_cfg   = get_cfg('propers_and_repacks', 'prefer_and_upgrade')
            if _revision['is_repack']:
                if _prop_cfg == 'do_not_upgrade':
                    # Never auto-grab repacks of already-downloaded volumes
                    _log_grab_rejection(series_id, title,
                                        'repack skipped: propers_and_repacks=do_not_upgrade')
                    return False
                elif _prop_cfg == 'prefer_and_upgrade':
                    # Only grab if same release group (cross-group repacks rejected)
                    existing_group = (existing_vol['release_group'] or '').strip().lower()
                    new_group      = parse_release_group(title).lower()
                    if existing_group and new_group and existing_group != new_group:
                        _log_grab_rejection(
                            series_id, title,
                            f'cross-group repack rejected '
                            f'(existing={existing_group!r}, repack={new_group!r})'
                        )
                        return False
                # do_not_prefer: fall through — treat repack same as any release
            # Cutoff check first — if current quality already meets cutoff, no upgrades needed
            with get_db() as _cutoff_db:
                _s_cutoff = _cutoff_db.execute(
                    "SELECT quality_cutoff FROM series WHERE id=?", (series_id,)
                ).fetchone()
            cutoff = (_s_cutoff['quality_cutoff'] if _s_cutoff else None) or get_cfg('quality_cutoff', '')
            if cutoff and quality_rank(existing_vol['quality'] or '') >= quality_rank(cutoff):
                return False  # already at or above quality cutoff — no upgrade needed
            # For 'downloaded' volumes: allow if new release is strictly higher quality
            new_q = quality_from_filename(title)   # heuristic from release title extension
            old_q = existing_vol['quality']
            if quality_rank(new_q) > quality_rank(old_q):
                pass  # quality upgrade — allow grab
            else:
                # Same or unknown quality — fall back to score comparison
                new_score = score_release(title)
                old_score = score_release(existing_vol['torrent_name'] or '')
                if new_score <= old_score:
                    return False  # not an upgrade

    # Quality cutoff enforcement on initial grab — reject releases below the configured
    # minimum quality so we don't grab CBR when the series requires CBZ.
    # (Upgrades have their own cutoff check above; this handles the 'wanted' case.)
    if vol_num is not None:
        with get_db() as _q_db:
            _q_cutoff_row = _q_db.execute(
                "SELECT quality_cutoff FROM series WHERE id=?", (series_id,)
            ).fetchone()
        _cutoff = (_q_cutoff_row['quality_cutoff'] if _q_cutoff_row else None) or get_cfg('quality_cutoff', '')
        if _cutoff:
            _new_q = quality_from_filename(title)
            if _new_q and quality_rank(_new_q) < quality_rank(_cutoff):
                _log_grab_rejection(
                    series_id, title,
                    f'quality {_new_q} below cutoff {_cutoff}'
                )
                return False

    # Outer timeout on the grab operation as a whole. grab_url has its own
    # per-HTTP-request timeout (30s for qBittorrent, similar for SABnzbd),
    # but a slow client + retry logic can accumulate well past that. Without
    # this wrapper an indexer/client combination that hangs will pin the URL
    # in _GRABBING_URLS until the httpx timeout chains expire, blocking any
    # retry for minutes.
    try:
        try:
            ok, client_name, dl_id = await asyncio.wait_for(
                grab_url(item['url'], protocol, save_path=save_path,
                         torrent_name=title),
                timeout=45,
            )
        except asyncio.TimeoutError:
            log_event(
                'grab_timeout',
                f'grab_url exceeded 45s for {title[:120]}',
                series_id,
            )
            return False
    finally:
        _GRABBING_URLS.discard(item['url'])
    if not ok:
        return False

    now      = datetime.utcnow().isoformat()
    rgroup   = parse_release_group(title)
    size     = item.get('size_bytes', 0)
    edition  = detect_edition_type(title)
    lang     = item.get('language') or detect_language(title)

    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO seen"
            "(torrent_url, torrent_name, series_id, volume_num, grabbed_at,"
            " indexer, protocol, client, download_id, release_group, size_bytes)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (item['url'], title, series_id, vol_num, now,
             indexer, protocol, client_name, dl_id, rgroup, size)
        )

        _ch_cascade_kw = dict(grabbed_at=now, torrent_name=title, torrent_url=item['url'],
                              indexer=indexer, protocol=protocol, client=client_name,
                              download_id=dl_id, release_group=rgroup, size_bytes=size)

        if vol_num is not None:
            # ── Single volume ────────────────────────────────────────────────
            existing = db.execute(
                "SELECT id FROM volumes WHERE series_id=? AND volume_num=?",
                (series_id, vol_num)
            ).fetchone()
            if existing:
                db.execute(
                    "UPDATE volumes SET status='grabbed', grabbed_at=?, source_url=?,"
                    " download_id=?, torrent_name=?, client=?, indexer=?, protocol=?,"
                    " release_group=?, size_bytes=?, edition_type=?, language=? WHERE id=?",
                    (now, item['url'], dl_id, title, client_name,
                     indexer, protocol, rgroup, size, edition, lang, existing['id'])
                )
                _cascade_chapters(db, series_id, [existing['id']], 'grabbed', **_ch_cascade_kw)
            else:
                db.execute(
                    "INSERT INTO volumes(series_id, volume_num, status, grabbed_at,"
                    " source_url, download_id, torrent_name, client,"
                    " indexer, protocol, release_group, size_bytes, edition_type, language)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (series_id, vol_num, 'grabbed', now, item['url'], dl_id, title, client_name,
                     indexer, protocol, rgroup, size, edition, lang)
                )
                new_vol = db.execute(
                    "SELECT id FROM volumes WHERE series_id=? AND volume_num=?",
                    (series_id, vol_num)
                ).fetchone()
                if new_vol:
                    _cascade_chapters(db, series_id, [new_vol['id']], 'grabbed', **_ch_cascade_kw)
        else:
            # ── Pack/range/complete ──────────────────────────────────────────
            # For chapter packs the extracted range is a CHAPTER range, not a volume range
            if pack_type == 'chapter':
                store_rng_start = store_rng_end = None
            else:
                store_rng_start = vol_rng[0] if vol_rng else None
                store_rng_end   = vol_rng[1] if vol_rng else None

            # Record a single pack entry for reference
            db.execute(
                "INSERT OR IGNORE INTO volumes"
                "(series_id, status, grabbed_at, source_url, download_id,"
                " vol_range_start, vol_range_end, pack_type, torrent_name, client,"
                " indexer, protocol, release_group, size_bytes, edition_type, language)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (series_id, 'grabbed', now, item['url'], dl_id,
                 store_rng_start, store_rng_end, pack_type, title, client_name,
                 indexer, protocol, rgroup, size, edition, lang)
            )

            # Determine which volume stubs this pack covers
            covered_vols: set[int] = set()
            if complete:
                # Mark every wanted stub as grabbed (minimal tracking only)
                db.execute(
                    "UPDATE volumes SET status='grabbed', grabbed_at=?, source_url=?,"
                    " download_id=?, torrent_name=?, client=?, indexer=?, protocol=?,"
                    " release_group=?, size_bytes=?, edition_type=?, language=?"
                    " WHERE series_id=? AND status='wanted' AND volume_num IS NOT NULL",
                    (now, item['url'], dl_id, title, client_name,
                     indexer, protocol, rgroup, size, edition, lang, series_id)
                )
                # Cascade to ALL chapters
                _cascade_chapters(db, series_id, None, 'grabbed', **_ch_cascade_kw)
            elif pack_type == 'chapter' and vol_rng:
                # Map chapter range → volume set using MangaDex map or approximation
                covered_vols = chapters_to_volume_set(
                    vol_rng[0], vol_rng[1], ch_map, total_chs, total_vols
                )
                # Also directly update chapters in the chapter range
                db.execute(
                    "UPDATE chapters SET status='grabbed', grabbed_at=?, torrent_name=?,"
                    " torrent_url=?, indexer=?, protocol=?, client=?, download_id=?"
                    " WHERE series_id=? AND chapter_num >= ? AND chapter_num <= ? AND monitored=1",
                    (now, title, item['url'], indexer, protocol, client_name, dl_id,
                     series_id, vol_rng[0], vol_rng[1])
                )
            elif pack_type == 'chapter' and not vol_rng:
                # Single chapter number — extract and map
                single_m = re.search(r'(?:^|[\s\[({])(\d{2,4})(?:[\s\])}]|$)', title)
                if single_m:
                    ch = float(single_m.group(1))
                    covered_vols = chapters_to_volume_set(
                        ch, ch, ch_map, total_chs, total_vols
                    )
                    db.execute(
                        "UPDATE chapters SET status='grabbed', grabbed_at=?, torrent_name=?,"
                        " torrent_url=?, indexer=?, protocol=?, client=?, download_id=?"
                        " WHERE series_id=? AND chapter_num=? AND monitored=1",
                        (now, title, item['url'], indexer, protocol, client_name, dl_id,
                         series_id, ch)
                    )
            elif vol_rng:
                # Volume range pack — update existing stubs then insert any missing ones
                db.execute(
                    "UPDATE volumes SET status='grabbed', grabbed_at=?, source_url=?,"
                    " download_id=?, torrent_name=?, client=?, indexer=?, protocol=?,"
                    " release_group=?, size_bytes=?, edition_type=?, language=?"
                    " WHERE series_id=? AND status='wanted'"
                    " AND volume_num IS NOT NULL"
                    " AND volume_num >= ? AND volume_num <= ?",
                    (now, item['url'], dl_id, title, client_name,
                     indexer, protocol, rgroup, size, edition, lang,
                     series_id, vol_rng[0], vol_rng[1])
                )
                # Insert stubs for volumes in the range that have no stub yet
                existing_in_range = {
                    r['volume_num']
                    for r in db.execute(
                        "SELECT volume_num FROM volumes WHERE series_id=?"
                        " AND volume_num IS NOT NULL"
                        " AND volume_num >= ? AND volume_num <= ?",
                        (series_id, vol_rng[0], vol_rng[1])
                    ).fetchall()
                }
                for vn in range(int(vol_rng[0]), int(vol_rng[1]) + 1):
                    if float(vn) not in existing_in_range:
                        db.execute(
                            "INSERT INTO volumes(series_id, volume_num, status,"
                            " grabbed_at, source_url, download_id, torrent_name, client,"
                            " indexer, protocol, release_group, size_bytes, edition_type, language)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (series_id, float(vn), 'grabbed', now, item['url'], dl_id,
                             title, client_name, indexer, protocol, rgroup, size, edition, lang)
                        )
                rng_vol_ids = [
                    r['id'] for r in db.execute(
                        "SELECT id FROM volumes WHERE series_id=? AND volume_num IS NOT NULL"
                        " AND volume_num >= ? AND volume_num <= ?",
                        (series_id, vol_rng[0], vol_rng[1])
                    ).fetchall()
                ]
                if rng_vol_ids:
                    _cascade_chapters(db, series_id, rng_vol_ids, 'grabbed', **_ch_cascade_kw)

            # Mark the resolved volume stubs for chapter packs.
            # Float-precise match (no CAST collapse): a chapter pack that
            # maps to volume 3 must not also flip volume 3.5 to grabbed.
            # Skip specials so a mainline grab doesn't touch side-story rows.
            if covered_vols:
                placeholders = ','.join('?' * len(covered_vols))
                _float_vols = [float(v) for v in covered_vols]
                db.execute(
                    f"UPDATE volumes SET status='grabbed', grabbed_at=?, source_url=?,"
                    f" download_id=?, torrent_name=?, client=?, indexer=?, protocol=?,"
                    f" release_group=?, size_bytes=?, edition_type=?, language=?"
                    f" WHERE series_id=? AND status='wanted'"
                    f" AND volume_num IS NOT NULL AND volume_num IN ({placeholders})"
                    f" AND COALESCE(is_special, 0) = 0",
                    [now, item['url'], dl_id, title, client_name,
                     indexer, protocol, rgroup, size, edition, lang,
                     series_id, *_float_vols]
                )
                covered_vol_ids = [
                    r['id'] for r in db.execute(
                        f"SELECT id FROM volumes WHERE series_id=? AND volume_num IS NOT NULL"
                        f" AND volume_num IN ({placeholders})"
                        f" AND COALESCE(is_special, 0) = 0",
                        [series_id, *_float_vols]
                    ).fetchall()
                ]
                if covered_vol_ids:
                    _cascade_chapters(db, series_id, covered_vol_ids, 'grabbed', **_ch_cascade_kw)

    # ── Label for logging/notifications ──────────────────────────────────────
    vol_label = build_volume_label(vol_num, vol_rng, pack_type if vol_num is None else None)
    series_title = (s_row['title'] or '') if s_row else ''

    log_event('grab', f"{vol_label} via {indexer} [{protocol}] → {client_name}", series_id)

    with get_db() as db:
        _grab_score = item.get('_score')
        _grab_data  = {'score': _grab_score} if _grab_score is not None else None
        add_history(db, 'grabbed', series_id, series_title, vol_label,
                    source_title=title, indexer=indexer, protocol=protocol,
                    client=client_name, download_id=dl_id or '',
                    size_bytes=size, release_group=rgroup,
                    data=_grab_data,
                    torrent_url=item.get('url', ''))

    asyncio.create_task(notify_discord(
        '',
        embed=make_grab_embed(series_title, vol_label, indexer, protocol, client_name, cover_url),
        event='on_grab'
    ))
    asyncio.create_task(broadcast_queue_event('grabbed', {
        'series_id': series_id, 'label': vol_label, 'series': series_title
    }))
    return True


def _collect_and_score(items: list[dict], seen_in_results: set[str]) -> list[dict]:
    """Deduplicate and score a list of release items. Filters out ignored releases."""
    # Load quality definitions once per call for size enforcement
    qual_defs: dict[str, dict] = {}
    try:
        with get_db() as _qdb:
            for row in _qdb.execute("SELECT * FROM quality_definitions").fetchall():
                qual_defs[row['quality']] = dict(row)
    except Exception:
        pass

    out = []
    for it in items:
        if not it.get('url') or it['url'] in seen_in_results:
            continue
        sc = score_release(it['title'],
                           release_group=it.get('release_group', ''),
                           indexer=it.get('indexer', ''))
        if sc <= -900:  # ignored or required-word failed
            continue

        # ── Quality size enforcement ──────────────────────────────────────────
        size_bytes = it.get('size_bytes') or it.get('size') or 0
        if size_bytes and qual_defs:
            size_mb = size_bytes / (1024 * 1024)
            quality = detect_quality_from_title(it['title'])
            qdef = qual_defs.get(quality) or qual_defs.get('unknown')
            if qdef:
                qname = qdef.get('quality', quality).upper()
                min_size = qdef.get('min_size') or 0
                max_size = qdef.get('max_size') or 0
                if min_size > 0 and size_mb < min_size:
                    print(f"[QualDef] Skipping '{it['title']}' — {qname} too small: "
                          f"{size_mb:.1f} MB (min: {min_size} MB)")
                    continue
                if max_size > 0 and size_mb > max_size:
                    print(f"[QualDef] Skipping '{it['title']}' — {qname} too large: "
                          f"{size_mb:.1f} MB (max: {max_size} MB)")
                    continue

        it = dict(it)
        it['_score'] = sc
        # Boost score slightly by seeder count so well-seeded releases rank higher
        seeders = it.get('seeders', 0) or 0
        it['_score'] += min(seeders, 20)  # cap contribution at 20 to avoid swamping quality score
        out.append(it)
        seen_in_results.add(it['url'])
    return out


async def _search_all(title: str) -> list[dict]:
    """Search all enabled DB indexers, deduplicate, score, and sort by score desc."""
    from routers.indexers import search_all_indexers as _search_db_indexers
    with get_db() as _sdb:
        raw_items = await _search_db_indexers(_sdb, title)
    seen_in_results: set[str] = set()
    all_items = _collect_and_score(raw_items, seen_in_results)
    all_items.sort(key=lambda x: x.get('_score', 0), reverse=True)
    return all_items


async def grab_existing(series_id: int, title: str, pattern: str) -> int:
    """Search all sources for all releases; grab unseen matches. Respects aliases.
    For FINISHED series with significant missing coverage, tries a complete pack search first."""
    from main import log_event  # noqa: WPS433 (lazy to avoid cycle)
    try:
        return await _grab_existing_inner(series_id, title, pattern)
    except Exception as e:
        log_event('error', f"[grab_existing] Unhandled error for '{title}': {e}", series_id)
        print(f"[grab_existing] series {series_id} '{title}': {e}")
        return 0


async def _grab_existing_inner(series_id: int, title: str, pattern: str) -> int:
    from main import log_event  # noqa: WPS433 (lazy to avoid cycle)

    # ── Complete-pack-first strategy for finished series ─────────────────────
    with get_db() as db:
        s_row = db.execute(
            "SELECT status, total_volumes FROM series WHERE id=?", (series_id,)
        ).fetchone()
        if s_row and s_row['status'] == 'FINISHED' and s_row['total_volumes']:
            wanted_count = db.execute(
                "SELECT COUNT(*) FROM volumes WHERE series_id=? AND status='wanted'",
                (series_id,)
            ).fetchone()[0]
            total = s_row['total_volumes']
            # If we're missing ≥50% of the series, try a complete pack first
            if wanted_count >= total * 0.5:
                grabbed = await search_complete_pack(series_id, title, total)
                if grabbed > 0:
                    log_event('search',
                              f"Complete pack grabbed for finished series '{title}' — skipping individual search",
                              series_id)
                    return grabbed

    # ── Normal per-volume/release search ─────────────────────────────────────
    all_items = await _search_all(title)

    with get_db() as db:
        seen_urls    = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM seen").fetchall()}
        blocked_urls = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM blocklist").fetchall()}
        alias_rows   = db.execute(
            "SELECT alias FROM series_aliases WHERE series_id=?", (series_id,)
        ).fetchall()

    all_patterns = [pattern] + [a['alias'] for a in alias_rows]

    # Also search aliases that may differ significantly from the main title
    for alias in [a['alias'] for a in alias_rows]:
        extra = await _search_all(alias)
        for it in extra:
            if it['url'] not in {x['url'] for x in all_items}:
                all_items.append(it)

    grabbed = 0
    for item in all_items:
        if item['url'] in seen_urls or item['url'] in blocked_urls:
            continue
        if any(matches(p, item['title']) for p in all_patterns):
            if await grab_item(item, series_id):
                grabbed += 1
    log_event('search', f"Search '{title}': {len(all_items)} candidates, {grabbed} grabbed", series_id)
    return grabbed


def _select_covering_packs(
    items: list[dict],
    missing_vols: set[float],
    total_volumes: int | None,
    all_patterns: list[str],
) -> list[dict]:
    """
    Greedy non-overlapping selection of complete/range packs that maximises
    coverage of missing_vols.  Sorted largest-coverage-first, then by seeders.
    Returns ordered list of packs to grab (non-overlapping by volume range).
    """
    candidates = []
    for item in items:
        if not any(matches(p, item['title']) for p in all_patterns):
            continue
        item_complete = is_complete_pack(item['title'], total_volumes)
        rng = extract_volume_range(item['title'])
        if item_complete:
            covered = set(missing_vols)  # treat as covering everything
        elif rng:
            covered = {v for v in missing_vols if rng[0] <= v <= rng[1]}
        else:
            continue  # single volume — handled in gap-fill phase
        if not covered:
            continue
        candidates.append((len(covered), rng, item, covered))

    # Sort by coverage desc, then seeders desc
    candidates.sort(key=lambda x: (x[0], x[2].get('seeders', 0)), reverse=True)

    selected: list[dict] = []
    claimed:  set[float] = set()
    for _coverage, _rng, item, covered in candidates:
        newly = covered - claimed
        if not newly:
            continue
        selected.append(item)
        claimed |= newly
        if claimed >= missing_vols:
            break
    return selected


async def search_complete_pack(series_id: int, title: str,
                               total_volumes: int | None) -> int:
    """
    Search all sources specifically for complete series packs.
    Searches the main title AND all aliases (critical for series whose Nyaa/indexer
    releases use the romaji/Japanese title instead of the English title).
    Only grabs items identified as complete or near-complete packs.
    Returns number of items grabbed.
    """
    from main import log_event  # noqa: WPS433 (lazy to avoid cycle)

    with get_db() as db:
        seen_urls    = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM seen").fetchall()}
        blocked_urls = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM blocklist").fetchall()}
        alias_rows   = db.execute(
            "SELECT alias FROM series_aliases WHERE series_id=?", (series_id,)
        ).fetchall()
    aliases = [a['alias'] for a in alias_rows]
    all_patterns = [title] + aliases

    # Only use aliases that are useful for searching:
    # Latin-script, not flagged as a foreign-language title, and meaningfully different
    def _useful_search_term(term: str) -> bool:
        if not term or len(term) < 3:
            return False
        if is_foreign_language(term):
            return False
        latin = len(re.findall(r'[a-zA-Z]', term))
        return latin >= max(1, len(term.replace(' ', '')) * 0.5)

    # Deduplicated list of search terms: main title first, then useful aliases.
    # Sort useful aliases by how DIFFERENT they are from the main title — the most
    # lexically dissimilar aliases (e.g. romaji "Shingeki no Kyojin") come first
    # because they're most likely to surface results the main-title search missed.
    norm_title = normalize(title)
    useful_aliases = [
        a for a in aliases
        if _useful_search_term(a) and normalize(a) != norm_title
    ]
    useful_aliases.sort(
        key=lambda a: difflib.SequenceMatcher(None, norm_title, normalize(a)).ratio()
    )  # ascending: lowest similarity (most different) first

    search_terms: list[str] = [title] + useful_aliases

    # Cap at 8 terms — run sequentially so we don't flood the indexer
    search_terms = search_terms[:8]

    end_str = f"v01-v{int(total_volumes):02d}" if total_volumes else None

    # Search sequentially per term (base + complete variant) to avoid rate-limiting
    seen_item_urls: set[str] = set()
    all_items: list[dict] = []

    async def _add_results(query: str):
        for item in await _search_all(query):
            if item['url'] not in seen_item_urls:
                seen_item_urls.add(item['url'])
                all_items.append(item)

    for term in search_terms:
        await _add_results(term)
        await _add_results(f"{term} complete")
        if end_str:
            await _add_results(f"{term} {end_str}")

    # Fetch currently wanted volumes for gap analysis
    with get_db() as db:
        missing_vols: set[float] = {
            float(r['volume_num'])
            for r in db.execute(
                "SELECT volume_num FROM volumes WHERE series_id=? AND status='wanted'"
                " AND volume_num IS NOT NULL",
                (series_id,)
            ).fetchall()
        }

    available = [i for i in all_items
                 if i['url'] not in seen_urls and i['url'] not in blocked_urls]

    # Phase 1: greedy non-overlapping pack selection
    packs_to_grab = _select_covering_packs(
        available, missing_vols or set(range(1, (total_volumes or 1) + 1)),
        total_volumes, all_patterns
    )

    grabbed = 0
    for item in packs_to_grab:
        if await grab_item(item, series_id):
            grabbed += 1

    # Phase 2: gap-fill — identify volumes not covered by any selected pack
    claimed_by_packs: set[float] = set()
    for item in packs_to_grab:
        if is_complete_pack(item['title'], total_volumes):
            claimed_by_packs |= missing_vols
        else:
            rng = extract_volume_range(item['title'])
            if rng:
                claimed_by_packs |= {v for v in missing_vols if rng[0] <= v <= rng[1]}
    gaps = missing_vols - claimed_by_packs

    # Cap gap-fill to 10 individual searches to avoid flooding the indexer
    gap_grabbed = 0
    for vol_num in sorted(gaps)[:10]:
        query = f"{title} vol {int(vol_num)}"
        for item in await _search_all(query):
            if item['url'] in seen_urls or item['url'] in blocked_urls:
                continue
            if not any(matches(p, item['title']) for p in all_patterns):
                continue
            item_vol = extract_volume_num(item['title'])
            if item_vol is not None and abs(item_vol - vol_num) < 0.02:
                if await grab_item(item, series_id):
                    gap_grabbed += 1
                break
    grabbed += gap_grabbed

    title_matched = sum(1 for item in all_items
                        if any(matches(p, item['title']) for p in all_patterns))
    n_queries = len(search_terms) * (3 if end_str else 2) + len(gaps)
    print(f"[CompleteSearch] '{title}': {n_queries} queries ({len(search_terms)} terms), "
          f"{len(all_items)} raw candidates, {title_matched} title-matched, "
          f"{len(packs_to_grab)} packs + {gap_grabbed} gaps = {grabbed} grabbed")
    log_event('search',
              f"Complete pack search '{title}': {len(all_items)} candidates "
              f"({title_matched} matched), {grabbed} grabbed",
              series_id)
    return grabbed


async def poll_rss():
    """Poll all enabled DB indexers for new releases."""
    from main import log_event  # noqa: WPS433 (lazy to avoid cycle)
    from routers.indexers import fetch_all_rss as _fetch_all_rss_db
    with get_db() as _rdb:
        items = await _fetch_all_rss_db(_rdb)
    source = 'Indexers'
    if not items:
        return

    # Global fallback delay (still used if delay profiles return 0)
    _global_delay = max(0, int(get_cfg('grab_delay_minutes', '0') or '0'))
    now_ts        = datetime.utcnow()

    with get_db() as db:
        series_list = [dict(r) for r in db.execute(
            "SELECT id, title, search_pattern, pub_year, edition_type FROM series WHERE enabled=1 AND monitored=1"
        ).fetchall()]
        seen_urls  = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM seen").fetchall()}
        blocked_urls = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM blocklist").fetchall()}
        # Build alias lookup: series_id → [alias, ...]
        alias_map: dict[int, list[str]] = {}
        for row in db.execute("SELECT series_id, alias FROM series_aliases").fetchall():
            alias_map.setdefault(row['series_id'], []).append(row['alias'])

    grabbed = 0
    for item in items:
        if not item['url'] or item['url'] in seen_urls or item['url'] in blocked_urls:
            continue
        if is_foreign_language(item['title']):
            continue
        for s in series_list:
            all_patterns = list({s['title'], s['search_pattern']} | set(alias_map.get(s['id'], [])))
            pub_year = s['pub_year']
            if not any(matches(p, item['title'], pub_year=pub_year) for p in all_patterns):
                continue

            # Determine effective delay from delay profiles or global fallback
            try:
                from routers.delay_profiles import get_delay_for_series
                with get_db() as _ddb:
                    delay_minutes = get_delay_for_series(_ddb, s['id'], item.get('protocol', 'torrent'))
                if delay_minutes == 0:
                    delay_minutes = _global_delay
            except Exception:
                delay_minutes = _global_delay

            if delay_minutes < 0:
                # Protocol explicitly disabled by delay profile for this series — skip
                break

            if delay_minutes > 0:
                # Insert or ignore into pending_releases; grab when delay elapses
                with get_db() as db2:
                    existing_pr = db2.execute(
                        "SELECT first_seen FROM pending_releases WHERE series_id=? AND url=?",
                        (s['id'], item['url'])
                    ).fetchone()
                    if not existing_pr:
                        db2.execute(
                            "INSERT OR IGNORE INTO pending_releases"
                            "(series_id, url, title, indexer, protocol, size_bytes)"
                            " VALUES(?,?,?,?,?,?)",
                            (s['id'], item['url'], item['title'],
                             item.get('indexer', ''), item.get('protocol', 'torrent'),
                             item.get('size_bytes', 0))
                        )
                    else:
                        elapsed = (now_ts - datetime.fromisoformat(
                            existing_pr['first_seen'].replace('Z', '')
                        )).total_seconds() / 60
                        if elapsed >= delay_minutes:
                            if await grab_item(item, s['id']):
                                grabbed += 1
                                seen_urls.add(item['url'])
                                with get_db() as db3:
                                    db3.execute(
                                        "DELETE FROM pending_releases WHERE series_id=? AND url=?",
                                        (s['id'], item['url'])
                                    )
            else:
                if await grab_item(item, s['id']):
                    grabbed += 1
                    seen_urls.add(item['url'])
            break

    # Expire stale pending_releases (older than 7 days)
    with get_db() as db:
        db.execute(
            "DELETE FROM pending_releases WHERE first_seen < datetime('now', '-7 days')"
        )

    log_event('rss_poll', f"{source} RSS: {len(items)} items checked, {grabbed} grabbed")
