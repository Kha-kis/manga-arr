"""Durability and concurrency tests for the volume-file deletion journal."""

from __future__ import annotations

import asyncio
import os
import signal
import sqlite3
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def deletion_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    import main
    import shared

    db_path = tmp_path / "deletion.db"
    library_root = tmp_path / "library"
    library_root.mkdir()
    file_path = library_root / "Journal Series v01.cbz"
    file_path.write_bytes(b"journal-volume-payload")

    monkeypatch.setattr(main, "DB_PATH", str(db_path))
    monkeypatch.setattr(shared, "DB_PATH", str(db_path))
    main.init_db()
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT OR REPLACE INTO root_folders(id, path, is_default)"
            " VALUES(1, ?, 1)",
            (str(library_root),),
        )
        db.execute(
            "INSERT INTO series(id, title, search_pattern, edition_type,"
            " root_folder_id) VALUES(1, 'Journal Series', 'Journal Series',"
            " 'standard', 1)"
        )
        db.execute(
            """
            INSERT INTO volumes(
                id, series_id, volume_num, status, import_path, download_id,
                download_client_id, grabbed_at, source_url, torrent_name,
                indexer, protocol, client, release_group, size_bytes, quality,
                imported_at
            ) VALUES(
                11, 1, 1.0, 'downloaded', ?, 'journal-download', 41,
                CURRENT_TIMESTAMP, 'https://example.invalid/release',
                'Journal Series v01', 'Indexer', 'torrent', 'Qbit',
                'Group', 22, 'cbz', CURRENT_TIMESTAMP
            )
            """,
            (str(file_path),),
        )
        db.execute(
            """
            INSERT INTO chapters(
                id, series_id, volume_id, chapter_num, status, import_path,
                download_id, download_client_id, torrent_name, indexer,
                protocol, client, release_group
            ) VALUES(
                101, 1, 11, 1.0, 'downloaded', ?, 'journal-download',
                41, 'Journal Series c001', 'Indexer', 'torrent', 'Qbit',
                'Group'
            )
            """,
            (str(file_path),),
        )
    return {
        "db_path": str(db_path),
        "file_path": str(file_path),
        "library_root": str(library_root),
    }


def _journal_state(db_path: str) -> tuple[str, str, str]:
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT state, claim_path, diagnostic"
            " FROM volume_file_deletions ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    return row


def _journal_id(db_path: str) -> int:
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT id FROM volume_file_deletions ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    return int(row[0])


def _fork_context() -> Any:
    import multiprocessing

    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("SIGKILL recovery test requires multiprocessing fork")
    return multiprocessing.get_context("fork")


def _kill_after_reservation() -> None:
    import volume_file_deletion

    reservation = volume_file_deletion.reserve_volume_file_deletion(1, 11)
    if reservation.status != "reserved":
        raise RuntimeError(f"reservation failed before SIGKILL: {reservation}")
    os.kill(os.getpid(), signal.SIGKILL)


def _kill_before_unlink(journal_id: int) -> None:
    import volume_file_deletion

    def kill_process(_path: str) -> None:
        os.kill(os.getpid(), signal.SIGKILL)

    volume_file_deletion._unlink_claim = kill_process
    volume_file_deletion.replay_volume_file_deletion(journal_id)


def _kill_after_unlink(journal_id: int) -> None:
    import volume_file_deletion

    real_fsync = volume_file_deletion._fsync_directory
    fsync_count = 0

    def kill_before_second_fsync(path: str) -> None:
        nonlocal fsync_count
        fsync_count += 1
        if fsync_count == 2:
            os.kill(os.getpid(), signal.SIGKILL)
        real_fsync(path)

    volume_file_deletion._fsync_directory = kill_before_second_fsync
    volume_file_deletion.replay_volume_file_deletion(journal_id)


def test_reservation_resets_db_and_series_fence_blocks_import_claim(
    deletion_env: dict[str, object],
) -> None:
    """The DB reservation wins before claim and remains the admission fence."""
    import volume_file_deletion
    from import_lease import claim_import_queue_row

    db_path = str(deletion_env["db_path"])
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO import_queue(id, series_id, volume_num, status)"
            " VALUES(71, 1, 1.0, 'pending')"
        )

    reservation = volume_file_deletion.reserve_volume_file_deletion(1, 11)

    assert reservation.status == "reserved"
    assert reservation.journal_id is not None
    assert Path(str(deletion_env["file_path"])).exists()
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT status, import_path, download_id, download_client_id,"
            " size_bytes, quality"
            " FROM volumes WHERE id=11"
        ).fetchone() == ("wanted", None, None, None, None, None)
        assert db.execute(
            "SELECT status, import_path, download_id, download_client_id"
            " FROM chapters WHERE id=101"
        ).fetchone() == ("wanted", None, None, None)
        assert not claim_import_queue_row(db, 71, "blocked-owner")

    assert (
        volume_file_deletion.replay_volume_file_deletion(
            reservation.journal_id
        )
        == "completed"
    )
    with sqlite3.connect(db_path) as db:
        assert claim_import_queue_row(db, 71, "terminal-owner")
    assert not Path(str(deletion_env["file_path"])).exists()


def test_owner_only_change_during_inspection_loses_snapshot_cas(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reassigned acquisition owner cannot be reset by a stale inspection."""
    import volume_file_deletion

    db_path = str(deletion_env["db_path"])
    real_inspect = volume_file_deletion.inspect_volume_file_deletion

    def inspect_then_reassign(
        series_id: int,
        volume_id: int,
    ) -> volume_file_deletion.DeletionInspection | None:
        inspection = real_inspect(series_id, volume_id)
        with sqlite3.connect(db_path) as db:
            db.execute(
                "UPDATE volumes SET download_client_id=42"
                " WHERE id=? AND series_id=?",
                (volume_id, series_id),
            )
        return inspection

    monkeypatch.setattr(
        volume_file_deletion,
        "inspect_volume_file_deletion",
        inspect_then_reassign,
    )

    reservation = volume_file_deletion.reserve_volume_file_deletion(1, 11)

    assert reservation.status == "changed"
    assert Path(str(deletion_env["file_path"])).exists()
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT status,import_path,download_client_id FROM volumes"
            " WHERE id=11"
        ).fetchone() == (
            "downloaded",
            str(deletion_env["file_path"]),
            42,
        )
        assert db.execute(
            "SELECT status,download_client_id FROM chapters WHERE id=101"
        ).fetchone() == ("downloaded", 41)
        assert db.execute(
            "SELECT COUNT(*) FROM volume_file_deletions"
        ).fetchone() == (0,)


def test_reservation_sets_full_synchronous_before_writer_transaction(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The DB-first deletion fence is durable before filesystem mutation."""
    import volume_file_deletion

    statements: list[str] = []
    real_get_db = volume_file_deletion.get_db

    @contextmanager
    def traced_get_db() -> Iterator[sqlite3.Connection]:
        with real_get_db() as db:
            db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(volume_file_deletion, "get_db", traced_get_db)

    reservation = volume_file_deletion.reserve_volume_file_deletion(1, 11)

    assert reservation.status == "reserved"
    normalized = [statement.strip().upper() for statement in statements]
    full_index = normalized.index("PRAGMA SYNCHRONOUS=FULL")
    begin_index = normalized.index("BEGIN IMMEDIATE")
    insert_index = next(
        index
        for index, statement in enumerate(normalized)
        if statement.startswith("INSERT INTO VOLUME_FILE_DELETIONS")
    )
    assert full_index < begin_index < insert_index


@pytest.mark.parametrize(
    ("range_start", "range_end", "pack_type"),
    [(0.5, 1.5, "volume_range"), (None, None, "complete")],
    ids=["volume-range", "complete-pack"],
)
def test_prejournal_file_coverage_blocks_deletion_reservation(
    deletion_env: dict[str, object],
    range_start: float | None,
    range_end: float | None,
    pack_type: str,
) -> None:
    """File-level range and complete-pack plans retain import authority."""
    import volume_file_deletion

    db_path = str(deletion_env["db_path"])
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO import_queue("
            "id, series_id, download_id, volume_num, status, lease_owner"
            ") VALUES(72, 1, 'different-download', 9.0, 'importing',"
            " 'pre-journal-owner')"
        )
        db.execute(
            """
            INSERT INTO import_queue_files(
                queue_id, filename, proposed_volume,
                proposed_volume_range_start, proposed_volume_range_end,
                proposed_pack_type
            ) VALUES(72, 'planned-pack.cbz', 9.0, ?, ?, ?)
            """,
            (range_start, range_end, pack_type),
        )

    reservation = volume_file_deletion.reserve_volume_file_deletion(1, 11)

    assert reservation.status == "import_in_progress"
    assert Path(str(deletion_env["file_path"])).exists()
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT status, import_path FROM volumes WHERE id=11"
        ).fetchone() == (
            "downloaded",
            str(deletion_env["file_path"]),
        )
        assert db.execute(
            "SELECT COUNT(*) FROM volume_file_deletions"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("phase", ["hash", "rename", "unlink"])
def test_slow_filesystem_phases_never_hold_sqlite_writer(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    """Hash, tombstone claim, and unlink all allow an independent writer."""
    import volume_file_deletion

    phase_started = threading.Event()
    release_phase = threading.Event()
    if phase == "hash":
        real_phase = volume_file_deletion._sha256_fd

        def paused_hash(descriptor: int) -> str:
            phase_started.set()
            assert release_phase.wait(timeout=5)
            return real_phase(descriptor)

        monkeypatch.setattr(volume_file_deletion, "_sha256_fd", paused_hash)
    elif phase == "rename":
        real_phase = volume_file_deletion._rename_noreplace

        def paused_rename(source: str, destination: str) -> None:
            phase_started.set()
            assert release_phase.wait(timeout=5)
            real_phase(source, destination)

        monkeypatch.setattr(
            volume_file_deletion,
            "_rename_noreplace",
            paused_rename,
        )
    else:
        real_phase = volume_file_deletion._unlink_claim

        def paused_unlink(path: str) -> None:
            phase_started.set()
            assert release_phase.wait(timeout=5)
            real_phase(path)

        monkeypatch.setattr(volume_file_deletion, "_unlink_claim", paused_unlink)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(volume_file_deletion.delete_volume_file, 1, 11)
        assert phase_started.wait(timeout=3)
        try:
            with sqlite3.connect(
                str(deletion_env["db_path"]),
                timeout=0.05,
            ) as writer:
                writer.execute("PRAGMA busy_timeout=50")
                writer.execute(
                    "UPDATE series SET description=? WHERE id=1",
                    (f"writer-during-{phase}",),
                )
        finally:
            release_phase.set()
        result = future.result(timeout=10)

    assert result.status == "complete"
    with sqlite3.connect(str(deletion_env["db_path"])) as db:
        assert db.execute(
            "SELECT description FROM series WHERE id=1"
        ).fetchone()[0] == f"writer-during-{phase}"


def test_power_safe_claim_and_unlink_fsync_order(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each namespace mutation is fsynced before the next durability boundary."""
    import volume_file_deletion

    reservation = volume_file_deletion.reserve_volume_file_deletion(1, 11)
    assert reservation.journal_id is not None
    calls: list[str] = []
    real_rename = volume_file_deletion._rename_noreplace
    real_fsync = volume_file_deletion._fsync_directory
    real_unlink = volume_file_deletion._unlink_claim
    real_complete = volume_file_deletion._complete_journal

    def ordered_rename(source: str, destination: str) -> None:
        calls.append("rename")
        real_rename(source, destination)

    def ordered_fsync(path: str) -> None:
        calls.append("fsync")
        real_fsync(path)

    def ordered_unlink(path: str) -> None:
        calls.append("unlink")
        real_unlink(path)

    def ordered_complete(journal, *, deleted: bool):
        calls.append("db_complete")
        return real_complete(journal, deleted=deleted)

    monkeypatch.setattr(volume_file_deletion, "_rename_noreplace", ordered_rename)
    monkeypatch.setattr(volume_file_deletion, "_fsync_directory", ordered_fsync)
    monkeypatch.setattr(volume_file_deletion, "_unlink_claim", ordered_unlink)
    monkeypatch.setattr(volume_file_deletion, "_complete_journal", ordered_complete)

    outcome = volume_file_deletion.replay_volume_file_deletion(
        reservation.journal_id
    )

    assert outcome == "completed"
    assert calls == ["rename", "fsync", "unlink", "fsync", "db_complete"]


def test_post_claim_mismatch_restores_without_clobber_and_stays_active(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A race after verification restores the claimed path and fails closed."""
    import volume_file_deletion

    reservation = volume_file_deletion.reserve_volume_file_deletion(1, 11)
    assert reservation.journal_id is not None
    target_path = Path(str(deletion_env["file_path"]))
    original = target_path.read_bytes()
    real_rename = volume_file_deletion._rename_noreplace
    first_rename = True

    def mutate_after_claim(source: str, destination: str) -> None:
        nonlocal first_rename
        real_rename(source, destination)
        if first_rename:
            first_rename = False
            Path(destination).write_bytes(b"x" * len(original))

    monkeypatch.setattr(
        volume_file_deletion,
        "_rename_noreplace",
        mutate_after_claim,
    )

    outcome = volume_file_deletion.replay_volume_file_deletion(
        reservation.journal_id
    )

    assert outcome == "blocked"
    assert target_path.exists()
    state, claim_path, diagnostic = _journal_state(
        str(deletion_env["db_path"])
    )
    assert state == "active"
    assert not Path(claim_path).exists()
    assert "claim restored without replacing" in diagnostic


def test_recreated_target_is_never_clobbered_or_claim_unlinked(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replacement arriving after claim leaves both paths for diagnosis."""
    import volume_file_deletion

    reservation = volume_file_deletion.reserve_volume_file_deletion(1, 11)
    assert reservation.journal_id is not None
    target_path = Path(str(deletion_env["file_path"]))
    real_rename = volume_file_deletion._rename_noreplace
    first_rename = True

    def recreate_after_claim(source: str, destination: str) -> None:
        nonlocal first_rename
        real_rename(source, destination)
        if first_rename:
            first_rename = False
            target_path.write_bytes(b"replacement")

    monkeypatch.setattr(
        volume_file_deletion,
        "_rename_noreplace",
        recreate_after_claim,
    )

    outcome = volume_file_deletion.replay_volume_file_deletion(
        reservation.journal_id
    )

    state, claim_path, diagnostic = _journal_state(
        str(deletion_env["db_path"])
    )
    assert outcome == "blocked"
    assert state == "active"
    assert target_path.read_bytes() == b"replacement"
    assert Path(claim_path).read_bytes() == b"journal-volume-payload"
    assert "refusing to remove the claim or clobber" in diagnostic


@pytest.mark.parametrize(
    ("boundary", "target_exists", "claim_exists"),
    [
        ("journal_commit", True, False),
        ("rename", False, True),
        ("unlink", False, False),
    ],
)
def test_sigkill_at_deletion_boundaries_replays_to_terminal_once(
    deletion_env: dict[str, object],
    boundary: str,
    target_exists: bool,
    claim_exists: bool,
) -> None:
    """A process death at every namespace boundary converges on recovery."""
    import volume_file_deletion

    context = _fork_context()
    if boundary == "journal_commit":
        process = context.Process(target=_kill_after_reservation)
    else:
        reservation = volume_file_deletion.reserve_volume_file_deletion(1, 11)
        assert reservation.journal_id is not None
        target = _kill_before_unlink if boundary == "rename" else _kill_after_unlink
        process = context.Process(target=target, args=(reservation.journal_id,))
    process.start()
    process.join(timeout=10)
    assert not process.is_alive()
    assert process.exitcode == -signal.SIGKILL

    db_path = str(deletion_env["db_path"])
    journal_id = _journal_id(db_path)
    state, claim_path, _ = _journal_state(db_path)
    assert state == "active"
    assert Path(str(deletion_env["file_path"])).exists() is target_exists
    assert Path(claim_path).exists() is claim_exists

    assert volume_file_deletion.replay_volume_file_deletion(journal_id) == "completed"
    assert volume_file_deletion.replay_volume_file_deletion(journal_id) == "terminal"
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT state FROM volume_file_deletions WHERE id=?",
            (journal_id,),
        ).fetchone() == ("completed",)
        assert db.execute(
            "SELECT COUNT(*) FROM history WHERE event_type='file_deleted'"
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='delete'"
        ).fetchone()[0] == 1


def test_missing_target_fsyncs_parent_and_completes(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file absent before reservation still gets a durable terminal record."""
    import volume_file_deletion

    Path(str(deletion_env["file_path"])).unlink()
    reservation = volume_file_deletion.reserve_volume_file_deletion(1, 11)
    assert reservation.journal_id is not None
    fsynced: list[str] = []
    real_fsync = volume_file_deletion._fsync_directory

    def record_fsync(path: str) -> None:
        fsynced.append(path)
        real_fsync(path)

    monkeypatch.setattr(volume_file_deletion, "_fsync_directory", record_fsync)

    assert (
        volume_file_deletion.replay_volume_file_deletion(
            reservation.journal_id
        )
        == "completed"
    )
    assert fsynced == [str(deletion_env["library_root"])]
    with sqlite3.connect(str(deletion_env["db_path"])) as db:
        data = db.execute(
            "SELECT data FROM history WHERE event_type='file_deleted'"
        ).fetchone()[0]
    assert '"deleted": false' in data


def test_concurrent_replay_reports_complete_after_other_replayer_finishes(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A losing route replay must honor the winner's terminal journal state."""
    import volume_file_deletion

    reservation = volume_file_deletion.reserve_volume_file_deletion(1, 11)
    journal_id = reservation.journal_id
    assert journal_id is not None
    first_unlink_started = threading.Event()
    second_unlink_started = threading.Event()
    winner_finished = threading.Event()
    unlink_lock = threading.Lock()
    unlink_calls = 0
    real_unlink = volume_file_deletion._unlink_claim

    def coordinated_unlink(path: str) -> None:
        nonlocal unlink_calls
        with unlink_lock:
            unlink_calls += 1
            call_number = unlink_calls
        if call_number == 1:
            first_unlink_started.set()
            assert second_unlink_started.wait(timeout=5)
        else:
            second_unlink_started.set()
            assert winner_finished.wait(timeout=5)
        real_unlink(path)

    monkeypatch.setattr(
        volume_file_deletion,
        "_unlink_claim",
        coordinated_unlink,
    )

    def replay_winner() -> str:
        try:
            return volume_file_deletion.replay_volume_file_deletion(journal_id)
        finally:
            winner_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(replay_winner)
        assert first_unlink_started.wait(timeout=5)
        loser = pool.submit(volume_file_deletion.delete_volume_file, 1, 11)
        assert winner.result(timeout=5) == "completed"
        loser_result = loser.result(timeout=5)

    assert loser_result.status == "complete"
    assert _journal_state(str(deletion_env["db_path"]))[0] == "completed"
    with sqlite3.connect(str(deletion_env["db_path"])) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM history WHERE event_type='file_deleted'"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='delete'"
        ).fetchone() == (1,)


def test_bounded_replay_settles_inflight_operation_before_cancellation(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation settles only the current filesystem operation."""
    import volume_file_deletion

    reservation = volume_file_deletion.reserve_volume_file_deletion(1, 11)
    assert reservation.journal_id is not None
    unlink_started = threading.Event()
    release_unlink = threading.Event()
    real_unlink = volume_file_deletion._unlink_claim

    def paused_unlink(path: str) -> None:
        unlink_started.set()
        assert release_unlink.wait(timeout=5)
        real_unlink(path)

    monkeypatch.setattr(volume_file_deletion, "_unlink_claim", paused_unlink)

    async def scenario() -> None:
        task = asyncio.create_task(
            volume_file_deletion.replay_volume_file_deletions(max_rows=1)
        )
        assert await asyncio.to_thread(unlink_started.wait, 3)
        task.cancel()
        release_unlink.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert _journal_state(str(deletion_env["db_path"]))[0] == "completed"


def test_startup_deletion_recovery_precedes_publication_and_import_producers() -> None:
    """Lifespan ordering keeps producers behind both durable recovery passes."""
    import inspect
    import main

    source = inspect.getsource(main.lifespan)
    deletion_recovery = source.index("drain_active_volume_file_deletions")
    publication_recovery = source.index("drain_active_import_publications")
    import_admission = source.index("initialize_import_semaphore")
    first_producer = source.index("create_background_task(rss_loop()")

    assert (
        deletion_recovery
        < publication_recovery
        < import_admission
        < first_producer
    )
