"""Import queueing: scan completed downloads, classify files, build queue entries."""
import json
import os

from files import MANGA_EXTENSIONS, build_filename, pack_image_dir_to_cbz, quality_from_filename, safe_join_under, sanitize_filename
from helpers import _resolve_series_dest_root
from parsing import (
    _parse_vol_suffix,
    detect_pack_type,
    extract_chapter_num,
    extract_chapter_range,
    extract_volume_num,
    extract_volume_range,
    is_foreign_language,
    is_special_release,
)
from shared import get_cfg, get_db
from comicinfo import read_comic_info
from events import add_history, log_event


def _queue_import(db, series_id: int, download_id: str, torrent_name: str,
                  torrent_url: str, volume_num: float | None,
                  content_path: str) -> tuple[int | None, bool]:
    """
    Scan completed download files at content_path and create an import_queue entry.
    Returns (queue_id, needs_review).
    needs_review=False means all files mapped cleanly → can auto-import.
    needs_review=True means at least one file is ambiguous → requires user review.
    """
    if not content_path:
        log_event('error', f"Import queue: no content_path for {torrent_name}", series_id, db=db)
        return None, False

    s = db.execute(
        "SELECT title, root_folder_id, chapter_vol_map, total_volumes FROM series WHERE id=?", (series_id,)
    ).fetchone()
    if not s:
        return None, False
    _total_vols = s['total_volumes'] if 'total_volumes' in s.keys() else None

    _rel_vol_range  = extract_volume_range(torrent_name or '')
    _rel_chap_range = extract_chapter_range(torrent_name or '')
    _rel_is_special = is_special_release(torrent_name or '')
    _rel_pack_type  = detect_pack_type(torrent_name or '', _rel_vol_range, _total_vols)

    # Check early: if this download is already fully imported, skip silently
    already_done = db.execute(
        "SELECT 1 FROM volumes WHERE series_id=? AND download_id=? AND status='downloaded' LIMIT 1",
        (series_id, download_id)
    ).fetchone()
    if already_done:
        db.execute(
            "UPDATE import_queue SET status='imported' WHERE series_id=? AND download_id=?"
            " AND status IN ('partial','failed')",
            (series_id, download_id)
        )
        return None, False

    rf = db.execute(
        "SELECT path FROM root_folders WHERE id=?", (s['root_folder_id'],)
    ).fetchone() if s['root_folder_id'] else None
    cvm: dict = json.loads(s['chapter_vol_map']) if s['chapter_vol_map'] else {}

    if os.path.isdir(content_path):
        src_dir    = content_path
        scan_paths = None

        image_leafs = sorted(_find_image_only_chapter_dirs(content_path))
        if image_leafs:
            pack_dir = safe_join_under(_get_pack_staging_root(), f'queue-{download_id}')
            packed_paths: list[str] = []
            used_names: set[str] = set()
            for leaf in image_leafs:
                leaf_basename = os.path.basename(leaf.rstrip('/')) or 'chapter'
                base_name = sanitize_filename(leaf_basename)
                cbz_name = base_name + '.cbz'
                n = 2
                while cbz_name in used_names:
                    cbz_name = f'{base_name} ({n}).cbz'
                    n += 1
                used_names.add(cbz_name)
                cbz_path = os.path.join(pack_dir, cbz_name)
                size = pack_image_dir_to_cbz(leaf, cbz_path)
                if size:
                    packed_paths.append(cbz_path)
                else:
                    log_event('error',
                        f"Auto-pack failed for {leaf}: "
                        f"check disk space + /config writable",
                        series_id, db=db, dedup=True)
            if packed_paths:
                log_event('import',
                    f"Auto-packed {len(packed_paths)} image-only chapter "
                    f"director{'ies' if len(packed_paths) != 1 else 'y'} "
                    f"into CBZs: {torrent_name}",
                    series_id, db=db)
                scan_paths = packed_paths
    elif os.path.isfile(content_path):
        src_dir    = os.path.dirname(content_path)
        scan_paths = [content_path]
    else:
        log_event('error', f"Import queue: content_path not found: {content_path}",
                  series_id, db=db, dedup=True)
        return None, False

    dest_root = _resolve_series_dest_root(db, s['root_folder_id'], rf)
    safe_dir  = sanitize_filename(s['title'] or 'Unknown')
    dst_dir   = os.path.join(dest_root, safe_dir)

    _chap_stub = db.execute(
        "SELECT id FROM volumes WHERE series_id=? AND download_id=?"
        " AND status='grabbed' AND pack_type='chapter'",
        (series_id, download_id)
    ).fetchone()
    _is_chapter_grab = _chap_stub is not None

    existing = db.execute(
        "SELECT id, status FROM import_queue WHERE series_id=? AND download_id=? LIMIT 1",
        (series_id, download_id)
    ).fetchone()
    if existing:
        if existing['status'] == 'pending':
            has_review = db.execute(
                "SELECT 1 FROM import_queue_files WHERE queue_id=? AND status='needs_review'",
                (existing['id'],)
            ).fetchone()
            return existing['id'], bool(has_review)
        return None, False

    cur = db.execute(
        "INSERT INTO import_queue(series_id, download_id, torrent_name, torrent_url, volume_num, src_dir, status)"
        " VALUES(?,?,?,?,?,?,'pending')",
        (series_id, download_id, torrent_name, torrent_url, volume_num, src_dir)
    )
    queue_id = cur.lastrowid

    if scan_paths is None:
        scan_paths = []
        for root, dirs, files in os.walk(src_dir):
            dirs.sort()
            for fname in sorted(files):
                scan_paths.append(os.path.join(root, fname))

    mapped = unmapped = 0
    for src_path in scan_paths:
        fname = os.path.basename(src_path)
        if os.path.splitext(fname)[1].lower() not in MANGA_EXTENSIONS:
            continue

        if is_foreign_language(fname):
            log_event('import', f"Skipped foreign-language file: {fname}", series_id, db=db)
            continue

        proposed_vol        = extract_volume_num(fname)
        proposed_chap       = extract_chapter_num(fname)
        file_vol_range      = extract_volume_range(fname)
        file_chap_range     = extract_chapter_range(fname)
        proposed_vol_rs: float | None = None
        proposed_vol_re: float | None = None
        proposed_chap_re: float | None = None
        if file_vol_range is not None:
            proposed_vol_rs, proposed_vol_re = file_vol_range
            proposed_vol  = None
        if file_chap_range is not None:
            proposed_chap, proposed_chap_re = file_chap_range
        proposed_is_special = int(_rel_is_special or is_special_release(fname))

        ext_lower = os.path.splitext(fname)[1].lower()
        if ext_lower in ('.cbz', '.zip'):
            ci = read_comic_info(src_path)
            if ci.get('volume') is not None:
                ci_vol = ci['volume']
                if ci_vol != proposed_vol:
                    log_event('import',
                        f"ComicInfo.xml: vol {proposed_vol} → {ci_vol} for {fname}",
                        series_id, db=db)
                    proposed_vol     = ci_vol
                    proposed_chap    = None
                    proposed_vol_rs  = None
                    proposed_vol_re  = None
                    proposed_chap_re = None
            elif ci.get('number') is not None and proposed_chap is None:
                proposed_chap = ci['number']
        elif ext_lower == '.cbr':
            try:
                import rarfile
                with rarfile.RarFile(src_path) as rf:
                    ci_name = next(
                        (n for n in rf.namelist() if n.lower().endswith('comicinfo.xml')),
                        None
                    )
                    if ci_name:
                        from defusedxml.ElementTree import fromstring as _safe_xml_fromstring
                        root = _safe_xml_fromstring(rf.read(ci_name))
                        def _cbr_text(tag: str):
                            el = root.find(tag)
                            return el.text.strip() if el is not None and el.text else None
                        _raw_vol = _cbr_text('Volume')
                        _raw_num = _cbr_text('Number')
                        if _raw_vol:
                            ci_vol = _parse_vol_suffix(_raw_vol)
                            if ci_vol is not None:
                                if ci_vol != proposed_vol:
                                    log_event('import',
                                        f"ComicInfo.xml (CBR): vol {proposed_vol} → {ci_vol} for {fname}",
                                        series_id, db=db)
                                proposed_vol     = ci_vol
                                proposed_chap    = None
                                proposed_vol_rs  = None
                                proposed_vol_re  = None
                                proposed_chap_re = None
                        elif _raw_num and proposed_chap is None:
                            ci_num = _parse_vol_suffix(_raw_num)
                            if ci_num is not None:
                                proposed_chap = ci_num
            except ImportError:
                pass
            except Exception:
                pass

        has_chap_signal = proposed_chap is not None or proposed_chap_re is not None
        has_vol_signal  = proposed_vol  is not None or proposed_vol_re  is not None

        if has_chap_signal and not has_vol_signal:
            file_type = 'chapter'
            _key_src = proposed_chap if proposed_chap is not None else proposed_chap_re
            if _key_src is not None:
                chap_key = str(int(_key_src)) if _key_src == int(_key_src) else str(_key_src)
                if chap_key in cvm:
                    proposed_vol = float(cvm[chap_key])
        else:
            file_type = 'volume'
            proposed_chap    = None
            proposed_chap_re = None

        if (proposed_vol is None and proposed_vol_rs is None
                and volume_num is not None and file_type == 'volume'):
            proposed_vol = volume_num

        dst_fname = build_filename(s['title'], proposed_vol, fname)
        dst_path  = os.path.join(dst_dir, dst_fname)

        if _rel_pack_type == 'complete':
            proposed_pack_type: str | None = 'complete'
        elif proposed_chap_re is not None:
            proposed_pack_type = 'chapter_range'
        elif proposed_vol_re is not None:
            proposed_pack_type = 'volume_range'
        elif _rel_pack_type in ('chapter', 'volume'):
            proposed_pack_type = _rel_pack_type
        else:
            proposed_pack_type = None

        if proposed_vol is None and proposed_chap is None and proposed_vol_rs is None \
                and proposed_chap_re is None and not _is_chapter_grab:
            unmapped += 1
        else:
            mapped += 1
        db.execute(
            "INSERT INTO import_queue_files"
            "(queue_id, filename, src_path, dst_path, proposed_volume, proposed_chapter,"
            " proposed_volume_range_start, proposed_volume_range_end,"
            " proposed_chapter_range_end, proposed_pack_type, proposed_is_special,"
            " file_type, status)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'pending')",
            (queue_id, dst_fname, src_path, dst_path,
             proposed_vol, proposed_chap,
             proposed_vol_rs, proposed_vol_re,
             proposed_chap_re, proposed_pack_type, proposed_is_special,
             file_type)
        )

    if mapped == 0 and unmapped == 0:
        db.execute("DELETE FROM import_queue WHERE id=?", (queue_id,))
        log_event('import', f"No manga files found in {src_dir} — skipping: {torrent_name}",
                  series_id, db=db, dedup=True)
        return None, False

    needs_review = unmapped > 0
    if unmapped > 0:
        log_event('import', f"Queued for review ({unmapped} unmapped file(s)): {torrent_name}", series_id, db=db)
    return queue_id, needs_review


def _find_image_only_chapter_dirs(content_path: str) -> list[str]:
    """Find leaf directories containing only image files."""
    result = []
    
    def _is_image_only_dir(dirpath: str) -> bool:
        try:
            files = os.listdir(dirpath)
            if not files:
                return False
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext and ext not in {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp'}:
                    return False
            return True
        except OSError:
            return False

    for root, dirs, files in os.walk(content_path):
        is_leaf = not dirs
        if is_leaf and _is_image_only_dir(root):
            result.append(root)
    
    return result


def _get_pack_staging_root() -> str:
    """Get the staging root for auto-packed image dirs.

    Reads from import_pipeline at runtime so tests can monkeypatch
    import_pipeline.PACK_STAGING_ROOT.
    """
    try:
        from import_pipeline import PACK_STAGING_ROOT as _psr
        return _psr
    except ImportError:
        return '/config/mangarr-image-pack'
