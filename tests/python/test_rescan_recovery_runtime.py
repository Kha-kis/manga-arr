"""Actual replay paging, startup entrypoint and cancellation ownership."""

import asyncio
from dataclasses import asdict
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from test_rescan_transactions import rescan_env as rescan_env
from test_rescan_recovery_protocol import _fixture, _rows


def _pending(env):
    import private_file_claim as claims
    import rescan_file_recovery as recovery
    from file_mutation_lock import file_mutation_guard

    path, _, target, context = _fixture(env)
    original = path.read_bytes()
    with file_mutation_guard(env["db_path"]) as guard:
        operation = recovery._reserve(
            target, context, recovery.fingerprint_path(str(path)), str(path)
        )
        assert operation is not None
        recovery._allocate(operation, guard, "stage")
        with recovery._open(operation, guard, "stage") as carrier:
            Path(carrier.artifact_path).write_bytes(original)
            with open(carrier.artifact_path, "rb") as file:
                import os

                os.fsync(file.fileno())
            os.fsync(carrier.fd)
            operation.fingerprints["stage"] = asdict(
                claims.fingerprint_regular(carrier)
            )
            recovery._store(operation)
        recovery._capture_source(operation, guard)
    assert not path.exists()
    return operation, path, original


def test_bounded_startup_keyset_drain_does_not_starve_later_operation(rescan_env):
    import rescan_file_recovery as recovery

    with sqlite3.connect(rescan_env["db_path"]) as db:
        for i in range(1, 8):
            db.execute(
                "INSERT INTO rescan_file_operations(operation_token,series_id,volume_id,source_path,destination_path,"
                "expected_volume_json,expected_context_json,fingerprints_json,carriers_json,state) "
                "VALUES(?,100,?,'/source','/dest','{}','{}','{}','{}','prepared')",
                (f"invalid{i}", i + 100),
            )
    operation, path, original = _pending(rescan_env)
    summary = asyncio.run(recovery.drain_active_rescan_file_operations(page_size=2))
    assert summary == {"blocked": 7, "rolled_back": 1}
    assert path.read_bytes() == original
    assert _rows(rescan_env)[-1]["state"] == "rolled_back"
    assert recovery.active_operation_ids(after_id=operation.row["id"], limit=2) == []


def test_actual_lifespan_replays_rescan_before_deletion_or_producers(
    rescan_env, monkeypatch
):
    import main
    import security

    _, path, original = _pending(rescan_env)
    observed = []
    # Scope startup to the isolated fixture DB and stop before any external
    # client bootstrap or background producer. The rescan drain is real.
    monkeypatch.setattr(security, "load_or_create_secret_cipher", lambda path: None)
    monkeypatch.setattr(
        main,
        "recover_pack_cleanup_state",
        lambda **kwargs: SimpleNamespace(
            reservations_recovered=0, tombstones_removed=0, tombstones_retained=0
        ),
    )

    class StopStartup(Exception):
        pass

    async def deletion(**kwargs):
        assert path.read_bytes() == original
        assert _rows(rescan_env)[0]["state"] == "rolled_back"
        observed.append("rescan completed before deletion")
        raise StopStartup

    monkeypatch.setattr(main, "drain_active_volume_file_deletions", deletion)

    async def startup():
        with pytest.raises(StopStartup):
            async with main.lifespan(main.app):
                pytest.fail("unexpected producer admission")

    asyncio.run(startup())
    assert observed == ["rescan completed before deletion"]


def test_replay_cancellation_settles_owned_thread_before_raising(
    rescan_env, monkeypatch
):
    import rescan_file_recovery as recovery

    operation, path, original = _pending(rescan_env)
    entered = threading.Event()
    release = threading.Event()
    real_restore = recovery._restore_source

    def paused(*args):
        entered.set()
        assert release.wait(10)
        return real_restore(*args)

    monkeypatch.setattr(recovery, "_restore_source", paused)

    async def exercise():
        task = asyncio.create_task(recovery.replay_rescan_file_operations(limit=1))
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(exercise())
    finally:
        release.set()
    assert path.read_bytes() == original
    durable = recovery.load_operation(operation.row["id"])
    assert durable is not None
    assert durable.row["state"] == "rolled_back"


def test_cancelled_rescan_dispatch_waits_for_real_owned_filesystem_unit(
    rescan_env, monkeypatch
):
    import rescan
    import rescan_file_recovery as recovery

    path, _, _, _ = _fixture(rescan_env)
    original = path.read_bytes()
    entered = threading.Event()
    release = threading.Event()
    real_capture = recovery._capture_source

    def paused(*args):
        result = real_capture(*args)
        entered.set()
        assert release.wait(10)
        return result

    # Construct a target through the real service by returning the wanted
    # record to its original pre-reconciliation state.
    with sqlite3.connect(rescan_env["db_path"]) as db:
        db.execute(
            "UPDATE volumes SET status='wanted',import_path=NULL WHERE series_id=7"
        )
    monkeypatch.setattr(recovery, "_capture_source", paused)

    async def exercise():
        task = asyncio.create_task(
            recovery.rescan_series_in_thread(7, rescan.rescan_series_folder)
        )
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not path.exists()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(exercise())
    finally:
        release.set()
    assert path.is_file()
    assert path.read_bytes() != original
    assert _rows(rescan_env)[0]["state"] == "completed"


def test_cancelled_series_settles_current_file_without_starting_next(
    rescan_env, monkeypatch
):
    import rescan
    import rescan_file_recovery as recovery
    from test_rescan_transactions import _insert_volume
    import zipfile

    first, _, _, _ = _fixture(rescan_env)
    second = first.parent / "Race Manga v02.cbz"
    with zipfile.ZipFile(second, "w") as archive:
        archive.writestr("001.jpg", b"second original")
    second_original = second.read_bytes()
    _insert_volume(rescan_env["db_path"], 2, "wanted")
    with sqlite3.connect(rescan_env["db_path"]) as db:
        db.execute(
            "UPDATE volumes SET status='wanted',import_path=NULL WHERE series_id=7 AND volume_num=1"
        )
    entered = threading.Event()
    release = threading.Event()
    seen = []
    real_capture = recovery._capture_source

    def pause(operation, guard):
        result = real_capture(operation, guard)
        seen.append(operation.row["source_path"])
        entered.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(recovery, "_capture_source", pause)

    async def exercise():
        task = asyncio.create_task(
            recovery.rescan_series_in_thread(7, rescan.rescan_series_folder)
        )
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(exercise())
    finally:
        release.set()
    assert seen == [str(first)]
    assert second.read_bytes() == second_original
    assert len(_rows(rescan_env)) == 1


def test_runtime_loop_advances_cursor_past_blocked_rows_and_wraps(
    rescan_env, monkeypatch
):
    import tasks
    import rescan_file_recovery as recovery

    calls = []
    real_sleep = asyncio.sleep

    async def replay(*, after_id, limit):
        calls.append((after_id, limit))
        if len(calls) == 4:
            raise asyncio.CancelledError
        return {
            "last_id": 100 if len(calls) == 1 else 101 if len(calls) == 2 else 101,
            "selected": 100 if len(calls) == 1 else 1 if len(calls) == 2 else 0,
            "outcomes": {"blocked": 100 if len(calls) == 1 else 1},
        }

    async def short_sleep(delay):
        assert 0 < delay <= 60
        await real_sleep(0)

    monkeypatch.setattr(recovery, "replay_rescan_file_operations", replay)
    monkeypatch.setattr(tasks.asyncio, "sleep", short_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(tasks.rescan_replay_loop())
    assert calls == [(0, 100), (100, 100), (101, 100), (0, 100)]


def test_cancelled_adoption_waits_for_owned_file_and_skips_second(
    rescan_env, monkeypatch
):
    import library_scan
    import rescan_file_recovery as recovery
    import zipfile

    directory = Path(rescan_env["library_root"]) / "New Adoption"
    directory.mkdir()
    paths = [directory / f"New Adoption v{number:02}.cbz" for number in (1, 2)]
    for path in paths:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("001.jpg", b"original page")
    second = paths[1].read_bytes()
    entered = threading.Event()
    release = threading.Event()
    seen = []
    real_capture = recovery._capture_source

    def pause(operation, guard):
        result = real_capture(operation, guard)
        seen.append(operation.row["source_path"])
        entered.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(recovery, "_capture_source", pause)

    async def exercise():
        task = asyncio.create_task(
            recovery.adopt_folder_in_thread(
                library_scan.adopt_unmapped_folder, 1, str(directory)
            )
        )
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(exercise())
    finally:
        release.set()
    assert seen == [str(paths[0])]
    assert paths[1].read_bytes() == second
    assert _rows(rescan_env)[0]["state"] == "completed"
