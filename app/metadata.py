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
        digits = '0123456789abcdefghijklmnopqrstuvwxyz'
        n = int(numeric_id)
        result = ''
        while n:
            result = digits[n % 36] + result
            n //= 36
        return result or '0'
    except (ValueError, TypeError):
        return str(numeric_id)


def _norm_status(s: str) -> str:
    """Normalise status strings from various sources to AniList-style enum."""
    if not s:
        return ''
    sl = s.lower()
    if 'complete' in sl or 'finished' in sl:
        return 'FINISHED'
    if 'ongoing' in sl or 'releasing' in sl or 'publishing' in sl:
        return 'RELEASING'
    if 'hiatus' in sl:
        return 'HIATUS'
    if 'cancelled' in sl or 'canceled' in sl:
        return 'CANCELLED'
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


async def fetch_anilist_aliases(series_id: int, anilist_id: int, main_title: str):
    """Fetch romaji title + synonyms from AniList and store as series aliases."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                'https://graphql.anilist.co',
                json={'query': ANILIST_ALIASES_QUERY, 'variables': {'id': anilist_id}},
                headers={'Content-Type': 'application/json'}
            )
        data = r.json().get('data', {}).get('Media', {})
    except Exception as e:
        print(f"[AniList] alias fetch error: {e}")
        return

    candidates = []
    title_block = data.get('title', {})
    # Always include the romaji title — critical for Nyaa which uses Japanese romanisations
    if title_block.get('romaji'):
        candidates.append(title_block['romaji'])
    candidates.extend(data.get('synonyms') or [])

    def _is_useful(alias: str) -> bool:
        if not alias or len(alias) < 4:
            return False
        if normalize(alias) == normalize(main_title):
            return False
        # Require at least 40% Latin alphabet characters — filters Arabic, Thai, Cyrillic, CJK, etc.
        latin = len(re.findall(r'[a-zA-Z]', alias))
        if latin < max(1, len(alias.replace(' ', '')) * 0.4):
            return False
        return True

    genres = data.get('genres') or []
    with get_db() as db:
        for alias in candidates:
            if _is_useful(alias):
                db.execute(
                    "INSERT OR IGNORE INTO series_aliases(series_id, alias) VALUES(?,?)",
                    (series_id, alias.strip())
                )
        for genre in genres[:8]:
            g = genre.strip().lower()
            if g:
                db.execute(
                    "INSERT OR IGNORE INTO series_tags(series_id, tag) VALUES(?,?)",
                    (series_id, g)
                )
    print(f"[AniList] aliases populated for series {series_id}: {[a for a in candidates if _is_useful(a)]}")
    if genres:
        print(f"[AniList] genres tagged for series {series_id}: {genres[:8]}")


async def anilist_search(query: str) -> list[dict]:
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(
                    'https://graphql.anilist.co',
                    json={'query': ANILIST_QUERY, 'variables': {'search': query}},
                    headers={'Content-Type': 'application/json'}
                )
            if r.status_code == 429:
                retry_after = int(r.headers.get('Retry-After', '60'))
                print(f"[AniList] Rate limited — waiting {retry_after}s (attempt {attempt+1}/3)")
                await asyncio.sleep(min(retry_after, 120))
                continue
            data = r.json()
            break
        except Exception as e:
            print(f"[AniList] error (attempt {attempt+1}/3): {e}")
            if attempt < 2:
                await asyncio.sleep(5 * (attempt + 1))
            else:
                return []
    else:
        return []
    results = []
    for m in data.get('data', {}).get('Page', {}).get('media', []):
        title = m['title'].get('english') or m['title'].get('romaji', '')
        desc  = re.sub(r'<[^>]+>', '', (m.get('description') or ''))[:300].strip()
        results.append({
            'anilist_id':  m['id'],
            'mal_id':      m.get('idMal'),
            'mu_id':       None,
            'title':       title,
            'cover_url':   m['coverImage']['large'],
            'status':      m.get('status', ''),
            'format':      m.get('format', ''),
            'volumes':     m.get('volumes'),
            'chapters':    m.get('chapters'),
            'pub_year':    (m.get('startDate') or {}).get('year'),
            'description': desc,
            'source':      'anilist',
        })
    return results


# ── MangaUpdates ─────────────────────────────────────────────────────────────

async def mu_search(query: str) -> list[dict]:
    """Search MangaUpdates — used as fallback when AniList has no results."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                'https://api.mangaupdates.com/v1/series/search',
                json={'search': query, 'per_page': 12},
                headers={'Content-Type': 'application/json'},
            )
        data = r.json()
    except Exception as e:
        print(f"[MangaUpdates] search error: {e}")
        return []
    results = []
    for item in data.get('results', []):
        rec = item.get('record', {})
        mu_num = str(rec.get('series_id', ''))
        # Parse total volumes from status string e.g. "34 Volumes (Complete)"
        status_str = rec.get('status') or ''
        vol_match  = re.search(r'(\d+)\s+[Vv]olume', status_str)
        volumes    = int(vol_match.group(1)) if vol_match else None
        latest_ch  = rec.get('latest_chapter')
        # Cover image
        img        = rec.get('image') or {}
        cover      = (img.get('url') or {}).get('original') or ''
        desc       = re.sub(r'<[^>]+>', '', (rec.get('description') or ''))[:300].strip()
        results.append({
            'anilist_id':  None,
            'mal_id':      None,
            'mu_id':       mu_num,
            'title':       rec.get('title', ''),
            'cover_url':   cover,
            'status':      _norm_status(status_str),
            'volumes':     volumes,
            'chapters':    int(latest_ch) if latest_ch else None,
            'description': desc,
            'source':      'mangaupdates',
        })
    return results


async def search_series(query: str) -> tuple[list[dict], str]:
    """Search across sources. Returns (results, source_used).

    Handles AniList URLs/IDs directly, then AniList text search, then
    MangaUpdates fallback.
    """
    q = query.strip()

    # AniList URL: https://anilist.co/manga/123/... → extract numeric ID
    _al_url = re.search(r'anilist\.co/(?:manga|anime)/(\d+)', q)
    if _al_url:
        q = _al_url.group(1)

    # Bare numeric ID → look up AniList by ID directly
    if q.isdigit():
        _id_gql = 'query($id:Int){Media(id:$id,type:MANGA){id idMal title{english romaji} coverImage{large} status volumes chapters startDate{year} description genres}}'
        try:
            async with httpx.AsyncClient(timeout=15) as _id_cli:
                _r = await _id_cli.post(
                    'https://graphql.anilist.co',
                    json={'query': _id_gql, 'variables': {'id': int(q)}},
                    headers={'Content-Type': 'application/json'},
                )
            _m = (_r.json().get('data') or {}).get('Media')
            if _m:
                _title = (_m.get('title') or {}).get('english') or (_m.get('title') or {}).get('romaji', '')
                _desc  = re.sub(r'<[^>]+>', '', (_m.get('description') or ''))[:300].strip()
                return [{
                    'anilist_id':  _m['id'],
                    'mal_id':      _m.get('idMal'),
                    'mu_id':       None,
                    'title':       _title,
                    'cover_url':   (_m.get('coverImage') or {}).get('large', ''),
                    'status':      _m.get('status', ''),
                    'volumes':     _m.get('volumes'),
                    'chapters':    _m.get('chapters'),
                    'pub_year':    ((_m.get('startDate') or {}).get('year')),
                    'description': _desc,
                    'source':      'anilist',
                }], 'anilist'
        except Exception:
            pass  # fall through to text search

    results = await anilist_search(q)
    if results:
        return results, 'anilist'
    results = await mu_search(q)
    return results, 'mangaupdates'


# ── MangaDex chapter→volume mapping ──────────────────────────────────────────

async def fetch_mangadex_id(title: str, anilist_id: int | None,
                            mu_id: str | None = None) -> tuple[str | None, dict]:
    """Find MangaDex manga UUID by matching AniList or MangaUpdates ID in external links.
    Returns (mangadex_uuid, links_dict) where links_dict has al/mal/mu/kt keys."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                'https://api.mangadex.org/manga',
                params={
                    'title': title,
                    'limit': 15,
                    'order[relevance]': 'desc',
                    'contentRating[]': ['safe', 'suggestive', 'erotica'],
                }
            )
        data = r.json()
        best_id, best_links = None, {}
        for manga in data.get('data', []):
            links = manga.get('attributes', {}).get('links', {}) or {}
            # Match by AniList ID (most reliable)
            if anilist_id and str(links.get('al', '')) == str(anilist_id):
                return manga['id'], links
            # Match by MangaUpdates slug (convert our numeric id to slug for comparison)
            if mu_id:
                mu_slug = mu_id_to_slug(mu_id)
                if links.get('mu', '') == mu_slug:
                    return manga['id'], links
            if best_id is None:
                best_id, best_links = manga['id'], links
        if best_id:
            return best_id, best_links
    except Exception as e:
        print(f"[MangaDex] ID lookup error: {e}")
        log_event('metadata_fetch_failed',
                   f'mangadex id lookup failed: {type(e).__name__}: {str(e)[:120]}')
    return None, {}


async def fetch_chapter_volume_map(mangadex_id: str) -> dict:
    """Fetch chapter→volume mapping from MangaDex aggregate endpoint.

    Returns {chapter_str: vol_int, ...} e.g. {"1": 1, "2": 1, "5": 2, ...}.
    No language filter — we only need the volume assignment metadata, not the text."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f'https://api.mangadex.org/manga/{mangadex_id}/aggregate'
            )
        data = r.json()
        mapping: dict[str, int] = {}
        volumes = data.get('volumes', {})
        # Guard against malformed response (list instead of dict)
        if not isinstance(volumes, dict):
            return mapping
        for vol_key, vol_data in volumes.items():
            try:
                vol_num = int(float(vol_key))
            except (ValueError, TypeError):
                continue  # skip "none" / uncollected chapters
            chapters = vol_data.get('chapters') if isinstance(vol_data, dict) else {}
            if isinstance(chapters, dict):
                for ch_key in chapters.keys():
                    mapping[ch_key] = vol_num
        return mapping
    except Exception as e:
        print(f"[MangaDex] aggregate error: {e}")
        log_event('metadata_fetch_failed',
                   f'mangadex aggregate failed for {mangadex_id}: '
                   f'{type(e).__name__}: {str(e)[:120]}')
    return {}


async def fetch_kitsu_chapter_map(title: str, anilist_id: int | None,
                                  total_chapters: int | None) -> dict:
    """Fetch chapter→volume mapping from Kitsu's chapters API.
    Returns {chapter_str: vol_int, ...} or {} on failure.
    Kitsu is a reliable fallback for DMCA'd MangaDex titles."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Find Kitsu manga ID by title search
            r = await client.get(
                'https://kitsu.io/api/edge/manga',
                params={'filter[text]': title, 'page[limit]': 10},
                headers={'Accept': 'application/vnd.api+json'},
            )
        data = r.json()
        kitsu_id = None
        for item in data.get('data', []):
            attrs = item.get('attributes', {})
            # Match on chapterCount to narrow down (AniList doesn't expose ID via Kitsu directly)
            ch_count = attrs.get('chapterCount') or 0
            vol_count = attrs.get('volumeCount') or 0
            # Prefer exact chapter count match, fall back to first result
            if total_chapters and abs(ch_count - total_chapters) <= 2:
                kitsu_id = item['id']
                break
            if kitsu_id is None:
                kitsu_id = item['id']

        if not kitsu_id:
            return {}

        # Paginate through all chapters
        mapping: dict[str, int] = {}
        offset = 0
        limit  = 20
        async with httpx.AsyncClient(timeout=15) as client:
            while True:
                r = await client.get(
                    'https://kitsu.io/api/edge/chapters',
                    params={
                        'filter[manga_id]': kitsu_id,
                        'page[limit]':      limit,
                        'page[offset]':     offset,
                        'fields[chapters]': 'number,volumeNumber',
                    },
                    headers={'Accept': 'application/vnd.api+json'},
                )
                page = r.json()
                rows = page.get('data', [])
                if not rows:
                    break
                for ch in rows:
                    attrs   = ch.get('attributes', {})
                    ch_num  = attrs.get('number')
                    vol_num = attrs.get('volumeNumber')
                    if ch_num is not None and vol_num is not None:
                        try:
                            mapping[str(int(float(ch_num)))] = int(float(vol_num))
                        except (ValueError, TypeError):
                            pass
                # Check if there are more pages
                next_link = (page.get('links') or {}).get('next')
                if not next_link:
                    break
                offset += limit
                if offset > 2000:  # safety cap
                    break

        return mapping
    except Exception as e:
        print(f"[Kitsu] chapter map error: {e}")
        log_event('metadata_fetch_failed',
                   f'kitsu chapter-map failed: {type(e).__name__}: {str(e)[:120]}')
    return {}


# ── Chapter-volume map validation / cleanup ──────────────────────────────────

def _trim_cvm_to_vol_range(mapping: dict, total_volumes: int | None,
                           source: str) -> dict:
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
        print(f"[{source}] dropped {dropped} cvm entries targeting "
              f"vol > {total_volumes} (likely continuous-numbering "
              f"contamination across series parts)")
    return kept


def _validate_chapter_map(mapping: dict, total_chapters: int | None, source: str) -> bool:
    """Return False if the map looks too sparse to be useful."""
    if not mapping:
        return False
    if total_chapters and total_chapters > 10:
        coverage = len(mapping) / total_chapters
        if coverage < 0.5:
            print(f"[{source}] map covers only {len(mapping)}/{total_chapters} chapters ({coverage:.0%}) — discarding")
            return False
    if len(set(mapping.values())) < 2:
        print(f"[{source}] all chapters map to same volume — discarding")
        return False
    return True


# ── Wikipedia number-word table (used by main.fetch_wikipedia_volume_count) ──

# Word-to-integer mapping used when parsing Wikipedia natural-language counts.
_WIKI_WORD_NUMS: dict[str, int] = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
    'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14,
    'fifteen': 15, 'sixteen': 16, 'seventeen': 17, 'eighteen': 18,
    'nineteen': 19, 'twenty': 20, 'twenty-one': 21, 'twenty-two': 22,
    'twenty-three': 23, 'twenty-four': 24, 'twenty-five': 25,
    'twenty-six': 26, 'twenty-seven': 27, 'twenty-eight': 28,
    'twenty-nine': 29, 'thirty': 30, 'thirty-one': 31, 'thirty-two': 32,
    'thirty-three': 33, 'thirty-four': 34, 'thirty-five': 35,
    'forty': 40, 'forty-five': 45, 'fifty': 50,
}
