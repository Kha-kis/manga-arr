"""PR 4c: PATCH /api/series/{id} updates only the fields submitted in
the JSON body, without clobbering other columns with form defaults.
Prior workaround for scripted callers was to pull-then-repost every
field against POST /series/{id}/edit; this endpoint fixes that."""
import json
import os
import sqlite3
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


@pytest.fixture
def env():
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-patch-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()
    main.ensure_api_key()

    # Series with several non-default values that the PATCH must preserve
    with sqlite3.connect(db.name) as c:
        c.execute(
            "INSERT INTO series(id, title, search_pattern, edition_type,"
            " omnibus_preference, update_strategy, source_type,"
            " quality_cutoff, total_volumes, enabled, monitored, monitor_mode)"
            " VALUES(5, 'S5', 'S5', 'official_color', 'prefer_omnibus',"
            " 'once', 'official_only', 'cbz', 42, 1, 1, 'missing')"
        )

    try:
        yield db.name
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


def _client():
    import main
    return TestClient(main.app)


def _api_key(env):
    with sqlite3.connect(env) as c:
        return c.execute("SELECT value FROM settings WHERE key='api_key'").fetchone()[0]


def _row(db_path: str) -> dict:
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        return dict(c.execute("SELECT * FROM series WHERE id=5").fetchone())


def test_patch_title_does_not_clobber_other_fields(env):
    import main
    # Load config so the cipher is applied in the running process
    main.load_config()
    # Decrypt key for the header
    from security import decrypt_secret
    api_key = decrypt_secret(_api_key(env))

    c = _client()
    resp = c.request(
        'PATCH', '/api/series/5',
        json={'title': 'S5 Renamed'},
        headers={'X-Api-Key': api_key},
    )
    assert resp.status_code == 200, resp.text
    row = _row(env)
    assert row['title'] == 'S5 Renamed'
    # Every other field stays as seeded
    assert row['edition_type']       == 'official_color'
    assert row['omnibus_preference'] == 'prefer_omnibus'
    assert row['update_strategy']    == 'once'
    assert row['source_type']        == 'official_only'
    assert row['quality_cutoff']     == 'cbz'
    assert row['total_volumes']      == 42
    assert row['monitor_mode']       == 'missing'


def test_patch_rejects_unknown_field(env):
    import main
    from security import decrypt_secret
    api_key = decrypt_secret(_api_key(env))
    c = _client()
    resp = c.request(
        'PATCH', '/api/series/5',
        json={'not_a_field': 'oops'},
        headers={'X-Api-Key': api_key},
    )
    assert resp.status_code == 400


def test_patch_rejects_empty_body(env):
    import main
    from security import decrypt_secret
    api_key = decrypt_secret(_api_key(env))
    c = _client()
    resp = c.request(
        'PATCH', '/api/series/5',
        json={},
        headers={'X-Api-Key': api_key},
    )
    assert resp.status_code == 400


def test_patch_404s_on_unknown_series(env):
    import main
    from security import decrypt_secret
    api_key = decrypt_secret(_api_key(env))
    c = _client()
    resp = c.request(
        'PATCH', '/api/series/9999',
        json={'title': 'nope'},
        headers={'X-Api-Key': api_key},
    )
    assert resp.status_code == 404


def test_patch_total_volumes_sets_vol_count_source_manual(env):
    import main
    from security import decrypt_secret
    api_key = decrypt_secret(_api_key(env))
    c = _client()
    resp = c.request(
        'PATCH', '/api/series/5',
        json={'total_volumes': 50},
        headers={'X-Api-Key': api_key},
    )
    assert resp.status_code == 200
    row = _row(env)
    assert row['total_volumes'] == 50
    assert row['vol_count_source'] == 'manual'


def test_patch_preferred_groups_accepts_list_and_stores_json(env):
    import main
    from security import decrypt_secret
    api_key = decrypt_secret(_api_key(env))
    c = _client()
    resp = c.request(
        'PATCH', '/api/series/5',
        json={'preferred_groups': ['ScanGroupA', 'ScanGroupB']},
        headers={'X-Api-Key': api_key},
    )
    assert resp.status_code == 200
    row = _row(env)
    stored = json.loads(row['preferred_groups'])
    assert stored == ['ScanGroupA', 'ScanGroupB']


def test_patch_requires_api_key(env):
    import main
    c = _client()
    resp = c.request(
        'PATCH', '/api/series/5',
        json={'title': 'no-auth'},
    )
    assert resp.status_code == 401
