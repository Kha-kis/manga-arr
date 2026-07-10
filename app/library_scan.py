"""Read-only library folder discovery."""
from __future__ import annotations

import os

from files import MANGA_EXTENSIONS
from rescan import _series_library_dir
from shared import get_db


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
