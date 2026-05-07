"""Volume/chapter state management - mark, reset, toggle, delete, upgrade."""

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response as _Resp

from app.routers.series_ import router
from app.routers._templates import templates
from app.shared import get_cfg, get_db, vol_num_to_display, quality_rank, cascade_chapters
from app import main as _m
from datetime import datetime
import os


@router.post("/series/{series_id}/volumes/{volume_id}/mark-downloaded")
async def mark_volume_downloaded(request: Request, series_id: int, volume_id: int):
    """Mark a volume as downloaded."""
    with get_db() as db:
        now_ts = datetime.utcnow().isoformat()
        v = db.execute(
            "SELECT volume_num FROM volumes WHERE id=? AND series_id=?",
            (volume_id, series_id)
        ).fetchone()
        s = db.execute("SELECT title FROM series WHERE id=?", (series_id,)).fetchone()
        db.execute(
            "UPDATE volumes SET status='downloaded', imported_at=COALESCE(imported_at,?)"
            " WHERE id=? AND series_id=?",
            (now_ts, volume_id, series_id)
        )
        cascade_chapters(db, series_id, [volume_id], 'downloaded')
        if v and s:
            vol_label = f"Vol {vol_num_to_display(v['volume_num'])}" if v['volume_num'] else '—'
            _m.add_history(db, 'volume_marked_downloaded', series_id, s['title'], vol_label)
    if request.headers.get("HX-Request") == "true":
        ctx = await _get_volume_row_ctx(series_id, volume_id)
        return templates.TemplateResponse(request, "partials/volume_row.html", ctx)
    return RedirectResponse(f"/series/{series_id}", status_code=303)


@router.post("/series/{series_id}/volumes/{volume_id}/mark-wanted")
async def mark_volume_wanted(request: Request, series_id: int, volume_id: int):
    """Mark a volume as wanted."""
    with get_db() as db:
        row = db.execute(
            "SELECT source_url, download_id, volume_num FROM volumes WHERE id=? AND series_id=?",
            (volume_id, series_id)
        ).fetchone()
        s = db.execute("SELECT title FROM series WHERE id=?", (series_id,)).fetchone()
        if row:
            if row['source_url']:
                db.execute("DELETE FROM seen WHERE torrent_url=?", (row['source_url'],))
            if row['download_id']:
                others = db.execute(
                    "SELECT COUNT(*) FROM volumes WHERE download_id=? AND status='grabbed' AND id != ?",
                    (row['download_id'], volume_id)
                ).fetchone()[0]
                if others == 0:
                    db.execute("DELETE FROM seen WHERE download_id=?", (row['download_id'],))
        db.execute(
            "UPDATE volumes SET status='wanted', grabbed_at=NULL, imported_at=NULL,"
            " import_path=NULL, source_url=NULL, download_id=NULL, torrent_name=NULL,"
            " indexer=NULL, protocol=NULL, client=NULL, release_group=NULL,"
            " size_bytes=NULL, quality=NULL WHERE id=? AND series_id=?",
            (volume_id, series_id)
        )
        cascade_chapters(db, series_id, [volume_id], 'wanted',
                         grabbed_at=None, torrent_name=None, torrent_url=None,
                         indexer=None, protocol=None, client=None,
                         download_id=None, release_group=None)
        if row and s:
            vol_label = f"Vol {vol_num_to_display(row['volume_num'])}" if row['volume_num'] else '—'
            _m.add_history(db, 'volume_marked_wanted', series_id, s['title'], vol_label)
    if request.headers.get("HX-Request") == "true":
        ctx = await _get_volume_row_ctx(series_id, volume_id)
        return templates.TemplateResponse(request, "partials/volume_row.html", ctx)
    return RedirectResponse(f"/series/{series_id}", status_code=303)


@router.post("/series/{series_id}/volumes/{volume_id}/reset-to-wanted")
async def reset_volume_to_wanted(series_id: int, volume_id: int):
    """Reset a grabbed volume back to wanted."""
    with get_db() as db:
        db.execute(
            "UPDATE volumes SET status='wanted', download_id=NULL, grabbed_at=NULL,"
            " source_url=NULL, torrent_name=NULL, indexer=NULL, protocol=NULL,"
            " client=NULL, release_group=NULL, import_path=NULL"
            " WHERE id=? AND series_id=? AND status='grabbed'",
            (volume_id, series_id)
        )
    return RedirectResponse(f"/series/{series_id}", status_code=303)


@router.post("/series/{series_id}/volumes/{volume_id}/toggle-monitor")
async def toggle_volume_monitor(request: Request, series_id: int, volume_id: int):
    """Toggle volume monitored state."""
    with get_db() as db:
        v = db.execute(
            "SELECT monitored FROM volumes WHERE id=? AND series_id=?", (volume_id, series_id)
        ).fetchone()
        if v:
            db.execute(
                "UPDATE volumes SET monitored=? WHERE id=?",
                (0 if v['monitored'] else 1, volume_id)
            )
    if request.headers.get("HX-Request") == "true":
        ctx = await _get_volume_row_ctx(series_id, volume_id)
        return templates.TemplateResponse(request, "partials/volume_row.html", ctx)
    return RedirectResponse(f"/series/{series_id}", status_code=303)


@router.post("/series/{series_id}/volumes/{volume_id}/delete-file")
async def delete_volume_file(request: Request, series_id: int, volume_id: int):
    """Delete volume file from disk and reset to wanted."""
    with get_db() as db:
        v = db.execute(
            "SELECT * FROM volumes WHERE id=? AND series_id=?", (volume_id, series_id)
        ).fetchone()
        s = db.execute("SELECT title FROM series WHERE id=?", (series_id,)).fetchone()
        if not v:
            return RedirectResponse(f"/series/{series_id}", status_code=303)

        deleted = False
        if v['import_path']:
            path = v['import_path']
            if os.path.isfile(path):
                try:
                    os.remove(path)
                    deleted = True
                except Exception as e:
                    _m.log_event('error', f"File delete failed: {e}", series_id)
            elif os.path.isdir(path) and v['volume_num']:
                import re
                for fname in os.listdir(path):
                    fvol = _m.extract_volume_num(fname)
                    if fvol is not None and abs(fvol - v['volume_num']) < 0.01:
                        try:
                            os.remove(os.path.join(path, fname))
                            deleted = True
                        except Exception as e:
                            _m.log_event('error', f"File delete failed: {e}", series_id)
                        break

        db.execute(
            "UPDATE volumes SET status='wanted', import_path=NULL, download_id=NULL, "
            "grabbed_at=NULL, source_url=NULL, torrent_name=NULL, indexer=NULL, protocol=NULL,"
            " client=NULL, release_group=NULL WHERE id=?", (volume_id,)
        )
        cascade_chapters(db, series_id, [volume_id], 'wanted',
                         grabbed_at=None, torrent_name=None, torrent_url=None,
                         indexer=None, protocol=None, client=None,
                         download_id=None, release_group=None)
        from shared import build_volume_label
        vol_label = build_volume_label(v['volume_num'], None, None)
        _m.add_history(db, 'file_deleted', series_id, s['title'] if s else '',
                       vol_label, source_title=v['torrent_name'] or '',
                       data={'deleted': deleted, 'path': v['import_path']})
        msg = f"Deleted file for {vol_label}" if deleted else f"Reset {vol_label} to wanted (file not found)"
        _m.log_event('delete', msg, series_id)
    if request.headers.get("HX-Request") == "true":
        ctx = await _get_volume_row_ctx(series_id, volume_id)
        return templates.TemplateResponse(request, "partials/volume_row.html", ctx)
    return RedirectResponse(f"/series/{series_id}", status_code=303)


@router.post("/series/{series_id}/volumes/{volume_id}/set-range")
async def set_pack_range(
    request:         Request,
    series_id:       int,
    volume_id:       int,
    vol_range_start: float = Form(0),
    vol_range_end:   float = Form(0),
    mark_stubs:      str   = Form("1"),
):
    """Set volume range for a pack."""
    now = datetime.utcnow().isoformat()
    with get_db() as db:
        pack = db.execute(
            "SELECT torrent_name FROM volumes WHERE id=? AND series_id=?",
            (volume_id, series_id)
        ).fetchone()
        if not pack:
            return RedirectResponse(f"/series/{series_id}", status_code=303)
        db.execute(
            "UPDATE volumes SET vol_range_start=?, vol_range_end=? WHERE id=?",
            (vol_range_start or None, vol_range_end or None, volume_id)
        )
        if mark_stubs and vol_range_start and vol_range_end:
            db.execute(
                "UPDATE volumes SET status='grabbed', grabbed_at=?, torrent_name=? "
                "WHERE series_id=? AND status='wanted' "
                "AND volume_num IS NOT NULL "
                "AND volume_num >= ? AND volume_num <= ?",
                (now, pack['torrent_name'], series_id, vol_range_start, vol_range_end)
            )
        elif mark_stubs and not vol_range_start and not vol_range_end:
            db.execute(
                "UPDATE volumes SET status='grabbed', grabbed_at=?, torrent_name=? "
                "WHERE series_id=? AND status='wanted' AND volume_num IS NOT NULL",
                (now, pack['torrent_name'], series_id)
            )
    if request.headers.get("HX-Request") == "true":
        return _Resp(headers={"HX-Refresh": "true"})
    return RedirectResponse(f"/series/{series_id}", status_code=303)


@router.post("/series/{series_id}/set-root-folder")
async def set_series_root_folder(request: Request, series_id: int, root_folder_id: int = Form(0)):
    """Set series root folder."""
    with get_db() as db:
        db.execute(
            "UPDATE series SET root_folder_id=? WHERE id=?",
            (root_folder_id or None, series_id)
        )
    if request.headers.get("HX-Request") == "true":
        return _Resp(headers={"HX-Trigger": json.dumps({
            "showToast": {"msg": "Root folder updated", "type": "success"}
        })})
    return RedirectResponse(f"/series/{series_id}", status_code=303)


@router.post("/series/{series_id}/set-monitor-mode")
async def set_monitor_mode(request: Request, series_id: int, mode: str = Form("all")):
    """Set series monitor mode."""
    valid = ('all', 'future', 'missing', 'existing', 'none')
    if mode not in valid:
        mode = 'all'
    with get_db() as db:
        db.execute("UPDATE series SET monitor_mode=? WHERE id=?", (mode, series_id))
        if mode == 'none':
            db.execute("UPDATE volumes SET monitored=0 WHERE series_id=?", (series_id,))
        elif mode == 'all':
            db.execute("UPDATE volumes SET monitored=1 WHERE series_id=?", (series_id,))
        elif mode == 'missing':
            db.execute(
                "UPDATE volumes SET monitored=CASE WHEN status='wanted' THEN 1 ELSE 0 END "
                "WHERE series_id=?", (series_id,)
            )
        elif mode == 'existing':
            db.execute(
                "UPDATE volumes SET monitored=CASE WHEN status='downloaded' THEN 1 ELSE 0 END "
                "WHERE series_id=?", (series_id,)
            )
        elif mode == 'future':
            max_dl = db.execute(
                "SELECT MAX(volume_num) as m FROM volumes "
                "WHERE series_id=? AND status='downloaded' AND volume_num IS NOT NULL",
                (series_id,)
            ).fetchone()
            threshold = (max_dl['m'] or 0) if max_dl else 0
            db.execute(
                "UPDATE volumes SET monitored=CASE WHEN volume_num > ? THEN 1 ELSE 0 END "
                "WHERE series_id=?", (threshold, series_id)
            )
    _m.log_event('monitor', f"Monitor mode set to '{mode}'", series_id)
    if request.headers.get("HX-Request") == "true":
        labels = {'all': 'All', 'future': 'Future', 'missing': 'Missing', 'existing': 'Existing', 'none': 'None'}
        return _Resp(headers={"HX-Trigger": json.dumps({
            "showToast": {"msg": f"Monitor mode: {labels.get(mode, mode)}", "type": "success"}
        })})
    return RedirectResponse(f"/series/{series_id}", status_code=303)


@router.post("/series/{series_id}/rescan")
async def rescan_series(request: Request, series_id: int):
    """Rescan series folder."""
    with get_db() as db:
        result = _m.rescan_series_folder(db, series_id)
    parts = []
    if result['found']:     parts.append(f"{result['found']} file(s) on disk")
    if result['recovered']: parts.append(f"{result['recovered']} marked downloaded")
    if result['missing']:   parts.append(f"{result['missing']} reset to wanted (files missing)")
    if result['lost']:      parts.append(f"{result['lost']} reset to wanted (grab lost)")
    if result.get('created'): parts.append(f"{result['created']} new stub(s) created from disk")
    msg = "Rescan: " + (", ".join(parts) if parts else "nothing changed")
    _m.log_event('rescan', msg, series_id)
    if request.headers.get("HX-Request") == "true":
        return _Resp(headers={"HX-Trigger": json.dumps({
            "showToast": {"msg": msg, "type": "success"}
        })})
    return RedirectResponse(f"/series/{series_id}", status_code=303)


@router.post("/api/series/{series_id}/reinject-metadata")
async def reinject_metadata(series_id: int):
    """Re-inject ComicInfo.xml."""
    with get_db() as db:
        s = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
        if not s:
            return JSONResponse({"ok": False, "message": "Series not found"})
        tags = [r['tag'] for r in db.execute(
            "SELECT tag FROM series_tags WHERE series_id=?", (series_id,)
        ).fetchall()]
        vols = db.execute(
            "SELECT volume_num, import_path FROM volumes"
            " WHERE series_id=? AND status='downloaded' AND import_path IS NOT NULL",
            (series_id,)
        ).fetchall()
        chaps = db.execute(
            "SELECT chapter_num, import_path FROM chapters"
            " WHERE series_id=? AND status='downloaded' AND import_path IS NOT NULL",
            (series_id,)
        ).fetchall()

    ok_count = skip_count = fail_count = 0
    for v in vols:
        if not os.path.isfile(v['import_path']):
            skip_count += 1; continue
        xml = _m.build_comicinfo_xml(dict(s), volume_num=v['volume_num'], tags=tags)
        if _m.inject_comicinfo(v['import_path'], xml):
            ok_count += 1
        else:
            fail_count += 1
    for c in chaps:
        if not os.path.isfile(c['import_path']):
            skip_count += 1; continue
        xml = _m.build_comicinfo_xml(dict(s), chapter_num=c['chapter_num'], tags=tags)
        if _m.inject_comicinfo(c['import_path'], xml):
            ok_count += 1
        else:
            fail_count += 1

    _m.log_event('metadata',
                 f"Re-injected ComicInfo.xml: {ok_count} updated, "
                 f"{skip_count} missing, {fail_count} skipped (non-CBZ)",
                 series_id)
    return JSONResponse({
        "ok": True, "updated": ok_count,
        "skipped_missing": skip_count, "skipped_format": fail_count,
    })


@router.post("/library/rescan")
async def rescan_all_series(request: Request):
    """Rescan entire library."""
    _m.create_background_task(_rescan_all_impl(), name="series:rescan_all")
    if request.headers.get("HX-Request") == "true":
        return _Resp(headers={"HX-Trigger": json.dumps({
            "showToast": {"msg": "Library rescan started in background", "type": "success"}
        })})
    return RedirectResponse("/health", status_code=303)


async def _rescan_all_impl():
    """Core logic for full library rescan."""
    import main as _m
    with get_db() as db:
        series_ids = [r['id'] for r in db.execute("SELECT id FROM series").fetchall()]
        total = {'found': 0, 'recovered': 0, 'missing': 0, 'lost': 0, 'created': 0}
        for sid in series_ids:
            r = _m.rescan_series_folder(db, sid)
            total['found']     += r['found']
            total['recovered'] += r['recovered']
            total['missing']   += r['missing']
            total['lost']      += r['lost']
            total['created']   += r.get('created', 0)
    _m.log_event('rescan',
        f"Full library rescan: {total['found']} files, "
        f"{total['recovered']} recovered, {total['missing']} missing, "
        f"{total['lost']} grabs lost, {total['created']} stubs created")


@router.post("/series/{series_id}/mark-all-downloaded")
async def mark_all_grabbed_downloaded(request: Request, series_id: int):
    """Mark all grabbed volumes as downloaded."""
    with get_db() as db:
        cur = db.execute(
            "UPDATE volumes SET status='downloaded' WHERE series_id=? AND status='grabbed'"
            " AND volume_num IS NOT NULL",
            (series_id,)
        )
        marked = cur.rowcount
    _m.log_event('download_complete', "Manually marked all grabbed volumes as downloaded", series_id)
    if request.headers.get("HX-Request") == "true":
        return _Resp(headers={"HX-Refresh": "true"})
    return RedirectResponse(f"/series/{series_id}", status_code=303)


@router.post("/series/{sid}/chapters/{cid}/toggle-monitor")
async def toggle_chapter_monitor(request: Request, sid: int, cid: int):
    """Toggle chapter monitored state."""
    with get_db() as db:
        ch = db.execute(
            "SELECT monitored, volume_id FROM chapters WHERE id=? AND series_id=?", (cid, sid)
        ).fetchone()
        if ch:
            db.execute("UPDATE chapters SET monitored=? WHERE id=?",
                       (0 if ch['monitored'] else 1, cid))
    if request.headers.get("HX-Request") == "true" and ch and ch['volume_id']:
        ctx = await _get_volume_row_ctx(sid, ch['volume_id'])
        return templates.TemplateResponse(request, "partials/volume_row.html", ctx)
    return RedirectResponse(f"/series/{sid}", status_code=303)


@router.post("/series/{sid}/chapters/{cid}/mark-downloaded")
async def mark_chapter_downloaded(request: Request, sid: int, cid: int):
    """Mark a chapter as downloaded."""
    with get_db() as db:
        ch = db.execute("SELECT chapter_num, volume_id FROM chapters WHERE id=?", (cid,)).fetchone()
        s  = db.execute("SELECT title FROM series WHERE id=?", (sid,)).fetchone()
        now_iso = datetime.utcnow().isoformat()
        if ch and ch['volume_id']:
            _sib = db.execute(
                "SELECT import_path, quality, torrent_name, indexer, protocol,"
                " client, release_group, size_bytes, download_id"
                " FROM volumes WHERE id=?",
                (ch['volume_id'],)
            ).fetchone()
            _sib = dict(_sib) if _sib else {}
            db.execute(
                "UPDATE chapters SET status='downloaded',"
                " imported_at=COALESCE(imported_at,?),"
                " import_path=COALESCE(import_path,?),"
                " quality=COALESCE(quality,?),"
                " torrent_name=COALESCE(torrent_name,?),"
                " indexer=COALESCE(indexer,?),"
                " protocol=COALESCE(protocol,?),"
                " client=COALESCE(client,?),"
                " release_group=COALESCE(release_group,?),"
                " size_bytes=COALESCE(NULLIF(size_bytes,0),?),"
                " download_id=COALESCE(download_id,?)"
                " WHERE id=? AND series_id=?",
                (now_iso,
                 _sib.get('import_path'), _sib.get('quality'),
                 _sib.get('torrent_name'), _sib.get('indexer'),
                 _sib.get('protocol'), _sib.get('client'),
                 _sib.get('release_group'), _sib.get('size_bytes'),
                 _sib.get('download_id'),
                 cid, sid)
            )
            _m._check_volume_completion(db, sid, ch['volume_id'])
        else:
            db.execute(
                "UPDATE chapters SET status='downloaded',"
                " imported_at=COALESCE(imported_at,?)"
                " WHERE id=? AND series_id=?",
                (now_iso, cid, sid)
            )
        if ch and s:
            ch_label = f"Ch {ch['chapter_num']}" if ch['chapter_num'] is not None else '—'
            _m.add_history(db, 'chapter_marked_downloaded', sid, s['title'], ch_label)
    if request.headers.get("HX-Request") == "true":
        if ch and ch['volume_id']:
            ctx = await _get_volume_row_ctx(sid, ch['volume_id'])
            return templates.TemplateResponse(request, "partials/volume_row.html", ctx)
        return _Resp(headers={"HX-Refresh": "true"})
    return RedirectResponse(f"/series/{sid}", status_code=303)


@router.post("/series/{sid}/chapters/{cid}/mark-wanted")
async def mark_chapter_wanted(request: Request, sid: int, cid: int):
    """Mark a chapter as wanted."""
    with get_db() as db:
        ch = db.execute("SELECT chapter_num, volume_id FROM chapters WHERE id=?", (cid,)).fetchone()
        s  = db.execute("SELECT title FROM series WHERE id=?", (sid,)).fetchone()
        db.execute(
            "UPDATE chapters SET status='wanted', grabbed_at=NULL WHERE id=? AND series_id=?",
            (cid, sid)
        )
        if ch and s:
            ch_label = f"Ch {ch['chapter_num']}" if ch['chapter_num'] is not None else '—'
            _m.add_history(db, 'chapter_marked_wanted', sid, s['title'], ch_label)
    if request.headers.get("HX-Request") == "true" and ch and ch['volume_id']:
        ctx = await _get_volume_row_ctx(sid, ch['volume_id'])
        return templates.TemplateResponse(request, "partials/volume_row.html", ctx)
    return RedirectResponse(f"/series/{sid}", status_code=303)


@router.post("/series/{sid}/chapters/{cid}/grab")
async def grab_chapter_route(request: Request, sid: int, cid: int):
    """Grab a specific chapter."""
    with get_db() as db:
        s  = db.execute("SELECT * FROM series WHERE id=?", (sid,)).fetchone()
        ch = db.execute(
            "SELECT * FROM chapters WHERE id=? AND series_id=?", (cid, sid)
        ).fetchone()
    if not s or not ch:
        return RedirectResponse(with_flash(f"/series/{sid}", "No wanted chapters found", "info"), status_code=303)
    _m.create_background_task(_grab_chapter_task(sid, dict(s), dict(ch)), name=f"series:{sid}:grab_chapter:{cid}")
    if request.headers.get("HX-Request") == "true":
        if ch['volume_id']:
            ctx = await _get_volume_row_ctx(sid, ch['volume_id'])
            return templates.TemplateResponse(request, "partials/volume_row.html", ctx)
        return _Resp(headers={"HX-Refresh": "true"})
    return RedirectResponse(with_flash(f"/series/{sid}", "Grab queued for 1 chapter", "success"), status_code=303)


async def _grab_chapter_task(sid: int, s: dict, ch: dict):
    """Grab chapter task."""
    ch_num = ch['chapter_num']
    ch_int = int(ch_num) if ch_num == int(ch_num) else ch_num

    from app.routers import suwayomi_ as _swy
    if _swy._ddl_enabled() and _swy._get_series_source(sid, s):
        with get_db() as _db:
            _swy_client = _swy.get_suwayomi_client(_db)
        if _swy_client:
            ok = await _swy.suwayomi_chapter_grab(sid, float(ch_num))
            if ok:
                return

    query  = f"{s['search_pattern']} chapter {ch_int}"
    all_items = await _m._search_all(query, series_id=sid)
    with get_db() as db:
        seen_urls    = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM seen").fetchall()}
        blocked_urls = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM blocklist").fetchall()}
    for item in all_items:
        if item['url'] in seen_urls or item['url'] in blocked_urls:
            continue
        if not _m.matches(s['search_pattern'], item['title']):
            continue
        item_ch  = _m.extract_chapter_num(item['title'])
        item_rng = _m.extract_volume_range(item['title'])
        ch_ok = (
            (item_ch is not None and abs(item_ch - ch_num) < 0.01)
            or (item_rng and item_rng[0] <= ch_num <= item_rng[1])
            or _m.is_complete_pack(item['title'])
        )
        if ch_ok:
            if await _m.grab_item(item, sid, respect_monitoring=False):
                with get_db() as db:
                    if ch['volume_id']:
                        _sib = db.execute(
                            "SELECT source_url, torrent_name, indexer, protocol, client,"
                            " download_id, release_group, size_bytes"
                            " FROM volumes WHERE id=?",
                            (ch['volume_id'],)
                        ).fetchone()
                        _sib = dict(_sib) if _sib else {}
                        db.execute(
                            "UPDATE chapters SET status='grabbed', grabbed_at=?,"
                            " torrent_url=COALESCE(torrent_url,?),"
                            " torrent_name=COALESCE(torrent_name,?),"
                            " indexer=COALESCE(indexer,?),"
                            " protocol=COALESCE(protocol,?),"
                            " client=COALESCE(client,?),"
                            " download_id=COALESCE(download_id,?),"
                            " release_group=COALESCE(release_group,?),"
                            " size_bytes=COALESCE(NULLIF(size_bytes,0),?)"
                            " WHERE id=? AND status='wanted'",
                            (datetime.utcnow().isoformat(),
                             _sib.get('source_url') or item['url'],
                             _sib.get('torrent_name') or item['title'],
                             _sib.get('indexer') or item.get('indexer'),
                             _sib.get('protocol') or item.get('protocol'),
                             _sib.get('client'),
                             _sib.get('download_id'),
                             _sib.get('release_group'),
                             _sib.get('size_bytes'),
                             ch['id'])
                        )
                    else:
                        db.execute(
                            "UPDATE chapters SET status='grabbed', grabbed_at=?,"
                            " torrent_url=?, torrent_name=?, indexer=?, protocol=?"
                            " WHERE id=? AND status='wanted'",
                            (datetime.utcnow().isoformat(), item['url'], item['title'],
                             item.get('indexer'), item.get('protocol'), ch['id'])
                        )
            break


# ── Uncollected chapters ──────────────────────────────────────────────────────

@router.post("/series/{sid}/uncollected/toggle-monitor")
async def uncollected_toggle_monitor(request: Request, sid: int):
    """Toggle uncollected chapters monitored state."""
    with get_db() as db:
        current = db.execute(
            "SELECT monitored FROM chapters WHERE series_id=? AND volume_id IS NULL LIMIT 1", (sid,)
        ).fetchone()
        new_val = 0 if (current and current['monitored']) else 1
        db.execute(
            "UPDATE chapters SET monitored=? WHERE series_id=? AND volume_id IS NULL",
            (new_val, sid)
        )
    if request.headers.get("HX-Request") == "true":
        return _Resp(headers={"HX-Refresh": "true"})
    msg = "Uncollected chapters monitored" if new_val else "Uncollected chapters unmonitored"
    return RedirectResponse(with_flash(f"/series/{sid}", msg, "success"), status_code=303)


@router.post("/series/{sid}/uncollected/mark-downloaded")
async def uncollected_mark_downloaded(request: Request, sid: int):
    """Mark all uncollected chapters as downloaded."""
    with get_db() as db:
        db.execute(
            "UPDATE chapters SET status='downloaded',"
            " imported_at=COALESCE(imported_at,?)"
            " WHERE series_id=? AND volume_id IS NULL",
            (datetime.utcnow().isoformat(), sid)
        )
    if request.headers.get("HX-Request") == "true":
        return _Resp(headers={"HX-Refresh": "true"})
    return RedirectResponse(f"/series/{sid}", status_code=303)


@router.post("/series/{sid}/uncollected/grab-all")
async def uncollected_grab_all(request: Request, sid: int):
    """Grab all uncollected chapters."""
    with get_db() as db:
        s   = db.execute("SELECT * FROM series WHERE id=?", (sid,)).fetchone()
        chs = db.execute(
            "SELECT * FROM chapters WHERE series_id=? AND volume_id IS NULL"
            " AND status='wanted' AND monitored=1",
            (sid,)
        ).fetchall()
    if not s or not chs:
        if request.headers.get("HX-Request") == "true":
            return _Resp(headers={"HX-Trigger": json.dumps({
                "showToast": {"msg": "No wanted chapters found", "type": "info"}
            })})
        return RedirectResponse(f"/series/{sid}", status_code=303)
    for ch in chs:
        _m.create_background_task(_grab_chapter_task(sid, dict(s), dict(ch)), name=f"series:{sid}:grab_chapter:{ch['id']}")
    if request.headers.get("HX-Request") == "true":
        return _Resp(headers={"HX-Trigger": json.dumps({
            "showToast": {"msg": f"Grab queued for {len(chs)} chapters", "type": "success"}
        })})
    return RedirectResponse(f"/series/{sid}", status_code=303)


@router.post("/series/{sid}/volumes/{vol_id}/trigger-upgrade")
async def trigger_volume_upgrade(
    request: Request, sid: int, vol_id: int,
    redirect_to: str = Form("/calendar")
):
    """Trigger a volume upgrade search."""
    with get_db() as db:
        s = db.execute("SELECT * FROM series WHERE id=?", (sid,)).fetchone()
        v = db.execute("SELECT * FROM volumes WHERE id=? AND series_id=?", (vol_id, sid)).fetchone()
    if not s or not v or not v['volume_num']:
        if request.headers.get("HX-Request") == "true":
            ctx = await _get_volume_row_ctx(sid, vol_id)
            return templates.TemplateResponse(request, "partials/volume_row.html", ctx)
        return RedirectResponse(f"/series/{sid}", status_code=303)
    query = f"{s['search_pattern']} volume {vol_num_to_display(v['volume_num'])}"
    _m.create_background_task(_grab_volume_task(sid, s, v, query), name=f"series:{sid}:upgrade_volume:{vol_id}")
    if request.headers.get("HX-Request") == "true":
        ctx = await _get_volume_row_ctx(sid, vol_id)
        return templates.TemplateResponse(request, "partials/volume_row.html", ctx)
    safe_redirect = redirect_to if redirect_to.startswith('/') else f"/series/{sid}"
    return RedirectResponse(safe_redirect, status_code=303)


@router.post("/series/{sid}/grab-all-wanted")
async def grab_all_wanted_for_series(request: Request, sid: int):
    """Grab all wanted volumes for a series."""
    with get_db() as db:
        s      = db.execute("SELECT * FROM series WHERE id=?", (sid,)).fetchone()
        wanted = db.execute(
            "SELECT * FROM volumes WHERE series_id=? AND status='wanted' AND volume_num IS NOT NULL",
            (sid,)
        ).fetchall()
    if not s:
        return RedirectResponse("/wanted", status_code=303)
    for v in wanted:
        query = f"{s['search_pattern']} volume {vol_num_to_display(v['volume_num'])}"
        _m.create_background_task(_grab_volume_task(sid, dict(s), dict(v), query), name=f"series:{sid}:grab_volume:{v['id']}")
    if request.headers.get("HX-Request") == "true":
        return _Resp(headers={"HX-Trigger": json.dumps({
            "showToast": {"msg": f"Search queued for {len(wanted)} volumes", "type": "success"}
        })})
    return RedirectResponse(f"/series/{sid}", status_code=303)
