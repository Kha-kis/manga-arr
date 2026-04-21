"""PR A: Mangarr now follows the *arr convention — root folders are
the single library-destination mechanism. Bootstrap helper runs at
init_db time to migrate legacy save_path-only installs:

  - if no root folders exist and save_path is set, create one from it
  - assign any series with root_folder_id IS NULL to the default folder

Runs on every boot, but is a no-op once every series has a folder and
at least one folder exists, so it's safe to call idempotently.
"""
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
    key_dir = tempfile.mkdtemp(prefix="mangarr-rfboot-keys-")

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


def _folders(env):
    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(
            "SELECT id, path, label, is_default FROM root_folders"
        ).fetchall()]


def test_bootstrap_creates_folder_from_save_path_when_none_exist(env):
    import main
    # Fresh env may already have folders from init_db bootstrap — reset
    # to the pre-bootstrap state: zero folders, save_path set.
    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
        c.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES('save_path', '/legacy/manga')"
        )
    main.load_config()
    assert _folders(env) == []

    main._bootstrap_root_folders()

    folders = _folders(env)
    assert len(folders) == 1
    assert folders[0]['path'] == '/legacy/manga'
    assert folders[0]['label'] == 'Manga'
    assert folders[0]['is_default'] == 1


def test_bootstrap_skips_folder_creation_when_any_folder_exists(env):
    """If any root folder is already present, don't create one from save_path
    — the operator has clearly configured the app manually."""
    import main
    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
        c.execute(
            "INSERT INTO root_folders(path, label, is_default)"
            " VALUES('/data/media/manga', 'Manga', 1)"
        )
        c.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES('save_path', '/should/not/be/created')"
        )
    main.load_config()

    main._bootstrap_root_folders()

    folders = _folders(env)
    assert len(folders) == 1
    assert folders[0]['path'] == '/data/media/manga'


def test_bootstrap_skips_when_save_path_empty(env):
    """Nothing to bootstrap from. Don't silently create a weird folder."""
    import main
    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
        c.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES('save_path', '')"
        )
    main.load_config()

    main._bootstrap_root_folders()

    assert _folders(env) == []


def test_bootstrap_assigns_orphan_series_to_default_folder(env):
    import main
    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
        c.execute(
            "INSERT INTO root_folders(id, path, label, is_default)"
            " VALUES(5, '/data/media/manga', 'Manga', 1)"
        )
        c.execute(
            "INSERT INTO series(id, title, search_pattern, root_folder_id)"
            " VALUES(10, 'S', 'S', NULL)"
        )
        c.execute(
            "INSERT INTO series(id, title, search_pattern, root_folder_id)"
            " VALUES(11, 'T', 'T', NULL)"
        )

    main._bootstrap_root_folders()

    with sqlite3.connect(env) as c:
        rows = c.execute(
            "SELECT id, root_folder_id FROM series WHERE id IN (10, 11)"
        ).fetchall()
    assert all(r[1] == 5 for r in rows)


def test_bootstrap_assigns_to_lowest_id_when_no_default_flagged(env):
    """Safety: if no folder has is_default=1 (operator flubbed config),
    fall back to the lowest-id folder so series still get a home."""
    import main
    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
        c.execute(
            "INSERT INTO root_folders(id, path, label, is_default)"
            " VALUES(3, '/data/media/a', 'A', 0)"
        )
        c.execute(
            "INSERT INTO root_folders(id, path, label, is_default)"
            " VALUES(4, '/data/media/b', 'B', 0)"
        )
        c.execute(
            "INSERT INTO series(id, title, search_pattern, root_folder_id)"
            " VALUES(10, 'S', 'S', NULL)"
        )
    main._bootstrap_root_folders()

    with sqlite3.connect(env) as c:
        rf = c.execute(
            "SELECT root_folder_id FROM series WHERE id=10"
        ).fetchone()[0]
    assert rf == 3  # lowest id wins when no default is flagged


def test_bootstrap_is_no_op_when_every_series_has_folder(env):
    import main
    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
        c.execute(
            "INSERT INTO root_folders(id, path, label, is_default)"
            " VALUES(9, '/data/media/manga', 'Manga', 1)"
        )
        c.execute(
            "INSERT INTO series(id, title, search_pattern, root_folder_id)"
            " VALUES(10, 'S', 'S', 9)"
        )
    # Count pre-migration state
    with sqlite3.connect(env) as c:
        before_events = c.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='schema_migration'"
        ).fetchone()[0]

    main._bootstrap_root_folders()

    # No new schema_migration events written — no-op
    with sqlite3.connect(env) as c:
        after_events = c.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='schema_migration'"
        ).fetchone()[0]
        # The orphan-assignment UPDATE fires but touches zero rows, so
        # its event is not written (guarded by `if assigned > 0`).
        # Similarly the folder-creation block is skipped when a folder
        # already exists. Net: no new events.
    assert after_events == before_events


def test_bootstrap_logs_events_for_each_action(env):
    import main
    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
        c.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES('save_path', '/legacy/m')"
        )
        c.execute(
            "INSERT INTO series(id, title, search_pattern, root_folder_id)"
            " VALUES(10, 'S', 'S', NULL)"
        )
    main.load_config()

    main._bootstrap_root_folders()

    with sqlite3.connect(env) as c:
        events = [r[0] for r in c.execute(
            "SELECT message FROM events WHERE event_type='schema_migration'"
            " ORDER BY id DESC LIMIT 10"
        ).fetchall()]
    assert any('bootstrapped root folder' in e and '/legacy/m' in e for e in events)
    assert any('assigned 1 orphan series' in e for e in events)
