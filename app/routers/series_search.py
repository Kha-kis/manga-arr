"""Series search, add, and metadata refresh functionality."""
import os

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.routers._templates import templates
from app.shared import get_db, get_root_folders
from app import main as _m
from app.notifications import with_flash


# Import the router from series_ to register routes on it
# series_.py imports this module at module level, so router must exist
from app.routers.series_ import router as series_router


@series_router.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = ""):
    results, source_used = [], ''
    if q.strip():
        results, source_used = await _m.search_series(q)
    with get_db() as db:
        # Soft-deleted series don't block re-add — a user who soft-deleted
        # then searches again should be able to add fresh (or restore via
        # the recycle bin instead, but that's their choice).
        existing_anilist: dict[int, list[str]] = {}
        for r in db.execute(
            "SELECT anilist_id, edition_type FROM series"
            " WHERE anilist_id IS NOT NULL AND deleted_at IS NULL"
        ).fetchall():
            existing_anilist.setdefault(r['anilist_id'], []).append(r['edition_type'] or 'standard')
        existing_mu = {
            r['mu_id']
            for r in db.execute(
                "SELECT mu_id FROM series WHERE mu_id IS NOT NULL"
                " AND deleted_at IS NULL"
            ).fetchall()
        }
        existing_titles = {
            r['title'].lower()
            for r in db.execute(
                "SELECT title FROM series where deleted_at IS NULL"
            ).fetchall()
        }
        root_folders = get_root_folders(db)
    return templates.TemplateResponse(request, "search.html", {
        "search_results":  results,
        "query":           q,
        "source_used":     source_used,
        "existing_anilist": existing_anilist,
        "existing_mu":     existing_mu,
        "existing_titles": existing_titles,
        "root_folders":    root_folders,
    })


@series_router.post("/series/add")
async def add_series(
    title:          str = Form(...),
    search_pattern: str = Form(...),
    anilist_id:     int = Form(0),
    mal_id:         int = Form(0),
    mu_id:          str = Form(""),
    cover_url:      str = Form(""),
    status:         str = Form(""),
    description:    str = Form(""),
    total_volumes:  int = Form(0),
    total_chapters: int = Form(0),
    root_folder_id: int = Form(0),
    pub_year:       int = Form(0),
    edition_type:   str = Form("standard"),
    monitored:      str = Form("0"),
    search_now:     str = Form("0"),
):
    _valid_editions = {
        'standard', 'official_color', 'colored', 'omnibus', 'deluxe', 'digital',
        'raw', 'special', 'collector', 'remaster', 'unlocalized'
    }
    if edition_type not in _valid_editions:
        edition_type = 'standard'
    with get_db() as db:
        if anilist_id:
            existing = db.execute(
                "SELECT id FROM series WHERE anilist_id=? AND edition_type=?"
                " AND deleted_at IS NULL",
                (anilist_id, edition_type)
            ).fetchone()
        else:
            existing = db.execute(
                "SELECT id FROM series WHERE anilist_id IS NULL AND title=?"
                " AND edition_type=? AND deleted_at IS NULL",
                (title, edition_type)
            ).fetchone()
        if not existing and mu_id:
            existing = db.execute(
                "SELECT id FROM series WHERE mu_id=? AND edition_type=?"
                " AND deleted_at IS NULL",
                (mu_id, edition_type)
            ).fetchone()
        if existing:
            return RedirectResponse(f"/series/{existing['id']}", status_code=303)
        # Resolve a root folder (operator's pick, default, or lowest-id
        # fallback). If nothing resolves, refuse to create the series —
        # a series without a library destination is worse than a
        # clear error telling the operator to configure one.
        rf_id = _m.resolve_root_folder_id(db, preferred_id=root_folder_id or None)
        if rf_id is None:
            return JSONResponse(
                {"error": "No root folder configured. Add one in Settings "
                          "before adding series."},
                status_code=400,
            )
        _monitored = monitored == "1"
        _search_now = search_now == "1"
        cur = db.execute(
            "INSERT INTO series(title, search_pattern, anilist_id, mal_id, mu_id, cover_url,"
            " status, description, total_volumes, total_chapters, root_folder_id, pub_year,"
            " edition_type, vol_count_source, monitored)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (title, search_pattern, anilist_id or None, mal_id or None, mu_id or None,
             cover_url, status, description, total_volumes or None, total_chapters or None,
             rf_id, pub_year or None, edition_type, 'anilist', 1 if _monitored else 0)
        )
        series_id = cur.lastrowid
        if total_volumes and total_volumes > 0 and edition_type not in _m._NON_STANDARD_STUB_EDITIONS:
            _m.create_volume_stubs(db, series_id, total_volumes)
        _m.add_history(db, 'series_added', series_id, title, '',
                       source_title=title,
                       data={'total_volumes': total_volumes, 'status': status})
    # Fire all post-add tasks in background — don't block the response
    _m.create_background_task(_m.refresh_mangadex_map(series_id), name=f"series:{series_id}:refresh_mangadex")
    if anilist_id:
        _m.create_background_task(_m.fetch_anilist_aliases(series_id, anilist_id, title), name=f"series:{series_id}:fetch_aliases")
    if cover_url:
        _m.create_background_task(_m.download_cover(series_id, cover_url), name=f"series:{series_id}:download_cover")
    _m.create_background_task(_m.fetch_mu_metadata(series_id, title), name=f"series:{series_id}:fetch_mu")
    if _search_now:
        _m.create_background_task(_m.grab_existing(series_id, title, search_pattern), name=f"series:{series_id}:grab_existing")
    if edition_type in _m._NON_STANDARD_STUB_EDITIONS:
        _m.create_background_task(_m.fetch_edition_volume_count(series_id, title, edition_type), name=f"series:{series_id}:fetch_edition_count")
    _m.create_background_task(_m.notify_discord('', event='on_series_add', embed={
        'title': f'Added — {title}',
        'description': (f"Status: {status}" if status else "Added to library"),
        'color': 0x4cc9f0,
        'thumbnail': {'url': cover_url} if cover_url else {},
    }), name=f"series:{series_id}:notify_add")
    return RedirectResponse(with_flash(f"/series/{series_id}", "Search queued for all wanted volumes", "success"), status_code=303)


@series_router.get("/api/series/{series_id}/cover-refresh")
async def api_cover_refresh(series_id: int):
    with get_db() as db:
        s = db.execute("SELECT id, cover_url FROM series WHERE id=?", (series_id,)).fetchone()
    if not s:
        return JSONResponse({"error": "Series not found"}, status_code=404)
    dest = f"/config/covers/{series_id}.jpg"
    try:
        if os.path.exists(dest):
            os.remove(dest)
    except Exception:
        pass
    if s['cover_url']:
        await _m.download_cover(series_id, s['cover_url'])
        return JSONResponse({"ok": True, "cover_url": f"/covers/{series_id}.jpg"})
    return JSONResponse({"ok": False, "error": "No cover_url stored for this series"})
