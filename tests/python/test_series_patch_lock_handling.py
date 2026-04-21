"""Post-review fix: PATCH /api/series/{id} returns 503 with Retry-After
when SQLite raises OperationalError('database is locked') instead of
the generic 500 FastAPI emits by default.

Review context: during the live verification of PR 54, a PATCH call
took 46s and returned 500 because the DB was under heavy write
contention from the pre-existing check_download_status loop. The
lock contention itself is a separate (pre-existing) issue; this
patch just makes the PATCH endpoint fail more gracefully when it
hits a busy DB.
"""
import json
import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch as _patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


@pytest.fixture
def env():
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-patchlock-keys-")

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
            "INSERT INTO series(id, title, search_pattern) VALUES(6, 'P', 'P')"
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


def test_patch_returns_503_when_db_locked(env):
    """Force sqlite3.OperationalError from the UPDATE and verify the
    endpoint returns 503 with a Retry-After header."""
    import main
    client = TestClient(main.app)
    api_key = _api_key(env)

    # Replace shared.get_db with a context manager whose db.execute
    # raises OperationalError('database is locked') on UPDATE.
    import shared as _shared
    real_get_db = _shared.get_db

    class _LockingCursor:
        def __init__(self, real_conn):
            self._real = real_conn
        def execute(self, sql, *a, **kw):
            if sql.strip().upper().startswith('UPDATE'):
                raise sqlite3.OperationalError('database is locked')
            return self._real.execute(sql, *a, **kw)
        def fetchone(self):  # pragma: no cover — execute always raises first
            return None

    from contextlib import contextmanager
    @contextmanager
    def _locking_get_db():
        conn = sqlite3.connect(env)
        conn.row_factory = sqlite3.Row
        try:
            yield _LockingCursor(conn)
        finally:
            conn.commit()
            conn.close()

    # Patch at both the shared module and the router's imported reference
    from routers import series_ as _sr
    with _patch.object(_shared, 'get_db', _locking_get_db), \
         _patch.object(_sr, 'get_db', _locking_get_db):
        r = client.request(
            'PATCH', '/api/series/6',
            json={'title': 'NewTitle'},
            headers={'X-Api-Key': api_key},
        )
    assert r.status_code == 503, f"expected 503 on lock, got {r.status_code}: {r.text}"
    assert r.headers.get('Retry-After') is not None
    body = r.json()
    assert 'busy' in body.get('error', '').lower() or 'retry' in body.get('error', '').lower()


def test_patch_still_returns_200_on_happy_path(env):
    """Regression guard: the new try/except must not break the
    non-locked happy path."""
    import main
    client = TestClient(main.app)
    api_key = _api_key(env)

    r = client.request(
        'PATCH', '/api/series/6',
        json={'update_strategy': 'once'},
        headers={'X-Api-Key': api_key},
    )
    assert r.status_code == 200, r.text
    assert r.json()['ok'] is True


def test_patch_non_lock_operational_error_still_raises(env):
    """Not every OperationalError is a lock. Others (e.g. no such
    table) should NOT be swallowed as 503 — let them bubble up so
    real bugs aren't masked as 'retry me later'."""
    import main
    client = TestClient(main.app)
    api_key = _api_key(env)

    import shared as _shared
    from contextlib import contextmanager

    class _BrokenCursor:
        def __init__(self, real): self._real = real
        def execute(self, sql, *a, **kw):
            if sql.strip().upper().startswith('UPDATE'):
                raise sqlite3.OperationalError('no such table: series')
            return self._real.execute(sql, *a, **kw)

    @contextmanager
    def _broken_get_db():
        conn = sqlite3.connect(env); conn.row_factory = sqlite3.Row
        try:
            yield _BrokenCursor(conn)
        finally:
            conn.commit(); conn.close()

    # FastAPI's TestClient (raise_server_exceptions=True default) surfaces
    # unhandled exceptions as Python exceptions rather than converting them
    # to 500 responses. If our code correctly did NOT swallow the non-lock
    # OperationalError as 503, the exception will propagate here.
    from routers import series_ as _sr
    with _patch.object(_shared, 'get_db', _broken_get_db), \
         _patch.object(_sr, 'get_db', _broken_get_db):
        with pytest.raises(sqlite3.OperationalError, match='no such table'):
            client.request(
                'PATCH', '/api/series/6',
                json={'title': 'nope'},
                headers={'X-Api-Key': api_key},
            )
