"""Durable rescan enrichment: short SQLite decisions, owned FILE carriers.

Only journal-recorded authority is replayable. In particular an unacknowledged
public link is not a receipt, even when it still names the private inode.
"""

from __future__ import annotations

import asyncio
import errno
from contextlib import contextmanager
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import stat
import tempfile
import threading
from typing import Any, TYPE_CHECKING, TypeVar, cast

import private_file_claim as claims
import shared
from file_mutation_lock import (
    FileMutationGuard,
    FileMutationLockError,
    file_mutation_guard,
)
from shared import get_db

if TYPE_CHECKING:
    from library_scan import AdoptUnmappedFolderResult
    from rescan import RescanResult, _EnrichmentContext, _EnrichmentTarget

_Result = TypeVar("_Result")
ACTIVE_STATES = "('prepared','published','db_committed','rollback')"
_rescan_cancellation: ContextVar[threading.Event | None] = ContextVar(
    "rescan_cancellation", default=None
)


def rescan_cancel_requested() -> bool:
    cancellation = _rescan_cancellation.get()
    return cancellation is not None and cancellation.is_set()


def active_for_series(db: sqlite3.Connection, series_id: int) -> bool:
    return (
        db.execute(
            f"SELECT 1 FROM rescan_file_operations WHERE series_id=? AND state IN {ACTIVE_STATES} LIMIT 1",
            (series_id,),
        ).fetchone()
        is not None
    )


def active_for_path(db: sqlite3.Connection, path: str) -> bool:
    return (
        db.execute(
            f"SELECT 1 FROM rescan_file_operations WHERE state IN {ACTIVE_STATES} "
            "AND (substr(source_path,1,length(?)+1)=?||'/' "
            "OR substr(destination_path,1,length(?)+1)=?||'/') LIMIT 1",
            (path, path, path, path),
        ).fetchone()
        is not None
    )


def competing_for_series(db: sqlite3.Connection, series_id: int) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM volume_file_deletions WHERE series_id=? AND state='active' "
            "UNION ALL SELECT 1 FROM import_publications WHERE series_id=? AND state IN "
            "('staging','prepared','publishing','published','db_committed','cleaning') "
            "UNION ALL SELECT 1 FROM import_queue WHERE series_id=? AND "
            "(status='importing' OR lease_owner IS NOT NULL) LIMIT 1",
            (series_id,) * 3,
        ).fetchone()
        is not None
    )


def _encode(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise claims.PrivateClaimError("duplicate rescan journal JSON field")
        result[key] = value
    return result


def _decode(value: str, keys: set[str] | None = None) -> dict[str, Any]:
    result = json.loads(value, object_pairs_hook=_pairs)
    if not isinstance(result, dict) or (keys is not None and set(result) != keys):
        raise claims.PrivateClaimError("invalid rescan journal JSON fields")
    return result


def _path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not os.path.isabs(value)
        or os.path.abspath(value) != value
        or "\0" in value
    ):
        raise claims.PrivateClaimError("invalid rescan journal path")
    return value


def fingerprint_path(path: str) -> claims.FullFileFingerprint:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise claims.PrivateClaimError("rescan file is not regular")
        digest = hashlib.sha256()
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(fd)
        current = os.lstat(path)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
        if identity(before) != identity(after) or identity(after) != identity(current):
            raise claims.PrivateClaimError("rescan file changed while hashing")
        return claims.FullFileFingerprint(
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            digest.hexdigest(),
        )
    finally:
        os.close(fd)


@dataclass
class Operation:
    row: dict[str, Any]
    fingerprints: dict[str, Any]
    carriers: dict[str, Any]


def load_operation(operation_id: int) -> Operation | None:
    import rescan

    with get_db() as db:
        row = db.execute(
            "SELECT * FROM rescan_file_operations WHERE id=?", (operation_id,)
        ).fetchone()
        values = dict(row) if row else None
    if values is None:
        return None
    if values["version"] != 1 or values["state"] not in {
        "prepared",
        "published",
        "db_committed",
        "rollback",
        "rolled_back",
        "completed",
    }:
        raise claims.PrivateClaimError("unsupported rescan journal")
    for key in ("source_path", "destination_path"):
        _path(values[key])
    if os.path.dirname(values["source_path"]) != os.path.dirname(
        values["destination_path"]
    ):
        raise claims.PrivateClaimError("rescan destination parent differs")
    token = values["operation_token"]
    if (
        not isinstance(token, str)
        or len(token) != 32
        or any(c not in "0123456789abcdef" for c in token)
    ):
        raise claims.PrivateClaimError("invalid rescan operation token")
    fingerprints = _decode(
        values["fingerprints_json"],
        {"source", "stage", "publication", "captured_source"},
    )
    claims.FullFileFingerprint.from_value(fingerprints["source"])
    if fingerprints["stage"] is not None:
        claims.FullFileFingerprint.from_value(fingerprints["stage"])
    if fingerprints["captured_source"] is not None:
        claims.FullFileFingerprint.from_value(fingerprints["captured_source"])
    if fingerprints["publication"] is not None:
        claims.FullFileFingerprint.from_value(fingerprints["publication"])
    operation = Operation(
        values,
        fingerprints,
        _decode(
            values["carriers_json"], {"stage", "publication", "source", "rollback"}
        ),
    )
    for purpose in operation.carriers:
        _record(operation, purpose)
    volume = _decode(values["expected_volume_json"], set(rescan._VOLUME_GUARD))
    context = _decode(values["expected_context_json"], {"series", "tags"})
    if (
        volume["id"] != values["volume_id"]
        or volume["series_id"] != values["series_id"]
        or not isinstance(context["series"], dict)
        or context["series"].get("id") != values["series_id"]
        or not isinstance(context["tags"], list)
        or any(not isinstance(tag, str) for tag in context["tags"])
        or context["tags"] != sorted(set(context["tags"]))
    ):
        raise claims.PrivateClaimError("rescan snapshot binding differs")
    if values["publication_receipt_json"] is not None:
        receipt = claims.LinkReceipt.from_value(
            _decode(values["publication_receipt_json"])
        )
        if receipt.destination_path != values[
            "destination_path"
        ] or receipt.fingerprint != _fingerprint(operation, "publication"):
            raise claims.PrivateClaimError(
                "rescan publication receipt differs from prepared stage"
            )
    return operation


def _binding(operation: Operation, purpose: str) -> claims.ClaimBinding:
    return claims.ClaimBinding(
        "rescan", operation.row["operation_token"], operation.row["volume_id"], purpose
    )


def _record(operation: Operation | None, purpose: str) -> claims.CarrierRecord | None:
    if operation is None:
        raise claims.PrivateClaimError("rescan journal disappeared")
    value = operation.carriers[purpose]
    if value is None:
        return None
    record = claims.CarrierRecord.from_json(_encode(value))
    origin = (
        operation.row["source_path"]
        if purpose == "source"
        else operation.row["destination_path"]
    )
    if record.binding != _binding(operation, purpose) or record.origin_path != origin:
        raise claims.PrivateClaimError("rescan carrier binding differs")
    expected = (
        None
        if purpose in {"stage", "publication"}
        else _fingerprint(operation, "source" if purpose == "source" else "publication")
    )
    if record.artifact_fingerprint != expected:
        raise claims.PrivateClaimError("rescan carrier capture proof differs")
    return record


def _fingerprint(operation: Operation, purpose: str) -> claims.FullFileFingerprint:
    return claims.FullFileFingerprint.from_value(operation.fingerprints[purpose])


def _store(
    operation: Operation, *, state: str | None = None, diagnostic: str | None = None
) -> None:
    previous = operation.row["state"]
    next_state = state or previous
    with get_db() as db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        updated = db.execute(
            "UPDATE rescan_file_operations SET state=?,fingerprints_json=?,carriers_json=?,"
            "publication_receipt_json=?,diagnostic=?,updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND operation_token=? AND state=?",
            (
                next_state,
                _encode(operation.fingerprints),
                _encode(operation.carriers),
                operation.row["publication_receipt_json"],
                diagnostic,
                operation.row["id"],
                operation.row["operation_token"],
                previous,
            ),
        )
        if updated.rowcount != 1:
            raise claims.PrivateClaimError("rescan journal decision changed")
    operation.row["state"] = next_state
    operation.row["diagnostic"] = diagnostic


def _save_record(
    operation: Operation, purpose: str, record: claims.CarrierRecord
) -> None:
    operation.carriers[purpose] = asdict(record)
    _store(operation)


@contextmanager
def _open(operation: Operation, guard: FileMutationGuard, purpose: str):
    record = _record(operation, purpose)
    if record is None:
        raise claims.PrivateClaimError("rescan carrier lacks durable allocation")
    with claims.ensure_namespace(
        guard, os.path.dirname(record.origin_path)
    ) as namespace:
        with claims.open_carrier(
            namespace, _binding(operation, purpose), record
        ) as carrier:
            yield carrier


def _allocate(operation: Operation, guard: FileMutationGuard, purpose: str) -> None:
    origin = (
        operation.row["source_path"]
        if purpose == "source"
        else operation.row["destination_path"]
    )
    expected = (
        None
        if purpose in {"stage", "publication"}
        else _fingerprint(operation, "source" if purpose == "source" else "publication")
    )
    _store(operation, diagnostic=f"allocating:{purpose}")
    with claims.ensure_namespace(guard, os.path.dirname(origin)) as namespace:
        with claims.allocate_carrier(
            namespace, _binding(operation, purpose), origin, expected
        ) as carrier:
            _save_record(operation, purpose, carrier.record)


def _phase(
    operation: Operation,
    purpose: str,
    carrier: claims.CarrierHandle,
    phase: str,
    receipt: claims.LinkReceipt | None = None,
) -> None:
    carrier.record = replace(
        carrier.record,
        phase=phase,
        restore_receipt=receipt or carrier.record.restore_receipt,
    )
    _save_record(operation, purpose, carrier.record)


def _reserve(
    target: _EnrichmentTarget,
    context: _EnrichmentContext,
    source: claims.FullFileFingerprint,
    destination: str,
) -> Operation | None:
    import rescan

    with get_db() as db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        if rescan._current_enrichment_context(
            target, db
        ) != context or active_for_series(db, target.volume["series_id"]):
            return None
        if competing_for_series(db, target.volume["series_id"]):
            return None
        cursor = db.execute(
            "INSERT INTO rescan_file_operations(operation_token,series_id,volume_id,source_path,destination_path,"
            "expected_volume_json,expected_context_json,fingerprints_json,carriers_json,state) "
            "VALUES(?,?,?,?,?,?,?,?,?,'prepared')",
            (
                secrets.token_hex(16),
                target.volume["series_id"],
                target.volume["id"],
                target.source_path,
                destination,
                _encode(target.volume),
                _encode(asdict(context)),
                _encode(
                    {
                        "source": asdict(source),
                        "stage": None,
                        "publication": None,
                        "captured_source": None,
                    }
                ),
                _encode(
                    {
                        "source": None,
                        "stage": None,
                        "publication": None,
                        "rollback": None,
                    }
                ),
            ),
        )
        operation_id = cast(int, cursor.lastrowid)
    return load_operation(operation_id)


def _capture_source(
    operation: Operation, guard: FileMutationGuard
) -> claims.CarrierRecord:
    with _open(operation, guard, "stage") as stage:
        if claims.fingerprint_regular(stage) != _fingerprint(operation, "stage"):
            raise claims.PrivateClaimError(
                "source capture requires a verified ready stage"
            )
    _allocate(operation, guard, "source")
    with _open(operation, guard, "source") as carrier:
        _phase(operation, "source", carrier, "claiming")
        claims.claim_into_empty(
            guard,
            carrier,
            carrier.namespace.parent_fd,
            os.path.basename(carrier.record.origin_path),
        )
        _phase(operation, "source", carrier, "claimed")
        return carrier.record


def _publish(operation: Operation, guard: FileMutationGuard) -> claims.LinkReceipt:
    import rescan

    # Native rename consumes only this publication copy. The ready stage and
    # captured original remain available until the full DB decision.
    _allocate(operation, guard, "publication")
    with _open(operation, guard, "stage") as stage:
        if claims.fingerprint_regular(stage) != _fingerprint(operation, "stage"):
            raise claims.PrivateClaimError("prepared stage changed; retained")
        with _open(operation, guard, "publication") as carrier:
            _copy_prepared(operation, stage.artifact_path, carrier, "publication")
    with _open(operation, guard, "publication") as carrier:
        expected = _fingerprint(operation, "publication")
        _phase(operation, "publication", carrier, "restoring")
        carrier.verify()
        try:
            renamed = rescan._rename_noreplace(
                carrier.artifact_path, operation.row["destination_path"]
            )
        except OSError as exc:
            if exc.errno not in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
                raise
            receipt = claims.link_private_regular(
                guard,
                carrier,
                carrier.namespace.parent_fd,
                os.path.basename(operation.row["destination_path"]),
                expected,
            )
        else:
            if not renamed:
                raise FileExistsError(
                    errno.EEXIST, "rescan publication destination is occupied"
                )
            os.fsync(carrier.fd)
            os.fsync(carrier.namespace.parent_fd)
            carrier.verify()
            if fingerprint_path(operation.row["destination_path"]) != expected:
                raise claims.PrivateClaimError("native publication changed; retained")
            receipt = claims.LinkReceipt(operation.row["destination_path"], expected)
        operation.row["publication_receipt_json"] = _encode(asdict(receipt))
        _store(operation, state="published")
        return receipt


def _commit(operation: Operation, guard: FileMutationGuard) -> bool:
    import rescan

    guard.verify()
    receipt_json = operation.row["publication_receipt_json"]
    if receipt_json is None:
        return False
    receipt = claims.LinkReceipt.from_value(_decode(receipt_json))
    try:
        for purpose in ("source", "stage"):
            with _open(operation, guard, purpose) as carrier:
                if claims.fingerprint_regular(carrier) != _fingerprint(
                    operation, purpose
                ):
                    return False
        if fingerprint_path(receipt.destination_path) != receipt.fingerprint:
            return False
    except (OSError, claims.PrivateClaimError):
        return False
    guard.verify()
    volume = _decode(operation.row["expected_volume_json"])
    context_values = _decode(operation.row["expected_context_json"], {"series", "tags"})
    context = rescan._EnrichmentContext(
        context_values["series"], tuple(context_values["tags"])
    )
    source = _fingerprint(operation, "source")
    target = rescan._EnrichmentTarget(
        volume,
        float(volume["volume_num"]),
        operation.row["source_path"],
        rescan.FileFingerprint(source.size, source.mtime_ns, source.dev, source.inode),
    )
    with get_db() as db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        if rescan._current_enrichment_context(target, db) != context:
            return False
        if operation.row["destination_path"] != operation.row["source_path"]:
            updated = db.execute(
                rescan._UPDATE_CONVERTED_VOLUME_SQL,
                (
                    operation.row["destination_path"],
                    _fingerprint(operation, "stage").size,
                    *rescan._guard_values(volume, rescan._VOLUME_GUARD),
                ),
            )
            if updated.rowcount != 1:
                return False
        updated = db.execute(
            "UPDATE rescan_file_operations SET state='db_committed',diagnostic=NULL,updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND operation_token=? AND state='published'",
            (operation.row["id"], operation.row["operation_token"]),
        )
        if updated.rowcount != 1:
            raise claims.PrivateClaimError("rescan publication decision lost")
    operation.row["state"] = "db_committed"
    return True


def _discard(operation: Operation, guard: FileMutationGuard, purpose: str) -> None:
    record = _record(operation, purpose)
    if record is None:
        return
    if record.phase != "discarded":
        with _open(operation, guard, purpose) as carrier:
            fingerprint_key = (
                "source"
                if purpose == "source"
                else "publication"
                if purpose in {"publication", "rollback"}
                else "stage"
            )
            expected = operation.fingerprints[fingerprint_key]
            if purpose == "source" and carrier.record.restore_receipt is not None:
                expected = asdict(carrier.record.restore_receipt.fingerprint)
            _phase(operation, purpose, carrier, "discarding")
            if expected is None:
                # A recorded, empty allocation can be collected. Partial writes
                # without a ready full proof remain fenced, not guessed-owned.
                try:
                    claims.fingerprint_regular(carrier)
                except FileNotFoundError:
                    os.fsync(carrier.fd)
                    carrier.verify()
                else:
                    raise claims.PrivateClaimError(
                        "stage readiness proof missing; retained"
                    )
            else:
                claims.discard_private_regular(
                    guard, carrier, claims.FullFileFingerprint.from_value(expected)
                )
            _phase(operation, purpose, carrier, "discarded")
    record = _record(operation, purpose)
    assert record is not None
    with claims.ensure_namespace(
        guard, os.path.dirname(record.origin_path)
    ) as namespace:
        claims.gc_discarded_carrier(namespace, _binding(operation, purpose), record)


def _remove_publication(operation: Operation, guard: FileMutationGuard) -> None:
    record = _record(operation, "rollback")
    if record is not None and record.phase in {"discarding", "discarded"}:
        _discard(operation, guard, "rollback")
        return
    receipt_json = operation.row["publication_receipt_json"]
    if receipt_json is None:
        stage = _record(operation, "publication")
        if (
            stage is not None
            and stage.phase == "restoring"
            and os.path.lexists(operation.row["destination_path"])
        ):
            raise claims.PrivateClaimError("unacknowledged publication link; retained")
        return
    receipt = claims.LinkReceipt.from_value(_decode(receipt_json))
    if record is None:
        if fingerprint_path(receipt.destination_path) != receipt.fingerprint:
            raise claims.PrivateClaimError("publication changed; retained")
        _allocate(operation, guard, "rollback")
    with _open(operation, guard, "rollback") as carrier:
        try:
            captured = claims.fingerprint_regular(carrier)
        except FileNotFoundError:
            _phase(operation, "rollback", carrier, "claiming")
            claims.claim_into_empty(
                guard,
                carrier,
                carrier.namespace.parent_fd,
                os.path.basename(receipt.destination_path),
            )
            captured = claims.fingerprint_regular(carrier)
        if captured != receipt.fingerprint:
            raise claims.PrivateClaimError("rollback capture changed; retained")
        _phase(operation, "rollback", carrier, "claimed")
    _discard(operation, guard, "rollback")


def _restore_source(operation: Operation, guard: FileMutationGuard) -> None:
    record = _record(operation, "source")
    if record is None or record.phase in {"discarding", "discarded"}:
        if record is not None:
            _discard(operation, guard, "source")
        return
    with _open(operation, guard, "source") as carrier:
        if carrier.record.phase == "restored":
            receipt = carrier.record.restore_receipt
            if (
                receipt is None
                or receipt.destination_path != operation.row["source_path"]
                or fingerprint_path(receipt.destination_path) != receipt.fingerprint
            ):
                raise claims.PrivateClaimError(
                    "source restore receipt changed; retained"
                )
        else:
            try:
                actual = claims.fingerprint_regular(carrier)
            except FileNotFoundError:
                if record.phase not in {"allocated", "claiming"} or fingerprint_path(
                    record.origin_path
                ) != _fingerprint(operation, "source"):
                    raise claims.PrivateClaimError(
                        "source capture result is ambiguous; retained"
                    )
            else:
                captured = operation.fingerprints["captured_source"]
                if actual != _fingerprint(operation, "source"):
                    if captured is None and record.phase == "claiming":
                        # Preserve a post-capture race winner by restoring it,
                        # never by treating it as the expected original.
                        operation.fingerprints["captured_source"] = asdict(actual)
                        _store(operation)
                    elif (
                        captured is None
                        or claims.FullFileFingerprint.from_value(captured) != actual
                    ):
                        raise claims.PrivateClaimError(
                            "original source changed; retained"
                        )
                _phase(operation, "source", carrier, "restoring")
                receipt = claims.link_private_regular(
                    guard,
                    carrier,
                    carrier.namespace.parent_fd,
                    os.path.basename(record.origin_path),
                    actual,
                )
                _phase(operation, "source", carrier, "restored", receipt)
    _discard(operation, guard, "source")


def _settle(operation: Operation, guard: FileMutationGuard) -> str:
    if operation.row["state"] in {"completed", "rolled_back"}:
        return operation.row["state"]
    diagnostic = str(operation.row.get("diagnostic") or "")
    if diagnostic.startswith(("allocating:", "unrecorded allocation:")):
        purpose = diagnostic.split(":", 1)[1].split(";", 1)[0]
        if purpose not in operation.carriers or operation.carriers[purpose] is None:
            raise claims.PrivateClaimError(f"unrecorded allocation:{purpose}; retained")
    if operation.row["state"] == "db_committed":
        receipt_json = operation.row["publication_receipt_json"]
        if receipt_json is None:
            raise claims.PrivateClaimError("committed publication lacks receipt")
        receipt = claims.LinkReceipt.from_value(_decode(receipt_json))
        if fingerprint_path(receipt.destination_path) != receipt.fingerprint:
            raise claims.PrivateClaimError(
                "committed publication changed; original retained"
            )
        for purpose in ("source", "stage", "publication", "rollback"):
            _discard(operation, guard, purpose)
        _store(operation, state="completed")
        return "completed"
    _store(operation, state="rollback")
    try:
        _remove_publication(operation, guard)
    except Exception:
        if operation.row["source_path"] != operation.row["destination_path"]:
            _restore_source(operation, guard)
        raise
    _restore_source(operation, guard)
    _discard(operation, guard, "publication")
    _discard(operation, guard, "stage")
    _store(operation, state="rolled_back")
    return "rolled_back"


def _settle_safely(operation: Operation, guard: FileMutationGuard) -> str:
    # Reload durable authority: an in-memory receipt/allocation is not proof
    # that its transaction committed, including exceptions during DB exit.
    durable = load_operation(operation.row["id"])
    if durable is None:
        return "blocked"
    operation = durable
    try:
        return _settle(operation, guard)
    except Exception as exc:
        _store(operation, diagnostic=str(exc))
        return "blocked"


def _copy_prepared(
    operation: Operation, path: str, carrier: claims.CarrierHandle, purpose: str
) -> None:
    fd = os.open(
        "artifact",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=carrier.fd,
    )
    try:
        with open(path, "rb") as prepared:
            prepared_stat = os.fstat(prepared.fileno())
            while chunk := prepared.read(1024 * 1024):
                view = memoryview(chunk)
                while view:
                    written = os.write(fd, view)
                    if written == 0:
                        raise OSError(
                            errno.EIO, "rescan preparation made no write progress"
                        )
                    view = view[written:]
        try:
            os.fchown(fd, -1, prepared_stat.st_gid)
        except PermissionError:
            pass
        os.fchmod(fd, stat.S_IMODE(prepared_stat.st_mode))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(carrier.fd)
    operation.fingerprints[purpose] = asdict(claims.fingerprint_regular(carrier))
    _store(operation)


def enrich_target(target: _EnrichmentTarget, context: _EnrichmentContext) -> None:
    import rescan

    try:
        with file_mutation_guard(shared.DB_PATH) as guard:
            source = fingerprint_path(target.source_path)
            if (source.size, source.mtime_ns, source.dev, source.inode) != (
                target.source_fingerprint.size_bytes,
                target.source_fingerprint.mtime_ns,
                target.source_fingerprint.device,
                target.source_fingerprint.inode,
            ):
                return
            operation = None
            with tempfile.TemporaryDirectory(
                prefix=".mangarr-rescan-", dir=os.path.dirname(target.source_path)
            ) as scratch:
                staged = os.path.join(scratch, os.path.basename(target.source_path))
                shutil.copy2(target.source_path, staged)
                if fingerprint_path(staged).sha256 != source.sha256:
                    return
                destination = target.source_path
                if rescan.detect_file_type_magic(staged) == "cbr":
                    converted = rescan.convert_cbr_to_cbz(staged)
                    if not converted:
                        return
                    staged = converted
                    destination = os.path.splitext(target.source_path)[0] + ".cbz"
                    if destination == target.source_path:
                        return
                xml = rescan.build_comicinfo_xml(
                    context.series,
                    volume_num=target.volume_num,
                    tags=list(context.tags),
                )
                if (
                    not rescan.inject_comicinfo(staged, xml)
                    or fingerprint_path(target.source_path) != source
                ):
                    return
                operation = _reserve(target, context, source, destination)
                if operation is None:
                    return
                try:
                    _allocate(operation, guard, "stage")
                    with _open(operation, guard, "stage") as carrier:
                        _copy_prepared(operation, staged, carrier, "stage")
                except Exception:
                    _settle_safely(operation, guard)
                    raise
            # Scratch has been removed before any public name is captured.
            assert operation is not None
            try:
                if fingerprint_path(
                    target.source_path
                ) != source or not rescan._enrichment_context_is_current(
                    target, context
                ):
                    return
                _capture_source(operation, guard)
                _publish(operation, guard)
                _commit(operation, guard)
            finally:
                _settle_safely(operation, guard)
    except FileMutationLockError:
        return


def replay_rescan_file_operation(operation_id: int) -> str:
    try:
        with file_mutation_guard(shared.DB_PATH) as guard:
            operation = load_operation(operation_id)
            return "missing" if operation is None else _settle_safely(operation, guard)
    except (FileMutationLockError, claims.PrivateClaimError, ValueError):
        return "blocked"


def active_operation_ids(
    *, after_id: int = 0, limit: int = 100, series_id: int | None = None
) -> list[int]:
    if after_id < 0 or not 1 <= limit <= 1000:
        raise ValueError("rescan replay requires a nonnegative cursor and 1..1000 rows")
    with get_db() as db:
        rows = db.execute(
            f"SELECT id FROM rescan_file_operations WHERE state IN {ACTIVE_STATES} AND id>? "
            "AND (? IS NULL OR series_id=?) ORDER BY id LIMIT ?",
            (after_id, series_id, series_id, limit),
        ).fetchall()
        return [int(row["id"]) for row in rows]


def recover_series(series_id: int) -> bool:
    """Replay one series before inventory; true also suppresses immediate reenrichment."""
    after = 0
    recovered = False
    while ids := active_operation_ids(after_id=after, series_id=series_id):
        recovered = True
        for operation_id in ids:
            if rescan_cancel_requested():
                return recovered
            replay_rescan_file_operation(operation_id)
        after = ids[-1]
    return recovered


async def replay_rescan_file_operations(
    *, after_id: int = 0, limit: int = 100
) -> dict[str, Any]:
    ids = active_operation_ids(after_id=after_id, limit=limit)
    outcomes: dict[str, int] = {}
    for operation_id in ids:
        owned = asyncio.create_task(
            asyncio.to_thread(replay_rescan_file_operation, operation_id)
        )
        outcome = await _await_owned(owned)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return {
        "last_id": ids[-1] if ids else after_id,
        "selected": len(ids),
        "outcomes": outcomes,
    }


async def _await_owned(
    owned: asyncio.Task[Any], cancellation: threading.Event | None = None
) -> Any:
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(owned)
            break
        except asyncio.CancelledError:
            cancelled = True
            if cancellation is not None:
                cancellation.set()
            if owned.cancelled():
                raise
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _rescan_work_in_thread(
    worker: Callable[..., _Result], *args: Any, **kwargs: Any
) -> _Result:
    """Cancellation waits for this thread's owned journal/FS unit to settle."""
    cancellation = threading.Event()

    def run() -> _Result:
        token = _rescan_cancellation.set(cancellation)
        try:
            return worker(*args, **kwargs)
        finally:
            _rescan_cancellation.reset(token)

    return await _await_owned(asyncio.create_task(asyncio.to_thread(run)), cancellation)


async def rescan_series_in_thread(
    series_id: int, worker: Callable[[int], RescanResult]
) -> RescanResult:
    return await _rescan_work_in_thread(worker, series_id)


async def adopt_folder_in_thread(
    worker: Callable[..., AdoptUnmappedFolderResult], *args: Any, **kwargs: Any
) -> AdoptUnmappedFolderResult:
    return await _rescan_work_in_thread(worker, *args, **kwargs)


async def drain_active_rescan_file_operations(
    *, page_size: int = 100
) -> dict[str, int]:
    after = 0
    outcomes: dict[str, int] = {}
    while True:
        page = await replay_rescan_file_operations(after_id=after, limit=page_size)
        for key, count in page["outcomes"].items():
            outcomes[key] = outcomes.get(key, 0) + count
        if page["selected"] < page_size:
            return outcomes
        after = page["last_id"]
