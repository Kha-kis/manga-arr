"""Provenance for managed library chapter copies made from Suwayomi downloads.

Records which identified Suwayomi chapter a library chapter file was copied
from, plus a full-content fingerprint of the copy, when (and only when) this
import created that copy itself and verified it. The record is evidence for a
later, separately reviewed opt-in cleanup (issue #422); nothing here deletes
or moves files beyond rolling back a copy this import just created.

Never recorded for: a destination that already existed (reused), the original
Suwayomi file returned when no managed library directory is configured,
chapters resolved by legacy filename matching, or historical files.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import stat
import tempfile
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_HASH_CHUNK_SIZE = 1024 * 1024
# Fingerprints stream in fixed chunks; files above this bound are still
# imported but never get ownership evidence.
FINGERPRINT_MAX_BYTES = 4 * 1024 * 1024 * 1024
log = logging.getLogger(__name__)


class ProvenanceError(RuntimeError):
    """A copy or fingerprint could not be proven; ownership is not recorded."""


@dataclass(frozen=True)
class FileEvidence:
    dev: int
    inode: int
    size: int
    mtime_ns: int
    sha256: str


@dataclass
class _OwnedCopy:
    """Live allocation authority, never reconstructed from file evidence."""

    _descriptor: int | None = None

    def matches(self, expected: tuple[int, ...]) -> bool:
        if self._descriptor is None:
            return False
        try:
            held = os.fstat(self._descriptor)
        except OSError:
            return False
        return stat.S_ISREG(held.st_mode) and _identity(held)[:2] == expected[:2]

    def close(self) -> None:
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is not None:
            os.close(descriptor)


@dataclass(frozen=True)
class SourceIdentity:
    """The resolved Suwayomi context of one downloaded chapter."""

    client_id: int
    server: str
    manga_id: int
    source_id: str
    chapter_id: int


@dataclass(frozen=True)
class ChapterImport:
    """Outcome of one chapter import.

    ``created`` identifies the file at ``path`` when this import created it
    (used to roll back only that file). ``verified`` is true only when its
    content was checked against the bytes read from an unchanged source.
    ``identified`` is true only when the source file was selected through the
    queued chapter's own identity, not legacy filename matching.
    """

    path: str | None
    size: int
    created: FileEvidence | None = None
    verified: bool = False
    identified: bool = False
    _owner: _OwnedCopy | None = field(default=None, compare=False, repr=False)

    def close(self) -> None:
        if self._owner is not None:
            self._owner.close()


def _identity(st: os.stat_result) -> tuple[int, int, int, int]:
    return (int(st.st_dev), int(st.st_ino), int(st.st_size), int(st.st_mtime_ns))


def fingerprint(path: str) -> FileEvidence:
    """Hash one non-symlink regular file, refusing if it changes meanwhile."""
    descriptor = os.open(
        path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ProvenanceError(f"not a regular file: {path}")
        if before.st_size > FINGERPRINT_MAX_BYTES:
            raise ProvenanceError(f"file exceeds fingerprint bound: {path}")
        digest = hashlib.sha256()
        consumed = 0
        # Read at most one byte past the bound, so a growing file is refused
        # without streaming whatever it grows to.
        while chunk := os.read(
            descriptor, min(_HASH_CHUNK_SIZE, FINGERPRINT_MAX_BYTES - consumed + 1)
        ):
            consumed += len(chunk)
            if consumed > FINGERPRINT_MAX_BYTES:
                raise ProvenanceError(f"file grew past fingerprint bound: {path}")
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    current = os.lstat(path)
    if (
        stat.S_ISLNK(current.st_mode)
        or _identity(before) != _identity(after)
        or _identity(after) != _identity(current)
    ):
        raise ProvenanceError(f"file changed while hashing: {path}")
    dev, inode, size, mtime_ns = _identity(after)
    return FileEvidence(dev, inode, size, mtime_ns, digest.hexdigest())


def same_file(path: str, evidence: FileEvidence) -> bool:
    """Cheap identity check (no content read) against recorded evidence."""
    try:
        current = os.lstat(path)
    except OSError:
        return False
    return not stat.S_ISLNK(current.st_mode) and _identity(current) == (
        evidence.dev,
        evidence.inode,
        evidence.size,
        evidence.mtime_ns,
    )


def remove_if_unchanged(
    path: str, evidence: FileEvidence, *, _owner: _OwnedCopy | None = None,
) -> bool:
    """Roll back a copy this import created, only if it is still that file."""
    return _remove_captured(
        path, (evidence.dev, evidence.inode, evidence.size, evidence.mtime_ns), _owner
    )


def copy_new_file(
    src: str, dst: str, *, _owner: _OwnedCopy | None = None,
) -> tuple[FileEvidence, bool]:
    """Create ``dst`` exclusively from ``src`` and verify the copy.

    Returns ``(evidence, verified)``. ``evidence`` always identifies the file
    this call created, so a caller can roll it back. ``verified`` is true only
    when the copy's SHA-256 and size match the bytes read from a source that
    did not change during the read; a source above the fingerprint bound is
    copied unverified. All writes and metadata go through the exclusively
    created descriptor, so a file swapped in at ``dst`` is never written to or
    adopted. Raises ``FileExistsError`` when ``dst`` already exists, so a
    pre-existing file is never treated as created here. Other failures roll
    back only the captured created file or retain a pending receipt that
    blocks reuse until explicit review.
    """
    if _owner is not None and _owner._descriptor is not None:
        raise ProvenanceError("copy owner already holds an inode")
    try:
        with _pending_copy(dst) as release:
            return _copy_new_file(src, dst, release, _owner)
    except BaseException:
        if _owner is not None:
            _owner.close()
        raise


def _pending_path(path: str) -> str:
    path = os.path.abspath(path)
    key = hashlib.sha256(os.fsencode(os.path.basename(path))).hexdigest()
    return os.path.join(os.path.dirname(path), f".mangarr-claim-chapter-pending-{key}")


def assert_not_pending(path: str) -> None:
    """Unknown/pending creation intent is never authority to reuse a file."""
    pending = _pending_path(path)
    if os.path.lexists(pending):
        raise ProvenanceError(f"chapter copy pending explicit review: {pending}")


@contextmanager
def _pending_copy(path: str) -> Generator[Callable[[], None], None, None]:
    """Persist intent before public creation; never adopt an existing intent."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    pending = _pending_path(path)
    parent_fd = os.open(os.path.dirname(pending), flags)
    pending_fd: int | None = None
    receipt_created = False
    retire = True

    def release() -> None:
        nonlocal retire
        retire = True

    try:
        try:
            os.mkdir(os.path.basename(pending), 0o700, dir_fd=parent_fd)
        except FileExistsError as exc:
            raise ProvenanceError(f"chapter copy pending explicit review: {pending}") from exc
        created = os.stat(os.path.basename(pending), dir_fd=parent_fd, follow_symlinks=False)
        pending_fd = os.open(os.path.basename(pending), flags, dir_fd=parent_fd)
        held = os.fstat(pending_fd)
        if (
            (held.st_dev, held.st_ino) != (created.st_dev, created.st_ino)
            or held.st_uid != os.geteuid()
            or stat.S_IMODE(held.st_mode) not in (0o700, 0o2700)
        ):
            raise ProvenanceError("chapter pending private directory identity/permissions changed")
        os.fchmod(pending_fd, 0o700)
        marker_fd = os.open(
            "receipt.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=pending_fd,
        )
        receipt_created = True
        with os.fdopen(marker_fd, "w", encoding="ascii") as marker:
            json.dump({"version": 1, "origin": os.path.abspath(path)}, marker)
            marker.flush()
            os.fsync(marker.fileno())
        os.fsync(pending_fd)
        os.fsync(parent_fd)
        retire = False
        yield release
    finally:
        try:
            if pending_fd is not None:
                try:
                    if retire and receipt_created:
                        try:
                            os.unlink("receipt.json", dir_fd=pending_fd)
                        except FileNotFoundError:
                            pass
                    elif not retire:
                        log.error("Chapter copy pending explicit review at %s", pending)
                finally:
                    os.close(pending_fd)
                if retire and receipt_created:
                    os.rmdir(os.path.basename(pending), dir_fd=parent_fd)
        finally:
            os.close(parent_fd)


def _copy_new_file(
    src: str, dst: str, release: Callable[[], None], owner: _OwnedCopy | None,
) -> tuple[FileEvidence, bool]:
    owned = owner if owner is not None else _OwnedCopy()
    source_fd: int | None = None
    created: os.stat_result | None = None
    output_created = False
    succeeded = False
    try:
        source_fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(source_fd, "rb") as source:
            source_fd = None
            source_before = os.fstat(source.fileno())
            if not stat.S_ISREG(source_before.st_mode):
                raise ProvenanceError(f"source is not a regular file: {src}")
            verify = source_before.st_size <= FINGERPRINT_MAX_BYTES
            fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o644)
            owned._descriptor = fd
            output_created = True
            created = os.fstat(fd)
            while chunk := source.read(_HASH_CHUNK_SIZE):
                size += len(chunk)
                if size > source_before.st_size:
                    raise ProvenanceError(f"source grew while copying: {src}")
                if verify:
                    digest.update(chunk)
                _write_all(fd, chunk)
            source_after = os.fstat(source.fileno())
        if _identity(source_before) != _identity(source_after) or size != source_after.st_size:
            raise ProvenanceError(f"source changed while copying: {src}")
        os.fchmod(fd, stat.S_IMODE(source_after.st_mode))
        os.utime(fd, ns=(source_after.st_atime_ns, source_after.st_mtime_ns))
        os.fsync(fd)
        written = os.fstat(fd)
        current = os.lstat(dst)
        if stat.S_ISLNK(current.st_mode) or _identity(current) != _identity(written):
            raise ProvenanceError(f"copy was replaced while copying: {dst}")
        if verify:
            evidence = fingerprint(dst)
            if (evidence.dev, evidence.inode) != _identity(created)[:2]:
                raise ProvenanceError(f"copy was replaced while verifying: {dst}")
            if evidence.size != size or evidence.sha256 != digest.hexdigest():
                raise ProvenanceError(f"copy does not match source bytes: {dst}")
        else:
            evidence = FileEvidence(*_identity(written), "")
        release()
        succeeded = True
        return evidence, verify
    except BaseException:
        if not output_created or (created is not None and _unlink_created(dst, created, owned)):
            release()
        raise
    finally:
        try:
            if source_fd is not None:
                os.close(source_fd)
        finally:
            if not succeeded or owner is None:
                owned.close()


def _write_all(fd: int, chunk: bytes) -> None:
    view = memoryview(chunk)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _unlink_created(path: str, created: os.stat_result, owner: _OwnedCopy) -> bool:
    return _remove_captured(path, _identity(created)[:2], owner)


def _remove_captured(path: str, expected: tuple[int, ...], owner: _OwnedCopy | None = None) -> bool:
    """Capture before checking; never unlink the shared/public entry.

    Unlike durable application claims, rollback must also work after the DB
    guard becomes invalid. A fresh private carrier needs no DB authority and
    is never adopted/reused. Same-UID tampering inside it is outside the same
    private-directory boundary used by private_file_claim.
    """
    from import_publication import _rename_noreplace

    if owner is None or not owner.matches(expected):
        return False
    path = os.path.abspath(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    parent_fd: int | None = None
    carrier_fd: int | None = None
    carrier_path: str | None = None
    receipt_created = False
    captured = False
    try:
        parent = os.path.dirname(path)
        parent_fd = os.open(parent, flags)
        carrier_path = tempfile.mkdtemp(prefix=".mangarr-claim-chapter-rollback-", dir=parent)
        created = os.lstat(carrier_path)
        carrier_fd = os.open(os.path.basename(carrier_path), flags, dir_fd=parent_fd)
        held = os.fstat(carrier_fd)
        if (
            (held.st_dev, held.st_ino) != (created.st_dev, created.st_ino)
            or held.st_uid != os.geteuid()
            or stat.S_IMODE(held.st_mode) not in (0o700, 0o2700)
        ):
            raise ProvenanceError("rollback private directory identity/permissions changed")
        # Normalize inherited setgid only after accepting this private inode.
        os.fchmod(carrier_fd, 0o700)
        marker_fd = os.open(
            "receipt.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=carrier_fd,
        )
        receipt_created = True
        with os.fdopen(marker_fd, "w", encoding="ascii") as marker:
            json.dump({"version": 1, "origin": path, "expected": expected}, marker)
            marker.flush()
            os.fsync(marker.fileno())
        os.fsync(carrier_fd)
        os.fsync(parent_fd)
        os.rename(
            os.path.basename(path), "artifact",
            src_dir_fd=parent_fd, dst_dir_fd=carrier_fd,
        )
        captured = True
        os.fsync(carrier_fd)
        os.fsync(parent_fd)
        current = os.stat("artifact", dir_fd=carrier_fd, follow_symlinks=False)
        if (
            stat.S_ISREG(current.st_mode)
            and _identity(current)[:len(expected)] == expected
            and owner.matches(expected)
        ):
            os.unlink("artifact", dir_fd=carrier_fd)
            captured = False
            os.fsync(carrier_fd)
            return True
    except FileNotFoundError:
        pass
    except (OSError, ProvenanceError) as exc:
        log.warning("Chapter rollback refused for %s: %s", path, exc)
    finally:
        try:
            try:
                if captured and carrier_fd is not None and parent_fd is not None:
                    try:
                        # Existing atomic primitive; no check-then-rename fallback on NAS.
                        _rename_noreplace(
                            f"/proc/self/fd/{carrier_fd}/artifact",
                            f"/proc/self/fd/{parent_fd}/{os.path.basename(path)}",
                        )
                        captured = False
                        os.fsync(carrier_fd)
                        os.fsync(parent_fd)
                    except OSError as exc:
                        log.error(
                            "Chapter rollback retained entry at %s; restore refused: %s",
                            carrier_path, exc,
                        )
                if carrier_fd is not None and not captured and receipt_created:
                    try:
                        os.unlink("receipt.json", dir_fd=carrier_fd)
                    except FileNotFoundError:
                        pass
            finally:
                if carrier_fd is not None:
                    os.close(carrier_fd)
            if parent_fd is not None and carrier_path is not None and not captured and receipt_created:
                try:
                    os.rmdir(os.path.basename(carrier_path), dir_fd=parent_fd)
                    os.fsync(parent_fd)
                except OSError:
                    pass
        finally:
            if parent_fd is not None:
                os.close(parent_fd)
    return False


def suwayomi_server(client: Mapping[str, Any]) -> str:
    """Normalised Suwayomi base URL without credentials, query or fragment."""
    from routers.download_clients import client_base_url

    parts = urlsplit(client_base_url(dict(client)))
    host = parts.hostname or ""
    if ":" in host:
        # Keep IPv6 literals bracketed so the host/port boundary survives.
        host = f"[{host}]"
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme.lower(), host.lower(), parts.path.rstrip("/"), "", ""))


def source_identity(
    client: Mapping[str, Any] | None,
    manga_id: object,
    source_id: object,
    chapter_id: object,
) -> SourceIdentity | None:
    """Build the full identity, or ``None`` when any part is missing/invalid."""
    if not client:
        return None
    client_id = client.get("id")
    ints = (client_id, manga_id, chapter_id)
    if any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in ints):
        return None
    if isinstance(source_id, bool) or not isinstance(source_id, (str, int)):
        return None
    source_text = str(source_id).strip()
    if not source_text:
        return None
    server = suwayomi_server(client)
    if not server:
        return None
    return SourceIdentity(
        int(client_id),  # type: ignore[arg-type]
        server,
        int(manga_id),  # type: ignore[arg-type]
        source_text,
        int(chapter_id),  # type: ignore[arg-type]
    )


def record(
    db: sqlite3.Connection,
    *,
    series_id: int,
    chapter_num: float,
    path: str,
    evidence: FileEvidence,
    source: SourceIdentity,
    job_id: int | None,
) -> int | None:
    """Insert the ownership record inside the caller's import transaction.

    Records only when the chapter row was settled to exactly this path.
    Performs no filesystem I/O. Returns the provenance row id, if recorded.
    """
    row = db.execute(
        "SELECT id FROM chapters WHERE series_id=? AND chapter_num=?"
        " AND status='downloaded' AND import_path=?",
        (series_id, chapter_num, path),
    ).fetchone()
    if row is None:
        return None
    db.execute(
        "INSERT INTO chapter_file_provenance("
        " chapter_id, series_id, library_path, file_dev, file_inode,"
        " size_bytes, mtime_ns, sha256, suwayomi_client_id, suwayomi_server,"
        " suwayomi_manga_id, suwayomi_source_id, suwayomi_chapter_id,"
        " suwayomi_job_id, recorded_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)"
        " ON CONFLICT(library_path) DO UPDATE SET"
        " chapter_id=excluded.chapter_id, series_id=excluded.series_id,"
        " file_dev=excluded.file_dev, file_inode=excluded.file_inode,"
        " size_bytes=excluded.size_bytes, mtime_ns=excluded.mtime_ns,"
        " sha256=excluded.sha256,"
        " suwayomi_client_id=excluded.suwayomi_client_id,"
        " suwayomi_server=excluded.suwayomi_server,"
        " suwayomi_manga_id=excluded.suwayomi_manga_id,"
        " suwayomi_source_id=excluded.suwayomi_source_id,"
        " suwayomi_chapter_id=excluded.suwayomi_chapter_id,"
        " suwayomi_job_id=excluded.suwayomi_job_id,"
        " recorded_at=CURRENT_TIMESTAMP",
        (
            row["id"],
            series_id,
            path,
            # Text, not INTEGER: st_dev/st_ino are unsigned 64-bit (NFS file
            # IDs) and can exceed SQLite's signed range.
            str(evidence.dev),
            str(evidence.inode),
            evidence.size,
            evidence.mtime_ns,
            evidence.sha256,
            source.client_id,
            source.server,
            source.manga_id,
            source.source_id,
            source.chapter_id,
            job_id,
        ),
    )
    found = db.execute(
        "SELECT id FROM chapter_file_provenance WHERE library_path=?", (path,)
    ).fetchone()
    return int(found["id"]) if found else None
