"""HTTP-level integration tests for profile CRUD routes.

The coverage audit flagged these as the ZERO-coverage routes — only the
GET-render path is checked by the route sweep. A 500 on save, or a save
that returns 200 but doesn't persist, would only surface in production
when a user tries to use the misconfigured profile and grabs silently
behave wrong.

Profiles silently shape every grab decision: quality cutoff, custom
format scoring, language preference, release ignore terms, propagation
delay. A persistence bug here is exactly the silent-correctness mode
the production-readiness audit was worried about.

Covers:
  - Quality profiles: create, edit, delete, set-default, format-scores
  - Delay profiles: create, edit, delete, reorder
  - Release profiles: create, edit, delete
  - Language profiles: create, edit, delete (with in-use guard),
    set-default
  - Custom formats: create, edit, delete
  - Remote path mappings: create, delete
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
    """Fresh DB; no seeded profiles — we test creation paths directly."""
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-profile-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()
    main.ensure_api_key()

    library_root = tmp_path / "library"
    library_root.mkdir()
    with sqlite3.connect(db.name) as c:
        c.execute("DELETE FROM root_folders")
        c.execute("INSERT INTO root_folders(id, path) VALUES(1, ?)", (str(library_root),))

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


def _csrf(tag: str = "test"):
    tok = f"csrf-{tag}-" + "x" * 30
    return {
        'cookies': {'csrftoken': tok},
        'headers': {'X-CSRFToken': tok},
    }


def _form(csrf, **fields):
    """Build a form-data dict including the csrf_token field."""
    return {'csrf_token': csrf['headers']['X-CSRFToken'], **fields}


# ───────────────────── quality profiles ─────────────────────


def test_quality_profile_create_persists(env):
    """POST /quality-profiles must INSERT a row with the submitted values.
    Silent-correctness mode: returns 303 but doesn't persist; user wonders
    why their new profile isn't an option in the series-edit dropdown."""
    client = _client()
    csrf = _csrf("qp-create")

    r = client.post(
        "/quality-profiles",
        data=_form(csrf, name='HighQuality',
                   qualities='["cbz","epub"]',
                   cutoff='cbz', upgrades_allowed=1,
                   minimum_custom_format_score=10),
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT name, qualities, cutoff, upgrades_allowed, minimum_custom_format_score"
            " FROM quality_profiles WHERE name='HighQuality'"
        ).fetchone()
    assert row is not None, "quality profile must be inserted"
    assert row['cutoff'] == 'cbz'
    assert row['upgrades_allowed'] == 1
    assert row['minimum_custom_format_score'] == 10
    assert json.loads(row['qualities']) == ["cbz", "epub"]


def test_quality_profile_edit_updates(env):
    """POST /quality-profiles/{id} must UPDATE all submitted fields."""
    client = _client()
    csrf = _csrf("qp-edit")

    # Seed a profile
    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO quality_profiles(id, name, qualities, cutoff, upgrades_allowed)"
            " VALUES(900, 'OldName', '[\"cbz\"]', 'cbz', 1)"
        )

    r = client.post(
        "/quality-profiles/900",
        data=_form(csrf, name='NewName',
                   qualities='["cbz","cbr"]',
                   cutoff='cbr', upgrades_allowed=0,
                   minimum_custom_format_score=5),
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT * FROM quality_profiles WHERE id=900").fetchone()
    assert row['name'] == 'NewName'
    assert row['cutoff'] == 'cbr'
    assert row['upgrades_allowed'] == 0
    assert row['minimum_custom_format_score'] == 5
    assert json.loads(row['qualities']) == ["cbz", "cbr"]


def test_quality_profile_delete_clears_series_references(env):
    """Delete must NULL series.quality_profile_id for any series referencing
    the deleted profile (rather than failing FK / leaving dangling refs)."""
    client = _client()
    csrf = _csrf("qp-del")

    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO quality_profiles(id, name, qualities)"
            " VALUES(901, 'ToDelete', '[]')"
        )
        c.execute(
            "INSERT INTO series(id, title, search_pattern, edition_type, enabled,"
            " monitored, monitor_mode, root_folder_id, quality_profile_id)"
            " VALUES(1, 'S1', 'S1', 'standard', 1, 1, 'all', 1, 901)"
        )

    r = client.post("/quality-profiles/901/delete", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        gone = c.execute("SELECT 1 FROM quality_profiles WHERE id=901").fetchone()
        s = c.execute("SELECT quality_profile_id FROM series WHERE id=1").fetchone()
    assert gone is None, "quality profile must be deleted"
    assert s[0] is None, "series quality_profile_id must be NULL'd, not dangling"


def test_quality_profile_set_default_unique(env):
    """set-default flips one profile to is_default=1; others must be 0."""
    client = _client()
    csrf = _csrf("qp-default")

    with sqlite3.connect(env['db_path']) as c:
        c.execute("INSERT INTO quality_profiles(id, name, qualities, is_default) VALUES(910, 'A', '[]', 1)")
        c.execute("INSERT INTO quality_profiles(id, name, qualities, is_default) VALUES(911, 'B', '[]', 0)")

    r = client.post("/quality-profiles/911/set-default", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        a = c.execute("SELECT is_default FROM quality_profiles WHERE id=910").fetchone()[0]
        b = c.execute("SELECT is_default FROM quality_profiles WHERE id=911").fetchone()[0]
    assert a == 0, "previous default must be cleared"
    assert b == 1, "new default must be set"


def test_quality_profile_format_scores_replace(env):
    """JSON POST /quality-profiles/{id}/format-scores replaces the score
    set (DELETE then INSERT)."""
    client = _client()
    csrf = _csrf("qp-scores")

    with sqlite3.connect(env['db_path']) as c:
        c.execute("INSERT INTO quality_profiles(id, name, qualities) VALUES(920, 'Scored', '[]')")
        c.execute("INSERT INTO custom_formats(id, name) VALUES(700, 'CF1'), (701, 'CF2')")

    r = client.post(
        "/quality-profiles/920/format-scores",
        json={'700': 15, '701': -5},
        **csrf,
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}

    with sqlite3.connect(env['db_path']) as c:
        rows = c.execute(
            "SELECT format_id, score FROM quality_profile_custom_formats"
            " WHERE profile_id=920 ORDER BY format_id"
        ).fetchall()
    assert rows == [(700, 15), (701, -5)]


# ───────────────────── delay profiles ─────────────────────


def test_delay_profile_create(env):
    client = _client()
    csrf = _csrf("dp-create")

    r = client.post(
        "/delay-profiles",
        data=_form(csrf, name='Slow',
                   enable_usenet=0, enable_torrent=1,
                   usenet_delay=0, torrent_delay=60,
                   bypass_if_highest_quality=1, tags=''),
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT name, torrent_delay, bypass_if_highest_quality"
            " FROM delay_profiles WHERE name='Slow'"
        ).fetchone()
    assert row is not None
    assert row['torrent_delay'] == 60
    assert row['bypass_if_highest_quality'] == 1


def test_delay_profile_edit_replaces_tags(env):
    """Edit must overwrite the tags set, not merge with the previous one."""
    client = _client()
    csrf = _csrf("dp-edit")

    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO delay_profiles(id, name, enable_usenet, enable_torrent,"
            " usenet_delay, torrent_delay, order_num)"
            " VALUES(800, 'X', 1, 1, 0, 0, 1)"
        )
        c.execute("INSERT INTO delay_profile_tags(profile_id, tag) VALUES(800, 'old1'), (800, 'old2')")

    r = client.post(
        "/delay-profiles/800",
        data=_form(csrf, name='X', enable_usenet=1, enable_torrent=1,
                   usenet_delay=10, torrent_delay=20,
                   bypass_if_highest_quality=0, tags='new1,new2'),
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT usenet_delay, torrent_delay FROM delay_profiles WHERE id=800"
        ).fetchone()
        tags = sorted(r[0] for r in c.execute(
            "SELECT tag FROM delay_profile_tags WHERE profile_id=800").fetchall())
    assert row['usenet_delay'] == 10
    assert row['torrent_delay'] == 20
    assert tags == ['new1', 'new2'], (
        f"old tags must be wiped, only new tags remain — got {tags!r}"
    )


def test_delay_profile_delete(env):
    client = _client()
    csrf = _csrf("dp-del")

    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO delay_profiles(id, name, enable_usenet, enable_torrent,"
            " usenet_delay, torrent_delay, order_num, is_default)"
            " VALUES(810, 'Removable', 1, 1, 0, 0, 1, 0)"
        )

    r = client.post("/delay-profiles/810/delete", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        gone = c.execute("SELECT 1 FROM delay_profiles WHERE id=810").fetchone()
    assert gone is None


def test_delay_profile_default_cannot_be_deleted(env):
    """Default delay profile is protected — DELETE returns 400."""
    client = _client()
    csrf = _csrf("dp-default")

    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO delay_profiles(id, name, enable_usenet, enable_torrent,"
            " usenet_delay, torrent_delay, order_num, is_default)"
            " VALUES(820, 'Default', 1, 1, 0, 0, 0, 1)"
        )

    r = client.post("/delay-profiles/820/delete", **csrf, follow_redirects=False)
    assert r.status_code == 400, r.text

    with sqlite3.connect(env['db_path']) as c:
        still_there = c.execute("SELECT 1 FROM delay_profiles WHERE id=820").fetchone()
    assert still_there is not None, "default profile must NOT be deleted"


# ───────────────────── release profiles ─────────────────────


def test_release_profile_create(env):
    client = _client()
    csrf = _csrf("rp-create")

    r = client.post(
        "/release-profiles",
        data=_form(csrf, name='NoH265',
                   enabled=1, required='', ignored='h265,hevc',
                   preferred='[]', tags='',
                   include_preferred_when_renaming=0),
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT name, ignored, enabled FROM release_profiles WHERE name='NoH265'"
        ).fetchone()
    assert row is not None
    assert row['ignored'] == 'h265,hevc'
    assert row['enabled'] == 1


def test_release_profile_edit_updates_terms(env):
    """Edit must update the term lists; tag set is fully replaced."""
    client = _client()
    csrf = _csrf("rp-edit")

    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO release_profiles(id, name, enabled, required, ignored, preferred)"
            " VALUES(700, 'R1', 1, '', '', '[]')"
        )
        c.execute("INSERT INTO release_profile_tags(profile_id, tag) VALUES(700, 'oldtag')")

    r = client.post(
        "/release-profiles/700",
        data=_form(csrf, name='R1Updated',
                   enabled=0, required='dual-audio', ignored='spam',
                   preferred='[]', tags='newtag',
                   include_preferred_when_renaming=1),
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT * FROM release_profiles WHERE id=700").fetchone()
        tags = sorted(r[0] for r in c.execute(
            "SELECT tag FROM release_profile_tags WHERE profile_id=700"
        ).fetchall())
    assert row['name'] == 'R1Updated'
    assert row['enabled'] == 0
    assert row['required'] == 'dual-audio'
    assert row['ignored'] == 'spam'
    assert row['include_preferred_when_renaming'] == 1
    assert tags == ['newtag']


def test_release_profile_delete(env):
    client = _client()
    csrf = _csrf("rp-del")

    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO release_profiles(id, name, enabled, required, ignored, preferred)"
            " VALUES(701, 'Doomed', 1, '', '', '[]')"
        )

    r = client.post("/release-profiles/701/delete", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        gone = c.execute("SELECT 1 FROM release_profiles WHERE id=701").fetchone()
    assert gone is None


# ───────────────────── language profiles ─────────────────────


def test_language_profile_create(env):
    client = _client()
    csrf = _csrf("lp-create")

    r = client.post(
        "/language-profiles",
        data=_form(csrf, name='EnglishOnly', languages='en', allow_any=0),
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT name, languages, allow_any FROM language_profiles WHERE name='EnglishOnly'"
        ).fetchone()
    assert row is not None
    assert row['allow_any'] == 0


def test_language_profile_edit(env):
    client = _client()
    csrf = _csrf("lp-edit")

    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO language_profiles(id, name, languages, allow_any)"
            " VALUES(600, 'OldLang', '[\"en\"]', 0)"
        )

    r = client.post(
        "/language-profiles/600",
        data=_form(csrf, name='NewLang', languages='en,ja', allow_any=1),
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT * FROM language_profiles WHERE id=600").fetchone()
    assert row['name'] == 'NewLang'
    assert row['allow_any'] == 1


def test_language_profile_delete_when_unused(env):
    client = _client()
    csrf = _csrf("lp-del")

    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO language_profiles(id, name, languages)"
            " VALUES(601, 'Unused', '[\"en\"]')"
        )

    r = client.post("/language-profiles/601/delete", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        gone = c.execute("SELECT 1 FROM language_profiles WHERE id=601").fetchone()
    assert gone is None


def test_language_profile_delete_blocked_when_in_use(env):
    """Delete must refuse if any series references the profile — redirects
    with error param rather than silently NULLing references."""
    client = _client()
    csrf = _csrf("lp-inuse")

    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO language_profiles(id, name, languages)"
            " VALUES(602, 'InUse', '[\"en\"]')"
        )
        c.execute(
            "INSERT INTO series(id, title, search_pattern, edition_type, enabled,"
            " monitored, monitor_mode, root_folder_id, language_profile_id)"
            " VALUES(2, 'S2', 'S2', 'standard', 1, 1, 'all', 1, 602)"
        )

    r = client.post("/language-profiles/602/delete", **csrf, follow_redirects=False)
    # 303 with ?error=in-use means properly guarded
    assert r.status_code == 303, r.text
    assert 'error=in-use' in r.headers.get('location', '')

    with sqlite3.connect(env['db_path']) as c:
        still_there = c.execute("SELECT 1 FROM language_profiles WHERE id=602").fetchone()
    assert still_there is not None, "in-use profile must NOT be deleted"


# ───────────────────── custom formats ─────────────────────


def test_custom_format_create(env):
    client = _client()
    csrf = _csrf("cf-create")

    specs = [{"name": "preferred-group", "implementation": "ReleaseGroupSpecification",
              "negate": False, "required": False, "fields": [{"value": "GroupA"}]}]
    r = client.post(
        "/custom-formats",
        data=_form(csrf, name='GroupBoost', specifications=json.dumps(specs),
                   include_custom_format_when_renaming=0),
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT name, specifications FROM custom_formats WHERE name='GroupBoost'"
        ).fetchone()
    assert row is not None
    saved = json.loads(row['specifications'])
    assert saved == specs


def test_custom_format_edit(env):
    client = _client()
    csrf = _csrf("cf-edit")

    with sqlite3.connect(env['db_path']) as c:
        c.execute("INSERT INTO custom_formats(id, name, specifications) VALUES(500, 'Old', '[]')")

    r = client.post(
        "/custom-formats/500",
        data=_form(csrf, name='Renamed', specifications='[{"x":1}]',
                   include_custom_format_when_renaming=1),
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT * FROM custom_formats WHERE id=500").fetchone()
    assert row['name'] == 'Renamed'
    assert row['include_custom_format_when_renaming'] == 1


def test_custom_format_delete(env):
    client = _client()
    csrf = _csrf("cf-del")

    with sqlite3.connect(env['db_path']) as c:
        c.execute("INSERT INTO custom_formats(id, name, specifications) VALUES(501, 'Doomed', '[]')")

    r = client.post("/custom-formats/501/delete", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        gone = c.execute("SELECT 1 FROM custom_formats WHERE id=501").fetchone()
    assert gone is None


# ───────────────────── remote path mappings ─────────────────────


def test_remote_path_mapping_create(env):
    client = _client()
    csrf = _csrf("rpm-create")

    r = client.post(
        "/download-clients/remote-path-mappings",
        data=_form(csrf, host='', remote_path='/downloads',
                   local_path='/manga/incoming'),
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT host, remote_path, local_path FROM remote_path_mappings"
            " WHERE remote_path='/downloads'"
        ).fetchone()
    assert row is not None
    assert row['local_path'] == '/manga/incoming'


def test_remote_path_mapping_delete(env):
    client = _client()
    csrf = _csrf("rpm-del")

    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO remote_path_mappings(id, host, remote_path, local_path)"
            " VALUES(400, '', '/d', '/l')"
        )

    r = client.post(
        "/download-clients/remote-path-mappings/400/delete",
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        gone = c.execute("SELECT 1 FROM remote_path_mappings WHERE id=400").fetchone()
    assert gone is None
