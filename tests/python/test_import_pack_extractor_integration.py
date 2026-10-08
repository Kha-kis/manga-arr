"""Real 7z through the application's queue generation adapter."""

from __future__ import annotations

import json
import os
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from file_mutation_lock import FileMutationBusy, file_mutation_guard
from test_import_pack_cleanup_durability import _PackEnv, _probe_writer, pack_env  # noqa: F401
from test_import_pack_7z_output_pinning import (
    _write_nested_archive,
    _pause_after_output_starts,
    _FIRST,
    _LATER,
)
from test_import_pack_nfs_lifecycle import _expire


def _wrapped_scene(env: _PackEnv, *, large: bool) -> Path:
    source = env["tmp_path"] / "wrapped-scene"
    source.mkdir()
    rar = env["tmp_path"] / "fixture.rar"
    if large:
        _write_nested_archive(rar)
    else:
        cbz = env["tmp_path"] / "page.cbz"
        with zipfile.ZipFile(cbz, "w") as inner:
            inner.writestr("001.jpg", b"page")
        with zipfile.ZipFile(rar, "w") as archive:
            archive.write(cbz, "nested/Pack Series c001.cbz")
    with zipfile.ZipFile(source / "one.zip", "w") as archive:
        archive.write(rar, "scene.rar")
    with zipfile.ZipFile(source / "two.zip", "w") as archive:
        archive.writestr("scene.r00", b"unused part for placement fixture")
    return source


def _queue(env: _PackEnv, source: Path):
    import main
    import import_queue

    with main.get_db() as db:
        return import_queue._queue_import(
            db, 1, "real-scene", "Pack Series", "magnet:real-scene", None, str(source)
        )


def test_real_queue_7z_inherits_only_verified_sidecar_and_pinned_outputs(
    pack_env: _PackEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    import import_queue

    assert shutil.which("7z") is not None
    source = _wrapped_scene(pack_env, large=False)
    original = subprocess.Popen
    descriptors: list[tuple[int, ...]] = []

    def launch(command, **kwargs):
        passed = kwargs["pass_fds"]
        assert len(passed) == 3
        assert not any(os.get_inheritable(fd) for fd in passed)
        assert os.readlink(f"/proc/self/fd/{passed[0]}").endswith(".file-mutation.lock")
        assert all(
            os.fstat(fd).st_ino != os.stat(pack_env["db_path"]).st_ino for fd in passed
        )
        assert command[3] == f"-o/proc/self/fd/{passed[2]}"
        assert kwargs["stdout"] == kwargs["stderr"] == subprocess.PIPE
        with pytest.raises(FileMutationBusy):
            with file_mutation_guard(pack_env["db_path"]):
                pass
        _probe_writer(pack_env["db_path"], "writer-during-real-extract")
        descriptors.append(passed)
        return original(command, **kwargs)

    monkeypatch.setattr(import_queue.subprocess, "Popen", launch)
    queue_id, _ = _queue(pack_env, source)
    assert queue_id is not None and len(descriptors) == 1
    with sqlite3.connect(pack_env["db_path"]) as db:
        path = db.execute(
            "SELECT src_path FROM import_queue_files WHERE queue_id=?", (queue_id,)
        ).fetchone()[0]
        assert "/proc/" not in path and Path(path).is_file()
        assert (
            json.loads(
                db.execute(
                    "SELECT directory_ownership_json FROM import_pack_cleanup_reservations"
                ).fetchone()[0]
            )["phase"]
            == "attached"
        )


def test_real_queue_extractor_exception_kills_and_waits_before_guard_exit(
    pack_env: _PackEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    import import_queue

    source = _wrapped_scene(pack_env, large=True)
    original = subprocess.Popen
    children: list[subprocess.Popen] = []

    def launch(command, **kwargs):
        child = original(command, **kwargs)
        children.append(child)
        output = Path(os.readlink(f"/proc/self/fd/{kwargs['pass_fds'][-1]}"))
        _pause_after_output_starts(child, output)
        real_communicate = child.communicate
        first = True

        def communicate(*args, **kwargs2):
            nonlocal first
            if first:
                first = False
                raise KeyboardInterrupt("injected extractor parent error")
            result = real_communicate(*args, **kwargs2)
            assert child.returncode is not None
            with pytest.raises(FileMutationBusy):
                with file_mutation_guard(pack_env["db_path"]):
                    pass
            return result

        child.communicate = communicate
        return child

    monkeypatch.setattr(import_queue.subprocess, "Popen", launch)
    try:
        with pytest.raises(KeyboardInterrupt, match="parent error"):
            _queue(pack_env, source)
        assert children[0].returncode == -signal.SIGKILL
        with file_mutation_guard(pack_env["db_path"]):
            pass
        with sqlite3.connect(pack_env["db_path"]) as db:
            assert db.execute("SELECT COUNT(*) FROM import_queue").fetchone() == (0,)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)


_DRIVER = r"""
import json, os, signal, subprocess, sys
from pathlib import Path
import conftest
import main, shared, import_pipeline, import_queue
from test_import_pack_7z_output_pinning import _pause_after_output_starts
main.DB_PATH = shared.DB_PATH = sys.argv[1]
import_pipeline.PACK_STAGING_ROOT = sys.argv[2]
main.load_config()
real = subprocess.Popen
def launch(command, **kwargs):
    child = real(command, **kwargs)
    output = Path(os.readlink('/proc/self/fd/' + str(kwargs['pass_fds'][-1])))
    private = os.readlink('/proc/self/fd/' + str(kwargs['pass_fds'][1]))
    try:
        _pause_after_output_starts(child, output)
    except BaseException:
        child.kill()
        child.communicate(timeout=10)
        raise
    print(json.dumps({'child':child.pid,'output':str(output),'private':private}), flush=True)
    signal.pause()
    return child
subprocess.Popen = launch
with main.get_db() as db:
    import_queue._queue_import(db,1,'real-scene','Pack Series','magnet:real-scene',None,sys.argv[3])
"""


@pytest.mark.parametrize(
    "replace_path", [False, True], ids=["replay", "replacement-retained"]
)
def test_actual_queue_parent_sigkill_keeps_exclusion_until_7z_settles(
    pack_env: _PackEnv, replace_path: bool
) -> None:
    import import_pack_cleanup

    source = _wrapped_scene(pack_env, large=True)
    env = dict(os.environ)
    root = Path(__file__).resolve().parents[2]
    env["PYTHONPATH"] = os.pathsep.join(
        (str(root / "app"), str(root / "tests/python"), env.get("PYTHONPATH", ""))
    )
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _DRIVER,
            pack_env["db_path"],
            str(pack_env["pack_root"]),
            str(source),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    child_fd = None
    try:
        assert parent.stdout is not None
        assert select.select([parent.stdout], [], [], 15)[0], (
            "queue adapter did not launch/pause real 7z"
        )
        line = parent.stdout.readline()
        if not line:
            _, error = parent.communicate(timeout=10)
            pytest.fail(f"queue adapter exited before its pause checkpoint: {error}")
        record = json.loads(line)
        child_fd = os.pidfd_open(record["child"])
        private = Path(record["private"])
        output = Path(record["output"])
        if replace_path:
            displaced = pack_env["tmp_path"] / "displaced-queue-output"
            relative = output.relative_to(private)
            private.rename(displaced)
            private.mkdir(mode=0o700)
            (private / "unrelated.cbz").write_bytes(b"do not touch")
            output = displaced / relative
        parent.kill()
        parent.wait(timeout=10)
        _expire(pack_env)
        with pytest.raises(FileMutationBusy):
            with file_mutation_guard(pack_env["db_path"]):
                pass
        signal.pidfd_send_signal(child_fd, signal.SIGCONT)
        assert select.select([child_fd], [], [], 15)[0], (
            "orphan extractor did not settle"
        )
        assert (output / _FIRST).is_file() and (output / _LATER).is_file()
        with file_mutation_guard(pack_env["db_path"]):
            pass
        with sqlite3.connect(pack_env["db_path"]) as db:
            assert db.execute("SELECT COUNT(*) FROM import_queue").fetchone() == (0,)
        recovery = import_pack_cleanup.recover_pack_cleanup_state()
        if replace_path:
            assert recovery.reservations_recovered == 0
            assert (private / "unrelated.cbz").read_bytes() == b"do not touch"
            assert (output / _LATER).is_file()
        else:
            assert recovery.reservations_recovered == 1
            assert not private.exists()
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=10)
        if child_fd is not None:
            if not select.select([child_fd], [], [], 0)[0]:
                signal.pidfd_send_signal(child_fd, signal.SIGKILL)
                assert select.select([child_fd], [], [], 10)[0]
            os.close(child_fd)
