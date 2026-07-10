"""Read-only library rename planning."""
from __future__ import annotations

import os

from files import build_chapter_label, build_filename
from rescan import _series_library_dir
from shared import build_volume_label, get_cfg, get_db


def _norm(path: str | None) -> str:
    return os.path.normcase(os.path.abspath(path or ""))


def _rename_item(
    *,
    item_type: str,
    item_id: int,
    series_title: str,
    series_dir: str | None,
    import_path: str,
    volume_num: float | None,
    chapter_num: float | None,
    chapter_range_end: float | None = None,
    vol_range_start: float | None = None,
    vol_range_end: float | None = None,
    pack_type: str | None = None,
    pub_year: int | None = None,
) -> dict:
    old_path = import_path
    old_name = os.path.basename(old_path)
    target_name = build_filename(
        series_title,
        volume_num,
        old_name,
        pub_year=pub_year,
        chapter_num=chapter_num,
    )
    target_path = os.path.join(series_dir, target_name) if series_dir else None
    source_exists = os.path.exists(old_path)
    target_exists = (
        bool(target_path)
        and os.path.exists(target_path)
        and _norm(target_path) != _norm(old_path)
    )
    if item_type == "chapter":
        label = build_chapter_label(chapter_num, chapter_range_end)
    else:
        vol_range = (
            (vol_range_start, vol_range_end)
            if vol_range_start is not None and vol_range_end is not None
            else None
        )
        label = build_volume_label(volume_num, vol_range, pack_type)

    conflict = None
    if not series_dir:
        conflict = "series_path_unavailable"
    elif not source_exists:
        conflict = "source_missing"
    elif target_exists:
        conflict = "target_exists"

    path_changed = bool(target_path) and _norm(target_path) != _norm(old_path)
    return {
        "type": item_type,
        "id": item_id,
        "volumeId": item_id if item_type == "volume" else None,
        "chapterId": item_id if item_type == "chapter" else None,
        "label": label,
        "oldPath": old_path,
        "newPath": target_path,
        "oldName": old_name,
        "newName": target_name if target_path else None,
        "sourceExists": source_exists,
        "targetExists": target_exists,
        "pathChanged": path_changed,
        "conflict": conflict,
        "canRename": path_changed and conflict is None,
    }


def build_series_rename_preview(series_id: int) -> dict | None:
    """Return a read-only rename plan for downloaded files in one series."""
    with get_db() as db:
        series = db.execute(
            "SELECT id, title, pub_year FROM series WHERE id=? AND deleted_at IS NULL",
            (series_id,),
        ).fetchone()
        if not series:
            return None
        series_dir = _series_library_dir(db, series_id)
        volume_rows = db.execute(
            """
            SELECT id, volume_num, vol_range_start, vol_range_end, pack_type,
                   import_path
            FROM volumes
            WHERE series_id=? AND status='downloaded' AND import_path IS NOT NULL
            ORDER BY volume_num, id
            """,
            (series_id,),
        ).fetchall()
        chapter_rows = db.execute(
            """
            SELECT c.id, c.chapter_num, c.chapter_range_end, c.import_path,
                   v.volume_num
            FROM chapters c
            LEFT JOIN volumes v ON v.id=c.volume_id
            WHERE c.series_id=? AND c.status='downloaded' AND c.import_path IS NOT NULL
            ORDER BY c.chapter_num, c.id
            """,
            (series_id,),
        ).fetchall()

        title = series["title"]
        pub_year = series["pub_year"]
        items = []
        for row in volume_rows:
            items.append(
                _rename_item(
                    item_type="volume",
                    item_id=row["id"],
                    series_title=title,
                    series_dir=series_dir,
                    import_path=row["import_path"],
                    volume_num=row["volume_num"],
                    chapter_num=None,
                    vol_range_start=row["vol_range_start"],
                    vol_range_end=row["vol_range_end"],
                    pack_type=row["pack_type"],
                    pub_year=pub_year,
                )
            )
        for row in chapter_rows:
            items.append(
                _rename_item(
                    item_type="chapter",
                    item_id=row["id"],
                    series_title=title,
                    series_dir=series_dir,
                    import_path=row["import_path"],
                    volume_num=row["volume_num"],
                    chapter_num=row["chapter_num"],
                    chapter_range_end=row["chapter_range_end"],
                    pub_year=pub_year,
                )
            )

    by_target: dict[str, list[dict]] = {}
    for item in items:
        if item["newPath"]:
            by_target.setdefault(_norm(item["newPath"]), []).append(item)
    for duplicate_items in by_target.values():
        if len(duplicate_items) < 2:
            continue
        for item in duplicate_items:
            item["conflict"] = "duplicate_target"
            item["canRename"] = False

    renameable = sum(1 for item in items if item["canRename"])
    changed = sum(1 for item in items if item["pathChanged"])
    conflicts = sum(1 for item in items if item["conflict"])
    return {
        "seriesId": series_id,
        "seriesTitle": title,
        "seriesPath": series_dir,
        "fileFormat": get_cfg("file_format", ""),
        "chapterFormat": get_cfg("chapter_format", ""),
        "folderFormat": get_cfg("folder_format", ""),
        "total": len(items),
        "changed": changed,
        "renameable": renameable,
        "conflicts": conflicts,
        "items": items,
    }
