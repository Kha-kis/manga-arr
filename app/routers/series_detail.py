"""Series detail and read-only operations - detail page, metadata health, reconcile."""

from fastapi import Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response as _Resp,
)

from routers.series_ import router
from routers._templates import templates
from shared import get_cfg, get_db, get_root_folders, quality_rank, vol_num_to_display
from reconcile_map import reconcile_series_chapter_map
import main as _m
from collections import defaultdict
import json
import re
import math
import asyncio
import httpx
from datetime import datetime


def _chapter_map_to_ranges(chapter_vol_map_json: str | None) -> str:
    """Convert {ch_str: vol_int} JSON to human-readable 'one range per line' format."""
    if not chapter_vol_map_json:
        return ""
    try:
        cvm = json.loads(chapter_vol_map_json)
    except Exception:
        return ""
    vol_to_chs: dict[int, list[int]] = defaultdict(list)
    for ch_str, vol_num in cvm.items():
        try:
            vol_to_chs[int(vol_num)].append(int(float(ch_str)))
        except (ValueError, TypeError):
            pass
    lines = []
    for vol_num in sorted(vol_to_chs.keys()):
        chs = sorted(vol_to_chs[vol_num])
        if not chs:
            continue
        if len(chs) == 1:
            lines.append(str(chs[0]))
        elif chs[-1] - chs[0] + 1 == len(chs):
            lines.append(f"{chs[0]}-{chs[-1]}")
        else:
            lines.append(", ".join(str(c) for c in chs))
    return "\n".join(lines)


@router.get("/series/{series_id}", response_class=HTMLResponse)
async def series_detail(request: Request, series_id: int):
    """Series detail page."""
    with get_db() as db:
        s = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
        if not s:
            return HTMLResponse("Not found", status_code=404)
        all_rows = db.execute(
            "SELECT * FROM volumes WHERE series_id=? "
            "ORDER BY COALESCE(volume_num, 9999), COALESCE(chapter_num, 9999), id",
            (series_id,),
        ).fetchall()
        all_chapters = db.execute(
            "SELECT * FROM chapters WHERE series_id=? ORDER BY chapter_num",
            (series_id,),
        ).fetchall()
        stats = _m.get_series_stats(db, series_id)
        root_folders = get_root_folders(db)
        quality_profiles = db.execute(
            "SELECT id, name FROM quality_profiles ORDER BY name"
        ).fetchall()
        language_profiles = db.execute(
            "SELECT id, name FROM language_profiles ORDER BY name"
        ).fetchall()

    volumes = [v for v in all_rows if v["volume_num"] is not None]
    raw_packs = [
        v
        for v in all_rows
        if v["volume_num"] is None and v["status"] in ("grabbed", "downloaded")
    ]

    ch_map: dict = {}
    if s["chapter_vol_map"]:
        try:
            ch_map = json.loads(s["chapter_vol_map"])
        except Exception:
            pass
    total_vols = s["total_volumes"]
    total_chs = s["total_chapters"]

    def _vol_set_label(vols: set) -> str:
        if not vols:
            return ""
        sv = sorted(vols)
        if len(sv) == 1:
            return f"Vol {sv[0]}"
        runs, start, prev = [], sv[0], sv[0]
        for v in sv[1:]:
            if v == prev + 1:
                prev = v
            else:
                runs.append((start, prev))
                start = prev = v
        runs.append((start, prev))
        parts = [f"{a}" if a == b else f"{a}–{b}" for a, b in runs]
        return "Vol " + ", ".join(parts)

    def _enrich_pack(p) -> dict:
        pt = p["pack_type"] or "volume"
        name = p["torrent_name"] or ""
        ch_label = ""
        vol_label = ""
        covers: set = set()

        if pt == "complete":
            vol_label = "Complete Series"
            if total_vols:
                covers = set(range(1, total_vols + 1))
        elif pt == "volume":
            rs, re_ = p["vol_range_start"], p["vol_range_end"]
            if rs is not None and re_ is not None:
                vol_label = f"Vol {vol_num_to_display(rs)}–{vol_num_to_display(re_)}"
                covers = set(range(int(rs), int(re_) + 1))
            else:
                vn = _m.extract_volume_num(name)
                vol_label = f"Vol {vol_num_to_display(vn)}" if vn else ""
                if vn:
                    covers = {int(vn)}
        elif pt == "chapter":
            rng = _m.extract_volume_range(name)
            if rng:
                s_ch, e_ch = rng
                ch_label = (
                    f"Ch {int(s_ch)}–{int(e_ch)}" if s_ch != e_ch else f"Ch {int(s_ch)}"
                )
                covers = _m.chapters_to_volume_set(
                    s_ch, e_ch, ch_map, total_chs, total_vols
                )
            else:
                m = re.search(
                    r"(?:ch(?:apter)?s?\.?\s*|#\s*)(\d{1,4}(?:\.\d+)?)\b",
                    name,
                    re.IGNORECASE,
                )
                if not m:
                    m = re.search(r"(?:^|[\s\[({])(\d{2,4})(?:[\s\])}]|$)", name)
                if m:
                    ch = float(m.group(1))
                    ch_label = f"Ch {int(ch)}" if ch == int(ch) else f"Ch {ch}"
                    covers = _m.chapters_to_volume_set(
                        ch, ch, ch_map, total_chs, total_vols
                    )
                else:
                    ch_label = ""
            vol_label = _vol_set_label(covers) if covers else ""

        return dict(p) | {
            "ch_label": ch_label,
            "vol_label": vol_label,
            "covers": sorted(covers),
        }

    enriched = [_enrich_pack(p) for p in raw_packs]
    has_complete = any(p["pack_type"] == "complete" for p in enriched)
    seen_keys: dict[str, int] = {}
    packs: list[dict] = []
    for p in enriched:
        key = f"{p['ch_label']}|{p['vol_label']}|{p['pack_type']}"
        if key in seen_keys:
            packs[seen_keys[key]]["dup_count"] = (
                packs[seen_keys[key]].get("dup_count", 1) + 1
            )
        else:
            p["dup_count"] = 1
            p["superseded"] = has_complete and p["pack_type"] != "complete"
            seen_keys[key] = len(packs)
            packs.append(p)

    def _pack_sort_key(p):
        if p["pack_type"] == "complete":
            return (0, 0)
        if p["pack_type"] == "volume":
            return (1, p["vol_range_start"] or 0)
        m = re.search(r"\d+", p["ch_label"])
        return (2, float(m.group()) if m else 9999)

    packs.sort(key=_pack_sort_key)

    ch_map_count = 0
    if s["chapter_vol_map"]:
        try:
            ch_map_count = len(json.loads(s["chapter_vol_map"]))
        except Exception:
            pass

    with get_db() as db:
        _iq_rows = db.execute(
            "SELECT download_id, status FROM import_queue WHERE series_id=?"
            " AND status IN ('pending','partial')",
            (s["id"],),
        ).fetchall()
        pending_dl_ids: set[str] = {
            (r["download_id"] or "").lower()
            for r in _iq_rows
            if r["download_id"] and r["status"] == "pending"
        }
        review_dl_ids: set[str] = {
            (r["download_id"] or "").lower()
            for r in _iq_rows
            if r["download_id"] and r["status"] == "partial"
        }
        aliases = db.execute(
            "SELECT id, alias FROM series_aliases WHERE series_id=? ORDER BY alias",
            (s["id"],),
        ).fetchall()
        series_tags = []
        if s["tags"]:
            try:
                series_tags = json.loads(s["tags"])
            except Exception:
                pass
        all_tags_rows = db.execute(
            "SELECT tags FROM series WHERE tags IS NOT NULL"
        ).fetchall()

    all_tags: set[str] = set()
    for r in all_tags_rows:
        try:
            all_tags.update(json.loads(r["tags"]))
        except Exception:
            pass

    chapters_by_vol: dict = defaultdict(list)
    for ch in all_chapters:
        chapters_by_vol[ch["volume_id"]].append(ch)
    unlinked_chapters = list(chapters_by_vol.pop(None, []))

    ch_counts: dict = {}
    for vol_id, chs in chapters_by_vol.items():
        ch_counts[vol_id] = {
            "total": len(chs),
            "downloaded": sum(1 for c in chs if c["status"] == "downloaded"),
            "grabbed": sum(1 for c in chs if c["status"] == "grabbed"),
            "wanted": sum(1 for c in chs if c["status"] == "wanted" and c["monitored"]),
        }

    dl_stages: dict[str, str] = {}
    from app.routers.download_clients import get_client_for_protocol as _gcp

    with get_db() as _qb_db:
        _qb_c = _gcp(_qb_db, "torrent")
    if _qb_c:
        _qb_host = (_qb_c.get("host") or "").rstrip("/")
        _qb_user = _qb_c.get("username") or ""
        _qb_pw = _qb_c.get("password") or ""
        _qb_cat = _qb_c.get("category") or get_cfg("category")

        def _s_stage(state: str) -> str:
            sl = (state or "").lower()
            if "stalled" in sl and "up" not in sl:
                return "stalled"
            if "error" in sl or "missing" in sl:
                return "error"
            if "paused" in sl:
                return "paused"
            if "queued" in sl or "checking" in sl:
                return "queued_dl"
            if "upload" in sl or ("stalled" in sl and "up" in sl):
                return "completed"
            return "downloading"

        try:
            async with httpx.AsyncClient(timeout=5) as _qb:
                _r = await _qb.post(
                    f"{_qb_host}/api/v2/auth/login",
                    data={"username": _qb_user, "password": _qb_pw},
                )
                if "Ok" in _r.text:
                    _r2 = await _qb.get(
                        f"{_qb_host}/api/v2/torrents/info", params={"category": _qb_cat}
                    )
                    if _r2.status_code == 200:
                        for _t in _r2.json():
                            _h = _t.get("hash", "").lower()
                            if _h:
                                dl_stages[_h] = _s_stage(_t.get("state", ""))
        except Exception:
            pass
    active_dl_ids: set[str] = set(dl_stages.keys())

    effective_cutoff = (s["quality_cutoff"] or "").strip() or get_cfg(
        "quality_cutoff", ""
    )
    cutoff_rank_val = quality_rank(effective_cutoff)

    with get_db() as _swy_db:
        _swy_row = _swy_db.execute(
            "SELECT 1 FROM download_clients WHERE type='suwayomi' AND enabled=1 LIMIT 1"
        ).fetchone()
        swy_vol_jobs = _build_swy_vol_jobs(_swy_db, s["id"])
    suwayomi_enabled = _swy_row is not None

    from reconcile_map import build_metadata_health

    try:
        metadata_health = build_metadata_health(series_id)
    except Exception as _e:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "build_metadata_health(%s) failed: %r", series_id, _e
        )
        metadata_health = None

    return templates.TemplateResponse(
        request,
        "series.html",
        {
            "s": s,
            "volumes": volumes,
            "packs": packs,
            "stats": stats,
            "root_folders": root_folders,
            "ch_map_count": ch_map_count,
            "aliases": aliases,
            "series_tags": series_tags,
            "all_tags": sorted(all_tags),
            "chapters_by_vol": dict(chapters_by_vol),
            "unlinked_chapters": unlinked_chapters,
            "ch_counts": ch_counts,
            "pending_dl_ids": pending_dl_ids,
            "review_dl_ids": review_dl_ids,
            "active_dl_ids": active_dl_ids,
            "dl_stages": dl_stages,
            "quality_cutoff": effective_cutoff,
            "cutoff_rank": cutoff_rank_val,
            "chapter_map_text": _chapter_map_to_ranges(s["chapter_vol_map"]),
            "quality_profiles": quality_profiles,
            "language_profiles": language_profiles,
            "suwayomi_enabled": suwayomi_enabled,
            "swy_vol_jobs": swy_vol_jobs,
            "metadata_health": metadata_health,
        },
    )


@router.get("/api/series/{series_id}/metadata-health", response_class=HTMLResponse)
async def api_series_metadata_health(request: Request, series_id: int):
    """Return metadata health payload for a series."""
    from reconcile_map import build_metadata_health

    try:
        payload = build_metadata_health(series_id)
    except Exception as _e:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "build_metadata_health(%s) failed: %r", series_id, _e
        )
        return JSONResponse(
            {"error": "series not found or helper failed"}, status_code=404
        )
    if not payload or payload.get("title") is None:
        return JSONResponse({"error": "series not found"}, status_code=404)
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request, "partials/metadata_health_panel.html", {"metadata_health": payload}
        )
    return JSONResponse(payload)


@router.get("/api/series/{series_id}/reconcile/preview", response_class=HTMLResponse)
async def api_series_reconcile_preview(request: Request, series_id: int):
    """Dry-run preview of chapter→volume reconciler."""
    from reconcile_map import reconcile_series_chapter_map

    plan = reconcile_series_chapter_map(series_id, dry_run=True)
    if (
        not plan.get("rows")
        and plan.get("ok_move", 0) == 0
        and plan.get("already_correct", 0) == 0
        and plan.get("no_map_entry", 0) == 0
    ):
        with get_db() as _db:
            exists = _db.execute(
                "SELECT 1 FROM series WHERE id=?", (series_id,)
            ).fetchone()
        if not exists:
            return JSONResponse({"error": "series not found"}, status_code=404)

    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request,
            "partials/reconcile_preview_panel.html",
            {"plan": plan, "series_id": series_id},
        )
    return JSONResponse(plan)


@router.post(
    "/api/series/{series_id}/reconcile/refresh-then-preview",
    response_class=HTMLResponse,
)
async def api_series_reconcile_refresh_then_preview(request: Request, series_id: int):
    """Refresh MangaDex map then show preview."""
    from reconcile_map import build_metadata_health, reconcile_series_chapter_map

    with get_db() as db:
        exists = db.execute("SELECT 1 FROM series WHERE id=?", (series_id,)).fetchone()
    if not exists:
        return JSONResponse({"error": "series not found"}, status_code=404)

    refresh_ok = False
    refresh_error: str | None = None
    try:
        refresh_ok = bool(await _m.refresh_mangadex_map(series_id))
    except Exception as _e:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "refresh_mangadex_map(%s) raised: %r", series_id, _e
        )
        refresh_error = f"{type(_e).__name__}: {str(_e)[:160]}"

    if not refresh_ok:
        err_msg = (
            refresh_error or "MangaDex refresh returned no mapping for this series."
        )
        _m.log_event(
            "reconcile",
            f"Refresh-then-preview: refresh failed ({err_msg})",
            series_id,
        )
        if request.headers.get("HX-Request") == "true":
            return templates.TemplateResponse(
                request,
                "partials/reconcile_refresh_error.html",
                {"series_id": series_id, "error": err_msg},
            )
        return JSONResponse(
            {"refreshed": False, "error": err_msg},
            status_code=502 if refresh_error else 200,
        )

    plan = reconcile_series_chapter_map(series_id, dry_run=True)
    health = build_metadata_health(series_id)
    _m.log_event(
        "reconcile",
        f"Refresh-then-preview: map refreshed, plan has "
        f"{plan.get('ok_move', 0)} safe move(s)",
        series_id,
    )

    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request,
            "partials/reconcile_refresh_then_preview.html",
            {
                "plan": plan,
                "series_id": series_id,
                "metadata_health": health,
                "just_refreshed": True,
            },
        )
    return JSONResponse(
        {
            "refreshed": True,
            "chapter_vol_map_size": health["chapter_vol_map_size"],
            "state": health["state"],
            "plan": {
                k: plan.get(k, 0)
                for k in (
                    "ok_move",
                    "already_correct",
                    "no_map_entry",
                    "target_volume_missing",
                    "target_ambiguous",
                    "special_parent",
                )
            },
        }
    )


@router.post("/api/series/{series_id}/reconcile/apply", response_class=HTMLResponse)
async def api_series_reconcile_apply(request: Request, series_id: int):
    """Apply reconciler's safe moves."""
    result = reconcile_series_chapter_map(series_id, dry_run=False)
    _m.log_event(
        "reconcile",
        f"Reconcile apply: {result['applied']} moved, {result['skipped']} skipped",
        series_id,
    )
    follow_up = reconcile_series_chapter_map(series_id, dry_run=True)

    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request,
            "partials/reconcile_preview_panel.html",
            {
                "plan": follow_up,
                "series_id": series_id,
                "just_applied": result["applied"],
            },
        )
    return JSONResponse(
        {
            "applied": result["applied"],
            "skipped": result["skipped"],
            "summary": {
                k: result.get(k, 0)
                for k in (
                    "ok_move",
                    "already_correct",
                    "no_map_entry",
                    "target_volume_missing",
                    "target_ambiguous",
                    "special_parent",
                )
            },
        }
    )


def _build_swy_vol_jobs(db, series_id: int) -> dict:
    """Return {volume_num: {progress, total, status, error}} for active Suwayomi jobs."""
    rows = db.execute(
        "SELECT volume_num, progress, total, status, error"
        " FROM suwayomi_downloads"
        " WHERE series_id=? AND volume_num IS NOT NULL AND status IN ('queued','error')",
        (series_id,),
    ).fetchall()
    return {float(r["volume_num"]): dict(r) for r in rows}


async def _get_volume_row_ctx(series_id: int, volume_id: int) -> dict:
    """Build template context for a single volume row partial."""
    with get_db() as db:
        s = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
        v = db.execute(
            "SELECT * FROM volumes WHERE id=? AND series_id=?", (volume_id, series_id)
        ).fetchone()
        vchs = db.execute(
            "SELECT * FROM chapters WHERE volume_id=? AND series_id=? ORDER BY chapter_num",
            (volume_id, series_id),
        ).fetchall()
        _iq = db.execute(
            "SELECT download_id, status FROM import_queue WHERE series_id=?"
            " AND status IN ('pending','partial')",
            (series_id,),
        ).fetchall()
    swy_vol_jobs = _build_swy_vol_jobs(db, series_id)
    pending_dl_ids = {
        (r["download_id"] or "").lower()
        for r in _iq
        if r["download_id"] and r["status"] == "pending"
    }
    review_dl_ids = {
        (r["download_id"] or "").lower()
        for r in _iq
        if r["download_id"] and r["status"] == "partial"
    }
    vct = {
        "total": len(vchs),
        "downloaded": sum(1 for c in vchs if c["status"] == "downloaded"),
        "grabbed": sum(1 for c in vchs if c["status"] == "grabbed"),
        "wanted": sum(1 for c in vchs if c["status"] == "wanted" and c["monitored"]),
    }
    effective_cutoff = (s["quality_cutoff"] or "").strip() if s else ""
    effective_cutoff = effective_cutoff or get_cfg("quality_cutoff", "")
    return {
        "s": s,
        "v": v,
        "vchs": list(vchs),
        "vct": vct,
        "quality_cutoff": effective_cutoff,
        "cutoff_rank": quality_rank(effective_cutoff),
        "pending_dl_ids": pending_dl_ids,
        "review_dl_ids": review_dl_ids,
        "active_dl_ids": set(),
        "dl_stages": {},
        "swy_vol_jobs": swy_vol_jobs,
    }


@router.post("/series/{series_id}/rescan")
async def rescan_series(request: Request, series_id: int):
    """Rescan series folder."""
    import main as _m

    with get_db() as db:
        result = _m.rescan_series_folder(db, series_id)
    parts = []
    if result["found"]:
        parts.append(f"{result['found']} file(s) on disk")
    if result["recovered"]:
        parts.append(f"{result['recovered']} marked downloaded")
    if result["missing"]:
        parts.append(f"{result['missing']} reset to wanted (files missing)")
    if result["lost"]:
        parts.append(f"{result['lost']} reset to wanted (grab lost)")
    if result.get("created"):
        parts.append(f"{result['created']} new stub(s) created from disk")
    msg = "Rescan: " + (", ".join(parts) if parts else "nothing changed")
    _m.log_event("rescan", msg, series_id)
    if request.headers.get("HX-Request") == "true":
        return _Resp(
            headers={
                "HX-Trigger": json.dumps({"showToast": {"msg": msg, "type": "success"}})
            }
        )
    return RedirectResponse(f"/series/{series_id}", status_code=303)


@router.post("/api/series/{series_id}/reinject-metadata")
async def reinject_metadata(series_id: int):
    """Re-inject ComicInfo.xml."""
    import main as _m
    import os

    with get_db() as db:
        s = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
        if not s:
            return JSONResponse({"ok": False, "message": "Series not found"})
        tags = [
            r["tag"]
            for r in db.execute(
                "SELECT tag FROM series_tags WHERE series_id=?", (series_id,)
            ).fetchall()
        ]
        vols = db.execute(
            "SELECT volume_num, import_path FROM volumes"
            " WHERE series_id=? AND status='downloaded' AND import_path IS NOT NULL",
            (series_id,),
        ).fetchall()
        chaps = db.execute(
            "SELECT chapter_num, import_path FROM chapters"
            " WHERE series_id=? AND status='downloaded' AND import_path IS NOT NULL",
            (series_id,),
        ).fetchall()

    ok_count = skip_count = fail_count = 0
    for v in vols:
        if not os.path.isfile(v["import_path"]):
            skip_count += 1
            continue
        xml = _m.build_comicinfo_xml(dict(s), volume_num=v["volume_num"], tags=tags)
        if _m.inject_comicinfo(v["import_path"], xml):
            ok_count += 1
        else:
            fail_count += 1
    for c in chaps:
        if not os.path.isfile(c["import_path"]):
            skip_count += 1
            continue
        xml = _m.build_comicinfo_xml(dict(s), chapter_num=c["chapter_num"], tags=tags)
        if _m.inject_comicinfo(c["import_path"], xml):
            ok_count += 1
        else:
            fail_count += 1

    _m.log_event(
        "metadata",
        f"Re-injected ComicInfo.xml: {ok_count} updated, "
        f"{skip_count} missing, {fail_count} skipped (non-CBZ)",
        series_id,
    )
    return JSONResponse(
        {
            "ok": True,
            "updated": ok_count,
            "skipped_missing": skip_count,
            "skipped_format": fail_count,
        }
    )


@router.post("/library/rescan")
async def rescan_all_series(request: Request):
    """Rescan entire library."""
    _m.create_background_task(_rescan_all_impl(), name="series:rescan_all")
    if request.headers.get("HX-Request") == "true":
        return _Resp(
            headers={
                "HX-Trigger": json.dumps(
                    {
                        "showToast": {
                            "msg": "Library rescan started in background",
                            "type": "success",
                        }
                    }
                )
            }
        )
    return RedirectResponse("/health", status_code=303)


async def _rescan_all_impl():
    """Core logic for full library rescan."""
    import main as _m

    with get_db() as db:
        series_ids = [r["id"] for r in db.execute("SELECT id FROM series").fetchall()]
        total = {"found": 0, "recovered": 0, "missing": 0, "lost": 0, "created": 0}
        for sid in series_ids:
            r = _m.rescan_series_folder(db, sid)
            total["found"] += r["found"]
            total["recovered"] += r["recovered"]
            total["missing"] += r["missing"]
            total["lost"] += r["lost"]
            total["created"] += r.get("created", 0)
    _m.log_event(
        "rescan",
        f"Full library rescan: {total['found']} files, "
        f"{total['recovered']} recovered, {total['missing']} missing, "
        f"{total['lost']} grabs lost, {total['created']} stubs created",
    )


@router.post("/library/mark-all-grabbed-downloaded")
async def mark_all_grabbed_downloaded(request: Request):
    """Mark all grabbed volumes as downloaded across library."""
    with get_db() as db:
        cur = db.execute(
            "UPDATE volumes SET status='downloaded' WHERE status='grabbed' AND volume_num IS NOT NULL"
        )
        marked = cur.rowcount
    _m.log_event(
        "download_complete",
        f"Manually marked all grabbed volumes as downloaded ({marked})",
        0,
    )
    if request.headers.get("HX-Request") == "true":
        return _Resp(headers={"HX-Refresh": "true"})
    return RedirectResponse("/?sort=added", status_code=303)
