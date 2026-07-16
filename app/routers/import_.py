"""Import queue and manual import routes."""

import asyncio
import json
import os
import re
import shutil
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from routers._templates import templates
from files import sanitize_filename
from import_kinds import VALID_IMPORT_KINDS, infer_import_kind
from shared import cascade_chapters, get_cfg, get_db, vol_num_to_display, with_flash

router = APIRouter()

_BLOCKED_SCAN_PREFIXES = (
    "/proc",
    "/sys",
    "/dev",
    "/etc",
    "/boot",
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/run",
    "/snap",
)


def extract_series_name(filename: str) -> str:
    """Heuristically extract the series title from a manga filename."""
    name = os.path.splitext(filename)[0]
    name = re.sub(r"\s*\[[^\]]*\]|\s*\([^\)]*\)", "", name)
    name = re.sub(
        r"\s*[-\s]?\s*(?:v|vol\.?|volume|ch\.?|chapter|#)\s*[\d].*$",
        "",
        name,
        flags=re.IGNORECASE,
    )
    return re.sub(r"[\s\-_,]+$", "", name).strip()


def _build_series_match_patterns(series_list, alias_map: dict) -> list[tuple]:
    import main as _m

    return [
        (s, list({s["title"], s["search_pattern"]} | set(alias_map.get(s["id"], []))))
        for s in series_list
    ]


def _match_file_to_series(fname: str, series_patterns: list[tuple]):
    import main as _m

    for s, patterns in series_patterns:
        if any(_m.matches(p, fname) for p in patterns):
            return {"id": s["id"], "title": s["title"]}
    return None


# ── Import queue ──────────────────────────────────────────────────────────────


@router.get("/import", response_class=HTMLResponse)
async def import_queue_page(request: Request):
    """Redirect to unified queue page — import review happens inline there."""
    return RedirectResponse("/queue", status_code=301)


_VALID_PACK_TYPES = {
    "",
    "volume",
    "volume_range",
    "chapter",
    "chapter_range",
    "complete",
    "special",
}


def _parse_vol_input(raw: str) -> float | None:
    """Parse a volume form input into a float.

    Accepts plain numbers (1, 3.5), letter suffixes (3a → 3.01), and
    Unicode fraction suffixes (3½ → 3.5). Returns None on blank/invalid
    so process_import can fall back to the existing proposed value.
    """
    if not raw:
        return None
    import main as _m

    # Strip whitespace; the underlying _parse_vol_suffix handles the rest.
    return _m._parse_vol_suffix(raw.strip())


def dismiss_import_queue_entry(queue_id: int) -> dict:
    """Remove an import queue entry and reset its grabbed volumes to wanted."""
    with get_db() as db:
        q = db.execute(
            "SELECT series_id, download_id FROM import_queue WHERE id=?", (queue_id,)
        ).fetchone()
        if not q:
            return {"ok": False, "status": "not_found"}

        series_id = q["series_id"]
        dl_id = q["download_id"]
        if dl_id:
            others = db.execute(
                "SELECT COUNT(*) FROM import_queue WHERE download_id=? AND id != ?",
                (dl_id, queue_id),
            ).fetchone()[0]
            if others == 0:
                db.execute("DELETE FROM seen WHERE download_id=?", (dl_id,))
        grabbed = (
            db.execute(
                "SELECT id FROM volumes WHERE series_id=? AND download_id=? AND status='grabbed'",
                (series_id, dl_id or ""),
            ).fetchall()
            if dl_id
            else []
        )
        vol_ids = [r["id"] for r in grabbed]
        if vol_ids:
            db.execute(
                "UPDATE volumes SET status='wanted', download_id=NULL, grabbed_at=NULL,"
                " source_url=NULL, torrent_name=NULL, indexer=NULL, protocol=NULL,"
                " client=NULL, release_group=NULL"
                " WHERE series_id=? AND download_id=? AND status='grabbed'",
                (series_id, dl_id),
            )
            cascade_chapters(
                db,
                series_id,
                vol_ids,
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
        db.execute("DELETE FROM import_queue_files WHERE queue_id=?", (queue_id,))
        db.execute("DELETE FROM import_queue WHERE id=?", (queue_id,))
    return {"ok": True, "status": "dismissed"}


def skip_import_queue_entry(queue_id: int) -> dict:
    """Mark a pending or partial import queue entry as skipped."""
    with get_db() as db:
        q = db.execute(
            "SELECT status FROM import_queue WHERE id=?", (queue_id,)
        ).fetchone()
        if not q:
            return {"ok": False, "status": "not_found"}
        if q["status"] not in ("pending", "partial"):
            return {"ok": False, "status": "not_skippable"}
        db.execute(
            "UPDATE import_queue SET status='skipped' WHERE id=?",
            (queue_id,),
        )
        db.execute(
            "UPDATE import_queue_files SET status='skipped' WHERE queue_id=?",
            (queue_id,),
        )
    return {"ok": True, "status": "skipped"}


def clear_inactive_import_queue_entries() -> dict:
    """Delete failed/skipped import queue entries and their files."""
    with get_db() as db:
        file_cur = db.execute(
            "DELETE FROM import_queue_files WHERE queue_id IN ("
            "  SELECT id FROM import_queue WHERE status IN ('failed','skipped')"
            ")"
        )
        queue_cur = db.execute(
            "DELETE FROM import_queue WHERE status IN ('failed','skipped')"
        )
        deleted_files = file_cur.rowcount
        deleted = queue_cur.rowcount
    return {
        "ok": True,
        "status": "deleted",
        "deleted": deleted,
        "deleted_files": deleted_files,
    }


def retry_import_queue_entry(queue_id: int) -> dict:
    """Retry failed files while preserving decisions that still need review."""
    import main as _m

    with get_db() as db:
        q = db.execute(
            "SELECT status FROM import_queue WHERE id=?",
            (queue_id,),
        ).fetchone()
        if not q:
            return {"ok": False, "status": "not_found"}
        if q["status"] not in ("failed", "partial"):
            return {"ok": False, "status": "not_retryable"}

        db.execute(
            "UPDATE import_queue_files SET status='pending'"
            " WHERE queue_id=? AND status='failed'",
            (queue_id,),
        )
        has_review = db.execute(
            "SELECT 1 FROM import_queue_files WHERE queue_id=? AND status='needs_review'",
            (queue_id,),
        ).fetchone()
        queue_status = "partial" if has_review else "pending"
        db.execute(
            "UPDATE import_queue SET status=? WHERE id=?",
            (queue_status, queue_id),
        )

    queued = False
    if not has_review:
        _m.create_background_task(
            _m._process_auto_import(queue_id),
            name=f"import:retry:{queue_id}",
        )
        queued = True
    return {
        "ok": True,
        "status": "queued" if queued else "needs_review",
        "queued": queued,
    }


@router.post("/import/{queue_id}/process")
async def process_import(queue_id: int, request: Request):
    """Validate operator decisions, persist them, and execute valid files."""
    import main as _m

    form = await request.form()

    volume_overrides: dict[int, float] = {}
    chapter_overrides: dict[int, float] = {}
    skip_ids: set[int] = set()
    validation_errors = 0
    valid_files = 0

    with get_db() as db:
        file_rows = db.execute(
            "SELECT * FROM import_queue_files WHERE queue_id=?"
            " AND status IN ('pending','needs_review','failed')",
            (queue_id,),
        ).fetchall()

        for file_row in file_rows:
            fid = file_row["id"]
            raw_kind = form.get(f"kind_{fid}")
            if form.get(f"skip_{fid}") or raw_kind == "skip":
                skip_ids.add(fid)
                db.execute(
                    "UPDATE import_queue_files SET status='pending',"
                    " proposed_import_kind='skip' WHERE id=?",
                    (fid,),
                )
                continue

            # Volume (start) — accepts fractional/letter suffixes (D11).
            raw_vol = form.get(f"vol_{fid}", "") or ""
            raw_vol_end = form.get(f"vol_end_{fid}", "") or ""
            assert isinstance(raw_vol, str)
            assert isinstance(raw_vol_end, str)
            vol_val = _parse_vol_input(raw_vol)
            vol_end_val = _parse_vol_input(raw_vol_end)

            # Chapter (start) + range end. type=number already restricts
            # to decimals; still guard against bad input.
            raw_chap = form.get(f"chap_{fid}", "") or ""
            raw_chap_end = form.get(f"chap_end_{fid}", "") or ""
            assert isinstance(raw_chap, str)
            assert isinstance(raw_chap_end, str)
            try:
                chap_val = float(raw_chap) if raw_chap else None
            except ValueError:
                chap_val = None
            try:
                chap_end_val = float(raw_chap_end) if raw_chap_end else None
            except ValueError:
                chap_end_val = None

            pack_form_value = form.get(f"pack_{fid}")
            pack_raw = (
                pack_form_value
                if pack_form_value is not None
                else file_row["proposed_pack_type"]
            ) or ""
            assert isinstance(pack_raw, str)
            pack_raw = pack_raw.strip()
            if pack_raw not in _VALID_PACK_TYPES:
                pack_raw = ""
            is_special_flag = 1 if form.get(f"spec_{fid}") else 0

            # ── Conflict handling ─────────────────────────────────────
            # If both a volume and a chapter number are provided, the
            # pack-type select is the tie-breaker. Otherwise fall back
            # to whichever has a value; if both present and pack type
            # doesn't disambiguate, mark the file needs_review and let
            # the operator resolve it. One file never imports as both.
            vol_given = vol_val is not None or vol_end_val is not None
            chap_given = chap_val is not None or chap_end_val is not None
            conflict = (
                raw_kind is None
                and vol_given
                and chap_given
                and pack_raw not in (
                    "volume",
                    "volume_range",
                    "chapter",
                    "chapter_range",
                )
            )
            if raw_kind is not None:
                import_kind = str(raw_kind).strip().lower()
                if import_kind not in VALID_IMPORT_KINDS:
                    import_kind = ""
            elif conflict:
                import_kind = ""
            else:
                import_kind = infer_import_kind(
                    file_type=(
                        "chapter"
                        if chap_given or pack_raw in ("chapter", "chapter_range")
                        else "volume"
                    ),
                    pack_type=pack_raw,
                    is_special=is_special_flag,
                    volume_range_end=vol_end_val,
                    chapter_range_end=chap_end_val,
                )

            special_title = ""
            invalid = conflict or not import_kind
            if import_kind == "special":
                raw_title = form.get(f"special_title_{fid}", "") or ""
                special_title = sanitize_filename(str(raw_title).strip())[:180]
                invalid = invalid or not str(raw_title).strip()
                vol_val = vol_end_val = chap_val = chap_end_val = None
                pack_type = None
                file_type = "special"
            elif import_kind == "volume":
                invalid = invalid or vol_val is None or vol_end_val is not None
                pack_type = "complete" if pack_raw == "complete" else "volume"
                file_type = "volume"
                chap_val = chap_end_val = None
            elif import_kind == "volume_range":
                invalid = (
                    invalid
                    or vol_val is None
                    or vol_end_val is None
                    or vol_end_val < vol_val
                )
                pack_type = "complete" if pack_raw == "complete" else "volume_range"
                file_type = "volume"
                chap_val = chap_end_val = None
            elif import_kind == "chapter":
                invalid = invalid or chap_val is None or chap_end_val is not None
                pack_type = "chapter"
                file_type = "chapter"
                vol_val = vol_end_val = None
            elif import_kind == "chapter_range":
                invalid = (
                    invalid
                    or chap_val is None
                    or chap_end_val is None
                    or chap_end_val < chap_val
                )
                pack_type = "chapter_range"
                file_type = "chapter"
                vol_val = vol_end_val = None
            else:
                invalid = True
                pack_type = None
                file_type = file_row["file_type"] or "volume"

            if invalid:
                db.execute(
                    "UPDATE import_queue_files SET status='needs_review' WHERE id=?",
                    (fid,),
                )
                validation_errors += 1
                continue

            updates: list[tuple[str, object]] = [
                (
                    "proposed_volume",
                    vol_val if import_kind == "volume" else None,
                ),
                (
                    "proposed_volume_range_start",
                    vol_val if import_kind == "volume_range" else None,
                ),
                (
                    "proposed_volume_range_end",
                    vol_end_val if import_kind == "volume_range" else None,
                ),
                ("proposed_chapter", chap_val),
                ("proposed_chapter_range_end", chap_end_val),
                ("proposed_pack_type", pack_type),
                ("proposed_is_special", int(import_kind == "special")),
                ("proposed_import_kind", import_kind),
                ("proposed_special_title", special_title or None),
                ("file_type", file_type),
                ("status", "pending"),
            ]
            set_clause = ", ".join(f"{col}=?" for col, _ in updates)
            db.execute(
                f"UPDATE import_queue_files SET {set_clause} WHERE id=?",
                [v for _, v in updates] + [fid],
            )
            valid_files += 1

            # Mirror the resolved values into the legacy kwargs so a
            # fallback code path inside _execute_import still sees the
            # operator's intent even if it hasn't been updated to read
            # from the row yet.
            if vol_val is not None:
                volume_overrides[fid] = vol_val
            if chap_val is not None:
                chapter_overrides[fid] = chap_val

        if validation_errors and valid_files == 0 and not skip_ids:
            db.execute(
                "UPDATE import_queue SET status='partial' WHERE id=?", (queue_id,)
            )

    if valid_files or skip_ids:
        await _m._guarded_execute_import(
            queue_id, volume_overrides, skip_ids, chapter_overrides
        )

    with get_db() as db:
        result_row = db.execute(
            "SELECT status FROM import_queue WHERE id=?", (queue_id,)
        ).fetchone()
    if result_row is None:
        message, toast_type = "Import completed", "success"
    elif result_row["status"] == "partial":
        message, toast_type = "Review required for remaining files", "warning"
    elif result_row["status"] == "failed":
        message, toast_type = "Import failed", "error"
    else:
        message, toast_type = "Import queued", "info"

    if request.headers.get("HX-Request") == "true":
        return HTMLResponse(
            "",
            headers={
                "HX-Refresh": "true",
                "HX-Trigger": json.dumps(
                    {"showToast": {"msg": message, "type": toast_type}}
                ),
            },
        )
    return RedirectResponse(
        with_flash("/queue", message, toast_type), status_code=303
    )


@router.post("/import/{queue_id}/skip")
async def skip_import(request: Request, queue_id: int):
    """Skip an entire import queue item without moving files."""
    skip_import_queue_entry(queue_id)
    if request.headers.get("HX-Request") == "true":
        from fastapi.responses import Response as _Resp

        return _Resp(headers={"HX-Refresh": "true"})
    return RedirectResponse(
        with_flash("/import", "Failed imports cleared", "success"), status_code=303
    )


@router.post("/import/{queue_id}/dismiss")
async def dismiss_import(request: Request, queue_id: int):
    """Remove an import queue entry from Mangarr's DB only — resets grabbed volumes to wanted."""
    dismiss_import_queue_entry(queue_id)
    if request.headers.get("HX-Request") == "true":
        from fastapi.responses import Response as _Resp

        return _Resp(headers={"HX-Refresh": "true"})
    return RedirectResponse("/import", status_code=303)


@router.get("/api/import/pending-count")
async def import_pending_count():
    with get_db() as db:
        n = db.execute(
            "SELECT COUNT(*) FROM import_queue WHERE status='pending' OR ("
            "  status='partial' AND EXISTS ("
            "    SELECT 1 FROM import_queue_files f WHERE f.queue_id=import_queue.id"
            "    AND f.status='needs_review'"
            "  )"
            ")"
        ).fetchone()[0]
    return JSONResponse({"count": n})


@router.post("/import/{queue_id}/retry")
async def retry_import(request: Request, queue_id: int):
    """Reset a failed import back to pending and trigger auto-processing."""
    retry_import_queue_entry(queue_id)
    if request.headers.get("HX-Request") == "true":
        import json
        from fastapi.responses import Response as _Resp

        return _Resp(
            headers={
                "HX-Trigger": json.dumps(
                    {"showToast": {"msg": "Import queued for retry", "type": "success"}}
                ),
                "HX-Refresh": "true",
            }
        )
    return RedirectResponse("/import", status_code=303)


@router.post("/import/clear-old")
async def import_clear_old(request: Request):
    """Delete all failed/skipped import queue entries."""
    clear_inactive_import_queue_entries()
    if request.headers.get("HX-Request") == "true":
        import json
        from fastapi.responses import Response as _Resp

        return _Resp(
            headers={
                "HX-Trigger": json.dumps(
                    {"showToast": {"msg": "Failed imports cleared", "type": "success"}}
                ),
                "HX-Refresh": "true",
            }
        )
    return RedirectResponse("/import", status_code=303)


# ── Manual import ─────────────────────────────────────────────────────────────


@router.get("/manual-import", response_class=HTMLResponse)
async def manual_import_page(request: Request):
    with get_db() as db:
        rows = db.execute(
            "SELECT id, title FROM series WHERE deleted_at IS NULL ORDER BY title"
        ).fetchall()
    series_list = [{"id": r["id"], "title": r["title"]} for r in rows]
    return templates.TemplateResponse(
        request,
        "manual_import.html",
        {
            "series_list": series_list,
        },
    )


@router.post("/api/manual-import/scan")
async def manual_import_scan(request: Request):
    import main as _m

    data = await request.json()
    scan_path = os.path.realpath(data.get("path", "").strip())
    if not scan_path or not os.path.isdir(scan_path):
        return JSONResponse({"error": "Directory not found", "files": []})
    if any(
        scan_path == p or scan_path.startswith(p + "/") for p in _BLOCKED_SCAN_PREFIXES
    ):
        return JSONResponse({"error": "Path not allowed", "files": []}, status_code=403)

    with get_db() as db:
        series_list = db.execute(
            "SELECT id, title, search_pattern FROM series"
            " WHERE deleted_at IS NULL ORDER BY title"
        ).fetchall()
        alias_map: dict[int, list[str]] = {}
        for r in db.execute("SELECT series_id, alias FROM series_aliases").fetchall():
            alias_map.setdefault(r["series_id"], []).append(r["alias"])

    series_patterns = _build_series_match_patterns(series_list, alias_map)

    results = []
    for root, dirs, files in os.walk(scan_path):
        dirs.sort()
        for fname in sorted(files):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in _m.MANGA_EXTENSIONS:
                continue
            fpath = os.path.join(root, fname)
            vol_num = _m.extract_volume_num(fname)
            file_size = os.path.getsize(fpath)
            matched_series = _match_file_to_series(fname, series_patterns)
            results.append(
                {
                    "filename": fname,
                    "path": fpath,
                    "size": _m.format_bytes(file_size),
                    "size_bytes": file_size,
                    "proposed_volume": vol_num,
                    "matched_series": matched_series,
                    "suggested_title": extract_series_name(fname)
                    if not matched_series
                    else None,
                }
            )
    return JSONResponse({"files": results})


@router.post("/api/manual-import/auto-import")
async def manual_import_auto(request: Request):
    """Scan a directory, auto-detect series, add new series, move files, mark volumes downloaded."""
    import main as _m

    data = await request.json()
    scan_path = os.path.realpath(data.get("path", "").strip())
    remove_source = data.get("remove_source", True)

    if not scan_path or not os.path.isdir(scan_path):
        return JSONResponse({"error": "Directory not found"})
    if any(
        scan_path == p or scan_path.startswith(p + "/") for p in _BLOCKED_SCAN_PREFIXES
    ):
        return JSONResponse({"error": "Path not allowed"}, status_code=403)

    import_mode = get_cfg("import_mode", "hardlink")

    file_entries = []
    for root, dirs, filenames in os.walk(scan_path):
        dirs.sort()
        for fname in sorted(filenames):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in _m.MANGA_EXTENSIONS:
                continue
            fpath = os.path.join(root, fname)
            file_entries.append(
                {
                    "filename": fname,
                    "path": fpath,
                    "vol_num": _m.extract_volume_num(fname),
                    "size_bytes": os.path.getsize(fpath),
                }
            )

    if not file_entries:
        return JSONResponse({"error": "No manga files found", "results": []})

    with get_db() as db:
        series_list = list(
            db.execute(
                "SELECT id, title, search_pattern FROM series WHERE deleted_at IS NULL"
            ).fetchall()
        )
        alias_map: dict[int, list[str]] = {}
        for r in db.execute("SELECT series_id, alias FROM series_aliases").fetchall():
            alias_map.setdefault(r["series_id"], []).append(r["alias"])

    series_patterns = _build_series_match_patterns(series_list, alias_map)

    for f in file_entries:
        f["matched_series"] = _match_file_to_series(f["filename"], series_patterns)

    groups: dict[str, list] = {}
    for f in file_entries:
        if not f["matched_series"]:
            key = extract_series_name(f["filename"])
            if key:
                groups.setdefault(key, []).append(f)

    newly_added: list[dict] = []
    for detected_name, group_files in groups.items():
        results_search, _ = await _m.search_series(detected_name)
        if not results_search:
            continue
        best = results_search[0]
        with get_db() as db:
            existing = db.execute(
                "SELECT id FROM series WHERE (anilist_id=? OR title=?)"
                " AND deleted_at IS NULL",
                (best["anilist_id"], best["title"]),
            ).fetchone()
            if existing:
                sid = existing["id"]
            else:
                rf_id = _m.resolve_root_folder_id(db)
                if rf_id is None:
                    # No library destination possible → skip this entry.
                    # The manual-import flow is multi-item by nature, so
                    # skipping rather than aborting the whole batch is
                    # the less-bad failure mode.
                    _m.log_event(
                        "error",
                        f"manual import: cannot add {best['title']!r} — "
                        f"no root folder configured",
                        db=db,
                    )
                    continue
                cur = db.execute(
                    "INSERT INTO series(title, search_pattern, anilist_id, mal_id, cover_url,"
                    " status, description, total_volumes, total_chapters, root_folder_id)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        best["title"],
                        best["title"],
                        best["anilist_id"],
                        best.get("mal_id"),
                        best.get("cover_url", ""),
                        best.get("status", ""),
                        best.get("description", ""),
                        best.get("volumes"),
                        best.get("chapters"),
                        rf_id,
                    ),
                )
                sid = cur.lastrowid
                if best.get("volumes"):
                    _m.create_volume_stubs(db, sid, int(best["volumes"]))
            new_s_row = db.execute(
                "SELECT id, title, search_pattern FROM series WHERE id=?", (sid,)
            ).fetchone()

        series_list.append(new_s_row)
        newly_added.append({"id": sid, "title": best["title"]})

        from metadata_service import refresh_series_metadata

        _m.create_background_task(
            refresh_series_metadata(
                sid, force=True, include_manifest=True, reason="manual_import"
            ),
            name=f"manual_import:{sid}:metadata",
        )

    if newly_added:
        with get_db() as db:
            alias_map = {}
            for r in db.execute(
                "SELECT series_id, alias FROM series_aliases"
            ).fetchall():
                alias_map.setdefault(r["series_id"], []).append(r["alias"])
        series_patterns = _build_series_match_patterns(series_list, alias_map)
        for f in file_entries:
            if not f["matched_series"]:
                f["matched_series"] = _match_file_to_series(
                    f["filename"], series_patterns
                )

    import_results = []
    for f in file_entries:
        ms = f["matched_series"]
        if not ms:
            import_results.append(
                {
                    "path": f["path"],
                    "ok": False,
                    "message": f"No series match for: {f['filename']}",
                }
            )
            continue

        with get_db() as db:
            s_row_raw = db.execute(
                "SELECT * FROM series WHERE id=?", (ms["id"],)
            ).fetchone()
            s_row = dict(s_row_raw) if s_row_raw else None
            dst_dir = _m._series_library_dir(db, s_row["id"]) if s_row else None

        if not s_row or not dst_dir:
            import_results.append(
                {
                    "path": f["path"],
                    "ok": False,
                    "message": "Series destination not found",
                }
            )
            continue
        vol_num = f["vol_num"]
        dst_fname = _m.build_filename(s_row["title"], vol_num, f["filename"])

        try:
            os.makedirs(dst_dir, exist_ok=True)
            dst_path = _m.safe_join_under(dst_dir, dst_fname)
            if import_mode == "hardlink":
                if os.path.exists(dst_path):
                    os.remove(dst_path)
                os.link(f["path"], dst_path)
            elif import_mode == "move":
                shutil.move(f["path"], dst_path)
            else:
                shutil.copy2(f["path"], dst_path)

            if remove_source and import_mode != "move" and os.path.exists(f["path"]):
                os.remove(f["path"])

            file_size = (
                os.path.getsize(dst_path)
                if os.path.exists(dst_path)
                else f["size_bytes"]
            )
            imported_at = datetime.utcnow().isoformat()
            file_qual = _m.quality_from_filename(dst_path)
            with get_db() as db:
                if vol_num is not None:
                    vol_row = db.execute(
                        "SELECT id FROM volumes WHERE series_id=? AND volume_num=?",
                        (ms["id"], float(vol_num)),
                    ).fetchone()
                    if vol_row:
                        db.execute(
                            "UPDATE volumes SET status='downloaded', import_path=?,"
                            " size_bytes=?, quality=COALESCE(quality,?), imported_at=? WHERE id=?",
                            (
                                dst_path,
                                file_size,
                                file_qual,
                                imported_at,
                                vol_row["id"],
                            ),
                        )
                    else:
                        db.execute(
                            "INSERT INTO volumes(series_id, volume_num, status, import_path,"
                            " size_bytes, quality, imported_at) VALUES(?,?,?,?,?,?,?)",
                            (
                                ms["id"],
                                float(vol_num),
                                "downloaded",
                                dst_path,
                                file_size,
                                file_qual,
                                imported_at,
                            ),
                        )
                vol_label = (
                    f"Vol {vol_num_to_display(vol_num)}"
                    if vol_num is not None
                    else "Unknown"
                )
                _m.add_history(
                    db,
                    "imported",
                    ms["id"],
                    s_row["title"],
                    vol_label,
                    source_title=f["filename"],
                    data={"dst_path": dst_path, "auto_import": True},
                )

            import_results.append(
                {
                    "path": f["path"],
                    "ok": True,
                    "dst": dst_path,
                    "series": s_row["title"],
                    "volume": vol_num,
                }
            )
        except Exception as e:
            import_results.append({"path": f["path"], "ok": False, "message": str(e)})
            _m.log_event("error", f"Auto-import failed ({f['filename']}): {e}")

    ok_count = sum(1 for r in import_results if r["ok"])
    if ok_count:
        await _m.trigger_komga_scan()

    return JSONResponse(
        {
            "results": import_results,
            "imported": ok_count,
            "total": len(import_results),
            "new_series": newly_added,
        }
    )


@router.post("/api/manual-import/import")
async def manual_import_process(request: Request):
    import main as _m

    data = await request.json()
    entries = data.get("entries", [])
    import_mode = get_cfg("import_mode", "hardlink")
    results = []

    for entry in entries:
        src_path = os.path.realpath(entry.get("path", ""))
        series_id = entry.get("series_id")
        vol_num = entry.get("volume_num")

        if not src_path or not series_id or not os.path.isfile(src_path):
            results.append({"path": src_path, "ok": False, "message": "File not found"})
            continue
        if any(
            src_path == p or src_path.startswith(p + "/")
            for p in _BLOCKED_SCAN_PREFIXES
        ):
            results.append(
                {"path": src_path, "ok": False, "message": "Path not allowed"}
            )
            continue

        with get_db() as db:
            s_raw = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
            s = dict(s_raw) if s_raw else None
            dst_dir = _m._series_library_dir(db, s["id"]) if s else None

        if not s:
            results.append(
                {"path": src_path, "ok": False, "message": "Series not found"}
            )
            continue
        if not dst_dir:
            results.append(
                {
                    "path": src_path,
                    "ok": False,
                    "message": "Series destination not found",
                }
            )
            continue

        fname = os.path.basename(src_path)
        if vol_num is not None:
            fname = _m.build_filename(s["title"], float(vol_num), fname)

        try:
            os.makedirs(dst_dir, exist_ok=True)
            dst_path = _m.safe_join_under(dst_dir, fname)
            if import_mode == "hardlink":
                if os.path.exists(dst_path):
                    os.remove(dst_path)
                os.link(src_path, dst_path)
            elif import_mode == "move":
                shutil.move(src_path, dst_path)
            else:
                shutil.copy2(src_path, dst_path)

            dst_path = _m._maybe_convert_to_cbz(dst_path)
            with get_db() as _ci_db:
                _ci_tags = [
                    r["tag"]
                    for r in _ci_db.execute(
                        "SELECT tag FROM series_tags WHERE series_id=?", (series_id,)
                    ).fetchall()
                ]
            _m._try_inject_comicinfo(
                dst_path,
                s,
                volume_num=float(vol_num) if vol_num is not None else None,
                tags=_ci_tags,
            )

            file_size = os.path.getsize(dst_path) if os.path.exists(dst_path) else 0
            imported_at = datetime.utcnow().isoformat()
            file_qual = _m.quality_from_filename(dst_path)
            with get_db() as db:
                vol_label = (
                    f"Vol {vol_num_to_display(vol_num)}"
                    if vol_num is not None
                    else "Unknown"
                )
                if vol_num is not None:
                    existing = db.execute(
                        "SELECT id FROM volumes WHERE series_id=? AND volume_num=?",
                        (series_id, float(vol_num)),
                    ).fetchone()
                    if existing:
                        db.execute(
                            "UPDATE volumes SET status='downloaded', import_path=?,"
                            " size_bytes=?, quality=COALESCE(quality,?), imported_at=? WHERE id=?",
                            (
                                dst_path,
                                file_size,
                                file_qual,
                                imported_at,
                                existing["id"],
                            ),
                        )
                    else:
                        db.execute(
                            "INSERT INTO volumes(series_id, volume_num, status, import_path,"
                            " size_bytes, quality, imported_at) VALUES(?,?,?,?,?,?,?)",
                            (
                                series_id,
                                float(vol_num),
                                "downloaded",
                                dst_path,
                                file_size,
                                file_qual,
                                imported_at,
                            ),
                        )
                _m.add_history(
                    db,
                    "imported",
                    series_id,
                    s["title"],
                    vol_label,
                    source_title=fname,
                    data={"dst_path": dst_path, "import_mode": import_mode},
                )
            results.append({"path": src_path, "ok": True, "dst": dst_path})
        except Exception as e:
            results.append({"path": src_path, "ok": False, "message": str(e)})
            _m.log_event("error", f"Manual import failed for {fname}: {e}", series_id)

    ok_count = sum(1 for r in results if r["ok"])
    if ok_count:
        await _m.trigger_komga_scan()
    return JSONResponse(
        {"results": results, "imported": ok_count, "total": len(results)}
    )
