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
import os
import sqlite3
import tempfile

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


def _get_status(db_path, queue_id):
    with sqlite3.connect(db_path) as c:
        row = c.execute(
            "SELECT status FROM import_queue WHERE id=?", (queue_id,)
        ).fetchone()
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
        assert main.claim_import_queue_row(db, qid) is True
    assert _get_status(fresh_db, qid) == "importing"


def test_claim_succeeds_on_partial(fresh_db):
    import main

    qid = _insert_queue_row(fresh_db, status="partial")
    with main.get_db() as db:
        assert main.claim_import_queue_row(db, qid) is True
    assert _get_status(fresh_db, qid) == "importing"


def test_claim_fails_when_already_importing(fresh_db):
    """Two workers call claim on the same row; only the first wins."""
    import main

    qid = _insert_queue_row(fresh_db, status="pending")
    with main.get_db() as db:
        first = main.claim_import_queue_row(db, qid)
    with main.get_db() as db:
        second = main.claim_import_queue_row(db, qid)
    assert first is True
    assert second is False, "second claim should have lost the race"
    assert _get_status(fresh_db, qid) == "importing"


def test_claim_fails_on_terminal_states(fresh_db):
    import main

    for terminal in ("imported", "failed", "skipped"):
        qid = _insert_queue_row(fresh_db, download_id=f"dl-{terminal}", status=terminal)
        with main.get_db() as db:
            assert main.claim_import_queue_row(db, qid) is False, (
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
        probe["running"] += 1
        probe["started_ids"].append(queue_id)
        probe["peak"] = max(probe["peak"], probe["running"])
        await asyncio.sleep(0.05)
        probe["running"] -= 1
        with main.get_db() as db:
            db.execute(
                "UPDATE import_queue SET status='imported' WHERE id=? AND status='importing'",
                (queue_id,),
            )
        return True

    monkeypatch.setattr(import_execute, "_execute_import", _fake_execute_import)


def test_semaphore_bounds_concurrent_imports_to_two(fresh_db, monkeypatch):
    """Spawn 10 pending rows, kick off a worker for each, assert at most 2
    are ever in _execute_import simultaneously."""
    import main

    # Reset the semaphore so earlier tests don't leak state.
    import import_pipeline

    import_pipeline._IMPORT_SEM = asyncio.Semaphore(2)

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
    import import_pipeline

    import_pipeline._IMPORT_SEM = asyncio.Semaphore(2)

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
    import import_pipeline

    import_pipeline._IMPORT_SEM = asyncio.Semaphore(2)

    qid = _insert_queue_row(fresh_db, status="pending")
    # Simulate an in-progress import by pre-claiming.
    with main.get_db() as db:
        assert main.claim_import_queue_row(db, qid) is True
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
    import import_pipeline

    import_pipeline._IMPORT_SEM = asyncio.Semaphore(2)

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
    import import_pipeline

    import_pipeline._IMPORT_SEM = asyncio.Semaphore(2)

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
