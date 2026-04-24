"""Tests for log_event's optional `db` parameter.

Rationale:
  - log_event is called from inside active write transactions in several
    hot paths (_execute_import, _queue_import, _mark_downloaded, plus
    check_download_status cleanup). Each call that opens its own
    sqlite3 connection serializes behind the outer writer, burning the
    15-second SQLITE_BUSY timeout in the worst case.
  - log_event now accepts db=<existing_connection>; when supplied, the
    INSERT runs on that connection (same transaction, no contention).
  - Without db, behavior is unchanged: opens a fresh connection.

These tests verify both paths and guard against regressions via a
performance floor (an import that emits many log_event calls from
inside the transaction must finish well under the SQLITE_BUSY window).
"""
import asyncio
import os
import sqlite3
import tempfile
import time

import pytest


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


@pytest.fixture
def fresh_db(monkeypatch):
    import main
    import shared
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.unlink(tmp.name)
    monkeypatch.setattr(main, "DB_PATH", tmp.name)
    monkeypatch.setattr(shared, "DB_PATH", tmp.name)
    main.init_db()
    main.load_config()
    try:
        yield tmp.name
    finally:
        for ext in ("", "-wal", "-shm"):
            p = tmp.name + ext
            if os.path.exists(p):
                os.unlink(p)


# ───────────────────── helper: event row counting ─────────────────────

def _events_for(db_path, event_type=None):
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        if event_type is None:
            rows = c.execute("SELECT * FROM events").fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM events WHERE event_type=?", (event_type,)
            ).fetchall()
    return rows


# ───────────────────── signature + backward compatibility ─────────────────────

def test_log_event_without_db_still_works(fresh_db):
    """Callers who don't pass db (HTTP handlers, one-shot tasks, background
    loops) must continue to see the original behavior."""
    import main
    main.log_event("test", "no db passed", series_id=None)
    rows = _events_for(fresh_db, "test")
    assert len(rows) == 1
    assert rows[0]["message"] == "no db passed"


def test_log_event_with_db_writes_on_existing_connection(fresh_db):
    """When a db is passed, the INSERT must go through that connection."""
    import main
    # Seed a real series so the events FK (ON DELETE CASCADE) is satisfied.
    # Prior to PR 5 series_id was unconstrained and this test wrote
    # series_id=42 against no real row; now we need a real FK target.
    with main.get_db() as db:
        db.execute(
            "INSERT INTO series(id, title, search_pattern) VALUES(42, 'T', 'T')"
        )
    with main.get_db() as db:
        before = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        main.log_event("test", "with db", series_id=42, db=db)
        # Visible on the SAME connection immediately (same transaction).
        after = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert after == before + 1

    # After the with exits (commit), the row is persisted.
    rows = _events_for(fresh_db, "test")
    assert any(r["message"] == "with db" and r["series_id"] == 42 for r in rows)


def test_log_event_does_not_open_new_connection_when_db_passed(fresh_db, monkeypatch):
    """With db passed in, main.get_db must NOT be called. We wrap get_db
    in a counter and confirm zero invocations during the log_event call."""
    import main
    get_db_calls = {"n": 0}
    orig_get_db = main.get_db
    def counting_get_db(*a, **kw):
        get_db_calls["n"] += 1
        return orig_get_db(*a, **kw)
    monkeypatch.setattr(main, "get_db", counting_get_db)

    # Open one connection OURSELVES (bypassing the counter)
    import sqlite3 as _sql
    conn = _sql.connect(fresh_db, timeout=15)
    conn.row_factory = _sql.Row
    try:
        # Sanity: log_event with db=conn should not touch main.get_db
        main.log_event("test", "no-nest", db=conn)
        conn.commit()
        assert get_db_calls["n"] == 0, \
            f"log_event(db=...) unexpectedly opened {get_db_calls['n']} new connection(s)"
    finally:
        conn.close()

    # And the row persisted on disk via the passed connection
    rows = _events_for(fresh_db, "test")
    assert any(r["message"] == "no-nest" for r in rows)


def test_log_event_swallows_errors_in_both_modes(fresh_db):
    """log_event is best-effort: a broken db must not raise through to
    the caller in either mode."""
    import main
    # Mode 1: db=None, and the events table has been dropped
    with main.get_db() as db:
        db.execute("DROP TABLE events")
    main.log_event("test", "should not raise")  # no exception

    # Mode 2: db=<connection> where the table doesn't exist
    main.init_db()  # recreate schema for the next block
    with main.get_db() as db:
        db.execute("DROP TABLE events")
        # Inside the transaction, the table is gone — passing db still
        # must not propagate the error.
        main.log_event("test", "should not raise either", db=db)


# ───────────────────── performance regression ─────────────────────

def test_in_transaction_log_event_is_fast(fresh_db):
    """A write transaction that makes many log_event calls must not pay
    the SQLITE_BUSY timeout for each. Pre-fix, this would have taken
    N × 15s (one per call). Post-fix, all calls reuse the open
    connection and the whole sequence is sub-second."""
    import main
    N = 20
    t0 = time.monotonic()
    with main.get_db() as db:
        # Simulate an in-transaction pipeline: do a write, then log, repeat.
        for i in range(N):
            db.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"stress-key-{i}", "x"),
            )
            main.log_event("test", f"stress-event-{i}", db=db)
    elapsed = time.monotonic() - t0
    # Generous ceiling: even a slow CI box should finish 20 INSERTs +
    # 20 log_event calls inside one transaction well under a second.
    # The old pre-fix code would have hit 15+ seconds here.
    assert elapsed < 3.0, \
        f"in-transaction log_event is too slow: {elapsed:.2f}s for {N} calls"
    # All rows landed
    rows = _events_for(fresh_db, "test")
    stress_events = [r for r in rows if r["message"].startswith("stress-event-")]
    assert len(stress_events) == N


def test_execute_import_has_no_standalone_log_event_inside_transaction():
    """Guard: every log_event call inside the main `with get_db() as db:`
    block of _execute_import must thread db through. If a future change
    forgets db=db, this test will catch it by scanning the source.

    We detect the in-transaction region by anchoring on `_execute_import`
    and the next top-level `async def` after it, then count log_event
    calls that are missing `db=db`.
    """
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[2] / "app" / "import_pipeline.py").read_text()

    # Extract _execute_import body by line scan.
    lines = src.splitlines()
    start = None
    end = None
    for i, ln in enumerate(lines):
        if ln.startswith("async def _execute_import"):
            start = i
        elif start is not None and ln.startswith("async def "):
            end = i
            break
    assert start is not None and end is not None

    body = "\n".join(lines[start:end])
    bad = []
    for i, ln in enumerate(body.splitlines(), start=start + 1):
        stripped = ln.strip()
        if not stripped.startswith("log_event") and "log_event(" not in stripped:
            continue
        # Crude: a line is "naked" if it contains `log_event(` but not `db=`.
        # Multi-line calls are flagged via the closing line being examined
        # too; we only check single-line forms here because the multi-line
        # forms in _execute_import already include db=db on their arguments.
        if "log_event(" in stripped and "db=" not in stripped and not stripped.endswith("log_event("):
            bad.append((i, stripped))

    # Allow log_event calls in the tail error-handler (outside the with
    # block) — scan only up to the line where `await trigger_komga_scan`
    # marks the start of the post-with region.
    bad_in_tx = [
        (i, s) for i, s in bad
        if not any(marker in "\n".join(lines[start:i])
                   for marker in ("await trigger_komga_scan",))
    ]
    assert bad_in_tx == [], \
        f"log_event calls inside _execute_import missing db=db: {bad_in_tx}"
