"""Non-expiring filesystem ownership on an existing local SQLite inode.

Every caller uses a fresh open description, including nested calls. This is
advisory exclusion for participating actors, not support for online DB restore
or a SQLite database on a network filesystem.
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
    _pid: int
    _thread_id: int
    _active: bool = False

    def _verify_identity(self) -> None:
        held = os.fstat(self._descriptor)
        current = os.lstat(self._db_path)
        expected = (self._dev, self._inode)
        if (
            not stat.S_ISREG(held.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or (held.st_dev, held.st_ino) != expected
            or (current.st_dev, current.st_ino) != expected
        ):
            raise FileMutationLockError("filesystem guard database identity changed")

    def verify(self) -> None:
        """Reject closed, transferred, or path-replaced ownership before work."""
        if (
            not self._active
            or os.getpid() != self._pid
            or threading.get_ident() != self._thread_id
        ):
            raise FileMutationLockError("filesystem guard is not active in this worker")
        self._verify_identity()


@contextmanager
def file_mutation_guard(db_path: str) -> Generator[FileMutationGuard, None, None]:
    """Exclusively guard an existing local DB inode, without creating any path.

    Take this guard before opening a SQLite writer. Keep it through the
    filesystem action and its journal result, verifying before each mutation.
    Closing this descriptor releases ownership; no inode is unlinked or reused.
    """
    path = os.path.abspath(db_path)
    descriptor = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
    guard: FileMutationGuard | None = None
    try:
        identity = os.fstat(descriptor)
        guard = FileMutationGuard(
            path,
            descriptor,
            identity.st_dev,
            identity.st_ino,
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
        yield guard
        guard.verify()
    finally:
        if guard is not None:
            guard._active = False
        os.close(descriptor)
