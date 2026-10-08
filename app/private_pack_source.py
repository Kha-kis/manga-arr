"""Borrowed, snapshot-bound admission for generated import-pack sources."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from typing import Any, cast

import import_pack_cleanup as packs
import import_pipeline
import private_file_claim as claims
from download_identity import DownloadIdentity, DownloadProtocol, download_identity_key
from file_mutation_lock import FileMutationGuard
from private_file_claim import FullFileFingerprint
from private_pack_claim import (
    DIR_FLAGS,
    PackOwnership,
    PackProofError,
    _pairs,
    fingerprint_at,
    verify_directory,
)
from shared import get_db

_BINDING_KEYS = {
    "kind",
    "file_id",
    "src_path",
    "queue_id",
    "download_identity_key",
    "download_client_id",
    "download_protocol",
    "download_id",
}
_PACK_KEYS = _BINDING_KEYS | {
    "logical_pack_path",
    "artifact_owner_token",
    "relative_path",
    "ownership_json",
    "source_fingerprint",
}


def _positive(value: object) -> int:
    if type(value) is not int or cast(int, value) <= 0:
        raise PackProofError("invalid pack source row identity")
    return cast(int, value)


def _text(value: object, *, empty: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value or (not value and not empty):
        raise PackProofError("invalid pack source text")
    return value


def _path(value: object) -> str:
    path = _text(value)
    if not os.path.isabs(path) or os.path.abspath(path) != path:
        raise PackProofError("invalid pack source path")
    return path


def _binding(queue: Mapping[str, Any], file_id: int, src_path: str) -> dict[str, Any]:
    owner = queue.get("download_client_id")
    if owner is not None:
        owner = _positive(owner)
    protocol = queue.get("download_protocol")
    if protocol not in (None, "torrent", "nzb"):
        raise PackProofError("invalid pack source protocol")
    raw_download_id = queue.get("download_id")
    download_id = (
        _text(raw_download_id, empty=True) if raw_download_id is not None else None
    )
    identity = DownloadIdentity(
        owner, cast(DownloadProtocol | None, protocol), download_id or ""
    )
    return {
        "file_id": _positive(file_id),
        "src_path": _path(src_path),
        "queue_id": _positive(queue.get("id")),
        "download_identity_key": download_identity_key(identity),
        "download_client_id": owner,
        "download_protocol": protocol,
        "download_id": download_id,
    }


def _under(path: str, root: str) -> bool:
    return os.path.commonpath((path, os.path.abspath(root))) == os.path.abspath(root)


def _pack_origin(
    queue: Mapping[str, Any], file_id: int, src_path: str, origin: Mapping[str, Any]
) -> PackOwnership | None:
    binding = _binding(queue, file_id, src_path)
    kind = origin.get("kind")
    if kind not in ("pack", "nonpack") or set(origin) != (
        _PACK_KEYS if kind == "pack" else _BINDING_KEYS
    ):
        raise PackProofError("invalid pack source origin fields")
    if any(
        origin[key] != value or type(origin[key]) is not type(value)
        for key, value in binding.items()
    ):
        raise PackProofError("pack source origin binding changed")
    snapshot = queue.get("_pack_source_origins")
    if (
        not isinstance(snapshot, dict)
        or set(snapshot) != {"version", "files"}
        or type(snapshot["version"]) is not int
        or snapshot["version"] != 1
    ):
        raise PackProofError("pack source admission snapshot is missing or invalid")
    files = snapshot["files"]
    if not isinstance(files, dict) or files.get(str(file_id)) != dict(origin):
        raise PackProofError("pack source origin is not the persisted file admission")
    if kind == "nonpack":
        if _under(src_path, import_pipeline.PACK_STAGING_ROOT):
            raise PackProofError("pack-domain source cannot be classified as nonpack")
        return None
    try:
        ownership = PackOwnership.from_json(_text(origin["ownership_json"]))
        fingerprint = FullFileFingerprint.from_value(origin["source_fingerprint"])
    except claims.PrivateClaimError as exc:
        raise PackProofError("invalid admitted pack source fingerprint") from exc
    directory = ownership.canonical_directory
    relative = _text(origin["relative_path"])
    if (
        ownership.identity_key != binding["download_identity_key"]
        or ownership.phase != "attached"
        or ownership.artifact_owner_token != _text(origin["artifact_owner_token"])
        or directory is None
        or directory.marker_fingerprint is None
        or ownership.inventory is None
        or ownership.inventory.files.get(relative) != fingerprint
        or os.path.isabs(relative)
        or any(part in ("", ".", "..") for part in relative.split("/"))
        or os.path.join(directory.path, relative) != src_path
    ):
        raise PackProofError("pack source inventory or original ownership mismatch")
    _path(origin["logical_pack_path"])
    return ownership


def _current(origin: Mapping[str, Any], *, forward: bool) -> PackOwnership:
    reservation = packs._read_reservation(_text(origin["download_identity_key"]))
    if reservation is None or reservation.queue_id != origin["queue_id"]:
        raise PackProofError("pack source current reservation is missing")
    ownership = packs._ownership(reservation)
    admitted = PackOwnership.from_json(_text(origin["ownership_json"]))
    if (
        ownership is None
        or reservation.pack_path != origin["logical_pack_path"]
        or reservation.download_client_id != origin["download_client_id"]
        or reservation.protocol != origin["download_protocol"]
        or reservation.download_id != origin["download_id"]
        or ownership.artifact_owner_token != admitted.artifact_owner_token
        or ownership.canonical_directory != admitted.canonical_directory
        or ownership.inventory != admitted.inventory
        or ownership.placement_carrier != admitted.placement_carrier
    ):
        raise PackProofError("pack source current authority changed")
    if forward and (reservation.purpose != "queueing" or ownership.phase != "attached"):
        raise PackProofError("provisional pack source remains consumer-fenced")
    if not forward and ownership.phase not in ("attached", "cleaning"):
        raise PackProofError("pack source has no current terminal authority")
    return ownership


@contextmanager
def _directory(
    guard: FileMutationGuard, ownership: PackOwnership
) -> Iterator[tuple[int, Callable[[], None]]]:
    guard.verify()
    proof = ownership.canonical_directory
    if proof is None or proof.marker_fingerprint is None:
        raise PackProofError("pack source lacks directory ownership evidence")
    with ExitStack() as stack:
        carrier = ownership.placement_carrier
        verify_carrier: Callable[[], None] | None = None
        if carrier is not None and proof.path == os.path.join(
            carrier.carrier_path, "artifact"
        ):
            parent = os.path.dirname(os.path.dirname(carrier.carrier_path))
            with get_db() as db:
                row = db.execute(
                    "SELECT ownership_json FROM file_claim_namespaces WHERE parent_path=?",
                    (parent,),
                ).fetchone()
            if row is None or row[0] is None:
                raise PackProofError(
                    "pack source private namespace has no durable proof"
                )
            namespace = stack.enter_context(
                claims.open_namespace(guard, parent, str(row[0]))
            )
            handle = stack.enter_context(
                claims.open_carrier(namespace, carrier.binding, carrier)
            )
            verify_carrier = handle.verify
            fd = os.open("artifact", DIR_FLAGS, dir_fd=handle.fd)
        else:
            fd = os.open(proof.path, DIR_FLAGS)
        stack.callback(os.close, fd)

        def verify() -> None:
            guard.verify()
            if verify_carrier is not None:
                verify_carrier()
            verify_directory(fd, proof)

        verify()
        yield fd, verify
        verify()


def _entry(stack: ExitStack, directory: int, relative: str) -> tuple[int, str]:
    parts = relative.split("/")
    current = directory
    for part in parts[:-1]:
        current = os.open(part, DIR_FLAGS, dir_fd=current)
        stack.callback(os.close, current)
    return current, parts[-1]


def _metadata(info: os.stat_result) -> tuple[int, int, int, int]:
    if not stat.S_ISREG(info.st_mode):
        raise PackProofError("pack source is not a regular file")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _full_fd(fd: int) -> FullFileFingerprint:
    before = _metadata(os.fstat(fd))
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while chunk := os.read(fd, 1024 * 1024):
        digest.update(chunk)
    if _metadata(os.fstat(fd)) != before:
        raise PackProofError("pack source changed during pinned read")
    os.lseek(fd, 0, os.SEEK_SET)
    return FullFileFingerprint(*before, digest.hexdigest())


def _freeze_pack_file_origin(
    guard: FileMutationGuard,
    queue_snapshot: Mapping[str, Any],
    file_id: int,
    src_path: str,
) -> dict[str, Any]:
    guard.verify()
    binding = _binding(queue_snapshot, file_id, src_path)
    with get_db() as db:
        queue = db.execute(
            "SELECT * FROM import_queue WHERE id=?", (binding["queue_id"],)
        ).fetchone()
        file = db.execute(
            "SELECT queue_id,src_path FROM import_queue_files WHERE id=?", (file_id,)
        ).fetchone()
    if (
        queue is None
        or file is None
        or tuple(file) != (binding["queue_id"], src_path)
        or _binding(dict(queue), file_id, src_path) != binding
    ):
        raise PackProofError("pack source admission lost live queue binding")
    reservation = packs._read_reservation(binding["download_identity_key"])
    if not _under(src_path, import_pipeline.PACK_STAGING_ROOT):
        guard.verify()
        return {"kind": "nonpack", **binding}
    if reservation is None or reservation.queue_id != binding["queue_id"]:
        raise PackProofError("generated source has no exact ownership reservation")
    ownership = packs._ownership(reservation)
    if (
        ownership is None
        or ownership.canonical_directory is None
        or ownership.inventory is None
        or ownership.phase != "attached"
        or reservation.purpose != "queueing"
    ):
        raise PackProofError("generated source is unproved or provisional")
    relative = os.path.relpath(src_path, ownership.canonical_directory.path)
    expected = ownership.inventory.files.get(relative)
    if (
        expected is None
        or os.path.join(ownership.canonical_directory.path, relative) != src_path
    ):
        raise PackProofError("generated source lacks an exact inventory entry")
    try:
        with _directory(guard, ownership) as (fd, verify), ExitStack() as stack:
            parent, name = _entry(stack, fd, relative)
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if _metadata(info) != (
                expected.dev,
                expected.inode,
                expected.size,
                expected.mtime_ns,
            ):
                raise PackProofError(
                    "generated source identity changed before admission"
                )
            verify()
    except OSError as exc:
        raise PackProofError("generated source directory admission refused") from exc
    return {
        "kind": "pack",
        **binding,
        "logical_pack_path": reservation.pack_path,
        "artifact_owner_token": ownership.artifact_owner_token,
        "relative_path": relative,
        "ownership_json": ownership.to_json(),
        "source_fingerprint": asdict(expected),
    }


@contextmanager
def _open_pack_file_source(
    guard: FileMutationGuard,
    queue_snapshot: Mapping[str, Any],
    file_id: int,
    src_path: str,
    origin: Mapping[str, Any],
) -> Iterator[tuple[int, str, FullFileFingerprint] | None]:
    guard.verify()
    ownership = _pack_origin(queue_snapshot, file_id, src_path, origin)
    if ownership is None:
        yield None
        guard.verify()
        return
    _current(origin, forward=True)
    expected = FullFileFingerprint.from_value(origin["source_fingerprint"])
    try:
        with _directory(guard, ownership) as (directory, verify), ExitStack() as stack:
            parent, name = _entry(stack, directory, str(origin["relative_path"]))
            fd = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                dir_fd=parent,
            )
            stack.callback(os.close, fd)

            def verify_source() -> None:
                verify()
                if _full_fd(fd) != expected or fingerprint_at(parent, name) != expected:
                    raise PackProofError("admitted source fingerprint changed")
                _current(origin, forward=True)

            verify_source()
            yield fd, f"/proc/self/fd/{parent}/{name}", expected
            verify_source()
    except OSError as exc:
        raise PackProofError("admitted generated source read refused") from exc


def _pack_file_cleanup_delegated(
    guard: FileMutationGuard,
    queue_snapshot: Mapping[str, Any],
    file_id: int,
    src_path: str,
    origin: Mapping[str, Any],
    source_fingerprint: FullFileFingerprint,
    *,
    publication_id: int,
) -> bool:
    guard.verify()
    ownership = _pack_origin(queue_snapshot, file_id, src_path, origin)
    with get_db() as db:
        publication = db.execute(
            "SELECT queue_id,state,queue_snapshot_json,queue_download_id,"
            "queue_download_client_id,pack_cleanup_state FROM import_publications WHERE id=?",
            (_positive(publication_id),),
        ).fetchone()
        files = db.execute(
            "SELECT src_path,source_dev,source_inode,source_size,source_mtime_ns,"
            "source_sha256,source_claim_path FROM import_publication_files"
            " WHERE publication_id=? AND file_id=?",
            (publication_id, file_id),
        ).fetchall()
    if publication is None or len(files) != 1:
        raise PackProofError("pack source delegation lacks exact publication binding")
    try:
        stored = json.loads(
            publication["queue_snapshot_json"], object_pairs_hook=_pairs
        )
    except (ValueError, TypeError) as exc:
        raise PackProofError("invalid publication source origin snapshot") from exc
    if not isinstance(stored, dict):
        raise PackProofError("publication source origin snapshot is not an object")
    _pack_origin(stored, file_id, src_path, origin)
    if (
        publication["queue_id"] != origin["queue_id"]
        or publication["queue_download_id"] != origin["download_id"]
        or publication["queue_download_client_id"] != origin["download_client_id"]
        or publication["state"]
        not in ("db_committed", "cleaning", "finalized", "deleted")
        or files[0]["src_path"] != src_path
    ):
        raise PackProofError("pack source delegation publication authority mismatch")
    file = files[0]
    recorded = (
        file["source_dev"],
        file["source_inode"],
        file["source_size"],
        file["source_mtime_ns"],
        file["source_sha256"],
    )
    supplied = (
        source_fingerprint.dev,
        source_fingerprint.inode,
        source_fingerprint.size,
        source_fingerprint.mtime_ns,
        source_fingerprint.sha256,
    )
    if recorded != supplied:
        raise PackProofError("pack source delegation fingerprint mismatch")
    claim_path = file["source_claim_path"]
    if claim_path is not None and os.path.lexists(claim_path):
        raise PackProofError(
            "existing FILE source capture must recover before delegation"
        )
    if ownership is None:
        guard.verify()
        return False
    if source_fingerprint != FullFileFingerprint.from_value(
        origin["source_fingerprint"]
    ):
        raise PackProofError("generated delegation differs from admitted inventory")
    if (
        publication["state"] in ("finalized", "deleted")
        and publication["pack_cleanup_state"] != "pending"
    ):
        raise PackProofError("terminal pack disposition is not pending")
    current = _current(origin, forward=False)
    reservation = packs._read_reservation(origin["download_identity_key"])
    if reservation is None or reservation.publication_id not in (None, publication_id):
        raise PackProofError("pack terminal authority belongs to another publication")
    try:
        with _directory(guard, current) as (fd, verify), ExitStack() as stack:
            parent, name = _entry(stack, fd, str(origin["relative_path"]))
            if fingerprint_at(parent, name) != source_fingerprint:
                raise PackProofError(
                    "generated source changed before delegated disposition"
                )
            verify()
        _current(origin, forward=False)
    except OSError as exc:
        raise PackProofError("generated source delegation refused") from exc
    guard.verify()
    return True
