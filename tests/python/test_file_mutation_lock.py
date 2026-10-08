"""Persistent sidecar ownership and native deletion replay exclusion."""

from __future__ import annotations

import errno
import fcntl
import json
import multiprocessing
import os
import shutil
import signal
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from test_volume_file_deletion_journal import deletion_env as deletion_env


def _journal_snapshot(db_path: str) -> tuple[object, ...]:
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT state,claim_path,diagnostic,updated_at,claim_carrier_json FROM volume_file_deletions"
        ).fetchone()
    assert row is not None
    return row


def _assert_completed_once(db_path: str) -> None:
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT state FROM volume_file_deletions").fetchone() == (
            "completed",
        )
        assert db.execute(
            "SELECT COUNT(*) FROM history WHERE event_type='file_deleted'"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='delete'"
        ).fetchone() == (1,)


def _replay_child(
    db_path: str,
    journal_id: int,
    results: Any,
    entered: Any = None,
    release: Any = None,
) -> None:
    import shared
    import volume_file_deletion

    shared.DB_PATH = db_path
    if entered is not None:
        real_unlink = volume_file_deletion.private_claim.discard_private_regular

        def paused_unlink(*args: Any) -> None:
            entered.set()
            if not release.wait(15):
                raise RuntimeError("test replay was not released")
            real_unlink(*args)

        volume_file_deletion.private_claim.discard_private_regular = paused_unlink
    results.put(volume_file_deletion.replay_volume_file_deletion(journal_id))


def _hold_guard_child(db_path: str, entered: Any, release: Any) -> None:
    from file_mutation_lock import file_mutation_guard

    with file_mutation_guard(db_path) as guard:
        guard.verify()
        entered.set()
        if not release.wait(30):
            raise RuntimeError("test lock was not released")


def test_nested_native_replay_cannot_delete_recreated_claim(
    deletion_env: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    import volume_file_deletion

    db_path = str(deletion_env["db_path"])
    reservation = volume_file_deletion.reserve_volume_file_deletion(1, 11)
    assert reservation.journal_id is not None
    journal_id = reservation.journal_id
    real_unlink = volume_file_deletion.private_claim.discard_private_regular
    entered = False
    nested: list[str] = []
    recreated: list[Path] = []

    def overlapping_unlink(*args: Any) -> None:
        nonlocal entered
        path = args[1].artifact_path
        if not entered:
            entered = True
            before = _journal_snapshot(db_path)
            nested.append(volume_file_deletion.replay_volume_file_deletion(journal_id))
            if not Path(path).exists():
                Path(path).write_bytes(b"unrelated recreated claim")
                recreated.append(Path(path))
            if nested[-1] == "blocked":
                assert _journal_snapshot(db_path) == before
        real_unlink(*args)

    monkeypatch.setattr(
        volume_file_deletion.private_claim,
        "discard_private_regular",
        overlapping_unlink,
    )
    outcome = volume_file_deletion.replay_volume_file_deletion(journal_id)

    for path in recreated:
        assert path.exists(), "first replay deleted an unrelated recreated claim"
        assert path.read_bytes() == b"unrelated recreated claim"
    assert nested == ["blocked"]
    assert outcome == "completed"
    _assert_completed_once(db_path)


def test_two_threads_replay_without_rewriting_live_owners_journal(
    deletion_env: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    import volume_file_deletion

    db_path = str(deletion_env["db_path"])
    reservation = volume_file_deletion.reserve_volume_file_deletion(1, 11)
    assert reservation.journal_id is not None
    entered, release = threading.Event(), threading.Event()
    real_unlink = volume_file_deletion.private_claim.discard_private_regular

    def paused_unlink(*args: Any) -> None:
        entered.set()
        if not release.wait(10):
            raise RuntimeError("test replay was not released")
        real_unlink(*args)

    monkeypatch.setattr(
        volume_file_deletion.private_claim, "discard_private_regular", paused_unlink
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(
            volume_file_deletion.replay_volume_file_deletion, reservation.journal_id
        )
        try:
            assert entered.wait(10)
            before = _journal_snapshot(db_path)
            contender = pool.submit(
                volume_file_deletion.replay_volume_file_deletion, reservation.journal_id
            )
            assert contender.result(timeout=5) == "blocked"
            assert _journal_snapshot(db_path) == before
            assert (
                Path(
                    json.loads(str(before[4]))["carrier_path"], "artifact"
                ).read_bytes()
                == b"journal-volume-payload"
            )
        finally:
            release.set()
        assert owner.result(timeout=5) == "completed"
    _assert_completed_once(db_path)


def test_two_processes_replay_excludes_a_live_owner(
    deletion_env: dict[str, object],
) -> None:
    import volume_file_deletion

    db_path = str(deletion_env["db_path"])
    reservation = volume_file_deletion.reserve_volume_file_deletion(1, 11)
    assert reservation.journal_id is not None
    context = multiprocessing.get_context("spawn")
    entered, release = context.Event(), context.Event()
    owner_results, contender_results = context.Queue(), context.Queue()
    owner = context.Process(
        target=_replay_child,
        args=(db_path, reservation.journal_id, owner_results, entered, release),
    )
    contender = context.Process(
        target=_replay_child, args=(db_path, reservation.journal_id, contender_results)
    )
    owner.start()
    try:
        assert entered.wait(15)
        before = _journal_snapshot(db_path)
        contender.start()
        assert contender_results.get(timeout=10) == "blocked"
        assert _journal_snapshot(db_path) == before
        assert (
            Path(json.loads(str(before[4]))["carrier_path"], "artifact").read_bytes()
            == b"journal-volume-payload"
        )
    finally:
        release.set()
        owner.join(10)
        if contender.pid is not None:
            contender.join(10)
        for process in (owner, contender):
            if process.is_alive():
                process.kill()
                process.join(5)
    assert owner.exitcode == contender.exitcode == 0
    assert owner_results.get(timeout=5) == "completed"
    _assert_completed_once(db_path)


def test_guard_lifecycle_and_same_process_nested_contention(tmp_path: Path) -> None:
    from file_mutation_lock import (
        FileMutationBusy,
        FileMutationLockError,
        file_mutation_guard,
    )

    db_path = tmp_path / "local.db"
    db_path.write_bytes(b"unchanged database inode")
    identity = db_path.stat().st_ino
    with file_mutation_guard(str(db_path)) as guard:
        guard.verify()
        with pytest.raises(FileMutationBusy):
            with file_mutation_guard(str(db_path)):
                pytest.fail("a fresh nested open description acquired the lock")
    with pytest.raises(FileMutationLockError):
        guard.verify()
    with file_mutation_guard(str(db_path)) as next_guard:
        next_guard.verify()
    assert db_path.stat().st_ino == identity
    assert db_path.read_bytes() == b"unchanged database inode"
    lock_path = tmp_path / ".local.db.file-mutation.lock"
    assert set(tmp_path.iterdir()) == {db_path, lock_path}
    lock_identity = lock_path.stat().st_ino
    with file_mutation_guard(str(db_path)):
        assert lock_path.stat().st_ino == lock_identity
    assert lock_path.stat().st_ino == lock_identity
    assert lock_path.read_bytes() == b""


def test_stopped_process_keeps_lock_and_sigkill_releases_it(tmp_path: Path) -> None:
    from file_mutation_lock import FileMutationBusy, file_mutation_guard

    db_path = tmp_path / "local.db"
    db_path.write_bytes(b"unchanged")
    context = multiprocessing.get_context("spawn")
    entered, release = context.Event(), context.Event()
    process = context.Process(
        target=_hold_guard_child, args=(str(db_path), entered, release)
    )
    process.start()
    try:
        assert entered.wait(15)
        assert process.pid is not None
        os.kill(process.pid, signal.SIGSTOP)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pid, status = os.waitpid(process.pid, os.WNOHANG | os.WUNTRACED)
            if pid:
                assert os.WIFSTOPPED(status)
                break
            time.sleep(0.01)
        else:
            pytest.fail("owner did not stop")
        with pytest.raises(FileMutationBusy):
            with file_mutation_guard(str(db_path)):
                pytest.fail("a stopped live owner lost exclusion")
    finally:
        if process.is_alive():
            process.kill()
        process.join(10)
    assert process.exitcode == -signal.SIGKILL
    with file_mutation_guard(str(db_path)) as guard:
        guard.verify()
    assert db_path.read_bytes() == b"unchanged"


def test_sqlite_other_writer_commits_while_guard_is_held(tmp_path: Path) -> None:
    from file_mutation_lock import file_mutation_guard

    db_path = tmp_path / "local.db"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE proof(value TEXT)")

    def write() -> None:
        with sqlite3.connect(db_path, timeout=1) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO proof VALUES('independent writer')")

    with file_mutation_guard(str(db_path)) as guard:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(write).result(timeout=5)
        guard.verify()
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT value FROM proof").fetchall() == [
            ("independent writer",)
        ]


def test_guard_refuses_database_swapped_before_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from file_mutation_lock import FileMutationLockError, file_mutation_guard

    db_path, replacement = tmp_path / "local.db", tmp_path / "replacement.db"
    db_path.write_bytes(b"old")
    replacement.write_bytes(b"replacement")
    real_flock = fcntl.flock
    descriptors: list[int] = []

    def replace_before_lock(descriptor: int, operation: int) -> None:
        assert operation == fcntl.LOCK_EX | fcntl.LOCK_NB
        descriptors.append(descriptor)
        assert not os.get_inheritable(descriptor)
        os.replace(replacement, db_path)
        real_flock(descriptor, operation)

    monkeypatch.setattr(fcntl, "flock", replace_before_lock)
    with pytest.raises(FileMutationLockError):
        with file_mutation_guard(str(db_path)):
            pytest.fail("guard accepted a swapped database path")
    assert db_path.read_bytes() == b"replacement"
    with pytest.raises(OSError) as closed:
        os.fstat(descriptors[0])
    assert closed.value.errno == errno.EBADF


def test_guard_verify_refuses_a_swapped_path(tmp_path: Path) -> None:
    from file_mutation_lock import FileMutationLockError, file_mutation_guard

    db_path, replacement = tmp_path / "local.db", tmp_path / "replacement.db"
    db_path.write_bytes(b"old")
    replacement.write_bytes(b"replacement")
    with pytest.raises(FileMutationLockError):
        with file_mutation_guard(str(db_path)) as guard:
            os.replace(replacement, db_path)
            with pytest.raises(FileMutationLockError):
                guard.verify()
    assert db_path.read_bytes() == b"replacement"


def test_guard_exit_refuses_a_swapped_path(tmp_path: Path) -> None:
    from file_mutation_lock import FileMutationLockError, file_mutation_guard

    db_path, replacement = tmp_path / "local.db", tmp_path / "replacement.db"
    db_path.write_bytes(b"old")
    replacement.write_bytes(b"replacement")
    with pytest.raises(FileMutationLockError):
        with file_mutation_guard(str(db_path)):
            os.replace(replacement, db_path)
    assert db_path.read_bytes() == b"replacement"


def test_replay_refuses_swap_before_filesystem_action(
    deletion_env: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    import volume_file_deletion

    db_path = str(deletion_env["db_path"])
    reservation = volume_file_deletion.reserve_volume_file_deletion(1, 11)
    assert reservation.journal_id is not None
    before = _journal_snapshot(db_path)
    replacement = Path(db_path).with_suffix(".replacement")
    shutil.copy2(db_path, replacement)
    real_fingerprint = volume_file_deletion._regular_fingerprint

    def swap_after_fingerprint(path: str) -> volume_file_deletion.FileFingerprint:
        result = real_fingerprint(path)
        os.replace(replacement, db_path)
        return result

    monkeypatch.setattr(
        volume_file_deletion, "_regular_fingerprint", swap_after_fingerprint
    )
    assert (
        volume_file_deletion.replay_volume_file_deletion(reservation.journal_id)
        == "blocked"
    )
    assert (
        Path(str(deletion_env["file_path"])).read_bytes() == b"journal-volume-payload"
    )
    assert not Path(str(before[1])).exists()
    assert _journal_snapshot(db_path) == before


def test_guard_spans_final_database_audit(
    deletion_env: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    from file_mutation_lock import FileMutationBusy, file_mutation_guard
    import volume_file_deletion

    db_path = str(deletion_env["db_path"])
    reservation = volume_file_deletion.reserve_volume_file_deletion(1, 11)
    assert reservation.journal_id is not None
    real_complete = volume_file_deletion._complete_journal

    def complete(journal: Any, *, deleted: bool) -> bool:
        with pytest.raises(FileMutationBusy):
            with file_mutation_guard(db_path):
                pytest.fail("guard was released before the final audit")
        return real_complete(journal, deleted=deleted)

    monkeypatch.setattr(volume_file_deletion, "_complete_journal", complete)
    assert (
        volume_file_deletion.replay_volume_file_deletion(reservation.journal_id)
        == "completed"
    )
    _assert_completed_once(db_path)


@pytest.mark.parametrize("boundary", ["load", "claim", "unlink", "error"])
def test_replay_db_swap_retains_pending_journal_without_diagnostics(
    deletion_env: dict[str, object], monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    import volume_file_deletion

    db_path = str(deletion_env["db_path"])
    reservation = volume_file_deletion.reserve_volume_file_deletion(1, 11)
    assert reservation.journal_id is not None
    before = _journal_snapshot(db_path)
    original_db = Path(db_path).with_suffix(".original")
    replacement = Path(db_path).with_suffix(".replacement")
    shutil.copy2(db_path, replacement)

    def swap() -> None:
        nonlocal expected_original
        expected_original = _journal_snapshot(db_path)
        os.rename(db_path, original_db)
        os.replace(replacement, db_path)

    expected_original = before

    if boundary == "load":
        real_load = volume_file_deletion._load_journal

        def load(journal_id: int) -> Any:
            journal = real_load(journal_id)
            swap()
            return journal

        monkeypatch.setattr(volume_file_deletion, "_load_journal", load)
    elif boundary == "claim":
        real_rename = volume_file_deletion._rename_noreplace

        def claim(source: str, destination: str) -> None:
            real_rename(source, destination)
            swap()

        monkeypatch.setattr(volume_file_deletion, "_rename_noreplace", claim)
    elif boundary == "unlink":
        real_unlink = volume_file_deletion.private_claim.discard_private_regular

        def unlink(*args: Any) -> None:
            real_unlink(*args)
            swap()

        monkeypatch.setattr(
            volume_file_deletion.private_claim, "discard_private_regular", unlink
        )
    else:

        def fail(_path: str) -> Any:
            swap()
            raise OSError(errno.EIO, "injected fingerprint failure")

        monkeypatch.setattr(volume_file_deletion, "_regular_fingerprint", fail)

    assert (
        volume_file_deletion.replay_volume_file_deletion(reservation.journal_id)
        == "blocked"
    )
    assert _journal_snapshot(db_path) == before
    assert _journal_snapshot(str(original_db)) == expected_original
    for path in (db_path, str(original_db)):
        with sqlite3.connect(path) as db:
            assert db.execute(
                "SELECT COUNT(*) FROM history WHERE event_type='file_deleted'"
            ).fetchone() == (0,)
            assert db.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='delete'"
            ).fetchone() == (0,)
    target, claim_path = Path(str(deletion_env["file_path"])), Path(str(before[1]))
    assert target.exists() == (boundary in {"load", "error"})
    assert claim_path.exists() == (boundary == "claim")
    if boundary == "claim":
        assert claim_path.read_bytes() == b"journal-volume-payload"


def test_replay_refuses_db_swap_during_final_audit(
    deletion_env: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    import volume_file_deletion

    db_path = str(deletion_env["db_path"])
    reservation = volume_file_deletion.reserve_volume_file_deletion(1, 11)
    assert reservation.journal_id is not None
    before = _journal_snapshot(db_path)
    replacement = Path(db_path).with_suffix(".replacement")
    shutil.copy2(db_path, replacement)

    def swapped_audit(_journal: Any, *, deleted: bool) -> bool:
        assert deleted
        os.replace(replacement, db_path)
        return False

    monkeypatch.setattr(volume_file_deletion, "_complete_journal", swapped_audit)
    assert (
        volume_file_deletion.replay_volume_file_deletion(reservation.journal_id)
        == "blocked"
    )
    assert _journal_snapshot(db_path) == before


def test_guard_refuses_thread_transfer_and_releases_after_exception(
    tmp_path: Path,
) -> None:
    from file_mutation_lock import FileMutationLockError, file_mutation_guard

    db_path = tmp_path / "local.db"
    db_path.write_bytes(b"unchanged")
    with pytest.raises(RuntimeError, match="owner failed"):
        with file_mutation_guard(str(db_path)) as guard:
            with ThreadPoolExecutor(max_workers=1) as pool:
                with pytest.raises(FileMutationLockError):
                    pool.submit(guard.verify).result(timeout=5)
            guard.verify()
            raise RuntimeError("owner failed")
    with file_mutation_guard(str(db_path)) as next_guard:
        next_guard.verify()


@pytest.mark.parametrize("kind", ["missing", "symlink", "directory", "fifo"])
def test_guard_refuses_unsafe_or_missing_db_without_creation(
    tmp_path: Path, kind: str
) -> None:
    from file_mutation_lock import FileMutationLockError, file_mutation_guard

    db_path = tmp_path / "local.db"
    target = tmp_path / "target"
    target.write_bytes(b"untouched")
    if kind == "symlink":
        db_path.symlink_to(target)
    elif kind == "directory":
        db_path.mkdir()
    elif kind == "fifo":
        os.mkfifo(db_path)
    with pytest.raises((OSError, FileMutationLockError)):
        with file_mutation_guard(str(db_path)):
            pytest.fail("unsafe DB path was accepted")
    assert target.read_bytes() == b"untouched"
    if kind == "missing":
        assert not db_path.exists()


def test_replay_permission_error_leaves_owner_journal_untouched(
    deletion_env: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    import volume_file_deletion

    db_path = str(deletion_env["db_path"])
    reservation = volume_file_deletion.reserve_volume_file_deletion(1, 11)
    assert reservation.journal_id is not None
    before = _journal_snapshot(db_path)
    real_open = os.open

    def denied(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        lock_path = str(Path(db_path).with_name(".deletion.db.file-mutation.lock"))
        if os.fspath(path) == lock_path:
            raise PermissionError(
                errno.EACCES, "injected lock permission failure", lock_path
            )
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", denied)
    assert (
        volume_file_deletion.replay_volume_file_deletion(reservation.journal_id)
        == "blocked"
    )
    assert _journal_snapshot(db_path) == before
    assert (
        Path(str(deletion_env["file_path"])).read_bytes() == b"journal-volume-payload"
    )


def test_replay_malformed_database_does_not_create_or_mutate_files(
    deletion_env: dict[str, object],
) -> None:
    import volume_file_deletion

    db_path = Path(str(deletion_env["db_path"]))
    db_path.write_bytes(b"not a sqlite database")
    assert volume_file_deletion.replay_volume_file_deletion(1) == "blocked"
    assert db_path.read_bytes() == b"not a sqlite database"
    assert (
        Path(str(deletion_env["file_path"])).read_bytes() == b"journal-volume-payload"
    )
