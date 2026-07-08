"""DB-coupled metadata enrichment helpers.

Fifteenth module extracted from main.py. Picks up what was deferred
from metadata.py: these helpers all touch the DB (write chapter maps,
create volume stubs, log events), or orchestrate cross-source
enrichment across Wikipedia / Google Books / MangaUpdates in ways
that coordinate with the rest of the app state.

Module constants:
  - _NON_STANDARD_STUB_EDITIONS  — edition types where AniList's
                                   total_volumes reflects the standard
                                   edition, not the special one;
                                   stub auto-creation is suppressed
                                   for these (rescan creates them from
                                   real files instead)
  - _EDITION_SEARCH_KEYWORDS     — ordered keyword lists per edition
                                   type for Google Books queries

Chapter→volume mapping helpers:
  - get_series_chapter_map       — load cached cvm JSON for a series
  - chapters_to_volume_set       — resolve a chapter range to volumes,
                                   using the cvm when dense enough,
                                   falling back to linear approximation
  - _coverage_already_grabbed    — decide whether a candidate pack is
                                   already fully covered (Stage 3 rules)
  - _extract_map_from_cbzs       — scan CBZ filenames for danke-Empire
                                   c{N}(v{N}) patterns (fallback source)
  - refresh_mangadex_map         — orchestrate MangaDex → Kitsu → CBZ
                                   fallback chain and persist result

Edition volume-count enrichment:
  - fetch_wikipedia_volume_count — parse wikitext near edition keywords
  - fetch_edition_volume_count   — Google Books (with Wikipedia +
                                   AniList fallbacks), persists stubs
  - fetch_mu_metadata            — MangaUpdates cross-reference for
                                   standard editions, safe-no-downgrade

_series_library_dir is imported lazily inside refresh_mangadex_map to
break the rescan ↔ metadata_enrichment cycle.
"""

from __future__ import annotations

import json
import os
import re
import zipfile

import httpx

from metadata import (
    _WIKI_WORD_NUMS,
    _trim_cvm_to_vol_range,
    _validate_chapter_map,
    fetch_chapter_volume_map,
    fetch_kitsu_chapter_map,
    fetch_mangadex_id,
    mu_search,
    mu_slug_to_id,
)
from parsing import normalize
from shared import get_cfg, get_db
from events import log_event
from volumes import create_volume_stubs, populate_chapters


# ── Edition-related constants ────────────────────────────────────────────────

# Edition types where AniList's total_volumes reflects the *standard* edition count,
# not the special edition count. Stub auto-creation is suppressed for these; stubs
# are instead created by rescan once real files are present.
_NON_STANDARD_STUB_EDITIONS = {"omnibus", "deluxe", "special", "collector", "remaster"}

# Search keywords used when querying Google Books for edition-specific volume counts.
# Listed from most-specific to least-specific so we try the best match first.
_EDITION_SEARCH_KEYWORDS: dict[str, list[str]] = {
    "omnibus": [
        "omnibus",
        "2-in-1",
        "3-in-1",
        "two-in-one",
        "three-in-one",
        "two in one",
    ],
    "deluxe": ["deluxe edition", "deluxe"],
    "collector": ["collector's edition", "collector"],
    "special": ["special edition"],
    "remaster": ["remaster", "remastered"],
}


# ── Chapter→volume mapping helpers ───────────────────────────────────────────


def get_series_chapter_map(series_id: int) -> dict:
    """Load cached chapter→volume map for a series from DB."""
    with get_db() as db:
        row = db.execute(
            "SELECT chapter_vol_map FROM series WHERE id=?", (series_id,)
        ).fetchone()
    if row and row["chapter_vol_map"]:
        try:
            return json.loads(row["chapter_vol_map"])
        except Exception:
            pass
    return {}


def chapters_to_volume_set(
    ch_start: float,
    ch_end: float,
    chapter_map: dict,
    total_chapters: int | None,
    total_volumes: int | None,
) -> set:
    """Resolve a chapter range to the set of volume numbers it covers.

    Uses MangaDex mapping when available and sufficiently dense;
    falls back to linear approximation otherwise.
    """
    volumes: set[int] = set()
    if chapter_map:
        for ch_str, vol_num in chapter_map.items():
            try:
                ch = float(ch_str)
                if ch_start <= ch <= ch_end:
                    volumes.add(vol_num)
            except (ValueError, TypeError):
                pass
        # Only trust the map if it found volumes OR the map is dense enough
        # A sparse map (e.g. DMCA'd series with 2 entries) should fall through to approximation
        expected_in_range = ch_end - ch_start + 1
        # Map is trustworthy if it covers ≥30% of the expected chapters in range
        map_coverage = sum(
            1
            for ch_str in chapter_map
            if ch_start <= float(ch_str) <= ch_end
            if ch_str.replace(".", "").isdigit()
        )
        if volumes and map_coverage >= max(3, expected_in_range * 0.3):
            return volumes
    # Linear approximation fallback (also used when map is sparse)
    if total_chapters and total_chapters > 0 and total_volumes and total_volumes > 0:
        chs_per_vol = total_chapters / total_volumes
        ch_start_capped = min(ch_start, total_chapters)
        ch_end_capped = min(ch_end, total_chapters)
        vol_start = max(1, round(ch_start_capped / chs_per_vol))
        vol_end = min(total_volumes, round(ch_end_capped / chs_per_vol))
        if vol_start <= vol_end:
            return set(range(vol_start, vol_end + 1))
    return volumes


def _coverage_already_grabbed(
    series_id: int,
    pack_type: str,
    vol_rng: tuple | None,
    ch_range: tuple | None,
    ch_map: dict,
    total_chs: int | None,
    total_vols: int | None,
) -> bool:
    """Return True if the content this pack would provide is already fully
    covered by existing non-special grabbed/downloaded rows.

    Stage 3 rules:
      - Only non-special rows (is_special = 0) can satisfy mainline coverage.
        A Gaiden / oneshot / side-story grab does NOT cover mainline slots.
      - Volume matching is float-precise: volume 3 does not cover 3.5 or 3a.
      - Existing volume-range rows count as coverage — a row with
        vol_range_start=1, vol_range_end=5 and status in (grabbed, downloaded)
        satisfies targets 1..5 even if interior stubs are still 'wanted'.
      - "Grabbed" and "downloaded" both count as covering; the pre-Stage-3
        logic only inspected 'wanted' stubs, which missed ranges that hadn't
        cascaded into interior stubs.
    """
    with get_db() as db:
        # A non-special complete pack supersedes any narrower new pack.
        has_complete = db.execute(
            "SELECT 1 FROM volumes WHERE series_id=? AND pack_type='complete'"
            " AND status IN ('grabbed','downloaded')"
            " AND COALESCE(is_special, 0) = 0",
            (series_id,),
        ).fetchone()
        if has_complete and pack_type != "complete":
            return True

        # For a new complete pack, only skip if no mainline wanted+monitored
        # stubs remain (specials don't block a mainline complete grab).
        if pack_type == "complete":
            wanted = db.execute(
                "SELECT 1 FROM volumes WHERE series_id=? AND volume_num IS NOT NULL"
                " AND status='wanted' AND monitored=1"
                " AND COALESCE(is_special, 0) = 0",
                (series_id,),
            ).fetchone()
            return wanted is None

        # Determine target volumes. Keep them as floats so fractional
        # parser outputs round-trip cleanly (no int cast collapse).
        # For volume ranges, include the explicit endpoints plus any
        # integer volumes in between — (3.5, 3.5) → {3.5}, not {3}.
        target_vols: set[float] = set()
        if pack_type == "chapter" and ch_range:
            target_vols = {
                float(v)
                for v in chapters_to_volume_set(
                    ch_range[0], ch_range[1], ch_map, total_chs, total_vols
                )
            }
        elif pack_type == "chapter" and not ch_range:
            return False  # unknown coverage → don't skip
        elif pack_type == "volume" and vol_rng:
            start_f, end_f = float(vol_rng[0]), float(vol_rng[1])
            target_vols = {start_f, end_f}
            lo = int(start_f) + 1
            hi = int(end_f)
            for iv in range(lo, hi + 1):
                target_vols.add(float(iv))
        else:
            return False

        if not target_vols:
            return False

        # Each target must be satisfied by SOME non-special row, either a
        # precise volume_num match OR a range row covering it. Use one
        # parameterised SELECT per target — the loop is cheap and keeps
        # the SQL trivially readable.
        satisfy_sql = (
            "SELECT 1 FROM volumes WHERE series_id=?"
            "  AND status IN ('grabbed','downloaded')"
            "  AND COALESCE(is_special, 0) = 0"
            "  AND ("
            "    volume_num = ?"
            "    OR (vol_range_start IS NOT NULL AND vol_range_end IS NOT NULL"
            "        AND vol_range_start <= ? AND vol_range_end >= ?)"
            "  )"
            "  LIMIT 1"
        )
        for v in target_vols:
            row = db.execute(satisfy_sql, (series_id, v, v, v)).fetchone()
            if row is None:
                return False
        return True


def _extract_map_from_cbzs(series_dir: str) -> dict:
    """Scan downloaded CBZ/CBR files in series_dir for danke-Empire style filenames:
      Title - c{N} (v{N}) - p{N} ...
    Returns {chapter_str: vol_int} mapping.
    """
    mapping: dict[str, int] = {}
    if not series_dir or not os.path.isdir(series_dir):
        return mapping
    pat = re.compile(r"\bc(\d+(?:\.\d+)?)\s*\(v(\d+)\)", re.IGNORECASE)
    for fname in os.listdir(series_dir):
        if not fname.lower().endswith((".cbz", ".cbr", ".zip")):
            continue
        fpath = os.path.join(series_dir, fname)
        try:
            with zipfile.ZipFile(fpath) as zf:
                for entry in zf.namelist():
                    m = pat.search(entry)
                    if m:
                        ch_key = m.group(1)  # keep as string e.g. "1", "168.1"
                        vol_num = int(m.group(2))
                        mapping[ch_key] = vol_num
        except Exception:
            pass
    return mapping


async def refresh_mangadex_map(series_id: int) -> bool:
    """Look up MangaDex, store chapter→volume map and cross-reference IDs.
    Returns True if successful."""
    from rescan import _series_library_dir  # noqa: WPS433 (lazy to avoid cycle)

    with get_db() as db:
        s = db.execute(
            "SELECT title, anilist_id, mangadex_id, mal_id, mu_id FROM series WHERE id=?",
            (series_id,),
        ).fetchone()
    if not s:
        return False
    mdx_id = s["mangadex_id"]
    links: dict[str, str] = {}
    if not mdx_id:
        mdx_id, links = await fetch_mangadex_id(s["title"], s["anilist_id"], s["mu_id"])
    elif not s["mal_id"] or not s["mu_id"]:
        # Have UUID but missing cross-refs — fetch links from MangaDex by ID
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"https://api.mangadex.org/manga/{mdx_id}")
            md_data = r.json().get("data", {})
            links = (md_data.get("attributes") or {}).get("links") or {}
        except Exception:
            pass
    if not mdx_id:
        log_event("metadata_fetch_failed", f"[MangaDex] Could not find ID for series {series_id}", series_id)
        return False
    with get_db() as db:
        meta = db.execute(
            "SELECT title, total_volumes, total_chapters FROM series WHERE id=?",
            (series_id,),
        ).fetchone()
    total_ch = meta["total_chapters"] if meta else None
    total_vol = meta["total_volumes"] if meta else None

    mapping = await fetch_chapter_volume_map(mdx_id)
    mapping = _trim_cvm_to_vol_range(mapping, total_vol, "MangaDex")
    map_source = "mangadex"
    if not _validate_chapter_map(mapping, total_ch, "MangaDex"):
        mapping = {}

    # Fallback when MangaDex has no usable chapter data (DMCA'd / sparse): try Kitsu
    if not mapping and meta:
        kitsu_map = await fetch_kitsu_chapter_map(
            meta["title"], s["anilist_id"], meta["total_chapters"]
        )
        kitsu_map = _trim_cvm_to_vol_range(kitsu_map, total_vol, "Kitsu")
        if _validate_chapter_map(kitsu_map, total_ch, "Kitsu"):
            mapping = kitsu_map
            map_source = "kitsu"

    # Fallback: extract chapter→volume map from downloaded CBZ filenames
    if not mapping:
        with get_db() as db:
            cbz_dir = _series_library_dir(db, series_id)
        cbz_map = _extract_map_from_cbzs(cbz_dir) if cbz_dir else {}
        cbz_map = _trim_cvm_to_vol_range(cbz_map, total_vol, "CBZ")
        log_event(
            "metadata",
            f"[CBZ] series {series_id}: dir={cbz_dir}, entries={len(cbz_map)}, total_ch={total_ch}",
            series_id,
        )
        if _validate_chapter_map(cbz_map, total_ch, "CBZ"):
            mapping = cbz_map
            map_source = "cbz"

    # Extract cross-reference IDs from MangaDex links
    mal_from_mdx = links.get("mal")
    mu_slug = links.get("mu")
    mu_from_mdx = mu_slug_to_id(mu_slug) if mu_slug else None

    with get_db() as db:
        db.execute(
            "UPDATE series SET mangadex_id=?, chapter_vol_map=?,"
            " mal_id=COALESCE(mal_id, ?), mu_id=COALESCE(mu_id, ?) WHERE id=?",
            (
                mdx_id,
                json.dumps(mapping) if mapping else None,
                int(mal_from_mdx)
                if mal_from_mdx and str(mal_from_mdx).isdigit()
                else None,
                mu_from_mdx,
                series_id,
            ),
        )
        if mapping:
            ch_created = populate_chapters(db, series_id)
            log_event(
                "metadata",
                f"[{map_source.upper()}] Stored {len(mapping)} chapter→vol entries for series {series_id}"
                + (f", created {ch_created} chapter stubs" if ch_created else ""),
                series_id,
            )
        else:
            log_event(
                "metadata",
                f"[MangaDex] No chapter map for {mdx_id} — cross-refs only (no fallback data)",
                series_id,
            )
    return True


# ── Edition volume-count enrichment (Wikipedia / Google Books / MU) ──────────


async def fetch_wikipedia_volume_count(
    series_id: int, title: str, edition_type: str
) -> int | None:
    """Query Wikipedia to find edition-specific volume counts as a fallback when
    Google Books returns insufficient data. Parses wikitext for patterns like
    "X volumes" or "fourteen volumes have been released" near edition keywords.
    Returns the count or None if not found with sufficient confidence.
    No API key required — Wikipedia is free and openly accessible.
    """
    edition_kws = _EDITION_SEARCH_KEYWORDS.get(edition_type, [])
    if not edition_kws:
        return None

    # Try both "{title} (manga)" and bare title; follow redirects automatically
    wikitext: str | None = None
    for search_title in [f"{title} (manga)", title]:
        try:
            async with httpx.AsyncClient(timeout=15) as cli:
                r = await cli.get(
                    "https://en.wikipedia.org/w/api.php",
                    params={
                        "action": "query",
                        "titles": search_title,
                        "prop": "revisions",
                        "rvprop": "content",
                        "rvslots": "main",
                        "format": "json",
                        "redirects": "1",
                    },
                    headers={
                        "User-Agent": "mangarr/1.0 (manga metadata; github.com/khak1s/manga-arr)"
                    },
                )
            r.raise_for_status()
            pages = r.json().get("query", {}).get("pages", {})
            for page in pages.values():
                if page.get("pageid", -1) == -1:
                    continue  # missing page
                revs = page.get("revisions", [])
                if revs:
                    # Newer rvslots format
                    wikitext = revs[0].get("slots", {}).get("main", {}).get("*", "")
                    if not wikitext:
                        # Legacy format
                        wikitext = revs[0].get("*", "")
                break
            if wikitext:
                break
        except Exception as e:
            log_event("metadata_fetch_failed", f"[Wikipedia] series {series_id} '{search_title}': {e}", series_id)

    if not wikitext:
        log_event("metadata_fetch_failed", f"[Wikipedia] series {series_id} '{title}': no article found", series_id)
        return None

    # Strip <ref> footnotes — they contain long URLs that inflate character
    # distances between prose keywords and volume counts.
    wikitext = re.sub(r"<ref[^>]*/>", "", wikitext)
    wikitext = re.sub(r"<ref[^>]*>.*?</ref>", "", wikitext, flags=re.DOTALL)

    # Build a pattern that matches digit strings or written-out number words
    # followed by "volume(s)" — e.g. "14 volumes" or "fourteen volumes"
    _word_alts = "|".join(
        re.escape(w) for w in sorted(_WIKI_WORD_NUMS, key=len, reverse=True)
    )
    _num_pat = rf"(\d+|{_word_alts})\s+volumes?"

    def _parse(s: str) -> int | None:
        if s.isdigit():
            n = int(s)
            return n if 1 <= n <= 200 else None
        return _WIKI_WORD_NUMS.get(s.lower())

    # For each edition keyword, scan a 600-char window around each occurrence
    candidates: list[int] = []
    for kw in edition_kws:
        for m in re.finditer(re.escape(kw), wikitext, re.IGNORECASE):
            start = max(0, m.start() - 500)
            end = min(len(wikitext), m.end() + 500)
            window = wikitext[start:end]
            for nm in re.finditer(_num_pat, window, re.IGNORECASE):
                count = _parse(nm.group(1))
                if count:
                    candidates.append(count)

    if not candidates:
        log_event(
            "metadata",
            f"[Wikipedia] series {series_id} '{title}' ({edition_type}): "
            f"no volume counts found near edition keywords",
            series_id,
        )
        return None

    # Sanity filter: non-standard editions always have fewer volumes than the standard
    # edition. Candidates at or above 85% of the standard count are almost certainly
    # the standard count bleeding through from nearby text (e.g. "Deluxe Edition...
    # the series ran for 43 volumes"). Filter those out before taking the max.
    #
    # Only apply this filter when vol_count_source is 'anilist' — meaning total_volumes
    # is the provisional AniList standard count. If it's already been enriched by
    # Google Books or Wikipedia, total_volumes is the edition-specific count and
    # should not be used as the standard-edition upper bound.
    with get_db() as db:
        std_row = db.execute(
            "SELECT total_volumes, vol_count_source FROM series WHERE id=?",
            (series_id,),
        ).fetchone()
    std_count = 0
    if std_row and (std_row["vol_count_source"] or "anilist") == "anilist":
        std_count = std_row["total_volumes"] or 0

    if std_count > 0:
        threshold = std_count * 0.85
        filtered = [c for c in candidates if c < threshold]
        if filtered:
            candidates = filtered
            # else: all candidates were near the standard count — keep original set
            # rather than returning nothing

    best = max(candidates)
    log_event(
        "metadata",
        f"[Wikipedia] series {series_id} '{title}' ({edition_type}): "
        f"found {best} volumes (all candidates: {sorted(set(candidates))}, "
        f"std_count={std_count})",
        series_id,
    )
    return best


async def fetch_edition_volume_count(
    series_id: int, title: str, edition_type: str
) -> int | None:
    """Query Google Books to find the correct total_volumes for a non-standard edition
    (omnibus, deluxe, collector, special, remaster). Returns the max volume number
    found, or None if the result was not confident enough to trust.
    """
    keywords = _EDITION_SEARCH_KEYWORDS.get(edition_type)
    if not keywords:
        return None

    # Idempotency: don't overwrite a better source that's already set
    with get_db() as db:
        src_row = db.execute(
            "SELECT vol_count_source FROM series WHERE id=?", (series_id,)
        ).fetchone()
    current_source = (src_row["vol_count_source"] if src_row else None) or "anilist"
    if current_source in ("google_books", "wikipedia", "manual"):
        log_event(
            "metadata",
            f"[GoogleBooks] series {series_id}: skipping — source already '{current_source}'",
            series_id,
        )
        return None

    title_words = set(normalize(title).lower().split())
    _gb_key = get_cfg("google_books_api_key", "").strip()

    async def _gb_query(q: str) -> list[dict] | None:
        """Run one Google Books query. Returns items list, or None on quota/error."""
        _p: dict = {"q": q, "maxResults": 40, "printType": "books"}
        if _gb_key:
            _p["key"] = _gb_key
        try:
            async with httpx.AsyncClient(timeout=15) as cli:
                _r = await cli.get(
                    "https://www.googleapis.com/books/v1/volumes",
                    params=_p,
                    headers={"User-Agent": "mangarr/1.0"},
                )
            if _r.status_code == 429:
                log_event(
                    "metadata_fetch_failed",
                    f"[GoogleBooks] series {series_id}: daily quota exceeded. "
                    f"Add a Google Books API key in Settings to increase the limit.",
                    series_id,
                )
                return None  # signal quota — stop all queries
            _r.raise_for_status()
            return _r.json().get("items", [])
        except Exception as e:
            log_event("metadata_fetch_failed", f"[GoogleBooks] series {series_id} query '{q}': {e}", series_id)
            return []

    def _extract_vols(items: list[dict]) -> set[int]:
        nums: set[int] = set()
        for item in items:
            vol_title = ((item.get("volumeInfo") or {}).get("title") or "").lower()
            # Filter: all series title words must appear in the book title
            if not all(w in vol_title for w in title_words):
                continue
            # Strip parenthetical content like "(Vol. 22-24)" or "(Includes vols. 1-3)"
            # before extracting numbers — otherwise standard-range suffixes inflate the max.
            clean = re.sub(r"\s*\([^)]*\)", "", vol_title).strip()
            for m in re.finditer(
                r"(?:vol(?:ume)?\.?\s*)(\d+)|(?<!\d)(\d+)(?!\d)", clean, re.IGNORECASE
            ):
                n = int(m.group(1) or m.group(2))
                if 1 <= n <= 999:
                    nums.add(n)
        return nums

    found_volumes: set[int] = set()
    quota_hit = False

    # Strategy 1: exact quoted phrase for each keyword — most precise
    for keyword in keywords:
        items = await _gb_query(f'"{title}" "{keyword}"')
        if items is None:
            quota_hit = True
            break
        found_volumes |= _extract_vols(items)
        if len(found_volumes) >= 2:
            break

    # Strategy 2: unquoted fallback — more permissive, catches cases where Google Books
    # metadata doesn't contain the exact keyword string
    if not quota_hit and len(found_volumes) < 2:
        for keyword in keywords:
            items = await _gb_query(f"{title} {keyword}")
            if items is None:
                quota_hit = True
                break
            found_volumes |= _extract_vols(items)
            if len(found_volumes) >= 2:
                break

    if quota_hit:
        return None

    if len(found_volumes) < 2 or (max(found_volumes) - min(found_volumes)) < 1:
        log_event(
            "metadata",
            f"[GoogleBooks] series {series_id} '{title}' ({edition_type}): "
            f"insufficient data — found volumes {sorted(found_volumes)}",
            series_id,
        )

        # Fallback 1: Try Wikipedia for edition-specific volume count
        wiki_count = await fetch_wikipedia_volume_count(series_id, title, edition_type)
        if wiki_count:
            with get_db() as db:
                db.execute(
                    "UPDATE series SET total_volumes=?, vol_count_source='wikipedia' WHERE id=?",
                    (wiki_count, series_id),
                )
                create_volume_stubs(db, series_id, wiki_count)
            log_event(
                "metadata",
                f"[Wikipedia] {edition_type} edition: {wiki_count} volumes "
                f"(Google Books had insufficient data)",
                series_id,
            )
            return wiki_count

        # Fallback 2: use AniList standard count as provisional stubs so the series
        # isn't left idle with nothing to search for. vol_count_source stays 'anilist'
        # so the warning banner appears on the series page.
        with get_db() as db:
            al_row = db.execute(
                "SELECT total_volumes FROM series WHERE id=?", (series_id,)
            ).fetchone()
            al_count = (al_row["total_volumes"] or 0) if al_row else 0
            if al_count > 0:
                create_volume_stubs(db, series_id, al_count)
        if al_count > 0:
            log_event(
                "warning",
                f"[GoogleBooks/Wikipedia] Could not find {edition_type} volume count. "
                f"Using AniList standard count ({al_count}) as provisional fallback — "
                f"may be inaccurate. Use 'Refresh Edition Metadata' for the correct count.",
                series_id,
            )
        return None

    best_count = max(found_volumes)
    with get_db() as db:
        db.execute(
            "UPDATE series SET total_volumes=?, vol_count_source='google_books' WHERE id=?",
            (best_count, series_id),
        )
        create_volume_stubs(db, series_id, best_count)
    log_event(
        "metadata",
        f"[GoogleBooks] {edition_type} edition: {best_count} volumes "
        f"(keywords tried: {keywords[: len(found_volumes)]})",
        series_id,
    )
    return best_count


async def fetch_mu_metadata(series_id: int, title: str) -> dict | None:
    """Cross-reference MangaUpdates to get a more reliable volume count for standard
    editions, and to populate mu_id if missing. Never overwrites google_books or manual
    sources. Returns a summary dict or None if no confident match was found.
    """
    with get_db() as db:
        s_row = db.execute(
            "SELECT mu_id, edition_type, total_volumes, vol_count_source FROM series WHERE id=?",
            (series_id,),
        ).fetchone()
    if not s_row:
        return None

    current_source = s_row["vol_count_source"] or "anilist"
    if current_source in ("google_books", "wikipedia", "manual"):
        return None  # never downgrade

    # Search MU — reuse existing mu_search() which already parses volume counts
    results = await mu_search(title)
    if not results:
        return None

    stored_words = set(normalize(title).split())

    def _f1(r_title: str) -> float:
        r_words = set(normalize(r_title).split())
        if not r_words or not stored_words:
            return 0.0
        inter = stored_words & r_words
        rec = len(inter) / len(stored_words)
        prec = len(inter) / len(r_words)
        return 2 * rec * prec / (rec + prec) if (rec + prec) else 0.0

    best = max(results, key=lambda r: _f1(r["title"]))
    if _f1(best["title"]) < 0.7:
        return None  # not confident enough for silent background enrichment

    matched_mu_id = best["mu_id"]
    mu_vol_count = best[
        "volumes"
    ]  # already parsed from "N Volumes (Complete)" by mu_search()
    edition = s_row["edition_type"] or "standard"
    current_vols = s_row["total_volumes"] or 0

    updated_vols = False
    with get_db() as db:
        # Always store mu_id if we didn't have one
        if matched_mu_id and not s_row["mu_id"]:
            db.execute(
                "UPDATE series SET mu_id=? WHERE id=? AND (mu_id IS NULL OR mu_id='')",
                (matched_mu_id, series_id),
            )
        # Update volume count only for standard editions where MU count is strictly higher
        should_update = (
            edition not in _NON_STANDARD_STUB_EDITIONS
            and mu_vol_count is not None
            and mu_vol_count > current_vols
            and current_source not in ("google_books", "wikipedia", "manual")
        )
        if should_update:
            db.execute(
                "UPDATE series SET total_volumes=?, vol_count_source='mangaupdates' WHERE id=?",
                (mu_vol_count, series_id),
            )
            create_volume_stubs(db, series_id, mu_vol_count)
            updated_vols = True
            log_event(
                "metadata",
                f"[MangaUpdates] updated vol count: {current_vols}→{mu_vol_count}",
                series_id,
            )

    return {
        "mu_id": matched_mu_id,
        "volumes": mu_vol_count,
        "updated_vols": updated_vols,
    }
