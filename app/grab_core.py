"""Core grab logic: decide, persist, notify pipeline.

This module implements the full grab_item pipeline — the synchronous
"decide, persist, notify" flow that sits between indexer results and
the download client:
  - rejection logging & rate limiting
  - in-flight dedup
  - blocklist, monitoring mode, edition filter
  - min-seeders, coverage check, quality cutoff
  - repack/proper handling
  - download client submission
  - seen/volumes/history persistence
  - notification triggering
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
from events import add_history, broadcast_queue_event, log_event
from shared import get_cfg, get_db, vol_num_to_search
from volumes import _cascade_chapters
import grab_dedup


async def grab_item(
    item: dict, series_id: int, respect_monitoring: bool = True
) -> bool:
    """
    Send item to download client and record. Returns True on success.
    respect_monitoring=False bypasses per-volume and series monitor_mode checks
    (used for manual interactive grabs).
    """

    title = item["title"]
    indexer = item.get("indexer", "Unknown")
    protocol = item.get("protocol", "torrent")

    # Check seen (already grabbed) — must be a live DB query, not a cached set,
    # to guard against concurrent grabs (RSS poll + manual, or overlapping polls).
    #
    # Two-layer dedup: torrent_url is the primary key, but a release served by
    # two indexers (Prowlarr mirroring + the upstream tracker) often has two
    # distinct URLs for the same content. The indexer-supplied release_guid
    # catches that — same content, same guid, regardless of URL. release_guid
    # is nullable; a missing guid falls back to URL-only behavior.
    _release_guid = (item.get("guid") or "").strip() or None
    with get_db() as db:
        if db.execute(
            "SELECT 1 FROM seen WHERE torrent_url=?", (item["url"],)
        ).fetchone():
            return False
        if (
            _release_guid is not None
            and db.execute(
                "SELECT 1 FROM seen WHERE release_guid=? LIMIT 1", (_release_guid,)
            ).fetchone()
        ):
            grab_dedup._log_grab_rejection(
                series_id, title, f"duplicate release_guid: {_release_guid}"
            )
            return False

    # In-flight dedup: all code between here and `await grab_url` is synchronous,
    # so a second coroutine that also passed the seen check above will see this
    # entry and bail before it can send a duplicate to the download client.
    if item["url"] in grab_dedup._GRABBING_URLS:
        return False
    grab_dedup._GRABBING_URLS.add(item["url"])

    # Check blocklist
    with get_db() as db:
        if db.execute(
            "SELECT 1 FROM blocklist WHERE torrent_url=?", (item["url"],)
        ).fetchone():
            grab_dedup._GRABBING_URLS.discard(item["url"])
            grab_dedup._log_grab_rejection(series_id, title, "blocklisted")
            return False

    if respect_monitoring:
        # Check series monitor mode and edition type in one query
        with get_db() as db:
            s_mode_row = db.execute(
                "SELECT monitor_mode, edition_type FROM series WHERE id=?", (series_id,)
            ).fetchone()
        mode = (s_mode_row["monitor_mode"] if s_mode_row else None) or "all"
        if mode == "none":
            return False
        # Edition-type filter: series must only grab releases of its own edition.
        # Standard series skips colored/omnibus/deluxe etc. Non-standard series
        # skips standard (B&W) and other editions.
        _series_edition = (
            s_mode_row["edition_type"] if s_mode_row else None
        ) or "standard"
        _release_edition = detect_edition_type(title) or "standard"
        if _series_edition != _release_edition:
            grab_dedup._log_grab_rejection(
                series_id,
                title,
                f"edition mismatch (series={_series_edition}, release={_release_edition})",
            )
            return False

    # Minimum seeders check for torrents
    if item.get("protocol") == "torrent":
        _min_seeds = int(get_cfg("min_seeders", "0") or "0")
        if _min_seeds > 0 and (item.get("seeders") or 0) < _min_seeds:
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
            if _py_row and _py_row["pub_year"]:
                _pub_year = int(_py_row["pub_year"])
    except Exception:
        pass

    # Per-series scoring: apply language profile, release profiles, CF scoring,
    # and volume/year match bonuses for better disambiguation.
    _series_sc = score_release(
        title,
        series_id,
        release_group=item.get("release_group", ""),
        indexer=item.get("indexer", ""),
        volume_num=vol_num,
        pub_year=_pub_year,
    )
    if _series_sc <= -900:
        return False

    # Per-volume monitoring check (single volume)
    if respect_monitoring and vol_num is not None:
        with get_db() as db:
            vol_mon = db.execute(
                "SELECT monitored FROM volumes WHERE series_id=? AND volume_num=?",
                (series_id, vol_num),
            ).fetchone()
        if vol_mon and vol_mon["monitored"] == 0:
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
                (series_id, vol_rng[0], vol_rng[1]),
            ).fetchone()
        if not has_monitored:
            return False

    # Fetch series context (needed for coverage check and pack detection)
    with get_db() as db:
        s_row = db.execute(
            "SELECT title, total_volumes, total_chapters, chapter_vol_map, cover_url,"
            " root_folder_id, update_strategy FROM series WHERE id=?",
            (series_id,),
        ).fetchone()
        rf_row = (
            db.execute(
                "SELECT rf.path FROM root_folders rf WHERE rf.id=?",
                (s_row["root_folder_id"],),
            ).fetchone()
            if s_row and s_row["root_folder_id"]
            else None
        )

    total_vols = s_row["total_volumes"] if s_row else None
    total_chs = s_row["total_chapters"] if s_row else None
    cover_url = (s_row["cover_url"] or "") if s_row else ""
    # Let the download client use its own configured directory.
    # We query content_path from the client after completion for importing.
    save_path = None
    ch_map: dict = {}
    if s_row and s_row["chapter_vol_map"]:
        try:
            ch_map = json.loads(s_row["chapter_vol_map"])
        except Exception as e:
            print(f"[grab_item] chapter_vol_map parse failed: {e}")

    pack_type = (
        detect_pack_type(title, vol_rng, total_vols) if vol_num is None else None
    )
    complete = pack_type == "complete"

    # ── Coverage check: skip if content already fully grabbed ─────────────────
    if vol_num is None and pack_type:
        # Determine chapter range for chapter packs
        ch_range = vol_rng if pack_type == "chapter" else None
        if not ch_range and pack_type == "chapter":
            m = re.search(
                r"(?:ch(?:apter)?s?\.?\s*|#\s*)(\d{1,4}(?:\.\d+)?)\b",
                title,
                re.IGNORECASE,
            )
            if not m:
                m = re.search(r"(?:^|[\s\[({])(\d{2,4})(?:[\s\])}]|$)", title)
            if m:
                ch = float(m.group(1))
                ch_range = (ch, ch)
        if _coverage_already_grabbed(
            series_id, pack_type, vol_rng, ch_range, ch_map, total_chs, total_vols
        ):
            print(f"[Grab] Skipping '{title[:60]}' — coverage already satisfied")
            return False
    elif vol_num is not None:
        # Single volume — skip if already grabbed or downloaded, UNLESS this is a quality upgrade
        with get_db() as db:
            existing_vol = db.execute(
                "SELECT status, torrent_name, quality, release_group FROM volumes "
                "WHERE series_id=? AND volume_num=? AND status != 'wanted'",
                (series_id, vol_num),
            ).fetchone()
        if existing_vol:
            if existing_vol["status"] == "grabbed":
                return False  # already in flight
            # 'once' strategy: never upgrade — grab once and stop
            _strategy = (s_row["update_strategy"] or "always") if s_row else "always"
            if _strategy == "once":
                return False  # already have it; no upgrades for 'once' series
            # ── Repack / Proper handling (Sonarr RepackSpecification) ──────────
            _revision = parse_revision(title)
            _prop_cfg = get_cfg("propers_and_repacks", "prefer_and_upgrade")
            if _revision["is_repack"]:
                if _prop_cfg == "do_not_upgrade":
                    # Never auto-grab repacks of already-downloaded volumes
                    grab_dedup._log_grab_rejection(
                        series_id,
                        title,
                        "repack skipped: propers_and_repacks=do_not_upgrade",
                    )
                    return False
                elif _prop_cfg == "prefer_and_upgrade":
                    # Only grab if same release group (cross-group repacks rejected)
                    existing_group = (
                        (existing_vol["release_group"] or "").strip().lower()
                    )
                    new_group = parse_release_group(title).lower()
                    if existing_group and new_group and existing_group != new_group:
                        grab_dedup._log_grab_rejection(
                            series_id,
                            title,
                            f"cross-group repack rejected "
                            f"(existing={existing_group!r}, repack={new_group!r})",
                        )
                        return False
                # do_not_prefer: fall through — treat repack same as any release
            # Cutoff check first — if current quality already meets cutoff, no upgrades needed
            with get_db() as _cutoff_db:
                _s_cutoff = _cutoff_db.execute(
                    "SELECT quality_cutoff FROM series WHERE id=?", (series_id,)
                ).fetchone()
            cutoff = (_s_cutoff["quality_cutoff"] if _cutoff_db else None) or get_cfg(
                "quality_cutoff", ""
            )
            if cutoff and quality_rank(existing_vol["quality"] or "") >= quality_rank(
                cutoff
            ):
                return False  # already at or above quality cutoff — no upgrade needed
            # For 'downloaded' volumes: allow if new release is strictly higher quality
            new_q = quality_from_filename(
                title
            )  # heuristic from release title extension
            old_q = existing_vol["quality"]
            if quality_rank(new_q) > quality_rank(old_q):
                pass  # quality upgrade — allow grab
            else:
                # Same or unknown quality — fall back to CF score comparison.
                # Two profile-controlled gates (Sonarr v4 semantics, PR #124):
                #   1. cutoff_format_score — once old_score reaches this, no
                #      more CF-driven upgrades.
                #   2. min_upgrade_format_score — require a minimum delta or
                #      reject. Prevents +1-loop where the same release
                #      re-grabs forever for trivial score improvements.
                new_score = score_release(
                    title,
                    series_id,
                    release_group=item.get("release_group", ""),
                    indexer=item.get("indexer", ""),
                    volume_num=vol_num,
                    pub_year=_pub_year,
                )
                old_score = score_release(
                    existing_vol["torrent_name"] or "",
                    series_id,
                    release_group=existing_vol["release_group"] or "",
                    volume_num=vol_num,
                    pub_year=_pub_year,
                )
                # Look up the series's quality profile gates.
                cutoff_cf = 10000
                min_upgrade_cf = 10
                with get_db() as _qp_db:
                    qp_row = _qp_db.execute(
                        "SELECT qp.cutoff_format_score, qp.min_upgrade_format_score"
                        " FROM series s"
                        " LEFT JOIN quality_profiles qp ON qp.id = s.quality_profile_id"
                        " WHERE s.id=?",
                        (series_id,),
                    ).fetchone()
                if qp_row:
                    cutoff_cf = (
                        qp_row["cutoff_format_score"]
                        if qp_row["cutoff_format_score"] is not None
                        else 10000
                    )
                    min_upgrade_cf = (
                        qp_row["min_upgrade_format_score"]
                        if qp_row["min_upgrade_format_score"] is not None
                        else 10
                    )
                if old_score >= cutoff_cf:
                    grab_dedup._log_grab_rejection(
                        series_id,
                        title,
                        f"CF score upgrade ceiling reached "
                        f"(old={old_score} >= cutoff_format_score={cutoff_cf})",
                    )
                    return False
                if (new_score - old_score) < min_upgrade_cf:
                    grab_dedup._log_grab_rejection(
                        series_id,
                        title,
                        f"CF score delta too small "
                        f"(new={new_score} - old={old_score} = {new_score - old_score} "
                        f"< min_upgrade_format_score={min_upgrade_cf})",
                    )
                    return False

    # Quality cutoff enforcement on initial grab — reject releases below the configured
    # minimum quality so we don't grab CBR when the series requires CBZ.
    # (Upgrades have their own cutoff check above; this handles the 'wanted' case.)
    if vol_num is not None:
        with get_db() as _q_db:
            _q_cutoff_row = _q_db.execute(
                "SELECT quality_cutoff FROM series WHERE id=?", (series_id,)
            ).fetchone()
        _cutoff = (
            _q_cutoff_row["quality_cutoff"] if _q_cutoff_row else None
        ) or get_cfg("quality_cutoff", "")
        if _cutoff:
            _new_q = quality_from_filename(title)
            if _new_q and quality_rank(_new_q) < quality_rank(_cutoff):
                grab_dedup._log_grab_rejection(
                    series_id, title, f"quality {_new_q} below cutoff {_cutoff}"
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
            ok, client_name, dl_id, client_healthy = await asyncio.wait_for(
                grab_url(
                    item["url"], protocol, save_path=save_path, torrent_name=title
                ),
                timeout=45,
            )
        except asyncio.TimeoutError:
            log_event(
                "grab_timeout",
                f"grab_url exceeded 45s for {title[:120]}",
                series_id,
            )
            return False
    finally:
        grab_dedup._GRABBING_URLS.discard(item["url"])

    # Soft-failure recovery: qBit accepted the add but Mangarr couldn't
    # find its hash to track. Without a `seen` insert, the RSS poll
    # keeps re-finding this URL and qBit fills with duplicate adds —
    # observed in production via the recurring "[qBit] grab added but
    # hash not found" log spam. Insert `seen` here so the URL-dedup
    # path blocks future retries; the volume row stays in 'wanted' so
    # the orphan-cleanup logic in import_pipeline doesn't fight us.
    # The user can manually grab via interactive search if they want
    # the file, or improve the title that qBit fails to match.
    if not ok and client_healthy and dl_id is None:
        with get_db() as _seen_db:
            _seen_db.execute(
                "INSERT OR IGNORE INTO seen"
                "(torrent_url, torrent_name, series_id, volume_num, grabbed_at,"
                " indexer, protocol, client, download_id, release_group, size_bytes,"
                " release_guid)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item["url"],
                    title,
                    series_id,
                    vol_num,
                    datetime.utcnow().isoformat(),
                    indexer,
                    protocol,
                    client_name,
                    None,
                    parse_release_group(title),
                    item.get("size_bytes", 0),
                    _release_guid,
                ),
            )
        log_event(
            "grab_untracked",
            f"Client accepted but no hash returned; deduped to prevent retry loop: {title[:120]}",
            series_id,
        )
        return False

    if not ok:
        return False

    now = datetime.utcnow().isoformat()
    rgroup = parse_release_group(title)
    size = item.get("size_bytes", 0)
    edition = detect_edition_type(title)
    lang = item.get("language") or detect_language(title)

    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO seen"
            "(torrent_url, torrent_name, series_id, volume_num, grabbed_at,"
            " indexer, protocol, client, download_id, release_group, size_bytes,"
            " release_guid)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                item["url"],
                title,
                series_id,
                vol_num,
                now,
                indexer,
                protocol,
                client_name,
                dl_id,
                rgroup,
                size,
                _release_guid,
            ),
        )

        _ch_cascade_kw = dict(
            grabbed_at=now,
            torrent_name=title,
            torrent_url=item["url"],
            indexer=indexer,
            protocol=protocol,
            client=client_name,
            download_id=dl_id,
            release_group=rgroup,
            size_bytes=size,
        )

        if vol_num is not None:
            # ── Single volume ────────────────────────────────────────────────
            existing = db.execute(
                "SELECT id FROM volumes WHERE series_id=? AND volume_num=?",
                (series_id, vol_num),
            ).fetchone()
            if existing:
                db.execute(
                    "UPDATE volumes SET status='grabbed', grabbed_at=?, source_url=?,"
                    " download_id=?, torrent_name=?, client=?, indexer=?, protocol=?,"
                    " release_group=?, size_bytes=?, edition_type=?, language=? WHERE id=?",
                    (
                        now,
                        item["url"],
                        dl_id,
                        title,
                        client_name,
                        indexer,
                        protocol,
                        rgroup,
                        size,
                        edition,
                        lang,
                        existing["id"],
                    ),
                )
                _cascade_chapters(
                    db, series_id, [existing["id"]], "grabbed", **_ch_cascade_kw
                )
            else:
                db.execute(
                    "INSERT INTO volumes(series_id, volume_num, status, grabbed_at,"
                    " source_url, download_id, torrent_name, client,"
                    " indexer, protocol, release_group, size_bytes, edition_type, language)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        series_id,
                        vol_num,
                        "grabbed",
                        now,
                        item["url"],
                        dl_id,
                        title,
                        client_name,
                        indexer,
                        protocol,
                        rgroup,
                        size,
                        edition,
                        lang,
                    ),
                )
                new_vol = db.execute(
                    "SELECT id FROM volumes WHERE series_id=? AND volume_num=?",
                    (series_id, vol_num),
                ).fetchone()
                if new_vol:
                    _cascade_chapters(
                        db, series_id, [new_vol["id"]], "grabbed", **_ch_cascade_kw
                    )
        else:
            # ── Pack/range/complete ──────────────────────────────────────────
            if pack_type == "chapter":
                store_rng_start = store_rng_end = None
            else:
                store_rng_start = vol_rng[0] if vol_rng else None
                store_rng_end = vol_rng[1] if vol_rng else None

            # Record a single pack entry for reference
            db.execute(
                "INSERT OR IGNORE INTO volumes"
                "(series_id, status, grabbed_at, source_url, download_id,"
                " vol_range_start, vol_range_end, pack_type, torrent_name, client,"
                " indexer, protocol, release_group, size_bytes, edition_type, language)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    series_id,
                    "grabbed",
                    now,
                    item["url"],
                    dl_id,
                    store_rng_start,
                    store_rng_end,
                    pack_type,
                    title,
                    client_name,
                    indexer,
                    protocol,
                    rgroup,
                    size,
                    edition,
                    lang,
                ),
            )

            # Determine which volume stubs this pack covers
            covered_vols: set[int] = set()
            if complete:
                db.execute(
                    "UPDATE volumes SET status='grabbed', grabbed_at=?, source_url=?,"
                    " download_id=?, torrent_name=?, client=?, indexer=?, protocol=?,"
                    " release_group=?, size_bytes=?, edition_type=?, language=?"
                    " WHERE series_id=? AND status='wanted' AND volume_num IS NOT NULL",
                    (
                        now,
                        item["url"],
                        dl_id,
                        title,
                        client_name,
                        indexer,
                        protocol,
                        rgroup,
                        size,
                        edition,
                        lang,
                        series_id,
                    ),
                )
                _cascade_chapters(db, series_id, None, "grabbed", **_ch_cascade_kw)
            elif pack_type == "chapter" and vol_rng:
                covered_vols = chapters_to_volume_set(
                    vol_rng[0], vol_rng[1], ch_map, total_chs, total_vols
                )
                db.execute(
                    "UPDATE chapters SET status='grabbed', grabbed_at=?, torrent_name=?,"
                    " torrent_url=?, indexer=?, protocol=?, client=?, download_id=?"
                    " WHERE series_id=? AND chapter_num >= ? AND chapter_num <= ? AND monitored=1",
                    (
                        now,
                        title,
                        item["url"],
                        indexer,
                        protocol,
                        client_name,
                        dl_id,
                        series_id,
                        vol_rng[0],
                        vol_rng[1],
                    ),
                )
            elif pack_type == "chapter" and not vol_rng:
                single_m = re.search(r"(?:^|[\s\[({])(\d{2,4})(?:[\s\])}]|$)", title)
                if single_m:
                    ch = float(single_m.group(1))
                    covered_vols = chapters_to_volume_set(
                        ch, ch, ch_map, total_chs, total_vols
                    )
                    db.execute(
                        "UPDATE chapters SET status='grabbed', grabbed_at=?, torrent_name=?,"
                        " torrent_url=?, indexer=?, protocol=?, client=?, download_id=?"
                        " WHERE series_id=? AND chapter_num=? AND monitored=1",
                        (
                            now,
                            title,
                            item["url"],
                            indexer,
                            protocol,
                            client_name,
                            dl_id,
                            series_id,
                            ch,
                        ),
                    )
            elif vol_rng:
                db.execute(
                    "UPDATE volumes SET status='grabbed', grabbed_at=?, source_url=?,"
                    " download_id=?, torrent_name=?, client=?, indexer=?, protocol=?,"
                    " release_group=?, size_bytes=?, edition_type=?, language=?"
                    " WHERE series_id=? AND status='wanted'"
                    " AND volume_num IS NOT NULL"
                    " AND volume_num >= ? AND volume_num <= ?",
                    (
                        now,
                        item["url"],
                        dl_id,
                        title,
                        client_name,
                        indexer,
                        protocol,
                        rgroup,
                        size,
                        edition,
                        lang,
                        series_id,
                        vol_rng[0],
                        vol_rng[1],
                    ),
                )
                existing_in_range = {
                    r["volume_num"]
                    for r in db.execute(
                        "SELECT volume_num FROM volumes WHERE series_id=?"
                        " AND volume_num IS NOT NULL"
                        " AND volume_num >= ? AND volume_num <= ?",
                        (series_id, vol_rng[0], vol_rng[1]),
                    ).fetchall()
                }
                for vn in range(int(vol_rng[0]), int(vol_rng[1]) + 1):
                    if float(vn) not in existing_in_range:
                        db.execute(
                            "INSERT INTO volumes(series_id, volume_num, status,"
                            " grabbed_at, source_url, download_id, torrent_name, client,"
                            " indexer, protocol, release_group, size_bytes, edition_type, language)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                series_id,
                                float(vn),
                                "grabbed",
                                now,
                                item["url"],
                                dl_id,
                                title,
                                client_name,
                                indexer,
                                protocol,
                                rgroup,
                                size,
                                edition,
                                lang,
                            ),
                        )
                rng_vol_ids = [
                    r["id"]
                    for r in db.execute(
                        "SELECT id FROM volumes WHERE series_id=? AND volume_num IS NOT NULL"
                        " AND volume_num >= ? AND volume_num <= ?",
                        (series_id, vol_rng[0], vol_rng[1]),
                    ).fetchall()
                ]
                if rng_vol_ids:
                    _cascade_chapters(
                        db, series_id, rng_vol_ids, "grabbed", **_ch_cascade_kw
                    )

            if covered_vols:
                placeholders = ",".join("?" * len(covered_vols))
                _float_vols = [float(v) for v in covered_vols]
                db.execute(
                    f"UPDATE volumes SET status='grabbed', grabbed_at=?, source_url=?,"
                    f" download_id=?, torrent_name=?, client=?, indexer=?, protocol=?,"
                    f" release_group=?, size_bytes=?, edition_type=?, language=?"
                    f" WHERE series_id=? AND status='wanted'"
                    f" AND volume_num IS NOT NULL AND volume_num IN ({placeholders})"
                    f" AND COALESCE(is_special, 0) = 0",
                    [
                        now,
                        item["url"],
                        dl_id,
                        title,
                        client_name,
                        indexer,
                        protocol,
                        rgroup,
                        size,
                        edition,
                        lang,
                        series_id,
                        *_float_vols,
                    ],
                )
                covered_vol_ids = [
                    r["id"]
                    for r in db.execute(
                        f"SELECT id FROM volumes WHERE series_id=? AND volume_num IS NOT NULL"
                        f" AND volume_num IN ({placeholders})"
                        f" AND COALESCE(is_special, 0) = 0",
                        [series_id, *_float_vols],
                    ).fetchall()
                ]
                if covered_vol_ids:
                    _cascade_chapters(
                        db, series_id, covered_vol_ids, "grabbed", **_ch_cascade_kw
                    )

    vol_label = build_volume_label(
        vol_num, vol_rng, pack_type if vol_num is None else None
    )
    series_title = (s_row["title"] or "") if s_row else ""

    log_event(
        "grab", f"{vol_label} via {indexer} [{protocol}] → {client_name}", series_id
    )

    with get_db() as db:
        _grab_score = item.get("_score")
        _grab_data = {"score": _grab_score} if _grab_score is not None else None
        add_history(
            db,
            "grabbed",
            series_id,
            series_title,
            vol_label,
            source_title=title,
            indexer=indexer,
            protocol=protocol,
            client=client_name,
            download_id=dl_id or "",
            size_bytes=size,
            release_group=rgroup,
            data=_grab_data,
            torrent_url=item.get("url", ""),
        )

    asyncio.create_task(
        notify_discord(
            "",
            embed=make_grab_embed(
                series_title, vol_label, indexer, protocol, client_name, cover_url
            ),
            event="on_grab",
        )
    )
    asyncio.create_task(
        broadcast_queue_event(
            "grabbed",
            {"series_id": series_id, "label": vol_label, "series": series_title},
        )
    )
    return True


def _collect_and_score(items: list[dict], seen_in_results: set[str]) -> list[dict]:
    """Deduplicate and score a list of release items."""
    qual_defs: dict[str, dict] = {}
    try:
        with get_db() as _qdb:
            for row in _qdb.execute("SELECT * FROM quality_definitions").fetchall():
                qual_defs[row["quality"]] = dict(row)
    except Exception:
        pass

    out = []
    for it in items:
        if not it.get("url") or it["url"] in seen_in_results:
            continue
        sc = score_release(
            it["title"],
            release_group=it.get("release_group", ""),
            indexer=it.get("indexer", ""),
        )
        if sc <= -900:
            continue

        size_bytes = it.get("size_bytes") or it.get("size") or 0
        if size_bytes and qual_defs:
            size_mb = size_bytes / (1024 * 1024)
            quality = detect_quality_from_title(it["title"])
            qdef = qual_defs.get(quality) or qual_defs.get("unknown")
            if qdef:
                qname = qdef.get("quality", quality).upper()
                min_size = qdef.get("min_size") or 0
                max_size = qdef.get("max_size") or 0
                if min_size > 0 and size_mb < min_size:
                    print(
                        f"[QualDef] Skipping '{it['title']}' — {qname} too small: "
                        f"{size_mb:.1f} MB (min: {min_size} MB)"
                    )
                    continue
                if max_size > 0 and size_mb > max_size:
                    print(
                        f"[QualDef] Skipping '{it['title']}' — {qname} too large: "
                        f"{size_mb:.1f} MB (max: {max_size} MB)"
                    )
                    continue

        it = dict(it)
        it["_score"] = sc
        seeders = it.get("seeders", 0) or 0
        it["_score"] += min(seeders, 20)
        out.append(it)
        seen_in_results.add(it["url"])
    return out


async def _search_all(
    title: str, *, purpose: str = "auto", series_id: int | None = None
) -> list[dict]:
    """Search all enabled DB indexers, dedupe, score, and sort by score desc."""
    from routers.indexers import search_all_indexers as _search_db_indexers

    with get_db() as _sdb:
        raw_items = await _search_db_indexers(
            _sdb, title, purpose=purpose, series_id=series_id
        )
    seen_in_results: set[str] = set()
    all_items = _collect_and_score(raw_items, seen_in_results)
    all_items.sort(key=lambda x: x.get("_score", 0), reverse=True)
    return all_items
