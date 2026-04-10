"""Release Profiles — named profiles for release filtering (Sonarr parity)."""
import json
import re
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from routers._templates import templates

from shared import get_db, from_json

router = APIRouter()


def _all_profiles(db):
    profiles = db.execute("SELECT * FROM release_profiles ORDER BY id").fetchall()
    result = []
    for p in profiles:
        tags = db.execute(
            "SELECT tag FROM release_profile_tags WHERE profile_id=?", (p['id'],)
        ).fetchall()
        result.append({**dict(p), 'tags': [t['tag'] for t in tags]})
    return result


# ── List ──────────────────────────────────────────────────────────────────────
@router.get("/release-profiles", response_class=HTMLResponse)
async def release_profiles_page(request: Request):
    with get_db() as db:
        profiles = _all_profiles(db)
        all_tags = [r['tag'] for r in db.execute("SELECT DISTINCT tag FROM series_tags ORDER BY tag").fetchall()]
    return templates.TemplateResponse(request, "release_profiles.html", {
        "profiles": profiles,
        "all_tags": all_tags,
    })


# ── Create ────────────────────────────────────────────────────────────────────
@router.post("/release-profiles")
async def create_release_profile(
    name: str = Form(...),
    enabled: int = Form(1),
    required: str = Form(""),
    ignored: str = Form(""),
    preferred: str = Form("[]"),
    tags: str = Form(""),
    include_preferred_when_renaming: int = Form(0),
):
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO release_profiles(name,enabled,required,ignored,preferred,include_preferred_when_renaming)"
            " VALUES(?,?,?,?,?,?)",
            (name.strip(), enabled, required.strip(), ignored.strip(),
             preferred, include_preferred_when_renaming)
        )
        profile_id = cur.lastrowid
        for tag in [t.strip() for t in tags.split(',') if t.strip()]:
            db.execute("INSERT OR IGNORE INTO release_profile_tags(profile_id,tag) VALUES(?,?)",
                       (profile_id, tag))
    return RedirectResponse("/release-profiles", status_code=303)


# ── Edit (POST) ───────────────────────────────────────────────────────────────
@router.post("/release-profiles/{profile_id}")
async def edit_release_profile(
    profile_id: int,
    name: str = Form(...),
    enabled: int = Form(1),
    required: str = Form(""),
    ignored: str = Form(""),
    preferred: str = Form("[]"),
    tags: str = Form(""),
    include_preferred_when_renaming: int = Form(0),
):
    with get_db() as db:
        db.execute(
            "UPDATE release_profiles SET name=?,enabled=?,required=?,ignored=?,preferred=?,"
            " include_preferred_when_renaming=? WHERE id=?",
            (name.strip(), enabled, required.strip(), ignored.strip(),
             preferred, include_preferred_when_renaming, profile_id)
        )
        db.execute("DELETE FROM release_profile_tags WHERE profile_id=?", (profile_id,))
        for tag in [t.strip() for t in tags.split(',') if t.strip()]:
            db.execute("INSERT OR IGNORE INTO release_profile_tags(profile_id,tag) VALUES(?,?)",
                       (profile_id, tag))
    return RedirectResponse("/release-profiles", status_code=303)


# ── Delete ────────────────────────────────────────────────────────────────────
@router.post("/release-profiles/{profile_id}/delete")
async def delete_release_profile(profile_id: int):
    with get_db() as db:
        db.execute("DELETE FROM release_profiles WHERE id=?", (profile_id,))
    return RedirectResponse("/release-profiles", status_code=303)


# ── Term parsing helpers ──────────────────────────────────────────────────────
def _parse_term_list(raw: str) -> list:
    """
    Parse a comma-separated term string into a list of term objects.
    Each element is either a plain string or a dict {"term":..., "is_regex":bool}.
    Supports inline JSON dicts: {"term":"...","is_regex":true} mixed with plain words.
    """
    if not raw:
        return []
    terms = []
    for part in raw.split(','):
        part = part.strip()
        if not part:
            continue
        if part.startswith('{'):
            try:
                obj = json.loads(part)
                if isinstance(obj, dict) and obj.get('term'):
                    terms.append(obj)
                    continue
            except json.JSONDecodeError:
                pass
        terms.append(part.lower())
    return terms


def _profile_term_match(term, title_lower: str) -> bool:
    """Match a profile term (string or dict with is_regex) against a lowercased title."""
    if isinstance(term, dict):
        t = (term.get('term') or '').lower()
        if term.get('is_regex'):
            try:
                return bool(re.search(t, title_lower, re.IGNORECASE))
            except re.error:
                import warnings
                warnings.warn(f"[ReleaseProfile] Invalid regex '{t}', falling back to substring match")
        return t in title_lower
    return str(term).lower() in title_lower


def _profile_pref_match(pref: dict, title_lower: str) -> bool:
    """Match a preferred term dict against a lowercased title, respecting is_regex."""
    term = (pref.get('term') or '').lower()
    if not term:
        return False
    if pref.get('is_regex'):
        try:
            return bool(re.search(term, title_lower, re.IGNORECASE))
        except re.error:
            import warnings
            warnings.warn(f"[ReleaseProfile] Invalid regex in preferred term '{term}', falling back to substring")
    return term in title_lower


# ── Scoring helper (used by main.py score_release) ───────────────────────────
def get_applicable_profiles(db, series_tags: list[str]) -> list[dict]:
    """
    Return all enabled release profiles that apply to the given series tags.
    A profile with no tags applies to all series.
    A profile with tags only applies if the series has at least one matching tag.

    Terms in 'required' and 'ignored' are parsed via _parse_term_list(), which
    supports both plain strings and dict objects with is_regex=true.
    """
    profiles = db.execute(
        "SELECT * FROM release_profiles WHERE enabled=1"
    ).fetchall()
    result = []
    for p in profiles:
        profile_tags = {r['tag'] for r in db.execute(
            "SELECT tag FROM release_profile_tags WHERE profile_id=?", (p['id'],)
        ).fetchall()}
        if not profile_tags or profile_tags & set(series_tags):
            preferred = from_json(p['preferred'], [])
            result.append({
                'id':        p['id'],
                'required':  _parse_term_list(p['required'] or ''),
                'ignored':   _parse_term_list(p['ignored']  or ''),
                'preferred': preferred,  # list of {term, score[, is_regex]}
            })
    return result


def score_from_release_profiles(title: str, series_tags: list[str], db) -> int | None:
    """
    Apply all matching release profiles to a release title.
    Returns:
      -1000 if any required term is missing or any ignored term matches
      cumulative preferred score otherwise
    Returning None means no profiles matched (caller uses global settings).

    Terms support both plain strings and dict objects {"term":"...","is_regex":true}.
    """
    profiles = get_applicable_profiles(db, series_tags)
    if not profiles:
        return None

    title_lower = title.lower()
    total_score = 0
    for p in profiles:
        # Must contain at least one required term (if any)
        if p['required'] and not any(_profile_term_match(t, title_lower) for t in p['required']):
            return -1000
        # Must not contain any ignored term
        if any(_profile_term_match(t, title_lower) for t in p['ignored']):
            return -1000
        # Add preferred term scores
        for pref in p['preferred']:
            score = int(pref.get('score', 0))
            if _profile_pref_match(pref, title_lower):
                total_score += score
    return total_score
