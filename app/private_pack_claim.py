"""Pack-only directory proofs; shared carriers retain their exact FILE types."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import cast

from private_file_claim import CarrierRecord, FullFileFingerprint

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# Existing safe_join_under strips the leading dot from the legacy marker name.
OWNER_MARKER = "mangarr-pack-owner"


class PackProofError(OSError):
    """The recorded directory evidence does not authorize filesystem mutation."""


def _object(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise PackProofError("invalid pack proof fields")
    return cast(dict[str, object], value)


def _text(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise PackProofError("invalid pack proof text")
    return value


def _path(value: object, *, relative: bool = False) -> str:
    path = _text(value)
    if relative:
        if os.path.isabs(path) or any(
            part in ("", ".", "..") for part in path.split("/")
        ):
            raise PackProofError("invalid relative pack path")
    elif not os.path.isabs(path) or os.path.abspath(path) != path:
        raise PackProofError("invalid absolute pack path")
    return path


def _integer(value: object) -> int:
    if type(value) is not int or cast(int, value) < 0:
        raise PackProofError("invalid directory identity")
    return cast(int, value)


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PackProofError("duplicate pack proof key")
        result[key] = value
    return result


def _load(encoded: str, keys: set[str], *, version: int = 1) -> dict[str, object]:
    try:
        value = _object(
            json.loads(encoded, object_pairs_hook=_pairs), keys | {"version"}
        )
        if type(value["version"]) is not int or value["version"] != version:
            raise PackProofError("unsupported pack proof version")
        return value
    except (ValueError, TypeError) as exc:
        raise PackProofError("invalid pack proof JSON") from exc


@dataclass(frozen=True, slots=True)
class DirectoryProof:
    path: str
    dev: int
    inode: int
    marker_fingerprint: FullFileFingerprint | None = None

    @classmethod
    def from_value(cls, value: object) -> DirectoryProof:
        fields = _object(value, {"path", "dev", "inode", "marker_fingerprint"})
        marker = fields["marker_fingerprint"]
        return cls(
            _path(fields["path"]),
            _integer(fields["dev"]),
            _integer(fields["inode"]),
            FullFileFingerprint.from_value(marker) if marker is not None else None,
        )


@dataclass(frozen=True, slots=True)
class PackInventory:
    files: dict[str, FullFileFingerprint] = field(default_factory=dict)
    directories: tuple[str, ...] = ()

    @classmethod
    def from_value(cls, value: object) -> PackInventory:
        fields = _object(value, {"files", "directories"})
        if not isinstance(fields["files"], dict) or not isinstance(
            fields["directories"], list
        ):
            raise PackProofError("invalid pack inventory")
        files = {
            _path(k, relative=True): FullFileFingerprint.from_value(v)
            for k, v in fields["files"].items()
        }
        directories = tuple(_path(p, relative=True) for p in fields["directories"])
        if len(set(directories)) != len(directories) or set(files) & set(directories):
            raise PackProofError("conflicting pack inventory entries")
        for path in (*files, *directories):
            parent = os.path.dirname(path)
            if parent and parent not in directories:
                raise PackProofError("pack inventory lacks parent directory")
        return cls(files, directories)


@dataclass(frozen=True, slots=True)
class PackDirectoryClaim:
    carrier: CarrierRecord
    source_directory: DirectoryProof
    inventory: PackInventory
    artifact_owner_token: str | None
    version: int = 1

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, encoded: str) -> PackDirectoryClaim:
        value = _load(
            encoded,
            {"carrier", "source_directory", "inventory", "artifact_owner_token"},
        )
        carrier = CarrierRecord.from_json(json.dumps(value["carrier"]))
        source = DirectoryProof.from_value(value["source_directory"])
        owner = value["artifact_owner_token"]
        if (
            carrier.origin_path != source.path
            or carrier.artifact_fingerprint is not None
            or carrier.restore_receipt is not None
        ):
            raise PackProofError("directory carrier does not match pack proof")
        if (
            carrier.binding.domain != "pack"
            or carrier.binding.file_id is not None
            or carrier.binding.purpose != "pack_cleanup"
        ):
            raise PackProofError("directory carrier binding is not pack cleanup")
        return cls(
            carrier,
            source,
            PackInventory.from_value(value["inventory"]),
            _text(owner) if owner is not None else None,
        )


@dataclass(frozen=True, slots=True)
class PackOwnership:
    identity_key: str
    artifact_owner_token: str
    phase: str = "building"
    private_directory: DirectoryProof | None = None
    canonical_directory: DirectoryProof | None = None
    inventory: PackInventory | None = None
    claims: tuple[PackDirectoryClaim, ...] = ()
    retained_reason: str | None = None
    version: int = 1
    placement_carrier: CarrierRecord | None = None
    queue_receipt_sha256: str | None = None

    def to_json(self) -> str:
        value = asdict(self)
        if self.version < 3:
            if self.queue_receipt_sha256 is not None:
                raise PackProofError("legacy pack proof cannot encode queue receipt")
            value.pop("queue_receipt_sha256")
        if self.version == 1:
            if self.placement_carrier is not None:
                raise PackProofError(
                    "legacy pack proof cannot encode private placement"
                )
            value.pop("placement_carrier")
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, encoded: str) -> PackOwnership:
        try:
            raw = json.loads(encoded, object_pairs_hook=_pairs)
        except (ValueError, TypeError) as exc:
            raise PackProofError("invalid pack ownership JSON") from exc
        if (
            not isinstance(raw, dict)
            or type(raw.get("version")) is not int
            or raw["version"] not in (1, 2, 3)
        ):
            raise PackProofError("unsupported pack ownership version")
        version = cast(int, raw["version"])
        value = _load(
            encoded,
            {
                "identity_key",
                "artifact_owner_token",
                "phase",
                "private_directory",
                "canonical_directory",
                "inventory",
                "claims",
                "retained_reason",
            }
            | ({"placement_carrier"} if version >= 2 else set())
            | ({"queue_receipt_sha256"} if version == 3 else set()),
            version=version,
        )
        phase = _text(value["phase"])
        if phase not in {
            "building",
            "attaching",
            "ready",
            "queued",
            "attached",
            "cleaning",
            "retained",
        }:
            raise PackProofError("invalid pack ownership phase")
        if not isinstance(value["claims"], list):
            raise PackProofError("invalid pack claims")
        receipt = value.get("queue_receipt_sha256")
        if receipt is not None and (
            not isinstance(receipt, str)
            or len(receipt) != 64
            or any(char not in "0123456789abcdef" for char in receipt)
        ):
            raise PackProofError("invalid pack queue receipt")
        if (
            version == 3
            and phase in {"queued", "attached", "cleaning", "retained"}
            and receipt is None
        ):
            raise PackProofError("pack queue decision lacks its receipt")
        private, canonical, inventory, reason = (
            value[k]
            for k in (
                "private_directory",
                "canonical_directory",
                "inventory",
                "retained_reason",
            )
        )
        placement = value.get("placement_carrier")
        carrier = (
            CarrierRecord.from_json(json.dumps(placement))
            if placement is not None
            else None
        )
        if carrier is not None and (
            carrier.binding.domain != "pack"
            or carrier.binding.file_id is not None
            or carrier.binding.purpose != "pack_cleanup"
            or carrier.binding.operation_key != value["artifact_owner_token"]
            or carrier.artifact_fingerprint is not None
            or carrier.restore_receipt is not None
        ):
            raise PackProofError("invalid pack placement carrier binding")
        return cls(
            _text(value["identity_key"]),
            _text(value["artifact_owner_token"]),
            phase,
            DirectoryProof.from_value(private) if private is not None else None,
            DirectoryProof.from_value(canonical) if canonical is not None else None,
            PackInventory.from_value(inventory) if inventory is not None else None,
            tuple(PackDirectoryClaim.from_json(json.dumps(c)) for c in value["claims"]),
            _text(reason) if reason is not None else None,
            version,
            carrier,
            cast(str | None, receipt),
        )


@contextmanager
def open_directory(path: str) -> Iterator[int]:
    descriptor = os.open(path, DIR_FLAGS)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def fingerprint_at(fd: int, name: str) -> FullFileFingerprint:
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=fd
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise PackProofError("pack output is not a regular file")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=fd, follow_symlinks=False)
        evidence = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        if evidence(before) != evidence(after) or evidence(after) != evidence(current):
            raise PackProofError("pack file changed while hashing")
        return FullFileFingerprint(*evidence(after), digest.hexdigest())
    finally:
        os.close(descriptor)


def directory_proof(path: str, fd: int) -> DirectoryProof:
    info = os.fstat(fd)
    try:
        marker = fingerprint_at(fd, OWNER_MARKER)
    except FileNotFoundError:
        marker = None
    proof = DirectoryProof(os.path.abspath(path), info.st_dev, info.st_ino, marker)
    verify_directory(fd, proof)
    return proof


def verify_directory(
    fd: int,
    proof: DirectoryProof,
    *,
    path_binding: bool = True,
    allow_missing_marker: bool = False,
) -> None:
    held = os.fstat(fd)
    if not stat.S_ISDIR(held.st_mode) or (held.st_dev, held.st_ino) != (
        proof.dev,
        proof.inode,
    ):
        raise PackProofError("pack directory identity changed")
    if path_binding:
        current = os.lstat(proof.path)
        if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (
            proof.dev,
            proof.inode,
        ):
            raise PackProofError("pack directory path was replaced")
    try:
        actual = fingerprint_at(fd, OWNER_MARKER)
    except FileNotFoundError:
        actual = None
    if actual != proof.marker_fingerprint and not (
        allow_missing_marker and actual is None
    ):
        raise PackProofError("pack marker identity changed")


def inventory_tree(
    fd: int, *, checkpoint: Callable[[], None] | None = None, flush: bool = False
) -> PackInventory:
    files: dict[str, FullFileFingerprint] = {}
    directories: list[str] = []

    def walk(current: int, prefix: str) -> None:
        names = sorted(os.listdir(current))
        for name in names:
            if checkpoint is not None:
                checkpoint()
            info = os.stat(name, dir_fd=current, follow_symlinks=False)
            path = prefix + name
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, DIR_FLAGS, dir_fd=current)
                try:
                    if (os.fstat(child).st_dev, os.fstat(child).st_ino) != (
                        info.st_dev,
                        info.st_ino,
                    ):
                        raise PackProofError("pack child directory changed")
                    directories.append(path)
                    walk(child, path + "/")
                    after = os.stat(name, dir_fd=current, follow_symlinks=False)
                    if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
                        raise PackProofError("pack child name changed")
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                if prefix or name != OWNER_MARKER:
                    files[path] = fingerprint_at(current, name)
                if flush:
                    child = os.open(
                        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=current
                    )
                    try:
                        os.fsync(child)
                    finally:
                        os.close(child)
            else:
                raise PackProofError("pack contains a nonregular artifact")
        if names != sorted(os.listdir(current)):
            raise PackProofError("pack entries changed during inventory")
        if flush:
            os.fsync(current)

    walk(fd, "")
    return PackInventory(files, tuple(directories))


def verify_inventory(
    fd: int,
    expected: PackInventory,
    *,
    allowed_missing: frozenset[str] = frozenset(),
    discarding: bool = False,
    partial_attachment: bool = False,
) -> None:
    actual = inventory_tree(fd)
    if not set(actual.directories) <= set(expected.directories):
        raise PackProofError("pack contains unrecorded directory")
    if not (discarding or partial_attachment) and set(actual.directories) != set(
        expected.directories
    ):
        raise PackProofError("pack directory unexpectedly missing")
    if any(expected.files.get(name) != proof for name, proof in actual.files.items()):
        raise PackProofError("pack contains unrecorded or changed file")
    missing = set(expected.files) - set(actual.files)
    if not (discarding or partial_attachment) and not missing <= allowed_missing:
        raise PackProofError("pack file unexpectedly missing")
