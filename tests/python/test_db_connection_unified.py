"""Tests for the unified DB connection setup.

Issue #31 was caused by two problems:

1. `main.py` had its own thin `get_db()` missing the PRAGMAs (busy_timeout,
   synchronous=NORMAL, cache tuning) that `shared.py`'s version sets.
   Background loops (status_loop, suwayomi_monitor_loop, sync_mangadex_
   chapters) used `main.get_db`, so every write they did ran with
   synchronous=FULL — slow commits, long lock holds, cascading stalls
   for any route doing a concurrent write.

2. `PRAGMA journal_mode=WAL` was set on *every* connection. Even when
   the DB was already in WAL mode, the statement acquires a write lock.
   Under contention, that lock acquisition timed out at the 5-second
   busy_timeout on every get_db call, stacking into 15–60s page stalls.
   Since journal_mode is a persistent DB-file setting, it only needs to
   be applied once.

These tests pin both fixes so a future refactor can't quietly regress.
"""
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


def test_main_get_db_is_shared_get_db():
    """main.get_db must be the same callable as shared.get_db so every
    caller — routes and background loops — sees identical PRAGMAs.
    Regression guard against reintroducing a thin main-local copy."""
    import main, shared
    assert main.get_db is shared.get_db, (
        "main.get_db diverged from shared.get_db again — background loops "
        "will lose synchronous=NORMAL, busy_timeout, cache_size, mmap_size"
    )


def test_get_db_does_not_set_journal_mode_per_connection(tmp_path, monkeypatch):
    """Setting journal_mode on every connection caused 5-second lock
    stalls under contention (issue #31). Confirm the hot path does NOT
    emit a `PRAGMA journal_mode=WAL` statement."""
    import shared
    db = tmp_path / "test.db"
    monkeypatch.setattr(shared, "DB_PATH", str(db))

    # Pre-create the file and set WAL once (simulating the startup path).
    shared.ensure_wal_journal_mode()

    executed: list[str] = []

    # sqlite3 provides set_trace_callback for SQL-level tracing; use it
    # instead of monkey-patching conn.execute (which is read-only).
    orig_connect = sqlite3.connect
    def _connect(*a, **kw):
        conn = orig_connect(*a, **kw)
        conn.set_trace_callback(executed.append)
        return conn
    monkeypatch.setattr(sqlite3, "connect", _connect)

    with shared.get_db() as db_conn:
        db_conn.execute("SELECT 1").fetchone()

    journal_stmts = [s for s in executed if "journal_mode" in s.lower()]
    assert not journal_stmts, (
        "get_db set journal_mode inside the hot path; this acquires a "
        "write lock even when already WAL and cascades into page stalls. "
        f"Offending statements: {journal_stmts}"
    )


def test_get_db_still_sets_per_connection_pragmas(tmp_path, monkeypatch):
    """The PRAGMAs that ARE per-connection (busy_timeout, synchronous,
    foreign_keys, cache_size, mmap_size) must still be applied — they
    don't persist across connections like journal_mode does."""
    import shared
    db = tmp_path / "test.db"
    monkeypatch.setattr(shared, "DB_PATH", str(db))
    shared.ensure_wal_journal_mode()

    executed: list[str] = []
    orig_connect = sqlite3.connect
    def _connect(*a, **kw):
        conn = orig_connect(*a, **kw)
        conn.set_trace_callback(lambda s: executed.append(s.lower()))
        return conn
    monkeypatch.setattr(sqlite3, "connect", _connect)

    with shared.get_db() as db_conn:
        pass

    # These PRAGMAs are per-connection and MUST appear.
    expected_fragments = [
        "foreign_keys",
        "synchronous",
        "busy_timeout",
        "cache_size",
        "mmap_size",
    ]
    for frag in expected_fragments:
        assert any(frag in s for s in executed), (
            f"per-connection PRAGMA '{frag}' missing from get_db — "
            f"executed: {executed}"
        )


def test_ensure_wal_journal_mode_applies_wal_once(tmp_path, monkeypatch):
    """The startup helper flips the DB to WAL. After it runs, the file
    reports WAL and no further connection needs to re-apply."""
    import shared
    db = tmp_path / "test.db"
    # Seed the file with a trivial schema so PRAGMA can report.
    sqlite3.connect(str(db)).close()
    monkeypatch.setattr(shared, "DB_PATH", str(db))

    shared.ensure_wal_journal_mode()

    # Verify via a fresh connection that mode is now WAL.
    conn = sqlite3.connect(str(db))
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "wal"


def test_ensure_wal_is_idempotent(tmp_path, monkeypatch):
    """Running ensure_wal twice must not error and must leave the file
    in WAL mode."""
    import shared
    db = tmp_path / "test.db"
    sqlite3.connect(str(db)).close()
    monkeypatch.setattr(shared, "DB_PATH", str(db))

    shared.ensure_wal_journal_mode()
    shared.ensure_wal_journal_mode()  # must not raise

    conn = sqlite3.connect(str(db))
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "wal"


def test_lifespan_calls_ensure_wal_after_init_db():
    """Regression guard: the startup path must still include a call to
    ensure_wal_journal_mode(). Catches a future refactor that deletes
    the wiring and silently reverts to the slow per-connection PRAGMA."""
    import main, inspect
    src = inspect.getsource(main.lifespan)
    assert "ensure_wal_journal_mode" in src, (
        "lifespan no longer calls ensure_wal_journal_mode — WAL mode may "
        "not be applied on fresh DBs"
    )


def test_busy_timeout_is_reasonable(tmp_path, monkeypatch):
    """Confirm busy_timeout is set to the documented 5000ms. Too high
    compounds the stalls when multiple statements contend (reported as
    issue #31); too low produces spurious 'database is locked' errors."""
    import shared
    db = tmp_path / "test.db"
    monkeypatch.setattr(shared, "DB_PATH", str(db))
    shared.ensure_wal_journal_mode()

    with shared.get_db() as conn:
        t = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert t == 5000, f"busy_timeout drifted to {t}"
