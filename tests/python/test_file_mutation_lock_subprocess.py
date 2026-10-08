"""Borrowed sidecar capabilities verify authority without changing ownership."""

from __future__ import annotations

import fcntl
import os
import select
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NoReturn

import pytest


def test_subprocess_fd_is_borrowed_cloexec_and_does_not_reopen_or_relock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from file_mutation_lock import FileMutationBusy, file_mutation_guard

    database = tmp_path / "local.db"
    database.write_bytes(b"database")
    with file_mutation_guard(str(database)) as guard:
        before = set(os.listdir("/proc/self/fd"))

        def unexpected(*_args: object, **_kwargs: object) -> NoReturn:
            pytest.fail("accessor changed ownership instead of borrowing it")

        with monkeypatch.context() as patch:
            patch.setattr(os, "open", unexpected)
            patch.setattr(os, "dup", unexpected)
            patch.setattr(fcntl, "flock", unexpected)
            descriptor = guard.subprocess_fd
            assert guard.subprocess_fd == descriptor
        assert set(os.listdir("/proc/self/fd")) == before
        assert descriptor >= 3
        assert not os.get_inheritable(descriptor)
        held = os.fstat(descriptor)
        sidecar = (tmp_path / ".local.db.file-mutation.lock").stat()
        assert (held.st_dev, held.st_ino) == (sidecar.st_dev, sidecar.st_ino)
        assert (held.st_dev, held.st_ino) != (
            database.stat().st_dev,
            database.stat().st_ino,
        )
        with pytest.raises(FileMutationBusy):
            with file_mutation_guard(str(database)):
                pytest.fail("borrow released original ownership")


def test_subprocess_fd_refuses_thread_transfer(tmp_path: Path) -> None:
    from file_mutation_lock import FileMutationLockError, file_mutation_guard

    database = tmp_path / "local.db"
    database.write_bytes(b"database")
    with file_mutation_guard(str(database)) as guard:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(FileMutationLockError):
                pool.submit(lambda: guard.subprocess_fd).result(timeout=5)
        assert guard.subprocess_fd >= 3


def test_subprocess_fd_refuses_inherited_raw_fork_guard(tmp_path: Path) -> None:
    import file_mutation_lock

    database = tmp_path / "local.db"
    database.write_bytes(b"database")
    assert file_mutation_lock.__file__ is not None
    code = """
import os, sys
sys.path.insert(0, sys.argv[2])
from file_mutation_lock import FileMutationLockError, file_mutation_guard
with file_mutation_guard(sys.argv[1]) as guard:
    pid = os.fork()
    if pid == 0:
        try:
            guard.subprocess_fd
        except FileMutationLockError:
            os._exit(0)
        except BaseException:
            os._exit(2)
        os._exit(1)
    _, status = os.waitpid(pid, 0)
    if os.waitstatus_to_exitcode(status) != 0:
        raise RuntimeError('raw fork copy received a subprocess capability')
    guard.verify()
"""
    subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(database),
            str(Path(file_mutation_lock.__file__).parent),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )


@pytest.mark.parametrize("changed", ["database", "sidecar", "parent"])
def test_subprocess_fd_rechecks_path_identity(tmp_path: Path, changed: str) -> None:
    from file_mutation_lock import FileMutationLockError, file_mutation_guard

    parent = tmp_path / "config"
    parent.mkdir(mode=0o700)
    database = parent / "local.db"
    database.write_bytes(b"database")
    sidecar = parent / ".local.db.file-mutation.lock"
    with pytest.raises(FileMutationLockError):
        with file_mutation_guard(str(database)) as guard:
            if changed == "parent":
                original = tmp_path / "original-config"
                parent.rename(original)
                parent.mkdir(mode=0o700)
                os.link(original / database.name, database)
                sidecar.touch(mode=0o600)
            else:
                target = database if changed == "database" else sidecar
                target.rename(target.with_suffix(".original"))
                target.touch(mode=0o600)
            with pytest.raises(FileMutationLockError):
                guard.subprocess_fd


def test_subprocess_fd_refuses_a_changed_inheritable_flag(tmp_path: Path) -> None:
    from file_mutation_lock import FileMutationLockError, file_mutation_guard

    database = tmp_path / "local.db"
    database.write_bytes(b"database")
    with file_mutation_guard(str(database)) as guard:
        descriptor = guard.subprocess_fd
        os.set_inheritable(descriptor, True)
        try:
            with pytest.raises(FileMutationLockError):
                guard.subprocess_fd
            assert os.get_inheritable(descriptor), "getter must not mutate flags"
        finally:
            os.set_inheritable(descriptor, False)
        assert guard.subprocess_fd == descriptor


def test_normal_guard_exit_preserves_deliberately_inherited_child_hold(
    tmp_path: Path,
) -> None:
    from file_mutation_lock import (
        FileMutationBusy,
        FileMutationLockError,
        file_mutation_guard,
    )

    database = tmp_path / "local.db"
    database.write_bytes(b"database")
    child: subprocess.Popen[str] | None = None
    code = """
import os, sys
os.fstat(int(sys.argv[1]))
print('inherited', flush=True)
sys.stdin.read(1)
"""
    try:
        with file_mutation_guard(str(database)) as guard:
            descriptor = guard.subprocess_fd
            child = subprocess.Popen(
                [sys.executable, "-c", code, str(descriptor)],
                close_fds=True,
                pass_fds=(descriptor,),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert child.stdout is not None
            assert select.select([child.stdout], [], [], 5)[0]
            assert child.stdout.readline().strip() == "inherited"
        with pytest.raises(FileMutationLockError):
            guard.subprocess_fd
        with pytest.raises(FileMutationBusy):
            with file_mutation_guard(str(database)):
                pytest.fail("guard exit unlocked its child's shared description")
        child.communicate(input="x", timeout=5)
        assert child.returncode == 0
        with file_mutation_guard(str(database)) as successor:
            successor.verify()
    finally:
        if child is not None:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
