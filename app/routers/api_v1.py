"""Sonarr-style API v1 endpoints.

These endpoints are intentionally conservative: they expose stable JSON
contracts for external automation without replacing Mangarr's existing
workflow-specific `/api/*` actions.
"""
from __future__ import annotations

import difflib
import os
import platform
import re
import shutil
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.status import HTTP_400_BAD_REQUEST, HTTP_404_NOT_FOUND

from files import build_chapter_label
from library_scan import adopt_unmapped_folder, scan_unmapped_root_folder
from metadata import search_series
from parsing import normalize
from rename_plan import build_series_rename_preview, execute_series_rename
from routers.history_ import (
    clear_failed_history_entries,
    delete_history_entry,
    mark_history_failed,
)
from routers.import_ import (
    clear_inactive_import_queue_entries,
    dismiss_import_queue_entry,
    retry_import_queue_entry,
    skip_import_queue_entry,
)
from routers.queue_ import dismiss_pending_release, reset_grabbed_volume
from routers.series_ import patch_series as _patch_series
from routers.system import APP_VERSION, TASKS, TASK_STATE, run_command as _run_command
from shared import (
    build_volume_label,
    from_json,
    get_cfg,
    get_db,
    quality_rank,
)

router = APIRouter()


def _bool(value) -> bool:
    return bool(value)


def _bool_default_true(value) -> bool:
    return True if value is None else bool(value)


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return slug or "series"


def _dt_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _series_tags(db, series_id: int, json_tags: str | None) -> list[str]:
    tags: set[str] = set()
    for tag in from_json(json_tags, []) or []:
        if isinstance(tag, str) and tag:
            tags.add(tag)
    rows = db.execute(
        "SELECT tag FROM series_tags WHERE series_id=? ORDER BY tag",
        (series_id,),
    ).fetchall()
    for row in rows:
        if row["tag"]:
            tags.add(row["tag"])
    return sorted(tags)


def _quality_profile(row) -> dict:
    qualities = from_json(row["qualities"], []) or []
    return {
        "id": row["id"],
        "name": row["name"],
        "qualities": qualities,
        "cutoff": row["cutoff"],
        "upgradesAllowed": _bool(row["upgrades_allowed"]),
        "minimumCustomFormatScore": row["minimum_custom_format_score"] or 0,
        "cutoffFormatScore": row["cutoff_format_score"] or 10000,
        "minUpgradeFormatScore": row["min_upgrade_format_score"] or 10,
        "isDefault": _bool(row["is_default"]),
    }


def _language_profile(row, default_id: int | None) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "languages": from_json(row["languages"], []) or [],
        "allowAny": _bool(row["allow_any"]),
        "isDefault": row["id"] == default_id,
    }


def _custom_format(row, scores: list[dict]) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "specifications": from_json(row["specifications"], []) or [],
        "includeCustomFormatWhenRenaming": _bool(
            row["include_custom_format_when_renaming"]
        ),
        "qualityProfileScores": scores,
    }


def _release_profile(row, tags: list[str]) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "enabled": _bool(row["enabled"]),
        "required": row["required"] or "",
        "ignored": row["ignored"] or "",
        "preferred": from_json(row["preferred"], []) or [],
        "includePreferredWhenRenaming": _bool(
            row["include_preferred_when_renaming"]
        ),
        "tags": tags,
    }


def _root_folder(row) -> dict:
    path = row["path"]
    disk = {
        "totalSpace": None,
        "freeSpace": None,
        "unmappedFolders": [],
        "isAvailable": False,
    }
    try:
        usage = shutil.disk_usage(path)
        disk.update(
            {
                "totalSpace": usage.total,
                "freeSpace": usage.free,
                "isAvailable": True,
            }
        )
    except OSError:
        pass
    return {
        "id": row["id"],
        "path": path,
        "name": row["label"] or path,
        "label": row["label"],
        "isDefault": _bool(row["is_default"]),
        **disk,
    }


def _series(row, tags: list[str]) -> dict:
    title = row["title"]
    downloaded = row["downloaded_count"] or 0
    total = row["total_volume_count"] or row["total_volumes"] or 0
    wanted = row["wanted_count"] or 0
    grabbed = row["grabbed_count"] or 0
    return {
        "id": row["id"],
        "title": title,
        "sortTitle": title.lower(),
        "titleSlug": _slug(title),
        "searchPattern": row["search_pattern"],
        "status": row["status"],
        "overview": row["description"],
        "images": [{"coverType": "poster", "url": row["cover_url"]}]
        if row["cover_url"]
        else [],
        "monitored": _bool_default_true(row["monitored"]),
        "enabled": _bool_default_true(row["enabled"]),
        "monitorMode": row["monitor_mode"] or "all",
        "qualityProfileId": row["quality_profile_id"],
        "qualityProfileName": row["quality_profile_name"],
        "languageProfileId": row["language_profile_id"],
        "rootFolderId": row["root_folder_id"],
        "rootFolderPath": row["root_folder_path"],
        "path": row["root_folder_path"],
        "tags": tags,
        "added": row["added_at"],
        "year": row["pub_year"],
        "anilistId": row["anilist_id"],
        "mangadexId": row["mangadex_id"],
        "malId": row["mal_id"],
        "mangaUpdatesId": row["mu_id"],
        "totalVolumes": row["total_volumes"],
        "totalChapters": row["total_chapters"],
        "statistics": {
            "volumeCount": total,
            "volumeFileCount": downloaded,
            "wantedCount": wanted,
            "grabbedCount": grabbed,
            "percentOfVolumes": round((downloaded / total) * 100, 1) if total else 0,
        },
    }


def _series_payload(db, row) -> dict:
    return _series(row, _series_tags(db, row["id"], row["tags"]))


def _volume(row) -> dict:
    vol_range = (
        (row["vol_range_start"], row["vol_range_end"])
        if row["vol_range_start"] is not None and row["vol_range_end"] is not None
        else None
    )
    return {
        "id": row["id"],
        "seriesId": row["series_id"],
        "volumeNumber": row["volume_num"],
        "chapterNumber": row["chapter_num"],
        "label": build_volume_label(row["volume_num"], vol_range, row["pack_type"]),
        "title": row["title"],
        "status": row["status"],
        "monitored": _bool_default_true(row["monitored"]),
        "quality": row["quality"],
        "size": row["size_bytes"] or 0,
        "sourceTitle": row["torrent_name"],
        "indexer": row["indexer"],
        "protocol": row["protocol"],
        "downloadClient": row["client"],
        "downloadId": row["download_id"],
        "importPath": row["import_path"],
        "grabbedAt": row["grabbed_at"],
        "importedAt": row["imported_at"],
    }


def _chapter(row) -> dict:
    return {
        "id": row["id"],
        "seriesId": row["series_id"],
        "volumeId": row["volume_id"],
        "chapterNumber": row["chapter_num"],
        "chapterRangeEnd": row["chapter_range_end"],
        "label": build_chapter_label(row["chapter_num"], row["chapter_range_end"]),
        "title": row["title"],
        "status": row["status"],
        "monitored": _bool_default_true(row["monitored"]),
        "quality": row["quality"],
        "size": row["size_bytes"] or 0,
        "sourceTitle": row["torrent_name"],
        "indexer": row["indexer"],
        "protocol": row["protocol"],
        "downloadClient": row["client"],
        "downloadId": row["download_id"],
        "importPath": row["import_path"],
        "grabbedAt": row["grabbed_at"],
        "importedAt": row["imported_at"],
    }


def _series_base_query() -> str:
    return """
        SELECT s.*,
               rf.path AS root_folder_path,
               qp.name AS quality_profile_name,
               COUNT(v.id) AS total_volume_count,
               SUM(CASE WHEN v.status='downloaded' THEN 1 ELSE 0 END) AS downloaded_count,
               SUM(CASE WHEN v.status='wanted' THEN 1 ELSE 0 END) AS wanted_count,
               SUM(CASE WHEN v.status='grabbed' THEN 1 ELSE 0 END) AS grabbed_count
        FROM series s
        LEFT JOIN root_folders rf ON rf.id=s.root_folder_id
        LEFT JOIN quality_profiles qp ON qp.id=s.quality_profile_id
        LEFT JOIN volumes v ON v.series_id=s.id
    """


def _iso_or_none(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _optional_id_set(payload: dict, key: str) -> set[int] | None:
    if key not in payload:
        return None
    value = payload[key]
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list of integer IDs")
    ids: set[int] = set()
    for item in value:
        if not isinstance(item, int) or isinstance(item, bool):
            raise ValueError(f"{key} must be a list of integer IDs")
        ids.add(item)
    return ids


def _norm_fs_path(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _match_confidence(query: str, title: str) -> int:
    q = normalize(query)
    t = normalize(title)
    if not q or not t:
        return 0
    if q == t:
        return 100
    if q in t or t in q:
        return 86
    return int(round(difflib.SequenceMatcher(None, q, t).ratio() * 100))


def _metadata_match_payload(query: str, result: dict) -> dict:
    title = result.get("title") or ""
    return {
        "title": title,
        "source": result.get("source") or "",
        "confidence": _match_confidence(query, title),
        "anilistId": result.get("anilist_id"),
        "mangaUpdatesId": result.get("mu_id"),
        "malId": result.get("mal_id"),
        "coverUrl": result.get("cover_url") or "",
        "status": result.get("status") or "",
        "volumes": result.get("volumes"),
        "chapters": result.get("chapters"),
        "year": result.get("pub_year"),
        "description": result.get("description") or "",
    }


def _optional_payload_int(payload: dict, key: str) -> int | None:
    value = payload.get(key)
    if value in (None, ""):
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return value


def _optional_bool_query(value: str | None, name: str) -> bool | None:
    if value in (None, ""):
        return None
    normalized = str(value).strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be a boolean")


@router.get("/api/v1/system/status")
async def api_v1_system_status():
    return JSONResponse(
        {
            "appName": "Mangarr",
            "instanceName": get_cfg("instance_name", "Mangarr") or "Mangarr",
            "version": APP_VERSION,
            "authentication": "apikey",
            "databaseType": "sqlite",
            "pythonVersion": platform.python_version(),
            "osName": platform.system(),
            "startupPath": os.getcwd(),
            "urlBase": get_cfg("url_base", ""),
            "timestamp": _dt_utc(),
        }
    )


@router.get("/api/v1/rootfolder")
async def api_v1_root_folders():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM root_folders ORDER BY is_default DESC, label, path"
        ).fetchall()
        payload = [_root_folder(row) for row in rows]
    return JSONResponse(payload)


@router.get("/api/v1/qualityprofile")
async def api_v1_quality_profiles():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM quality_profiles ORDER BY id"
        ).fetchall()
        payload = [_quality_profile(row) for row in rows]
    return JSONResponse(payload)


@router.get("/api/v1/languageprofile")
async def api_v1_language_profiles():
    with get_db() as db:
        default_id = None
        row = db.execute(
            "SELECT value FROM settings WHERE key='default_language_profile_id'"
        ).fetchone()
        if row:
            try:
                default_id = int(row["value"])
            except (TypeError, ValueError):
                default_id = None
        rows = db.execute(
            "SELECT * FROM language_profiles ORDER BY id"
        ).fetchall()
        payload = [_language_profile(row, default_id) for row in rows]
    return JSONResponse(payload)


@router.get("/api/v1/customformat")
async def api_v1_custom_formats():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM custom_formats ORDER BY name COLLATE NOCASE"
        ).fetchall()
        score_rows = db.execute(
            """
            SELECT format_id, profile_id, score
            FROM quality_profile_custom_formats
            ORDER BY profile_id
            """
        ).fetchall()
        scores_by_format: dict[int, list[dict]] = {}
        for score in score_rows:
            scores_by_format.setdefault(score["format_id"], []).append(
                {
                    "qualityProfileId": score["profile_id"],
                    "score": score["score"],
                }
            )
        payload = [
            _custom_format(row, scores_by_format.get(row["id"], []))
            for row in rows
        ]
    return JSONResponse(payload)


@router.get("/api/v1/releaseprofile")
async def api_v1_release_profiles():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM release_profiles ORDER BY id"
        ).fetchall()
        tag_rows = db.execute(
            "SELECT profile_id, tag FROM release_profile_tags ORDER BY tag"
        ).fetchall()
        tags_by_profile: dict[int, list[str]] = {}
        for tag in tag_rows:
            tags_by_profile.setdefault(tag["profile_id"], []).append(tag["tag"])
        payload = [
            _release_profile(row, tags_by_profile.get(row["id"], []))
            for row in rows
        ]
    return JSONResponse(payload)


@router.get("/api/v1/series")
async def api_v1_series(
    request: Request,
    term: str = "",
    monitored: str | None = None,
    rootFolderId: int = 0,
    qualityProfileId: int = 0,
    languageProfileId: int = 0,
    status: str = "",
    tag: str = "",
    includeDeleted: str | None = None,
    sortKey: str = "title",
    sortDirection: str = "asc",
    page: int = 0,
    pageSize: int = 0,
):
    try:
        monitored_filter = _optional_bool_query(monitored, "monitored")
        include_deleted = _optional_bool_query(includeDeleted, "includeDeleted") or False
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    where_parts: list[str] = []
    params: list = []
    if not include_deleted:
        where_parts.append("s.deleted_at IS NULL")
    if term:
        like = f"%{term.strip()}%"
        where_parts.append("(s.title LIKE ? OR s.search_pattern LIKE ?)")
        params.extend([like, like])
    if monitored_filter is not None:
        where_parts.append("COALESCE(s.monitored, 1)=?")
        params.append(1 if monitored_filter else 0)
    if rootFolderId:
        where_parts.append("s.root_folder_id=?")
        params.append(rootFolderId)
    if qualityProfileId:
        where_parts.append("s.quality_profile_id=?")
        params.append(qualityProfileId)
    if languageProfileId:
        where_parts.append("s.language_profile_id=?")
        params.append(languageProfileId)
    if status:
        where_parts.append("s.status=?")
        params.append(status)

    sort_key = sortKey if sortKey in {"title", "added", "year", "id"} else "title"
    sort_dir = "DESC" if sortDirection.lower() == "desc" else "ASC"
    sort_sql = {
        "title": f"s.title COLLATE NOCASE {sort_dir}, s.id ASC",
        "added": f"s.added_at {sort_dir}, s.id ASC",
        "year": f"s.pub_year {sort_dir}, s.title COLLATE NOCASE ASC",
        "id": f"s.id {sort_dir}",
    }[sort_key]
    where = "WHERE " + " AND ".join(where_parts) if where_parts else ""

    with get_db() as db:
        rows = db.execute(
            _series_base_query()
            + f"""
            {where}
            GROUP BY s.id
            ORDER BY {sort_sql}
            """,
            params,
        ).fetchall()
        payload = [
            _series_payload(db, row)
            for row in rows
        ]
    if tag:
        payload = [row for row in payload if tag in row["tags"]]
    total = len(payload)
    if pageSize:
        page = max(page, 1)
        page_size = max(min(pageSize, 250), 1)
        offset = (page - 1) * page_size
        payload = payload[offset:offset + page_size]
    else:
        page = 1
        page_size = total
    return JSONResponse(
        payload,
        headers={
            "X-Total-Count": str(total),
            "X-Page": str(page),
            "X-Page-Size": str(page_size),
        },
    )


@router.get("/api/v1/series/{series_id}")
async def api_v1_series_detail(series_id: int):
    with get_db() as db:
        row = db.execute(
            _series_base_query()
            + """
            WHERE s.id=? AND s.deleted_at IS NULL
            GROUP BY s.id
            """,
            (series_id,),
        ).fetchone()
        if not row:
            return JSONResponse(
                {"message": "Not Found", "description": "Series not found"},
                status_code=HTTP_404_NOT_FOUND,
            )
        payload = _series_payload(db, row)
        volumes = db.execute(
            """
            SELECT * FROM volumes
            WHERE series_id=?
            ORDER BY COALESCE(volume_num, 999999), id
            """,
            (series_id,),
        ).fetchall()
        chapters = db.execute(
            """
            SELECT * FROM chapters
            WHERE series_id=?
            ORDER BY chapter_num, id
            """,
            (series_id,),
        ).fetchall()
        payload["volumes"] = [_volume(v) for v in volumes]
        payload["chapters"] = [_chapter(c) for c in chapters]
    return JSONResponse(payload)


@router.patch("/api/v1/series/{series_id}")
async def api_v1_patch_series(request: Request, series_id: int):
    return await _patch_series(request, series_id)


@router.get("/api/v1/queue")
async def api_v1_queue():
    with get_db() as db:
        grabbed_rows = db.execute(
            """
            SELECT v.id, v.series_id, s.title AS series_title, v.volume_num,
                   v.vol_range_start, v.vol_range_end, v.pack_type,
                   v.torrent_name, v.download_id, v.grabbed_at, v.indexer,
                   v.protocol, v.client, v.size_bytes
            FROM volumes v
            JOIN series s ON s.id=v.series_id
            WHERE v.status='grabbed'
            ORDER BY v.grabbed_at DESC
            """
        ).fetchall()
        import_rows = db.execute(
            """
            SELECT iq.*, s.title AS series_title
            FROM import_queue iq
            LEFT JOIN series s ON s.id=iq.series_id
            WHERE iq.status IN ('pending','processing','partial','failed')
            ORDER BY iq.created_at DESC
            """
        ).fetchall()
        pending_rows = db.execute(
            """
            SELECT pr.*, s.title AS series_title
            FROM pending_releases pr
            LEFT JOIN series s ON s.id=pr.series_id
            ORDER BY pr.first_seen DESC
            """
        ).fetchall()

        payload = []
        for row in grabbed_rows:
            vol_range = (
                (row["vol_range_start"], row["vol_range_end"])
                if row["vol_range_start"] is not None and row["vol_range_end"] is not None
                else None
            )
            payload.append(
                {
                    "id": f"volume-{row['id']}",
                    "queueId": None,
                    "seriesId": row["series_id"],
                    "seriesTitle": row["series_title"],
                    "title": row["torrent_name"] or "",
                    "volumeId": row["id"],
                    "volumeLabel": build_volume_label(
                        row["volume_num"], vol_range, row["pack_type"]
                    ),
                    "status": "grabbed",
                    "trackedDownloadStatus": "downloading",
                    "downloadId": row["download_id"],
                    "protocol": row["protocol"],
                    "indexer": row["indexer"],
                    "downloadClient": row["client"],
                    "size": row["size_bytes"] or 0,
                    "added": row["grabbed_at"],
                }
            )
        for row in import_rows:
            payload.append(
                {
                    "id": f"import-{row['id']}",
                    "queueId": row["id"],
                    "seriesId": row["series_id"],
                    "seriesTitle": row["series_title"],
                    "title": row["torrent_name"] or "",
                    "volumeId": None,
                    "volumeLabel": build_volume_label(row["volume_num"], None, None),
                    "status": row["status"],
                    "trackedDownloadStatus": "importPending",
                    "downloadId": row["download_id"],
                    "protocol": None,
                    "indexer": None,
                    "downloadClient": None,
                    "size": 0,
                    "added": row["created_at"],
                    "sourcePath": row["src_dir"],
                }
            )
        for row in pending_rows:
            payload.append(
                {
                    "id": f"pending-{row['id']}",
                    "queueId": None,
                    "seriesId": row["series_id"],
                    "seriesTitle": row["series_title"],
                    "title": row["title"] or "",
                    "volumeId": None,
                    "volumeLabel": "",
                    "status": "pending",
                    "trackedDownloadStatus": "delay",
                    "downloadId": None,
                    "protocol": row["protocol"],
                    "indexer": row["indexer"],
                    "downloadClient": None,
                    "size": row["size_bytes"] or 0,
                    "added": row["first_seen"],
                    "downloadUrl": row["url"],
                }
            )
    return JSONResponse(payload)


@router.post("/api/v1/queue/grabbed/{volume_id}/reset")
async def api_v1_queue_reset_grabbed_volume(volume_id: int):
    result = reset_grabbed_volume(volume_id)
    if result["status"] == "not_found":
        return JSONResponse(
            {"error": "queue volume not found"},
            status_code=HTTP_404_NOT_FOUND,
        )
    if result["status"] == "not_grabbed":
        return JSONResponse(
            {"error": "queue volume is not grabbed"},
            status_code=HTTP_400_BAD_REQUEST,
        )
    return JSONResponse({"ok": True, "id": volume_id})


@router.delete("/api/v1/queue/pending/{pending_id}")
async def api_v1_queue_dismiss_pending(pending_id: int):
    result = dismiss_pending_release(pending_id)
    if result["status"] == "not_found":
        return JSONResponse(
            {"error": "pending release not found"},
            status_code=HTTP_404_NOT_FOUND,
        )
    return JSONResponse({"ok": True, "id": pending_id})


@router.delete("/api/v1/queue/import/failed")
async def api_v1_queue_clear_failed_imports():
    result = clear_inactive_import_queue_entries()
    return JSONResponse(
        {
            "ok": True,
            "deleted": result["deleted"],
            "deletedFiles": result["deleted_files"],
        }
    )


@router.delete("/api/v1/queue/import/{queue_id}")
async def api_v1_queue_dismiss_import(queue_id: int):
    result = dismiss_import_queue_entry(queue_id)
    if result["status"] == "not_found":
        return JSONResponse(
            {"error": "import queue entry not found"},
            status_code=HTTP_404_NOT_FOUND,
        )
    return JSONResponse({"ok": True, "id": queue_id})


@router.post("/api/v1/queue/import/{queue_id}/skip")
async def api_v1_queue_skip_import(queue_id: int):
    result = skip_import_queue_entry(queue_id)
    if result["status"] == "not_found":
        return JSONResponse(
            {"error": "import queue entry not found"},
            status_code=HTTP_404_NOT_FOUND,
        )
    if result["status"] == "not_skippable":
        return JSONResponse(
            {"error": "import queue entry is not pending or partial"},
            status_code=HTTP_400_BAD_REQUEST,
        )
    return JSONResponse({"ok": True, "id": queue_id})


@router.post("/api/v1/queue/import/{queue_id}/retry")
async def api_v1_queue_retry_import(queue_id: int):
    result = retry_import_queue_entry(queue_id)
    if result["status"] == "not_found":
        return JSONResponse(
            {"error": "import queue entry not found"},
            status_code=HTTP_404_NOT_FOUND,
        )
    if result["status"] == "not_retryable":
        return JSONResponse(
            {"error": "import queue entry is not failed or partial"},
            status_code=HTTP_400_BAD_REQUEST,
        )
    return JSONResponse({"ok": True, "id": queue_id, "queued": result["queued"]})


@router.get("/api/v1/blocklist")
async def api_v1_blocklist():
    ttl_days = max(0, int(get_cfg("blocklist_ttl_days", "90") or "90"))
    with get_db() as db:
        rows = db.execute(
            """
            SELECT bl.*, s.title AS series_title
            FROM blocklist bl
            LEFT JOIN series s ON s.id=bl.series_id
            ORDER BY bl.added_at DESC
            """
        ).fetchall()
    payload = []
    for row in rows:
        expires_at = None
        if ttl_days > 0 and row["added_at"]:
            try:
                added = datetime.fromisoformat(
                    str(row["added_at"]).replace("Z", "+00:00")
                )
                if added.tzinfo is None:
                    added = added.replace(tzinfo=timezone.utc)
                expires_at = (added + timedelta(days=ttl_days)).isoformat()
            except Exception:
                expires_at = None
        payload.append(
            {
                "id": row["id"],
                "seriesId": row["series_id"],
                "seriesTitle": row["series_title"],
                "sourceTitle": row["torrent_name"],
                "downloadUrl": row["torrent_url"],
                "reason": row["reason"],
                "indexer": row["indexer"],
                "protocol": row["protocol"],
                "size": row["size_bytes"] or 0,
                "date": row["added_at"],
                "expiresAt": expires_at,
            }
        )
    return JSONResponse(payload)


@router.delete("/api/v1/blocklist/{blocklist_id}")
async def api_v1_delete_blocklist_entry(blocklist_id: int):
    with get_db() as db:
        cur = db.execute("DELETE FROM blocklist WHERE id=?", (blocklist_id,))
        deleted = cur.rowcount
    if deleted < 1:
        return JSONResponse(
            {"error": "blocklist entry not found"},
            status_code=HTTP_404_NOT_FOUND,
        )
    return JSONResponse({"ok": True, "id": blocklist_id})


@router.get("/api/v1/command")
async def api_v1_commands():
    payload = []
    for task in TASKS:
        state = TASK_STATE.get(task["key"], {})
        payload.append(
            {
                "name": task["key"],
                "displayName": task["name"],
                "interval": task["interval"],
                "manual": _bool(task["manual"]),
                "lastRun": _iso_or_none(state.get("last_run")),
                "nextRun": _iso_or_none(state.get("next_run")),
            }
        )
    return JSONResponse(payload)


@router.post("/api/v1/command")
async def api_v1_run_command(request: Request):
    return await _run_command(request)


@router.get("/api/v1/history")
async def api_v1_history(
    page: int = 1,
    pageSize: int = 50,
    eventType: str = "",
    seriesId: int = 0,
):
    page = max(page, 1)
    page_size = max(min(pageSize, 250), 1)
    where_parts: list[str] = []
    params: list = []
    if eventType:
        where_parts.append("event_type=?")
        params.append(eventType)
    if seriesId:
        where_parts.append("series_id=?")
        params.append(seriesId)
    where = "WHERE " + " AND ".join(where_parts) if where_parts else ""
    offset = (page - 1) * page_size
    with get_db() as db:
        total = db.execute(
            f"SELECT COUNT(*) FROM history {where}",
            params,
        ).fetchone()[0]
        rows = db.execute(
            f"SELECT * FROM history {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
        ).fetchall()
        records = [
            {
                "id": row["id"],
                "eventType": row["event_type"],
                "seriesId": row["series_id"],
                "seriesTitle": row["series_title"],
                "volumeLabel": row["volume_label"],
                "sourceTitle": row["source_title"],
                "indexer": row["indexer"],
                "protocol": row["protocol"],
                "downloadClient": row["client"],
                "downloadId": row["download_id"],
                "size": row["size_bytes"] or 0,
                "releaseGroup": row["release_group"],
                "data": from_json(row["data"], {}) or {},
                "date": row["created_at"],
            }
            for row in rows
        ]
    return JSONResponse(
        {
            "page": page,
            "pageSize": page_size,
            "totalRecords": total,
            "records": records,
        }
    )


@router.post("/api/v1/history/{history_id}/failed")
async def api_v1_history_mark_failed(history_id: int):
    result = mark_history_failed(history_id)
    if result["status"] == "not_found":
        return JSONResponse(
            {"error": "history entry not found"},
            status_code=HTTP_404_NOT_FOUND,
        )
    if result["status"] == "not_grabbed":
        return JSONResponse(
            {"error": "history entry is not grabbed"},
            status_code=HTTP_400_BAD_REQUEST,
        )
    return JSONResponse({"ok": True, "id": history_id})


@router.delete("/api/v1/history/failed")
async def api_v1_history_clear_failed():
    result = clear_failed_history_entries()
    return JSONResponse({"ok": True, "deleted": result["deleted"]})


@router.delete("/api/v1/history/{history_id}")
async def api_v1_history_delete(history_id: int):
    result = delete_history_entry(history_id)
    if result["status"] == "not_found":
        return JSONResponse(
            {"error": "history entry not found"},
            status_code=HTTP_404_NOT_FOUND,
        )
    return JSONResponse({"ok": True, "id": history_id})


@router.get("/api/v1/wanted")
async def api_v1_wanted():
    with get_db() as db:
        rows = db.execute(
            """
            SELECT v.id, v.series_id, s.title AS series_title, v.volume_num,
                   v.chapter_num, v.vol_range_start, v.vol_range_end,
                   v.pack_type, v.monitored, s.monitored AS series_monitored,
                   s.enabled AS series_enabled
            FROM volumes v
            JOIN series s ON s.id=v.series_id
            WHERE v.status='wanted'
              AND COALESCE(v.monitored, 1)=1
              AND COALESCE(s.monitored, 1)=1
              AND COALESCE(s.enabled, 1)=1
              AND s.deleted_at IS NULL
            ORDER BY s.title COLLATE NOCASE, v.volume_num
            """
        ).fetchall()
        payload = []
        for row in rows:
            vol_range = (
                (row["vol_range_start"], row["vol_range_end"])
                if row["vol_range_start"] is not None and row["vol_range_end"] is not None
                else None
            )
            payload.append(
                {
                    "id": row["id"],
                    "seriesId": row["series_id"],
                    "seriesTitle": row["series_title"],
                    "volumeNumber": row["volume_num"],
                    "chapterNumber": row["chapter_num"],
                    "volumeLabel": build_volume_label(
                        row["volume_num"], vol_range, row["pack_type"]
                    ),
                    "monitored": _bool_default_true(row["monitored"]),
                    "status": "wanted",
                }
            )
    return JSONResponse(payload)


@router.get("/api/v1/wanted/cutoff")
async def api_v1_wanted_cutoff():
    global_cutoff = get_cfg("quality_cutoff", "")
    with get_db() as db:
        rows = db.execute(
            """
            SELECT v.id, v.series_id, v.volume_num, v.quality, v.import_path,
                   s.title AS series_title, s.quality_cutoff, s.quality_profile_id,
                   qp.cutoff AS profile_cutoff, v.grabbed_at
            FROM volumes v
            JOIN series s ON s.id = v.series_id
            LEFT JOIN quality_profiles qp ON qp.id = s.quality_profile_id
            WHERE v.status = 'downloaded'
              AND s.monitored = 1
              AND s.deleted_at IS NULL
            ORDER BY s.title COLLATE NOCASE, v.volume_num
            """
        ).fetchall()
    payload = []
    for row in rows:
        effective_cutoff = (
            row["quality_cutoff"] or row["profile_cutoff"] or global_cutoff or ""
        ).lower()
        current_quality = (row["quality"] or "").lower()
        if not effective_cutoff or not current_quality:
            continue
        cutoff_rank = quality_rank(effective_cutoff)
        current_rank = quality_rank(current_quality)
        if cutoff_rank > 0 and current_rank < cutoff_rank:
            payload.append(
                {
                    "id": row["id"],
                    "seriesId": row["series_id"],
                    "seriesTitle": row["series_title"],
                    "volumeNumber": row["volume_num"],
                    "volumeLabel": build_volume_label(row["volume_num"], None, None),
                    "currentQuality": current_quality,
                    "cutoff": effective_cutoff,
                    "qualityCutoffSource": "series"
                    if row["quality_cutoff"]
                    else ("profile" if row["profile_cutoff"] else "global"),
                    "importPath": row["import_path"],
                    "grabbedAt": row["grabbed_at"],
                }
            )
    return JSONResponse(payload)


@router.get("/api/v1/rename/series/{series_id}/preview")
async def api_v1_rename_series_preview(series_id: int):
    preview = build_series_rename_preview(series_id)
    if preview is None:
        return JSONResponse(
            {"message": "Not Found", "description": "Series not found"},
            status_code=HTTP_404_NOT_FOUND,
        )
    return JSONResponse(preview)


@router.post("/api/v1/rename/series/{series_id}")
async def api_v1_rename_series_execute(request: Request, series_id: int):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return JSONResponse({"error": "expected an object body"}, status_code=400)
    try:
        volume_ids = _optional_id_set(payload, "volumeIds")
        chapter_ids = _optional_id_set(payload, "chapterIds")
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    result = execute_series_rename(
        series_id,
        volume_ids=volume_ids,
        chapter_ids=chapter_ids,
    )
    if result is None:
        return JSONResponse(
            {"message": "Not Found", "description": "Series not found"},
            status_code=HTTP_404_NOT_FOUND,
        )
    return JSONResponse(result)


@router.get("/api/v1/rootfolder/{root_folder_id}/unmappedfolders")
async def api_v1_root_folder_unmapped(root_folder_id: int):
    scan = scan_unmapped_root_folder(root_folder_id)
    if scan is None:
        return JSONResponse(
            {"message": "Not Found", "description": "Root folder not found"},
            status_code=HTTP_404_NOT_FOUND,
        )
    return JSONResponse(scan)


@router.get("/api/v1/rootfolder/{root_folder_id}/unmappedfolders/matches")
async def api_v1_root_folder_unmapped_matches(root_folder_id: int, path: str = ""):
    folder_path = (path or "").strip()
    if not folder_path:
        return JSONResponse({"error": "path is required"}, status_code=400)

    scan = scan_unmapped_root_folder(root_folder_id)
    if scan is None:
        return JSONResponse(
            {"message": "Not Found", "description": "Root folder not found"},
            status_code=HTTP_404_NOT_FOUND,
        )

    requested = _norm_fs_path(folder_path)
    folder = None
    for item in scan["unmappedFolders"]:
        if _norm_fs_path(item["path"]) == requested:
            folder = item
            break
    if folder is None:
        return JSONResponse(
            {
                "error": "path is not an unmapped folder",
                "description": "Requested path is not in the current unmapped-folder scan",
            },
            status_code=400,
        )

    query = folder["name"]
    results, source = await search_series(query)
    matches = [_metadata_match_payload(query, item) for item in results]
    matches.sort(key=lambda item: item["confidence"], reverse=True)
    return JSONResponse(
        {
            "rootFolderId": scan["rootFolderId"],
            "folder": folder,
            "query": query,
            "source": source,
            "matches": matches,
        }
    )


@router.post("/api/v1/rootfolder/{root_folder_id}/unmappedfolders/adopt")
async def api_v1_root_folder_adopt_unmapped(request: Request, root_folder_id: int):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return JSONResponse({"error": "expected an object body"}, status_code=400)

    path = str(payload.get("path") or "").strip()
    title = payload.get("title")
    if title is not None:
        title = str(title).strip()
    metadata_title = str(payload.get("metadataTitle") or "").strip() or None
    manga_updates_id = str(payload.get("mangaUpdatesId") or "").strip() or None
    cover_url = str(payload.get("coverUrl") or "").strip() or None
    status = str(payload.get("status") or "").strip() or None
    overview = str(payload.get("overview") or "").strip() or None
    metadata_source = str(payload.get("metadataSource") or "").strip() or None
    try:
        anilist_id = _optional_payload_int(payload, "anilistId")
        mal_id = _optional_payload_int(payload, "malId")
        total_volumes = _optional_payload_int(payload, "totalVolumes")
        total_chapters = _optional_payload_int(payload, "totalChapters")
        pub_year = _optional_payload_int(payload, "year")
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    monitored = payload.get("monitored")
    if monitored is not None and not isinstance(monitored, bool):
        return JSONResponse({"error": "monitored must be a boolean"}, status_code=400)
    monitored_bool = True if monitored is None else monitored
    quality_profile_id = payload.get("qualityProfileId")
    language_profile_id = payload.get("languageProfileId")
    if quality_profile_id is not None and (
        not isinstance(quality_profile_id, int) or isinstance(quality_profile_id, bool)
    ):
        return JSONResponse(
            {"error": "qualityProfileId must be an integer"}, status_code=400
        )
    if language_profile_id is not None and (
        not isinstance(language_profile_id, int) or isinstance(language_profile_id, bool)
    ):
        return JSONResponse(
            {"error": "languageProfileId must be an integer"}, status_code=400
        )

    result = adopt_unmapped_folder(
        root_folder_id,
        path,
        title=title,
        metadata_title=metadata_title,
        anilist_id=anilist_id,
        mal_id=mal_id,
        mu_id=manga_updates_id,
        cover_url=cover_url,
        status=status,
        description=overview,
        total_volumes=total_volumes,
        total_chapters=total_chapters,
        pub_year=pub_year,
        metadata_source=metadata_source,
        monitored=monitored_bool,
        quality_profile_id=quality_profile_id,
        language_profile_id=language_profile_id,
    )
    if result.ok:
        return JSONResponse(result.payload)
    body = {"error": result.error or "adoption failed"}
    if result.description:
        body["description"] = result.description
    return JSONResponse(body, status_code=result.status_code)
