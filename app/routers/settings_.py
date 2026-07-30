"""Settings pages and configuration management."""

import json
import logging
import os
import secrets
import sqlite3
from collections.abc import Callable
from typing import cast

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.datastructures import FormData

from routers._templates import templates
from shared import get_cfg, get_db, get_secret_health_summary, is_htmx
from security import (
    validate_outbound_url,
    UnsafeURLError,
    decrypt_secret_safe,
    encrypt_if_cipher_available,
)
from config import SETTINGS_SECRET_KEYS, normalize_url_base


def _encrypt_settings_secrets_in_place(
    fields: dict[str, str],
) -> dict[str, str]:
    """Return a copy of `fields` with any keys in SETTINGS_SECRET_KEYS
    encrypted. Plaintext fall-through when the cipher is unavailable;
    the next migration_encrypt_settings_secrets() boot picks them up.
    """
    out = dict(fields)
    for k in list(out):
        if k in SETTINGS_SECRET_KEYS:
            out[k] = cast(str, encrypt_if_cipher_available(out[k]))
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


def _coerce_bool_string(value, true_value: str = "true", false_value: str = "false"):
    return true_value if str(value or "").strip().lower() in (
        "1",
        "true",
        "on",
        "yes",
    ) else false_value


def _coerce_int_range(value, default: int, minimum: int, maximum: int) -> str:
    raw = str(value or "").strip()
    parsed = int(raw) if raw.lstrip("-").isdigit() else default
    return str(max(minimum, min(maximum, parsed)))


GENERAL_SETTING_COERCERS = {
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
    "blocklist_ttl_days": lambda v: _coerce_int_range(v, 90, 0, 10000000),
    "recycle_bin_retention_days": lambda v: _coerce_int_range(v, 30, 1, 365),
    "recycle_bin_remove_files": lambda v: _coerce_bool_string(v, "1", "0"),
}


MEDIA_MANAGEMENT_SETTING_COERCERS = {
    "torrent_save_path": lambda v: str(v or "").strip(),
    "import_mode": lambda v: (
        str(v or "hardlink")
        if str(v or "hardlink") in ("hardlink", "move", "copy")
        else "hardlink"
    ),
    "remove_completed": lambda v: _coerce_bool_string(v),
    "minimum_free_space_mb": lambda v: _coerce_int_range(v, 0, 0, 10000000),
    "file_format": lambda v: str(v or "").strip(),
    "chapter_format": lambda v: str(v or "").strip(),
    "folder_format": lambda v: str(v or "").strip(),
    "quality_cutoff": lambda v: str(v or "").strip(),
    "propers_and_repacks": lambda v: (
        str(v or "prefer_and_upgrade")
        if str(v or "prefer_and_upgrade")
        in ("prefer_and_upgrade", "do_not_upgrade", "do_not_prefer")
        else "prefer_and_upgrade"
    ),
}


INDEXER_SETTING_COERCERS = {
    "rss_interval": lambda v: _coerce_int_range(v, 900, 60, 86400),
}


METADATA_SETTING_COERCERS = {
    "refresh_interval": lambda v: _coerce_int_range(v, 86400, 60, 86400 * 30),
}


def _coerce_decimal_int_range(
    value: object,
    *,
    minimum: int,
    maximum: int,
) -> str | None:
    """Clamp a decimal form value, or skip it when it is not an integer."""
    raw = str(value or "").strip()
    if not raw.isdigit():
        return None
    return str(max(minimum, min(maximum, int(raw))))


def _coerce_choice(value: object, *, default: str, choices: frozenset[str]) -> str:
    raw = str(value or default)
    return raw if raw in choices else default


_IMPORT_MODES = frozenset({"hardlink", "move", "copy"})
_PROPERS_AND_REPACKS_MODES = frozenset(
    {"prefer_and_upgrade", "do_not_upgrade", "do_not_prefer"}
)
_DDL_GRAB_MODES = frozenset({"fallback", "prefer", "only", "off"})
# Exact values offered by the Preferred Language select in settings.html.
_DDL_LANGUAGES = frozenset(
    {
        "en",
        "ja",
        "zh-hant",
        "zh-hans",
        "ko",
        "fr",
        "de",
        "es",
        "pt-br",
        "it",
        "pl",
        "ru",
    }
)
# Match config.SETTINGS_VALIDATORS so invalid writes use its documented default.
_QUALITY_CUTOFFS = frozenset(
    {"", "pdf", "epub", "cbr", "cbz", "rar", "zip", "mobi"}
)

_SettingsFormCoercer = Callable[[object], str | None]

_SETTINGS_FORM_COERCERS: dict[str, _SettingsFormCoercer] = {
    # Media Management
    "category": lambda v: str(v or ""),
    "min_seeders": lambda v: _coerce_int_range(v, 0, 0, 10000000),
    "torrent_save_path": lambda v: str(v or "").strip(),
    "import_mode": lambda v: _coerce_choice(
        v,
        default="hardlink",
        choices=_IMPORT_MODES,
    ),
    "remove_completed": _coerce_bool_string,
    "minimum_free_space_mb": lambda v: _coerce_int_range(v, 0, 0, 10000000),
    "propers_and_repacks": lambda v: _coerce_choice(
        v,
        default="prefer_and_upgrade",
        choices=_PROPERS_AND_REPACKS_MODES,
    ),
    "file_format": lambda v: str(v or "").strip(),
    "chapter_format": lambda v: str(v or "").strip(),
    "folder_format": lambda v: str(v or "").strip(),
    # Direct Download
    "ddl_grab_mode": lambda v: _coerce_choice(
        v,
        default="fallback",
        choices=_DDL_GRAB_MODES,
    ),
    "ddl_language": lambda v: _coerce_choice(
        v,
        default="en",
        choices=_DDL_LANGUAGES,
    ),
    "suwayomi_check_interval": lambda v: _coerce_decimal_int_range(
        v,
        minimum=3600,
        maximum=604800,
    ),
    # General
    "rss_interval": lambda v: _coerce_decimal_int_range(
        v,
        minimum=60,
        maximum=86400,
    ),
    "grab_delay_minutes": lambda v: _coerce_int_range(v, 0, 0, 10080),
    "quality_cutoff": lambda v: _coerce_choice(
        str(v or "").strip(),
        default="",
        choices=_QUALITY_CUTOFFS,
    ),
    "ignored_words": lambda v: str(v or ""),
    "preferred_words": lambda v: str(v or ""),
    "required_words": lambda v: str(v or ""),
    "preferred_groups": lambda v: str(v or ""),
    "blocked_groups": lambda v: str(v or ""),
    # Metadata
    "google_books_api_key": lambda v: str(v or "").strip(),
    "komga_url": lambda v: str(v or ""),
    "komga_user": lambda v: str(v or ""),
    "komga_pass": lambda v: str(v or ""),
    "komga_library_id": lambda v: str(v or ""),
    "komga_scan_enabled": _coerce_bool_string,
}

_SETTINGS_FORM_CLEARABLE_KEYS = frozenset(
    {
        "torrent_save_path",
        "file_format",
        "chapter_format",
        "folder_format",
        "quality_cutoff",
        "ignored_words",
        "preferred_words",
        "required_words",
        "preferred_groups",
        "blocked_groups",
        "komga_url",
        "komga_library_id",
    }
)


def _last_form_value(form: FormData, key: str) -> object:
    """Return the last value so hidden-input-first checkboxes work."""
    values = form.getlist(key)
    return values[-1] if values else ""


def _submitted_settings_fields(form: FormData) -> dict[str, str]:
    """Coerce only settings keys actually present in the submitted form."""
    fields: dict[str, str] = {}
    for key, coerce in _SETTINGS_FORM_COERCERS.items():
        if key not in form:
            continue
        raw_value = _last_form_value(form, key)
        if key in SETTINGS_SECRET_KEYS and not str(raw_value or "").strip():
            # Password-like blanks mean "keep the stored credential".
            continue
        value = coerce(raw_value)
        if value is None:
            continue
        if value or key in _SETTINGS_FORM_CLEARABLE_KEYS:
            fields[key] = value
    return _encrypt_settings_secrets_in_place(fields)


def _write_settings_fields(fields: dict[str, str]) -> None:
    with get_db() as db:
        for key, value in fields.items():
            db.execute(
                "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                (key, value),
            )
    _reload_config()


def update_general_settings_entries(raw_fields: dict) -> dict[str, str]:
    fields = {
        key: GENERAL_SETTING_COERCERS[key](value)
        for key, value in raw_fields.items()
        if key in GENERAL_SETTING_COERCERS
    }
    _write_settings_fields(fields)
    if "log_level" in fields:
        logging.getLogger().setLevel(
            getattr(logging, fields["log_level"].upper(), logging.INFO)
        )
    return fields


def update_media_management_settings_entries(raw_fields: dict) -> dict[str, str]:
    fields = {
        key: MEDIA_MANAGEMENT_SETTING_COERCERS[key](value)
        for key, value in raw_fields.items()
        if key in MEDIA_MANAGEMENT_SETTING_COERCERS
    }
    _write_settings_fields(fields)
    return fields


def update_indexer_settings_entries(raw_fields: dict) -> dict[str, str]:
    fields = {
        key: INDEXER_SETTING_COERCERS[key](value)
        for key, value in raw_fields.items()
        if key in INDEXER_SETTING_COERCERS
    }
    _write_settings_fields(fields)
    return fields


def update_metadata_settings_entries(raw_fields: dict) -> dict[str, str]:
    fields = {
        key: METADATA_SETTING_COERCERS[key](value)
        for key, value in raw_fields.items()
        if key in METADATA_SETTING_COERCERS
    }
    _write_settings_fields(fields)
    return fields


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
async def save_settings(request: Request) -> Response:
    """Persist only settings keys present in this form submission."""
    form = await request.form()
    _write_settings_fields(_submitted_settings_fields(form))
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
        cfg = {}
        for row in db.execute("SELECT key, value FROM settings"):
            key = row["key"]
            value = row["value"]
            if key in SETTINGS_SECRET_KEYS:
                value = decrypt_secret_safe(
                    value,
                    field_name=f"settings.{key}",
                    context="General Settings",
                )
            cfg[key] = value
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


def update_root_folder_entry(
    folder_id: int,
    *,
    path: str | None = None,
    label: str | None = None,
    is_default: bool | None = None,
) -> dict:
    """Update a root folder row and keep one remaining row defaulted."""
    with get_db() as db:
        existing = db.execute(
            "SELECT * FROM root_folders WHERE id=?",
            (folder_id,),
        ).fetchone()
        if not existing:
            return {"ok": False, "status": "not_found"}

        fields: list[str] = []
        params: list = []
        if path is not None:
            path = str(path or "").strip().rstrip("/")
            if not path:
                return {"ok": False, "status": "invalid_path"}
            fields.append("path=?")
            params.append(path)
        if label is not None:
            fields.append("label=?")
            params.append(str(label or "").strip() or None)

        if fields:
            params.append(folder_id)
            try:
                db.execute(
                    f"UPDATE root_folders SET {', '.join(fields)} WHERE id=?",
                    params,
                )
            except sqlite3.IntegrityError:
                return {"ok": False, "status": "duplicate_path"}

        if is_default is True:
            db.execute("UPDATE root_folders SET is_default=0")
            db.execute(
                "UPDATE root_folders SET is_default=1 WHERE id=?",
                (folder_id,),
            )
        elif is_default is False:
            db.execute(
                "UPDATE root_folders SET is_default=0 WHERE id=?",
                (folder_id,),
            )
        has_default = db.execute(
            "SELECT 1 FROM root_folders WHERE is_default=1"
        ).fetchone()
        if not has_default:
            fallback = db.execute(
                "SELECT id FROM root_folders WHERE id<>? ORDER BY id LIMIT 1",
                (folder_id,),
            ).fetchone()
            fallback_id = fallback["id"] if fallback else folder_id
            db.execute(
                "UPDATE root_folders SET is_default=1 WHERE id=?",
                (fallback_id,),
            )

        row = db.execute(
            "SELECT * FROM root_folders WHERE id=?",
            (folder_id,),
        ).fetchone()
    return {"ok": True, "status": "updated", "root_folder": dict(row)}


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
