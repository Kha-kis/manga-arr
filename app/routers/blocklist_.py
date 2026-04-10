"""Blocklist — manual and automatic release blocking."""
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from routers._templates import templates
from shared import get_db, get_cfg

router = APIRouter()


@router.get("/blocklist", response_class=HTMLResponse)
async def blocklist_page(request: Request):
    ttl_days = max(0, int(get_cfg('blocklist_ttl_days', '90') or '90'))
    with get_db() as db:
        raw_rows = db.execute(
            "SELECT bl.*, s.title as series_title FROM blocklist bl "
            "LEFT JOIN series s ON s.id=bl.series_id "
            "ORDER BY bl.added_at DESC"
        ).fetchall()

    rows = []
    for r in raw_rows:
        row = dict(r)
        if ttl_days > 0 and r['added_at']:
            try:
                added = datetime.fromisoformat(r['added_at'].replace('Z', '+00:00'))
                if added.tzinfo is None:
                    added = added.replace(tzinfo=timezone.utc)
                row['expires_at'] = (added + timedelta(days=ttl_days)).isoformat()
            except Exception:
                row['expires_at'] = None
        else:
            row['expires_at'] = None
        rows.append(row)

    return templates.TemplateResponse(request, "blocklist.html", {
        "rows": rows,
        "blocklist_ttl_days": ttl_days,
    })


@router.post("/blocklist/{bl_id}/delete")
async def delete_blocklist_entry(request: Request, bl_id: int):
    with get_db() as db:
        db.execute("DELETE FROM blocklist WHERE id=?", (bl_id,))
    if request.headers.get("HX-Request") == "true":
        from fastapi.responses import Response as _Resp
        return _Resp(headers={"HX-Refresh": "true"})
    return RedirectResponse("/blocklist", status_code=303)


@router.post("/blocklist/clear-all")
async def clear_all_blocklist(request: Request):
    with get_db() as db:
        db.execute("DELETE FROM blocklist")
    if request.headers.get("HX-Request") == "true":
        import json
        from fastapi.responses import Response as _Resp
        return _Resp(headers={"HX-Trigger": json.dumps({
            "showToast": {"msg": "Blocklist cleared", "type": "success"}
        }), "HX-Refresh": "true"})
    return RedirectResponse("/blocklist", status_code=303)


@router.post("/blocklist/add")
async def add_to_blocklist(
    series_id:    int = Form(0),
    torrent_url:  str = Form(""),
    torrent_name: str = Form(""),
    reason:       str = Form(""),
):
    if torrent_url:
        with get_db() as db:
            db.execute(
                "INSERT OR IGNORE INTO blocklist(series_id, torrent_url, torrent_name, reason)"
                " VALUES(?,?,?,?)",
                (series_id or None, torrent_url, torrent_name, reason or 'Manual')
            )
    return RedirectResponse(f"/series/{series_id}" if series_id else "/", status_code=303)
