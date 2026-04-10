"""Delay Profiles — per-protocol grab delays with tag-based assignment (Sonarr parity)."""
from datetime import datetime
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from routers._templates import templates

from shared import get_db

router = APIRouter()


def _all_profiles(db):
    profiles = db.execute("SELECT * FROM delay_profiles ORDER BY order_num, id").fetchall()
    result = []
    for p in profiles:
        tags = db.execute(
            "SELECT tag FROM delay_profile_tags WHERE profile_id=?", (p['id'],)
        ).fetchall()
        result.append({**dict(p), 'tags': [t['tag'] for t in tags]})
    return result


# ── List ──────────────────────────────────────────────────────────────────────
@router.get("/delay-profiles", response_class=HTMLResponse)
async def delay_profiles_page(request: Request):
    with get_db() as db:
        profiles = _all_profiles(db)
        all_tags = [r['tag'] for r in db.execute("SELECT DISTINCT tag FROM series_tags ORDER BY tag").fetchall()]
    return templates.TemplateResponse(request, "delay_profiles.html", {
        "profiles": profiles,
        "all_tags": all_tags,
    })


# ── Create ────────────────────────────────────────────────────────────────────
@router.post("/delay-profiles")
async def create_delay_profile(
    name: str = Form(""),
    enable_usenet: int = Form(1),
    enable_torrent: int = Form(1),
    usenet_delay: int = Form(0),
    torrent_delay: int = Form(0),
    bypass_if_highest_quality: int = Form(0),
    tags: str = Form(""),
):
    with get_db() as db:
        max_order = db.execute("SELECT COALESCE(MAX(order_num),0) FROM delay_profiles").fetchone()[0]
        cur = db.execute(
            "INSERT INTO delay_profiles(name,enable_usenet,enable_torrent,usenet_delay,"
            " torrent_delay,bypass_if_highest_quality,order_num) VALUES(?,?,?,?,?,?,?)",
            (name.strip() or "Custom", enable_usenet, enable_torrent, usenet_delay,
             torrent_delay, bypass_if_highest_quality, max_order + 1)
        )
        pid = cur.lastrowid
        for tag in [t.strip() for t in tags.split(',') if t.strip()]:
            db.execute("INSERT OR IGNORE INTO delay_profile_tags(profile_id,tag) VALUES(?,?)", (pid, tag))
    return RedirectResponse("/delay-profiles", status_code=303)


# ── Edit ─────────────────────────────────────────────────────────────────────
@router.post("/delay-profiles/{profile_id}")
async def edit_delay_profile(
    profile_id: int,
    name: str = Form(""),
    enable_usenet: int = Form(1),
    enable_torrent: int = Form(1),
    usenet_delay: int = Form(0),
    torrent_delay: int = Form(0),
    bypass_if_highest_quality: int = Form(0),
    tags: str = Form(""),
):
    with get_db() as db:
        db.execute(
            "UPDATE delay_profiles SET name=?,enable_usenet=?,enable_torrent=?,"
            " usenet_delay=?,torrent_delay=?,bypass_if_highest_quality=? WHERE id=?",
            (name.strip() or "Custom", enable_usenet, enable_torrent, usenet_delay,
             torrent_delay, bypass_if_highest_quality, profile_id)
        )
        db.execute("DELETE FROM delay_profile_tags WHERE profile_id=?", (profile_id,))
        for tag in [t.strip() for t in tags.split(',') if t.strip()]:
            db.execute("INSERT OR IGNORE INTO delay_profile_tags(profile_id,tag) VALUES(?,?)",
                       (profile_id, tag))
    return RedirectResponse("/delay-profiles", status_code=303)


# ── Reorder ───────────────────────────────────────────────────────────────────
@router.post("/delay-profiles/reorder")
async def reorder_delay_profiles(request: Request):
    """Body: {"order": [id1, id2, ...]}"""
    body = await request.json()
    ids = body.get("order", [])
    with get_db() as db:
        for i, pid in enumerate(ids):
            db.execute("UPDATE delay_profiles SET order_num=? WHERE id=?", (i, pid))
    return JSONResponse({"ok": True})


# ── Delete ────────────────────────────────────────────────────────────────────
@router.post("/delay-profiles/{profile_id}/delete")
async def delete_delay_profile(profile_id: int):
    with get_db() as db:
        p = db.execute("SELECT * FROM delay_profiles WHERE id=?", (profile_id,)).fetchone()
        if p and p['is_default']:
            return JSONResponse({"error": "Cannot delete the default delay profile"}, status_code=400)
        db.execute("DELETE FROM delay_profiles WHERE id=?", (profile_id,))
    return RedirectResponse("/delay-profiles", status_code=303)


# ── Helper: get effective delay for a series ──────────────────────────────────
def get_delay_for_series(db, series_id: int, protocol: str) -> int:
    """
    Return delay in minutes for a series + protocol.
    Checks delay profiles in order_num order; first matching tag wins.
    Falls back to the 'all' default profile (no tags).

    Returns -1 if the protocol is explicitly disabled by the matching profile
    (enable_torrent=0 or enable_usenet=0). Callers must treat -1 as "do not grab".
    """
    series_tags = {r['tag'] for r in db.execute(
        "SELECT tag FROM series_tags WHERE series_id=?", (series_id,)
    ).fetchall()}

    profiles = db.execute(
        "SELECT dp.*, GROUP_CONCAT(dpt.tag) as tag_list"
        " FROM delay_profiles dp"
        " LEFT JOIN delay_profile_tags dpt ON dpt.profile_id=dp.id"
        " GROUP BY dp.id ORDER BY dp.order_num"
    ).fetchall()

    col = 'usenet_delay' if protocol == 'nzb' else 'torrent_delay'
    enabled_col = 'enable_usenet' if protocol == 'nzb' else 'enable_torrent'

    default_delay = 0
    default_disabled = False
    for p in profiles:
        p_tags = set(filter(None, (p['tag_list'] or '').split(',')))
        if p_tags & series_tags:
            # Tagged profile matches this series — respect its enabled/disabled state
            if not p[enabled_col]:
                return -1  # protocol explicitly disabled for this series
            return p[col]
        if not p_tags:
            # Default profile (applies to all)
            if not p[enabled_col]:
                default_disabled = True
            else:
                default_delay = p[col]
                default_disabled = False
    return -1 if default_disabled else default_delay
