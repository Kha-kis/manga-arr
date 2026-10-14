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
import os
import sqlite3
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_HASH_CHUNK_SIZE = 1024 * 1024
# Fingerprints stream in fixed chunks; files above this bound are still
# imported but never get ownership evidence.
FINGERPRINT_MAX_BYTES = 4 * 1024 * 1024 * 1024


class ProvenanceError(RuntimeError):
    """A copy or fingerprint could not be proven; ownership is not recorded."""


@dataclass(frozen=True)
class FileEvidence:
    dev: int
    inode: int
    size: int
    mtime_ns: int
    sha256: str


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


def _identity(st: os.stat_result) -> tuple[int, int, int, int]:
    return (int(st.st_dev), int(st.st_ino), int(st.st_size), int(st.st_mtime_ns))


def fingerprint(path: str) -> FileEvidence:
    """Hash one non-symlink regular file, refusing if it changes meanwhile."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
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


def remove_if_unchanged(path: str, evidence: FileEvidence) -> bool:
    """Roll back a copy this import created, only if it is still that file."""
    if not same_file(path, evidence):
        return False
    try:
        os.unlink(path)
    except FileNotFoundError:
        return False
    return True


def copy_new_file(src: str, dst: str) -> tuple[FileEvidence, bool]:
    """Create ``dst`` exclusively from ``src`` and verify the copy.

    Returns ``(evidence, verified)``. ``evidence`` always identifies the file
    this call created, so a caller can roll it back. ``verified`` is true only
    when the copy's SHA-256 and size match the bytes read from a source that
    did not change during the read; a source above the fingerprint bound is
    copied unverified. All writes and metadata go through the exclusively
    created descriptor, so a file swapped in at ``dst`` is never written to or
    adopted. Raises ``FileExistsError`` when ``dst`` already exists, so a
    pre-existing file is never treated as created here. Any other failure
    removes the partial copy this call created and re-raises.
    """
    verify = os.path.getsize(src) <= FINGERPRINT_MAX_BYTES
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    created = os.fstat(fd)
    try:
        digest = hashlib.sha256()
        size = 0
        with open(src, "rb") as source:
            source_before = os.fstat(source.fileno())
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
    except BaseException:
        os.close(fd)
        _unlink_created(dst, created)
        raise
    os.close(fd)
    try:
        current = os.lstat(dst)
        if stat.S_ISLNK(current.st_mode) or _identity(current) != _identity(written):
            raise ProvenanceError(f"copy was replaced while copying: {dst}")
        if not verify:
            return FileEvidence(*_identity(written), ""), False
        evidence = fingerprint(dst)
        if (evidence.dev, evidence.inode) != _identity(created)[:2]:
            raise ProvenanceError(f"copy was replaced while verifying: {dst}")
        if evidence.size != size or evidence.sha256 != digest.hexdigest():
            raise ProvenanceError(f"copy does not match source bytes: {dst}")
    except BaseException:
        _unlink_created(dst, created)
        raise
    return evidence, True


def _write_all(fd: int, chunk: bytes) -> None:
    view = memoryview(chunk)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _unlink_created(path: str, created: os.stat_result) -> None:
    try:
        current = os.lstat(path)
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
        os.unlink(path)


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
