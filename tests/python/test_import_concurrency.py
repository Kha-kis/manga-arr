"""Tests for H3: concurrent import race protection.

Covers:
- atomic claim_import_queue_row
- bounded _IMPORT_SEM (max 2 concurrent imports)
- _guarded_execute_import behaviour: claim then run under sem
- two workers racing the same queue_id: only one claim succeeds
- manual retry while a row is 'importing' does NOT start a duplicate worker
- stuck-retry vs auto-import cannot double-process the same row
- happy path: a single import still completes cleanly
"""

import asyncio
import math
import os
import random
import sqlite3
import statistics
import tempfile
import threading
import time
from collections import defaultdict

import pytest


def _run(coro):
    """Run a coroutine in a fresh event loop, then restore a fresh default
    loop so subsequent tests that use the deprecated
    asyncio.get_event_loop().run_until_complete() pattern (our SSRF sink
    tests) still work. Plain _run() closes the loop and leaves the
    thread without one set, which trips get_event_loop on Python 3.11."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


# ───────────────────── fixtures ─────────────────────


@pytest.fixture
def fresh_db(monkeypatch):
    """Point main.DB_PATH at an empty tmp file and run init_db."""
    import import_execute
    import main
    import shared

    original_sem = import_execute._IMPORT_SEM
    original_sem_value = original_sem._value if original_sem is not None else None
    original_main_config = main.CONFIG
    original_main_values = dict(main.CONFIG)
    original_shared_config = shared.CONFIG
    original_shared_values = dict(shared.CONFIG)
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
        if original_sem is not None and original_sem_value is not None:
            original_sem._value = original_sem_value
        import_execute._IMPORT_SEM = original_sem
        main.CONFIG = original_main_config
        main.CONFIG.clear()
        main.CONFIG.update(original_main_values)
        shared.CONFIG = original_shared_config
        shared.CONFIG.clear()
        shared.CONFIG.update(original_shared_values)
        for ext in ("", "-wal", "-shm"):
            p = tmp.name + ext
            if os.path.exists(p):
                os.unlink(p)


def _insert_queue_row(
    db_path,
    series_id=1,
    download_id="dl-x",
    torrent_name="x.cbz",
    torrent_url="",
    volume_num=1.0,
    status="pending",
):
    """Insert one import_queue row; return its id."""
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        # series table has a NOT NULL search_pattern; seed one row so the FK
        # on import_queue.series_id can be satisfied (init_db doesn't seed).
        c.execute(
            "INSERT OR IGNORE INTO series(id,title,search_pattern) VALUES(?,?,?)",
            (series_id, f"Series {series_id}", f"series-{series_id}"),
        )
        cur = c.execute(
            "INSERT INTO import_queue(series_id,download_id,torrent_name,torrent_url,"
            "volume_num,src_dir,status) VALUES(?,?,?,?,?,?,?)",
            (
                series_id,
                download_id,
                torrent_name,
                torrent_url,
                volume_num,
                "/tmp",
                status,
            ),
        )
        c.commit()
        return cur.lastrowid


def _get_queue_state(db_path, queue_id):
    with sqlite3.connect(db_path) as c:
        row = c.execute(
            "SELECT status, lease_owner, lease_expires_at"
            " FROM import_queue WHERE id=?",
            (queue_id,),
        ).fetchone()
    return row


def _get_status(db_path, queue_id):
    row = _get_queue_state(db_path, queue_id)
    return row[0] if row else None


# ───────────────────── claim_import_queue_row ─────────────────────


def test_qbit_completed_aliases_schedule_one_canonical_download():
    from import_discovery import _deduplicate_qbit_matches

    torrent = {"hash": "ABC123", "name": "Completed Release"}
    rows = [
        {"series_id": 7, "download_id": "abc123", "torrent_name": "Alias A"},
        {"series_id": 7, "download_id": "ABC123", "torrent_name": "Alias B"},
        {"series_id": 8, "download_id": "", "torrent_name": "Completed Release"},
    ]

    matched = _deduplicate_qbit_matches(
        rows,
        {"abc123": torrent},
        {"completed release": torrent},
    )

    assert [(row["series_id"], download_id) for row, _, download_id in matched] == [
        (7, "abc123"),
        (8, "abc123"),
    ]


def test_claim_succeeds_on_pending(fresh_db):
    import main

    qid = _insert_queue_row(fresh_db, status="pending")
    with main.get_db() as db:
        assert main.claim_import_queue_row(db, qid, "owner-pending") is True
    assert _get_queue_state(fresh_db, qid) == (
        "importing",
        "owner-pending",
        _get_queue_state(fresh_db, qid)[2],
    )
    assert _get_queue_state(fresh_db, qid)[2] is not None


def test_claim_succeeds_on_partial(fresh_db):
    import main

    qid = _insert_queue_row(fresh_db, status="partial")
    with main.get_db() as db:
        assert main.claim_import_queue_row(db, qid, "owner-partial") is True
    assert _get_status(fresh_db, qid) == "importing"


def test_claim_fails_when_already_importing(fresh_db):
    """Two workers call claim on the same row; only the first wins."""
    import main

    qid = _insert_queue_row(fresh_db, status="pending")
    with main.get_db() as db:
        first = main.claim_import_queue_row(db, qid, "owner-a")
    with main.get_db() as db:
        second = main.claim_import_queue_row(db, qid, "owner-b")
    assert first is True
    assert second is False, "second claim should have lost the race"
    assert _get_queue_state(fresh_db, qid)[0:2] == ("importing", "owner-a")


def test_claim_fails_on_terminal_states(fresh_db):
    import main

    for terminal in ("imported", "failed", "skipped"):
        qid = _insert_queue_row(fresh_db, download_id=f"dl-{terminal}", status=terminal)
        with main.get_db() as db:
            assert main.claim_import_queue_row(db, qid, f"owner-{terminal}") is False, (
                f"claim must not pick up a row in terminal state {terminal!r}"
            )
        assert _get_status(fresh_db, qid) == terminal


# ───────────────────── _guarded_execute_import: bounded concurrency ─────────────────────


def _install_fake_execute_import(monkeypatch, probe):
    """Replace _execute_import with a fake that records how many copies are
    running concurrently, via an in-memory probe dict:
        probe['running']      current in-flight count
        probe['peak']         max observed in-flight
        probe['started_ids']  list of queue_ids that actually ran
    """
    import main
    import import_execute

    async def _fake_execute_import(queue_id, *a, **kw):
        from import_lease import transition_import_queue_row

        probe["running"] += 1
        probe["started_ids"].append(queue_id)
        probe["peak"] = max(probe["peak"], probe["running"])
        await asyncio.sleep(0.05)
        probe["running"] -= 1
        with main.get_db() as db:
            assert transition_import_queue_row(
                db,
                queue_id,
                kw["lease_owner"],
                "imported",
            )
        return True

    monkeypatch.setattr(import_execute, "_execute_import", _fake_execute_import)


def test_semaphore_bounds_concurrent_imports_to_two(fresh_db, monkeypatch):
    """Spawn 10 pending rows, kick off a worker for each, assert at most 2
    are ever in _execute_import simultaneously."""
    import main

    # Reset the semaphore so earlier tests don't leak state.
    import import_execute

    import_execute._IMPORT_SEM = asyncio.Semaphore(2)

    qids = [_insert_queue_row(fresh_db, download_id=f"dl-{i}") for i in range(10)]
    probe = {"running": 0, "peak": 0, "started_ids": []}
    _install_fake_execute_import(monkeypatch, probe)

    async def _run_all():
        await asyncio.gather(*[main._guarded_execute_import(q) for q in qids])

    _run(_run_all())

    assert probe["peak"] <= 2, f"semaphore breach: peak={probe['peak']} in-flight"
    assert sorted(probe["started_ids"]) == sorted(qids), (
        "every queue_id should have been processed exactly once"
    )
    # All rows reached 'imported'
    for q in qids:
        assert _get_status(fresh_db, q) == "imported"


# ───────────────────── same-row race ─────────────────────


def test_two_guarded_workers_for_same_queue_id_only_one_runs(fresh_db, monkeypatch):
    """Fire two _guarded_execute_import coroutines against the same queue_id;
    only one should actually call _execute_import."""
    import main
    import import_execute

    import_execute._IMPORT_SEM = asyncio.Semaphore(2)

    qid = _insert_queue_row(fresh_db)
    probe = {"running": 0, "peak": 0, "started_ids": []}
    _install_fake_execute_import(monkeypatch, probe)

    async def _race():
        a, b = await asyncio.gather(
            main._guarded_execute_import(qid),
            main._guarded_execute_import(qid),
        )
        return a, b

    a, b = _run(_race())

    # Exactly one returned True (won claim + ran); the other got False (claim lost).
    assert (a is True) ^ (b is True), (
        f"expected exactly one winner, got a={a!r} b={b!r}"
    )
    assert probe["started_ids"].count(qid) == 1, (
        f"_execute_import ran {probe['started_ids'].count(qid)} times for qid={qid}"
    )
    assert _get_status(fresh_db, qid) == "imported"


# ───────────────────── manual retry during import ─────────────────────


def test_retry_during_import_does_not_start_duplicate_worker(fresh_db, monkeypatch):
    """The retry endpoint's UPDATE only matches status IN ('failed','partial').
    A row currently 'importing' is therefore NOT reset to 'pending', and the
    subsequent _guarded_execute_import call will lose its claim."""
    import main
    import import_execute

    import_execute._IMPORT_SEM = asyncio.Semaphore(2)

    qid = _insert_queue_row(fresh_db, status="pending")
    # Simulate an in-progress import by pre-claiming.
    with main.get_db() as db:
        assert main.claim_import_queue_row(db, qid, "active-owner") is True
    assert _get_status(fresh_db, qid) == "importing"

    # Now simulate the retry endpoint's SQL (routers/import_.py:169-172).
    with main.get_db() as db:
        cur = db.execute(
            "UPDATE import_queue SET status='pending'"
            " WHERE id=? AND status IN ('failed','partial')",
            (qid,),
        )
    assert cur.rowcount == 0, "retry should not reset an already-importing row"
    assert _get_status(fresh_db, qid) == "importing"

    # And a second worker that tries to start loses its claim.
    probe = {"running": 0, "peak": 0, "started_ids": []}
    _install_fake_execute_import(monkeypatch, probe)

    async def _second_worker():
        return await main._guarded_execute_import(qid)

    result = _run(_second_worker())

    assert result is False, "second worker must fail claim, not duplicate the import"
    assert probe["started_ids"] == [], "no _execute_import call should have fired"


# ───────────────────── stuck-retry vs auto-import ─────────────────────


def test_stuck_retry_and_auto_import_cannot_both_claim(fresh_db, monkeypatch):
    """Simulates the two background paths (stuck-retry loop + qbit-complete
    auto-import) calling _guarded_execute_import on the same queue_id at
    roughly the same time. Only one should actually run."""
    import main
    import import_execute

    import_execute._IMPORT_SEM = asyncio.Semaphore(2)

    qid = _insert_queue_row(fresh_db, status="pending")
    probe = {"running": 0, "peak": 0, "started_ids": []}
    _install_fake_execute_import(monkeypatch, probe)

    async def _stuck_retry_path():
        # Mimics main.py:3418   asyncio.create_task(_process_auto_import(qid))
        await main._process_auto_import(qid)

    async def _auto_import_path():
        # Mimics main.py:3478   asyncio.create_task(_process_auto_import(qid))
        await main._process_auto_import(qid)

    async def _race():
        await asyncio.gather(_stuck_retry_path(), _auto_import_path())

    _run(_race())

    assert probe["started_ids"].count(qid) == 1, (
        f"queue_id {qid} was processed {probe['started_ids'].count(qid)} times; expected 1"
    )
    assert _get_status(fresh_db, qid) == "imported"


# ───────────────────── happy path ─────────────────────


def test_single_import_happy_path_still_works(fresh_db, monkeypatch):
    """With no contention, a single _guarded_execute_import call runs
    _execute_import exactly once and leaves the row in 'imported'."""
    import main
    import import_execute

    import_execute._IMPORT_SEM = asyncio.Semaphore(2)

    qid = _insert_queue_row(fresh_db)
    probe = {"running": 0, "peak": 0, "started_ids": []}
    _install_fake_execute_import(monkeypatch, probe)

    async def _single():
        return await main._guarded_execute_import(qid)

    result = _run(_single())

    assert result is True
    assert probe["started_ids"] == [qid]
    assert probe["peak"] == 1
    assert _get_status(fresh_db, qid) == "imported"


def test_cancellation_while_waiting_for_capacity_does_not_claim(
    fresh_db,
    monkeypatch,
):
    """A queued waiter must remain pending and unleased when cancelled."""
    import import_execute
    import main

    import_execute._IMPORT_SEM = asyncio.Semaphore(1)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _blocking_execute(queue_id, *args, **kwargs):
        from import_lease import transition_import_queue_row

        del args
        entered.set()
        await release.wait()
        with main.get_db() as db:
            transition_import_queue_row(
                db,
                queue_id,
                kwargs["lease_owner"],
                "imported",
            )
        return True

    monkeypatch.setattr(import_execute, "_execute_import", _blocking_execute)
    first = _insert_queue_row(fresh_db, download_id="capacity-first")
    second = _insert_queue_row(fresh_db, download_id="capacity-second")

    async def _exercise():
        first_task = asyncio.create_task(main._guarded_execute_import(first))
        await entered.wait()
        second_task = asyncio.create_task(main._guarded_execute_import(second))
        await asyncio.sleep(0)
        second_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second_task
        assert _get_queue_state(fresh_db, second) == ("pending", None, None)
        release.set()
        assert await first_task is True

    _run(_exercise())


def test_owner_a_cannot_mutate_after_expiry_and_owner_b_reclaim(fresh_db):
    """Every late owner-A operation loses after recovery and B's claim."""
    from import_lease import (
        claim_import_queue_row,
        owns_import_queue_lease,
        recover_expired_import_leases,
        refresh_import_queue_lease,
        release_import_queue_lease,
        transition_import_queue_row,
    )

    qid = _insert_queue_row(fresh_db, status="pending")
    with sqlite3.connect(fresh_db) as db:
        db.execute(
            "INSERT INTO import_queue_files(queue_id, filename, status)"
            " VALUES(?, 'review.cbz', 'needs_review')",
            (qid,),
        )
    with sqlite3.connect(fresh_db) as db:
        assert claim_import_queue_row(db, qid, "owner-a")
    with sqlite3.connect(fresh_db) as db:
        db.execute(
            "UPDATE import_queue SET lease_expires_at=datetime('now','-1 second')"
            " WHERE id=?",
            (qid,),
        )
    with sqlite3.connect(fresh_db) as db:
        db.row_factory = sqlite3.Row
        assert recover_expired_import_leases(db) == 1
        assert claim_import_queue_row(db, qid, "owner-b")
    with sqlite3.connect(fresh_db) as db:
        assert not refresh_import_queue_lease(db, qid, "owner-a")
        assert not owns_import_queue_lease(db, qid, "owner-a")
        assert not release_import_queue_lease(db, qid, "owner-a")
        for final_status in ("failed", "pending", "partial", "imported"):
            assert not transition_import_queue_row(
                db,
                qid,
                "owner-a",
                final_status,
            )
    assert _get_queue_state(fresh_db, qid)[0:2] == ("importing", "owner-b")
    with sqlite3.connect(fresh_db) as db:
        assert db.execute(
            "SELECT status FROM import_queue_files WHERE queue_id=?",
            (qid,),
        ).fetchone() == ("needs_review",)


def test_randomized_10k_claim_vs_cleanup_cas_race(fresh_db):
    """Real WAL writers cannot let actual cleanup fail a claimed row."""
    from import_lease import claim_import_queue_row
    from tasks import _fail_stale_queue_and_reset_volume

    row_count = 10_000
    with sqlite3.connect(fresh_db) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        db.executemany(
            "INSERT INTO import_queue("
            "series_id, download_id, torrent_name, status, created_at"
            ") VALUES(1, ?, ?, 'pending', datetime('now','-40 days'))",
            ((f"race-{i}", f"race-{i}") for i in range(row_count)),
        )
        db.executemany(
            "INSERT INTO volumes("
            "series_id, volume_num, status, grabbed_at, download_id, source_url"
            ") VALUES(1, ?, 'grabbed', datetime('now'), ?, ?)",
            (
                (float(i), f"race-{i}", f"race-{i}")
                for i in range(row_count)
            ),
        )
        queue_rows = db.execute(
            "SELECT id, download_id FROM import_queue"
            " WHERE download_id LIKE 'race-%' ORDER BY id"
        ).fetchall()
        queue_ids = [row[0] for row in queue_rows]
        download_id_by_queue = {row[0]: row[1] for row in queue_rows}

    barrier = threading.Barrier(3)
    errors: list[BaseException] = []
    latencies_ms: list[float] = []
    claim_winners: dict[int, list[str]] = defaultdict(list)
    cleanup_winners: set[int] = set()
    result_lock = threading.Lock()

    def _connection() -> sqlite3.Connection:
        db = sqlite3.connect(fresh_db, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA synchronous=NORMAL")
        return db

    def _claim_worker(label: str, seed: int) -> None:
        ids = list(queue_ids)
        random.Random(seed).shuffle(ids)
        try:
            with _connection() as db:
                barrier.wait()
                for queue_id in ids:
                    started = time.perf_counter()
                    won = claim_import_queue_row(
                        db,
                        queue_id,
                        f"{label}-{queue_id}",
                    )
                    db.commit()
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    with result_lock:
                        latencies_ms.append(elapsed_ms)
                        if won:
                            claim_winners[queue_id].append(label)
        except BaseException as exc:
            with result_lock:
                errors.append(exc)

    def _cleanup_worker() -> None:
        ids = list(queue_ids)
        random.Random(3003).shuffle(ids)
        try:
            with _connection() as db:
                barrier.wait()
                for queue_id in ids:
                    started = time.perf_counter()
                    observed = db.execute(
                        "SELECT status, lease_owner, download_id, series_id"
                        " FROM import_queue WHERE id=?",
                        (queue_id,),
                    ).fetchone()
                    won = bool(
                        observed
                        and observed["status"] in ("pending", "partial")
                        and observed["lease_owner"] is None
                        and _fail_stale_queue_and_reset_volume(
                            db,
                            queue_id=queue_id,
                            observed_status=observed["status"],
                            download_id=observed["download_id"],
                            series_id=observed["series_id"],
                        )
                    )
                    db.commit()
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    with result_lock:
                        latencies_ms.append(elapsed_ms)
                        if won:
                            cleanup_winners.add(queue_id)
        except BaseException as exc:
            with result_lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=_claim_worker, args=("owner-a", 1001)),
        threading.Thread(target=_claim_worker, args=("owner-b", 2002)),
        threading.Thread(target=_cleanup_worker),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert all(not thread.is_alive() for thread in threads)
    assert not errors, errors

    # Correctness gate: parent CAS and its domain reset must agree.
    assert all(len(owners) <= 1 for owners in claim_winners.values())
    assert claim_winners.keys().isdisjoint(cleanup_winners)
    assert len(claim_winners) + len(cleanup_winners) == row_count

    with sqlite3.connect(fresh_db) as db:
        final_rows = {
            row[0]: (row[1], row[2])
            for row in db.execute(
                "SELECT id, status, lease_owner FROM import_queue"
                " WHERE download_id LIKE 'race-%'"
            ).fetchall()
        }
        volume_rows = {
            row[0]: (row[1], row[2])
            for row in db.execute(
                "SELECT source_url, status, download_id FROM volumes"
                " WHERE source_url LIKE 'race-%'"
            ).fetchall()
        }
    for queue_id, owners in claim_winners.items():
        assert final_rows[queue_id] == (
            "importing",
            f"{owners[0]}-{queue_id}",
        )
        download_id = download_id_by_queue[queue_id]
        assert volume_rows[download_id] == ("grabbed", download_id)
    for queue_id in cleanup_winners:
        assert final_rows[queue_id] == ("failed", None)
        download_id = download_id_by_queue[queue_id]
        assert volume_rows[download_id] == ("wanted", None)

    # Performance gate is intentionally evaluated only after correctness.
    ordered = sorted(latencies_ms)
    p99_ms = ordered[math.ceil(len(ordered) * 0.99) - 1]
    print(
        "10k lease race:"
        f" claims={len(claim_winners)} cleanup={len(cleanup_winners)}"
        f" mean_ms={statistics.fmean(ordered):.3f}"
        f" p99_ms={p99_ms:.3f} max_ms={ordered[-1]:.3f}"
    )
    assert not math.isnan(statistics.fmean(ordered))
    assert p99_ms < 100, f"p99 committed operation latency was {p99_ms:.2f} ms"
