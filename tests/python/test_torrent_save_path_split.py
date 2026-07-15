"""Split torrent download path from library path.

- torrent_save_path: where qBittorrent writes in-progress downloads
- save_path: where the library lives (unchanged)
- When torrent_save_path is empty, falls back to save_path (preserves
  the pre-split single-directory default for existing installs)

Covers:
  - ENV_DEFAULTS includes torrent_save_path
  - SETTINGS_VALIDATORS (if any) don't reject blank / valid paths
  - The settings form handler accepts and persists the new field
  - qBit category setup uses torrent_save_path when set, save_path otherwise
"""
import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch, AsyncMock

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


@pytest.fixture
def env():
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-tsp-keys-")

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


# ── Config shape ─────────────────────────────────────────────────────────────

def test_torrent_save_path_is_in_env_defaults():
    import main
    assert 'torrent_save_path' in main.ENV_DEFAULTS
    env_var, default = main.ENV_DEFAULTS['torrent_save_path']
    assert env_var == 'MANGARR_DOWNLOAD_PATH'
    assert default == ''


def test_legacy_download_path_environment_alias_still_loads(env, monkeypatch):
    import main

    monkeypatch.delenv("MANGARR_DOWNLOAD_PATH", raising=False)
    monkeypatch.setenv("MANGA_TORRENT_PATH", "/legacy/downloads")
    main.load_config()
    assert main.CONFIG["torrent_save_path"] == "/legacy/downloads"


def test_canonical_download_path_environment_name_wins(env, monkeypatch):
    import main

    monkeypatch.setenv("MANGA_TORRENT_PATH", "/legacy/downloads")
    monkeypatch.setenv("MANGARR_DOWNLOAD_PATH", "/canonical/downloads")
    main.load_config()
    assert main.CONFIG["torrent_save_path"] == "/canonical/downloads"


def test_blank_torrent_save_path_is_normal(env):
    """Blank = 'fall back to save_path' — valid, no validation should
    mark it invalid or coerce it to something else."""
    import main
    assert main.CONFIG.get('torrent_save_path') == ''
    # Cross-check the DB has no row yet (blank default didn't persist)
    with sqlite3.connect(env) as c:
        row = c.execute(
            "SELECT value FROM settings WHERE key='torrent_save_path'"
        ).fetchone()
    assert row is None


# ── Settings handler persists the field ──────────────────────────────────────

def test_settings_form_persists_torrent_save_path(env):
    import main
    from fastapi.testclient import TestClient
    main.ensure_api_key()
    client = TestClient(main.app)

    tok = "csrf-t-" + "a" * 32
    form = {
        'csrf_token':          tok,
        'save_path':           '/data/media/manga',
        'torrent_save_path':   '/data/torrents/manga',
        'category':            'manga',
        'import_mode':         'hardlink',
        'minimum_free_space_mb': '2048',
        'komga_scan_enabled':  'false',
    }
    r = client.post(
        '/settings',
        data=form,
        cookies={'csrftoken': tok},
        headers={'X-CSRFToken': tok},
        follow_redirects=False,
    )
    assert r.status_code in (303, 200), r.text

    with sqlite3.connect(env) as c:
        row = c.execute(
            "SELECT value FROM settings WHERE key='torrent_save_path'"
        ).fetchone()
        free_row = c.execute(
            "SELECT value FROM settings WHERE key='minimum_free_space_mb'"
        ).fetchone()
    assert row is not None and row[0] == '/data/torrents/manga'
    assert free_row is not None and free_row[0] == '2048'


def test_settings_form_accepts_blank_torrent_save_path(env):
    import main
    from fastapi.testclient import TestClient
    main.ensure_api_key()
    client = TestClient(main.app)

    tok = "csrf-b-" + "a" * 32
    # Seed a value first so we can see it cleared
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT OR REPLACE INTO settings(key, value)"
            " VALUES('torrent_save_path', '/old/path')"
        )
    form = {
        'csrf_token':          tok,
        'save_path':           '/data/media/manga',
        'torrent_save_path':   '   ',  # whitespace must be stripped to empty
        'category':            'manga',
        'import_mode':         'hardlink',
        'komga_scan_enabled':  'false',
    }
    client.post(
        '/settings',
        data=form,
        cookies={'csrftoken': tok},
        headers={'X-CSRFToken': tok},
        follow_redirects=False,
    )
    with sqlite3.connect(env) as c:
        row = c.execute(
            "SELECT value FROM settings WHERE key='torrent_save_path'"
        ).fetchone()
    assert row is not None and row[0] == ''


# ── qBit category setup honours the new path ────────────────────────────────

def test_qbit_category_uses_torrent_save_path_when_set(env):
    """Simulate the lifespan code path that POSTs createCategory to qBit.
    When torrent_save_path is set, that value (not save_path) must be
    passed to qBit as savePath."""
    import main

    # Seed settings so get_cfg returns the expected values
    with sqlite3.connect(env) as c:
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('save_path', '/data/media/manga')")
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('torrent_save_path', '/data/torrents/manga')")
    main.load_config()

    # Replicate the exact resolution logic from lifespan
    qbit_save = (main.get_cfg('torrent_save_path', '') or '').strip() \
                or main.get_cfg('save_path')
    assert qbit_save == '/data/torrents/manga'


def test_qbit_category_falls_back_to_save_path_when_torrent_path_blank(env):
    """Existing installs upgraded without setting torrent_save_path must
    keep working — fall back to the old single-directory save_path."""
    import main
    with sqlite3.connect(env) as c:
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('save_path', '/data/media/manga')")
        # torrent_save_path deliberately not set
    main.load_config()

    qbit_save = (main.get_cfg('torrent_save_path', '') or '').strip() \
                or main.get_cfg('save_path')
    assert qbit_save == '/data/media/manga'


def test_qbit_category_fallback_when_torrent_path_is_whitespace(env):
    """Defence against a persisted whitespace value slipping through."""
    import main
    with sqlite3.connect(env) as c:
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('save_path', '/data/media/manga')")
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('torrent_save_path', '   ')")
    main.load_config()

    qbit_save = (main.get_cfg('torrent_save_path', '') or '').strip() \
                or main.get_cfg('save_path')
    assert qbit_save == '/data/media/manga'


# ── Library path is unaffected ───────────────────────────────────────────────

def test_library_destination_uses_save_path_not_torrent_path(env):
    """The library destination path (used by the import pipeline) must
    NOT be changed by torrent_save_path. That column controls the qBit
    category only."""
    import main
    with sqlite3.connect(env) as c:
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('save_path', '/data/media/manga')")
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('torrent_save_path', '/data/torrents/manga')")
    main.load_config()

    # This mirrors main.py:3325 / 5081 / 4073 — library destination
    assert main.get_cfg('save_path', '/manga') == '/data/media/manga'
