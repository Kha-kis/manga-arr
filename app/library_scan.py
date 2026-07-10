"""Library folder discovery and adoption."""
from __future__ import annotations

import os
from dataclasses import dataclass

from files import MANGA_EXTENSIONS
from rescan import _series_library_dir, rescan_series_folder
from shared import get_db
from volumes import create_volume_stubs


@dataclass
class AdoptUnmappedFolderResult:
    ok: bool
    status_code: int
    error: str | None = None
    description: str | None = None
    payload: dict | None = None


def _folder_stats(path: str) -> dict:
    total_files = 0
    manga_files = 0
    size_bytes = 0
    for root, _, files in os.walk(path):
        for name in files:
            total_files += 1
            full_path = os.path.join(root, name)
            try:
                size_bytes += os.path.getsize(full_path)
            except OSError:
                pass
            if os.path.splitext(name)[1].lower() in MANGA_EXTENSIONS:
                manga_files += 1
    return {
        "totalFileCount": total_files,
        "mangaFileCount": manga_files,
        "sizeBytes": size_bytes,
    }


def scan_unmapped_root_folder(root_folder_id: int) -> dict | None:
    """Return immediate child directories not mapped to a known series."""
    with get_db() as db:
        root = db.execute(
            "SELECT id, path, label, is_default FROM root_folders WHERE id=?",
            (root_folder_id,),
        ).fetchone()
        if not root:
            return None
        root_path = root["path"]
        series_rows = db.execute(
            "SELECT id FROM series WHERE root_folder_id=? AND deleted_at IS NULL",
            (root_folder_id,),
        ).fetchall()
        known_paths = {
            os.path.normcase(os.path.abspath(path))
            for path in (_series_library_dir(db, row["id"]) for row in series_rows)
            if path
        }

    exists = os.path.isdir(root_path)
    unmapped = []
    if exists:
        for entry in os.scandir(root_path):
            if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
                continue
            full_path = os.path.abspath(entry.path)
            if os.path.normcase(full_path) in known_paths:
                continue
            unmapped.append(
                {
                    "name": entry.name,
                    "path": full_path,
                    "relativePath": os.path.relpath(full_path, root_path),
                    "status": "unmapped",
                    **_folder_stats(full_path),
                }
            )
    unmapped.sort(key=lambda item: item["name"].lower())

    return {
        "rootFolderId": root["id"],
        "path": root_path,
        "label": root["label"],
        "isDefault": bool(root["is_default"]),
        "exists": exists,
        "knownFolderCount": len(known_paths),
        "unmappedFolderCount": len(unmapped),
        "unmappedFolders": unmapped,
    }


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


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


def adopt_unmapped_folder(
    root_folder_id: int,
    folder_path: str,
    *,
    title: str | None = None,
    metadata_title: str | None = None,
    anilist_id: int | None = None,
    mal_id: int | None = None,
    mu_id: str | None = None,
    cover_url: str | None = None,
    status: str | None = None,
    description: str | None = None,
    total_volumes: int | None = None,
    total_chapters: int | None = None,
    pub_year: int | None = None,
    metadata_source: str | None = None,
    monitored: bool = True,
    quality_profile_id: int | None = None,
    language_profile_id: int | None = None,
) -> AdoptUnmappedFolderResult:
    """Create a series for an unmapped direct child folder and rescan it."""
    raw_path = (folder_path or "").strip()
    if not raw_path:
        return AdoptUnmappedFolderResult(False, 400, "path is required")
    requested_path = os.path.abspath(raw_path)

    with get_db() as db:
        root = db.execute(
            "SELECT id, path, label, is_default FROM root_folders WHERE id=?",
            (root_folder_id,),
        ).fetchone()
        if not root:
            return AdoptUnmappedFolderResult(
                False,
                404,
                "Not Found",
                "Root folder not found",
            )

        root_path = os.path.abspath(root["path"])
        if not os.path.isdir(root_path):
            return AdoptUnmappedFolderResult(
                False,
                400,
                "root folder is not available",
                "Root folder path does not exist on disk",
            )
        if not os.path.isdir(requested_path):
            return AdoptUnmappedFolderResult(
                False,
                400,
                "path is not an unmapped folder",
                "Requested path is not a directory",
            )

        root_norm = _norm_path(root_path)
        requested_norm = _norm_path(requested_path)
        parent_norm = _norm_path(os.path.dirname(requested_path))
        if parent_norm != root_norm or requested_norm == root_norm:
            return AdoptUnmappedFolderResult(
                False,
                400,
                "path is not an unmapped folder",
                "Requested path must be a direct child of the root folder",
            )

        series_rows = db.execute(
            "SELECT id FROM series WHERE root_folder_id=? AND deleted_at IS NULL",
            (root_folder_id,),
        ).fetchall()
        known_paths = {
            _norm_path(path)
            for path in (_series_library_dir(db, row["id"]) for row in series_rows)
            if path
        }
        if requested_norm in known_paths:
            return AdoptUnmappedFolderResult(
                False,
                400,
                "path is already mapped",
                "Requested path is already assigned to a series",
            )

        folder_name = os.path.basename(requested_path)
        series_title = (title or folder_name).strip()
        if not series_title:
            return AdoptUnmappedFolderResult(False, 400, "title is required")
        search_pattern = (metadata_title or series_title).strip() or series_title
        vol_count_source = metadata_source if metadata_source in (
            "anilist",
            "mangaupdates",
            "manual",
        ) else "manual"

        if quality_profile_id is not None:
            if not db.execute(
                "SELECT 1 FROM quality_profiles WHERE id=?", (quality_profile_id,)
            ).fetchone():
                return AdoptUnmappedFolderResult(False, 400, "qualityProfileId not found")
        else:
            quality_profile_id = _default_profile_id(db, "quality_profiles")

        if language_profile_id is not None:
            if not db.execute(
                "SELECT 1 FROM language_profiles WHERE id=?", (language_profile_id,)
            ).fetchone():
                return AdoptUnmappedFolderResult(False, 400, "languageProfileId not found")
        else:
            language_profile_id = _default_profile_id(db, "language_profiles")

        monitor_mode = "missing" if monitored else "none"
        cur = db.execute(
            "INSERT INTO series(title, search_pattern, anilist_id, mal_id, mu_id,"
            " cover_url, status, description, total_volumes, total_chapters,"
            " root_folder_id, folder_name, pub_year, enabled, monitored, monitor_mode,"
            " quality_profile_id, language_profile_id, vol_count_source)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                series_title,
                search_pattern,
                anilist_id,
                mal_id,
                mu_id,
                cover_url,
                status,
                description,
                total_volumes,
                total_chapters,
                root_folder_id,
                folder_name,
                pub_year,
                1,
                1 if monitored else 0,
                monitor_mode,
                quality_profile_id,
                language_profile_id,
                vol_count_source,
            ),
        )
        series_id = cur.lastrowid
        expected_path = _series_library_dir(db, series_id)
        if _norm_path(expected_path or "") != requested_norm:
            db.execute("DELETE FROM series WHERE id=?", (series_id,))
            return AdoptUnmappedFolderResult(
                False,
                400,
                "path does not match title",
                "Requested path does not match the configured series folder path",
            )

        if total_volumes and total_volumes > 0:
            create_volume_stubs(db, series_id, total_volumes)
        rescan = rescan_series_folder(db, series_id)
        series_row = db.execute(
            "SELECT id, title, search_pattern, root_folder_id, monitored,"
            " monitor_mode, quality_profile_id, language_profile_id,"
            " anilist_id, mal_id, mu_id, cover_url, status, description,"
            " total_volumes, total_chapters, pub_year, vol_count_source, folder_name"
            " FROM series WHERE id=?",
            (series_id,),
        ).fetchone()
        if not series_row:
            return AdoptUnmappedFolderResult(False, 500, "series adoption failed")

        return AdoptUnmappedFolderResult(
            True,
            200,
            payload={
                "series": {
                    "id": series_row["id"],
                    "title": series_row["title"],
                    "searchPattern": series_row["search_pattern"],
                    "rootFolderId": series_row["root_folder_id"],
                    "folderName": series_row["folder_name"],
                    "path": requested_path,
                    "monitored": bool(series_row["monitored"]),
                    "monitorMode": series_row["monitor_mode"] or "all",
                    "qualityProfileId": series_row["quality_profile_id"],
                    "languageProfileId": series_row["language_profile_id"],
                    "anilistId": series_row["anilist_id"],
                    "malId": series_row["mal_id"],
                    "mangaUpdatesId": series_row["mu_id"],
                    "coverUrl": series_row["cover_url"],
                    "status": series_row["status"],
                    "overview": series_row["description"],
                    "totalVolumes": series_row["total_volumes"],
                    "totalChapters": series_row["total_chapters"],
                    "year": series_row["pub_year"],
                    "volumeCountSource": series_row["vol_count_source"],
                },
                "rescan": rescan,
            },
        )
