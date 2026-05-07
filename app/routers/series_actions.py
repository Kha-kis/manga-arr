"""Series lifecycle actions - edit, delete, restore, purge, refresh, manual grab."""

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response as _Resp

from app.routers.series_ import router
from app.routers._templates import templates
from app.routers._form_helpers import (
    submitted_subset, str_or_none, fk_id_or_none, csv_to_json_array
)
from app.shared import get_cfg, get_db, vol_num_to_display, quality_rank
from app import main as _m
from app.routers.series_search import search_series
from app.notifications import with_flash
from datetime import datetime
import asyncio
import json
import os


_VALID_OMNIBUS_PREFS = {'prefer_individual', 'prefer_omnibus', 'only_individual', 'only_omnibus'}
_VALID_QUALITY_CUTOFFS = {'', 'cbz', 'cbr', 'epub', 'pdf', 'zip', 'mobi'}
_VALID_UPDATE_STRATEGIES = {'always', 'once', 'throttled'}
_VALID_EDITIONS = {
    'standard', 'official_color', 'colored', 'omnibus', 'deluxe', 'digital',
    'raw', 'special', 'collector', 'remaster', 'unlocalized'
}
_VALID_SOURCE_TYPES = {'any', 'official_only', 'fan_only'}
_EDITION_IMPLIED_SOURCE = {
    'official_color': 'official_only',
    'colored':        'fan_only',
    'unlocalized':    'fan_only',
}


@router.post("/series/{series_id}/edit")
async def edit_series(request: Request, series_id: int):
    """Edit a series. Partial-POST safe: only columns whose form key is
    present in the request body are written.
    """
    submitted = await request.form()
    map_updated = False

    plain_fields = {
        'title':                  ('title',              str_or_none),
        'search_pattern':         ('search_pattern',     str_or_none),
        'preferred_groups_input': ('preferred_groups',   csv_to_json_array),
        'blocked_groups_input':   ('blocked_groups',     csv_to_json_array),
        'omnibus_preference': (
            'omnibus_preference',
            lambda v: v if (v := str(v or '').strip()) in _VALID_OMNIBUS_PREFS
                      else 'prefer_individual',
        ),
        'quality_profile_id':  ('quality_profile_id',  fk_id_or_none),
        'language_profile_id': ('language_profile_id', fk_id_or_none),
        'quality_cutoff': (
            'quality_cutoff',
            lambda v: v if (v := str(v or '').strip()) in _VALID_QUALITY_CUTOFFS else '',
        ),
        'update_strategy': (
            'update_strategy',
            lambda v: v if (v := str(v or '').strip()) in _VALID_UPDATE_STRATEGIES
                      else 'always',
        ),
        'required_scanlator': ('required_scanlator', str_or_none),
        'ddl_language': (
            'ddl_language',
            lambda v: (str(v or '').strip().lower()[:5] or None),
        ),
    }

    with get_db() as db:
        updates, params = submitted_subset(submitted, plain_fields)

        if 'edition_type' in submitted:
            ed_raw = str(submitted.get('edition_type') or '').strip()
            ed = ed_raw if ed_raw in _VALID_EDITIONS else 'standard'
            updates.append('edition_type=?'); params.append(ed)
            if ed in _EDITION_IMPLIED_SOURCE:
                updates.append('source_type=?')
                params.append(_EDITION_IMPLIED_SOURCE[ed])
            elif 'source_type' in submitted:
                src_raw = str(submitted.get('source_type') or '').strip()
                src = src_raw if src_raw in _VALID_SOURCE_TYPES else 'any'
                updates.append('source_type=?'); params.append(src)
        elif 'source_type' in submitted:
            src_raw = str(submitted.get('source_type') or '').strip()
            src = src_raw if src_raw in _VALID_SOURCE_TYPES else 'any'
            updates.append('source_type=?'); params.append(src)

        chapter_map_text = str(submitted.get('chapter_map_text') or '').strip()
        if chapter_map_text:
            from app.routers.series_core import _parse_chapter_ranges
            new_map = _parse_chapter_ranges(chapter_map_text)
            if new_map:
                updates.append("chapter_vol_map=?")
                params.append(json.dumps(new_map))
                map_updated = True

        _manual_new = _manual_old = None
        if 'total_volumes' in submitted:
            try:
                tv = int(str(submitted['total_volumes']) or '0')
            except (TypeError, ValueError):
                tv = 0
            if tv > 0:
                tv_row = db.execute(
                    "SELECT total_volumes FROM series WHERE id=?", (series_id,)
                ).fetchone()
                _manual_old = (tv_row['total_volumes'] or 0) if tv_row else 0
                _manual_new = tv
                updates.append("total_volumes=?"); params.append(_manual_new)
                updates.append("vol_count_source=?"); params.append('manual')

        if updates:
            params.append(series_id)
            db.execute(f"UPDATE series SET {', '.join(updates)} WHERE id=?", params)

        if _manual_new is not None:
            if _manual_old and _manual_new < _manual_old:
                db.execute(
                    "UPDATE chapters SET volume_id=NULL"
                    " WHERE volume_id IN ("
                    "   SELECT id FROM volumes WHERE series_id=? AND volume_num>? AND status='wanted'"
                    " )",
                    (series_id, float(_manual_new))
                )
                db.execute(
                    "DELETE FROM volumes WHERE series_id=? AND volume_num>? AND status='wanted'",
                    (series_id, float(_manual_new))
                )
                _m.log_event('metadata', f"[Manual] removed wanted stubs > vol {_manual_new}", series_id)

        needs_stub_reconcile = map_updated or _manual_new is not None
        if needs_stub_reconcile:
            _eff = db.execute(
                "SELECT total_volumes FROM series WHERE id=?", (series_id,)
            ).fetchone()
            eff_total = (_eff['total_volumes'] or 0) if _eff else 0
            if eff_total > 0:
                _m.create_volume_stubs(db, series_id, eff_total)

        if map_updated:
            _m.populate_chapters(db, series_id)

    if request.headers.get("HX-Request") == "true":
        return _Resp(headers={"HX-Refresh": "true"})
    return RedirectResponse(f"/series/{series_id}", status_code=303)


_PATCHABLE_FIELDS = {
    'title', 'search_pattern', 'preferred_groups', 'blocked_groups',
    'omnibus_preference', 'quality_profile_id', 'language_profile_id',
    'quality_cutoff', 'update_strategy', 'required_scanlator',
    'source_type', 'edition_type', 'total_volumes', 'ddl_language',
    'monitor_mode', 'monitored', 'enabled',
}


@router.patch("/api/series/{series_id}")
async def patch_series(request: Request, series_id: int):
    """Update a subset of series fields without clobbering unsubmitted ones."""
    import sqlite3 as _sql
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict) or not payload:
        return JSONResponse({"error": "expected a non-empty object"}, status_code=400)

    unknown = set(payload.keys()) - _PATCHABLE_FIELDS
    if unknown:
        return JSONResponse(
            {"error": f"unknown or non-patchable fields: {sorted(unknown)}"},
            status_code=400,
        )

    if 'total_volumes' in payload:
        tv = payload['total_volumes']
        if tv is not None:
            if not isinstance(tv, int) or isinstance(tv, bool) or tv <= 0:
                return JSONResponse(
                    {"error": "total_volumes must be null or a positive integer"},
                    status_code=400,
                )

    try:
        with get_db() as db:
            exists = db.execute(
                "SELECT 1 FROM series WHERE id=?", (series_id,)
            ).fetchone()
            if not exists:
                return JSONResponse({"error": "series not found"}, status_code=404)

            sets, params = [], []
            for k, v in payload.items():
                if k in ('preferred_groups', 'blocked_groups') and isinstance(v, list):
                    v = json.dumps([str(g).strip() for g in v if str(g).strip()])
                sets.append(f"{k}=?")
                params.append(v)
            params.append(series_id)
            db.execute(f"UPDATE series SET {', '.join(sets)} WHERE id=?", params)

            if 'total_volumes' in payload and payload['total_volumes'] is not None:
                db.execute(
                    "UPDATE series SET vol_count_source='manual' WHERE id=?",
                    (series_id,)
                )
    except _sql.OperationalError as e:
        msg = str(e).lower()
        if 'locked' in msg or 'busy' in msg:
            return JSONResponse(
                {"error": "database busy — retry"},
                status_code=503,
                headers={"Retry-After": "5"},
            )
        raise

    return JSONResponse({"ok": True, "updated": sorted(payload.keys())})


def _hard_delete_series(
    db, series_id: int,
    *,
    log_history: bool = False,
    remove_files: bool = False,
) -> str:
    """Destructive cascade for a series."""
    s = db.execute("SELECT title FROM series WHERE id=?", (series_id,)).fetchone()
    title = s['title'] if s else ''

    if remove_files:
        for vol in db.execute(
            "SELECT import_path FROM volumes WHERE series_id=?"
            " AND import_path IS NOT NULL AND import_path != ''",
            (series_id,)
        ).fetchall():
            try:
                fpath = vol['import_path']
                if fpath and os.path.exists(fpath) and os.path.isfile(fpath):
                    os.remove(fpath)
            except OSError:
                pass

    iq_ids = [r['id'] for r in db.execute(
        "SELECT id FROM import_queue WHERE series_id=?", (series_id,)
    ).fetchall()]
    for iq_id in iq_ids:
        db.execute("DELETE FROM import_queue_files WHERE queue_id=?", (iq_id,))
    db.execute("DELETE FROM import_queue WHERE series_id=?", (series_id,))
    db.execute("DELETE FROM chapters WHERE series_id=?", (series_id,))
    db.execute("DELETE FROM volumes WHERE series_id=?", (series_id,))
    db.execute("DELETE FROM pending_releases WHERE series_id=?", (series_id,))
    db.execute("DELETE FROM seen WHERE series_id=?", (series_id,))
    db.execute("DELETE FROM blocklist WHERE series_id=?", (series_id,))
    db.execute("DELETE FROM series_aliases WHERE series_id=?", (series_id,))
    db.execute("DELETE FROM series_tags WHERE series_id=?", (series_id,))
    if log_history:
        _m.add_history(db, 'series_purged', None, title, '', source_title=title)
    db.execute("DELETE FROM series WHERE id=?", (series_id,))
    cover_path = f"/config/covers/{series_id}.jpg"
    try:
        if os.path.exists(cover_path):
            os.remove(cover_path)
    except OSError:
        pass
    return title


@router.post("/series/{series_id}/delete")
async def delete_series(request: Request, series_id: int):
    """Soft-delete a series."""
    with get_db() as db:
        s = db.execute("SELECT title FROM series WHERE id=?", (series_id,)).fetchone()
        if not s:
            title = ''
        else:
            title = s['title']
            db.execute(
                "UPDATE series SET deleted_at=CURRENT_TIMESTAMP,"
                " deletion_reason=? WHERE id=? AND deleted_at IS NULL",
                ('user_action', series_id)
            )
            _m.add_history(db, 'series_soft_deleted', None, title, '',
                           source_title=title)

    if request.headers.get("HX-Request") == "true":
        return _Resp(headers={
            "HX-Trigger": json.dumps({
                "showToast": {
                    "msg": f"{title} moved to recycle bin" if title else "Series moved to recycle bin",
                    "type": "success",
                    "actionLabel": "Undo",
                    "actionUrl": f"/series/{series_id}/restore",
                }
            }),
            "HX-Redirect": "/",
        })
    return RedirectResponse("/", status_code=303)


@router.post("/series/{series_id}/restore")
async def restore_series(request: Request, series_id: int):
    """Restore a soft-deleted series."""
    with get_db() as db:
        s = db.execute(
            "SELECT title, deleted_at FROM series WHERE id=?", (series_id,)
        ).fetchone()
        if s and s['deleted_at']:
            db.execute(
                "UPDATE series SET deleted_at=NULL, deletion_reason=NULL"
                " WHERE id=?", (series_id,)
            )
            _m.add_history(db, 'series_restored', None, s['title'] or '',
                           '', source_title=s['title'] or '')

    if request.headers.get("HX-Request") == "true":
        return _Resp(headers={"HX-Redirect": f"/series/{series_id}"})
    return RedirectResponse(f"/series/{series_id}", status_code=303)


@router.post("/series/{series_id}/purge")
async def purge_series(request: Request, series_id: int):
    """Permanent delete from the recycle bin."""
    with get_db() as db:
        row = db.execute(
            "SELECT deleted_at FROM series WHERE id=?", (series_id,)
        ).fetchone()
        if not row or not row['deleted_at']:
            if request.headers.get("HX-Request") == "true":
                return _Resp(headers={"HX-Redirect": "/recycle-bin"})
            return RedirectResponse("/recycle-bin", status_code=303)
        _hard_delete_series(db, series_id, log_history=True, remove_files=True)

    if request.headers.get("HX-Request") == "true":
        return _Resp(headers={
            "HX-Trigger": json.dumps({
                "showToast": {"msg": "Series permanently deleted", "type": "success"}
            }),
            "HX-Redirect": "/recycle-bin",
        })
    return RedirectResponse("/recycle-bin", status_code=303)


@router.get("/recycle-bin", response_class=HTMLResponse)
async def recycle_bin_page(request: Request):
    """Listing of soft-deleted series."""
    try:
        retention_days = max(1, int(get_cfg('recycle_bin_retention_days', '30')))
    except (TypeError, ValueError):
        retention_days = 30

    with get_db() as db:
        rows = db.execute(
            "SELECT id, title, cover_url, deleted_at, deletion_reason,"
            " (SELECT COUNT(*) FROM volumes WHERE series_id=series.id) as volume_count"
            " FROM series"
            " WHERE deleted_at IS NOT NULL"
            " ORDER BY deleted_at DESC"
        ).fetchall()
        binned = [dict(r) for r in rows]

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    for entry in binned:
        try:
            ts_str = (entry['deleted_at'] or '').replace('T', ' ').rstrip('Z')
            dt = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
            elapsed_days = (now - dt).total_seconds() / 86400
            entry['days_remaining'] = max(0, int(retention_days - elapsed_days))
        except (TypeError, ValueError):
            entry['days_remaining'] = retention_days

    return templates.TemplateResponse(request, "recycle_bin.html", {
        "binned":         binned,
        "retention_days": retention_days,
    })


@router.post("/recycle-bin/restore-all")
async def recycle_bin_restore_all(request: Request):
    """Restore every series in the recycle bin."""
    restored = 0
    with get_db() as db:
        rows = db.execute(
            "SELECT id, title FROM series WHERE deleted_at IS NOT NULL"
        ).fetchall()
        for r in rows:
            db.execute(
                "UPDATE series SET deleted_at=NULL, deletion_reason=NULL"
                " WHERE id=?", (r['id'],)
            )
            _m.add_history(db, 'series_restored', None, r['title'] or '',
                           '', source_title=r['title'] or '')
            restored += 1

    if request.headers.get("HX-Request") == "true":
        return _Resp(headers={
            "HX-Trigger": json.dumps({
                "showToast": {
                    "msg": f"Restored {restored} series" if restored else "Recycle bin is empty",
                    "type": "success" if restored else "info",
                }
            }),
            "HX-Redirect": "/recycle-bin",
        })
    return RedirectResponse("/recycle-bin", status_code=303)


@router.post("/recycle-bin/empty")
async def recycle_bin_empty(request: Request):
    """Permanently delete EVERY series in the recycle bin."""
    purged = 0
    with get_db() as db:
        rows = db.execute(
            "SELECT id FROM series WHERE deleted_at IS NOT NULL"
        ).fetchall()
        for r in rows:
            try:
                _hard_delete_series(db, r['id'], log_history=True, remove_files=True)
                purged += 1
            except Exception:
                pass

    if request.headers.get("HX-Request") == "true":
        return _Resp(headers={
            "HX-Trigger": json.dumps({
                "showToast": {
                    "msg": f"Permanently deleted {purged} series" if purged else "Recycle bin is empty",
                    "type": "success" if purged else "info",
                }
            }),
            "HX-Redirect": "/recycle-bin",
        })
    return RedirectResponse("/recycle-bin", status_code=303)


@router.post("/series/{series_id}/grab")
async def manual_grab(request: Request, series_id: int):
    """Manual grab for all wanted volumes."""
    with get_db() as db:
        s = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
    if s:
        _m.create_background_task(_m.grab_existing(series_id, s['title'], s['search_pattern']), name=f"series:{series_id}:manual_grab")
    if request.headers.get("HX-Request") == "true":
        return _Resp(headers={"HX-Trigger": json.dumps({
            "showToast": {"msg": "Search queued for all wanted volumes", "type": "success"}
        })})
    return RedirectResponse(f"/series/{series_id}", status_code=303)


@router.post("/api/series/{series_id}/search-complete")
async def api_search_complete_pack(series_id: int):
    """Search for a complete pack."""
    with get_db() as db:
        s = db.execute(
            "SELECT title, total_volumes FROM series WHERE id=?", (series_id,)
        ).fetchone()
    if not s:
        return JSONResponse({'error': 'Series not found'}, status_code=404)
    grabbed = await _m.search_complete_pack(series_id, s['title'], s['total_volumes'])
    return JSONResponse({'grabbed': grabbed, 'title': s['title']})


@router.post("/series/{series_id}/volumes/{volume_id}/grab")
async def grab_volume(request: Request, series_id: int, volume_id: int):
    """Grab a specific volume."""
    with get_db() as db:
        s = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
        v = db.execute(
            "SELECT * FROM volumes WHERE id=? AND series_id=?", (volume_id, series_id)
        ).fetchone()
        swy_client = None
        if s and v and v['volume_num']:
            from app.routers.suwayomi_ import get_suwayomi_client, _get_series_source
            swy_client = get_suwayomi_client(db)
            if swy_client and not _get_series_source(series_id, dict(s)):
                swy_client = None

    if s and v:
        ddl_mode = get_cfg('ddl_grab_mode', 'fallback')
        ddl_available = swy_client and v['volume_num'] and ddl_mode != 'off'

        if ddl_mode == 'only' and ddl_available:
            from app.routers import suwayomi_ as _swy
            await _swy.suwayomi_grab(series_id, float(v['volume_num']))

        elif ddl_mode == 'prefer' and ddl_available:
            from app.routers import suwayomi_ as _swy
            ddl_ok = await _swy.suwayomi_grab(series_id, float(v['volume_num']))
            if not ddl_ok:
                vol_q = f"{s['title']} v{vol_num_to_display(v['volume_num'])}" if v['volume_num'] else s['title']
                _m.create_background_task(_grab_volume_task(series_id, s, v, vol_q), name=f"series:{series_id}:grab_volume:{volume_id}")

        else:
            vol_q = f"{s['title']} v{vol_num_to_display(v['volume_num'])}" if v['volume_num'] else s['title']
            grabbed = await _grab_volume_task_sync(series_id, s, v, vol_q)
            if not grabbed and ddl_available:
                from app.routers import suwayomi_ as _swy
                await _swy.suwayomi_grab(series_id, float(v['volume_num']))

    if request.headers.get("HX-Request") == "true":
        ctx = await _get_volume_row_ctx(series_id, volume_id)
        return templates.TemplateResponse(request, "partials/volume_row.html", ctx)
    return RedirectResponse(f"/series/{series_id}", status_code=303)


async def _grab_volume_task(series_id: int, s, v, query: str):
    """Core grab volume task."""
    specific = await _m._search_all(query, purpose='interactive', series_id=series_id)
    general  = await _m._search_all(s['title'], purpose='interactive', series_id=series_id) if query != s['title'] else []
    seen_urls_all = {i['url'] for i in specific}
    all_items = list(specific) + [i for i in general if i['url'] not in seen_urls_all]
    all_items.sort(key=lambda x: x.get('_score', 0), reverse=True)

    with get_db() as db:
        seen_urls    = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM seen").fetchall()}
        blocked_urls = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM blocklist").fetchall()}
        alias_rows   = db.execute(
            "SELECT alias FROM series_aliases WHERE series_id=?", (series_id,)
        ).fetchall()
    all_patterns = list({s['search_pattern'], s['title']} | {a['alias'] for a in alias_rows})
    target_vol = v['volume_num'] if v else None
    for item in all_items:
        if item['url'] in seen_urls or item['url'] in blocked_urls:
            continue
        if any(_m.matches(p, item['title']) for p in all_patterns):
            item_vol = _m.extract_volume_num(item['title'])
            item_rng = _m.extract_volume_range(item['title'])
            if item_rng is not None:
                item_vol = None
            vol_ok = (
                target_vol is None
                or item_vol is None
                or abs(item_vol - target_vol) < 0.01
                or (item_rng and item_rng[0] <= target_vol <= item_rng[1])
                or _m.is_complete_pack(item['title'])
            )
            if vol_ok:
                await _m.grab_item(item, series_id, respect_monitoring=False)
                break


async def _grab_volume_task_sync(series_id: int, s, v, query: str) -> bool:
    """Same as _grab_volume_task but returns True if something was grabbed."""
    specific = await _m._search_all(query, purpose='interactive', series_id=series_id)
    general  = await _m._search_all(s['title'], purpose='interactive', series_id=series_id) if query != s['title'] else []
    seen_urls_all = {i['url'] for i in specific}
    all_items = list(specific) + [i for i in general if i['url'] not in seen_urls_all]
    all_items.sort(key=lambda x: x.get('_score', 0), reverse=True)

    with get_db() as db:
        seen_urls    = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM seen").fetchall()}
        blocked_urls = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM blocklist").fetchall()}
        alias_rows   = db.execute(
            "SELECT alias FROM series_aliases WHERE series_id=?", (series_id,)
        ).fetchall()
    all_patterns = list({s['search_pattern'], s['title']} | {a['alias'] for a in alias_rows})
    target_vol = v['volume_num'] if v else None
    for item in all_items:
        if item['url'] in seen_urls or item['url'] in blocked_urls:
            continue
        if any(_m.matches(p, item['title']) for p in all_patterns):
            item_vol = _m.extract_volume_num(item['title'])
            item_rng = _m.extract_volume_range(item['title'])
            if item_rng is not None:
                item_vol = None
            vol_ok = (
                target_vol is None
                or item_vol is None
                or abs(item_vol - target_vol) < 0.01
                or (item_rng and item_rng[0] <= target_vol <= item_rng[1])
                or _m.is_complete_pack(item['title'])
            )
            if vol_ok:
                if await _m.grab_item(item, series_id, respect_monitoring=False):
                    return True
                break
    return False


@router.post("/series/{series_id}/toggle")
async def toggle_monitored(request: Request, series_id: int):
    """Toggle series monitored state."""
    with get_db() as db:
        cur = db.execute("SELECT monitored FROM series WHERE id=?", (series_id,)).fetchone()
        if cur:
            new_val = 0 if cur['monitored'] else 1
            db.execute("UPDATE series SET monitored=? WHERE id=?", (new_val, series_id))
    if request.headers.get("HX-Request") == "true":
        state = "monitored" if new_val else "unmonitored"
        return _Resp(headers={"HX-Trigger": json.dumps({
            "showToast": {"msg": f"Series {state}", "type": "success"}
        })})
    return RedirectResponse(f"/series/{series_id}", status_code=303)


@router.post("/series/{series_id}/refresh")
async def refresh_series(request: Request, series_id: int):
    """Refresh metadata from AniList."""
    with get_db() as db:
        s = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
    if not s:
        return RedirectResponse(f"/series/{series_id}", status_code=303)
    results = await _m.anilist_search(s['title'])
    if results:
        stored_words = set(_m.normalize(s['title']).split())

        def _title_f1(r) -> float:
            r_words = set(_m.normalize(r['title']).split())
            if not r_words or not stored_words:
                return 0.0
            inter     = stored_words & r_words
            recall    = len(inter) / len(stored_words)
            precision = len(inter) / len(r_words)
            return 2 * recall * precision / (recall + precision) if (recall + precision) else 0.0

        with get_db() as db2:
            max_stub_row = db2.execute(
                "SELECT MAX(volume_num) as m FROM volumes WHERE series_id=? AND volume_num IS NOT NULL",
                (series_id,)
            ).fetchone()
        min_vols   = int(max_stub_row['m']) if max_stub_row and max_stub_row['m'] else 0
        plausible  = [r for r in results if not r.get('volumes') or r.get('volumes', 0) >= min_vols]
        candidates = plausible if plausible else results
        best_by_title = max(candidates, key=lambda r: (_title_f1(r), r.get('volumes') or 0))
        match = None
        if stored_words and _title_f1(best_by_title) >= 0.5:
            match = best_by_title
        elif s['anilist_id']:
            match = next((r for r in candidates if r['anilist_id'] == s['anilist_id']), None)
        if not match:
            match = results[0]
        with get_db() as db3:
            existing = db3.execute(
                "SELECT total_volumes, total_chapters FROM series WHERE id=?", (series_id,)
            ).fetchone()
            new_total_vols = match['volumes'] or (existing['total_volumes'] if existing else None)
            new_total_chs  = match['chapters'] or (existing['total_chapters'] if existing else None)
            db3.execute(
                "UPDATE series SET status=?, cover_url=?, total_volumes=?, total_chapters=?,"
                " description=?, anilist_id=?, last_metadata_refresh=?,"
                " vol_count_source=CASE WHEN COALESCE(vol_count_source,'anilist')"
                " IN ('google_books','wikipedia','manual') THEN vol_count_source ELSE 'anilist' END"
                " WHERE id=?",
                (match['status'], match['cover_url'], new_total_vols, new_total_chs,
                 match['description'], match['anilist_id'], datetime.utcnow().isoformat(), series_id)
            )
            if match['volumes'] and int(match['volumes']) > 0 \
                    and (s['edition_type'] or 'standard') not in _m._NON_STANDARD_STUB_EDITIONS:
                _m.create_volume_stubs(db3, series_id, int(match['volumes']))
                has_complete = db3.execute(
                    "SELECT 1 FROM volumes WHERE series_id=? AND pack_type='complete' AND status='grabbed'",
                    (series_id,)
                ).fetchone()
                if has_complete:
                    db3.execute(
                        "UPDATE volumes SET status='grabbed' WHERE series_id=? "
                        "AND status='wanted' AND volume_num IS NOT NULL",
                        (series_id,)
                    )
        _m.log_event('refresh', f"Refreshed from AniList: status={match['status']}, "
                     f"{match['volumes'] or '?'} vols", series_id)
        await _m.refresh_mangadex_map(series_id)
        _m.backfill_pack_ranges()
        if match.get('anilist_id'):
            _m.create_background_task(_m.fetch_anilist_aliases(series_id, match['anilist_id'], s['title']), name=f"series:{series_id}:refresh_aliases")
        _m.create_background_task(_m.fetch_mu_metadata(series_id, s['title']), name=f"series:{series_id}:refresh_mu")
    if request.headers.get("HX-Request") == "true":
        return _Resp(headers={"HX-Trigger": json.dumps({
            "showToast": {"msg": "Refreshed", "type": "success"}
        })})
    return RedirectResponse(f"/series/{series_id}", status_code=303)


async def _get_volume_row_ctx(series_id: int, volume_id: int) -> dict:
    """Build template context for a single volume row partial."""
    with get_db() as db:
        s = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
        v = db.execute("SELECT * FROM volumes WHERE id=? AND series_id=?",
                       (volume_id, series_id)).fetchone()
        vchs = db.execute(
            "SELECT * FROM chapters WHERE volume_id=? AND series_id=? ORDER BY chapter_num",
            (volume_id, series_id)
        ).fetchall()
        _iq = db.execute(
            "SELECT download_id, status FROM import_queue WHERE series_id=?"
            " AND status IN ('pending','partial')", (series_id,)
        ).fetchall()
    swy_vol_jobs = _build_swy_vol_jobs(db, series_id)
    pending_dl_ids = {(r['download_id'] or '').lower() for r in _iq
                      if r['download_id'] and r['status'] == 'pending'}
    review_dl_ids  = {(r['download_id'] or '').lower() for r in _iq
                      if r['download_id'] and r['status'] == 'partial'}
    vct = {
        'total':      len(vchs),
        'downloaded': sum(1 for c in vchs if c['status'] == 'downloaded'),
        'grabbed':    sum(1 for c in vchs if c['status'] == 'grabbed'),
        'wanted':     sum(1 for c in vchs if c['status'] == 'wanted' and c['monitored']),
    }
    effective_cutoff = (s['quality_cutoff'] or '').strip() if s else ''
    effective_cutoff = effective_cutoff or get_cfg('quality_cutoff', '')
    return {
        "s": s, "v": v,
        "vchs": list(vchs), "vct": vct,
        "quality_cutoff":  effective_cutoff,
        "cutoff_rank":     quality_rank(effective_cutoff),
        "pending_dl_ids":  pending_dl_ids,
        "review_dl_ids":   review_dl_ids,
        "active_dl_ids":   set(),
        "dl_stages":       {},
        "swy_vol_jobs":    swy_vol_jobs,
    }


def _build_swy_vol_jobs(db, series_id: int) -> dict:
    """Return {volume_num: {progress, total, status, error}} for active Suwayomi jobs."""
    rows = db.execute(
        "SELECT volume_num, progress, total, status, error"
        " FROM suwayomi_downloads"
        " WHERE series_id=? AND volume_num IS NOT NULL AND status IN ('queued','error')",
        (series_id,),
    ).fetchall()
    return {float(r["volume_num"]): dict(r) for r in rows}


@router.post("/library/refresh-all")
async def refresh_all_series(request: Request):
    """Refresh metadata from AniList for all monitored series."""
    async def _run():
        with get_db() as db:
            series = db.execute(
                "SELECT id, title, edition_type FROM series WHERE monitored=1 ORDER BY title"
            ).fetchall()
        refreshed = 0
        for s in series:
            try:
                results = await _m.anilist_search(s['title'])
                if results:
                    stored_words = set(_m.normalize(s['title']).split())
                    def _f1(r) -> float:
                        r_words = set(_m.normalize(r['title']).split())
                        if not r_words or not stored_words: return 0.0
                        inter = stored_words & r_words
                        rec = len(inter)/len(stored_words); prec = len(inter)/len(r_words)
                        return 2*rec*prec/(rec+prec) if (rec+prec) else 0.0
                    with get_db() as db2:
                        max_row = db2.execute(
                            "SELECT MAX(volume_num) as m FROM volumes"
                            " WHERE series_id=? AND volume_num IS NOT NULL",
                            (s['id'],)
                        ).fetchone()
                        s_row = db2.execute(
                            "SELECT anilist_id FROM series WHERE id=?", (s['id'],)
                        ).fetchone()
                    min_vols   = int(max_row['m']) if max_row and max_row['m'] else 0
                    plausible  = [r for r in results if not r.get('volumes') or r.get('volumes', 0) >= min_vols]
                    candidates = plausible if plausible else results
                    best = max(candidates, key=lambda r: (_f1(r), r.get('volumes') or 0))
                    match = None
                    if stored_words and _f1(best) >= 0.5:
                        match = best
                    elif s_row and s_row['anilist_id']:
                        match = next((r for r in candidates if r['anilist_id'] == s_row['anilist_id']), None)
                    if not match:
                        match = results[0]
                    with get_db() as db3:
                        db3.execute(
                            "UPDATE series SET status=?, cover_url=?, total_volumes=?, total_chapters=?,"
                            " description=?,"
                            " vol_count_source=CASE WHEN COALESCE(vol_count_source,'anilist')"
                            " IN ('google_books','wikipedia','manual') THEN vol_count_source ELSE 'anilist' END"
                            " WHERE id=?",
                            (match['status'], match['cover_url'], match['volumes'] or None,
                             match['chapters'] or None, match['description'], s['id'])
                        )
                        if match['volumes'] and int(match['volumes']) > 0 \
                                and (s['edition_type'] or 'standard') not in _m._NON_STANDARD_STUB_EDITIONS:
                            _m.create_volume_stubs(db3, s['id'], int(match['volumes']))
                    refreshed += 1
            except Exception as e:
                print(f"[RefreshAll] Error refreshing {s['title']}: {e}")
            await asyncio.sleep(1.5)
        _m.log_event('refresh', f"Refresh all: {refreshed}/{len(series)} series updated")

    _m.create_background_task(_run(), name="series:refresh_all")
    if request.headers.get("HX-Request") == "true":
        return _Resp(headers={"HX-Trigger": json.dumps({
            "showToast": {"msg": "Metadata refresh started in background", "type": "success"}
        })})
    return RedirectResponse("/?sort=added", status_code=303)
