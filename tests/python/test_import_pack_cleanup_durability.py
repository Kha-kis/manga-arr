"""Durability and fencing contracts for generated import-pack staging."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TypedDict

import pytest


class _PackEnv(TypedDict):
    db_path: str
    pack_root: Path
    library: Path
    tmp_path: Path


@pytest.fixture
def pack_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[_PackEnv]:
    import import_pipeline
    import main
    import shared

    original_main_config = main.CONFIG
    original_main_values = dict(main.CONFIG)
    original_shared_config = shared.CONFIG
    original_shared_values = dict(shared.CONFIG)
    db_path = str(tmp_path / "pack-durability.db")
    pack_root = tmp_path / "pack-root"
    library = tmp_path / "library"
    library.mkdir()

    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(shared, "DB_PATH", db_path)
    monkeypatch.setattr(import_pipeline, "PACK_STAGING_ROOT", str(pack_root))
    main.init_db()
    main.load_config()
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT OR REPLACE INTO root_folders(id, path, label, is_default)"
            " VALUES(1, ?, 'Pack durability', 1)",
            (str(library),),
        )
        db.execute(
            "INSERT INTO series("
            "id, title, search_pattern, root_folder_id"
            ") VALUES(1, 'Pack Series', 'Pack Series', 1)"
        )

    try:
        yield {
            "db_path": db_path,
            "pack_root": pack_root,
            "library": library,
            "tmp_path": tmp_path,
        }
    finally:
        main.CONFIG = original_main_config
        main.CONFIG.clear()
        main.CONFIG.update(original_main_values)
        shared.CONFIG = original_shared_config
        shared.CONFIG.clear()
        shared.CONFIG.update(original_shared_values)


def _terminal_queue(
    db_path: str,
    download_id: str,
    *,
    download_client_id: int | None = None,
    protocol: str | None = None,
) -> int:
    with sqlite3.connect(db_path) as db:
        cur = db.execute(
            "INSERT INTO import_queue("
            "series_id, download_id, download_client_id, download_protocol,"
            " torrent_name, status) VALUES(1, ?, ?, ?, ?, 'failed')",
            (download_id, download_client_id, protocol, download_id),
        )
        queue_id = cur.lastrowid
        assert queue_id is not None
        return queue_id


def _write_cbz(path: Path) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("page.jpg", b"page")


def _probe_writer(db_path: str, title: str) -> None:
    with sqlite3.connect(db_path, timeout=0.25) as db:
        db.execute("UPDATE series SET title=? WHERE id=1", (title,))
        db.commit()


def _pack_paths(
    download_id: str,
    owner_token: str = "test-owner",
    *,
    download_client_id: int | None = None,
    protocol: str | None = None,
) -> tuple[Path, Path]:
    import import_pack_cleanup

    canonical, private = import_pack_cleanup.pack_queue_creation_paths(
        download_id,
        owner_token,
        download_client_id=download_client_id,
        protocol=protocol,
    )
    return Path(canonical), Path(private)


def test_cleanup_fsyncs_after_detach_and_remove_before_journal_deletion(
    pack_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_pack_cleanup

    db_path = str(pack_env["db_path"])
    queue_id = _terminal_queue(db_path, "fsync-order")
    canonical, _ = _pack_paths("fsync-order")
    canonical.mkdir(parents=True)
    (canonical / "page.cbz").write_bytes(b"page")

    events: list[str] = []
    original_rename = import_pack_cleanup._rename_noreplace
    original_fsync = import_pack_cleanup._fsync_pack_root
    original_track = import_pack_cleanup._track_detached_tombstone
    original_rmtree = import_pack_cleanup.shutil.rmtree

    def _rename(source: str, destination: str) -> None:
        events.append("rename")
        original_rename(source, destination)

    def _fsync(path: str) -> None:
        events.append("fsync")
        original_fsync(path)

    def _track(*args: Any, **kwargs: Any) -> None:
        events.append("track")
        original_track(*args, **kwargs)

    def _rmtree(path: str) -> None:
        events.append("rmtree")
        original_rmtree(path)

    monkeypatch.setattr(import_pack_cleanup, "_rename_noreplace", _rename)
    monkeypatch.setattr(import_pack_cleanup, "_fsync_pack_root", _fsync)
    monkeypatch.setattr(
        import_pack_cleanup,
        "_track_detached_tombstone",
        _track,
    )
    monkeypatch.setattr(import_pack_cleanup.shutil, "rmtree", _rmtree)

    assert import_pack_cleanup.cleanup_terminal_pack_staging(
        queue_id,
        "fsync-order",
        download_client_id=None,
        protocol=None,
    )
    assert events == ["rename", "fsync", "track", "rmtree", "fsync"]
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM import_pack_cleanup_tombstones"
        ).fetchone() == (0,)


def test_delayed_detach_and_rmtree_do_not_hold_sqlite_writer(
    pack_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_pack_cleanup

    db_path = str(pack_env["db_path"])
    queue_id = _terminal_queue(db_path, "slow-filesystem")
    canonical, _ = _pack_paths("slow-filesystem")
    canonical.mkdir(parents=True)
    (canonical / "page.cbz").write_bytes(b"page")

    rename_started = threading.Event()
    rename_release = threading.Event()
    rmtree_started = threading.Event()
    rmtree_release = threading.Event()
    original_rename = import_pack_cleanup._rename_noreplace
    original_rmtree = import_pack_cleanup.shutil.rmtree

    def _slow_rename(source: str, destination: str) -> None:
        rename_started.set()
        assert rename_release.wait(timeout=5)
        original_rename(source, destination)

    def _slow_rmtree(path: str) -> None:
        rmtree_started.set()
        assert rmtree_release.wait(timeout=5)
        original_rmtree(path)

    monkeypatch.setattr(
        import_pack_cleanup,
        "_rename_noreplace",
        _slow_rename,
    )
    monkeypatch.setattr(import_pack_cleanup.shutil, "rmtree", _slow_rmtree)
    results: list[bool] = []
    failures: list[BaseException] = []

    def _cleanup() -> None:
        try:
            results.append(
                import_pack_cleanup.cleanup_terminal_pack_staging(
                        queue_id,
                        "slow-filesystem",
                        download_client_id=None,
                        protocol=None,
                )
            )
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=_cleanup)
    worker.start()
    try:
        assert rename_started.wait(timeout=5)
        _probe_writer(db_path, "writer-during-rename")
        rename_release.set()

        assert rmtree_started.wait(timeout=5)
        _probe_writer(db_path, "writer-during-rmtree")
        rmtree_release.set()
    finally:
        rename_release.set()
        rmtree_release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert not failures
    assert results == [True]
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT title FROM series WHERE id=1"
        ).fetchone() == ("writer-during-rmtree",)


def test_crash_after_detach_before_tracking_replays_idempotently(
    pack_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_pack_cleanup

    db_path = str(pack_env["db_path"])
    queue_id = _terminal_queue(
        db_path,
        "DETACH-CRASH",
        download_client_id=101,
        protocol="torrent",
    )
    canonical, _ = _pack_paths(
        "detach-crash",
        download_client_id=101,
        protocol="torrent",
    )
    canonical.mkdir(parents=True)
    (canonical / "page.cbz").write_bytes(b"page")
    original_record = import_pack_cleanup._record_owned_tombstone

    class SimulatedCrash(RuntimeError):
        pass

    def _crash(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        raise SimulatedCrash

    with monkeypatch.context() as crash_patch:
        crash_patch.setattr(
            import_pack_cleanup,
            "_record_owned_tombstone",
            _crash,
        )
        with pytest.raises(SimulatedCrash):
            import_pack_cleanup.cleanup_terminal_pack_staging(
                queue_id,
                "detach-crash",
                download_client_id=101,
                protocol="torrent",
            )

    assert import_pack_cleanup._record_owned_tombstone is original_record
    assert not canonical.exists()
    with sqlite3.connect(db_path) as db:
        reservation = db.execute(
            "SELECT tombstone_path FROM import_pack_cleanup_reservations"
        ).fetchone()
        assert reservation is not None
        tombstone = Path(reservation[0])
        assert tombstone.is_dir()
        assert db.execute(
            "SELECT COUNT(*) FROM import_pack_cleanup_tombstones"
        ).fetchone() == (0,)
        db.execute(
            "UPDATE import_pack_cleanup_reservations"
            " SET expires_at=datetime('now', '-1 second')"
        )

    recovered = import_pack_cleanup.recover_pack_cleanup_state()
    assert recovered.reservations_recovered == 1
    assert recovered.tombstones_removed == 1
    assert not tombstone.exists()
    replayed = import_pack_cleanup.recover_pack_cleanup_state()
    assert replayed == import_pack_cleanup.PackCleanupRecovery()
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM import_pack_cleanup_reservations"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT COUNT(*) FROM import_pack_cleanup_tombstones"
        ).fetchone() == (0,)


def test_expired_reservation_recovery_does_not_hold_writer_during_detach(
    pack_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_pack_cleanup
    import main

    db_path = str(pack_env["db_path"])
    with main.get_db() as db:
        owner = import_pack_cleanup.reserve_pack_queue_creation(
            db,
            "recovery-writer",
            download_client_id=1001,
            protocol="torrent",
        )
    assert owner is not None
    _, private = _pack_paths(
        "recovery-writer",
        owner,
        download_client_id=1001,
        protocol="torrent",
    )
    private.mkdir(parents=True)
    (private / "artifact.cbz").write_bytes(b"artifact")
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE import_pack_cleanup_reservations"
            " SET expires_at=datetime('now', '-1 second')"
        )

    detach_started = threading.Event()
    detach_release = threading.Event()
    original_detach = import_pack_cleanup._detach_directory

    def _slow_detach(source: str, destination: str) -> str:
        detach_started.set()
        assert detach_release.wait(timeout=5)
        return original_detach(source, destination)

    monkeypatch.setattr(
        import_pack_cleanup,
        "_detach_directory",
        _slow_detach,
    )
    results: list[Any] = []
    failures: list[BaseException] = []

    def _recover() -> None:
        try:
            results.append(
                import_pack_cleanup.recover_pack_cleanup_state(max_rows=1)
            )
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=_recover)
    worker.start()
    try:
        assert detach_started.wait(timeout=5)
        _probe_writer(db_path, "writer-during-recovery-detach")
    finally:
        detach_release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert not failures
    assert len(results) == 1
    assert results[0].reservations_recovered == 1


def test_pack_cleanup_runtime_loop_is_bounded_and_backs_off(
    pack_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del pack_env
    import import_pack_cleanup
    import tasks

    calls: list[int] = []
    summaries = iter(
        (
            import_pack_cleanup.PackCleanupRecovery(
                reservations_recovered=1,
            ),
            import_pack_cleanup.PackCleanupRecovery(),
        )
    )

    def _recover(*, max_rows: int) -> object:
        calls.append(max_rows)
        return next(summaries)

    delays: list[float] = []

    async def _sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(
        import_pack_cleanup,
        "recover_pack_cleanup_state",
        _recover,
    )
    monkeypatch.setattr(tasks.asyncio, "sleep", _sleep)
    monkeypatch.setattr(tasks, "log_event", lambda *args, **kwargs: None)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(tasks.pack_cleanup_recovery_loop())

    assert calls == [100, 100]
    assert delays == [1.0, 1.0, 5.0]


def test_pack_cleanup_runtime_cancellation_settles_inflight_recovery(
    pack_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del pack_env
    import import_pack_cleanup
    import tasks

    started = threading.Event()
    release = threading.Event()

    def _recover(*, max_rows: int) -> object:
        assert max_rows == 100
        started.set()
        assert release.wait(timeout=5)
        return import_pack_cleanup.PackCleanupRecovery()

    monkeypatch.setattr(
        import_pack_cleanup,
        "recover_pack_cleanup_state",
        _recover,
    )
    monkeypatch.setattr(tasks, "log_event", lambda *args, **kwargs: None)

    async def _scenario() -> None:
        loop = asyncio.create_task(tasks.pack_cleanup_recovery_loop())
        await asyncio.to_thread(started.wait, 5)
        loop.cancel()
        await asyncio.sleep(0)
        assert not loop.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await loop

    asyncio.run(_scenario())


def test_pack_namespace_separates_concrete_owners_and_sab_case_variants(
    pack_env: _PackEnv,
) -> None:
    import import_pack_cleanup
    import main

    with main.get_db() as db:
        qbit_a = import_pack_cleanup.reserve_pack_queue_creation(
            db,
            "ABCDEF",
            download_client_id=101,
            protocol="torrent",
        )
    with main.get_db() as db:
        qbit_b = import_pack_cleanup.reserve_pack_queue_creation(
            db,
            "abcdef",
            download_client_id=102,
            protocol="torrent",
        )
    assert qbit_a is not None and qbit_b is not None
    qbit_a_path, _ = _pack_paths(
        "ABCDEF",
        qbit_a,
        download_client_id=101,
        protocol="torrent",
    )
    qbit_b_path, _ = _pack_paths(
        "abcdef",
        qbit_b,
        download_client_id=102,
        protocol="torrent",
    )
    assert qbit_a_path != qbit_b_path

    with main.get_db() as db:
        assert (
            import_pack_cleanup.reserve_pack_queue_creation(
                db,
                "AbCdEf",
                download_client_id=101,
                protocol="torrent",
            )
            is None
        )

    with main.get_db() as db:
        sab_upper = import_pack_cleanup.reserve_pack_queue_creation(
            db,
            "NZO-Case",
            download_client_id=201,
            protocol="nzb",
        )
    with main.get_db() as db:
        sab_lower = import_pack_cleanup.reserve_pack_queue_creation(
            db,
            "nzo-case",
            download_client_id=201,
            protocol="nzb",
        )
    assert sab_upper is not None and sab_lower is not None
    sab_upper_path, _ = _pack_paths(
        "NZO-Case",
        sab_upper,
        download_client_id=201,
        protocol="nzb",
    )
    sab_lower_path, _ = _pack_paths(
        "nzo-case",
        sab_lower,
        download_client_id=201,
        protocol="nzb",
    )
    assert sab_upper_path != sab_lower_path


@pytest.mark.parametrize(
    ("persisted_protocol", "configured_type", "stored_id", "target_id", "matches"),
    [
        ("torrent", "sabnzbd", "ABCDEF", "abcdef", True),
        ("nzb", "qbittorrent", "NZO-Exact", "NZO-Exact", True),
        ("nzb", "qbittorrent", "NZO-Exact", "nzo-exact", False),
    ],
)
def test_queue_matching_prefers_persisted_protocol_over_mutable_client_type(
    pack_env: _PackEnv,
    persisted_protocol: str,
    configured_type: str,
    stored_id: str,
    target_id: str,
    matches: bool,
) -> None:
    import import_queue
    import main
    from download_identity import DownloadIdentity

    with sqlite3.connect(pack_env["db_path"]) as db:
        db.execute(
            "INSERT INTO download_clients("
            "id,name,type,host,password,enabled,priority"
            ") VALUES(801001,'Mutable client',?,"
            "'https://mutable.invalid','secret',1,1)",
            (configured_type,),
        )
        db.execute(
            "INSERT INTO import_queue("
            "series_id,download_id,download_client_id,download_protocol,"
            "torrent_name,status"
            ") VALUES(1,?,801001,?,'Persisted identity','pending')",
            (stored_id, persisted_protocol),
        )

    with main.get_db() as db:
        rows = import_queue._matching_queue_rows(
            db,
            series_id=1,
            identity=DownloadIdentity(
                801001,
                persisted_protocol,
                target_id,
            ),
        )
    assert bool(rows) is matches


def test_legacy_null_pack_owner_blocks_without_being_adopted(
    pack_env: _PackEnv,
) -> None:
    import import_pack_cleanup
    import main

    with main.get_db() as db:
        legacy_owner = import_pack_cleanup.reserve_pack_queue_creation(
            db,
            "LEGACY-HASH",
            download_client_id=None,
            protocol="torrent",
        )
    assert legacy_owner is not None
    with main.get_db() as db:
        assert (
            import_pack_cleanup.reserve_pack_queue_creation(
                db,
                "legacy-hash",
                download_client_id=301,
                protocol="torrent",
            )
            is None
        )
    with sqlite3.connect(pack_env["db_path"]) as db:
        row = db.execute(
            "SELECT download_client_id, protocol, normalized_download_id"
            " FROM import_pack_cleanup_reservations"
            " WHERE owner_token=?",
            (legacy_owner,),
        ).fetchone()
    assert row == (None, "torrent", "legacy-hash")


def test_expired_pack_owner_cannot_replace_successor_artifacts(
    pack_env: _PackEnv,
) -> None:
    import import_pack_cleanup
    import main

    db_path = str(pack_env["db_path"])
    with main.get_db() as db:
        stale_owner = import_pack_cleanup.reserve_pack_queue_creation(
            db,
            "takeover",
            download_client_id=None,
            protocol=None,
            lease_seconds=5,
        )
    assert stale_owner is not None
    canonical, stale_private = import_pack_cleanup.pack_queue_creation_paths(
        "takeover",
        stale_owner,
        download_client_id=None,
        protocol=None,
    )
    Path(stale_private).mkdir(parents=True)
    (Path(stale_private) / "artifact.cbz").write_bytes(b"stale")
    with main.get_db() as db:
        assert import_pack_cleanup.begin_pack_queue_attachment(
            db,
            "takeover",
            stale_owner,
            download_client_id=None,
            protocol=None,
            lease_seconds=5,
        )
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE import_pack_cleanup_reservations"
            " SET expires_at=datetime('now', '-1 second')"
        )

    recovered = import_pack_cleanup.recover_pack_cleanup_state()
    assert recovered.reservations_recovered == 1
    with main.get_db() as db:
        successor_owner = import_pack_cleanup.reserve_pack_queue_creation(
            db,
            "takeover",
            download_client_id=None,
            protocol=None,
        )
    assert successor_owner is not None
    _, successor_private = import_pack_cleanup.pack_queue_creation_paths(
        "takeover",
        successor_owner,
        download_client_id=None,
        protocol=None,
    )
    Path(successor_private).mkdir(parents=True)
    (Path(successor_private) / "artifact.cbz").write_bytes(b"successor")
    with main.get_db() as db:
        assert import_pack_cleanup.begin_pack_queue_attachment(
            db,
            "takeover",
            successor_owner,
            download_client_id=None,
            protocol=None,
        )
    import_pack_cleanup.durably_attach_pack_queue_directory(
        "takeover",
        successor_owner,
        download_client_id=None,
        protocol=None,
    )

    # Model an already-running stale worker reopening only its private path.
    Path(stale_private).mkdir(parents=True)
    (Path(stale_private) / "artifact.cbz").write_bytes(b"late-stale-write")
    with pytest.raises(FileExistsError):
        import_pack_cleanup.durably_attach_pack_queue_directory(
            "takeover",
            stale_owner,
            download_client_id=None,
            protocol=None,
        )
    assert (Path(canonical) / "artifact.cbz").read_bytes() == b"successor"

    import_pack_cleanup.remove_pack_queue_private_artifacts(
        "takeover",
        stale_owner,
        download_client_id=None,
        protocol=None,
    )
    with main.get_db() as db:
        assert import_pack_cleanup.release_pack_queue_creation(
            db,
            "takeover",
            successor_owner,
            download_client_id=None,
            protocol=None,
            commit=False,
            attaching=True,
        )


def test_power_loss_queueing_state_recovers_only_owned_canonical_pack(
    pack_env: _PackEnv,
) -> None:
    import import_pack_cleanup
    import main

    db_path = str(pack_env["db_path"])
    with main.get_db() as db:
        owner = import_pack_cleanup.reserve_pack_queue_creation(
            db,
            "attach-power-loss",
            download_client_id=901,
            protocol="torrent",
        )
    assert owner is not None
    canonical, private = _pack_paths(
        "attach-power-loss",
        owner,
        download_client_id=901,
        protocol="torrent",
    )
    private.mkdir(parents=True)
    (private / "artifact.cbz").write_bytes(b"owned")

    statements: list[str] = []
    with main.get_db() as db:
        db.set_trace_callback(statements.append)
        assert import_pack_cleanup.begin_pack_queue_attachment(
            db,
            "attach-power-loss",
            owner,
            download_client_id=901,
            protocol="torrent",
        )
    normalized_statements = [
        " ".join(statement.upper().split()) for statement in statements
    ]
    full_index = normalized_statements.index("PRAGMA SYNCHRONOUS=FULL")
    begin_index = normalized_statements.index("BEGIN IMMEDIATE")
    commit_index = normalized_statements.index("COMMIT")
    normal_index = normalized_statements.index("PRAGMA SYNCHRONOUS=NORMAL")
    assert full_index < begin_index < commit_index < normal_index

    import_pack_cleanup.durably_attach_pack_queue_directory(
        "attach-power-loss",
        owner,
        download_client_id=901,
        protocol="torrent",
    )
    assert canonical.is_dir()
    assert not private.exists()

    # Model a power loss where an older NORMAL pre-rename CAS rolled back even
    # though the canonical directory rename reached stable storage.
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE import_pack_cleanup_reservations"
            " SET purpose='queueing', expires_at=datetime('now', '-1 second')"
            " WHERE owner_token=?",
            (owner,),
        )

    recovery = import_pack_cleanup.recover_pack_cleanup_state()
    assert recovery.reservations_recovered == 1
    assert recovery.tombstones_removed == 1
    assert not canonical.exists()
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM import_pack_cleanup_reservations"
        ).fetchone() == (0,)


def test_queueing_recovery_retains_unowned_canonical_pack(
    pack_env: _PackEnv,
) -> None:
    import import_pack_cleanup
    import main

    db_path = str(pack_env["db_path"])
    with main.get_db() as db:
        owner = import_pack_cleanup.reserve_pack_queue_creation(
            db,
            "unowned-canonical",
            download_client_id=902,
            protocol="torrent",
        )
    assert owner is not None
    canonical, private = _pack_paths(
        "unowned-canonical",
        owner,
        download_client_id=902,
        protocol="torrent",
    )
    assert not private.exists()
    canonical.mkdir(parents=True)
    unrelated = canonical / "do-not-clobber.cbz"
    unrelated.write_bytes(b"unrelated")
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE import_pack_cleanup_reservations"
            " SET expires_at=datetime('now', '-1 second')"
            " WHERE owner_token=?",
            (owner,),
        )

    assert (
        import_pack_cleanup.recover_pack_cleanup_state()
        == import_pack_cleanup.PackCleanupRecovery()
    )
    assert unrelated.read_bytes() == b"unrelated"
    with main.get_db() as db:
        assert (
            import_pack_cleanup.reserve_pack_queue_creation(
                db,
                "unowned-canonical",
                download_client_id=902,
                protocol="torrent",
            )
            is None
        )
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM import_pack_cleanup_reservations"
        ).fetchone() == (1,)


def test_archive_scan_heartbeats_past_initial_lease_and_blocks_takeover(
    pack_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_pack_cleanup
    import import_queue
    import main

    source_dir = Path(pack_env["tmp_path"]) / "download"
    source_dir.mkdir()
    for volume in range(1, 5):
        _write_cbz(source_dir / f"Pack Series v{volume:02d}.cbz")

    monkeypatch.setattr(import_queue, "PACK_RESERVATION_SECONDS", 2.0)
    third_scan_started = threading.Event()
    third_scan_release = threading.Event()
    scan_count = 0

    def _slow_comicinfo(path: str) -> dict[str, object]:
        nonlocal scan_count
        del path
        scan_count += 1
        time.sleep(0.75)
        if scan_count == 3:
            third_scan_started.set()
            assert third_scan_release.wait(timeout=5)
        return {}

    monkeypatch.setattr(import_queue, "read_comic_info", _slow_comicinfo)
    results: list[tuple[int | None, bool]] = []
    failures: list[BaseException] = []

    def _queue() -> None:
        try:
            with main.get_db() as db:
                results.append(
                    import_queue._queue_import(
                        db,
                        1,
                        "scan-heartbeat",
                        "Pack Series v01-04",
                        "magnet:scan-heartbeat",
                        None,
                        str(source_dir),
                    )
                )
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=_queue)
    worker.start()
    try:
        assert third_scan_started.wait(timeout=5)
        # The original two-second lease elapsed, but loop checkpoints renewed it.
        import_pack_cleanup.recover_pack_cleanup_state()
        with main.get_db() as db:
            takeover = import_pack_cleanup.reserve_pack_queue_creation(
                db,
                "scan-heartbeat",
                download_client_id=None,
                protocol=None,
                lease_seconds=2.0,
            )
        assert takeover is None
    finally:
        third_scan_release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert not failures
    assert results and results[0][0] is not None
    with sqlite3.connect(str(pack_env["db_path"])) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM import_queue"
            " WHERE download_id='scan-heartbeat'"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT COUNT(*) FROM import_pack_cleanup_reservations"
        ).fetchone() == (0,)


def test_generated_queue_rows_reference_only_attached_canonical_paths(
    pack_env: _PackEnv,
) -> None:
    import import_queue
    import main

    source_dir = pack_env["tmp_path"] / "image-download"
    chapter_dir = source_dir / "Pack Series c001"
    chapter_dir.mkdir(parents=True)
    (chapter_dir / "001.jpg").write_bytes(b"page-one")
    (chapter_dir / "002.jpg").write_bytes(b"page-two")

    with main.get_db() as db:
        queue_id, _ = import_queue._queue_import(
            db,
            1,
            "canonical-src",
            "Pack Series c001",
            "magnet:canonical-src",
            None,
            str(source_dir),
        )
    assert queue_id is not None

    with sqlite3.connect(pack_env["db_path"]) as db:
        source_row = db.execute(
            "SELECT src_path FROM import_queue_files WHERE queue_id=?",
            (queue_id,),
        ).fetchone()
        assert source_row is not None
        queued_source = Path(source_row[0])
        canonical, _ = _pack_paths("canonical-src")
        assert queued_source.parent == canonical
        assert ".owner-" not in str(queued_source)
        assert queued_source.is_file()
        assert db.execute(
            "SELECT COUNT(*) FROM import_pack_cleanup_reservations"
        ).fetchone() == (0,)
