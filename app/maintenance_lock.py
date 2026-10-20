"""Persistent local exclusion between a live service and offline recovery.

Take maintenance ownership before file_mutation_guard and any SQLite writer.
All service processes hold shared ownership; offline mutators take exclusive,
nonblocking ownership. Older binaries must be stopped before upgrading: this
advisory protocol cannot exclude writers that do not participate.

The local config parent and its writers are trusted, including fsGroup layouts.
Never unlink/replace the sidecar or parent during runtime. This is not online
restore authority or protection against hostile trusted-directory tampering.
Never open SQLite here: closing an unmanaged DB descriptor can release another
connection's process-owned record locks. Raw fork inheritance is unsupported.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
import errno
import fcntl
import os
import stat


class MaintenanceLockError(RuntimeError):
    """Maintenance ownership could not be established for the configured DB."""


class MaintenanceBusy(MaintenanceLockError):
    """A cooperating service or recovery process holds incompatible ownership."""


def _validate_parent(parent: os.stat_result) -> None:
    if not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o002:
        raise MaintenanceLockError(
            "maintenance config parent is not a non-world-writable directory"
        )


def _database_identity(path: str) -> tuple[int, int] | None:
    try:
        database = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(database.st_mode):
        raise MaintenanceLockError("maintenance database is not a regular file")
    return database.st_dev, database.st_ino


def _validate_lock(lock: os.stat_result, db_identity: tuple[int, int] | None) -> None:
    if (
        not stat.S_ISREG(lock.st_mode)
        or lock.st_uid != os.geteuid()
        or stat.S_IMODE(lock.st_mode) != 0o600
        or lock.st_size != 0
        or lock.st_nlink != 1
        or (lock.st_dev, lock.st_ino) == db_identity
    ):
        raise MaintenanceLockError(
            "maintenance sidecar is not an owned empty single-link file"
        )


def _open_sidecar(
    path: str, db_identity: tuple[int, int] | None
) -> tuple[int, os.stat_result | None]:
    flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        existing = os.lstat(path)
    except FileNotFoundError:
        try:
            return os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600), None
        except FileExistsError:
            existing = os.lstat(path)
    # Refuse DB aliases before opening, even if the DB happens to be empty.
    _validate_lock(existing, db_identity)
    return os.open(path, flags), existing


@contextmanager
def service_maintenance_guard(
    db_path: str, *, exclusive: bool
) -> Generator[None, None, None]:
    """Hold ``.<DB basename>.ownership-maintenance.lock`` until context exit.

    Both modes fail immediately when busy, including service startup during
    offline recovery. A missing DB is valid for fresh installs; this guard only
    creates the persistent 0600 sidecar, never the DB. Release only by closing
    this CLOEXEC descriptor, without explicitly unlocking or unlinking anything.
    """
    path = os.path.abspath(db_path)
    database_identity = _database_identity(path)
    parent_path = os.path.dirname(path)
    parent = os.lstat(parent_path)
    _validate_parent(parent)
    lock_path = os.path.join(
        parent_path, f".{os.path.basename(path)}.ownership-maintenance.lock"
    )
    descriptor, existing = _open_sidecar(lock_path, database_identity)
    try:
        identity = existing if existing is not None else os.fstat(descriptor)
        lock_identity = identity.st_dev, identity.st_ino

        def verify() -> None:
            current_database = _database_identity(path)
            if database_identity is not None and current_database != database_identity:
                raise MaintenanceLockError("maintenance database identity changed")
            current_parent = os.lstat(parent_path)
            _validate_parent(current_parent)
            if (current_parent.st_dev, current_parent.st_ino) != (
                parent.st_dev,
                parent.st_ino,
            ):
                raise MaintenanceLockError("maintenance config parent identity changed")
            held = os.fstat(descriptor)
            named = os.lstat(lock_path)
            for candidate in (held, named):
                _validate_lock(candidate, current_database)
                if (candidate.st_dev, candidate.st_ino) != lock_identity:
                    raise MaintenanceLockError("maintenance sidecar identity changed")

        verify()
        try:
            fcntl.flock(
                descriptor,
                (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB,
            )
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise MaintenanceBusy("maintenance ownership is busy") from exc
            raise
        verify()
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
                    raise MaintenanceLockError(
                        "maintenance config parent identity changed"
                    )
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            verify()
        yield
        verify()
    finally:
        os.close(descriptor)
