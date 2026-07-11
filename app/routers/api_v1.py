"""Sonarr-style API v1 endpoints.

These endpoints are intentionally conservative: they expose stable JSON
contracts for external automation without replacing Mangarr's existing
workflow-specific `/api/*` actions.
"""
from __future__ import annotations

import difflib
import json
import os
import platform
import re
import shutil
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO

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
from rename_plan import (
    build_library_rename_preview,
    build_series_rename_preview,
    execute_library_rename,
    execute_series_rename,
)
from routers.history_ import (
    clear_failed_history_entries,
    delete_history_entry,
    mark_history_failed,
)
from routers.import_lists import _sync_all_lists, _sync_list
from routers.import_ import (
    clear_inactive_import_queue_entries,
    dismiss_import_queue_entry,
    retry_import_queue_entry,
    skip_import_queue_entry,
)
from routers.health_ import build_health_payload
from routers.language_profiles import SUPPORTED_LANGUAGES
from routers.notification_connections import (
    CONNECTION_TYPES as NOTIFICATION_CONNECTION_TYPES,
    _encrypt_secret_fields as _encrypt_notification_secret_fields,
    _secret_keys_for as _notification_secret_keys_for,
)
from routers.queue_ import dismiss_pending_release, reset_grabbed_volume
from routers.settings_ import (
    add_root_folder_entry,
    delete_root_folder_entry,
    set_default_root_folder_entry,
)
from routers.series_ import patch_series as _patch_series
import routers.system as system_router
from routers.system import APP_VERSION, TASKS, TASK_STATE, run_command as _run_command
from shared import (
    build_volume_label,
    from_json,
    get_cfg,
    get_db,
    quality_rank,
)
from security import encrypt_if_cipher_available
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


def _query_int(request: Request, name: str, default: int) -> int:
    try:
        return int(request.query_params.get(name, default))
    except (TypeError, ValueError):
        return default


def _query_bool(request: Request, *names: str) -> bool | None:
    for name in names:
        if name not in request.query_params:
            continue
        value = request.query_params.get(name)
        if value is None:
            return None
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return None


def _filtered_config_list_response(
    request: Request,
    payload: list[dict],
    *,
    text_fields: tuple[str, ...],
    sortable_fields: set[str],
    default_sort_key: str = "name",
) -> JSONResponse:
    query = (
        request.query_params.get("term")
        or request.query_params.get("query")
        or request.query_params.get("name")
        or ""
    ).strip().lower()
    implementation = (
        request.query_params.get("implementation")
        or request.query_params.get("type")
        or ""
    ).strip().lower()
    tag = (request.query_params.get("tag") or "").strip().lower()
    enabled = _query_bool(request, "enabled", "enable")

    def _matches(item: dict) -> bool:
        if query and not any(
            query in str(item.get(field) or "").lower()
            for field in text_fields
        ):
            return False
        if implementation and implementation != str(
            item.get("implementation") or ""
        ).lower():
            return False
        item_enabled = item.get("enable", item.get("enabled"))
        if enabled is not None and item_enabled is not enabled:
            return False
        if tag and tag not in {
            str(value).lower() for value in item.get("tags", []) or []
        }:
            return False
        return True

    payload = [item for item in payload if _matches(item)]
    sort_key = request.query_params.get("sortKey") or default_sort_key
    if sort_key not in sortable_fields:
        sort_key = default_sort_key if default_sort_key in sortable_fields else "name"
    reverse = (request.query_params.get("sortDirection") or "").lower() == "desc"

    def _sort_value(item: dict):
        value = item.get(sort_key)
        if isinstance(value, (int, float, bool)):
            return (0, value)
        return (1, str(value or "").lower())

    payload.sort(key=_sort_value, reverse=reverse)
    return _paged_list_response(
        payload,
        _query_int(request, "page", 1),
        _query_int(request, "pageSize", 0),
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


def _quality_profile_by_id(db, profile_id: int) -> dict | None:
    row = db.execute(
        "SELECT * FROM quality_profiles WHERE id=?",
        (profile_id,),
    ).fetchone()
    return _quality_profile(row) if row else None


def _language_profile(row, default_id: int | None) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "languages": from_json(row["languages"], []) or [],
        "allowAny": _bool(row["allow_any"]),
        "isDefault": row["id"] == default_id,
    }


def _language_profile_by_id(db, profile_id: int) -> dict | None:
    default_id = _default_language_profile_id(db)
    row = db.execute(
        "SELECT * FROM language_profiles WHERE id=?",
        (profile_id,),
    ).fetchone()
    return _language_profile(row, default_id) if row else None


def _release_profile_by_id(db, profile_id: int) -> dict | None:
    row = db.execute(
        "SELECT * FROM release_profiles WHERE id=?",
        (profile_id,),
    ).fetchone()
    if not row:
        return None
    tags = [
        tag["tag"]
        for tag in db.execute(
            "SELECT tag FROM release_profile_tags"
            " WHERE profile_id=? ORDER BY tag",
            (profile_id,),
        ).fetchall()
    ]
    return _release_profile(row, tags)


def _delay_profile_by_id(db, profile_id: int) -> dict | None:
    row = db.execute(
        "SELECT * FROM delay_profiles WHERE id=?",
        (profile_id,),
    ).fetchone()
    if not row:
        return None
    tags = [
        tag["tag"]
        for tag in db.execute(
            "SELECT tag FROM delay_profile_tags"
            " WHERE profile_id=? ORDER BY tag",
            (profile_id,),
        ).fetchall()
    ]
    return _delay_profile(row, tags)


def _custom_format_by_id(db, format_id: int) -> dict | None:
    row = db.execute(
        "SELECT * FROM custom_formats WHERE id=?",
        (format_id,),
    ).fetchone()
    if not row:
        return None
    scores = [
        {
            "qualityProfileId": score["profile_id"],
            "score": score["score"],
        }
        for score in db.execute(
            "SELECT profile_id, score FROM quality_profile_custom_formats"
            " WHERE format_id=? ORDER BY profile_id",
            (format_id,),
        ).fetchall()
    ]
    return _custom_format(row, scores)


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


def _import_list_by_id(db, list_id: int) -> dict | None:
    row = db.execute(
        "SELECT * FROM import_lists WHERE id=?",
        (list_id,),
    ).fetchone()
    return _import_list(row) if row else None


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


def _import_list_exclusion_by_id(db, exclusion_id: int) -> dict | None:
    row = db.execute(
        "SELECT * FROM import_list_exclusions WHERE id=?",
        (exclusion_id,),
    ).fetchone()
    return _import_list_exclusion(row) if row else None


def _notification_connection(row) -> dict:
    settings = from_json(row["settings"], {}) or {}
    if not isinstance(settings, dict):
        settings = {}
    secret_keys = set(_notification_secret_keys_for(row["type"]))
    redacted_settings = {
        key: value for key, value in settings.items() if key not in secret_keys
    }
    has_secret_settings = {
        key: bool(settings.get(key))
        for key in sorted(secret_keys)
        if key in settings
    }
    return {
        "id": row["id"],
        "name": row["name"],
        "implementation": row["type"],
        "implementationName": row["type"],
        "configContract": row["type"],
        "enable": _bool(row["enabled"]),
        "settings": redacted_settings,
        "hasSecretSettings": has_secret_settings,
        "onGrab": _bool(row["on_grab"]),
        "onDownload": _bool(row["on_download"]),
        "onUpgrade": _bool(row["on_upgrade"]),
        "onSeriesAdd": _bool(row["on_series_add"]),
        "onHealthIssue": _bool(row["on_health_issue"]),
        "onHealthRestored": _bool(row["on_health_restored"]),
    }


def _notification_connection_by_id(db, connection_id: int) -> dict | None:
    row = db.execute(
        "SELECT * FROM notification_connections WHERE id=?",
        (connection_id,),
    ).fetchone()
    return _notification_connection(row) if row else None


def _quality_definition(row) -> dict:
    return {
        "quality": row["quality"],
        "title": row["title"],
        "minSize": row["min_size"],
        "maxSize": row["max_size"],
        "order": row["order_num"],
    }


def _quality_definition_by_quality(db, quality: str) -> dict | None:
    row = db.execute(
        "SELECT * FROM quality_definitions WHERE quality=?",
        (quality,),
    ).fetchone()
    return _quality_definition(row) if row else None


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


def _indexer_by_id(db, indexer_id: int) -> dict | None:
    row = db.execute(
        "SELECT * FROM indexers WHERE id=?",
        (indexer_id,),
    ).fetchone()
    if not row:
        return None
    tags = [
        tag["tag"]
        for tag in db.execute(
            "SELECT tag FROM indexer_tags WHERE indexer_id=? ORDER BY tag",
            (indexer_id,),
        ).fetchall()
    ]
    return _indexer(row, tags)


def _replace_indexer_tags(db, indexer_id: int, tags: list[str]) -> None:
    db.execute("DELETE FROM indexer_tags WHERE indexer_id=?", (indexer_id,))
    for tag in tags:
        db.execute(
            "INSERT OR IGNORE INTO indexer_tags(indexer_id, tag) VALUES(?, ?)",
            (indexer_id, tag),
        )


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


def _download_client_by_id(db, client_id: int) -> dict | None:
    row = db.execute(
        "SELECT * FROM download_clients WHERE id=?",
        (client_id,),
    ).fetchone()
    if not row:
        return None
    tags = [
        tag["tag"]
        for tag in db.execute(
            "SELECT tag FROM download_client_tags"
            " WHERE client_id=? ORDER BY tag",
            (client_id,),
        ).fetchall()
    ]
    return _download_client(row, tags)


def _replace_download_client_tags(db, client_id: int, tags: list[str]) -> None:
    db.execute("DELETE FROM download_client_tags WHERE client_id=?", (client_id,))
    for tag in tags:
        db.execute(
            "INSERT OR IGNORE INTO download_client_tags(client_id, tag)"
            " VALUES(?, ?)",
            (client_id, tag),
        )


def _remote_path_mapping(row) -> dict:
    return {
        "id": row["id"],
        "host": row["host"] or "",
        "remotePath": row["remote_path"],
        "localPath": row["local_path"],
    }


def _remote_path_mapping_by_id(db, mapping_id: int) -> dict | None:
    row = db.execute(
        "SELECT * FROM remote_path_mappings WHERE id=?",
        (mapping_id,),
    ).fetchone()
    return _remote_path_mapping(row) if row else None


_TAG_TABLES: tuple[tuple[str, str, str], ...] = (
    ("series_tags", "series_id", "seriesCount"),
    ("indexer_tags", "indexer_id", "indexerCount"),
    ("delay_profile_tags", "profile_id", "delayProfileCount"),
    ("release_profile_tags", "profile_id", "releaseProfileCount"),
    ("download_client_tags", "client_id", "downloadClientCount"),
)


def _empty_tag(label: str) -> dict:
    return {
        "label": label,
        "seriesCount": 0,
        "indexerCount": 0,
        "delayProfileCount": 0,
        "releaseProfileCount": 0,
        "downloadClientCount": 0,
    }


def _tag_by_label(db, label: str) -> dict | None:
    tag = _empty_tag(label)
    found = False
    for table, _owner_column, field in _TAG_TABLES:
        row = db.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE tag=?",
            (label,),
        ).fetchone()
        count = row["n"] if row else 0
        if count:
            found = True
        tag[field] = count
    return tag if found else None


def _replace_legacy_series_json_tag(
    db,
    old_tag: str,
    *,
    new_tag: str | None,
) -> None:
    rows = db.execute(
        "SELECT id, tags FROM series WHERE tags IS NOT NULL AND tags != ''"
    ).fetchall()
    for row in rows:
        tags = from_json(row["tags"], []) or []
        if not isinstance(tags, list):
            continue
        changed = False
        updated: list[str] = []
        seen: set[str] = set()
        for item in tags:
            tag = str(item).strip()
            if not tag:
                continue
            if tag == old_tag:
                changed = True
                tag = new_tag or ""
            if tag and tag not in seen:
                updated.append(tag)
                seen.add(tag)
        if changed:
            db.execute(
                "UPDATE series SET tags=? WHERE id=?",
                (json.dumps(updated), row["id"]),
            )


def _rename_tag_everywhere(db, old_tag: str, new_tag: str) -> None:
    for table, owner_column, _field in _TAG_TABLES:
        db.execute(
            f"INSERT OR IGNORE INTO {table}({owner_column}, tag)"
            f" SELECT {owner_column}, ? FROM {table} WHERE tag=?",
            (new_tag, old_tag),
        )
        db.execute(f"DELETE FROM {table} WHERE tag=?", (old_tag,))
    _replace_legacy_series_json_tag(db, old_tag, new_tag=new_tag)


def _delete_tag_everywhere(db, tag: str) -> None:
    for table, _owner_column, _field in _TAG_TABLES:
        db.execute(f"DELETE FROM {table} WHERE tag=?", (tag,))
    _replace_legacy_series_json_tag(db, tag, new_tag=None)


def _root_folder(row) -> dict:
    path = row["path"]
    disk = _disk_space_entry(path)
    return {
        "id": row["id"],
        "path": path,
        "name": row["label"] or path,
        "label": row["label"],
        "isDefault": _bool(row["is_default"]),
        "unmappedFolders": [],
        "totalSpace": disk["totalSpace"],
        "freeSpace": disk["freeSpace"],
        "isAvailable": disk["isAvailable"],
    }


def _disk_space_entry(path: str) -> dict:
    payload = {
        "path": path,
        "totalSpace": None,
        "freeSpace": None,
        "isAvailable": False,
    }
    try:
        usage = shutil.disk_usage(path)
        payload.update(
            {
                "totalSpace": usage.total,
                "freeSpace": usage.free,
                "isAvailable": True,
            }
        )
    except OSError:
        pass
    return payload


def _safe_backup_filename(filename: str) -> bool:
    safe_name = os.path.basename(filename or "")
    return bool(safe_name and safe_name == filename and safe_name.endswith(".zip"))


def _backup_entry(filename: str) -> dict | None:
    if not _safe_backup_filename(filename):
        return None
    fpath = os.path.join(system_router.BACKUP_DIR, filename)
    try:
        stat = os.stat(fpath)
    except OSError:
        return None
    mtime = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
    return {
        "id": filename,
        "name": filename,
        "filename": filename,
        "path": fpath,
        "type": "manual",
        "size": stat.st_size,
        "sizeHuman": system_router._fmt_bytes(stat.st_size),
        "time": mtime.isoformat(),
    }


def _list_backup_entries() -> list[dict]:
    os.makedirs(system_router.BACKUP_DIR, exist_ok=True)
    try:
        filenames = sorted(os.listdir(system_router.BACKUP_DIR), reverse=True)
    except OSError:
        return []
    backups = []
    for filename in filenames:
        entry = _backup_entry(filename)
        if entry:
            backups.append(entry)
    return backups


def _create_backup_file() -> dict:
    os.makedirs(system_router.BACKUP_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"mangarr_backup_{ts}.zip"
    saved_path = os.path.join(system_router.BACKUP_DIR, filename)

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if os.path.exists(system_router.DB_PATH):
            zf.write(system_router.DB_PATH, arcname="manga_arr.db")
    buf.seek(0)
    zip_bytes = buf.read()
    with open(saved_path, "wb") as f:
        f.write(zip_bytes)
    entry = _backup_entry(filename)
    if entry is None:
        raise OSError("backup was not written")
    return entry


def _system_task(task: dict) -> dict:
    state = TASK_STATE.get(task["key"], {})
    return {
        "name": task["key"],
        "displayName": task["name"],
        "interval": task["interval"],
        "manual": _bool(task["manual"]),
        "lastRun": _iso_or_none(state.get("last_run")),
        "nextRun": _iso_or_none(state.get("next_run")),
    }


def _event_log_record(row) -> dict:
    return {
        "id": row["id"],
        "eventType": row["event_type"],
        "seriesId": row["series_id"],
        "seriesTitle": row["series_title"],
        "message": row["message"],
        "date": row["created_at"],
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


def _normalize_import_list_title(title: str | None) -> str:
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def _optional_non_negative_int(payload: dict, *keys: str) -> int | None:
    for key in keys:
        if key in payload:
            value = _optional_payload_int(payload, key)
            if value is not None and value < 0:
                raise ValueError(f"{key} must be zero or a positive integer")
            return value
    return None


def _payload_int(payload: dict, key: str, default: int) -> int:
    if key not in payload:
        return default
    value = _optional_payload_int(payload, key)
    return default if value is None else value


def _payload_bool_alias(payload: dict, keys: tuple[str, ...], default: bool) -> bool:
    for key in keys:
        if key in payload:
            return _json_bool(payload.get(key), default)
    return default


def _payload_non_negative_alias(
    payload: dict, keys: tuple[str, ...], default: int
) -> int:
    value = _optional_non_negative_int(payload, *keys)
    return default if value is None else value


def _payload_non_negative_float_alias(
    payload: dict, keys: tuple[str, ...], default: float
) -> float:
    for key in keys:
        if key not in payload:
            continue
        value = payload.get(key)
        if value in (None, ""):
            return default
        if isinstance(value, bool):
            raise ValueError(f"{key} must be zero or a positive number")
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be zero or a positive number") from exc
        if parsed < 0:
            raise ValueError(f"{key} must be zero or a positive number")
        return parsed
    return default


def _payload_optional_fk_alias(payload: dict, keys: tuple[str, ...]) -> int | None:
    for key in keys:
        if key not in payload:
            continue
        value = payload.get(key)
        if value in (None, ""):
            return None
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{key} must be zero or a positive integer")
        return value or None
    return None


def _payload_json_object(payload: dict, key: str, default: dict) -> str:
    if key not in payload:
        return json.dumps(default)
    value = payload.get(key)
    if value in (None, ""):
        return json.dumps(default)
    if isinstance(value, str):
        value = from_json(value, None)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return json.dumps(value)


def _validate_optional_fk(db, table: str, value: int | None, label: str) -> None:
    if value is None:
        return
    row = db.execute(f"SELECT 1 FROM {table} WHERE id=?", (value,)).fetchone()
    if not row:
        raise ValueError(f"{label} not found")


def _payload_quality_list(payload: dict, key: str, default: list[str]) -> str:
    if key not in payload:
        return json.dumps(default)
    value = payload.get(key)
    if value in (None, ""):
        return json.dumps(default)
    if isinstance(value, str):
        parsed = from_json(value, None)
        if not isinstance(parsed, list):
            raise ValueError(f"{key} must be a list of quality names")
        value = parsed
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list of quality names")
    qualities = [str(item).strip() for item in value if str(item).strip()]
    return json.dumps(qualities)


def _payload_language_list(payload: dict, key: str, default: list[str]) -> str:
    if key not in payload:
        return json.dumps(default)
    value = payload.get(key)
    if value in (None, ""):
        return json.dumps(default)
    if isinstance(value, str):
        raw_codes = [item.strip().lower() for item in value.split(",")]
    elif isinstance(value, list):
        raw_codes = [str(item).strip().lower() for item in value]
    else:
        raise ValueError(f"{key} must be a list of language codes")
    codes = [code for code in raw_codes if code in SUPPORTED_LANGUAGES]
    return json.dumps(codes if codes else ["any"])


def _payload_json_list(payload: dict, key: str, default: list) -> str:
    if key not in payload:
        return json.dumps(default)
    value = payload.get(key)
    if value in (None, ""):
        return json.dumps(default)
    if isinstance(value, str):
        value = from_json(value, None)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    return json.dumps(value)


def _payload_category_list(payload: dict, key: str = "categories") -> str:
    if key not in payload:
        return json.dumps([7000, 7010, 7020])
    value = payload.get(key)
    if value in (None, ""):
        return json.dumps([7000, 7010, 7020])
    if isinstance(value, str):
        parsed = from_json(value, None)
        if isinstance(parsed, list):
            value = parsed
        else:
            value = value.split(",")
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list of category ids")
    categories: list[int] = []
    seen: set[int] = set()
    for item in value:
        if isinstance(item, bool):
            raise ValueError(f"{key} entries must be category ids")
        try:
            category = int(str(item).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} entries must be category ids") from exc
        if category < 0:
            raise ValueError(f"{key} entries must be category ids")
        if category not in seen:
            categories.append(category)
            seen.add(category)
    return json.dumps(categories)


def _payload_score_list(payload: dict, key: str = "qualityProfileScores") -> list[dict]:
    value = payload.get(key)
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = from_json(value, None)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    scores: list[dict] = []
    seen: set[int] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"{key} entries must be objects")
        profile_id = item.get("qualityProfileId", item.get("profileId"))
        score = item.get("score", 0)
        if (
            not isinstance(profile_id, int)
            or isinstance(profile_id, bool)
            or not isinstance(score, int)
            or isinstance(score, bool)
        ):
            raise ValueError(
                f"{key} entries require integer qualityProfileId and score"
            )
        if profile_id in seen:
            raise ValueError(f"{key} entries must not repeat a qualityProfileId")
        seen.add(profile_id)
        scores.append({"qualityProfileId": profile_id, "score": score})
    return scores


def _payload_tag_list(payload: dict, key: str = "tags") -> list[str]:
    value = payload.get(key)
    if value in (None, ""):
        return []
    if isinstance(value, str):
        raw_tags = value.split(",")
    elif isinstance(value, list):
        raw_tags = value
    else:
        raise ValueError(f"{key} must be a list of tag names")
    tags: list[str] = []
    seen: set[str] = set()
    for item in raw_tags:
        tag = str(item).strip()
        if tag and tag not in seen:
            tags.append(tag)
            seen.add(tag)
    return tags


def _payload_notification_settings(
    payload: dict,
    connection_type: str,
    *,
    base_settings: dict | None = None,
    reset_base: bool = False,
) -> str:
    if "settings" not in payload:
        settings = {} if reset_base else dict(base_settings or {})
        return json.dumps(_encrypt_notification_secret_fields(connection_type, settings))

    value = payload.get("settings")
    if value in (None, ""):
        submitted = {}
    elif isinstance(value, str):
        submitted = from_json(value, None)
    else:
        submitted = value
    if not isinstance(submitted, dict):
        raise ValueError("settings must be an object")

    settings = {} if reset_base else dict(base_settings or {})
    secret_keys = set(_notification_secret_keys_for(connection_type))
    for key, raw_value in submitted.items():
        if key in secret_keys and raw_value in (None, ""):
            continue
        if raw_value is None:
            settings.pop(key, None)
        else:
            settings[str(key)] = raw_value
    return json.dumps(_encrypt_notification_secret_fields(connection_type, settings))


def _default_language_profile_id(db) -> int | None:
    row = db.execute(
        "SELECT value FROM settings WHERE key='default_language_profile_id'"
    ).fetchone()
    if not row:
        return None
    try:
        return int(row["value"])
    except (TypeError, ValueError):
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


def _int_cfg(name: str, default: int) -> int:
    try:
        return int(str(get_cfg(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


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


@router.get("/api/v1/system/update")
async def api_v1_system_update():
    return JSONResponse(system_router.build_update_status())


@router.get("/api/v1/health")
async def api_v1_health():
    payload = await build_health_payload()
    checks = payload["checks"]
    stats = payload["stats"]
    return JSONResponse(
        {
            "ok": all(check["ok"] for check in checks),
            "checks": checks,
            "issues": [check for check in checks if not check["ok"]],
            "lastRss": payload["last_rss"],
            "lastBacklog": payload["last_backlog"],
            "stats": {
                "series": stats["series"] or 0,
                "wanted": stats["wanted"] or 0,
                "grabbed": stats["grabbed"] or 0,
                "downloaded": stats["downloaded"] or 0,
            } if stats else {
                "series": 0,
                "wanted": 0,
                "grabbed": 0,
                "downloaded": 0,
            },
            "staleSeriesCount": len(payload["stale_series"]),
            "staleGrabCount": len(payload["stale_grabs"]),
            "stuckImportCount": len(payload["stuck_imports"]),
            "recentErrorCount": len(payload["recent_errors"]),
        }
    )


@router.get("/api/v1/diskspace")
async def api_v1_disk_space():
    with get_db() as db:
        rows = db.execute(
            "SELECT id, path, label FROM root_folders ORDER BY is_default DESC, label, path"
        ).fetchall()
    payload = []
    for row in rows:
        entry = _disk_space_entry(row["path"])
        entry.update(
            {
                "id": row["id"],
                "name": row["label"] or row["path"],
                "label": row["label"],
            }
        )
        payload.append(entry)
    return JSONResponse(payload)


@router.get("/api/v1/config/host")
async def api_v1_config_host():
    return JSONResponse(
        {
            "instanceName": get_cfg("instance_name", "Mangarr") or "Mangarr",
            "urlBase": get_cfg("url_base", ""),
            "logLevel": get_cfg("log_level", "INFO"),
            "backupFolder": get_cfg("backup_folder", "/config/backups/"),
            "backupIntervalDays": _int_cfg("backup_interval_days", 7),
            "backupRetention": _int_cfg("backup_retention", 10),
            "uiDateFormat": get_cfg("ui_date_format", "relative"),
            "blocklistTtlDays": _int_cfg("blocklist_ttl_days", 90),
            "recycleBinRetentionDays": _int_cfg("recycle_bin_retention_days", 30),
            "recycleBinRemoveFiles": _json_bool(
                get_cfg("recycle_bin_remove_files", "false")
            ),
        }
    )


@router.get("/api/v1/config/mediamanagement")
async def api_v1_config_media_management():
    return JSONResponse(
        {
            "torrentSavePath": get_cfg("torrent_save_path", ""),
            "importMode": get_cfg("import_mode", "hardlink"),
            "removeCompleted": _json_bool(get_cfg("remove_completed", "false")),
            "minimumFreeSpaceMb": _int_cfg("minimum_free_space_mb", 0),
            "fileFormat": get_cfg("file_format", ""),
            "chapterFormat": get_cfg("chapter_format", ""),
            "folderFormat": get_cfg("folder_format", ""),
            "qualityCutoff": get_cfg("quality_cutoff", ""),
            "propersAndRepacks": get_cfg(
                "propers_and_repacks", "prefer_and_upgrade"
            ),
        }
    )


@router.get("/api/v1/config/indexer")
async def api_v1_config_indexer():
    return JSONResponse(
        {
            "rssSyncInterval": _int_cfg("rss_interval", 900),
            "minimumAge": 0,
            "retention": 0,
            "maximumSize": 0,
            "enableRss": True,
            "enableAutomaticSearch": True,
            "enableInteractiveSearch": True,
        }
    )


@router.get("/api/v1/config/downloadclient")
async def api_v1_config_download_client():
    return JSONResponse(
        {
            "downloadClientWorkingFolders": get_cfg("torrent_save_path", ""),
            "removeCompletedDownloads": _json_bool(
                get_cfg("remove_completed", "false")
            ),
            "removeFailedDownloads": True,
            "redownloadFailed": False,
            "enableCompletedDownloadHandling": True,
        }
    )


@router.get("/api/v1/config/ui")
async def api_v1_config_ui():
    ui_date_format = get_cfg("ui_date_format", "relative") or "relative"
    return JSONResponse(
        {
            "uiDateFormat": ui_date_format,
            "showRelativeDates": ui_date_format == "relative",
            "theme": "dark",
            "language": "en",
            "timeFormat": "24h",
        }
    )


@router.get("/api/v1/config/naming")
async def api_v1_config_naming():
    file_format = get_cfg("file_format", "")
    chapter_format = get_cfg("chapter_format", "")
    folder_format = get_cfg("folder_format", "")
    return JSONResponse(
        {
            "renameVolumes": bool(file_format or chapter_format),
            "replaceIllegalCharacters": True,
            "fileFormat": file_format,
            "chapterFormat": chapter_format,
            "folderFormat": folder_format,
        }
    )


@router.get("/api/v1/system/backup")
async def api_v1_system_backups():
    return JSONResponse(_list_backup_entries())


@router.post("/api/v1/system/backup")
async def api_v1_create_system_backup():
    try:
        backup = _create_backup_file()
    except OSError as exc:
        return JSONResponse(
            {"error": f"backup failed: {type(exc).__name__}"},
            status_code=500,
        )
    return JSONResponse({"ok": True, "status": "created", "backup": backup})


@router.post("/api/v1/system/backup/{filename}/validate")
async def api_v1_validate_system_backup(filename: str):
    payload, status_code = system_router._validate_backup_zip(filename)
    if not payload.get("ok"):
        payload = {
            **payload,
            "error": payload.get("message") or "backup validation failed",
        }
    return JSONResponse(payload, status_code=status_code)


@router.delete("/api/v1/system/backup/{filename}")
async def api_v1_delete_system_backup(filename: str):
    if not _safe_backup_filename(filename):
        return JSONResponse({"error": "invalid filename"}, status_code=400)
    fpath = os.path.join(system_router.BACKUP_DIR, filename)
    if not os.path.exists(fpath):
        return JSONResponse(
            {"error": "backup not found"},
            status_code=HTTP_404_NOT_FOUND,
        )
    try:
        os.remove(fpath)
    except OSError as exc:
        return JSONResponse(
            {"error": f"backup delete failed: {type(exc).__name__}"},
            status_code=500,
        )
    return JSONResponse({"ok": True, "id": filename})


@router.get("/api/v1/system/task")
async def api_v1_system_tasks():
    return JSONResponse([_system_task(task) for task in TASKS])


@router.get("/api/v1/rootfolder")
async def api_v1_root_folders():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM root_folders ORDER BY is_default DESC, label, path"
        ).fetchall()
        payload = [_root_folder(row) for row in rows]
    return JSONResponse(payload)


@router.get("/api/v1/rootfolder/{root_folder_id}")
async def api_v1_root_folder(root_folder_id: int):
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM root_folders WHERE id=?",
            (root_folder_id,),
        ).fetchone()
        if not row:
            return JSONResponse(
                {"error": "root folder not found"},
                status_code=HTTP_404_NOT_FOUND,
            )
        payload = _root_folder(row)
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


@router.get("/api/v1/notification")
async def api_v1_notifications(request: Request):
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM notification_connections ORDER BY name, id"
        ).fetchall()
        payload = [_notification_connection(row) for row in rows]
    return _filtered_config_list_response(
        request,
        payload,
        text_fields=("name", "implementation"),
        sortable_fields={"id", "name", "implementation", "enable"},
    )


@router.get("/api/v1/notification/{connection_id}")
async def api_v1_notification(connection_id: int):
    with get_db() as db:
        payload = _notification_connection_by_id(db, connection_id)
    if not payload:
        return JSONResponse(
            {"error": "notification connection not found"},
            status_code=HTTP_404_NOT_FOUND,
        )
    return JSONResponse(payload)


@router.post("/api/v1/notification")
async def api_v1_create_notification(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "expected an object body"}, status_code=400)

    name = _payload_str(payload, "name")
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    implementation = _payload_str(payload, "implementation", "type")
    if not implementation:
        return JSONResponse({"error": "implementation is required"}, status_code=400)
    if implementation not in NOTIFICATION_CONNECTION_TYPES:
        return JSONResponse(
            {"error": "implementation is not supported"},
            status_code=400,
        )
    try:
        settings = _payload_notification_settings(payload, implementation)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    with get_db() as db:
        cur = db.execute(
            "INSERT INTO notification_connections"
            "(name,type,enabled,settings,on_grab,on_download,on_upgrade,"
            " on_series_add,on_health_issue,on_health_restored)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                name,
                implementation,
                1 if _payload_bool_alias(payload, ("enable", "enabled"), True) else 0,
                settings,
                1 if _payload_bool_alias(payload, ("onGrab", "on_grab"), True) else 0,
                1
                if _payload_bool_alias(payload, ("onDownload", "on_download"), True)
                else 0,
                1
                if _payload_bool_alias(payload, ("onUpgrade", "on_upgrade"), True)
                else 0,
                1
                if _payload_bool_alias(
                    payload,
                    ("onSeriesAdd", "on_series_add"),
                    True,
                )
                else 0,
                1
                if _payload_bool_alias(
                    payload,
                    ("onHealthIssue", "on_health_issue"),
                    True,
                )
                else 0,
                1
                if _payload_bool_alias(
                    payload,
                    ("onHealthRestored", "on_health_restored"),
                    False,
                )
                else 0,
            ),
        )
        connection = _notification_connection_by_id(db, cur.lastrowid)
    return JSONResponse(
        {"ok": True, "status": "created", "notification": connection}
    )


@router.put("/api/v1/notification/{connection_id}")
@router.patch("/api/v1/notification/{connection_id}")
async def api_v1_update_notification(request: Request, connection_id: int):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict) or not payload:
        return JSONResponse(
            {"error": "expected a non-empty object body"},
            status_code=400,
        )

    with get_db() as db:
        existing = db.execute(
            "SELECT * FROM notification_connections WHERE id=?",
            (connection_id,),
        ).fetchone()
        if not existing:
            return JSONResponse(
                {"error": "notification connection not found"},
                status_code=HTTP_404_NOT_FOUND,
            )

        fields: list[str] = []
        params: list = []
        if "name" in payload:
            name = _payload_str(payload, "name")
            if not name:
                return JSONResponse({"error": "name is required"}, status_code=400)
            fields.append("name=?")
            params.append(name)

        type_submitted = "implementation" in payload or "type" in payload
        implementation = (
            _payload_str(payload, "implementation", "type")
            if type_submitted
            else existing["type"]
        )
        if type_submitted:
            if not implementation:
                return JSONResponse(
                    {"error": "implementation is required"},
                    status_code=400,
                )
            if implementation not in NOTIFICATION_CONNECTION_TYPES:
                return JSONResponse(
                    {"error": "implementation is not supported"},
                    status_code=400,
                )
            fields.append("type=?")
            params.append(implementation)

        if "enable" in payload or "enabled" in payload:
            fields.append("enabled=?")
            params.append(
                1 if _payload_bool_alias(payload, ("enable", "enabled"), True) else 0
            )

        if "settings" in payload or type_submitted:
            current_settings = from_json(existing["settings"], {}) or {}
            if not isinstance(current_settings, dict):
                current_settings = {}
            try:
                settings = _payload_notification_settings(
                    payload,
                    implementation,
                    base_settings=current_settings,
                    reset_base=implementation != existing["type"],
                )
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            fields.append("settings=?")
            params.append(settings)

        event_aliases = {
            "on_grab": ("onGrab", "on_grab"),
            "on_download": ("onDownload", "on_download"),
            "on_upgrade": ("onUpgrade", "on_upgrade"),
            "on_series_add": ("onSeriesAdd", "on_series_add"),
            "on_health_issue": ("onHealthIssue", "on_health_issue"),
            "on_health_restored": ("onHealthRestored", "on_health_restored"),
        }
        for column, aliases in event_aliases.items():
            if any(alias in payload for alias in aliases):
                fields.append(f"{column}=?")
                params.append(1 if _payload_bool_alias(payload, aliases, True) else 0)

        if fields:
            params.append(connection_id)
            db.execute(
                f"UPDATE notification_connections SET {', '.join(fields)}"
                " WHERE id=?",
                params,
            )
        connection = _notification_connection_by_id(db, connection_id)
    return JSONResponse({"ok": True, "notification": connection})


@router.delete("/api/v1/notification/{connection_id}")
async def api_v1_delete_notification(connection_id: int):
    with get_db() as db:
        existing = db.execute(
            "SELECT 1 FROM notification_connections WHERE id=?",
            (connection_id,),
        ).fetchone()
        if not existing:
            return JSONResponse(
                {"error": "notification connection not found"},
                status_code=HTTP_404_NOT_FOUND,
            )
        db.execute(
            "DELETE FROM notification_connections WHERE id=?",
            (connection_id,),
        )
    return JSONResponse({"ok": True, "id": connection_id})


@router.get("/api/v1/qualityprofile")
async def api_v1_quality_profiles(request: Request):
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM quality_profiles ORDER BY id"
        ).fetchall()
        payload = [_quality_profile(row) for row in rows]
    return _filtered_config_list_response(
        request,
        payload,
        text_fields=("name", "cutoff"),
        sortable_fields={"id", "name", "cutoff", "isDefault"},
        default_sort_key="id",
    )


@router.get("/api/v1/qualityprofile/{profile_id}")
async def api_v1_quality_profile(profile_id: int):
    with get_db() as db:
        payload = _quality_profile_by_id(db, profile_id)
    if not payload:
        return JSONResponse(
            {"error": "quality profile not found"},
            status_code=HTTP_404_NOT_FOUND,
        )
    return JSONResponse(payload)


@router.post("/api/v1/qualityprofile")
async def api_v1_create_quality_profile(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "expected an object body"}, status_code=400)

    name = _payload_str(payload, "name")
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    try:
        qualities = _payload_quality_list(
            payload, "qualities", ["cbz", "epub", "cbr", "pdf"]
        )
        upgrades_allowed = _json_bool(payload.get("upgradesAllowed"), True)
        minimum_score = _payload_int(payload, "minimumCustomFormatScore", 0)
        cutoff_score = _payload_int(payload, "cutoffFormatScore", 10000)
        min_upgrade_score = _payload_int(payload, "minUpgradeFormatScore", 10)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    cutoff = _payload_str(payload, "cutoff") or None
    is_default = _json_bool(payload.get("isDefault"), False)

    try:
        with get_db() as db:
            if is_default:
                db.execute("UPDATE quality_profiles SET is_default=0")
            cur = db.execute(
                "INSERT INTO quality_profiles"
                "(name, qualities, cutoff, upgrades_allowed,"
                " minimum_custom_format_score, cutoff_format_score,"
                " min_upgrade_format_score, is_default)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    name,
                    qualities,
                    cutoff,
                    1 if upgrades_allowed else 0,
                    minimum_score,
                    cutoff_score,
                    min_upgrade_score,
                    1 if is_default else 0,
                ),
            )
            profile = _quality_profile_by_id(db, cur.lastrowid)
    except sqlite3.IntegrityError:
        return JSONResponse(
            {"error": "quality profile name already exists"},
            status_code=HTTP_400_BAD_REQUEST,
        )
    return JSONResponse({"ok": True, "status": "created", "qualityProfile": profile})


@router.put("/api/v1/qualityprofile/{profile_id}")
@router.patch("/api/v1/qualityprofile/{profile_id}")
async def api_v1_update_quality_profile(request: Request, profile_id: int):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict) or not payload:
        return JSONResponse(
            {"error": "expected a non-empty object body"},
            status_code=400,
        )

    fields: list[str] = []
    params: list = []
    try:
        if "name" in payload:
            name = _payload_str(payload, "name")
            if not name:
                return JSONResponse({"error": "name is required"}, status_code=400)
            fields.append("name=?")
            params.append(name)
        if "qualities" in payload:
            fields.append("qualities=?")
            params.append(_payload_quality_list(payload, "qualities", []))
        if "cutoff" in payload:
            fields.append("cutoff=?")
            params.append(_payload_str(payload, "cutoff") or None)
        if "upgradesAllowed" in payload:
            fields.append("upgrades_allowed=?")
            params.append(
                1 if _json_bool(payload.get("upgradesAllowed"), True) else 0
            )
        int_fields = {
            "minimumCustomFormatScore": "minimum_custom_format_score",
            "cutoffFormatScore": "cutoff_format_score",
            "minUpgradeFormatScore": "min_upgrade_format_score",
        }
        for key, column in int_fields.items():
            if key in payload:
                fields.append(f"{column}=?")
                params.append(_payload_int(payload, key, 0))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    is_default = payload.get("isDefault")
    wants_default = _json_bool(is_default, False) if is_default is not None else None

    try:
        with get_db() as db:
            existing = db.execute(
                "SELECT 1 FROM quality_profiles WHERE id=?",
                (profile_id,),
            ).fetchone()
            if not existing:
                return JSONResponse(
                    {"error": "quality profile not found"},
                    status_code=HTTP_404_NOT_FOUND,
                )
            if wants_default:
                db.execute("UPDATE quality_profiles SET is_default=0")
                fields.append("is_default=?")
                params.append(1)
            elif wants_default is False:
                fields.append("is_default=?")
                params.append(0)
            if fields:
                params.append(profile_id)
                db.execute(
                    f"UPDATE quality_profiles SET {', '.join(fields)} WHERE id=?",
                    params,
                )
            profile = _quality_profile_by_id(db, profile_id)
    except sqlite3.IntegrityError:
        return JSONResponse(
            {"error": "quality profile name already exists"},
            status_code=HTTP_400_BAD_REQUEST,
        )
    return JSONResponse({"ok": True, "qualityProfile": profile})


@router.post("/api/v1/qualityprofile/{profile_id}/default")
async def api_v1_set_default_quality_profile(profile_id: int):
    with get_db() as db:
        existing = db.execute(
            "SELECT 1 FROM quality_profiles WHERE id=?",
            (profile_id,),
        ).fetchone()
        if not existing:
            return JSONResponse(
                {"error": "quality profile not found"},
                status_code=HTTP_404_NOT_FOUND,
            )
        db.execute("UPDATE quality_profiles SET is_default=0")
        db.execute("UPDATE quality_profiles SET is_default=1 WHERE id=?", (profile_id,))
        profile = _quality_profile_by_id(db, profile_id)
    return JSONResponse({"ok": True, "qualityProfile": profile})


@router.delete("/api/v1/qualityprofile/{profile_id}")
async def api_v1_delete_quality_profile(profile_id: int):
    with get_db() as db:
        existing = db.execute(
            "SELECT 1 FROM quality_profiles WHERE id=?",
            (profile_id,),
        ).fetchone()
        if not existing:
            return JSONResponse(
                {"error": "quality profile not found"},
                status_code=HTTP_404_NOT_FOUND,
            )
        db.execute(
            "UPDATE series SET quality_profile_id=NULL WHERE quality_profile_id=?",
            (profile_id,),
        )
        db.execute("DELETE FROM quality_profiles WHERE id=?", (profile_id,))
    return JSONResponse({"ok": True, "id": profile_id})


@router.get("/api/v1/languageprofile")
async def api_v1_language_profiles(request: Request):
    with get_db() as db:
        default_id = _default_language_profile_id(db)
        rows = db.execute(
            "SELECT * FROM language_profiles ORDER BY id"
        ).fetchall()
        payload = [_language_profile(row, default_id) for row in rows]
    return _filtered_config_list_response(
        request,
        payload,
        text_fields=("name",),
        sortable_fields={"id", "name", "isDefault"},
        default_sort_key="id",
    )


@router.get("/api/v1/languageprofile/{profile_id}")
async def api_v1_language_profile(profile_id: int):
    with get_db() as db:
        payload = _language_profile_by_id(db, profile_id)
    if not payload:
        return JSONResponse(
            {"error": "language profile not found"},
            status_code=HTTP_404_NOT_FOUND,
        )
    return JSONResponse(payload)


@router.post("/api/v1/languageprofile")
async def api_v1_create_language_profile(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "expected an object body"}, status_code=400)

    name = _payload_str(payload, "name")
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    try:
        languages = _payload_language_list(payload, "languages", ["any"])
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    allow_any = _json_bool(payload.get("allowAny", payload.get("allow_any")), False)
    is_default = _json_bool(payload.get("isDefault"), False)

    try:
        with get_db() as db:
            cur = db.execute(
                "INSERT INTO language_profiles(name, languages, allow_any)"
                " VALUES(?,?,?)",
                (name, languages, 1 if allow_any else 0),
            )
            profile_id = cur.lastrowid
            if is_default:
                db.execute(
                    "INSERT OR REPLACE INTO settings(key, value)"
                    " VALUES('default_language_profile_id', ?)",
                    (str(profile_id),),
                )
            profile = _language_profile_by_id(db, profile_id)
    except sqlite3.IntegrityError:
        return JSONResponse(
            {"error": "language profile name already exists"},
            status_code=HTTP_400_BAD_REQUEST,
        )
    return JSONResponse({"ok": True, "status": "created", "languageProfile": profile})


@router.put("/api/v1/languageprofile/{profile_id}")
@router.patch("/api/v1/languageprofile/{profile_id}")
async def api_v1_update_language_profile(request: Request, profile_id: int):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict) or not payload:
        return JSONResponse(
            {"error": "expected a non-empty object body"},
            status_code=400,
        )

    fields: list[str] = []
    params: list = []
    try:
        if "name" in payload:
            name = _payload_str(payload, "name")
            if not name:
                return JSONResponse({"error": "name is required"}, status_code=400)
            fields.append("name=?")
            params.append(name)
        if "languages" in payload:
            fields.append("languages=?")
            params.append(_payload_language_list(payload, "languages", ["any"]))
        if "allowAny" in payload or "allow_any" in payload:
            fields.append("allow_any=?")
            allow_any = payload.get("allowAny", payload.get("allow_any"))
            params.append(1 if _json_bool(allow_any) else 0)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    is_default = payload.get("isDefault")
    wants_default = _json_bool(is_default, False) if is_default is not None else None

    try:
        with get_db() as db:
            existing = db.execute(
                "SELECT 1 FROM language_profiles WHERE id=?",
                (profile_id,),
            ).fetchone()
            if not existing:
                return JSONResponse(
                    {"error": "language profile not found"},
                    status_code=HTTP_404_NOT_FOUND,
                )
            if fields:
                params.append(profile_id)
                db.execute(
                    f"UPDATE language_profiles SET {', '.join(fields)} WHERE id=?",
                    params,
                )
            if wants_default:
                db.execute(
                    "INSERT OR REPLACE INTO settings(key, value)"
                    " VALUES('default_language_profile_id', ?)",
                    (str(profile_id),),
                )
            elif wants_default is False:
                default_id = _default_language_profile_id(db)
                if default_id == profile_id:
                    db.execute(
                        "DELETE FROM settings"
                        " WHERE key='default_language_profile_id'"
                    )
            profile = _language_profile_by_id(db, profile_id)
    except sqlite3.IntegrityError:
        return JSONResponse(
            {"error": "language profile name already exists"},
            status_code=HTTP_400_BAD_REQUEST,
        )
    return JSONResponse({"ok": True, "languageProfile": profile})


@router.post("/api/v1/languageprofile/{profile_id}/default")
async def api_v1_set_default_language_profile(profile_id: int):
    with get_db() as db:
        existing = db.execute(
            "SELECT 1 FROM language_profiles WHERE id=?",
            (profile_id,),
        ).fetchone()
        if not existing:
            return JSONResponse(
                {"error": "language profile not found"},
                status_code=HTTP_404_NOT_FOUND,
            )
        db.execute(
            "INSERT OR REPLACE INTO settings(key, value)"
            " VALUES('default_language_profile_id', ?)",
            (str(profile_id),),
        )
        profile = _language_profile_by_id(db, profile_id)
    return JSONResponse({"ok": True, "languageProfile": profile})


@router.delete("/api/v1/languageprofile/{profile_id}")
async def api_v1_delete_language_profile(profile_id: int):
    with get_db() as db:
        existing = db.execute(
            "SELECT 1 FROM language_profiles WHERE id=?",
            (profile_id,),
        ).fetchone()
        if not existing:
            return JSONResponse(
                {"error": "language profile not found"},
                status_code=HTTP_404_NOT_FOUND,
            )
        ref = db.execute(
            "SELECT id FROM series WHERE language_profile_id=? LIMIT 1",
            (profile_id,),
        ).fetchone()
        if ref:
            return JSONResponse(
                {"error": "language profile is in use"},
                status_code=HTTP_400_BAD_REQUEST,
            )
        default_id = _default_language_profile_id(db)
        if default_id == profile_id:
            db.execute(
                "DELETE FROM settings WHERE key='default_language_profile_id'"
            )
        db.execute("DELETE FROM language_profiles WHERE id=?", (profile_id,))
    return JSONResponse({"ok": True, "id": profile_id})


@router.get("/api/v1/customformat")
async def api_v1_custom_formats(request: Request):
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
    return _filtered_config_list_response(
        request,
        payload,
        text_fields=("name",),
        sortable_fields={"id", "name"},
    )


@router.get("/api/v1/customformat/{format_id}")
async def api_v1_custom_format(format_id: int):
    with get_db() as db:
        payload = _custom_format_by_id(db, format_id)
    if not payload:
        return JSONResponse(
            {"error": "custom format not found"},
            status_code=HTTP_404_NOT_FOUND,
        )
    return JSONResponse(payload)


def _replace_custom_format_scores(db, format_id: int, scores: list[dict]) -> None:
    db.execute(
        "DELETE FROM quality_profile_custom_formats WHERE format_id=?",
        (format_id,),
    )
    for item in scores:
        profile_id = item["qualityProfileId"]
        profile = db.execute(
            "SELECT 1 FROM quality_profiles WHERE id=?",
            (profile_id,),
        ).fetchone()
        if not profile:
            raise ValueError(f"qualityProfileId {profile_id} not found")
        score = item["score"]
        if score:
            db.execute(
                "INSERT INTO quality_profile_custom_formats"
                "(profile_id, format_id, score) VALUES(?, ?, ?)",
                (profile_id, format_id, score),
            )


@router.post("/api/v1/customformat")
async def api_v1_create_custom_format(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "expected an object body"}, status_code=400)

    name = _payload_str(payload, "name")
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    try:
        specifications = _payload_json_list(payload, "specifications", [])
        include_when_renaming = _json_bool(
            payload.get(
                "includeCustomFormatWhenRenaming",
                payload.get("include_custom_format_when_renaming"),
            ),
            False,
        )
        scores = _payload_score_list(payload)
        with get_db() as db:
            cur = db.execute(
                "INSERT INTO custom_formats"
                "(name, specifications, include_custom_format_when_renaming)"
                " VALUES(?,?,?)",
                (
                    name,
                    specifications,
                    1 if include_when_renaming else 0,
                ),
            )
            format_id = cur.lastrowid
            _replace_custom_format_scores(db, format_id, scores)
            custom_format = _custom_format_by_id(db, format_id)
    except sqlite3.IntegrityError:
        return JSONResponse(
            {"error": "custom format name already exists"},
            status_code=HTTP_400_BAD_REQUEST,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(
        {"ok": True, "status": "created", "customFormat": custom_format}
    )


@router.put("/api/v1/customformat/{format_id}")
@router.patch("/api/v1/customformat/{format_id}")
async def api_v1_update_custom_format(request: Request, format_id: int):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict) or not payload:
        return JSONResponse(
            {"error": "expected a non-empty object body"},
            status_code=400,
        )

    fields: list[str] = []
    params: list = []
    try:
        if "name" in payload:
            name = _payload_str(payload, "name")
            if not name:
                return JSONResponse({"error": "name is required"}, status_code=400)
            fields.append("name=?")
            params.append(name)
        if "specifications" in payload:
            fields.append("specifications=?")
            params.append(_payload_json_list(payload, "specifications", []))
        include_key = (
            "includeCustomFormatWhenRenaming"
            if "includeCustomFormatWhenRenaming" in payload
            else "include_custom_format_when_renaming"
        )
        if include_key in payload:
            fields.append("include_custom_format_when_renaming=?")
            params.append(1 if _json_bool(payload.get(include_key)) else 0)
        scores = (
            _payload_score_list(payload)
            if "qualityProfileScores" in payload
            else None
        )
        with get_db() as db:
            existing = db.execute(
                "SELECT 1 FROM custom_formats WHERE id=?",
                (format_id,),
            ).fetchone()
            if not existing:
                return JSONResponse(
                    {"error": "custom format not found"},
                    status_code=HTTP_404_NOT_FOUND,
                )
            if fields:
                params.append(format_id)
                db.execute(
                    f"UPDATE custom_formats SET {', '.join(fields)} WHERE id=?",
                    params,
                )
            if scores is not None:
                _replace_custom_format_scores(db, format_id, scores)
            custom_format = _custom_format_by_id(db, format_id)
    except sqlite3.IntegrityError:
        return JSONResponse(
            {"error": "custom format name already exists"},
            status_code=HTTP_400_BAD_REQUEST,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "customFormat": custom_format})


@router.delete("/api/v1/customformat/{format_id}")
async def api_v1_delete_custom_format(format_id: int):
    with get_db() as db:
        existing = db.execute(
            "SELECT 1 FROM custom_formats WHERE id=?",
            (format_id,),
        ).fetchone()
        if not existing:
            return JSONResponse(
                {"error": "custom format not found"},
                status_code=HTTP_404_NOT_FOUND,
            )
        db.execute(
            "DELETE FROM quality_profile_custom_formats WHERE format_id=?",
            (format_id,),
        )
        db.execute("DELETE FROM custom_formats WHERE id=?", (format_id,))
    return JSONResponse({"ok": True, "id": format_id})


@router.get("/api/v1/releaseprofile")
async def api_v1_release_profiles(request: Request):
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
    return _filtered_config_list_response(
        request,
        payload,
        text_fields=("name", "required", "ignored"),
        sortable_fields={"id", "name", "enabled"},
        default_sort_key="id",
    )


@router.get("/api/v1/releaseprofile/{profile_id}")
async def api_v1_release_profile(profile_id: int):
    with get_db() as db:
        payload = _release_profile_by_id(db, profile_id)
    if not payload:
        return JSONResponse(
            {"error": "release profile not found"},
            status_code=HTTP_404_NOT_FOUND,
        )
    return JSONResponse(payload)


@router.post("/api/v1/releaseprofile")
async def api_v1_create_release_profile(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "expected an object body"}, status_code=400)

    name = _payload_str(payload, "name")
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    try:
        preferred = _payload_json_list(payload, "preferred", [])
        tags = _payload_tag_list(payload)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    enabled = _json_bool(payload.get("enabled"), True)
    include_preferred = _json_bool(
        payload.get(
            "includePreferredWhenRenaming",
            payload.get("include_preferred_when_renaming"),
        ),
        False,
    )

    with get_db() as db:
        cur = db.execute(
            "INSERT INTO release_profiles"
            "(name, enabled, required, ignored, preferred,"
            " include_preferred_when_renaming)"
            " VALUES(?,?,?,?,?,?)",
            (
                name,
                1 if enabled else 0,
                _payload_str(payload, "required"),
                _payload_str(payload, "ignored"),
                preferred,
                1 if include_preferred else 0,
            ),
        )
        profile_id = cur.lastrowid
        for tag in tags:
            db.execute(
                "INSERT OR IGNORE INTO release_profile_tags(profile_id, tag)"
                " VALUES(?, ?)",
                (profile_id, tag),
            )
        profile = _release_profile_by_id(db, profile_id)
    return JSONResponse({"ok": True, "status": "created", "releaseProfile": profile})


@router.put("/api/v1/releaseprofile/{profile_id}")
@router.patch("/api/v1/releaseprofile/{profile_id}")
async def api_v1_update_release_profile(request: Request, profile_id: int):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict) or not payload:
        return JSONResponse(
            {"error": "expected a non-empty object body"},
            status_code=400,
        )

    fields: list[str] = []
    params: list = []
    try:
        if "name" in payload:
            name = _payload_str(payload, "name")
            if not name:
                return JSONResponse({"error": "name is required"}, status_code=400)
            fields.append("name=?")
            params.append(name)
        if "enabled" in payload:
            fields.append("enabled=?")
            params.append(1 if _json_bool(payload.get("enabled"), True) else 0)
        if "required" in payload:
            fields.append("required=?")
            params.append(_payload_str(payload, "required"))
        if "ignored" in payload:
            fields.append("ignored=?")
            params.append(_payload_str(payload, "ignored"))
        if "preferred" in payload:
            fields.append("preferred=?")
            params.append(_payload_json_list(payload, "preferred", []))
        include_key = (
            "includePreferredWhenRenaming"
            if "includePreferredWhenRenaming" in payload
            else "include_preferred_when_renaming"
        )
        if include_key in payload:
            fields.append("include_preferred_when_renaming=?")
            params.append(1 if _json_bool(payload.get(include_key)) else 0)
        tags = _payload_tag_list(payload) if "tags" in payload else None
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    with get_db() as db:
        existing = db.execute(
            "SELECT 1 FROM release_profiles WHERE id=?",
            (profile_id,),
        ).fetchone()
        if not existing:
            return JSONResponse(
                {"error": "release profile not found"},
                status_code=HTTP_404_NOT_FOUND,
            )
        if fields:
            params.append(profile_id)
            db.execute(
                f"UPDATE release_profiles SET {', '.join(fields)} WHERE id=?",
                params,
            )
        if tags is not None:
            db.execute(
                "DELETE FROM release_profile_tags WHERE profile_id=?",
                (profile_id,),
            )
            for tag in tags:
                db.execute(
                    "INSERT OR IGNORE INTO release_profile_tags(profile_id, tag)"
                    " VALUES(?, ?)",
                    (profile_id, tag),
                )
        profile = _release_profile_by_id(db, profile_id)
    return JSONResponse({"ok": True, "releaseProfile": profile})


@router.delete("/api/v1/releaseprofile/{profile_id}")
async def api_v1_delete_release_profile(profile_id: int):
    with get_db() as db:
        existing = db.execute(
            "SELECT 1 FROM release_profiles WHERE id=?",
            (profile_id,),
        ).fetchone()
        if not existing:
            return JSONResponse(
                {"error": "release profile not found"},
                status_code=HTTP_404_NOT_FOUND,
            )
        db.execute(
            "DELETE FROM release_profile_tags WHERE profile_id=?",
            (profile_id,),
        )
        db.execute("DELETE FROM release_profiles WHERE id=?", (profile_id,))
    return JSONResponse({"ok": True, "id": profile_id})


@router.get("/api/v1/delayprofile")
async def api_v1_delay_profiles(request: Request):
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
    return _filtered_config_list_response(
        request,
        payload,
        text_fields=("name",),
        sortable_fields={"id", "name", "order", "isDefault"},
        default_sort_key="order",
    )


@router.get("/api/v1/delayprofile/{profile_id}")
async def api_v1_delay_profile(profile_id: int):
    with get_db() as db:
        payload = _delay_profile_by_id(db, profile_id)
    if not payload:
        return JSONResponse(
            {"error": "delay profile not found"},
            status_code=HTTP_404_NOT_FOUND,
        )
    return JSONResponse(payload)


@router.post("/api/v1/delayprofile")
async def api_v1_create_delay_profile(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "expected an object body"}, status_code=400)

    try:
        tags = _payload_tag_list(payload)
        enable_usenet = _payload_bool_alias(
            payload, ("enableUsenet", "enable_usenet"), True
        )
        enable_torrent = _payload_bool_alias(
            payload, ("enableTorrent", "enable_torrent"), True
        )
        usenet_delay = _payload_non_negative_alias(
            payload, ("usenetDelay", "usenet_delay"), 0
        )
        torrent_delay = _payload_non_negative_alias(
            payload, ("torrentDelay", "torrent_delay"), 0
        )
        bypass = _payload_bool_alias(
            payload,
            ("bypassIfHighestQuality", "bypass_if_highest_quality"),
            False,
        )
        is_default = _payload_bool_alias(payload, ("isDefault", "is_default"), False)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    name = _payload_str(payload, "name") or "Custom"

    with get_db() as db:
        max_order = db.execute(
            "SELECT COALESCE(MAX(order_num), 0) FROM delay_profiles"
        ).fetchone()[0]
        try:
            order_num = _payload_non_negative_alias(
                payload, ("order", "order_num"), max_order + 1
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if is_default:
            db.execute("UPDATE delay_profiles SET is_default=0")
        cur = db.execute(
            "INSERT INTO delay_profiles"
            "(name, order_num, enable_usenet, enable_torrent, usenet_delay,"
            " torrent_delay, bypass_if_highest_quality, is_default)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (
                name,
                order_num,
                1 if enable_usenet else 0,
                1 if enable_torrent else 0,
                usenet_delay,
                torrent_delay,
                1 if bypass else 0,
                1 if is_default else 0,
            ),
        )
        profile_id = cur.lastrowid
        for tag in tags:
            db.execute(
                "INSERT OR IGNORE INTO delay_profile_tags(profile_id, tag)"
                " VALUES(?, ?)",
                (profile_id, tag),
            )
        profile = _delay_profile_by_id(db, profile_id)
    return JSONResponse({"ok": True, "status": "created", "delayProfile": profile})


@router.put("/api/v1/delayprofile/{profile_id}")
@router.patch("/api/v1/delayprofile/{profile_id}")
async def api_v1_update_delay_profile(request: Request, profile_id: int):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict) or not payload:
        return JSONResponse(
            {"error": "expected a non-empty object body"},
            status_code=400,
        )

    fields: list[str] = []
    params: list = []
    try:
        if "name" in payload:
            fields.append("name=?")
            params.append(_payload_str(payload, "name") or "Custom")
        int_fields = {
            ("order", "order_num"): "order_num",
            ("usenetDelay", "usenet_delay"): "usenet_delay",
            ("torrentDelay", "torrent_delay"): "torrent_delay",
        }
        for keys, column in int_fields.items():
            if any(key in payload for key in keys):
                fields.append(f"{column}=?")
                params.append(_payload_non_negative_alias(payload, keys, 0))
        bool_fields = {
            ("enableUsenet", "enable_usenet"): "enable_usenet",
            ("enableTorrent", "enable_torrent"): "enable_torrent",
            (
                "bypassIfHighestQuality",
                "bypass_if_highest_quality",
            ): "bypass_if_highest_quality",
        }
        for keys, column in bool_fields.items():
            if any(key in payload for key in keys):
                fields.append(f"{column}=?")
                params.append(1 if _payload_bool_alias(payload, keys, False) else 0)
        tags = _payload_tag_list(payload) if "tags" in payload else None
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    is_default = (
        _payload_bool_alias(payload, ("isDefault", "is_default"), False)
        if "isDefault" in payload or "is_default" in payload
        else None
    )

    with get_db() as db:
        existing = db.execute(
            "SELECT 1 FROM delay_profiles WHERE id=?",
            (profile_id,),
        ).fetchone()
        if not existing:
            return JSONResponse(
                {"error": "delay profile not found"},
                status_code=HTTP_404_NOT_FOUND,
            )
        if is_default:
            db.execute("UPDATE delay_profiles SET is_default=0")
            fields.append("is_default=?")
            params.append(1)
        elif is_default is False:
            fields.append("is_default=?")
            params.append(0)
        if fields:
            params.append(profile_id)
            db.execute(
                f"UPDATE delay_profiles SET {', '.join(fields)} WHERE id=?",
                params,
            )
        if tags is not None:
            db.execute(
                "DELETE FROM delay_profile_tags WHERE profile_id=?",
                (profile_id,),
            )
            for tag in tags:
                db.execute(
                    "INSERT OR IGNORE INTO delay_profile_tags(profile_id, tag)"
                    " VALUES(?, ?)",
                    (profile_id, tag),
                )
        profile = _delay_profile_by_id(db, profile_id)
    return JSONResponse({"ok": True, "delayProfile": profile})


@router.delete("/api/v1/delayprofile/{profile_id}")
async def api_v1_delete_delay_profile(profile_id: int):
    with get_db() as db:
        existing = db.execute(
            "SELECT is_default FROM delay_profiles WHERE id=?",
            (profile_id,),
        ).fetchone()
        if not existing:
            return JSONResponse(
                {"error": "delay profile not found"},
                status_code=HTTP_404_NOT_FOUND,
            )
        if existing["is_default"]:
            return JSONResponse(
                {"error": "Cannot delete the default delay profile"},
                status_code=HTTP_400_BAD_REQUEST,
            )
        db.execute(
            "DELETE FROM delay_profile_tags WHERE profile_id=?",
            (profile_id,),
        )
        db.execute("DELETE FROM delay_profiles WHERE id=?", (profile_id,))
    return JSONResponse({"ok": True, "id": profile_id})


@router.get("/api/v1/importlist")
async def api_v1_import_lists(request: Request):
    with get_db() as db:
        rows = db.execute("SELECT * FROM import_lists ORDER BY name").fetchall()
        payload = [_import_list(row) for row in rows]
    return _filtered_config_list_response(
        request,
        payload,
        text_fields=("name", "implementation", "monitorMode"),
        sortable_fields={"id", "name", "implementation", "enable", "lastSync"},
    )


@router.get("/api/v1/importlist/{list_id}")
async def api_v1_import_list(list_id: int):
    with get_db() as db:
        payload = _import_list_by_id(db, list_id)
    if not payload:
        return JSONResponse(
            {"error": "import list not found"},
            status_code=HTTP_404_NOT_FOUND,
        )
    return JSONResponse(payload)


@router.post("/api/v1/importlist")
async def api_v1_create_import_list(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "expected an object body"}, status_code=400)

    name = _payload_str(payload, "name")
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    implementation = _payload_str(payload, "implementation", "type")
    if not implementation:
        return JSONResponse({"error": "implementation is required"}, status_code=400)
    try:
        settings = _payload_json_object(payload, "settings", {})
        quality_profile_id = _payload_optional_fk_alias(
            payload, ("qualityProfileId", "quality_profile_id")
        )
        root_folder_id = _payload_optional_fk_alias(
            payload, ("rootFolderId", "root_folder_id")
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    enabled = _payload_bool_alias(payload, ("enable", "enabled"), True)
    monitor_mode = _payload_str(payload, "monitorMode", "monitor_mode") or "all"

    try:
        with get_db() as db:
            _validate_optional_fk(
                db, "quality_profiles", quality_profile_id, "qualityProfileId"
            )
            _validate_optional_fk(db, "root_folders", root_folder_id, "rootFolderId")
            cur = db.execute(
                "INSERT INTO import_lists"
                "(name, type, enabled, quality_profile_id, root_folder_id,"
                " monitor_mode, settings)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    name,
                    implementation,
                    1 if enabled else 0,
                    quality_profile_id,
                    root_folder_id,
                    monitor_mode,
                    settings,
                ),
            )
            import_list = _import_list_by_id(db, cur.lastrowid)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "status": "created", "importList": import_list})


@router.post("/api/v1/importlist/sync")
async def api_v1_sync_import_lists():
    import main as _m

    _m.create_background_task(_sync_all_lists(), name="import_lists:sync_all")
    return JSONResponse({"ok": True, "message": "Sync started in background"})


@router.put("/api/v1/importlist/{list_id}")
@router.patch("/api/v1/importlist/{list_id}")
async def api_v1_update_import_list(request: Request, list_id: int):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict) or not payload:
        return JSONResponse(
            {"error": "expected a non-empty object body"},
            status_code=400,
        )

    fields: list[str] = []
    params: list = []
    try:
        if "name" in payload:
            name = _payload_str(payload, "name")
            if not name:
                return JSONResponse({"error": "name is required"}, status_code=400)
            fields.append("name=?")
            params.append(name)
        if "implementation" in payload or "type" in payload:
            implementation = _payload_str(payload, "implementation", "type")
            if not implementation:
                return JSONResponse(
                    {"error": "implementation is required"},
                    status_code=400,
                )
            fields.append("type=?")
            params.append(implementation)
        if "enable" in payload or "enabled" in payload:
            fields.append("enabled=?")
            params.append(
                1 if _payload_bool_alias(payload, ("enable", "enabled"), True) else 0
            )
        if "qualityProfileId" in payload or "quality_profile_id" in payload:
            quality_profile_id = _payload_optional_fk_alias(
                payload, ("qualityProfileId", "quality_profile_id")
            )
            fields.append("quality_profile_id=?")
            params.append(quality_profile_id)
        else:
            quality_profile_id = None
        if "rootFolderId" in payload or "root_folder_id" in payload:
            root_folder_id = _payload_optional_fk_alias(
                payload, ("rootFolderId", "root_folder_id")
            )
            fields.append("root_folder_id=?")
            params.append(root_folder_id)
        else:
            root_folder_id = None
        if "monitorMode" in payload or "monitor_mode" in payload:
            fields.append("monitor_mode=?")
            params.append(
                _payload_str(payload, "monitorMode", "monitor_mode") or "all"
            )
        if "settings" in payload:
            fields.append("settings=?")
            params.append(_payload_json_object(payload, "settings", {}))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    try:
        with get_db() as db:
            existing = db.execute(
                "SELECT 1 FROM import_lists WHERE id=?",
                (list_id,),
            ).fetchone()
            if not existing:
                return JSONResponse(
                    {"error": "import list not found"},
                    status_code=HTTP_404_NOT_FOUND,
                )
            if "qualityProfileId" in payload or "quality_profile_id" in payload:
                _validate_optional_fk(
                    db, "quality_profiles", quality_profile_id, "qualityProfileId"
                )
            if "rootFolderId" in payload or "root_folder_id" in payload:
                _validate_optional_fk(db, "root_folders", root_folder_id, "rootFolderId")
            if fields:
                params.append(list_id)
                db.execute(
                    f"UPDATE import_lists SET {', '.join(fields)} WHERE id=?",
                    params,
                )
            import_list = _import_list_by_id(db, list_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "importList": import_list})


@router.post("/api/v1/importlist/{list_id}/sync")
async def api_v1_sync_import_list(list_id: int):
    with get_db() as db:
        row = db.execute("SELECT * FROM import_lists WHERE id=?", (list_id,)).fetchone()
        if not row:
            return JSONResponse(
                {"error": "import list not found"},
                status_code=HTTP_404_NOT_FOUND,
            )
        import_list = dict(row)
    import main as _m

    _m.create_background_task(_sync_list(import_list), name=f"import_lists:sync:{list_id}")
    return JSONResponse({"ok": True, "message": f"Sync started for {import_list['name']}"})


@router.delete("/api/v1/importlist/{list_id}")
async def api_v1_delete_import_list(list_id: int):
    with get_db() as db:
        existing = db.execute(
            "SELECT 1 FROM import_lists WHERE id=?",
            (list_id,),
        ).fetchone()
        if not existing:
            return JSONResponse(
                {"error": "import list not found"},
                status_code=HTTP_404_NOT_FOUND,
            )
        db.execute("DELETE FROM import_lists WHERE id=?", (list_id,))
    return JSONResponse({"ok": True, "id": list_id})


@router.get("/api/v1/importlistexclusion")
async def api_v1_import_list_exclusions():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM import_list_exclusions"
            " ORDER BY source, title, external_id, id"
        ).fetchall()
        payload = [_import_list_exclusion(row) for row in rows]
    return JSONResponse(payload)


@router.get("/api/v1/importlistexclusion/{exclusion_id}")
async def api_v1_import_list_exclusion(exclusion_id: int):
    with get_db() as db:
        payload = _import_list_exclusion_by_id(db, exclusion_id)
    if not payload:
        return JSONResponse(
            {"error": "import list exclusion not found"},
            status_code=HTTP_404_NOT_FOUND,
        )
    return JSONResponse(payload)


@router.post("/api/v1/importlistexclusion")
async def api_v1_create_import_list_exclusion(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "expected an object body"}, status_code=400)

    source = _payload_str(payload, "source")
    external_id = _payload_str(payload, "externalId", "external_id") or None
    title = _payload_str(payload, "title")
    title_normalized = _normalize_import_list_title(title)
    reason = _payload_str(payload, "reason") or None
    if not source or (not external_id and not title_normalized):
        return JSONResponse(
            {"error": "source plus either externalId or title is required"},
            status_code=400,
        )

    with get_db() as db:
        cur = db.execute(
            "INSERT OR IGNORE INTO import_list_exclusions"
            "(source, external_id, title, title_normalized, reason)"
            " VALUES(?,?,?,?,?)",
            (source, external_id, title, title_normalized, reason),
        )
        status = "created" if cur.rowcount else "exists"
        row = None
        if external_id:
            row = db.execute(
                "SELECT * FROM import_list_exclusions"
                " WHERE source=? AND external_id=?",
                (source, external_id),
            ).fetchone()
        if not row and title_normalized:
            row = db.execute(
                "SELECT * FROM import_list_exclusions"
                " WHERE source=? AND title_normalized=?",
                (source, title_normalized),
            ).fetchone()
        exclusion = _import_list_exclusion(row)
    return JSONResponse(
        {"ok": True, "status": status, "importListExclusion": exclusion}
    )


@router.put("/api/v1/importlistexclusion/{exclusion_id}")
@router.patch("/api/v1/importlistexclusion/{exclusion_id}")
async def api_v1_update_import_list_exclusion(
    request: Request,
    exclusion_id: int,
):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict) or not payload:
        return JSONResponse(
            {"error": "expected a non-empty object body"},
            status_code=400,
        )

    with get_db() as db:
        existing = db.execute(
            "SELECT * FROM import_list_exclusions WHERE id=?",
            (exclusion_id,),
        ).fetchone()
        if not existing:
            return JSONResponse(
                {"error": "import list exclusion not found"},
                status_code=HTTP_404_NOT_FOUND,
            )
        source = (
            _payload_str(payload, "source")
            if "source" in payload
            else existing["source"]
        )
        external_id = (
            _payload_str(payload, "externalId", "external_id") or None
            if "externalId" in payload or "external_id" in payload
            else existing["external_id"]
        )
        title = (
            _payload_str(payload, "title")
            if "title" in payload
            else existing["title"]
        )
        title_normalized = _normalize_import_list_title(title)
        reason = (
            _payload_str(payload, "reason") or None
            if "reason" in payload
            else existing["reason"]
        )
        if not source or (not external_id and not title_normalized):
            return JSONResponse(
                {"error": "source plus either externalId or title is required"},
                status_code=400,
            )
        try:
            db.execute(
                "UPDATE import_list_exclusions"
                " SET source=?, external_id=?, title=?, title_normalized=?,"
                " reason=? WHERE id=?",
                (
                    source,
                    external_id,
                    title,
                    title_normalized,
                    reason,
                    exclusion_id,
                ),
            )
        except sqlite3.IntegrityError:
            return JSONResponse(
                {"error": "import list exclusion already exists"},
                status_code=HTTP_400_BAD_REQUEST,
            )
        exclusion = _import_list_exclusion_by_id(db, exclusion_id)
    return JSONResponse({"ok": True, "importListExclusion": exclusion})


@router.delete("/api/v1/importlistexclusion/{exclusion_id}")
async def api_v1_delete_import_list_exclusion(exclusion_id: int):
    with get_db() as db:
        existing = db.execute(
            "SELECT 1 FROM import_list_exclusions WHERE id=?",
            (exclusion_id,),
        ).fetchone()
        if not existing:
            return JSONResponse(
                {"error": "import list exclusion not found"},
                status_code=HTTP_404_NOT_FOUND,
            )
        db.execute("DELETE FROM import_list_exclusions WHERE id=?", (exclusion_id,))
    return JSONResponse({"ok": True, "id": exclusion_id})


@router.get("/api/v1/qualitydefinition")
async def api_v1_quality_definitions():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM quality_definitions ORDER BY order_num, quality"
        ).fetchall()
        payload = [_quality_definition(row) for row in rows]
    return JSONResponse(payload)


@router.get("/api/v1/qualitydefinition/{quality}")
async def api_v1_quality_definition(quality: str):
    with get_db() as db:
        payload = _quality_definition_by_quality(db, quality)
    if not payload:
        return JSONResponse(
            {"error": "quality definition not found"},
            status_code=HTTP_404_NOT_FOUND,
        )
    return JSONResponse(payload)


@router.get("/api/v1/indexer")
async def api_v1_indexers(request: Request):
    with get_db() as db:
        rows = db.execute("SELECT * FROM indexers ORDER BY priority, id").fetchall()
        tag_rows = db.execute(
            "SELECT indexer_id, tag FROM indexer_tags ORDER BY tag"
        ).fetchall()
        tags_by_indexer: dict[int, list[str]] = {}
        for tag in tag_rows:
            tags_by_indexer.setdefault(tag["indexer_id"], []).append(tag["tag"])
        payload = [_indexer(row, tags_by_indexer.get(row["id"], [])) for row in rows]
    return _filtered_config_list_response(
        request,
        payload,
        text_fields=("name", "implementation", "baseUrl"),
        sortable_fields={"id", "name", "implementation", "enable", "priority"},
        default_sort_key="priority",
    )


@router.get("/api/v1/indexer/{indexer_id}")
async def api_v1_indexer(indexer_id: int):
    with get_db() as db:
        payload = _indexer_by_id(db, indexer_id)
    if not payload:
        return JSONResponse(
            {"error": "indexer not found"},
            status_code=HTTP_404_NOT_FOUND,
        )
    return JSONResponse(payload)


@router.post("/api/v1/indexer")
async def api_v1_create_indexer(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "expected an object body"}, status_code=400)

    name = _payload_str(payload, "name")
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    implementation = _payload_str(payload, "implementation", "type")
    if not implementation:
        return JSONResponse({"error": "implementation is required"}, status_code=400)
    try:
        priority = _payload_non_negative_alias(payload, ("priority",), 25)
        categories = _payload_category_list(payload)
        settings = _payload_json_object(payload, "settings", {})
        client_id = _payload_optional_fk_alias(
            payload,
            ("downloadClientId", "client_id"),
        )
        min_seeders = _payload_non_negative_alias(
            payload,
            ("minimumSeeders", "min_seeders"),
            0,
        )
        seed_ratio = _payload_non_negative_float_alias(
            payload,
            ("seedRatio", "seed_ratio"),
            0.0,
        )
        min_size = _payload_non_negative_alias(
            payload,
            ("minimumSize", "min_size_mb"),
            0,
        )
        max_size = _payload_non_negative_alias(
            payload,
            ("maximumSize", "max_size_mb"),
            0,
        )
        parent_prowlarr_id = _payload_optional_fk_alias(
            payload,
            ("parentProwlarrId", "parent_prowlarr_id"),
        )
        prowlarr_indexer_id = _payload_optional_fk_alias(
            payload,
            ("prowlarrIndexerId", "prowlarr_indexer_id"),
        )
        tags = _payload_tag_list(payload)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    api_key = _payload_str(payload, "apiKey", "api_key")
    stored_key = encrypt_if_cipher_available(api_key) if api_key else None

    with get_db() as db:
        try:
            _validate_optional_fk(db, "download_clients", client_id, "downloadClientId")
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        cur = db.execute(
            "INSERT INTO indexers"
            "(name,type,url,api_key,priority,enabled,categories,settings,"
            " client_id,min_seeders,seed_ratio,parent_prowlarr_id,"
            " prowlarr_indexer_id,use_rss,use_auto_search,"
            " use_interactive_search,min_size_mb,max_size_mb)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                name,
                implementation,
                _payload_str(payload, "baseUrl", "url") or None,
                stored_key,
                priority,
                1 if _payload_bool_alias(payload, ("enable", "enabled"), True) else 0,
                categories,
                settings,
                client_id,
                min_seeders,
                seed_ratio,
                parent_prowlarr_id,
                prowlarr_indexer_id,
                1
                if _payload_bool_alias(payload, ("enableRss", "use_rss"), True)
                else 0,
                1
                if _payload_bool_alias(
                    payload,
                    ("enableAutomaticSearch", "use_auto_search"),
                    True,
                )
                else 0,
                1
                if _payload_bool_alias(
                    payload,
                    ("enableInteractiveSearch", "use_interactive_search"),
                    True,
                )
                else 0,
                min_size,
                max_size,
            ),
        )
        indexer_id = cur.lastrowid
        _replace_indexer_tags(db, indexer_id, tags)
        indexer = _indexer_by_id(db, indexer_id)
    return JSONResponse({"ok": True, "status": "created", "indexer": indexer})


@router.put("/api/v1/indexer/{indexer_id}")
@router.patch("/api/v1/indexer/{indexer_id}")
async def api_v1_update_indexer(request: Request, indexer_id: int):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict) or not payload:
        return JSONResponse(
            {"error": "expected a non-empty object body"},
            status_code=400,
        )

    fields: list[str] = []
    params: list = []
    client_id = None
    client_id_submitted = "downloadClientId" in payload or "client_id" in payload
    try:
        if "name" in payload:
            name = _payload_str(payload, "name")
            if not name:
                return JSONResponse({"error": "name is required"}, status_code=400)
            fields.append("name=?")
            params.append(name)
        if "implementation" in payload or "type" in payload:
            implementation = _payload_str(payload, "implementation", "type")
            if not implementation:
                return JSONResponse(
                    {"error": "implementation is required"},
                    status_code=400,
                )
            fields.append("type=?")
            params.append(implementation)
        if "baseUrl" in payload or "url" in payload:
            fields.append("url=?")
            params.append(_payload_str(payload, "baseUrl", "url") or None)
        if "apiKey" in payload or "api_key" in payload:
            api_key = _payload_str(payload, "apiKey", "api_key")
            if api_key:
                fields.append("api_key=?")
                params.append(encrypt_if_cipher_available(api_key))
        if "priority" in payload:
            fields.append("priority=?")
            params.append(_payload_non_negative_alias(payload, ("priority",), 0))
        if "categories" in payload:
            fields.append("categories=?")
            params.append(_payload_category_list(payload))
        if "settings" in payload:
            fields.append("settings=?")
            params.append(_payload_json_object(payload, "settings", {}))
        if client_id_submitted:
            client_id = _payload_optional_fk_alias(
                payload,
                ("downloadClientId", "client_id"),
            )
            fields.append("client_id=?")
            params.append(client_id)
        int_fields = {
            ("minimumSeeders", "min_seeders"): "min_seeders",
            ("minimumSize", "min_size_mb"): "min_size_mb",
            ("maximumSize", "max_size_mb"): "max_size_mb",
        }
        for keys, column in int_fields.items():
            if any(key in payload for key in keys):
                fields.append(f"{column}=?")
                params.append(_payload_non_negative_alias(payload, keys, 0))
        if "seedRatio" in payload or "seed_ratio" in payload:
            fields.append("seed_ratio=?")
            params.append(
                _payload_non_negative_float_alias(
                    payload,
                    ("seedRatio", "seed_ratio"),
                    0.0,
                )
            )
        for keys, column in {
            ("parentProwlarrId", "parent_prowlarr_id"): "parent_prowlarr_id",
            ("prowlarrIndexerId", "prowlarr_indexer_id"): "prowlarr_indexer_id",
        }.items():
            if any(key in payload for key in keys):
                fields.append(f"{column}=?")
                params.append(_payload_optional_fk_alias(payload, keys))
        bool_fields = {
            ("enable", "enabled"): "enabled",
            ("enableRss", "use_rss"): "use_rss",
            ("enableAutomaticSearch", "use_auto_search"): "use_auto_search",
            ("enableInteractiveSearch", "use_interactive_search"):
                "use_interactive_search",
        }
        for keys, column in bool_fields.items():
            if any(key in payload for key in keys):
                fields.append(f"{column}=?")
                params.append(1 if _payload_bool_alias(payload, keys, True) else 0)
        tags = _payload_tag_list(payload) if "tags" in payload else None
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    with get_db() as db:
        existing = db.execute(
            "SELECT 1 FROM indexers WHERE id=?",
            (indexer_id,),
        ).fetchone()
        if not existing:
            return JSONResponse(
                {"error": "indexer not found"},
                status_code=HTTP_404_NOT_FOUND,
            )
        if client_id_submitted:
            try:
                _validate_optional_fk(
                    db,
                    "download_clients",
                    client_id,
                    "downloadClientId",
                )
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
        if fields:
            params.append(indexer_id)
            db.execute(
                f"UPDATE indexers SET {', '.join(fields)} WHERE id=?",
                params,
            )
        if tags is not None:
            _replace_indexer_tags(db, indexer_id, tags)
        indexer = _indexer_by_id(db, indexer_id)
    return JSONResponse({"ok": True, "indexer": indexer})


@router.delete("/api/v1/indexer/{indexer_id}")
async def api_v1_delete_indexer(indexer_id: int):
    with get_db() as db:
        existing = db.execute(
            "SELECT 1 FROM indexers WHERE id=?",
            (indexer_id,),
        ).fetchone()
        if not existing:
            return JSONResponse(
                {"error": "indexer not found"},
                status_code=HTTP_404_NOT_FOUND,
            )
        db.execute("DELETE FROM indexer_tags WHERE indexer_id=?", (indexer_id,))
        db.execute("DELETE FROM indexers WHERE id=?", (indexer_id,))
    return JSONResponse({"ok": True, "id": indexer_id})


@router.get("/api/v1/downloadclient")
async def api_v1_download_clients(request: Request):
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
    return _filtered_config_list_response(
        request,
        payload,
        text_fields=("name", "implementation", "host", "category"),
        sortable_fields={"id", "name", "implementation", "enable", "priority"},
        default_sort_key="priority",
    )


@router.post("/api/v1/downloadclient")
async def api_v1_create_download_client(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "expected an object body"}, status_code=400)

    name = _payload_str(payload, "name")
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    implementation = _payload_str(payload, "implementation", "type")
    if not implementation:
        return JSONResponse({"error": "implementation is required"}, status_code=400)
    try:
        port = _payload_optional_fk_alias(payload, ("port",))
        priority = _payload_non_negative_alias(payload, ("priority",), 1)
        tags = _payload_tag_list(payload)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    password = _payload_str(payload, "password")
    stored_password = encrypt_if_cipher_available(password) if password else None

    with get_db() as db:
        cur = db.execute(
            "INSERT INTO download_clients"
            "(name, type, host, port, use_ssl, url_base, username, password,"
            " category, post_import_category, recent_priority, older_priority,"
            " initial_state, sequential_order, first_last_first, content_layout,"
            " priority, enabled, remove_completed, remove_failed, source_id,"
            " download_path, merge_chapters)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                name,
                implementation,
                _payload_str(payload, "host"),
                port,
                1 if _payload_bool_alias(payload, ("useSsl", "use_ssl"), False) else 0,
                _payload_str(payload, "urlBase", "url_base") or None,
                _payload_str(payload, "username") or None,
                stored_password,
                _payload_str(payload, "category") or "manga",
                _payload_str(payload, "postImportCategory", "post_import_category")
                or None,
                _payload_str(payload, "recentPriority", "recent_priority")
                or "last",
                _payload_str(payload, "olderPriority", "older_priority")
                or "last",
                _payload_str(payload, "initialState", "initial_state")
                or "normal",
                1
                if _payload_bool_alias(
                    payload, ("sequentialOrder", "sequential_order"), False
                )
                else 0,
                1
                if _payload_bool_alias(
                    payload, ("firstLastFirst", "first_last_first"), False
                )
                else 0,
                _payload_str(payload, "contentLayout", "content_layout")
                or "original",
                priority,
                1 if _payload_bool_alias(payload, ("enable", "enabled"), True) else 0,
                1
                if _payload_bool_alias(
                    payload,
                    ("removeCompletedDownloads", "remove_completed"),
                    False,
                )
                else 0,
                1
                if _payload_bool_alias(
                    payload,
                    ("removeFailedDownloads", "remove_failed"),
                    False,
                )
                else 0,
                _payload_str(payload, "sourceId", "source_id") or None,
                _payload_str(payload, "downloadPath", "download_path") or None,
                1
                if _payload_bool_alias(
                    payload,
                    ("mergeChapters", "merge_chapters"),
                    True,
                )
                else 0,
            ),
        )
        client_id = cur.lastrowid
        _replace_download_client_tags(db, client_id, tags)
        client = _download_client_by_id(db, client_id)
    return JSONResponse({"ok": True, "status": "created", "downloadClient": client})


@router.get("/api/v1/downloadclient/remotepathmapping")
async def api_v1_remote_path_mappings():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM remote_path_mappings ORDER BY id"
        ).fetchall()
        payload = [_remote_path_mapping(row) for row in rows]
    return JSONResponse(payload)


@router.get("/api/v1/downloadclient/remotepathmapping/{mapping_id}")
async def api_v1_remote_path_mapping(mapping_id: int):
    with get_db() as db:
        payload = _remote_path_mapping_by_id(db, mapping_id)
    if not payload:
        return JSONResponse(
            {"error": "remote path mapping not found"},
            status_code=HTTP_404_NOT_FOUND,
        )
    return JSONResponse(payload)


@router.post("/api/v1/downloadclient/remotepathmapping")
async def api_v1_create_remote_path_mapping(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "expected an object body"}, status_code=400)

    remote_path = _payload_str(payload, "remotePath", "remote_path")
    local_path = _payload_str(payload, "localPath", "local_path")
    if not remote_path:
        return JSONResponse({"error": "remotePath is required"}, status_code=400)
    if not local_path:
        return JSONResponse({"error": "localPath is required"}, status_code=400)
    host = _payload_str(payload, "host")

    with get_db() as db:
        cur = db.execute(
            "INSERT INTO remote_path_mappings(host, remote_path, local_path)"
            " VALUES(?,?,?)",
            (host, remote_path, local_path),
        )
        mapping = _remote_path_mapping_by_id(db, cur.lastrowid)
    return JSONResponse(
        {"ok": True, "status": "created", "remotePathMapping": mapping}
    )


@router.put("/api/v1/downloadclient/remotepathmapping/{mapping_id}")
@router.patch("/api/v1/downloadclient/remotepathmapping/{mapping_id}")
async def api_v1_update_remote_path_mapping(request: Request, mapping_id: int):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict) or not payload:
        return JSONResponse(
            {"error": "expected a non-empty object body"},
            status_code=400,
        )

    fields: list[str] = []
    params: list = []
    if "host" in payload:
        fields.append("host=?")
        params.append(_payload_str(payload, "host"))
    if "remotePath" in payload or "remote_path" in payload:
        remote_path = _payload_str(payload, "remotePath", "remote_path")
        if not remote_path:
            return JSONResponse({"error": "remotePath is required"}, status_code=400)
        fields.append("remote_path=?")
        params.append(remote_path)
    if "localPath" in payload or "local_path" in payload:
        local_path = _payload_str(payload, "localPath", "local_path")
        if not local_path:
            return JSONResponse({"error": "localPath is required"}, status_code=400)
        fields.append("local_path=?")
        params.append(local_path)

    with get_db() as db:
        existing = db.execute(
            "SELECT 1 FROM remote_path_mappings WHERE id=?",
            (mapping_id,),
        ).fetchone()
        if not existing:
            return JSONResponse(
                {"error": "remote path mapping not found"},
                status_code=HTTP_404_NOT_FOUND,
            )
        if fields:
            params.append(mapping_id)
            db.execute(
                f"UPDATE remote_path_mappings SET {', '.join(fields)} WHERE id=?",
                params,
            )
        mapping = _remote_path_mapping_by_id(db, mapping_id)
    return JSONResponse({"ok": True, "remotePathMapping": mapping})


@router.delete("/api/v1/downloadclient/remotepathmapping/{mapping_id}")
async def api_v1_delete_remote_path_mapping(mapping_id: int):
    with get_db() as db:
        existing = db.execute(
            "SELECT 1 FROM remote_path_mappings WHERE id=?",
            (mapping_id,),
        ).fetchone()
        if not existing:
            return JSONResponse(
                {"error": "remote path mapping not found"},
                status_code=HTTP_404_NOT_FOUND,
            )
        db.execute("DELETE FROM remote_path_mappings WHERE id=?", (mapping_id,))
    return JSONResponse({"ok": True, "id": mapping_id})


@router.get("/api/v1/downloadclient/{client_id}")
async def api_v1_download_client(client_id: int):
    with get_db() as db:
        payload = _download_client_by_id(db, client_id)
    if not payload:
        return JSONResponse(
            {"error": "download client not found"},
            status_code=HTTP_404_NOT_FOUND,
        )
    return JSONResponse(payload)


@router.put("/api/v1/downloadclient/{client_id}")
@router.patch("/api/v1/downloadclient/{client_id}")
async def api_v1_update_download_client(request: Request, client_id: int):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict) or not payload:
        return JSONResponse(
            {"error": "expected a non-empty object body"},
            status_code=400,
        )

    fields: list[str] = []
    params: list = []
    try:
        if "name" in payload:
            name = _payload_str(payload, "name")
            if not name:
                return JSONResponse({"error": "name is required"}, status_code=400)
            fields.append("name=?")
            params.append(name)
        if "implementation" in payload or "type" in payload:
            implementation = _payload_str(payload, "implementation", "type")
            if not implementation:
                return JSONResponse(
                    {"error": "implementation is required"},
                    status_code=400,
                )
            fields.append("type=?")
            params.append(implementation)
        text_fields = {
            ("host",): "host",
            ("urlBase", "url_base"): "url_base",
            ("username",): "username",
            ("category",): "category",
            ("postImportCategory", "post_import_category"): "post_import_category",
            ("recentPriority", "recent_priority"): "recent_priority",
            ("olderPriority", "older_priority"): "older_priority",
            ("initialState", "initial_state"): "initial_state",
            ("contentLayout", "content_layout"): "content_layout",
            ("sourceId", "source_id"): "source_id",
            ("downloadPath", "download_path"): "download_path",
        }
        nullable_text_columns = {
            "url_base",
            "username",
            "post_import_category",
            "source_id",
            "download_path",
        }
        defaults = {
            "category": "manga",
            "recent_priority": "last",
            "older_priority": "last",
            "initial_state": "normal",
            "content_layout": "original",
        }
        for keys, column in text_fields.items():
            if any(key in payload for key in keys):
                value = _payload_str(payload, *keys)
                if value:
                    stored_value = value
                elif column in defaults:
                    stored_value = defaults[column]
                elif column in nullable_text_columns:
                    stored_value = None
                else:
                    stored_value = ""
                fields.append(f"{column}=?")
                params.append(stored_value)
        if "password" in payload:
            password = _payload_str(payload, "password")
            if password:
                fields.append("password=?")
                params.append(encrypt_if_cipher_available(password))
        if "port" in payload:
            fields.append("port=?")
            params.append(_payload_optional_fk_alias(payload, ("port",)))
        if "priority" in payload:
            fields.append("priority=?")
            params.append(_payload_non_negative_alias(payload, ("priority",), 0))
        bool_fields = {
            ("useSsl", "use_ssl"): "use_ssl",
            ("enable", "enabled"): "enabled",
            ("removeCompletedDownloads", "remove_completed"): "remove_completed",
            ("removeFailedDownloads", "remove_failed"): "remove_failed",
            ("sequentialOrder", "sequential_order"): "sequential_order",
            ("firstLastFirst", "first_last_first"): "first_last_first",
            ("mergeChapters", "merge_chapters"): "merge_chapters",
        }
        for keys, column in bool_fields.items():
            if any(key in payload for key in keys):
                default = column in {"enabled", "merge_chapters"}
                fields.append(f"{column}=?")
                params.append(1 if _payload_bool_alias(payload, keys, default) else 0)
        tags = _payload_tag_list(payload) if "tags" in payload else None
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    with get_db() as db:
        existing = db.execute(
            "SELECT 1 FROM download_clients WHERE id=?",
            (client_id,),
        ).fetchone()
        if not existing:
            return JSONResponse(
                {"error": "download client not found"},
                status_code=HTTP_404_NOT_FOUND,
            )
        if fields:
            params.append(client_id)
            db.execute(
                f"UPDATE download_clients SET {', '.join(fields)} WHERE id=?",
                params,
            )
        if tags is not None:
            _replace_download_client_tags(db, client_id, tags)
        client = _download_client_by_id(db, client_id)
    return JSONResponse({"ok": True, "downloadClient": client})


@router.delete("/api/v1/downloadclient/{client_id}")
async def api_v1_delete_download_client(client_id: int):
    with get_db() as db:
        existing = db.execute(
            "SELECT 1 FROM download_clients WHERE id=?",
            (client_id,),
        ).fetchone()
        if not existing:
            return JSONResponse(
                {"error": "download client not found"},
                status_code=HTTP_404_NOT_FOUND,
            )
        db.execute(
            "DELETE FROM download_client_tags WHERE client_id=?",
            (client_id,),
        )
        db.execute("DELETE FROM download_clients WHERE id=?", (client_id,))
    return JSONResponse({"ok": True, "id": client_id})


@router.get("/api/v1/tag")
async def api_v1_tags():
    with get_db() as db:
        tag_counts: dict[str, dict] = {}

        def _bucket(tag: str) -> dict:
            return tag_counts.setdefault(tag, _empty_tag(tag))

        for table, _owner_column, field in _TAG_TABLES:
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


@router.get("/api/v1/tag/{tag_label}")
async def api_v1_tag(tag_label: str):
    tag = str(tag_label or "").strip()
    if not tag:
        return JSONResponse({"error": "tag is required"}, status_code=400)
    with get_db() as db:
        payload = _tag_by_label(db, tag)
    if not payload:
        return JSONResponse(
            {"error": "tag not found"},
            status_code=HTTP_404_NOT_FOUND,
        )
    return JSONResponse(payload)


@router.put("/api/v1/tag/{tag_label}")
@router.patch("/api/v1/tag/{tag_label}")
async def api_v1_update_tag(request: Request, tag_label: str):
    old_tag = str(tag_label or "").strip()
    if not old_tag:
        return JSONResponse({"error": "tag is required"}, status_code=400)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "expected an object body"}, status_code=400)

    new_tag = _payload_str(payload, "label", "name", "tag")
    if not new_tag:
        return JSONResponse({"error": "label is required"}, status_code=400)
    if new_tag == old_tag:
        with get_db() as db:
            tag = _tag_by_label(db, old_tag)
        if not tag:
            return JSONResponse(
                {"error": "tag not found"},
                status_code=HTTP_404_NOT_FOUND,
            )
        return JSONResponse({"ok": True, "tag": tag})

    with get_db() as db:
        existing = _tag_by_label(db, old_tag)
        if not existing:
            return JSONResponse(
                {"error": "tag not found"},
                status_code=HTTP_404_NOT_FOUND,
            )
        _rename_tag_everywhere(db, old_tag, new_tag)
        tag = _tag_by_label(db, new_tag)
    return JSONResponse({"ok": True, "tag": tag})


@router.delete("/api/v1/tag/{tag_label}")
async def api_v1_delete_tag(tag_label: str):
    tag = str(tag_label or "").strip()
    if not tag:
        return JSONResponse({"error": "tag is required"}, status_code=400)
    with get_db() as db:
        existing = _tag_by_label(db, tag)
        if not existing:
            return JSONResponse(
                {"error": "tag not found"},
                status_code=HTTP_404_NOT_FOUND,
            )
        _delete_tag_everywhere(db, tag)
    return JSONResponse({"ok": True, "id": tag})


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
    return JSONResponse([_system_task(task) for task in TASKS])


@router.post("/api/v1/command")
async def api_v1_run_command(request: Request):
    return await _run_command(request)


@router.get("/api/v1/log")
async def api_v1_logs(
    page: int = 1,
    pageSize: int = 100,
    eventType: str = "",
    seriesId: int = 0,
):
    page = max(page, 1)
    page_size = max(min(pageSize, 250), 1)
    where_parts: list[str] = []
    params: list = []
    if eventType:
        where_parts.append("e.event_type=?")
        params.append(eventType)
    if seriesId:
        where_parts.append("e.series_id=?")
        params.append(seriesId)
    where = "WHERE " + " AND ".join(where_parts) if where_parts else ""
    offset = (page - 1) * page_size
    with get_db() as db:
        total = db.execute(
            f"SELECT COUNT(*) FROM events e {where}",
            params,
        ).fetchone()[0]
        rows = db.execute(
            "SELECT e.*, s.title AS series_title"
            " FROM events e"
            " LEFT JOIN series s ON s.id=e.series_id"
            f" {where}"
            " ORDER BY e.created_at DESC, e.id DESC"
            " LIMIT ? OFFSET ?",
            params + [page_size, offset],
        ).fetchall()
        records = [_event_log_record(row) for row in rows]
    return JSONResponse(
        {
            "page": page,
            "pageSize": page_size,
            "totalRecords": total,
            "records": records,
        }
    )


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


@router.get("/api/v1/rename/library/preview")
async def api_v1_rename_library_preview():
    return JSONResponse(build_library_rename_preview())


@router.post("/api/v1/rename/library")
async def api_v1_rename_library_execute(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return JSONResponse({"error": "expected an object body"}, status_code=400)
    try:
        series_ids = _optional_id_set(payload, "seriesIds")
        volume_ids = _optional_id_set(payload, "volumeIds")
        chapter_ids = _optional_id_set(payload, "chapterIds")
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    return JSONResponse(
        execute_library_rename(
            series_ids=series_ids,
            volume_ids=volume_ids,
            chapter_ids=chapter_ids,
        )
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
async def api_v1_root_folder_unmapped_matches(
    root_folder_id: int, path: str = "", query: str = ""
):
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

    search_query = (query or "").strip() or folder["name"]
    results, source = await search_series(search_query)
    matches = [_metadata_match_payload(search_query, item) for item in results]
    matches.sort(key=lambda item: item["confidence"], reverse=True)
    return JSONResponse(
        {
            "rootFolderId": scan["rootFolderId"],
            "folder": folder,
            "query": search_query,
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
