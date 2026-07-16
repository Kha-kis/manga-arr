"""Field-level metadata provenance, candidates, and operator selection."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from shared import get_db


FIELD_CONFIG: dict[str, dict[str, str | None]] = {
    "title": {"column": "title", "label": "Title", "source_column": None},
    "anilist_id": {
        "column": "anilist_id",
        "label": "AniList ID",
        "source_column": None,
    },
    "mal_id": {"column": "mal_id", "label": "MyAnimeList ID", "source_column": None},
    "mangadex_id": {
        "column": "mangadex_id",
        "label": "MangaDex ID",
        "source_column": None,
    },
    "mu_id": {"column": "mu_id", "label": "MangaUpdates ID", "source_column": None},
    "cover_url": {"column": "cover_url", "label": "Cover", "source_column": None},
    "status": {
        "column": "status",
        "label": "Publication status",
        "source_column": None,
    },
    "description": {
        "column": "description",
        "label": "Description",
        "source_column": None,
    },
    "pub_year": {
        "column": "pub_year",
        "label": "Publication year",
        "source_column": None,
    },
    "total_volumes": {
        "column": "total_volumes",
        "label": "Total volumes",
        "source_column": "vol_count_source",
    },
    "total_chapters": {
        "column": "total_chapters",
        "label": "Total chapters",
        "source_column": "chapter_count_source",
    },
    "chapter_vol_map": {
        "column": "chapter_vol_map",
        "label": "Chapter to volume map",
        "source_column": "chapter_map_source",
    },
}

_DEFAULT_SOURCES = {
    "anilist_id": "anilist",
    "mangadex_id": "mangadex",
    "mu_id": "mangaupdates",
    "cover_url": "anilist",
    "status": "anilist",
    "description": "anilist",
    "pub_year": "anilist",
    "total_volumes": "anilist",
    "total_chapters": "anilist",
}

_SOURCE_PRIORITY = {
    "manual": 1000,
    "local": 900,
    "google_books": 850,
    "wikipedia": 825,
    "mangaupdates": 800,
    "mangadex": 775,
    "kitsu": 700,
    "cbz": 675,
    "anilist": 650,
    "legacy": 100,
}

_FIELD_SOURCE_PRIORITY = {
    "chapter_vol_map": {
        "manual": 1000,
        "mangadex": 850,
        "kitsu": 800,
        "cbz": 750,
        "legacy": 100,
    },
    "total_chapters": {"manual": 1000, "local": 850, "anilist": 700},
    "total_volumes": {
        "manual": 1000,
        "google_books": 900,
        "wikipedia": 875,
        "mangaupdates": 800,
        "local": 750,
        "anilist": 700,
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _value_from_storage(field_name: str, value: Any) -> Any:
    if field_name == "chapter_vol_map" and isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else value
        except (TypeError, ValueError):
            return value
    return value


def _value_for_storage(field_name: str, value: Any) -> Any:
    if field_name == "chapter_vol_map" and isinstance(value, dict):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return value


def _encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _decode(value_json: str | None) -> Any:
    if value_json is None:
        return None
    try:
        return json.loads(value_json)
    except (TypeError, ValueError):
        return value_json


def _source_for_row(field_name: str, row: dict) -> str:
    source_column = FIELD_CONFIG[field_name]["source_column"]
    if source_column:
        return row.get(str(source_column)) or _DEFAULT_SOURCES.get(field_name, "legacy")
    return _DEFAULT_SOURCES.get(field_name, "legacy")


def _priority(field_name: str, source: str) -> int:
    return _FIELD_SOURCE_PRIORITY.get(field_name, {}).get(
        source, _SOURCE_PRIORITY.get(source, 500)
    )


def _display_value(field_name: str, value: Any) -> str:
    if value is None or value == "":
        return "Not set"
    if field_name == "chapter_vol_map" and isinstance(value, dict):
        return f"{len(value)} mapped chapters"
    text = str(value).strip().replace("\n", " ")
    limit = 92 if field_name == "description" else 68
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


def backfill_metadata_provenance(db) -> None:
    """Seed provenance for existing and newly inserted legacy series rows."""
    columns = ",".join(
        dict.fromkeys(
            [
                "id",
                "added_at",
                "last_metadata_refresh",
                *[str(config["column"]) for config in FIELD_CONFIG.values()],
                *[
                    str(config["source_column"])
                    for config in FIELD_CONFIG.values()
                    if config["source_column"]
                ],
            ]
        )
    )
    rows = db.execute(f"SELECT {columns} FROM series").fetchall()
    now = _now()
    for raw_row in rows:
        row = dict(raw_row)
        selected_at = row.get("last_metadata_refresh") or row.get("added_at") or now
        for field_name, config in FIELD_CONFIG.items():
            value = _value_from_storage(field_name, row.get(str(config["column"])))
            source = _source_for_row(field_name, row)
            locked = int(source == "manual")
            db.execute(
                "INSERT OR IGNORE INTO series_metadata_fields"
                "(series_id,field_name,value_json,selected_source,locked,selected_at)"
                " VALUES(?,?,?,?,?,?)",
                (row["id"], field_name, _encode(value), source, locked, selected_at),
            )
            if value is not None and value != "":
                db.execute(
                    "INSERT OR IGNORE INTO series_metadata_candidates"
                    "(series_id,field_name,source,value_json,confidence,fetched_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (row["id"], field_name, source, _encode(value), None, selected_at),
                )


def record_metadata_candidates(
    series_id: int,
    source: str,
    values: dict[str, Any],
    *,
    confidence: float | None = None,
    db=None,
) -> None:
    now = _now()

    def write(connection) -> None:
        for field_name, value in values.items():
            if field_name not in FIELD_CONFIG or value is None or value == "":
                continue
            value = _value_from_storage(field_name, value)
            connection.execute(
                "INSERT INTO series_metadata_candidates"
                "(series_id,field_name,source,value_json,confidence,fetched_at)"
                " VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(series_id,field_name,source) DO UPDATE SET"
                " value_json=excluded.value_json,confidence=excluded.confidence,"
                " fetched_at=excluded.fetched_at",
                (series_id, field_name, source, _encode(value), confidence, now),
            )

    if db is not None:
        write(db)
    else:
        with get_db() as connection:
            write(connection)


def _record_selection(
    db,
    series_id: int,
    field_name: str,
    value: Any,
    source: str,
    *,
    locked: bool | None = None,
) -> None:
    if field_name not in FIELD_CONFIG:
        raise ValueError(f"unsupported metadata field: {field_name}")
    value = _value_from_storage(field_name, value)
    lock_value = int(source == "manual") if locked is None else int(locked)
    now = _now()
    db.execute(
        "INSERT INTO series_metadata_fields"
        "(series_id,field_name,value_json,selected_source,locked,selected_at)"
        " VALUES(?,?,?,?,?,?)"
        " ON CONFLICT(series_id,field_name) DO UPDATE SET"
        " value_json=excluded.value_json,selected_source=excluded.selected_source,"
        " locked=excluded.locked,selected_at=excluded.selected_at",
        (series_id, field_name, _encode(value), source, lock_value, now),
    )


def record_metadata_selections(
    series_id: int,
    values: dict[str, Any],
    sources: dict[str, str],
    *,
    locks: dict[str, bool] | None = None,
    db=None,
) -> None:
    def write(connection) -> None:
        for field_name, value in values.items():
            if field_name in FIELD_CONFIG:
                lock_state = (
                    locks[field_name]
                    if locks is not None and field_name in locks
                    else None
                )
                _record_selection(
                    connection,
                    series_id,
                    field_name,
                    value,
                    sources.get(field_name, "legacy"),
                    locked=lock_state,
                )

    if db is not None:
        write(db)
    else:
        with get_db() as connection:
            write(connection)


def record_manual_metadata(
    series_id: int, values: dict[str, Any], *, db=None, locked: bool = True
) -> None:
    def write(connection) -> None:
        for field_name, value in values.items():
            if field_name in FIELD_CONFIG:
                _record_selection(
                    connection,
                    series_id,
                    field_name,
                    value,
                    "manual",
                    locked=locked,
                )
                record_metadata_candidates(
                    series_id,
                    "manual",
                    {field_name: value},
                    db=connection,
                )

    if db is not None:
        write(db)
    else:
        with get_db() as connection:
            write(connection)


def get_metadata_field_states(series_id: int) -> list[dict[str, Any]]:
    columns = ",".join(
        dict.fromkeys(str(config["column"]) for config in FIELD_CONFIG.values())
    )
    with get_db() as db:
        row = db.execute(
            f"SELECT {columns} FROM series WHERE id=?", (series_id,)
        ).fetchone()
        if not row:
            return []
        current_row = dict(row)
        selected_rows = {
            item["field_name"]: dict(item)
            for item in db.execute(
                "SELECT field_name,value_json,selected_source,locked,selected_at"
                " FROM series_metadata_fields WHERE series_id=?",
                (series_id,),
            ).fetchall()
        }
        candidate_rows = [
            dict(item)
            for item in db.execute(
                "SELECT field_name,source,value_json,confidence,fetched_at"
                " FROM series_metadata_candidates WHERE series_id=?",
                (series_id,),
            ).fetchall()
        ]

    by_field: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidate_rows:
        candidate["value"] = _decode(candidate["value_json"])
        candidate["display_value"] = _display_value(
            candidate["field_name"], candidate["value"]
        )
        by_field.setdefault(candidate["field_name"], []).append(candidate)

    result: list[dict[str, Any]] = []
    for field_name, config in FIELD_CONFIG.items():
        current = _value_from_storage(
            field_name, current_row.get(str(config["column"]))
        )
        selected = selected_rows.get(field_name) or {}
        selected_source = selected.get("selected_source") or "legacy"
        locked = bool(selected.get("locked", selected_source == "manual"))

        def candidate_priority(item: dict[str, Any]) -> tuple[int, str]:
            priority = _priority(field_name, item["source"])
            if item["source"] == "manual" and not locked:
                priority = 0
            return (-priority, item["source"])

        candidates = sorted(
            by_field.get(field_name, []),
            key=candidate_priority,
        )
        recommended = candidates[0] if candidates else None
        candidate_values = {
            _encode(item["value"])
            for item in candidates
            if item["value"] is not None
            and item["value"] != ""
            and item["source"] != "legacy"
            and (item["source"] != "manual" or locked)
        }
        pending = bool(
            recommended
            and _encode(recommended["value"]) != _encode(current)
            and not locked
        )
        for candidate in candidates:
            candidate["is_current"] = _encode(candidate["value"]) == _encode(current)
            candidate["is_recommended"] = candidate is recommended
            candidate["is_decrease"] = bool(
                field_name in {"total_volumes", "total_chapters"}
                and isinstance(current, (int, float))
                and isinstance(candidate["value"], (int, float))
                and candidate["value"] < current
            )
        alternative_count = sum(
            1 for candidate in candidates if not candidate["is_current"]
        )
        result.append(
            {
                "field_name": field_name,
                "label": config["label"],
                "value": current,
                "display_value": _display_value(field_name, current),
                "selected_source": selected_source,
                "selected_at": selected.get("selected_at"),
                "locked": locked,
                "pending": pending,
                "conflict": len(candidate_values) > 1,
                "recommended": recommended,
                "candidates": candidates,
                "alternative_count": alternative_count,
            }
        )
    return result


def metadata_field_is_locked(series_id: int, field_name: str) -> bool:
    if field_name not in FIELD_CONFIG:
        raise ValueError(f"unsupported metadata field: {field_name}")
    with get_db() as db:
        row = db.execute(
            "SELECT locked FROM series_metadata_fields"
            " WHERE series_id=? AND field_name=?",
            (series_id, field_name),
        ).fetchone()
    return bool(row["locked"]) if row else False


def set_metadata_field_lock(series_id: int, field_name: str, locked: bool) -> None:
    if field_name not in FIELD_CONFIG:
        raise ValueError(f"unsupported metadata field: {field_name}")
    states = {item["field_name"]: item for item in get_metadata_field_states(series_id)}
    state = states.get(field_name)
    if not state:
        raise ValueError("series not found")
    with get_db() as db:
        _record_selection(
            db,
            series_id,
            field_name,
            state["value"],
            state["selected_source"],
            locked=locked,
        )


def apply_metadata_candidate(
    series_id: int,
    field_name: str,
    source: str,
    *,
    allow_decrease: bool = False,
) -> dict[str, Any]:
    if field_name not in FIELD_CONFIG:
        raise ValueError(f"unsupported metadata field: {field_name}")
    config = FIELD_CONFIG[field_name]
    with get_db() as db:
        selected = db.execute(
            "SELECT locked FROM series_metadata_fields WHERE series_id=? AND field_name=?",
            (series_id, field_name),
        ).fetchone()
        if selected and selected["locked"]:
            raise ValueError("field is locked; unlock it before accepting a candidate")
        candidate = db.execute(
            "SELECT value_json FROM series_metadata_candidates"
            " WHERE series_id=? AND field_name=? AND source=?",
            (series_id, field_name, source),
        ).fetchone()
        if not candidate:
            raise ValueError("metadata candidate not found")
        value = _decode(candidate["value_json"])
        column = str(config["column"])
        current_row = db.execute(
            f"SELECT {column} FROM series WHERE id=?", (series_id,)
        ).fetchone()
        if not current_row:
            raise ValueError("series not found")
        current = _value_from_storage(field_name, current_row[column])
        if (
            field_name in {"total_volumes", "total_chapters"}
            and isinstance(current, (int, float))
            and isinstance(value, (int, float))
            and value < current
            and not allow_decrease
        ):
            raise ValueError("lower counts require explicit confirmation")

        stored_value = _value_for_storage(field_name, value)
        assignments = [f"{column}=?"]
        params: list[Any] = [stored_value]
        source_column = config["source_column"]
        if source_column:
            assignments.append(f"{source_column}=?")
            params.append(source)
        if field_name == "chapter_vol_map":
            assignments.append("chapter_map_updated_at=?")
            params.append(_now())
        params.append(series_id)
        db.execute(f"UPDATE series SET {', '.join(assignments)} WHERE id=?", params)
        _record_selection(db, series_id, field_name, value, source, locked=False)

        if field_name == "total_volumes" and value:
            from volumes import create_volume_stubs

            create_volume_stubs(db, series_id, int(value))
        elif field_name == "chapter_vol_map" and value:
            from metadata_enrichment import populate_chapters

            populate_chapters(db, series_id)

    return {"field_name": field_name, "source": source, "value": value}


def apply_recommended_candidates(series_id: int) -> dict[str, Any]:
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for state in get_metadata_field_states(series_id):
        candidate = state["recommended"]
        if not candidate or not state["pending"]:
            continue
        if state["conflict"]:
            skipped.append({"field_name": state["field_name"], "reason": "conflict"})
            continue
        if candidate["is_decrease"]:
            skipped.append({"field_name": state["field_name"], "reason": "decrease"})
            continue
        applied.append(
            apply_metadata_candidate(
                series_id, state["field_name"], candidate["source"]
            )
        )
    return {"applied": applied, "skipped": skipped}


def build_metadata_repair_report(series_id: int) -> dict[str, Any]:
    fields = get_metadata_field_states(series_id)
    return {
        "series_id": series_id,
        "fields": fields,
        "pending_count": sum(1 for field in fields if field["pending"]),
        "conflict_count": sum(1 for field in fields if field["conflict"]),
        "locked_count": sum(1 for field in fields if field["locked"]),
        "candidate_count": sum(len(field["candidates"]) for field in fields),
    }
