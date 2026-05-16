"""Regression test for the import-pipeline DB lock contention fix.

Background: _execute_import_impl used to keep one `with get_db() as db:`
block open across the whole batch (planner + per-file file I/O + per-file
DB writes), with a SAVEPOINT held throughout. Multi-file imports with
CBR->CBZ conversion could hold the SQLite write lock for 30+ seconds -
concurrent writers hit busy_timeout and errored out with
`OperationalError: database is locked`.

The fix splits _execute_import_impl into three phases:
  - Phase 1 (short DB tx): build _ImportPlan, persist overrides, mark
    needs_review, mkdir dst_dir, close.
  - Phase 2 (NO DB held): _stage_files runs CBR->CBZ + ComicInfo
    injection without any SQLite connection open.
  - Phase 3 (short DB tx): _commit_import replays per-file writes.

This test pins that property structurally: while Phase 2 is paused
mid-flight, a concurrent SQLite writer with a short busy_timeout must
commit successfully. Without the fix the probe would block on the
held write lock until busy_timeout elapsed and raise OperationalError.
"""

import asyncio
import os
import sqlite3
import tempfile
import threading

import pytest


def _run(coro):
    """Run a coroutine in a fresh event loop and clean up afterwards
    so subsequent tests that use asyncio.get_event_loop() still work."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


@pytest.fixture
def lock_env(tmp_path, monkeypatch):
    """Fresh DB + library + source dir; fire-and-forget side effects
    stubbed. Mirrors test_import_atomicity.exec_env minus the parts
    this test doesn't need."""
    import main
    import shared

    db_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_tmp.close()
    os.unlink(db_tmp.name)
    monkeypatch.setattr(main, "DB_PATH", db_tmp.name)
    monkeypatch.setattr(shared, "DB_PATH", db_tmp.name)
    main.init_db()
    main.load_config()

    async def _noop_async(*a, **kw):
        return None

    monkeypatch.setattr(main, "notify_discord", _noop_async)
    monkeypatch.setattr(main, "trigger_komga_scan", _noop_async)
    monkeypatch.setattr(main, "broadcast_queue_event", _noop_async)

    library_root = tmp_path / "library"
    library_root.mkdir()
    src_root = tmp_path / "downloads"
    src_root.mkdir()

    with sqlite3.connect(db_tmp.name) as c:
        c.execute(
            "INSERT OR REPLACE INTO root_folders(id, path, label, is_default)"
            " VALUES(1, ?, 'Manga', 1)",
            (str(library_root),),
        )

    monkeypatch.setitem(main.CONFIG, "save_path", str(library_root))
    monkeypatch.setitem(shared.CONFIG, "save_path", str(library_root))
    monkeypatch.setitem(main.CONFIG, "import_mode", "copy")

    yield {
        "db_path": db_tmp.name,
        "library_root": str(library_root),
        "src_root": str(src_root),
    }

    for ext in ("", "-wal", "-shm"):
        p = db_tmp.name + ext
        if os.path.exists(p):
            os.unlink(p)


def _seed(db_path, src_root):
    """Seed a series + a single-file import_queue row so we have
    something to drive _execute_import_impl with. Returns (queue_id,
    series_id)."""
    name = "Test Series v01.cbz"
    src = os.path.join(src_root, name)
    # Real ZIP magic so _maybe_convert_to_cbz treats it as already CBZ
    # and the file passes safe_join_under + isfile checks.
    with open(src, "wb") as f:
        f.write(b"PK\x03\x04" + b"x" * 200)

    with sqlite3.connect(db_path) as c:
        c.execute(
            "INSERT INTO series(title, search_pattern, monitored, root_folder_id)"
            " VALUES(?,?,1,1)",
            ("Test Series", "test-series"),
        )
        sid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute(
            "INSERT INTO import_queue(series_id, download_id, torrent_name,"
            " torrent_url, volume_num, src_dir, status) VALUES(?,?,?,?,?,?,'pending')",
            (sid, "dl-test", "Test Series v01", "magnet:test", 1.0, src_root),
        )
        qid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute(
            "INSERT INTO import_queue_files(queue_id, filename, src_path,"
            " dst_path, proposed_volume, file_type, status)"
            " VALUES(?,?,?,?,?,'volume','pending')",
            (qid, name, src, src, 1.0),
        )
        c.commit()
    return qid, sid


def test_phase2_does_not_hold_db_write_lock(lock_env, monkeypatch):
    """While Phase 2 (file I/O via _stage_files) is paused, the SQLite
    write lock must be free.

    Pauses Phase 2 inside _try_inject_comicinfo (which _stage_files
    calls via asyncio.to_thread for every staged file) using a
    threading.Event. While paused, attempts a write from a separate
    sqlite3 connection with a 1-second busy_timeout.

    Passes a volume_overrides arg so the planner persists an UPDATE on
    import_queue_files before Phase 2 starts. Pre-fix that UPDATE fired
    inside the SAVEPOINT-wrapped loop, escalating the connection to a
    RESERVED write lock that stayed held throughout Phase 2; post-fix
    it fires in Phase 1's separate connection that closes before
    Phase 2.

    Pre-fix: write lock held through Phase 2 -> probe blocks for the
    1-second busy_timeout and raises OperationalError.
    Post-fix: probe succeeds immediately.
    """
    import import_staging
    import import_execute

    qid, sid = _seed(lock_env["db_path"], lock_env["src_root"])

    with sqlite3.connect(lock_env["db_path"]) as c:
        file_id = c.execute(
            "SELECT id FROM import_queue_files WHERE queue_id=?", (qid,)
        ).fetchone()[0]

    in_phase_2 = threading.Event()
    resume_phase_2 = threading.Event()

    real_inject = import_staging._try_inject_comicinfo

    def _slow_inject(*args, **kwargs):
        in_phase_2.set()
        if not resume_phase_2.wait(timeout=10):
            raise TimeoutError("test never resumed Phase 2")
        return real_inject(*args, **kwargs)

    monkeypatch.setattr(import_staging, "_try_inject_comicinfo", _slow_inject)

    async def _drive():
        import_task = asyncio.create_task(
            import_execute._execute_import_impl(
                qid,
                volume_overrides={file_id: 2.0},
            )
        )

        entered = await asyncio.to_thread(in_phase_2.wait, 10)
        assert entered, "import never reached Phase 2 (_try_inject_comicinfo)"

        def _probe_write():
            # If Phase 2 still held the SQLite write lock, this BEGIN
            # IMMEDIATE would block on busy_timeout (=1s) and then
            # raise OperationalError: database is locked.
            with sqlite3.connect(lock_env["db_path"], timeout=1.0) as c:
                c.execute("UPDATE series SET title='probe' WHERE id=?", (sid,))
                c.commit()

        try:
            await asyncio.to_thread(_probe_write)
        finally:
            # Release the import before leaving, even if the probe failed
            # against pre-fix code.
            resume_phase_2.set()
            await import_task

    _run(_drive())

    # Confirm the probe actually committed.
    with sqlite3.connect(lock_env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        title = c.execute("SELECT title FROM series WHERE id=?", (sid,)).fetchone()[
            "title"
        ]
    assert title == "probe", "probe write did not commit"
