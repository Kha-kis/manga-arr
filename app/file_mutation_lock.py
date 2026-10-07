"""Non-expiring filesystem ownership on a persistent local config sidecar.

Never open the SQLite inode: closing ANY unmanaged FD on it can release another
connection's process-owned POSIX record locks. Each caller instead flocks a fresh
open description of one reserved empty sidecar, including nested calls.

The configured local parent and its writers are trusted; root-owned fsGroup
and group-writable layouts are supported, but world-write is refused. Neither
the app nor other trusted writers may unlink or recreate its lock entry or
parent during runtime. Permissions/ownership of user config are never changed.
This is advisory exclusion, not protection from hostile config tampering or
online config/DB restore. Raw fork ownership is unsupported; the sidecar FD is
CLOEXEC by default. SQLite and this coordination directory must remain local.
"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass


class FileMutationLockError(RuntimeError):
    """The guard no longer has authority over its configured database path."""


class FileMutationBusy(FileMutationLockError):
    """Another open description owns the filesystem mutation guard."""


@dataclass(slots=True)
class FileMutationGuard:
    """A live, thread-bound capability; obtain it with file_mutation_guard."""

    _db_path: str
    _descriptor: int
    _dev: int
    _inode: int
    _lock_path: str
    _lock_dev: int
    _lock_inode: int
    _parent_path: str
    _parent_dev: int
    _parent_inode: int
    _pid: int
    _thread_id: int
    _active: bool = False

    def _verify_identity(self) -> None:
        current = os.lstat(self._db_path)
        expected = (self._dev, self._inode)
        if (
            not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != expected
        ):
            raise FileMutationLockError("filesystem guard database identity changed")
        parent = os.lstat(self._parent_path)
        _validate_parent(parent)
        if (parent.st_dev, parent.st_ino) != (self._parent_dev, self._parent_inode):
            raise FileMutationLockError(
                "filesystem guard config parent identity changed"
            )
        held = os.fstat(self._descriptor)
        lock_path_stat = os.lstat(self._lock_path)
        _validate_lock_stat(held, expected)
        _validate_lock_stat(lock_path_stat, expected)
        lock_identity = (self._lock_dev, self._lock_inode)
        if (held.st_dev, held.st_ino) != lock_identity or (
            lock_path_stat.st_dev,
            lock_path_stat.st_ino,
        ) != lock_identity:
            raise FileMutationLockError("filesystem guard sidecar identity changed")

    def verify(self) -> None:
        """Reject closed, transferred, or path-replaced ownership before work."""
        if (
            not self._active
            or os.getpid() != self._pid
            or threading.get_ident() != self._thread_id
        ):
            raise FileMutationLockError("filesystem guard is not active in this worker")
        self._verify_identity()


def _validate_parent(parent: os.stat_result) -> None:
    if not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o002:
        raise FileMutationLockError(
            "filesystem guard config parent is not a non-world-writable directory"
        )


def _validate_lock_stat(lock: os.stat_result, db_identity: tuple[int, int]) -> None:
    if (
        not stat.S_ISREG(lock.st_mode)
        or lock.st_uid != os.geteuid()
        or stat.S_IMODE(lock.st_mode) != 0o600
        or lock.st_size != 0
        or lock.st_nlink != 1
        or (lock.st_dev, lock.st_ino) == db_identity
    ):
        raise FileMutationLockError(
            "filesystem guard sidecar is not an owned empty single-link file"
        )


def _open_sidecar(
    path: str, db_identity: tuple[int, int]
) -> tuple[int, os.stat_result | None]:
    flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        existing = os.lstat(path)
    except FileNotFoundError:
        try:
            return os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600), None
        except FileExistsError:
            existing = os.lstat(path)
    # Reject aliases BEFORE opening: even closing a rejected DB alias would
    # release SQLite's unrelated process-owned record locks.
    _validate_lock_stat(existing, db_identity)
    return os.open(path, flags), existing


@contextmanager
def file_mutation_guard(db_path: str) -> Generator[FileMutationGuard, None, None]:
    """Guard via ``.<DB basename>.file-mutation.lock`` in its trusted parent.

    Take this guard before opening a SQLite writer. Keep it through the
    filesystem action and its journal result, verifying before each mutation.
    DB identity uses lstat only. The sidecar is created once as 0600 and is never
    removed, recreated, truncated, or written by the application. Closing only
    its descriptor releases ownership without disturbing SQLite's own locks.
    """
    path = os.path.abspath(db_path)
    database = os.lstat(path)
    if not stat.S_ISREG(database.st_mode):
        raise FileMutationLockError("filesystem guard database is not a regular file")
    parent_path = os.path.dirname(path)
    parent = os.lstat(parent_path)
    _validate_parent(parent)
    lock_path = os.path.join(
        parent_path, f".{os.path.basename(path)}.file-mutation.lock"
    )
    descriptor, existing = _open_sidecar(lock_path, (database.st_dev, database.st_ino))
    guard: FileMutationGuard | None = None
    try:
        identity = existing if existing is not None else os.fstat(descriptor)
        guard = FileMutationGuard(
            path,
            descriptor,
            database.st_dev,
            database.st_ino,
            lock_path,
            identity.st_dev,
            identity.st_ino,
            parent_path,
            parent.st_dev,
            parent.st_ino,
            os.getpid(),
            threading.get_ident(),
        )
        guard._verify_identity()
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise FileMutationBusy(
                    "filesystem operation already has an owner"
                ) from exc
            raise
        guard._active = True
        guard.verify()
        if existing is None:
            os.fsync(descriptor)
            parent_fd = os.open(
                parent_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
            )
            try:
                pinned_parent = os.fstat(parent_fd)
                if (pinned_parent.st_dev, pinned_parent.st_ino) != (
                    parent.st_dev,
                    parent.st_ino,
                ):
                    raise FileMutationLockError(
                        "filesystem guard config parent identity changed"
                    )
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            guard.verify()
        yield guard
        guard.verify()
    finally:
        if guard is not None:
            guard._active = False
        os.close(descriptor)
