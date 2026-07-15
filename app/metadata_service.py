"""Unified series metadata lifecycle.

All add, manual refresh, bulk refresh, and scheduled refresh paths converge
here. Provider I/O is performed without an open database connection; each
result is committed in a short transaction and recorded in persistent source
state.
"""
from __future__ import annotations

import asyncio
import math
import os
from collections import Counter
from typing import Any

from cover_images import COVERS_DIR, cached_cover_is_valid, download_cover
from events import log_event
from metadata import MetadataProviderError, anilist_search, fetch_anilist_by_id
from metadata_enrichment import (
    _NON_STANDARD_STUB_EDITIONS,
    fetch_edition_volume_count,
    fetch_mu_metadata,
    refresh_mangadex_map,
)
from metadata_state import (
    SOURCE_ALIASES,
    SOURCE_ANILIST,
    SOURCE_COVER,
    SOURCE_MANGADEX_MANIFEST,
    SOURCE_MANGAUPDATES,
    finish_series_refresh,
    mark_series_attempt,
    mark_source_attempt,
    mark_source_failure,
    mark_source_success,
    source_retry_due,
    utc_now_iso,
)
from parsing import normalize
from shared import get_db
from volumes import create_volume_stubs


_REFRESH_LOCKS: dict[int, asyncio.Lock] = {}
_REFRESH_LOCKS_GUARD = asyncio.Lock()
_PROTECTED_COUNT_SOURCES = {"manual", "google_books", "wikipedia"}


async def _refresh_lock(series_id: int) -> asyncio.Lock:
    async with _REFRESH_LOCKS_GUARD:
        return _REFRESH_LOCKS.setdefault(series_id, asyncio.Lock())


def _title_f1(left: str, right: str) -> float:
    left_words = set(normalize(left).split())
    right_words = set(normalize(right).split())
    if not left_words or not right_words:
        return 0.0
    overlap = left_words & right_words
    recall = len(overlap) / len(left_words)
    precision = len(overlap) / len(right_words)
    return 2 * recall * precision / (recall + precision) if overlap else 0.0


async def _resolve_anilist_record(series: dict) -> dict:
    if series.get("anilist_id"):
        return await fetch_anilist_by_id(int(series["anilist_id"]))

    results = await anilist_search(series["title"], strict=True)
    if not results:
        raise MetadataProviderError("AniList returned no candidates")
    best = max(
        results,
        key=lambda item: max(
            _title_f1(series["title"], item.get("title") or ""),
            _title_f1(series["title"], item.get("romaji_title") or ""),
        ),
    )
    confidence = max(
        _title_f1(series["title"], best.get("title") or ""),
        _title_f1(series["title"], best.get("romaji_title") or ""),
    )
    if confidence < 0.85:
        raise MetadataProviderError(
            f"AniList identity match was not confident ({confidence:.0%})"
        )
    return best


def _useful_alias(alias: str, main_title: str) -> bool:
    if not alias or len(alias.strip()) < 4:
        return False
    if normalize(alias) == normalize(main_title):
        return False
    latin = sum(1 for char in alias if char.isascii() and char.isalpha())
    return latin >= max(1, int(len(alias.replace(" ", "")) * 0.4))


def _store_aliases_and_genres(series_id: int, main_title: str, record: dict) -> dict:
    candidates = [record.get("romaji_title") or "", *(record.get("aliases") or [])]
    aliases = sorted(
        {alias.strip() for alias in candidates if _useful_alias(alias, main_title)},
        key=str.casefold,
    )
    genres = sorted(
        {str(genre).strip().lower() for genre in (record.get("genres") or []) if str(genre).strip()}
    )[:8]
    with get_db() as db:
        db.execute(
            "DELETE FROM series_aliases WHERE series_id=? AND source='anilist'",
            (series_id,),
        )
        for alias in aliases:
            db.execute(
                "INSERT INTO series_aliases(series_id,alias,source) VALUES(?,?,'anilist')"
                " ON CONFLICT(series_id,alias) DO NOTHING",
                (series_id, alias),
            )
        db.execute(
            "DELETE FROM series_tags WHERE series_id=? AND source='anilist'",
            (series_id,),
        )
        for genre in genres:
            db.execute(
                "INSERT INTO series_tags(series_id,tag,source) VALUES(?,?,'anilist')"
                " ON CONFLICT(series_id,tag) DO NOTHING",
                (series_id, genre),
            )
    return {"aliases": len(aliases), "genres": len(genres)}


def _apply_anilist_record(series_id: int, record: dict) -> list[str]:
    """Apply canonical fields while preserving manual and observed counts."""
    with get_db() as db:
        row = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
        if not row:
            raise ValueError(f"series {series_id} not found")
        current = dict(row)
        max_downloaded_volume = db.execute(
            "SELECT MAX(volume_num) FROM volumes WHERE series_id=?"
            " AND status='downloaded' AND COALESCE(is_special,0)=0",
            (series_id,),
        ).fetchone()[0]
        max_downloaded_chapter = db.execute(
            "SELECT MAX(COALESCE(chapter_range_end,chapter_num)) FROM chapters"
            " WHERE series_id=? AND status='downloaded'",
            (series_id,),
        ).fetchone()[0]

        volume_source = current.get("vol_count_source") or "anilist"
        current_volumes = current.get("total_volumes") or 0
        incoming_volumes = int(record["volumes"] or 0)
        observed_volumes = int(math.ceil(float(max_downloaded_volume or 0)))
        if volume_source in _PROTECTED_COUNT_SOURCES:
            total_volumes = current_volumes or None
            next_volume_source = volume_source
        else:
            total_volumes = max(
                current_volumes, incoming_volumes, observed_volumes
            ) or None
            next_volume_source = (
                volume_source
                if volume_source == "mangaupdates" and current_volumes >= (total_volumes or 0)
                else "anilist"
            )

        chapter_source = current.get("chapter_count_source") or "anilist"
        current_chapters = current.get("total_chapters") or 0
        incoming_chapters = int(record["chapters"] or 0)
        observed_chapters = int(math.ceil(float(max_downloaded_chapter or 0)))
        if chapter_source == "manual":
            total_chapters = current_chapters or None
            next_chapter_source = chapter_source
        else:
            total_chapters = max(
                current_chapters, incoming_chapters, observed_chapters
            ) or None
            next_chapter_source = (
                "local"
                if observed_chapters > incoming_chapters
                or (
                    chapter_source == "local"
                    and current_chapters > incoming_chapters
                )
                else "anilist"
            )

        values = {
            "anilist_id": record.get("anilist_id") or current.get("anilist_id"),
            "mal_id": record.get("mal_id") or current.get("mal_id"),
            "cover_url": record.get("cover_url") or current.get("cover_url"),
            "status": record.get("status") or current.get("status"),
            "description": record.get("description") or current.get("description"),
            "pub_year": record.get("pub_year") or current.get("pub_year"),
            "total_volumes": total_volumes,
            "total_chapters": total_chapters,
            "vol_count_source": next_volume_source,
            "chapter_count_source": next_chapter_source,
        }
        changed = [key for key, value in values.items() if current.get(key) != value]
        db.execute(
            "UPDATE series SET anilist_id=?,mal_id=?,cover_url=?,status=?,"
            " description=?,pub_year=?,total_volumes=?,total_chapters=?,"
            " vol_count_source=?,chapter_count_source=? WHERE id=?",
            (
                values["anilist_id"],
                values["mal_id"],
                values["cover_url"],
                values["status"],
                values["description"],
                values["pub_year"],
                values["total_volumes"],
                values["total_chapters"],
                values["vol_count_source"],
                values["chapter_count_source"],
                series_id,
            ),
        )
        edition = current.get("edition_type") or "standard"
        if total_volumes and edition not in _NON_STANDARD_STUB_EDITIONS:
            create_volume_stubs(db, series_id, int(total_volumes))
        if values["status"] in {"FINISHED", "CANCELLED"} and (
            current.get("update_strategy") or "always"
        ) == "always":
            db.execute(
                "UPDATE series SET update_strategy='once' WHERE id=?", (series_id,)
            )
    return changed


def _series_snapshot(series_id: int) -> dict | None:
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM series WHERE id=? AND deleted_at IS NULL", (series_id,)
        ).fetchone()
    return dict(row) if row else None


async def refresh_series_cover(
    series_id: int, *, force: bool = False
) -> tuple[bool, str | None]:
    series = _series_snapshot(series_id)
    if not series:
        return False, "series not found"
    cover_url = (series.get("cover_url") or "").strip()
    dest = os.path.join(COVERS_DIR, f"{series_id}.jpg")
    if not cover_url:
        if cached_cover_is_valid(dest):
            mark_source_success(
                series_id,
                SOURCE_COVER,
                degraded=True,
                error="using local cover because no remote URL is available",
                details={"source": "local"},
            )
            return True, None
        mark_source_failure(series_id, SOURCE_COVER, "no cover URL or local cover")
        return False, "cover unavailable"

    cached_url = series.get("cover_cached_url")
    should_force = force or cached_url != cover_url or not os.path.isfile(dest)
    mark_source_attempt(series_id, SOURCE_COVER)
    result = await download_cover(series_id, cover_url, force=should_force)
    if not result.get("ok"):
        error = result.get("error") or result.get("status") or "cover download failed"
        mark_source_failure(series_id, SOURCE_COVER, str(error), details=result)
        return False, str(error)
    now = utc_now_iso()
    with get_db() as db:
        db.execute(
            "UPDATE series SET cover_cached_url=?,cover_updated_at=? WHERE id=?",
            (cover_url, now, series_id),
        )
    mark_source_success(series_id, SOURCE_COVER, details=result)
    return True, None


async def refresh_series_metadata(
    series_id: int,
    *,
    force: bool = False,
    include_manifest: bool = True,
    reason: str = "manual",
) -> dict[str, Any]:
    """Refresh every metadata layer for a series and return a stable summary."""
    lock = await _refresh_lock(series_id)
    async with lock:
        series = _series_snapshot(series_id)
        if not series:
            return {
                "ok": False,
                "series_id": series_id,
                "status": "failed",
                "errors": ["series not found"],
            }

        mark_series_attempt(series_id)
        changed_fields: list[str] = []
        errors: list[str] = []
        warnings: list[str] = []
        sources: dict[str, str] = {}

        mark_source_attempt(series_id, SOURCE_ANILIST)
        core_ok = False
        record: dict | None = None
        try:
            record = await _resolve_anilist_record(series)
            changed_fields.extend(_apply_anilist_record(series_id, record))
            mark_source_success(
                series_id,
                SOURCE_ANILIST,
                details={"anilist_id": record["anilist_id"], "reason": reason},
            )
            sources[SOURCE_ANILIST] = "healthy"
            core_ok = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = f"AniList: {type(exc).__name__}: {str(exc)[:220]}"
            mark_source_failure(series_id, SOURCE_ANILIST, error)
            sources[SOURCE_ANILIST] = "failed"
            errors.append(error)

        if record:
            mark_source_attempt(series_id, SOURCE_ALIASES)
            try:
                alias_result = _store_aliases_and_genres(
                    series_id, series["title"], record
                )
                mark_source_success(
                    series_id, SOURCE_ALIASES, details=alias_result
                )
                sources[SOURCE_ALIASES] = "healthy"
            except Exception as exc:
                error = f"aliases: {type(exc).__name__}: {str(exc)[:180]}"
                mark_source_failure(series_id, SOURCE_ALIASES, error)
                warnings.append(error)
                sources[SOURCE_ALIASES] = "failed"

        current = _series_snapshot(series_id) or series
        volume_source = current.get("vol_count_source") or "anilist"
        if volume_source in _PROTECTED_COUNT_SOURCES:
            mark_source_success(
                series_id,
                SOURCE_MANGAUPDATES,
                details={"skipped": True, "protected_source": volume_source},
            )
            sources[SOURCE_MANGAUPDATES] = "healthy"
        elif force or source_retry_due(series_id, SOURCE_MANGAUPDATES):
            mark_source_attempt(series_id, SOURCE_MANGAUPDATES)
            try:
                mu_result = await fetch_mu_metadata(series_id, current["title"])
                if mu_result:
                    mark_source_success(
                        series_id, SOURCE_MANGAUPDATES, details=mu_result
                    )
                    sources[SOURCE_MANGAUPDATES] = "healthy"
                else:
                    mark_source_success(
                        series_id,
                        SOURCE_MANGAUPDATES,
                        details={"matched": False},
                    )
                    sources[SOURCE_MANGAUPDATES] = "healthy"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = f"MangaUpdates: {type(exc).__name__}: {str(exc)[:180]}"
                mark_source_failure(series_id, SOURCE_MANGAUPDATES, error)
                warnings.append(error)
                sources[SOURCE_MANGAUPDATES] = "failed"

        try:
            map_ok = await refresh_mangadex_map(series_id)
            sources["chapter_map"] = "healthy" if map_ok else "degraded"
            if not map_ok:
                warnings.append("chapter map refresh returned no usable map")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = f"chapter map: {type(exc).__name__}: {str(exc)[:180]}"
            warnings.append(error)
            sources["chapter_map"] = "failed"

        current = _series_snapshot(series_id) or series
        edition = current.get("edition_type") or "standard"
        if edition in _NON_STANDARD_STUB_EDITIONS and edition not in {
            "official_color",
            "colored",
        }:
            try:
                await fetch_edition_volume_count(series_id, current["title"], edition)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                warnings.append(
                    f"edition metadata: {type(exc).__name__}: {str(exc)[:180]}"
                )

        current = _series_snapshot(series_id) or series
        if include_manifest and current.get("mangadex_id"):
            if force or source_retry_due(series_id, SOURCE_MANGADEX_MANIFEST):
                try:
                    from routers.mangadex_ import sync_mangadex_chapters

                    await sync_mangadex_chapters(series_id)
                    sources[SOURCE_MANGADEX_MANIFEST] = "healthy"
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error = f"chapter manifest: {type(exc).__name__}: {str(exc)[:180]}"
                    warnings.append(error)
                    sources[SOURCE_MANGADEX_MANIFEST] = "failed"

        cover_ok, cover_error = await refresh_series_cover(series_id, force=force)
        sources[SOURCE_COVER] = "healthy" if cover_ok else "failed"
        if cover_error:
            warnings.append(f"cover: {cover_error}")

        status = "healthy" if core_ok and not warnings else "degraded" if core_ok else "failed"
        combined_error = "; ".join([*errors, *warnings])[:1000] or None
        finish_series_refresh(
            series_id,
            status=status,
            error=combined_error,
            successful=core_ok,
        )
        log_event(
            "metadata_refresh",
            f"Metadata refresh {status} ({reason}); changed={sorted(set(changed_fields))}; "
            f"warnings={len(warnings)} errors={len(errors)}",
            series_id,
        )
        return {
            "ok": core_ok,
            "series_id": series_id,
            "status": status,
            "changed_fields": sorted(set(changed_fields)),
            "sources": sources,
            "warnings": warnings,
            "errors": errors,
        }


async def refresh_library_metadata(
    *,
    force: bool = False,
    include_manifest: bool = True,
    reason: str = "bulk",
) -> dict[str, Any]:
    with get_db() as db:
        ids = [
            row["id"]
            for row in db.execute(
                "SELECT id FROM series WHERE monitored=1 AND deleted_at IS NULL"
                " ORDER BY title"
            ).fetchall()
        ]
    results: list[dict[str, Any]] = []
    for index, series_id in enumerate(ids):
        results.append(
            await refresh_series_metadata(
                series_id,
                force=force,
                include_manifest=include_manifest,
                reason=reason,
            )
        )
        if index < len(ids) - 1:
            await asyncio.sleep(1)
    counts = Counter(result["status"] for result in results)
    return {
        "total": len(results),
        "healthy": counts["healthy"],
        "degraded": counts["degraded"],
        "failed": counts["failed"],
        "results": results,
    }
