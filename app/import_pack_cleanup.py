"""Durable coordination for generated import-pack staging."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import importlib
import json
import logging
import math
import os
import secrets
import shutil
import sqlite3
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from contextlib import ExitStack, contextmanager
from collections.abc import Iterator
from typing import Literal, cast

from download_identity import (
    DownloadIdentity,
    DownloadProtocol,
    coerce_download_client_id,
    download_identities_match,
    download_identity_key,
    download_identity_path_token,
    normalize_download_id,
    normalize_download_protocol,
    resolve_download_protocol,
)
from acquisition_policy import acquisition_policy
from files import safe_join_under
from shared import get_db
import shared
from file_mutation_lock import FileMutationGuard, FileMutationBusy, file_mutation_guard
from private_file_claim import (
    PrivateClaimError,
    ClaimBinding,
    ensure_namespace,
    open_namespace,
    open_carrier,
    allocate_carrier,
    claim_into_empty,
    gc_discarded_carrier,
)
from private_pack_claim import (
    DIR_FLAGS,
    OWNER_MARKER,
    DirectoryProof,
    PackOwnership,
    PackDirectoryClaim,
    PackInventory,
    PackProofError,
    open_directory,
    directory_proof,
    verify_directory,
    inventory_tree,
    verify_inventory,
    FullFileFingerprint,
)

log = logging.getLogger(__name__)

PACK_RESERVATION_SECONDS = 15 * 60
_TERMINAL_QUEUE_STATUSES = frozenset(("imported", "failed", "skipped"))
_ACTIVE_PUBLICATION_STATES = (
    "staging",
    "prepared",
    "publishing",
    "published",
    "db_committed",
    "cleaning",
)
_RENAME_NOREPLACE = 1
_AT_FDCWD = -100
_PACK_OWNER_MARKER_NAME = ".mangarr-pack-owner"
_FD_SAFE_RMTREE = shutil.rmtree.avoids_symlink_attacks
_SYNCHRONOUS_NAMES = {
    0: "OFF",
    1: "NORMAL",
    2: "FULL",
    3: "EXTRA",
}

ReservationPurpose = Literal["queueing", "cleanup"]
FilesystemCheckpoint = Callable[[], None]


@dataclass(frozen=True, slots=True)
class PackCleanupRecovery:
    """Summary of one bounded stale-reservation/tombstone recovery pass."""

    reservations_recovered: int = 0
    tombstones_removed: int = 0
    tombstones_retained: int = 0


@dataclass(frozen=True, slots=True)
class _PackReservation:
    download_identity_key: str
    download_client_id: int | None
    protocol: DownloadProtocol | None
    normalized_download_id: str
    download_id: str
    purpose: ReservationPurpose
    owner_token: str
    artifact_owner_token: str
    queue_id: int | None
    publication_id: int | None
    pack_path: str
    tombstone_path: str | None
    directory_ownership_json: str | None = None


@contextmanager
def _borrow_pack_guard(guard: FileMutationGuard | None) -> Iterator[FileMutationGuard]:
    if guard is not None:
        guard.verify()
        yield guard
    else:
        with file_mutation_guard(shared.DB_PATH) as owned:
            yield owned


def _ownership(reservation: _PackReservation) -> PackOwnership | None:
    if reservation.directory_ownership_json is None:
        return None
    proof = PackOwnership.from_json(reservation.directory_ownership_json)
    if proof.identity_key != reservation.download_identity_key:
        raise PackProofError("pack proof identity mismatch")
    canonical, private = pack_queue_creation_paths(
        reservation.download_id,
        proof.artifact_owner_token,
        download_client_id=reservation.download_client_id,
        protocol=reservation.protocol,
    )
    placement = proof.placement_carrier
    physical = None
    if placement is not None:
        if placement.origin_path != private or placement.carrier_path != os.path.join(
            os.path.dirname(canonical), ".mangarr-claims", placement.owner_token
        ):
            raise PackProofError("pack placement origin mismatch")
        physical = os.path.join(placement.carrier_path, "artifact")
    if (
        reservation.pack_path != canonical
        or (
            proof.private_directory is not None
            and proof.private_directory.path not in (private, physical)
        )
        or (
            proof.canonical_directory is not None
            and proof.canonical_directory.path not in (canonical, physical)
        )
    ):
        raise PackProofError("pack proof paths mismatch")
    for claim in proof.claims:
        if (
            claim.carrier.binding.operation_key != proof.artifact_owner_token
            or claim.source_directory.path not in (canonical, private)
            or claim.artifact_owner_token not in (None, proof.artifact_owner_token)
        ):
            raise PackProofError("pack directory claim ownership mismatch")
    return proof


def _read_reservation(identity_key: str) -> _PackReservation | None:
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM import_pack_cleanup_reservations WHERE download_identity_key=?",
            (identity_key,),
        ).fetchone()
    return _reservation_from_row(dict(row)) if row is not None else None


def _save_ownership(
    guard: FileMutationGuard,
    reservation: _PackReservation,
    ownership: PackOwnership,
    *,
    tombstone_path: str | None = None,
) -> _PackReservation:
    guard.verify()
    encoded = ownership.to_json()
    PackOwnership.from_json(encoded)
    with get_db() as db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        cur = db.execute(
            "UPDATE import_pack_cleanup_reservations SET directory_ownership_json=?,"
            " tombstone_path=COALESCE(?,tombstone_path), updated_at=CURRENT_TIMESTAMP"
            " WHERE download_identity_key=? AND owner_token=?",
            (
                encoded,
                tombstone_path,
                reservation.download_identity_key,
                reservation.owner_token,
            ),
        )
        if cur.rowcount != 1:
            raise PackProofError("pack reservation authority lost")
    return replace(
        reservation,
        directory_ownership_json=encoded,
        tombstone_path=tombstone_path or reservation.tombstone_path,
        artifact_owner_token=ownership.artifact_owner_token,
    )


def _pack_reservation_for_owner(
    download_id: str,
    owner: str,
    *,
    download_client_id: int | None,
    protocol: DownloadProtocol | None,
) -> _PackReservation:
    identity = _pack_identity(
        download_id, download_client_id=download_client_id, protocol=protocol
    )
    reservation = _read_reservation(download_identity_key(identity))
    if (
        reservation is None
        or reservation.owner_token != owner
        or reservation.queue_id is not None
    ):
        raise FileExistsError(errno.EEXIST, "pack reservation authority lost")
    return reservation


def _create_pack_directory(parent: int, name: str) -> int:
    os.mkdir(name, 0o700, dir_fd=parent)
    created = os.stat(name, dir_fd=parent, follow_symlinks=False)
    descriptor = os.open(name, DIR_FLAGS, dir_fd=parent)
    try:
        held = os.fstat(descriptor)
        if (held.st_dev, held.st_ino) != (created.st_dev, created.st_ino):
            raise PackProofError("fresh pack directory was replaced before open")
        if (
            not stat.S_ISDIR(held.st_mode)
            or held.st_uid != os.geteuid()
            or stat.S_IMODE(held.st_mode) not in (0o700, 0o2700)
        ):
            raise PackProofError("fresh pack directory has unexpected owner/mode")
        # Like shared carriers, normalize inherited setgid only on our fresh inode.
        if stat.S_IMODE(held.st_mode) != 0o700:
            os.fchmod(descriptor, 0o700)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
            raise PackProofError("fresh pack directory binding changed")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _prepare_pack_private_directory(
    guard: FileMutationGuard,
    download_id: str,
    owner: str,
    *,
    download_client_id: int | None,
    protocol: DownloadProtocol | None,
) -> tuple[int, DirectoryProof]:
    guard.verify()
    reservation = _pack_reservation_for_owner(
        download_id, owner, download_client_id=download_client_id, protocol=protocol
    )
    canonical, private = pack_queue_creation_paths(
        download_id, owner, download_client_id=download_client_id, protocol=protocol
    )
    # Distinguish new creation gaps from pre-upgrade NULL journals before mkdir.
    reservation = _save_ownership(
        guard,
        reservation,
        PackOwnership(
            reservation.download_identity_key, owner, retained_reason="creating-private"
        ),
    )
    root = os.path.dirname(canonical)
    if not os.path.lexists(root):
        parent_path = os.path.dirname(root)
        with open_directory(parent_path) as parent:
            before = os.fstat(parent)

            def verify_birth_parent() -> None:
                guard.verify()
                held, current = os.fstat(parent), os.lstat(parent_path)
                if (
                    not stat.S_ISDIR(current.st_mode)
                    or (held.st_dev, held.st_ino) != (before.st_dev, before.st_ino)
                    or (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino)
                    or held.st_uid != os.geteuid()
                    or stat.S_IMODE(held.st_mode) & 0o022
                ):
                    raise PackProofError(
                        "fresh pack cache requires exclusive app-owned parent"
                    )

            verify_birth_parent()
            os.mkdir(os.path.basename(root), 0o755, dir_fd=parent)
            with open_directory(root) as root_fd:
                os.fsync(root_fd)
            os.fsync(parent)
            verify_birth_parent()
    binding = ClaimBinding("pack", owner, None, "pack_cleanup")
    with ensure_namespace(guard, os.path.dirname(canonical)) as namespace:
        with allocate_carrier(namespace, binding, private) as carrier:
            ownership = PackOwnership(
                reservation.download_identity_key,
                owner,
                version=2,
                placement_carrier=carrier.record,
                retained_reason="creating-private",
            )
            reservation = _save_ownership(guard, reservation, ownership)
            descriptor = _create_pack_directory(carrier.fd, "artifact")
            try:
                _write_pack_owner_marker(
                    f"/proc/self/fd/{descriptor}",
                    _pack_identity(
                        download_id,
                        download_client_id=download_client_id,
                        protocol=protocol,
                    ),
                    owner,
                )
                os.fsync(descriptor)
                os.fsync(carrier.fd)
                carrier.verify()
                proof = directory_proof(carrier.artifact_path, descriptor)
                _save_ownership(
                    guard,
                    reservation,
                    replace(
                        ownership,
                        private_directory=proof,
                        retained_reason=None,
                        placement_carrier=replace(carrier.record, phase="claimed"),
                    ),
                )
            except BaseException:
                os.close(descriptor)
                raise
    return descriptor, proof


def _pack_identity(
    download_id: str,
    *,
    download_client_id: int | None,
    protocol: DownloadProtocol | None,
) -> DownloadIdentity:
    """Return a validated identity without assigning legacy ownership."""
    return DownloadIdentity(
        coerce_download_client_id(download_client_id),
        normalize_download_protocol(protocol),
        download_id,
    )


def normalize_pack_download_id(
    download_id: str,
    protocol: DownloadProtocol | None = None,
) -> str:
    """Compatibility wrapper around the shared protocol-aware normalizer."""
    return normalize_download_id(download_id, protocol)


def _lease_modifier(lease_seconds: float | None) -> str:
    duration = PACK_RESERVATION_SECONDS if lease_seconds is None else lease_seconds
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("lease_seconds must be positive")
    return f"+{duration:.6f} seconds"


def _pack_root() -> str:
    pipeline = importlib.import_module("import_pipeline")
    return str(getattr(pipeline, "PACK_STAGING_ROOT"))


def _canonical_pack_path(identity: DownloadIdentity) -> str:
    path_token = download_identity_path_token(identity)
    if not path_token:
        raise ValueError("download_id must be non-empty")
    owner = (
        f"client-{identity.download_client_id}"
        if identity.download_client_id is not None
        else "client-legacy"
    )
    protocol = identity.protocol or "unknown"
    return safe_join_under(
        _pack_root(),
        f"queue-{owner}-{protocol}-{path_token}",
    )


def pack_queue_creation_paths(
    download_id: str,
    owner_token: str,
    *,
    download_client_id: int | None,
    protocol: DownloadProtocol | None,
) -> tuple[str, str]:
    """Return canonical and owner-private paths for one queue reservation."""
    identity = _pack_identity(
        download_id,
        download_client_id=download_client_id,
        protocol=protocol,
    )
    canonical_path = _canonical_pack_path(identity)
    private_path = safe_join_under(
        _pack_root(),
        f"{os.path.basename(canonical_path)}.owner-{owner_token}",
    )
    return canonical_path, private_path


def _cleanup_tombstone_path(pack_path: str, owner_token: str) -> str:
    return safe_join_under(
        os.path.dirname(pack_path),
        f"{os.path.basename(pack_path)}.cleanup-{owner_token}",
    )


def _active_publication_exists(db: sqlite3.Connection, queue_id: int) -> bool:
    placeholders = ",".join("?" for _ in _ACTIVE_PUBLICATION_STATES)
    row = db.execute(
        f"""
        SELECT 1
        FROM import_publications
        WHERE queue_id=? AND state IN ({placeholders})
        LIMIT 1
        """,
        (queue_id, *_ACTIVE_PUBLICATION_STATES),
    ).fetchone()
    return row is not None


def _terminal_cleanup_eligible(
    db: sqlite3.Connection,
    *,
    queue_id: int,
    identity: DownloadIdentity,
) -> bool:
    row = db.execute(
        "SELECT status, lease_owner FROM import_queue WHERE id=?",
        (queue_id,),
    ).fetchone()
    if row is not None and (
        str(row["status"]) not in _TERMINAL_QUEUE_STATUSES
        or row["lease_owner"] is not None
    ):
        return False
    if _active_publication_exists(db, queue_id):
        return False

    placeholders = ",".join("?" for _ in _ACTIVE_PUBLICATION_STATES)
    siblings = db.execute(
        f"""
        SELECT sibling.download_id, sibling.download_client_id,
               sibling.download_protocol, sibling.torrent_url,
               sibling.series_id
        FROM import_queue AS sibling
        WHERE sibling.id != ?
          AND sibling.download_id IS NOT NULL
          AND (
              sibling.status IN ('pending','partial','importing')
              OR sibling.lease_owner IS NOT NULL
              OR EXISTS (
                  SELECT 1
                  FROM import_publications AS publication
                  WHERE publication.queue_id=sibling.id
                    AND publication.state IN ({placeholders})
              )
          )
        """,
        (queue_id, *_ACTIVE_PUBLICATION_STATES),
    ).fetchall()
    for sibling in siblings:
        sibling_owner = coerce_download_client_id(sibling["download_client_id"])
        sibling_protocol = normalize_download_protocol(
            sibling["download_protocol"]
        ) or resolve_download_protocol(
            db,
            download_client_id=sibling_owner,
            series_id=int(sibling["series_id"]),
            download_id=str(sibling["download_id"] or ""),
            source_url=str(sibling["torrent_url"] or ""),
            allow_client_configuration=False,
        )
        if download_identities_match(
            identity,
            DownloadIdentity(
                sibling_owner,
                sibling_protocol,
                str(sibling["download_id"] or ""),
            ),
        ):
            return False
    return True


def _reservation_conflicts(
    db: sqlite3.Connection,
    identity: DownloadIdentity,
    *,
    purpose: ReservationPurpose | None = None,
    live_only: bool = False,
) -> bool:
    """Return whether a journal row overlaps this conservative identity."""
    conditions: list[str] = []
    params: list[object] = []
    if purpose is not None:
        conditions.append("purpose=?")
        params.append(purpose)
    if live_only:
        conditions.append("expires_at > CURRENT_TIMESTAMP")
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    rows = db.execute(
        "SELECT download_client_id, protocol, download_id"
        " FROM import_pack_cleanup_reservations" + where,
        params,
    ).fetchall()
    return any(
        download_identities_match(
            identity,
            DownloadIdentity(
                coerce_download_client_id(row["download_client_id"]),
                normalize_download_protocol(row["protocol"]),
                str(row["download_id"] or ""),
            ),
        )
        for row in rows
    )


def reserve_pack_queue_creation(
    db: sqlite3.Connection,
    download_id: str,
    *,
    download_client_id: int | None,
    protocol: DownloadProtocol | None,
    lease_seconds: float | None = None,
    _guard: FileMutationGuard | None = None,
) -> str | None:
    try:
        with _borrow_pack_guard(_guard) as guard:
            guard.verify()
            if db.in_transaction:
                raise RuntimeError(
                    "pack queue reservation requires a clean DB connection"
                )
            level = int(db.execute("PRAGMA synchronous").fetchone()[0])
            db.execute("PRAGMA synchronous=FULL")
            try:
                return _reserve_pack_queue_creation_with_guard(
                    db,
                    download_id,
                    download_client_id=download_client_id,
                    protocol=protocol,
                    lease_seconds=lease_seconds,
                )
            finally:
                db.execute(f"PRAGMA synchronous={_SYNCHRONOUS_NAMES[level]}")
    except FileMutationBusy:
        return None


def _reserve_pack_queue_creation_with_guard(
    db: sqlite3.Connection,
    download_id: str,
    *,
    download_client_id: int | None,
    protocol: DownloadProtocol | None,
    lease_seconds: float | None,
) -> str | None:
    """Reserve an ownership-qualified ID before creating private artifacts."""
    identity = _pack_identity(
        download_id,
        download_client_id=download_client_id,
        protocol=protocol,
    )
    identity_key = download_identity_key(identity)
    normalized = normalize_download_id(download_id, identity.protocol)
    if not identity_key:
        return None
    if db.in_transaction:
        raise RuntimeError("pack queue reservation requires a clean DB connection")

    owner_token = secrets.token_urlsafe(24)
    canonical_path, private_path = pack_queue_creation_paths(
        download_id,
        owner_token,
        download_client_id=identity.download_client_id,
        protocol=identity.protocol,
    )
    try:
        db.execute("BEGIN IMMEDIATE")
        if _reservation_conflicts(db, identity):
            db.commit()
            return None
        cur = db.execute(
            """
            INSERT INTO import_pack_cleanup_reservations(
                download_identity_key, download_client_id, protocol,
                normalized_download_id, download_id, purpose, owner_token,
                queue_id, publication_id, pack_path, tombstone_path, expires_at
            ) VALUES(
                ?, ?, ?, ?, ?, 'queueing', ?, NULL, NULL, ?, ?,
                datetime('now', ?)
            )
            ON CONFLICT(download_identity_key) DO NOTHING
            """,
            (
                identity_key,
                identity.download_client_id,
                identity.protocol,
                normalized,
                download_id,
                owner_token,
                canonical_path,
                private_path,
                _lease_modifier(lease_seconds),
            ),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return owner_token if cur.rowcount == 1 else None


def refresh_pack_queue_creation(
    db: sqlite3.Connection,
    download_id: str,
    owner_token: str,
    *,
    download_client_id: int | None,
    protocol: DownloadProtocol | None,
    lease_seconds: float | None = None,
    commit: bool,
) -> bool:
    """Renew a live build/attach reservation, optionally committing it."""
    identity = _pack_identity(
        download_id,
        download_client_id=download_client_id,
        protocol=protocol,
    )
    identity_key = download_identity_key(identity)
    if not identity_key or not owner_token:
        return False
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
    cur = db.execute(
        """
        UPDATE import_pack_cleanup_reservations
        SET expires_at=datetime('now', ?), updated_at=CURRENT_TIMESTAMP
        WHERE download_identity_key=? AND owner_token=?
          AND expires_at > CURRENT_TIMESTAMP
          AND (
              purpose='queueing'
              OR (purpose='cleanup' AND queue_id IS NULL)
          )
        """,
        (
            _lease_modifier(lease_seconds),
            identity_key,
            owner_token,
        ),
    )
    if commit:
        db.commit()
    return cur.rowcount == 1


def begin_pack_queue_attachment(
    db: sqlite3.Connection,
    download_id: str,
    owner_token: str,
    *,
    download_client_id: int | None,
    protocol: DownloadProtocol | None,
    lease_seconds: float | None = None,
    _guard: FileMutationGuard | None = None,
) -> bool:
    try:
        with _borrow_pack_guard(_guard) as guard:
            guard.verify()
            return _begin_pack_queue_attachment_with_guard(
                db,
                download_id,
                owner_token,
                download_client_id=download_client_id,
                protocol=protocol,
                lease_seconds=lease_seconds,
            )
    except FileMutationBusy:
        return False


def _begin_pack_queue_attachment_with_guard(
    db: sqlite3.Connection,
    download_id: str,
    owner_token: str,
    *,
    download_client_id: int | None,
    protocol: DownloadProtocol | None,
    lease_seconds: float | None,
) -> bool:
    """Owner-CAS a live build reservation into the filesystem attach phase."""
    identity_key = download_identity_key(
        _pack_identity(
            download_id,
            download_client_id=download_client_id,
            protocol=protocol,
        )
    )
    if not identity_key or not owner_token:
        return False
    if db.in_transaction:
        raise RuntimeError("pack attachment CAS requires a clean DB connection")
    synchronous_row = cast(
        tuple[int] | None,
        db.execute("PRAGMA synchronous").fetchone(),
    )
    if synchronous_row is None:
        raise RuntimeError("could not read SQLite synchronous mode")
    synchronous_level = int(synchronous_row[0])
    synchronous_name = _SYNCHRONOUS_NAMES.get(synchronous_level)
    if synchronous_name is None:
        raise RuntimeError(f"unsupported SQLite synchronous mode: {synchronous_level}")
    try:
        # The filesystem rename is allowed only after this owner transition is
        # power-loss durable. The application's normal WAL setting is NORMAL,
        # so strengthen this one pre-rename transaction explicitly.
        _ = db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        cur = db.execute(
            """
            UPDATE import_pack_cleanup_reservations
            SET purpose='cleanup', queue_id=NULL,
                expires_at=datetime('now', ?), updated_at=CURRENT_TIMESTAMP
            WHERE download_identity_key=? AND owner_token=?
              AND purpose='queueing' AND expires_at > CURRENT_TIMESTAMP
            """,
            (
                _lease_modifier(lease_seconds),
                identity_key,
                owner_token,
            ),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        _ = db.execute(f"PRAGMA synchronous={synchronous_name}")
    return cur.rowcount == 1


def release_pack_queue_creation(
    db: sqlite3.Connection,
    download_id: str,
    owner_token: str,
    *,
    download_client_id: int | None,
    protocol: DownloadProtocol | None,
    commit: bool,
    attaching: bool = False,
    _guard: FileMutationGuard | None = None,
) -> bool:
    try:
        with _borrow_pack_guard(_guard) as guard:
            guard.verify()
            return _release_pack_queue_creation_with_guard(
                db,
                download_id,
                owner_token,
                download_client_id=download_client_id,
                protocol=protocol,
                commit=commit,
                attaching=attaching,
            )
    except FileMutationBusy:
        return False


def _release_pack_queue_creation_with_guard(
    db: sqlite3.Connection,
    download_id: str,
    owner_token: str,
    *,
    download_client_id: int | None,
    protocol: DownloadProtocol | None,
    commit: bool,
    attaching: bool,
) -> bool:
    """Release only the caller's build or completed attach reservation."""
    identity_key = download_identity_key(
        _pack_identity(
            download_id,
            download_client_id=download_client_id,
            protocol=protocol,
        )
    )
    if not identity_key or not owner_token:
        return False
    row = db.execute(
        "SELECT directory_ownership_json FROM import_pack_cleanup_reservations"
        " WHERE download_identity_key=? AND owner_token=?",
        (identity_key, owner_token),
    ).fetchone()
    if row is not None and row[0] is not None:
        ownership = PackOwnership.from_json(str(row[0]))
        if (
            ownership.private_directory is not None
            or ownership.canonical_directory is not None
            or ownership.claims
            or ownership.retained_reason == "creating-private"
        ):
            return False
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
    cur = db.execute(
        """
        DELETE FROM import_pack_cleanup_reservations
        WHERE download_identity_key=? AND owner_token=?
          AND (
              purpose='queueing'
              OR (? AND purpose='cleanup' AND queue_id IS NULL)
          )
        """,
        (identity_key, owner_token, int(attaching)),
    )
    if commit:
        db.commit()
    return cur.rowcount == 1


def cleanup_reservation_blocks(
    db: sqlite3.Connection,
    download_id: str,
    *,
    download_client_id: int | None,
    protocol: DownloadProtocol | None,
) -> bool:
    """Return whether a live cleanup/attach reservation blocks new work."""
    identity = _pack_identity(
        download_id,
        download_client_id=download_client_id,
        protocol=protocol,
    )
    if not download_identity_key(identity):
        return False
    return _reservation_conflicts(
        db,
        identity,
        purpose="cleanup",
        live_only=False,
    )


def _rename_noreplace(source: str, destination: str) -> None:
    """Atomically rename a directory without replacing an existing path."""
    if os.name != "posix":
        raise OSError(
            errno.ENOSYS,
            "atomic no-replace rename requires Linux renameat2",
        )
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError as exc:
        raise OSError(
            errno.ENOSYS,
            "C library does not expose Linux renameat2",
        ) from exc
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
            destination,
        )


def _fsync_directory(path: str) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_pack_root(pack_path: str) -> None:
    root = os.path.dirname(pack_path)
    try:
        _fsync_directory(root)
    except FileNotFoundError:
        return


def _fsync_tree(
    root: str,
    *,
    checkpoint: FilesystemCheckpoint | None = None,
) -> None:
    """Durably flush a generated tree before publishing its directory entry."""
    directories: list[str] = []
    for current_root, dirs, files in os.walk(root, topdown=True, followlinks=False):
        if checkpoint is not None:
            checkpoint()
        dirs.sort()
        files.sort()
        directories.append(current_root)
        for name in dirs:
            child = os.path.join(current_root, name)
            if stat.S_ISLNK(os.lstat(child).st_mode):
                raise OSError(f"generated pack contains symlink directory: {child}")
        for name in files:
            if checkpoint is not None:
                checkpoint()
            path = os.path.join(current_root, name)
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode):
                raise OSError(f"generated pack contains non-file artifact: {path}")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    for directory in reversed(directories):
        if checkpoint is not None:
            checkpoint()
        _fsync_directory(directory)


def _pack_owner_marker_payload(
    identity: DownloadIdentity,
    owner_token: str,
) -> bytes:
    if not owner_token:
        raise ValueError("owner_token must be non-empty")
    identity_key = download_identity_key(identity)
    if not identity_key:
        raise ValueError("download identity must be non-empty")
    digest = hashlib.sha256(
        f"{identity_key}\0{owner_token}".encode("utf-8")
    ).hexdigest()
    return f"mangarr-pack-owner-v1:{digest}\n".encode()


def _pack_tree_has_owner_marker(
    tree_path: str,
    identity: DownloadIdentity,
    owner_token: str,
) -> bool:
    marker_path = safe_join_under(tree_path, _PACK_OWNER_MARKER_NAME)
    expected = _pack_owner_marker_payload(identity, owner_token)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(marker_path, flags)
    except OSError:
        return False
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size != len(expected):
            return False
        payload = bytearray()
        while len(payload) <= len(expected):
            chunk = os.read(descriptor, len(expected) + 1 - len(payload))
            if not chunk:
                break
            payload.extend(chunk)
        return bytes(payload) == expected
    finally:
        os.close(descriptor)


def _write_pack_owner_marker(
    private_path: str,
    identity: DownloadIdentity,
    owner_token: str,
) -> None:
    marker_path = safe_join_under(private_path, _PACK_OWNER_MARKER_NAME)
    payload = _pack_owner_marker_payload(identity, owner_token)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(marker_path, flags, 0o600)
    except FileExistsError:
        if _pack_tree_has_owner_marker(private_path, identity, owner_token):
            return
        raise OSError(
            errno.EEXIST,
            "generated pack owner marker is already occupied",
            marker_path,
        ) from None
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written == 0:
                raise OSError(errno.EIO, "short owner marker write", marker_path)
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _legacy_pack_directory(guard: FileMutationGuard, path: str) -> Iterator[int]:
    """First ownership evidence requires present exclusive entry control."""
    parent_path = os.path.dirname(path)
    with open_directory(parent_path) as parent:
        before = os.fstat(parent)

        def verify_parent() -> None:
            guard.verify()
            held, current = os.fstat(parent), os.lstat(parent_path)
            if (
                not stat.S_ISDIR(current.st_mode)
                or (held.st_dev, held.st_ino) != (before.st_dev, before.st_ino)
                or (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino)
                or held.st_uid != os.geteuid()
                or stat.S_IMODE(held.st_mode) & 0o022
            ):
                raise PackProofError("legacy first proof requires exclusive parent")

        verify_parent()
        with open_directory(path) as source:
            held = os.fstat(source)
            if held.st_uid != os.geteuid() or stat.S_IMODE(held.st_mode) & 0o022:
                raise PackProofError("legacy first proof requires exclusive directory")
            yield source
            verify_parent()


def durably_attach_pack_queue_directory(
    download_id: str,
    owner_token: str,
    *,
    download_client_id: int | None,
    protocol: DownloadProtocol | None,
    checkpoint: FilesystemCheckpoint | None = None,
    _guard: FileMutationGuard | None = None,
) -> str:
    """Flush and no-replace attach an owner-private tree to its canonical path."""
    identity = _pack_identity(
        download_id,
        download_client_id=download_client_id,
        protocol=protocol,
    )
    canonical_path, private_path = pack_queue_creation_paths(
        download_id,
        owner_token,
        download_client_id=download_client_id,
        protocol=protocol,
    )
    with _borrow_pack_guard(_guard) as guard:
        reservation = _pack_reservation_for_owner(
            download_id,
            owner_token,
            download_client_id=download_client_id,
            protocol=protocol,
        )
        ownership = _ownership(reservation)
        if ownership is not None and ownership.private_directory is not None:
            private_path = ownership.private_directory.path
        if os.path.lexists(canonical_path):
            raise FileExistsError(
                errno.EEXIST, "canonical pack path is occupied", canonical_path
            )
        source_context = (
            _legacy_pack_directory(guard, private_path)
            if ownership is None
            else open_directory(private_path)
        )
        with source_context as source:
            if ownership is None:
                # Current exclusion permits a first proof, not a birth receipt.
                _write_pack_owner_marker(
                    f"/proc/self/fd/{source}", identity, owner_token
                )
                ownership = PackOwnership(
                    reservation.download_identity_key,
                    owner_token,
                    private_directory=directory_proof(private_path, source),
                )
            proof = ownership.private_directory
            if proof is None:
                raise PackProofError("attachment lacks private directory proof")
            verify_directory(source, proof)
            inventory = inventory_tree(source, checkpoint=checkpoint, flush=True)
            ownership = replace(
                ownership,
                phase="attaching",
                inventory=inventory,
                canonical_directory=replace(proof, path=canonical_path),
            )
            reservation = _save_ownership(guard, reservation, ownership)
            if checkpoint is not None:
                checkpoint()
            verify_directory(source, proof)
            guard.verify()
            try:
                _rename_noreplace(private_path, canonical_path)
            except OSError as exc:
                if exc.errno not in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
                    raise
                if os.path.lexists(canonical_path):
                    raise FileExistsError(
                        errno.EEXIST, "canonical pack path is occupied", canonical_path
                    )
                # mkdir returns no inode capability. Never bless its first public
                # observation; keep the payload in the already-proven private carrier.
                if ownership.placement_carrier is None:
                    reservation = _capture_pack_directory(
                        guard, reservation, proof, inventory, queued_private=True
                    )
                    captured = _ownership(reservation)
                    if captured is None or not captured.claims:
                        raise PackProofError("legacy private placement capture missing")
                    claim = captured.claims[-1]
                    proof = replace(
                        proof, path=os.path.join(claim.carrier.carrier_path, "artifact")
                    )
                    ownership = replace(
                        captured,
                        version=2,
                        placement_carrier=claim.carrier,
                        claims=captured.claims[:-1],
                    )
                canonical_proof = proof
                verify_directory(source, canonical_proof)
                verify_inventory(source, inventory)
                inventory_tree(source, flush=True, checkpoint=None)
                ownership = replace(
                    ownership,
                    private_directory=None,
                    canonical_directory=canonical_proof,
                )
            else:
                canonical_proof = replace(proof, path=canonical_path)
                verify_directory(source, canonical_proof)
                verify_inventory(source, inventory)
                _fsync_pack_root(canonical_path)
                ownership = replace(
                    ownership,
                    private_directory=None,
                    canonical_directory=canonical_proof,
                )
            ownership = replace(ownership, phase="ready")
            _save_ownership(guard, reservation, ownership)
    return canonical_proof.path


def _link_pack_tree(
    guard: FileMutationGuard,
    source: int,
    destination: int,
    source_proof: DirectoryProof,
    destination_proof: DirectoryProof,
    inventory: PackInventory,
    checkpoint: FilesystemCheckpoint | None,
) -> None:
    def walk(src: int, dst: int, prefix: str) -> None:
        for name in sorted(os.listdir(src)):
            if not prefix and name == OWNER_MARKER:
                continue
            guard.verify()
            verify_directory(source, source_proof)
            verify_directory(destination, destination_proof)
            if checkpoint is not None:
                checkpoint()
            relative = prefix + name
            if relative in inventory.directories:
                os.mkdir(name, 0o700, dir_fd=dst)
                child_src = os.open(name, DIR_FLAGS, dir_fd=src)
                child_dst = os.open(name, DIR_FLAGS, dir_fd=dst)
                try:
                    walk(child_src, child_dst, relative + "/")
                finally:
                    os.close(child_src)
                    os.close(child_dst)
            elif relative in inventory.files:
                os.link(
                    name, name, src_dir_fd=src, dst_dir_fd=dst, follow_symlinks=False
                )
            else:
                raise PackProofError("pack source inventory changed")

    walk(source, destination, "")
    verify_inventory(source, inventory)


class _PackQueueDecisionUnresolved(RuntimeError):
    """A queue decision could not be resolved; do not reset or compensate it."""


def _queue_rows(
    db: sqlite3.Connection, queue_id: int
) -> tuple[dict[str, object], list[dict[str, object]]]:
    queue = db.execute("SELECT * FROM import_queue WHERE id=?", (queue_id,)).fetchone()
    files = db.execute(
        "SELECT * FROM import_queue_files WHERE queue_id=? ORDER BY id", (queue_id,)
    ).fetchall()
    if queue is None or not files:
        raise PackProofError("provisional queue is not complete")
    return dict(queue), [dict(row) for row in files]


def _queue_receipt(db: sqlite3.Connection, queue_id: int) -> str:
    queue, files = _queue_rows(db, queue_id)
    encoded = json.dumps(
        {"queue": queue, "files": files},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sql_reservation_matches(
    db: sqlite3.Connection, expected: _PackReservation
) -> bool:
    row = db.execute(
        "SELECT * FROM import_pack_cleanup_reservations WHERE download_identity_key=?",
        (expected.download_identity_key,),
    ).fetchone()
    return row is not None and _reservation_from_row(dict(row)) == expected


def _sql_queue_authority(
    db: sqlite3.Connection,
    reservation: _PackReservation,
    ownership: PackOwnership,
    *,
    promoted: bool = False,
) -> None:
    """SQL-only exact pending-row authority; safe inside a short writer."""
    from import_queue import _has_terminal_download_receipt

    queue_id = reservation.queue_id
    if (
        queue_id is None
        or reservation.publication_id is not None
        or reservation.purpose != ("queueing" if promoted else "cleanup")
        or ownership.phase != ("attached" if promoted else "queued")
        or not _sql_reservation_matches(db, reservation)
    ):
        raise PackProofError("provisional pack reservation changed")
    queue, _ = _queue_rows(db, queue_id)
    identity = DownloadIdentity(
        reservation.download_client_id, reservation.protocol, reservation.download_id
    )
    if (
        queue["status"] != "pending"
        or any(
            queue[key] is not None
            for key in ("lease_owner", "lease_expires_at", "failed_at")
        )
        or coerce_download_client_id(queue["download_client_id"])
        != reservation.download_client_id
        or normalize_download_protocol(queue["download_protocol"])
        != reservation.protocol
        or normalize_download_id(str(queue["download_id"] or ""), reservation.protocol)
        != reservation.normalized_download_id
        or db.execute(
            "SELECT 1 FROM import_publications WHERE queue_id=? LIMIT 1", (queue_id,)
        ).fetchone()
        is not None
        or _has_terminal_download_receipt(
            db,
            series_id=cast(int, queue["series_id"]),
            torrent_url=cast(str, queue["torrent_url"]),
            identity=identity,
        )
    ):
        raise PackProofError(
            "provisional queue has changed execution or domain authority"
        )
    if (
        ownership.version == 3
        and _queue_receipt(db, queue_id) != ownership.queue_receipt_sha256
    ):
        raise PackProofError("provisional queue row receipt changed")


@contextmanager
def _open_generated_queue_directory(
    guard: FileMutationGuard, reservation: _PackReservation, ownership: PackOwnership
) -> Iterator[Callable[[], None]]:
    proof, inventory = ownership.canonical_directory, ownership.inventory
    if proof is None or inventory is None:
        raise PackProofError("generated queue directory proof missing")
    with ExitStack() as stack:
        placement = ownership.placement_carrier
        carrier_verify: Callable[[], None] | None = None
        if placement is not None and proof.path == os.path.join(
            placement.carrier_path, "artifact"
        ):
            parent = os.path.dirname(placement.origin_path)
            with get_db() as db:
                row = db.execute(
                    "SELECT ownership_json FROM file_claim_namespaces WHERE parent_path=?",
                    (parent,),
                ).fetchone()
            if row is None or row[0] is None:
                raise PackProofError("generated queue namespace proof missing")
            namespace = stack.enter_context(open_namespace(guard, parent, str(row[0])))
            handle = stack.enter_context(
                open_carrier(namespace, placement.binding, placement)
            )
            carrier_verify = handle.verify
            fd = os.open("artifact", DIR_FLAGS, dir_fd=handle.fd)
            stack.callback(os.close, fd)
        else:
            fd = stack.enter_context(open_directory(proof.path))

        def verify() -> None:
            guard.verify()
            if carrier_verify is not None:
                carrier_verify()
            verify_directory(fd, proof)
            verify_inventory(fd, inventory)
            if proof.path != reservation.pack_path and os.path.lexists(
                reservation.pack_path
            ):
                raise PackProofError(
                    "logical canonical became occupied before queue decision"
                )

        verify()
        yield verify


def _cancel_generated_pack_queue(
    reservation: _PackReservation, ownership: PackOwnership
) -> bool:
    """Cancel only the exact new provisional receipt; never mutate artifacts."""
    if ownership.version != 3 or reservation.queue_id is None:
        return False
    retained = replace(
        ownership, phase="retained", retained_reason="queue validation failed"
    )
    cancelled = replace(
        reservation, queue_id=None, directory_ownership_json=retained.to_json()
    )
    try:
        with get_db() as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            _sql_queue_authority(db, reservation, ownership)
            db.execute(
                "DELETE FROM import_queue_files WHERE queue_id=?",
                (reservation.queue_id,),
            )
            db.execute("DELETE FROM import_queue WHERE id=?", (reservation.queue_id,))
            cur = db.execute(
                "UPDATE import_pack_cleanup_reservations SET queue_id=NULL,directory_ownership_json=?"
                " WHERE download_identity_key=? AND owner_token=? AND queue_id=? AND directory_ownership_json=?",
                (
                    cancelled.directory_ownership_json,
                    reservation.download_identity_key,
                    reservation.owner_token,
                    reservation.queue_id,
                    reservation.directory_ownership_json,
                ),
            )
            if cur.rowcount != 1:
                raise PackProofError("queue cancellation authority changed")
            db.commit()
        return True
    except PackProofError:
        return False
    except BaseException as exc:
        try:
            with get_db() as db:
                if (
                    _sql_reservation_matches(db, cancelled)
                    and db.execute(
                        "SELECT 1 FROM import_queue WHERE id=?", (reservation.queue_id,)
                    ).fetchone()
                    is None
                    and db.execute(
                        "SELECT 1 FROM import_queue_files WHERE queue_id=?",
                        (reservation.queue_id,),
                    ).fetchone()
                    is None
                ):
                    return True
        except (sqlite3.Error, PackProofError):
            pass
        raise _PackQueueDecisionUnresolved(
            "queue cancellation outcome is unresolved"
        ) from exc


def _known_fenced_generated_queue(reservation: _PackReservation, queue_id: int) -> bool:
    try:
        current = _read_reservation(reservation.download_identity_key)
        if (
            current is None
            or current.owner_token != reservation.owner_token
            or current.queue_id != queue_id
        ):
            return False
        ownership = _ownership(current)
        if (
            ownership is None
            or current.purpose != "cleanup"
            or ownership.phase != "queued"
        ):
            return False
        with get_db() as db:
            _sql_queue_authority(db, current, ownership)
        return True
    except (sqlite3.Error, PackProofError):
        return False


def _commit_generated_pack_queue(
    guard: FileMutationGuard,
    reservation: _PackReservation,
    values: tuple[object, ...],
    file_rows: list[tuple[object, ...]],
    *,
    respect_grab_claims: bool | None = None,
) -> int:
    ownership = _ownership(reservation)
    if (
        ownership is None
        or ownership.phase != "ready"
        or ownership.canonical_directory is None
        or ownership.inventory is None
    ):
        raise PackProofError("pack is not durably ready for queue commit")
    guard.verify()
    queue_id: int | None = None
    decision: _PackReservation | None = None
    with _open_generated_queue_directory(guard, reservation, ownership) as verify:
        try:
            with get_db() as db:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                if (
                    reservation.purpose != "cleanup"
                    or reservation.queue_id is not None
                    or reservation.publication_id is not None
                    or not _sql_reservation_matches(db, reservation)
                ):
                    raise PackProofError("pack final commit authority lost")
                queue_policy = (
                    int(respect_grab_claims)
                    if respect_grab_claims is not None
                    else int(
                        acquisition_policy(
                            db,
                            series_id=cast(int, values[0]),
                            source_url=cast(str, values[5]),
                            identity=DownloadIdentity(
                                reservation.download_client_id,
                                reservation.protocol,
                                reservation.download_id,
                            ),
                        )
                        != 0
                    )
                )
                cur = db.execute(
                    "INSERT INTO import_queue(series_id, download_id, download_client_id,"
                    " download_protocol, torrent_name, torrent_url, volume_num, src_dir,"
                    " respect_grab_claims,status) VALUES(?,?,?,?,?,?,?,?,?,'pending')",
                    (*values, queue_policy),
                )
                queue_id = cur.lastrowid
                if queue_id is None:
                    raise RuntimeError("queue insert did not return an identity")
                db.executemany(
                    "INSERT INTO import_queue_files(queue_id, filename, src_path, dst_path,"
                    " proposed_volume, proposed_chapter, proposed_volume_range_start,"
                    " proposed_volume_range_end, proposed_chapter_range_end, proposed_pack_type,"
                    " proposed_is_special, proposed_import_kind, proposed_special_title, file_type,status)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(queue_id, *row) for row in file_rows],
                )
                ownership = replace(
                    ownership,
                    phase="queued",
                    version=3,
                    queue_receipt_sha256=_queue_receipt(db, queue_id),
                )
                decision = replace(
                    reservation,
                    queue_id=queue_id,
                    directory_ownership_json=ownership.to_json(),
                )
                cur = db.execute(
                    "UPDATE import_pack_cleanup_reservations SET queue_id=?,directory_ownership_json=?"
                    " WHERE download_identity_key=? AND owner_token=? AND queue_id IS NULL"
                    " AND purpose='cleanup' AND directory_ownership_json=?",
                    (
                        queue_id,
                        decision.directory_ownership_json,
                        reservation.download_identity_key,
                        reservation.owner_token,
                        reservation.directory_ownership_json,
                    ),
                )
                if cur.rowcount != 1:
                    raise PackProofError("pack queue link authority lost")
                db.commit()
        except BaseException:
            # Settle the writer first. A persisted B receipt still requires C.
            if decision is None:
                raise
            with get_db() as db:
                if not _sql_reservation_matches(db, decision):
                    raise
                _sql_queue_authority(db, decision, ownership)
        if decision is None or queue_id is None:
            raise _PackQueueDecisionUnresolved("complete queue receipt missing")
        try:
            verify()
        except (OSError, PrivateClaimError):
            _cancel_generated_pack_queue(decision, ownership)
            raise
        with get_db() as db:
            _sql_queue_authority(db, decision, ownership)
        return queue_id


def _finish_generated_pack_queue(
    guard: FileMutationGuard, reservation: _PackReservation, queue_id: int
) -> None:
    current = _read_reservation(reservation.download_identity_key)
    if (
        current is None
        or current.owner_token != reservation.owner_token
        or current.queue_id != queue_id
    ):
        raise PackProofError("queued pack authority changed")
    ownership = _ownership(current)
    if ownership is None or ownership.phase != "queued":
        raise PackProofError("queued pack decision proof missing")
    with get_db() as db:
        _sql_queue_authority(db, current, ownership)
    if (
        ownership.placement_carrier is not None
        and ownership.canonical_directory is not None
    ):
        physical = os.path.join(ownership.placement_carrier.carrier_path, "artifact")
        if ownership.canonical_directory.path != physical:
            current = _discard_pack_placement(guard, current, empty_only=True)
            ownership = _ownership(current)
            if ownership is None:
                raise PackProofError("native placement cleanup proof missing")
    if ownership.private_directory is not None:
        if not ownership.claims:
            current = _capture_pack_directory(
                guard,
                current,
                ownership.private_directory,
                ownership.inventory,
                queued_private=True,
            )
        if not _discard_reservation_claims(guard, current):
            return
        current = _read_reservation(current.download_identity_key)
        if current is None:
            raise PackProofError("queued pack cleanup journal missing")
        ownership = _ownership(current)
        if ownership is None:
            raise PackProofError("queued pack cleanup proof missing")
    guard.verify()
    attached = replace(ownership, phase="attached", private_directory=None, claims=())
    promoted = replace(
        current,
        purpose="queueing",
        tombstone_path=None,
        directory_ownership_json=attached.to_json(),
    )
    receipt: str | None = None
    attempted = False
    try:
        with get_db() as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            _sql_queue_authority(db, current, ownership)
            receipt = _queue_receipt(db, queue_id)
            cur = db.execute(
                "UPDATE import_pack_cleanup_reservations SET purpose='queueing',directory_ownership_json=?,"
                " tombstone_path=NULL WHERE download_identity_key=? AND owner_token=? AND queue_id=?"
                " AND purpose='cleanup' AND directory_ownership_json=?",
                (
                    promoted.directory_ownership_json,
                    current.download_identity_key,
                    current.owner_token,
                    queue_id,
                    current.directory_ownership_json,
                ),
            )
            if cur.rowcount != 1:
                raise PackProofError("pack promotion authority lost")
            attempted = True
            db.commit()
    except BaseException as exc:
        if not attempted:
            raise
        fenced = False
        try:
            with get_db() as db:
                if _sql_reservation_matches(db, promoted):
                    _sql_queue_authority(db, promoted, attached, promoted=True)
                    if _queue_receipt(db, queue_id) == receipt:
                        return
                elif _sql_reservation_matches(db, current):
                    _sql_queue_authority(db, current, ownership)
                    if _queue_receipt(db, queue_id) == receipt:
                        fenced = True
        except (sqlite3.Error, PackProofError) as read_error:
            raise _PackQueueDecisionUnresolved(
                "pack promotion outcome is unreadable or mismatched"
            ) from read_error
        if fenced:
            raise PackProofError(
                "pack promotion did not commit; exact queue remains fenced"
            ) from exc
        raise _PackQueueDecisionUnresolved(
            "pack promotion outcome does not match its receipt"
        ) from exc


def _committed_missing(
    reservation: _PackReservation, inventory: PackInventory
) -> frozenset[str]:
    if reservation.queue_id is None:
        return frozenset()
    with get_db() as db:
        rows = db.execute(
            "SELECT f.src_path, f.source_dev, f.source_inode, f.source_size,"
            " f.source_mtime_ns, f.source_sha256 FROM import_publication_files f"
            " JOIN import_publications p ON p.id=f.publication_id"
            " WHERE p.queue_id=? AND p.state IN ('db_committed','cleaning','finalized','deleted')"
            " AND f.cleanup_state IN ('deleted','missing')",
            (reservation.queue_id,),
        ).fetchall()
    allowed: set[str] = set()
    ownership = _ownership(reservation)
    base = (
        ownership.canonical_directory.path
        if ownership is not None and ownership.canonical_directory is not None
        else reservation.pack_path
    )
    for row in rows:
        source = os.path.abspath(str(row[0]))
        if os.path.commonpath((source, base)) != base:
            continue
        relative = os.path.relpath(source, base)
        proof = inventory.files.get(relative)
        if proof is not None and tuple(row[1:]) == (
            proof.dev,
            proof.inode,
            proof.size,
            proof.mtime_ns,
            proof.sha256,
        ):
            allowed.add(relative)
    return frozenset(allowed)


def _discard_pack_placement(
    guard: FileMutationGuard,
    reservation: _PackReservation,
    *,
    empty_only: bool = False,
) -> _PackReservation:
    ownership = _ownership(reservation)
    if ownership is None or ownership.placement_carrier is None:
        raise PackProofError("pack placement proof missing")
    record = ownership.placement_carrier
    with ensure_namespace(guard, os.path.dirname(record.origin_path)) as namespace:
        if record.phase != "discarded":
            with open_carrier(namespace, record.binding, record) as carrier:
                try:
                    fd = os.open("artifact", DIR_FLAGS, dir_fd=carrier.fd)
                except FileNotFoundError:
                    if not empty_only and record.phase not in (
                        "allocated",
                        "discarding",
                    ):
                        raise PackProofError(
                            "live pack placement unexpectedly missing"
                        ) from None
                    fd = None
                if fd is not None:
                    try:
                        if empty_only:
                            raise PackProofError(
                                "native move left an unexplained placement artifact"
                            )
                        proof = next(
                            (
                                p
                                for p in (
                                    ownership.canonical_directory,
                                    ownership.private_directory,
                                )
                                if p is not None and p.path == carrier.artifact_path
                            ),
                            None,
                        )
                        if proof is None:
                            raise PackProofError(
                                "placement creation lacks directory proof"
                            )
                        verify_directory(
                            fd, proof, allow_missing_marker=record.phase == "discarding"
                        )
                        inventory = ownership.inventory or inventory_tree(fd)
                        verify_inventory(
                            fd,
                            inventory,
                            allowed_missing=_committed_missing(reservation, inventory),
                            discarding=record.phase == "discarding",
                        )
                        ownership = replace(ownership, inventory=inventory)
                    finally:
                        os.close(fd)
                    record = replace(record, phase="discarding")
                    ownership = replace(ownership, placement_carrier=record)
                    reservation = _save_ownership(guard, reservation, ownership)
                    carrier.record = record
                    carrier.verify()
                    if not _FD_SAFE_RMTREE:
                        raise PackProofError("fd-safe directory removal is unavailable")
                    shutil.rmtree("artifact", dir_fd=carrier.fd)
                os.fsync(carrier.fd)
                carrier.verify()
                record = replace(record, phase="discarded")
                ownership = replace(ownership, placement_carrier=record)
                reservation = _save_ownership(guard, reservation, ownership)
        gc_discarded_carrier(namespace, record.binding, record)
    if empty_only:
        ownership = replace(ownership, placement_carrier=None, private_directory=None)
        reservation = _save_ownership(guard, reservation, ownership)
    return reservation


def _capture_pack_directory(
    guard: FileMutationGuard,
    reservation: _PackReservation,
    proof: DirectoryProof,
    inventory: PackInventory | None,
    *,
    queued_private: bool = False,
    allow_partial: bool = False,
) -> _PackReservation:
    guard.verify()
    ownership = _ownership(reservation)
    source_context = (
        _legacy_pack_directory(guard, proof.path)
        if ownership is None
        else open_directory(proof.path)
    )
    with source_context as source:
        verify_directory(source, proof)
        if inventory is None:
            inventory = inventory_tree(source)
        verify_inventory(
            source,
            inventory,
            allowed_missing=_committed_missing(reservation, inventory),
            partial_attachment=allow_partial,
        )
        if allow_partial:
            inventory = inventory_tree(source)
    if ownership is None:
        ownership = PackOwnership(
            reservation.download_identity_key,
            reservation.artifact_owner_token,
            phase="cleaning",
        )
    binding = ClaimBinding("pack", ownership.artifact_owner_token, None, "pack_cleanup")
    with ensure_namespace(guard, os.path.dirname(proof.path)) as namespace:
        with allocate_carrier(namespace, binding, proof.path) as carrier:
            claim = PackDirectoryClaim(
                carrier.record,
                proof,
                inventory,
                ownership.artifact_owner_token
                if proof.marker_fingerprint is not None
                else None,
            )
            ownership = replace(
                ownership,
                claims=(*ownership.claims, claim),
                phase=ownership.phase if queued_private else "cleaning",
            )
            reservation = _save_ownership(
                guard, reservation, ownership, tombstone_path=carrier.artifact_path
            )
            claim = replace(claim, carrier=replace(claim.carrier, phase="claiming"))
            ownership = replace(ownership, claims=(*ownership.claims[:-1], claim))
            reservation = _save_ownership(guard, reservation, ownership)
            carrier.record = claim.carrier
            guard.verify()
            try:
                outcome = _detach_directory(proof.path, carrier.artifact_path)
            except OSError as exc:
                if exc.errno not in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
                    raise
                claim_into_empty(
                    guard, carrier, namespace.parent_fd, os.path.basename(proof.path)
                )
            else:
                if outcome != "detached":
                    raise PackProofError("private directory capture did not complete")
                os.fsync(carrier.fd)
                _fsync_pack_root(proof.path)
                carrier.verify()
            artifact = os.open("artifact", DIR_FLAGS, dir_fd=carrier.fd)
            try:
                verify_directory(artifact, proof, path_binding=False)
                verify_inventory(
                    artifact,
                    inventory,
                    allowed_missing=_committed_missing(reservation, inventory),
                )
            finally:
                os.close(artifact)
            claim = replace(claim, carrier=replace(claim.carrier, phase="claimed"))
            ownership = replace(ownership, claims=(*ownership.claims[:-1], claim))
            return _save_ownership(guard, reservation, ownership)


def _discard_pack_claim(
    guard: FileMutationGuard,
    claim: PackDirectoryClaim,
    persist: Callable[[PackDirectoryClaim], None],
    *,
    allowed_missing: frozenset[str] = frozenset(),
) -> PackDirectoryClaim:
    binding = claim.carrier.binding
    with ensure_namespace(
        guard, os.path.dirname(claim.carrier.origin_path)
    ) as namespace:
        if claim.carrier.phase == "discarded":
            gc_discarded_carrier(namespace, binding, claim.carrier)
            return claim
        with open_carrier(namespace, binding, claim.carrier) as carrier:
            phase = claim.carrier.phase
            if phase == "allocated":
                try:
                    os.stat("artifact", dir_fd=carrier.fd, follow_symlinks=False)
                except FileNotFoundError:
                    claim = replace(
                        claim, carrier=replace(claim.carrier, phase="claiming")
                    )
                    persist(claim)
                    carrier.record = claim.carrier
                    phase = "claiming"
                else:
                    raise PackProofError(
                        "allocated directory carrier has an unexplained artifact"
                    )
            if phase not in ("claiming", "claimed", "discarding"):
                raise PackProofError("directory claim lacks capture intent")
            try:
                artifact = os.open("artifact", DIR_FLAGS, dir_fd=carrier.fd)
            except FileNotFoundError:
                if phase == "claiming":
                    with open_directory(claim.source_directory.path) as source:
                        verify_directory(source, claim.source_directory)
                        verify_inventory(
                            source, claim.inventory, allowed_missing=allowed_missing
                        )
                    claim_into_empty(
                        guard,
                        carrier,
                        namespace.parent_fd,
                        os.path.basename(claim.carrier.origin_path),
                    )
                    artifact = os.open("artifact", DIR_FLAGS, dir_fd=carrier.fd)
                elif phase != "discarding":
                    raise PackProofError(
                        "directory claim unexpectedly missing"
                    ) from None
                else:
                    artifact = None
            if artifact is not None:
                try:
                    verify_directory(
                        artifact,
                        claim.source_directory,
                        path_binding=False,
                        allow_missing_marker=phase == "discarding",
                    )
                    verify_inventory(
                        artifact,
                        claim.inventory,
                        allowed_missing=allowed_missing,
                        discarding=phase == "discarding",
                    )
                finally:
                    os.close(artifact)
                if phase == "claiming":
                    claim = replace(
                        claim, carrier=replace(claim.carrier, phase="claimed")
                    )
                    persist(claim)
                    carrier.record = claim.carrier
                claim = replace(
                    claim, carrier=replace(claim.carrier, phase="discarding")
                )
                persist(claim)
                carrier.record = claim.carrier
                carrier.verify()
                guard.verify()
                if not _FD_SAFE_RMTREE:
                    raise PackProofError("fd-safe directory removal is unavailable")
                shutil.rmtree("artifact", dir_fd=carrier.fd)
            os.fsync(carrier.fd)
            carrier.verify()
            claim = replace(claim, carrier=replace(claim.carrier, phase="discarded"))
            persist(claim)
        gc_discarded_carrier(namespace, binding, claim.carrier)
    return claim


def _discard_reservation_claims(
    guard: FileMutationGuard, reservation: _PackReservation
) -> bool:
    ownership = _ownership(reservation)
    if ownership is None:
        return False
    for position, claim in enumerate(ownership.claims):

        def persist(updated: PackDirectoryClaim) -> None:
            nonlocal ownership, reservation
            if ownership is None:
                raise PackProofError("missing pack cleanup proof")
            claims = list(ownership.claims)
            claims[position] = updated
            ownership = replace(ownership, claims=tuple(claims))
            reservation = _save_ownership(guard, reservation, ownership)

        _discard_pack_claim(
            guard,
            claim,
            persist,
            allowed_missing=_committed_missing(reservation, claim.inventory),
        )
    ownership = replace(ownership, claims=(), private_directory=None)
    _save_ownership(guard, reservation, ownership)
    return True


def remove_pack_queue_private_artifacts(
    download_id: str,
    owner_token: str,
    *,
    download_client_id: int | None,
    protocol: DownloadProtocol | None,
    _guard: FileMutationGuard | None = None,
) -> None:
    """Remove only the live executor's proven unpublished directory via carrier."""
    try:
        with _borrow_pack_guard(_guard) as guard:
            reservation = _pack_reservation_for_owner(
                download_id,
                owner_token,
                download_client_id=download_client_id,
                protocol=protocol,
            )
            ownership = _ownership(reservation)
            if ownership is not None and ownership.placement_carrier is not None:
                _discard_pack_placement(guard, reservation)
                current = _read_reservation(reservation.download_identity_key)
                if current is not None:
                    updated = _ownership(current)
                    if updated is not None:
                        _save_ownership(
                            guard,
                            current,
                            replace(
                                updated,
                                placement_carrier=None,
                                private_directory=None,
                                canonical_directory=None,
                            ),
                        )
                return
            if ownership is None or ownership.private_directory is None:
                return
            proof = ownership.private_directory
            if not os.path.lexists(proof.path):
                return
            reservation = _capture_pack_directory(
                guard, reservation, proof, ownership.inventory
            )
            _discard_reservation_claims(guard, reservation)
    except (FileMutationBusy, OSError, PrivateClaimError) as exc:
        log.warning("Retaining unpublished pack evidence: %s", exc)


def _acquire_cleanup_reservation(
    *,
    queue_id: int,
    download_id: str,
    download_client_id: int | None,
    protocol: DownloadProtocol | None,
    publication_id: int | None,
    lease_seconds: float | None,
) -> tuple[str, str, str] | None:
    identity = _pack_identity(
        download_id,
        download_client_id=download_client_id,
        protocol=protocol,
    )
    identity_key = download_identity_key(identity)
    normalized = normalize_download_id(download_id, identity.protocol)
    if not identity_key:
        return None
    owner_token = secrets.token_urlsafe(24)
    pack_path = _canonical_pack_path(identity)
    tombstone_path = _cleanup_tombstone_path(pack_path, owner_token)
    with get_db() as db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        if not _terminal_cleanup_eligible(
            db,
            queue_id=queue_id,
            identity=identity,
        ):
            return None
        existing = db.execute(
            "SELECT * FROM import_pack_cleanup_reservations WHERE download_identity_key=?",
            (identity_key,),
        ).fetchone()
        if existing is not None:
            observed = _reservation_from_row(dict(existing))
            if (
                observed.queue_id != queue_id
                or observed.purpose != "queueing"
                or _ownership(observed) is None
            ):
                return None
            db.execute(
                "UPDATE import_pack_cleanup_reservations SET owner_token=?,purpose='cleanup',"
                " publication_id=?,tombstone_path=?,expires_at=datetime('now',?)"
                " WHERE download_identity_key=? AND owner_token=?",
                (
                    owner_token,
                    publication_id,
                    tombstone_path,
                    _lease_modifier(lease_seconds),
                    identity_key,
                    observed.owner_token,
                ),
            )
            return owner_token, pack_path, tombstone_path
        if _reservation_conflicts(db, identity):
            return None
        cur = db.execute(
            """
            INSERT INTO import_pack_cleanup_reservations(
                download_identity_key, download_client_id, protocol,
                normalized_download_id, download_id, purpose, owner_token,
                queue_id, publication_id, pack_path, tombstone_path, expires_at
            ) VALUES(
                ?, ?, ?, ?, ?, 'cleanup', ?, ?, ?, ?, ?, datetime('now', ?)
            )
            ON CONFLICT(download_identity_key) DO NOTHING
            """,
            (
                identity_key,
                identity.download_client_id,
                identity.protocol,
                normalized,
                download_id,
                owner_token,
                queue_id,
                publication_id,
                pack_path,
                tombstone_path,
                _lease_modifier(lease_seconds),
            ),
        )
        if cur.rowcount != 1:
            return None
    return owner_token, pack_path, tombstone_path


def _track_detached_tombstone(
    db: sqlite3.Connection,
    *,
    download_identity_key: str,
    download_client_id: int | None,
    protocol: DownloadProtocol | None,
    normalized_download_id: str,
    download_id: str,
    queue_id: int,
    publication_id: int | None,
    pack_path: str,
    tombstone_path: str,
    carrier_json: str | None = None,
) -> None:
    db.execute(
        """
        INSERT INTO import_pack_cleanup_tombstones(
            tombstone_path, download_identity_key, download_client_id, protocol,
            normalized_download_id, download_id, queue_id, publication_id,
            pack_path, carrier_json
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tombstone_path) DO UPDATE SET
            publication_id=COALESCE(
                import_pack_cleanup_tombstones.publication_id,
                excluded.publication_id
            ),
            updated_at=CURRENT_TIMESTAMP
        """,
        (
            tombstone_path,
            download_identity_key,
            download_client_id,
            protocol,
            normalized_download_id,
            download_id,
            queue_id,
            publication_id,
            pack_path,
            carrier_json,
        ),
    )


def _valid_tombstone_path(pack_path: str, tombstone_path: str) -> bool:
    pack_abs = os.path.abspath(pack_path)
    tombstone_abs = os.path.abspath(tombstone_path)
    return os.path.dirname(tombstone_abs) == os.path.dirname(
        pack_abs
    ) and os.path.basename(tombstone_abs).startswith(
        f"{os.path.basename(pack_abs)}.cleanup-"
    )


def _mark_publication_cleanup_complete_in_db(
    db: sqlite3.Connection,
    publication_id: int | None,
) -> bool:
    if publication_id is None:
        return True
    cur = db.execute(
        """
        UPDATE import_publications
        SET pack_cleanup_state='complete',
            pack_cleanup_completed_at=CURRENT_TIMESTAMP,
            updated_at=CURRENT_TIMESTAMP
        WHERE id=? AND state IN ('finalized','deleted')
          AND pack_cleanup_state='pending'
        """,
        (publication_id,),
    )
    if cur.rowcount == 1:
        return True
    row = db.execute(
        "SELECT pack_cleanup_state FROM import_publications WHERE id=?",
        (publication_id,),
    ).fetchone()
    return row is not None and row["pack_cleanup_state"] == "complete"


def _mark_publication_cleanup_complete(publication_id: int | None) -> bool:
    with get_db() as db:
        return _mark_publication_cleanup_complete_in_db(db, publication_id)


def _artifact_owner_from_private_path(
    pack_path: str,
    private_path: str | None,
    fallback: str,
) -> str:
    if private_path is None:
        return fallback
    pack_abs = os.path.abspath(pack_path)
    private_abs = os.path.abspath(private_path)
    prefix = f"{os.path.basename(pack_abs)}.owner-"
    if os.path.dirname(private_abs) == os.path.dirname(pack_abs) and os.path.basename(
        private_abs
    ).startswith(prefix):
        artifact_owner = os.path.basename(private_abs)[len(prefix) :]
        if artifact_owner:
            return artifact_owner
    return fallback


def _reservation_from_row(row: Mapping[str, object]) -> _PackReservation:
    queue_id = row["queue_id"]
    publication_id = row["publication_id"]
    tombstone_path = row["tombstone_path"]
    owner_token = str(row["owner_token"])
    pack_path = str(row["pack_path"])
    private_path = str(tombstone_path) if tombstone_path is not None else None
    encoded = row.get("directory_ownership_json")
    original = (
        PackOwnership.from_json(str(encoded)).artifact_owner_token
        if encoded is not None
        else None
    )
    return _PackReservation(
        download_identity_key=str(row["download_identity_key"]),
        download_client_id=coerce_download_client_id(row["download_client_id"]),
        protocol=normalize_download_protocol(row["protocol"]),
        normalized_download_id=str(row["normalized_download_id"]),
        download_id=str(row["download_id"]),
        purpose=("queueing" if str(row["purpose"]) == "queueing" else "cleanup"),
        owner_token=owner_token,
        artifact_owner_token=original
        or _artifact_owner_from_private_path(
            pack_path,
            private_path,
            owner_token,
        ),
        queue_id=_optional_int(queue_id),
        publication_id=_optional_int(publication_id),
        pack_path=pack_path,
        tombstone_path=private_path,
        directory_ownership_json=str(encoded) if encoded is not None else None,
    )


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, (int, str, bytes)):
        raise TypeError(f"expected SQLite integer value, got {type(value).__name__}")
    return int(value)


def _claim_expired_reservation(
    observed: _PackReservation,
    *,
    lease_seconds: float | None = None,
) -> _PackReservation | None:
    recovery_owner = secrets.token_urlsafe(24)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        cur = db.execute(
            """
            UPDATE import_pack_cleanup_reservations
            SET owner_token=?, expires_at=datetime('now', ?),
                updated_at=CURRENT_TIMESTAMP
            WHERE download_identity_key=? AND owner_token=?
              AND expires_at <= CURRENT_TIMESTAMP
            """,
            (
                recovery_owner,
                _lease_modifier(lease_seconds),
                observed.download_identity_key,
                observed.owner_token,
            ),
        )
        if cur.rowcount != 1:
            return None
    return _PackReservation(
        download_identity_key=observed.download_identity_key,
        download_client_id=observed.download_client_id,
        protocol=observed.protocol,
        normalized_download_id=observed.normalized_download_id,
        download_id=observed.download_id,
        purpose=observed.purpose,
        owner_token=recovery_owner,
        artifact_owner_token=observed.artifact_owner_token,
        queue_id=observed.queue_id,
        publication_id=observed.publication_id,
        pack_path=observed.pack_path,
        tombstone_path=observed.tombstone_path,
        directory_ownership_json=observed.directory_ownership_json,
    )


def _real_directory_or_missing(path: str) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise OSError(f"pack path is not a real directory: {path}")
    return True


def _detach_directory(
    source: str,
    destination: str,
) -> Literal["detached", "missing", "blocked"]:
    if not _real_directory_or_missing(source):
        return "missing"
    try:
        _rename_noreplace(source, destination)
    except FileNotFoundError:
        return "missing"
    except FileExistsError:
        return "blocked"
    return "detached"


def _record_owned_tombstone(
    reservation: _PackReservation,
    tombstone_path: str,
) -> bool:
    ownership = _ownership(reservation)
    matching = (
        [
            c
            for c in ownership.claims
            if os.path.join(c.carrier.carrier_path, "artifact") == tombstone_path
        ]
        if ownership
        else []
    )
    if len(matching) != 1 or reservation.queue_id is None:
        return False
    with get_db() as db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        owned = db.execute(
            """
            SELECT 1
            FROM import_pack_cleanup_reservations
            WHERE download_identity_key=? AND owner_token=?
              AND expires_at > CURRENT_TIMESTAMP
            """,
            (
                reservation.download_identity_key,
                reservation.owner_token,
            ),
        ).fetchone()
        if owned is None:
            return False
        if reservation.queue_id is not None and not _terminal_cleanup_eligible(
            db,
            queue_id=reservation.queue_id,
            identity=DownloadIdentity(
                reservation.download_client_id,
                reservation.protocol,
                reservation.download_id,
            ),
        ):
            return False
        _track_detached_tombstone(
            db,
            download_identity_key=reservation.download_identity_key,
            download_client_id=reservation.download_client_id,
            protocol=reservation.protocol,
            normalized_download_id=reservation.normalized_download_id,
            download_id=reservation.download_id,
            queue_id=reservation.queue_id or 0,
            publication_id=reservation.publication_id,
            pack_path=reservation.pack_path,
            tombstone_path=tombstone_path,
            carrier_json=matching[0].to_json(),
        )
        db.execute(
            """
            DELETE FROM import_pack_cleanup_reservations
            WHERE download_identity_key=? AND owner_token=?
            """,
            (
                reservation.download_identity_key,
                reservation.owner_token,
            ),
        )
    return True


def _complete_owned_missing_reservation(
    reservation: _PackReservation,
) -> bool:
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        owned = db.execute(
            """
            SELECT 1
            FROM import_pack_cleanup_reservations
            WHERE download_identity_key=? AND owner_token=?
              AND expires_at > CURRENT_TIMESTAMP
            """,
            (
                reservation.download_identity_key,
                reservation.owner_token,
            ),
        ).fetchone()
        if owned is None:
            return False
        if reservation.queue_id is not None and not _terminal_cleanup_eligible(
            db,
            queue_id=reservation.queue_id,
            identity=DownloadIdentity(
                reservation.download_client_id,
                reservation.protocol,
                reservation.download_id,
            ),
        ):
            return False
        db.execute(
            """
            DELETE FROM import_pack_cleanup_reservations
            WHERE download_identity_key=? AND owner_token=?
            """,
            (
                reservation.download_identity_key,
                reservation.owner_token,
            ),
        )
        return _mark_publication_cleanup_complete_in_db(
            db,
            reservation.publication_id,
        )


def _release_owned_reservation(reservation: _PackReservation) -> bool:
    """Release a fenced stale reservation without declaring cleanup complete."""
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        cur = db.execute(
            """
            DELETE FROM import_pack_cleanup_reservations
            WHERE download_identity_key=? AND owner_token=?
              AND expires_at > CURRENT_TIMESTAMP
            """,
            (
                reservation.download_identity_key,
                reservation.owner_token,
            ),
        )
        return cur.rowcount == 1


def _detach_terminal_cleanup(
    reservation: _PackReservation,
    *,
    _guard: FileMutationGuard,
) -> Literal["tracked", "missing", "retry"]:
    try:
        ownership = _ownership(reservation)
        if ownership is not None and ownership.placement_carrier is not None:
            proof = ownership.canonical_directory
            physical = os.path.join(
                ownership.placement_carrier.carrier_path, "artifact"
            )
            if proof is None or proof.path != physical:
                raise PackProofError("terminal private placement proof missing")
            reservation = _discard_pack_placement(_guard, reservation)
            completed = _ownership(reservation)
            if completed is None:
                raise PackProofError("terminal placement disposition missing")
            with get_db() as db:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                changed = db.execute(
                    "UPDATE import_pack_cleanup_reservations SET purpose='queueing',tombstone_path=NULL,directory_ownership_json=? WHERE download_identity_key=? AND owner_token=?",
                    (
                        replace(
                            completed,
                            phase="attached",
                            retained_reason="terminal-discarded",
                        ).to_json(),
                        reservation.download_identity_key,
                        reservation.owner_token,
                    ),
                )
                if changed.rowcount != 1:
                    raise PackProofError("terminal placement authority lost")
                if not _mark_publication_cleanup_complete_in_db(
                    db, reservation.publication_id
                ):
                    raise PackProofError("terminal publication cleanup authority lost")
            return "missing"
        if ownership is not None and ownership.claims:
            claim = ownership.claims[-1]
            return (
                "tracked"
                if _record_owned_tombstone(
                    reservation, os.path.join(claim.carrier.carrier_path, "artifact")
                )
                else "retry"
            )
        tombstone = reservation.tombstone_path
        if tombstone and os.path.lexists(tombstone):
            return "retry"
        if not _real_directory_or_missing(reservation.pack_path):
            return (
                "missing"
                if _complete_owned_missing_reservation(reservation)
                else "retry"
            )
        with open_directory(reservation.pack_path) as source:
            proof = ownership.canonical_directory if ownership else None
            if proof is None:
                if ownership is not None:
                    raise PackProofError("terminal canonical proof missing")
                proof = directory_proof(reservation.pack_path, source)
            verify_directory(source, proof)
            inventory = ownership.inventory if ownership else None
        reservation = _capture_pack_directory(_guard, reservation, proof, inventory)
        if reservation.tombstone_path is None:
            return "retry"
        return (
            "tracked"
            if _record_owned_tombstone(reservation, reservation.tombstone_path)
            else "retry"
        )
    except (OSError, PrivateClaimError) as exc:
        log.warning("Retaining terminal pack cleanup evidence: %s", exc)
        return "retry"


def _recover_abandoned_queue_creation(
    reservation: _PackReservation,
    *,
    _guard: FileMutationGuard,
) -> bool:
    try:
        ownership = _ownership(reservation)
        if ownership is not None and ownership.placement_carrier is not None:
            proof = ownership.canonical_directory or ownership.private_directory
            native = proof is not None and proof.path == reservation.pack_path
            native = (
                native
                and os.path.lexists(reservation.pack_path)
                and not os.path.lexists(
                    os.path.join(ownership.placement_carrier.carrier_path, "artifact")
                )
            )
            reservation = _discard_pack_placement(
                _guard, reservation, empty_only=native
            )
            if not native:
                return _complete_owned_missing_reservation(reservation)
            ownership = _ownership(reservation)
        if ownership is None:
            private = reservation.tombstone_path
            if private and _real_directory_or_missing(private):
                if os.path.lexists(reservation.pack_path):
                    return False
                source_path = private
            elif _real_directory_or_missing(reservation.pack_path):
                identity = DownloadIdentity(
                    reservation.download_client_id,
                    reservation.protocol,
                    reservation.download_id,
                )
                if not _pack_tree_has_owner_marker(
                    reservation.pack_path, identity, reservation.artifact_owner_token
                ):
                    return False
                source_path = reservation.pack_path
            else:
                return _complete_owned_missing_reservation(reservation)
            with open_directory(source_path) as source:
                proof = directory_proof(source_path, source)
            reservation = _capture_pack_directory(_guard, reservation, proof, None)
        else:
            claimed_paths = {c.source_directory.path for c in ownership.claims}
            candidates = [
                p
                for p in (ownership.private_directory, ownership.canonical_directory)
                if p is not None
                and p.path not in claimed_paths
                and os.path.lexists(p.path)
            ]

            def partial_canonical(proof: DirectoryProof) -> bool:
                private = ownership.private_directory
                return (
                    ownership.phase == "attaching"
                    and proof == ownership.canonical_directory
                    and private is not None
                    and (proof.dev, proof.inode) != (private.dev, private.inode)
                )

            for proof in candidates:
                with open_directory(proof.path) as fd:
                    verify_directory(fd, proof)
                    if ownership.inventory is not None:
                        verify_inventory(
                            fd,
                            ownership.inventory,
                            partial_attachment=partial_canonical(proof),
                        )
            for path in (reservation.pack_path, reservation.tombstone_path):
                if (
                    path
                    and os.path.lexists(path)
                    and path not in claimed_paths
                    and not any(p.path == path for p in candidates)
                ):
                    if not any(
                        os.path.join(c.carrier.carrier_path, "artifact") == path
                        for c in ownership.claims
                    ):
                        return False
            for proof in candidates:
                reservation = _capture_pack_directory(
                    _guard,
                    reservation,
                    proof,
                    ownership.inventory,
                    allow_partial=partial_canonical(proof),
                )
        if not _discard_reservation_claims(_guard, reservation):
            return False
        return _complete_owned_missing_reservation(reservation)
    except (OSError, PrivateClaimError) as exc:
        log.warning("Retaining abandoned pack proof: %s", exc)
        return False


def _capture_proven_legacy_tombstone(
    guard: FileMutationGuard, row: Mapping[str, object]
) -> dict[str, object]:
    source_path = str(row["tombstone_path"])
    pack_path = str(row["pack_path"])
    with get_db() as db:
        journal = db.execute(
            "SELECT * FROM import_pack_cleanup_tombstones WHERE tombstone_path=? AND carrier_json IS NULL",
            (source_path,),
        ).fetchone()
        if journal is None:
            raise PackProofError("legacy tombstone journal changed")
        identity = DownloadIdentity(
            coerce_download_client_id(journal["download_client_id"]),
            normalize_download_protocol(journal["protocol"]),
            str(journal["download_id"]),
        )
        if pack_path != _canonical_pack_path(
            identity
        ) or not _terminal_cleanup_eligible(
            db, queue_id=int(journal["queue_id"]), identity=identity
        ):
            raise PackProofError("legacy tombstone is not authorized")
        records = db.execute(
            "SELECT f.src_path,f.source_dev,f.source_inode,f.source_size,f.source_mtime_ns,f.source_sha256"
            " FROM import_publication_files f JOIN import_publications p ON p.id=f.publication_id"
            " WHERE p.queue_id=? AND p.state IN ('finalized','deleted')",
            (journal["queue_id"],),
        ).fetchall()
    expected: dict[str, FullFileFingerprint] = {}
    for record in records:
        if os.path.commonpath((str(record[0]), pack_path)) != pack_path or any(
            v is None for v in record[1:]
        ):
            continue
        relative = os.path.relpath(str(record[0]), pack_path)
        expected[relative] = FullFileFingerprint.from_value(
            dict(zip(("dev", "inode", "size", "mtime_ns", "sha256"), record[1:]))
        )
    with open_directory(source_path) as source:
        proof = directory_proof(source_path, source)
        inventory = inventory_tree(source)
    if not inventory.files or any(
        expected.get(name) != fp for name, fp in inventory.files.items()
    ):
        raise PackProofError("legacy tombstone lacks independent remaining-file proof")
    parents = {os.path.dirname(name) for name in inventory.files}
    ancestors: set[str] = set()
    for parent in parents:
        while parent:
            ancestors.add(parent)
            parent = os.path.dirname(parent)
    if set(inventory.directories) != ancestors:
        raise PackProofError("legacy tombstone contains unproven directories")
    binding = ClaimBinding("pack", source_path, None, "pack_cleanup")
    with ensure_namespace(guard, os.path.dirname(source_path)) as namespace:
        with allocate_carrier(namespace, binding, source_path) as carrier:
            claim = PackDirectoryClaim(carrier.record, proof, inventory, None)
            updated = dict(journal)
            updated["tombstone_path"] = carrier.artifact_path
            encoded = claim.to_json()
            with get_db() as db:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                cur = db.execute(
                    "UPDATE import_pack_cleanup_tombstones SET tombstone_path=?,carrier_json=?"
                    " WHERE tombstone_path=? AND carrier_json IS NULL",
                    (carrier.artifact_path, encoded, source_path),
                )
                if cur.rowcount != 1:
                    raise PackProofError("legacy claim journal authority lost")

            def persist(record: PackDirectoryClaim) -> None:
                nonlocal encoded
                guard.verify()
                with get_db() as db:
                    db.execute("PRAGMA synchronous=FULL")
                    db.execute("BEGIN IMMEDIATE")
                    cur = db.execute(
                        "UPDATE import_pack_cleanup_tombstones SET carrier_json=? WHERE tombstone_path=? AND carrier_json=?",
                        (record.to_json(), carrier.artifact_path, encoded),
                    )
                    if cur.rowcount != 1:
                        raise PackProofError("legacy claim phase authority lost")
                encoded = record.to_json()

            claim = replace(claim, carrier=replace(claim.carrier, phase="claiming"))
            persist(claim)
            carrier.record = claim.carrier
            with open_directory(source_path) as source:
                verify_directory(source, proof)
                verify_inventory(source, inventory)
            claim_into_empty(
                guard, carrier, namespace.parent_fd, os.path.basename(source_path)
            )
            with open_directory(carrier.artifact_path) as artifact:
                verify_directory(artifact, proof, path_binding=False)
                verify_inventory(artifact, inventory)
            claim = replace(claim, carrier=replace(claim.carrier, phase="claimed"))
            persist(claim)
            updated["carrier_json"] = encoded
            return updated


def _remove_tracked_tombstone(
    row: Mapping[str, object], *, _guard: FileMutationGuard | None = None
) -> bool:
    try:
        with _borrow_pack_guard(_guard) as guard:
            tombstone = str(row["tombstone_path"])
            encoded = row.get("carrier_json")
            if encoded is None:
                if not _valid_tombstone_path(str(row["pack_path"]), tombstone):
                    return False
                if os.path.lexists(tombstone):
                    updated = _capture_proven_legacy_tombstone(guard, row)
                    return _remove_tracked_tombstone(updated, _guard=guard)
                _fsync_pack_root(str(row["pack_path"]))
            else:
                claim = PackDirectoryClaim.from_json(str(encoded))
                if tombstone != os.path.join(claim.carrier.carrier_path, "artifact"):
                    return False
                with get_db() as db:
                    journal = db.execute(
                        "SELECT * FROM import_pack_cleanup_tombstones WHERE tombstone_path=?",
                        (tombstone,),
                    ).fetchone()
                    if journal is None or str(journal["carrier_json"]) != str(encoded):
                        return False
                    identity = DownloadIdentity(
                        coerce_download_client_id(journal["download_client_id"]),
                        normalize_download_protocol(journal["protocol"]),
                        str(journal["download_id"]),
                    )
                    pack_path = str(journal["pack_path"])
                    if (
                        pack_path != _canonical_pack_path(identity)
                        or not (
                            claim.source_directory.path == pack_path
                            or _valid_tombstone_path(
                                pack_path, claim.source_directory.path
                            )
                        )
                        or (
                            claim.artifact_owner_token is not None
                            and (
                                claim.source_directory.path != pack_path
                                or claim.carrier.binding.operation_key
                                != claim.artifact_owner_token
                            )
                        )
                    ):
                        raise PackProofError(
                            "tombstone directory claim target mismatch"
                        )
                    if not _terminal_cleanup_eligible(
                        db, queue_id=int(journal["queue_id"]), identity=identity
                    ):
                        return False
                reservation = _PackReservation(
                    str(journal["download_identity_key"]),
                    identity.download_client_id,
                    identity.protocol,
                    str(journal["normalized_download_id"]),
                    identity.download_id,
                    "cleanup",
                    "",
                    claim.artifact_owner_token or claim.carrier.binding.operation_key,
                    int(journal["queue_id"]),
                    _optional_int(journal["publication_id"]),
                    str(journal["pack_path"]),
                    tombstone,
                )

                def persist(updated: PackDirectoryClaim) -> None:
                    nonlocal encoded
                    guard.verify()
                    with get_db() as db:
                        db.execute("PRAGMA synchronous=FULL")
                        db.execute("BEGIN IMMEDIATE")
                        cur = db.execute(
                            "UPDATE import_pack_cleanup_tombstones SET carrier_json=? WHERE tombstone_path=? AND carrier_json=?",
                            (updated.to_json(), tombstone, encoded),
                        )
                        if cur.rowcount != 1:
                            raise PackProofError("tombstone proof authority changed")
                    encoded = updated.to_json()

                _discard_pack_claim(
                    guard,
                    claim,
                    persist,
                    allowed_missing=_committed_missing(reservation, claim.inventory),
                )
                _fsync_pack_root(str(row["pack_path"]))
                if (
                    claim.artifact_owner_token is not None
                    and claim.source_directory.path == reservation.pack_path
                ):
                    # Keep original provenance while the terminal queue still exists.
                    # A repeated cleanup must not treat a recreated name as fresh v0 data.
                    completed = PackOwnership(
                        reservation.download_identity_key,
                        claim.artifact_owner_token,
                        phase="attached",
                        canonical_directory=claim.source_directory,
                        inventory=claim.inventory,
                        retained_reason="terminal-discarded",
                    )
                    with get_db() as db:
                        db.execute("PRAGMA synchronous=FULL")
                        db.execute("BEGIN IMMEDIATE")
                        db.execute(
                            "INSERT INTO import_pack_cleanup_reservations(download_identity_key,download_client_id,"
                            "protocol,normalized_download_id,download_id,purpose,owner_token,queue_id,publication_id,"
                            "pack_path,tombstone_path,expires_at,directory_ownership_json)"
                            " VALUES(?,?,?,?,?,'queueing',?,?,?,?,NULL,datetime('now',?),?)"
                            " ON CONFLICT(download_identity_key) DO NOTHING",
                            (
                                reservation.download_identity_key,
                                reservation.download_client_id,
                                reservation.protocol,
                                reservation.normalized_download_id,
                                reservation.download_id,
                                claim.artifact_owner_token,
                                reservation.queue_id,
                                reservation.publication_id,
                                reservation.pack_path,
                                _lease_modifier(None),
                                completed.to_json(),
                            ),
                        )
            with get_db() as db:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "DELETE FROM import_pack_cleanup_tombstones WHERE tombstone_path=? AND carrier_json IS ?",
                    (tombstone, encoded),
                )
                _mark_publication_cleanup_complete_in_db(
                    db, _optional_int(row.get("publication_id"))
                )
            return True
    except (OSError, PrivateClaimError) as exc:
        log.warning("Retaining pack tombstone evidence: %s", exc)
        return False


def recover_pack_cleanup_state(
    *,
    max_rows: int = 100,
    publication_id: int | None = None,
    _guard: FileMutationGuard | None = None,
) -> PackCleanupRecovery:
    """Replay guarded reservations and carriers, with no payload IO in writers."""
    if max_rows <= 0:
        return PackCleanupRecovery()
    try:
        with _borrow_pack_guard(_guard) as guard:
            return _recover_pack_cleanup_with_guard(guard, max_rows, publication_id)
    except FileMutationBusy:
        return PackCleanupRecovery()


def _recover_pack_cleanup_with_guard(
    guard: FileMutationGuard, max_rows: int, publication_id: int | None
) -> PackCleanupRecovery:
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM import_pack_cleanup_reservations r WHERE expires_at <= CURRENT_TIMESTAMP"
            " AND (purpose='cleanup' OR queue_id IS NULL OR NOT EXISTS"
            " (SELECT 1 FROM import_queue q WHERE q.id=r.queue_id AND q.status IN ('pending','partial','importing')))"
            " AND NOT (purpose='queueing' AND queue_id IS NOT NULL AND EXISTS"
            " (SELECT 1 FROM import_queue q WHERE q.id=r.queue_id) AND COALESCE(CASE"
            " WHEN json_valid(directory_ownership_json) THEN json_extract(directory_ownership_json,'$.retained_reason')"
            " END,'')='terminal-discarded')"
            + (" AND publication_id=?" if publication_id is not None else "")
            + " ORDER BY updated_at, normalized_download_id LIMIT ?",
            (publication_id, max_rows) if publication_id is not None else (max_rows,),
        ).fetchall()
    recovered = removed = retained = 0
    for row in rows:
        try:
            observed = _reservation_from_row(dict(row))
            _ownership(observed)
            guard.verify()
            reservation = _claim_expired_reservation(observed)
            if reservation is None:
                continue
            ownership = _ownership(reservation)
            if reservation.queue_id is None:
                handled = _recover_abandoned_queue_creation(reservation, _guard=guard)
                removed += int(
                    handled
                    and (
                        ownership is None
                        or ownership.phase != "building"
                        or ownership.private_directory is not None
                    )
                )
            elif ownership is not None and ownership.phase == "queued":
                with get_db() as db:
                    _sql_queue_authority(db, reservation, ownership)
                try:
                    with _open_generated_queue_directory(guard, reservation, ownership):
                        pass
                except (OSError, PrivateClaimError):
                    _cancel_generated_pack_queue(reservation, ownership)
                    raise
                _finish_generated_pack_queue(guard, reservation, reservation.queue_id)
                handled = True
            else:
                with get_db() as db:
                    eligible = _terminal_cleanup_eligible(
                        db,
                        queue_id=reservation.queue_id,
                        identity=DownloadIdentity(
                            reservation.download_client_id,
                            reservation.protocol,
                            reservation.download_id,
                        ),
                    )
                if eligible:
                    handled = (
                        _detach_terminal_cleanup(reservation, _guard=guard) != "retry"
                    )
                elif (
                    ownership is None
                    and not os.path.lexists(reservation.pack_path)
                    and (
                        reservation.tombstone_path is None
                        or not os.path.lexists(reservation.tombstone_path)
                    )
                ):
                    # A legacy fence with no artifacts must not strand a pending consumer.
                    handled = _release_owned_reservation(reservation)
                else:
                    handled = False
            recovered += int(handled)
        except (OSError, PrivateClaimError) as exc:
            log.warning("Retaining invalid pack reservation proof: %s", exc)
    with get_db() as db:
        tombstones = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM import_pack_cleanup_tombstones"
                + (" WHERE publication_id=?" if publication_id is not None else "")
                + " ORDER BY created_at,tombstone_path LIMIT ?",
                (publication_id, max_rows)
                if publication_id is not None
                else (max_rows,),
            ).fetchall()
        ]
    for tombstone in tombstones:
        if _remove_tracked_tombstone(tombstone, _guard=guard):
            removed += 1
        else:
            retained += 1
    return PackCleanupRecovery(recovered, removed, retained)


def cleanup_terminal_pack_staging(
    queue_id: int,
    download_id: str,
    *,
    download_client_id: int | None,
    protocol: DownloadProtocol | None,
    publication_id: int | None = None,
    lease_seconds: float | None = None,
    _guard: FileMutationGuard | None = None,
) -> bool:
    """Capture authorized terminal output privately before recursive deletion."""
    try:
        with _borrow_pack_guard(_guard) as guard:
            recovery = recover_pack_cleanup_state(
                publication_id=publication_id, _guard=guard
            )
            if recovery.tombstones_retained:
                return False
            acquired = _acquire_cleanup_reservation(
                queue_id=queue_id,
                download_id=download_id,
                download_client_id=download_client_id,
                protocol=protocol,
                publication_id=publication_id,
                lease_seconds=lease_seconds,
            )
            if acquired is None:
                return False
            identity = _pack_identity(
                download_id, download_client_id=download_client_id, protocol=protocol
            )
            reservation = _read_reservation(download_identity_key(identity))
            if reservation is None:
                return False
            detached = _detach_terminal_cleanup(reservation, _guard=guard)
            if detached == "retry":
                return False
            if detached == "missing":
                return True
            with get_db() as db:
                rows = [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM import_pack_cleanup_tombstones WHERE download_identity_key=?",
                        (reservation.download_identity_key,),
                    ).fetchall()
                ]
            return bool(rows) and all(
                _remove_tracked_tombstone(row, _guard=guard) for row in rows
            )
    except (FileMutationBusy, OSError, PrivateClaimError) as exc:
        log.warning("Pack cleanup remains fenced: %s", exc)
        return False
