"""MangaDex chapter manifest — fetch and store available chapters per series.
No downloading happens here. This is metadata only: chapter UUIDs, volume/chapter
numbers, scanlation groups, and page counts stored in mangadex_chapters table.
"""
import asyncio
import logging
import time

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from shared import get_cfg, get_db, timed_block

log = logging.getLogger(__name__)

MANGADEX_API = "https://api.mangadex.org"
_PAGE_LIMIT   = 500          # MangaDex max per request
_RATE_SLEEP   = 1.0          # seconds between paginated requests

# Groups known to upload official/publisher content on MangaDex.
# Used when source_type='official_only' to filter results.
KNOWN_OFFICIAL_GROUPS = {
    "official", "viz media", "viz", "kodansha", "square enix", "shueisha",
    "yen press", "seven seas", "dark horse", "tokyopop", "mangaplus",
    "manga plus", "shonen jump", "jump+",
}

router = APIRouter()

# Per-series locks prevent concurrent MangaDex syncs for the same series
# (e.g. startup backfill + user-triggered API call both hitting the same id).
# Two concurrent syncs can create duplicate upserts and race on chapter→vol maps.
_sync_locks: dict[int, asyncio.Lock] = {}
_sync_locks_guard = asyncio.Lock()


async def _get_sync_lock(series_id: int) -> asyncio.Lock:
    async with _sync_locks_guard:
        lock = _sync_locks.get(series_id)
        if lock is None:
            lock = asyncio.Lock()
            _sync_locks[series_id] = lock
        return lock


# ── Core sync ─────────────────────────────────────────────────────────────────

async def sync_mangadex_chapters(series_id: int) -> dict:
    """
    Fetch the full chapter feed from MangaDex for a series, paginated 500/page.
    Upserts rows into mangadex_chapters table.
    Returns {'added': N, 'updated': N, 'total': N, 'external_skipped': N}
    Raises ValueError if series has no mangadex_id.
    Serialized per-series to prevent concurrent double-sync races.
    """
    lock = await _get_sync_lock(series_id)
    async with lock:
        return await _sync_mangadex_chapters_impl(series_id)


async def _sync_mangadex_chapters_impl(series_id: int) -> dict:
    with get_db() as db:
        row = db.execute("SELECT mangadex_id, ddl_language FROM series WHERE id=?",
                         (series_id,)).fetchone()
    if not row or not row['mangadex_id']:
        raise ValueError(f"Series {series_id} has no mangadex_id")

    language = row['ddl_language'] or get_cfg('ddl_language', 'en')
    mangadex_id = row['mangadex_id']

    all_chapters: list[dict] = []
    offset = 0
    total_remote = None

    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "Mangarr/1.0"}) as client:
        while True:
            params = {
                "limit": _PAGE_LIMIT,
                "offset": offset,
                f"translatedLanguage[]": language,
                "order[chapter]": "asc",
                "includes[]": "scanlation_group",
                "contentRating[]": ["safe", "suggestive", "erotica", "pornographic"],
            }
            for attempt in range(3):
                try:
                    r = await client.get(
                        f"{MANGADEX_API}/manga/{mangadex_id}/feed",
                        params=params,
                    )
                    if r.status_code == 429:
                        wait = int(r.headers.get("X-RateLimit-Retry-After", 5))
                        await asyncio.sleep(wait)
                        continue
                    r.raise_for_status()
                    break
                except httpx.HTTPStatusError:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(2)

            data = r.json()
            if total_remote is None:
                total_remote = data.get("total", 0)

            items = data.get("data", [])
            if not items:
                break

            all_chapters.extend(items)
            offset += len(items)

            if offset >= total_remote:
                break

            await asyncio.sleep(_RATE_SLEEP)

    added = updated = external_skipped = 0
    # Instrumentation for issue #31 follow-up A: this block holds a single
    # write transaction across N chapter upserts. For large series (200+
    # chapters) it can dominate the event loop and stall other DB work.
    with timed_block("sync_mangadex_chapters.db_upsert",
                     series_id=series_id, rows=len(all_chapters)), \
         get_db() as db:
        for item in all_chapters:
            parsed = _parse_chapter(item, series_id)
            if parsed['is_external']:
                external_skipped += 1
                # Still store it so we can show availability (just can't grab it)
            existing = db.execute(
                "SELECT id FROM mangadex_chapters WHERE mangadex_chapter_id=?",
                (parsed['mangadex_chapter_id'],)
            ).fetchone()
            if existing:
                db.execute(
                    "UPDATE mangadex_chapters SET chapter_num=?,volume_num=?,title=?,"
                    " pages=?,scanlation_group=?,language=?,is_external=?,publish_at=?,"
                    " synced_at=datetime('now') WHERE mangadex_chapter_id=?",
                    (parsed['chapter_num'], parsed['volume_num'], parsed['title'],
                     parsed['pages'], parsed['scanlation_group'], parsed['language'],
                     parsed['is_external'], parsed['publish_at'],
                     parsed['mangadex_chapter_id'])
                )
                updated += 1
            else:
                db.execute(
                    "INSERT INTO mangadex_chapters"
                    " (series_id,mangadex_chapter_id,chapter_num,volume_num,title,"
                    "  pages,scanlation_group,language,is_external,publish_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (series_id, parsed['mangadex_chapter_id'], parsed['chapter_num'],
                     parsed['volume_num'], parsed['title'], parsed['pages'],
                     parsed['scanlation_group'], parsed['language'],
                     parsed['is_external'], parsed['publish_at'])
                )
                added += 1

    log.info("MangaDex sync series=%d lang=%s: +%d updated=%d external=%d",
             series_id, language, added, updated, external_skipped)
    return {
        "added": added,
        "updated": updated,
        "total": added + updated,
        "external_skipped": external_skipped,
    }


def _parse_chapter(item: dict, series_id: int) -> dict:
    """Extract fields from a MangaDex chapter object."""
    attrs = item.get("attributes", {})

    # Chapter number (can be null for one-shots)
    ch_raw = attrs.get("chapter")
    try:
        chapter_num = float(ch_raw) if ch_raw not in (None, "") else None
    except (ValueError, TypeError):
        chapter_num = None

    # Volume number
    vol_raw = attrs.get("volume")
    try:
        volume_num = float(vol_raw) if vol_raw not in (None, "") else None
    except (ValueError, TypeError):
        volume_num = None

    # Scanlation group name from relationships
    group_name = None
    for rel in item.get("relationships", []):
        if rel.get("type") == "scanlation_group":
            group_name = (rel.get("attributes") or {}).get("name")
            if group_name:
                break

    return {
        "mangadex_chapter_id": item["id"],
        "series_id":           series_id,
        "chapter_num":         chapter_num,
        "volume_num":          volume_num,
        "title":               attrs.get("title") or None,
        "pages":               attrs.get("pages", 0) or 0,
        "scanlation_group":    group_name,
        "language":            attrs.get("translatedLanguage", "en"),
        "is_external":         1 if attrs.get("externalUrl") else 0,
        "publish_at":          attrs.get("publishAt"),
    }


# ── Query helpers used by other modules ──────────────────────────────────────

def get_chapter_availability(series_id: int, language: str = None) -> dict:
    """
    Returns {volume_num: {'chapter_count': N, 'has_external': bool, 'groups': [...]}}
    volume_num None = chapters with no volume assignment.
    """
    lang = language or get_cfg('ddl_language', 'en')
    with get_db() as db:
        rows = db.execute(
            "SELECT volume_num, scanlation_group, is_external, COUNT(*) as cnt"
            " FROM mangadex_chapters"
            " WHERE series_id=? AND language=?"
            " GROUP BY volume_num, scanlation_group, is_external",
            (series_id, lang)
        ).fetchall()

    result: dict = {}
    for row in rows:
        key = row['volume_num']
        if key not in result:
            result[key] = {'chapter_count': 0, 'has_external': False, 'groups': []}
        result[key]['chapter_count'] += row['cnt']
        if row['is_external']:
            result[key]['has_external'] = True
        if row['scanlation_group'] and row['scanlation_group'] not in result[key]['groups']:
            result[key]['groups'].append(row['scanlation_group'])
    return result


def select_best_chapters_for_volume(
    series_id: int,
    volume_num: float,
    preferred_groups: list[str],
    source_type: str,
    language: str,
) -> list[dict]:
    """
    Returns one row per chapter_num for the given volume — the best available translation.

    Selection priority:
      1. preferred_groups match (in order of preference list)
      2. source_type filter (official_only / fan_only)
      3. highest page count
      4. most recent publish_at

    Excludes external chapters (is_external=1).
    Returns [] if no chapters found for this volume.
    """
    with get_db() as db:
        if volume_num is None:
            rows = db.execute(
                "SELECT * FROM mangadex_chapters"
                " WHERE series_id=? AND volume_num IS NULL AND language=? AND is_external=0"
                " ORDER BY chapter_num ASC, pages DESC",
                (series_id, language)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM mangadex_chapters"
                " WHERE series_id=? AND volume_num=? AND language=? AND is_external=0"
                " ORDER BY chapter_num ASC, pages DESC",
                (series_id, volume_num, language)
            ).fetchall()

    rows = [dict(r) for r in rows]

    # Apply source_type filter
    if source_type == 'official_only':
        official = [r for r in rows if (r.get('scanlation_group') or '').lower()
                    in KNOWN_OFFICIAL_GROUPS]
        if official:
            rows = official
    elif source_type == 'fan_only':
        rows = [r for r in rows if (r.get('scanlation_group') or '').lower()
                not in KNOWN_OFFICIAL_GROUPS]

    # Group by chapter_num, pick best per chapter
    chapter_map: dict[float | None, list[dict]] = {}
    for row in rows:
        key = row['chapter_num']
        chapter_map.setdefault(key, []).append(row)

    preferred_lower = [g.lower() for g in preferred_groups]

    def _rank(ch: dict) -> tuple:
        group = (ch.get('scanlation_group') or '').lower()
        pref_score = 0
        for i, pg in enumerate(preferred_lower):
            if pg in group or group in pg:
                pref_score = len(preferred_lower) - i
                break
        return (pref_score, ch.get('pages', 0), ch.get('publish_at') or '')

    best: list[dict] = []
    for ch_num in sorted(chapter_map.keys(), key=lambda x: (x is None, x or 0)):
        candidates = chapter_map[ch_num]
        best.append(max(candidates, key=_rank))

    return best


# ── Router endpoints ──────────────────────────────────────────────────────────

@router.post("/api/series/{series_id}/mangadex/sync")
async def api_sync_chapters(series_id: int):
    """Trigger a MangaDex chapter manifest sync for one series."""
    try:
        result = await sync_mangadex_chapters(series_id)
        return JSONResponse({"ok": True, **result})
    except ValueError as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=400)
    except Exception as e:
        log.error("MangaDex sync error series=%d: %s", series_id, e)
        return JSONResponse({"ok": False, "message": str(e)}, status_code=500)


@router.get("/api/series/{series_id}/mangadex/availability")
async def api_availability(series_id: int):
    """Return per-volume chapter availability for a series."""
    language = None  # use global default
    avail = get_chapter_availability(series_id, language)
    # Convert float keys to strings for JSON
    return JSONResponse({str(k): v for k, v in avail.items()})


@router.get("/api/series/{series_id}/mangadex/chapters")
async def api_chapters(series_id: int, volume_num: float = None, language: str = None):
    """List raw mangadex_chapters rows for a series (optionally filtered)."""
    lang = language or get_cfg('ddl_language', 'en')
    with get_db() as db:
        if volume_num is not None:
            rows = db.execute(
                "SELECT * FROM mangadex_chapters WHERE series_id=? AND volume_num=? AND language=?"
                " ORDER BY chapter_num ASC",
                (series_id, volume_num, lang)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM mangadex_chapters WHERE series_id=? AND language=?"
                " ORDER BY volume_num ASC, chapter_num ASC",
                (series_id, lang)
            ).fetchall()
    return JSONResponse([dict(r) for r in rows])
