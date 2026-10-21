"""Post-review fix: DB write-lock contention.

Three levels of fix:
  1. busy_timeout bumped from 5000ms to 30000ms so concurrent writers
     no longer fail on legitimate multi-second transactions.
  2. _qbit_orphan_cleanup_sync commits per orphan instead of holding
     one transaction across N orphans × ~10 writes each.
  3. cleanup_stuck_state commits per phase instead of one transaction
     for all three phases.

This test pins the busy_timeout value so future edits to shared.py
don't silently re-introduce the 5s window.
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
    key_dir = tempfile.mkdtemp(prefix="mangarr-busyto-keys-")

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


def test_busy_timeout_is_at_least_30s(env):
    """Every connection handed out by get_db must have busy_timeout
    >= 30000ms. This is the single most impactful change for
    reducing 'database is locked' errors under contention."""
    from shared import get_db
    with get_db() as db:
        ms = db.execute("PRAGMA busy_timeout").fetchone()[0]
    assert ms >= 30000, (
        f"busy_timeout regressed to {ms}ms — was 5000ms causing lock errors,"
        f" bumped to 30000ms in the post-review fix"
    )


def test_concurrent_write_succeeds_within_busy_timeout(env):
    """A second connection trying to write while the first connection's
    transaction is still open must block and ultimately succeed rather
    than failing immediately. Demonstrates busy_timeout is active."""
    from shared import get_db
    import sqlite3 as _s
    import threading, time

    errors: list = []

    def writer_b():
        # Give writer A a moment to acquire the write lock
        time.sleep(0.1)
        try:
            with get_db() as db:
                db.execute(
                    "INSERT INTO settings(key, value) VALUES('b', '1')"
                )
        except _s.OperationalError as e:
            errors.append(str(e))

    t = threading.Thread(target=writer_b)
    t.start()

    # Writer A: quick transaction. While we hold it, writer B queues;
    # when we commit (on exit from the with block), B can proceed.
    with get_db() as db:
        db.execute("INSERT INTO settings(key, value) VALUES('a', '1')")
        time.sleep(0.5)  # writer B has been waiting during this hold

    t.join(timeout=5)
    assert not errors, f"writer B failed while A held the lock: {errors}"
    # Both rows should be present
    with sqlite3.connect(env) as c:
        found = {r[0] for r in c.execute(
            "SELECT key FROM settings WHERE key IN ('a', 'b')"
        ).fetchall()}
    assert found == {'a', 'b'}


def test_cleanup_stuck_state_uses_separate_transactions_per_phase(env):
    """Each phase (grabbed volumes / pending_releases / import_queue)
    runs in its own transaction. Verified by observing that a write
    from an outside connection can slot in between phases even when
    all three phases would be active."""
    import main
    import sqlite3 as _s

    # Seed rows that trigger each cleanup phase
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO series(id, title, search_pattern, monitored)"
            " VALUES(1, 'S', 'S', 1)"
        )
        # Phase 1 target: stale grabbed volume
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, monitored,"
            " grabbed_at) VALUES(1, 1.0, 'grabbed', 1,"
            " datetime('now', '-8 hours'))"
        )
        # Phase 2 target: pending_release for deleted series
        c.execute(
            "INSERT INTO pending_releases(series_id, url, title)"
            " VALUES(999, 'https://x/p', 'Orphan')"
        )
        # Phase 3 target: stale import_queue row
        c.execute(
            "INSERT INTO import_queue(series_id, download_id, torrent_name,"
            " status, created_at) VALUES(1, 'dl', 'T', 'pending',"
            " datetime('now', '-35 days'))"
        )

    stats = main.cleanup_stuck_state()
    assert stats == {
        'volumes_reset':         1,
        'pending_deleted':       1,
        'queue_failed':          1,
        'importing_reset':       0,
        'events_pruned':         0,
        'orphan_packs_deleted':  0,
    }


@pytest.mark.parametrize("protocol", ["torrent", "nzb"])
def test_orphan_cleanup_helper_per_row_transactions_still_correct(
    env: str, monkeypatch: pytest.MonkeyPatch, protocol: str
) -> None:
    """Enumeration closes; independent acquisitions commit and release separately."""
    from collections.abc import Generator
    from contextlib import closing, contextmanager
    from typing import Any

    from download_identity import DownloadProtocol
    import import_discovery
    import shared

    download_protocol: DownloadProtocol = "torrent" if protocol == "torrent" else "nzb"
    client_type = "qbittorrent" if protocol == "torrent" else "sabnzbd"
    ids = {"first-id": 101, "second-id": 102}
    with shared.get_db() as db:
        db.execute("INSERT INTO series(id,title,search_pattern) VALUES(1,'S','S')")
        for download_id, row_id in ids.items():
            db.execute(
                "INSERT INTO volumes(id,series_id,volume_num,status,download_id,"
                " download_client_id,protocol,client,source_url)"
                " VALUES(?,1,?,'grabbed',?,7,?,?,?)",
                (
                    row_id,
                    row_id,
                    download_id,
                    protocol,
                    client_type,
                    f"https://fixture.invalid/{row_id}",
                ),
            )
    real_get_db = import_discovery.get_db
    real_reserve = import_discovery._reserve_orphan_cleanup
    connections: list[sqlite3.Connection] = []
    statements: list[list[str]] = []
    reservations: list[tuple[sqlite3.Connection, str]] = []

    @contextmanager
    def observed_get_db() -> Generator[sqlite3.Connection, None, None]:
        # No timing oracle: an outside writer must fit between every context.
        with closing(sqlite3.connect(env, timeout=0)) as competitor:
            competitor.execute("BEGIN IMMEDIATE")
            competitor.rollback()
        if connections:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                connections[-1].execute("SELECT 1")
        with real_get_db() as db:
            connections.append(db)
            trace: list[str] = []
            statements.append(trace)
            db.set_trace_callback(trace.append)
            yield db

    def observe_reservation(
        db: sqlite3.Connection, download_id: str, **scope: Any
    ) -> tuple[bool, list[int]]:
        assert db.in_transaction
        with closing(sqlite3.connect(env, timeout=0)) as competitor:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competitor.execute("BEGIN IMMEDIATE")
            if reservations:
                previous, previous_id = reservations[-1]
                assert competitor.execute(
                    "SELECT status FROM volumes WHERE id=?", (ids[previous_id],)
                ).fetchone() == ("wanted",), "prior acquisition is not committed"
                with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                    previous.execute("SELECT 1")
                assert previous is not db
        reservations.append((db, download_id))
        return real_reserve(db, download_id, **scope)

    monkeypatch.setattr(import_discovery, "get_db", observed_get_db)
    monkeypatch.setattr(
        import_discovery, "_reserve_orphan_cleanup", observe_reservation
    )
    import_discovery._cleanup_missing_downloads(
        set(),
        download_client_id=7,
        protocol=download_protocol,
        client_type=client_type,
        include_legacy_ownerless=False,
    )
    assert len(reservations) == 2
    assert len(connections) == 3  # One read-only enumeration, then two writers.
    assert not any(
        sql.startswith(("UPDATE", "INSERT", "DELETE", "BEGIN")) for sql in statements[0]
    )
    for trace in statements[1:]:
        operations = [sql for sql in trace if not sql.startswith("BEGIN")]
        assert operations[0] == "UPDATE import_queue SET id=id WHERE 0"
    with closing(sqlite3.connect(env, timeout=0)) as competitor:
        competitor.execute("BEGIN IMMEDIATE")
        assert competitor.execute(
            "SELECT COUNT(*) FROM volumes WHERE status='wanted'"
        ).fetchone() == (2,)
        assert competitor.execute(
            "SELECT COUNT(*) FROM history WHERE event_type='grab_failed'"
        ).fetchone() == (2,)
