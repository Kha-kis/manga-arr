"""Regression test for init_db on a fresh /config.

History: init_db() previously called add_col('chapters', 'quality', 'TEXT')
*before* CREATE TABLE chapters. On a fresh DB the ALTER TABLE failed,
get_db rolled back the surrounding transaction, and no schema was created
at all — leaving the app unusable on first boot.

This test runs init_db against a brand-new, empty database file and
asserts that all expected tables (and a couple of late-added columns)
are present afterwards.
"""
import os
import sqlite3
import tempfile

import pytest


# Tables that init_db is responsible for creating. Not exhaustive — just
# enough to catch a transaction rollback that swallows the whole schema.
EXPECTED_TABLES = {
    "settings",
    "series",
    "volumes",
    "seen",
    "events",
    "root_folders",
    "blocklist",
    "history",
    "import_queue",
    "import_queue_files",
    "series_aliases",
    "pending_releases",
    "chapters",
    "quality_profiles",
    "custom_formats",
    "release_profiles",
    "delay_profiles",
    "download_clients",
    "indexers",
    "notification_connections",
    "import_lists",
    "import_list_exclusions",
    "language_profiles",
    "quality_definitions",
    "remote_path_mappings",
    "mangadex_chapters",
    "suwayomi_downloads",
    "suwayomi_sources",
}


@pytest.fixture
def fresh_db(monkeypatch):
    """Point main.DB_PATH at an empty tmp file and yield it."""
    import main

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.unlink(tmp.name)  # init_db will create it from scratch

    monkeypatch.setattr(main, "DB_PATH", tmp.name)
    # shared.get_db reads its own DB_PATH constant; patch there too so the
    # context manager points at the temp file.
    import shared
    monkeypatch.setattr(shared, "DB_PATH", tmp.name)

    try:
        yield tmp.name
    finally:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)
        # Also clean up the WAL/SHM sidecars that sqlite may have written.
        for ext in ("-wal", "-shm"):
            p = tmp.name + ext
            if os.path.exists(p):
                os.unlink(p)


def test_init_db_succeeds_on_fresh_database(fresh_db):
    """init_db must run cleanly against an empty file and create the schema."""
    import main
    main.init_db()  # must not raise

    conn = sqlite3.connect(fresh_db)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        actual = {r[0] for r in rows}
    finally:
        conn.close()

    missing = EXPECTED_TABLES - actual
    assert not missing, f"init_db left these tables uncreated: {sorted(missing)}"


def test_init_db_creates_chapters_quality_and_imported_at(fresh_db):
    """The two add_col calls that triggered the original bug must run.
    On a fresh DB, chapters.quality and chapters.imported_at are added by
    add_col — not by the CREATE TABLE — so they're a sensitive indicator
    that the post-CREATE add_col block executed."""
    import main
    main.init_db()

    conn = sqlite3.connect(fresh_db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(chapters)").fetchall()}
    finally:
        conn.close()

    assert "quality" in cols, f"chapters.quality missing; got {sorted(cols)}"
    assert "imported_at" in cols, f"chapters.imported_at missing; got {sorted(cols)}"


def test_init_db_is_idempotent(fresh_db):
    """Existing populated DBs must continue to start cleanly: running
    init_db a second time on an already-initialised DB is a no-op."""
    import main
    main.init_db()
    # Insert a sentinel row so we can verify init_db doesn't drop or wipe
    # data on the second pass.
    with sqlite3.connect(fresh_db) as c:
        c.execute("INSERT INTO settings(key,value) VALUES('sentinel','keep-me')")
        c.commit()

    main.init_db()  # second pass — must not raise, must not lose data

    with sqlite3.connect(fresh_db) as c:
        row = c.execute("SELECT value FROM settings WHERE key='sentinel'").fetchone()
    assert row is not None and row[0] == "keep-me"
