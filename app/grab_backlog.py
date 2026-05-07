"""Backlog search: find and grab existing releases across all sources.

Implements the full-title backlog search pipeline for existing series:
  - Complete pack search for finished series
  - Per-release matching across main title and aliases
  - Coverage-aware greedy pack selection
  - Gap-fill for missing volumes
"""
from __future__ import annotations

import difflib
import re
from shared import get_cfg, get_db, vol_num_to_search
try:
    from .grab_core import grab_item, _search_all
except ImportError:
    from grab_core import grab_item, _search_all


async def grab_existing(series_id: int, title: str, pattern: str) -> int:
    """Search all sources for all releases; grab unseen matches. Respects aliases."""
    from events import log_event
    try:
        return await _grab_existing_inner(series_id, title, pattern)
    except Exception as e:
        log_event('error', f"[grab_existing] Unhandled error for '{title}': {e}", series_id)
        print(f"[grab_existing] series {series_id} '{title}': {e}")
        return 0


async def _grab_existing_inner(series_id: int, title: str, pattern: str) -> int:
    from events import log_event

    # ── Complete-pack-first strategy for finished series ─────────────────────
    with get_db() as db:
        s_row = db.execute(
            "SELECT status, total_volumes FROM series WHERE id=?", (series_id,)
        ).fetchone()
        if s_row and s_row['status'] == 'FINISHED' and s_row['total_volumes']:
            wanted_count = db.execute(
                "SELECT COUNT(*) FROM volumes WHERE series_id=? AND status='wanted'",
                (series_id,)
            ).fetchone()[0]
            total = s_row['total_volumes']
            if wanted_count >= total * 0.5:
                grabbed = await search_complete_pack(series_id, title, total)
                if grabbed > 0:
                    log_event('search',
                              f"Complete pack grabbed for finished series '{title}' — skipping individual search",
                              series_id)
                    return grabbed

    all_items = await _search_all(title, series_id=series_id)

    with get_db() as db:
        seen_urls    = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM seen").fetchall()}
        blocked_urls = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM blocklist").fetchall()}
        alias_rows   = db.execute(
            "SELECT alias FROM series_aliases WHERE series_id=?", (series_id,)
        ).fetchall()

    all_patterns = [pattern] + [a['alias'] for a in alias_rows]

    for alias in [a['alias'] for a in alias_rows]:
        extra = await _search_all(alias, series_id=series_id)
        for it in extra:
            if it['url'] not in {x['url'] for x in all_items}:
                all_items.append(it)

    grabbed = 0
    for item in all_items:
        if item['url'] in seen_urls or item['url'] in blocked_urls:
            continue
        if any(matches(p, item['title']) for p in all_patterns):
            if await grab_item(item, series_id):
                grabbed += 1
    log_event('search', f"Search '{title}': {len(all_items)} candidates, {grabbed} grabbed", series_id)
    return grabbed


def matches(pattern: str, text: str) -> bool:
    """Simple pattern matching helper (moved here to avoid circularity)."""
    return re.search(pattern, text, re.IGNORECASE) is not None


def _select_covering_packs(
    items: list[dict],
    missing_vols: set[float],
    total_volumes: int | None,
    all_patterns: list[str],
) -> list[dict]:
    """
    Greedy non-overlapping selection of complete/range packs that maximises
    coverage of missing_vols.  Sorted largest-coverage-first, then by seeders.
    Returns ordered list of packs to grab (non-overlapping by volume range).
    """
    from parsing import matches as _matches
    candidates = []
    for item in items:
        if not any(_matches(p, item['title']) for p in all_patterns):
            continue
        item_complete = is_complete_pack(item['title'], total_volumes)
        rng = extract_volume_range(item['title'])
        if item_complete:
            covered = set(missing_vols)
        elif rng:
            covered = {v for v in missing_vols if rng[0] <= v <= rng[1]}
        else:
            continue
        if not covered:
            continue
        candidates.append((len(covered), rng, item, covered))

    candidates.sort(key=lambda x: (x[0], x[2].get('seeders', 0)), reverse=True)

    selected: list[dict] = []
    claimed:  set[float] = set()
    for _coverage, _rng, item, covered in candidates:
        newly = covered - claimed
        if not newly:
            continue
        selected.append(item)
        claimed |= newly
        if claimed >= missing_vols:
            break
    return selected


def _matches(pattern: str, text: str) -> bool:
    """Simple pattern matching helper."""
    return re.search(pattern, text, re.IGNORECASE) is not None


def is_complete_pack(title: str, total_volumes: int | None) -> bool:
    """Check if title indicates a complete pack."""
    from parsing import is_complete_pack as _is_complete
    return _is_complete(title, total_volumes)


def extract_volume_range(title: str) -> tuple[float, float] | None:
    """Extract volume range from title."""
    from parsing import extract_volume_range as _extract
    return _extract(title)


async def search_complete_pack(series_id: int, title: str,
                                total_volumes: int | None) -> int:
    """Search all sources specifically for complete series packs."""
    from events import log_event
    from parsing import is_complete_pack, extract_volume_range, is_foreign_language, matches as _matches, normalize

    with get_db() as db:
        seen_urls    = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM seen").fetchall()}
        blocked_urls = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM blocklist").fetchall()}
        alias_rows   = db.execute(
            "SELECT alias FROM series_aliases WHERE series_id=?", (series_id,)
        ).fetchall()
    aliases = [a['alias'] for a in alias_rows]
    all_patterns = [title] + aliases

    def _useful_search_term(term: str) -> bool:
        if not term or len(term) < 3:
            return False
        if is_foreign_language(term):
            return False
        latin = len(re.findall(r'[a-zA-Z]', term))
        return latin >= max(1, len(term.replace(' ', '')) * 0.5)

    norm_title = normalize(title)
    useful_aliases = [
        a for a in aliases
        if _useful_search_term(a) and normalize(a) != norm_title
    ]
    useful_aliases.sort(
        key=lambda a: difflib.SequenceMatcher(None, norm_title, normalize(a)).ratio()
    )

    search_terms: list[str] = [title] + useful_aliases[:7]

    end_str = f"v01-v{int(total_volumes):02d}" if total_volumes else None

    seen_item_urls: set[str] = set()
    all_items: list[dict] = []

    async def _add_results(query: str):
        for item in await _search_all(query, series_id=series_id):
            if item['url'] not in seen_item_urls:
                seen_item_urls.add(item['url'])
                all_items.append(item)

    for term in search_terms:
        await _add_results(term)
        await _add_results(f"{term} complete")
        if end_str:
            await _add_results(f"{term} {end_str}")

    with get_db() as db:
        missing_vols: set[float] = {
            float(r['volume_num'])
            for r in db.execute(
                "SELECT volume_num FROM volumes WHERE series_id=? AND status='wanted'"
                " AND volume_num IS NOT NULL",
                (series_id,)
            ).fetchall()
        }

    available = [i for i in all_items
                 if i['url'] not in seen_urls and i['url'] not in blocked_urls]

    packs_to_grab = _select_covering_packs(
        available, missing_vols or set(range(1, (total_volumes or 1) + 1)),
        total_volumes, all_patterns
    )

    grabbed = 0
    for item in packs_to_grab:
        if await grab_item(item, series_id):
            grabbed += 1

    claimed_by_packs: set[float] = set()
    for item in packs_to_grab:
        if is_complete_pack(item['title'], total_volumes):
            claimed_by_packs |= missing_vols
        else:
            rng = extract_volume_range(item['title'])
            if rng:
                claimed_by_packs |= {v for v in missing_vols if rng[0] <= v <= rng[1]}
    gaps = missing_vols - claimed_by_packs

    gap_grabbed = 0
    for vol_num in sorted(gaps)[:10]:
        query = f"{title} vol {vol_num_to_search(vol_num)}"
        for item in await _search_all(query, series_id=series_id):
            if item['url'] in seen_urls or item['url'] in blocked_urls:
                continue
            if not any(_matches(p, item['title']) for p in all_patterns):
                continue
            item_vol = extract_volume_num(item['title'])
            if item_vol is not None and abs(item_vol - vol_num) < 0.02:
                if await grab_item(item, series_id):
                    gap_grabbed += 1
                break
    grabbed += gap_grabbed

    title_matched = sum(1 for item in all_items
                        if any(_matches(p, item['title']) for p in all_patterns))
    n_queries = len(search_terms) * (3 if end_str else 2) + len(gaps)
    print(f"[CompleteSearch] '{title}': {n_queries} queries ({len(search_terms)} terms), "
          f"{len(all_items)} raw candidates, {title_matched} title-matched, "
          f"{len(packs_to_grab)} packs + {gap_grabbed} gaps = {grabbed} grabbed")
    log_event('search',
              f"Complete pack search '{title}': {len(all_items)} candidates "
              f"({title_matched} matched), {grabbed} grabbed",
              series_id)
    return grabbed
