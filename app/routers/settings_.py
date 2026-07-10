"""Settings pages and configuration management."""

import json
import logging
import secrets
import os

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from routers._templates import templates
from shared import get_cfg, get_db, get_secret_health_summary, is_htmx
from security import (
    validate_outbound_url,
    UnsafeURLError,
    encrypt_if_cipher_available,
)
from config import normalize_url_base


def _encrypt_settings_secrets_in_place(fields: dict) -> dict:
    """Return a copy of `fields` with any keys in SETTINGS_SECRET_KEYS
    encrypted. Plaintext fall-through when the cipher is unavailable;
    the next migration_encrypt_settings_secrets() boot picks them up.
    """
    from config import SETTINGS_SECRET_KEYS

    out = dict(fields)
    for k in list(out):
        if k in SETTINGS_SECRET_KEYS:
            out[k] = encrypt_if_cipher_available(out[k])
    return out


router = APIRouter()


def _reload_config():
    """Reload the in-memory config from DB. Delegates to main to keep CONFIG in sync."""
    import main as _m

    _m.load_config()


def _get_root_folders(db) -> list:
    return db.execute(
        "SELECT * FROM root_folders ORDER BY is_default DESC, label, path"
    ).fetchall()


def _is_first_run(db) -> bool:
    for table in ("series", "indexers", "download_clients", "notification_connections"):
        if db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
            return False
    return True


# ── Settings pages ────────────────────────────────────────────────────────────


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: str = ""):
    from shared import CONFIG

    with get_db() as db:
        root_folders = _get_root_folders(db)
        quality_profiles = db.execute(
            "SELECT id, name, is_default FROM quality_profiles ORDER BY is_default DESC, name"
        ).fetchall()
        language_profiles = db.execute(
            "SELECT id, name FROM language_profiles ORDER BY name"
        ).fetchall()
        secret_health = get_secret_health_summary(db)
        first_run = _is_first_run(db)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "cfg": CONFIG,
            "saved": saved,
            "root_folders": root_folders,
            "quality_profiles": quality_profiles,
            "language_profiles": language_profiles,
            "secret_health": secret_health,
            "secret_key_source": "environment"
            if os.getenv("MANGARR_SECRET_KEY")
            else "file",
            "first_run": first_run,
        },
    )


@router.post("/settings")
async def save_settings(
    request: Request,
    torrent_save_path: str = Form(""),
    category: str = Form(""),
    rss_interval: str = Form(""),
    komga_url: str = Form(""),
    komga_user: str = Form(""),
    komga_pass: str = Form(""),
    komga_library_id: str = Form(""),
    komga_scan_enabled: str = Form(""),
    ignored_words: str = Form(""),
    preferred_words: str = Form(""),
    required_words: str = Form(""),
    preferred_groups: str = Form(""),
    blocked_groups: str = Form(""),
    import_mode: str = Form("hardlink"),
    remove_completed: str = Form("false"),
    minimum_free_space_mb: str = Form("0"),
    grab_delay_minutes: str = Form("0"),
    file_format: str = Form(""),
    chapter_format: str = Form(""),
    folder_format: str = Form(""),
    quality_cutoff: str = Form(""),
    google_books_api_key: str = Form(""),
    ddl_language: str = Form("en"),
    ddl_grab_mode: str = Form("fallback"),
    suwayomi_check_interval: str = Form(""),
):
    fields = {
        # Empty string means "fall back to save_path" — the old single-directory
        # behaviour. Strip whitespace so accidental spaces don't make it look
        # configured when it isn't.
        "torrent_save_path": torrent_save_path.strip(),
        "category": category,
        "import_mode": import_mode
        if import_mode in ("hardlink", "move", "copy")
        else "hardlink",
        "remove_completed": "true" if remove_completed == "true" else "false",
        "minimum_free_space_mb": str(
            max(
                0,
                min(
                    10000000,
                    int(minimum_free_space_mb)
                    if minimum_free_space_mb.isdigit()
                    else 0,
                ),
            )
        ),
        "grab_delay_minutes": str(max(0, min(10080, int(grab_delay_minutes))))
        if grab_delay_minutes.isdigit()
        else "0",
        "file_format": file_format.strip(),
        "chapter_format": chapter_format.strip(),
        "folder_format": folder_format.strip(),
        "quality_cutoff": quality_cutoff.strip(),
        "komga_scan_enabled": "true" if komga_scan_enabled else "false",
        "ignored_words": ignored_words,
        "preferred_words": preferred_words,
        "required_words": required_words,
        "preferred_groups": preferred_groups,
        "blocked_groups": blocked_groups,
        "ddl_language": ddl_language if ddl_language else "en",
        "ddl_grab_mode": ddl_grab_mode
        if ddl_grab_mode in ("fallback", "prefer", "only", "off")
        else "fallback",
    }
    if suwayomi_check_interval and suwayomi_check_interval.isdigit():
        fields["suwayomi_check_interval"] = str(
            max(3600, min(604800, int(suwayomi_check_interval)))
        )
    if komga_url:
        fields["komga_url"] = komga_url
    if komga_user:
        fields["komga_user"] = komga_user
    if komga_pass:
        fields["komga_pass"] = komga_pass
    if komga_library_id:
        fields["komga_library_id"] = komga_library_id
    if rss_interval and rss_interval.isdigit():
        fields["rss_interval"] = str(max(60, min(86400, int(rss_interval))))
    if google_books_api_key.strip():
        fields["google_books_api_key"] = google_books_api_key.strip()

    fields = _encrypt_settings_secrets_in_place(fields)
    # Some settings are legitimately clearable via the form — their
    # empty value is meaningful (e.g. torrent_save_path="" means "fall
    # back to save_path"). For those keys we persist the empty value;
    # for everything else we skip empties so a blank form field doesn't
    # wipe an existing DB row.
    _CLEARABLE_KEYS = {"torrent_save_path"}
    with get_db() as db:
        for k, v in fields.items():
            if v or k in _CLEARABLE_KEYS:
                db.execute(
                    "INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)", (k, v)
                )
    _reload_config()
    if is_htmx(request):
        return Response(
            headers={
                "HX-Trigger": json.dumps(
                    {"showToast": {"msg": "Settings saved", "type": "success"}}
                )
            }
        )
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.get("/settings/general", response_class=HTMLResponse)
async def settings_general_page(request: Request, saved: str = ""):
    with get_db() as db:
        cfg = {
            row["key"]: row["value"]
            for row in db.execute("SELECT key, value FROM settings")
        }
        secret_health = get_secret_health_summary(db)
        first_run = _is_first_run(db)
    return templates.TemplateResponse(
        request,
        "settings_general.html",
        {
            "cfg": cfg,
            "saved": saved,
            "secret_health": secret_health,
            "secret_key_source": "environment"
            if os.getenv("MANGARR_SECRET_KEY")
            else "file",
            "first_run": first_run,
        },
    )


@router.post("/settings/general")
async def save_general_settings(request: Request):
    """Save general settings. Partial-POST safe: only key/value rows
    whose form key is present in the request body are written. Each
    setting is its own row in the `settings` table, so partial POSTs
    naturally only touch what they submit (no row-level UPDATE to
    contaminate other columns)."""
    form = await request.form()

    # Per-key coercers — most are passthrough; blocklist_ttl_days and
    # backup_retention need numeric clamping to match prior behaviour.
    coercers = {
        "instance_name": lambda v: str(v or ""),
        "log_level": lambda v: (
            str(v or "INFO").strip().upper()
            if str(v or "INFO").strip().upper()
            in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
            else "INFO"
        ),
        "url_base": normalize_url_base,
        "backup_folder": lambda v: str(v or "/config/backups/"),
        "backup_interval_days": lambda v: str(v or "7"),
        "backup_retention": lambda v: str(v or "10"),
        "ui_date_format": lambda v: str(v or "relative").strip() or "relative",
        "blocklist_ttl_days": lambda v: str(
            max(
                0,
                int(
                    str(v or "90") if str(v or "").strip().lstrip("-").isdigit() else 90
                ),
            )
        ),
        # Recycle-bin retention: how many days a soft-deleted series sits
        # in /recycle-bin before the reaper hard-deletes it. Clamped 1–365.
        "recycle_bin_retention_days": lambda v: str(
            max(
                1,
                min(
                    365,
                    int(
                        str(v or "30")
                        if str(v or "").strip().lstrip("-").isdigit()
                        else 30
                    ),
                ),
            )
        ),
        # Reaper file deletion (PR-4). When set, the recycle-bin reaper
        # additionally removes volume files from disk (matches what the
        # explicit "Empty bin" / "Permanent delete" buttons always do).
        # Default off — opt-in, preserves pre-epic behaviour where
        # Mangarr never touched files on series delete.
        "recycle_bin_remove_files": lambda v: (
            "1" if str(v or "").strip().lower() in ("1", "true", "on", "yes") else "0"
        ),
    }
    with get_db() as db:
        for key, coerce in coercers.items():
            if key in form:
                db.execute(
                    "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                    (key, coerce(form[key])),
                )
        # api_key: only write if the form carries it AND it's non-empty
        if "api_key" in form:
            api_raw = str(form["api_key"] or "").strip()
            if api_raw:
                encrypted_api_key = encrypt_if_cipher_available(api_raw)
                db.execute(
                    "INSERT OR REPLACE INTO settings(key,value) VALUES('api_key',?)",
                    (encrypted_api_key,),
                )
    _reload_config()
    # Only reset the active log level if log_level was actually submitted
    if "log_level" in form:
        log_level = str(form["log_level"] or "INFO").strip().upper() or "INFO"
        logging.getLogger().setLevel(getattr(logging, log_level.upper(), logging.INFO))
    if is_htmx(request):
        return Response(
            headers={
                "HX-Trigger": json.dumps(
                    {"showToast": {"msg": "Settings saved", "type": "success"}}
                )
            }
        )
    return RedirectResponse("/settings/general?saved=1", status_code=303)


# ── Root folder management ────────────────────────────────────────────────────


def add_root_folder_entry(path: str, label: str = "", is_default: bool = False) -> dict:
    """Create a root folder row or return the existing row for the path."""
    path = str(path or "").strip().rstrip("/")
    if not path:
        return {"ok": False, "status": "invalid_path"}

    with get_db() as db:
        if is_default:
            db.execute("UPDATE root_folders SET is_default=0")
        cur = db.execute(
            "INSERT OR IGNORE INTO root_folders(path, label, is_default) VALUES(?,?,?)",
            (path, label.strip() or None, 1 if is_default else 0),
        )
        status = "created" if cur.rowcount else "exists"
        count = db.execute("SELECT COUNT(*) FROM root_folders").fetchone()[0]
        if count == 1:
            db.execute("UPDATE root_folders SET is_default=1")
        row = db.execute("SELECT * FROM root_folders WHERE path=?", (path,)).fetchone()
        if not row:
            return {"ok": False, "status": "not_found"}
        return {"ok": True, "status": status, "root_folder": dict(row)}


def delete_root_folder_entry(folder_id: int) -> dict:
    """Delete a root folder row and keep one remaining row defaulted."""
    with get_db() as db:
        existing = db.execute(
            "SELECT 1 FROM root_folders WHERE id=?",
            (folder_id,),
        ).fetchone()
        if not existing:
            return {"ok": False, "status": "not_found"}
        db.execute("DELETE FROM root_folders WHERE id=?", (folder_id,))
        has_default = db.execute(
            "SELECT 1 FROM root_folders WHERE is_default=1"
        ).fetchone()
        if not has_default:
            db.execute(
                "UPDATE root_folders SET is_default=1 "
                "WHERE id=(SELECT id FROM root_folders LIMIT 1)"
            )
    return {"ok": True, "status": "deleted"}


def set_default_root_folder_entry(folder_id: int) -> dict:
    """Make a root folder the default."""
    with get_db() as db:
        existing = db.execute(
            "SELECT 1 FROM root_folders WHERE id=?",
            (folder_id,),
        ).fetchone()
        if not existing:
            return {"ok": False, "status": "not_found"}
        db.execute("UPDATE root_folders SET is_default=0")
        db.execute("UPDATE root_folders SET is_default=1 WHERE id=?", (folder_id,))
        row = db.execute("SELECT * FROM root_folders WHERE id=?", (folder_id,)).fetchone()
    return {"ok": True, "status": "defaulted", "root_folder": dict(row)}


@router.post("/settings/root-folders/add")
async def add_root_folder(
    path: str = Form(...),
    label: str = Form(""),
    is_default: str = Form(""),
):
    add_root_folder_entry(path, label, bool(is_default))
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/root-folders/{folder_id}/delete")
async def delete_root_folder(folder_id: int):
    delete_root_folder_entry(folder_id)
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/root-folders/{folder_id}/default")
async def set_default_root_folder(folder_id: int):
    set_default_root_folder_entry(folder_id)
    return RedirectResponse("/settings?saved=1", status_code=303)


# ── Settings-adjacent API endpoints ──────────────────────────────────────────


@router.post("/api/system/regenerate-api-key")
async def regenerate_api_key():
    new_key = secrets.token_hex(32)
    # H4 PR #2: encrypt the stored value when the cipher is available.
    # The plaintext key is what we return to the caller (the UI shows it
    # once); only the at-rest copy is encrypted.
    stored_value = encrypt_if_cipher_available(new_key)
    with get_db() as db:
        db.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES('api_key',?)",
            (stored_value,),
        )
    _reload_config()
    return JSONResponse({"ok": True, "api_key": new_key})


@router.post("/api/test/komga")
async def test_komga(url: str = Form(""), user: str = Form(""), pw: str = Form("")):
    u = url or get_cfg("komga_url")
    us = user or get_cfg("komga_user")
    p = pw or get_cfg("komga_pass")
    if not u:
        return JSONResponse({"ok": False, "message": "No URL configured"})
    try:
        validate_outbound_url(u, allow_private=True)
    except UnsafeURLError as e:
        return JSONResponse({"ok": False, "message": f"URL rejected: {e}"})
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(f"{u}/api/v1/libraries", auth=(us, p) if us else None)
        if r.status_code == 401:
            return JSONResponse(
                {"ok": False, "message": "Authentication failed — check credentials"}
            )
        if r.status_code == 200:
            libs = r.json()
            names = [lib["name"] for lib in libs]
            return JSONResponse(
                {
                    "ok": True,
                    "message": f"Connected · {len(libs)} librar{'ies' if len(libs) != 1 else 'y'}: {', '.join(names[:4]) or 'none'}",
                }
            )
        return JSONResponse(
            {"ok": False, "message": f"HTTP {r.status_code} — check URL"}
        )
    except Exception as e:
        return JSONResponse({"ok": False, "message": f"Connection failed: {e}"})


@router.get("/api/tags")
async def list_all_tags():
    with get_db() as db:
        rows = db.execute(
            "SELECT tags FROM series WHERE tags IS NOT NULL AND deleted_at IS NULL"
        ).fetchall()
    tags: set[str] = set()
    for r in rows:
        try:
            tags.update(json.loads(r["tags"]))
        except Exception:
            pass
    return JSONResponse({"tags": sorted(tags)})
