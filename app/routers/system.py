"""System router — status, tasks, backup, and tags pages for Mangarr."""

import asyncio
import os
import platform
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)

from routers._templates import templates
from shared import DB_PATH, get_cfg, get_db

router = APIRouter()

# ── Module-level startup time ─────────────────────────────────────────────────
_STARTUP_TIME: datetime = datetime.now(timezone.utc)
APP_VERSION = "1.0.0"

BACKUP_DIR = "/config/backups"


def _backup_file_path(filename: str) -> tuple[str, str] | None:
    safe_name = os.path.basename(filename or "")
    if safe_name != filename or not safe_name.endswith(".zip"):
        return None
    return safe_name, os.path.join(BACKUP_DIR, safe_name)


def _validate_backup_zip(filename: str) -> tuple[dict, int]:
    resolved = _backup_file_path(filename)
    if not resolved:
        return {"ok": False, "message": "Invalid filename"}, 400

    safe_name, fpath = resolved
    if not os.path.exists(fpath):
        return {"ok": False, "message": "Backup not found"}, 404

    try:
        with zipfile.ZipFile(fpath, "r") as zf:
            names = zf.namelist()
            if "manga_arr.db" not in names:
                return {
                    "ok": False,
                    "filename": safe_name,
                    "message": "Backup does not contain manga_arr.db",
                    "entries": names,
                    "containsDatabase": False,
                    "databaseValid": False,
                }, 422
            db_bytes = zf.read("manga_arr.db")
    except zipfile.BadZipFile:
        return {"ok": False, "filename": safe_name, "message": "Invalid ZIP file"}, 400
    except OSError as exc:
        return {
            "ok": False,
            "filename": safe_name,
            "message": f"Backup validation failed: {type(exc).__name__}",
        }, 500

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    try:
        tmp.write(db_bytes)
        tmp.close()
        with sqlite3.connect(tmp.name) as c:
            quick_check = c.execute("PRAGMA quick_check").fetchone()
            db_valid = bool(quick_check and quick_check[0] == "ok")
            if db_valid:
                c.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
    except sqlite3.DatabaseError:
        db_valid = False
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    if not db_valid:
        return {
            "ok": False,
            "filename": safe_name,
            "message": "manga_arr.db is not a valid SQLite database",
            "entries": names,
            "containsDatabase": True,
            "databaseValid": False,
        }, 422

    return {
        "ok": True,
        "filename": safe_name,
        "message": "Backup validated",
        "entries": names,
        "containsDatabase": True,
        "databaseValid": True,
        "sizeBytes": os.path.getsize(fpath),
    }, 200

# ── Task registry ─────────────────────────────────────────────────────────────
TASKS: list[dict] = [
    {"name": "RSS Sync", "key": "RssSyncAll", "interval": "15 min", "manual": False},
    {
        "name": "Check Downloads",
        "key": "CheckDownloads",
        "interval": "1 min",
        "manual": False,
    },
    {
        "name": "Backlog Search",
        "key": "BacklogSearch",
        "interval": "24 hr",
        "manual": True,
    },
    {
        "name": "Refresh Metadata",
        "key": "RefreshMetadata",
        "interval": "24 hr",
        "manual": True,
    },
    {
        "name": "Import List Sync",
        "key": "ImportListSync",
        "interval": "12 hr",
        "manual": True,
    },
    {"name": "Auto Backup", "key": "Backup", "interval": "daily", "manual": True},
    {
        "name": "Reset Stuck Grabs",
        "key": "ResetStuckGrabs",
        "interval": "manual",
        "manual": True,
    },
    {
        "name": "Cleanup Seen Cache",
        "key": "CleanupSeen",
        "interval": "manual",
        "manual": True,
    },
    {
        "name": "Recycle Bin Purge",
        "key": "RecycleBinPurge",
        "interval": "6 hr",
        "manual": True,
    },
]

TASK_STATE: dict[str, dict] = {
    t["key"]: {"last_run": None, "next_run": None} for t in TASKS
}


def update_task_state(
    key: str, last_run: Optional[datetime] = None, next_run: Optional[datetime] = None
):
    """Called by main.py to update task last_run/next_run timestamps."""
    if key in TASK_STATE:
        if last_run is not None:
            TASK_STATE[key]["last_run"] = last_run
        if next_run is not None:
            TASK_STATE[key]["next_run"] = next_run


def _fmt_uptime(start: datetime) -> str:
    delta = datetime.now(timezone.utc) - start
    total_seconds = int(delta.total_seconds())
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def _db_size() -> int:
    try:
        return os.path.getsize(DB_PATH)
    except OSError:
        return 0


def _fmt_bytes(n: float) -> str:
    n = int(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"


def _root_folders_disk(db) -> list[dict]:
    rows = db.execute("SELECT path FROM root_folders ORDER BY path").fetchall()
    result = []
    for row in rows:
        path = row["path"]
        try:
            usage = shutil.disk_usage(path)
            used_pct = int(usage.used / usage.total * 100) if usage.total else 0
            result.append(
                {
                    "path": path,
                    "free": usage.free,
                    "total": usage.total,
                    "used": usage.used,
                    "used_pct": used_pct,
                    "free_fmt": _fmt_bytes(usage.free),
                    "total_fmt": _fmt_bytes(usage.total),
                }
            )
        except OSError:
            result.append(
                {
                    "path": path,
                    "free": 0,
                    "total": 0,
                    "used": 0,
                    "used_pct": 0,
                    "free_fmt": "N/A",
                    "total_fmt": "N/A",
                }
            )
    return result


# ── System Status ─────────────────────────────────────────────────────────────
@router.get("/system/status", response_class=HTMLResponse)
async def system_status_page(request: Request):
    with get_db() as db:
        series_count = db.execute(
            "SELECT COUNT(*) FROM series WHERE deleted_at IS NULL"
        ).fetchone()[0]
        volumes_count = db.execute(
            "SELECT COUNT(*) FROM volumes WHERE volume_num IS NOT NULL"
        ).fetchone()[0]
        downloaded_count = db.execute(
            "SELECT COUNT(*) FROM volumes WHERE status='downloaded'"
        ).fetchone()[0]
        wanted_count = db.execute(
            "SELECT COUNT(*) FROM volumes WHERE status='wanted' AND monitored=1"
        ).fetchone()[0]
        root_folders = _root_folders_disk(db)

    db_size = _db_size()
    return templates.TemplateResponse(
        request,
        "system_status.html",
        {
            "app_version": APP_VERSION,
            "python_version": sys.version.split()[0],
            "os_system": platform.system(),
            "os_release": platform.release(),
            "db_path": DB_PATH,
            "db_size": _fmt_bytes(db_size),
            "uptime": _fmt_uptime(_STARTUP_TIME),
            "series_count": series_count,
            "volumes_count": volumes_count,
            "downloaded_count": downloaded_count,
            "wanted_count": wanted_count,
            "root_folders": root_folders,
        },
    )


# ── Task Scheduler ────────────────────────────────────────────────────────────
@router.get("/system/tasks", response_class=HTMLResponse)
async def system_tasks_page(request: Request):
    tasks_with_state = []
    for t in TASKS:
        state = TASK_STATE.get(t["key"], {})
        last_run = state.get("last_run")  # datetime | None
        next_run = state.get("next_run")  # datetime | None
        tasks_with_state.append(
            {
                **t,
                "last_run_dt": last_run,
                "next_run_dt": next_run,
            }
        )
    return templates.TemplateResponse(
        request,
        "system_tasks.html",
        {
            "tasks": tasks_with_state,
        },
    )


# ── Command API ───────────────────────────────────────────────────────────────
@router.post("/api/command")
async def run_command(request: Request):
    body = await request.json()
    name = body.get("name", "")

    try:
        import main as main_module  # lazy import to avoid circular deps
    except ImportError:
        main_module = None  # type: ignore[assignment]

    def _create(coro, name: str):
        """Schedule a coroutine safely regardless of whether we have a running loop."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                if main_module and hasattr(main_module, "create_background_task"):
                    main_module.create_background_task(coro, name=f"command:{name}")
                else:
                    loop.create_task(coro)
            else:
                loop.run_until_complete(coro)
        except Exception:
            pass

    if name == "RssSyncAll":
        if main_module and hasattr(main_module, "poll_rss"):
            _create(main_module.poll_rss(), name)
    elif name == "CheckDownloads":
        if main_module and hasattr(main_module, "check_download_status"):
            _create(main_module.check_download_status(), name)
    elif name == "BacklogSearch":
        if main_module and hasattr(main_module, "backlog_search"):
            _create(main_module.backlog_search(), name)
    elif name == "RefreshMetadata":
        if main_module and hasattr(main_module, "refresh_ongoing_loop"):
            _create(main_module.refresh_ongoing_loop(), name)
    elif name == "ImportListSync":
        if main_module and hasattr(main_module, "import_list_sync"):
            _create(main_module.import_list_sync(), name)
    elif name == "CleanupSeen":
        with get_db() as db:
            # Delete seen entries older than 90 days where the volume was never downloaded
            # Keep entries tied to volumes still in grabbed/wanted so dedup still works
            result = db.execute(
                "DELETE FROM seen WHERE grabbed_at < datetime('now', '-90 days')"
                " AND (series_id IS NULL OR NOT EXISTS ("
                "   SELECT 1 FROM volumes v WHERE v.download_id = seen.download_id"
                "   AND v.status IN ('grabbed','wanted')"
                "))"
            )
            count = result.rowcount
        try:
            import main as _m

            _m.log_event("info", f"Seen cache cleanup: removed {count} old entries")
        except Exception:
            pass
        return JSONResponse(
            {"ok": True, "message": f"Removed {count} stale seen-cache entries"}
        )
    elif name == "ResetStuckGrabs":
        with get_db() as db:
            result = db.execute(
                "UPDATE volumes SET status='wanted', grabbed_at=NULL, download_id=NULL,"
                " source_url=NULL, torrent_name=NULL, indexer=NULL, protocol=NULL,"
                " client=NULL, release_group=NULL, import_path=NULL"
                " WHERE status='grabbed'"
                "   AND grabbed_at < datetime('now', '-2 days')"
                "   AND NOT EXISTS ("
                "     SELECT 1 FROM import_queue iq WHERE iq.download_id = volumes.download_id"
                "     AND iq.status IN ('pending','partial')"
                "   )"
            )
            count = result.rowcount
        try:
            import main as _m

            _m.log_event(
                "info", f"Reset {count} stuck grabbed volume(s) back to wanted"
            )
        except Exception:
            pass
        return JSONResponse(
            {"ok": True, "message": f"Reset {count} stuck grabbed volume(s) to wanted"}
        )
    else:
        return JSONResponse(
            {"ok": False, "message": f"Unknown command: {name}"}, status_code=400
        )

    return JSONResponse({"ok": True, "message": f"{name} started"})


# ── Backup ────────────────────────────────────────────────────────────────────
@router.get("/system/backup", response_class=HTMLResponse)
async def system_backup_page(request: Request):
    backups = []
    os.makedirs(BACKUP_DIR, exist_ok=True)
    try:
        for fname in sorted(os.listdir(BACKUP_DIR), reverse=True):
            if not fname.endswith(".zip"):
                continue
            fpath = os.path.join(BACKUP_DIR, fname)
            try:
                stat = os.stat(fpath)
                backups.append(
                    {
                        "filename": fname,
                        "size": _fmt_bytes(stat.st_size),
                        "date": datetime.fromtimestamp(stat.st_mtime).strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),
                        "mtime": stat.st_mtime,
                    }
                )
            except OSError:
                pass
    except OSError:
        pass
    return templates.TemplateResponse(
        request,
        "system_backup.html",
        {
            "backups": backups,
        },
    )


@router.post("/api/system/backup/create")
async def create_backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"mangarr_backup_{ts}.zip"
    saved_path = os.path.join(BACKUP_DIR, filename)

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if os.path.exists(DB_PATH):
            zf.write(DB_PATH, arcname="manga_arr.db")
    buf.seek(0)
    zip_bytes = buf.read()

    # Save a copy to backup dir
    with open(saved_path, "wb") as f:
        f.write(zip_bytes)

    return StreamingResponse(
        BytesIO(zip_bytes),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/api/system/backup/{filename}/delete")
async def delete_backup(filename: str):
    resolved = _backup_file_path(filename)
    if not resolved:
        return JSONResponse(
            {"ok": False, "message": "Invalid filename"}, status_code=400
        )
    _safe_name, fpath = resolved
    try:
        os.remove(fpath)
    except OSError:
        pass
    return RedirectResponse("/system/backup", status_code=303)


@router.post("/api/system/backup/{filename}/validate")
async def validate_backup(filename: str):
    payload, status_code = _validate_backup_zip(filename)
    return JSONResponse(payload, status_code=status_code)


# ── Tags ──────────────────────────────────────────────────────────────────────
@router.get("/tags", response_class=HTMLResponse)
async def tags_page(request: Request):
    with get_db() as db:
        rows = db.execute(
            """SELECT tag, COUNT(*) AS series_count
               FROM series_tags
               GROUP BY tag
               ORDER BY tag COLLATE NOCASE"""
        ).fetchall()
    tags = [{"tag": r["tag"], "series_count": r["series_count"]} for r in rows]
    return templates.TemplateResponse(request, "tags.html", {"tags": tags})


@router.post("/api/tags/rename")
async def rename_tag(old_name: str = Form(...), new_name: str = Form(...)):
    new_name = new_name.strip()
    if not new_name:
        return RedirectResponse("/tags", status_code=303)
    with get_db() as db:
        db.execute("UPDATE series_tags SET tag=? WHERE tag=?", (new_name, old_name))
    return RedirectResponse("/tags", status_code=303)


@router.post("/api/tags/{tag}/delete")
async def delete_tag(tag: str):
    with get_db() as db:
        db.execute("DELETE FROM series_tags WHERE tag=?", (tag,))
    return RedirectResponse("/tags", status_code=303)
