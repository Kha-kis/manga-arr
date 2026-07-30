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
import threading
from collections.abc import Callable

import pytest


# Tables that init_db is responsible for creating. Not exhaustive — just
# enough to catch a transaction rollback that swallows the whole schema.
EXPECTED_TABLES = {
    "settings",
    "auth_admin",
    "auth_sessions",
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


def _database_state(db_path: str) -> tuple[tuple[str, ...], dict[str, object]]:
    """Return logical schema/data and persistent pragma state.

    SQLite may create or remove WAL, SHM, or rollback-journal sidecars while
    opening and rolling back connections. Those files are engine bookkeeping;
    this snapshot intentionally asserts the logical database state instead.
    """
    pragma_names = (
        "application_id",
        "auto_vacuum",
        "encoding",
        "freelist_count",
        "journal_mode",
        "page_size",
        "schema_version",
        "user_version",
    )
    with sqlite3.connect(db_path) as conn:
        dump = tuple(conn.iterdump())
        pragmas = {
            name: conn.execute(f"PRAGMA {name}").fetchone()[0]
            for name in pragma_names
        }
    return dump, pragmas


def _interleave_future_schema_writer(
    monkeypatch,
    db_path: str,
    write_future_state: Callable[[sqlite3.Connection], None],
) -> tuple[Callable[[], None], list[bool]]:
    """Start a future-schema writer immediately after version validation."""
    import schema

    original_validate = schema._validate_schema_version
    attempted = threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []
    lock_states: list[bool] = []
    thread: threading.Thread | None = None

    def writer() -> None:
        try:
            with sqlite3.connect(db_path, timeout=10) as conn:
                conn.execute("PRAGMA busy_timeout=10000")
                conn.execute("PRAGMA ignore_check_constraints=ON")
                attempted.set()
                conn.execute("BEGIN IMMEDIATE")
                write_future_state(conn)
                conn.execute("PRAGMA user_version = 999")
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    def validate_with_interleave(db: sqlite3.Connection) -> int:
        nonlocal thread
        version = original_validate(db)
        if thread is None:
            lock_states.append(db.in_transaction)
            thread = threading.Thread(target=writer, daemon=True)
            thread.start()
            assert attempted.wait(2), "future-schema writer did not start"
            if not db.in_transaction:
                assert finished.wait(5), "unlocked future-schema writer did not finish"
        return version

    def finish() -> None:
        assert thread is not None, "schema validation hook was not reached"
        thread.join(15)
        assert not thread.is_alive(), "future-schema writer remained blocked"
        assert not errors, errors

    monkeypatch.setattr(schema, "_validate_schema_version", validate_with_interleave)
    return finish, lock_states


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


def test_init_db_rejects_future_schema_without_mutating_database(fresh_db: str):
    """A newer database must fail closed without changing logical DB state."""
    import main

    with sqlite3.connect(fresh_db) as conn:
        conn.execute(
            "CREATE TABLE future_data(id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO future_data(value) VALUES('keep-me')")
        conn.execute("PRAGMA application_id = 4242")
        conn.execute("PRAGMA user_version = 999")

    before = _database_state(fresh_db)

    with pytest.raises(RuntimeError, match="newer than this Mangarr version"):
        main.init_db()

    assert _database_state(fresh_db) == before


@pytest.mark.parametrize("journal_mode", ("delete", "wal"))
def test_init_db_serializes_future_schema_writer(
    fresh_db: str,
    monkeypatch,
    journal_mode: str,
):
    """Initialization must not write after another connection claims the DB."""
    import main

    main.init_db()
    with sqlite3.connect(fresh_db) as conn:
        actual_mode = conn.execute(
            f"PRAGMA journal_mode={journal_mode}"
        ).fetchone()[0]
        assert actual_mode == journal_mode
        conn.execute(
            "INSERT INTO series(title, search_pattern) VALUES('Race', 'Race')"
        )
        series_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO volumes(series_id, volume_num, status)"
            " VALUES(?, 1, 'wanted')",
            (series_id,),
        )
        volume_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def write_future_state(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE volumes SET status='downloaded', import_path=NULL,"
            " quality=NULL, imported_at=NULL WHERE id=?",
            (volume_id,),
        )

    finish_writer, lock_states = _interleave_future_schema_writer(
        monkeypatch,
        fresh_db,
        write_future_state,
    )
    try:
        main.init_db()
    finally:
        finish_writer()

    with sqlite3.connect(fresh_db) as conn:
        state = conn.execute(
            "SELECT status FROM volumes WHERE id=?", (volume_id,)
        ).fetchone()[0]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert lock_states == [True]
    assert state == "downloaded"
    assert version == 999


@pytest.mark.parametrize("journal_mode", ("delete", "wal"))
def test_direct_migration_serializes_future_schema_writer(
    fresh_db: str,
    monkeypatch,
    journal_mode: str,
):
    """Direct migration must hold its writer lock through version updates."""
    import main

    main.init_db()
    with sqlite3.connect(fresh_db) as conn:
        actual_mode = conn.execute(
            f"PRAGMA journal_mode={journal_mode}"
        ).fetchone()[0]
        assert actual_mode == journal_mode
        conn.execute(
            "INSERT INTO series(title, search_pattern) VALUES('Race', 'Race')"
        )
        series_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO volumes(series_id, volume_num, status)"
            " VALUES(?, 1, 'wanted')",
            (series_id,),
        )
        volume_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("PRAGMA user_version = 1")

    def write_future_state(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE volumes SET status='future-owned' WHERE id=?",
            (volume_id,),
        )

    finish_writer, lock_states = _interleave_future_schema_writer(
        monkeypatch,
        fresh_db,
        write_future_state,
    )
    try:
        main._migrate_schema_constraints()
    finally:
        finish_writer()

    with sqlite3.connect(fresh_db) as conn:
        state = conn.execute(
            "SELECT status FROM volumes WHERE id=?", (volume_id,)
        ).fetchone()[0]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert lock_states == [True]
    assert state == "future-owned"
    assert version == 999
