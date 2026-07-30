"""Focused lease lifecycle contracts for durable import Phase 1."""

import asyncio
import sqlite3
import threading
from collections.abc import Coroutine, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def lease_db(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[str]:
    import import_execute
    import main
    import shared

    original_sem = import_execute._IMPORT_SEM
    original_sem_value = original_sem._value if original_sem is not None else None
    original_main_config = main.CONFIG
    original_main_values = dict(main.CONFIG)
    original_shared_config = shared.CONFIG
    original_shared_values = dict(shared.CONFIG)
    db_path = str(tmp_path / "leases.db")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(shared, "DB_PATH", db_path)
    import_execute._IMPORT_SEM = None
    main.init_db()
    main.load_config()
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO series(id, title, search_pattern)"
            " VALUES(1, 'Lease Series', 'Lease Series')"
        )
    try:
        yield db_path
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


def _queue(
    db_path: str,
    *,
    download_id: str,
    status: str = "pending",
    owner: str | None = None,
    expiry_sql: str | None = None,
) -> int:
    with sqlite3.connect(db_path) as db:
        if expiry_sql is None:
            cur = db.execute(
                "INSERT INTO import_queue("
                "series_id, download_id, torrent_name, status, lease_owner"
                ") VALUES(1, ?, ?, ?, ?)",
                (download_id, download_id, status, owner),
            )
        else:
            cur = db.execute(
                "INSERT INTO import_queue("
                "series_id, download_id, torrent_name, status,"
                " lease_owner, lease_expires_at"
                f") VALUES(1, ?, ?, ?, ?, {expiry_sql})",
                (download_id, download_id, status, owner),
            )
        queue_id = cur.lastrowid
        assert queue_id is not None
        return queue_id


def _state(db_path: str, queue_id: int) -> tuple[str, str | None, str | None]:
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT status, lease_owner, lease_expires_at"
            " FROM import_queue WHERE id=?",
            (queue_id,),
        ).fetchone()
    assert row is not None
    return row


def _pack_dir(download_id: str) -> Path:
    from import_pack_cleanup import pack_queue_creation_paths

    canonical, _ = pack_queue_creation_paths(
        download_id,
        "test-owner",
        download_client_id=None,
        protocol=None,
    )
    return Path(canonical)


def _ready_import(
    db_path: str,
    tmp_path: Path,
    *,
    download_id: str,
) -> tuple[int, Path]:
    library = tmp_path / f"{download_id}-library"
    source = tmp_path / f"{download_id}.cbz"
    library.mkdir()
    source.write_bytes(b"lease-test")
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT OR REPLACE INTO root_folders(id, path, label, is_default)"
            " VALUES(1, ?, 'Lease tests', 1)",
            (str(library),),
        )
        db.execute("UPDATE series SET root_folder_id=1 WHERE id=1")
    queue_id = _queue(db_path, download_id=download_id)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO import_queue_files("
            "queue_id, src_path, filename, status, file_type,"
            "proposed_volume, proposed_import_kind"
            ") VALUES(?, ?, ?, 'pending', 'volume', 1, 'volume')",
            (queue_id, str(source), source.name),
        )
    return queue_id, source


def test_cancellation_after_claim_preserves_children_and_review_state(
    lease_db: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import import_execute
    import import_pipeline

    queue_id = _queue(lease_db, download_id="cancel-owned")
    monkeypatch.setattr(import_pipeline, "PACK_STAGING_ROOT", str(tmp_path))
    pack_dir = _pack_dir("cancel-owned")
    pack_dir.mkdir()
    (pack_dir / "page.jpg").write_bytes(b"page")
    with sqlite3.connect(lease_db) as db:
        db.execute(
            "INSERT INTO import_queue_files(queue_id, filename, status)"
            " VALUES(?, 'pending.cbz', 'pending')",
            (queue_id,),
        )
        db.execute(
            "INSERT INTO import_queue_files(queue_id, filename, status)"
            " VALUES(?, 'review.cbz', 'needs_review')",
            (queue_id,),
        )

    import_execute._IMPORT_SEM = asyncio.Semaphore(1)
    entered = asyncio.Event()

    async def _blocked(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        entered.set()
        await asyncio.Event().wait()
        return True

    monkeypatch.setattr(import_execute, "_execute_import", _blocked)

    async def _exercise() -> None:
        task = asyncio.create_task(
            import_execute._guarded_execute_import(queue_id)
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_exercise())

    assert _state(lease_db, queue_id) == ("partial", None, None)
    with sqlite3.connect(lease_db) as db:
        child_states = [
            row[0]
            for row in db.execute(
                "SELECT status FROM import_queue_files"
                " WHERE queue_id=? ORDER BY id",
                (queue_id,),
            ).fetchall()
        ]
    assert child_states == ["pending", "needs_review"]
    assert pack_dir.is_dir()


def test_ordinary_exception_cleans_pack_only_after_failed_transition(
    lease_db: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import import_execute
    import import_pipeline

    monkeypatch.setattr(import_pipeline, "PACK_STAGING_ROOT", str(tmp_path))
    queue_id = _queue(lease_db, download_id="ordinary-failure")
    pack_dir = _pack_dir("ordinary-failure")
    pack_dir.mkdir()
    (pack_dir / "page.jpg").write_bytes(b"page")

    async def _explode(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        raise OSError("transient execution failure")

    monkeypatch.setattr(import_execute, "_execute_import", _explode)
    assert not asyncio.run(import_execute._guarded_execute_import(queue_id))
    assert _state(lease_db, queue_id) == ("failed", None, None)
    assert not pack_dir.exists()


def test_double_cancel_waits_for_publication_and_phase3(
    lease_db: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Two cancellations cannot release the lease ahead of commit/Phase 3."""
    import import_execute
    import import_publication
    import shared

    queue_id, _ = _ready_import(
        lease_db,
        tmp_path,
        download_id="double-cancel",
    )
    import_execute._IMPORT_SEM = asyncio.Semaphore(1)
    shared.CONFIG["import_mode"] = "copy"

    worker_started = threading.Event()
    worker_release = threading.Event()
    worker_finished = threading.Event()
    phase3_called = threading.Event()
    original_rename = import_publication._rename_noreplace
    original_phase3 = import_publication.mark_publication_db_committed

    def _blocking_publish(source: str, destination: str) -> None:
        worker_started.set()
        assert worker_release.wait(timeout=5)
        original_rename(source, destination)
        worker_finished.set()

    def _observed_phase3(*args: object, **kwargs: object):
        assert worker_finished.is_set()
        phase3_called.set()
        return original_phase3(*args, **kwargs)

    monkeypatch.setattr(
        import_publication,
        "_rename_noreplace",
        _blocking_publish,
    )
    monkeypatch.setattr(
        import_publication,
        "mark_publication_db_committed",
        _observed_phase3,
    )
    async def _exercise() -> None:
        task = asyncio.create_task(
            import_execute._guarded_execute_import(queue_id)
        )
        assert await asyncio.to_thread(worker_started.wait, 2)

        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.05)

        assert not task.done()
        status, owner, expiry = _state(lease_db, queue_id)
        assert status == "importing"
        assert owner
        assert expiry
        assert not phase3_called.is_set()

        worker_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_exercise())

    assert worker_finished.is_set()
    assert phase3_called.is_set()
    with sqlite3.connect(lease_db) as db:
        assert db.execute(
            "SELECT 1 FROM import_queue WHERE id=?",
            (queue_id,),
        ).fetchone() is None
        assert db.execute(
            "SELECT state, pack_cleanup_state FROM import_publications"
            " WHERE queue_id=?",
            (queue_id,),
        ).fetchone() == ("deleted", "complete")


def test_heartbeat_retries_transient_sqlite_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import import_execute

    attempts = 0
    second_attempt = threading.Event()

    def _transient_refresh(queue_id: int, owner: str) -> bool:
        nonlocal attempts
        del queue_id, owner
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("database is temporarily locked")
        second_attempt.set()
        return True

    monkeypatch.setattr(import_execute, "IMPORT_LEASE_REFRESH_SECONDS", 0.01)
    monkeypatch.setattr(import_execute, "IMPORT_LEASE_SECONDS", 0.2)
    monkeypatch.setattr(import_execute, "_HEARTBEAT_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(import_execute, "_refresh_owned_import", _transient_refresh)

    async def _exercise() -> bool:
        stop = asyncio.Event()
        lost = asyncio.Event()
        task = asyncio.create_task(
            import_execute._lease_heartbeat(1, "owner", stop, lost)
        )
        assert await asyncio.to_thread(second_attempt.wait, 1)
        stop.set()
        await task
        return lost.is_set()

    with caplog.at_level("WARNING"):
        assert asyncio.run(_exercise()) is False
    assert attempts >= 2
    assert "retrying" in caplog.text


def test_heartbeat_db_wait_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_execute

    refresh_started = threading.Event()
    refresh_release = threading.Event()

    def _blocking_refresh(queue_id: int, owner: str) -> bool:
        del queue_id, owner
        refresh_started.set()
        assert refresh_release.wait(timeout=0.5)
        return True

    monkeypatch.setattr(import_execute, "IMPORT_LEASE_REFRESH_SECONDS", 0.01)
    monkeypatch.setattr(import_execute, "_refresh_owned_import", _blocking_refresh)

    async def _exercise() -> float:
        stop = asyncio.Event()
        lost = asyncio.Event()
        task = asyncio.create_task(
            import_execute._lease_heartbeat(1, "owner", stop, lost)
        )
        while not refresh_started.is_set():
            await asyncio.sleep(0.001)
        loop = asyncio.get_running_loop()
        started = loop.time()
        marker = asyncio.Event()
        loop.call_later(0.02, marker.set)
        await marker.wait()
        elapsed = loop.time() - started
        refresh_release.set()
        stop.set()
        await task
        assert not lost.is_set()
        return elapsed

    assert asyncio.run(_exercise()) < 0.15


def test_phase1_final_expiry_cas_rolls_back_child_changes(
    lease_db: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import import_plan
    import main
    from import_lease import claim_import_queue_row, refresh_import_queue_lease

    queue_id, _ = _ready_import(
        lease_db,
        tmp_path,
        download_id="phase1-expiry",
    )
    with main.get_db() as db:
        assert claim_import_queue_row(db, queue_id, "owner-a")
        child_id = db.execute(
            "SELECT id FROM import_queue_files WHERE queue_id=?",
            (queue_id,),
        ).fetchone()["id"]

    def _expire_before_final_cas(
        db: sqlite3.Connection,
        queue_id_arg: int,
        owner: str,
        *,
        lease_seconds: float,
    ) -> bool:
        db.execute(
            "UPDATE import_queue"
            " SET lease_expires_at=datetime('now', '-1 second')"
            " WHERE id=? AND lease_owner=?",
            (queue_id_arg, owner),
        )
        return refresh_import_queue_lease(
            db,
            queue_id_arg,
            owner,
            lease_seconds=lease_seconds,
        )

    monkeypatch.setattr(
        import_plan,
        "refresh_import_queue_lease",
        _expire_before_final_cas,
    )

    with pytest.raises(import_plan._ImportPlanLeaseLost):
        with main.get_db() as db:
            import_plan._plan_import(
                db,
                queue_id,
                "owner-a",
                {child_id: 9.5},
                {},
                set(),
                "copy",
                lease_seconds=300,
            )

    with sqlite3.connect(lease_db) as db:
        child = db.execute(
            "SELECT proposed_volume, status FROM import_queue_files WHERE id=?",
            (child_id,),
        ).fetchone()
        parent = db.execute(
            "SELECT status, lease_owner,"
            " lease_expires_at > datetime('now')"
            " FROM import_queue WHERE id=?",
            (queue_id,),
        ).fetchone()
    assert child == (1.0, "pending")
    assert parent == ("importing", "owner-a", 1)


def test_pack_cleanup_requires_terminal_or_absent_queue(
    lease_db: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import import_execute
    import import_pipeline
    from import_lease import (
        claim_import_queue_row,
        recover_expired_import_leases,
    )

    monkeypatch.setattr(import_pipeline, "PACK_STAGING_ROOT", str(tmp_path))
    pack_dir = _pack_dir("pack-successor")
    pack_dir.mkdir()
    (pack_dir / "page.jpg").write_bytes(b"page")
    queue_id = _queue(lease_db, download_id="pack-successor")
    with sqlite3.connect(lease_db) as db:
        assert claim_import_queue_row(db, queue_id, "owner-a")
        db.execute(
            "UPDATE import_queue"
            " SET lease_expires_at=datetime('now', '-1 second') WHERE id=?",
            (queue_id,),
        )

    assert not import_execute._cleanup_pack_staging_if_safe(
        queue_id,
        "pack-successor",
        "owner-a",
    )
    assert pack_dir.is_dir()

    with sqlite3.connect(lease_db) as db:
        db.row_factory = sqlite3.Row
        assert recover_expired_import_leases(db) == 1
        assert claim_import_queue_row(db, queue_id, "owner-b")

    assert not import_execute._cleanup_pack_staging_if_safe(
        queue_id,
        "pack-successor",
        "owner-a",
    )
    assert pack_dir.is_dir()
    assert not import_execute._cleanup_pack_staging_if_safe(
        queue_id,
        "pack-successor",
        "owner-b",
    )
    with sqlite3.connect(lease_db) as db:
        from import_lease import transition_import_queue_row

        assert transition_import_queue_row(
            db,
            queue_id,
            "owner-b",
            "failed",
        )

    assert import_execute._cleanup_pack_staging_if_safe(
        queue_id,
        "pack-successor",
        "owner-b",
    )
    assert not pack_dir.exists()
    assert import_execute._cleanup_pack_staging_if_safe(
        queue_id,
        "pack-successor",
        "owner-b",
    )


def test_absent_original_queue_cannot_cleanup_live_sibling_pack(
    lease_db: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import import_execute
    import import_pipeline
    from import_lease import claim_import_queue_row

    monkeypatch.setattr(import_pipeline, "PACK_STAGING_ROOT", str(tmp_path))
    pack_dir = _pack_dir("shared-pack")
    pack_dir.mkdir()
    (pack_dir / "page.jpg").write_bytes(b"page")
    original_id = _queue(lease_db, download_id="shared-pack")
    successor_id = _queue(lease_db, download_id="shared-pack")
    with sqlite3.connect(lease_db) as db:
        db.execute("DELETE FROM import_queue WHERE id=?", (original_id,))
        assert claim_import_queue_row(db, successor_id, "owner-b")

    assert not import_execute._cleanup_pack_staging_if_safe(
        original_id,
        "shared-pack",
        "owner-a",
    )
    assert pack_dir.is_dir()


def test_pack_cleanup_detaches_before_slow_delete_without_writer_lock(
    lease_db: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import import_execute
    import import_pack_cleanup
    import import_pipeline
    from import_lease import claim_import_queue_row, transition_import_queue_row

    monkeypatch.setattr(import_pipeline, "PACK_STAGING_ROOT", str(tmp_path))
    pack_dir = _pack_dir("slow-pack")
    pack_dir.mkdir()
    (pack_dir / "page.jpg").write_bytes(b"page")
    queue_id = _queue(lease_db, download_id="slow-pack")
    with sqlite3.connect(lease_db) as db:
        assert claim_import_queue_row(db, queue_id, "owner-a")
        assert transition_import_queue_row(
            db,
            queue_id,
            "owner-a",
            "failed",
        )

    delete_started = threading.Event()
    delete_release = threading.Event()
    original_rmtree = import_pack_cleanup.shutil.rmtree

    def _slow_rmtree(path: str) -> None:
        delete_started.set()
        assert delete_release.wait(timeout=5)
        original_rmtree(path)

    monkeypatch.setattr(import_pack_cleanup.shutil, "rmtree", _slow_rmtree)

    async def _exercise() -> float:
        cleanup = asyncio.create_task(
            asyncio.to_thread(
                import_execute._cleanup_pack_staging_if_safe,
                queue_id,
                "slow-pack",
                "owner-a",
            )
        )
        assert await asyncio.to_thread(delete_started.wait, 2)
        assert not pack_dir.exists()

        loop = asyncio.get_running_loop()
        started = loop.time()
        marker = asyncio.Event()
        loop.call_later(0.02, marker.set)
        with sqlite3.connect(lease_db) as db:
            db.execute(
                "UPDATE series SET title='writer progressed' WHERE id=1"
            )
            db.commit()
        await marker.wait()
        elapsed = loop.time() - started

        delete_release.set()
        assert await cleanup
        return elapsed

    assert asyncio.run(_exercise()) < 0.15


@pytest.mark.parametrize("status", ("pending", "partial", "importing"))
def test_pack_cleanup_retains_nonterminal_queue(
    lease_db: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status: str,
) -> None:
    import import_execute
    import import_pipeline

    monkeypatch.setattr(import_pipeline, "PACK_STAGING_ROOT", str(tmp_path))
    pack_dir = _pack_dir(f"retain-{status}")
    pack_dir.mkdir()
    (pack_dir / "page.jpg").write_bytes(b"page")
    queue_id = _queue(
        lease_db,
        download_id=f"retain-{status}",
        status=status,
        owner="worker" if status == "importing" else None,
        expiry_sql=(
            "datetime('now', '+5 minutes')" if status == "importing" else None
        ),
    )

    assert not import_execute._cleanup_pack_staging_if_safe(
        queue_id,
        f"retain-{status}",
        "worker",
    )
    assert pack_dir.is_dir()


def test_cleanup_reservation_blocks_normalized_successor_queue_creation(
    lease_db: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import import_execute
    import import_pack_cleanup
    import import_pipeline
    import main

    monkeypatch.setattr(import_pipeline, "PACK_STAGING_ROOT", str(tmp_path))
    library = tmp_path / "reservation-library"
    library.mkdir()
    source = tmp_path / "Race v01.cbz"
    source.write_bytes(b"reservation")
    with sqlite3.connect(lease_db) as db:
        db.execute(
            "INSERT OR REPLACE INTO root_folders(id,path,label,is_default)"
            " VALUES(1,?,'Reservation',1)",
            (str(library),),
        )
        db.execute("UPDATE series SET root_folder_id=1 WHERE id=1")

    original_id = _queue(
        lease_db,
        download_id="pack-race",
        status="failed",
    )
    pack_dir = _pack_dir("pack-race")
    pack_dir.mkdir()
    (pack_dir / "page.jpg").write_bytes(b"old")

    reservation_ready = threading.Event()
    reservation_release = threading.Event()
    original_acquire = import_pack_cleanup._acquire_cleanup_reservation

    def _pause_after_reservation(**kwargs: object):
        reservation = original_acquire(**kwargs)
        assert reservation is not None
        reservation_ready.set()
        assert reservation_release.wait(timeout=5)
        return reservation

    monkeypatch.setattr(
        import_pack_cleanup,
        "_acquire_cleanup_reservation",
        _pause_after_reservation,
    )
    cleanup_result: list[bool] = []
    cleanup_thread = threading.Thread(
        target=lambda: cleanup_result.append(
            import_execute._cleanup_pack_staging_if_safe(
                original_id,
                "pack-race",
                "old-owner",
            )
        )
    )
    cleanup_thread.start()
    assert reservation_ready.wait(timeout=5)
    with sqlite3.connect(lease_db) as db:
        db.execute("DELETE FROM import_queue WHERE id=?", (original_id,))

    with main.get_db() as db:
        successor = main._queue_import(
            db,
            1,
            "PACK-RACE",
            "Race v01",
            "magnet:race",
            1.0,
            str(source),
        )
    assert successor == (None, False)
    with sqlite3.connect(lease_db) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM import_queue",
        ).fetchone() == (0,)

    reservation_release.set()
    cleanup_thread.join(timeout=5)
    assert not cleanup_thread.is_alive()
    assert cleanup_result == [True]
    assert not pack_dir.exists()

    with main.get_db() as db:
        successor_id, needs_review = main._queue_import(
            db,
            1,
            "PACK-RACE",
            "Race v01",
            "magnet:race",
            1.0,
            str(source),
        )
    assert successor_id is not None
    assert not needs_review


def test_expired_cleanup_reservation_recovers_and_no_longer_blocks_claim(
    lease_db: str,
    tmp_path: Path,
) -> None:
    from download_identity import (
        DownloadIdentity,
        download_identity_key,
        normalize_download_id,
    )
    from import_lease import claim_import_queue_row
    from import_pack_cleanup import recover_pack_cleanup_state

    queue_id = _queue(lease_db, download_id="Case-Safe")
    identity = DownloadIdentity(None, None, "CASE-SAFE")
    pack_dir = _pack_dir(identity.download_id)
    tombstone = Path(f"{pack_dir}.cleanup-crashed")
    with sqlite3.connect(lease_db) as db:
        db.execute(
            """
            INSERT INTO import_pack_cleanup_reservations(
                download_identity_key, download_client_id, protocol,
                normalized_download_id, download_id, purpose, owner_token,
                queue_id, pack_path, tombstone_path, expires_at
            ) VALUES(
                ?, NULL, NULL, ?, 'CASE-SAFE', 'cleanup', 'crashed-cleaner',
                ?, ?, ?, datetime('now', '+5 minutes')
            )
            """,
            (
                download_identity_key(identity),
                normalize_download_id(identity.download_id, identity.protocol),
                queue_id,
                str(pack_dir),
                str(tombstone),
            ),
        )
        assert not claim_import_queue_row(db, queue_id, "worker")
        db.execute(
            "UPDATE import_pack_cleanup_reservations"
            " SET expires_at=datetime('now', '-1 second')"
        )

    recovered = recover_pack_cleanup_state()
    assert recovered.reservations_recovered == 1
    with sqlite3.connect(lease_db) as db:
        assert claim_import_queue_row(db, queue_id, "worker")
        assert db.execute(
            "SELECT COUNT(*) FROM import_pack_cleanup_reservations"
        ).fetchone() == (0,)


def test_missing_destination_failure_preserves_live_sibling_download(
    lease_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_plan
    import main
    from import_lease import claim_import_queue_row

    queue_a = _queue(lease_db, download_id="shared-plan")
    queue_b = _queue(lease_db, download_id="shared-plan")
    with sqlite3.connect(lease_db) as db:
        db.execute(
            "INSERT INTO volumes(series_id, volume_num, status, download_id)"
            " VALUES(1, 1, 'grabbed', 'shared-plan')"
        )
        db.execute(
            "INSERT INTO import_queue_files(queue_id, filename, status)"
            " VALUES(?, 'a.cbz', 'pending')",
            (queue_a,),
        )
        assert claim_import_queue_row(db, queue_a, "owner-a")
        assert claim_import_queue_row(db, queue_b, "owner-b")

    monkeypatch.setattr(import_plan, "_series_library_dir", lambda *args: None)
    with main.get_db() as db:
        plan = import_plan._plan_import(
            db,
            queue_a,
            "owner-a",
            {},
            {},
            set(),
            "copy",
            lease_seconds=300,
        )
    assert plan is None
    assert _state(lease_db, queue_a) == ("failed", None, None)
    assert _state(lease_db, queue_b)[0:2] == ("importing", "owner-b")
    with sqlite3.connect(lease_db) as db:
        assert db.execute(
            "SELECT status, download_id FROM volumes WHERE series_id=1"
        ).fetchone() == ("grabbed", "shared-plan")


def test_phase3_failure_preserves_live_sibling_download(
    lease_db: str,
) -> None:
    import import_commit
    import main
    from import_lease import claim_import_queue_row
    from import_plan import _FilePlan, _ImportPlan

    queue_a = _queue(lease_db, download_id="shared-phase3")
    queue_b = _queue(lease_db, download_id="shared-phase3")
    with sqlite3.connect(lease_db) as db:
        db.execute(
            "INSERT INTO volumes(series_id, volume_num, status, download_id)"
            " VALUES(1, 1, 'grabbed', 'shared-phase3')"
        )
        child_id = db.execute(
            "INSERT INTO import_queue_files(queue_id, filename, status)"
            " VALUES(?, 'failed.cbz', 'pending')",
            (queue_a,),
        ).lastrowid
        assert child_id is not None
        assert claim_import_queue_row(db, queue_a, "owner-a")
        assert claim_import_queue_row(db, queue_b, "owner-b")

    with main.get_db() as db:
        queue = dict(
            db.execute(
                "SELECT * FROM import_queue WHERE id=?",
                (queue_a,),
            ).fetchone()
        )
        plan = _ImportPlan(
            queue=queue,
            series={"id": 1},
            series_tags=[],
            dst_dir="/unused",
            import_mode="copy",
            now_ts=None,
            files=[
                _FilePlan(
                    file_id=int(child_id),
                    src_path="/missing",
                    filename="failed.cbz",
                    dst_path="/unused/failed.cbz",
                    import_kind="volume",
                    file_type="volume",
                    proposed_vol=1,
                    proposed_chap=None,
                    chap_range_end=None,
                    vol_range_start=None,
                    vol_range_end=None,
                    pack_type=None,
                    is_special=0,
                    special_title=None,
                    has_volume_range=False,
                    is_legacy_chapter_stub=False,
                    is_legacy_chapter_recheck=False,
                    plan_status="pre_failed",
                    plan_failure_reason="fault",
                )
            ],
            series_id=1,
        )
        result = import_commit._commit_import(
            db,
            plan,
            [],
            False,
            "",
            lease_owner="owner-a",
            lease_seconds=300,
        )

    assert result == (False, 0, "failed")
    assert _state(lease_db, queue_a) == ("failed", None, None)
    assert _state(lease_db, queue_b)[0:2] == ("importing", "owner-b")
    with sqlite3.connect(lease_db) as db:
        assert db.execute(
            "SELECT status, download_id FROM volumes WHERE series_id=1"
        ).fetchone() == ("grabbed", "shared-phase3")


def test_active_heartbeat_prevents_cleanup_recovery(
    lease_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_execute
    import tasks
    from import_lease import transition_import_queue_row

    queue_id = _queue(lease_db, download_id="heartbeat")
    import_execute._IMPORT_SEM = asyncio.Semaphore(1)
    monkeypatch.setattr(import_execute, "IMPORT_LEASE_REFRESH_SECONDS", 0.01)
    refreshed = asyncio.Event()
    finish = asyncio.Event()
    original_refresh = import_execute.refresh_import_queue_lease

    def _recording_refresh(*args: object, **kwargs: object) -> bool:
        result = original_refresh(*args, **kwargs)
        if result:
            refreshed.set()
        return result

    async def _blocked(
        queue_id_arg: int,
        *args: object,
        **kwargs: object,
    ) -> bool:
        del args
        await finish.wait()
        with import_execute.get_db() as db:
            assert transition_import_queue_row(
                db,
                queue_id_arg,
                kwargs["lease_owner"],
                "imported",
            )
        return True

    monkeypatch.setattr(
        import_execute,
        "refresh_import_queue_lease",
        _recording_refresh,
    )
    monkeypatch.setattr(import_execute, "_execute_import", _blocked)

    async def _exercise() -> None:
        task = asyncio.create_task(
            import_execute._guarded_execute_import(queue_id)
        )
        await asyncio.wait_for(refreshed.wait(), timeout=2)
        stats = tasks.cleanup_stuck_state(
            queue_stale_days=0,
            events_retention_days=0,
            orphan_pack_cleanup=False,
        )
        assert stats["importing_reset"] == 0
        current = _state(lease_db, queue_id)
        assert current[0] == "importing"
        assert current[1] is not None
        assert current[2] is not None
        finish.set()
        assert await task is True

    asyncio.run(_exercise())


def test_ordinary_exception_owner_cas_fails_without_changing_children(
    lease_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_execute

    queue_id = _queue(lease_db, download_id="ordinary-failure")
    with sqlite3.connect(lease_db) as db:
        db.execute(
            "INSERT INTO import_queue_files(queue_id, filename, status)"
            " VALUES(?, 'keep.cbz', 'pending')",
            (queue_id,),
        )
    import_execute._IMPORT_SEM = asyncio.Semaphore(1)

    async def _raise(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        raise RuntimeError("simulated execution failure")

    monkeypatch.setattr(import_execute, "_execute_import", _raise)

    assert asyncio.run(
        import_execute._guarded_execute_import(queue_id)
    ) is False
    assert _state(lease_db, queue_id) == ("failed", None, None)
    with sqlite3.connect(lease_db) as db:
        child = db.execute(
            "SELECT status FROM import_queue_files WHERE queue_id=?",
            (queue_id,),
        ).fetchone()
    assert child == ("pending",)


def test_phase3_refuses_lost_owner_before_any_domain_mutation(
    lease_db: str,
) -> None:
    from import_commit import _commit_import

    queue_id = _queue(
        lease_db,
        download_id="phase3-lost",
        status="importing",
        owner="owner-b",
        expiry_sql="datetime('now', '+5 minutes')",
    )
    with sqlite3.connect(lease_db) as db:
        db.execute(
            "INSERT INTO import_queue_files(queue_id, filename, status)"
            " VALUES(?, 'untouched.cbz', 'pending')",
            (queue_id,),
        )
        db.execute(
            "INSERT INTO volumes(series_id, volume_num, status)"
            " VALUES(1, 1, 'wanted')"
        )

    plan = SimpleNamespace(
        queue={"id": queue_id},
        series_id=1,
        dst_dir="/unused",
        files=[],
    )
    with sqlite3.connect(lease_db) as db:
        db.row_factory = sqlite3.Row
        result = _commit_import(
            db,
            plan,
            [],
            fs_committed=False,
            commit_failure_reason="",
            lease_owner="owner-a",
            lease_seconds=300,
        )
    assert result == (False, 0, "lease_lost")
    assert _state(lease_db, queue_id)[0:2] == ("importing", "owner-b")
    with sqlite3.connect(lease_db) as db:
        assert db.execute(
            "SELECT status FROM import_queue_files WHERE queue_id=?",
            (queue_id,),
        ).fetchone() == ("pending",)
        assert db.execute(
            "SELECT status FROM volumes WHERE series_id=1"
        ).fetchone() == ("wanted",)
        assert db.execute(
            "SELECT COUNT(*) FROM history WHERE series_id=1"
        ).fetchone()[0] == 0


def test_startup_recovers_before_retry_and_excludes_live_lease(
    lease_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import auth
    import main
    import security

    expired_id = _queue(
        lease_db,
        download_id="startup-expired",
        status="importing",
        owner="owner-expired",
        expiry_sql="datetime('now', '-1 second')",
    )
    live_id = _queue(
        lease_db,
        download_id="startup-live",
        status="importing",
        owner="owner-live",
        expiry_sql="datetime('now', '+5 minutes')",
    )
    review_id = _queue(
        lease_db,
        download_id="startup-legacy-review",
        status="importing",
    )
    with sqlite3.connect(lease_db) as db:
        db.execute(
            "INSERT INTO import_queue_files(queue_id, filename, status)"
            " VALUES(?, 'review.cbz', 'needs_review')",
            (review_id,),
        )

    retried: list[int] = []
    retry_tasks: list[asyncio.Task[object]] = []
    real_sleep = asyncio.sleep

    def _record_retry(queue_id: int) -> None:
        retried.append(queue_id)

    async def _fast_retry_sleep(delay: float) -> None:
        if delay == 5:
            return
        await real_sleep(delay)

    def _create(
        coro: Coroutine[object, object, object],
        *,
        name: str,
    ) -> asyncio.Task[object] | None:
        if name == "retry_stuck_imports":
            task = asyncio.create_task(coro, name=name)
            retry_tasks.append(task)
            return task
        coro.close()
        return None

    async def _cancel_none() -> None:
        return None

    monkeypatch.setattr(main, "schedule_import_worker", _record_retry)
    monkeypatch.setattr(main, "create_background_task", _create)
    monkeypatch.setattr(main, "_cancel_background_tasks", _cancel_none)
    monkeypatch.setattr(main.asyncio, "sleep", _fast_retry_sleep)
    monkeypatch.setattr(auth, "remove_legacy_setup_token", lambda: None)
    monkeypatch.setattr(auth, "purge_expired_sessions", lambda: 0)
    monkeypatch.setattr(
        security,
        "load_or_create_secret_cipher",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(main, "migrate_encrypt_settings_secrets", lambda: None)
    monkeypatch.setattr(
        main,
        "migrate_encrypt_table_column_secrets",
        lambda: None,
    )
    monkeypatch.setattr(
        main,
        "migrate_encrypt_notification_connection_secrets",
        lambda: None,
    )

    async def _exercise() -> None:
        async with main.lifespan(main.app):
            if retry_tasks:
                await asyncio.gather(*retry_tasks)

    asyncio.run(_exercise())

    assert retried == [expired_id]
    assert _state(lease_db, expired_id) == ("pending", None, None)
    assert _state(lease_db, live_id)[0:2] == ("importing", "owner-live")
    assert _state(lease_db, review_id) == ("partial", None, None)
