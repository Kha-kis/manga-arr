"""Library rename planning and execution."""
from __future__ import annotations

import os

from events import add_history, log_event
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


def build_library_rename_preview() -> dict:
    """Return a read-only rename plan across all series with downloaded files."""
    with get_db() as db:
        rows = db.execute(
            """
            SELECT DISTINCT s.id
            FROM series s
            WHERE s.deleted_at IS NULL
              AND (
                EXISTS (
                  SELECT 1
                  FROM volumes v
                  WHERE v.series_id=s.id
                    AND v.status='downloaded'
                    AND v.import_path IS NOT NULL
                )
                OR EXISTS (
                  SELECT 1
                  FROM chapters c
                  WHERE c.series_id=s.id
                    AND c.status='downloaded'
                    AND c.import_path IS NOT NULL
                )
              )
            ORDER BY s.title COLLATE NOCASE, s.id
            """
        ).fetchall()
        series_ids = [row["id"] for row in rows]

    series_plans = []
    for series_id in series_ids:
        plan = build_series_rename_preview(series_id)
        if not plan or not plan["total"]:
            continue
        series_plans.append(plan)

    total = sum(plan["total"] for plan in series_plans)
    changed = sum(plan["changed"] for plan in series_plans)
    renameable = sum(plan["renameable"] for plan in series_plans)
    conflicts = sum(plan["conflicts"] for plan in series_plans)
    return {
        "fileFormat": get_cfg("file_format", ""),
        "chapterFormat": get_cfg("chapter_format", ""),
        "folderFormat": get_cfg("folder_format", ""),
        "seriesCount": len(series_plans),
        "seriesWithChanges": sum(1 for plan in series_plans if plan["changed"]),
        "total": total,
        "changed": changed,
        "renameable": renameable,
        "conflicts": conflicts,
        "series": series_plans,
    }


def execute_series_rename(
    series_id: int,
    *,
    volume_ids: set[int] | None = None,
    chapter_ids: set[int] | None = None,
) -> dict | None:
    """Rename selected downloaded files and update their stored import paths.

    If neither ID set is provided, every currently renameable item is executed.
    The plan is recalculated immediately before each run, so conflict checks are
    based on current DB and filesystem state rather than a stale preview.
    """
    preview = build_series_rename_preview(series_id)
    if preview is None:
        return None

    filter_active = volume_ids is not None or chapter_ids is not None
    selected = []
    skipped = []
    for item in preview["items"]:
        if filter_active:
            if item["type"] == "volume":
                wanted = volume_ids is not None and item["id"] in volume_ids
            else:
                wanted = chapter_ids is not None and item["id"] in chapter_ids
            if not wanted:
                continue
        if item["canRename"]:
            selected.append(item)
        else:
            skipped.append(
                {
                    "type": item["type"],
                    "id": item["id"],
                    "oldPath": item["oldPath"],
                    "newPath": item["newPath"],
                    "status": "skipped",
                    "conflict": item["conflict"],
                }
            )

    renamed = []
    errors = []
    for item in selected:
        old_path = item["oldPath"]
        new_path = item["newPath"]
        if not old_path or not new_path:
            errors.append(
                {
                    "type": item["type"],
                    "id": item["id"],
                    "oldPath": old_path,
                    "newPath": new_path,
                    "status": "error",
                    "message": "missing rename path",
                }
            )
            continue

        moved = False
        try:
            if not os.path.exists(old_path):
                raise FileNotFoundError(old_path)
            if os.path.exists(new_path) and _norm(new_path) != _norm(old_path):
                raise FileExistsError(new_path)
            os.makedirs(os.path.dirname(new_path), exist_ok=True)
            os.rename(old_path, new_path)
            moved = True

            with get_db() as db:
                if item["type"] == "volume":
                    row = db.execute(
                        "SELECT import_path FROM volumes WHERE id=? AND series_id=?",
                        (item["id"], series_id),
                    ).fetchone()
                    if not row:
                        raise RuntimeError("volume row no longer exists")
                    if _norm(row["import_path"]) != _norm(old_path):
                        raise RuntimeError("volume import path changed")
                    db.execute(
                        "UPDATE volumes SET import_path=? WHERE id=? AND series_id=?",
                        (new_path, item["id"], series_id),
                    )
                else:
                    row = db.execute(
                        "SELECT import_path FROM chapters WHERE id=? AND series_id=?",
                        (item["id"], series_id),
                    ).fetchone()
                    if not row:
                        raise RuntimeError("chapter row no longer exists")
                    if _norm(row["import_path"]) != _norm(old_path):
                        raise RuntimeError("chapter import path changed")
                    db.execute(
                        "UPDATE chapters SET import_path=? WHERE id=? AND series_id=?",
                        (new_path, item["id"], series_id),
                    )
                add_history(
                    db,
                    "file_renamed",
                    series_id,
                    preview["seriesTitle"],
                    item["label"],
                    source_title=item["oldName"],
                    data={
                        "type": item["type"],
                        "old_path": old_path,
                        "new_path": new_path,
                    },
                )
                log_event(
                    "rename",
                    f"Renamed {item['label']}: {item['oldName']} -> {item['newName']}",
                    series_id,
                    db=db,
                )
            renamed.append(
                {
                    "type": item["type"],
                    "id": item["id"],
                    "oldPath": old_path,
                    "newPath": new_path,
                    "status": "renamed",
                }
            )
        except Exception as exc:
            if moved:
                try:
                    if not os.path.exists(old_path) and os.path.exists(new_path):
                        os.rename(new_path, old_path)
                except Exception as rollback_exc:
                    log_event(
                        "error",
                        "Rename rollback failed for "
                        f"{new_path}: {rollback_exc}",
                        series_id,
                    )
            errors.append(
                {
                    "type": item["type"],
                    "id": item["id"],
                    "oldPath": old_path,
                    "newPath": new_path,
                    "status": "error",
                    "message": str(exc),
                }
            )
            log_event("error", f"Rename failed for {old_path}: {exc}", series_id)

    return {
        "seriesId": preview["seriesId"],
        "seriesTitle": preview["seriesTitle"],
        "requested": len(selected) + len(skipped),
        "renamed": len(renamed),
        "skipped": len(skipped),
        "errors": len(errors),
        "results": renamed + skipped + errors,
    }


def execute_library_rename(
    *,
    series_ids: set[int] | None = None,
    volume_ids: set[int] | None = None,
    chapter_ids: set[int] | None = None,
) -> dict:
    """Rename selected downloaded files across the library.

    This intentionally delegates each series to execute_series_rename() so
    file-system checks and DB updates remain series-scoped and current.
    """
    preview = build_library_rename_preview()
    target_series_ids = [
        plan["seriesId"]
        for plan in preview["series"]
        if series_ids is None or plan["seriesId"] in series_ids
    ]

    series_results = []
    for series_id in target_series_ids:
        result = execute_series_rename(
            series_id,
            volume_ids=volume_ids,
            chapter_ids=chapter_ids,
        )
        if result is None:
            continue
        if result["requested"] or (volume_ids is None and chapter_ids is None):
            series_results.append(result)

    requested = sum(result["requested"] for result in series_results)
    renamed = sum(result["renamed"] for result in series_results)
    skipped = sum(result["skipped"] for result in series_results)
    errors = sum(result["errors"] for result in series_results)
    return {
        "requested": requested,
        "renamed": renamed,
        "skipped": skipped,
        "errors": errors,
        "seriesCount": len(series_results),
        "series": series_results,
    }
