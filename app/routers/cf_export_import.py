"""JSON export / import for Custom Formats + Quality Profiles (PR #128).

A single bundle shape covers both — the typical "share my setup" workflow
is one file containing everything. Profile score rows reference CFs by
NAME (not id) so:

  1. The bundle survives moving between Mangarr instances where CF ids
     don't line up.
  2. Users can hand-pick a subset of CFs to import without breaking
     score references — the importer skips score entries whose CF name
     isn't present in the destination DB.

Import semantics: UPSERT by name. CFs and profiles with the same name
have their config replaced. Existing CFs/profiles not mentioned in the
bundle are left alone — this is additive ("merge in these presets")
rather than destructive ("replace my entire setup").
"""
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from routers._templates import templates

from shared import get_db, from_json

router = APIRouter()

BUNDLE_VERSION = 1


# ── Build bundle dict from current DB state ──────────────────────────────────
def _build_bundle() -> dict:
    with get_db() as db:
        cfs = [dict(r) for r in db.execute(
            "SELECT id, name, specifications, include_custom_format_when_renaming"
            " FROM custom_formats ORDER BY name"
        ).fetchall()]
        profiles = [dict(r) for r in db.execute(
            "SELECT id, name, qualities, cutoff, upgrades_allowed,"
            " minimum_custom_format_score, cutoff_format_score,"
            " min_upgrade_format_score, is_default"
            " FROM quality_profiles ORDER BY name"
        ).fetchall()]
        # Fetch all scores keyed by (profile_id, cf_id) — translate to names
        score_rows = db.execute(
            "SELECT qpcf.profile_id, qpcf.format_id, qpcf.score, cf.name AS cf_name"
            " FROM quality_profile_custom_formats qpcf"
            " JOIN custom_formats cf ON cf.id = qpcf.format_id"
        ).fetchall()
    scores_by_profile: dict[int, dict[str, int]] = {}
    for r in score_rows:
        scores_by_profile.setdefault(r['profile_id'], {})[r['cf_name']] = r['score']

    return {
        'version': BUNDLE_VERSION,
        'exported_at': datetime.now(timezone.utc).isoformat(),
        'custom_formats': [
            {
                'name': cf['name'],
                'include_custom_format_when_renaming': bool(
                    cf['include_custom_format_when_renaming']
                ),
                'specifications': from_json(cf['specifications'], []),
            }
            for cf in cfs
        ],
        'quality_profiles': [
            {
                'name': p['name'],
                'qualities': from_json(p['qualities'], []),
                'cutoff': p['cutoff'],
                'upgrades_allowed': bool(p['upgrades_allowed']),
                'minimum_custom_format_score': p['minimum_custom_format_score'] or 0,
                'cutoff_format_score': p['cutoff_format_score'] or 10000,
                'min_upgrade_format_score': p['min_upgrade_format_score'] or 10,
                'is_default': bool(p['is_default']),
                'scores': scores_by_profile.get(p['id'], {}),
            }
            for p in profiles
        ],
    }


# ── Apply a bundle dict to the DB (UPSERT by name) ───────────────────────────
def _apply_bundle(bundle: dict) -> dict:
    """Returns a counts summary: {cfs_added, cfs_updated, profiles_added,
    profiles_updated, scores_skipped_missing_cf}."""
    counts = {
        'cfs_added': 0, 'cfs_updated': 0,
        'profiles_added': 0, 'profiles_updated': 0,
        'scores_skipped_missing_cf': 0,
    }
    with get_db() as db:
        # ── Custom formats ────────────────────────────────────────────────
        for cf in bundle.get('custom_formats', []) or []:
            name = (cf.get('name') or '').strip()
            if not name:
                continue
            specs_json = json.dumps(cf.get('specifications') or [])
            include_rename = 1 if cf.get('include_custom_format_when_renaming') else 0
            existing = db.execute(
                "SELECT id FROM custom_formats WHERE name=?", (name,)
            ).fetchone()
            if existing:
                db.execute(
                    "UPDATE custom_formats SET specifications=?,"
                    " include_custom_format_when_renaming=? WHERE id=?",
                    (specs_json, include_rename, existing['id'])
                )
                counts['cfs_updated'] += 1
            else:
                db.execute(
                    "INSERT INTO custom_formats(name, specifications,"
                    " include_custom_format_when_renaming) VALUES(?, ?, ?)",
                    (name, specs_json, include_rename)
                )
                counts['cfs_added'] += 1

        # Build name→id lookup AFTER CFs are inserted so newly-added ones
        # get their score wiring on the same import.
        cf_id_by_name = {
            r['name']: r['id'] for r in db.execute(
                "SELECT id, name FROM custom_formats"
            ).fetchall()
        }

        # ── Quality profiles ──────────────────────────────────────────────
        for p in bundle.get('quality_profiles', []) or []:
            name = (p.get('name') or '').strip()
            if not name:
                continue
            qualities_json = json.dumps(p.get('qualities') or [])
            cutoff = p.get('cutoff') or 'cbz'
            upgrades = 1 if p.get('upgrades_allowed', True) else 0
            min_cf = int(p.get('minimum_custom_format_score') or 0)
            cutoff_cf = int(p.get('cutoff_format_score') or 10000)
            min_upgrade_cf = int(p.get('min_upgrade_format_score') or 10)
            is_default = 1 if p.get('is_default') else 0

            existing = db.execute(
                "SELECT id FROM quality_profiles WHERE name=?", (name,)
            ).fetchone()
            if existing:
                pid = existing['id']
                db.execute(
                    "UPDATE quality_profiles SET qualities=?, cutoff=?,"
                    " upgrades_allowed=?, minimum_custom_format_score=?,"
                    " cutoff_format_score=?, min_upgrade_format_score=?"
                    " WHERE id=?",
                    (qualities_json, cutoff, upgrades, min_cf,
                     cutoff_cf, min_upgrade_cf, pid)
                )
                # is_default only updated if explicitly true (don't demote
                # someone's existing default by re-importing a non-default
                # preset of the same name)
                if is_default:
                    db.execute(
                        "UPDATE quality_profiles SET is_default=0 WHERE id != ?",
                        (pid,)
                    )
                    db.execute(
                        "UPDATE quality_profiles SET is_default=1 WHERE id=?",
                        (pid,)
                    )
                counts['profiles_updated'] += 1
            else:
                cur = db.execute(
                    "INSERT INTO quality_profiles(name, qualities, cutoff,"
                    " upgrades_allowed, minimum_custom_format_score,"
                    " cutoff_format_score, min_upgrade_format_score, is_default)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (name, qualities_json, cutoff, upgrades, min_cf,
                     cutoff_cf, min_upgrade_cf, is_default)
                )
                pid = cur.lastrowid
                if is_default:
                    db.execute(
                        "UPDATE quality_profiles SET is_default=0 WHERE id != ?",
                        (pid,)
                    )
                counts['profiles_added'] += 1

            # Replace score wiring: clear existing for this profile, then
            # insert from the bundle. Skips score entries whose CF name
            # isn't in the destination DB (counts those as
            # scores_skipped_missing_cf).
            db.execute(
                "DELETE FROM quality_profile_custom_formats WHERE profile_id=?",
                (pid,)
            )
            for cf_name, score in (p.get('scores') or {}).items():
                try:
                    score_int = int(score)
                except (ValueError, TypeError):
                    continue
                if score_int == 0:
                    continue
                cf_id = cf_id_by_name.get(cf_name)
                if cf_id is None:
                    counts['scores_skipped_missing_cf'] += 1
                    continue
                db.execute(
                    "INSERT INTO quality_profile_custom_formats"
                    "(profile_id, format_id, score) VALUES(?, ?, ?)",
                    (pid, cf_id, score_int)
                )
    return counts


# ── Routes ────────────────────────────────────────────────────────────────────
@router.get("/custom-formats/export.json")
async def export_bundle():
    """Download the full CF + profile bundle as a JSON file. Filename
    is timestamped so users can keep multiple exports without clobbering."""
    bundle = _build_bundle()
    body = json.dumps(bundle, indent=2)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    return Response(
        content=body,
        media_type='application/json',
        headers={
            'Content-Disposition': f'attachment; filename="mangarr-cf-bundle-{stamp}.json"'
        }
    )


@router.get("/custom-formats/import", response_class=HTMLResponse)
async def import_bundle_page(request: Request):
    """Render the import page — paste JSON or upload a file."""
    return templates.TemplateResponse(request, "cf_import.html", {})


@router.post("/custom-formats/import")
async def import_bundle(request: Request, json_payload: str = Form(default="")):
    """Apply a bundle from a pasted JSON string. Returns to the import
    page with a success summary or an inline error for malformed JSON."""
    raw = (json_payload or '').strip()
    if not raw:
        return templates.TemplateResponse(
            request, "cf_import.html",
            {'error': 'No JSON provided.'},
            status_code=400,
        )
    try:
        bundle = json.loads(raw)
    except json.JSONDecodeError as e:
        return templates.TemplateResponse(
            request, "cf_import.html",
            {'error': f'Invalid JSON: {e}'},
            status_code=400,
        )
    if not isinstance(bundle, dict):
        return templates.TemplateResponse(
            request, "cf_import.html",
            {'error': 'JSON root must be an object with custom_formats / quality_profiles keys.'},
            status_code=400,
        )
    counts = _apply_bundle(bundle)
    summary = (
        f"Imported: {counts['cfs_added']} CFs added, "
        f"{counts['cfs_updated']} CFs updated, "
        f"{counts['profiles_added']} profiles added, "
        f"{counts['profiles_updated']} profiles updated"
    )
    if counts['scores_skipped_missing_cf']:
        summary += (
            f". Skipped {counts['scores_skipped_missing_cf']} score entries "
            "referencing CFs not in the bundle."
        )
    return templates.TemplateResponse(
        request, "cf_import.html",
        {'success': summary, 'counts': counts},
    )
