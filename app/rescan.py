"""Series library directory resolution and reconciliation (rescan).

Thirteenth module extracted from main.py. Contains two closely-coupled
helpers that keep the on-disk library and the DB volumes table in sync:

  - _series_library_dir     — compute the library directory path for
                              a series (honours folder_name overrides,
                              folder_format, and root_folder_id)
  - rescan_series_folder    — walk the library dir, reconcile every
                              volume / pack status to match what's
                              actually on disk, create stubs for
                              unexpected files (non-standard editions),
                              opportunistically convert CBR → CBZ and
                              inject ComicInfo.xml

`add_history` and `_resolve_series_dest_root` are imported lazily from
main to avoid an import cycle.
"""
from __future__ import annotations

import os
from datetime import datetime

from comicinfo import _try_inject_comicinfo
from files import (
    MANGA_EXTENSIONS,
    _apply_format_tokens,
    _maybe_convert_to_cbz,
    quality_from_filename,
    sanitize_filename,
)
from parsing import extract_volume_num, vol_num_to_display
from shared import get_cfg
from events import add_history
from helpers import _resolve_series_dest_root
from volumes import _cascade_chapters


def _series_library_dir(db, series_id: int) -> str | None:
    """Return the library directory path for a series, or None if not configured."""
    s = db.execute(
        "SELECT title, root_folder_id, pub_year, folder_name FROM series WHERE id=?",
        (series_id,),
    ).fetchone()
    if not s:
        return None
    rf = db.execute(
        "SELECT path FROM root_folders WHERE id=?", (s['root_folder_id'],)
    ).fetchone() if s['root_folder_id'] else None
    dest_root = _resolve_series_dest_root(db, s['root_folder_id'], rf)
    folder_name = (s["folder_name"] or "").strip()
    if (
        folder_name
        and folder_name not in (".", "..")
        and os.path.basename(folder_name) == folder_name
        and "/" not in folder_name
        and "\\" not in folder_name
    ):
        safe_dir = folder_name
    else:
        title = s['title'] or 'Unknown'
        fmt = get_cfg('folder_format', '').strip()
        if fmt:
            safe_dir = _apply_format_tokens(fmt, title, pub_year=s['pub_year'])
            safe_dir = sanitize_filename(safe_dir)
        else:
            safe_dir = sanitize_filename(title)
    return os.path.join(dest_root, safe_dir)


def rescan_series_folder(db, series_id: int) -> dict:
    """Walk the series' library directory and reconcile volume and pack statuses.

    - File found on disk but volume is wanted/grabbed     → mark downloaded
    - Volume is downloaded but no matching file found     → reset to wanted
    - Volume is grabbed but download no longer active     → reset to wanted
    - Pack (volume_num IS NULL) confirmed on disk         → mark downloaded + cascade stubs
    - Pack is grabbed but no files and download is gone   → reset to wanted
    - File on disk with no stub at all                   → create stub and mark downloaded

    Returns {'found': N, 'recovered': N, 'missing': N, 'lost': N, 'created': N}.
    """

    series_dir = _series_library_dir(db, series_id)
    _s_row = db.execute("SELECT title FROM series WHERE id=?", (series_id,)).fetchone()
    _s_title = _s_row['title'] if _s_row else ''

    # ── Scan library directory ────────────────────────────────────────────────
    on_disk: set[float] = set()   # numbered volumes confirmed in library
    any_lib_files = False          # any manga file at all in series library dir
    if series_dir and os.path.isdir(series_dir):
        for root, dirs, files in os.walk(series_dir):
            for fname in files:
                if os.path.splitext(fname)[1].lower() not in MANGA_EXTENSIONS:
                    continue
                any_lib_files = True
                vol = extract_volume_num(fname)
                if vol is not None:
                    on_disk.add(vol)

    recovered = missing = lost = created = 0

    # ── Numbered volume stubs ─────────────────────────────────────────────────
    stubs = db.execute(
        "SELECT id, volume_num, status, download_id, torrent_name, client FROM volumes "
        "WHERE series_id=? AND volume_num IS NOT NULL",
        (series_id,)
    ).fetchall()
    stubbed_vols: set[float] = {stub['volume_num'] for stub in stubs}

    for stub in stubs:
        vol      = stub['volume_num']
        has_file = vol in on_disk

        if stub['status'] == 'downloaded' and not has_file:
            # Suwayomi files live in Suwayomi's own directory, not the managed library
            if stub['client'] == 'suwayomi':
                continue
            # File was deleted from library
            _vol_label = f"Vol {vol_num_to_display(vol)}"
            add_history(db, 'file_deleted', series_id, _s_title, _vol_label,
                        source_title=stub['torrent_name'] or '')
            db.execute(
                "UPDATE volumes SET status='wanted', import_path=NULL, download_id=NULL,"
                " torrent_name=NULL, indexer=NULL, protocol=NULL, client=NULL,"
                " grabbed_at=NULL, imported_at=NULL, source_url=NULL, release_group=NULL "
                "WHERE id=?", (stub['id'],)
            )
            _cascade_chapters(db, series_id, [stub['id']], 'wanted',
                              grabbed_at=None, torrent_name=None, torrent_url=None,
                              indexer=None, protocol=None, client=None,
                              download_id=None, release_group=None)
            missing += 1
        elif stub['status'] in ('wanted', 'grabbed') and has_file:
            # File exists on disk but status not updated — recover it
            matched_path = None
            if series_dir and os.path.isdir(series_dir):
                for _root, _dirs, _files in os.walk(series_dir):
                    for _fname in _files:
                        if os.path.splitext(_fname)[1].lower() in MANGA_EXTENSIONS:
                            _v = extract_volume_num(_fname)
                            if _v is not None and abs(_v - vol) < 0.01:
                                matched_path = os.path.join(_root, _fname)
                                break
                    if matched_path:
                        break
            # Opportunistically convert CBR to CBZ on recovery (best-effort)
            if matched_path:
                matched_path = _maybe_convert_to_cbz(matched_path)
            file_size = os.path.getsize(matched_path) if matched_path else 0
            file_qual = quality_from_filename(matched_path or '') if matched_path else None
            db.execute(
                "UPDATE volumes SET status='downloaded', import_path=?,"
                " size_bytes=COALESCE(NULLIF(size_bytes,0),?),"
                " quality=COALESCE(quality,?),"
                " imported_at=COALESCE(imported_at,?) WHERE id=?",
                (matched_path, file_size, file_qual,
                 datetime.utcnow().isoformat(), stub['id'])
            )
            # Inject ComicInfo.xml into newly-recovered files (best-effort)
            if matched_path:
                _s_full = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
                _tags   = [r['tag'] for r in db.execute(
                    "SELECT tag FROM series_tags WHERE series_id=?", (series_id,)
                ).fetchall()]
                if _s_full:
                    _try_inject_comicinfo(matched_path, _s_full, volume_num=vol, tags=_tags)
            recovered += 1
        # NOTE: "grabbed but not in client" is intentionally NOT handled here.
        # check_download_status() manages that by querying qBit/SABnzbd directly.

    # ── Backfill quality for downloaded volumes where it was never set ─────────
    # Handles volumes imported before the quality column existed, or where the
    # import pipeline set quality=NULL (e.g. manual file drops, pre-migration data).
    db.execute("""
        UPDATE volumes
        SET quality = CASE
            WHEN LOWER(SUBSTR(import_path, -4)) = '.cbz' THEN 'cbz'
            WHEN LOWER(SUBSTR(import_path, -4)) = '.zip' THEN 'zip'
            WHEN LOWER(SUBSTR(import_path, -4)) = '.cbr' THEN 'cbr'
            WHEN LOWER(SUBSTR(import_path, -4)) = '.rar' THEN 'rar'
            WHEN LOWER(SUBSTR(import_path, -5)) = '.epub' THEN 'epub'
            WHEN LOWER(SUBSTR(import_path, -5)) = '.mobi' THEN 'mobi'
            WHEN LOWER(SUBSTR(import_path, -4)) = '.pdf'  THEN 'pdf'
        END
        WHERE series_id = ?
          AND status = 'downloaded'
          AND quality IS NULL
          AND import_path IS NOT NULL
    """, (series_id,))

    # ── Pack entries (volume_num IS NULL) ─────────────────────────────────────
    packs = db.execute(
        "SELECT id, pack_type, vol_range_start, vol_range_end, status, download_id, import_path "
        "FROM volumes WHERE series_id=? AND volume_num IS NULL",
        (series_id,)
    ).fetchall()

    for pack in packs:
        dl_id = (pack['download_id'] or '').lower()
        pt    = pack['pack_type'] or ''

        if pack['status'] == 'downloaded':
            # Verify the imported file still exists; if not, remove the pack stub entirely
            if pack['import_path'] and not os.path.exists(pack['import_path']):
                db.execute("DELETE FROM volumes WHERE id=?", (pack['id'],))
                missing += 1
            continue

        if pack['status'] != 'grabbed':
            continue

        # Determine if this pack's content is confirmed on disk
        confirmed = False
        if pt == 'complete':
            # Complete series pack — any library file means it landed
            confirmed = any_lib_files
        elif pt == 'volume' and pack['vol_range_start'] is not None and pack['vol_range_end'] is not None:
            # Range pack — at least one covered volume file must be present
            confirmed = any(
                pack['vol_range_start'] <= v <= pack['vol_range_end'] for v in on_disk
            )
        # chapter-type packs (spin-offs) rely solely on import_path; skip disk inference

        if confirmed:
            db.execute("UPDATE volumes SET status='downloaded' WHERE id=?", (pack['id'],))
            # Cascade status to the covered numbered stubs
            if pt == 'complete':
                db.execute(
                    "UPDATE volumes SET status='downloaded'"
                    " WHERE series_id=? AND volume_num IS NOT NULL AND status != 'downloaded'",
                    (series_id,)
                )
                _cascade_chapters(db, series_id, None, 'downloaded')
            elif pt == 'volume':
                db.execute(
                    "UPDATE volumes SET status='downloaded'"
                    " WHERE series_id=? AND volume_num IS NOT NULL AND status != 'downloaded'"
                    " AND volume_num >= ? AND volume_num <= ?",
                    (series_id, pack['vol_range_start'], pack['vol_range_end'])
                )
                rng_ids = [r['id'] for r in db.execute(
                    "SELECT id FROM volumes WHERE series_id=? AND volume_num IS NOT NULL"
                    " AND volume_num >= ? AND volume_num <= ?",
                    (series_id, pack['vol_range_start'], pack['vol_range_end'])
                ).fetchall()]
                if rng_ids:
                    _cascade_chapters(db, series_id, rng_ids, 'downloaded')
            recovered += 1
        # NOTE: pack orphan cleanup is handled by check_download_status() via qBit query.

    # ── Create stubs for files on disk that have no stub yet ─────────────────
    # This is the primary mechanism for non-standard editions (omnibus, deluxe, etc.)
    # where AniList's volume count is unreliable and stubs are not auto-created on add.
    unmatched = on_disk - stubbed_vols
    if unmatched:
        _s_meta = db.execute(
            "SELECT monitor_mode, edition_type FROM series WHERE id=?", (series_id,)
        ).fetchone()
        _monitored = 1 if (_s_meta and (_s_meta['monitor_mode'] or 'all') in ('all', 'missing')) else 0
        for vol_num in sorted(unmatched):
            matched_path = None
            if series_dir and os.path.isdir(series_dir):
                for _root, _dirs, _files in os.walk(series_dir):
                    for _fname in _files:
                        if os.path.splitext(_fname)[1].lower() in MANGA_EXTENSIONS:
                            _v = extract_volume_num(_fname)
                            if _v is not None and abs(_v - vol_num) < 0.01:
                                matched_path = os.path.join(_root, _fname)
                                break
                    if matched_path:
                        break
            if matched_path:
                matched_path = _maybe_convert_to_cbz(matched_path)
            file_size = os.path.getsize(matched_path) if matched_path else 0
            file_qual = quality_from_filename(matched_path or '') if matched_path else None
            db.execute(
                "INSERT INTO volumes(series_id, volume_num, status, import_path,"
                " size_bytes, quality, imported_at, monitored)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (series_id, vol_num, 'downloaded', matched_path,
                 file_size, file_qual, datetime.utcnow().isoformat(), _monitored)
            )
            created += 1
            if matched_path:
                _s_full = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
                _tags   = [r['tag'] for r in db.execute(
                    "SELECT tag FROM series_tags WHERE series_id=?", (series_id,)
                ).fetchall()]
                if _s_full:
                    _try_inject_comicinfo(matched_path, _s_full, volume_num=vol_num, tags=_tags)
        # Update total_volumes if newly created stubs push the max higher
        if created:
            max_row = db.execute(
                "SELECT MAX(volume_num) as m FROM volumes"
                " WHERE series_id=? AND volume_num IS NOT NULL", (series_id,)
            ).fetchone()
            if max_row and max_row['m'] is not None:
                new_max = int(max_row['m'])
                tv_row = db.execute(
                    "SELECT total_volumes FROM series WHERE id=?", (series_id,)
                ).fetchone()
                current_tv = tv_row['total_volumes'] if tv_row else None
                if current_tv is None or new_max > current_tv:
                    db.execute(
                        "UPDATE series SET total_volumes=?,vol_count_source='local'"
                        " WHERE id=?",
                        (new_max, series_id),
                    )
                    from metadata_provenance import (
                        record_metadata_candidates,
                        record_metadata_selections,
                    )

                    record_metadata_candidates(
                        series_id,
                        'local',
                        {'total_volumes': new_max},
                        confidence=1.0,
                        db=db,
                    )
                    record_metadata_selections(
                        series_id,
                        {'total_volumes': new_max},
                        {'total_volumes': 'local'},
                        db=db,
                    )

    return {'found': len(on_disk), 'recovered': recovered, 'missing': missing, 'lost': lost, 'created': created}
