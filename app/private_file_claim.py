"""Owned private carriers for filesystems without RENAME_NOREPLACE.

One durable namespace per origin parent is reused. Application journals must
commit a carrier record BEFORE capture, and each action's intent before its
filesystem mutation. This module writes only the bounded namespace registry.
All callers borrow the same live synchronous guard for the configured local
DB. Participating actors respect that guard; hostile same-UID private-directory
tampering is outside the boundary. No shared/public source is link/unlinked.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import cast

from file_mutation_lock import FileMutationGuard
from shared import get_db

_ROOT = ".mangarr-claims"
_MARKER = "owner.json"
_ARTIFACT = "artifact"
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_PHASES = {
    "allocated",
    "claiming",
    "claimed",
    "restoring",
    "restored",
    "discarding",
    "discarded",
}


class PrivateClaimError(RuntimeError):
    """Ownership or captured-file evidence does not authorize this action."""


def _object(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise PrivateClaimError("invalid private claim fields")
    return cast(dict[str, object], value)


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise PrivateClaimError("invalid private claim string")
    return value


def _integer(value: object) -> int:
    if type(value) is not int or cast(int, value) < 0:
        raise PrivateClaimError("invalid private claim integer")
    return cast(int, value)


def _signed_integer(value: object) -> int:
    if type(value) is not int:
        raise PrivateClaimError("invalid private claim signed integer")
    return cast(int, value)


def _path(value: object) -> str:
    path = _text(value)
    if "\x00" in path or not os.path.isabs(path) or os.path.abspath(path) != path:
        raise PrivateClaimError("private claim path is not canonical absolute")
    return path


def _token(value: object) -> str:
    token = _text(value)
    if len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
        raise PrivateClaimError("invalid private claim owner token")
    return token


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PrivateClaimError("duplicate private claim JSON key")
        result[key] = value
    return result


def _load(encoded: str) -> object:
    try:
        return json.loads(encoded, object_pairs_hook=_pairs)
    except (ValueError, TypeError) as exc:
        raise PrivateClaimError("invalid private claim JSON") from exc


def _encode(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True, slots=True)
class FullFileFingerprint:
    dev: int
    inode: int
    size: int
    mtime_ns: int
    sha256: str

    @classmethod
    def from_value(cls, value: object) -> FullFileFingerprint:
        fields = _object(value, {"dev", "inode", "size", "mtime_ns", "sha256"})
        digest = _text(fields["sha256"])
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise PrivateClaimError("invalid private claim digest")
        return cls(
            _integer(fields["dev"]),
            _integer(fields["inode"]),
            _integer(fields["size"]),
            _signed_integer(fields["mtime_ns"]),
            digest,
        )


@dataclass(frozen=True, slots=True)
class ClaimBinding:
    domain: str
    operation_key: str
    file_id: int | None
    purpose: str

    @classmethod
    def from_value(cls, value: object) -> ClaimBinding:
        fields = _object(value, {"domain", "operation_key", "file_id", "purpose"})
        file_id = fields["file_id"]
        return cls(
            _text(fields["domain"]),
            _text(fields["operation_key"]),
            _integer(file_id) if file_id is not None else None,
            _text(fields["purpose"]),
        )


@dataclass(frozen=True, slots=True)
class LinkReceipt:
    destination_path: str
    fingerprint: FullFileFingerprint

    @classmethod
    def from_value(cls, value: object) -> LinkReceipt:
        fields = _object(value, {"destination_path", "fingerprint"})
        return cls(
            _path(fields["destination_path"]),
            FullFileFingerprint.from_value(fields["fingerprint"]),
        )


@dataclass(frozen=True, slots=True)
class CarrierRecord:
    version: int
    owner_token: str
    binding: ClaimBinding
    carrier_path: str
    dir_dev: int
    dir_ino: int
    marker_fingerprint: FullFileFingerprint
    origin_path: str
    phase: str
    artifact_fingerprint: FullFileFingerprint | None
    restore_receipt: LinkReceipt | None = None

    def to_json(self) -> str:
        return _encode(asdict(self))

    @classmethod
    def from_json(cls, encoded: str) -> CarrierRecord:
        fields = _object(
            _load(encoded),
            {
                "version",
                "owner_token",
                "binding",
                "carrier_path",
                "dir_dev",
                "dir_ino",
                "marker_fingerprint",
                "origin_path",
                "phase",
                "artifact_fingerprint",
                "restore_receipt",
            },
        )
        if type(fields["version"]) is not int or fields["version"] != 1:
            raise PrivateClaimError("unsupported private carrier version")
        phase = _text(fields["phase"])
        if phase not in _PHASES:
            raise PrivateClaimError("invalid private carrier phase")
        artifact = fields["artifact_fingerprint"]
        receipt = fields["restore_receipt"]
        return cls(
            1,
            _token(fields["owner_token"]),
            ClaimBinding.from_value(fields["binding"]),
            _path(fields["carrier_path"]),
            _integer(fields["dir_dev"]),
            _integer(fields["dir_ino"]),
            FullFileFingerprint.from_value(fields["marker_fingerprint"]),
            _path(fields["origin_path"]),
            phase,
            FullFileFingerprint.from_value(artifact) if artifact is not None else None,
            LinkReceipt.from_value(receipt) if receipt is not None else None,
        )


def _identity(st: os.stat_result) -> tuple[int, int]:
    return st.st_dev, st.st_ino


def _file_identity(st: os.stat_result) -> tuple[int, int, int, int]:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


def _private_dir(st: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(st.st_mode)
        or st.st_uid != os.geteuid()
        or stat.S_IMODE(st.st_mode) != 0o700
    ):
        raise PrivateClaimError("private claim directory is not owned 0700")


def _fresh_private_dir(fd: int) -> None:
    st = os.fstat(fd)
    if (
        not stat.S_ISDIR(st.st_mode)
        or st.st_uid != os.geteuid()
        or stat.S_IMODE(st.st_mode) not in (0o700, 0o2700)
    ):
        raise PrivateClaimError(
            "fresh private claim directory has unexpected ownership/mode"
        )
    # Linux inherits setgid from media parents. Normalize only our freshly
    # created private inode, never a user parent or an existing namespace.
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.fchmod(fd, 0o700)
    _private_dir(os.fstat(fd))


def _fingerprint_at(fd: int, name: str, *, marker: bool = False) -> FullFileFingerprint:
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=fd
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise PrivateClaimError("private claim artifact is not regular")
        if marker and (
            before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
        ):
            raise PrivateClaimError(
                "private claim marker is not owned 0600 single-link"
            )
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if (
            _file_identity(before) != _file_identity(after)
            or _file_identity(current) != _file_identity(after)
            or not stat.S_ISREG(current.st_mode)
        ):
            raise PrivateClaimError("private claim artifact changed while hashing")
        return FullFileFingerprint(*_file_identity(after), digest.hexdigest())
    finally:
        os.close(descriptor)


def _write_marker(fd: int, payload: dict[str, object]) -> FullFileFingerprint:
    descriptor = os.open(
        _MARKER,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=fd,
    )
    try:
        data = _encode(payload).encode("ascii")
        while data:
            written = os.write(descriptor, data)
            if written <= 0:
                raise OSError("private marker write made no progress")
            data = data[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return _fingerprint_at(fd, _MARKER, marker=True)


def _verify_marker(
    fd: int, expected: FullFileFingerprint, payload: dict[str, object]
) -> None:
    if _fingerprint_at(fd, _MARKER, marker=True) != expected:
        raise PrivateClaimError("private claim marker evidence changed")
    descriptor = os.open(_MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
    try:
        data = os.read(descriptor, 65537)
        if len(data) > 65536 or _load(data.decode("ascii")) != payload:
            raise PrivateClaimError("private claim marker binding changed")
    except UnicodeError as exc:
        raise PrivateClaimError("private claim marker is not ASCII") from exc
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class _NamespaceProof:
    owner_token: str
    parent_dev: int
    parent_ino: int
    root_path: str
    root_dev: int
    root_ino: int
    marker_fingerprint: FullFileFingerprint
    version: int = 1

    def marker_payload(self) -> dict[str, object]:
        return {
            "version": 1,
            "owner_token": self.owner_token,
            "parent_dev": self.parent_dev,
            "parent_ino": self.parent_ino,
            "root_path": self.root_path,
            "root_dev": self.root_dev,
            "root_ino": self.root_ino,
        }

    @classmethod
    def from_json(cls, encoded: str) -> _NamespaceProof:
        fields = _object(
            _load(encoded),
            {
                "version",
                "owner_token",
                "parent_dev",
                "parent_ino",
                "root_path",
                "root_dev",
                "root_ino",
                "marker_fingerprint",
            },
        )
        if type(fields["version"]) is not int or fields["version"] != 1:
            raise PrivateClaimError("unsupported private namespace version")
        return cls(
            _token(fields["owner_token"]),
            _integer(fields["parent_dev"]),
            _integer(fields["parent_ino"]),
            _path(fields["root_path"]),
            _integer(fields["root_dev"]),
            _integer(fields["root_ino"]),
            FullFileFingerprint.from_value(fields["marker_fingerprint"]),
        )


@dataclass(slots=True)
class NamespaceHandle:
    guard: FileMutationGuard
    fd: int
    parent_fd: int
    parent_path: str
    proof: _NamespaceProof
    _active: bool = True

    @property
    def path(self) -> str:
        return self.proof.root_path

    def verify(self) -> None:
        self.guard.verify()
        if not self._active:
            raise PrivateClaimError("closed private namespace handle")
        expected_parent = self.proof.parent_dev, self.proof.parent_ino
        if (
            _identity(os.fstat(self.parent_fd)) != expected_parent
            or _identity(os.lstat(self.parent_path)) != expected_parent
        ):
            raise PrivateClaimError("private namespace parent identity changed")
        held = os.fstat(self.fd)
        current = os.stat(_ROOT, dir_fd=self.parent_fd, follow_symlinks=False)
        _private_dir(held)
        _private_dir(current)
        if _identity(held) != (self.proof.root_dev, self.proof.root_ino) or _identity(
            current
        ) != _identity(held):
            raise PrivateClaimError("private namespace identity changed")
        _verify_marker(
            self.fd, self.proof.marker_fingerprint, self.proof.marker_payload()
        )


@contextmanager
def open_namespace(
    guard: FileMutationGuard, parent_path: str, ownership_json: str
) -> Generator[NamespaceHandle, None, None]:
    guard.verify()
    parent_path = _path(parent_path)
    proof = _NamespaceProof.from_json(ownership_json)
    if proof.root_path != os.path.join(parent_path, _ROOT):
        raise PrivateClaimError("private namespace belongs to another parent")
    parent_fd = os.open(parent_path, _DIR_FLAGS)
    handle: NamespaceHandle | None = None
    try:
        root_fd = os.open(_ROOT, _DIR_FLAGS, dir_fd=parent_fd)
        handle = NamespaceHandle(guard, root_fd, parent_fd, parent_path, proof)
        handle.verify()
        yield handle
    finally:
        if handle is not None:
            handle._active = False
            os.close(handle.fd)
        os.close(parent_fd)


@contextmanager
def ensure_namespace(
    guard: FileMutationGuard, parent_path: str
) -> Generator[NamespaceHandle, None, None]:
    """Ensure one namespace for the configured DB; no FS inside a SQL writer.

    A NULL intent with an existing root is a proof gap, not adoption authority.
    Creation failures retain that gap for explicit operator recovery.
    """
    guard.verify()
    parent_path = _path(parent_path)
    with get_db() as db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT ownership_json FROM file_claim_namespaces WHERE parent_path=?",
            (parent_path,),
        ).fetchone()
        if row is None:
            db.execute(
                "INSERT INTO file_claim_namespaces(parent_path) VALUES(?)",
                (parent_path,),
            )
        ownership = cast(str | None, row[0]) if row is not None else None
    guard.verify()
    if ownership is None:
        parent_fd = os.open(parent_path, _DIR_FLAGS)
        root_fd: int | None = None
        try:
            try:
                os.mkdir(_ROOT, 0o700, dir_fd=parent_fd)
            except FileExistsError as exc:
                raise PrivateClaimError(
                    "unproven private namespace already exists"
                ) from exc
            root_fd = os.open(_ROOT, _DIR_FLAGS, dir_fd=parent_fd)
            _fresh_private_dir(root_fd)
            parent = os.fstat(parent_fd)
            root = os.fstat(root_fd)
            payload: dict[str, object] = {
                "version": 1,
                "owner_token": secrets.token_hex(16),
                "parent_dev": parent.st_dev,
                "parent_ino": parent.st_ino,
                "root_path": os.path.join(parent_path, _ROOT),
                "root_dev": root.st_dev,
                "root_ino": root.st_ino,
            }
            marker = _write_marker(root_fd, payload)
            proof = _NamespaceProof(
                cast(str, payload["owner_token"]),
                parent.st_dev,
                parent.st_ino,
                cast(str, payload["root_path"]),
                root.st_dev,
                root.st_ino,
                marker,
            )
            handle = NamespaceHandle(guard, root_fd, parent_fd, parent_path, proof)
            handle.verify()
            os.fsync(root_fd)
            os.fsync(parent_fd)
            handle.verify()
            ownership = _encode(asdict(proof))
        finally:
            if root_fd is not None:
                os.close(root_fd)
            os.close(parent_fd)
        guard.verify()
        with get_db() as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute(
                "UPDATE file_claim_namespaces SET ownership_json=? WHERE parent_path=? AND ownership_json IS NULL",
                (ownership, parent_path),
            )
            if updated.rowcount != 1:
                raise PrivateClaimError("private namespace registry ownership changed")
    with open_namespace(guard, parent_path, ownership) as handle:
        yield handle


def _carrier_payload(record: CarrierRecord) -> dict[str, object]:
    return {
        "version": 1,
        "owner_token": record.owner_token,
        "binding": asdict(record.binding),
        "origin_path": record.origin_path,
        "dir_dev": record.dir_dev,
        "dir_ino": record.dir_ino,
        "artifact_fingerprint": asdict(record.artifact_fingerprint)
        if record.artifact_fingerprint
        else None,
    }


def _carrier_name(
    namespace: NamespaceHandle, binding: ClaimBinding, record: CarrierRecord
) -> str:
    validated = CarrierRecord.from_json(record.to_json())
    if validated.binding != binding or validated.carrier_path != os.path.join(
        namespace.path, validated.owner_token
    ):
        raise PrivateClaimError(
            "private carrier belongs to another operation/namespace"
        )
    if os.path.dirname(validated.origin_path) != namespace.parent_path:
        raise PrivateClaimError("private carrier origin belongs to another parent")
    return validated.owner_token


@dataclass(slots=True)
class CarrierHandle:
    namespace: NamespaceHandle
    fd: int
    record: CarrierRecord
    binding: ClaimBinding
    _active: bool = True

    @property
    def artifact_path(self) -> str:
        return os.path.join(self.record.carrier_path, _ARTIFACT)

    def verify(self) -> None:
        self.namespace.verify()
        if not self._active:
            raise PrivateClaimError("closed private carrier handle")
        name = _carrier_name(self.namespace, self.binding, self.record)
        held = os.fstat(self.fd)
        current = os.stat(name, dir_fd=self.namespace.fd, follow_symlinks=False)
        _private_dir(held)
        _private_dir(current)
        if _identity(held) != (self.record.dir_dev, self.record.dir_ino) or _identity(
            current
        ) != _identity(held):
            raise PrivateClaimError("private carrier identity changed")
        _verify_marker(
            self.fd, self.record.marker_fingerprint, _carrier_payload(self.record)
        )


@contextmanager
def allocate_carrier(
    namespace: NamespaceHandle,
    binding: ClaimBinding,
    origin_path: str,
    artifact_fingerprint: FullFileFingerprint | None = None,
) -> Generator[CarrierHandle, None, None]:
    namespace.verify()
    origin_path = _path(origin_path)
    if os.path.dirname(origin_path) != namespace.parent_path:
        raise PrivateClaimError("private carrier must share its origin parent")
    binding = ClaimBinding.from_value(asdict(binding))
    if artifact_fingerprint is not None:
        artifact_fingerprint = FullFileFingerprint.from_value(
            asdict(artifact_fingerprint)
        )
    token = secrets.token_hex(16)
    os.mkdir(token, 0o700, dir_fd=namespace.fd)
    fd = os.open(token, _DIR_FLAGS, dir_fd=namespace.fd)
    carrier: CarrierHandle | None = None
    try:
        _fresh_private_dir(fd)
        st = os.fstat(fd)
        payload: dict[str, object] = {
            "version": 1,
            "owner_token": token,
            "binding": asdict(binding),
            "origin_path": origin_path,
            "dir_dev": st.st_dev,
            "dir_ino": st.st_ino,
            "artifact_fingerprint": asdict(artifact_fingerprint)
            if artifact_fingerprint
            else None,
        }
        marker = _write_marker(fd, payload)
        record = CarrierRecord(
            1,
            token,
            binding,
            os.path.join(namespace.path, token),
            st.st_dev,
            st.st_ino,
            marker,
            origin_path,
            "allocated",
            artifact_fingerprint,
        )
        carrier = CarrierHandle(namespace, fd, record, binding)
        carrier.verify()
        os.fsync(fd)
        os.fsync(namespace.fd)
        carrier.verify()
        yield carrier
    finally:
        if carrier is not None:
            carrier._active = False
        os.close(fd)


@contextmanager
def open_carrier(
    namespace: NamespaceHandle, binding: ClaimBinding, record: CarrierRecord
) -> Generator[CarrierHandle, None, None]:
    namespace.verify()
    name = _carrier_name(namespace, binding, record)
    fd = os.open(name, _DIR_FLAGS, dir_fd=namespace.fd)
    carrier = CarrierHandle(namespace, fd, record, binding)
    try:
        carrier.verify()
        yield carrier
    finally:
        carrier._active = False
        os.close(fd)


def _action(guard: FileMutationGuard, carrier: CarrierHandle, phases: set[str]) -> None:
    if guard is not carrier.namespace.guard:
        raise PrivateClaimError("private claim action borrowed another guard")
    carrier.verify()
    if carrier.record.phase not in phases:
        raise PrivateClaimError("private claim action lacks durable intent")


def _child(name: str) -> None:
    if not name or name in (".", "..") or "/" in name or "\x00" in name:
        raise PrivateClaimError("private claim action requires one child name")


def claim_into_empty(
    guard: FileMutationGuard,
    carrier: CarrierHandle,
    source_parent_fd: int,
    source_name: str,
) -> None:
    _action(guard, carrier, {"claiming"})
    _child(source_name)
    if _identity(os.fstat(source_parent_fd)) != _identity(
        os.fstat(carrier.namespace.parent_fd)
    ) or source_name != os.path.basename(carrier.record.origin_path):
        raise PrivateClaimError("private capture source does not match durable origin")
    try:
        os.stat(_ARTIFACT, dir_fd=carrier.fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise PrivateClaimError("private capture child is occupied")
    # Emptiness is stable under the borrowed nonreentrant owner guard and the
    # fresh, owned private boundary; ordinary rename never targets public data.
    os.rename(
        source_name, _ARTIFACT, src_dir_fd=source_parent_fd, dst_dir_fd=carrier.fd
    )
    os.fsync(carrier.fd)
    os.fsync(source_parent_fd)
    carrier.verify()
    if (
        carrier.record.artifact_fingerprint is not None
        and fingerprint_regular(carrier) != carrier.record.artifact_fingerprint
    ):
        raise PrivateClaimError(
            "captured source differs from recorded full fingerprint; retained"
        )


def fingerprint_regular(carrier: CarrierHandle) -> FullFileFingerprint:
    carrier.verify()
    result = _fingerprint_at(carrier.fd, _ARTIFACT)
    carrier.verify()
    return result


def link_private_regular(
    guard: FileMutationGuard,
    carrier: CarrierHandle,
    destination_parent_fd: int,
    destination_name: str,
    expected: FullFileFingerprint,
) -> LinkReceipt:
    _action(guard, carrier, {"restoring"})
    _child(destination_name)
    if fingerprint_regular(carrier) != expected:
        raise PrivateClaimError("private restore fingerprint changed; retained")
    parent_path = _path(os.readlink(f"/proc/self/fd/{destination_parent_fd}"))
    parent_identity = _identity(os.fstat(destination_parent_fd))
    if _identity(os.lstat(parent_path)) != parent_identity:
        raise PrivateClaimError("private restore destination parent changed")
    # Every EEXIST refuses, even if the existing public name has the same inode.
    os.link(
        _ARTIFACT,
        destination_name,
        src_dir_fd=carrier.fd,
        dst_dir_fd=destination_parent_fd,
        follow_symlinks=False,
    )
    os.fsync(destination_parent_fd)
    os.fsync(carrier.fd)
    carrier.verify()
    if (
        _identity(os.lstat(parent_path)) != parent_identity
        or _fingerprint_at(destination_parent_fd, destination_name) != expected
    ):
        raise PrivateClaimError(
            "private restore destination changed after link; retained"
        )
    return LinkReceipt(os.path.join(parent_path, destination_name), expected)


def discard_private_regular(
    guard: FileMutationGuard, carrier: CarrierHandle, expected: FullFileFingerprint
) -> None:
    _action(guard, carrier, {"discarding"})
    try:
        actual = fingerprint_regular(carrier)
    except FileNotFoundError:
        # Only a durable discarding intent plus verified carrier authorizes this
        # crash window; missing markers/carriers are NOT swallowed here.
        carrier.verify()
    else:
        if actual != expected:
            raise PrivateClaimError("private discard fingerprint changed; retained")
        carrier.verify()
        os.unlink(_ARTIFACT, dir_fd=carrier.fd)
    os.fsync(carrier.fd)
    carrier.verify()


def gc_discarded_carrier(
    namespace: NamespaceHandle, binding: ClaimBinding, record: CarrierRecord
) -> None:
    namespace.verify()
    name = _carrier_name(namespace, binding, record)
    if record.phase != "discarded":
        raise PrivateClaimError("private carrier GC requires durable discarded")
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=namespace.fd)
    except FileNotFoundError:
        os.fsync(namespace.fd)
        namespace.verify()
        return
    try:
        held = os.fstat(fd)
        _private_dir(held)
        if _identity(held) != (record.dir_dev, record.dir_ino):
            raise PrivateClaimError("private GC carrier identity changed")
        entries = os.listdir(fd)
        if entries not in ([], [_MARKER]):
            raise PrivateClaimError(
                "private GC carrier contains undiscarded/unexpected children"
            )
        if entries:
            _verify_marker(fd, record.marker_fingerprint, _carrier_payload(record))
        namespace.verify()
        if _identity(
            os.stat(name, dir_fd=namespace.fd, follow_symlinks=False)
        ) != _identity(held):
            raise PrivateClaimError("private GC carrier path changed")
        if entries:
            os.unlink(_MARKER, dir_fd=fd)
            os.fsync(fd)
        os.rmdir(name, dir_fd=namespace.fd)
        os.fsync(namespace.fd)
        namespace.verify()
    finally:
        os.close(fd)
