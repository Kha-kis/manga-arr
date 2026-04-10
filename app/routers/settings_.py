"""Settings pages and configuration management."""
import json
import logging
import secrets

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from routers._templates import templates
from shared import get_cfg, get_db, is_htmx

router = APIRouter()


def _reload_config():
    """Reload the in-memory config from DB. Delegates to main to keep CONFIG in sync."""
    import main as _m
    _m.load_config()


def _get_root_folders(db) -> list:
    return db.execute(
        "SELECT * FROM root_folders ORDER BY is_default DESC, label, path"
    ).fetchall()


# ── Settings pages ────────────────────────────────────────────────────────────

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: str = ""):
    from shared import CONFIG
    with get_db() as db:
        root_folders = _get_root_folders(db)
    return templates.TemplateResponse(request, "settings.html", {
        "cfg": CONFIG, "saved": saved, "root_folders": root_folders,
    })


@router.post("/settings")
async def save_settings(
    request:              Request,
    save_path:            str = Form(""),
    category:             str = Form(""),
    rss_interval:         str = Form(""),
    komga_url:            str = Form(""),
    komga_user:           str = Form(""),
    komga_pass:           str = Form(""),
    komga_library_id:     str = Form(""),
    komga_scan_enabled:   str = Form(""),
    ignored_words:        str = Form(""),
    preferred_words:      str = Form(""),
    required_words:       str = Form(""),
    preferred_groups:     str = Form(""),
    blocked_groups:       str = Form(""),
    import_mode:          str = Form("hardlink"),
    remove_completed:     str = Form("false"),
    grab_delay_minutes:   str = Form("0"),
    file_format:          str = Form(""),
    chapter_format:       str = Form(""),
    folder_format:        str = Form(""),
    quality_cutoff:       str = Form(""),
    google_books_api_key:    str = Form(""),
    ddl_language:            str = Form("en"),
    ddl_grab_mode:           str = Form("fallback"),
    suwayomi_check_interval: str = Form(""),
):
    fields = {
        'save_path':          save_path,
        'category':           category,
        'import_mode':        import_mode if import_mode in ('hardlink', 'move', 'copy') else 'hardlink',
        'remove_completed':   'true' if remove_completed == 'true' else 'false',
        'grab_delay_minutes': str(max(0, min(10080, int(grab_delay_minutes)))) if grab_delay_minutes.isdigit() else '0',
        'file_format':        file_format.strip(),
        'chapter_format':     chapter_format.strip(),
        'folder_format':      folder_format.strip(),
        'quality_cutoff':     quality_cutoff.strip(),
        'komga_scan_enabled': 'true' if komga_scan_enabled else 'false',
        'ignored_words':      ignored_words,
        'preferred_words':    preferred_words,
        'required_words':     required_words,
        'preferred_groups':   preferred_groups,
        'blocked_groups':     blocked_groups,
        'ddl_language':       ddl_language if ddl_language else 'en',
        'ddl_grab_mode':      ddl_grab_mode if ddl_grab_mode in ('fallback', 'prefer', 'only', 'off') else 'fallback',
    }
    if suwayomi_check_interval and suwayomi_check_interval.isdigit():
        fields['suwayomi_check_interval'] = str(max(3600, min(604800, int(suwayomi_check_interval))))
    if komga_url:
        fields['komga_url'] = komga_url
    if komga_user:
        fields['komga_user'] = komga_user
    if komga_pass:
        fields['komga_pass'] = komga_pass
    if komga_library_id:
        fields['komga_library_id'] = komga_library_id
    if rss_interval and rss_interval.isdigit():
        fields['rss_interval'] = str(max(60, min(86400, int(rss_interval))))
    if google_books_api_key.strip():
        fields['google_books_api_key'] = google_books_api_key.strip()

    with get_db() as db:
        for k, v in fields.items():
            if v:
                db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)", (k, v))
    _reload_config()
    if is_htmx(request):
        return Response(headers={"HX-Trigger": json.dumps({"showToast": {"msg": "Settings saved", "type": "success"}})})
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.get("/settings/general", response_class=HTMLResponse)
async def settings_general_page(request: Request, saved: str = ""):
    with get_db() as db:
        cfg = {row['key']: row['value'] for row in db.execute("SELECT key, value FROM settings")}
    return templates.TemplateResponse(request, "settings_general.html", {
        "cfg": cfg, "saved": saved,
    })


@router.post("/settings/general")
async def save_general_settings(
    request:              Request,
    instance_name:        str = Form(""),
    log_level:            str = Form("INFO"),
    backup_folder:        str = Form("/config/backups/"),
    backup_interval_days: str = Form("7"),
    backup_retention:     str = Form("10"),
    ui_date_format:       str = Form("relative"),
    blocklist_ttl_days:   str = Form("90"),
    api_key:              str = Form(""),
):
    with get_db() as db:
        for k, v in {
            'instance_name':        instance_name,
            'log_level':            log_level,
            'backup_folder':        backup_folder,
            'backup_interval_days': backup_interval_days,
            'backup_retention':     backup_retention,
            'ui_date_format':       ui_date_format,
            'blocklist_ttl_days':   str(max(0, int(blocklist_ttl_days or '90'))),
        }.items():
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, v))
        if api_key.strip():
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('api_key',?)", (api_key.strip(),))
    _reload_config()
    logging.getLogger().setLevel(getattr(logging, log_level.upper(), logging.INFO))
    if is_htmx(request):
        return Response(headers={"HX-Trigger": json.dumps({"showToast": {"msg": "Settings saved", "type": "success"}})})
    return RedirectResponse("/settings/general?saved=1", status_code=303)


@router.post("/settings/extra")
async def save_extra_settings(
    grab_delay_minutes: str = Form("0"),
    file_format:        str = Form(""),
    chapter_format:     str = Form(""),
    folder_format:      str = Form(""),
):
    fields = {
        'grab_delay_minutes': grab_delay_minutes if grab_delay_minutes.isdigit() else '0',
        'file_format':        file_format.strip(),
        'chapter_format':     chapter_format.strip(),
        'folder_format':      folder_format.strip(),
    }
    with get_db() as db:
        for k, v in fields.items():
            db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)", (k, v))
    _reload_config()
    return RedirectResponse("/settings?saved=1", status_code=303)


# ── Root folder management ────────────────────────────────────────────────────

@router.post("/settings/root-folders/add")
async def add_root_folder(
    path:       str = Form(...),
    label:      str = Form(""),
    is_default: str = Form(""),
):
    path = path.strip().rstrip('/')
    if path:
        with get_db() as db:
            if is_default:
                db.execute("UPDATE root_folders SET is_default=0")
            db.execute(
                "INSERT OR IGNORE INTO root_folders(path, label, is_default) VALUES(?,?,?)",
                (path, label.strip() or None, 1 if is_default else 0)
            )
            count = db.execute("SELECT COUNT(*) FROM root_folders").fetchone()[0]
            if count == 1:
                db.execute("UPDATE root_folders SET is_default=1")
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/root-folders/{folder_id}/delete")
async def delete_root_folder(folder_id: int):
    with get_db() as db:
        db.execute("DELETE FROM root_folders WHERE id=?", (folder_id,))
        has_default = db.execute("SELECT 1 FROM root_folders WHERE is_default=1").fetchone()
        if not has_default:
            db.execute(
                "UPDATE root_folders SET is_default=1 WHERE id=(SELECT id FROM root_folders LIMIT 1)"
            )
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/root-folders/{folder_id}/default")
async def set_default_root_folder(folder_id: int):
    with get_db() as db:
        db.execute("UPDATE root_folders SET is_default=0")
        db.execute("UPDATE root_folders SET is_default=1 WHERE id=?", (folder_id,))
    return RedirectResponse("/settings?saved=1", status_code=303)


# ── Settings-adjacent API endpoints ──────────────────────────────────────────

@router.post("/api/system/regenerate-api-key")
async def regenerate_api_key():
    new_key = secrets.token_hex(32)
    with get_db() as db:
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('api_key',?)", (new_key,))
    _reload_config()
    return JSONResponse({"ok": True, "api_key": new_key})


@router.post("/api/test/komga")
async def test_komga(url: str = Form(""), user: str = Form(""), pw: str = Form("")):
    u  = url  or get_cfg('komga_url')
    us = user or get_cfg('komga_user')
    p  = pw   or get_cfg('komga_pass')
    if not u:
        return JSONResponse({"ok": False, "message": "No URL configured"})
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(f"{u}/api/v1/libraries", auth=(us, p) if us else None)
        if r.status_code == 401:
            return JSONResponse({"ok": False, "message": "Authentication failed — check credentials"})
        if r.status_code == 200:
            libs  = r.json()
            names = [lib['name'] for lib in libs]
            return JSONResponse({
                "ok": True,
                "message": f"Connected · {len(libs)} librar{'ies' if len(libs)!=1 else 'y'}: {', '.join(names[:4]) or 'none'}"
            })
        return JSONResponse({"ok": False, "message": f"HTTP {r.status_code} — check URL"})
    except Exception as e:
        return JSONResponse({"ok": False, "message": f"Connection failed: {e}"})


@router.get("/api/tags")
async def list_all_tags():
    with get_db() as db:
        rows = db.execute("SELECT tags FROM series WHERE tags IS NOT NULL").fetchall()
    tags: set[str] = set()
    for r in rows:
        try:
            tags.update(json.loads(r['tags']))
        except Exception:
            pass
    return JSONResponse({"tags": sorted(tags)})
