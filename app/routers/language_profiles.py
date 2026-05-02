"""Language Profiles — filter releases by detected language."""
import json
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from routers._templates import templates

from shared import get_db

router = APIRouter()

# ── Language detection ────────────────────────────────────────────────────────

SUPPORTED_LANGUAGES = {
    'en':   'English',
    'ja':   'Japanese Raw',
    'ko':   'Korean',
    'zh-s': 'Chinese Simplified',
    'zh-t': 'Chinese Traditional',
    'fr':   'French',
    'es':   'Spanish',
    'de':   'German',
    'any':  'Any Language',
}

LANGUAGE_KEYWORDS = {
    'ja':   ['raw', 'japanese', 'jp'],
    'ko':   ['korean', 'kr'],
    'zh-s': ['chinese', 'simplified', 'mandarin'],
    'zh-t': ['traditional'],
    'fr':   ['french', 'fr'],
    'es':   ['spanish', 'es'],
    'de':   ['german', 'de'],
    'en':   ['english', 'en'],
}


def detect_language(title: str) -> str:
    """Detect language from release title keywords. Returns language code or 'en' (default)."""
    title_lower = title.lower()
    for lang, keywords in LANGUAGE_KEYWORDS.items():
        if any(kw in title_lower for kw in keywords):
            return lang
    return 'en'  # default to English if no language markers found


def check_language_profile(db, profile_id: int | None, release_title: str) -> tuple[bool, str]:
    """Check if a release title passes the language profile. Returns (allowed, reason)."""
    if not profile_id:
        return True, ''
    profile = db.execute("SELECT * FROM language_profiles WHERE id=?", (profile_id,)).fetchone()
    if not profile:
        return True, ''
    if profile['allow_any']:
        return True, ''
    try:
        allowed_langs = json.loads(profile['languages'])
    except Exception:
        return True, ''
    if 'any' in allowed_langs:
        return True, ''
    detected = detect_language(release_title)
    if detected in allowed_langs:
        return True, ''
    return False, f"Language '{detected}' not in profile '{profile['name']}' ({', '.join(allowed_langs)})"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _all_profiles(db):
    return db.execute("SELECT * FROM language_profiles ORDER BY id").fetchall()


def _get_default_id(db) -> int | None:
    row = db.execute("SELECT value FROM settings WHERE key='default_language_profile_id'").fetchone()
    if row:
        try:
            return int(row['value'])
        except (TypeError, ValueError):
            return None
    return None


def _parse_languages(languages_str: str) -> str:
    """Parse comma-separated language codes into a JSON list, validating codes."""
    codes = [c.strip().lower() for c in languages_str.split(',') if c.strip()]
    valid = [c for c in codes if c in SUPPORTED_LANGUAGES]
    return json.dumps(valid if valid else ['any'])


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("/language-profiles", response_class=HTMLResponse)
async def language_profiles_page(request: Request):
    with get_db() as db:
        profiles    = _all_profiles(db)
        default_id  = _get_default_id(db)
    return templates.TemplateResponse(request, "language_profiles.html", {
        "profiles":           profiles,
        "default_id":         default_id,
        "supported_languages": SUPPORTED_LANGUAGES,
    })


# ── Create ────────────────────────────────────────────────────────────────────

@router.post("/language-profiles")
async def create_language_profile(
    name:      str = Form(...),
    languages: str = Form('any'),
    allow_any: int = Form(0),
):
    langs_json = _parse_languages(languages)
    with get_db() as db:
        db.execute(
            "INSERT INTO language_profiles(name, languages, allow_any) VALUES(?,?,?)",
            (name.strip(), langs_json, allow_any)
        )
    return RedirectResponse("/language-profiles", status_code=303)


# ── Update ────────────────────────────────────────────────────────────────────

@router.post("/language-profiles/{profile_id}")
async def update_language_profile(request: Request, profile_id: int):
    """Edit a language profile. Partial-POST safe."""
    from routers._form_helpers import submitted_subset, bool_int
    submitted = await request.form()

    plain_fields = {
        'name':      ('name',      lambda v: str(v or '').strip()),
        'languages': ('languages', lambda v: _parse_languages(str(v or ''))),
        'allow_any': ('allow_any', bool_int),
    }

    with get_db() as db:
        updates, params = submitted_subset(submitted, plain_fields)
        if updates:
            params.append(profile_id)
            db.execute(
                f"UPDATE language_profiles SET {', '.join(updates)} WHERE id=?",
                params
            )
    return RedirectResponse("/language-profiles", status_code=303)


# ── Delete ────────────────────────────────────────────────────────────────────

@router.post("/language-profiles/{profile_id}/delete")
async def delete_language_profile(profile_id: int):
    with get_db() as db:
        # Guard: refuse if any series references this profile
        ref = db.execute(
            "SELECT id FROM series WHERE language_profile_id=? LIMIT 1", (profile_id,)
        ).fetchone()
        if ref:
            # Redirect back with an error indicator rather than silently failing
            return RedirectResponse(
                f"/language-profiles?error=in-use&profile={profile_id}", status_code=303
            )
        # Clear from settings default if it was the default
        default_row = db.execute(
            "SELECT value FROM settings WHERE key='default_language_profile_id'"
        ).fetchone()
        if default_row:
            try:
                if int(default_row['value']) == profile_id:
                    db.execute("DELETE FROM settings WHERE key='default_language_profile_id'")
            except (TypeError, ValueError):
                pass
        db.execute("DELETE FROM language_profiles WHERE id=?", (profile_id,))
    return RedirectResponse("/language-profiles", status_code=303)


# ── Set default ───────────────────────────────────────────────────────────────

@router.post("/language-profiles/{profile_id}/set-default")
async def set_default_language_profile(profile_id: int):
    with get_db() as db:
        db.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES('default_language_profile_id', ?)",
            (str(profile_id),)
        )
    return RedirectResponse("/language-profiles", status_code=303)
