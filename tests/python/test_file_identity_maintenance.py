"""Offline ownership recovery excludes every cooperating service process."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Generator
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import threading
from typing import Any

import pytest

from test_root_folder_bootstrap import isolated_state as isolated_state


_CHILD = """
import json, sys
from maintenance_lock import service_maintenance_guard, MaintenanceBusy
try:
    with service_maintenance_guard(sys.argv[1], exclusive=sys.argv[2] == 'exclusive'):
        print(json.dumps({'state': 'held'}), flush=True)
        if sys.argv[3] == 'hold':
            sys.stdin.readline()
except MaintenanceBusy:
    print(json.dumps({'state': 'busy'}), flush=True)
"""


def _command(db_path: str, exclusive: bool, action: str) -> list[str]:
    return [
        sys.executable,
        "-c",
        _CHILD,
        db_path,
        "exclusive" if exclusive else "shared",
        action,
    ]


def _child_env() -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "app"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _attempt(db_path: str, *, exclusive: bool) -> str:
    result = subprocess.run(
        _command(db_path, exclusive, "attempt"),
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return str(json.loads(result.stdout)["state"])


@contextmanager
def _holder(
    db_path: str, *, exclusive: bool
) -> Generator[subprocess.Popen[str], None, None]:
    process = subprocess.Popen(
        _command(db_path, exclusive, "hold"),
        env=_child_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert select.select([process.stdout], [], [], 10)[0], "holder never answered"
        answer = process.stdout.readline()
        if not answer:
            assert process.stderr is not None
            pytest.fail(process.stderr.read())
        assert json.loads(answer) == {"state": "held"}
        yield process
    finally:
        if process.poll() is None:
            assert process.stdin is not None
            process.stdin.write("release\n")
            process.stdin.flush()
        try:
            _, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        assert process.returncode in (0, -9), stderr


def _lock_path(db_path: str | Path) -> Path:
    path = Path(db_path)
    return path.with_name(f".{path.name}.ownership-maintenance.lock")


def test_exact_enrollment_requires_live_same_thread_exclusive(tmp_path: Path) -> None:
    from maintenance_lock import (
        MaintenanceLockError,
        _assert_exclusive_maintenance,
        service_maintenance_guard,
    )

    path = str(tmp_path / "local.db")
    with pytest.raises(MaintenanceLockError):
        _assert_exclusive_maintenance(path)
    with service_maintenance_guard(path, exclusive=False) as result:
        assert result is None
        with pytest.raises(MaintenanceLockError):
            _assert_exclusive_maintenance(path)
    failures: list[BaseException] = []
    with service_maintenance_guard(path, exclusive=True) as result:
        assert result is None
        _assert_exclusive_maintenance(path)
        with pytest.raises(MaintenanceLockError):
            _assert_exclusive_maintenance(str(tmp_path / "other.db"))

        def transferred() -> None:
            try:
                _assert_exclusive_maintenance(path)
            except BaseException as exc:
                failures.append(exc)

        thread = threading.Thread(target=transferred)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert len(failures) == 1 and isinstance(failures[0], MaintenanceLockError)
    with pytest.raises(MaintenanceLockError):
        _assert_exclusive_maintenance(path)


@pytest.mark.parametrize("exists", [False, True])
def test_shared_services_coexist_and_last_exit_allows_recovery(
    tmp_path: Path, exists: bool
) -> None:
    db = tmp_path / "local.db"
    if exists:
        db.write_bytes(b"database unchanged")
    with _holder(str(db), exclusive=False):
        lock = _lock_path(db)
        original_inode = lock.stat().st_ino
        assert lock.stat().st_size == 0
        assert lock.stat().st_mode & 0o777 == 0o600
        assert lock.stat().st_uid == os.geteuid()
        before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
        with _holder(str(db), exclusive=False):
            assert _attempt(str(db), exclusive=True) == "busy"
        assert _attempt(str(db), exclusive=True) == "busy"
        assert before == {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    assert _attempt(str(db), exclusive=True) == "held"
    assert lock.stat().st_ino == original_inode
    assert lock.stat().st_size == 0
    assert db.exists() is exists


@pytest.mark.parametrize("exclusive", [False, True])
def test_process_death_releases_but_never_recreates_entry(
    tmp_path: Path, exclusive: bool
) -> None:
    db = tmp_path / "missing.db"
    with _holder(str(db), exclusive=exclusive) as process:
        lock = _lock_path(db)
        original_inode = lock.stat().st_ino
        assert _attempt(str(db), exclusive=True) == "busy"
        if exclusive:
            assert _attempt(str(db), exclusive=False) == "busy"
        process.kill()
        process.wait(timeout=10)
    assert _attempt(str(db), exclusive=True) == "held"
    assert lock.stat().st_ino == original_inode
    assert lock.stat().st_size == 0
    assert not db.exists()


@pytest.mark.parametrize(
    "kind",
    [
        "symlink",
        "directory",
        "fifo",
        "nonempty",
        "hardlink",
        "db-alias",
        "mode",
        "foreign",
    ],
)
def test_unsafe_sidecars_refuse_before_any_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from maintenance_lock import MaintenanceLockError, service_maintenance_guard

    db = tmp_path / "local.db"
    db.touch(mode=0o600)
    lock = _lock_path(db)
    if kind == "symlink":
        lock.symlink_to(db)
    elif kind == "directory":
        lock.mkdir()
    elif kind == "fifo":
        os.mkfifo(lock, mode=0o600)
    elif kind == "db-alias":
        os.link(db, lock)
    else:
        lock.write_bytes(b"invalid" if kind == "nonempty" else b"")
        lock.chmod(0o640 if kind == "mode" else 0o600)
        if kind == "hardlink":
            os.link(lock, tmp_path / "alias")
        if kind == "foreign":
            uid = os.geteuid()
            monkeypatch.setattr(os, "geteuid", lambda: uid + 1)
    before = lock.lstat()

    def forbidden_open(*args: Any, **kwargs: Any) -> int:
        del args, kwargs
        pytest.fail("unsafe sidecar or SQLite inode opened")

    monkeypatch.setattr(os, "open", forbidden_open)
    with pytest.raises(MaintenanceLockError):
        with service_maintenance_guard(str(db), exclusive=True):
            pytest.fail("unsafe sidecar accepted")
    assert lock.lstat() == before
    assert db.stat().st_size == 0


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_database_must_be_regular_if_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from maintenance_lock import MaintenanceLockError, service_maintenance_guard

    db = tmp_path / "local.db"
    if kind == "symlink":
        target = tmp_path / "target"
        target.touch()
        db.symlink_to(target)
    elif kind == "directory":
        db.mkdir()
    else:
        os.mkfifo(db)
    monkeypatch.setattr(os, "open", lambda *a, **kw: pytest.fail("DB opened"))
    with pytest.raises(MaintenanceLockError):
        with service_maintenance_guard(str(db), exclusive=False):
            pytest.fail("nonregular DB accepted")
    assert not _lock_path(db).exists()


@pytest.mark.parametrize(
    "mode,allowed", [(0o777, False), (0o770, True), (0o2770, True)]
)
def test_parent_policy(tmp_path: Path, mode: int, allowed: bool) -> None:
    from maintenance_lock import MaintenanceLockError, service_maintenance_guard

    parent = tmp_path / "config"
    parent.mkdir()
    parent.chmod(mode)
    db = parent / "new.db"
    if allowed:
        with service_maintenance_guard(str(db), exclusive=False):
            assert not db.exists()
    else:
        with pytest.raises(MaintenanceLockError):
            with service_maintenance_guard(str(db), exclusive=False):
                pytest.fail("untrusted parent accepted")
        assert list(parent.iterdir()) == []
    assert parent.stat().st_mode & 0o7777 == mode


@pytest.mark.parametrize("changed", ["sidecar", "parent", "database"])
def test_preacquisition_swap_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    from maintenance_lock import MaintenanceLockError, service_maintenance_guard

    parent = tmp_path / "config"
    parent.mkdir()
    db = parent / "local.db"
    db.touch(mode=0o600)
    lock = _lock_path(db)
    lock.touch(mode=0o600)
    original_inode = lock.stat().st_ino
    real_open = os.open
    swapped = False

    def swapping_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal swapped
        assert os.fspath(path) != str(db), "SQLite inode opened"
        if os.fspath(path) == str(lock) and not swapped:
            swapped = True
            if changed == "parent":
                parent.rename(tmp_path / "original-parent")
                parent.mkdir()
                os.close(real_open(db, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
                os.close(real_open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            else:
                target = lock if changed == "sidecar" else db
                target.rename(parent / "original")
                os.close(real_open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swapping_open)
    with pytest.raises(MaintenanceLockError):
        with service_maintenance_guard(str(db), exclusive=False):
            pytest.fail("preflight identity changed")
    retained = (
        tmp_path / "original-parent" / lock.name
        if changed == "parent"
        else parent / "original"
        if changed == "sidecar"
        else lock
    )
    assert retained.stat().st_ino == original_inode


def test_descriptor_is_cloexec_close_only_and_database_never_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from maintenance_lock import service_maintenance_guard

    db = tmp_path / "local.db"
    db.write_bytes(b"unchanged")
    real_open, real_flock = os.open, fcntl.flock
    operations: list[int] = []

    def checked_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        assert os.fspath(path) != str(db)
        descriptor = real_open(path, flags, *args, **kwargs)
        assert not os.get_inheritable(descriptor)
        return descriptor

    def checked_flock(descriptor: int, operation: int) -> None:
        operations.append(operation)
        assert operation != fcntl.LOCK_UN
        real_flock(descriptor, operation)

    monkeypatch.setattr(os, "open", checked_open)
    monkeypatch.setattr(fcntl, "flock", checked_flock)
    with service_maintenance_guard(str(db), exclusive=False):
        assert _attempt(str(db), exclusive=True) == "busy"
    assert operations == [fcntl.LOCK_SH | fcntl.LOCK_NB]
    assert db.read_bytes() == b"unchanged"


def test_two_process_lock_order_maintenance_then_mutation_then_sql(
    tmp_path: Path,
) -> None:
    from file_mutation_lock import file_mutation_guard

    db = tmp_path / "local.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE proof(value TEXT)")
    script = """
import sys, sqlite3
from maintenance_lock import service_maintenance_guard, MaintenanceBusy
from file_mutation_lock import file_mutation_guard, FileMutationBusy
try:
    with service_maintenance_guard(sys.argv[1], exclusive=True):
        print('maintenance', flush=True)
        with file_mutation_guard(sys.argv[1]):
            print('mutation', flush=True)
            with sqlite3.connect(sys.argv[1]) as db:
                db.execute("INSERT INTO proof VALUES('sql')")
            print('sql', flush=True)
except MaintenanceBusy:
    print('maintenance busy')
except FileMutationBusy:
    print('mutation busy')
"""

    def attempt() -> list[str]:
        result = subprocess.run(
            [sys.executable, "-c", script, str(db)],
            env=_child_env(),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.splitlines()

    with _holder(str(db), exclusive=False):
        assert attempt() == ["maintenance busy"]
        assert not (tmp_path / ".local.db.file-mutation.lock").exists()
    with file_mutation_guard(str(db)):
        assert attempt() == ["maintenance", "mutation busy"]
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT * FROM proof").fetchall() == []
    assert attempt() == ["maintenance", "mutation", "sql"]
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT * FROM proof").fetchall() == [("sql",)]


def test_lifespan_holds_before_init_and_releases_on_startup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import main

    db = tmp_path / "fresh.db"
    monkeypatch.setattr(main, "DB_PATH", str(db))

    class StopStartup(Exception):
        pass

    def init_probe() -> None:
        assert _lock_path(db).exists(), "maintenance hold missing before init_db"
        assert not db.exists(), "guard must not create SQLite inode"
        assert _attempt(str(db), exclusive=True) == "busy"
        raise StopStartup

    monkeypatch.setattr(main, "init_db", init_probe)

    async def exercise() -> None:
        with pytest.raises(StopStartup):
            async with main.lifespan(main.app):
                pytest.fail("startup should fail")

    asyncio.run(exercise())
    assert _attempt(str(db), exclusive=True) == "held"
    assert not db.exists()


def test_lifespan_refuses_recovery_before_schema_or_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import main
    from maintenance_lock import MaintenanceBusy

    db = tmp_path / "fresh.db"
    monkeypatch.setattr(main, "DB_PATH", str(db))
    monkeypatch.setattr(
        main, "init_db", lambda: pytest.fail("schema reached while recovery holds")
    )

    async def exercise() -> None:
        with pytest.raises(MaintenanceBusy):
            async with main.lifespan(main.app):
                pytest.fail("service admitted during recovery")

    with _holder(str(db), exclusive=True):
        asyncio.run(exercise())
        assert not db.exists()


def test_startup_cancellation_settles_pack_thread_before_releasing_hold(
    isolated_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import auth
    import main
    import security
    from types import SimpleNamespace

    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(auth, "remove_legacy_setup_token", lambda: None)
    monkeypatch.setattr(security, "load_or_create_secret_cipher", lambda path: None)

    def replay(**kwargs: Any) -> SimpleNamespace:
        del kwargs
        entered.set()
        assert release.wait(10)
        assert _attempt(isolated_state, exclusive=True) == "busy"
        return SimpleNamespace(
            reservations_recovered=0, tombstones_removed=0, tombstones_retained=0
        )

    monkeypatch.setattr(main, "recover_pack_cleanup_state", replay)

    async def startup() -> None:
        async with main.lifespan(main.app):
            pytest.fail("cancelled startup admitted producers")

    async def exercise() -> None:
        task = asyncio.create_task(startup())
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done(), "hold released before startup thread settled"
                assert _attempt(isolated_state, exclusive=True) == "busy"
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(exercise())
    finally:
        release.set()
    assert _attempt(isolated_state, exclusive=True) == "held"


@pytest.mark.parametrize("actor", ["chapter", "deletion", "publication"])
def test_actual_startup_delegate_settles_current_thread_on_repeated_cancel(
    isolated_state: str, monkeypatch: pytest.MonkeyPatch, actor: str
) -> None:
    import auth
    import import_publication
    import main
    import security
    import volume_chapter_cleanup
    import volume_file_deletion
    from types import SimpleNamespace

    entered, release, settled = threading.Event(), threading.Event(), threading.Event()
    calls: list[int] = []
    monkeypatch.setattr(auth, "remove_legacy_setup_token", lambda: None)
    monkeypatch.setattr(security, "load_or_create_secret_cipher", lambda path: None)

    def owned_work(identifier: int, **_kwargs: Any) -> str | bool:
        calls.append(identifier)
        entered.set()
        try:
            assert release.wait(10)
            assert _attempt(isolated_state, exclusive=True) == "busy"
            return True if actor == "publication" else "terminal"
        finally:
            settled.set()

    if actor == "chapter":
        monkeypatch.setattr(volume_chapter_cleanup, "active_ids", lambda **_kw: [1, 2])
        monkeypatch.setattr(volume_chapter_cleanup, "replay", owned_work)
    elif actor == "deletion":
        monkeypatch.setattr(volume_file_deletion, "_replay_lock", None)
        monkeypatch.setattr(
            volume_file_deletion, "active_deletion_journal_ids", lambda **_kw: [1, 2]
        )
        monkeypatch.setattr(
            volume_file_deletion, "replay_volume_file_deletion", owned_work
        )
    else:
        monkeypatch.setattr(import_publication, "_replay_lock", None)
        monkeypatch.setattr(
            import_publication, "pending_publication_ids", lambda *_a, **_kw: [1, 2]
        )
        monkeypatch.setattr(
            import_publication,
            "load_publication",
            lambda *_a, **_kw: SimpleNamespace(state="staging"),
        )
        monkeypatch.setattr(import_publication, "abort_private_staging", owned_work)

    async def startup() -> None:
        async with main.lifespan(main.app):
            pytest.fail("cancelled startup admitted producers")

    async def exercise() -> None:
        task = asyncio.create_task(startup())
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                state = {
                    "lifespan_done": task.done(),
                    "thread_settled": settled.is_set(),
                    "exclusion": _attempt(isolated_state, exclusive=True),
                }
                assert state == {
                    "lifespan_done": False,
                    "thread_settled": False,
                    "exclusion": "busy",
                }, state
        finally:
            release.set()
            assert await asyncio.to_thread(settled.wait, 10)
            await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        assert calls == [1], "cancellation must not start the next filesystem unit"

    asyncio.run(exercise())
    assert _attempt(isolated_state, exclusive=True) == "held"


@pytest.mark.parametrize("repeat_cancel", [False, True])
@pytest.mark.parametrize("worker_fails", [False, True])
def test_actual_backup_loop_settles_thread_before_lifespan_releases(
    isolated_state: str,
    monkeypatch: pytest.MonkeyPatch,
    repeat_cancel: bool,
    worker_fails: bool,
) -> None:
    import auth
    import main
    import routers.system as system
    import security
    import tasks

    entered, release, settled = threading.Event(), threading.Event(), threading.Event()
    exit_service, service_ready = asyncio.Event(), asyncio.Event()
    worker_settling: asyncio.Queue[None] = asyncio.Queue()
    service_settling: asyncio.Queue[None] = asyncio.Queue()
    service_task: asyncio.Task[None] | None = None
    registered: list[asyncio.Task[None]] = []
    real_sleep = asyncio.sleep
    real_shield = asyncio.shield
    calls: list[str] = []
    monkeypatch.setattr(auth, "remove_legacy_setup_token", lambda: None)
    monkeypatch.setattr(security, "load_or_create_secret_cipher", lambda path: None)

    async def skip_initial_delay(delay: float, *_args: Any, **_kwargs: Any) -> None:
        await real_sleep(0 if delay == 3600 else delay)

    monkeypatch.setattr(tasks.asyncio, "sleep", skip_initial_delay)

    def observed_shield(awaitable: Awaitable[Any]) -> asyncio.Future[Any]:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            if task in registered:
                worker_settling.put_nowait(None)
            elif task is service_task:
                service_settling.put_nowait(None)
        return real_shield(awaitable)

    monkeypatch.setattr(tasks.asyncio, "shield", observed_shield)

    def backup(prefix: str) -> tuple[str, str]:
        calls.append(prefix)
        entered.set()
        try:
            assert release.wait(10)
            assert _attempt(isolated_state, exclusive=True) == "busy"
            if worker_fails:
                raise OSError("synthetic backup boundary failure")
            return "unused.zip", "unused.zip"
        finally:
            settled.set()

    monkeypatch.setattr(system, "_create_backup_file", backup)

    def register(coro: Coroutine[Any, Any, Any], *, name: str) -> None:
        if name == "backup_loop":
            registered.append(tasks.create_background_task(coro, name=name))
        else:
            coro.close()

    monkeypatch.setattr(main, "create_background_task", register)

    async def service() -> None:
        async with main.lifespan(main.app):
            assert await asyncio.to_thread(entered.wait, 10)
            service_ready.set()
            await exit_service.wait()

    async def exercise() -> None:
        nonlocal service_task
        task = asyncio.create_task(service())
        service_task = task
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            await asyncio.wait_for(service_ready.wait(), 10)
            assert len(registered) == 1
            worker = registered[0]
            exit_service.set()
            await asyncio.wait_for(worker_settling.get(), 10)
            assert worker.cancelling(), (
                "normal shutdown must cancel the actual backup loop"
            )
            assert not settled.is_set()
            assert not worker.done(), "backup coroutine abandoned its filesystem thread"
            assert not task.done()
            assert _attempt(isolated_state, exclusive=True) == "busy"
            if repeat_cancel:
                for _ in range(2):
                    worker.cancel()
                    task.cancel()
                    await asyncio.wait_for(worker_settling.get(), 10)
                    await asyncio.wait_for(service_settling.get(), 10)
                    assert not worker.done()
                    assert not task.done()
                    assert _attempt(isolated_state, exclusive=True) == "busy"
        finally:
            release.set()
            assert await asyncio.to_thread(settled.wait, 10)
            result = await asyncio.gather(task, return_exceptions=True)
        if repeat_cancel:
            assert isinstance(result[0], asyncio.CancelledError)
        else:
            assert result == [None]
        assert registered[0].cancelled()
        assert calls == ["mangarr_auto"]
        assert not main._BACKGROUND_TASKS
        assert not main._IMPORT_WORKERS

    asyncio.run(exercise())
    assert _attempt(isolated_state, exclusive=True) == "held"


def test_backup_shutdown_handles_executor_delivery_inversion(
    isolated_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_to_thread, real_sleep = asyncio.to_thread, asyncio.sleep
    parked, deliver = asyncio.Event(), asyncio.Event()
    service_delivered = False
    exercise_waited = False

    async def inverted(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        nonlocal service_delivered, exercise_waited
        task = asyncio.current_task()
        assert task is not None
        name = task.get_coro().__qualname__
        result = await real_to_thread(func, *args, **kwargs)
        if name.endswith(".<locals>.service"):
            parked.set()
            await deliver.wait()
            service_delivered = True
        elif name.endswith(".<locals>.exercise") and not exercise_waited:
            exercise_waited = True
            await parked.wait()
            asyncio.get_running_loop().call_soon(deliver.set)
        return result

    async def no_executor_delivery_on_yield(
        delay: float, *args: Any, **kwargs: Any
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        if (
            delay == 0
            and not service_delivered
            and task.get_coro().__qualname__.endswith(".<locals>.exercise")
        ):
            return
        await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inverted)
    monkeypatch.setattr(asyncio, "sleep", no_executor_delivery_on_yield)
    test_actual_backup_loop_settles_thread_before_lifespan_releases(
        isolated_state, monkeypatch, False, False
    )
    assert exercise_waited and service_delivered


def test_repeated_shutdown_cancellation_keeps_hold_until_workers_settle(
    isolated_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import auth
    import main
    import security

    serving, stopping, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observed: list[str] = []
    monkeypatch.setattr(auth, "remove_legacy_setup_token", lambda: None)
    monkeypatch.setattr(security, "load_or_create_secret_cipher", lambda path: None)

    def discard(coro: Coroutine[Any, Any, Any], *, name: str) -> None:
        del name
        coro.close()

    async def background() -> None:
        stopping.set()
        await release.wait()
        assert _attempt(isolated_state, exclusive=True) == "busy"
        observed.append("background")

    async def workers() -> None:
        await asyncio.sleep(0)
        assert _attempt(isolated_state, exclusive=True) == "busy"
        observed.append("workers")

    monkeypatch.setattr(main, "create_background_task", discard)
    monkeypatch.setattr(main, "start_import_worker_scheduling", lambda: None)
    monkeypatch.setattr(main, "stop_import_worker_scheduling", lambda: None)
    monkeypatch.setattr(main, "_cancel_background_tasks", background)
    monkeypatch.setattr(main, "cancel_import_workers", workers)

    async def lifecycle() -> None:
        async with main.lifespan(main.app):
            serving.set()
            await asyncio.Event().wait()

    async def exercise() -> None:
        task = asyncio.create_task(lifecycle())
        try:
            await asyncio.wait_for(serving.wait(), timeout=10)
            task.cancel()
            await asyncio.wait_for(stopping.wait(), timeout=10)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done(), "shutdown abandoned live workers"
                assert _attempt(isolated_state, exclusive=True) == "busy"
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert observed == ["background", "workers"]
    assert _attempt(isolated_state, exclusive=True) == "held"


@pytest.mark.parametrize("outcome", ["normal", "cancel", "startup-failure"])
def test_lifespan_retains_hold_through_replay_and_ordered_shutdown(
    isolated_state: str, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    import auth
    import main
    import security

    observed: list[str] = []

    def held(stage: str) -> None:
        assert _attempt(isolated_state, exclusive=True) == "busy", stage
        observed.append(stage)

    monkeypatch.setattr(auth, "remove_legacy_setup_token", lambda: None)
    monkeypatch.setattr(security, "load_or_create_secret_cipher", lambda path: None)
    real_replay = main.drain_active_rescan_file_operations

    async def replay(**kwargs: Any) -> dict[str, int]:
        held("replay")
        return await real_replay(**kwargs)

    monkeypatch.setattr(main, "drain_active_rescan_file_operations", replay)

    def discard(coro: Coroutine[Any, Any, Any], *, name: str) -> None:
        del name
        coro.close()
        if outcome == "startup-failure":
            raise ValueError("producer registration failed")

    def start() -> None:
        held("start")

    def stop() -> None:
        held("stop")

    async def cancel_background() -> None:
        await asyncio.sleep(0)
        held("background settled")

    async def cancel_workers() -> None:
        await asyncio.sleep(0)
        held("workers settled")

    monkeypatch.setattr(main, "create_background_task", discard)
    monkeypatch.setattr(main, "start_import_worker_scheduling", start)
    monkeypatch.setattr(main, "stop_import_worker_scheduling", stop)
    monkeypatch.setattr(main, "_cancel_background_tasks", cancel_background)
    monkeypatch.setattr(main, "cancel_import_workers", cancel_workers)

    async def exercise() -> None:
        async with main.lifespan(main.app):
            held("serving")
            if outcome == "cancel":
                task = asyncio.current_task()
                assert task is not None
                task.cancel()
                await asyncio.sleep(0)

    if outcome == "normal":
        asyncio.run(exercise())
    else:
        expected = asyncio.CancelledError if outcome == "cancel" else ValueError
        with pytest.raises(expected):
            asyncio.run(exercise())
    assert observed == [
        "replay",
        "start",
        *([] if outcome == "startup-failure" else ["serving"]),
        "stop",
        "background settled",
        "workers settled",
    ]
    inode = _lock_path(isolated_state).stat().st_ino
    assert _attempt(isolated_state, exclusive=True) == "held"
    assert _lock_path(isolated_state).stat().st_ino == inode
    assert _lock_path(isolated_state).stat().st_size == 0
