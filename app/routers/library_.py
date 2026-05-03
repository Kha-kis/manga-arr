"""Library pages — wanted, cutoff-unmet, stats, calendar."""
import asyncio
import json
import shutil as _shutil
from collections import defaultdict as _dd

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from routers._templates import templates
from shared import get_cfg, get_db, get_root_folders, is_htmx, quality_rank

router = APIRouter()


@router.get("/wanted", response_class=HTMLResponse)
async def wanted(request: Request, page: int = 1):
    per_page = 100
    offset   = (page - 1) * per_page

    with get_db() as db:
        total_count = db.execute(
            "SELECT COUNT(*) FROM volumes v JOIN series s ON s.id = v.series_id "
            "WHERE v.status='wanted' AND s.monitored=1 AND v.monitored=1"
            "  AND s.deleted_at IS NULL"
        ).fetchone()[0]
        wanted_rows = db.execute(
            "SELECT v.*, s.title as series_title, s.id as series_id "
            "FROM volumes v JOIN series s ON s.id = v.series_id "
            "WHERE v.status='wanted' AND s.monitored=1 AND v.monitored=1 "
            "  AND s.deleted_at IS NULL "
            "ORDER BY s.title, COALESCE(v.volume_num, 9999) "
            "LIMIT ? OFFSET ?",
            (per_page, offset)
        ).fetchall()
        wanted_chapters = db.execute(
            "SELECT c.*, s.title as series_title, s.id as series_id "
            "FROM chapters c "
            "JOIN series s ON s.id = c.series_id "
            "LEFT JOIN volumes v ON v.id = c.volume_id "
            "WHERE c.status='wanted' AND c.monitored=1 AND s.monitored=1 "
            "  AND s.deleted_at IS NULL "
            "  AND (c.volume_id IS NULL OR v.status = 'downloaded') "
            "ORDER BY s.title, c.chapter_num"
        ).fetchall()

    # Merge volumes and chapters into unified per-series groups
    all_series: dict = {}
    for row in wanted_rows:
        sid = row['series_id']
        if sid not in all_series:
            all_series[sid] = {'series_id': sid, 'series_title': row['series_title'], 'volumes': [], 'chapters': []}
        all_series[sid]['volumes'].append(row)
    for row in wanted_chapters:
        sid = row['series_id']
        if sid not in all_series:
            all_series[sid] = {'series_id': sid, 'series_title': row['series_title'], 'volumes': [], 'chapters': []}
        all_series[sid]['chapters'].append(row)
    wanted_groups = sorted(all_series.values(), key=lambda g: g['series_title'])

    total_pages = max(1, (total_count + per_page - 1) // per_page) if total_count else 1
    total_wanted = sum(len(g['volumes']) + len(g['chapters']) for g in wanted_groups)

    return templates.TemplateResponse(request, "wanted.html", {
        "active_tab":       "missing",
        "wanted_groups":    wanted_groups,
        "total_wanted":     total_wanted,
        "page":             page,
        "total_pages":      total_pages,
        "total_count":      total_count,
        # cutoff tab context (not loaded on this route)
        "cutoff_unmet":     [],
        "cutoff_total":     0,
    })


@router.post("/wanted/search-all")
async def search_all_wanted(request: Request):
    """Trigger a search-and-grab for every wanted volume across all monitored series."""
    import main as _m
    with get_db() as db:
        series_list = db.execute(
            "SELECT id, title, search_pattern FROM series "
            "WHERE monitored=1 AND deleted_at IS NULL AND EXISTS ("
            "  SELECT 1 FROM volumes WHERE series_id=series.id AND status='wanted'"
            ")"
        ).fetchall()
    n = len(series_list)
    for s in series_list:
        asyncio.create_task(_m.grab_existing(s['id'], s['title'], s['search_pattern']))
    if is_htmx(request):
        return Response(headers={"HX-Trigger": json.dumps({
            "showToast": {"msg": f"Search started for {n} series", "type": "success"}
        })})
    return RedirectResponse("/wanted", status_code=303)


@router.post("/series/{series_id}/grab-wanted")
async def grab_all_wanted(request: Request, series_id: int):
    import main as _m
    with get_db() as db:
        s = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
    if s:
        asyncio.create_task(_m.grab_existing(series_id, s['title'], s['search_pattern']))
    if is_htmx(request):
        return Response(headers={"HX-Trigger": json.dumps({
            "showToast": {"msg": f"Search started for {s['title']}", "type": "success"}
        })})
    return RedirectResponse("/wanted", status_code=303)


@router.get("/wanted/cutoff-unmet", response_class=HTMLResponse)
async def cutoff_unmet_page(request: Request):
    from shared import quality_rank, get_cfg
    global_cutoff = get_cfg('quality_cutoff', '')
    with get_db() as db:
        rows = db.execute("""
            SELECT v.id, v.series_id, v.volume_num, v.quality, v.import_path,
                   s.title as series_title, s.quality_cutoff, s.quality_profile_id,
                   qp.cutoff as profile_cutoff,
                   v.grabbed_at
            FROM volumes v
            JOIN series s ON s.id = v.series_id
            LEFT JOIN quality_profiles qp ON qp.id = s.quality_profile_id
            WHERE v.status = 'downloaded'
              AND s.monitored = 1
              AND s.deleted_at IS NULL
        """).fetchall()
        cutoff_unmet = []
        for row in rows:
            # Resolve effective cutoff: per-series > profile > global
            effective_cutoff = (row['quality_cutoff'] or row['profile_cutoff'] or global_cutoff or '').lower()
            if not effective_cutoff:
                continue
            vol_quality = (row['quality'] or '').lower()
            if not vol_quality:
                # NULL quality — can't determine if below cutoff, skip
                continue
            vol_cut_rank = quality_rank(effective_cutoff)
            vol_q_rank   = quality_rank(vol_quality)
            if vol_cut_rank > 0 and vol_q_rank < vol_cut_rank:
                cutoff_unmet.append(dict(row) | {
                    'effective_cutoff': effective_cutoff,
                    'current_quality':  vol_quality,
                })

    return templates.TemplateResponse(request, "wanted.html", {
        "active_tab":       "cutoff_unmet",
        "cutoff_unmet":     cutoff_unmet,
        "cutoff_total":     len(cutoff_unmet),
        # missing tab context (empty — not loaded on this route)
        "wanted_groups":    [],
        "total_wanted":     0,
        "total_count":      0,
        "page":             1,
        "total_pages":      1,
    })


@router.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    with get_db() as db:
        overview = db.execute("""
            SELECT
                COUNT(DISTINCT s.id) as total_series,
                SUM(CASE WHEN UPPER(s.status)='RELEASING' THEN 1 ELSE 0 END) as releasing,
                SUM(CASE WHEN UPPER(s.status) IN ('FINISHED','COMPLETED') THEN 1 ELSE 0 END) as finished,
                SUM(CASE WHEN s.monitored=1 THEN 1 ELSE 0 END) as monitored
            FROM series s
            WHERE s.deleted_at IS NULL
        """).fetchone()

        volumes_stats = db.execute("""
            SELECT
                COUNT(CASE WHEN volume_num IS NOT NULL THEN 1 END) as total,
                COUNT(CASE WHEN status='downloaded' AND volume_num IS NOT NULL THEN 1 END) as downloaded,
                COUNT(CASE WHEN status='grabbed'    AND volume_num IS NOT NULL THEN 1 END) as grabbed,
                COUNT(CASE WHEN status='wanted'     AND volume_num IS NOT NULL THEN 1 END) as wanted,
                COALESCE(SUM(CASE WHEN status='downloaded' THEN size_bytes ELSE 0 END), 0) as total_bytes
            FROM volumes
        """).fetchone()

        history_counts = db.execute("""
            SELECT event_type, COUNT(*) as cnt FROM history GROUP BY event_type
        """).fetchall()

        daily_grabs = db.execute("""
            SELECT DATE(created_at) as day, COUNT(*) as cnt
            FROM history WHERE event_type='grabbed' AND created_at >= DATE('now', '-30 days')
            GROUP BY day ORDER BY day
        """).fetchall()

        top_indexers = db.execute("""
            SELECT indexer, COUNT(*) as cnt FROM history
            WHERE event_type='grabbed' AND indexer IS NOT NULL AND indexer != ''
            GROUP BY indexer ORDER BY cnt DESC LIMIT 8
        """).fetchall()

        top_series = db.execute("""
            SELECT s.title, s.id, COUNT(*) as cnt, COALESCE(SUM(h.size_bytes),0) as total_size
            FROM history h JOIN series s ON s.id=h.series_id
            WHERE h.event_type='grabbed'
            GROUP BY h.series_id ORDER BY cnt DESC LIMIT 10
        """).fetchall()

        pending_count = db.execute("SELECT COUNT(*) FROM pending_releases").fetchone()[0]
        bl_count      = db.execute("SELECT COUNT(*) FROM blocklist").fetchone()[0]

        disk_info = []
        for rf in get_root_folders(db):
            try:
                usage = _shutil.disk_usage(rf['path'])
                disk_info.append({
                    'label': rf['label'] or rf['path'],
                    'total': usage.total, 'used': usage.used, 'free': usage.free,
                    'pct':   round(usage.used / usage.total * 100, 1) if usage.total else 0,
                })
            except Exception:
                pass

    hist_map = {r['event_type']: r['cnt'] for r in history_counts}
    return templates.TemplateResponse(request, "stats.html", {
        "overview":      overview,
        "volumes_stats": volumes_stats,
        "hist_map":      hist_map,
        "daily_grabs":   daily_grabs,
        "top_indexers":  top_indexers,
        "top_series":    top_series,
        "pending_count": pending_count,
        "bl_count":      bl_count,
        "disk_info":     disk_info,
    })


@router.get("/calendar", response_class=HTMLResponse)
async def calendar_page(request: Request):
    with get_db() as db:
        releasing = db.execute(
            "SELECT s.id, s.title, s.cover_url, s.status, s.total_volumes,"
            " s.added_at, COUNT(v.id) as wanted_count,"
            " MAX(v.id) as latest_vol_id,"
            " SUM(CASE WHEN v.status='downloaded' THEN 1 ELSE 0 END) as have,"
            " SUM(CASE WHEN v.status IN ('wanted','grabbed') THEN 1 ELSE 0 END) as missing"
            " FROM series s"
            " JOIN volumes v ON v.series_id=s.id AND v.volume_num IS NOT NULL"
            " WHERE UPPER(s.status)='RELEASING' AND s.monitored=1"
            " AND s.deleted_at IS NULL"
            " GROUP BY s.id HAVING missing > 0"
            " ORDER BY wanted_count DESC, s.title"
        ).fetchall()

        releasing_detail = []
        for row in releasing:
            vols = db.execute(
                "SELECT volume_num, status FROM volumes"
                " WHERE series_id=? AND volume_num IS NOT NULL"
                " ORDER BY volume_num",
                (row['id'],)
            ).fetchall()
            wanted_vols  = [v['volume_num'] for v in vols if v['status'] == 'wanted']
            grabbed_vols = [v['volume_num'] for v in vols if v['status'] == 'grabbed']
            releasing_detail.append({
                'id': row['id'], 'title': row['title'], 'cover_url': row['cover_url'],
                'total_volumes': row['total_volumes'], 'have': row['have'],
                'missing': row['missing'], 'wanted': wanted_vols, 'grabbed': grabbed_vols,
                'status': row['status'],
            })

        upcoming = db.execute(
            "SELECT s.id, s.title, s.cover_url, s.status, s.total_volumes, s.pub_year"
            " FROM series s"
            " WHERE UPPER(s.status)='NOT_YET_RELEASED' AND s.monitored=1"
            " AND s.deleted_at IS NULL"
            " ORDER BY COALESCE(s.pub_year, 9999), s.title"
        ).fetchall()

        hiatus = db.execute(
            "SELECT s.id, s.title, s.cover_url, s.status,"
            " SUM(CASE WHEN v.status='downloaded' THEN 1 ELSE 0 END) as have,"
            " COUNT(v.id) as total"
            " FROM series s"
            " JOIN volumes v ON v.series_id=s.id AND v.volume_num IS NOT NULL"
            " WHERE UPPER(s.status) IN ('HIATUS','ON_HIATUS') AND s.monitored=1"
            " AND s.deleted_at IS NULL"
            " GROUP BY s.id"
            " ORDER BY s.title"
        ).fetchall()

    return templates.TemplateResponse(request, "calendar.html", {
        "releasing": releasing_detail,
        "upcoming":  upcoming,
        "hiatus":    hiatus,
    })
