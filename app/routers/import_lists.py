"""Import Lists — auto-add series from external lists (Sonarr parity)."""
import json
import asyncio
import httpx
import re
from datetime import datetime
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from routers._templates import templates

from shared import get_db, from_json
from security import validate_outbound_url, UnsafeURLError
from events import log_event

router = APIRouter()

LIST_TYPES = [
    ("anilist_user",      "AniList — User List"),
    ("anilist_top",       "AniList — Top 100"),
    ("anilist_popular",   "AniList — Popular"),
    ("mal_user",          "MyAnimeList — User List"),
    ("custom_rss",        "Custom RSS Feed"),
]


def _all_lists(db):
    lists = db.execute("SELECT * FROM import_lists ORDER BY name").fetchall()
    result = []
    for lst in lists:
        qp = db.execute(
            "SELECT name FROM quality_profiles WHERE id=?", (lst['quality_profile_id'],)
        ).fetchone() if lst['quality_profile_id'] else None
        rf = db.execute(
            "SELECT path FROM root_folders WHERE id=?", (lst['root_folder_id'],)
        ).fetchone() if lst['root_folder_id'] else None
        result.append({
            **dict(lst),
            'quality_profile_name': qp['name'] if qp else 'Default',
            'root_folder_path':     rf['path'] if rf else 'Default',
        })
    return result


def _normalize_title(title: str | None) -> str:
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def _entry_external_id(entry: dict) -> str:
    for key in ("external_id", "anilist_id", "mal_id", "mu_id", "mangadex_id"):
        value = entry.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _all_exclusions(db):
    return db.execute(
        "SELECT * FROM import_list_exclusions ORDER BY source, title, external_id, id"
    ).fetchall()


def _exclusion_keys(db, source: str) -> tuple[set[str], set[str]]:
    rows = db.execute(
        "SELECT external_id, title_normalized FROM import_list_exclusions"
        " WHERE source=?",
        (source,),
    ).fetchall()
    external_ids = {
        str(row["external_id"]).strip()
        for row in rows
        if row["external_id"] and str(row["external_id"]).strip()
    }
    titles = {
        row["title_normalized"]
        for row in rows
        if row["title_normalized"]
    }
    return external_ids, titles


def _entry_is_excluded(
    entry: dict,
    external_ids: set[str],
    title_keys: set[str],
) -> bool:
    external_id = _entry_external_id(entry)
    if external_id and external_id in external_ids:
        return True
    title_key = _normalize_title(entry.get("title"))
    return bool(title_key and title_key in title_keys)


# ── List ──────────────────────────────────────────────────────────────────────
@router.get("/import-lists", response_class=HTMLResponse)
async def import_lists_page(request: Request):
    with get_db() as db:
        lists       = _all_lists(db)
        profiles    = db.execute("SELECT id, name FROM quality_profiles ORDER BY name").fetchall()
        root_folders = db.execute("SELECT id, path FROM root_folders ORDER BY path").fetchall()
        exclusions  = _all_exclusions(db)
    return templates.TemplateResponse(request, "import_lists.html", {
        "lists":        lists,
        "exclusions":   exclusions,
        "list_types":   LIST_TYPES,
        "profiles":     profiles,
        "root_folders": root_folders,
    })


# ── Create ────────────────────────────────────────────────────────────────────
@router.post("/import-lists")
async def create_import_list(
    name: str = Form(...),
    type: str = Form(...),
    enabled: int = Form(1),
    quality_profile_id: str = Form(""),
    root_folder_id: str = Form(""),
    monitor_mode: str = Form("all"),
    settings: str = Form("{}"),
):
    try:
        settings_dict = json.loads(settings)
    except Exception:
        settings_dict = {}
    with get_db() as db:
        db.execute(
            "INSERT INTO import_lists(name,type,enabled,quality_profile_id,root_folder_id,"
            " monitor_mode,settings) VALUES(?,?,?,?,?,?,?)",
            (name.strip(), type, enabled,
             int(quality_profile_id) if quality_profile_id.isdigit() else None,
             int(root_folder_id) if root_folder_id.isdigit() else None,
             monitor_mode, json.dumps(settings_dict))
        )
    return RedirectResponse("/import-lists", status_code=303)


# ── Sync (manual trigger) — defined BEFORE /{list_id} to avoid path conflict ──
@router.post("/import-lists/sync")
async def sync_import_lists(request: Request):
    import main as _m
    _m.create_background_task(_sync_all_lists(), name="import_lists:sync_all")
    if request.headers.get("HX-Request") == "true":
        from fastapi.responses import Response as _Resp
        return _Resp(headers={"HX-Trigger": json.dumps({
            "showToast": {"msg": "Sync started in background", "type": "success"}
        })})
    return JSONResponse({"ok": True, "message": "Sync started in background"})


# ── Edit ──────────────────────────────────────────────────────────────────────
def _settings_json_passthrough(v) -> str:
    """Re-encode submitted settings JSON; replace malformed with '{}'."""
    raw = str(v or '').strip()
    try:
        parsed = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        parsed = {}
    return json.dumps(parsed)


@router.post("/import-lists/exclusions")
async def create_import_list_exclusion(
    request: Request,
    source: str = Form(...),
    external_id: str = Form(""),
    title: str = Form(""),
    reason: str = Form(""),
):
    source = source.strip()
    external_id = external_id.strip()
    title = title.strip()
    title_key = _normalize_title(title)
    if not source or (not external_id and not title_key):
        if request.headers.get("HX-Request") == "true":
            from fastapi.responses import Response as _Resp
            return _Resp(headers={"HX-Refresh": "true"}, status_code=400)
        return JSONResponse(
            {
                "ok": False,
                "message": "Source plus either external ID or title is required",
            },
            status_code=400,
        )
    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO import_list_exclusions"
            "(source, external_id, title, title_normalized, reason)"
            " VALUES(?,?,?,?,?)",
            (source, external_id or None, title, title_key, reason.strip() or None),
        )
    if request.headers.get("HX-Request") == "true":
        from fastapi.responses import Response as _Resp
        return _Resp(headers={"HX-Refresh": "true"})
    return RedirectResponse("/import-lists", status_code=303)


@router.post("/import-lists/exclusions/{exclusion_id}/delete")
async def delete_import_list_exclusion(request: Request, exclusion_id: int):
    with get_db() as db:
        db.execute("DELETE FROM import_list_exclusions WHERE id=?", (exclusion_id,))
    if request.headers.get("HX-Request") == "true":
        from fastapi.responses import Response as _Resp
        return _Resp(headers={"HX-Refresh": "true"})
    return RedirectResponse("/import-lists", status_code=303)


@router.post("/import-lists/{list_id}")
async def edit_import_list(request: Request, list_id: int):
    """Edit an import list. Partial-POST safe."""
    from routers._form_helpers import (
        submitted_subset, fk_id_or_none, bool_int,
    )
    submitted = await request.form()

    plain_fields = {
        'name':               ('name',               lambda v: str(v or '').strip()),
        'type':               ('type',               lambda v: str(v or '').strip()),
        'enabled':            ('enabled',            bool_int),
        'quality_profile_id': ('quality_profile_id', fk_id_or_none),
        'root_folder_id':     ('root_folder_id',     fk_id_or_none),
        'monitor_mode':       ('monitor_mode',       lambda v: str(v or '').strip() or 'all'),
        'settings':           ('settings',           _settings_json_passthrough),
    }

    with get_db() as db:
        updates, params = submitted_subset(submitted, plain_fields)
        if updates:
            params.append(list_id)
            db.execute(
                f"UPDATE import_lists SET {', '.join(updates)} WHERE id=?",
                params
            )
    return RedirectResponse("/import-lists", status_code=303)


# ── Delete ────────────────────────────────────────────────────────────────────
@router.post("/import-lists/{list_id}/delete")
async def delete_import_list(request: Request, list_id: int):
    with get_db() as db:
        db.execute("DELETE FROM import_lists WHERE id=?", (list_id,))
    if request.headers.get("HX-Request") == "true":
        from fastapi.responses import Response as _Resp
        return _Resp(headers={"HX-Refresh": "true"})
    return RedirectResponse("/import-lists", status_code=303)


@router.post("/import-lists/{list_id}/sync")
async def sync_single_import_list(request: Request, list_id: int):
    with get_db() as db:
        lst = db.execute("SELECT * FROM import_lists WHERE id=?", (list_id,)).fetchone()
    if not lst:
        if request.headers.get("HX-Request") == "true":
            from fastapi.responses import Response as _Resp
            return _Resp(headers={"HX-Trigger": json.dumps({
                "showToast": {"msg": "List not found", "type": "error"}
            })}, status_code=404)
        return JSONResponse({"ok": False, "message": "List not found"})
    import main as _m
    _m.create_background_task(_sync_list(dict(lst)), name=f"import_lists:sync:{list_id}")
    if request.headers.get("HX-Request") == "true":
        from fastapi.responses import Response as _Resp
        return _Resp(headers={"HX-Trigger": json.dumps({
            "showToast": {"msg": f"Sync started for {lst['name']}", "type": "success"}
        })})
    return RedirectResponse("/import-lists", status_code=303)


async def _sync_all_lists():
    """Background task: sync all enabled import lists."""
    with get_db() as db:
        lists = [dict(r) for r in db.execute("SELECT * FROM import_lists WHERE enabled=1").fetchall()]
    for lst in lists:
        try:
            await _sync_list(lst)
        except Exception as e:
            log_event("error", f"[ImportList:{lst['name']}] Sync error: {e}")


async def _sync_list(lst: dict):
    """Fetch series from an import list and add any not already in library."""
    t        = lst['type']
    settings = from_json(lst.get('settings'), {})

    try:
        series_list = await _fetch_list(t, settings)
    except Exception as e:
        log_event("error", f"[ImportList:{lst['name']}] Fetch error: {e}")
        return

    if not series_list:
        return

    added_entries: list[tuple[int, str, str, str, int | None]] = []  # (series_id, title, search_pattern, cover_url, anilist_id)
    skipped_excluded = 0

    with get_db() as db:
        # Update last_sync
        db.execute("UPDATE import_lists SET last_sync=? WHERE id=?",
                   (datetime.utcnow().isoformat(), lst['id']))
        excluded_external_ids, excluded_titles = _exclusion_keys(db, t)

        # Dedup by (anilist_id, edition_type) — same series can exist in multiple editions.
        # Soft-deleted series don't count: an import-list re-add should create a fresh
        # row rather than inheriting state from the recycle-bin entry.
        existing_ids = {(r['anilist_id'], r['edition_type'] or 'standard') for r in db.execute(
            "SELECT anilist_id, edition_type FROM series"
            " WHERE anilist_id IS NOT NULL AND deleted_at IS NULL"
        ).fetchall()}

        # Title-based dedup for sources without anilist_id (MAL, custom RSS)
        existing_titles = {r['title'].lower().strip() for r in db.execute(
            "SELECT title FROM series WHERE deleted_at IS NULL"
        ).fetchall()}

        for entry in series_list:
            al_id = entry.get('anilist_id')
            title = entry.get('title', '') or ''
            if not title.strip():
                continue
            if _entry_is_excluded(entry, excluded_external_ids, excluded_titles):
                skipped_excluded += 1
                continue
            # Dedup: prefer anilist_id match; fall back to title match for MAL/RSS
            if al_id:
                if (al_id, 'standard') in existing_ids:
                    continue
            else:
                if title.lower().strip() in existing_titles:
                    continue
            # Add to library — resolve a root folder or bail for the
            # entire list. If no folders exist we can't place any series
            # from this list, so stop here rather than creating orphans.
            from helpers import resolve_root_folder_id as _rrf
            rf_id = _rrf(db, preferred_id=lst.get('root_folder_id'))
            if rf_id is None:
                from events import log_event as _log
                _log('error',
                     f"import-list {lst.get('name', lst.get('id'))!r}: "
                     f"no root folders configured — add one in Settings "
                     f"before import lists can add series",
                     db=db)
                break
            search_pattern = entry.get('search_pattern', title)
            cover_url      = entry.get('cover_url', '') or ''
            status         = entry.get('status', '')
            total_volumes  = entry.get('total_volumes')
            cur = db.execute(
                "INSERT OR IGNORE INTO series(title,search_pattern,anilist_id,cover_url,"
                " status,total_volumes,enabled,monitored,monitor_mode,quality_profile_id,root_folder_id,edition_type)"
                " VALUES(?,?,?,?,?,?,1,1,?,?,?,'standard')",
                (title, search_pattern, al_id, cover_url, status, total_volumes,
                 lst.get('monitor_mode', 'all'),
                 lst.get('quality_profile_id'),
                 rf_id)
            )
            new_id = cur.lastrowid
            if new_id and total_volumes and total_volumes > 0:
                try:
                    from volumes import create_volume_stubs
                    create_volume_stubs(db, new_id, total_volumes)
                except Exception as e:
                    from events import log_event as _log
                    _log('error',
                         f'create_volume_stubs failed for new import {title!r}: '
                         f'{type(e).__name__}: {str(e)[:120]}',
                         new_id)
            if new_id:
                added_entries.append((new_id, title, search_pattern, cover_url, al_id))
            if al_id:
                existing_ids.add((al_id, 'standard'))
            else:
                existing_titles.add(title.lower().strip())

    # Fire background tasks for each newly added series (covers, maps, aliases, grabs)
    for series_id, title, search_pattern, cover_url, al_id in added_entries:
        try:
            import main as _m
            _m.create_background_task(_m.refresh_mangadex_map(series_id), name=f"import_list:{series_id}:refresh_mangadex")
            if cover_url:
                _m.create_background_task(_m.download_cover(series_id, cover_url), name=f"import_list:{series_id}:download_cover")
            if al_id:
                _m.create_background_task(_m.fetch_anilist_aliases(series_id, al_id, title), name=f"import_list:{series_id}:fetch_aliases")
            _m.create_background_task(_m.fetch_mu_metadata(series_id, title), name=f"import_list:{series_id}:fetch_mu")
            _m.create_background_task(_m.grab_existing(series_id, title, search_pattern), name=f"import_list:{series_id}:grab_existing")
        except Exception as e:
            try:
                import main as _m
                _m.log_event(
                    'error',
                    f'import-list post-add task spawn failed for {title!r}: '
                    f'{type(e).__name__}: {str(e)[:120]}',
                    series_id,
                )
            except Exception:
                pass

    added_count = len(added_entries)
    log_event(
        "import_list_sync",
        f"[ImportList:{lst['name']}] Synced {len(series_list)} items, "
        f"added {added_count} new, skipped {skipped_excluded} excluded",
    )


async def _fetch_list(list_type: str, settings: dict) -> list[dict]:
    """Fetch series entries from a list source. Returns list of {anilist_id, title, ...}."""
    if list_type == 'anilist_user':
        return await _fetch_anilist_user(settings)
    elif list_type == 'anilist_top':
        return await _fetch_anilist_top()
    elif list_type == 'anilist_popular':
        return await _fetch_anilist_popular()
    elif list_type == 'mal_user':
        return await _fetch_mal_user(settings)
    elif list_type == 'custom_rss':
        return await _fetch_custom_rss(settings)
    return []


async def _fetch_anilist_user(settings: dict) -> list[dict]:
    username   = settings.get('username', '')
    list_names = settings.get('lists', ['PLANNING', 'CURRENT'])
    if not username:
        return []
    if isinstance(list_names, str):
        list_names = [s.strip() for s in list_names.split(',') if s.strip()]

    query = """
    query ($user: String, $type: MediaType) {
      MediaListCollection(userName: $user, type: $type) {
        lists { name entries { media {
          id title { romaji english } coverImage { large }
          status volumes format
        }}}
      }
    }"""
    async with httpx.AsyncClient(timeout=20) as cli:
        r = await cli.post(
            "https://graphql.anilist.co",
            json={"query": query, "variables": {"user": username, "type": "MANGA"}}
        )
    data = r.json()
    results = []
    for lst in data.get('data', {}).get('MediaListCollection', {}).get('lists', []):
        if lst['name'] not in list_names:
            continue
        for entry in lst.get('entries', []):
            m = entry.get('media', {})
            title = (m.get('title', {}).get('english') or
                     m.get('title', {}).get('romaji') or f"AniList {m.get('id')}")
            results.append({
                'anilist_id':    m.get('id'),
                'title':         title,
                'search_pattern': title,
                'cover_url':     (m.get('coverImage') or {}).get('large', ''),
                'status':        (m.get('status') or '').lower(),
                'total_volumes': m.get('volumes'),
            })
    return results


async def _fetch_anilist_top() -> list[dict]:
    query = """
    query ($page: Int) {
      Page(page: $page, perPage: 50) {
        media(type: MANGA, sort: SCORE_DESC, format_in: [MANGA, ONE_SHOT]) {
          id title { romaji english } coverImage { large } status volumes
        }
      }
    }"""
    results = []
    for page in range(1, 3):
        async with httpx.AsyncClient(timeout=20) as cli:
            r = await cli.post(
                "https://graphql.anilist.co",
                json={"query": query, "variables": {"page": page}}
            )
        for m in r.json().get('data', {}).get('Page', {}).get('media', []):
            title = (m.get('title', {}).get('english') or m.get('title', {}).get('romaji', ''))
            results.append({
                'anilist_id':    m.get('id'),
                'title':         title,
                'search_pattern': title,
                'cover_url':     (m.get('coverImage') or {}).get('large', ''),
                'status':        (m.get('status') or '').lower(),
                'total_volumes': m.get('volumes'),
            })
    return results


async def _fetch_anilist_popular() -> list[dict]:
    query = """
    query ($page: Int) {
      Page(page: $page, perPage: 50) {
        media(type: MANGA, sort: POPULARITY_DESC, format_in: [MANGA]) {
          id title { romaji english } coverImage { large } status volumes
        }
      }
    }"""
    results = []
    for page in range(1, 3):
        async with httpx.AsyncClient(timeout=20) as cli:
            r = await cli.post(
                "https://graphql.anilist.co",
                json={"query": query, "variables": {"page": page}}
            )
        for m in r.json().get('data', {}).get('Page', {}).get('media', []):
            title = (m.get('title', {}).get('english') or m.get('title', {}).get('romaji', ''))
            results.append({
                'anilist_id':    m.get('id'),
                'title':         title,
                'search_pattern': title,
                'cover_url':     (m.get('coverImage') or {}).get('large', ''),
                'status':        (m.get('status') or '').lower(),
                'total_volumes': m.get('volumes'),
            })
    return results


async def _fetch_mal_user(settings: dict) -> list[dict]:
    """Fetch MAL reading list. Requires MAL client_id."""
    username  = settings.get('username', '')
    client_id = settings.get('client_id', '')
    if not username or not client_id:
        return []
    results = []
    offset = 0
    while True:
        async with httpx.AsyncClient(timeout=20) as cli:
            r = await cli.get(
                f"https://api.myanimelist.net/v2/users/{username}/mangalist",
                headers={'X-MAL-CLIENT-ID': client_id},
                params={'fields': 'title,main_picture,num_volumes,status',
                        'limit': 100, 'offset': offset,
                        'status': settings.get('status', 'plan_to_read')}
            )
        data = r.json()
        for item in data.get('data', []):
            node = item.get('node', {})
            results.append({
                'anilist_id':    None,  # would need cross-ref lookup
                'title':         node.get('title', ''),
                'search_pattern': node.get('title', ''),
                'cover_url':     (node.get('main_picture') or {}).get('large', ''),
                'status':        '',
                'total_volumes': node.get('num_volumes'),
            })
        if not data.get('paging', {}).get('next'):
            break
        offset += 100
    return results


async def _fetch_custom_rss(settings: dict) -> list[dict]:
    """Parse a custom RSS/Atom feed for manga titles."""
    from defusedxml.ElementTree import fromstring as _safe_fromstring
    url = settings.get('url', '')
    if not url:
        return []
    try:
        validate_outbound_url(url)
    except UnsafeURLError:
        return []
    async with httpx.AsyncClient(timeout=20) as cli:
        r = await cli.get(url, headers={'User-Agent': 'mangarr/1.0'})
    results = []
    try:
        root = _safe_fromstring(r.text)
        for item in root.findall('.//item'):
            title = item.findtext('title', '').strip()
            if title:
                results.append({
                    'anilist_id':    None,
                    'title':         title,
                    'search_pattern': title,
                    'cover_url':     '',
                    'status':        '',
                    'total_volumes': None,
                })
    except Exception:
        pass
    return results
