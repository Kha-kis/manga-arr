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

from events import add_history
from helpers import resolve_root_folder_id
from routers.blocklist_ import clear_blocklist_entries
from files import build_chapter_label
from library_scan import adopt_unmapped_folder, scan_unmapped_root_folder
from metadata import search_series
from metadata_enrichment import _NON_STANDARD_STUB_EDITIONS
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
from routers.settings_ import (
    add_root_folder_entry,
    delete_root_folder_entry,
    set_default_root_folder_entry,
)
from routers.series_ import patch_series as _patch_series
from routers.system import APP_VERSION, TASKS, TASK_STATE, run_command as _run_command
from shared import (
    build_volume_label,
    from_json,
    get_cfg,
    get_db,
    quality_rank,
)
from volumes import create_volume_stubs

router = APIRouter()

_VALID_EDITION_TYPES = {
    "standard",
    "official_color",
    "colored",
    "omnibus",
    "deluxe",
    "digital",
    "raw",
    "special",
    "collector",
    "remaster",
    "unlocalized",
}


def _bool(value) -> bool:
    return bool(value)


def _bool_default_true(value) -> bool:
    return True if value is None else bool(value)


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return slug or "series"


def _dt_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _paged_list_response(
    payload: list[dict], page: int, page_size: int
) -> JSONResponse:
    total = len(payload)
    if page_size:
        page = max(page, 1)
        page_size = max(min(page_size, 250), 1)
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


def _delay_profile(row, tags: list[str]) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "order": row["order_num"],
        "enableUsenet": _bool(row["enable_usenet"]),
        "enableTorrent": _bool(row["enable_torrent"]),
        "usenetDelay": row["usenet_delay"] or 0,
        "torrentDelay": row["torrent_delay"] or 0,
        "bypassIfHighestQuality": _bool(row["bypass_if_highest_quality"]),
        "isDefault": _bool(row["is_default"]),
        "tags": tags,
    }


def _import_list(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "implementation": row["type"],
        "implementationName": row["type"],
        "configContract": row["type"],
        "enable": _bool(row["enabled"]),
        "qualityProfileId": row["quality_profile_id"],
        "rootFolderId": row["root_folder_id"],
        "monitorMode": row["monitor_mode"] or "all",
        "settings": from_json(row["settings"], {}) or {},
        "lastSync": row["last_sync"],
    }


def _import_list_exclusion(row) -> dict:
    return {
        "id": row["id"],
        "source": row["source"],
        "externalId": row["external_id"],
        "title": row["title"],
        "titleNormalized": row["title_normalized"],
        "reason": row["reason"],
        "addedAt": row["added_at"],
    }


def _quality_definition(row) -> dict:
    return {
        "quality": row["quality"],
        "title": row["title"],
        "minSize": row["min_size"],
        "maxSize": row["max_size"],
        "order": row["order_num"],
    }


def _indexer(row, tags: list[str]) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "implementation": row["type"],
        "implementationName": row["type"],
        "configContract": row["type"],
        "enable": _bool(row["enabled"]),
        "priority": row["priority"],
        "baseUrl": row["url"] or "",
        "categories": from_json(row["categories"], []) or [],
        "settings": from_json(row["settings"], {}) or {},
        "downloadClientId": row["client_id"],
        "minimumSeeders": row["min_seeders"] or 0,
        "seedRatio": row["seed_ratio"] or 0,
        "minimumSize": row["min_size_mb"] or 0,
        "maximumSize": row["max_size_mb"] or 0,
        "enableRss": _bool_default_true(row["use_rss"]),
        "enableAutomaticSearch": _bool_default_true(row["use_auto_search"]),
        "enableInteractiveSearch": _bool_default_true(
            row["use_interactive_search"]
        ),
        "parentProwlarrId": row["parent_prowlarr_id"],
        "prowlarrIndexerId": row["prowlarr_indexer_id"],
        "hasApiKey": bool(row["api_key"]),
        "tags": tags,
    }


def _download_client(row, tags: list[str]) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "implementation": row["type"],
        "implementationName": row["type"],
        "configContract": row["type"],
        "enable": _bool(row["enabled"]),
        "priority": row["priority"],
        "host": row["host"] or "",
        "port": row["port"],
        "useSsl": _bool(row["use_ssl"]),
        "urlBase": row["url_base"] or "",
        "username": row["username"] or "",
        "hasPassword": bool(row["password"]),
        "category": row["category"] or "",
        "postImportCategory": row["post_import_category"] or "",
        "removeCompletedDownloads": _bool(row["remove_completed"]),
        "removeFailedDownloads": _bool(row["remove_failed"]),
        "recentPriority": row["recent_priority"] or "last",
        "olderPriority": row["older_priority"] or "last",
        "initialState": row["initial_state"] or "normal",
        "sequentialOrder": _bool(row["sequential_order"]),
        "firstLastFirst": _bool(row["first_last_first"]),
        "contentLayout": row["content_layout"] or "original",
        "sourceId": row["source_id"],
        "downloadPath": row["download_path"],
        "mergeChapters": _bool_default_true(row["merge_chapters"]),
        "tags": tags,
    }


def _remote_path_mapping(row) -> dict:
    return {
        "id": row["id"],
        "host": row["host"] or "",
        "remotePath": row["remote_path"],
        "localPath": row["local_path"],
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


def _calendar_series(row) -> dict:
    return {
        "seriesId": row["id"],
        "seriesTitle": row["title"],
        "status": row["status"],
        "coverUrl": row["cover_url"],
        "totalVolumes": row["total_volumes"],
    }


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


def _payload_str(payload: dict, *keys: str, default: str = "") -> str:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return str(value).strip()
    return default


def _optional_non_negative_int(payload: dict, *keys: str) -> int | None:
    for key in keys:
        if key in payload:
            value = _optional_payload_int(payload, key)
            if value is not None and value < 0:
                raise ValueError(f"{key} must be zero or a positive integer")
            return value
    return None


def _default_profile_id(db, table: str) -> int | None:
    if table == "quality_profiles":
        row = db.execute(
            "SELECT id FROM quality_profiles ORDER BY is_default DESC, id LIMIT 1"
        ).fetchone()
    else:
        row = db.execute(
            "SELECT id FROM language_profiles ORDER BY id LIMIT 1"
        ).fetchone()
    return row["id"] if row else None


def _require_profile_id(
    db, table: str, profile_id: int | None, name: str
) -> int | None:
    if profile_id is None:
        return _default_profile_id(db, table)
    row = db.execute(f"SELECT 1 FROM {table} WHERE id=?", (profile_id,)).fetchone()
    if not row:
        raise ValueError(f"{name} not found")
    return profile_id


def _optional_bool_query(value: str | None, name: str) -> bool | None:
    if value in (None, ""):
        return None
    normalized = str(value).strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be a boolean")


def _json_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("1", "true", "yes", "on"):
            return True
        if normalized in ("0", "false", "no", "off"):
            return False
    return bool(value)


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


@router.post("/api/v1/rootfolder")
async def api_v1_create_root_folder(request: Request):
    data = await request.json()
    path = str(data.get("path") or "").strip()
    result = add_root_folder_entry(
        path,
        str(data.get("label") or data.get("name") or ""),
        _json_bool(data.get("isDefault", data.get("is_default")), False),
    )
    if result["status"] == "invalid_path":
        return JSONResponse(
            {"error": "path is required"},
            status_code=HTTP_400_BAD_REQUEST,
        )
    return JSONResponse(
        {
            "ok": True,
            "status": result["status"],
            "rootFolder": _root_folder(result["root_folder"]),
        }
    )


@router.post("/api/v1/rootfolder/{root_folder_id}/default")
async def api_v1_set_default_root_folder(root_folder_id: int):
    result = set_default_root_folder_entry(root_folder_id)
    if result["status"] == "not_found":
        return JSONResponse(
            {"error": "root folder not found"},
            status_code=HTTP_404_NOT_FOUND,
        )
    return JSONResponse(
        {"ok": True, "rootFolder": _root_folder(result["root_folder"])}
    )


@router.delete("/api/v1/rootfolder/{root_folder_id}")
async def api_v1_delete_root_folder(root_folder_id: int):
    result = delete_root_folder_entry(root_folder_id)
    if result["status"] == "not_found":
        return JSONResponse(
            {"error": "root folder not found"},
            status_code=HTTP_404_NOT_FOUND,
        )
    return JSONResponse({"ok": True, "id": root_folder_id})


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


@router.get("/api/v1/delayprofile")
async def api_v1_delay_profiles():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM delay_profiles ORDER BY order_num, id"
        ).fetchall()
        tag_rows = db.execute(
            "SELECT profile_id, tag FROM delay_profile_tags ORDER BY tag"
        ).fetchall()
        tags_by_profile: dict[int, list[str]] = {}
        for tag in tag_rows:
            tags_by_profile.setdefault(tag["profile_id"], []).append(tag["tag"])
        payload = [
            _delay_profile(row, tags_by_profile.get(row["id"], []))
            for row in rows
        ]
    return JSONResponse(payload)


@router.get("/api/v1/importlist")
async def api_v1_import_lists():
    with get_db() as db:
        rows = db.execute("SELECT * FROM import_lists ORDER BY name").fetchall()
        payload = [_import_list(row) for row in rows]
    return JSONResponse(payload)


@router.get("/api/v1/importlistexclusion")
async def api_v1_import_list_exclusions():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM import_list_exclusions"
            " ORDER BY source, title, external_id, id"
        ).fetchall()
        payload = [_import_list_exclusion(row) for row in rows]
    return JSONResponse(payload)


@router.get("/api/v1/qualitydefinition")
async def api_v1_quality_definitions():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM quality_definitions ORDER BY order_num, quality"
        ).fetchall()
        payload = [_quality_definition(row) for row in rows]
    return JSONResponse(payload)


@router.get("/api/v1/indexer")
async def api_v1_indexers():
    with get_db() as db:
        rows = db.execute("SELECT * FROM indexers ORDER BY priority, id").fetchall()
        tag_rows = db.execute(
            "SELECT indexer_id, tag FROM indexer_tags ORDER BY tag"
        ).fetchall()
        tags_by_indexer: dict[int, list[str]] = {}
        for tag in tag_rows:
            tags_by_indexer.setdefault(tag["indexer_id"], []).append(tag["tag"])
        payload = [_indexer(row, tags_by_indexer.get(row["id"], [])) for row in rows]
    return JSONResponse(payload)


@router.get("/api/v1/downloadclient")
async def api_v1_download_clients():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM download_clients ORDER BY priority, id"
        ).fetchall()
        tag_rows = db.execute(
            "SELECT client_id, tag FROM download_client_tags ORDER BY tag"
        ).fetchall()
        tags_by_client: dict[int, list[str]] = {}
        for tag in tag_rows:
            tags_by_client.setdefault(tag["client_id"], []).append(tag["tag"])
        payload = [
            _download_client(row, tags_by_client.get(row["id"], []))
            for row in rows
        ]
    return JSONResponse(payload)


@router.get("/api/v1/downloadclient/remotepathmapping")
async def api_v1_remote_path_mappings():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM remote_path_mappings ORDER BY id"
        ).fetchall()
        payload = [_remote_path_mapping(row) for row in rows]
    return JSONResponse(payload)


@router.get("/api/v1/tag")
async def api_v1_tags():
    with get_db() as db:
        tag_counts: dict[str, dict] = {}

        def _bucket(tag: str) -> dict:
            return tag_counts.setdefault(
                tag,
                {
                    "label": tag,
                    "seriesCount": 0,
                    "indexerCount": 0,
                    "delayProfileCount": 0,
                    "releaseProfileCount": 0,
                    "downloadClientCount": 0,
                },
            )

        sources = [
            ("series_tags", "seriesCount"),
            ("indexer_tags", "indexerCount"),
            ("delay_profile_tags", "delayProfileCount"),
            ("release_profile_tags", "releaseProfileCount"),
            ("download_client_tags", "downloadClientCount"),
        ]
        for table, field in sources:
            rows = db.execute(
                f"SELECT tag, COUNT(*) AS n FROM {table} GROUP BY tag"
            ).fetchall()
            for row in rows:
                if row["tag"]:
                    _bucket(row["tag"])[field] = row["n"]

        payload = [
            tag_counts[tag]
            for tag in sorted(tag_counts.keys(), key=lambda value: value.lower())
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
    return _paged_list_response(payload, page, pageSize)


@router.post("/api/v1/series")
async def api_v1_create_series(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "expected an object body"}, status_code=400)

    title = _payload_str(payload, "title")
    if not title:
        return JSONResponse({"error": "title is required"}, status_code=400)
    search_pattern = (
        _payload_str(payload, "searchPattern", "search_pattern") or title
    )
    edition_type = _payload_str(
        payload, "editionType", "edition_type", default="standard"
    )
    if edition_type not in _VALID_EDITION_TYPES:
        edition_type = "standard"

    try:
        anilist_id = _optional_non_negative_int(payload, "anilistId", "anilist_id")
        mal_id = _optional_non_negative_int(payload, "malId", "mal_id")
        total_volumes = _optional_non_negative_int(
            payload, "totalVolumes", "total_volumes"
        )
        total_chapters = _optional_non_negative_int(
            payload, "totalChapters", "total_chapters"
        )
        root_folder_id = _optional_non_negative_int(
            payload, "rootFolderId", "root_folder_id"
        )
        pub_year = _optional_non_negative_int(payload, "year", "pubYear", "pub_year")
        quality_profile_id = _optional_non_negative_int(
            payload, "qualityProfileId", "quality_profile_id"
        )
        language_profile_id = _optional_non_negative_int(
            payload, "languageProfileId", "language_profile_id"
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    mu_id = _payload_str(payload, "mangaUpdatesId", "muId", "mu_id")
    cover_url = _payload_str(payload, "coverUrl", "cover_url")
    status = _payload_str(payload, "status")
    description = _payload_str(payload, "overview", "description")
    monitored = _json_bool(payload.get("monitored"), True)
    enabled = _json_bool(payload.get("enabled"), True)
    monitor_mode = _payload_str(payload, "monitorMode", "monitor_mode")
    if monitor_mode not in {"all", "future", "missing", "existing", "none"}:
        monitor_mode = "missing" if monitored else "none"
    vol_count_source = (
        "anilist" if anilist_id else ("mangaupdates" if mu_id else "manual")
    )

    with get_db() as db:
        if anilist_id:
            existing = db.execute(
                "SELECT id FROM series WHERE anilist_id=? AND edition_type=?"
                " AND deleted_at IS NULL",
                (anilist_id, edition_type),
            ).fetchone()
        else:
            existing = db.execute(
                "SELECT id FROM series WHERE anilist_id IS NULL AND title=?"
                " AND edition_type=? AND deleted_at IS NULL",
                (title, edition_type),
            ).fetchone()
        if not existing and mu_id:
            existing = db.execute(
                "SELECT id FROM series WHERE mu_id=? AND edition_type=?"
                " AND deleted_at IS NULL",
                (mu_id, edition_type),
            ).fetchone()
        if existing:
            row = db.execute(
                _series_base_query()
                + """
                WHERE s.id=? AND s.deleted_at IS NULL
                GROUP BY s.id
                """,
                (existing["id"],),
            ).fetchone()
            return JSONResponse(
                {
                    "ok": True,
                    "status": "exists",
                    "series": _series_payload(db, row),
                }
            )

        resolved_root_folder_id = resolve_root_folder_id(
            db, preferred_id=root_folder_id
        )
        if resolved_root_folder_id is None:
            return JSONResponse(
                {
                    "error": "No root folder configured. Add one in Settings "
                    "before adding series."
                },
                status_code=HTTP_400_BAD_REQUEST,
            )
        try:
            quality_profile_id = _require_profile_id(
                db, "quality_profiles", quality_profile_id, "qualityProfileId"
            )
            language_profile_id = _require_profile_id(
                db, "language_profiles", language_profile_id, "languageProfileId"
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        cur = db.execute(
            "INSERT INTO series(title, search_pattern, anilist_id, mal_id, mu_id,"
            " cover_url, status, description, total_volumes, total_chapters,"
            " root_folder_id, pub_year, edition_type, vol_count_source, enabled,"
            " monitored, monitor_mode, quality_profile_id, language_profile_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                title,
                search_pattern,
                anilist_id or None,
                mal_id or None,
                mu_id or None,
                cover_url,
                status,
                description,
                total_volumes or None,
                total_chapters or None,
                resolved_root_folder_id,
                pub_year or None,
                edition_type,
                vol_count_source,
                1 if enabled else 0,
                1 if monitored else 0,
                monitor_mode,
                quality_profile_id,
                language_profile_id,
            ),
        )
        series_id = cur.lastrowid
        if (
            total_volumes
            and total_volumes > 0
            and edition_type not in _NON_STANDARD_STUB_EDITIONS
        ):
            create_volume_stubs(db, series_id, total_volumes)
        add_history(
            db,
            "series_added",
            series_id,
            title,
            "",
            source_title=title,
            data={"total_volumes": total_volumes or 0, "status": status},
        )
        row = db.execute(
            _series_base_query()
            + """
            WHERE s.id=? AND s.deleted_at IS NULL
            GROUP BY s.id
            """,
            (series_id,),
        ).fetchone()

        return JSONResponse(
            {
                "ok": True,
                "status": "created",
                "series": _series_payload(db, row),
            }
        )


@router.get("/api/v1/series/lookup")
async def api_v1_series_lookup(term: str = ""):
    query = term.strip()
    if not query:
        return JSONResponse({"error": "term is required"}, status_code=400)

    results, _source = await search_series(query)
    matches = [_metadata_match_payload(query, item) for item in results]
    matches.sort(key=lambda item: item["confidence"], reverse=True)
    return JSONResponse(matches)


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


@router.delete("/api/v1/series/{series_id}")
async def api_v1_delete_series(series_id: int):
    with get_db() as db:
        row = db.execute(
            "SELECT title, deleted_at FROM series WHERE id=?",
            (series_id,),
        ).fetchone()
        if not row:
            return JSONResponse(
                {"error": "series not found"},
                status_code=HTTP_404_NOT_FOUND,
            )
        if row["deleted_at"] is None:
            title = row["title"] or ""
            db.execute(
                "UPDATE series SET deleted_at=CURRENT_TIMESTAMP,"
                " deletion_reason=? WHERE id=?",
                ("user_action", series_id),
            )
            add_history(
                db, "series_soft_deleted", None, title, "", source_title=title
            )
    return JSONResponse({"ok": True, "id": series_id})


@router.post("/api/v1/series/{series_id}/restore")
async def api_v1_restore_series(series_id: int):
    with get_db() as db:
        row = db.execute(
            "SELECT title, deleted_at FROM series WHERE id=?",
            (series_id,),
        ).fetchone()
        if not row:
            return JSONResponse(
                {"error": "series not found"},
                status_code=HTTP_404_NOT_FOUND,
            )
        if row["deleted_at"] is not None:
            title = row["title"] or ""
            db.execute(
                "UPDATE series SET deleted_at=NULL, deletion_reason=NULL WHERE id=?",
                (series_id,),
            )
            add_history(db, "series_restored", None, title, "", source_title=title)
    return JSONResponse({"ok": True, "id": series_id})


@router.get("/api/v1/queue")
async def api_v1_queue(
    seriesId: int = 0,
    status: str = "",
    trackedDownloadStatus: str = "",
    queueType: str = "",
    page: int = 0,
    pageSize: int = 0,
):
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
                    "queueType": "grabbed",
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
                    "queueType": "import",
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
                    "queueType": "pending",
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
    if seriesId:
        payload = [row for row in payload if row["seriesId"] == seriesId]
    if status:
        status_key = status.strip().lower()
        payload = [row for row in payload if row["status"].lower() == status_key]
    if trackedDownloadStatus:
        tracked_key = trackedDownloadStatus.strip().lower()
        payload = [
            row for row in payload
            if row["trackedDownloadStatus"].lower() == tracked_key
        ]
    if queueType:
        type_key = queueType.strip().lower()
        if type_key not in {"grabbed", "import", "pending"}:
            return JSONResponse(
                {"error": "queueType must be grabbed, import, or pending"},
                status_code=HTTP_400_BAD_REQUEST,
            )
        payload = [row for row in payload if row["queueType"] == type_key]

    return _paged_list_response(payload, page, pageSize)


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
async def api_v1_blocklist(
    seriesId: int = 0,
    protocol: str = "",
    indexer: str = "",
    term: str = "",
    page: int = 0,
    pageSize: int = 0,
):
    ttl_days = max(0, int(get_cfg("blocklist_ttl_days", "90") or "90"))
    where_parts: list[str] = []
    params: list = []
    if seriesId:
        where_parts.append("bl.series_id=?")
        params.append(seriesId)
    if protocol:
        where_parts.append("LOWER(COALESCE(bl.protocol, ''))=?")
        params.append(protocol.strip().lower())
    if indexer:
        where_parts.append("LOWER(COALESCE(bl.indexer, ''))=?")
        params.append(indexer.strip().lower())
    if term:
        like = f"%{term.strip()}%"
        where_parts.append(
            "(bl.torrent_name LIKE ? OR bl.torrent_url LIKE ? OR s.title LIKE ?)"
        )
        params.extend([like, like, like])
    where = "WHERE " + " AND ".join(where_parts) if where_parts else ""
    with get_db() as db:
        rows = db.execute(
            f"""
            SELECT bl.*, s.title AS series_title
            FROM blocklist bl
            LEFT JOIN series s ON s.id=bl.series_id
            {where}
            ORDER BY bl.added_at DESC
            """,
            params,
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
    return _paged_list_response(payload, page, pageSize)


@router.delete("/api/v1/blocklist")
async def api_v1_clear_blocklist():
    result = clear_blocklist_entries()
    return JSONResponse({"ok": True, "deleted": result["deleted"]})


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
async def api_v1_wanted(
    seriesId: int = 0,
    term: str = "",
    page: int = 0,
    pageSize: int = 0,
):
    where_parts = [
        "v.status='wanted'",
        "COALESCE(v.monitored, 1)=1",
        "COALESCE(s.monitored, 1)=1",
        "COALESCE(s.enabled, 1)=1",
        "s.deleted_at IS NULL",
    ]
    params: list = []
    if seriesId:
        where_parts.append("v.series_id=?")
        params.append(seriesId)
    if term:
        like = f"%{term.strip()}%"
        where_parts.append("(s.title LIKE ? OR s.search_pattern LIKE ?)")
        params.extend([like, like])
    where = "WHERE " + " AND ".join(where_parts)
    with get_db() as db:
        rows = db.execute(
            f"""
            SELECT v.id, v.series_id, s.title AS series_title, v.volume_num,
                   v.chapter_num, v.vol_range_start, v.vol_range_end,
                   v.pack_type, v.monitored, s.monitored AS series_monitored,
                   s.enabled AS series_enabled
            FROM volumes v
            JOIN series s ON s.id=v.series_id
            {where}
            ORDER BY s.title COLLATE NOCASE, v.volume_num
            """,
            params,
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
    return _paged_list_response(payload, page, pageSize)


@router.get("/api/v1/wanted/cutoff")
async def api_v1_wanted_cutoff(
    seriesId: int = 0,
    term: str = "",
    page: int = 0,
    pageSize: int = 0,
):
    global_cutoff = get_cfg("quality_cutoff", "")
    where_parts = [
        "v.status = 'downloaded'",
        "s.monitored = 1",
        "s.deleted_at IS NULL",
    ]
    params: list = []
    if seriesId:
        where_parts.append("v.series_id=?")
        params.append(seriesId)
    if term:
        like = f"%{term.strip()}%"
        where_parts.append("(s.title LIKE ? OR s.search_pattern LIKE ?)")
        params.extend([like, like])
    where = "WHERE " + " AND ".join(where_parts)
    with get_db() as db:
        rows = db.execute(
            f"""
            SELECT v.id, v.series_id, v.volume_num, v.quality, v.import_path,
                   s.title AS series_title, s.quality_cutoff, s.quality_profile_id,
                   qp.cutoff AS profile_cutoff, v.grabbed_at
            FROM volumes v
            JOIN series s ON s.id = v.series_id
            LEFT JOIN quality_profiles qp ON qp.id = s.quality_profile_id
            {where}
            ORDER BY s.title COLLATE NOCASE, v.volume_num
            """,
            params,
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
    return _paged_list_response(payload, page, pageSize)


@router.get("/api/v1/calendar")
async def api_v1_calendar():
    with get_db() as db:
        releasing_rows = db.execute(
            """
            SELECT s.id, s.title, s.cover_url, s.status, s.total_volumes,
                   COUNT(v.id) AS volume_count,
                   SUM(CASE WHEN v.status='downloaded' THEN 1 ELSE 0 END) AS have,
                   SUM(CASE WHEN v.status IN ('wanted','grabbed') THEN 1 ELSE 0 END) AS missing
            FROM series s
            JOIN volumes v ON v.series_id=s.id AND v.volume_num IS NOT NULL
            WHERE UPPER(s.status)='RELEASING'
              AND COALESCE(s.monitored, 1)=1
              AND COALESCE(s.enabled, 1)=1
              AND s.deleted_at IS NULL
            GROUP BY s.id
            HAVING missing > 0
            ORDER BY volume_count DESC, s.title COLLATE NOCASE
            """
        ).fetchall()

        releasing = []
        for row in releasing_rows:
            vols = db.execute(
                """
                SELECT volume_num, status
                FROM volumes
                WHERE series_id=? AND volume_num IS NOT NULL
                ORDER BY volume_num
                """,
                (row["id"],),
            ).fetchall()
            item = _calendar_series(row)
            item.update(
                {
                    "have": row["have"] or 0,
                    "missing": row["missing"] or 0,
                    "wantedVolumes": [
                        v["volume_num"] for v in vols if v["status"] == "wanted"
                    ],
                    "grabbedVolumes": [
                        v["volume_num"] for v in vols if v["status"] == "grabbed"
                    ],
                }
            )
            releasing.append(item)

        upcoming_rows = db.execute(
            """
            SELECT s.id, s.title, s.cover_url, s.status, s.total_volumes, s.pub_year
            FROM series s
            WHERE UPPER(s.status)='NOT_YET_RELEASED'
              AND COALESCE(s.monitored, 1)=1
              AND COALESCE(s.enabled, 1)=1
              AND s.deleted_at IS NULL
            ORDER BY COALESCE(s.pub_year, 9999), s.title COLLATE NOCASE
            """
        ).fetchall()
        upcoming = []
        for row in upcoming_rows:
            item = _calendar_series(row)
            item["year"] = row["pub_year"]
            upcoming.append(item)

        hiatus_rows = db.execute(
            """
            SELECT s.id, s.title, s.cover_url, s.status, s.total_volumes,
                   SUM(CASE WHEN v.status='downloaded' THEN 1 ELSE 0 END) AS have,
                   COUNT(v.id) AS volume_count
            FROM series s
            JOIN volumes v ON v.series_id=s.id AND v.volume_num IS NOT NULL
            WHERE UPPER(s.status) IN ('HIATUS','ON_HIATUS')
              AND COALESCE(s.monitored, 1)=1
              AND COALESCE(s.enabled, 1)=1
              AND s.deleted_at IS NULL
            GROUP BY s.id
            ORDER BY s.title COLLATE NOCASE
            """
        ).fetchall()
        hiatus = []
        for row in hiatus_rows:
            item = _calendar_series(row)
            item.update(
                {
                    "have": row["have"] or 0,
                    "volumeCount": row["volume_count"] or 0,
                }
            )
            hiatus.append(item)

    return JSONResponse(
        {
            "releasing": releasing,
            "upcoming": upcoming,
            "hiatus": hiatus,
        }
    )


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
