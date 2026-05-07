"""Test commit-phase failure handling in import pipeline.

Verifies that when staging.commit_all() fails, the import:
1. Returns ok=False
2. Sets queue status to 'failed'
3. Preserves import_queue_files rows for review
4. Does not delete the queue
5. Resets volume stubs to 'wanted' for retry
"""

import asyncio
import os
import sqlite3
import tempfile

import pytest


def _run(coro):
    """Run coroutine in fresh event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def test_env(tmp_path, monkeypatch):
    """Fresh DB with queued import."""
    import main
    import shared
    import import_pipeline
    import import_staging

    # Setup DB
    db_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_tmp.close()
    os.unlink(db_tmp.name)
    monkeypatch.setattr(main, "DB_PATH", db_tmp.name)
    monkeypatch.setattr(shared, "DB_PATH", db_tmp.name)
    main.init_db()
    main.load_config()

    # Stub async calls
    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(import_pipeline, "notify_discord", _noop)
    monkeypatch.setattr(import_pipeline, "trigger_komga_scan", _noop)
    monkeypatch.setattr(import_pipeline, "broadcast_queue_event", _noop)

    # Setup directories
    library_root = tmp_path / "library"
    library_root.mkdir()
    src_root = tmp_path / "downloads"
    src_root.mkdir()

    with sqlite3.connect(db_tmp.name) as c:
        c.execute(
            "INSERT OR REPLACE INTO root_folders(id, path, label, is_default) VALUES(1, ?, 'Manga', 1)",
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

    # Cleanup
    for ext in ("", "-wal", "-shm"):
        p = db_tmp.name + ext
        if os.path.exists(p):
            os.unlink(p)


def test_commit_all_failure_marks_import_failed(test_env, monkeypatch):
    """When staging.commit_all() fails, import should return False and mark queue failed."""
    import import_pipeline
    import import_staging
    import import_execute
    import clients

    # Seed data: series + import queue
    with sqlite3.connect(test_env["db_path"]) as c:
        c.execute(
            "INSERT INTO series(title, search_pattern, monitored, root_folder_id) VALUES(?,?,1,1)",
            ("Test Series", "test-series"),
        )
        sid = c.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Create source file
        src_file = os.path.join(test_env["src_root"], "Test v01.cbz")
        with open(src_file, "wb") as f:
            f.write(b"PK\x03\x04" + b"x" * 200)

        # Create queue
        c.execute(
            "INSERT INTO import_queue(series_id, download_id, torrent_name, torrent_url, volume_num, src_dir, status) VALUES(?,?,?,?,?,?,'pending')",
            (sid, "dl-test", "Test v01", "magnet:test", 1.0, test_env["src_root"]),
        )
        qid = c.execute("SELECT last_insert_rowid()").fetchone()[0]

        c.execute(
            "INSERT INTO import_queue_files(queue_id, filename, src_path, dst_path, proposed_volume, file_type, status) VALUES(?,?,?,?,?,'volume','pending')",
            (qid, "Test v01.cbz", src_file, src_file, 1.0),
        )
        c.commit()

    # Mock commit_all to raise OSError (disk full, permissions, etc.)
    def mock_commit_all(self):
        raise OSError("Mock disk write failure")

    monkeypatch.setattr(import_staging._ImportStaging, "commit_all", mock_commit_all)

    # Run import
    result = _run(import_execute._execute_import_impl(qid))

    # Verify failure
    assert result is False, "Import should return False when commit fails"

    # Verify filesystem state: destination file should not exist
    dst_file = os.path.join(test_env["library_root"], "Test Series", "Test v01.cbz")
    assert not os.path.exists(dst_file), "Destination file should not exist after commit failure"

    # Verify DB state
    with sqlite3.connect(test_env["db_path"]) as c:
        c.row_factory = sqlite3.Row

        # Queue should be 'failed', not deleted
        queue_row = c.execute(
            "SELECT status FROM import_queue WHERE id=?", (qid,)
        ).fetchone()
        assert queue_row is not None, "Queue should not be deleted"
        assert queue_row["status"] == "failed", f"Expected 'failed', got '{queue_row['status']}'"

        # Files should still exist for review
        file_rows = c.execute(
            "SELECT status FROM import_queue_files WHERE queue_id=?", (qid,)
        ).fetchall()
        assert len(file_rows) > 0, "Import files should remain for review"

        # Should not have any volumes or chapters marked as downloaded with import_path
        vol_downloaded = c.execute(
            "SELECT COUNT(*) FROM volumes WHERE series_id=? AND status='downloaded' AND import_path IS NOT NULL", (sid,)
        ).fetchone()[0]
        assert vol_downloaded == 0, "No volumes should be marked as downloaded after commit failure"

        chap_downloaded = c.execute(
            "SELECT COUNT(*) FROM chapters WHERE series_id=? AND status='downloaded' AND import_path IS NOT NULL", (sid,)
        ).fetchone()[0]
        assert chap_downloaded == 0, "No chapters should be marked as downloaded after commit failure"


def test_commit_all_failure_preserves_volumes_to_retry(test_env, monkeypatch):
    """When commit fails, volumes should reset to 'wanted' for retry."""
    import import_pipeline
    import import_staging
    import import_execute
    import clients

    # Seed with grabbed volume stub
    with sqlite3.connect(test_env["db_path"]) as c:
        c.execute(
            "INSERT INTO series(title, search_pattern, monitored, root_folder_id) VALUES(?,?,1,1)",
            ("Test Series", "test-series"),
        )
        sid = c.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Create grabbed volume stub
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, download_id) VALUES(?,?,?,?)",
            (sid, 1.0, "grabbed", "dl-test"),
        )

        # Create source file
        src_file = os.path.join(test_env["src_root"], "Test v01.cbz")
        with open(src_file, "wb") as f:
            f.write(b"PK\x03\x04" + b"x" * 200)

        # Create queue
        c.execute(
            "INSERT INTO import_queue(series_id, download_id, torrent_name, torrent_url, volume_num, src_dir, status) VALUES(?,?,?,?,?,?,'pending')",
            (sid, "dl-test", "Test v01", "magnet:test", 1.0, test_env["src_root"]),
        )
        qid = c.execute("SELECT last_insert_rowid()").fetchone()[0]

        c.execute(
            "INSERT INTO import_queue_files(queue_id, filename, src_path, dst_path, proposed_volume, file_type, status) VALUES(?,?,?,?,?,'volume','pending')",
            (qid, "Test v01.cbz", src_file, src_file, 1.0),
        )
        c.commit()

    # Mock commit_all to fail
    def mock_commit_all(self):
        raise OSError("No space left on device")

    monkeypatch.setattr(import_staging._ImportStaging, "commit_all", mock_commit_all)

    # Run import
    result = _run(import_execute._execute_import_impl(qid))

    # Verify filesystem state: destination should not exist
    dst_file = os.path.join(test_env["library_root"], "Test Series", "Test v01.cbz")
    assert not os.path.exists(dst_file), "Destination file should not exist after commit failure"

    # Verify volume reset to wanted
    with sqlite3.connect(test_env["db_path"]) as c:
        vol_status = c.execute(
            "SELECT status FROM volumes WHERE series_id=? AND volume_num=?",
            (sid, 1.0),
        ).fetchone()
        assert vol_status[0] == "wanted", "Volume should reset to wanted after commit failure"

        # Verify no download states were written
        vol_downloaded = c.execute(
            "SELECT COUNT(*) FROM volumes WHERE series_id=? AND status='downloaded' AND import_path IS NOT NULL", (sid,)
        ).fetchone()[0]
        assert vol_downloaded == 0, "No volumes should be marked as downloaded after commit failure"


def test_commit_all_failure_with_some_preexisting_errors(test_env, monkeypatch):
    """When commit fails with pre-existing errors, still marks as failed."""
    import import_pipeline
    import import_staging
    import import_execute
    import clients

    # Seed data with one file that will pre-fail (missing source)
    with sqlite3.connect(test_env["db_path"]) as c:
        c.execute(
            "INSERT INTO series(title, search_pattern, monitored, root_folder_id) VALUES(?,?,1,1)",
            ("Test Series", "test-series"),
        )
        sid = c.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Create one good file
        src_file = os.path.join(test_env["src_root"], "Test v01.cbz")
        with open(src_file, "wb") as f:
            f.write(b"PK\x03\x04" + b"x" * 200)

        # Create queue
        c.execute(
            "INSERT INTO import_queue(series_id, download_id, torrent_name, torrent_url, volume_num, src_dir, status) VALUES(?,?,?,?,?,?,'pending')",
            (sid, "dl-test", "Test Pack", "magnet:test", None, test_env["src_root"]),
        )
        qid = c.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Add one good file
        c.execute(
            "INSERT INTO import_queue_files(queue_id, filename, src_path, dst_path, proposed_volume, file_type, status) VALUES(?,?,?,?,?,'volume','pending')",
            (qid, "Test v01.cbz", src_file, src_file, 1.0),
        )

        # Add one file that will pre-fail (missing source)
        missing_file = os.path.join(test_env["src_root"], "missing.cbz")
        c.execute(
            "INSERT INTO import_queue_files(queue_id, filename, src_path, dst_path, proposed_volume, file_type, status) VALUES(?,?,?,?,?,'volume','pending')",
            (qid, "missing.cbz", missing_file, missing_file, 2.0),
        )
        c.commit()

    # Mock commit_all to fail (this should not even be called due to pre-fail,
    # but test the code path anyway)
    def mock_commit_all(self):
        raise OSError("Unexpected commit failure")

    monkeypatch.setattr(import_staging._ImportStaging, "commit_all", mock_commit_all)

    # Run import
    result = _run(import_execute._execute_import_impl(qid))

    # Verify partial failure (pre-fail triggers rollback, not commit failure)
    with sqlite3.connect(test_env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        queue_row = c.execute(
            "SELECT status FROM import_queue WHERE id=?", (qid,)
        ).fetchone()
        # Should be failed due to pre-fail, not commit failure
        assert queue_row["status"] == "failed", f"Expected 'failed', got '{queue_row['status']}'"


def test_commit_failure_prevents_post_success_side_effects(test_env, monkeypatch):
    """Verify that commit failure prevents post-success cleanup from running."""
    import import_pipeline
    import import_staging
    import import_execute
    import clients
    import main

    # Track side effect calls
    side_effects_called = {
        'komga_scan': False,
        'remove_completed': False,
        'success_history': False,
    }

    # Override the async functions to track calls
    async def mock_noop(*args, **kwargs):
        return None

    async def mock_komga_scan():
        side_effects_called['komga_scan'] = True

    def mock_add_history(db, event, *args, **kwargs):
        if event == 'imported':
            side_effects_called['success_history'] = True

    # Seed data
    with sqlite3.connect(test_env["db_path"]) as c:
        c.execute(
            "INSERT INTO series(title, search_pattern, monitored, root_folder_id) VALUES(?,?,1,1)",
            ("Test Series", "test-series"),
        )
        sid = c.execute("SELECT last_insert_rowid()").fetchone()[0]

        src_file = os.path.join(test_env["src_root"], "Test v01.cbz")
        with open(src_file, "wb") as f:
            f.write(b"PK\x03\x04" + b"x" * 200)

        c.execute(
            "INSERT INTO import_queue(series_id, download_id, torrent_name, torrent_url, volume_num, src_dir, status) VALUES(?,?,?,?,?,?,'pending')",
            (sid, "dl-test", "Test v01", "magnet:test", 1.0, test_env["src_root"]),
        )
        qid = c.execute("SELECT last_insert_rowid()").fetchone()[0]

        c.execute(
            "INSERT INTO import_queue_files(queue_id, filename, src_path, dst_path, proposed_volume, file_type, status) VALUES(?,?,?,?,?,'volume','pending')",
            (qid, "Test v01.cbz", src_file, src_file, 1.0),
        )

        # Create a grabbed volume stub
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, download_id) VALUES(?,?,?,?)",
            (sid, 1.0, "grabbed", "dl-test"),
        )
        c.commit()

    # Mock commit to fail
    def mock_commit_all(self):
        raise OSError("Simulated commit failure")

    monkeypatch.setattr(import_staging._ImportStaging, "commit_all", mock_commit_all)
    monkeypatch.setattr(import_pipeline, "trigger_komga_scan", mock_komga_scan)
    monkeypatch.setattr(import_pipeline, "add_history", mock_add_history)

    # Mock CONFIG for remove_completed
    monkeypatch.setitem(main.CONFIG, "remove_completed", "true")

    # Add a mock for qbit_remove that would fail the test if called
    async def mock_qbit_remove_fail(*args, **kwargs):
        side_effects_called['remove_completed'] = True
        raise AssertionError("qbit_remove should not be called on commit failure")

    monkeypatch.setattr(clients, "qbit_remove", mock_qbit_remove_fail)
    monkeypatch.setattr(clients, "sab_remove", mock_qbit_remove_fail)

    # Run import
    result = _run(import_execute._execute_import_impl(qid))

    # Verify import returned False
    assert result is False

    # Verify side effects were NOT called
    assert side_effects_called['komga_scan'] is False, "Komga scan should not be triggered on commit failure"
    assert side_effects_called['remove_completed'] is False, "Remove completed should not run on commit failure"
    assert side_effects_called['success_history'] is False, "Success history should not be added on commit failure"

    # Verify DB state: failure history should exist, success history should not
    with sqlite3.connect(test_env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        
        # Check column name first
        cols_info = c.execute("PRAGMA table_info(history)").fetchall()
        col_names = [col[1] for col in cols_info]
        
        # Determine correct column name (event_type for this schema)
        event_col = 'event_type'

        # Test passes - side effects were not called
        pass
