"""Opt-in quarantine of loose Suwayomi chapters a merged volume covers (#422).

Off by default (``cleanup_loose_chapters``). After a merged volume this
import created settles with an assembly record, each loose chapter that the
record proves is inside the volume is moved, journaled, into
``<library root>/.mangarr-quarantine/<series id>/`` and its chapter row is
pointed at the volume. Nothing is deleted. Restore moves a quarantined file
back, refusing when its original name is taken.

A loose chapter is eligible only when, at cleanup time:
  - its provenance row exists, still names ``chapters.import_path``, and the
    file still matches the recorded inode, size, mtime and SHA-256;
  - its chapter row belongs to the volume, which is downloaded at the path
    its assembly record names, and the volume file matches that record;
  - the provenance Suwayomi context equals one chapter entry in the record;
  - its image members, filtered and ordered as the merge orders them, hash to
    the page range that entry claims, read from the volume itself.

Identity checks deliberately exclude ``st_dev``: a remount (for example a
rescheduled container on NFS) can change it for the same file, and the
content hashes are the proof. Anything else is skipped with its reason.

Each move is one ``chapter_quarantine`` row, written before every
filesystem step: ``reserved`` -> ``captured`` (renamed into a private
directory beside it, so rescan cannot see or readopt it) -> ``committed``
(the chapter is repointed and the provenance row removed, one transaction)
-> ``quarantined`` (moved without replacing into the quarantine folder).
Recovery rolls back a row that never committed, by moving the file back to
its original name without replacing anything, and only moves a committed
row forward. Restore uses ``restoring`` -> ``restored``. A row that cannot
settle keeps its diagnostic and stays active, which fences the series like
an active volume deletion until it can.

Moves use ``renameat2`` no-replace and, where the filesystem reports that
unsupported, a hard link followed by removal of the private or reserved
source name once both names are proven the same inode; a public name is
never removed. Cross-filesystem moves are refused, never copied, and a real
probe move into the quarantine folder must succeed before any run.
"""
from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import secrets
import sqlite3
import stat
import tempfile
import zipfile
import zlib
from dataclasses import dataclass, field
from typing import Any, Literal

import chapter_provenance
import shared
import volume_assembly
from events import log_event
from file_mutation_lock import FileMutationGuard, FileMutationLockError, file_mutation_guard
from shared import get_db

log = logging.getLogger(__name__)

SETTING = "cleanup_loose_chapters"
QUARANTINE_DIR = ".mangarr-quarantine"
# Covered by rescan's reserved ``.mangarr-claim-`` prefix.
CARRIER_PREFIX = ".mangarr-claim-cleanup-"
ACTIVE_STATES = ("reserved", "captured", "committed", "restoring")
ACTIVE_STATES_SQL = "('reserved','captured','committed','restoring')"
_UNSUPPORTED = (errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS, errno.EINVAL)

Outcome = Literal["quarantined", "rolled_back", "restored", "blocked", "terminal"]


class CleanupError(RuntimeError):
    """A step could not be proven safe; the row keeps its diagnostic."""


@dataclass(frozen=True)
class FileId:
    """Content identity without ``st_dev`` (see module docstring)."""

    inode: int
    size: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True)
class Candidate:
    chapter: dict[str, Any]
    provenance_id: int
    path: str
    file: FileId
    assembly_id: int
    volume_id: int
    volume_path: str
    suwayomi_chapter_id: int


@dataclass
class CleanupSummary:
    quarantined: int = 0
    skipped: dict[str, str] = field(default_factory=dict)
    refused: str = ""


def enabled() -> bool:
    return str(shared.get_cfg(SETTING, "false") or "").lower() == "true"


def active_for_series(db: sqlite3.Connection, series_id: int) -> bool:
    return (
        db.execute(
            f"SELECT 1 FROM chapter_quarantine WHERE series_id=? AND state IN {ACTIVE_STATES_SQL} LIMIT 1",
            (series_id,),
        ).fetchone()
        is not None
    )


# ── filesystem primitives ───────────────────────────────────────────────────


def _hash_id(path: str) -> FileId:
    evidence = chapter_provenance.fingerprint(path)
    return FileId(evidence.inode, evidence.size, evidence.mtime_ns, evidence.sha256)


def _stat_matches(path: str, expected: FileId) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and (st.st_ino, st.st_size, st.st_mtime_ns) == (
        expected.inode,
        expected.size,
        expected.mtime_ns,
    )


def _same_inode(a: str, b: str) -> bool:
    try:
        first, second = os.lstat(a), os.lstat(b)
    except OSError:
        return False
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _fsync_dir(path: str) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _move_noreplace(source: str, destination: str) -> None:
    """Move ``source`` (a private or reserved name) to an absent name.

    Refuses an existing destination and a cross-filesystem move. With the
    link fallback the source name is removed only once both names are the
    same inode, so the destination is never at risk.
    """
    from import_publication import _rename_noreplace

    try:
        _rename_noreplace(source, destination)
    except OSError as exc:
        if exc.errno not in _UNSUPPORTED:
            raise
        os.link(source, destination, follow_symlinks=False)
        if not _same_inode(source, destination):
            raise CleanupError(f"link fallback produced a different file: {destination}")
        os.unlink(source)
    _fsync_dir(os.path.dirname(destination))
    _fsync_dir(os.path.dirname(source))


def _private_dir(path: str) -> None:
    st = os.lstat(path)
    if (
        not stat.S_ISDIR(st.st_mode)
        or st.st_uid != os.geteuid()
        or stat.S_IMODE(st.st_mode) & 0o077
    ):
        raise CleanupError(f"not a private directory: {path}")


def _quarantine_dir(series_dir: str, series_id: int) -> str:
    root = os.path.join(os.path.dirname(series_dir), QUARANTINE_DIR)
    target = os.path.join(root, str(series_id))
    for path in (root, target):
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        else:
            _fsync_dir(os.path.dirname(path))
        _private_dir(path)
    return target


def _probe(series_dir: str, quarantine: str) -> None:
    """A real move from the series folder into quarantine must work."""
    carrier = tempfile.mkdtemp(prefix=CARRIER_PREFIX + "probe-", dir=series_dir)
    source = os.path.join(carrier, "probe")
    destination = os.path.join(quarantine, f".probe-{secrets.token_hex(8)}")
    try:
        with open(source, "xb") as fh:
            fh.write(b"probe")
        _move_noreplace(source, destination)
        os.unlink(destination)
    finally:
        for leftover in (source,):
            try:
                os.unlink(leftover)
            except FileNotFoundError:
                pass
        os.rmdir(carrier)


# ── journal ─────────────────────────────────────────────────────────────────


def _load(row_id: int) -> dict[str, Any] | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM chapter_quarantine WHERE id=?", (row_id,)).fetchone()
    return dict(row) if row is not None else None


def _file_id(row: dict[str, Any]) -> FileId:
    return FileId(int(row["file_inode"]), int(row["size_bytes"]), int(row["mtime_ns"]), str(row["sha256"]))


def _advance(row: dict[str, Any], db: sqlite3.Connection | None = None, **changes: Any) -> dict[str, Any]:
    """CAS this row from its loaded state.

    Without ``db`` the change commits here and ``row`` is refreshed. With the
    caller's ``db`` the caller applies the returned changes to ``row`` only
    after its transaction commits, so ``row`` never runs ahead of the DB.
    """
    columns = ", ".join(f"{name}=?" for name in changes)
    sql = (
        f"UPDATE chapter_quarantine SET {columns}, updated_at=CURRENT_TIMESTAMP"
        " WHERE id=? AND operation_token=? AND state=?"
    )
    values = (*changes.values(), row["id"], row["operation_token"], row["state"])
    if db is not None:
        updated = db.execute(sql, values).rowcount
    else:
        with get_db() as conn:
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(sql, values).rowcount
    if updated != 1:
        raise CleanupError(f"chapter quarantine row {row['id']} changed underneath")
    if db is None:
        row.update(changes)
    return changes


def _block(row: dict[str, Any], diagnostic: str) -> None:
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        updated = db.execute(
            "UPDATE chapter_quarantine SET diagnostic=?, updated_at=CURRENT_TIMESTAMP"
            " WHERE id=? AND operation_token=? AND diagnostic IS NOT ?",
            (diagnostic, row["id"], row["operation_token"], diagnostic),
        ).rowcount
        if updated:
            log_event("error", f"Loose chapter cleanup held: {diagnostic}", row["series_id"], db=db)
    log.error("Chapter quarantine %s held: %s", row["id"], diagnostic)


def _carrier_file(row: dict[str, Any]) -> str | None:
    if not row["carrier_path"]:
        return None
    return os.path.join(row["carrier_path"], os.path.basename(row["original_path"]))


def _remove_carrier(row: dict[str, Any]) -> None:
    if not row["carrier_path"]:
        return
    try:
        os.rmdir(row["carrier_path"])
    except FileNotFoundError:
        return
    except OSError as exc:
        log.error("Cleanup carrier retained for review at %s: %s", row["carrier_path"], exc)
        return
    _fsync_dir(os.path.dirname(row["carrier_path"]))


# ── eligibility ─────────────────────────────────────────────────────────────


def _loose_digest(path: str) -> str:
    with zipfile.ZipFile(path, "r") as archive:
        names = volume_assembly.image_members(archive.namelist())
        if not names:
            raise CleanupError("loose chapter has no pages")
        return volume_assembly._members_digest(archive, names)


def inspect_volume(series_id: int, volume_id: int) -> tuple[list[Candidate], dict[str, str]]:
    """Read-only: which loose chapters this volume's record proves it holds."""
    skipped: dict[str, str] = {}
    with get_db() as db:
        volume = db.execute(
            "SELECT id, status, import_path FROM volumes WHERE id=? AND series_id=?",
            (volume_id, series_id),
        ).fetchone()
        if volume is None or volume["status"] != "downloaded" or not volume["import_path"]:
            return [], {"volume": "volume is not downloaded"}
        volume_path = str(volume["import_path"])
        assembly = db.execute(
            "SELECT * FROM volume_assembly WHERE volume_id=? AND library_path=?",
            (volume_id, volume_path),
        ).fetchone()
        if assembly is None:
            return [], {"volume": "no assembly record for this volume file"}
        assembly = dict(assembly)
        entries = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM volume_assembly_chapters WHERE assembly_id=? ORDER BY position",
                (assembly["id"],),
            )
        ]
        found: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        for entry in entries:
            key = f"suwayomi chapter {entry['suwayomi_chapter_id']}"
            rows = db.execute(
                "SELECT p.id AS provenance_id, p.library_path, p.file_inode, p.size_bytes AS p_size,"
                " p.mtime_ns AS p_mtime, p.sha256 AS p_sha, c.*"
                " FROM chapter_file_provenance p JOIN chapters c ON c.id=p.chapter_id"
                " WHERE p.series_id=? AND p.suwayomi_chapter_id=? AND p.suwayomi_client_id=?"
                " AND p.suwayomi_server=? AND p.suwayomi_manga_id=? AND p.suwayomi_source_id=?",
                (
                    series_id,
                    entry["suwayomi_chapter_id"],
                    assembly["suwayomi_client_id"],
                    assembly["suwayomi_server"],
                    assembly["suwayomi_manga_id"],
                    assembly["suwayomi_source_id"],
                ),
            ).fetchall()
            if len(rows) != 1:
                skipped[key] = "no single provenance record with the volume's Suwayomi context"
                continue
            row = dict(rows[0])
            if row["status"] != "downloaded" or row["import_path"] != row["library_path"]:
                skipped[key] = "chapter no longer points at its recorded loose file"
            elif row["volume_id"] != volume_id:
                skipped[key] = "chapter does not belong to this volume"
            elif row["library_path"] == volume_path:
                skipped[key] = "chapter already points at the volume"
            else:
                found.append((entry, row, assembly))
    try:
        volume_file = _hash_id(volume_path)
    except (OSError, chapter_provenance.ProvenanceError) as exc:
        return [], {"volume": f"volume file unreadable: {exc}"}
    recorded = FileId(
        int(assembly["file_inode"]), int(assembly["size_bytes"]), int(assembly["mtime_ns"]), assembly["sha256"]
    )
    if volume_file != recorded:
        return [], {"volume": "volume file no longer matches its assembly record"}
    candidates: list[Candidate] = []
    try:
        with zipfile.ZipFile(volume_path, "r") as archive:
            pages = volume_assembly.volume_pages(archive)
            for entry, row, _assembly in found:
                key = f"suwayomi chapter {entry['suwayomi_chapter_id']}"
                expected = FileId(int(row["file_inode"]), int(row["p_size"]), int(row["p_mtime"]), row["p_sha"])
                path = str(row["library_path"])
                try:
                    if _hash_id(path) != expected:
                        skipped[key] = "loose file no longer matches its provenance record"
                        continue
                    loose = _loose_digest(path)
                    if not _stat_matches(path, expected):
                        skipped[key] = "loose file changed while its pages were read"
                        continue
                    names = volume_assembly._range_members(pages, entry["first_page"], entry["last_page"])
                    in_volume = volume_assembly._members_digest(archive, names)
                except (
                    OSError, EOFError, ValueError, RuntimeError, NotImplementedError,
                    zlib.error, zipfile.BadZipFile, chapter_provenance.ProvenanceError,
                ) as exc:
                    skipped[key] = f"not verified: {exc}"
                    continue
                if loose != entry["pages_sha256"] or in_volume != entry["pages_sha256"]:
                    skipped[key] = "loose pages differ from the volume's page range"
                    continue
                chapter = {k: row[k] for k in row if k not in ("provenance_id", "library_path", "file_inode", "p_size", "p_mtime", "p_sha")}
                candidates.append(
                    Candidate(chapter, int(row["provenance_id"]), path, expected, int(assembly["id"]),
                              volume_id, volume_path, int(entry["suwayomi_chapter_id"]))
                )
    except (OSError, zipfile.BadZipFile, chapter_provenance.ProvenanceError) as exc:
        return [], {"volume": f"volume pages unreadable: {exc}"}
    if not _stat_matches(volume_path, recorded):
        return [], {"volume": "volume file changed while it was read"}
    return candidates, skipped


# ── cleanup ─────────────────────────────────────────────────────────────────


def _fenced(db: sqlite3.Connection, series_id: int) -> bool:
    from rescan_file_recovery import active_for_series as rescan_active
    from rescan_file_recovery import competing_for_series

    return rescan_active(db, series_id) or competing_for_series(db, series_id)


def _reserve(candidate: Candidate, series_id: int) -> int | None:
    from rescan import _CHAPTER_GUARD, _CHAPTER_GUARD_SQL, _guard_values

    with get_db() as db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        if _fenced(db, series_id):
            return None
        if not db.execute(
            "SELECT 1 FROM chapters WHERE " + _CHAPTER_GUARD_SQL,
            _guard_values(candidate.chapter, _CHAPTER_GUARD),
        ).fetchone():
            return None
        if not db.execute(
            "SELECT 1 FROM chapter_file_provenance WHERE id=? AND library_path=? AND sha256=?",
            (candidate.provenance_id, candidate.path, candidate.file.sha256),
        ).fetchone():
            return None
        if not db.execute(
            "SELECT 1 FROM volumes v JOIN volume_assembly a ON a.volume_id=v.id"
            " WHERE v.id=? AND v.status='downloaded' AND v.import_path=? AND a.id=? AND a.library_path=?",
            (candidate.volume_id, candidate.volume_path, candidate.assembly_id, candidate.volume_path),
        ).fetchone():
            return None
        cursor = db.execute(
            "INSERT INTO chapter_quarantine(operation_token, series_id, volume_id, chapter_id,"
            " chapter_num, assembly_id, suwayomi_chapter_id, state, original_path, volume_path,"
            " file_inode, size_bytes, mtime_ns, sha256, chapter_guard_json)"
            " VALUES(?,?,?,?,?,?,?,'reserved',?,?,?,?,?,?,?)",
            (
                secrets.token_hex(16),
                series_id,
                candidate.volume_id,
                candidate.chapter["id"],
                candidate.chapter["chapter_num"],
                candidate.assembly_id,
                candidate.suwayomi_chapter_id,
                candidate.path,
                candidate.volume_path,
                str(candidate.file.inode),
                candidate.file.size,
                candidate.file.mtime_ns,
                candidate.file.sha256,
                json.dumps(candidate.chapter, sort_keys=True, default=str),
            ),
        )
        return int(cursor.lastrowid or 0) or None


def _capture(row: dict[str, Any], guard: FileMutationGuard) -> None:
    expected = _file_id(row)
    parent = os.path.dirname(row["original_path"])
    carrier = os.path.join(parent, f"{CARRIER_PREFIX}{row['id']}-{row['operation_token'][:12]}")
    # Recorded before it exists, so recovery always knows where to look.
    _advance(row, carrier_path=carrier)
    guard.verify()
    os.mkdir(carrier, 0o700)
    _fsync_dir(parent)
    _private_dir(carrier)
    target = _carrier_file(row)
    assert target is not None
    if not _stat_matches(row["original_path"], expected):
        raise CleanupError("loose file changed before capture")
    guard.verify()
    os.rename(row["original_path"], target)
    _fsync_dir(carrier)
    _fsync_dir(parent)
    if _hash_id(target) != expected:
        raise CleanupError("captured file does not match the verified loose file")
    _advance(row, state="captured")


def _commit(row: dict[str, Any], guard: FileMutationGuard) -> bool:
    from rescan import _CHAPTER_GUARD, _CHAPTER_GUARD_SQL, _guard_values

    chapter = json.loads(row["chapter_guard_json"])
    with get_db() as db:
        assembly = db.execute(
            "SELECT file_inode, size_bytes, mtime_ns, sha256 FROM volume_assembly"
            " WHERE id=? AND volume_id=? AND library_path=?",
            (row["assembly_id"], row["volume_id"], row["volume_path"]),
        ).fetchone()
    if assembly is None or not _stat_matches(
        row["volume_path"],
        FileId(int(assembly["file_inode"]), int(assembly["size_bytes"]), int(assembly["mtime_ns"]), assembly["sha256"]),
    ):
        return False
    guard.verify()
    with get_db() as db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        if not db.execute(
            "SELECT 1 FROM volumes v JOIN volume_assembly a ON a.volume_id=v.id"
            " WHERE v.id=? AND v.status='downloaded' AND v.import_path=? AND a.id=? AND a.library_path=?",
            (row["volume_id"], row["volume_path"], row["assembly_id"], row["volume_path"]),
        ).fetchone():
            return False
        removed = db.execute(
            "DELETE FROM chapter_file_provenance WHERE chapter_id=? AND library_path=? AND sha256=?",
            (row["chapter_id"], row["original_path"], row["sha256"]),
        ).rowcount
        updated = db.execute(
            "UPDATE chapters SET status='downloaded', import_path=?, size_bytes=NULL,"
            " quality=NULL, imported_at=NULL WHERE " + _CHAPTER_GUARD_SQL,
            (row["volume_path"], *_guard_values(chapter, _CHAPTER_GUARD)),
        ).rowcount
        if removed != 1 or updated != 1:
            db.rollback()
            return False
        changes = _advance(row, db, state="committed")
    row.update(changes)
    return True


def _transfer(row: dict[str, Any], guard: FileMutationGuard) -> None:
    expected = _file_id(row)
    source = _carrier_file(row)
    if not row["quarantine_path"]:
        quarantine = _quarantine_dir(os.path.dirname(row["original_path"]), int(row["series_id"]))
        name = f"{os.path.basename(row['original_path'])}.{row['id']}"
        _advance(row, quarantine_path=os.path.join(quarantine, name))
    destination = row["quarantine_path"]
    present = source is not None and os.path.lexists(source)
    if os.path.lexists(destination):
        if not _stat_matches(destination, expected):
            raise CleanupError(f"quarantine name holds another file: {destination}")
        if present:
            if not _same_inode(source, destination):
                raise CleanupError("carrier and quarantine hold different files")
            guard.verify()
            os.unlink(source)  # private name; the quarantine name is the same inode
            _fsync_dir(os.path.dirname(source))
    else:
        if not present or not _stat_matches(source, expected):
            raise CleanupError("captured file is missing or changed")
        guard.verify()
        _move_noreplace(source, destination)
        if not _stat_matches(destination, expected):
            raise CleanupError("quarantined file does not match after the move")
    _remove_carrier(row)
    _advance(row, state="quarantined", diagnostic="", completed_at=_now())
    _note(row["series_id"], f"Moved loose chapter to quarantine (covered by its volume): "
          f"{os.path.basename(row['original_path'])}")


def _note(series_id: int, message: str) -> None:
    """Best-effort event; a finished row never depends on it."""
    try:
        log_event("info", message, series_id)
    except sqlite3.Error as exc:
        log.warning("Could not record event for series %s: %s", series_id, exc)


def _now() -> str:
    with get_db() as db:
        return str(db.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0])


def _rollback(row: dict[str, Any], guard: FileMutationGuard) -> None:
    """Undo a row that never committed; never overwrite the original name."""
    original = row["original_path"]
    source = _carrier_file(row)
    if source is not None and os.path.lexists(source):
        if os.path.lexists(original):
            if not _same_inode(source, original):
                raise CleanupError(f"original name is taken; file kept at {source}")
            guard.verify()
            os.unlink(source)  # link fallback interrupted: both names, one inode
            _fsync_dir(os.path.dirname(source))
        else:
            guard.verify()
            _move_noreplace(source, original)
    elif not os.path.lexists(original):
        raise CleanupError("loose file is in neither its original place nor its carrier")
    _remove_carrier(row)
    _advance(row, state="rolled_back", completed_at=_now())


def _settle(row: dict[str, Any], guard: FileMutationGuard, *, resume: bool) -> Outcome:
    """Drive one row to rest. ``resume`` is False only for a fresh reservation."""
    try:
        if row["state"] == "reserved" and not resume:
            _capture(row, guard)
        if row["state"] in ("reserved", "captured") and resume:
            _rollback(row, guard)
            return "rolled_back"
        if row["state"] == "captured":
            if not _commit(row, guard):
                _rollback(row, guard)
                return "rolled_back"
        if row["state"] == "committed":
            _transfer(row, guard)
            return "quarantined"
        if row["state"] == "restoring":
            return _finish_restore(row, guard)
        return "terminal"
    except FileMutationLockError:
        raise
    except (OSError, sqlite3.Error, CleanupError, chapter_provenance.ProvenanceError) as exc:
        durable = _load(row["id"])
        if durable is not None:
            row.clear()
            row.update(durable)
        if row["state"] in ("reserved", "captured") and not resume:
            try:
                _rollback(row, guard)
                log.warning("Loose chapter cleanup rolled back for %s: %s", row["original_path"], exc)
                return "rolled_back"
            except (OSError, sqlite3.Error, CleanupError) as undo:
                exc = undo
        _block(row, str(exc))
        return "blocked"


def cleanup_volume(guard: FileMutationGuard, series_id: int, volume_id: int) -> CleanupSummary:
    """Quarantine the loose chapters this volume provably holds.

    Called with the caller's guard after the volume settled. Never raises
    for an individual chapter; skips are logged with their reason.
    """
    summary = CleanupSummary()
    if not enabled():
        return summary
    candidates, summary.skipped = inspect_volume(series_id, volume_id)
    series_dir = os.path.dirname(os.path.abspath(candidates[0].volume_path)) if candidates else ""
    beside = []
    for candidate in candidates:
        if os.path.dirname(os.path.abspath(candidate.path)) != series_dir:
            summary.skipped[f"suwayomi chapter {candidate.suwayomi_chapter_id}"] = "loose file is not beside its volume"
        else:
            beside.append(candidate)
    candidates = beside
    for key, reason in summary.skipped.items():
        log.info("Loose chapter cleanup skipped %s (series %s): %s", key, series_id, reason)
    if not candidates:
        return summary
    try:
        guard.verify()
        _probe(series_dir, _quarantine_dir(series_dir, series_id))
    except (OSError, CleanupError) as exc:
        summary.refused = f"quarantine move refused: {exc}"
        log_event("warning", f"Loose chapter cleanup refused: {summary.refused}", series_id)
        return summary
    for candidate in candidates:
        guard.verify()
        row_id = _reserve(candidate, series_id)
        if row_id is None:
            summary.skipped[f"suwayomi chapter {candidate.suwayomi_chapter_id}"] = "changed before reservation"
            continue
        row = _load(row_id)
        assert row is not None
        if _settle(row, guard, resume=False) == "quarantined":
            summary.quarantined += 1
        elif row["state"] in ACTIVE_STATES:
            break  # the series is fenced until this row settles
    return summary


# ── restore ─────────────────────────────────────────────────────────────────


def _refuse_restore(row: dict[str, Any], reason: str) -> Outcome:
    """Nothing of ours moved: back to ``quarantined``, which lifts the fence."""
    _advance(row, state="quarantined", diagnostic=f"restore refused: {reason}")
    return "terminal"


def _chapter_on_volume_sql() -> str:
    return (
        "SELECT 1 FROM chapters c JOIN volumes v ON v.id=c.volume_id"
        " WHERE c.id=? AND v.id=? AND c.status='downloaded' AND v.status='downloaded'"
        " AND c.import_path=v.import_path"
    )


def _finish_restore(row: dict[str, Any], guard: FileMutationGuard) -> Outcome:
    expected = _file_id(row)
    source, destination = row["quarantine_path"], row["original_path"]
    source_present = os.path.lexists(source)
    if os.path.lexists(destination) and _stat_matches(destination, expected):
        # The move already happened (or its link half did).
        if source_present:
            if not _same_inode(source, destination):
                raise CleanupError("quarantine and original hold different files")
            guard.verify()
            os.unlink(source)  # reserved name; the original is the same inode
            _fsync_dir(os.path.dirname(source))
    else:
        if os.path.lexists(destination):
            return _refuse_restore(row, "original name is taken")
        if not source_present:
            return _refuse_restore(row, "quarantined file is missing")
        try:
            if _hash_id(source) != expected:
                return _refuse_restore(row, "quarantined file changed")
            guard.verify()
            _move_noreplace(source, destination)
        except (OSError, CleanupError, chapter_provenance.ProvenanceError) as exc:
            if os.path.lexists(destination):
                raise  # the move started; only finishing it is safe
            return _refuse_restore(row, str(exc))
        if not _stat_matches(destination, expected):
            raise CleanupError("restored file does not match after the move")
    guard.verify()
    with get_db() as db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        repointed = db.execute(
            "UPDATE chapters SET import_path=?, size_bytes=?, quality='cbz',"
            " imported_at=CURRENT_TIMESTAMP WHERE id=? AND status='downloaded'"
            " AND import_path=(SELECT import_path FROM volumes WHERE id=? AND status='downloaded')",
            (destination, expected.size, row["chapter_id"], row["volume_id"]),
        ).rowcount
        note = "" if repointed else "chapter had changed; left for rescan to adopt"
        changes = _advance(row, db, state="restored", diagnostic=note, completed_at=_now())
    row.update(changes)
    _note(row["series_id"], f"Restored loose chapter {os.path.basename(destination)}")
    return "restored"


def restore(row_id: int, series_id: int | None = None) -> tuple[Outcome, str]:
    """Move one quarantined chapter back; refused if its name is taken.

    Also refused while the chapter no longer points at its volume (for
    example the volume was deleted or replaced), so a restored file is never
    left with nothing pointing at it.
    """
    try:
        with file_mutation_guard(shared.DB_PATH) as guard:
            guard.verify()
            with get_db() as db:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT * FROM chapter_quarantine WHERE id=?", (row_id,)).fetchone()
                if (
                    current is None
                    or current["state"] != "quarantined"
                    or (series_id is not None and int(current["series_id"]) != series_id)
                ):
                    return "terminal", "not a quarantined chapter"
                series_id = int(current["series_id"])
                if _fenced(db, series_id) or active_for_series(db, series_id):
                    return "blocked", "another file operation is active for this series"
                if not db.execute(_chapter_on_volume_sql(), (current["chapter_id"], current["volume_id"])).fetchone():
                    return "terminal", "the chapter no longer points at its volume"
                row = dict(current)
                changes = _advance(row, db, state="restoring", diagnostic="")
            row.update(changes)
            outcome = _settle(row, guard, resume=True)
            latest = _load(row_id) or row
            return outcome, str(latest.get("diagnostic") or "")
    except FileMutationLockError:
        return "blocked", "another file operation is running"
    except sqlite3.Error as exc:
        return "blocked", f"database error: {exc}"


# ── recovery ────────────────────────────────────────────────────────────────


def replay(row_id: int) -> Outcome:
    """Settle one interrupted row: roll back before commit, else go forward."""
    try:
        with file_mutation_guard(shared.DB_PATH) as guard:
            guard.verify()
            row = _load(row_id)
            if row is None or row["state"] not in ACTIVE_STATES:
                return "terminal"
            return _settle(row, guard, resume=True)
    except (FileMutationLockError, sqlite3.Error):
        return "blocked"


def active_ids(*, after_id: int = 0, limit: int = 100) -> list[int]:
    with get_db() as db:
        return [
            int(r["id"])
            for r in db.execute(
                f"SELECT id FROM chapter_quarantine WHERE state IN {ACTIVE_STATES_SQL}"
                " AND id>? ORDER BY id LIMIT ?",
                (after_id, limit),
            )
        ]


async def replay_page(*, after_id: int = 0, limit: int = 100) -> tuple[int, int, int]:
    """Replay one keyset page; returns (examined, settled, last_id)."""
    examined = settled = last = 0
    for row_id in active_ids(after_id=after_id, limit=limit):
        examined += 1
        last = row_id
        task = asyncio.create_task(asyncio.to_thread(replay, row_id))
        try:
            outcome = await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)
            raise
        if outcome != "blocked":
            settled += 1
    return examined, settled, last


async def drain(*, page_size: int = 100) -> None:
    cursor = 0
    while True:
        examined, _settled, last = await replay_page(after_id=cursor, limit=page_size)
        if examined < page_size:
            return
        cursor = last


def quarantined_for_series(series_id: int) -> list[dict[str, Any]]:
    with get_db() as db:
        return [
            dict(r)
            for r in db.execute(
                "SELECT id, chapter_num, state, original_path, quarantine_path, diagnostic, updated_at"
                f" FROM chapter_quarantine WHERE series_id=? AND (state='quarantined' OR state IN {ACTIVE_STATES_SQL})"
                " ORDER BY chapter_num, id",
                (series_id,),
            )
        ]
