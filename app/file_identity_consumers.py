"""Disabled SQL-only reference inventory, not filesystem or recovery authority.

Presence describes persisted evidence, never a stat result. Owner-local roles
and raw dependency edges are not Task2 manifests and cannot enable a profile.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import cast

from download_identity import (
    DownloadIdentity,
    DownloadProtocol,
    download_identity_key,
    download_identity_path_token,
    normalize_download_id,
)
from file_identity import (
    IdentityMismatch,
    InvalidIdentity,
    NativeHandle,
    OwnerRef,
    RoleKey,
    StorageProfile,
)
import file_identity_store as _store
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
_LEDGER_KEYS = {
    "file_identity_authorities": "authority_id",
    "file_identity_epochs": "epoch_id",
    "file_identity_sets": "set_id",
    "file_identity_events": "id",
}
_TABLES = (
    {table: pk for table, pk in _ROOTS.values()}
    | {
        "import_queue_files": "id",
        "import_publication_files": "id",
        "series": "id",
        "volumes": "id",
        "chapters": "id",
        "chapter_quarantine": "id",
        "root_folders": "id",
        "series_tags": "series_id",
    }
    | _LEDGER_KEYS
)
_PURE_LEDGER_KEYS = _LEDGER_KEYS | {"file_identity_provision_intents": "intent_id"}
_PURE_TABLES = _TABLES | _PURE_LEDGER_KEYS
_REQUIRED_RAW = {
    **{table: frozenset(columns) for table, columns in _store._PROJECTIONS.items()},
    "series": frozenset("id title root_folder_id".split()),
    "volumes": frozenset(
        "id series_id volume_num status import_path download_id download_client_id torrent_name indexer protocol client grabbed_at imported_at source_url release_group size_bytes quality".split()
    ),
    "chapters": frozenset("id series_id volume_id import_path".split()),
    "chapter_quarantine": frozenset(
        "id series_id volume_id state original_path volume_path carrier_path quarantine_path".split()
    ),
    "root_folders": frozenset("id path".split()),
    "series_tags": frozenset("series_id tag".split()),
    "file_identity_authorities": frozenset(
        "authority_id incarnation_token profile_json declaration_json qualification_sha256".split()
    ),
    "file_identity_epochs": frozenset(
        "epoch_id authority_id origin_path allocation_path root_path birth_json".split()
    ),
    "file_identity_sets": frozenset(
        "set_id owner_domain owner_key_json prior_set_id snapshot_json snapshot_sha256 origin epoch_id manifest_json manifest_sha256".split()
    ),
    "file_identity_events": frozenset(
        "id owner_domain owner_key_json revision predecessor_event_id set_id kind payload_json".split()
    ),
}
_REQUIRED_RAW["file_identity_provision_intents"] = frozenset(
    "intent_id authority_id payload_json payload_sha256".split()
)
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
            self.table not in _PURE_TABLES
            or type(self.key) is not tuple
            or not self.key
            or type(self.values) is not tuple
        ):
            raise InvalidIdentity("invalid inventory raw row")
        expected_key = (
            ("series_id", "tag")
            if self.table == "series_tags"
            else (_PURE_TABLES[self.table],)
        )
        if len(self.key) != len(expected_key):
            raise InvalidIdentity("invalid raw row key length")
        for column, value in zip(expected_key, self.key, strict=True):
            if column in {"parent_path", "tombstone_path"}:
                _path(value)
            elif column in {"download_identity_key", "tag"}:
                _text(value)
            elif self.table in _PURE_LEDGER_KEYS and column != "id":
                _hex(value)
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
            "identity_ledger",
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
        if len({(r.table, r.key) for r in self.raw_rows}) != len(self.raw_rows):
            raise InvalidIdentity("inventory raw table/key is duplicated")


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
            document = _mapping(roles.evidence.load(snapshot.rows_json))
            if document["version"] == 2:
                intent, _ = _store._provision_snapshot(document)
                roles.add(
                    "epoch_origin_parent", _path(intent["origin_path"]), "directory"
                )
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


def _claim_origin_matches(
    claim: PackDirectoryClaim, origin: dict[str, object], evidence: _Evidence
) -> bool:
    if origin.get("kind") != "pack":
        return False
    proof = evidence.pack(_text(origin["ownership_json"]))
    relative = _relative(origin["relative_path"])
    return (
        proof.artifact_owner_token == claim.artifact_owner_token
        and proof.canonical_directory == claim.source_directory
        and proof.inventory == claim.inventory
        and proof.inventory is not None
        and proof.inventory.files.get(relative)
        == _fingerprint(origin["source_fingerprint"])
        and _join(claim.source_directory.path, relative) == origin["src_path"]
    )


def _pack_restore_references(
    closure: ReferenceClosure,
    owner: OwnerRef,
    claim: PackDirectoryClaim,
    ownership_json: str | None,
) -> tuple[tuple[RoleSpec, dict[str, object]], ...]:
    """Project exact current/frozen source references of one original claim."""
    evidence = _Evidence()
    roots = {s.owner: _snapshot_records(s, evidence)[0] for s in closure.snapshots}
    inventories = {s.owner: _inventory(s, evidence=evidence) for s in closure.snapshots}
    if owner.domain not in {"pack_tombstone", "pack_reservation"} or owner not in roots:
        raise InvalidIdentity("ownership_context")
    row = roots[owner]
    queue_id = row["queue_id"]
    if queue_id is None:
        if ownership_json is None:
            return _transferred_claim_restore_references(closure, owner, claim)
        return ()
    identity = _identity(row)
    source = claim.source_directory.path
    result: list[tuple[RoleSpec, dict[str, object]]] = []
    frozen_sources: dict[int, set[str]] = {}
    for snapshot in closure.snapshots:
        if snapshot.owner.domain not in {"publication", "queue_source"}:
            continue
        root, related = _snapshot_records(snapshot, evidence)
        publications = (
            [root]
            if snapshot.owner.domain == "publication"
            else [
                _mapping(r["row"])
                for r in related
                if r["table"] == "import_publications"
            ]
        )
        for publication in publications:
            frozen = _mapping(evidence.load(publication["queue_snapshot_json"]))
            if frozen["id"] != queue_id:
                continue
            origins = frozen.get("_pack_source_origins")
            if origins is None:
                continue
            for file_id, value in _mapping(_mapping(origins)["files"]).items():
                origin = _mapping(value)
                applicable = origin.get("kind") == "pack" and (
                    origin["logical_pack_path"] == source
                    or _within(_path(origin["src_path"]), source)
                )
                if applicable and (
                    _identity(frozen, "download_protocol") != identity
                    or not _claim_origin_matches(claim, origin, evidence)
                ):
                    raise InvalidIdentity("ownership_context")
                if _identity(frozen, "download_protocol") != identity:
                    continue
                if not _claim_origin_matches(claim, origin, evidence):
                    continue
                fid = _integer(origin["file_id"], 1)
                raw = _text(origin["ownership_json"])
                frozen_sources.setdefault(fid, set()).add(raw)
                prefix = (
                    "origin_"
                    if snapshot.owner.domain == "publication"
                    else f"origin_{publication['id']}_"
                ) + file_id
                canonical = RoleKey(None, prefix + "_canonical", "directory", None)
                for spec in inventories[snapshot.owner].values.values():
                    projected = (
                        spec.role
                        in {
                            canonical,
                            RoleKey(None, prefix + "_logical", "directory", None),
                        }
                        or spec.parent_role == canonical
                    )
                    if (
                        snapshot.owner.domain == "publication"
                        and spec.role.file_id == fid
                        and spec.role.purpose in {"source", "admission_source"}
                        and spec.path == origin["src_path"]
                    ):
                        projected = True
                    if not projected or not _within(spec.path, source):
                        continue
                    result.append(
                        (
                            spec,
                            {
                                "kind": "frozen-pack-origin",
                                "publication_id": _integer(publication["id"], 1)
                                if snapshot.owner.domain == "publication"
                                else None,
                                "queue_id": _integer(queue_id, 1),
                                "file_id": fid,
                                "identity_key": download_identity_key(identity),
                                "artifact_owner_token": claim.artifact_owner_token,
                                "origin_sha256": hashlib.sha256(
                                    _encode(origin).encode()
                                ).hexdigest(),
                                "origin_ownership_sha256": hashlib.sha256(
                                    raw.encode()
                                ).hexdigest(),
                                "relative": spec.role.relative,
                            },
                        )
                    )
    queue_owner = OwnerRef("queue_source", (_integer(queue_id, 1),))
    queue = roots.get(queue_owner)
    if queue is not None:
        if _identity(queue, "download_protocol") != identity:
            raise InvalidIdentity("ownership_context")
        _, related = _snapshot_records(
            next(s for s in closure.snapshots if s.owner == queue_owner), evidence
        )
        files = [
            _mapping(r["row"]) for r in related if r["table"] == "import_queue_files"
        ]
        raw_sources: set[str] = set()
        if ownership_json is not None:
            raw_sources.add(ownership_json)
        else:
            for strings in frozen_sources.values():
                raw_sources.update(strings)
        if ownership_json is None and not raw_sources:
            return _transferred_claim_restore_references(closure, owner, claim)
        if len(raw_sources) != 1:
            raise InvalidIdentity("ownership_phase")
        raw = next(iter(raw_sources))
        proof = evidence.pack(raw)
        if (
            proof.artifact_owner_token != claim.artifact_owner_token
            or proof.canonical_directory != claim.source_directory
            or proof.inventory != claim.inventory
        ):
            raise InvalidIdentity("ownership_context")
        for file in files:
            path = _path(file["src_path"])
            if not _within(path, source) or path == source:
                continue
            relative = path[len(source.rstrip("/")) + 1 :]
            if relative not in claim.inventory.files:
                raise InvalidIdentity("ownership_context")
            fid = _integer(file["id"], 1)
            if ownership_json is None and raw not in frozen_sources.get(fid, set()):
                raise InvalidIdentity("ownership_context")
            spec = inventories[queue_owner].values[RoleKey(fid, "source", "file", None)]
            result.append(
                (
                    spec,
                    {
                        "kind": "pack-source-file",
                        "queue_id": _integer(queue_id, 1),
                        "queue_file_id": fid,
                        "identity_key": download_identity_key(identity),
                        "artifact_owner_token": claim.artifact_owner_token,
                        "relative": relative,
                        "origin_ownership_sha256": hashlib.sha256(
                            raw.encode()
                        ).hexdigest(),
                    },
                )
            )
        if queue["src_dir"] == source:
            spec = inventories[queue_owner].values[
                RoleKey(None, "source_directory", "directory", None)
            ]
            result.append(
                (
                    spec,
                    {
                        "kind": "pack-source-root",
                        "queue_id": _integer(queue_id, 1),
                        "queue_file_id": None,
                        "identity_key": download_identity_key(identity),
                        "artifact_owner_token": claim.artifact_owner_token,
                        "relative": None,
                        "origin_ownership_sha256": hashlib.sha256(
                            raw.encode()
                        ).hexdigest(),
                    },
                )
            )
    elif ownership_json is None and not frozen_sources:
        return _transferred_claim_restore_references(closure, owner, claim)
    return tuple(result)


def _transferred_claim_restore_references(
    closure: ReferenceClosure,
    owner: OwnerRef,
    claim: PackDirectoryClaim,
) -> tuple[tuple[RoleSpec, dict[str, object]], ...]:
    """Bind retained queue names to opaque transferred claim bytes, not ownership."""
    from import_pack_cleanup import _pack_owner_marker_payload

    _owner(owner)
    snapshots = {snapshot.owner: snapshot for snapshot in closure.snapshots}
    if owner.domain != "pack_tombstone" or owner not in snapshots:
        raise InvalidIdentity("ownership_context")
    evidence = _Evidence()
    snapshot = snapshots[owner]
    projection = _inventory(snapshot, evidence=evidence)
    row, _ = _snapshot_records(snapshot, evidence)
    raw = _text(row["carrier_json"])
    source = _path(row["pack_path"])
    token = claim.artifact_owner_token
    primary = RoleKey(None, "tombstone_captured", "directory", None)
    if (
        evidence.claim(raw) != claim
        or token is None
        or claim.carrier.binding != ClaimBinding("pack", token, None, "pack_cleanup")
        or claim.carrier.artifact_fingerprint is not None
        or claim.carrier.restore_receipt is not None
        or claim.source_directory.path != source
        or row["tombstone_path"] != _join(claim.carrier.carrier_path, "artifact")
        or projection.values[primary] not in closure.roles
        or not claim.inventory.files
    ):
        raise InvalidIdentity("ownership_context")
    if claim.carrier.phase != "claimed":
        raise InvalidIdentity("ownership_phase")
    identity = _identity(row)
    _ = _pack_paths(source, identity, token)
    marker = claim.source_directory.marker_fingerprint
    payload = _pack_owner_marker_payload(identity, token)
    if (
        marker is None
        or marker.size != len(payload)
        or marker.sha256 != hashlib.sha256(payload).hexdigest()
    ):
        raise InvalidIdentity("ownership_context")
    # A current owner of this logical generation is not a lost-document case.
    for other in closure.snapshots:
        if other.owner.domain != "pack_reservation":
            continue
        current, _ = _snapshot_records(other, evidence)
        if (
            current["pack_path"] == source
            or current["download_identity_key"] == row["download_identity_key"]
        ):
            raise InvalidIdentity("ownership_context")
    queue_id = _integer(row["queue_id"], 1)
    queue_owner = OwnerRef("queue_source", (queue_id,))
    queue_snapshot = snapshots.get(queue_owner)
    if queue_snapshot is None:
        raise InvalidIdentity("ownership_context")
    queue_projection = _inventory(queue_snapshot, evidence=evidence)
    queue, related = _snapshot_records(queue_snapshot, evidence)
    if (
        _integer(queue["id"], 1) != queue_id
        or _identity(queue, "download_protocol") != identity
        or queue["status"] not in _TERMINAL
        or queue["lease_owner"] is not None
    ):
        raise InvalidIdentity("ownership_context")
    common: dict[str, object] = {
        "claim_owner": {"domain": owner.domain, "key": list(owner.key)},
        "claim_role": asdict(primary),
        "claim_snapshot_sha256": snapshot.sha256,
        "claim_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "queue_snapshot_sha256": queue_snapshot.sha256,
        "queue_id": queue_id,
        "identity_key": download_identity_key(identity),
        "artifact_owner_token": token,
    }
    result: list[tuple[RoleSpec, dict[str, object]]] = []
    for record in related:
        if record["table"] != "import_queue_files":
            continue
        file = _mapping(record["row"])
        if _integer(file["queue_id"], 1) != queue_id:
            raise InvalidIdentity("ownership_context")
        path = _path(file["src_path"])
        if not _within(path, source):
            continue
        relative = _relative(path[len(source.rstrip("/")) + 1 :])
        fingerprint = claim.inventory.files.get(relative)
        fid = _integer(file["id"], 1)
        spec = queue_projection.values.get(RoleKey(fid, "source", "file", None))
        if (
            fingerprint is None
            or spec is None
            or spec not in closure.roles
            or spec.path != path
        ):
            raise InvalidIdentity("ownership_context")
        result.append(
            (
                spec,
                {
                    **common,
                    "kind": "transferred-pack-claim-source-file",
                    "queue_file_id": fid,
                    "relative": relative,
                    "source_fingerprint": asdict(fingerprint),
                },
            )
        )
    if not result:
        raise InvalidIdentity("ownership_context")
    if queue["src_dir"] == source:
        spec = queue_projection.values.get(
            RoleKey(None, "source_directory", "directory", None)
        )
        if spec is None or spec not in closure.roles or spec.path != source:
            raise InvalidIdentity("ownership_context")
        result.append(
            (
                spec,
                {
                    **common,
                    "kind": "transferred-pack-claim-source-root",
                    "queue_file_id": None,
                    "relative": None,
                    "source_fingerprint": None,
                },
            )
        )
    return tuple(sorted(result, key=lambda item: _role_order(item[0])))


def _location_cohorts(closure: ReferenceClosure) -> tuple[frozenset[RoleSpec], ...]:
    """Closed producer relationships for location only, never action consent."""
    evidence = _Evidence()
    inventories = {s.owner: _inventory(s, evidence=evidence) for s in closure.snapshots}
    roots = {s.owner: _snapshot_records(s, evidence)[0] for s in closure.snapshots}
    cohorts: list[frozenset[RoleSpec]] = []
    sources: list[tuple[OwnerRef, PackDirectoryClaim, frozenset[RoleSpec]]] = []

    def tree(inventory: _Roles, purpose: str) -> frozenset[RoleSpec]:
        key = RoleKey(None, purpose, "directory", None)
        return frozenset(
            r
            for r in inventory.values.values()
            if r.role == key or r.parent_role == key
        )

    def carrier_tree(inventory: _Roles, purpose: str) -> frozenset[RoleSpec]:
        return tree(inventory, purpose + "_carrier") | tree(
            inventory, purpose + "_captured"
        )

    for owner, inventory in inventories.items():
        row = roots[owner]
        if owner.domain == "pack_tombstone" and row["carrier_json"] is not None:
            claims = [("tombstone", evidence.claim(_text(row["carrier_json"])))]
        elif (
            owner.domain == "pack_reservation"
            and row["directory_ownership_json"] is not None
        ):
            proof = evidence.pack(_text(row["directory_ownership_json"]))
            claims = [
                (f"pack_claim_{i}", claim) for i, claim in enumerate(proof.claims)
            ]
            if (
                proof.phase == "attaching"
                and proof.private_directory is not None
                and proof.canonical_directory is not None
                and proof.inventory is not None
            ):
                private, canonical = proof.private_directory, proof.canonical_directory
                if (private.dev, private.inode, private.marker_fingerprint) == (
                    canonical.dev,
                    canonical.inode,
                    canonical.marker_fingerprint,
                ):
                    cohorts.append(tree(inventory, "pack_private"))
            placement = proof.placement_carrier
            if placement is not None and placement.phase in {"discarding", "discarded"}:
                physical = _join(placement.carrier_path, "artifact")
                group = (
                    tree(inventory, "pack_placement_carrier")
                    if placement.phase == "discarded"
                    else frozenset[RoleSpec]()
                )
                for purpose, directory in (
                    ("pack_private", proof.private_directory),
                    ("pack_canonical", proof.canonical_directory),
                ):
                    if directory is not None and directory.path == physical:
                        group |= tree(inventory, purpose)
                        if row["queue_id"] is not None and proof.inventory is not None:
                            queue_owner = OwnerRef(
                                "queue_source", (_integer(row["queue_id"], 1),)
                            )
                            queue = roots.get(queue_owner)
                            if queue is not None and _identity(
                                queue, "download_protocol"
                            ) == _identity(row):
                                files = {
                                    _join(physical, relative)
                                    for relative in proof.inventory.files
                                }
                                group |= frozenset(
                                    r
                                    for r in inventories[queue_owner].values.values()
                                    if (r.role.purpose == "source" and r.path in files)
                                    or (
                                        r.role
                                        == RoleKey(
                                            None, "source_directory", "directory", None
                                        )
                                        and r.path == physical
                                    )
                                )
                if group:
                    cohorts.append(group)
        else:
            continue
        for purpose, claim in claims:
            phase = claim.carrier.phase
            if phase in {"claimed", "discarding", "discarded"}:
                group = tree(inventory, purpose + "_source")
                if owner.domain == "pack_reservation":
                    proof = evidence.pack(_text(row["directory_ownership_json"]))
                    if (
                        proof.canonical_directory == claim.source_directory
                        and proof.inventory == claim.inventory
                    ):
                        group |= tree(inventory, "pack_canonical")
                cohorts.append(group)
                sources.append((owner, claim, group))
            if phase in {"discarding", "discarded"}:
                group = (
                    carrier_tree(inventory, purpose)
                    if phase == "discarded"
                    else tree(inventory, purpose + "_captured")
                )
                if phase == "discarded":
                    group |= frozenset(
                        r
                        for r in inventory.values.values()
                        if r.role == RoleKey(None, "tombstone_path", "directory", None)
                        and r.path == _join(claim.carrier.carrier_path, "artifact")
                    )
                cohorts.append(group)

    # Join aliases only through the captured queue and exact original generation.
    for owner, claim, source in sources:
        row = roots[owner]
        queue_id = row["queue_id"]
        if queue_id is None:
            continue
        identity = _identity(row)
        queue_owner = OwnerRef("queue_source", (_integer(queue_id, 1),))
        queue = roots.get(queue_owner)
        if queue is not None and _identity(queue, "download_protocol") == identity:
            aliases = set(source)
            if queue["src_dir"] == claim.source_directory.path:
                aliases.update(
                    r
                    for r in inventories[queue_owner].values.values()
                    if r.role == RoleKey(None, "source_directory", "directory", None)
                )
            for snapshot in closure.snapshots:
                if snapshot.owner != queue_owner:
                    continue
                _, related = _snapshot_records(snapshot, evidence)
                files = {
                    _integer(_mapping(r["row"])["id"], 1): _mapping(r["row"])
                    for r in related
                    if r["table"] == "import_queue_files"
                }
                paths = {
                    _join(claim.source_directory.path, relative)
                    for relative in claim.inventory.files
                }
                for role in inventories[queue_owner].values.values():
                    file = (
                        files.get(role.role.file_id)
                        if role.role.file_id is not None
                        else None
                    )
                    if (
                        role.role.purpose == "source"
                        and file is not None
                        and file["queue_id"] == queue_id
                        and file["src_path"] == role.path
                        and role.path in paths
                    ):
                        aliases.add(role)
            cohorts.append(frozenset(aliases))
        for snapshot in closure.snapshots:
            _, related = _snapshot_records(snapshot, evidence)
            publications = (
                [roots[snapshot.owner]]
                if snapshot.owner.domain == "publication"
                else [
                    _mapping(r["row"])
                    for r in related
                    if r["table"] == "import_publications"
                ]
                if snapshot.owner == queue_owner
                else []
            )
            for publication in publications:
                frozen = _mapping(evidence.load(publication["queue_snapshot_json"]))
                origins = frozen.get("_pack_source_origins")
                if (
                    frozen["id"] != queue_id
                    or _identity(frozen, "download_protocol") != identity
                    or origins is None
                ):
                    continue
                for file_id, raw in _mapping(_mapping(origins)["files"]).items():
                    origin = _mapping(raw)
                    if origin["kind"] != "pack":
                        continue
                    admitted = evidence.pack(_text(origin["ownership_json"]))
                    if (
                        admitted.artifact_owner_token != claim.artifact_owner_token
                        or admitted.canonical_directory != claim.source_directory
                        or admitted.inventory != claim.inventory
                        or origin["relative_path"] not in claim.inventory.files
                        or _fingerprint(origin["source_fingerprint"])
                        != claim.inventory.files[_text(origin["relative_path"])]
                    ):
                        continue
                    prefix = (
                        "origin_"
                        if snapshot.owner.domain == "publication"
                        else f"origin_{publication['id']}_"
                    ) + file_id
                    group = tree(inventories[snapshot.owner], prefix + "_canonical")
                    if snapshot.owner.domain == "publication":
                        values = inventories[snapshot.owner].values
                        fid = _integer(origin["file_id"], 1)
                        role = values.get(RoleKey(fid, "source", "file", None))
                        if role is not None and role.path == origin["src_path"]:
                            group |= frozenset({role})
                        protocol = _mapping(frozen.get("_file_publication", {}))
                        admission = protocol.get("admission")
                        if admission is not None:
                            files = _mapping(_mapping(admission)["files"])
                            mapping = _mapping(_mapping(files[file_id])["mapping"])
                            role = values.get(
                                RoleKey(fid, "admission_source", "file", None)
                            )
                            if (
                                role is not None
                                and mapping.get("queue_id") == queue_id
                                and mapping.get("id") == fid
                                and mapping.get("src_path")
                                == role.path
                                == origin["src_path"]
                            ):
                                group |= frozenset({role})
                    cohorts.append(source | group)
    return tuple(group for group in cohorts if group)


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
            count = 0
            try:
                for values in cursor:
                    counter.charge(rows=1)
                    row = dict(zip(names, values, strict=True))
                    raw = RawRow(
                        table,
                        tuple(cast(str | int, row[c]) for c in columns),
                        tuple(sorted(row.items())),
                    )
                    counter.charge(
                        hash_bytes=_row_bytes(
                            raw.values,
                            counter.budget.max_hash_bytes - counter.hash_bytes,
                        )
                    )
                    result[table].append(raw)
                    count += 1
            finally:
                cursor.close()
            if count < 64:
                break
            last = tuple(dict(result[table][-1].values)[c] for c in columns)
    return result


def _row_bytes(values: tuple[tuple[str, SQLScalar], ...], remaining: int) -> int:
    """Admit ASCII JSON evidence before allocating an escaped row document."""
    size = 2 + max(0, len(values) - 1)
    for name, scalar in values:
        size += 3  # pair brackets and comma
        for value in (name, scalar):
            if isinstance(value, str):
                size += 2
                for character in value:
                    code = ord(character)
                    size += (
                        2
                        if character in '\\"\b\t\n\f\r'
                        else 6
                        if code < 32 or 127 <= code <= 0xFFFF
                        else 12
                        if code > 0xFFFF
                        else 1
                    )
                    if size > remaining:
                        raise InvalidIdentity("inventory budget exceeded")
            else:
                size += len(_encode(value))
            if size > remaining:
                raise InvalidIdentity("inventory budget exceeded")
    if size > remaining:
        raise InvalidIdentity("inventory budget exceeded")
    return size


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
    if domain in {"rescan", "deletion"} and row.get("recovery_event_id") is not None:
        return True
    if domain == "publication":
        return row["state"] in _TERMINAL_PUBLICATIONS
    if domain == "rescan":
        return row["state"] in {"completed", "rolled_back"}
    if domain == "deletion":
        return row["state"] == "completed"
    return False


def _version(db: sqlite3.Connection) -> int:
    return cast(int, db.execute("PRAGMA data_version").fetchone()[0])


def _snapshot_from_rows(raw: dict[str, list[RawRow]], owner: OwnerRef) -> OwnerSnapshot:
    table, _pk = _ROOTS[owner.domain]
    matches = [r for r in raw[table] if r.key == owner.key]
    if len(matches) != 1:
        raise IdentityMismatch("owner raw root is missing")

    def project(record: RawRow) -> dict[str, object]:
        values = dict(record.values)
        return {
            "table": record.table,
            "key": list(record.key),
            "row": {
                c: values[c]
                for c in sorted(
                    _store._PROVISION_ROWS[record.table]
                    if record.table in _store._PROVISION_ROWS
                    else _store._PROJECTIONS[record.table]
                )
            },
        }

    root = matches[0]
    related: list[RawRow] = []
    version = 1
    if owner.domain == "namespace":
        epochs = [
            r
            for r in raw["file_identity_epochs"]
            if dict(r.values)["allocation_path"] == owner.key[0]
            and _mapping(_load(dict(r.values)["birth_json"]))["version"] == 2
        ]
        if len(epochs) > 1:
            raise InvalidIdentity("ownership_binding")
        if epochs:
            version = 2
            epoch = epochs[0]
            birth = _store._epoch_birth(_text(dict(epoch.values)["birth_json"]))
            iid = _mapping(birth["controlled_birth"])["intent_id"]
            intents = [
                r
                for r in raw.get("file_identity_provision_intents", [])
                if r.key == (iid,)
            ]
            if len(intents) != 1:
                raise InvalidIdentity("ownership_binding")
            related = [epoch, *intents]
    if owner.domain in {"publication", "queue_source"}:
        queue_id = (
            dict(root.values)["queue_id"]
            if owner.domain == "publication"
            else owner.key[0]
        )
        publications = {
            r.key[0]
            for r in raw["import_publications"]
            if dict(r.values)["queue_id"] == queue_id
        }
        related = [
            r
            for name in (
                "import_queue",
                "import_queue_files",
                "import_publications",
                "import_publication_files",
            )
            for r in raw[name]
            if (
                r.key[0] == queue_id
                if name == "import_queue"
                else dict(r.values)["publication_id"] in publications
                if name == "import_publication_files"
                else dict(r.values)["queue_id"] == queue_id
            )
            and r != root
        ]
    encoded = _encode(
        {
            "version": version,
            "owner": {"domain": owner.domain, "key": list(owner.key)},
            "root": project(root),
            "related": [project(r) for r in sorted(related, key=_raw_order)],
        }
    )
    return OwnerSnapshot(owner, encoded, hashlib.sha256(encoded.encode()).hexdigest())


class _Ledger:
    """Inventory-only immutable history; never active-CAS or qualification authority."""

    def __init__(
        self, raw: dict[str, list[RawRow]], evidence: _Evidence, counter: _Counter
    ) -> None:
        self.raw = raw
        self.evidence = evidence
        self.counter = counter
        self.index = {
            table: {r.key[0]: r for r in raw.get(table, [])}
            for table in _PURE_LEDGER_KEYS
        }
        self.sets: dict[OwnerRef, list[str]] = {}
        self.events: dict[OwnerRef, list[dict[str, object]]] = {}
        self.snapshots: dict[str, OwnerSnapshot] = {}
        self.specs: dict[str, tuple[RoleSpec, ...]] = {}
        self.proofs: dict[str, _store.ProofSet] = {}
        self.rows: set[RawRow] = set()
        self.validated: set[OwnerRef] = set()
        self.dependencies: dict[OwnerRef, set[OwnerRef]] = {}
        self.links: dict[OwnerRef, set[OwnerRef]] = {}
        self.frozen_dependencies: dict[str, tuple[OwnerSnapshot, ...]] = {}
        self.intents: dict[str, dict[str, object]] = {}
        reservations: list[tuple[str, str]] = []
        for record in raw.get("file_identity_provision_intents", []):
            row = dict(record.values)
            if set(row) != _REQUIRED_RAW[record.table]:
                raise InvalidIdentity("ownership_wire")
            intent = _store._provision_intent(_text(row["payload_json"]))
            iid, eid = _hex(row["intent_id"]), _hex(intent["epoch_id"])
            if (
                iid != intent["allocation_token"]
                or row["authority_id"] != intent["authority_id"]
                or row["payload_sha256"]
                != hashlib.sha256(_encode(intent).encode()).hexdigest()
            ):
                raise InvalidIdentity("ownership_binding")
            authority_record = self.index["file_identity_authorities"].get(
                _hex(row["authority_id"])
            )
            if authority_record is None:
                raise InvalidIdentity("ownership_authority")
            authority = dict(authority_record.values)
            if (
                authority["incarnation_token"] != intent["authority_incarnation"]
                or self.evidence.load(authority["profile_json"]) != intent["profile"]
                or self.evidence.load(authority["declaration_json"]) != intent["scope"]
                or authority["qualification_sha256"]
                != _mapping(intent["profile"])["qualification_sha256"]
            ):
                raise InvalidIdentity("ownership_authority")
            allocation = _path(intent["allocation_path"])
            if any(
                old_eid == eid or _within(allocation, path) or _within(path, allocation)
                for old_eid, path in reservations
            ):
                raise InvalidIdentity("ownership_binding")
            for epoch_record in raw["file_identity_epochs"]:
                epoch = dict(epoch_record.values)
                if epoch["epoch_id"] == eid:
                    birth = _store._epoch_birth(_text(epoch["birth_json"]))
                    if (
                        birth["version"] != 2
                        or _mapping(birth["controlled_birth"])["intent_id"] != iid
                    ):
                        raise InvalidIdentity("ownership_binding")
                elif _within(allocation, _path(epoch["root_path"])) or allocation in {
                    epoch["allocation_path"],
                    epoch["root_path"],
                }:
                    raise InvalidIdentity("ownership_binding")
            reservations.append((eid, allocation))
            self.intents[iid] = intent
        for record in raw["file_identity_sets"]:
            row = dict(record.values)
            owner = self.owner(row)
            set_id = _hex(row["set_id"])
            snapshot = OwnerSnapshot(
                owner, _text(row["snapshot_json"]), _hex(row["snapshot_sha256"], 64)
            )
            self.snapshots[set_id] = snapshot
            inventory = _inventory(snapshot, counter, evidence)
            self.specs[set_id] = tuple(inventory.values.values())
            manifest = _mapping(evidence.load(row["manifest_json"]))
            dependencies = tuple(_store._dependencies(manifest, owner))
            self.frozen_dependencies[set_id] = dependencies
            self.links.setdefault(owner, set()).update(d.owner for d in dependencies)
            for dependency in dependencies:
                frozen = _inventory(dependency, counter, evidence)
                self.specs[set_id] += tuple(frozen.values.values())
            self.sets.setdefault(owner, []).append(set_id)
        for record in raw["file_identity_events"]:
            row = cast(dict[str, object], dict(record.values))
            self.events.setdefault(self.owner(row), []).append(row)

    def document(self, encoded: object, keys: set[str]) -> dict[str, object]:
        value = _mapping(self.evidence.load(encoded), keys | {"version"})
        _integer(value["version"], 1, 1)
        if _encode(value) != encoded:
            raise InvalidIdentity("identity ledger JSON is not canonical")
        return value

    def owner(self, row: Mapping[str, object]) -> OwnerRef:
        key = self.document(row["owner_key_json"], {"key"})
        owner = OwnerRef(
            _text(row["owner_domain"]), tuple(cast(list[str | int], _list(key["key"])))
        )
        _owner(owner)
        return owner

    def require(self, table: str, key: str | int) -> dict[str, object]:
        record = self.index[table].get(key)
        if record is None:
            raise IdentityMismatch("identity ledger reference is dangling")
        self.rows.add(record)
        return cast(dict[str, object], dict(record.values))

    def seeds(self, parent: str) -> set[OwnerRef]:
        return {
            self.snapshots[s].owner
            for s, specs in self.specs.items()
            if any(_within(r.path, parent) for r in specs)
        }

    def neighbours(self, owners: set[OwnerRef]) -> set[OwnerRef]:
        result: set[OwnerRef] = set()
        for owner, requires in self.links.items():
            if owner in owners or requires.intersection(owners):
                result.update((owner, *requires))
        return result

    def epoch(self, row: dict[str, object], snapshot: OwnerSnapshot) -> tuple[str, str]:
        epoch_id = _hex(row["epoch_id"])
        epoch = self.require("file_identity_epochs", epoch_id)
        authority_id = _hex(epoch["authority_id"])
        authority = self.require("file_identity_authorities", authority_id)
        profile = self.document(
            authority["profile_json"],
            {
                "profile_id",
                "revision",
                "qualification_sha256",
                "client_contract",
                "server_contract",
            },
        )
        StorageProfile(
            _text(profile["profile_id"]),
            _integer(profile["revision"], 1),
            _hex(profile["qualification_sha256"], 64),
            _mapping(profile["client_contract"]),
            _mapping(profile["server_contract"]),
        )
        if (
            _hex(authority["qualification_sha256"], 64)
            != profile["qualification_sha256"]
        ):
            raise InvalidIdentity("authority qualification digest mismatch")
        declaration = self.document(
            authority["declaration_json"], {"scope_path", "scope_token"}
        )
        scope = _path(declaration["scope_path"])
        _hex(declaration["scope_token"])
        incarnation = _hex(authority["incarnation_token"])
        birth = _store._epoch_birth(_text(epoch["birth_json"]))
        if birth["version"] == 2:
            receipt = _mapping(birth["controlled_birth"])
            intent_row = self.require(
                "file_identity_provision_intents", _hex(receipt["intent_id"])
            )
            intent = _store._provision_intent(_text(intent_row["payload_json"]))
            if (
                intent_row["intent_id"] != intent["allocation_token"]
                or intent_row["payload_sha256"]
                != hashlib.sha256(_encode(intent).encode()).hexdigest()
            ):
                raise InvalidIdentity("ownership_binding")
            if (
                intent["authority_id"] != authority_id
                or intent_row["authority_id"] != authority_id
                or intent["authority_incarnation"] != incarnation
                or intent["profile"] != profile
                or intent["scope"] != declaration
            ):
                raise InvalidIdentity("ownership_authority")
            for name in (
                "epoch_id",
                "authority_id",
                "origin_path",
                "allocation_path",
                "root_path",
            ):
                if epoch[name] != intent[name] or epoch[name] != receipt[name]:
                    raise InvalidIdentity("ownership_binding")
            if (
                snapshot.owner != _store._owner_from(intent["namespace_owner"])
                or _mapping(_load(snapshot.rows_json))["version"] != 2
            ):
                raise InvalidIdentity("ownership_binding")
            return epoch_id, authority_id
        birth = self.document(
            epoch["birth_json"],
            {"layout", "enrollment", "legacy_namespace", "controlled_birth"},
        )
        enrollment = _text(birth["enrollment"])
        if enrollment not in {"birth", "exact_legacy", "recovery"}:
            raise InvalidIdentity("invalid epoch enrollment")
        origin, allocation, root = (
            _path(epoch[c]) for c in ("origin_path", "allocation_path", "root_path")
        )
        if (
            not _within(origin, scope)
            or _parent(root) != allocation
            or root == allocation
        ):
            raise InvalidIdentity("epoch scope geometry mismatch")
        if birth["layout"] == "same_parent":
            valid = allocation == origin
        elif birth["layout"] == "dedicated_parent":
            valid = _parent(allocation) == origin and allocation != origin
        else:
            valid = False
        if not valid:
            raise InvalidIdentity("epoch allocation geometry mismatch")
        legacy, controlled = birth["legacy_namespace"], birth["controlled_birth"]
        if legacy is not None:
            _store._legacy(legacy, namespace=True)
        if controlled is not None:
            receipt = _mapping(
                controlled,
                {
                    "authority_incarnation",
                    "allocation_token",
                    "root_native",
                    "marker_native",
                    "receipt_sha256",
                },
            )
            if _hex(receipt["authority_incarnation"]) != incarnation:
                raise InvalidIdentity("epoch incarnation mismatch")
            _hex(receipt["allocation_token"])
            _hex(receipt["receipt_sha256"], 64)
            NativeHandle.from_value(receipt["root_native"])
            NativeHandle.from_value(receipt["marker_native"])
        if (
            (enrollment == "birth" and (controlled is None or legacy is not None))
            or (
                enrollment == "exact_legacy"
                and (legacy is None or controlled is not None)
            )
            or (legacy is None and controlled is None)
        ):
            raise InvalidIdentity("epoch receipt/enrollment mismatch")
        return epoch_id, authority_id

    def validate(self, owner: OwnerRef) -> set[OwnerRef]:
        if owner in self.validated:
            return self.dependencies[owner]
        dependencies: set[OwnerRef] = set()
        self.dependencies[owner] = dependencies
        prior_edges: dict[str, list[str]] = {}
        for set_id in self.sets.get(owner, []):
            row = self.require("file_identity_sets", set_id)
            prior = None if row["prior_set_id"] is None else _hex(row["prior_set_id"])
            if prior is not None and prior not in self.sets.get(owner, []):
                raise IdentityMismatch(
                    "identity prior set belongs to another owner or is missing"
                )
            prior_edges[set_id] = [] if prior is None else [prior]
            snapshot = self.snapshots[set_id]
            epoch_id, authority_id = self.epoch(row, snapshot)
            manifest_json = _text(row["manifest_json"])
            if hashlib.sha256(manifest_json.encode()).hexdigest() != _hex(
                row["manifest_sha256"], 64
            ):
                raise InvalidIdentity("stored manifest digest mismatch")
            manifest = _mapping(self.evidence.load(manifest_json))
            proof = _store.ProofSet(
                set_id,
                owner,
                snapshot,
                authority_id,
                epoch_id,
                _text(row["origin"]),
                tuple(_store._object_from(o) for o in _list(manifest["roles"])),
                manifest_json,
            )
            declaration = self.document(
                self.require("file_identity_authorities", authority_id)[
                    "declaration_json"
                ],
                {"scope_path", "scope_token"},
            )
            if any(
                not _within(o.canonical_path, _path(declaration["scope_path"]))
                for o in proof.roles
            ):
                raise InvalidIdentity("object outside declared authority scope")
            self.proofs[set_id] = proof
            for dependency in self.frozen_dependencies[set_id]:
                dependencies.add(dependency.owner)
        _store._acyclic(prior_edges)
        previous: dict[str, object] | None = None
        seen: set[tuple[str, str]] = set()
        capabilities: dict[str, tuple[int, dict[str, object]]] = {}
        provisioned = any(
            _mapping(self.evidence.load(self.snapshots[s].rows_json))["version"] == 2
            for s in self.sets.get(owner, [])
        )
        events = sorted(
            self.events.get(owner, []), key=lambda r: _integer(r["revision"], 1)
        )
        for revision, event in enumerate(events, 1):
            self.require("file_identity_events", _integer(event["id"], 1))
            if _integer(event["revision"], 1) != revision or event[
                "predecessor_event_id"
            ] != (None if previous is None else previous["id"]):
                if provisioned:
                    raise InvalidIdentity("ownership_phase")
                raise InvalidIdentity("identity event predecessor/revision mismatch")
            set_id = _hex(event["set_id"])
            proof = self.proofs.get(set_id)
            if proof is None or proof.owner != owner:
                raise IdentityMismatch(
                    "identity event belongs to a missing or foreign set"
                )
            kind = _text(event["kind"])
            if kind in {"prepared", "activated"}:
                payload = self.document(
                    event["payload_json"], {"snapshot_sha256", "manifest_sha256"}
                )
                if (
                    payload["snapshot_sha256"] != proof.snapshot.sha256
                    or payload["manifest_sha256"]
                    != hashlib.sha256(proof.manifest_json.encode()).hexdigest()
                    or (set_id, kind) in seen
                ):
                    raise InvalidIdentity("identity event digest/uniqueness mismatch")
                seen.add((set_id, kind))
                set_row = self.require("file_identity_sets", set_id)
                if kind == "prepared" and set_row["prior_set_id"] != (
                    None if previous is None else previous["set_id"]
                ):
                    raise InvalidIdentity("identity preparation predecessor mismatch")
                if kind == "activated" and (
                    previous is None
                    or previous["kind"] != "prepared"
                    or previous["set_id"] != set_id
                ):
                    if provisioned:
                        raise InvalidIdentity("ownership_phase")
                    raise InvalidIdentity("identity activation predecessor mismatch")
            elif kind in {
                "recovery_intent",
                "recovery_step",
                "superseded",
                "retained",
                "retired",
            }:
                version = _mapping(self.evidence.load(event["payload_json"])).get(
                    "version"
                )
                if type(version) is int and version == 2:
                    payload = _store._allocation_payload(_text(event["payload_json"]))
                    if kind != "recovery_step":
                        raise InvalidIdentity("ownership_wire")
                    self.capability(event, previous, payload, proof, capabilities)
                    previous = event
                    continue
                payload = self.document(
                    event["payload_json"],
                    {"before_snapshot_json", "before_snapshot_sha256", "plan_sha256"},
                )
                if (
                    payload["before_snapshot_json"] != proof.snapshot.rows_json
                    or payload["before_snapshot_sha256"] != proof.snapshot.sha256
                ):
                    raise InvalidIdentity("identity event preimage mismatch")
                _hex(payload["plan_sha256"], 64)
            else:
                raise InvalidIdentity("unknown identity event kind")
            previous = event
        for set_id in self.sets.get(owner, []):
            proof = self.proofs[set_id]
            manifest = _mapping(self.evidence.load(proof.manifest_json))
            intent = manifest["intent"]
            if intent is not None and _mapping(intent)["kind"] == "recovery":
                intent = _mapping(intent)
                event = self.require(
                    "file_identity_events", _integer(intent["event_id"], 1)
                )
                payload = self.document(
                    event["payload_json"],
                    {"before_snapshot_json", "before_snapshot_sha256", "plan_sha256"},
                )
                if (
                    self.owner(event) != owner
                    or event["kind"] not in {"recovery_intent", "recovery_step"}
                    or payload["before_snapshot_json"] != proof.snapshot.rows_json
                    or payload["before_snapshot_sha256"] != proof.snapshot.sha256
                    or payload["plan_sha256"] != intent["plan_sha256"]
                ):
                    raise IdentityMismatch(
                        "recovery intent snapshot/owner/plan mismatch"
                    )
        self.validated.add(owner)
        return dependencies

    def capability(
        self,
        event: dict[str, object],
        previous: dict[str, object] | None,
        payload: dict[str, object],
        proof: _store.ProofSet,
        transitions: dict[str, tuple[int, dict[str, object]]],
    ) -> None:
        snapshot = _mapping(_load(proof.snapshot.rows_json))
        if snapshot["version"] != 2:
            raise InvalidIdentity("ownership_phase")
        intent, birth = _store._provision_snapshot(snapshot)
        epoch = self.require("file_identity_epochs", proof.epoch_id)
        if (
            payload["before_snapshot_json"] != proof.snapshot.rows_json
            or payload["before_snapshot_sha256"] != proof.snapshot.sha256
            or payload["epoch_id"] != proof.epoch_id
            or payload["intent_id"] != birth["intent_id"]
            or payload["birth_sha256"]
            != hashlib.sha256(
                _store._encode(_store._epoch_birth(_text(epoch["birth_json"]))).encode()
            ).hexdigest()
        ):
            raise InvalidIdentity("ownership_binding")
        for name in (
            "authority_id",
            "authority_incarnation",
            "origin_path",
            "allocation_path",
            "root_path",
        ):
            if payload[name] != intent[name]:
                raise InvalidIdentity("ownership_authority")
        activation = self.require(
            "file_identity_events", _integer(payload["activation_event_id"], 1)
        )
        if (
            activation["kind"] != "activated"
            or activation["set_id"] != proof.set_id
            or self.owner(activation) != proof.owner
        ):
            raise InvalidIdentity("ownership_phase")
        prepared = self.require(
            "file_identity_events", _integer(activation["predecessor_event_id"], 1)
        )
        if (
            prepared["kind"] != "prepared"
            or prepared["set_id"] != proof.set_id
            or self.owner(prepared) != proof.owner
            or _integer(activation["revision"], 1)
            != _integer(prepared["revision"], 1) + 1
        ):
            raise InvalidIdentity("ownership_phase")
        prior = transitions.get(proof.epoch_id)
        if payload["state"] == "grant":
            if (
                prior is not None
                or previous != activation
                or payload["plan_sha256"] != intent["plan_sha256"]
            ):
                raise InvalidIdentity("ownership_phase")
        else:
            if (
                prior is None
                or prior[1]["state"] != "grant"
                or payload["prior_capability_event_id"] != prior[0]
            ):
                raise InvalidIdentity("ownership_phase")
            if any(
                payload[name] != prior[1][name]
                for name in payload
                if name not in {"state", "prior_capability_event_id", "plan_sha256"}
            ):
                raise InvalidIdentity("ownership_binding")
        transitions[proof.epoch_id] = _integer(event["id"], 1), payload

    def recovery_flag(self, owner: OwnerRef, row: dict[str, object]) -> None:
        if owner.domain not in {"rescan", "deletion"}:
            return
        flag = row.get("recovery_event_id")
        if flag is None:
            return
        self.validate(owner)
        event = self.require("file_identity_events", _integer(flag, 1))
        if self.owner(event) != owner or event["kind"] != "superseded":
            raise IdentityMismatch(
                "journal recovery flag has a foreign or wrong-kind event"
            )
        proof = self.proofs[_hex(event["set_id"])]
        frozen = _mapping(
            _mapping(self.evidence.load(proof.snapshot.rows_json))["root"]
        )
        before = _mapping(frozen["row"])
        projected = {c: row[c] for c in _store._PROJECTIONS[_ROOTS[owner.domain][0]]}
        if before.get("recovery_event_id") is not None or projected != {
            **before,
            "recovery_event_id": flag,
        }:
            raise IdentityMismatch("journal recovery flag preimage differs")


def _validate_closure(
    closure: ReferenceClosure, parent: str, budget: InventoryBudget
) -> None:
    raw: dict[str, list[RawRow]] = {table: [] for table in _PURE_TABLES}
    for row in closure.raw_rows:
        row.__post_init__()
        raw[row.table].append(row)
    try:
        rebuilt = _bind_references(
            raw,
            parent,
            InventoryBudget(budget.max_rows, budget.max_objects, _JSON_LIMIT),
        )
    except IdentityMismatch as exc:
        raise InvalidIdentity("inconsistent observation SQL closure") from exc
    if rebuilt != closure:
        raise InvalidIdentity("observation closure omits or contradicts SQL evidence")


def _bind_references(
    raw: dict[str, list[RawRow]],
    parent: str,
    budget: InventoryBudget,
    counter: _Counter | None = None,
    expected: dict[OwnerRef, OwnerSnapshot] | None = None,
) -> ReferenceClosure:
    """Pure fixed-schema binding shared by SQL acquisition and wire validation."""
    parent = _path(parent)
    if type(budget) is not InventoryBudget:
        raise InvalidIdentity("expected inventory budget")
    budget.__post_init__()
    counter = counter or _Counter(budget)
    evidence = _Evidence()
    raw = {
        **raw,
        "file_identity_provision_intents": raw.get(
            "file_identity_provision_intents", []
        ),
    }
    try:
        for table, records in raw.items():
            if len({r.key for r in records}) != len(records):
                raise InvalidIdentity("inventory raw table/key is duplicated")
            for record in records:
                if not _REQUIRED_RAW[table] <= dict(record.values).keys():
                    raise InvalidIdentity("inventory raw row omits required columns")
        ledger = _Ledger(raw, evidence, counter)
        for iid, intent in ledger.intents.items():
            if _within(_path(intent["origin_path"]), parent):
                _ = ledger.require("file_identity_provision_intents", iid)
                _ = ledger.require(
                    "file_identity_authorities", _hex(intent["authority_id"])
                )
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
                snapshot = _snapshot_from_rows(raw, owner)
                if expected is not None and expected.get(owner) != snapshot:
                    raise IdentityMismatch("same-connection SQL drift during inventory")
                root, related = _snapshot_records(snapshot, evidence)
                if expected is None:
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
        relevant_history = ledger.seeds(parent)
        selected.update(relevant_history.intersection(snapshots))
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
            before = set(selected), set(relevant_history)
            relevant_history.update(selected)
            relevant_history.update(ledger.neighbours(relevant_history))
            selected.update(relevant_history.intersection(snapshots))
            for owner in tuple(relevant_history):
                for target in ledger.validate(owner):
                    relevant_history.add(target)
                    if target in snapshots:
                        selected.add(target)
                        if owner in snapshots and owner != target:
                            edges.add(
                                ReferenceDependency(owner, target, "identity_ledger")
                            )
                if owner in selected:
                    ledger.recovery_flag(owner, roots[owner])
            for edge in edges:
                if edge.owner in selected or edge.requires in selected:
                    selected.update((edge.owner, edge.requires))
            for owner in selected:
                series_ids.update(context_series[owner])
            for owner, row in roots.items():
                if row.get("series_id") in series_ids:
                    selected.add(owner)
            if (selected, relevant_history) == before:
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
        if root_ids - {None} - {r.key[0] for r in raw["root_folders"]}:
            raise IdentityMismatch("reference root folder context is dangling")
        selected_raw.update(r for r in raw["root_folders"] if r.key[0] in root_ids)
        selected_raw.update(ledger.rows)
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
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidIdentity("invalid inventory raw binding") from exc
    return result


def collect_references(
    db: sqlite3.Connection, parent: str, budget: InventoryBudget
) -> ReferenceClosure:
    """Capture a coherent bounded graph, releasing only our DEFERRED read view."""
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
    try:
        raw = _scan(db, counter)
        expected: dict[OwnerRef, OwnerSnapshot] = {}
        for domain, (table, _pk) in _ROOTS.items():
            for row in raw[table]:
                owner = OwnerRef(domain, row.key)
                snapshot = capture_owner_snapshot(db, owner)
                # Admit each capture before retaining the expectation collection.
                counter.charge(hash_bytes=len(snapshot.rows_json.encode("utf-8")))
                _, related = _snapshot_records(snapshot, _Evidence())
                counter.charge(rows=1 + len(related))
                expected[owner] = snapshot
        result = _bind_references(raw, parent, budget, counter, expected)
        if db.total_changes != changes:
            raise IdentityMismatch("same-connection SQL drift during inventory")
    except sqlite3.Error as exc:
        raise InvalidIdentity("SQL reference inventory could not be completed") from exc
    finally:
        db.rollback()
    if _version(db) != version or db.total_changes != changes:
        raise IdentityMismatch("SQL reference inventory changed during capture")
    return result
