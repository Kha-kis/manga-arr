"""PR B: every series-creation path resolves a root_folder_id or fails
with a clear error. Pre-PR, these paths left root_folder_id NULL and
the library-destination code relied on a save_path fallback; post-PR,
no series row can be created without a folder."""
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


@pytest.fixture
def env():
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-rfreq-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()

    try:
        yield db.name
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


# ── resolver helper ──────────────────────────────────────────────────────────

def test_resolver_prefers_explicit_id_when_valid(env):
    import main
    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
        c.execute("INSERT INTO root_folders(id, path, label, is_default) VALUES(7, '/a', 'A', 0)")
        c.execute("INSERT INTO root_folders(id, path, label, is_default) VALUES(8, '/b', 'B', 1)")
    with main.get_db() as db:
        assert main.resolve_root_folder_id(db, preferred_id=7) == 7
        assert main.resolve_root_folder_id(db, preferred_id=8) == 8


def test_resolver_ignores_invalid_preferred_id(env):
    """Preferred ID that doesn't exist → fall through to default."""
    import main
    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
        c.execute("INSERT INTO root_folders(id, path, label, is_default) VALUES(8, '/b', 'B', 1)")
    with main.get_db() as db:
        assert main.resolve_root_folder_id(db, preferred_id=999) == 8


def test_resolver_picks_default_when_no_preferred(env):
    import main
    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
        c.execute("INSERT INTO root_folders(id, path, label, is_default) VALUES(3, '/a', 'A', 0)")
        c.execute("INSERT INTO root_folders(id, path, label, is_default) VALUES(4, '/b', 'B', 1)")
    with main.get_db() as db:
        assert main.resolve_root_folder_id(db) == 4


def test_resolver_falls_back_to_lowest_id_when_no_default(env):
    import main
    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
        c.execute("INSERT INTO root_folders(id, path, label, is_default) VALUES(10, '/a', 'A', 0)")
        c.execute("INSERT INTO root_folders(id, path, label, is_default) VALUES(11, '/b', 'B', 0)")
    with main.get_db() as db:
        assert main.resolve_root_folder_id(db) == 10


def test_resolver_returns_none_when_no_folders(env):
    import main
    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
    with main.get_db() as db:
        assert main.resolve_root_folder_id(db) is None


# ── import_lists path ────────────────────────────────────────────────────────

def test_import_list_skips_when_no_root_folders(env):
    """_sync_list should stop and log an error instead of creating
    series with root_folder_id NULL."""
    import asyncio
    import main
    from routers import import_lists as _il
    from unittest.mock import patch

    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")

    async def _fake_list(*a, **kw):
        return [{
            'anilist_id': 42, 'title': 'ShouldNotLand',
            'search_pattern': 'ShouldNotLand', 'cover_url': '',
            'status': 'RELEASING', 'total_volumes': 3,
        }]

    with patch.object(_il, '_fetch_list', _fake_list):
        asyncio.run(_il._sync_list({
            'id': 1, 'name': 'TestList', 'type': 'anilist_user',
            'settings': '{}', 'monitor_mode': 'all',
            'quality_profile_id': None, 'root_folder_id': None,
        }))

    with sqlite3.connect(env) as c:
        rows = c.execute(
            "SELECT id FROM series WHERE title='ShouldNotLand'"
        ).fetchall()
        evs = c.execute(
            "SELECT message FROM events WHERE event_type='error'"
            " ORDER BY id DESC LIMIT 3"
        ).fetchall()

    assert rows == [], f"series was created with no root folder: {rows}"
    assert any('no root folders' in (e[0] or '').lower() for e in evs), evs


def test_import_list_succeeds_when_root_folder_exists(env):
    """Regression guard: the normal path still adds series."""
    import asyncio
    import main
    from routers import import_lists as _il
    from unittest.mock import patch

    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
        c.execute(
            "INSERT INTO root_folders(id, path, label, is_default)"
            " VALUES(1, '/data/media/manga', 'Manga', 1)"
        )

    async def _fake_list(*a, **kw):
        return [{
            'anilist_id': 51, 'title': 'HappyPath',
            'search_pattern': 'HappyPath', 'cover_url': '',
            'status': 'RELEASING', 'total_volumes': 5,
        }]

    with patch.object(_il, '_fetch_list', _fake_list):
        asyncio.run(_il._sync_list({
            'id': 1, 'name': 'TestList', 'type': 'anilist_user',
            'settings': '{}', 'monitor_mode': 'all',
            'quality_profile_id': None, 'root_folder_id': None,
        }))

    with sqlite3.connect(env) as c:
        row = c.execute(
            "SELECT id, root_folder_id FROM series WHERE title='HappyPath'"
        ).fetchone()
    assert row is not None
    assert row[1] == 1


# ── library search → add path (series_.py:1002) ─────────────────────────────

def test_add_series_returns_400_when_no_folders(env):
    """The UI form handler must refuse to create a series when there's
    no library destination, returning a clear 400."""
    import main
    from fastapi.testclient import TestClient

    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
    main.ensure_api_key()

    client = TestClient(main.app)
    tok = "csrf-addseries-" + "a" * 30
    r = client.post(
        '/series/add',
        data={
            'csrf_token':    tok,
            'title':         'NoFolderSeries',
            'search_pattern': 'NoFolderSeries',
            'anilist_id':    '',
            'edition_type':  'standard',
            'monitored':     '1',
            'search_now':    '0',
        },
        cookies={'csrftoken': tok},
        headers={'X-CSRFToken': tok},
        follow_redirects=False,
    )
    assert r.status_code == 400, r.text
    assert 'root folder' in r.json().get('error', '').lower()

    # No series row was created
    with sqlite3.connect(env) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM series WHERE title='NoFolderSeries'"
        ).fetchone()[0]
    assert n == 0
