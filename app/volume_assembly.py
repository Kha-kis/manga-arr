"""Evidence of how a merged volume was assembled from Suwayomi chapters.

A merged volume is staged under a private name in the series directory,
finished (ComicInfo) there, fingerprinted, and only then published to its
absent final name. When every source chapter was selected by its own
Suwayomi identity, the import records, per published volume, the final
file's fingerprint and, per source chapter in order, the source file's
SHA-256 and the page range it produced, with a SHA-256 over those pages.

The record is evidence for a later, separately reviewed opt-in cleanup
(issue #422); nothing here deletes or moves library files beyond the private
stage this import created.

Never recorded for: a volume that already existed (reused), non-merge mode,
chapters resolved by legacy filename matching, a source or final volume above
the fingerprint bound, a source that changed while it was read, a chapter that
produced no pages, pages that cannot be verified, or historical volumes. None
of these prevents the import itself.

Page ``N`` of a volume is the image member whose stem is the decimal ``N``
(``0001.jpg``, ..., ``10000.png``). A chapter's page digest is SHA-256 over,
for each of its pages in order, the 8-byte big-endian length followed by the
page bytes.
"""
from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import stat
import tempfile
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import chapter_provenance
from chapter_provenance import (
    FileEvidence,
    ProvenanceError,
    SourceIdentity,
    fingerprint,
)

log = logging.getLogger(__name__)

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif")
STAGE_PREFIX = ".mangarr-claim-volume-"
_STAGE_NAME = "volume.cbz"
_HASH_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class AssemblySource:
    """One chapter file going into a volume, with its identity when known."""

    path: str
    identity: SourceIdentity | None = None


@dataclass(frozen=True)
class ChapterPages:
    position: int
    identity: SourceIdentity
    source_sha256: str
    source_size: int
    first_page: int
    last_page: int
    pages_sha256: str


@dataclass(frozen=True)
class VolumeEvidence:
    evidence: FileEvidence
    chapters: tuple[ChapterPages, ...]


@dataclass(frozen=True)
class AssembledVolume:
    """Outcome of one volume import.

    ``assembly`` is set only when this import created the file at ``path``
    and every source chapter's pages were verified in the published bytes.
    """

    path: str | None
    size: int
    assembly: VolumeEvidence | None = None


def image_members(names: Sequence[str]) -> list[str]:
    """Image members of a chapter archive, in the order they are merged."""
    return sorted(
        n
        for n in names
        if os.path.splitext(n)[1].lower() in IMAGE_EXTENSIONS
        and not os.path.basename(n).startswith(".")
    )


def page_digest(pages: Sequence[bytes]) -> str:
    digest = hashlib.sha256()
    for data in pages:
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def volume_pages(archive: zipfile.ZipFile) -> dict[int, str]:
    """Map page numbers to member names; refuse ambiguous numbering."""
    pages: dict[int, str] = {}
    for name in archive.namelist():
        stem, ext = os.path.splitext(name)
        if ext.lower() not in IMAGE_EXTENSIONS:
            continue
        if not stem.isdigit() or not stem.isascii():
            raise ProvenanceError(f"unexpected volume member: {name}")
        number = int(stem)
        if number in pages:
            raise ProvenanceError(f"duplicate volume page {number}")
        pages[number] = name
    return pages


def _range_members(pages: dict[int, str], first: int, last: int) -> list[str]:
    names = []
    for number in range(first, last + 1):
        name = pages.get(number)
        if name is None:
            raise ProvenanceError(f"volume page {number} is missing")
        names.append(name)
    return names


def _members_digest(archive: zipfile.ZipFile, names: Sequence[str]) -> str:
    """``page_digest`` of these members, read in bounded chunks."""
    digest = hashlib.sha256()
    for name in names:
        info = archive.getinfo(name)
        digest.update(info.file_size.to_bytes(8, "big"))
        consumed = 0
        with archive.open(info) as member:
            while chunk := member.read(_HASH_CHUNK_SIZE):
                consumed += len(chunk)
                digest.update(chunk)
        if consumed != info.file_size:
            raise ProvenanceError(f"volume member size mismatch: {name}")
    return digest.hexdigest()


def range_digest(archive: zipfile.ZipFile, first: int, last: int) -> str:
    """Page digest of volume pages ``first``..``last``, one chunk at a time."""
    return _members_digest(archive, _range_members(volume_pages(archive), first, last))


def _identity(st: os.stat_result) -> tuple[int, int, int, int]:
    return (int(st.st_dev), int(st.st_ino), int(st.st_size), int(st.st_mtime_ns))


def _hash_open_file(fh) -> str:
    digest = hashlib.sha256()
    fh.seek(0)
    while chunk := fh.read(_HASH_CHUNK_SIZE):
        digest.update(chunk)
    fh.seek(0)
    return digest.hexdigest()


def _merge_into(
    out: zipfile.ZipFile, sources: Sequence[AssemblySource]
) -> tuple[ChapterPages, ...] | None:
    """Write every source's pages into ``out``; return evidence if provable."""
    page = 1
    chapters: list[ChapterPages] = []
    provable = all(s.identity is not None for s in sources)
    for position, source in enumerate(sources):
        descriptor = os.open(
            source.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        )
        with os.fdopen(descriptor, "rb") as fh:
            before = os.fstat(fh.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ProvenanceError(f"source is not a regular file: {source.path}")
            source_sha = None
            if provable and before.st_size <= chapter_provenance.FINGERPRINT_MAX_BYTES:
                source_sha = _hash_open_file(fh)
            first = page
            digest = hashlib.sha256()
            with zipfile.ZipFile(fh, "r") as chapter:
                for name in image_members(chapter.namelist()):
                    data = chapter.read(name)
                    out.writestr(f"{page:04d}{os.path.splitext(name)[1]}", data)
                    digest.update(len(data).to_bytes(8, "big"))
                    digest.update(data)
                    page += 1
            after = os.fstat(fh.fileno())
        if source_sha is None or _identity(before) != _identity(after) or page == first:
            if provable:
                log.info("Volume assembly evidence not recorded: %s unproven", source.path)
            provable = False
            continue
        assert source.identity is not None
        chapters.append(
            ChapterPages(
                position=position,
                identity=source.identity,
                source_sha256=source_sha,
                source_size=int(after.st_size),
                first_page=first,
                last_page=page - 1,
                pages_sha256=digest.hexdigest(),
            )
        )
    if page == 1:
        raise ProvenanceError("no pages to merge")
    return tuple(chapters) if provable else None


def _verify_pages(path: str, chapters: Sequence[ChapterPages]) -> bool:
    """Whether the finished file holds exactly the recorded pages.

    Any failure here only withholds evidence; it never fails the import.
    """
    try:
        with zipfile.ZipFile(path, "r") as archive:
            pages = volume_pages(archive)
            for chapter in chapters:
                names = _range_members(pages, chapter.first_page, chapter.last_page)
                if _members_digest(archive, names) != chapter.pages_sha256:
                    return False
    except (OSError, zipfile.BadZipFile, ProvenanceError) as exc:
        log.info("Volume pages not verified: %s: %s", path, exc)
        return False
    last = chapters[-1].last_page if chapters else 0
    return max(pages, default=0) == last


def _file_identity(path: str) -> tuple[int, int, int, int]:
    current = os.lstat(path)
    if not stat.S_ISREG(current.st_mode):
        raise ProvenanceError(f"not a regular file: {path}")
    return _identity(current)


def _open_carrier(parent: str) -> tuple[str, int]:
    """Create this attempt's private 0700 directory beside the final name."""
    carrier = tempfile.mkdtemp(prefix=STAGE_PREFIX, dir=parent)
    descriptor = os.open(
        carrier, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    held = os.fstat(descriptor)
    if held.st_uid != os.geteuid() or stat.S_IMODE(held.st_mode) not in (0o700, 0o2700):
        os.close(descriptor)
        raise ProvenanceError(f"volume stage directory identity changed: {carrier}")
    # Normalize inherited setgid only after accepting this private inode.
    os.fchmod(descriptor, 0o700)
    return carrier, descriptor


def _dispose_carrier(carrier: str, carrier_fd: int) -> None:
    """Remove this attempt's private directory; keep anything unexpected."""
    try:
        try:
            # After the link fallback this is a second name for the volume.
            os.unlink(_STAGE_NAME, dir_fd=carrier_fd)
        except FileNotFoundError:
            pass
        held = os.fstat(carrier_fd)
        current = os.lstat(carrier)
        if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
            raise ProvenanceError(f"volume stage directory replaced: {carrier}")
        os.rmdir(carrier)
    except (OSError, ProvenanceError) as exc:
        log.error("Volume stage retained for review at %s: %s", carrier, exc)
    finally:
        os.close(carrier_fd)


def assemble(
    sources: Sequence[AssemblySource],
    out_path: str,
    finish: Callable[[str], None] = lambda _path: None,
) -> AssembledVolume:
    """Merge ``sources`` into a new volume at the absent ``out_path``.

    The volume is written and finished (``finish``, e.g. ComicInfo) inside a
    private directory beside ``out_path``, then published without replacing
    anything, provided the finished file is still the same file. Content
    evidence (SHA-256 and verified page ranges) is optional: when it cannot be
    produced the volume still imports, with no ``assembly``. Returns
    ``AssembledVolume(None, 0)`` when nothing was published; an existing
    ``out_path`` is never replaced.
    """
    from import_publication import _fsync_directory, _publish_absent_stage

    parent = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(parent, exist_ok=True)
    try:
        carrier, carrier_fd = _open_carrier(parent)
    except (OSError, ProvenanceError) as exc:
        log.error("Volume assembly failed → %s: %s", out_path, exc)
        return AssembledVolume(None, 0)
    stage = os.path.join(carrier, _STAGE_NAME)
    published = False
    finished: tuple[int, int, int, int] | None = None
    try:
        descriptor = os.open(
            _STAGE_NAME,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o644,
            dir_fd=carrier_fd,
        )
        with os.fdopen(descriptor, "w+b") as fh:
            with zipfile.ZipFile(fh, "w", zipfile.ZIP_STORED) as out:
                chapters = _merge_into(out, sources)
            fh.flush()
            os.fsync(fh.fileno())
        finish(stage)
        finished = _file_identity(stage)
        evidence: FileEvidence | None = None
        if chapters is not None and finished[2] > chapter_provenance.FINGERPRINT_MAX_BYTES:
            log.info("Volume above the fingerprint bound; no evidence: %s", out_path)
            chapters = None
        if chapters is not None:
            evidence = fingerprint(stage)
            if (evidence.dev, evidence.inode, evidence.size, evidence.mtime_ns) != finished:
                raise ProvenanceError(f"volume stage changed while hashing: {stage}")
            if not _verify_pages(stage, chapters):
                log.warning("Volume pages differ from their sources; no evidence: %s", out_path)
                chapters = None
        if _file_identity(stage) != finished:
            raise ProvenanceError(f"volume stage changed before publication: {stage}")
        _publish_absent_stage(stage, out_path)
        published = True
        _fsync_directory(parent)
    except FileExistsError:
        log.warning("Volume already exists; not replaced: %s", out_path)
        return AssembledVolume(None, 0)
    except (OSError, zipfile.BadZipFile, ProvenanceError) as exc:
        if not published:
            log.error("Volume assembly failed → %s: %s", out_path, exc)
            return AssembledVolume(None, 0)
        log.warning("Volume published but not synced: %s: %s", out_path, exc)
        chapters = None
    finally:
        _dispose_carrier(carrier, carrier_fd)
    try:
        unchanged = _file_identity(out_path) == finished
    except (OSError, ProvenanceError):
        unchanged = False
    if not unchanged:
        log.warning("Volume changed after publication; no evidence: %s", out_path)
        chapters = None
    size = os.path.getsize(out_path)
    return AssembledVolume(
        out_path,
        size,
        VolumeEvidence(evidence, chapters) if chapters and evidence else None,
    )


def record(
    db: sqlite3.Connection,
    *,
    series_id: int,
    volume_num: float,
    path: str,
    assembly: VolumeEvidence,
    job_id: int | None,
) -> int | None:
    """Insert the assembly record inside the caller's import transaction.

    Records only when the volume row was settled to exactly this path.
    Performs no filesystem I/O. Returns the record id, if recorded.
    """
    row = db.execute(
        "SELECT id FROM volumes WHERE series_id=? AND volume_num=?"
        " AND status='downloaded' AND import_path=?",
        (series_id, volume_num, path),
    ).fetchone()
    if row is None or not assembly.chapters:
        return None
    first = assembly.chapters[0].identity
    if any(
        (c.identity.client_id, c.identity.server, c.identity.manga_id, c.identity.source_id)
        != (first.client_id, first.server, first.manga_id, first.source_id)
        for c in assembly.chapters
    ):
        return None
    evidence = assembly.evidence
    if not _well_formed(assembly):
        log.warning("Volume assembly evidence malformed; not recorded: %s", path)
        return None
    # Evidence is optional: a refused insert must never fail the import.
    db.execute("SAVEPOINT volume_assembly_record")
    try:
        assembly_id = _insert(db, row["id"], series_id, path, assembly, first, job_id)
    except sqlite3.Error as exc:
        db.execute("ROLLBACK TO volume_assembly_record")
        db.execute("RELEASE volume_assembly_record")
        log.warning("Volume assembly evidence not recorded for %s: %s", path, exc)
        return None
    db.execute("RELEASE volume_assembly_record")
    return assembly_id


def _well_formed(assembly: VolumeEvidence) -> bool:
    expected_first = 1
    for position, c in enumerate(assembly.chapters):
        if (
            c.position != position
            or c.first_page != expected_first
            or c.last_page < c.first_page
            or c.source_size <= 0
            or len(c.source_sha256) != 64
            or len(c.pages_sha256) != 64
        ):
            return False
        expected_first = c.last_page + 1
    return assembly.evidence.size > 0 and len(assembly.evidence.sha256) == 64


def _insert(
    db: sqlite3.Connection,
    volume_id: int,
    series_id: int,
    path: str,
    assembly: VolumeEvidence,
    first: SourceIdentity,
    job_id: int | None,
) -> int:
    evidence = assembly.evidence
    db.execute("DELETE FROM volume_assembly WHERE library_path=?", (path,))
    cursor = db.execute(
        "INSERT INTO volume_assembly("
        " volume_id, series_id, library_path, file_dev, file_inode, size_bytes,"
        " mtime_ns, sha256, suwayomi_client_id, suwayomi_server,"
        " suwayomi_manga_id, suwayomi_source_id, suwayomi_job_id, recorded_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
        (
            volume_id,
            series_id,
            path,
            # Text, as in chapter_file_provenance: unsigned 64-bit NFS ids.
            str(evidence.dev),
            str(evidence.inode),
            evidence.size,
            evidence.mtime_ns,
            evidence.sha256,
            first.client_id,
            first.server,
            first.manga_id,
            first.source_id,
            job_id,
        ),
    )
    assembly_id = int(cursor.lastrowid)
    db.executemany(
        "INSERT INTO volume_assembly_chapters("
        " assembly_id, position, suwayomi_chapter_id, source_sha256,"
        " source_size_bytes, first_page, last_page, pages_sha256)"
        " VALUES(?,?,?,?,?,?,?,?)",
        [
            (
                assembly_id,
                c.position,
                c.identity.chapter_id,
                c.source_sha256,
                c.source_size,
                c.first_page,
                c.last_page,
                c.pages_sha256,
            )
            for c in assembly.chapters
        ],
    )
    return assembly_id


def void(db: sqlite3.Connection, assembly_id: int) -> None:
    db.execute("DELETE FROM volume_assembly WHERE id=?", (assembly_id,))


__all__ = [
    "AssembledVolume",
    "AssemblySource",
    "ChapterPages",
    "VolumeEvidence",
    "assemble",
    "image_members",
    "page_digest",
    "range_digest",
    "record",
    "void",
    "volume_pages",
]
