"""External metadata-source adapters.

Sixth module extracted from main.py. Contains the thin HTTP/JSON
adapters for the services Mangarr queries for series metadata and
chapter-to-volume mappings:

  - AniList   (GraphQL search + alias fetch)
  - MangaUpdates (v1 REST search)
  - MangaDex  (ID resolution + aggregate chapter→volume map)
  - Kitsu     (chapters API fallback for DMCA'd MangaDex titles)

Also holds a small number of pure helpers used across those adapters:
status normalisation, MU slug↔ID conversion, and two validation/cleanup
helpers for the chapter→volume map (`_trim_cvm_to_vol_range` and
`_validate_chapter_map`).

Deliberately NOT here:
  - `fetch_wikipedia_volume_count` / `fetch_edition_volume_count`
    (coupled to `create_volume_stubs`, `_EDITION_SEARCH_KEYWORDS`, etc.)
  - `fetch_mu_metadata` (DB write + create_volume_stubs coupling)
  - `refresh_mangadex_map` (DB read + file scan + populate_chapters)
  - MangaDex backoff state (module-level global lives with the
    backfill loop in main.py)
  - `get_series_chapter_map`, `chapters_to_volume_set` (grab-logic
    helpers — move when grab.py is extracted)

Pure move — no behaviour changes.
"""

from __future__ import annotations

import asyncio
import html
import json
import re

import httpx

from parsing import normalize
from shared import get_db
from events import log_event


# ── MangaUpdates slug / status helpers ───────────────────────────────────────


def mu_slug_to_id(slug: str) -> str:
    """Convert MangaUpdates URL slug (base36) to numeric ID string."""
    try:
        return str(int(slug, 36))
    except (ValueError, TypeError):
        return slug


def mu_id_to_slug(numeric_id) -> str:
    """Convert MangaUpdates numeric ID to URL slug (base36)."""
    try:
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"
        n = int(numeric_id)
        result = ""
        while n:
            result = digits[n % 36] + result
            n //= 36
        return result or "0"
    except (ValueError, TypeError):
        return str(numeric_id)


def _norm_status(s: str) -> str:
    """Normalise status strings from various sources to AniList-style enum."""
    if not s:
        return ""
    sl = s.lower()
    if "complete" in sl or "finished" in sl:
        return "FINISHED"
    if "ongoing" in sl or "releasing" in sl or "publishing" in sl:
        return "RELEASING"
    if "hiatus" in sl:
        return "HIATUS"
    if "cancelled" in sl or "canceled" in sl:
        return "CANCELLED"
    return s.upper()


# ── AniList ──────────────────────────────────────────────────────────────────

ANILIST_QUERY = """
query ($search: String) {
  Page(perPage: 12) {
    media(search: $search, type: MANGA, sort: SEARCH_MATCH) {
      id
      idMal
      title { romaji english }
      coverImage { large }
      status
      format
      description(asHtml: false)
      volumes
      chapters
      startDate { year }
    }
  }
}
"""

ANILIST_ALIASES_QUERY = """
query ($id: Int) {
  Media(id: $id, type: MANGA) {
    title { romaji english }
    synonyms
    genres
  }
}
"""

ANILIST_BY_ID_QUERY = """
query ($id: Int) {
  Media(id: $id, type: MANGA) {
    id
    idMal
    title { romaji english }
    synonyms
    genres
    coverImage { large }
    status
    format
    description(asHtml: false)
    volumes
    chapters
    startDate { year }
  }
}
"""


class MetadataProviderError(RuntimeError):
    """A provider could not return a valid metadata response."""


def _clean_description(value: str | None) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()[:4000]


def _anilist_media_to_result(media: dict) -> dict:
    title_block = media.get("title") or {}
    title = title_block.get("english") or title_block.get("romaji", "")
    return {
        "anilist_id": media["id"],
        "mal_id": media.get("idMal"),
        "mu_id": None,
        "title": title,
        "romaji_title": title_block.get("romaji") or "",
        "aliases": media.get("synonyms") or [],
        "genres": media.get("genres") or [],
        "cover_url": (media.get("coverImage") or {}).get("large", ""),
        "status": media.get("status", ""),
        "format": media.get("format", ""),
        "volumes": media.get("volumes"),
        "chapters": media.get("chapters"),
        "pub_year": (media.get("startDate") or {}).get("year"),
        "description": _clean_description(media.get("description")),
        "source": "anilist",
    }


async def fetch_anilist_by_id(anilist_id: int) -> dict:
    """Fetch one exact AniList record; never fuzzy-match a stored identity."""
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    "https://graphql.anilist.co",
                    json={
                        "query": ANILIST_BY_ID_QUERY,
                        "variables": {"id": int(anilist_id)},
                    },
                    headers={"Content-Type": "application/json"},
                )
            if response.status_code == 429:
                wait = int(response.headers.get("Retry-After", "30"))
                if attempt < 2:
                    await asyncio.sleep(min(wait, 120))
                    continue
            response.raise_for_status()
            payload = response.json()
            if payload.get("errors"):
                raise MetadataProviderError(str(payload["errors"][0].get("message", "GraphQL error")))
            media = (payload.get("data") or {}).get("Media")
            if not media:
                raise MetadataProviderError(f"AniList manga {anilist_id} was not found")
            return _anilist_media_to_result(media)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
    raise MetadataProviderError(
        f"AniList {anilist_id} failed: {type(last_error).__name__}: {str(last_error)[:180]}"
    )


async def fetch_anilist_aliases(series_id: int, anilist_id: int, main_title: str) -> bool:
    """Fetch romaji title + synonyms from AniList and store as series aliases."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://graphql.anilist.co",
                json={"query": ANILIST_ALIASES_QUERY, "variables": {"id": anilist_id}},
                headers={"Content-Type": "application/json"},
            )
        data = r.json().get("data", {}).get("Media", {})
    except Exception as e:
        log_event("metadata_fetch_failed", f"[AniList] alias fetch error: {e}", series_id)
        return False

    candidates = []
    title_block = data.get("title", {})
    # Always include the romaji title — critical for Nyaa which uses Japanese romanisations
    if title_block.get("romaji"):
        candidates.append(title_block["romaji"])
    candidates.extend(data.get("synonyms") or [])

    def _is_useful(alias: str) -> bool:
        if not alias or len(alias) < 4:
            return False
        if normalize(alias) == normalize(main_title):
            return False
        # Require at least 40% Latin alphabet characters — filters Arabic, Thai, Cyrillic, CJK, etc.
        latin = len(re.findall(r"[a-zA-Z]", alias))
        if latin < max(1, len(alias.replace(" ", "")) * 0.4):
            return False
        return True

    genres = data.get("genres") or []
    with get_db() as db:
        for alias in candidates:
            if _is_useful(alias):
                db.execute(
                    "INSERT OR IGNORE INTO series_aliases(series_id, alias) VALUES(?,?)",
                    (series_id, alias.strip()),
                )
        for genre in genres[:8]:
            g = genre.strip().lower()
            if g:
                db.execute(
                    "INSERT OR IGNORE INTO series_tags(series_id, tag) VALUES(?,?)",
                    (series_id, g),
                )
    log_event(
        "metadata",
        f"[AniList] aliases populated for series {series_id}: {[a for a in candidates if _is_useful(a)]}",
        series_id,
    )
    if genres:
        log_event("metadata", f"[AniList] genres tagged for series {series_id}: {genres[:8]}", series_id)
    return True


async def anilist_search(query: str, *, strict: bool = False) -> list[dict]:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(
                    "https://graphql.anilist.co",
                    json={"query": ANILIST_QUERY, "variables": {"search": query}},
                    headers={"Content-Type": "application/json"},
                )
            if r.status_code == 429:
                retry_after = int(r.headers.get("Retry-After", "60"))
                log_event("metadata", f"[AniList] Rate limited — waiting {retry_after}s (attempt {attempt + 1}/3)")
                if attempt < 2:
                    await asyncio.sleep(min(retry_after, 120))
                    continue
            r.raise_for_status()
            data = r.json()
            if data.get("errors"):
                raise MetadataProviderError(
                    str(data["errors"][0].get("message", "GraphQL error"))
                )
            break
        except Exception as e:
            last_error = e
            log_event("metadata_fetch_failed", f"[AniList] error (attempt {attempt + 1}/3): {e}")
            if attempt < 2:
                await asyncio.sleep(5 * (attempt + 1))
            else:
                if strict:
                    raise MetadataProviderError(
                        f"AniList search failed: {type(e).__name__}: {str(e)[:180]}"
                    ) from e
                return []
    else:
        if strict:
            raise MetadataProviderError(
                f"AniList search failed: {type(last_error).__name__}:"
                f" {str(last_error)[:180]}"
            )
        return []
    results = []
    for m in data.get("data", {}).get("Page", {}).get("media", []):
        results.append(_anilist_media_to_result(m))
    return results


# ── MangaUpdates ─────────────────────────────────────────────────────────────


async def mu_search(query: str, *, strict: bool = False) -> list[dict]:
    """Search MangaUpdates — used as fallback when AniList has no results."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://api.mangaupdates.com/v1/series/search",
                json={"search": query, "per_page": 12},
                headers={"Content-Type": "application/json"},
            )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log_event("metadata_fetch_failed", f"[MangaUpdates] search error: {e}")
        if strict:
            raise MetadataProviderError(
                f"MangaUpdates search failed: {type(e).__name__}: {str(e)[:180]}"
            ) from e
        return []
    results = []
    for item in data.get("results", []):
        rec = item.get("record", {})
        mu_num = str(rec.get("series_id", ""))
        # Parse total volumes from status string e.g. "34 Volumes (Complete)"
        status_str = rec.get("status") or ""
        vol_match = re.search(r"(\d+)\s+[Vv]olume", status_str)
        volumes = int(vol_match.group(1)) if vol_match else None
        latest_ch = rec.get("latest_chapter")
        # Cover image
        img = rec.get("image") or {}
        cover = (img.get("url") or {}).get("original") or ""
        desc = _clean_description(rec.get("description"))
        results.append(
            {
                "anilist_id": None,
                "mal_id": None,
                "mu_id": mu_num,
                "title": rec.get("title", ""),
                "cover_url": cover,
                "status": _norm_status(status_str),
                "volumes": volumes,
                "chapters": int(latest_ch) if latest_ch else None,
                "description": desc,
                "source": "mangaupdates",
            }
        )
    return results


async def search_series(query: str) -> tuple[list[dict], str]:
    """Search across sources. Returns (results, source_used).

    Handles AniList URLs/IDs directly, then AniList text search, then
    MangaUpdates fallback.
    """
    q = query.strip()

    # AniList URL: https://anilist.co/manga/123/... → extract numeric ID
    _al_url = re.search(r"anilist\.co/(?:manga|anime)/(\d+)", q)
    if _al_url:
        q = _al_url.group(1)

    # Bare numeric ID → look up AniList by ID directly
    if q.isdigit():
        try:
            return [await fetch_anilist_by_id(int(q))], "anilist"
        except Exception:
            pass  # fall through to text search

    results = await anilist_search(q)
    if results:
        return results, "anilist"
    results = await mu_search(q)
    return results, "mangaupdates"


# ── MangaDex chapter→volume mapping ──────────────────────────────────────────


async def fetch_mangadex_id(
    title: str, anilist_id: int | None, mu_id: str | None = None
) -> tuple[str | None, dict]:
    """Find MangaDex manga UUID by matching AniList or MangaUpdates ID in external links.
    Returns (mangadex_uuid, links_dict) where links_dict has al/mal/mu/kt keys."""
    def _score(candidate: str, query: str) -> float:
        left = set(normalize(candidate).split())
        right = set(normalize(query).split())
        if not left or not right:
            return 0.0
        overlap = left & right
        if not overlap:
            return 0.0
        recall = len(overlap) / len(right)
        precision = len(overlap) / len(left)
        return 2 * recall * precision / (recall + precision)

    queries = [title]
    for shortened in (title.split(":", 1)[0], title.split("(", 1)[0]):
        shortened = shortened.strip()
        if shortened and shortened not in queries:
            queries.append(shortened)

    try:
        best: tuple[float, str, dict] | None = None
        async with httpx.AsyncClient(timeout=15) as client:
            for query in queries:
                r = await client.get(
                    "https://api.mangadex.org/manga",
                    params={
                        "title": query,
                        "limit": 15,
                        "order[relevance]": "desc",
                        "contentRating[]": ["safe", "suggestive", "erotica"],
                    },
                )
                r.raise_for_status()
                for manga in r.json().get("data", []):
                    attrs = manga.get("attributes") or {}
                    links = attrs.get("links") or {}
                    if anilist_id and str(links.get("al", "")) == str(anilist_id):
                        return manga["id"], links
                    if mu_id and links.get("mu", "") == mu_id_to_slug(mu_id):
                        return manga["id"], links

                    titles = list((attrs.get("title") or {}).values())
                    for alt in attrs.get("altTitles") or []:
                        titles.extend(alt.values())
                    # Shortened queries are discovery aids only. Confidence is
                    # always measured against the stored title so a subtitle or
                    # edition search cannot silently bind the base work.
                    confidence = max((_score(str(value), title) for value in titles), default=0.0)
                    if best is None or confidence > best[0]:
                        best = (confidence, manga["id"], links)
        # Never silently bind an unrelated first search result. A strong title
        # match is an acceptable fallback when external IDs are absent.
        if best and best[0] >= 0.85:
            return best[1], best[2]
    except Exception as e:
        log_event(
            "metadata_fetch_failed",
            f"mangadex id lookup failed: {type(e).__name__}: {str(e)[:120]}",
        )
    return None, {}


async def fetch_chapter_volume_map(mangadex_id: str) -> dict:
    """Fetch chapter→volume mapping from MangaDex aggregate endpoint.

    Returns {chapter_str: vol_int, ...} e.g. {"1": 1, "2": 1, "5": 2, ...}.
    No language filter — we only need the volume assignment metadata, not the text."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"https://api.mangadex.org/manga/{mangadex_id}/aggregate"
            )
        data = r.json()
        mapping: dict[str, int] = {}
        volumes = data.get("volumes", {})
        # Guard against malformed response (list instead of dict)
        if not isinstance(volumes, dict):
            return mapping
        for vol_key, vol_data in volumes.items():
            try:
                vol_num = int(float(vol_key))
            except (ValueError, TypeError):
                continue  # skip "none" / uncollected chapters
            chapters = vol_data.get("chapters") if isinstance(vol_data, dict) else {}
            if isinstance(chapters, dict):
                for ch_key in chapters.keys():
                    mapping[ch_key] = vol_num
        return mapping
    except Exception as e:
        log_event(
            "metadata_fetch_failed",
            f"mangadex aggregate failed for {mangadex_id}: "
            f"{type(e).__name__}: {str(e)[:120]}",
        )
    return {}


async def fetch_kitsu_chapter_map(
    title: str, anilist_id: int | None, total_chapters: int | None
) -> dict:
    """Fetch chapter→volume mapping from Kitsu's chapters API.
    Returns {chapter_str: vol_int, ...} or {} on failure.
    Kitsu is a reliable fallback for DMCA'd MangaDex titles."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Find Kitsu manga ID by title search
            r = await client.get(
                "https://kitsu.io/api/edge/manga",
                params={"filter[text]": title, "page[limit]": 10},
                headers={"Accept": "application/vnd.api+json"},
            )
        data = r.json()
        reference_titles = [title]
        if re.search(
            r"\((?:official\s+colou?r|colou?red|omnibus|deluxe|collector|remaster)\)",
            title,
            re.IGNORECASE,
        ):
            reference_titles.append(title.split("(", 1)[0].strip())

        def _title_score(candidate: str) -> float:
            candidate_words = set(normalize(candidate).split())
            if not candidate_words:
                return 0.0
            best = 0.0
            for reference in reference_titles:
                reference_words = set(normalize(reference).split())
                overlap = candidate_words & reference_words
                if not overlap:
                    continue
                precision = len(overlap) / len(candidate_words)
                recall = len(overlap) / len(reference_words)
                best = max(best, 2 * precision * recall / (precision + recall))
            return best

        candidates: list[tuple[float, int, str]] = []
        for item in data.get("data", []):
            attrs = item.get("attributes", {})
            titles = [
                attrs.get("canonicalTitle") or "",
                *((attrs.get("titles") or {}).values()),
                *(attrs.get("abbreviatedTitles") or []),
            ]
            score = max((_title_score(str(value)) for value in titles), default=0.0)
            if score < 0.85:
                continue
            chapter_count = int(attrs.get("chapterCount") or 0)
            distance = (
                abs(chapter_count - total_chapters)
                if total_chapters and chapter_count
                else 999999
            )
            candidates.append((score, -distance, item["id"]))

        kitsu_id = max(candidates)[2] if candidates else None

        if not kitsu_id:
            return {}

        # Paginate through all chapters
        mapping: dict[str, int] = {}
        offset = 0
        limit = 20
        async with httpx.AsyncClient(timeout=15) as client:
            while True:
                try:
                    r = await client.get(
                        "https://kitsu.io/api/edge/chapters",
                        params={
                            "filter[manga_id]": kitsu_id,
                            "page[limit]": limit,
                            "page[offset]": offset,
                            "fields[chapters]": "number,volumeNumber",
                        },
                        headers={"Accept": "application/vnd.api+json"},
                    )
                except asyncio.CancelledError:
                    log_event("metadata", f"[Kitsu] chapter map fetch cancelled for kitsu_id={kitsu_id}")
                    raise
                page = r.json()
                rows = page.get("data", [])
                if not rows:
                    break
                for ch in rows:
                    attrs = ch.get("attributes", {})
                    ch_num = attrs.get("number")
                    vol_num = attrs.get("volumeNumber")
                    if ch_num is not None and vol_num is not None:
                        try:
                            # Preserve half-chapters: int(float("0.5")) = 0
                            # collides with chapter 0. Stringify the float
                            # form so "0", "0.5", "1", "1.5" are all distinct
                            # keys. The mapping consumer (`populate_chapters`,
                            # `_check_volume_completion`) looks up via
                            # str(chapter_num), which produces matching
                            # keys for whole and fractional chapters.
                            # Volume number stays int — ComicInfo / Komga
                            # only support integer volumes anyway.
                            ch_f = float(ch_num)
                            ch_key = str(int(ch_f)) if ch_f == int(ch_f) else str(ch_f)
                            mapping[ch_key] = int(float(vol_num))
                        except (ValueError, TypeError):
                            pass
                # Check if there are more pages
                next_link = (page.get("links") or {}).get("next")
                if not next_link:
                    break
                offset += limit
                if offset > 2000:  # safety cap
                    break

        return mapping
    except Exception as e:
        log_event(
            "metadata_fetch_failed",
            f"kitsu chapter-map failed: {type(e).__name__}: {str(e)[:120]}",
        )
    return {}


# ── Chapter-volume map validation / cleanup ──────────────────────────────────


def _trim_cvm_to_vol_range(
    mapping: dict, total_volumes: int | None, source: str
) -> dict:
    """Drop entries whose target volume exceeds ``total_volumes``.

    Upstream sources (notably MangaDex) sometimes catalogue multi-part
    series under a single UUID with continuous chapter numbering, so a
    per-part series record ends up with cvm entries pointing at volumes
    that belong to a later part. Those entries then drive
    populate_chapters to create phantom `wanted` chapter rows for
    chapter numbers that don't belong to this series at all.

    We can't always tell upstream data is wrong, but we can enforce
    the local invariant: no cvm entry should target a volume the
    series itself doesn't have. When total_volumes is None/0 we can't
    judge, so return the map untouched."""
    if not mapping or not total_volumes or total_volumes <= 0:
        return mapping
    kept: dict = {}
    dropped = 0
    for k, v in mapping.items():
        try:
            vol = float(v)
        except (TypeError, ValueError):
            # Non-numeric target — let _validate_chapter_map decide.
            kept[k] = v
            continue
        if vol > float(total_volumes):
            dropped += 1
            continue
        kept[k] = v
    if dropped:
        log_event(
            "metadata",
            f"[{source}] dropped {dropped} cvm entries targeting "
            f"vol > {total_volumes} (likely continuous-numbering "
            f"contamination across series parts)",
        )
    return kept


def _validate_chapter_map(
    mapping: dict,
    total_chapters: int | None,
    source: str,
    total_volumes: int | None = None,
) -> bool:
    """Return False if the map looks too sparse to be useful."""
    if not mapping:
        return False
    if total_chapters and total_chapters > 10:
        coverage = len(mapping) / total_chapters
        if coverage < 0.5:
            log_event("metadata", f"[{source}] map covers only {len(mapping)}/{total_chapters} chapters ({coverage:.0%}) — discarding")
            return False
    if len(set(mapping.values())) < 2 and total_volumes != 1:
        log_event("metadata", f"[{source}] all chapters map to same volume — discarding")
        return False
    return True


# ── Wikipedia number-word table (used by main.fetch_wikipedia_volume_count) ──

# Word-to-integer mapping used when parsing Wikipedia natural-language counts.
_WIKI_WORD_NUMS: dict[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "twenty-one": 21,
    "twenty-two": 22,
    "twenty-three": 23,
    "twenty-four": 24,
    "twenty-five": 25,
    "twenty-six": 26,
    "twenty-seven": 27,
    "twenty-eight": 28,
    "twenty-nine": 29,
    "thirty": 30,
    "thirty-one": 31,
    "thirty-two": 32,
    "thirty-three": 33,
    "thirty-four": 34,
    "thirty-five": 35,
    "forty": 40,
    "forty-five": 45,
    "fifty": 50,
}
