"""Review finding C: the FK migration's hardcoded CREATE TABLE _new
DDL must carry every column the old table has. If a future add_col
targets one of the four rebuilt tables (events, blocklist, seen,
pending_releases) without being reflected in the DDL, the old table
would have columns the new one doesn't and INSERT..SELECT would fail
mid-migration — leaving the DB half-migrated (old dropped, new not
yet renamed). The drift guard detects this before data is touched."""
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
    key_dir = tempfile.mkdtemp(prefix="mangarr-drift-keys-")

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


def test_migration_aborts_with_clear_error_when_old_has_extra_column(env):
    """Simulate the post-deploy hazard: sometime after PR 5 lands, a
    future add_col('events', 'source_ref', 'TEXT') gets wired up but
    the migration's events_new DDL isn't updated. On a DB where
    user_version was reset, the migration should abort with a clear
    RuntimeError instead of corrupting the table mid-rebuild."""
    import main

    # Drop the FK'd events table and recreate it with an extra column
    # the migration's DDL does not know about.
    with sqlite3.connect(env) as c:
        c.execute("PRAGMA foreign_keys=OFF")
        c.execute("DROP TABLE events")
        c.execute("""
            CREATE TABLE events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT,
                series_id  INTEGER,
                message    TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                future_col TEXT          -- simulated future add_col
            )
        """)
        c.execute("PRAGMA user_version = 0")
        # Seed a row so there's something to migrate
        c.execute(
            "INSERT INTO events(event_type, series_id, message, future_col)"
            " VALUES('x', NULL, 'legit', 'unknown-to-migration')"
        )

    with pytest.raises(RuntimeError, match='schema migration drift'):
        main._migrate_schema_constraints()

    # Old table must still be intact — we aborted before ALTER RENAME
    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        # Old table still there
        tbls = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'events%'"
        ).fetchall()]
        assert 'events' in tbls, tbls
        # _new cleanup happened — no half-built table left around
        assert 'events_new' not in tbls, f"leaked events_new after aborted migration: {tbls}"
        # Data preserved
        count = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert count == 1
        # user_version stays at 0 so a corrected migration can re-run
        ver = c.execute("PRAGMA user_version").fetchone()[0]
    assert ver == 0


def test_migration_succeeds_when_old_schema_matches_ddl(env):
    """Regression guard: a normal pre-migration DB (no unexpected
    columns) must still migrate successfully without tripping the
    drift guard."""
    import main
    with sqlite3.connect(env) as c:
        c.execute("PRAGMA foreign_keys=OFF")
        c.execute("DROP TABLE events")
        c.execute("""
            CREATE TABLE events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT,
                series_id  INTEGER,
                message    TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("INSERT INTO events(event_type, series_id, message) VALUES('x', NULL, 'ok')")
        c.execute("PRAGMA user_version = 0")

    # Should NOT raise
    main._migrate_schema_constraints()

    # FK is now in place on the rebuilt events table
    with sqlite3.connect(env) as c:
        fks = c.execute("PRAGMA foreign_key_list(events)").fetchall()
        assert any(fk[2] == 'series' and fk[6] == 'CASCADE' for fk in fks), fks
        ver = c.execute("PRAGMA user_version").fetchone()[0]
    assert ver == 2
