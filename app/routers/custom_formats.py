"""Custom Formats — regex-based release scoring rules (Sonarr parity)."""
import json
import re
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from routers._templates import templates

from shared import get_db, from_json
from security import safe_regex_search

router = APIRouter()

SPEC_TYPES = [
    "release_title_contains",        # regex match on release title
    "release_title_not_contains",
    "release_group_contains",        # case-insensitive substring on release group field
    "release_group_not_contains",    # inverse
    "indexer_is",                    # exact match on indexer name
    "indexer_contains",              # substring match on indexer name
    "language_is",                   # matches detected language code
    "edition_contains",              # matches edition keywords (Deluxe, Omnibus, etc.)
    "size_minimum",
    "size_maximum",
]


def _all_formats(db):
    return db.execute("SELECT * FROM custom_formats ORDER BY name").fetchall()


# ── List ──────────────────────────────────────────────────────────────────────
@router.get("/custom-formats", response_class=HTMLResponse)
async def custom_formats_page(request: Request):
    with get_db() as db:
        formats = _all_formats(db)
        # Attach scores from default quality profile if any
        profile_scores = {}
        default_profile = db.execute(
            "SELECT id FROM quality_profiles WHERE is_default=1 LIMIT 1"
        ).fetchone()
        if default_profile:
            for r in db.execute(
                "SELECT format_id, score FROM quality_profile_custom_formats WHERE profile_id=?",
                (default_profile['id'],)
            ).fetchall():
                profile_scores[r['format_id']] = r['score']
    return templates.TemplateResponse(request, "custom_formats.html", {
        "formats":       formats,
        "spec_types":    SPEC_TYPES,
        "profile_scores": profile_scores,
    })


# ── Create ────────────────────────────────────────────────────────────────────
@router.post("/custom-formats")
async def create_custom_format(
    name: str = Form(...),
    specifications: str = Form("[]"),
    include_custom_format_when_renaming: int = Form(0),
):
    # Validate JSON
    try:
        specs = json.loads(specifications)
    except Exception:
        specs = []
    with get_db() as db:
        db.execute(
            "INSERT INTO custom_formats(name,specifications,include_custom_format_when_renaming)"
            " VALUES(?,?,?)",
            (name.strip(), json.dumps(specs), include_custom_format_when_renaming)
        )
    return RedirectResponse("/custom-formats", status_code=303)


# ── Edit ─────────────────────────────────────────────────────────────────────
@router.post("/custom-formats/{format_id}")
async def edit_custom_format(
    format_id: int,
    name: str = Form(...),
    specifications: str = Form("[]"),
    include_custom_format_when_renaming: int = Form(0),
):
    try:
        specs = json.loads(specifications)
    except Exception:
        specs = []
    with get_db() as db:
        db.execute(
            "UPDATE custom_formats SET name=?,specifications=?,include_custom_format_when_renaming=?"
            " WHERE id=?",
            (name.strip(), json.dumps(specs), include_custom_format_when_renaming, format_id)
        )
    return RedirectResponse("/custom-formats", status_code=303)


# ── Delete ────────────────────────────────────────────────────────────────────
@router.post("/custom-formats/{format_id}/delete")
async def delete_custom_format(format_id: int):
    with get_db() as db:
        db.execute("DELETE FROM custom_formats WHERE id=?", (format_id,))
    return RedirectResponse("/custom-formats", status_code=303)


# ── Preview: test a format against a title ────────────────────────────────────
@router.post("/api/custom-formats/preview")
async def preview_custom_format(request: Request):
    """Body: {format_id: N, title: "..."}"""
    body = await request.json()
    fid   = int(body.get('format_id', 0))
    title = body.get('title', '')
    with get_db() as db:
        fmt = db.execute("SELECT * FROM custom_formats WHERE id=?", (fid,)).fetchone()
    if not fmt:
        return JSONResponse({"error": "Format not found"})
    specs  = from_json(fmt['specifications'], [])
    matched = evaluate_custom_format(specs, title, 0, 0)
    return JSONResponse({"matched": matched, "title": title})


# ── Scoring engine ────────────────────────────────────────────────────────────
def evaluate_custom_format(
    specifications: list,
    title: str,
    size_bytes: int,
    quality_rank: int,
    release_group: str = '',
    indexer: str = '',
    language: str = '',
) -> bool:
    """
    Returns True if ALL required specifications match (AND logic).
    Negated specs: returns True if the condition does NOT match.

    Extra keyword args:
      release_group  — parsed release group string (e.g. 'LuCaZ')
      indexer        — indexer name (e.g. 'NyaaTorrents')
      language       — detected language code (e.g. 'en')
    """
    title_lower  = title.lower()
    rgroup_lower = (release_group or '').lower()
    idx_lower    = (indexer or '').lower()
    lang_lower   = (language or '').lower()

    for spec in specifications:
        spec_type = spec.get('type', '')
        negate    = spec.get('negate', False)
        value     = spec.get('value', '')
        val_lower = (value or '').lower()

        if spec_type == 'release_title_contains':
            hit = safe_regex_search(value, title, re.IGNORECASE)
            if hit is None:
                # Unsafe / invalid regex → substring fallback so one bad
                # spec doesn't break the rest of the format.
                hit = val_lower in title_lower
            if negate:
                hit = not hit
            if not hit:
                return False

        elif spec_type == 'release_title_not_contains':
            # Shorthand: title must NOT contain value
            hit = safe_regex_search(value, title, re.IGNORECASE)
            if hit is None:
                hit = val_lower in title_lower
            if negate:
                hit = not hit
            if hit:  # must NOT contain → if hit, spec fails
                return False

        elif spec_type == 'release_group_contains':
            hit = val_lower in rgroup_lower
            if negate:
                hit = not hit
            if not hit:
                return False

        elif spec_type == 'release_group_not_contains':
            hit = val_lower in rgroup_lower
            if negate:
                hit = not hit
            if hit:  # must NOT contain
                return False

        elif spec_type == 'indexer_is':
            hit = idx_lower == val_lower
            if negate:
                hit = not hit
            if not hit:
                return False

        elif spec_type == 'indexer_contains':
            hit = val_lower in idx_lower
            if negate:
                hit = not hit
            if not hit:
                return False

        elif spec_type == 'language_is':
            hit = lang_lower == val_lower
            if negate:
                hit = not hit
            if not hit:
                return False

        elif spec_type == 'edition_contains':
            # Match edition keywords in the title (Deluxe, Omnibus, Collector, etc.)
            hit = safe_regex_search(value, title, re.IGNORECASE)
            if hit is None:
                hit = val_lower in title_lower
            if negate:
                hit = not hit
            if not hit:
                return False

        elif spec_type == 'size_minimum':
            try:
                threshold = int(value) * 1024 * 1024  # value in MB
                hit = size_bytes >= threshold
            except (ValueError, TypeError):
                hit = True
            if negate:
                hit = not hit
            if not hit:
                return False

        elif spec_type == 'size_maximum':
            try:
                threshold = int(value) * 1024 * 1024
                hit = size_bytes <= threshold
            except (ValueError, TypeError):
                hit = True
            if negate:
                hit = not hit
            if not hit:
                return False

    return True


def score_custom_formats(db, series_id: int | None, title: str,
                          size_bytes: int = 0, quality_rank: int = 0,
                          release_group: str = '', indexer: str = '',
                          language: str = '') -> int:
    """
    Return the total custom format score for a release.
    Uses the quality profile linked to the series (or the default profile).
    """
    if series_id:
        profile = db.execute(
            "SELECT qp.id FROM quality_profiles qp"
            " JOIN series s ON s.quality_profile_id=qp.id WHERE s.id=?",
            (series_id,)
        ).fetchone()
    else:
        profile = None

    if not profile:
        profile = db.execute(
            "SELECT id FROM quality_profiles WHERE is_default=1 LIMIT 1"
        ).fetchone()

    if not profile:
        return 0

    profile_id = profile['id']
    format_scores = db.execute(
        "SELECT cf.specifications, qpcf.score"
        " FROM quality_profile_custom_formats qpcf"
        " JOIN custom_formats cf ON cf.id=qpcf.format_id"
        " WHERE qpcf.profile_id=?",
        (profile_id,)
    ).fetchall()

    total = 0
    for row in format_scores:
        specs = from_json(row['specifications'], [])
        if evaluate_custom_format(specs, title, size_bytes, quality_rank,
                                   release_group=release_group,
                                   indexer=indexer, language=language):
            total += row['score']
    return total
