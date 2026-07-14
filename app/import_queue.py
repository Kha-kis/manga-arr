"""Import queueing: scan completed downloads, classify files, build queue entries."""

import json
import os
import re
import shutil
import subprocess
import zipfile

from files import (
    MANGA_EXTENSIONS,
    build_filename,
    pack_image_dir_to_cbz,
    quality_from_filename,
    safe_join_under,
    sanitize_filename,
)
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
from rescan import _series_library_dir


_SPLIT_RAR_PART_RE = re.compile(r"^(?P<stem>.+)\.(?:rar|r\d{2})$", re.IGNORECASE)


def _queue_import(
    db,
    series_id: int,
    download_id: str,
    torrent_name: str,
    torrent_url: str,
    volume_num: float | None,
    content_path: str,
) -> tuple[int | None, bool]:
    """
    Scan completed download files at content_path and create an import_queue entry.
    Returns (queue_id, needs_review).
    needs_review=False means all files mapped cleanly → can auto-import.
    needs_review=True means at least one file is ambiguous → requires user review.
    """
    if not content_path:
        log_event(
            "error",
            f"Import queue: no content_path for {torrent_name}",
            series_id,
            db=db,
        )
        return None, False

    s = db.execute(
        "SELECT title, root_folder_id, chapter_vol_map, total_volumes FROM series WHERE id=?",
        (series_id,),
    ).fetchone()
    if not s:
        return None, False
    _total_vols = s["total_volumes"] if "total_volumes" in s.keys() else None
    scan_events: list[tuple[str, str, bool]] = []

    def _defer_scan_event(
        event_type: str,
        message: str,
        *,
        dedup: bool = False,
    ) -> None:
        scan_events.append((event_type, message, dedup))

    def _replay_scan_events() -> None:
        for event_type, message, dedup in scan_events:
            log_event(event_type, message, series_id, db=db, dedup=dedup)

    _rel_vol_range = extract_volume_range(torrent_name or "")
    _rel_chap_range = extract_chapter_range(torrent_name or "")
    _rel_is_special = is_special_release(torrent_name or "")
    _rel_pack_type = detect_pack_type(torrent_name or "", _rel_vol_range, _total_vols)

    # Check early: if this download is already fully imported, skip silently
    already_done = db.execute(
        "SELECT 1 FROM volumes WHERE series_id=? AND download_id=? AND status='downloaded' LIMIT 1",
        (series_id, download_id),
    ).fetchone()
    if already_done:
        db.execute(
            "UPDATE import_queue SET status='imported' WHERE series_id=? AND download_id=?"
            " AND status IN ('partial','failed')",
            (series_id, download_id),
        )
        return None, False

    cvm: dict = json.loads(s["chapter_vol_map"]) if s["chapter_vol_map"] else {}

    if os.path.isdir(content_path):
        src_dir = content_path
        scan_paths = None

        image_leafs = sorted(_find_image_only_chapter_dirs(content_path))
        if image_leafs:
            pack_dir = safe_join_under(_get_pack_staging_root(), f"queue-{download_id}")
            packed_paths: list[str] = []
            used_names: set[str] = set()
            for leaf in image_leafs:
                leaf_basename = os.path.basename(leaf.rstrip("/")) or "chapter"
                base_name = sanitize_filename(leaf_basename)
                cbz_name = base_name + ".cbz"
                n = 2
                while cbz_name in used_names:
                    cbz_name = f"{base_name} ({n}).cbz"
                    n += 1
                used_names.add(cbz_name)
                cbz_path = os.path.join(pack_dir, cbz_name)
                size = pack_image_dir_to_cbz(leaf, cbz_path)
                if size:
                    packed_paths.append(cbz_path)
                else:
                    _defer_scan_event(
                        "error",
                        f"Auto-pack failed for {leaf}: "
                        f"check disk space + /config writable",
                        dedup=True,
                    )
            if packed_paths:
                _defer_scan_event(
                    "import",
                    f"Auto-packed {len(packed_paths)} image-only chapter "
                    f"director{'ies' if len(packed_paths) != 1 else 'y'} "
                    f"into CBZs: {torrent_name}",
                )
                scan_paths = packed_paths
        if scan_paths is None:
            split_payloads = _extract_zip_wrapped_split_rars(
                content_path,
                download_id,
                _defer_scan_event,
            )
            if split_payloads is not None:
                scan_paths = split_payloads
    elif os.path.isfile(content_path):
        src_dir = os.path.dirname(content_path)
        scan_paths = [content_path]
    else:
        log_event(
            "error",
            f"Import queue: content_path not found: {content_path}",
            series_id,
            db=db,
            dedup=True,
        )
        return None, False

    dst_dir = _series_library_dir(db, series_id)
    if not dst_dir:
        log_event(
            "error",
            f"Import queue: cannot resolve destination folder for {torrent_name}",
            series_id,
            db=db,
            dedup=True,
        )
        return None, False

    _chap_stub = db.execute(
        "SELECT id FROM volumes WHERE series_id=? AND download_id=?"
        " AND status='grabbed' AND pack_type='chapter'",
        (series_id, download_id),
    ).fetchone()
    _is_chapter_grab = _chap_stub is not None

    existing = db.execute(
        "SELECT id, status FROM import_queue WHERE series_id=? AND download_id=? LIMIT 1",
        (series_id, download_id),
    ).fetchone()
    if existing:
        if existing["status"] == "pending":
            has_review = db.execute(
                "SELECT 1 FROM import_queue_files WHERE queue_id=? AND status='needs_review'",
                (existing["id"],),
            ).fetchone()
            return existing["id"], bool(has_review)
        return None, False

    if scan_paths is None:
        scan_paths = []
        for root, dirs, files in os.walk(src_dir):
            dirs.sort()
            for fname in sorted(files):
                scan_paths.append(os.path.join(root, fname))

    mapped = unmapped = 0
    file_rows = []
    for src_path in scan_paths:
        fname = os.path.basename(src_path)
        if os.path.splitext(fname)[1].lower() not in MANGA_EXTENSIONS:
            continue

        if is_foreign_language(fname):
            _defer_scan_event(
                "import",
                f"Skipped foreign-language file: {fname}",
            )
            continue

        proposed_vol = extract_volume_num(fname)
        proposed_chap = extract_chapter_num(fname)
        file_vol_range = extract_volume_range(fname)
        file_chap_range = extract_chapter_range(fname)
        proposed_vol_rs: float | None = None
        proposed_vol_re: float | None = None
        proposed_chap_re: float | None = None
        if file_vol_range is not None:
            proposed_vol_rs, proposed_vol_re = file_vol_range
            proposed_vol = None
        if file_chap_range is not None:
            proposed_chap, proposed_chap_re = file_chap_range
        proposed_is_special = int(_rel_is_special or is_special_release(fname))

        ext_lower = os.path.splitext(fname)[1].lower()
        if ext_lower in (".cbz", ".zip"):
            ci = read_comic_info(src_path)
            if ci.get("volume") is not None:
                ci_vol = ci["volume"]
                if ci_vol != proposed_vol:
                    _defer_scan_event(
                        "import",
                        f"ComicInfo.xml: vol {proposed_vol} → {ci_vol} for {fname}",
                    )
                    proposed_vol = ci_vol
                    proposed_chap = None
                    proposed_vol_rs = None
                    proposed_vol_re = None
                    proposed_chap_re = None
            elif ci.get("number") is not None and proposed_chap is None:
                proposed_chap = ci["number"]
        elif ext_lower == ".cbr":
            try:
                import rarfile

                with rarfile.RarFile(src_path) as rf:
                    ci_name = next(
                        (
                            n
                            for n in rf.namelist()
                            if n.lower().endswith("comicinfo.xml")
                        ),
                        None,
                    )
                    if ci_name:
                        from defusedxml.ElementTree import (
                            fromstring as _safe_xml_fromstring,
                        )

                        cbr_root = _safe_xml_fromstring(rf.read(ci_name))

                        def _cbr_text(tag: str):
                            el = cbr_root.find(tag)
                            return (
                                el.text.strip() if el is not None and el.text else None
                            )

                        _raw_vol = _cbr_text("Volume")
                        _raw_num = _cbr_text("Number")
                        if _raw_vol:
                            ci_vol = _parse_vol_suffix(_raw_vol)
                            if ci_vol is not None:
                                if ci_vol != proposed_vol:
                                    _defer_scan_event(
                                        "import",
                                        f"ComicInfo.xml (CBR): vol {proposed_vol} → {ci_vol} for {fname}",
                                    )
                                proposed_vol = ci_vol
                                proposed_chap = None
                                proposed_vol_rs = None
                                proposed_vol_re = None
                                proposed_chap_re = None
                        elif _raw_num and proposed_chap is None:
                            ci_num = _parse_vol_suffix(_raw_num)
                            if ci_num is not None:
                                proposed_chap = ci_num
            except ImportError:
                pass
            except Exception:
                pass

        if (
            volume_num is not None
            and _rel_pack_type == "volume"
            and not _is_chapter_grab
            and len(scan_paths) == 1
        ):
            proposed_vol = volume_num
            proposed_chap = None
            file_vol_range = None
            file_chap_range = None
            proposed_vol_rs = None
            proposed_vol_re = None
            proposed_chap_re = None

        has_chap_signal = proposed_chap is not None or proposed_chap_re is not None
        has_vol_signal = proposed_vol is not None or proposed_vol_re is not None

        if has_chap_signal and not has_vol_signal:
            file_type = "chapter"
            _key_src = proposed_chap if proposed_chap is not None else proposed_chap_re
            if _key_src is not None:
                chap_key = (
                    str(int(_key_src)) if _key_src == int(_key_src) else str(_key_src)
                )
                if chap_key in cvm:
                    proposed_vol = float(cvm[chap_key])
        else:
            file_type = "volume"
            proposed_chap = None
            proposed_chap_re = None

        if (
            proposed_vol is None
            and proposed_vol_rs is None
            and volume_num is not None
            and file_type == "volume"
        ):
            proposed_vol = volume_num

        dst_fname = build_filename(
            s["title"],
            proposed_vol,
            fname,
            chapter_num=proposed_chap if file_type == "chapter" else None,
        )
        dst_path = os.path.join(dst_dir, dst_fname)

        if _rel_pack_type == "complete":
            proposed_pack_type: str | None = "complete"
        elif proposed_chap_re is not None:
            proposed_pack_type = "chapter_range"
        elif proposed_vol_re is not None:
            proposed_pack_type = "volume_range"
        elif _rel_pack_type in ("chapter", "volume"):
            proposed_pack_type = _rel_pack_type
        else:
            proposed_pack_type = None

        if (
            proposed_vol is None
            and proposed_chap is None
            and proposed_vol_rs is None
            and proposed_chap_re is None
            and not _is_chapter_grab
        ):
            unmapped += 1
        else:
            mapped += 1
        file_rows.append(
            (
                dst_fname,
                src_path,
                dst_path,
                proposed_vol,
                proposed_chap,
                proposed_vol_rs,
                proposed_vol_re,
                proposed_chap_re,
                proposed_pack_type,
                proposed_is_special,
                file_type,
            )
        )

    if mapped == 0 and unmapped == 0:
        _defer_scan_event(
            "import",
            f"No manga files found in {src_dir} — skipping: {torrent_name}",
            dedup=True,
        )
        _replay_scan_events()
        return None, False

    cur = db.execute(
        "INSERT INTO import_queue(series_id, download_id, torrent_name, torrent_url, volume_num, src_dir, status)"
        " VALUES(?,?,?,?,?,?,'pending')",
        (series_id, download_id, torrent_name, torrent_url, volume_num, src_dir),
    )
    queue_id = cur.lastrowid

    db.executemany(
        "INSERT INTO import_queue_files"
        "(queue_id, filename, src_path, dst_path, proposed_volume, proposed_chapter,"
        " proposed_volume_range_start, proposed_volume_range_end,"
        " proposed_chapter_range_end, proposed_pack_type, proposed_is_special,"
        " file_type, status)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'pending')",
        [(queue_id, *row) for row in file_rows],
    )

    needs_review = unmapped > 0
    if unmapped > 0:
        log_event(
            "import",
            f"Queued for review ({unmapped} unmapped file(s)): {torrent_name}",
            series_id,
            db=db,
        )
    _replay_scan_events()
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
                if ext and ext not in {
                    ".jpg",
                    ".jpeg",
                    ".png",
                    ".webp",
                    ".gif",
                    ".bmp",
                }:
                    return False
            return True
        except OSError:
            return False

    for root, dirs, files in os.walk(content_path):
        is_leaf = not dirs
        if is_leaf and _is_image_only_dir(root):
            result.append(root)

    return result


def _extract_zip_wrapped_split_rars(content_path: str, download_id: str, defer_event) -> list[str] | None:
    """Extract scene-style ZIP wrapped split RAR payloads.

    Some DDL/tracker releases contain files like ``abc1.zip``/``abc2.zip``.
    Each outer ZIP contains one split RAR part (``abc.rar``, ``abc.r00``...),
    and the real manga payload is inside the reconstructed RAR. Treating the
    outer ZIPs as manga archives misclassifies opaque scene filenames as
    chapters, so queue the extracted payload instead.
    """
    zip_paths = [
        os.path.join(content_path, name)
        for name in sorted(os.listdir(content_path))
        if name.lower().endswith(".zip")
    ]
    if len(zip_paths) < 2:
        return None

    groups: dict[str, list[tuple[str, str]]] = {}
    for zip_path in zip_paths:
        try:
            with zipfile.ZipFile(zip_path) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    member_name = os.path.basename(info.filename)
                    m = _SPLIT_RAR_PART_RE.match(member_name)
                    if not m:
                        continue
                    groups.setdefault(m.group("stem").lower(), []).append(
                        (zip_path, member_name)
                    )
        except zipfile.BadZipFile:
            continue

    selected = [
        parts for parts in groups.values()
        if any(name.lower().endswith(".rar") for _, name in parts)
        and any(re.search(r"\.r\d{2}$", name, re.IGNORECASE) for _, name in parts)
    ]
    if not selected:
        return None

    pack_dir = safe_join_under(_get_pack_staging_root(), f"queue-{download_id}")
    split_root = os.path.join(pack_dir, "split-rar")
    shutil.rmtree(split_root, ignore_errors=True)
    os.makedirs(split_root, exist_ok=True)

    payloads: list[str] = []
    for idx, parts in enumerate(selected, start=1):
        group_dir = os.path.join(split_root, f"group-{idx}")
        out_dir = os.path.join(group_dir, "out")
        os.makedirs(group_dir, exist_ok=True)
        rar_path = None
        for zip_path, member_name in parts:
            try:
                with zipfile.ZipFile(zip_path) as zf:
                    source_member = next(
                        info for info in zf.infolist()
                        if os.path.basename(info.filename) == member_name
                    )
                    target = os.path.join(group_dir, member_name)
                    with zf.open(source_member) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    if member_name.lower().endswith(".rar"):
                        rar_path = target
            except Exception as exc:
                defer_event(
                    "error",
                    f"Split archive extract failed for {os.path.basename(zip_path)}: {exc}",
                    dedup=True,
                )
                return []

        if not rar_path:
            continue
        extractor = shutil.which("7zz") or shutil.which("7z") or shutil.which("7za")
        if extractor:
            archive_cmd = [extractor, "x", "-y", f"-o{out_dir}", rar_path]
        else:
            archive_cmd = ["unrar", "x", "-o+", rar_path, out_dir + os.sep]

        try:
            result = subprocess.run(
                archive_cmd,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except Exception as exc:
            defer_event(
                "error",
                f"Split RAR unpack failed for {os.path.basename(rar_path)}: {exc}",
                dedup=True,
            )
            return []
        if getattr(result, "returncode", 0) != 0:
            detail = ((result.stderr or result.stdout or "").strip())[:500]
            suffix = f": {detail}" if detail else ""
            defer_event(
                "error",
                f"Split RAR unpack failed for {os.path.basename(rar_path)}{suffix}",
                dedup=True,
            )
            return []

        group_payloads: list[str] = []
        for root, dirs, files in os.walk(out_dir):
            dirs.sort()
            for fname in sorted(files):
                ext = os.path.splitext(fname)[1].lower()
                if ext in MANGA_EXTENSIONS:
                    group_payloads.append(os.path.join(root, fname))
        if not group_payloads:
            detail = ((result.stderr or result.stdout or "").strip())[:500]
            suffix = f": {detail}" if detail else ""
            defer_event(
                "error",
                f"Split RAR unpack produced no manga payloads for "
                f"{os.path.basename(rar_path)}{suffix}",
                dedup=True,
            )
            return []
        payloads.extend(group_payloads)

    if payloads:
        defer_event(
            "import",
            f"Unpacked {len(payloads)} ZIP-wrapped split RAR payload(s)",
        )
    return payloads


def _get_pack_staging_root() -> str:
    """Get the staging root for auto-packed image dirs.

    Reads from import_pipeline at runtime so tests can monkeypatch
    import_pipeline.PACK_STAGING_ROOT.
    """
    try:
        from import_pipeline import PACK_STAGING_ROOT as _psr

        return _psr
    except ImportError:
        return "/config/mangarr-image-pack"
