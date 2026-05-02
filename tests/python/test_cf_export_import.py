"""Tests for CF + Quality Profile JSON export/import (PR #128).

A typical "share my Mangarr setup" workflow is one JSON file containing
both CFs and profiles. Profile score rows reference CFs by NAME so the
bundle survives moving between instances where ids don't line up.

Behavior pinned here:
  - GET /custom-formats/export.json returns a JSON bundle with both
    custom_formats and quality_profiles, plus per-profile scores keyed
    by CF name.
  - POST /custom-formats/import upserts CFs + profiles by name.
  - Importing a bundle whose profile scores reference a CF not in the
    destination DB silently skips those score entries (counts them).
  - Round-trip: export → import on a clean DB reproduces the state.
  - Malformed JSON returns 400 with an inline error message.
  - Existing CFs/profiles not mentioned in the bundle are left alone
    (additive semantics, not destructive replace).
"""
import json
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401


@pytest.fixture
def env(tmp_path):
    """Fresh DB; the seed brings up the 11 built-in CFs + 4 presets so
    most tests start with realistic data."""
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-cfio-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()
    main.ensure_api_key()
    try:
        yield {'db_path': db.name}
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


def _client():
    import main
    from fastapi.testclient import TestClient
    return TestClient(main.app)


def _csrf(tag="t"):
    tok = f"csrf-{tag}-" + "x" * 30
    return {
        'cookies': {'csrftoken': tok},
        'headers': {'X-CSRFToken': tok},
    }


# ───────────────────── Export ─────────────────────


def test_export_returns_json_bundle_with_seeded_data(env):
    """The exported bundle must contain the 11 seeded CFs + 4 preset
    profiles, with score wiring intact."""
    r = _client().get("/custom-formats/export.json")
    assert r.status_code == 200, r.text
    assert r.headers['content-type'].startswith('application/json')
    bundle = r.json()
    assert bundle['version'] == 1
    assert 'exported_at' in bundle

    cf_names = {cf['name'] for cf in bundle['custom_formats']}
    assert 'Official Digital' in cf_names
    assert 'Quality Scanlation' in cf_names
    assert 'Tankobon' in cf_names

    profile_names = {p['name'] for p in bundle['quality_profiles']}
    assert {'Best Available', 'Official Digital Only',
            'Scanlations OK', 'Japanese Raw'} <= profile_names

    # Best Available's scores must include Official Digital → 500
    best = next(p for p in bundle['quality_profiles']
                if p['name'] == 'Best Available')
    assert best['scores'].get('Official Digital') == 500
    assert best['is_default'] is True


def test_export_filename_includes_timestamp(env):
    """The Content-Disposition filename must be timestamped so users can
    keep multiple exports without overwriting."""
    r = _client().get("/custom-formats/export.json")
    cd = r.headers.get('content-disposition', '')
    assert 'attachment' in cd
    assert 'mangarr-cf-bundle-' in cd
    assert '.json' in cd


def test_export_serializes_specifications_as_objects_not_strings(env):
    """The DB stores specifications as JSON-encoded text; the bundle must
    decode them so users see structured data, not escaped strings."""
    r = _client().get("/custom-formats/export.json")
    bundle = r.json()
    od = next(cf for cf in bundle['custom_formats']
              if cf['name'] == 'Official Digital')
    assert isinstance(od['specifications'], list)
    assert od['specifications'][0]['type'] == 'source_is'


def test_export_translates_score_format_ids_to_names(env):
    """Scores in the DB use format_id; the bundle must use CF names so
    the bundle is portable across instances."""
    r = _client().get("/custom-formats/export.json")
    bundle = r.json()
    for p in bundle['quality_profiles']:
        for cf_name in p['scores']:
            assert isinstance(cf_name, str)
            # Ensure the name actually resolves to a CF in the same bundle
            cf_names = {cf['name'] for cf in bundle['custom_formats']}
            assert cf_name in cf_names, (
                f"profile {p['name']} score references unknown CF: {cf_name}"
            )


# ───────────────────── Import ─────────────────────


def test_import_get_renders_form(env):
    r = _client().get("/custom-formats/import")
    assert r.status_code == 200
    assert 'JSON bundle' in r.text or 'json_payload' in r.text


def test_import_inserts_new_cfs(env):
    """A bundle containing a CF name not already in the DB must INSERT
    that CF."""
    bundle = {
        'version': 1,
        'custom_formats': [
            {
                'name': 'Test New Format',
                'specifications': [
                    {'type': 'release_title_contains',
                     'value': 'special-token', 'negate': False}
                ],
                'include_custom_format_when_renaming': False,
            }
        ],
        'quality_profiles': [],
    }
    csrf = _csrf("ins")
    r = _client().post(
        "/custom-formats/import",
        data={'csrf_token': csrf['headers']['X-CSRFToken'],
              'json_payload': json.dumps(bundle)},
        **csrf,
    )
    assert r.status_code == 200, r.text
    with sqlite3.connect(env['db_path']) as c:
        row = c.execute(
            "SELECT specifications FROM custom_formats WHERE name=?",
            ('Test New Format',)
        ).fetchone()
    assert row is not None, "new CF must be inserted"
    specs = json.loads(row[0])
    assert specs[0]['value'] == 'special-token'


def test_import_updates_existing_cf_by_name(env):
    """If the bundle contains a CF whose name matches an existing one,
    the existing row's specs are replaced."""
    bundle = {
        'version': 1,
        'custom_formats': [
            {
                'name': 'Official Digital',  # exists from seed
                'specifications': [
                    {'type': 'release_title_contains',
                     'value': 'OVERRIDDEN', 'negate': False}
                ],
                'include_custom_format_when_renaming': True,
            }
        ],
        'quality_profiles': [],
    }
    csrf = _csrf("upd")
    _client().post(
        "/custom-formats/import",
        data={'csrf_token': csrf['headers']['X-CSRFToken'],
              'json_payload': json.dumps(bundle)},
        **csrf,
    )
    with sqlite3.connect(env['db_path']) as c:
        row = c.execute(
            "SELECT specifications, include_custom_format_when_renaming"
            " FROM custom_formats WHERE name=?", ('Official Digital',)
        ).fetchone()
    specs = json.loads(row[0])
    assert specs[0]['value'] == 'OVERRIDDEN', "existing CF specs must be replaced"
    assert row[1] == 1


def test_import_inserts_new_profile_with_score_wiring(env):
    """Importing a bundle with a new profile must INSERT it AND wire
    its scores to existing CFs by name."""
    bundle = {
        'version': 1,
        'custom_formats': [],  # use existing CFs
        'quality_profiles': [
            {
                'name': 'My Custom Profile',
                'qualities': ['cbz', 'epub'],
                'cutoff': 'cbz',
                'upgrades_allowed': True,
                'minimum_custom_format_score': 0,
                'cutoff_format_score': 5000,
                'min_upgrade_format_score': 50,
                'is_default': False,
                'scores': {
                    'Official Digital': 999,
                    'PDF (avoid)': -50,
                },
            }
        ],
    }
    csrf = _csrf("newprof")
    _client().post(
        "/custom-formats/import",
        data={'csrf_token': csrf['headers']['X-CSRFToken'],
              'json_payload': json.dumps(bundle)},
        **csrf,
    )
    with sqlite3.connect(env['db_path']) as c:
        pid = c.execute(
            "SELECT id FROM quality_profiles WHERE name=?",
            ('My Custom Profile',)
        ).fetchone()
        assert pid is not None
        scores = {
            cf_name: score for cf_name, score in c.execute(
                "SELECT cf.name, qpcf.score FROM quality_profile_custom_formats qpcf"
                " JOIN custom_formats cf ON cf.id = qpcf.format_id"
                " WHERE qpcf.profile_id=?", (pid[0],)
            ).fetchall()
        }
    assert scores.get('Official Digital') == 999
    assert scores.get('PDF (avoid)') == -50


def test_import_skips_score_entries_for_missing_cfs(env):
    """If a profile's score references a CF name not in the destination
    DB, that score entry is silently skipped (the rest still apply)."""
    bundle = {
        'version': 1,
        'custom_formats': [],
        'quality_profiles': [
            {
                'name': 'Partial Profile',
                'qualities': ['cbz'],
                'cutoff': 'cbz',
                'scores': {
                    'Official Digital': 500,        # exists
                    'NonexistentFormat-XYZ': 100,   # doesn't
                },
            }
        ],
    }
    csrf = _csrf("partial")
    r = _client().post(
        "/custom-formats/import",
        data={'csrf_token': csrf['headers']['X-CSRFToken'],
              'json_payload': json.dumps(bundle)},
        **csrf,
    )
    assert r.status_code == 200
    body = r.text
    assert 'Skipped 1' in body, "summary must report the skipped score entry"

    with sqlite3.connect(env['db_path']) as c:
        pid = c.execute(
            "SELECT id FROM quality_profiles WHERE name=?",
            ('Partial Profile',)
        ).fetchone()[0]
        scored_cfs = {
            r[0] for r in c.execute(
                "SELECT cf.name FROM quality_profile_custom_formats qpcf"
                " JOIN custom_formats cf ON cf.id = qpcf.format_id"
                " WHERE qpcf.profile_id=?", (pid,)
            ).fetchall()
        }
    assert 'Official Digital' in scored_cfs
    assert 'NonexistentFormat-XYZ' not in scored_cfs


def test_import_does_not_clobber_existing_unrelated_cfs(env):
    """Importing a small bundle must leave CFs not mentioned in the
    bundle untouched (additive semantics)."""
    with sqlite3.connect(env['db_path']) as c:
        cf_count_before = c.execute(
            "SELECT COUNT(*) FROM custom_formats"
        ).fetchone()[0]
    bundle = {
        'version': 1,
        'custom_formats': [
            {'name': 'Brand New CF', 'specifications': []}
        ],
        'quality_profiles': [],
    }
    csrf = _csrf("addonly")
    _client().post(
        "/custom-formats/import",
        data={'csrf_token': csrf['headers']['X-CSRFToken'],
              'json_payload': json.dumps(bundle)},
        **csrf,
    )
    with sqlite3.connect(env['db_path']) as c:
        cf_count_after = c.execute(
            "SELECT COUNT(*) FROM custom_formats"
        ).fetchone()[0]
    assert cf_count_after == cf_count_before + 1, (
        "import must add the new CF without removing existing ones"
    )


def test_import_replaces_score_wiring_for_existing_profile(env):
    """When updating an existing profile by name, its score join-rows
    are replaced by the bundle's scores (not merged) — this lets users
    actually clear out a stale score by omitting it from the bundle."""
    csrf = _csrf("replace")
    bundle = {
        'version': 1,
        'custom_formats': [],
        'quality_profiles': [
            {
                'name': 'Best Available',  # existing preset
                'qualities': ['cbz', 'epub'],
                'cutoff': 'cbz',
                'scores': {'Official Digital': 1234},  # only this score
            }
        ],
    }
    _client().post(
        "/custom-formats/import",
        data={'csrf_token': csrf['headers']['X-CSRFToken'],
              'json_payload': json.dumps(bundle)},
        **csrf,
    )
    with sqlite3.connect(env['db_path']) as c:
        pid = c.execute(
            "SELECT id FROM quality_profiles WHERE name=?",
            ('Best Available',)
        ).fetchone()[0]
        score_rows = c.execute(
            "SELECT cf.name, qpcf.score FROM quality_profile_custom_formats qpcf"
            " JOIN custom_formats cf ON cf.id = qpcf.format_id"
            " WHERE qpcf.profile_id=?", (pid,)
        ).fetchall()
    score_dict = dict(score_rows)
    assert score_dict == {'Official Digital': 1234}, (
        f"existing profile's scores must be replaced, not merged; got {score_dict}"
    )


def test_import_malformed_json_returns_400(env):
    csrf = _csrf("bad")
    r = _client().post(
        "/custom-formats/import",
        data={'csrf_token': csrf['headers']['X-CSRFToken'],
              'json_payload': '{not valid json'},
        **csrf,
    )
    assert r.status_code == 400
    assert 'Invalid JSON' in r.text


def test_import_empty_payload_returns_400(env):
    csrf = _csrf("empty")
    r = _client().post(
        "/custom-formats/import",
        data={'csrf_token': csrf['headers']['X-CSRFToken'],
              'json_payload': ''},
        **csrf,
    )
    assert r.status_code == 400
    assert 'No JSON' in r.text


def test_import_root_must_be_object(env):
    """A JSON array at root is malformed for our shape — must reject."""
    csrf = _csrf("notobj")
    r = _client().post(
        "/custom-formats/import",
        data={'csrf_token': csrf['headers']['X-CSRFToken'],
              'json_payload': '[]'},
        **csrf,
    )
    assert r.status_code == 400
    assert 'object' in r.text.lower()


# ───────────────────── Round-trip ─────────────────────


def test_round_trip_export_import_reproduces_state(env, tmp_path):
    """Export from a populated DB, wipe it, import the bundle — the
    resulting state must match the original (same CF names, profile
    names, and per-profile score map)."""
    # Snapshot original state
    r = _client().get("/custom-formats/export.json")
    original = r.json()

    # Wipe everything
    with sqlite3.connect(env['db_path']) as c:
        c.execute("DELETE FROM quality_profile_custom_formats")
        c.execute("DELETE FROM quality_profiles")
        c.execute("DELETE FROM custom_formats")

    # Re-import the snapshot
    csrf = _csrf("rt")
    r = _client().post(
        "/custom-formats/import",
        data={'csrf_token': csrf['headers']['X-CSRFToken'],
              'json_payload': json.dumps(original)},
        **csrf,
    )
    assert r.status_code == 200, r.text

    # Re-export and compare
    r2 = _client().get("/custom-formats/export.json")
    after = r2.json()

    # Compare CF names + spec content
    orig_cfs = {cf['name']: cf['specifications'] for cf in original['custom_formats']}
    after_cfs = {cf['name']: cf['specifications'] for cf in after['custom_formats']}
    assert orig_cfs == after_cfs

    # Compare profile names + score maps
    orig_profiles = {
        p['name']: (p['cutoff'], p['scores']) for p in original['quality_profiles']
    }
    after_profiles = {
        p['name']: (p['cutoff'], p['scores']) for p in after['quality_profiles']
    }
    assert orig_profiles == after_profiles


# ───────────────────── Discoverability ─────────────────────


def test_export_button_appears_on_custom_formats_page(env):
    r = _client().get("/custom-formats")
    assert r.status_code == 200
    assert '/custom-formats/export.json' in r.text
    assert '/custom-formats/import' in r.text


def test_export_button_appears_on_quality_profiles_page(env):
    r = _client().get("/quality-profiles")
    assert r.status_code == 200
    assert '/custom-formats/export.json' in r.text
    assert '/custom-formats/import' in r.text


# ───────────────────── Route order regression ─────────────────────


def test_import_route_not_shadowed_by_format_id_handler(env):
    """The route order invariant from CLAUDE.md: literal paths must come
    before parameterized siblings. POST /custom-formats/import must NOT
    fall through to POST /custom-formats/{format_id} (which would 422
    on the non-integer 'import')."""
    csrf = _csrf("order")
    r = _client().post(
        "/custom-formats/import",
        data={'csrf_token': csrf['headers']['X-CSRFToken'],
              'json_payload': '{"custom_formats":[],"quality_profiles":[]}'},
        **csrf,
    )
    # Either 200 (handled correctly) or our 400 (handled correctly with
    # bad payload), but NEVER 422 (which would mean format_id="import"
    # got tried as the matching handler).
    assert r.status_code != 422, (
        "POST /custom-formats/import is being shadowed by "
        "/custom-formats/{format_id} — fix router include order in main.py"
    )
    assert r.status_code in (200, 400)
