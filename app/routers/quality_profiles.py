"""Quality Profiles — named profiles with quality tiers and cutoffs (Sonarr parity)."""
import json
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from routers._templates import templates

from shared import get_cfg, get_db, from_json

router = APIRouter()

QUALITY_ORDER = ["cbz", "epub", "cbr", "pdf", "raw"]


def _all_profiles(db):
    return db.execute("SELECT * FROM quality_profiles ORDER BY id").fetchall()


def _profile_custom_formats(db, profile_id: int):
    return db.execute(
        """SELECT cf.id, cf.name, qpcf.score
           FROM quality_profile_custom_formats qpcf
           JOIN custom_formats cf ON cf.id = qpcf.format_id
           WHERE qpcf.profile_id = ?
           ORDER BY cf.name""",
        (profile_id,)
    ).fetchall()


# ── List ──────────────────────────────────────────────────────────────────────
@router.get("/quality-profiles", response_class=HTMLResponse)
async def quality_profiles_page(request: Request):
    with get_db() as db:
        profiles = _all_profiles(db)
        all_formats = db.execute("SELECT id, name FROM custom_formats ORDER BY name").fetchall()
    return templates.TemplateResponse(request, "quality_profiles.html", {
        "profiles":    profiles,
        "all_formats": all_formats,
        "quality_order": QUALITY_ORDER,
    })


# ── Create ────────────────────────────────────────────────────────────────────
@router.post("/quality-profiles")
async def create_quality_profile(
    name: str = Form(...),
    qualities: str = Form('["cbz","epub","cbr","pdf"]'),
    cutoff: str = Form(""),
    upgrades_allowed: int = Form(1),
    minimum_custom_format_score: int = Form(0),
    cutoff_format_score: int = Form(10000),
    min_upgrade_format_score: int = Form(10),
):
    with get_db() as db:
        db.execute(
            "INSERT INTO quality_profiles"
            "(name, qualities, cutoff, upgrades_allowed,"
            " minimum_custom_format_score, cutoff_format_score, min_upgrade_format_score)"
            " VALUES(?,?,?,?,?,?,?)",
            (name.strip(), qualities, cutoff or None, upgrades_allowed,
             minimum_custom_format_score, cutoff_format_score, min_upgrade_format_score)
        )
    return RedirectResponse("/quality-profiles", status_code=303)


# ── Edit (GET) ────────────────────────────────────────────────────────────────
@router.get("/quality-profiles/{profile_id}", response_class=HTMLResponse)
async def edit_quality_profile_page(request: Request, profile_id: int):
    with get_db() as db:
        profile = db.execute("SELECT * FROM quality_profiles WHERE id=?", (profile_id,)).fetchone()
        if not profile:
            return RedirectResponse("/quality-profiles", status_code=303)
        all_formats = db.execute("SELECT id, name FROM custom_formats ORDER BY name").fetchall()
        linked_formats = _profile_custom_formats(db, profile_id)
    return templates.TemplateResponse(request, "quality_profile_edit.html", {
        "profile":       profile,
        "all_formats":   all_formats,
        "linked_formats": linked_formats,
        "quality_order": QUALITY_ORDER,
    })


# ── Edit (POST) ───────────────────────────────────────────────────────────────
@router.post("/quality-profiles/{profile_id}")
async def edit_quality_profile(request: Request, profile_id: int):
    """Edit a quality profile. Partial-POST safe: only columns whose
    form key is present in the request body are written."""
    from routers._form_helpers import (
        submitted_subset, str_or_none, int_default_zero, bool_int,
    )
    submitted = await request.form()

    plain_fields = {
        'name':                        ('name',                        lambda v: str(v or '').strip()),
        'qualities':                   ('qualities',                   lambda v: str(v or '')),
        'cutoff':                      ('cutoff',                      str_or_none),
        'upgrades_allowed':            ('upgrades_allowed',            bool_int),
        'minimum_custom_format_score': ('minimum_custom_format_score', int_default_zero),
        'cutoff_format_score':         ('cutoff_format_score',         int_default_zero),
        'min_upgrade_format_score':    ('min_upgrade_format_score',    int_default_zero),
    }

    with get_db() as db:
        updates, params = submitted_subset(submitted, plain_fields)
        if updates:
            params.append(profile_id)
            db.execute(
                f"UPDATE quality_profiles SET {', '.join(updates)} WHERE id=?",
                params
            )
    return RedirectResponse("/quality-profiles", status_code=303)


# ── Custom format score for a profile ────────────────────────────────────────
@router.post("/quality-profiles/{profile_id}/format-scores")
async def set_format_scores(request: Request, profile_id: int):
    """Set custom format scores for a quality profile. Body: {format_id: score, ...}"""
    body = await request.json()
    with get_db() as db:
        db.execute("DELETE FROM quality_profile_custom_formats WHERE profile_id=?", (profile_id,))
        for fid, score in body.items():
            try:
                db.execute(
                    "INSERT INTO quality_profile_custom_formats(profile_id, format_id, score) VALUES(?,?,?)",
                    (profile_id, int(fid), int(score))
                )
            except Exception:
                pass
    return JSONResponse({"ok": True})


# ── Delete ────────────────────────────────────────────────────────────────────
@router.post("/quality-profiles/{profile_id}/delete")
async def delete_quality_profile(profile_id: int):
    with get_db() as db:
        # Don't delete if series are using it; reset them to NULL
        db.execute("UPDATE series SET quality_profile_id=NULL WHERE quality_profile_id=?", (profile_id,))
        db.execute("DELETE FROM quality_profiles WHERE id=?", (profile_id,))
    return RedirectResponse("/quality-profiles", status_code=303)


# ── Set default ───────────────────────────────────────────────────────────────
@router.post("/quality-profiles/{profile_id}/set-default")
async def set_default_quality_profile(profile_id: int):
    with get_db() as db:
        db.execute("UPDATE quality_profiles SET is_default=0")
        db.execute("UPDATE quality_profiles SET is_default=1 WHERE id=?", (profile_id,))
    return RedirectResponse("/quality-profiles", status_code=303)


# ── API: get profile for scoring ──────────────────────────────────────────────
def get_series_quality_profile(db, series_id: int) -> dict | None:
    """Return the effective quality profile for a series, or None if not set."""
    row = db.execute(
        "SELECT qp.* FROM quality_profiles qp"
        " JOIN series s ON s.quality_profile_id = qp.id"
        " WHERE s.id=?",
        (series_id,)
    ).fetchone()
    if not row:
        # Fall back to default profile
        row = db.execute(
            "SELECT * FROM quality_profiles WHERE is_default=1 LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def quality_meets_cutoff(profile: dict, quality: str | None) -> bool:
    """Return True if quality is at or above the profile's cutoff."""
    if not quality or not profile or not profile.get('cutoff'):
        return False
    order = from_json(profile['qualities'], QUALITY_ORDER)
    try:
        q_idx  = order.index(quality)
        co_idx = order.index(profile['cutoff'])
        return q_idx <= co_idx
    except ValueError:
        return False
