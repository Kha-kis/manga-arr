"""Review finding B: PATCH /api/series/{id} treats total_volumes=0 as
falsy, skipping the vol_count_source='manual' update and leaving the
series in an inconsistent state (column set to 0 but source still
says 'anilist' or whatever). Full-form editor explicitly guards with
total_volumes > 0; PATCH now matches that invariant: accept only
null (clears) or a positive int, reject 0 / negatives with 400."""
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
    key_dir = tempfile.mkdtemp(prefix="mangarr-tv-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()
    main.ensure_api_key()

    with sqlite3.connect(db.name) as c:
        c.execute(
            "INSERT INTO series(id, title, search_pattern, total_volumes,"
            " vol_count_source) VALUES(5, 'S', 'S', 10, 'anilist')"
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


def _api_key(env):
    from security import decrypt_secret
    with sqlite3.connect(env) as c:
        raw = c.execute("SELECT value FROM settings WHERE key='api_key'").fetchone()[0]
    return decrypt_secret(raw)


def _row(env):
    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        return dict(c.execute("SELECT total_volumes, vol_count_source FROM series WHERE id=5").fetchone())


def test_positive_total_volumes_sets_source_to_manual(env):
    import main
    client = TestClient(main.app)
    r = client.request(
        'PATCH', '/api/series/5',
        json={'total_volumes': 20},
        headers={'X-Api-Key': _api_key(env)},
    )
    assert r.status_code == 200, r.text
    row = _row(env)
    assert row['total_volumes'] == 20
    assert row['vol_count_source'] == 'manual'


def test_zero_total_volumes_rejected_with_400(env):
    import main
    client = TestClient(main.app)
    r = client.request(
        'PATCH', '/api/series/5',
        json={'total_volumes': 0},
        headers={'X-Api-Key': _api_key(env)},
    )
    assert r.status_code == 400, r.text
    # Row must be unchanged
    row = _row(env)
    assert row['total_volumes'] == 10
    assert row['vol_count_source'] == 'anilist'


def test_negative_total_volumes_rejected(env):
    import main
    client = TestClient(main.app)
    r = client.request(
        'PATCH', '/api/series/5',
        json={'total_volumes': -3},
        headers={'X-Api-Key': _api_key(env)},
    )
    assert r.status_code == 400
    assert _row(env)['total_volumes'] == 10


def test_non_integer_total_volumes_rejected(env):
    import main
    client = TestClient(main.app)
    r = client.request(
        'PATCH', '/api/series/5',
        json={'total_volumes': 'twelve'},
        headers={'X-Api-Key': _api_key(env)},
    )
    assert r.status_code == 400


def test_boolean_total_volumes_rejected(env):
    """bool is-a int in Python; explicitly reject it so True/False can't
    sneak in and set total_volumes=1/0."""
    import main
    client = TestClient(main.app)
    r = client.request(
        'PATCH', '/api/series/5',
        json={'total_volumes': True},
        headers={'X-Api-Key': _api_key(env)},
    )
    assert r.status_code == 400


def test_null_total_volumes_clears_column_without_touching_source(env):
    import main
    client = TestClient(main.app)
    r = client.request(
        'PATCH', '/api/series/5',
        json={'total_volumes': None},
        headers={'X-Api-Key': _api_key(env)},
    )
    assert r.status_code == 200, r.text
    row = _row(env)
    assert row['total_volumes'] is None
    # Clearing should leave the source alone — the operator is un-setting
    # the value, not declaring a new source.
    assert row['vol_count_source'] == 'anilist'
