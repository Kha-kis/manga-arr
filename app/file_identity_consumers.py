"""Disabled SQL-only reference inventory, not filesystem or recovery authority.

Presence describes persisted evidence, never a stat result. Owner-local roles
and raw dependency edges are not Task2 manifests and cannot enable a profile.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from typing import cast

from download_identity import (
    DownloadIdentity,
    DownloadProtocol,
    download_identity_key,
    download_identity_path_token,
    normalize_download_id,
)
from file_identity import IdentityMismatch, InvalidIdentity, OwnerRef, RoleKey
from file_identity_store import OwnerSnapshot, capture_owner_snapshot
from private_file_claim import (
    CarrierRecord,
    ClaimBinding,
    FullFileFingerprint,
    LinkReceipt,
    PrivateClaimError,
    _NamespaceProof,
)
from private_pack_claim import (
    DirectoryProof,
    PackDirectoryClaim,
    PackInventory,
    PackOwnership,
    PackProofError,
)

_LIMIT = 10_000
_JSON_LIMIT = 16 * 1024**2
_ROOTS = {
    "namespace": ("file_claim_namespaces", "parent_path"),
    "rescan": ("rescan_file_operations", "id"),
    "publication": ("import_publications", "id"),
    "deletion": ("volume_file_deletions", "id"),
    "pack_reservation": ("import_pack_cleanup_reservations", "download_identity_key"),
    "pack_tombstone": ("import_pack_cleanup_tombstones", "tombstone_path"),
    "queue_source": ("import_queue", "id"),
}
_TABLES = {table: pk for table, pk in _ROOTS.values()} | {
    "import_queue_files": "id",
    "import_publication_files": "id",
    "series": "id",
    "volumes": "id",
    "chapters": "id",
    "chapter_quarantine": "id",
    "root_folders": "id",
    "series_tags": "series_id",
}
_VOLUME_FIELDS = frozenset(
    "id series_id volume_num status import_path download_id download_client_id torrent_name indexer protocol client grabbed_at imported_at source_url release_group size_bytes quality".split()
)
_TERMINAL = frozenset({"imported", "failed", "skipped"})
_TERMINAL_PUBLICATIONS = frozenset({"finalized", "deleted"})
_INACTIVE_QUARANTINE = frozenset({"quarantined", "rolled_back", "restored"})
_ACTIVE_QUARANTINE = frozenset({"reserved", "captured", "committed", "restoring"})
_PUBLICATION_STATES = frozenset(
    {
        "staging",
        "prepared",
        "publishing",
        "published",
        "db_committed",
        "cleaning",
        "finalized",
        "deleted",
    }
)
SQLScalar = str | int | float | None


def _integer(value: object, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not minimum <= cast(int, value) <= maximum:
        raise InvalidIdentity("invalid inventory integer")
    return cast(int, value)


def _text(value: object, *, empty: bool = False) -> str:
    if type(value) is not str or (not empty and not value):
        raise InvalidIdentity("invalid inventory text")
    result = cast(str, value)
    if "\x00" in result or any(0xD800 <= ord(c) <= 0xDFFF for c in result):
        raise InvalidIdentity("invalid inventory text characters")
    return result


def _path(value: object) -> str:
    path = _text(value)
    if (
        not path.startswith("/")
        or "\\" in path
        or (path != "/" and any(p in {"", ".", ".."} for p in path[1:].split("/")))
    ):
        raise InvalidIdentity("inventory path is not canonical absolute POSIX")
    return path


def _relative(value: object) -> str:
    path = _text(value)
    if (
        path.startswith("/")
        or "\\" in path
        or any(p in {"", ".", ".."} for p in path.split("/"))
    ):
        raise InvalidIdentity("invalid relative inventory path")
    return path


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] or "/"


def _join(parent: str, relative: str) -> str:
    return parent.rstrip("/") + "/" + _relative(relative)


def _within(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def _hex(value: object, length: int = 32) -> str:
    result = _text(value)
    if len(result) != length or any(c not in "0123456789abcdef" for c in result):
        raise InvalidIdentity("invalid inventory hex value")
    return result


def _mapping(
    value: object, keys: set[str] | frozenset[str] | None = None
) -> dict[str, object]:
    if type(value) is not dict or (keys is not None and set(value) != keys):
        raise InvalidIdentity("invalid inventory object fields")
    return cast(dict[str, object], value)


def _list(value: object) -> list[object]:
    if type(value) is not list or len(cast(list[object], value)) > _LIMIT:
        raise InvalidIdentity("invalid or oversized inventory list")
    return cast(list[object], value)


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidIdentity("duplicate inventory JSON key")
        result[key] = value
    return result


def _finite(value: object, depth: int = 0) -> None:
    if depth > 32:
        raise InvalidIdentity("inventory JSON exceeds nesting bound")
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is str:
        _text(value, empty=True)
    elif type(value) is float:
        if not math.isfinite(cast(float, value)):
            raise InvalidIdentity("nonfinite inventory JSON")
    elif type(value) is dict:
        fields = _mapping(value)
        if len(fields) > _LIMIT:
            raise InvalidIdentity("inventory object exceeds bound")
        for key, item in fields.items():
            _text(key, empty=True)
            _finite(item, depth + 1)
    elif type(value) is list:
        for item in _list(value):
            _finite(item, depth + 1)
    else:
        raise InvalidIdentity("unsupported inventory JSON scalar")


def _load(value: object) -> object:
    text = _text(value)
    if len(text.encode("utf-8")) > _JSON_LIMIT:
        raise InvalidIdentity("inventory JSON exceeds byte bound")
    try:
        result: object = json.loads(text, object_pairs_hook=_pairs)
        _finite(result)
        return result
    except (ValueError, TypeError, RecursionError) as exc:
        raise InvalidIdentity("invalid inventory JSON") from exc


def _encode(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _owner_order(owner: OwnerRef) -> str:
    return _encode({"domain": owner.domain, "key": owner.key})


def _role_order(role: RoleSpec) -> tuple[str, str]:
    return _owner_order(role.owner), _encode(
        {
            "file_id": role.role.file_id,
            "purpose": role.role.purpose,
            "kind": role.role.kind,
            "relative": role.role.relative,
        }
    )


def _owner(owner: OwnerRef) -> None:
    if (
        type(owner) is not OwnerRef
        or owner.domain not in _ROOTS
        or type(owner.key) is not tuple
        or len(owner.key) != 1
    ):
        raise InvalidIdentity("invalid inventory owner")
    if owner.domain in {"namespace", "pack_tombstone"}:
        _path(owner.key[0])
    elif owner.domain == "pack_reservation":
        _text(owner.key[0])
    else:
        _integer(owner.key[0], 1)


@dataclass(frozen=True, slots=True)
class InventoryBudget:
    max_rows: int = 1000
    max_objects: int = 10000
    max_hash_bytes: int = 64 * 1024**3

    def __post_init__(self) -> None:
        _integer(self.max_rows, 1)
        _integer(self.max_objects, 1, _LIMIT)
        _integer(self.max_hash_bytes, 1)


@dataclass(frozen=True, slots=True)
class RoleSpec:
    role: RoleKey
    path: str
    parent_role: RoleKey | None
    legacy_json: str | None
    presence: str
    policy: str
    owner: OwnerRef

    def __post_init__(self) -> None:
        self._validate_fields()
        if self.legacy_json is not None:
            _load(self.legacy_json)

    def _validate_fields(self) -> None:
        _owner(self.owner)
        if type(self.role) is not RoleKey:
            raise InvalidIdentity("invalid inventory role")
        self.role.__post_init__()
        _path(self.path)
        if self.role.relative is not None:
            _relative(self.role.relative)
        if self.parent_role is not None:
            if (
                type(self.parent_role) is not RoleKey
                or self.parent_role.kind != "directory"
                or self.role.relative is None
            ):
                raise InvalidIdentity("invalid inventory role parent")
        elif self.role.relative is not None:
            raise InvalidIdentity("relative inventory role lacks parent")
        if _text(self.presence) not in {"proof", "expectation", "intent"}:
            raise InvalidIdentity("invalid inventory presence evidence")
        policies = {
            "file": {"file"},
            "marker": {"marker"},
            "directory": {"private_directory", "parent_identity", "exclusive_parent"},
        }
        if _text(self.policy) not in policies[self.role.kind]:
            raise InvalidIdentity("inventory role policy/kind mismatch")
        if self.presence == "proof" and self.legacy_json is None:
            raise InvalidIdentity("inventory proof lacks persisted evidence")


@dataclass(frozen=True, slots=True)
class RawRow:
    """Complete immutable operator SQL row, not an authority projection."""

    table: str
    key: tuple[str | int, ...]
    values: tuple[tuple[str, SQLScalar], ...]

    def __post_init__(self) -> None:
        _text(self.table)
        if (
            self.table not in _TABLES
            or type(self.key) is not tuple
            or not self.key
            or type(self.values) is not tuple
        ):
            raise InvalidIdentity("invalid inventory raw row")
        expected_key = (
            ("series_id", "tag")
            if self.table == "series_tags"
            else (_TABLES[self.table],)
        )
        if len(self.key) != len(expected_key):
            raise InvalidIdentity("invalid raw row key length")
        for column, value in zip(expected_key, self.key, strict=True):
            if column in {"parent_path", "tombstone_path"}:
                _path(value)
            elif column in {"download_identity_key", "tag"}:
                _text(value)
            else:
                _integer(value, 1)
        columns: list[str] = []
        for pair in self.values:
            if type(pair) is not tuple or len(pair) != 2:
                raise InvalidIdentity("invalid raw column")
            name, scalar = pair
            columns.append(_text(name))
            if scalar is not None and type(scalar) not in {str, int, float}:
                raise InvalidIdentity("unsupported SQL scalar")
            _finite(scalar)
        if (
            columns != sorted(set(columns))
            or tuple(dict(self.values).get(c) for c in expected_key) != self.key
        ):
            raise InvalidIdentity("invalid raw row key or column order")


@dataclass(frozen=True, slots=True)
class ReferenceDependency:
    owner: OwnerRef
    requires: OwnerRef
    reason: str

    def __post_init__(self) -> None:
        _owner(self.owner)
        _owner(self.requires)
        if self.owner == self.requires or _text(self.reason) not in {
            "queue",
            "publication",
            "namespace",
            "pack",
            "shared_context",
        }:
            raise InvalidIdentity("invalid inventory dependency")


@dataclass(frozen=True, slots=True)
class ReferenceClosure:
    snapshots: tuple[OwnerSnapshot, ...]
    roles: tuple[RoleSpec, ...]
    dependencies: tuple[ReferenceDependency, ...] = ()
    raw_rows: tuple[RawRow, ...] = ()

    def __post_init__(self) -> None:
        for records in (self.snapshots, self.roles, self.dependencies, self.raw_rows):
            if type(records) is not tuple or len(records) > _LIMIT:
                raise InvalidIdentity("invalid inventory closure collection")
        owners = [s.owner for s in self.snapshots if type(s) is OwnerSnapshot]
        if len(owners) != len(self.snapshots) or owners != sorted(
            set(owners), key=_owner_order
        ):
            raise InvalidIdentity("inventory snapshots duplicate or unordered")
        if any(type(r) is not RoleSpec or r.owner not in owners for r in self.roles):
            raise InvalidIdentity("inventory role lacks owner snapshot")
        if list(self.roles) != sorted(set(self.roles), key=_role_order) or len(
            {(r.owner, r.role) for r in self.roles}
        ) != len(self.roles):
            raise InvalidIdentity("inventory roles duplicate or unordered")
        by_role = {(r.owner, r.role): r for r in self.roles}
        for r in self.roles:
            if r.parent_role is not None:
                parent = by_role.get((r.owner, r.parent_role))
                if (
                    parent is None
                    or _join(parent.path, cast(str, r.role.relative)) != r.path
                ):
                    raise InvalidIdentity("inventory parent/path mismatch")
        if any(
            type(d) is not ReferenceDependency
            or d.owner not in owners
            or d.requires not in owners
            for d in self.dependencies
        ):
            raise InvalidIdentity("inventory dependency is not closed")
        if list(self.dependencies) != sorted(
            set(self.dependencies), key=_dependency_order
        ):
            raise InvalidIdentity("inventory dependencies duplicate or unordered")
        if any(type(r) is not RawRow for r in self.raw_rows) or list(
            self.raw_rows
        ) != sorted(set(self.raw_rows), key=_raw_order):
            raise InvalidIdentity("inventory raw rows duplicate or unordered")


def _dependency_order(value: ReferenceDependency) -> tuple[str, str, str]:
    return _owner_order(value.owner), _owner_order(value.requires), value.reason


def _raw_order(value: RawRow) -> tuple[str, str]:
    return value.table, _encode(value.key)


class _Evidence:
    """Bounded parse reuse owned by one invocation, never process-global."""

    def __init__(self) -> None:
        self.documents: dict[str, object] = {}
        self.packs: dict[str, PackOwnership] = {}
        self.carriers: dict[str, CarrierRecord] = {}
        self.namespaces: dict[str, _NamespaceProof] = {}
        self.claims: dict[str, PackDirectoryClaim] = {}
        self.snapshots: dict[
            OwnerSnapshot, tuple[dict[str, object], list[dict[str, object]]]
        ] = {}

    def load(self, value: object) -> object:
        encoded = _text(value)
        if encoded not in self.documents:
            if len(self.documents) >= _LIMIT:
                raise InvalidIdentity("inventory parsed-document budget exceeded")
            self.documents[encoded] = _load(encoded)
        return self.documents[encoded]

    def pack(self, encoded: str) -> PackOwnership:
        self.load(encoded)
        if encoded not in self.packs:
            self.packs[encoded] = PackOwnership.from_json(encoded)
        return self.packs[encoded]

    def carrier(self, encoded: object) -> CarrierRecord:
        text = _text(encoded)
        self.load(text)
        if text not in self.carriers:
            self.carriers[text] = _carrier(text)
        return self.carriers[text]

    def namespace(self, encoded: str) -> _NamespaceProof:
        self.load(encoded)
        if encoded not in self.namespaces:
            self.namespaces[encoded] = _NamespaceProof.from_json(encoded)
        return self.namespaces[encoded]

    def claim(self, encoded: str) -> PackDirectoryClaim:
        self.load(encoded)
        if encoded not in self.claims:
            self.claims[encoded] = PackDirectoryClaim.from_json(encoded)
        return self.claims[encoded]


class _Roles:
    def __init__(
        self,
        owner: OwnerRef,
        counter: _Counter | None = None,
        evidence: _Evidence | None = None,
    ) -> None:
        self.owner = owner
        self.counter = counter
        self.evidence = evidence if evidence is not None else _Evidence()
        self.values: dict[RoleKey, RoleSpec] = {}
        self.carriers: list[CarrierRecord] = []

    def add(
        self,
        purpose: str,
        path: str,
        kind: str,
        legacy: str | None = None,
        presence: str = "intent",
        policy: str | None = None,
        *,
        parent: RoleSpec | None = None,
        file_id: int | None = None,
    ) -> RoleSpec:
        path = _path(path)
        relative = None
        if parent is not None:
            if path == parent.path or not _within(path, parent.path):
                raise InvalidIdentity("inventory child escapes its parent")
            relative = path[len(parent.path.rstrip("/")) + 1 :]
        key = RoleKey(file_id, purpose, kind, relative)
        fields: dict[str, object] = dict(
            role=key,
            path=path,
            parent_role=parent.role if parent else None,
            legacy_json=legacy,
            presence=presence,
            policy=policy or ("parent_identity" if kind == "directory" else kind),
            owner=self.owner,
        )
        if key in self.values:
            existing = self.values[key]
            if any(getattr(existing, name) != value for name, value in fields.items()):
                raise InvalidIdentity("conflicting inventory role evidence")
            return existing
        if len(self.values) >= _LIMIT:
            raise InvalidIdentity("inventory object count exceeds bound")
        if self.counter is not None:
            self.counter.charge(objects=1)
        if legacy is not None:
            self.evidence.load(legacy)
        # Private factory: only the legacy parse is reused. All owner, role,
        # path, parent, presence and policy invariants run for every new role.
        result = object.__new__(RoleSpec)
        for name, value in fields.items():
            object.__setattr__(result, name, value)
        result._validate_fields()
        self.values[key] = result
        return result

    def carrier(
        self,
        record: CarrierRecord,
        legacy: str,
        purpose: str,
        binding: ClaimBinding,
        origins: set[str],
        *,
        directory: bool = False,
        artifact: bool = True,
    ) -> RoleSpec:
        record = self.evidence.carrier(_encode(asdict(record)))
        if record.binding != binding or record.origin_path not in origins:
            raise IdentityMismatch("carrier owner/file/purpose/origin mismatch")
        origin = _path(record.origin_path)
        expected = _join(_parent(origin), ".mangarr-claims/" + record.owner_token)
        if _path(record.carrier_path) != expected:
            raise IdentityMismatch("carrier path escapes origin namespace")
        self.carriers.append(record)
        if record.restore_receipt is not None:
            _path(record.restore_receipt.destination_path)
            if record.restore_receipt.destination_path not in origins:
                raise IdentityMismatch("carrier restore receipt destination mismatch")
        root = self.add(
            purpose + "_carrier",
            record.carrier_path,
            "directory",
            legacy,
            "proof",
            "private_directory",
            file_id=binding.file_id,
        )
        self.add(
            purpose + "_marker",
            _join(root.path, "owner.json"),
            "marker",
            legacy,
            "proof",
            parent=root,
            file_id=binding.file_id,
        )
        if not artifact:
            return root
        return self.add(
            purpose + "_artifact",
            _join(root.path, "artifact"),
            "directory" if directory else "file",
            legacy,
            "expectation"
            if record.phase not in {"allocated", "discarded", "restored"}
            else "intent",
            parent=root,
            file_id=binding.file_id,
        )


def _fingerprint(value: object) -> FullFileFingerprint:
    _finite(value)
    result = FullFileFingerprint.from_value(value)
    for value in (result.dev, result.inode, result.size):
        _integer(value)
    _integer(result.mtime_ns, -(2**63))
    return result


def _carrier(encoded: object) -> CarrierRecord:
    # Called only by _Evidence after strict, collection-local JSON validation.
    result = CarrierRecord.from_json(_text(encoded))
    _path(result.carrier_path)
    _path(result.origin_path)
    _integer(result.dir_dev)
    _integer(result.dir_ino)
    _fingerprint(asdict(result.marker_fingerprint))
    if result.artifact_fingerprint is not None:
        _fingerprint(asdict(result.artifact_fingerprint))
    if result.restore_receipt is not None:
        _fingerprint(asdict(result.restore_receipt.fingerprint))
    if result.binding.file_id is not None:
        _integer(result.binding.file_id, 1)
    return result


def _columns_fingerprint(
    row: dict[str, object], prefix: str
) -> FullFileFingerprint | None:
    fields = {
        k: row[prefix + k] for k in ("dev", "inode", "size", "mtime_ns", "sha256")
    }
    if all(v is None for v in fields.values()):
        return None
    return _fingerprint(fields)


def _directory_roles(
    roles: _Roles,
    purpose: str,
    proof: DirectoryProof,
    inventory: PackInventory | None,
    legacy: str,
) -> None:
    root = roles.add(purpose, _path(proof.path), "directory", legacy, "proof")
    _integer(proof.dev)
    _integer(proof.inode)
    if proof.marker_fingerprint is not None:
        _fingerprint(asdict(proof.marker_fingerprint))
        roles.add(
            purpose + "_marker",
            _join(proof.path, "mangarr-pack-owner"),
            "marker",
            legacy,
            "proof",
            parent=root,
        )
    if inventory is None:
        return
    if len(inventory.directories) + len(inventory.files) > _LIMIT:
        raise InvalidIdentity("pack inventory exceeds object bound")
    for relative in inventory.directories:
        roles.add(
            purpose + "_directory",
            _join(proof.path, relative),
            "directory",
            legacy,
            "expectation",
            parent=root,
        )
    for relative, fingerprint in inventory.files.items():
        _fingerprint(
            {
                "dev": fingerprint.dev,
                "inode": fingerprint.inode,
                "size": fingerprint.size,
                "mtime_ns": fingerprint.mtime_ns,
                "sha256": fingerprint.sha256,
            }
        )
        roles.add(
            purpose + "_file",
            _join(proof.path, relative),
            "file",
            legacy,
            "expectation",
            parent=root,
        )


def _legacy_pack_label(value: object) -> str:
    label = _text(value)
    if label in {".", ".."} or "/" in label or "\\" in label:
        raise InvalidIdentity("legacy pack label is not a single safe component")
    return label


def _legacy_cleanup_path(pack_path: str, path: str) -> bool:
    prefix = pack_path + ".cleanup-"
    if _parent(path) != _parent(pack_path) or not path.startswith(prefix):
        return False
    _legacy_pack_label(path[len(prefix) :])
    return True


def _private_creation_intent(
    row: dict[str, object], path: str, proof: PackOwnership | None
) -> bool:
    prefix = _path(row["pack_path"]) + ".owner-"
    if _parent(path) != _parent(_path(row["pack_path"])) or not path.startswith(prefix):
        return False
    label = _legacy_pack_label(path[len(prefix) :])
    # The lease may rotate without changing this original artifact label.
    if proof is None:
        return row["queue_id"] is None and row["publication_id"] is None
    if proof.artifact_owner_token != label or proof.claims:
        return False
    if proof.phase == "building":
        return (
            proof.canonical_directory is None
            and proof.inventory is None
            and (
                proof.private_directory is not None
                or proof.retained_reason == "creating-private"
            )
        )
    # The attachment CAS changes purpose before rename, retaining this intent
    # through ready/queued and a cancelled provisional queue decision.
    return (
        row["purpose"] == "cleanup"
        and proof.phase in {"attaching", "ready", "queued", "retained"}
        and proof.canonical_directory is not None
        and proof.inventory is not None
        and (proof.phase != "attaching" or proof.private_directory is not None)
    )


def _pack_paths(
    pack_path: str, identity: DownloadIdentity, token: str
) -> tuple[str, str]:
    parent = _parent(_path(pack_path))
    owner = (
        f"client-{identity.download_client_id}"
        if identity.download_client_id is not None
        else "client-legacy"
    )
    canonical = _join(
        parent,
        f"queue-{owner}-{identity.protocol or 'unknown'}-{download_identity_path_token(identity)}",
    )
    if canonical != pack_path:
        raise IdentityMismatch("pack logical path/identity mismatch")
    return canonical, _path(canonical + ".owner-" + _legacy_pack_label(token))


def _identity(
    row: dict[str, object], protocol_column: str = "protocol"
) -> DownloadIdentity:
    client = row["download_client_id"]
    if client is not None:
        _integer(client, 1)
    protocol = row[protocol_column]
    if protocol not in {None, "torrent", "nzb"}:
        raise InvalidIdentity("unknown download protocol")
    download = (
        _text(row["download_id"], empty=True) if row["download_id"] is not None else ""
    )
    return DownloadIdentity(
        cast(int | None, client), cast(DownloadProtocol | None, protocol), download
    )


def _pack_claim(
    roles: _Roles,
    claim: PackDirectoryClaim,
    legacy: str,
    purpose: str,
    pack_path: str,
    token: str | None = None,
) -> str:
    binding = claim.carrier.binding
    if (
        binding.domain != "pack"
        or binding.file_id is not None
        or binding.purpose != "pack_cleanup"
    ):
        raise IdentityMismatch("pack claim binding mismatch")
    if token is not None and (
        binding.operation_key != token
        or claim.artifact_owner_token not in {None, token}
    ):
        raise IdentityMismatch("pack claim artifact owner mismatch")
    if claim.artifact_owner_token is not None:
        _legacy_pack_label(claim.artifact_owner_token)
        if binding.operation_key != claim.artifact_owner_token:
            raise IdentityMismatch("pack claim origin owner mismatch")
    source = _path(claim.source_directory.path)
    if (
        source != pack_path
        and not (
            token is not None
            and source == pack_path + ".owner-" + _legacy_pack_label(token)
        )
        and not _legacy_cleanup_path(pack_path, source)
    ):
        raise IdentityMismatch("pack directory claim source escapes logical pack")
    record = (
        roles.evidence.carrier(
            _encode(_mapping(roles.evidence.load(legacy)).get("carrier"))
        )
        if roles.owner.domain == "pack_tombstone"
        else claim.carrier
    )
    artifact = roles.carrier(record, legacy, purpose, binding, {source}, directory=True)
    _directory_roles(
        roles, purpose + "_source", claim.source_directory, claim.inventory, legacy
    )
    # Inventory follows the claimed directory as well as the original label.
    _directory_roles(
        roles,
        purpose + "_captured",
        DirectoryProof(
            artifact.path,
            claim.source_directory.dev,
            claim.source_directory.inode,
            claim.source_directory.marker_fingerprint,
        ),
        claim.inventory,
        legacy,
    )
    return artifact.path


def _pack_ownership(
    roles: _Roles,
    encoded: str,
    pack_path: str,
    identity: DownloadIdentity,
    purpose: str,
) -> PackOwnership:
    proof = roles.evidence.pack(encoded)
    if proof.identity_key != download_identity_key(identity):
        raise IdentityMismatch("pack ownership identity differs")
    canonical, private = _pack_paths(pack_path, identity, proof.artifact_owner_token)
    physical = None
    if proof.placement_carrier is not None:
        record = proof.placement_carrier
        if record.binding != ClaimBinding(
            "pack", proof.artifact_owner_token, None, "pack_cleanup"
        ):
            raise IdentityMismatch("pack placement binding differs")
        artifact = roles.carrier(
            record,
            encoded,
            purpose + "_placement",
            record.binding,
            {private},
            directory=True,
        )
        physical = artifact.path
    for name, directory, allowed in (
        ("private", proof.private_directory, {private, physical}),
        ("canonical", proof.canonical_directory, {canonical, physical}),
    ):
        if directory is not None:
            if directory.path not in allowed:
                raise IdentityMismatch("pack proof directory path mismatch")
            _directory_roles(
                roles, purpose + "_" + name, directory, proof.inventory, encoded
            )
    if (
        proof.inventory is not None
        and proof.private_directory is None
        and proof.canonical_directory is None
    ):
        raise InvalidIdentity("pack inventory has no directory binding")
    for index, claim in enumerate(proof.claims):
        _pack_claim(
            roles,
            claim,
            encoded,
            purpose + f"_claim_{index}",
            canonical,
            proof.artifact_owner_token,
        )
    roles.add(purpose + "_logical", canonical, "directory", encoded, "intent")
    needs_canonical = proof.phase in {"ready", "queued", "attached"} or (
        proof.phase == "cleaning" and not proof.claims
    )
    if needs_canonical and (
        proof.canonical_directory is None or proof.inventory is None
    ):
        raise InvalidIdentity("pack phase lacks directory/inventory evidence")
    return proof


def _namespace_roles(roles: _Roles, row: dict[str, object]) -> None:
    parent = _path(row["parent_path"])
    legacy = _text(row["ownership_json"])
    proof = roles.evidence.namespace(legacy)
    if proof.root_path != _join(parent, ".mangarr-claims"):
        raise IdentityMismatch("namespace root/parent mismatch")
    root_parent = roles.add(
        "namespace_parent",
        parent,
        "directory",
        legacy,
        "proof",
        "exclusive_parent" if proof.version == 2 else "parent_identity",
    )
    root = roles.add(
        "namespace_root",
        proof.root_path,
        "directory",
        legacy,
        "proof",
        "private_directory",
        parent=root_parent,
    )
    roles.add(
        "namespace_marker",
        _join(root.path, "owner.json"),
        "marker",
        legacy,
        "proof",
        parent=root,
    )


def _rescan_roles(roles: _Roles, row: dict[str, object]) -> None:
    _integer(row["version"], 1, 1)
    if row["state"] not in {
        "prepared",
        "published",
        "db_committed",
        "rollback",
        "rolled_back",
        "completed",
    }:
        raise InvalidIdentity("unknown rescan phase")
    token = _hex(row["operation_token"])
    volume_id = _integer(row["volume_id"], 1)
    series_id = _integer(row["series_id"], 1)
    source, destination = _path(row["source_path"]), _path(row["destination_path"])
    if _parent(source) != _parent(destination):
        raise IdentityMismatch("rescan destination parent differs")
    volume = _mapping(roles.evidence.load(row["expected_volume_json"]), _VOLUME_FIELDS)
    context = _mapping(
        roles.evidence.load(row["expected_context_json"]), {"series", "tags"}
    )
    series = _mapping(context["series"])
    tags = _list(context["tags"])
    if (
        type(volume["id"]) is not int
        or volume["id"] != volume_id
        or type(volume["series_id"]) is not int
        or volume["series_id"] != series_id
        or type(series.get("id")) is not int
        or series["id"] != series_id
        or any(type(t) is not str for t in tags)
        or tags != sorted(set(cast(list[str], tags)))
    ):
        raise IdentityMismatch("rescan context owner mismatch")
    if volume["import_path"] not in {None, ""}:
        _path(volume["import_path"])
    fingerprints = _mapping(
        roles.evidence.load(row["fingerprints_json"]),
        {"source", "stage", "publication", "captured_source"},
    )
    parsed = {
        key: _fingerprint(value) if value is not None else None
        for key, value in fingerprints.items()
    }
    if parsed["source"] is None:
        raise InvalidIdentity("rescan source proof is missing")
    if (
        parsed["captured_source"] is not None
        and parsed["captured_source"] != parsed["source"]
    ):
        raise IdentityMismatch("rescan captured source differs")
    roles.add(
        "source",
        source,
        "file",
        _text(row["fingerprints_json"]),
        "expectation",
        file_id=volume_id,
    )
    roles.add(
        "destination",
        destination,
        "file",
        _text(row["fingerprints_json"]),
        "expectation" if parsed["publication"] else "intent",
        file_id=volume_id,
    )
    carriers = _mapping(
        roles.evidence.load(row["carriers_json"]),
        {"source", "stage", "publication", "rollback"},
    )
    for purpose, value in carriers.items():
        if value is None:
            continue
        record = roles.evidence.carrier(_encode(value))
        expected = (
            None
            if purpose in {"stage", "publication"}
            else parsed["source" if purpose == "source" else "publication"]
        )
        if record.artifact_fingerprint != expected:
            raise IdentityMismatch("rescan carrier fingerprint differs")
        roles.carrier(
            record,
            _text(row["carriers_json"]),
            purpose,
            ClaimBinding("rescan", token, volume_id, purpose),
            {source if purpose == "source" else destination},
        )
    if row["publication_receipt_json"] is not None:
        receipt = LinkReceipt.from_value(
            roles.evidence.load(row["publication_receipt_json"])
        )
        if (
            receipt.destination_path != destination
            or receipt.fingerprint != parsed["publication"]
        ):
            raise IdentityMismatch("rescan publication receipt differs")


def _deletion_roles(roles: _Roles, row: dict[str, object]) -> None:
    if row["state"] not in {"active", "completed"}:
        raise InvalidIdentity("unknown deletion phase")
    volume_id = _integer(row["volume_id"], 1)
    _integer(row["series_id"], 1)
    present = _integer(row["target_present"], 0, 1)
    target = _text(row["target_path"], empty=True)
    fingerprint = _columns_fingerprint(row, "target_")
    if (fingerprint is not None) != bool(present):
        raise InvalidIdentity("deletion target expectation is incomplete")
    if not target:
        if row["claim_path"] or present or row["claim_carrier_json"] is not None:
            raise InvalidIdentity("pathless deletion has artifact evidence")
        return
    target = _path(target)
    parent, claim = _path(row["parent_path"]), _path(row["claim_path"])
    digest = hashlib.sha256(target.encode("utf-8")).hexdigest()[:20]
    if _parent(target) != parent or claim != _join(
        parent, f".mangarr-volume-delete-{volume_id}-{digest}.tombstone"
    ):
        raise IdentityMismatch("deletion target/claim binding differs")
    roles.add(
        "target",
        target,
        "file",
        None,
        "expectation" if present else "intent",
        file_id=volume_id,
    )
    roles.add("claim", claim, "file", None, "intent", file_id=volume_id)
    if row["original_import_path"] not in {None, ""}:
        _path(row["original_import_path"])
    if row["claim_carrier_json"] is not None:
        legacy = _text(row["claim_carrier_json"])
        record = roles.evidence.carrier(legacy)
        if (
            not present
            or record.artifact_fingerprint is None
            or (
                record.origin_path == target
                and record.artifact_fingerprint != fingerprint
            )
        ):
            raise IdentityMismatch("deletion carrier target fingerprint differs")
        roles.carrier(
            record,
            legacy,
            "delete",
            ClaimBinding("deletion", str(row["id"]), volume_id, "delete"),
            {target, claim},
        )


def _publication_staging(
    roles: _Roles, row: dict[str, object], queue: dict[str, object], staging: str
) -> RoleSpec:
    value = queue.get("_publication_staging")
    if value is None:
        suffix = hashlib.sha256(_text(row["owner_token"]).encode()).hexdigest()[:32]
        origin = _join(_path(row["dst_dir"]), f".mangarr-publication-{row['queue_id']}")
        if staging not in {origin, origin + "-" + suffix}:
            raise IdentityMismatch("legacy staging path differs from producer intent")
        return roles.add("staging", staging, "directory")
    record = _mapping(value, {"version", "binding", "origin_path", "phase", "carrier"})
    _integer(record["version"], 1, 1)
    phase = record["phase"]
    if phase not in {"allocating", "ready", "discarding", "discarded"}:
        raise InvalidIdentity("unknown publication staging phase")
    binding = ClaimBinding("import_publication", str(row["id"]), None, "staging")
    if ClaimBinding.from_value(record["binding"]) != binding:
        raise IdentityMismatch("publication stage owner mismatch")
    suffix = hashlib.sha256(_text(row["owner_token"]).encode()).hexdigest()[:32]
    origin = _join(
        _path(row["dst_dir"]), f".mangarr-publication-{row['queue_id']}-{suffix}"
    )
    if _path(record["origin_path"]) != origin:
        raise IdentityMismatch("publication stage origin mismatch")
    legacy = _text(row["queue_snapshot_json"])
    if record["carrier"] is None:
        if phase != "allocating" or staging != origin:
            raise InvalidIdentity("staging phase lacks its carrier")
        return roles.add("staging", staging, "directory", legacy, "intent")
    carrier = roles.evidence.carrier(_encode(record["carrier"]))
    expected_phase = {
        "ready": "allocated",
        "discarding": "discarding",
        "discarded": "discarded",
    }.get(cast(str, phase))
    if (
        carrier.carrier_path != staging
        or carrier.phase != expected_phase
        or carrier.artifact_fingerprint is not None
        or carrier.restore_receipt is not None
    ):
        raise IdentityMismatch("publication staging carrier phase differs")
    return roles.carrier(
        carrier, legacy, "staging", binding, {origin}, directory=True, artifact=False
    )


def _origin_roles(
    roles: _Roles,
    queue: dict[str, object],
    files: list[dict[str, object]],
    prefix: str,
) -> None:
    if "_pack_source_origins" not in queue:
        return
    snapshot = _mapping(queue["_pack_source_origins"], {"version", "files"})
    _integer(snapshot["version"], 1, 1)
    origins = _mapping(snapshot["files"])
    by_id = {
        str(_integer(f["file_id"] if "file_id" in f else f["id"], 1)): f for f in files
    }
    if set(origins) != set(by_id):
        raise IdentityMismatch("frozen source origin file set differs")
    identity = _identity(queue, "download_protocol")
    keys = {
        "kind",
        "file_id",
        "src_path",
        "queue_id",
        "download_identity_key",
        "download_client_id",
        "download_protocol",
        "download_id",
    }
    pack_keys = {
        "logical_pack_path",
        "artifact_owner_token",
        "relative_path",
        "ownership_json",
        "source_fingerprint",
    }
    for file_id, raw in origins.items():
        origin = _mapping(raw)
        kind = origin.get("kind")
        if kind not in {"pack", "nonpack"} or set(origin) != (
            keys | pack_keys if kind == "pack" else keys
        ):
            raise InvalidIdentity("unknown source origin kind or fields")
        file = by_id[file_id]
        expected: dict[str, object] = {
            "file_id": int(file_id),
            "src_path": _path(file["src_path"]),
            "queue_id": _integer(queue["id"], 1),
            "download_identity_key": download_identity_key(identity),
            "download_client_id": identity.download_client_id,
            "download_protocol": identity.protocol,
            "download_id": queue["download_id"],
        }
        if any(
            type(origin[k]) is not type(v) or origin[k] != v
            for k, v in expected.items()
        ):
            raise IdentityMismatch("source origin owner/file/download binding differs")
        if kind == "nonpack":
            continue
        logical = _path(origin["logical_pack_path"])
        proof = _pack_ownership(
            roles,
            _text(origin["ownership_json"]),
            logical,
            identity,
            prefix + "_" + file_id,
        )
        fingerprint = _fingerprint(origin["source_fingerprint"])
        relative = _relative(origin["relative_path"])
        directory = proof.canonical_directory
        if (
            proof.phase != "attached"
            or proof.artifact_owner_token
            != _legacy_pack_label(origin["artifact_owner_token"])
            or directory is None
            or directory.marker_fingerprint is None
            or proof.inventory is None
            or proof.inventory.files.get(relative) != fingerprint
            or _join(directory.path, relative) != origin["src_path"]
        ):
            raise IdentityMismatch("frozen pack source inventory differs")
        if (
            "source_dev" in file
            and _columns_fingerprint(file, "source_") != fingerprint
        ):
            raise IdentityMismatch("publication source fingerprint differs from origin")


def _admission_roles(
    roles: _Roles, value: object, file_ids: set[str], legacy: str
) -> None:
    if value is None:
        return
    from import_plan import _PUBLICATION_CHAPTER_FIELDS, _PUBLICATION_VOLUME_FIELDS

    admission = _mapping(
        value,
        {
            "acquisition_metadata",
            "series",
            "queue",
            "identity",
            "policy",
            "manual_files",
            "files",
        },
    )
    _mapping(admission["acquisition_metadata"])
    _integer(admission["policy"], 0, 1)
    identity = _list(admission["identity"])
    if len(identity) != 3:
        raise InvalidIdentity("invalid publication admission identity")
    if identity[0] is not None:
        _integer(identity[0], 1)
    if identity[1] not in {None, "torrent", "nzb"}:
        raise InvalidIdentity("unknown admission download protocol")
    _text(identity[2], empty=True)
    for file_id in _list(admission["manual_files"]):
        _integer(file_id, 1)
    if admission["series"] is not None:
        series = _mapping(
            admission["series"], {"id", "root_folder_id", "deleted_at", "edition_type"}
        )
        _integer(series["id"], 1)
    if admission["queue"] is not None:
        _mapping(
            admission["queue"],
            {
                "series_id",
                "torrent_url",
                "download_client_id",
                "download_id",
                "download_protocol",
                "respect_grab_claims",
            },
        )
    files = _mapping(admission["files"])
    if set(files) != file_ids:
        raise IdentityMismatch("admission ready publication file set differs")
    for file_id, raw in files.items():
        record = _mapping(
            raw,
            {
                "mapping",
                "effective",
                "volumes",
                "chapters",
                "other_path_claims",
                "claims",
            },
        )
        mapping = _mapping(record["mapping"])
        if mapping:
            if type(mapping.get("id")) is not int or mapping["id"] != int(file_id):
                raise IdentityMismatch("admission mapping file differs")
            if mapping.get("src_path") not in {None, ""}:
                roles.add(
                    "admission_source",
                    _path(mapping["src_path"]),
                    "file",
                    legacy,
                    "intent",
                    file_id=int(file_id),
                )
        effective = _mapping(record["effective"])
        roles.add(
            "admission_destination",
            _path(effective["dst_path"]),
            "file",
            legacy,
            "intent",
            file_id=int(file_id),
        )
        if type(record["claims"]) is not bool:
            raise InvalidIdentity("invalid admission claims decision")
        other = _mapping(record["other_path_claims"], {"volumes", "chapters"})
        for table, fields in (
            ("volumes", _PUBLICATION_VOLUME_FIELDS),
            ("chapters", _PUBLICATION_CHAPTER_FIELDS),
        ):
            for index, item in enumerate([*_list(record[table]), *_list(other[table])]):
                context = _mapping(item, set(fields))
                _integer(context["id"], 1)
                _integer(context["series_id"], 1)
                if context["import_path"] not in {None, ""}:
                    roles.add(
                        f"admission_{table}_{index}",
                        _path(context["import_path"]),
                        "file",
                        legacy,
                        "intent",
                        file_id=int(file_id),
                    )


def _publication_roles(
    roles: _Roles, row: dict[str, object], related: list[dict[str, object]]
) -> None:
    if (
        row["state"] not in _PUBLICATION_STATES
        or row["import_mode"] not in {"copy", "move", "hardlink"}
        or row["pack_cleanup_state"] not in {"pending", "complete", "retained"}
    ):
        raise InvalidIdentity("unknown publication phase/mode")
    _text(row["owner_token"])
    _integer(row["series_id"], 1)
    queue_id = _integer(row["queue_id"], 1)
    queue = _mapping(roles.evidence.load(row["queue_snapshot_json"]))
    if type(queue.get("id")) is not int or queue["id"] != queue_id:
        raise IdentityMismatch("publication frozen queue binding differs")
    # These columns are copies made by create_publication, not live queue CAS.
    for field in (
        "status",
        "download_id",
        "download_client_id",
        "torrent_name",
        "torrent_url",
        "volume_num",
        "src_dir",
        "failed_at",
        "lease_owner",
        "lease_expires_at",
        "created_at",
    ):
        frozen, mirrored = queue.get(field), row["queue_" + field]
        if type(frozen) is not type(mirrored) or frozen != mirrored:
            raise IdentityMismatch("publication frozen queue mirror differs")
    if row["series_snapshot_json"] is not None:
        series = _mapping(roles.evidence.load(row["series_snapshot_json"]))
        if type(series.get("id")) is not int or series["id"] != row["series_id"]:
            raise IdentityMismatch("publication series snapshot differs")
    tags = _list(roles.evidence.load(row["series_tags_json"]))
    if any(type(t) is not str for t in tags):
        raise InvalidIdentity("invalid publication series tags")
    destination, staging = _path(row["dst_dir"]), _path(row["staging_dir"])
    if row["queue_src_dir"] not in {None, ""}:
        _path(row["queue_src_dir"])
    destination_role = roles.add("destination", destination, "directory")
    staging_role = _publication_staging(roles, row, queue, staging)
    protocol: dict[str, object] = {}
    if "_file_publication" in queue:
        protocol = _mapping(queue["_file_publication"])
        if set(protocol) - {
            "version",
            "decision",
            "admission",
            "artifacts",
            "allocating",
        } or not {"version", "decision", "admission", "artifacts"} <= set(protocol):
            raise InvalidIdentity("unknown publication protocol fields")
        _integer(protocol["version"], 1, 1)
        if protocol["decision"] not in {
            "pending",
            "commit",
            "compensate",
            "compensated",
        }:
            raise InvalidIdentity("unknown publication decision")
    artifacts = _mapping(protocol.get("artifacts", {}))
    children = [
        r
        for r in related
        if r["table"] == "import_publication_files"
        and _mapping(r["row"])["publication_id"] == row["id"]
    ]
    files = [_mapping(r["row"]) for r in children]
    by_id = {str(_integer(f["file_id"], 1)): f for f in files}
    if len(by_id) != len(files) or set(artifacts) - set(by_id):
        raise InvalidIdentity("publication artifact refers to unknown file")
    _admission_roles(
        roles,
        protocol.get("admission"),
        {key for key, file in by_id.items() if file["plan_status"] == "ready"},
        _text(row["queue_snapshot_json"]),
    )
    allocating = protocol.get("allocating")
    if allocating is not None:
        parts = _text(allocating).split(":")
        if (
            len(parts) != 2
            or parts[0] not in by_id
            or parts[1] not in {"source", "original", "publication"}
        ):
            raise InvalidIdentity("unknown publication allocation intent")
    for file_id, file in by_id.items():
        fid = int(file_id)
        if (
            file["plan_status"] not in {"ready", "skip", "needs_review", "pre_failed"}
            or file["stage_state"] not in {"pending", "staged", "skipped", "failed"}
            or file["publish_state"]
            not in {"pending", "published", "not_applicable", "blocked"}
            or file["cleanup_state"]
            not in {
                "pending",
                "deleted",
                "missing",
                "replaced",
                "not_applicable",
                "blocked",
            }
        ):
            raise InvalidIdentity("unknown publication file phase")
        source, dst = _path(file["src_path"]), _path(file["dst_path"])
        final = _path(file["final_path"]) if file["final_path"] is not None else dst
        if _parent(dst) != destination or _parent(final) != destination:
            raise IdentityMismatch("publication final escapes destination")
        for prefix in ("source_", "staged_", "prepared_final_"):
            _columns_fingerprint(file, prefix)
        roles.add(
            "source",
            source,
            "file",
            None,
            "expectation" if file["source_inode"] is not None else "intent",
            file_id=fid,
        )
        roles.add(
            "final",
            final,
            "file",
            None,
            "expectation" if file["publish_state"] == "published" else "intent",
            file_id=fid,
            parent=destination_role,
        )
        if file["stage_path"] is not None:
            stage = _path(file["stage_path"])
            if _parent(stage) != staging:
                raise IdentityMismatch(
                    "publication stage file escapes staging directory"
                )
            roles.add(
                "stage",
                stage,
                "file",
                None,
                "expectation" if file["staged_inode"] is not None else "intent",
                file_id=fid,
                parent=staging_role,
            )
        for column, purpose, origin_path in (
            ("source_claim_path", "source", source),
            ("final_claim_path", "final", final),
        ):
            if file[column] is not None:
                expected = _join(
                    _parent(origin_path),
                    f".mangarr-publication-{row['id']}-{fid}-{purpose}-claim",
                )
                if _path(file[column]) != expected:
                    raise IdentityMismatch("publication claim path differs")
                roles.add(purpose + "_claim", expected, "file", file_id=fid)
        entry = _mapping(artifacts.get(file_id, {}))
        allowed = {
            "original",
            "source",
            "publication",
            "captured_original",
            "captured_source",
            "publication_ready",
            "publication_receipt",
            "quality",
        }
        if set(entry) - allowed:
            raise InvalidIdentity("unknown per-file publication role")
        for proof_key in ("captured_original", "captured_source", "publication_ready"):
            if proof_key in entry:
                _fingerprint(entry[proof_key])
        if "publication_receipt" in entry:
            receipt = LinkReceipt.from_value(entry["publication_receipt"])
            if receipt.destination_path != final or (
                "publication_ready" in entry
                and receipt.fingerprint != _fingerprint(entry["publication_ready"])
            ):
                raise IdentityMismatch("publication link receipt differs")
        for purpose, column, allowed_origins in (
            (
                "source",
                "source_claim_carrier_json",
                {source, file["source_claim_path"]},
            ),
            ("original", "final_claim_carrier_json", {final, file["final_claim_path"]}),
            ("publication", None, {final}),
        ):
            nested = entry.get(purpose)
            durable = file[column] if column else nested
            first = roles.evidence.carrier(nested) if nested is not None else None
            second = roles.evidence.carrier(durable) if durable is not None else None
            if first != second:
                raise IdentityMismatch("duplicate publication carrier proofs disagree")
            if second is not None:
                if purpose == "publication":
                    staged = _columns_fingerprint(file, "staged_")
                    ready = (
                        _fingerprint(entry["publication_ready"])
                        if "publication_ready" in entry
                        else None
                    )
                    # Disposal preserves either both acknowledgements or neither.
                    acknowledgement = (
                        LinkReceipt.from_value(entry["publication_receipt"])
                        if "publication_receipt" in entry
                        else None
                    )
                    if (
                        staged is None
                        or second.artifact_fingerprint != staged
                        or (ready is not None and ready != staged)
                        or (
                            ready is None
                            and second.phase
                            not in {"allocated", "claiming", "discarding", "discarded"}
                        )
                        or (
                            ready is not None
                            and second.phase in {"allocated", "claiming"}
                        )
                        or (
                            second.restore_receipt is not None
                            and second.restore_receipt.fingerprint != ready
                        )
                        or second.restore_receipt != acknowledgement
                        or (second.phase == "restored" and acknowledgement is None)
                        or (
                            second.phase
                            in {"allocated", "claiming", "claimed", "restoring"}
                            and second.restore_receipt is not None
                        )
                    ):
                        raise IdentityMismatch(
                            "publication staged carrier fingerprint/phase differs"
                        )
                expected = (
                    _columns_fingerprint(file, "source_")
                    if purpose == "source"
                    else (
                        _fingerprint(entry["captured_original"])
                        if purpose == "original" and "captured_original" in entry
                        else None
                    )
                )
                if (
                    expected is not None
                    and second.artifact_fingerprint is not None
                    and second.artifact_fingerprint != expected
                ):
                    raise IdentityMismatch(
                        "publication captured carrier fingerprint differs"
                    )
                roles.carrier(
                    second,
                    _text(durable),
                    purpose,
                    ClaimBinding("import_publication", str(row["id"]), fid, purpose),
                    cast(set[str], allowed_origins - {None}),
                )
    _origin_roles(roles, queue, files, "origin")


def _pack_row_roles(roles: _Roles, row: dict[str, object]) -> None:
    identity = _identity(row)
    if (
        not identity.download_id
        or row["download_identity_key"] != download_identity_key(identity)
        or row["normalized_download_id"]
        != normalize_download_id(identity.download_id, identity.protocol)
    ):
        raise IdentityMismatch("pack row download identity differs")
    path = _path(row["pack_path"])
    # Even v0/NULL intent must prove that its logical name belongs to this
    # identity; a missing carrier is not permission to accept an arbitrary path.
    _pack_paths(path, identity, "0" * 32)
    proof: PackOwnership | None = None
    if roles.owner.domain == "pack_reservation":
        if row["purpose"] not in {"queueing", "cleanup"}:
            raise InvalidIdentity("unknown pack reservation purpose")
        _text(row["owner_token"])
        _text(row["expires_at"])
        legacy = row["directory_ownership_json"]
        if legacy is None:
            roles.add("pack_logical", path, "directory")
        else:
            proof = _pack_ownership(roles, _text(legacy), path, identity, "pack")
    else:
        legacy = row["carrier_json"]
        if legacy is not None:
            claim = roles.evidence.claim(_text(legacy))
            _pack_paths(
                path,
                identity,
                claim.artifact_owner_token or claim.carrier.binding.operation_key,
            )
            artifact = _pack_claim(roles, claim, _text(legacy), "tombstone", path)
            if artifact != _path(row["tombstone_path"]):
                raise IdentityMismatch("tombstone path differs from directory claim")
        else:
            roles.add("pack_logical", path, "directory")
    if row["tombstone_path"] is not None:
        tombstone = _path(row["tombstone_path"])
        legacy_intent = _legacy_cleanup_path(path, tombstone)
        if roles.owner.domain == "pack_reservation":
            matches = (
                [
                    claim
                    for claim in proof.claims
                    if _join(claim.carrier.carrier_path, "artifact") == tombstone
                ]
                if proof is not None
                else []
            )
            placement = proof.placement_carrier if proof is not None else None
            placement_matches = (
                placement is not None
                and _join(placement.carrier_path, "artifact") == tombstone
            )
            cleanup_intent = (
                row["purpose"] == "cleanup"
                and legacy_intent
                and (proof is None or not proof.claims)
            )
            if len(matches) > 1 or not (
                len(matches) == 1
                or placement_matches
                or cleanup_intent
                or _private_creation_intent(row, tombstone, proof)
            ):
                raise IdentityMismatch(
                    "reservation tombstone lacks phase-specific provenance"
                )
        elif legacy is None and not legacy_intent:
            raise IdentityMismatch("legacy pack tombstone intent differs")
        roles.add("tombstone_path", tombstone, "directory", None, "intent")


def _snapshot_records(
    snapshot: OwnerSnapshot,
    evidence: _Evidence,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    if type(snapshot) is not OwnerSnapshot:
        raise InvalidIdentity("expected immutable SQL owner snapshot")
    if snapshot not in evidence.snapshots:
        snapshot.__post_init__()
        value = _mapping(evidence.load(snapshot.rows_json))
        root = _mapping(_mapping(value["root"])["row"])
        evidence.snapshots[snapshot] = (
            root,
            [_mapping(r) for r in _list(value["related"])],
        )
    return evidence.snapshots[snapshot]


def _inventory(
    snapshot: OwnerSnapshot,
    counter: _Counter | None = None,
    evidence: _Evidence | None = None,
) -> _Roles:
    try:
        roles = _Roles(snapshot.owner, counter, evidence)
        root, related = _snapshot_records(snapshot, roles.evidence)
        domain = snapshot.owner.domain
        if domain == "namespace":
            _namespace_roles(roles, root)
        elif domain == "rescan":
            _rescan_roles(roles, root)
        elif domain == "deletion":
            _deletion_roles(roles, root)
        elif domain in {"pack_reservation", "pack_tombstone"}:
            _pack_row_roles(roles, root)
        elif domain == "publication":
            if root["state"] not in _TERMINAL_PUBLICATIONS and not any(
                r["table"] == "import_queue"
                and _mapping(r["row"])["id"] == root["queue_id"]
                for r in related
            ):
                raise IdentityMismatch("publication queue is dangling")
            _publication_roles(roles, root, related)
        else:
            _identity(root, "download_protocol")
            if root["status"] not in {
                "pending",
                "partial",
                "importing",
                "imported",
                "failed",
                "skipped",
            }:
                raise InvalidIdentity("unknown queue phase")
            if root["src_dir"] not in {None, ""}:
                roles.add("source_directory", _path(root["src_dir"]), "directory")
            files = [
                _mapping(r["row"])
                for r in related
                if r["table"] == "import_queue_files"
            ]
            for file in files:
                fid = _integer(file["id"], 1)
                if file["queue_id"] != root["id"]:
                    raise IdentityMismatch("queue file belongs to another queue")
                for column, purpose in (
                    ("src_path", "source"),
                    ("dst_path", "destination"),
                ):
                    if file[column] not in {None, ""}:
                        roles.add(purpose, _path(file[column]), "file", file_id=fid)
            for record in related:
                if record["table"] == "import_publications":
                    row = _mapping(record["row"])
                    frozen = _mapping(roles.evidence.load(row["queue_snapshot_json"]))
                    pubfiles = [
                        _mapping(r["row"])
                        for r in related
                        if r["table"] == "import_publication_files"
                        and _mapping(r["row"])["publication_id"] == row["id"]
                    ]
                    _origin_roles(
                        roles,
                        frozen,
                        pubfiles,
                        "origin_" + str(row["id"]),
                    )
        return roles
    except (
        PrivateClaimError,
        PackProofError,
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
    ) as exc:
        raise InvalidIdentity("invalid persisted domain reference evidence") from exc


def roles_for(snapshot: OwnerSnapshot) -> tuple[RoleSpec, ...]:
    """Validate fixed producer fields and describe only their persisted intent."""
    return tuple(sorted(_inventory(snapshot).values.values(), key=_role_order))


def snapshot_owner(db: sqlite3.Connection, owner: OwnerRef) -> OwnerSnapshot:
    """Delegate unchanged Task2 capture, then validate producer semantics."""
    _owner(owner)
    try:
        snapshot = capture_owner_snapshot(db, owner)
    except sqlite3.Error as exc:
        raise InvalidIdentity("SQL owner snapshot could not be captured") from exc
    roles_for(snapshot)
    return snapshot


@dataclass(slots=True)
class _Counter:
    budget: InventoryBudget
    rows: int = 0
    objects: int = 0
    hash_bytes: int = 0

    def charge(self, *, rows: int = 0, objects: int = 0, hash_bytes: int = 0) -> None:
        self.rows += rows
        self.objects += objects
        self.hash_bytes += hash_bytes
        if (
            self.rows > self.budget.max_rows
            or self.objects > self.budget.max_objects
            or self.hash_bytes > self.budget.max_hash_bytes
        ):
            raise InvalidIdentity("inventory budget exceeded")


def _scan(db: sqlite3.Connection, counter: _Counter) -> dict[str, list[RawRow]]:
    result: dict[str, list[RawRow]] = {}
    for table, pk in _TABLES.items():
        result[table] = []
        # series_tags has a composite key; all other static selectors have one.
        columns = ("series_id", "tag") if table == "series_tags" else (pk,)
        last: tuple[SQLScalar, ...] | None = None
        while True:
            where = (
                ""
                if last is None
                else " WHERE ("
                + ",".join(columns)
                + ") > ("
                + ",".join("?" for _ in columns)
                + ")"
            )
            cursor = db.execute(
                f"SELECT * FROM {table}{where} ORDER BY {','.join(columns)} LIMIT 64",
                last or (),
            )
            names = tuple(c[0] for c in cursor.description)
            batch = cursor.fetchmany(64)
            counter.charge(rows=len(batch))
            for values in batch:
                row = dict(zip(names, values, strict=True))
                raw = RawRow(
                    table,
                    tuple(cast(str | int, row[c]) for c in columns),
                    tuple(sorted(row.items())),
                )
                result[table].append(raw)
                counter.charge(hash_bytes=len(_encode(raw.values).encode("utf-8")))
            if len(batch) < 64:
                break
            last = tuple(dict(result[table][-1].values)[c] for c in columns)
    return result


def _context_series(snapshot: OwnerSnapshot, evidence: _Evidence) -> set[int]:
    root, _ = _snapshot_records(snapshot, evidence)
    result = (
        {_integer(root["series_id"], 1)} if root.get("series_id") is not None else set()
    )
    if snapshot.owner.domain != "publication":
        return result
    queue = _mapping(evidence.load(root["queue_snapshot_json"]))
    if queue.get("series_id") is not None:
        result.add(_integer(queue["series_id"], 1))
    protocol = (
        _mapping(queue["_file_publication"]) if "_file_publication" in queue else {}
    )
    if protocol.get("admission") is None:
        return result
    admission = _mapping(protocol["admission"])
    for key in ("series", "queue"):
        if admission[key] is not None:
            record = _mapping(admission[key])
            result.add(_integer(record["id" if key == "series" else "series_id"], 1))
    for raw in _mapping(admission["files"]).values():
        file = _mapping(raw)
        other = _mapping(file["other_path_claims"])
        for table in ("volumes", "chapters"):
            for item in [*_list(file[table]), *_list(other[table])]:
                result.add(_integer(_mapping(item)["series_id"], 1))
    return result


def _historical_context(domain: str, row: dict[str, object]) -> bool:
    if domain == "publication":
        return row["state"] in _TERMINAL_PUBLICATIONS
    if domain == "rescan":
        return row["state"] in {"completed", "rolled_back"}
    if domain == "deletion":
        return row["state"] == "completed"
    return False


def _version(db: sqlite3.Connection) -> int:
    return cast(int, db.execute("PRAGMA data_version").fetchone()[0])


def collect_references(
    db: sqlite3.Connection, parent: str, budget: InventoryBudget
) -> ReferenceClosure:
    """Capture a bounded all-state SQL graph without media I/O or a writer.

    Own a DEFERRED read view only. A caller-owned transaction cannot expose
    post-view data_version, so it is refused rather than committed or reopened.
    Concurrent commits and same-connection changes invalidate the whole result.
    """
    parent = _path(parent)
    if type(budget) is not InventoryBudget:
        raise InvalidIdentity("expected inventory budget")
    budget.__post_init__()
    try:
        if db.in_transaction:
            raise IdentityMismatch("inventory requires a clean SQL connection")
        version, changes = _version(db), db.total_changes
        db.execute("BEGIN DEFERRED")
    except sqlite3.Error as exc:
        raise InvalidIdentity("SQL inventory read view could not be opened") from exc
    counter = _Counter(budget)
    evidence = _Evidence()
    try:
        raw = _scan(db, counter)
        snapshots: dict[OwnerRef, OwnerSnapshot] = {}
        roles: dict[OwnerRef, tuple[RoleSpec, ...]] = {}
        roots: dict[OwnerRef, dict[str, object]] = {}
        carriers: dict[OwnerRef, list[CarrierRecord]] = {}
        context_series: dict[OwnerRef, set[int]] = {}
        edges: set[ReferenceDependency] = set()
        for domain, (table, pk) in _ROOTS.items():
            for record in raw[table]:
                row = cast(dict[str, object], dict(record.values))
                owner = OwnerRef(domain, (cast(str | int, row[pk]),))
                snapshot = capture_owner_snapshot(db, owner)
                root, related = _snapshot_records(snapshot, evidence)
                counter.charge(
                    rows=1 + len(related),
                    hash_bytes=len(snapshot.rows_json.encode("utf-8")),
                )
                inventory = _inventory(snapshot, counter, evidence)
                objects = tuple(sorted(inventory.values.values(), key=_role_order))
                carriers[owner] = inventory.carriers
                snapshots[owner], roots[owner], roles[owner] = snapshot, root, objects
                context_series[owner] = _context_series(snapshot, evidence)

        def link(owner: OwnerRef, target: OwnerRef, reason: str) -> None:
            if target not in snapshots:
                raise IdentityMismatch("persisted reference has a dangling owner")
            if owner != target:
                edges.add(ReferenceDependency(owner, target, reason))

        for owner, row in roots.items():
            if owner.domain in {"publication", "pack_reservation", "pack_tombstone"}:
                if row["queue_id"] is not None:
                    target = OwnerRef("queue_source", (_integer(row["queue_id"], 1),))
                    if target in snapshots:
                        link(owner, target, "queue")
                    else:
                        if owner.domain == "publication":
                            publication = row
                        elif row["publication_id"] is not None:
                            publication = roots.get(
                                OwnerRef(
                                    "publication", (_integer(row["publication_id"], 1),)
                                )
                            )
                        else:
                            publication = None
                        if (
                            publication is None
                            or publication["state"] not in _TERMINAL_PUBLICATIONS
                            or publication["queue_id"] != row["queue_id"]
                        ):
                            raise IdentityMismatch(
                                "persisted reference has a dangling queue"
                            )
                        if owner.domain != "publication":
                            frozen = _mapping(
                                evidence.load(publication["queue_snapshot_json"])
                            )
                            if (
                                download_identity_key(
                                    _identity(frozen, "download_protocol")
                                )
                                != row["download_identity_key"]
                            ):
                                raise IdentityMismatch(
                                    "historical pack queue identity differs"
                                )
                    if (
                        owner.domain in {"pack_reservation", "pack_tombstone"}
                        and target in roots
                        and download_identity_key(
                            _identity(roots[target], "download_protocol")
                        )
                        != row["download_identity_key"]
                    ):
                        raise IdentityMismatch("pack queue download identity differs")
                if owner.domain != "publication" and row["publication_id"] is not None:
                    link(
                        owner,
                        OwnerRef("publication", (_integer(row["publication_id"], 1),)),
                        "publication",
                    )
            for role in roles[owner]:
                if role.role.purpose.endswith("_carrier"):
                    namespace = _parent(_parent(role.path))
                    link(owner, OwnerRef("namespace", (namespace,)), "namespace")
            for record in carriers[owner]:
                namespace_owner = OwnerRef("namespace", (_parent(record.origin_path),))
                link(owner, namespace_owner, "namespace")
                namespace = evidence.namespace(
                    _text(roots[namespace_owner]["ownership_json"])
                )
                if namespace.root_dev != record.dir_dev:
                    raise IdentityMismatch(
                        "carrier and namespace device evidence differs"
                    )
        # Child rows must resolve even when no owner or path would select them.
        for table, field, domain in (
            ("import_queue_files", "queue_id", "queue_source"),
            ("import_publication_files", "publication_id", "publication"),
        ):
            for record in raw[table]:
                target = OwnerRef(domain, (_integer(dict(record.values)[field], 1),))
                if target not in snapshots:
                    raise IdentityMismatch("orphaned persisted child reference")
        for owner, row in roots.items():
            if owner.domain == "pack_tombstone":
                reservation = OwnerRef(
                    "pack_reservation", (_text(row["download_identity_key"]),)
                )
                if reservation in snapshots:
                    link(owner, reservation, "pack")
            if owner.domain not in {"publication", "queue_source"}:
                continue
            snapshot = snapshots[owner]
            root, related = _snapshot_records(snapshot, evidence)
            publications = (
                [root]
                if owner.domain == "publication"
                else [
                    _mapping(r["row"])
                    for r in related
                    if r["table"] == "import_publications"
                ]
            )
            for publication in publications:
                frozen = _mapping(evidence.load(publication["queue_snapshot_json"]))
                origins = (
                    _mapping(_mapping(frozen["_pack_source_origins"])["files"])
                    if "_pack_source_origins" in frozen
                    else {}
                )
                for value in origins.values():
                    origin = _mapping(value)
                    if origin["kind"] != "pack":
                        continue
                    key = _text(origin["download_identity_key"])
                    reservation = OwnerRef("pack_reservation", (key,))
                    candidates = [reservation] if reservation in snapshots else []
                    candidates += [
                        o
                        for o, r in roots.items()
                        if o.domain == "pack_tombstone"
                        and r["download_identity_key"] == key
                    ]
                    if not candidates:
                        raise IdentityMismatch("frozen pack origin chain is dangling")
                    origin_queue = roots.get(
                        OwnerRef("queue_source", (_integer(origin["queue_id"], 1),))
                    )
                    historical = publication["state"] in _TERMINAL_PUBLICATIONS and (
                        origin_queue is None or origin_queue["status"] in _TERMINAL
                    )
                    if not historical:
                        current_row = roots.get(reservation)
                        if (
                            current_row is None
                            or current_row["directory_ownership_json"] is None
                        ):
                            raise IdentityMismatch(
                                "active origin lacks its current reservation"
                            )
                        admitted = evidence.pack(_text(origin["ownership_json"]))
                        current = evidence.pack(
                            _text(current_row["directory_ownership_json"])
                        )
                        if (
                            current_row["queue_id"] != origin["queue_id"]
                            or current_row["pack_path"] != origin["logical_pack_path"]
                            or current_row["download_client_id"]
                            != origin["download_client_id"]
                            or current_row["protocol"] != origin["download_protocol"]
                            or current_row["download_id"] != origin["download_id"]
                            or current_row["publication_id"]
                            not in {None, publication["id"]}
                            or current.artifact_owner_token
                            != admitted.artifact_owner_token
                            or current.canonical_directory
                            != admitted.canonical_directory
                            or current.inventory != admitted.inventory
                            or current.placement_carrier != admitted.placement_carrier
                            or current.queue_receipt_sha256
                            != admitted.queue_receipt_sha256
                        ):
                            raise IdentityMismatch(
                                "active pack origin generation differs"
                            )
                        forward = publication["state"] not in {
                            "db_committed",
                            "cleaning",
                        }
                        if (
                            forward
                            and (
                                current_row["purpose"] != "queueing"
                                or current.phase != "attached"
                            )
                        ) or (
                            not forward
                            and current.phase not in {"attached", "cleaning"}
                        ):
                            raise IdentityMismatch(
                                "active pack source is consumer-fenced"
                            )
                    for target in candidates:
                        # Terminal admission preserves an old object generation.
                        # Include the current identity row, never reinterpret it
                        # as proof of that historical generation's ownership.
                        link(owner, target, "pack")
        # Context rows and blockers are evidence, never quarantine enrollment.
        path_series: set[int] = set()
        for table in ("volumes", "chapters", "chapter_quarantine", "root_folders"):
            for record in raw[table]:
                row = dict(record.values)
                fields = (
                    ("original_path", "volume_path", "carrier_path", "quarantine_path")
                    if table == "chapter_quarantine"
                    else (("path",) if table == "root_folders" else ("import_path",))
                )
                for field in fields:
                    if row[field] not in {None, ""}:
                        path = _path(row[field])
                        if table != "root_folders" and _within(path, parent):
                            path_series.add(_integer(row["series_id"], 1))
        selected = {
            o
            for o, specs in roles.items()
            if any(_within(r.path, parent) for r in specs)
        }
        # Only declared objects close an external path reference. These three
        # directory roles are context/parents, not claims on arbitrary children.
        by_path: dict[str, set[OwnerRef]] = {}
        for owner, specs in roles.items():
            for spec in specs:
                if spec.role.kind != "directory" or spec.role.purpose not in {
                    "source_directory",
                    "destination",
                    "namespace_parent",
                }:
                    by_path.setdefault(spec.path, set()).add(owner)
        for owners in by_path.values():
            anchor = min(owners, key=_owner_order)
            for owner in owners - {anchor}:
                link(owner, anchor, "shared_context")
        series_ids = set(path_series)
        while True:
            before = set(selected)
            for edge in edges:
                if edge.owner in selected or edge.requires in selected:
                    selected.update((edge.owner, edge.requires))
            for owner in selected:
                series_ids.update(context_series[owner])
            for owner, row in roots.items():
                if row.get("series_id") in series_ids:
                    selected.add(owner)
            if selected == before:
                break
        live_series = {cast(int, r.key[0]) for r in raw["series"]}
        volumes = {r.key[0]: dict(r.values) for r in raw["volumes"]}
        for owner in selected:
            row = roots[owner]
            historical = _historical_context(owner.domain, row)
            if not historical and context_series[owner] - live_series:
                raise IdentityMismatch("reference series context is dangling")
            if owner.domain in {"rescan", "deletion"}:
                volume = volumes.get(cast(int, row["volume_id"]))
                if (volume is None and not historical) or (
                    volume is not None and volume["series_id"] != row["series_id"]
                ):
                    raise IdentityMismatch("reference volume context is dangling")
        for table in ("volumes", "chapters", "chapter_quarantine"):
            for record in raw[table]:
                row = dict(record.values)
                if row["series_id"] not in series_ids:
                    continue
                historical = False
                if table == "chapter_quarantine":
                    if row["state"] not in _ACTIVE_QUARANTINE | _INACTIVE_QUARANTINE:
                        raise InvalidIdentity("unknown quarantine phase")
                    historical = row["state"] in _INACTIVE_QUARANTINE
                if not historical and row["series_id"] not in live_series:
                    raise IdentityMismatch("live media series context is dangling")
                if table == "chapter_quarantine":
                    volume = volumes.get(cast(int, row["volume_id"]))
                    if (volume is None and not historical) or (
                        volume is not None and volume["series_id"] != row["series_id"]
                    ):
                        raise IdentityMismatch("quarantine volume context is dangling")
        selected_raw: set[RawRow] = set()
        for owner in selected:
            snapshot = snapshots[owner]
            value = _mapping(evidence.load(snapshot.rows_json))
            for item in [value["root"], *_list(value["related"])]:
                record = _mapping(item)
                table = _text(record["table"])
                key = tuple(_list(record["key"]))
                selected_raw.update(r for r in raw[table] if r.key == key)
        for table in (
            "series",
            "volumes",
            "chapters",
            "chapter_quarantine",
            "series_tags",
        ):
            selected_raw.update(
                r
                for r in raw[table]
                if (
                    dict(r.values)["id"]
                    if table == "series"
                    else dict(r.values)["series_id"]
                )
                in series_ids
            )
        root_ids = {
            dict(r.values)["root_folder_id"]
            for r in selected_raw
            if r.table == "series"
        }
        selected_raw.update(r for r in raw["root_folders"] if r.key[0] in root_ids)
        if len(selected_raw) > _LIMIT or len(selected) > _LIMIT:
            raise InvalidIdentity("inventory closure exceeds collection bound")
        result = ReferenceClosure(
            tuple(snapshots[o] for o in sorted(selected, key=_owner_order)),
            tuple(sorted((r for o in selected for r in roles[o]), key=_role_order)),
            tuple(
                sorted((e for e in edges if e.owner in selected), key=_dependency_order)
            ),
            tuple(sorted(selected_raw, key=_raw_order)),
        )
        if db.total_changes != changes:
            raise IdentityMismatch("same-connection SQL drift during inventory")
    except sqlite3.Error as exc:
        raise InvalidIdentity("SQL reference inventory could not be completed") from exc
    finally:
        db.rollback()
    if _version(db) != version or db.total_changes != changes:
        raise IdentityMismatch("SQL reference inventory changed during capture")
    return result
