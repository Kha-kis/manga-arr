"""History, activity, and logs pages."""
import csv
import io

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from routers._templates import templates
from shared import cascade_chapters, get_db

router = APIRouter()


@router.get("/activity")
async def activity_redirect():
    return RedirectResponse("/history", status_code=302)


@router.get("/history", response_class=HTMLResponse)
async def history_page(
    request:    Request,
    event_type: str = "",
    series_id:  int = 0,
    page:       int = 1,
    page_size:  int = 50,
    export:     str = "",
):
    conditions: list[str] = []
    params_f:   list      = []
    if event_type:
        conditions.append("event_type=?")
        params_f.append(event_type)
    if series_id:
        conditions.append("series_id=?")
        params_f.append(series_id)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    # CSV export — stream all matching rows
    if export == "csv":
        def _generate():
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["Date", "Series", "Volume", "Event Type",
                             "Indexer", "Protocol", "Client", "Release", "Size", "Release Group"])
            with get_db() as db:
                for row in db.execute(
                    f"SELECT * FROM history {where} ORDER BY created_at DESC", params_f
                ):
                    writer.writerow([
                        row['created_at'] or '',
                        row['series_title'] or '',
                        row['volume_label'] or '',
                        row['event_type'] or '',
                        row['indexer'] or '',
                        row['protocol'] or '',
                        row['client'] or '',
                        row['source_title'] or '',
                        row['size_bytes'] or '',
                        row['release_group'] or '',
                    ])
                    yield buf.getvalue()
                    buf.seek(0); buf.truncate(0)
        return StreamingResponse(
            _generate(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=mangarr-history.csv"},
        )

    offset = (page - 1) * page_size
    with get_db() as db:
        total = db.execute(
            f"SELECT COUNT(*) FROM history {where}", params_f
        ).fetchone()[0]
        rows = db.execute(
            f"SELECT * FROM history {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params_f + [page_size, offset]
        ).fetchall()
        series_list = db.execute("SELECT id, title FROM series ORDER BY title").fetchall()

    return templates.TemplateResponse(request, "history.html", {
        "events":        rows,
        "page":          page,
        "page_size":     page_size,
        "total":         total,
        "pages":         (total + page_size - 1) // page_size,
        "filter_type":   event_type,
        "filter_series": series_id,
        "series_list":   series_list,
    })


@router.post("/history/{hist_id}/mark-failed")
async def history_mark_failed(request: Request, hist_id: int):
    """Mark a grabbed entry as failed and add to blocklist."""
    with get_db() as db:
        h = db.execute("SELECT * FROM history WHERE id=?", (hist_id,)).fetchone()
        if h and h['event_type'] == 'grabbed':
            bl_url = (h['torrent_url'] if 'torrent_url' in h.keys() and h['torrent_url']
                      else h['download_id'] or h['source_title'] or '')
            db.execute(
                "INSERT OR IGNORE INTO blocklist"
                "(series_id, torrent_url, torrent_name, reason, indexer, protocol, size_bytes)"
                " VALUES(?,?,?,?,?,?,?)",
                (h['series_id'], bl_url, h['source_title'] or '',
                 'Marked failed via history',
                 h['indexer'], h['protocol'], h['size_bytes'])
            )
            db.execute("UPDATE history SET event_type='grab_failed' WHERE id=?", (hist_id,))
            if h['download_id']:
                grabbed = db.execute(
                    "SELECT id FROM volumes WHERE series_id=? AND download_id=?"
                    " AND status='grabbed' AND volume_num IS NOT NULL",
                    (h['series_id'], h['download_id'])
                ).fetchall()
                vol_ids = [r['id'] for r in grabbed]
                db.execute(
                    "DELETE FROM volumes WHERE series_id=? AND download_id=?"
                    " AND status='grabbed' AND volume_num IS NULL",
                    (h['series_id'], h['download_id'])
                )
                db.execute(
                    "UPDATE volumes SET status='wanted', download_id=NULL, grabbed_at=NULL,"
                    " source_url=NULL, torrent_name=NULL, indexer=NULL, protocol=NULL,"
                    " client=NULL, release_group=NULL "
                    "WHERE series_id=? AND download_id=? AND status='grabbed'",
                    (h['series_id'], h['download_id'])
                )
                if vol_ids:
                    cascade_chapters(db, h['series_id'], vol_ids, 'wanted',
                                     grabbed_at=None, torrent_name=None, torrent_url=None,
                                     indexer=None, protocol=None, client=None,
                                     download_id=None, release_group=None)
                db.execute("DELETE FROM seen WHERE download_id=?", (h['download_id'],))
    if request.headers.get("HX-Request") == "true":
        import json
        from fastapi.responses import Response as _Resp
        return _Resp(headers={"HX-Trigger": json.dumps({
            "showToast": {"msg": "Marked failed and added to blocklist", "type": "success"}
        }), "HX-Refresh": "true"})
    return RedirectResponse("/history", status_code=303)


@router.post("/history/{hist_id}/delete")
async def history_delete(request: Request, hist_id: int):
    """Delete a single history entry."""
    with get_db() as db:
        db.execute("DELETE FROM history WHERE id=?", (hist_id,))
    if request.headers.get("HX-Request") == "true":
        from fastapi.responses import Response as _Resp
        return _Resp(headers={"HX-Refresh": "true"})
    return RedirectResponse("/history", status_code=303)


@router.post("/history/clear-failed")
async def history_clear_failed(request: Request):
    """Delete all import_failed and grab_failed history entries."""
    with get_db() as db:
        db.execute("DELETE FROM history WHERE event_type IN ('import_failed','grab_failed')")
    if request.headers.get("HX-Request") == "true":
        import json
        from fastapi.responses import Response as _Resp
        return _Resp(headers={"HX-Trigger": json.dumps({
            "showToast": {"msg": "Failed history cleared", "type": "success"}
        }), "HX-Refresh": "true"})
    return RedirectResponse("/history", status_code=303)


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request, page: int = 1, event_type: str = ""):
    per_page = 100
    offset   = (page - 1) * per_page
    _VALID_TYPES = {'grab', 'imported', 'error', 'rss_poll', 'backlog_search',
                    'refresh', 'import_list_sync', 'backup', 'series_added',
                    'series_deleted', 'grab_failed', 'import_failed', 'info'}
    et = event_type if event_type in _VALID_TYPES else ""
    with get_db() as db:
        if et:
            total = db.execute("SELECT COUNT(*) FROM events WHERE event_type=?", (et,)).fetchone()[0]
            rows  = db.execute(
                "SELECT e.*, s.title as series_title FROM events e "
                "LEFT JOIN series s ON s.id = e.series_id "
                "WHERE e.event_type=? ORDER BY e.created_at DESC LIMIT ? OFFSET ?",
                (et, per_page, offset)
            ).fetchall()
        else:
            total = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            rows  = db.execute(
                "SELECT e.*, s.title as series_title FROM events e "
                "LEFT JOIN series s ON s.id = e.series_id "
                "ORDER BY e.created_at DESC LIMIT ? OFFSET ?",
                (per_page, offset)
            ).fetchall()
        event_types = [r[0] for r in db.execute(
            "SELECT DISTINCT event_type FROM events ORDER BY event_type"
        ).fetchall()]
    return templates.TemplateResponse(request, "logs.html", {
        "events": rows, "page": page, "total": total,
        "per_page": per_page, "pages": (total + per_page - 1) // per_page,
        "event_type": et, "event_types": event_types,
    })
