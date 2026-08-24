"""Library folder discovery and adoption."""

from __future__ import annotations

import fcntl
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

import shared
from files import MANGA_EXTENSIONS
from metadata_provenance import record_initial_title
from rescan import (
    SeriesRescanSnapshot,
    _series_library_dir,
    build_filesystem_inventory,
    enrich_reconciled_files,
    reconcile_series_inventory,
    snapshot_series_rescan,
)
from shared import get_db
from volumes import create_volume_stubs


_ADOPTION_LOCK = threading.Lock()


@contextmanager
def _adoption_process_lock() -> Iterator[None]:
    lock_path = f"{shared.DB_PATH}.adoption.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


@dataclass
class AdoptUnmappedFolderResult:
    ok: bool
    status_code: int
    error: str | None = None
    description: str | None = None
    payload: dict[str, Any] | None = None


def _folder_stats(path: str) -> dict[str, int]:
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


def scan_unmapped_root_folder(root_folder_id: int) -> dict[str, Any] | None:
    """Return immediate child directories not mapped to a known series."""
    with get_db() as db:
        root = db.execute(
            "SELECT id, path, label, is_default FROM root_folders WHERE id=?",
            (root_folder_id,),
        ).fetchone()
        if not root:
            return None
        root_snapshot = dict(root)
        root_path = root_snapshot["path"]
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
        "rootFolderId": root_snapshot["id"],
        "path": root_path,
        "label": root_snapshot["label"],
        "isDefault": bool(root_snapshot["is_default"]),
        "exists": exists,
        "knownFolderCount": len(known_paths),
        "unmappedFolderCount": len(unmapped),
        "unmappedFolders": unmapped,
    }


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _default_profile_id(db: sqlite3.Connection, table: str) -> int | None:
    if table == "quality_profiles":
        row = db.execute(
            "SELECT id FROM quality_profiles ORDER BY is_default DESC, id LIMIT 1"
        ).fetchone()
    else:
        row = db.execute(
            "SELECT id FROM language_profiles ORDER BY id LIMIT 1"
        ).fetchone()
    return row["id"] if row else None


def _lexical_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _mapping_exists(
    db: sqlite3.Connection,
    root_folder_id: int,
    requested_path: str,
) -> bool:
    requested = _lexical_path(requested_path)
    return any(
        _lexical_path(series_path) == requested
        for series_path in _series_library_paths(db, root_folder_id)
    )


def _series_library_paths(
    db: sqlite3.Connection,
    root_folder_id: int,
) -> tuple[str, ...]:
    rows = db.execute(
        "SELECT id FROM series WHERE root_folder_id=? AND deleted_at IS NULL",
        (root_folder_id,),
    ).fetchall()
    return tuple(
        series_path
        for series_path in (_series_library_dir(db, int(row["id"])) for row in rows)
        if series_path
    )


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
    with _ADOPTION_LOCK:
        with _adoption_process_lock():
            return _adopt_unmapped_folder_locked(
                root_folder_id,
                folder_path,
                title=title,
                metadata_title=metadata_title,
                anilist_id=anilist_id,
                mal_id=mal_id,
                mu_id=mu_id,
                cover_url=cover_url,
                status=status,
                description=description,
                total_volumes=total_volumes,
                total_chapters=total_chapters,
                pub_year=pub_year,
                metadata_source=metadata_source,
                monitored=monitored,
                quality_profile_id=quality_profile_id,
                language_profile_id=language_profile_id,
            )


def _adopt_unmapped_folder_locked(
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
        root_row = db.execute(
            "SELECT id, path, label, is_default FROM root_folders WHERE id=?",
            (root_folder_id,),
        ).fetchone()
        if not root_row:
            return AdoptUnmappedFolderResult(
                False,
                404,
                "Not Found",
                "Root folder not found",
            )
        root = dict(root_row)

        if quality_profile_id is not None:
            if not db.execute(
                "SELECT 1 FROM quality_profiles WHERE id=?", (quality_profile_id,)
            ).fetchone():
                return AdoptUnmappedFolderResult(
                    False, 400, "qualityProfileId not found"
                )
        else:
            quality_profile_id = _default_profile_id(db, "quality_profiles")

        if language_profile_id is not None:
            if not db.execute(
                "SELECT 1 FROM language_profiles WHERE id=?", (language_profile_id,)
            ).fetchone():
                return AdoptUnmappedFolderResult(
                    False, 400, "languageProfileId not found"
                )
        else:
            language_profile_id = _default_profile_id(db, "language_profiles")
        known_paths = _series_library_paths(db, root_folder_id)
        if any(
            _lexical_path(series_path) == _lexical_path(requested_path)
            for series_path in known_paths
        ):
            return AdoptUnmappedFolderResult(
                False,
                400,
                "path is already mapped",
                "Requested path is already assigned to a series",
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
    parent_norm = os.path.normcase(os.path.dirname(requested_norm))
    if parent_norm != root_norm or requested_norm == root_norm:
        return AdoptUnmappedFolderResult(
            False,
            400,
            "path is not an unmapped folder",
            "Requested path must be a direct child of the root folder",
        )
    if requested_norm in {_norm_path(path) for path in known_paths}:
        return AdoptUnmappedFolderResult(
            False,
            400,
            "path is already mapped",
            "Requested path is already assigned to a series",
        )

    folder_name = os.path.basename(requested_path)
    explicit_title = bool(title and title.strip())
    series_title = (title or folder_name).strip()
    if not series_title:
        return AdoptUnmappedFolderResult(False, 400, "title is required")
    search_pattern = (metadata_title or series_title).strip() or series_title
    vol_count_source = (
        metadata_source
        if metadata_source in ("anilist", "mangaupdates", "manual")
        else "manual"
    )
    precreation_snapshot = SeriesRescanSnapshot(
        series={},
        series_dir=requested_path,
        numbered=(),
        packs=(),
        chapters=(),
        chapters_by_volume={},
    )
    inventory = build_filesystem_inventory(precreation_snapshot)

    with get_db() as db:
        # Serialize the mapping check with creation so two adopters cannot claim
        # the same folder after both observed it as unmapped.
        db.execute("BEGIN IMMEDIATE")
        current_root_row = db.execute(
            "SELECT id,path FROM root_folders WHERE id=?",
            (root_folder_id,),
        ).fetchone()
        if not current_root_row:
            return AdoptUnmappedFolderResult(
                False,
                404,
                "Not Found",
                "Root folder not found",
            )
        current_root = dict(current_root_row)
        if current_root["path"] != root["path"]:
            return AdoptUnmappedFolderResult(
                False,
                409,
                "root folder changed",
                "Root folder configuration changed during adoption",
            )
        if _mapping_exists(db, root_folder_id, requested_path):
            return AdoptUnmappedFolderResult(
                False,
                400,
                "path is already mapped",
                "Requested path is already assigned to a series",
            )
        if (
            quality_profile_id is not None
            and not db.execute(
                "SELECT 1 FROM quality_profiles WHERE id=?",
                (quality_profile_id,),
            ).fetchone()
        ):
            return AdoptUnmappedFolderResult(False, 400, "qualityProfileId not found")
        if (
            language_profile_id is not None
            and not db.execute(
                "SELECT 1 FROM language_profiles WHERE id=?",
                (language_profile_id,),
            ).fetchone()
        ):
            return AdoptUnmappedFolderResult(False, 400, "languageProfileId not found")

        monitor_mode = "missing" if monitored else "none"
        cur = db.execute(
            "INSERT INTO series(title, search_pattern, anilist_id, mal_id, mu_id,"
            " cover_url, status, description, total_volumes, total_chapters,"
            " root_folder_id, folder_name, pub_year, enabled, monitored, monitor_mode,"
            " quality_profile_id, language_profile_id, vol_count_source,"
            " chapter_count_source)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                "anilist" if anilist_id else "manual",
            ),
        )
        if cur.lastrowid is None:
            raise RuntimeError("series insert did not return an id")
        series_id = cur.lastrowid
        record_initial_title(
            series_id,
            series_title,
            "manual" if explicit_title else "local",
            locked=True,
            db=db,
        )

        if total_volumes and total_volumes > 0:
            create_volume_stubs(db, series_id, total_volumes)
        snapshot = snapshot_series_rescan(db, series_id)
        if snapshot is None or _lexical_path(
            snapshot.series_dir or ""
        ) != _lexical_path(requested_path):
            raise RuntimeError("adopted series folder did not match requested path")
        reconciliation = reconcile_series_inventory(db, snapshot, inventory)
        series_row = db.execute(
            "SELECT id, title, search_pattern, root_folder_id, monitored,"
            " monitor_mode, quality_profile_id, language_profile_id,"
            " anilist_id, mal_id, mu_id, cover_url, status, description,"
            " total_volumes,total_chapters,pub_year,vol_count_source,"
            " chapter_count_source,folder_name"
            " FROM series WHERE id=?",
            (series_id,),
        ).fetchone()
        if not series_row:
            return AdoptUnmappedFolderResult(False, 500, "series adoption failed")
        series = dict(series_row)

    enrich_reconciled_files(reconciliation)
    return AdoptUnmappedFolderResult(
        True,
        200,
        payload={
            "series": {
                "id": series["id"],
                "title": series["title"],
                "searchPattern": series["search_pattern"],
                "rootFolderId": series["root_folder_id"],
                "folderName": series["folder_name"],
                "path": requested_path,
                "monitored": bool(series["monitored"]),
                "monitorMode": series["monitor_mode"] or "all",
                "qualityProfileId": series["quality_profile_id"],
                "languageProfileId": series["language_profile_id"],
                "anilistId": series["anilist_id"],
                "malId": series["mal_id"],
                "mangaUpdatesId": series["mu_id"],
                "coverUrl": series["cover_url"],
                "status": series["status"],
                "overview": series["description"],
                "totalVolumes": series["total_volumes"],
                "totalChapters": series["total_chapters"],
                "year": series["pub_year"],
                "volumeCountSource": series["vol_count_source"],
                "chapterCountSource": series["chapter_count_source"],
            },
            "rescan": reconciliation.result,
        },
    )
