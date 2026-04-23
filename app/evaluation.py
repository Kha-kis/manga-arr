"""Release scoring and evaluation.

Twelfth module extracted from main.py. Contains the logic that turns
a raw indexer hit into a grab decision:

  - score_release          — compute a priority score, applying every
                             filter (language, release profiles, source
                             type, blocked/preferred groups, omnibus
                             preference, custom formats, year/volume
                             match). Returns a negative sentinel when
                             the release should be rejected outright.
  - evaluate_release       — structured evaluator used by the search UI;
                             returns the score plus human-readable
                             rejections + custom-format matches
  - _term_display          — human-readable display for a profile term
  - _term_match            — substring / regex match for a profile term
  - parse_size_bytes       — convert '1.2 GB' → int bytes

Router modules (release_profiles, custom_formats, language_profiles)
are imported lazily inside each function to avoid import cycles.
"""
from __future__ import annotations

import json
import re

from files import (
    detect_quality_from_title,
    is_official_release,
    is_quality_fan_release,
)
from parsing import (
    extract_volume_num,
    extract_volume_range,
    is_complete_pack,
    is_foreign_language,
)
from shared import from_json as _cfj, get_cfg, get_db


def score_release(title: str, series_id: int | None = None,
                  release_group: str = '', indexer: str = '', language: str = '',
                  volume_num: float | None = None, pub_year: int | None = None) -> int:
    """Score a release for grab priority.

    Returns -999 if the release should be ignored entirely. Higher
    score = higher priority.

    Uses release profiles when available (Sonarr-parity); falls back
    to global settings. Optional release_group/indexer/language are
    passed to custom format scoring.
    """
    t = title.lower()

    # Language rejection — skip non-English unless the series has a language profile
    # (language profiles handle per-series filtering; global reject only applies without one)
    _series_lang_profile_id: int | None = None
    if series_id is not None:
        try:
            with get_db() as _ldb:
                _lp_row = _ldb.execute(
                    "SELECT language_profile_id FROM series WHERE id=?", (series_id,)
                ).fetchone()
                if _lp_row and _lp_row['language_profile_id']:
                    _series_lang_profile_id = _lp_row['language_profile_id']
                else:
                    # Fall back to default language profile from settings
                    _def_row = _ldb.execute(
                        "SELECT value FROM settings WHERE key='default_language_profile_id'"
                    ).fetchone()
                    if _def_row:
                        try:
                            _series_lang_profile_id = int(_def_row['value'])
                        except (TypeError, ValueError):
                            pass
        except Exception as e:
            print(f"[score_release] language profile lookup failed: {e}")

    if _series_lang_profile_id is None and is_foreign_language(title):
        return -999

    # ── Release profiles (Sonarr-parity) ─────────────────────────────────────
    profile_score = None
    if series_id is not None:
        try:
            from routers.release_profiles import score_from_release_profiles
            with get_db() as _rp_db:
                _rp_tags = [r['tag'] for r in _rp_db.execute(
                    "SELECT tag FROM series_tags WHERE series_id=?", (series_id,)
                ).fetchall()]
                profile_score = score_from_release_profiles(title, _rp_tags, _rp_db)
        except Exception as e:
            print(f"[score_release] release profile scoring failed: {e}")

    if profile_score is not None:
        if profile_score <= -1000:
            return -999
        score = profile_score
    else:
        # ── Fall back to global settings ──────────────────────────────────────
        # Ignored words — skip entirely
        ignored = [w.strip().lower() for w in get_cfg('ignored_words', '').split(',') if w.strip()]
        for word in ignored:
            if re.search(r'\b' + re.escape(word) + r'\b', t, re.IGNORECASE):
                return -999

        # Required words — must match at least one
        required = [w.strip().lower() for w in get_cfg('required_words', '').split(',') if w.strip()]
        if required and not any(re.search(r'\b' + re.escape(w) + r'\b', t, re.IGNORECASE) for w in required):
            return -998

        # User preferred words — add score per match
        preferred = [w.strip().lower() for w in get_cfg('preferred_words', '').split(',') if w.strip()]
        score = 0
        for word in preferred:
            if word in t:
                score += 10

    # Blocked release groups — global + per-series
    blocked_groups = [g.strip().lower() for g in get_cfg('blocked_groups', '').split(',') if g.strip()]
    if series_id is not None:
        try:
            with get_db() as _bgdb:
                _s_bg = _bgdb.execute("SELECT blocked_groups FROM series WHERE id=?", (series_id,)).fetchone()
                if _s_bg and _s_bg['blocked_groups']:
                    blocked_groups += [g.strip().lower() for g in json.loads(_s_bg['blocked_groups']) if g.strip()]
        except Exception as e:
            print(f"[score_release] blocked groups lookup failed: {e}")
    for grp in blocked_groups:
        if grp in t:
            return -999

    # ── Source type filter ────────────────────────────────────────────────────
    # 'official_only' → only licensed publishers (Viz, Kodansha, Seven Seas…)
    # 'fan_only'      → only fan scanlations (no known publisher in title)
    # 'any' (default) → no filter
    if series_id is not None:
        try:
            with get_db() as _stdb:
                _st_row = _stdb.execute(
                    "SELECT source_type FROM series WHERE id=?", (series_id,)
                ).fetchone()
                if _st_row:
                    _st = (_st_row['source_type'] or 'any')
                    if _st == 'official_only' and not is_official_release(title):
                        return -999
                    elif _st == 'fan_only' and is_official_release(title):
                        return -999
        except Exception as e:
            print(f"[score_release] source type check failed: {e}")

    # ── Required source (strict name match) ───────────────────────────────────
    # If set, only releases whose title contains this exact string are grabbed.
    # Works for both publisher names ("Viz Media") and fan groups ("1r0n").
    if series_id is not None:
        try:
            with get_db() as _rsdb:
                _rs_row = _rsdb.execute(
                    "SELECT required_scanlator FROM series WHERE id=?", (series_id,)
                ).fetchone()
                if _rs_row and _rs_row['required_scanlator']:
                    _req_sc = _rs_row['required_scanlator'].strip().lower()
                    if _req_sc and _req_sc not in t:
                        return -999
        except Exception as e:
            print(f"[score_release] required source check failed: {e}")

    # ── Language profile check ────────────────────────────────────────────────
    if _series_lang_profile_id is not None:
        try:
            from routers.language_profiles import check_language_profile
            with get_db() as _lpdb:
                _lp_allowed, _lp_reason = check_language_profile(_lpdb, _series_lang_profile_id, title)
            if not _lp_allowed:
                return -9999
        except Exception as e:
            print(f"[score_release] language profile check failed: {e}")

    # Preferred release groups — global + per-series boost
    pref_groups = [g.strip().lower() for g in get_cfg('preferred_groups', '').split(',') if g.strip()]
    if series_id is not None:
        try:
            with get_db() as _pgdb:
                _s_pg = _pgdb.execute("SELECT preferred_groups FROM series WHERE id=?", (series_id,)).fetchone()
                if _s_pg and _s_pg['preferred_groups']:
                    pref_groups += [g.strip().lower() for g in json.loads(_s_pg['preferred_groups']) if g.strip()]
        except Exception as e:
            print(f"[score_release] preferred groups lookup failed: {e}")
    for grp in pref_groups:
        if grp in t:
            score += 15

    # ── Filter out non-manga content ──────────────────────────────────────
    # Video quality/codec markers — definitely not manga
    if re.search(r'\b(1080p|720p|480p|2160p|4k uhd|bluray|blu-ray|bdrip|webrip|web-dl|'
                 r'hdtv|x264|x265|h264|h265|hevc|avc|xvid|divx|remux|'
                 r'\.mkv|\.mp4|\.avi)\b', t):
        return -999
    # Game platform/release markers
    if re.search(r'\b(ps4|ps5|xbox|nintendo switch|pc game|repack|fitgirl|skidrow|'
                 r'codex|plaza|cpy|empress)\b', t) or re.search(r'\biso\b', t):
        return -999
    # Audio releases
    if re.search(r'\b(flac|mp3|aac|320kbps|lossless|discography)\b', t):
        return -999

    # ── Built-in quality preferences for manga ────────────────────────────
    if is_official_release(title):
        score += 15
    elif is_quality_fan_release(title):
        score += 10
    # ── Omnibus / multi-volume pack preference ────────────────────────────────
    _is_omnibus = (extract_volume_range(title) is not None or is_complete_pack(title) or
                   any(w in t for w in ('omnibus', '3-in-1', '2-in-1', 'box set',
                                        'collected edition', 'deluxe edition')))
    _is_complete = is_complete_pack(title)
    _omnibus_pref = 'prefer_individual'
    if series_id is not None:
        try:
            with get_db() as _opdb:
                _op_row = _opdb.execute(
                    "SELECT omnibus_preference FROM series WHERE id=?", (series_id,)
                ).fetchone()
                if _op_row and _op_row['omnibus_preference']:
                    _omnibus_pref = _op_row['omnibus_preference']
        except Exception as e:
            print(f"[score_release] omnibus preference lookup failed: {e}")

    if _omnibus_pref == 'only_individual':
        # Reject any omnibus/multi-volume release
        if _is_omnibus:
            return -999
    elif _omnibus_pref == 'only_omnibus':
        # Reject single-volume releases (prefer packs only)
        if not _is_omnibus:
            return -999
        score += 25  # strongly prefer
        if _is_complete:
            score += 15
    elif _omnibus_pref == 'prefer_omnibus':
        if _is_omnibus:
            score += 20
            if _is_complete:
                score += 10
        else:
            score -= 10  # penalise singles
    else:  # prefer_individual (default)
        if _is_omnibus:
            score += 8
        if _is_complete:
            score += 12

    # ── Volume number match bonus (Kapowarr-inspired) ─────────────────────────
    # If we know which volume we're searching for, reward releases that match exactly.
    # This helps when multiple volumes appear in the same RSS feed entry.
    if volume_num is not None:
        _rel_vol = extract_volume_num(title)
        if _rel_vol is not None:
            if abs(_rel_vol - volume_num) < 0.01:
                score += 3   # exact volume match
            elif abs(_rel_vol - volume_num) <= 1.0:
                score += 1   # adjacent volume (off-by-one tolerance)
        _rel_rng = extract_volume_range(title)
        if _rel_rng is not None:
            rng_width = _rel_rng[1] - _rel_rng[0] + 1
            if _rel_rng[0] <= volume_num <= _rel_rng[1]:
                # Range covers the desired volume; smaller range = better match
                score += max(0, 3 - int(rng_width / 5))

    # ── Year match bonus ──────────────────────────────────────────────────────
    # Releases that include the publication year matching the series are more
    # likely to be correct scans of that edition.
    if pub_year and pub_year > 1900:
        _year_m = re.search(r'\b(20\d{2}|19\d{2})\b', title)
        if _year_m:
            _rel_year = int(_year_m.group(1))
            if _rel_year == pub_year:
                score += 1   # exact year match
            elif abs(_rel_year - pub_year) <= 1:
                pass         # close year — neutral (neither bonus nor penalty)

    # ── Custom Format scoring ─────────────────────────────────────────────────
    try:
        from routers.custom_formats import score_custom_formats
        with get_db() as _cfdb:
            cf_score = score_custom_formats(_cfdb, series_id, title,
                                            release_group=release_group,
                                            indexer=indexer, language=language)
        score += cf_score
    except Exception as e:
        print(f"[score_release] custom format scoring failed: {e}")

    return score


def evaluate_release(item: dict, series_id: int, db) -> dict:
    """Run all scoring and filtering checks on a single release item and
    return a structured evaluation result suitable for display in the
    interactive search UI.

    Returns:
        {
            "score": int,
            "status": "would_grab" | "low_score" | "rejected",
            "rejections": ["..."],
            "custom_format_matches": [{"name": "...", "score": N}],
            "quality": "cbz" | "epub" | ...,
            "size_mb": float,
        }
    """
    title      = item.get('title', '')
    size_bytes = item.get('size_bytes') or item.get('size') or 0
    size_mb    = round(size_bytes / (1024 * 1024), 1) if size_bytes else 0.0
    quality    = detect_quality_from_title(title)
    rejections: list[str] = []

    # ── Language rejection ────────────────────────────────────────────────────
    if is_foreign_language(title):
        rejections.append("Release appears to be a foreign-language scan")
        return {
            "score": -999,
            "status": "rejected",
            "rejections": rejections,
            "custom_format_matches": [],
            "quality": quality,
            "size_mb": size_mb,
        }

    # ── Blocked release groups (global setting) ───────────────────────────────
    t_lower = title.lower()
    blocked_groups = [g.strip().lower() for g in get_cfg('blocked_groups', '').split(',') if g.strip()]
    for grp in blocked_groups:
        if grp in t_lower:
            rejections.append(f"Release group '{grp}' is blocked")

    # ── Release profiles ──────────────────────────────────────────────────────
    try:
        series_tags = [r['tag'] for r in db.execute(
            "SELECT tag FROM series_tags WHERE series_id=?", (series_id,)
        ).fetchall()]
        from routers.release_profiles import get_applicable_profiles
        profiles = get_applicable_profiles(db, series_tags)
        for p in profiles:
            if p['required'] and not any(_term_match(t, t_lower) for t in p['required']):
                required_terms = [_term_display(t) for t in p['required']]
                rejections.append(f"Required terms not found: {', '.join(required_terms)}")
            for ig in p['ignored']:
                if _term_match(ig, t_lower):
                    rejections.append(f"Ignored word found: '{_term_display(ig)}'")
        # Global required/ignored words (only when no profiles apply)
        if not profiles:
            ignored_words = [w.strip().lower() for w in get_cfg('ignored_words', '').split(',') if w.strip()]
            for word in ignored_words:
                if word in t_lower:
                    rejections.append(f"Ignored word found: '{word}'")
            required_words = [w.strip().lower() for w in get_cfg('required_words', '').split(',') if w.strip()]
            if required_words and not any(w in t_lower for w in required_words):
                rejections.append(f"Required words not found: {', '.join(required_words)}")
    except Exception:
        pass

    # ── Quality size bounds ───────────────────────────────────────────────────
    try:
        qdef_row = db.execute(
            "SELECT * FROM quality_definitions WHERE quality=?", (quality,)
        ).fetchone()
        if qdef_row and size_mb:
            min_size = qdef_row['min_size'] or 0
            max_size = qdef_row['max_size'] or 0
            if min_size > 0 and size_mb < min_size:
                rejections.append(
                    f"Size {size_mb:.1f} MB is below {quality.upper()} minimum ({min_size} MB)"
                )
            if max_size > 0 and size_mb > max_size:
                rejections.append(
                    f"Size {size_mb:.1f} MB exceeds {quality.upper()} maximum ({max_size} MB)"
                )
    except Exception:
        pass

    # ── Custom format matches ─────────────────────────────────────────────────
    cf_matches: list[dict] = []
    try:
        from routers.custom_formats import evaluate_custom_format
        profile_row = db.execute(
            "SELECT qp.id, qp.minimum_custom_format_score FROM quality_profiles qp"
            " JOIN series s ON s.quality_profile_id=qp.id WHERE s.id=?",
            (series_id,)
        ).fetchone()
        if not profile_row:
            profile_row = db.execute(
                "SELECT id, minimum_custom_format_score FROM quality_profiles WHERE is_default=1 LIMIT 1"
            ).fetchone()
        profile_id   = profile_row['id'] if profile_row else None
        min_cf_score = int(profile_row['minimum_custom_format_score'] or 0) if profile_row else 0

        if profile_id:
            format_rows = db.execute(
                "SELECT cf.name, cf.specifications, qpcf.score"
                " FROM quality_profile_custom_formats qpcf"
                " JOIN custom_formats cf ON cf.id=qpcf.format_id"
                " WHERE qpcf.profile_id=?",
                (profile_id,)
            ).fetchall()
            total_cf = 0
            for row in format_rows:
                specs = _cfj(row['specifications'], [])
                if evaluate_custom_format(specs, title, size_bytes, 0):
                    cf_matches.append({"name": row['name'], "score": row['score']})
                    total_cf += row['score']
            if min_cf_score > 0 and total_cf < min_cf_score:
                rejections.append(
                    f"Custom format score {total_cf} is below profile minimum ({min_cf_score})"
                )
    except Exception:
        pass

    # ── Compute final score via score_release ─────────────────────────────────
    sc = score_release(title, series_id)

    if rejections:
        status = "rejected"
    elif sc < 0:
        status = "low_score"
    else:
        status = "would_grab"

    return {
        "score": sc,
        "status": status,
        "rejections": rejections,
        "custom_format_matches": cf_matches,
        "quality": quality,
        "size_mb": size_mb,
    }


def _term_display(term) -> str:
    """Return human-readable display for a profile term (string or dict)."""
    if isinstance(term, dict):
        return term.get('term', '')
    return str(term)


def _term_match(term, title_lower: str) -> bool:
    """Match a profile term (string or dict with is_regex) against a lowercased title."""
    if isinstance(term, dict):
        t = (term.get('term') or '').lower()
        if term.get('is_regex'):
            try:
                return bool(re.search(t, title_lower, re.IGNORECASE))
            except re.error:
                pass  # fall through to substring
        return t in title_lower
    return str(term).lower() in title_lower


def parse_size_bytes(size_str: str) -> int:
    if not size_str:
        return 0
    m = re.match(r'([\d.]+)\s*(K|M|G|T)?i?B', size_str, re.IGNORECASE)
    if not m:
        return 0
    val  = float(m.group(1))
    unit = (m.group(2) or '').upper()
    return int(val * {'K': 1024, 'M': 1024**2, 'G': 1024**3, 'T': 1024**4}.get(unit, 1))
