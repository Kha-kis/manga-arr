"""Deliberate extractor inheritance of the shared sidecar open description.

Run against the shared guard module when Step 1 is integrated, or expose that
reviewed module on PYTHONPATH before integration. No private guard fields are
used here. This qualifies the subprocess capability, not the full pack adapter.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from test_import_pack_7z_output_pinning import (
    _FIRST,
    _LATER,
    _PAGE_SIZE,
    _pause_after_output_starts,
    _write_nested_archive,
)
from test_import_pack_cleanup_durability import (
    _PackEnv,
    _pack_paths,
    pack_env,  # noqa: F401
)

_WRITER = """
import json, os, signal, sys, zipfile
output_fd, sidecar_fd = map(int, sys.argv[1:])
base = '/proc/self/fd/' + str(output_fd)
lock = os.fstat(sidecar_fd)
with open(base + '/child-evidence.json', 'w') as stream:
    json.dump({'sidecar': [lock.st_dev, lock.st_ino]}, stream)
os.kill(os.getpid(), signal.SIGSTOP)
with zipfile.ZipFile(base + '/owned.cbz', 'w') as archive:
    archive.writestr('001.jpg', b'pinned-page')
"""

_OWNER = """
import json, os, pathlib, subprocess, sys, time
sys.path.insert(0, sys.argv[3])
from file_mutation_lock import file_mutation_guard
with file_mutation_guard(sys.argv[1]) as guard:
    sidecar_fd = guard.subprocess_fd
    output_fd = os.open(sys.argv[2], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    child = None
    try:
        real_engine = sys.argv[4] == '7z'
        if real_engine:
            sys.path.insert(0, sys.argv[5])
            from test_import_pack_7z_output_pinning import _pause_after_output_starts
        command = ([sys.argv[6], 'x', '-y', '-o/proc/self/fd/' + str(output_fd), sys.argv[7]]
                   if real_engine else [sys.executable, '-c', sys.argv[4], str(output_fd), str(sidecar_fd)])
        child = subprocess.Popen(
            command,
            pass_fds=(sidecar_fd, output_fd) if sys.argv[-1] == 'inherit' else (output_fd,),
            close_fds=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        if real_engine:
            _pause_after_output_starts(child, pathlib.Path(sys.argv[2]))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = pathlib.Path('/proc/' + str(child.pid) + '/status').read_text()
            if any(line.startswith('State:') and 'T' in line.split()[1] for line in status.splitlines()):
                break
            if child.poll() is not None:
                raise RuntimeError('extractor exited before pause')
            time.sleep(0.01)
        else:
            child.kill()
            child.wait()
            raise RuntimeError('extractor did not pause')
        print(json.dumps({'pid': child.pid, 'sidecar_fd': sidecar_fd}), flush=True)
        child.wait()
    finally:
        if child is not None:
            if child.poll() is None:
                child.kill()
            child.communicate()
        os.close(output_fd)
"""


def _borrowed_subprocess_fd(guard: object, database: str) -> int:
    descriptor = getattr(guard, "subprocess_fd", None)
    assert isinstance(descriptor, int), (
        "shared guard needs verified borrowed subprocess_fd"
    )
    assert descriptor >= 3, "stdio redirection must not replace the inherited guard"
    held = os.fstat(descriptor)
    database_stat = os.lstat(database)
    assert (held.st_dev, held.st_ino) != (database_stat.st_dev, database_stat.st_ino)
    assert stat.S_ISREG(held.st_mode)
    assert stat.S_IMODE(held.st_mode) == 0o600
    assert held.st_size == 0
    assert not os.get_inheritable(descriptor), "inheritance must be pass_fds-only"
    return descriptor


@pytest.mark.skipif(
    sys.platform != "linux", reason="pack proc-FD/sidecar contract is Linux"
)
@pytest.mark.parametrize(
    ("engine", "inherit"),
    [("python", True), ("7z", True), ("7z", False)],
    ids=["python", "7z", "7z-omission-control"],
)
def test_paused_extractor_excludes_takeover_after_parent_sigkill_and_path_replacement(
    pack_env: _PackEnv,
    engine: str,
    inherit: bool,
) -> None:
    import file_mutation_lock
    import import_pack_cleanup
    import main

    with main.get_db() as db:
        owner_token = import_pack_cleanup.reserve_pack_queue_creation(
            db, "orphan-extractor", download_client_id=None, protocol=None
        )
    assert owner_token is not None
    _, private = _pack_paths("orphan-extractor", owner_token)
    private.mkdir(parents=True, mode=0o700)
    engine_args: list[str] = []
    if engine == "7z":
        executable = shutil.which("7zz") or shutil.which("7z") or shutil.which("7za")
        if executable is None:
            pytest.skip("real 7z required for orphan/placement qualification")
        source = pack_env["tmp_path"] / "orphan-source.zip"
        _write_nested_archive(source)
        engine_args = [str(Path(__file__).parent), executable, str(source)]
    with file_mutation_lock.file_mutation_guard(pack_env["db_path"]) as guard:
        held = os.fstat(_borrowed_subprocess_fd(guard, pack_env["db_path"]))
        expected_sidecar = (held.st_dev, held.st_ino)
    assert file_mutation_lock.__file__ is not None
    owner = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _OWNER,
            pack_env["db_path"],
            str(private),
            str(Path(file_mutation_lock.__file__).parent),
            "7z" if engine == "7z" else _WRITER,
            *engine_args,
            "inherit" if inherit else "omit",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    child_pidfd: int | None = None
    try:
        assert owner.stdout is not None
        assert select.select([owner.stdout], [], [], 10)[0], (
            "owner did not report paused child"
        )
        ready_line = owner.stdout.readline()
        if not ready_line:
            pytest.fail(owner.communicate(timeout=5)[1])
        ready = json.loads(ready_line)
        child_pidfd = os.pidfd_open(int(ready["pid"]))
        child_sidecar = f"/proc/{ready['pid']}/fd/{ready['sidecar_fd']}"
        if inherit:
            inherited = os.stat(child_sidecar)
            assert (inherited.st_dev, inherited.st_ino) == expected_sidecar
        else:
            try:
                reused = os.stat(child_sidecar)
            except FileNotFoundError:
                pass
            else:
                assert (reused.st_dev, reused.st_ino) != expected_sidecar
        displaced = private.with_name(private.name + ".displaced")
        private.rename(displaced)
        private.mkdir(mode=0o700)
        (private / "unrelated.cbz").write_bytes(b"leave replacement alone")
        owner.kill()
        owner.wait(timeout=5)
        with sqlite3.connect(pack_env["db_path"]) as db:
            db.execute(
                "UPDATE import_pack_cleanup_reservations"
                " SET expires_at=datetime('now', '-1 second')"
            )
        if inherit:
            with pytest.raises(file_mutation_lock.FileMutationBusy):
                with file_mutation_lock.file_mutation_guard(pack_env["db_path"]):
                    pytest.fail(
                        "expired lease permitted takeover while orphan still writes"
                    )
        else:
            with file_mutation_lock.file_mutation_guard(pack_env["db_path"]) as early:
                early.verify()  # Negative control: omission permits takeover.
        signal.pidfd_send_signal(child_pidfd, signal.SIGCONT)
        assert select.select([child_pidfd], [], [], 10)[0], "extractor did not exit"
        with file_mutation_lock.file_mutation_guard(pack_env["db_path"]) as successor:
            successor.verify()
        if engine == "7z":
            with zipfile.ZipFile(displaced / _FIRST) as archive:
                assert archive.getinfo("001.jpg").file_size == _PAGE_SIZE
                assert archive.testzip() is None
            with zipfile.ZipFile(displaced / _LATER) as archive:
                assert archive.read("002.jpg") == b"later-page"
        else:
            with zipfile.ZipFile(displaced / "owned.cbz") as archive:
                assert archive.read("001.jpg") == b"pinned-page"
        assert {p.name: p.read_bytes() for p in private.iterdir()} == {
            "unrelated.cbz": b"leave replacement alone"
        }
        with sqlite3.connect(pack_env["db_path"]) as db:
            assert db.execute("SELECT COUNT(*) FROM import_queue").fetchone() == (0,)
            assert db.execute(
                "SELECT owner_token FROM import_pack_cleanup_reservations"
            ).fetchone() == (owner_token,)
    finally:
        if child_pidfd is not None:
            try:
                signal.pidfd_send_signal(child_pidfd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            assert select.select([child_pidfd], [], [], 5)[0]
            os.close(child_pidfd)
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=5)


def test_extractor_error_cannot_close_or_reuse_borrowed_owner_guard(
    pack_env: _PackEnv,
) -> None:
    import file_mutation_lock

    private = pack_env["tmp_path"] / "error-output"
    private.mkdir(mode=0o700)
    output_fd = os.open(private, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        with file_mutation_lock.file_mutation_guard(pack_env["db_path"]) as guard:
            sidecar_fd = _borrowed_subprocess_fd(guard, pack_env["db_path"])
            with pytest.raises(subprocess.CalledProcessError):
                subprocess.run(
                    [sys.executable, "-c", "raise SystemExit(7)"],
                    pass_fds=(sidecar_fd, output_fd),
                    close_fds=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True,
                )
            guard.verify()
            assert _borrowed_subprocess_fd(guard, pack_env["db_path"]) == sidecar_fd
            with pytest.raises(file_mutation_lock.FileMutationBusy):
                with file_mutation_lock.file_mutation_guard(pack_env["db_path"]):
                    pytest.fail("extractor exception released borrowed owner")
        with pytest.raises(file_mutation_lock.FileMutationLockError):
            getattr(guard, "subprocess_fd")
        with file_mutation_lock.file_mutation_guard(pack_env["db_path"]) as successor:
            successor.verify()
    finally:
        os.close(output_fd)


def test_extractor_guard_accessor_refuses_unsafe_stdio_descriptor(
    pack_env: _PackEnv,
) -> None:
    import file_mutation_lock

    assert file_mutation_lock.__file__ is not None
    child = """
import os, sys
sys.path.insert(0, sys.argv[2])
from file_mutation_lock import FileMutationLockError, file_mutation_guard
os.close(2)
verdict = 'missing-api'
with file_mutation_guard(sys.argv[1]) as guard:
    try:
        descriptor = getattr(guard, 'subprocess_fd', None)
    except FileMutationLockError:
        verdict = 'safe-refusal'
    else:
        if isinstance(descriptor, int):
            verdict = 'unsafe-stdio' if descriptor < 3 else 'safe-fd'
# Never report errors through stderr while it could be the guard sidecar.
print(verdict, flush=True)
sys.exit(0 if verdict.startswith('safe-') else 1)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            child,
            pack_env["db_path"],
            str(Path(file_mutation_lock.__file__).parent),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout


@pytest.mark.skipif(sys.platform != "linux", reason="Linux real-extractor contract")
def test_real_7z_exception_kills_and_reaps_before_guard_exit(
    pack_env: _PackEnv,
) -> None:
    import file_mutation_lock

    extractor = shutil.which("7zz") or shutil.which("7z") or shutil.which("7za")
    if extractor is None:
        pytest.skip("real 7z required for kill/wait qualification")
    source = pack_env["tmp_path"] / "exception-source.zip"
    _write_nested_archive(source)
    output = pack_env["tmp_path"] / "exception-output"
    output.mkdir(mode=0o700)
    descriptor = os.open(
        output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    child: subprocess.Popen[str] | None = None
    try:
        with file_mutation_lock.file_mutation_guard(pack_env["db_path"]) as guard:
            sidecar_fd = _borrowed_subprocess_fd(guard, pack_env["db_path"])
            child = subprocess.Popen(
                [extractor, "x", "-y", f"-o/proc/self/fd/{descriptor}", str(source)],
                close_fds=True,
                pass_fds=(sidecar_fd, descriptor),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                _pause_after_output_starts(child, output)
                with pytest.raises(subprocess.TimeoutExpired):
                    child.communicate(timeout=0.02)
            finally:
                child.kill()
                child.communicate(timeout=5)
            assert child.returncode == -signal.SIGKILL
            guard.verify()
            assert _borrowed_subprocess_fd(guard, pack_env["db_path"]) == sidecar_fd
            os.fstat(descriptor)
            with pytest.raises(file_mutation_lock.FileMutationBusy):
                with file_mutation_lock.file_mutation_guard(pack_env["db_path"]):
                    pytest.fail("reaping extractor closed its owner's borrowed FD")
        with file_mutation_lock.file_mutation_guard(pack_env["db_path"]) as next_guard:
            next_guard.verify()
    finally:
        if child is not None:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        os.close(descriptor)
