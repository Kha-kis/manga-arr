"""PR 5: FK constraints on events / blocklist / seen / pending_releases.

Pre-migration those four tables declared series_id with no REFERENCES
clause, so deleting a series silently orphaned rows in them. Post-
migration each column is INTEGER REFERENCES series(id) ON DELETE
CASCADE, enforced by SQLite (foreign_keys=ON is already the default
for every Mangarr connection via shared.get_db).

Migrations are gated by PRAGMA user_version so they run exactly once
per DB. Fresh installs skip the rebuild path entirely — their CREATE
TABLE IF NOT EXISTS already contains the new shape.
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
    """Temp DB with a full init_db() — includes the FK migration."""
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-fkschema-keys-")

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


def _fk_list(db_path: str, table: str) -> list[tuple]:
    """Return list of (id, seq, table, from_col, to_col, on_delete, on_update, match)."""
    with sqlite3.connect(db_path) as c:
        return c.execute(f"PRAGMA foreign_key_list({table})").fetchall()


# ── Fresh install: new schema already has FK ─────────────────────────────────

def test_events_has_fk_to_series_on_delete_cascade(env):
    fks = _fk_list(env, 'events')
    # Expect exactly one FK pointing at series(id) with ON DELETE CASCADE
    assert any(fk[2] == 'series' and fk[3] == 'series_id'
               and fk[6] == 'CASCADE'
               for fk in fks), fks


def test_blocklist_has_fk_to_series(env):
    fks = _fk_list(env, 'blocklist')
    assert any(fk[2] == 'series' and fk[3] == 'series_id'
               and fk[6] == 'CASCADE'
               for fk in fks), fks


def test_seen_has_fk_to_series(env):
    fks = _fk_list(env, 'seen')
    assert any(fk[2] == 'series' and fk[3] == 'series_id'
               and fk[6] == 'CASCADE'
               for fk in fks), fks


def test_pending_releases_has_fk_to_series(env):
    fks = _fk_list(env, 'pending_releases')
    assert any(fk[2] == 'series' and fk[3] == 'series_id'
               and fk[6] == 'CASCADE'
               for fk in fks), fks


# ── Cascade behaviour: delete series → dependent rows vanish ─────────────────

def test_delete_series_cascades_to_events(env):
    with sqlite3.connect(env) as c:
        c.execute("PRAGMA foreign_keys=ON")
        c.execute(
            "INSERT INTO series(id, title, search_pattern) VALUES(7, 'S', 'S')"
        )
        c.execute(
            "INSERT INTO events(event_type, series_id, message)"
            " VALUES('test', 7, 'hello')"
        )
        c.execute("DELETE FROM series WHERE id=7")
        remaining = c.execute(
            "SELECT COUNT(*) FROM events WHERE series_id=7"
        ).fetchone()[0]
    assert remaining == 0


def test_delete_series_cascades_to_blocklist(env):
    with sqlite3.connect(env) as c:
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("INSERT INTO series(id, title, search_pattern) VALUES(8, 'B', 'B')")
        c.execute(
            "INSERT INTO blocklist(series_id, torrent_url, reason)"
            " VALUES(8, 'https://x.test/1', 'low')"
        )
        c.execute("DELETE FROM series WHERE id=8")
        assert c.execute(
            "SELECT COUNT(*) FROM blocklist WHERE series_id=8"
        ).fetchone()[0] == 0


def test_delete_series_cascades_to_seen(env):
    with sqlite3.connect(env) as c:
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("INSERT INTO series(id, title, search_pattern) VALUES(9, 'X', 'X')")
        c.execute(
            "INSERT INTO seen(torrent_url, series_id) VALUES('u', 9)"
        )
        c.execute("DELETE FROM series WHERE id=9")
        assert c.execute(
            "SELECT COUNT(*) FROM seen WHERE series_id=9"
        ).fetchone()[0] == 0


def test_delete_series_cascades_to_pending_releases(env):
    with sqlite3.connect(env) as c:
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("INSERT INTO series(id, title, search_pattern) VALUES(10, 'P', 'P')")
        c.execute(
            "INSERT INTO pending_releases(series_id, url, title)"
            " VALUES(10, 'https://x.test/2', 'Q')"
        )
        c.execute("DELETE FROM series WHERE id=10")
        assert c.execute(
            "SELECT COUNT(*) FROM pending_releases WHERE series_id=10"
        ).fetchone()[0] == 0


# ── Migration path: old DB gets rebuilt with FKs ────────────────────────────

def test_legacy_db_without_fks_gets_migrated():
    """Simulate an old DB that has been through prior init_db runs but
    never had the FK-shape migration: drop the target tables, recreate
    them with the legacy FK-less DDL, reset user_version, then re-run
    init_db and verify the migration rebuilds them with CASCADE FKs and
    drops orphan rows."""
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-legacy-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)

    try:
        # Step 1: let init_db build the full current schema
        main.init_db()
        main.load_config()

        # Step 2: tear down the FK-bearing tables and re-create the
        # FK-less legacy shape. Reset user_version so the migration
        # thinks it needs to run.
        with sqlite3.connect(db.name) as c:
            c.execute("PRAGMA foreign_keys=OFF")
            for t in ('events', 'blocklist', 'seen', 'pending_releases'):
                c.execute(f"DROP TABLE {t}")
            c.execute("""
                CREATE TABLE events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT,
                    series_id  INTEGER,
                    message    TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            c.execute("""
                CREATE TABLE blocklist(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    series_id INTEGER,
                    torrent_url TEXT UNIQUE,
                    torrent_name TEXT,
                    reason TEXT,
                    indexer TEXT, protocol TEXT, size_bytes INTEGER,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            c.execute("""
                CREATE TABLE seen(
                    torrent_url TEXT PRIMARY KEY,
                    torrent_name TEXT,
                    series_id INTEGER,
                    volume_num REAL,
                    grabbed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    indexer TEXT, protocol TEXT, client TEXT,
                    download_id TEXT, release_group TEXT, size_bytes INTEGER
                )
            """)
            c.execute("""
                CREATE TABLE pending_releases(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    series_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT, indexer TEXT, protocol TEXT,
                    size_bytes INTEGER DEFAULT 0,
                    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(series_id, url)
                )
            """)
            c.execute("PRAGMA user_version = 0")
            # Seed rows: 1 legit + 1 orphan per table
            c.execute("INSERT INTO series(id, title, search_pattern) VALUES(1, 'A', 'A')")
            c.execute("INSERT INTO events(event_type, series_id, message) VALUES('x', 1, 'ok')")
            c.execute("INSERT INTO events(event_type, series_id, message) VALUES('x', 999, 'orphan')")
            c.execute("INSERT INTO blocklist(series_id, torrent_url) VALUES(1, 'u1')")
            c.execute("INSERT INTO blocklist(series_id, torrent_url) VALUES(999, 'u2')")
            c.execute("INSERT INTO seen(torrent_url, series_id) VALUES('ok-seen', 1)")
            c.execute("INSERT INTO seen(torrent_url, series_id) VALUES('orphan-seen', 999)")
            c.execute("INSERT INTO pending_releases(series_id, url) VALUES(1, 'ok-pr')")
            c.execute("INSERT INTO pending_releases(series_id, url) VALUES(999, 'orphan-pr')")

        # Step 3: re-run init_db — migration should fire
        main.init_db()

        # All four tables should now carry the CASCADE FK
        for tbl in ('events', 'blocklist', 'seen', 'pending_releases'):
            fks = _fk_list(db.name, tbl)
            assert any(fk[2] == 'series' and fk[6] == 'CASCADE' for fk in fks), (
                f"{tbl} still missing FK: {fks}"
            )

        # Orphan rows dropped, legitimate rows preserved
        with sqlite3.connect(db.name) as c:
            for tbl in ('events', 'blocklist', 'seen', 'pending_releases'):
                n_orphan = c.execute(
                    f"SELECT COUNT(*) FROM {tbl} WHERE series_id=999"
                ).fetchone()[0]
                n_legit = c.execute(
                    f"SELECT COUNT(*) FROM {tbl} WHERE series_id=1"
                ).fetchone()[0]
                assert n_orphan == 0, f"{tbl} still has orphan rows"
                assert n_legit == 1, f"{tbl} lost the legitimate row"

        # user_version bumped + idempotent
        with sqlite3.connect(db.name) as c:
            ver = c.execute("PRAGMA user_version").fetchone()[0]
        assert ver >= 1
        main.init_db()  # second run — no-op because user_version is already bumped
        with sqlite3.connect(db.name) as c:
            assert c.execute("PRAGMA user_version").fetchone()[0] == ver

    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


def test_fresh_install_sets_user_version(env):
    """A fresh init_db run must set user_version even though there was
    no data to migrate — otherwise a future boot would try to rebuild
    empty tables."""
    with sqlite3.connect(env) as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
    assert ver == 2


def test_volumes_status_has_check_constraint(env):
    with sqlite3.connect(env) as c:
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("INSERT INTO series(id, title, search_pattern) VALUES(101, 'S', 'S')")
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(
                "INSERT INTO volumes(series_id, volume_num, status)"
                " VALUES(101, 1, 'bogus')"
            )


def test_chapters_status_has_check_constraint(env):
    with sqlite3.connect(env) as c:
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("INSERT INTO series(id, title, search_pattern) VALUES(102, 'S', 'S')")
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(
                "INSERT INTO chapters(series_id, chapter_num, status)"
                " VALUES(102, 1, 'bogus')"
            )


def test_delete_series_cascades_to_volumes(env):
    with sqlite3.connect(env) as c:
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("INSERT INTO series(id, title, search_pattern) VALUES(103, 'S', 'S')")
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status)"
            " VALUES(103, 1, 'wanted')"
        )
        c.execute("DELETE FROM series WHERE id=103")
        assert c.execute(
            "SELECT COUNT(*) FROM volumes WHERE series_id=103"
        ).fetchone()[0] == 0
