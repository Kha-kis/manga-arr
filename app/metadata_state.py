"""Persistent metadata refresh state and catalogue-health helpers.

Provider calls happen outside SQLite transactions.  This module only records
their small attempt/success/failure transitions so retries and UI health survive
container restarts.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from shared import get_db


SOURCE_ANILIST = "anilist"
SOURCE_ALIASES = "anilist_aliases"
SOURCE_MANGAUPDATES = "mangaupdates"
SOURCE_CHAPTER_MAP = "chapter_map"
SOURCE_MANGADEX_MANIFEST = "mangadex_manifest"
SOURCE_COVER = "cover"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_details(details: dict[str, Any] | None) -> str | None:
    if details is None:
        return None
    return json.dumps(details, sort_keys=True, separators=(",", ":"))


def mark_series_attempt(series_id: int) -> str:
    now = utc_now_iso()
    with get_db() as db:
        db.execute(
            "UPDATE series SET metadata_status='refreshing',"
            " metadata_last_attempt=?, metadata_error=NULL WHERE id=?",
            (now, series_id),
        )
    return now


def finish_series_refresh(
    series_id: int,
    *,
    status: str,
    error: str | None = None,
    successful: bool,
) -> str:
    if status not in {"healthy", "degraded", "failed"}:
        raise ValueError(f"invalid metadata status: {status}")
    now = utc_now_iso()
    with get_db() as db:
        if successful:
            db.execute(
                "UPDATE series SET metadata_status=?, metadata_error=?,"
                " last_metadata_refresh=? WHERE id=?",
                (status, error, now, series_id),
            )
        else:
            db.execute(
                "UPDATE series SET metadata_status=?, metadata_error=? WHERE id=?",
                (status, error, series_id),
            )
    return now


def mark_source_attempt(series_id: int, source: str) -> str:
    now = utc_now_iso()
    with get_db() as db:
        db.execute(
            "INSERT INTO series_metadata_sources"
            " (series_id,source,status,last_attempt_at) VALUES(?,?,'refreshing',?)"
            " ON CONFLICT(series_id,source) DO UPDATE SET"
            " status='refreshing', last_attempt_at=excluded.last_attempt_at,"
            " error=NULL",
            (series_id, source, now),
        )
    return now


def mark_source_success(
    series_id: int,
    source: str,
    *,
    details: dict[str, Any] | None = None,
    degraded: bool = False,
    error: str | None = None,
) -> str:
    now = utc_now_iso()
    status = "degraded" if degraded else "healthy"
    with get_db() as db:
        db.execute(
            "INSERT INTO series_metadata_sources"
            " (series_id,source,status,last_attempt_at,last_success_at,"
            "  next_retry_at,failure_count,error,details)"
            " VALUES(?,?,?,?,?,NULL,0,?,?)"
            " ON CONFLICT(series_id,source) DO UPDATE SET"
            " status=excluded.status, last_attempt_at=excluded.last_attempt_at,"
            " last_success_at=excluded.last_success_at, next_retry_at=NULL,"
            " failure_count=0, error=excluded.error, details=excluded.details",
            (series_id, source, status, now, now, error, _json_details(details)),
        )
    return now


def mark_source_failure(
    series_id: int,
    source: str,
    error: str,
    *,
    details: dict[str, Any] | None = None,
    has_usable_cache: bool = False,
) -> str:
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    clean_error = f"{error}"[:500]
    with get_db() as db:
        row = db.execute(
            "SELECT failure_count,last_success_at FROM series_metadata_sources"
            " WHERE series_id=? AND source=?",
            (series_id, source),
        ).fetchone()
        failures = (int(row["failure_count"] or 0) if row else 0) + 1
        retry_minutes = min(24 * 60, 5 * (2 ** min(failures - 1, 8)))
        retry_at = (now_dt + timedelta(minutes=retry_minutes)).isoformat()
        status = (
            "degraded"
            if has_usable_cache or (row and row["last_success_at"])
            else "failed"
        )
        db.execute(
            "INSERT INTO series_metadata_sources"
            " (series_id,source,status,last_attempt_at,next_retry_at,"
            "  failure_count,error,details) VALUES(?,?,?,?,?,?,?,?)"
            " ON CONFLICT(series_id,source) DO UPDATE SET"
            " status=excluded.status, last_attempt_at=excluded.last_attempt_at,"
            " next_retry_at=excluded.next_retry_at,"
            " failure_count=excluded.failure_count, error=excluded.error,"
            " details=COALESCE(excluded.details,series_metadata_sources.details)",
            (
                series_id,
                source,
                status,
                now,
                retry_at,
                failures,
                clean_error,
                _json_details(details),
            ),
        )
    return retry_at


def source_retry_due(series_id: int, source: str) -> bool:
    with get_db() as db:
        row = db.execute(
            "SELECT next_retry_at FROM series_metadata_sources"
            " WHERE series_id=? AND source=?",
            (series_id, source),
        ).fetchone()
    if not row or not row["next_retry_at"]:
        return True
    try:
        retry_at = datetime.fromisoformat(row["next_retry_at"])
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return retry_at <= datetime.now(timezone.utc)
    except ValueError:
        return True


def metadata_retry_candidates(limit: int = 20) -> list[int]:
    """Return monitored series with pending or due provider work."""
    safe_limit = max(1, min(int(limit), 100))
    with get_db() as db:
        rows = db.execute(
            "SELECT s.id FROM series s WHERE s.monitored=1"
            " AND s.deleted_at IS NULL AND ("
            " COALESCE(s.metadata_status,'pending')='pending' OR EXISTS("
            "   SELECT 1 FROM series_metadata_sources ms"
            "   WHERE ms.series_id=s.id AND ("
            "     ms.status='pending' OR ("
            "       ms.status IN ('failed','degraded')"
            "       AND ms.next_retry_at IS NOT NULL"
            "       AND datetime(ms.next_retry_at)<=datetime('now')"
            "     )"
            "   )"
            " )) ORDER BY COALESCE(s.metadata_last_attempt,''),s.id LIMIT ?",
            (safe_limit,),
        ).fetchall()
    return [int(row["id"]) for row in rows]


def get_source_states(series_id: int) -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute(
            "SELECT source,status,last_attempt_at,last_success_at,next_retry_at,"
            " failure_count,error,details FROM series_metadata_sources"
            " WHERE series_id=? ORDER BY source",
            (series_id,),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            item["details"] = json.loads(item["details"]) if item["details"] else {}
        except (TypeError, ValueError):
            item["details"] = {}
        result.append(item)
    return result


def build_catalog_metadata_health(series_id: int) -> dict[str, Any]:
    """Return catalogue, provider, map, and cover health for one series."""
    with get_db() as db:
        row = db.execute(
            "SELECT id,title,anilist_id,mangadex_id,mal_id,mu_id,cover_url,"
            " description,total_volumes,total_chapters,chapter_vol_map,"
            " metadata_status,metadata_last_attempt,metadata_error,"
            " last_metadata_refresh,chapter_map_source,chapter_map_updated_at,"
            " cover_cached_url,cover_updated_at"
            " FROM series WHERE id=?",
            (series_id,),
        ).fetchone()
    if not row:
        return {"state": "unknown", "issues": ["series_not_found"], "sources": []}

    s = dict(row)
    issues: list[str] = []
    if not s["anilist_id"]:
        issues.append("missing_anilist_id")
    if not s["mangadex_id"]:
        issues.append("missing_mangadex_id")
    if not (s["description"] or "").strip():
        issues.append("missing_description")
    if not (s["cover_url"] or "").strip():
        issues.append("missing_cover_url")
    if not s["chapter_vol_map"]:
        issues.append("missing_chapter_map")

    from cover_images import cached_cover_is_valid

    cover_path = f"/config/covers/{series_id}.jpg"
    local_cover = cached_cover_is_valid(cover_path)
    if not local_cover:
        issues.append("missing_local_cover")

    stale = False
    if s["last_metadata_refresh"]:
        try:
            refreshed = datetime.fromisoformat(s["last_metadata_refresh"])
            if refreshed.tzinfo is None:
                refreshed = refreshed.replace(tzinfo=timezone.utc)
            stale = datetime.now(timezone.utc) - refreshed > timedelta(days=30)
        except ValueError:
            stale = True
    else:
        stale = True
    if stale:
        issues.append("stale_metadata")

    sources = get_source_states(series_id)
    failed_sources = [
        src["source"] for src in sources if src["status"] in {"failed", "degraded"}
    ]
    if failed_sources:
        issues.append("provider_failures")

    state = s["metadata_status"] or "pending"
    if state == "healthy" and issues:
        state = "degraded"
    return {
        "state": state,
        "issues": issues,
        "failed_sources": failed_sources,
        "sources": sources,
        "last_attempt": s["metadata_last_attempt"],
        "last_success": s["last_metadata_refresh"],
        "error": s["metadata_error"],
        "identifiers": {
            "anilist": s["anilist_id"],
            "mangadex": s["mangadex_id"],
            "mal": s["mal_id"],
            "mangaupdates": s["mu_id"],
        },
        "cover": {
            "available": local_cover,
            "source_url": s["cover_url"],
            "cached_url": s["cover_cached_url"],
            "updated_at": s["cover_updated_at"],
        },
        "chapter_map": {
            "available": bool(s["chapter_vol_map"]),
            "source": s["chapter_map_source"],
            "updated_at": s["chapter_map_updated_at"],
        },
    }
