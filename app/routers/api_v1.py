"""Read-only Sonarr-style API v1 endpoints.

These endpoints are intentionally conservative: they expose stable JSON
contracts for external automation without replacing Mangarr's existing
workflow-specific `/api/*` actions.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from starlette.status import HTTP_404_NOT_FOUND

from rename_plan import build_series_rename_preview
from routers.system import APP_VERSION
from shared import (
    build_volume_label,
    from_json,
    get_cfg,
    get_db,
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


@router.get("/api/v1/series")
async def api_v1_series():
    with get_db() as db:
        rows = db.execute(
            """
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
            WHERE s.deleted_at IS NULL
            GROUP BY s.id
            ORDER BY s.title COLLATE NOCASE
            """
        ).fetchall()
        payload = [
            _series(row, _series_tags(db, row["id"], row["tags"]))
            for row in rows
        ]
    return JSONResponse(payload)


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


@router.get("/api/v1/rename/series/{series_id}/preview")
async def api_v1_rename_series_preview(series_id: int):
    preview = build_series_rename_preview(series_id)
    if preview is None:
        return JSONResponse(
            {"message": "Not Found", "description": "Series not found"},
            status_code=HTTP_404_NOT_FOUND,
        )
    return JSONResponse(preview)
