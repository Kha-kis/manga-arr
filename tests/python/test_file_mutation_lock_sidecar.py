"""Private persistent sidecar validation without opening SQLite inodes."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest


def test_guard_never_opens_database_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from file_mutation_lock import file_mutation_guard

    db_path = tmp_path / "local.db"
    db_path.write_bytes(b"unchanged database")
    real_open = os.open

    def open_sidecar_only(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        assert os.fspath(path) != str(db_path), "unmanaged FD opened on SQLite inode"
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_sidecar_only)
    with file_mutation_guard(str(db_path)) as guard:
        guard.verify()
    assert db_path.read_bytes() == b"unchanged database"


@pytest.mark.parametrize(
    "kind", ["symlink", "directory", "fifo", "nonempty", "hardlink", "mode"]
)
def test_guard_refuses_untrusted_sidecar_without_mutation(
    tmp_path: Path, kind: str
) -> None:
    from file_mutation_lock import FileMutationLockError, file_mutation_guard

    db_path = tmp_path / "local.db"
    db_path.write_bytes(b"database")
    lock_path = tmp_path / ".local.db.file-mutation.lock"
    other = tmp_path / "unrelated"
    other.write_bytes(b"untouched")
    if kind == "symlink":
        lock_path.symlink_to(other)
    elif kind == "directory":
        lock_path.mkdir(mode=0o700)
    elif kind == "fifo":
        os.mkfifo(lock_path, mode=0o600)
    else:
        lock_path.write_bytes(b"not a lock" if kind == "nonempty" else b"")
        lock_path.chmod(0o640 if kind == "mode" else 0o600)
        if kind == "hardlink":
            os.link(lock_path, tmp_path / "alias")
    identity = lock_path.lstat()
    with pytest.raises((OSError, FileMutationLockError)):
        with file_mutation_guard(str(db_path)):
            pytest.fail("invalid sidecar accepted")
    assert lock_path.lstat() == identity
    assert other.read_bytes() == b"untouched"
    assert db_path.read_bytes() == b"database"


@pytest.mark.parametrize("changed", ["sidecar", "parent"])
def test_guard_exit_rejects_replaced_lock_or_config_parent(
    tmp_path: Path, changed: str
) -> None:
    from file_mutation_lock import FileMutationLockError, file_mutation_guard

    parent = tmp_path / "config"
    parent.mkdir(mode=0o700)
    db_path = parent / "local.db"
    db_path.write_bytes(b"database")
    lock_path = parent / ".local.db.file-mutation.lock"
    with pytest.raises(FileMutationLockError):
        with file_mutation_guard(str(db_path)):
            if changed == "sidecar":
                lock_path.rename(parent / "original-lock")
                lock_path.touch(mode=0o600)
            else:
                parent.rename(tmp_path / "original-config")
                parent.mkdir(mode=0o700)
                os.link(tmp_path / "original-config" / "local.db", db_path)
                lock_path.touch(mode=0o600)
    assert db_path.read_bytes() == b"database"
    assert lock_path.read_bytes() == b""


def test_guard_refuses_world_writable_config_parent(tmp_path: Path) -> None:
    from file_mutation_lock import FileMutationLockError, file_mutation_guard

    parent = tmp_path / "config"
    parent.mkdir()
    parent.chmod(0o777)
    db_path = parent / "local.db"
    db_path.write_bytes(b"database")
    with pytest.raises(FileMutationLockError):
        with file_mutation_guard(str(db_path)):
            pytest.fail("sidecar created in an untrusted parent")
    assert list(parent.iterdir()) == [db_path]


def test_guard_refuses_sidecar_swapped_between_preflight_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from file_mutation_lock import FileMutationLockError, file_mutation_guard

    db_path = tmp_path / "local.db"
    db_path.write_bytes(b"database")
    lock_path = tmp_path / ".local.db.file-mutation.lock"
    lock_path.touch(mode=0o600)
    original_inode = lock_path.stat().st_ino
    real_open = os.open
    replaced = False

    def replace_before_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal replaced
        if os.fspath(path) == str(lock_path) and not replaced:
            replaced = True
            lock_path.rename(tmp_path / "original-lock")
            lock_path.touch(mode=0o600)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_before_open)
    with pytest.raises(FileMutationLockError):
        with file_mutation_guard(str(db_path)):
            pytest.fail("guard rebound to a replacement after preflight")
    assert (tmp_path / "original-lock").stat().st_ino == original_inode
    assert lock_path.read_bytes() == b""
