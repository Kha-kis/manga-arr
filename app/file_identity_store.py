"""Private immutable SQL evidence, disabled until source and role qualification.

This module does not establish filesystem ownership. Callers provide observed
evidence and own writer transactions; later domain adapters must close semantic
role/reference policy before any storage profile can be enabled.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from typing import cast

from file_identity import (
    IdentityMismatch,
    IdentityUnavailable,
    InvalidIdentity,
    NativeHandle,
    OwnerRef,
    RoleKey,
    StorageProfile,
    validate_profile,
)

_MAX_JSON = 16 * 1024 * 1024
_MAX_RECORDS = 10_000
_HEX = frozenset("0123456789abcdef")
_DOMAINS = {
    "namespace": "file_claim_namespaces",
    "rescan": "rescan_file_operations",
    "publication": "import_publications",
    "deletion": "volume_file_deletions",
    "pack_reservation": "import_pack_cleanup_reservations",
    "pack_tombstone": "import_pack_cleanup_tombstones",
    "queue_source": "import_queue",
}


def _columns(text: str, integers: str, reals: str = "") -> dict[str, str]:
    return {
        **dict.fromkeys(text.split(), "text"),
        **dict.fromkeys(integers.split(), "integer"),
        **dict.fromkeys(reals.split(), "real"),
    }


# Static authority projections, shared only with the schema's preimage guards.
# Reporting/notification state is deliberately absent. No caller supplies SQL
# names, and opaque legacy JSON columns are copied as exact strings.
_PROJECTIONS: dict[str, dict[str, str]] = {
    "file_claim_namespaces": _columns("parent_path ownership_json", ""),
    "rescan_file_operations": _columns(
        "operation_token source_path destination_path expected_volume_json "
        "expected_context_json fingerprints_json carriers_json publication_receipt_json state",
        "id version series_id volume_id recovery_event_id",
    ),
    "volume_file_deletions": _columns(
        "state target_path parent_path claim_path target_sha256 series_title "
        "source_title original_import_path claim_carrier_json",
        "id volume_id series_id target_present target_dev target_inode target_size "
        "target_mtime_ns recovery_event_id",
        "volume_num",
    ),
    "import_pack_cleanup_reservations": _columns(
        "download_identity_key protocol normalized_download_id download_id purpose "
        "owner_token pack_path tombstone_path directory_ownership_json expires_at",
        "download_client_id queue_id publication_id",
    ),
    "import_pack_cleanup_tombstones": _columns(
        "tombstone_path download_identity_key protocol normalized_download_id "
        "download_id pack_path carrier_json",
        "download_client_id queue_id publication_id",
    ),
    "import_queue": _columns(
        "download_id download_protocol torrent_name torrent_url src_dir status "
        "failed_at lease_owner lease_expires_at",
        "id series_id download_client_id respect_grab_claims",
        "volume_num",
    ),
    "import_queue_files": _columns(
        "filename src_path dst_path proposed_import_kind proposed_special_title "
        "status file_type proposed_pack_type",
        "id queue_id proposed_is_special",
        "proposed_volume proposed_chapter proposed_volume_range_start "
        "proposed_volume_range_end proposed_chapter_range_end",
    ),
    "import_publications": _columns(
        "state owner_token operation_owner operation_expires_at dst_dir import_mode "
        "staging_dir queue_snapshot_json series_snapshot_json series_tags_json "
        "queue_status queue_download_id queue_torrent_name queue_torrent_url "
        "queue_src_dir queue_failed_at queue_lease_owner queue_lease_expires_at "
        "queue_created_at result_queue_status pack_cleanup_state",
        "id queue_id series_id queue_download_client_id result_ok result_imported_count",
        "queue_volume_num",
    ),
    "import_publication_files": _columns(
        "src_path filename dst_path import_kind file_type pack_type special_title "
        "plan_status stage_path final_path source_sha256 source_claim_path "
        "staged_sha256 prepared_final_sha256 final_claim_path stage_state "
        "publish_state cleanup_state final_claim_carrier_json source_claim_carrier_json",
        "id publication_id ordinal file_id is_special has_volume_range "
        "is_legacy_chapter_stub is_legacy_chapter_recheck stage_ok source_dev "
        "source_inode source_size source_mtime_ns staged_dev staged_inode "
        "staged_size staged_mtime_ns final_expected_absent prepared_final_dev "
        "prepared_final_inode prepared_final_size prepared_final_mtime_ns",
        "proposed_vol proposed_chap chap_range_end vol_range_start vol_range_end",
    ),
}
_PKS = {
    table: (
        "parent_path"
        if table == "file_claim_namespaces"
        else "download_identity_key"
        if table == "import_pack_cleanup_reservations"
        else "tombstone_path"
        if table == "import_pack_cleanup_tombstones"
        else "id"
    )
    for table in _PROJECTIONS
}


def _text(value: object, *, empty: bool = False) -> str:
    if type(value) is not str or (not empty and not value):
        raise InvalidIdentity("identity text has an invalid type or is empty")
    result = cast(str, value)
    if "\x00" in result or any(0xD800 <= ord(c) <= 0xDFFF for c in result):
        raise InvalidIdentity("identity text contains invalid characters")
    return result


def _integer(value: object, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not minimum <= cast(int, value) <= maximum:
        raise InvalidIdentity("identity integer has an invalid type or range")
    return cast(int, value)


def _hex(value: object, size: int = 64) -> str:
    text = _text(value)
    if len(text) != size or any(c not in _HEX for c in text):
        raise InvalidIdentity("invalid identity hex")
    return text


def _digest(encoded: str) -> str:
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _path(value: object) -> str:
    path = _text(value)
    if (
        not path.startswith("/")
        or "\\" in path
        or (path != "/" and any(p in {"", ".", ".."} for p in path[1:].split("/")))
    ):
        raise InvalidIdentity("identity path is not canonical absolute")
    return path


def _within(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip("/") + "/")


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] or "/"


def _object(value: object, keys: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise InvalidIdentity("invalid identity object fields")
    return cast(dict[str, object], value)


def _version(value: object, keys: set[str]) -> dict[str, object]:
    record = _object(value, keys | {"version"})
    _integer(record["version"], 1, 1)
    return record


def _list(value: object) -> list[object]:
    if type(value) is not list or len(cast(list[object], value)) > _MAX_RECORDS:
        raise InvalidIdentity("identity record count exceeds bound or is not a list")
    return cast(list[object], value)


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidIdentity("duplicate identity JSON key")
        result[key] = value
    return result


def _constant(_value: str) -> object:
    raise InvalidIdentity("nonfinite identity JSON")


def _finite(value: object, depth: int = 0) -> None:
    if depth > 32:
        raise InvalidIdentity("identity JSON nesting exceeds bound")
    if value is None or type(value) in (bool, int):
        return
    if type(value) is str:
        _text(value, empty=True)
        return
    if type(value) is float and math.isfinite(cast(float, value)):
        return
    if type(value) is list:
        for child in cast(list[object], value):
            _finite(child, depth + 1)
        return
    if type(value) is dict:
        for key, child in cast(dict[str, object], value).items():
            _text(key)
            _finite(child, depth + 1)
        return
    raise InvalidIdentity("identity JSON requires finite values")


def _encode(value: object) -> str:
    _finite(value)
    encoded = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _MAX_JSON:
        raise InvalidIdentity("identity JSON exceeds document bound")
    return encoded


def _load(encoded: object) -> object:
    text = _text(encoded)
    if len(text) > _MAX_JSON or len(text.encode("utf-8")) > _MAX_JSON:
        raise InvalidIdentity("identity JSON exceeds document bound")
    try:
        value: object = json.loads(
            text, object_pairs_hook=_pairs, parse_constant=_constant
        )
        if _encode(value) != text:
            raise InvalidIdentity("identity JSON is not canonical")
        return value
    except (ValueError, TypeError, RecursionError) as exc:
        raise InvalidIdentity("invalid identity JSON") from exc


def _owner(owner: OwnerRef) -> OwnerRef:
    if (
        type(owner) is not OwnerRef
        or owner.domain not in _DOMAINS
        or len(owner.key) != 1
    ):
        raise InvalidIdentity("invalid ledger owner")
    table = _DOMAINS[owner.domain]
    if _PROJECTIONS[table][_PKS[table]] == "integer":
        _integer(owner.key[0], 1)
    else:
        _text(owner.key[0])
        if owner.domain in {"namespace", "pack_tombstone"}:
            _path(owner.key[0])
    return owner


def _owner_value(owner: OwnerRef) -> dict[str, object]:
    _owner(owner)
    return {"domain": owner.domain, "key": list(owner.key)}


def _owner_from(value: object) -> OwnerRef:
    record = _object(value, {"domain", "key"})
    key = _list(record["key"])
    if len(key) != 1 or type(key[0]) not in (str, int):
        raise InvalidIdentity("invalid ledger owner key")
    return _owner(OwnerRef(_text(record["domain"]), (cast(str | int, key[0]),)))


def _key_json(owner: OwnerRef) -> str:
    _owner(owner)
    return _encode({"version": 1, "key": list(owner.key)})


def _row(value: object, allowed: set[str]) -> dict[str, object]:
    record = _object(value, {"table", "key", "row"})
    table = _text(record["table"])
    if table not in allowed:
        raise InvalidIdentity("unexpected snapshot table")
    columns = _PROJECTIONS[table]
    row = _object(record["row"], set(columns))
    for column, kind in columns.items():
        scalar = row[column]
        if scalar is None:
            continue
        if kind == "integer":
            _integer(scalar, -(2**63))
        elif kind == "real":
            if type(scalar) not in (int, float) or not math.isfinite(
                cast(float, scalar)
            ):
                raise InvalidIdentity("invalid snapshot real scalar")
        else:
            _text(scalar, empty=True)
    key = _list(record["key"])
    pk = _PKS[table]
    if len(key) != 1 or type(key[0]) is not type(row[pk]) or key[0] != row[pk]:
        raise InvalidIdentity("snapshot primary key mismatch")
    if columns[pk] == "integer":
        _integer(key[0], 1)
    else:
        _text(key[0])
    return record


def _row_order(record: dict[str, object]) -> tuple[str, str]:
    return _text(record["table"]), _encode(record["key"])


@dataclass(frozen=True, slots=True)
class OwnerSnapshot:
    owner: OwnerRef
    rows_json: str
    sha256: str

    def __post_init__(self) -> None:
        _owner(self.owner)
        value = _version(_load(self.rows_json), {"owner", "root", "related"})
        if _owner_from(value["owner"]) != self.owner:
            raise InvalidIdentity("snapshot owner mismatch")
        table = _DOMAINS[self.owner.domain]
        root = _row(value["root"], {table})
        if root["key"] != list(self.owner.key):
            raise InvalidIdentity("snapshot root key mismatch")
        allowed = (
            {
                "import_queue",
                "import_queue_files",
                "import_publications",
                "import_publication_files",
            }
            if self.owner.domain in {"publication", "queue_source"}
            else set()
        )
        records = [_row(item, allowed) for item in _list(value["related"])]
        orders = [_row_order(item) for item in records]
        if orders != sorted(set(orders)) or _row_order(root) in orders:
            raise InvalidIdentity("snapshot related rows duplicate or unordered")
        if len(records) + 1 > _MAX_RECORDS:
            raise InvalidIdentity("snapshot row count exceeds bound")
        if _hex(self.sha256) != _digest(self.rows_json):
            raise InvalidIdentity("snapshot digest mismatch")


def _select(
    db: sqlite3.Connection, table: str, where: str, params: tuple[object, ...]
) -> list[dict[str, object]]:
    columns = sorted(_PROJECTIONS[table])
    cursor = db.execute(
        f"SELECT {','.join(columns)} FROM {table} WHERE {where} LIMIT {_MAX_RECORDS + 1}",
        params,
    )
    rows = cursor.fetchmany(_MAX_RECORDS + 1)
    if len(rows) > _MAX_RECORDS:
        raise InvalidIdentity("snapshot row count exceeds bound")
    return [
        {
            "table": table,
            "key": [row[columns.index(_PKS[table])]],
            "row": dict(zip(columns, row, strict=True)),
        }
        for row in rows
    ]


def _data_version(db: sqlite3.Connection) -> int:
    return cast(int, db.execute("PRAGMA data_version").fetchone()[0])


def capture_owner_snapshot(db: sqlite3.Connection, owner: OwnerRef) -> OwnerSnapshot:
    """Capture current authority projections, including phantom-sensitive children."""
    _owner(owner)
    version = _data_version(db)
    table = _DOMAINS[owner.domain]
    roots = _select(db, table, f"{_PKS[table]}=?", (owner.key[0],))
    if len(roots) != 1:
        raise IdentityMismatch("owner root does not exist")
    root = roots[0]
    related: list[dict[str, object]] = []
    if owner.domain in {"publication", "queue_source"}:
        root_row = cast(dict[str, object], root["row"])
        queue_id = (
            root_row["queue_id"] if owner.domain == "publication" else owner.key[0]
        )
        related.extend(_select(db, "import_queue", "id=?", (queue_id,)))
        related.extend(_select(db, "import_queue_files", "queue_id=?", (queue_id,)))
        related.extend(_select(db, "import_publications", "queue_id=?", (queue_id,)))
        related.extend(
            _select(
                db,
                "import_publication_files",
                "publication_id IN (SELECT id FROM import_publications WHERE queue_id=?)",
                (queue_id,),
            )
        )
    related = sorted(
        (row for row in related if _row_order(row) != _row_order(root)), key=_row_order
    )
    if len(related) + 1 > _MAX_RECORDS:
        raise InvalidIdentity("snapshot row count exceeds bound")
    encoded = _encode(
        {"version": 1, "owner": _owner_value(owner), "root": root, "related": related}
    )
    if _data_version(db) != version:
        raise IdentityMismatch("snapshot changed during capture")
    return OwnerSnapshot(owner, encoded, _digest(encoded))


def assert_snapshot(db: sqlite3.Connection, expected: OwnerSnapshot) -> None:
    """Refuse stale or supplied-only snapshots by recapturing actual SQL rows."""
    if type(expected) is not OwnerSnapshot:
        raise InvalidIdentity("expected an owner snapshot")
    if capture_owner_snapshot(db, expected.owner) != expected:
        raise IdentityMismatch("owner snapshot is stale")


def _role_value(role: RoleKey) -> dict[str, object]:
    if type(role) is not RoleKey:
        raise InvalidIdentity("expected a role key")
    return {
        "file_id": role.file_id,
        "purpose": role.purpose,
        "kind": role.kind,
        "relative": role.relative,
    }


def _role_from(value: object) -> RoleKey:
    record = _object(value, {"file_id", "purpose", "kind", "relative"})
    return RoleKey(
        None if record["file_id"] is None else _integer(record["file_id"]),
        _text(record["purpose"]),
        _text(record["kind"]),
        None if record["relative"] is None else _text(record["relative"]),
    )


def _legacy(value: object, *, namespace: bool = False) -> dict[str, object]:
    record = _object(
        value,
        {"version", "json", "sha256"} if namespace else {"kind", "json", "sha256"},
    )
    if namespace:
        _integer(record["version"], 1, 2)
    elif _text(record["kind"]) not in {
        "file_fingerprint",
        "carrier",
        "namespace_v1",
        "namespace_v2",
        "pack_ownership_v1",
        "pack_ownership_v2",
        "pack_ownership_v3",
        "directory_proof",
        "pack_claim",
        "journal",
    }:
        raise InvalidIdentity("unknown legacy evidence kind")
    if _digest(_text(record["json"])) != _hex(record["sha256"]):
        raise InvalidIdentity("legacy evidence digest mismatch")
    return record


def _constraints(encoded: str, role: RoleKey) -> dict[str, object]:
    value = _version(_load(encoded), {"legacy", "binding", "policy", "non_device"})
    if value["legacy"] is not None:
        _legacy(value["legacy"])
    binding = _object(
        value["binding"], {"owner", "role", "operation_key", "owner_token"}
    )
    _owner_from(binding["owner"])
    if _role_from(binding["role"]) != role:
        raise InvalidIdentity("constraint role binding mismatch")
    _text(binding["operation_key"])
    if binding["owner_token"] is not None:
        _hex(binding["owner_token"], 32)
    fields = {"inode", "size", "mtime_ns", "sha256", "uid", "mode", "nlink"}
    observed = _object(value["non_device"], fields)
    policies = {
        "file": {"inode", "size", "mtime_ns", "sha256"},
        "marker": fields,
        "private_directory": {"inode", "uid", "mode"},
        "parent_identity": {"inode"},
        "exclusive_parent": {"inode", "uid", "mode"},
    }
    policy = _text(value["policy"])
    if policy not in policies or role.kind != (
        policy if policy in {"file", "marker"} else "directory"
    ):
        raise InvalidIdentity("constraint policy does not match role kind")
    for name, scalar in observed.items():
        if name not in policies[policy]:
            if scalar is not None:
                raise InvalidIdentity("inapplicable constraint must be null")
        elif name == "sha256":
            _hex(scalar)
        else:
            _integer(
                scalar,
                -(2**63) if name == "mtime_ns" else 1 if name == "nlink" else 0,
                0o7777 if name == "mode" else 2**63 - 1,
            )
    if (
        (policy == "marker" and (observed["mode"] != 0o600 or observed["nlink"] != 1))
        or (policy == "private_directory" and observed["mode"] != 0o700)
        or (policy == "exclusive_parent" and cast(int, observed["mode"]) & 0o022)
    ):
        raise InvalidIdentity("private ownership mode/link policy mismatch")
    return value


@dataclass(frozen=True, slots=True)
class ObjectProof:
    role: RoleKey
    canonical_path: str
    parent_role: RoleKey | None
    native: NativeHandle
    constraints_json: str

    def __post_init__(self) -> None:
        _role_value(self.role)
        _path(self.canonical_path)
        if self.parent_role is not None:
            _role_value(self.parent_role)
        if type(self.native) is not NativeHandle:
            raise InvalidIdentity("expected a native handle")
        _constraints(self.constraints_json, self.role)


def _object_from(value: object) -> ObjectProof:
    record = _object(
        value, {"role", "canonical_path", "parent_role", "native", "constraints_json"}
    )
    return ObjectProof(
        _role_from(record["role"]),
        _path(record["canonical_path"]),
        None if record["parent_role"] is None else _role_from(record["parent_role"]),
        NativeHandle.from_value(record["native"]),
        _text(record["constraints_json"]),
    )


def _ordered_unique(values: list[str]) -> None:
    if values != sorted(set(values)):
        raise InvalidIdentity("identity requirements duplicate or unordered")


def _acyclic(edges: dict[str, list[str]]) -> None:
    # Iterative topological traversal avoids recursion limits on large packs.
    incoming = dict.fromkeys(edges, 0)
    for children in edges.values():
        for child in children:
            if child not in incoming:
                raise InvalidIdentity("identity dependency is undeclared")
            incoming[child] += 1
    ready = [node for node, degree in incoming.items() if degree == 0]
    visited = 0
    while ready:
        node = ready.pop()
        visited += 1
        for child in edges[node]:
            incoming[child] -= 1
            if incoming[child] == 0:
                ready.append(child)
    if visited != len(edges):
        raise InvalidIdentity("identity relationships are cyclic")


def _dependencies(manifest: dict[str, object], owner: OwnerRef) -> list[OwnerSnapshot]:
    required = [
        _encode(_owner_value(_owner_from(item)))
        for item in _list(manifest["required_dependencies"])
    ]
    _ordered_unique(required)
    snapshots: list[OwnerSnapshot] = []
    edges: dict[str, list[str]] = {}
    for item in _list(manifest["dependencies"]):
        dep = _object(item, {"snapshot_json", "sha256", "requires"})
        text = _text(dep["snapshot_json"])
        value = _version(_load(text), {"owner", "root", "related"})
        snapshot = OwnerSnapshot(_owner_from(value["owner"]), text, _hex(dep["sha256"]))
        key = _encode(_owner_value(snapshot.owner))
        if key in edges or snapshot.owner == owner:
            raise InvalidIdentity("duplicate or self dependency")
        edges[key] = [
            _encode(_owner_value(_owner_from(child)))
            for child in _list(dep["requires"])
        ]
        _ordered_unique(edges[key])
        snapshots.append(snapshot)
    if list(edges) != required:
        raise InvalidIdentity("dependency evidence differs from declared requirements")
    _acyclic(edges)
    return snapshots


def _manifest(proof: ProofSet) -> dict[str, object]:
    value = _version(
        _load(proof.manifest_json),
        {
            "snapshot_sha256",
            "phase",
            "required_roles",
            "roles",
            "absent_roles",
            "required_dependencies",
            "dependencies",
            "intent",
        },
    )
    if _hex(value["snapshot_sha256"]) != proof.snapshot.sha256:
        raise InvalidIdentity("manifest snapshot mismatch")
    _text(value["phase"])
    objects = tuple(_object_from(item) for item in _list(value["roles"]))
    if not objects or objects != proof.roles:
        raise InvalidIdentity("manifest objects differ from proof roles")
    keys = [_encode(_role_value(item.role)) for item in objects]
    _ordered_unique(keys)
    required = [
        _encode(_role_value(_role_from(item)))
        for item in _list(value["required_roles"])
    ]
    _ordered_unique(required)
    present = {key: item for key, item in zip(keys, objects, strict=True)}
    edges: dict[str, list[str]] = {key: [] for key in keys}
    paths: set[str] = set()

    def relationship(role: RoleKey, path: str, parent: RoleKey | None) -> None:
        if path in paths:
            raise InvalidIdentity("duplicate identity object path")
        paths.add(path)
        if parent is None:
            if role.relative is not None:
                raise InvalidIdentity("root role must have no relative path")
            return
        key = _encode(_role_value(parent))
        if key not in present or parent.kind != "directory" or role.relative is None:
            raise InvalidIdentity("missing or unsafe role parent")
        if path != present[key].canonical_path.rstrip("/") + "/" + role.relative:
            raise InvalidIdentity("role parent path mismatch")

    for key, obj in present.items():
        relationship(obj.role, obj.canonical_path, obj.parent_role)
        if obj.parent_role is not None:
            edges[key].append(_encode(_role_value(obj.parent_role)))
        constraints = _constraints(obj.constraints_json, obj.role)
        binding = cast(dict[str, object], constraints["binding"])
        if _owner_from(binding["owner"]) != proof.owner or (
            proof.origin == "exact_legacy" and constraints["legacy"] is None
        ):
            raise InvalidIdentity("object owner/legacy evidence mismatch")
    _acyclic(edges)
    intent = value["intent"]
    if intent is not None:
        if type(intent) is not dict:
            raise InvalidIdentity("invalid tagged intent")
        tag = cast(dict[str, object], intent).get("kind")
        if tag == "journal":
            journal = _object(
                intent,
                {
                    "kind",
                    "snapshot_sha256",
                    "phase",
                    "column",
                    "evidence_json",
                    "evidence_sha256",
                },
            )
            root = cast(
                dict[str, object],
                cast(dict[str, object], _load(proof.snapshot.rows_json))["root"],
            )
            row = cast(dict[str, object], root["row"])
            column = _text(journal["column"])
            if (
                journal["phase"] != value["phase"]
                or column not in row
                or type(row[column]) is not str
                or journal["evidence_json"] != row[column]
            ):
                raise InvalidIdentity("journal intent snapshot/phase evidence mismatch")
            if _digest(_text(journal["evidence_json"])) != _hex(
                journal["evidence_sha256"]
            ):
                raise InvalidIdentity("journal intent evidence digest mismatch")
        elif tag == "recovery":
            recovery = _object(
                intent, {"kind", "snapshot_sha256", "event_id", "plan_sha256"}
            )
            _integer(recovery["event_id"], 1)
            _hex(recovery["plan_sha256"])
        else:
            raise InvalidIdentity("unknown intent kind")
        if cast(dict[str, object], intent)["snapshot_sha256"] != proof.snapshot.sha256:
            raise InvalidIdentity("intent snapshot mismatch")
    absent_keys: list[str] = []
    for item in _list(value["absent_roles"]):
        absent = _object(
            item, {"role", "canonical_path", "parent_role", "intent_sha256"}
        )
        role = _role_from(absent["role"])
        relationship(
            role, _path(absent["canonical_path"]), _role_from(absent["parent_role"])
        )
        if intent is None or _hex(absent["intent_sha256"]) != _digest(_encode(intent)):
            raise InvalidIdentity("absence lacks bound action intent")
        absent_keys.append(_encode(_role_value(role)))
    _ordered_unique(absent_keys)
    if set(keys) & set(absent_keys) or sorted(keys + absent_keys) != required:
        raise InvalidIdentity("role evidence differs from declared requirements")
    _dependencies(value, proof.owner)
    return value


@dataclass(frozen=True, slots=True)
class ProofSet:
    set_id: str
    owner: OwnerRef
    snapshot: OwnerSnapshot
    authority_id: str
    epoch_id: str
    origin: str
    roles: tuple[ObjectProof, ...]
    manifest_json: str

    def __post_init__(self) -> None:
        _hex(self.set_id, 32)
        _owner(self.owner)
        _hex(self.authority_id, 32)
        _hex(self.epoch_id, 32)
        if (
            type(self.snapshot) is not OwnerSnapshot
            or self.snapshot.owner != self.owner
        ):
            raise InvalidIdentity("proof snapshot owner mismatch")
        if _text(self.origin) not in {"birth", "exact_legacy", "recovery"}:
            raise InvalidIdentity("unknown proof set origin")
        if type(self.roles) is not tuple or len(self.roles) > _MAX_RECORDS:
            raise InvalidIdentity("invalid proof role collection")
        _manifest(self)


@dataclass(frozen=True, slots=True)
class ActiveProofSet:
    proof: ProofSet
    event_id: int

    def __post_init__(self) -> None:
        if type(self.proof) is not ProofSet:
            raise InvalidIdentity("expected a proof set")
        _integer(self.event_id, 1)


def _dict_rows(cursor: sqlite3.Cursor) -> list[dict[str, object]]:
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _latest(db: sqlite3.Connection, owner: OwnerRef) -> dict[str, object] | None:
    rows = _dict_rows(
        db.execute(
            "SELECT * FROM file_identity_events WHERE owner_domain=? AND json_extract(owner_key_json,'$.key[0]') IS ? ORDER BY revision DESC LIMIT 1",
            (owner.domain, owner.key[0]),
        )
    )
    if rows and rows[0]["owner_key_json"] != _key_json(owner):
        raise InvalidIdentity("noncanonical stored owner history")
    return rows[0] if rows else None


def _validate_epoch(db: sqlite3.Connection, proof: ProofSet) -> None:
    rows = _dict_rows(
        db.execute(
            "SELECT a.*,e.origin_path,e.allocation_path,e.root_path,e.birth_json FROM "
            "file_identity_epochs e JOIN file_identity_authorities a USING(authority_id) "
            "WHERE e.epoch_id=? AND a.authority_id=?",
            (proof.epoch_id, proof.authority_id),
        )
    )
    if len(rows) != 1:
        raise IdentityMismatch("identity epoch/authority is missing or foreign")
    row = rows[0]
    profile_value = _version(
        _load(row["profile_json"]),
        {
            "profile_id",
            "revision",
            "qualification_sha256",
            "client_contract",
            "server_contract",
        },
    )
    profile = StorageProfile(
        _text(profile_value["profile_id"]),
        _integer(profile_value["revision"], 1),
        _hex(profile_value["qualification_sha256"]),
        cast(dict[str, object], profile_value["client_contract"]),
        cast(dict[str, object], profile_value["server_contract"]),
    )
    if _hex(row["qualification_sha256"]) != profile.qualification_sha256:
        raise InvalidIdentity("authority qualification digest mismatch")
    validate_profile(profile)
    declaration = _version(
        _load(row["declaration_json"]), {"scope_path", "scope_token"}
    )
    scope = _path(declaration["scope_path"])
    _hex(declaration["scope_token"], 32)
    incarnation = _hex(row["incarnation_token"], 32)
    birth = _version(
        _load(row["birth_json"]),
        {"layout", "enrollment", "legacy_namespace", "controlled_birth"},
    )
    if _text(birth["enrollment"]) not in {"birth", "exact_legacy", "recovery"}:
        raise InvalidIdentity("invalid epoch enrollment")
    origin, allocation, root = (
        _path(row[key]) for key in ("origin_path", "allocation_path", "root_path")
    )
    if not _within(origin, scope) or _parent(root) != allocation or root == allocation:
        raise InvalidIdentity("epoch scope geometry mismatch")
    if birth["layout"] == "same_parent":
        if allocation != origin:
            raise InvalidIdentity("same-parent epoch geometry mismatch")
    elif birth["layout"] == "dedicated_parent":
        if _parent(allocation) != origin or allocation == origin:
            raise InvalidIdentity("dedicated-parent epoch geometry mismatch")
    else:
        raise InvalidIdentity("unknown epoch layout")
    legacy, controlled = birth["legacy_namespace"], birth["controlled_birth"]
    if legacy is not None:
        _legacy(legacy, namespace=True)
    if controlled is not None:
        receipt = _object(
            controlled,
            {
                "authority_incarnation",
                "allocation_token",
                "root_native",
                "marker_native",
                "receipt_sha256",
            },
        )
        if _hex(receipt["authority_incarnation"], 32) != incarnation:
            raise InvalidIdentity("epoch incarnation mismatch")
        _hex(receipt["allocation_token"], 32)
        _hex(receipt["receipt_sha256"])
        NativeHandle.from_value(receipt["root_native"])
        NativeHandle.from_value(receipt["marker_native"])
    if (
        (birth["enrollment"] == "birth" and (controlled is None or legacy is not None))
        or (
            birth["enrollment"] == "exact_legacy"
            and (legacy is None or controlled is not None)
        )
        or (legacy is None and controlled is None)
    ):
        raise InvalidIdentity("epoch receipt/enrollment mismatch")
    for obj in proof.roles:
        if not _within(obj.canonical_path, scope):
            raise InvalidIdentity("object outside declared authority scope")


def _validate_current(db: sqlite3.Connection, proof: ProofSet) -> None:
    if type(proof) is not ProofSet:
        raise InvalidIdentity("expected a proof set")
    _validate_epoch(db, proof)
    assert_snapshot(db, proof.snapshot)
    manifest = _manifest(proof)
    for dependency in _dependencies(manifest, proof.owner):
        assert_snapshot(db, dependency)
    intent = manifest["intent"]
    if intent is not None and cast(dict[str, object], intent)["kind"] == "recovery":
        recovery = cast(dict[str, object], intent)
        rows = _dict_rows(
            db.execute(
                "SELECT * FROM file_identity_events WHERE id=?", (recovery["event_id"],)
            )
        )
        if (
            not rows
            or rows[0]["owner_domain"] != proof.owner.domain
            or rows[0]["owner_key_json"] != _key_json(proof.owner)
            or rows[0]["kind"] not in {"recovery_intent", "recovery_step"}
        ):
            raise IdentityMismatch("foreign or missing recovery intent")
        payload = _version(
            _load(rows[0]["payload_json"]),
            {"before_snapshot_json", "before_snapshot_sha256", "plan_sha256"},
        )
        if (
            payload["plan_sha256"] != recovery["plan_sha256"]
            or payload["before_snapshot_json"] != proof.snapshot.rows_json
            or payload["before_snapshot_sha256"] != proof.snapshot.sha256
        ):
            raise IdentityMismatch("recovery intent snapshot/plan mismatch")


def _require_writer(db: sqlite3.Connection) -> None:
    if not db.in_transaction:
        raise IdentityUnavailable("identity writes require caller transaction")


def _event_cas(
    db: sqlite3.Connection, owner: OwnerRef, expected: int | None
) -> dict[str, object] | None:
    if expected is not None:
        _integer(expected, 1)
    latest = _latest(db, owner)
    if (None if latest is None else latest["id"]) != expected:
        raise IdentityMismatch("identity event predecessor is stale")
    return latest


def _append(
    db: sqlite3.Connection, proof: ProofSet, kind: str, latest: dict[str, object] | None
) -> int:
    payload = _encode(
        {
            "version": 1,
            "snapshot_sha256": proof.snapshot.sha256,
            "manifest_sha256": _digest(proof.manifest_json),
        }
    )
    cursor = db.execute(
        "INSERT INTO file_identity_events(owner_domain,owner_key_json,revision,predecessor_event_id,set_id,kind,payload_json) VALUES(?,?,?,?,?,?,?)",
        (
            proof.owner.domain,
            _key_json(proof.owner),
            1 if latest is None else _integer(latest["revision"], 1) + 1,
            None if latest is None else latest["id"],
            proof.set_id,
            kind,
            payload,
        ),
    )
    if cursor.lastrowid is None:
        raise IdentityMismatch("identity event was not inserted")
    return cursor.lastrowid


def prepare_set(
    db: sqlite3.Connection, proof: ProofSet, expected_event_id: int | None
) -> int:
    """Persist a complete qualified proposal, never commit or grant authority."""
    _require_writer(db)
    _validate_current(db, proof)
    latest = _event_cas(db, proof.owner, expected_event_id)
    if db.execute(
        "SELECT 1 FROM file_identity_sets WHERE set_id=?", (proof.set_id,)
    ).fetchone():
        raise IdentityMismatch("identity set is already prepared")
    db.execute("SAVEPOINT file_identity_prepare")
    try:
        db.execute(
            "INSERT INTO file_identity_sets(set_id,owner_domain,owner_key_json,prior_set_id,snapshot_json,snapshot_sha256,origin,epoch_id,manifest_json,manifest_sha256) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                proof.set_id,
                proof.owner.domain,
                _key_json(proof.owner),
                None if latest is None else latest["set_id"],
                proof.snapshot.rows_json,
                proof.snapshot.sha256,
                proof.origin,
                proof.epoch_id,
                proof.manifest_json,
                _digest(proof.manifest_json),
            ),
        )
        event = _append(db, proof, "prepared", latest)
    except BaseException:
        db.execute("ROLLBACK TO file_identity_prepare")
        db.execute("RELEASE file_identity_prepare")
        raise
    db.execute("RELEASE file_identity_prepare")
    return event


def _load_set(db: sqlite3.Connection, set_id: object) -> ProofSet:
    rows = _dict_rows(
        db.execute(
            "SELECT s.*,e.authority_id FROM file_identity_sets s JOIN file_identity_epochs e USING(epoch_id) WHERE s.set_id=?",
            (_hex(set_id, 32),),
        )
    )
    if len(rows) != 1:
        raise IdentityMismatch("identity set is missing")
    row = rows[0]
    key = _version(_load(row["owner_key_json"]), {"key"})
    owner = _owner_from({"domain": row["owner_domain"], "key": key["key"]})
    snapshot = OwnerSnapshot(
        owner, _text(row["snapshot_json"]), _hex(row["snapshot_sha256"])
    )
    manifest_json = _text(row["manifest_json"])
    if _digest(manifest_json) != _hex(row["manifest_sha256"]):
        raise InvalidIdentity("stored manifest digest mismatch")
    manifest = _version(
        _load(manifest_json),
        {
            "snapshot_sha256",
            "phase",
            "required_roles",
            "roles",
            "absent_roles",
            "required_dependencies",
            "dependencies",
            "intent",
        },
    )
    return ProofSet(
        _hex(row["set_id"], 32),
        owner,
        snapshot,
        _hex(row["authority_id"], 32),
        _hex(row["epoch_id"], 32),
        _text(row["origin"]),
        tuple(_object_from(item) for item in _list(manifest["roles"])),
        manifest_json,
    )


def activate_set(
    db: sqlite3.Connection,
    proof: ProofSet,
    expected: OwnerSnapshot,
    expected_event_id: int,
) -> int:
    """Append activation only for this exact latest prepared set and SQL state."""
    _require_writer(db)
    if type(proof) is not ProofSet or expected != proof.snapshot:
        raise IdentityMismatch("activation snapshot differs from proposal")
    _validate_current(db, proof)
    latest = _event_cas(db, proof.owner, expected_event_id)
    if (
        latest is None
        or latest["kind"] != "prepared"
        or latest["set_id"] != proof.set_id
        or _load_set(db, proof.set_id) != proof
    ):
        raise IdentityMismatch("activation requires exact latest prepared set")
    return _append(db, proof, "activated", latest)


def read_active(db: sqlite3.Connection, owner: OwnerRef) -> ActiveProofSet | None:
    """Reload committed latest authority; history is never a legacy fallback."""
    _owner(owner)
    if db.in_transaction:
        raise IdentityUnavailable("active identity readback requires ended transaction")
    version = _data_version(db)
    latest = _latest(db, owner)
    if latest is None:
        if _data_version(db) != version:
            raise IdentityMismatch("identity history changed during readback")
        return None
    if latest["kind"] != "activated":
        raise IdentityMismatch("latest identity history is not an activated set")
    proof = _load_set(db, latest["set_id"])
    if proof.owner != owner:
        raise InvalidIdentity("active identity owner mismatch")
    payload = _version(
        _load(latest["payload_json"]), {"snapshot_sha256", "manifest_sha256"}
    )
    if payload["snapshot_sha256"] != proof.snapshot.sha256 or payload[
        "manifest_sha256"
    ] != _digest(proof.manifest_json):
        raise InvalidIdentity("activation payload digest mismatch")
    predecessor = _dict_rows(
        db.execute(
            "SELECT * FROM file_identity_events WHERE id=?",
            (latest["predecessor_event_id"],),
        )
    )
    if (
        not predecessor
        or predecessor[0]["kind"] != "prepared"
        or predecessor[0]["set_id"] != proof.set_id
        or predecessor[0]["owner_domain"] != owner.domain
        or predecessor[0]["owner_key_json"] != _key_json(owner)
        or _integer(latest["revision"], 1)
        != _integer(predecessor[0]["revision"], 1) + 1
        or predecessor[0]["payload_json"] != latest["payload_json"]
    ):
        raise InvalidIdentity("activation predecessor mismatch")
    _validate_current(db, proof)
    if _data_version(db) != version:
        raise IdentityMismatch("identity changed during readback")
    return ActiveProofSet(proof, _integer(latest["id"], 1))
