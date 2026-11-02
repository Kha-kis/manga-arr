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

# Versioned projections, separate from the unchanged legacy preimage guards.
_PROVISION_ROWS = {
    "file_identity_epochs": _columns(
        "epoch_id authority_id origin_path allocation_path root_path birth_json", ""
    ),
    "file_identity_provision_intents": _columns(
        "intent_id authority_id payload_json payload_sha256", ""
    ),
}
_PROVISION_PKS = {
    "file_identity_epochs": "epoch_id",
    "file_identity_provision_intents": "intent_id",
}


def _text(value: object, *, empty: bool = False) -> str:
    if type(value) is not str or (not empty and not value):
        raise InvalidIdentity("identity text has an invalid type or is empty")
    result = cast(str, value)
    if "\x00" in result or (
        not result.isascii() and any(0xD800 <= ord(c) <= 0xDFFF for c in result)
    ):
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


def _successor_size(value: object, remaining: int = _MAX_JSON, depth: int = 0) -> int:
    """Charge escaped bytes before encoding strings or accumulating wire records."""
    if depth > 32 or remaining < 0:
        raise InvalidIdentity("ownership_budget")
    if type(value) is str:
        text = _text(value, empty=True)
        size = 2
        for char in text:
            code = ord(char)
            size += (
                2
                if char in '"\\\b\f\n\r\t'
                else 6
                if code < 32 or 127 <= code <= 0xFFFF
                else 12
                if code > 0xFFFF
                else 1
            )
            if size > remaining:
                raise InvalidIdentity("ownership_budget")
        return size
    if type(value) in (list, dict):
        collection = cast(list[object] | dict[str, object], value)
        if len(collection) > _MAX_RECORDS:
            raise InvalidIdentity("ownership_budget")
        size = 2
        if type(value) is list:
            for index, child in enumerate(cast(list[object], value)):
                size += bool(index)
                size += _successor_size(child, remaining - size, depth + 1)
        else:
            for index, (key, child) in enumerate(
                cast(dict[str, object], value).items()
            ):
                size += bool(index) + 1
                size += _successor_size(key, remaining - size, depth + 1)
                size += _successor_size(child, remaining - size, depth + 1)
        if size > remaining:
            raise InvalidIdentity("ownership_budget")
        return size
    if value is not None and type(value) not in (bool, int, float):
        raise InvalidIdentity("ownership_wire")
    if type(value) is float and not math.isfinite(cast(float, value)):
        raise InvalidIdentity("ownership_wire")
    size = len(json.dumps(value, allow_nan=False, separators=(",", ":")))
    if size > remaining:
        raise InvalidIdentity("ownership_budget")
    return size


def _encode(value: object) -> str:
    _ = _successor_size(value)
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


def _row(value: object, allowed: set[str], *, version: int = 1) -> dict[str, object]:
    record = _object(value, {"table", "key", "row"})
    table = _text(record["table"])
    if table not in allowed:
        raise InvalidIdentity("unexpected snapshot table")
    columns = (
        _PROVISION_ROWS[table]
        if version == 2 and table in _PROVISION_ROWS
        else _PROJECTIONS[table]
    )
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
    pk = _PROVISION_PKS[table] if table in _PROVISION_ROWS else _PKS[table]
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
        value = _object(_load(self.rows_json), {"version", "owner", "root", "related"})
        version = _integer(value["version"], 1, 2)
        if version == 2 and self.owner.domain != "namespace":
            raise InvalidIdentity("ownership_wire")
        if _owner_from(value["owner"]) != self.owner:
            raise InvalidIdentity("snapshot owner mismatch")
        table = _DOMAINS[self.owner.domain]
        root = _row(value["root"], {table})
        if root["key"] != list(self.owner.key):
            raise InvalidIdentity("snapshot root key mismatch")
        allowed = (
            set(_PROVISION_ROWS)
            if version == 2
            else (
                {
                    "import_queue",
                    "import_queue_files",
                    "import_publications",
                    "import_publication_files",
                }
                if self.owner.domain in {"publication", "queue_source"}
                else set()
            )
        )
        records = [
            _row(item, allowed, version=version) for item in _list(value["related"])
        ]
        orders = [_row_order(item) for item in records]
        if orders != sorted(set(orders)) or _row_order(root) in orders:
            raise InvalidIdentity("snapshot related rows duplicate or unordered")
        if len(records) + 1 > _MAX_RECORDS:
            raise InvalidIdentity("snapshot row count exceeds bound")
        if _hex(self.sha256) != _digest(self.rows_json):
            raise InvalidIdentity("snapshot digest mismatch")
        if version == 2:
            _ = _provision_snapshot(value)


def _select(
    db: sqlite3.Connection, table: str, where: str, params: tuple[object, ...]
) -> list[dict[str, object]]:
    columns = sorted(
        _PROVISION_ROWS[table] if table in _PROVISION_ROWS else _PROJECTIONS[table]
    )
    primary = _PROVISION_PKS[table] if table in _PROVISION_PKS else _PKS[table]
    cursor = db.execute(
        f"SELECT {','.join(columns)} FROM {table} WHERE {where} LIMIT {_MAX_RECORDS + 1}",
        params,
    )
    try:
        rows = cursor.fetchmany(_MAX_RECORDS + 1)
    finally:
        cursor.close()
    if len(rows) > _MAX_RECORDS:
        raise InvalidIdentity("snapshot row count exceeds bound")
    return [
        {
            "table": table,
            "key": [row[columns.index(primary)]],
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
    snapshot_version = 1
    if (
        owner.domain == "namespace"
        and db.execute("PRAGMA user_version").fetchone()[0] == 8
    ):
        epochs = _select(
            db, "file_identity_epochs", "allocation_path=?", (owner.key[0],)
        )
        born = [
            row
            for row in epochs
            if _epoch_birth(_text(cast(dict[str, object], row["row"])["birth_json"]))[
                "version"
            ]
            == 2
        ]
        if len(born) > 1:
            raise InvalidIdentity("ownership_binding")
        if born:
            snapshot_version = 2
            epoch = born[0]
            birth = _epoch_birth(
                _text(cast(dict[str, object], epoch["row"])["birth_json"])
            )
            intent_id = cast(dict[str, object], birth["controlled_birth"])["intent_id"]
            intents = _select(
                db, "file_identity_provision_intents", "intent_id=?", (intent_id,)
            )
            if len(intents) != 1:
                raise IdentityMismatch("identity ledger reference is dangling")
            related.extend((epoch, intents[0]))
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
        {
            "version": snapshot_version,
            "owner": _owner_value(owner),
            "root": root,
            "related": related,
        }
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


def _birth_namespace(encoded: str) -> dict[str, object]:
    """Decode the existing namespace2 receipt without importing live consumers.

    Legacy text is retained byte-for-byte; unlike the surrounding new wire its
    original JSON serialization is not rewritten or required to be canonical.
    """
    text = _text(encoded)
    if len(text) > _MAX_JSON or len(text.encode("utf-8")) > _MAX_JSON:
        raise InvalidIdentity("ownership_budget")
    try:
        value = cast(
            object, json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
        )
    except (ValueError, TypeError, RecursionError) as exc:
        raise InvalidIdentity("ownership_wire") from exc
    _ = _successor_size(value)
    record = _object(
        value,
        {
            "version",
            "owner_token",
            "parent_dev",
            "parent_ino",
            "root_path",
            "root_dev",
            "root_ino",
            "marker_fingerprint",
            "creation_boundary",
        },
    )
    _ = _integer(record["version"], 2, 2)
    _ = _hex(record["owner_token"], 32)
    _ = _path(record["root_path"])
    for name in ("parent_dev", "parent_ino", "root_dev", "root_ino"):
        _ = _integer(record[name])
    marker = _object(
        record["marker_fingerprint"], {"dev", "inode", "size", "mtime_ns", "sha256"}
    )
    for name in ("dev", "inode", "size", "mtime_ns"):
        _ = _integer(marker[name], -(2**63) if name == "mtime_ns" else 0)
    _ = _hex(marker["sha256"])
    boundary = _object(
        record["creation_boundary"], {"kind", "parent_uid", "parent_mode"}
    )
    mode = _integer(boundary["parent_mode"], 0, 0o7777)
    _ = _integer(boundary["parent_uid"])
    if boundary["kind"] != "exclusive_parent" or mode & 0o022:
        raise InvalidIdentity("ownership_wire")
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
        if policy == "file" and name in {"uid", "mode", "nlink"} and scalar is not None:
            _integer(
                scalar,
                1 if name == "nlink" else 0,
                0o7777 if name == "mode" else 2**63 - 1,
            )
            continue
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


def _provision_profile(value: object) -> dict[str, object]:
    profile = _version(
        value,
        {
            "profile_id",
            "revision",
            "qualification_sha256",
            "client_contract",
            "server_contract",
        },
    )
    _ = _text(profile["profile_id"])
    _ = _integer(profile["revision"], 1)
    _ = _hex(profile["qualification_sha256"])
    client = _object(profile["client_contract"], {"platforms", "mount"})
    platforms = _list(client["platforms"])
    if not platforms:
        raise InvalidIdentity("ownership_wire")
    _ordered_unique([_encode(p) for p in platforms])
    for platform in platforms:
        for field in _object(
            platform, {"arch", "kernel_release", "libc_version", "encoder_revision"}
        ).values():
            _ = _text(field)
    mount = _object(
        client["mount"], {"filesystem", "nfs_version", "transport", "security", "hard"}
    )
    if type(mount["hard"]) is not bool:
        raise InvalidIdentity("ownership_wire")
    for name in ("filesystem", "nfs_version", "transport", "security"):
        _ = _text(mount[name])
    server = _object(
        profile["server_contract"],
        {
            "vendor",
            "version",
            "filesystem",
            "exporter",
            "handle_contract",
            "allowed_transitions",
            "recovery_transitions",
        },
    )
    for name in ("vendor", "version", "filesystem", "exporter", "handle_contract"):
        _ = _text(server[name])
    for name in ("allowed_transitions", "recovery_transitions"):
        _ordered_unique([_text(v) for v in _list(server[name])])
    return profile


def _historical_role(value: object) -> dict[str, object]:
    """Validate RoleSpec wire leaves without reconstructing live consumers."""
    spec = _object(
        value,
        {"owner", "role", "path", "parent_role", "legacy_json", "presence", "policy"},
    )
    _ = _owner_from(spec["owner"])
    role = _role_from(spec["role"])
    _ = _path(spec["path"])
    parent = None if spec["parent_role"] is None else _role_from(spec["parent_role"])
    if (parent is None) != (role.relative is None) or (
        parent is not None and parent.kind != "directory"
    ):
        raise InvalidIdentity("ownership_wire")
    presence = _text(spec["presence"])
    policies = {
        "file": {"file"},
        "marker": {"marker"},
        "directory": {"private_directory", "parent_identity", "exclusive_parent"},
    }
    if (
        presence not in {"proof", "expectation", "intent"}
        or _text(spec["policy"]) not in policies[role.kind]
    ):
        raise InvalidIdentity("ownership_wire")
    legacy = spec["legacy_json"]
    if legacy is None:
        if presence == "proof":
            raise InvalidIdentity("ownership_wire")
    else:
        # RoleSpec legacy text is bounded JSON, not necessarily canonical JSON.
        # Validate its value without rewriting the persisted receipt bytes.
        text = _text(legacy)
        _ = _successor_size(text)
        try:
            decoded = cast(
                object,
                json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant),
            )
        except (ValueError, TypeError, RecursionError) as exc:
            raise InvalidIdentity("ownership_wire") from exc
        _ = _successor_size(decoded)
    return spec


def _historical_raw_row(value: object) -> tuple[str, dict[str, object]]:
    """Match complete RawRow grammar, not the narrower SQL authority projection."""
    record = _object(value, {"table", "key", "values"})
    table = _text(record["table"])
    primary_keys = (
        _PKS
        | _PROVISION_PKS
        | {
            "series": "id",
            "volumes": "id",
            "chapters": "id",
            "chapter_quarantine": "id",
            "root_folders": "id",
            "series_tags": "series_id",
            "file_identity_authorities": "authority_id",
            "file_identity_sets": "set_id",
            "file_identity_events": "id",
        }
    )
    if table not in primary_keys:
        raise InvalidIdentity("ownership_wire")
    expected = (
        ("series_id", "tag") if table == "series_tags" else (primary_keys[table],)
    )
    key = _list(record["key"])
    if len(key) != len(expected):
        raise InvalidIdentity("ownership_wire")
    for column, scalar in zip(expected, key, strict=True):
        if column in {"parent_path", "tombstone_path"}:
            _ = _path(scalar)
        elif column in {"download_identity_key", "tag"}:
            _ = _text(scalar)
        elif table.startswith("file_identity_") and column != "id":
            _ = _hex(scalar, 32)
        else:
            _ = _integer(scalar, 1)
    columns: dict[str, object] = {}
    names: list[str] = []
    for value in _list(record["values"]):
        pair = _list(value)
        if len(pair) != 2:
            raise InvalidIdentity("ownership_wire")
        name, scalar = _text(pair[0]), pair[1]
        if scalar is not None and type(scalar) not in {str, int, float}:
            raise InvalidIdentity("ownership_wire")
        _finite(scalar)
        names.append(name)
        columns[name] = scalar
    if names != sorted(set(names)) or [columns.get(c) for c in expected] != key:
        raise InvalidIdentity("ownership_wire")
    return table, columns


def _historical_observation(value: object) -> None:
    """Validate readonly-observation wire, never acquire or authorize evidence."""
    observation = _object(value, {"spec", "native", "constraints_json", "outcome"})
    spec = _historical_role(observation["spec"])
    role = _role_from(spec["role"])
    outcomes = {
        "observed",
        "absent",
        "native_unsupported",
        "policy_mismatch",
        "boundary_unknown",
        "cross_device",
        "unsafe_path",
        "changed",
        "unreadable",
        "budget_exceeded",
        "unknown_children",
        "unreachable_below_absent_root",
    }
    outcome = _text(observation["outcome"])
    if outcome not in outcomes:
        raise InvalidIdentity("ownership_wire")
    native = observation["native"]
    if native is not None:
        _ = NativeHandle.from_value(native)
    document = _load(observation["constraints_json"])
    if type(document) is not dict:
        raise InvalidIdentity("ownership_wire")
    constraints = cast(dict[str, object], document)
    version = _integer(constraints.get("version"), 1, 2)
    fields = _object(
        constraints,
        {
            "version",
            "kind",
            "current",
            "content",
            "expected",
            "policy",
            "boundary",
            "children",
        }
        | ({"ancestor"} if version == 2 else set()),
    )
    if version == 2:
        ancestor = _object(fields["ancestor"], {"owner", "role", "observation_sha256"})
        _ = _owner_from(ancestor["owner"])
        _ = _role_from(ancestor["role"])
        _ = _hex(ancestor["observation_sha256"])
    if fields["kind"] != "readonly_observation" or fields["expected"] != (
        "unverified_legacy" if spec["legacy_json"] is not None else "undeclared"
    ):
        raise InvalidIdentity("ownership_wire")
    policy, boundary = _text(fields["policy"]), _text(fields["boundary"])
    if policy not in {"satisfied", "mismatch", "not_checked"} or boundary not in {
        "verified",
        "unknown",
        "cross_device",
    }:
        raise InvalidIdentity("ownership_wire")
    current, content = fields["current"], fields["content"]
    info: dict[str, object] | None = None
    if current is not None:
        info = _object(
            current,
            {"dev", "inode", "uid", "mode", "nlink", "size", "mtime_ns", "ctime_ns"},
        )
        for name in ("dev", "inode"):
            _ = _integer(info[name], 0, (1 << 64) - 1)
        _ = _integer(info["uid"])
        _ = _integer(info["mode"], 0, 0o7777)
        for name in ("nlink", "size", "mtime_ns", "ctime_ns"):
            if role.kind == "directory":
                if info[name] is not None:
                    raise InvalidIdentity("ownership_wire")
            else:
                _ = _integer(info[name], -(1 << 63) if name.endswith("_ns") else 0)
    if content is not None:
        if role.kind == "directory" or info is None:
            raise InvalidIdentity("ownership_wire")
        data = _object(content, {"size", "sha256"})
        _ = _integer(data["size"])
        _ = _hex(data["sha256"])
        if info["size"] != data["size"]:
            raise InvalidIdentity("ownership_wire")
    children = _list(fields["children"])
    paths: list[str] = []
    for child in children:
        item = _object(child, {"path", "outcome"})
        path = _path(item["path"])
        if (
            not _within(path, _path(spec["path"]))
            or path == spec["path"]
            or _text(item["outcome"])
            not in outcomes - {"unreachable_below_absent_root"}
        ):
            raise InvalidIdentity("ownership_wire")
        paths.append(path)
    if paths != sorted(set(paths)) or (version == 2) != (
        outcome == "unreachable_below_absent_root"
    ):
        raise InvalidIdentity("ownership_wire")
    if outcome == "unreachable_below_absent_root":
        valid = (
            native is None
            and current is None
            and content is None
            and not children
            and policy == "not_checked"
            and boundary == "verified"
        )
    elif outcome in {"observed", "absent", "native_unsupported"}:
        valid = boundary == "verified" and policy == "satisfied" and not children
        if outcome == "absent":
            valid = valid and current is None and content is None and native is None
        else:
            valid = (
                valid
                and current is not None
                and (role.kind == "directory" or content is not None)
                and ((outcome == "observed") == (native is not None))
            )
    elif outcome == "unknown_children":
        valid = (
            role.kind == "directory"
            and current is not None
            and content is None
            and policy == "satisfied"
            and boundary == "verified"
            and bool(children)
        )
    else:
        valid = (
            native is None
            and current is None
            and content is None
            and not children
            and policy
            in (
                {"mismatch"}
                if outcome == "policy_mismatch"
                else {"not_checked", "satisfied"}
            )
            and boundary
            in (
                {"cross_device"}
                if outcome == "cross_device"
                else {"unknown", "verified"}
            )
        )
    if not valid:
        raise InvalidIdentity("ownership_wire")


def _prior_plan(encoded: str) -> dict[str, object]:
    """Validate a historical wire/digest, never execute or freshly approve P.

    In particular this does not import the planner, traverse nested historical
    intents, consult a qualification registry, or perform any current-state I/O.
    """
    plan = _object(
        _load(encoded),
        {
            "version",
            "inventory",
            "decisions",
            "declaration_json",
            "backup_receipt_json",
            "epoch_bindings_json",
            "sha256",
        },
    )
    _ = _integer(plan["version"], 2, 2)
    if _hex(plan["sha256"]) != _digest(
        _encode({k: v for k, v in plan.items() if k != "sha256"})
    ):
        raise InvalidIdentity("ownership_binding")
    inventory = _object(
        plan["inventory"],
        {"version", "parent", "closure", "observed", "unresolved", "budget", "sha256"},
    )
    _ = _integer(inventory["version"], 1, 2)
    _ = _path(inventory["parent"])
    if _hex(inventory["sha256"]) != _digest(
        _encode({k: v for k, v in inventory.items() if k != "sha256"})
    ):
        raise InvalidIdentity("ownership_binding")
    limits = _object(inventory["budget"], {"max_rows", "max_objects", "max_hash_bytes"})
    for scalar in limits.values():
        _ = _integer(scalar, 1)
    closure = _object(
        inventory["closure"], {"snapshots", "roles", "dependencies", "raw_rows"}
    )
    new_ledger = False
    for item in _list(closure["snapshots"]):
        snapshot = _object(item, {"owner", "rows_json", "sha256"})
        owner = _owner_from(snapshot["owner"])
        text = _text(snapshot["rows_json"])
        document = _object(_load(text), {"version", "owner", "root", "related"})
        version = _integer(document["version"], 1, 2)
        new_ledger |= version == 2
        if _owner_from(document["owner"]) != owner or _hex(
            snapshot["sha256"]
        ) != _digest(text):
            raise InvalidIdentity("ownership_binding")
        _ = _row(document["root"], {_DOMAINS[owner.domain]})
        for record in _list(document["related"]):
            _ = _row(
                record,
                set(_PROJECTIONS) | (set(_PROVISION_ROWS) if version == 2 else set()),
                version=version,
            )
    for item in _list(closure["roles"]):
        _ = _historical_role(item)
    for item in _list(closure["dependencies"]):
        dependency = _object(item, {"owner", "requires", "reason"})
        _ = _owner_from(dependency["owner"])
        _ = _owner_from(dependency["requires"])
        _ = _text(dependency["reason"])
    for item in _list(closure["raw_rows"]):
        table, columns = _historical_raw_row(item)
        new_ledger |= table == "file_identity_provision_intents"
        for ledger_table, column in (
            ("file_identity_epochs", "birth_json"),
            ("file_identity_sets", "snapshot_json"),
            ("file_identity_sets", "manifest_json"),
            ("file_identity_events", "payload_json"),
        ):
            if table == ledger_table and type(columns.get(column)) is str:
                # Historical RawRow scalars remain opaque. Recognize ledger2
                # for the receipt floor without reapproving old row semantics.
                try:
                    document = _load(columns[column])
                except InvalidIdentity:
                    continue
                if type(document) is dict:
                    new_ledger |= cast(dict[str, object], document).get("version") == 2
    for item in _list(inventory["observed"]):
        _historical_observation(item)
    for item in _list(inventory["unresolved"]):
        _ = _text(item)
    for item in _list(plan["decisions"]):
        decision = _object(
            item, {"owner", "action", "roles", "destination", "retained_roles"}
        )
        _ = _owner_from(decision["owner"])
        if decision["action"] not in {
            "cancel-unmutated",
            "restore-original",
            "resume-commit",
            "keep-current",
            "cleanup-committed",
            "retain-blocked",
        }:
            raise InvalidIdentity("ownership_wire")
        if decision["destination"] is not None:
            _ = _path(decision["destination"])
        for name in ("roles", "retained_roles"):
            for role in _list(decision[name]):
                _ = _role_from(role)
    epoch = _object(
        _load(plan["epoch_bindings_json"]),
        {
            "version",
            "role_bindings",
            "existing",
            "provision",
            "rollback_outputs",
            "linked_effects",
            "allocation_capabilities",
        },
    )
    _ = _integer(epoch["version"], 2, 2)
    declaration = _object(
        _load(plan["declaration_json"]),
        {
            "version",
            "inventory_sha256",
            "selection_sha256",
            "authorities",
            "owner_reviews",
            "role_reviews",
            "linked_reviews",
        },
    )
    receipt = _object(
        _load(plan["backup_receipt_json"]),
        {
            "version",
            "inventory_sha256",
            "selection_sha256",
            "declaration_sha256",
            "archive",
            "owner_snapshots",
            "media",
        },
    )
    selection = {
        "version": 2,
        "inventory_sha256": inventory["sha256"],
        "decisions": plan["decisions"],
        "epoch_bindings": epoch,
    }
    for document in (declaration, receipt):
        _ = _integer(document["version"], 2, 2)
        if document["inventory_sha256"] != inventory["sha256"] or document[
            "selection_sha256"
        ] != _digest(_encode(selection)):
            raise InvalidIdentity("ownership_binding")
    if receipt["declaration_sha256"] != _digest(_encode(declaration)):
        raise InvalidIdentity("ownership_binding")
    # Closed record shapes are checked independently of historical action policy.
    shapes = (
        (
            epoch,
            "role_bindings",
            {"owner", "role", "namespace_owner", "epoch_id", "authority_id"},
        ),
        (
            epoch,
            "existing",
            {
                "epoch_id",
                "authority_id",
                "origin_path",
                "allocation_path",
                "root_path",
                "birth_json",
            },
        ),
        (
            epoch,
            "provision",
            {
                "epoch_id",
                "authority_id",
                "origin_parent",
                "allocation_parent",
                "root_path",
                "old_namespace_owners",
                "handling",
            },
        ),
        (
            epoch,
            "rollback_outputs",
            {
                "version",
                "owner",
                "original_role",
                "publication_role",
                "epoch_id",
                "carrier_token",
                "handling",
            },
        ),
        (
            epoch,
            "allocation_capabilities",
            {"epoch_id", "owner", "set_id", "event_id", "event_sha256", "birth_sha256"},
        ),
        (
            declaration,
            "authorities",
            {
                "authority_id",
                "incarnation_token",
                "scope",
                "profile",
                "original_storage_statement",
                "administration_statement",
            },
        ),
        (
            declaration,
            "owner_reviews",
            {"owner", "snapshot_sha256", "intended_original_authority_id"},
        ),
        (
            declaration,
            "role_reviews",
            {"owner", "role", "observation_sha256", "authority_id", "assertion"},
        ),
        (declaration, "linked_reviews", {"effect_sha256", "authority_id"}),
        (receipt, "owner_snapshots", {"owner", "sha256"}),
        (
            receipt,
            "media",
            {"owner", "role", "observation_sha256", "handling", "recovery_reference"},
        ),
    )
    for document, name, keys in shapes:
        order: list[tuple[str, str]] = []
        for item in _list(document[name]):
            record = _object(item, keys)
            for field, scalar in record.items():
                if field in {"owner", "namespace_owner"}:
                    if scalar is not None or field == "owner":
                        _ = _owner_from(scalar)
                elif field in {"role", "original_role", "publication_role"}:
                    _ = _role_from(scalar)
                elif field.endswith("sha256"):
                    _ = _hex(scalar)
                elif field in {
                    "authority_id",
                    "epoch_id",
                    "set_id",
                    "intended_original_authority_id",
                    "incarnation_token",
                    "carrier_token",
                }:
                    if scalar is None and (
                        field == "intended_original_authority_id"
                        or name in {"role_bindings", "role_reviews"}
                    ):
                        continue
                    _ = _hex(scalar, 32)
                elif field in {"event_id", "version"}:
                    _ = _integer(scalar, 1, 1 if field == "version" else 2**63 - 1)
                elif field in {
                    "origin_path",
                    "allocation_path",
                    "origin_parent",
                    "allocation_parent",
                    "root_path",
                }:
                    _ = _path(scalar)
                elif field == "birth_json":
                    _ = _epoch_birth(_text(scalar))
                elif field == "scope":
                    scope = _version(scalar, {"scope_path", "scope_token"})
                    _ = _path(scope["scope_path"])
                    _ = _hex(scope["scope_token"], 32)
                elif field == "profile":
                    _ = _provision_profile(scalar)
                elif field == "old_namespace_owners":
                    owners = [_owner_from(o) for o in _list(scalar)]
                    if any(o.domain != "namespace" for o in owners):
                        raise InvalidIdentity("ownership_wire")
                    _ordered_unique([_encode(_owner_value(o)) for o in owners])
                elif field == "assertion":
                    if scalar not in {
                        "retain-only",
                        "reviewed-recovery-subject",
                        "reviewed-linked-reference",
                    }:
                        raise InvalidIdentity("ownership_wire")
                elif field == "handling":
                    allowed = (
                        {"retain-old-disable-new-allocation"}
                        if name == "provision"
                        else {"retain-non-replayable"}
                        if name == "rollback_outputs"
                        else {
                            "snapshot",
                            "retain-in-place",
                            "observed-absent",
                            "observed-unreachable",
                        }
                    )
                    if scalar not in allowed:
                        raise InvalidIdentity("ownership_wire")
                elif field == "recovery_reference" and scalar is None:
                    continue
                else:
                    _ = _text(scalar)
            if name in {"existing", "provision", "allocation_capabilities"}:
                identity = (_text(record["epoch_id"]), "")
            elif name == "authorities":
                identity = (_text(record["authority_id"]), "")
            elif name == "linked_reviews":
                identity = (_text(record["effect_sha256"]), "")
            else:
                role_field = (
                    "publication_role" if name == "rollback_outputs" else "role"
                )
                identity = (
                    _encode(record["owner"]),
                    _encode(record[role_field]) if role_field in record else "",
                )
            order.append(identity)
        if order != sorted(set(order)):
            raise InvalidIdentity("ownership_wire")
    _ordered_unique([_encode(e) for e in _list(epoch["linked_effects"])])
    for item in _list(epoch["linked_effects"]):
        effect = _object(
            item,
            {
                "decision_sha256",
                "original_role",
                "affected_owner",
                "affected_role",
                "observation_sha256",
                "relationship",
                "effect",
                "handling",
            },
        )
        _ = _hex(effect["decision_sha256"])
        _ = _hex(effect["observation_sha256"])
        _ = _role_from(effect["original_role"])
        _ = _role_from(effect["affected_role"])
        _ = _owner_from(effect["affected_owner"])
        if (
            effect["effect"] not in {"create-name", "replace-acknowledged-name"}
            or effect["handling"] != "retain-reference-non-replayable"
        ):
            raise InvalidIdentity("ownership_wire")
        _prior_relationship(effect["relationship"])
    archive = _object(
        receipt["archive"],
        {
            "path",
            "sha256",
            "format",
            "format_version",
            "schema_version",
            "self_contained",
            "components",
            "source_image_digest",
        },
    )
    _ = _path(archive["path"])
    _ = _hex(archive["sha256"])
    _ = _integer(archive["format_version"], 1, 1)
    _ = _integer(
        archive["schema_version"],
        8 if new_ledger else 7,
        8 if inventory["version"] == 2 else 7,
    )
    components = _object(archive["components"], {"database", "configuration", "key"})
    image = _text(archive["source_image_digest"])
    if (
        archive["format"] != "mangarr-backup"
        or archive["self_contained"] is not True
        or any(v is not True for v in components.values())
        or not image.startswith("sha256:")
    ):
        raise InvalidIdentity("ownership_wire")
    _ = _hex(image[7:])
    return plan


def _prior_relationship(value: object) -> None:
    if type(value) is not dict:
        raise InvalidIdentity("ownership_wire")
    fields = cast(dict[str, object], value)
    kind = _text(fields.get("kind"))
    shapes = {
        "publication-destination": {
            "kind",
            "publication_id",
            "publication_file_id",
            "queue_id",
            "file_id",
        },
        "frozen-pack-origin": {
            "kind",
            "publication_id",
            "queue_id",
            "file_id",
            "identity_key",
            "artifact_owner_token",
            "origin_sha256",
            "origin_ownership_sha256",
            "relative",
        },
        "pack-source-file": {
            "kind",
            "queue_id",
            "queue_file_id",
            "identity_key",
            "artifact_owner_token",
            "relative",
            "origin_ownership_sha256",
        },
        "pack-source-root": {
            "kind",
            "queue_id",
            "queue_file_id",
            "identity_key",
            "artifact_owner_token",
            "relative",
            "origin_ownership_sha256",
        },
        "transferred-pack-claim-source-file": {
            "kind",
            "claim_owner",
            "claim_role",
            "claim_snapshot_sha256",
            "claim_sha256",
            "queue_snapshot_sha256",
            "queue_id",
            "identity_key",
            "artifact_owner_token",
            "queue_file_id",
            "relative",
            "source_fingerprint",
        },
        "transferred-pack-claim-source-root": {
            "kind",
            "claim_owner",
            "claim_role",
            "claim_snapshot_sha256",
            "claim_sha256",
            "queue_snapshot_sha256",
            "queue_id",
            "identity_key",
            "artifact_owner_token",
            "queue_file_id",
            "relative",
            "source_fingerprint",
        },
    }
    if kind not in shapes:
        raise InvalidIdentity("ownership_wire")
    fields = _object(fields, shapes[kind])
    for name, scalar in fields.items():
        if name.endswith("sha256"):
            _ = _hex(scalar)
        elif name in {
            "publication_id",
            "publication_file_id",
            "queue_id",
            "queue_file_id",
            "file_id",
        }:
            if scalar is not None or name not in {"publication_id", "queue_file_id"}:
                _ = _integer(scalar, 1)
        elif name == "artifact_owner_token":
            label = _text(scalar)
            if label in {".", ".."} or "/" in label or "\\" in label:
                raise InvalidIdentity("ownership_wire")
        elif name == "claim_owner":
            _ = _owner_from(scalar)
        elif name == "claim_role":
            _ = _role_from(scalar)
        elif name == "source_fingerprint":
            if scalar is not None:
                fingerprint = _object(
                    scalar, {"dev", "inode", "size", "mtime_ns", "sha256"}
                )
                for column in ("dev", "inode", "size", "mtime_ns"):
                    _ = _integer(
                        fingerprint[column], -(2**63) if column == "mtime_ns" else 0
                    )
                _ = _hex(fingerprint["sha256"])
        elif name == "relative":
            if scalar is not None:
                relative = _text(scalar)
                if (
                    relative.startswith("/")
                    or "\\" in relative
                    or any(p in {"", ".", ".."} for p in relative.split("/"))
                ):
                    raise InvalidIdentity("ownership_wire")
        else:
            _ = _text(scalar)
    if kind.endswith("-root") and any(
        fields[name] is not None for name in ("queue_file_id", "relative")
    ):
        raise InvalidIdentity("ownership_wire")
    if kind.endswith("-file") and (
        fields["queue_file_id"] is None or fields["relative"] is None
    ):
        raise InvalidIdentity("ownership_wire")


def _provision_intent(encoded: str) -> dict[str, object]:
    try:
        _ = _text(encoded)
        if len(encoded) > _MAX_JSON or len(encoded.encode("utf-8")) > _MAX_JSON:
            raise InvalidIdentity("ownership_budget")
        value = _version(
            _load(encoded),
            {
                "kind",
                "plan_json",
                "plan_sha256",
                "inventory_sha256",
                "epoch_id",
                "authority_id",
                "authority_incarnation",
                "scope",
                "profile",
                "origin_path",
                "allocation_path",
                "root_path",
                "namespace_owner",
                "allocation_token",
            },
        )
        if value["kind"] != "epoch-provision-intent":
            raise InvalidIdentity("ownership_wire")
        for name in (
            "epoch_id",
            "authority_id",
            "authority_incarnation",
            "allocation_token",
        ):
            _ = _hex(value[name], 32)
        scope = _version(value["scope"], {"scope_path", "scope_token"})
        _ = _hex(scope["scope_token"], 32)
        _ = _provision_profile(value["profile"])
        origin, allocation, root = (
            _path(value[n]) for n in ("origin_path", "allocation_path", "root_path")
        )
        if (
            allocation
            != origin.rstrip("/") + "/.mangarr-epoch-" + _text(value["epoch_id"])
            or root != allocation + "/.mangarr-claims"
            or not _within(origin, _path(scope["scope_path"]))
            or _owner_from(value["namespace_owner"])
            != OwnerRef("namespace", (allocation,))
        ):
            raise InvalidIdentity("ownership_binding")
        plan = _prior_plan(_text(value["plan_json"]))
        inventory = cast(dict[str, object], plan["inventory"])
        closure = cast(dict[str, object], inventory["closure"])
        if any(
            _owner_from(cast(dict[str, object], item)["owner"])
            == _owner_from(value["namespace_owner"])
            for item in _list(closure["snapshots"])
        ) or any(
            _within(_path(cast(dict[str, object], item)["path"]), allocation)
            for item in _list(closure["roles"])
        ):
            raise InvalidIdentity("ownership_binding")
        for item in _list(closure["raw_rows"]):
            raw = cast(dict[str, object], item)
            columns = {
                _text(_list(pair)[0]): _list(pair)[1] for pair in _list(raw["values"])
            }
            if (
                raw["table"] in {"file_identity_epochs", "file_identity_sets"}
                and columns.get("epoch_id") == value["epoch_id"]
            ):
                raise InvalidIdentity("ownership_binding")
            if raw["table"] == "file_identity_provision_intents":
                parsed = _load(columns.get("payload_json"))
                if type(parsed) is not dict:
                    raise InvalidIdentity("ownership_wire")
                reserved = cast(dict[str, object], parsed)
                if (
                    reserved.get("epoch_id") == value["epoch_id"]
                    or _within(allocation, _path(reserved.get("allocation_path")))
                    or _within(_path(reserved.get("allocation_path")), allocation)
                ):
                    raise InvalidIdentity("ownership_binding")
            if raw["table"] == "file_identity_events":
                parsed = _load(columns.get("payload_json"))
                if type(parsed) is not dict:
                    raise InvalidIdentity("ownership_wire")
                payload = cast(dict[str, object], parsed)
                if payload.get("epoch_id") == value["epoch_id"]:
                    raise InvalidIdentity("ownership_binding")
        if (
            value["plan_sha256"] != plan["sha256"]
            or value["inventory_sha256"] != inventory["sha256"]
        ):
            raise InvalidIdentity("ownership_binding")
        epochs = cast(dict[str, object], _load(plan["epoch_bindings_json"]))
        matches = [
            cast(dict[str, object], i)
            for i in _list(epochs["provision"])
            if cast(dict[str, object], i)["epoch_id"] == value["epoch_id"]
        ]
        if len(matches) != 1:
            raise InvalidIdentity("ownership_binding")
        selected = matches[0]
        if (
            selected["authority_id"] != value["authority_id"]
            or selected["origin_parent"] != origin
            or selected["allocation_parent"] != allocation
            or selected["root_path"] != root
            or selected["handling"] != "retain-old-disable-new-allocation"
        ):
            raise InvalidIdentity("ownership_binding")
        declaration = cast(dict[str, object], _load(plan["declaration_json"]))
        authorities = [
            cast(dict[str, object], a)
            for a in _list(declaration["authorities"])
            if cast(dict[str, object], a)["authority_id"] == value["authority_id"]
        ]
        if len(authorities) != 1 or any(
            authorities[0][key] != value[field]
            for key, field in (
                ("incarnation_token", "authority_incarnation"),
                ("scope", "scope"),
                ("profile", "profile"),
            )
        ):
            raise InvalidIdentity("ownership_authority")
        if any(
            cast(dict[str, object], e)["epoch_id"] == value["epoch_id"]
            for name in ("existing", "allocation_capabilities")
            for e in _list(epochs[name])
        ):
            raise InvalidIdentity("ownership_binding")
        return value
    except InvalidIdentity as exc:
        if str(exc).startswith("ownership_"):
            raise
        raise InvalidIdentity("ownership_wire") from exc
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise InvalidIdentity("ownership_wire") from exc


def _epoch_birth(encoded: str) -> dict[str, object]:
    value = _object(
        _load(encoded),
        {"version", "layout", "enrollment", "legacy_namespace", "controlled_birth"},
    )
    version = _integer(value["version"], 1, 2)
    if version == 1:
        if value["layout"] not in {"same_parent", "dedicated_parent"} or value[
            "enrollment"
        ] not in {"birth", "exact_legacy", "recovery"}:
            raise InvalidIdentity("invalid epoch enrollment or layout")
        legacy, controlled = value["legacy_namespace"], value["controlled_birth"]
        if legacy is not None:
            _ = _legacy(legacy, namespace=True)
        if controlled is not None:
            old = _object(
                controlled,
                {
                    "authority_incarnation",
                    "allocation_token",
                    "root_native",
                    "marker_native",
                    "receipt_sha256",
                },
            )
            _ = _hex(old["authority_incarnation"], 32)
            _ = _hex(old["allocation_token"], 32)
            _ = _hex(old["receipt_sha256"])
            _ = NativeHandle.from_value(old["root_native"])
            _ = NativeHandle.from_value(old["marker_native"])
        if (
            (
                value["enrollment"] == "birth"
                and (controlled is None or legacy is not None)
            )
            or (
                value["enrollment"] == "exact_legacy"
                and (legacy is None or controlled is not None)
            )
            or (legacy is None and controlled is None)
        ):
            raise InvalidIdentity("epoch receipt/enrollment mismatch")
        return value
    if (
        value["layout"] != "dedicated_parent"
        or value["enrollment"] != "birth"
        or value["legacy_namespace"] is not None
    ):
        raise InvalidIdentity("ownership_wire")
    receipt = _object(
        value["controlled_birth"],
        {
            "version",
            "authority_incarnation",
            "allocation_token",
            "namespace_owner",
            "intent_id",
            "plan_sha256",
            "epoch_id",
            "authority_id",
            "origin_path",
            "allocation_path",
            "root_path",
            "objects",
            "receipt_sha256",
        },
    )
    _ = _integer(receipt["version"], 2, 2)
    for name in (
        "authority_incarnation",
        "allocation_token",
        "intent_id",
        "epoch_id",
        "authority_id",
    ):
        _ = _hex(receipt[name], 32)
    _ = _hex(receipt["plan_sha256"])
    origin, allocation, root = (
        _path(receipt[n]) for n in ("origin_path", "allocation_path", "root_path")
    )
    owner = OwnerRef("namespace", (allocation,))
    if (
        receipt["allocation_token"] != receipt["intent_id"]
        or _owner_from(receipt["namespace_owner"]) != owner
        or allocation
        != origin.rstrip("/") + "/.mangarr-epoch-" + _text(receipt["epoch_id"])
        or root != allocation + "/.mangarr-claims"
    ):
        raise InvalidIdentity("ownership_binding")
    objects = [_object_from(o) for o in _list(receipt["objects"])]
    keys = [_encode(_role_value(o.role)) for o in objects]
    _ordered_unique(keys)
    parent = RoleKey(None, "namespace_parent", "directory", None)
    root_role = RoleKey(None, "namespace_root", "directory", ".mangarr-claims")
    expected = {
        RoleKey(None, "epoch_origin_parent", "directory", None): (
            origin,
            None,
            "parent_identity",
        ),
        parent: (allocation, None, "exclusive_parent"),
        root_role: (root, parent, "private_directory"),
        RoleKey(None, "namespace_marker", "marker", "owner.json"): (
            root + "/owner.json",
            root_role,
            "marker",
        ),
    }
    if {o.role for o in objects} != set(expected) or len(objects) != 4:
        raise InvalidIdentity("ownership_binding")
    legacy_text: set[str] = set()
    uids: set[int] = set()
    for obj in objects:
        path, parent_role, policy = expected[obj.role]
        constraints = _constraints(obj.constraints_json, obj.role)
        binding = cast(dict[str, object], constraints["binding"])
        fields = cast(dict[str, object], constraints["non_device"])
        if (
            obj.canonical_path != path
            or obj.parent_role != parent_role
            or constraints["policy"] != policy
            or _owner_from(binding["owner"]) != owner
            or binding["operation_key"] != receipt["epoch_id"]
            or binding["owner_token"] != receipt["intent_id"]
        ):
            raise InvalidIdentity("ownership_binding")
        if policy == "parent_identity":
            if constraints["legacy"] is not None:
                raise InvalidIdentity("ownership_binding")
        else:
            legacy = _legacy(constraints["legacy"])
            if legacy["kind"] != "namespace_v2" or fields["mode"] != (
                0o600 if policy == "marker" else 0o700
            ):
                raise InvalidIdentity("ownership_binding")
            legacy_text.add(_text(legacy["json"]))
            uids.add(_integer(fields["uid"]))
    if len(legacy_text) != 1 or len(uids) != 1:
        raise InvalidIdentity("ownership_binding")
    registry = _birth_namespace(next(iter(legacy_text)))
    boundary = cast(dict[str, object], registry["creation_boundary"])
    if (
        registry["owner_token"] != receipt["intent_id"]
        or registry["root_path"] != root
        or boundary["parent_uid"] not in uids
        or boundary["parent_mode"] != 0o700
    ):
        raise InvalidIdentity("ownership_binding")
    fingerprint = cast(dict[str, object], registry["marker_fingerprint"])
    marker_fields = {
        name: fingerprint[name] for name in ("inode", "size", "mtime_ns", "sha256")
    }
    for obj in objects:
        fields = cast(
            dict[str, object],
            _constraints(obj.constraints_json, obj.role)["non_device"],
        )
        if obj.role.purpose == "namespace_marker" and any(
            fields[name] != scalar for name, scalar in marker_fields.items()
        ):
            raise InvalidIdentity("ownership_binding")
        if obj.role.purpose in {"namespace_parent", "namespace_root"} and fields[
            "inode"
        ] != (
            registry["parent_ino"]
            if obj.role.purpose == "namespace_parent"
            else registry["root_ino"]
        ):
            raise InvalidIdentity("ownership_binding")
    if _hex(receipt["receipt_sha256"]) != _digest(
        _encode({k: v for k, v in receipt.items() if k != "receipt_sha256"})
    ):
        raise InvalidIdentity("ownership_binding")
    return value


def _provision_snapshot(
    value: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    records = [
        _row(r, set(_PROVISION_ROWS), version=2) for r in _list(value["related"])
    ]
    if [r["table"] for r in records] != [
        "file_identity_epochs",
        "file_identity_provision_intents",
    ]:
        raise InvalidIdentity("ownership_wire")
    epoch, row = (cast(dict[str, object], r["row"]) for r in records)
    intent = _provision_intent(_text(row["payload_json"]))
    birth = _epoch_birth(_text(epoch["birth_json"]))
    if birth["version"] != 2:
        raise InvalidIdentity("ownership_wire")
    receipt = cast(dict[str, object], birth["controlled_birth"])
    if (
        row["intent_id"] != intent["allocation_token"]
        or row["intent_id"] != receipt["intent_id"]
        or row["payload_sha256"] != _digest(_encode(intent))
        or row["authority_id"] != intent["authority_id"]
    ):
        raise InvalidIdentity("ownership_binding")
    for name in (
        "epoch_id",
        "authority_id",
        "origin_path",
        "allocation_path",
        "root_path",
    ):
        if epoch[name] != intent[name] or epoch[name] != receipt[name]:
            raise InvalidIdentity("ownership_binding")
    for name in (
        "authority_incarnation",
        "namespace_owner",
        "plan_sha256",
        "allocation_token",
    ):
        if receipt[name] != intent[name]:
            raise InvalidIdentity("ownership_binding")
    root = cast(dict[str, object], cast(dict[str, object], value["root"])["row"])
    if (
        value["owner"] != intent["namespace_owner"]
        or root["parent_path"] != intent["allocation_path"]
    ):
        raise InvalidIdentity("ownership_binding")
    legacy = _birth_namespace(_text(root["ownership_json"]))
    if (
        legacy["owner_token"] != row["intent_id"]
        or legacy["root_path"] != epoch["root_path"]
    ):
        raise InvalidIdentity("ownership_binding")
    for item in _list(receipt["objects"]):
        obj = _object_from(item)
        constraints = _constraints(obj.constraints_json, obj.role)
        if (
            obj.role.purpose != "epoch_origin_parent"
            and cast(dict[str, object], constraints["legacy"])["json"]
            != root["ownership_json"]
        ):
            raise InvalidIdentity("ownership_binding")
    return intent, receipt


def _allocation_payload(encoded: str) -> dict[str, object]:
    _ = _text(encoded)
    if len(encoded) > _MAX_JSON or len(encoded.encode("utf-8")) > _MAX_JSON:
        raise InvalidIdentity("ownership_budget")
    value = _object(
        _load(encoded),
        {
            "version",
            "kind",
            "before_snapshot_json",
            "before_snapshot_sha256",
            "plan_sha256",
            "intent_id",
            "activation_event_id",
            "epoch_id",
            "authority_id",
            "authority_incarnation",
            "origin_path",
            "allocation_path",
            "root_path",
            "birth_sha256",
            "state",
            "prior_capability_event_id",
            "consumer_contract_sha256",
        },
    )
    _ = _integer(value["version"], 2, 2)
    if value["kind"] != "allocation-capability" or value["state"] not in {
        "grant",
        "revoke",
    }:
        raise InvalidIdentity("ownership_wire")
    for name in ("intent_id", "epoch_id", "authority_id", "authority_incarnation"):
        _ = _hex(value[name], 32)
    for name in (
        "before_snapshot_sha256",
        "plan_sha256",
        "birth_sha256",
        "consumer_contract_sha256",
    ):
        _ = _hex(value[name])
    for name in ("origin_path", "allocation_path", "root_path"):
        _ = _path(value[name])
    if (
        type(value["activation_event_id"]) is not int
        or not 1 <= cast(int, value["activation_event_id"]) <= 2**63 - 1
    ):
        raise InvalidIdentity("ownership_wire")
    prior = value["prior_capability_event_id"]
    if prior is not None:
        if type(prior) is not int or not 1 <= cast(int, prior) <= 2**63 - 1:
            raise InvalidIdentity("ownership_wire")
    if (value["state"] == "grant") != (prior is None):
        raise InvalidIdentity("ownership_phase")
    snapshot = _object(
        _load(value["before_snapshot_json"]), {"version", "owner", "root", "related"}
    )
    _ = _integer(snapshot["version"], 2, 2)
    if _digest(_text(value["before_snapshot_json"])) != value["before_snapshot_sha256"]:
        raise InvalidIdentity("ownership_binding")
    return value


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
        value = _object(_load(text), {"version", "owner", "root", "related"})
        if manifest.get("version") == 1 and value["version"] != 1:
            raise InvalidIdentity("ownership_wire")
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
    value = _object(
        _load(proof.manifest_json),
        {
            "version",
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
    version = _integer(value["version"], 1, 2)
    snapshot_value = cast(dict[str, object], _load(proof.snapshot.rows_json))
    if version != snapshot_value["version"]:
        raise InvalidIdentity("ownership_wire")
    provision = _provision_snapshot(snapshot_value) if version == 2 else None
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
        if version == 2:
            tagged = _object(
                cast(dict[str, object], intent),
                {"kind", "snapshot_sha256", "intent_id", "plan_sha256"},
            )
            if provision is None or tagged != {
                "kind": "epoch-provision",
                "snapshot_sha256": proof.snapshot.sha256,
                "intent_id": provision[1]["intent_id"],
                "plan_sha256": provision[0]["plan_sha256"],
            }:
                raise InvalidIdentity("ownership_binding")
        elif tag == "journal":
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
    absent_records = [
        _object(item, {"role", "canonical_path", "parent_role", "intent_sha256"})
        for item in _list(value["absent_roles"])
    ]
    # Discarded carriers can have absent descendants and an absent root after
    # GC. Validate the entire intended graph, not only parents still present.
    absent_parents = {
        _encode(_role_value(_role_from(a["role"]))): a for a in absent_records
    }
    for absent in absent_records:
        role = _role_from(absent["role"])
        path = _path(absent["canonical_path"])
        parent = (
            None if absent["parent_role"] is None else _role_from(absent["parent_role"])
        )
        key = _encode(_role_value(role))
        if parent is not None and _encode(_role_value(parent)) in absent_parents:
            ancestor = absent_parents[_encode(_role_value(parent))]
            if (
                parent.kind != "directory"
                or role.relative is None
                or path
                != _path(ancestor["canonical_path"]).rstrip("/") + "/" + role.relative
                or path in paths
            ):
                raise InvalidIdentity("absent role parent/path mismatch")
            paths.add(path)
        else:
            relationship(role, path, parent)
        edges[key] = [] if parent is None else [_encode(_role_value(parent))]
        if intent is None or _hex(absent["intent_sha256"]) != _digest(_encode(intent)):
            raise InvalidIdentity("absence lacks bound action intent")
        absent_keys.append(_encode(_role_value(role)))
    _acyclic(edges)
    _ordered_unique(absent_keys)
    if set(keys) & set(absent_keys) or sorted(keys + absent_keys) != required:
        raise InvalidIdentity("role evidence differs from declared requirements")
    dependencies = _dependencies(value, proof.owner)
    if provision is not None:
        intent, birth = provision
        if (
            proof.origin != "birth"
            or proof.epoch_id != intent["epoch_id"]
            or proof.authority_id != intent["authority_id"]
            or value["phase"] != "epoch-provisioned"
            or value["intent"] is None
            or value["absent_roles"]
            or [_object_from(o) for o in _list(birth["objects"])] != list(proof.roles)
        ):
            raise InvalidIdentity("ownership_binding")
        prior = _prior_plan(_text(intent["plan_json"]))
        protected = cast(
            dict[str, object], cast(dict[str, object], prior["inventory"])["closure"]
        )
        expected = {
            _owner_from(cast(dict[str, object], s)["owner"]): (
                cast(dict[str, object], s)["rows_json"],
                cast(dict[str, object], s)["sha256"],
            )
            for s in _list(protected["snapshots"])
        }
        if {s.owner: (s.rows_json, s.sha256) for s in dependencies} != expected:
            raise InvalidIdentity("ownership_binding")
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
    birth = _object(
        _load(row["birth_json"]),
        {"version", "layout", "enrollment", "legacy_namespace", "controlled_birth"},
    )
    birth_version = _integer(birth["version"], 1, 2)
    if birth_version == 2:
        birth = _epoch_birth(_text(row["birth_json"]))
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
    if controlled is not None and birth_version == 2:
        receipt = cast(dict[str, object], controlled)
        if (
            receipt["authority_incarnation"] != incarnation
            or receipt["epoch_id"] != proof.epoch_id
            or receipt["authority_id"] != proof.authority_id
            or any(
                receipt[name] != row[name]
                for name in ("origin_path", "allocation_path", "root_path")
            )
        ):
            raise InvalidIdentity("epoch incarnation/receipt binding mismatch")
    elif controlled is not None:
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
    manifest = _object(
        _load(manifest_json),
        {
            "version",
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
    _integer(manifest["version"], 1, 2)
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
    capability: dict[str, object] | None = None
    if latest["kind"] == "recovery_step" and owner.domain == "namespace":
        step = _load(_text(latest["payload_json"]))
        if type(step) is dict and cast(dict[str, object], step).get("version") == 1:
            _ = _version(
                step, {"before_snapshot_json", "before_snapshot_sha256", "plan_sha256"}
            )
            raise IdentityMismatch("latest identity history is not an activated set")
        capability = _allocation_payload(_text(latest["payload_json"]))
        activations = _dict_rows(
            db.execute(
                "SELECT * FROM file_identity_events WHERE id=? AND kind='activated' "
                "AND owner_domain=? AND owner_key_json=? AND set_id=?",
                (
                    capability["activation_event_id"],
                    owner.domain,
                    _key_json(owner),
                    latest["set_id"],
                ),
            )
        )
        if len(activations) != 1:
            raise IdentityMismatch(
                "allocation capability lacks its committed activation"
            )
        latest = activations[0]
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
    if capability is not None:
        snapshot = cast(dict[str, object], _load(proof.snapshot.rows_json))
        if snapshot["version"] != 2:
            raise IdentityMismatch(
                "allocation capability references a legacy activation"
            )
        intent, receipt = _provision_snapshot(snapshot)
        if (
            capability["before_snapshot_json"] != proof.snapshot.rows_json
            or capability["before_snapshot_sha256"] != proof.snapshot.sha256
            or capability["birth_sha256"]
            != _digest(
                _encode(
                    {
                        "version": 2,
                        "layout": "dedicated_parent",
                        "enrollment": "birth",
                        "legacy_namespace": None,
                        "controlled_birth": receipt,
                    }
                )
            )
            or any(
                capability[name] != intent[name]
                for name in (
                    "epoch_id",
                    "authority_id",
                    "authority_incarnation",
                    "origin_path",
                    "allocation_path",
                    "root_path",
                )
            )
            or capability["intent_id"] != intent["allocation_token"]
        ):
            raise IdentityMismatch(
                "allocation capability differs from complete activation"
            )
    if _data_version(db) != version:
        raise IdentityMismatch("identity changed during readback")
    return ActiveProofSet(proof, _integer(latest["id"], 1))


def unresolved_rescan_sql(alias: str = "rescan_file_operations") -> str:
    """SQL-only reciprocal fence: a nullable flag alone never releases ownership.

    The immutable event, exact projected preimage, canonical owner, and latest
    revision must all agree. Malformed JSON remains fenced, never evaluated.
    """
    if alias not in {"rescan_file_operations", "rescan_operation"}:
        raise ValueError("unsupported rescan selector alias")
    return _unresolved_journal_sql("rescan_file_operations", "rescan", alias)


def unresolved_deletion_sql(alias: str = "volume_file_deletions") -> str:
    """Keep a deletion fenced unless its own exact committed event resolves it."""
    if alias not in {"volume_file_deletions", "deletion"}:
        raise ValueError("unsupported deletion selector alias")
    return _unresolved_journal_sql("volume_file_deletions", "deletion", alias)


def _unresolved_journal_sql(table: str, domain: str, alias: str) -> str:
    columns = sorted(_PROJECTIONS[table])
    matches = " AND ".join(
        "json_type(s.snapshot_json,'$.root.row.recovery_event_id')='null'"
        if column == "recovery_event_id"
        else f"json_type(s.snapshot_json,'$.root.row.{column}') IS "
        f"CASE typeof({alias}.{column}) WHEN 'null' THEN 'null' "
        "WHEN 'integer' THEN 'integer' WHEN 'real' THEN 'real' ELSE 'text' END "
        f"AND json_extract(s.snapshot_json,'$.root.row.{column}') IS {alias}.{column}"
        for column in columns
    )
    return f"""NOT EXISTS (
        SELECT 1 FROM file_identity_events e JOIN file_identity_sets s USING(set_id)
        WHERE e.id={alias}.recovery_event_id
        AND CASE WHEN json_valid(e.payload_json) AND json_valid(s.snapshot_json)
        AND json_valid(e.owner_key_json) THEN
            e.kind='superseded' AND e.owner_domain='{domain}'
            AND s.owner_domain=e.owner_domain AND s.owner_key_json=e.owner_key_json
            AND e.owner_key_json=json_object('key',json_array({alias}.id),'version',1)
            AND e.id=(SELECT id FROM file_identity_events newer
                      WHERE newer.owner_domain=e.owner_domain
                      AND newer.owner_key_json=e.owner_key_json ORDER BY revision DESC LIMIT 1)
            AND json_type(e.payload_json,'$.version')='integer'
            AND json_extract(e.payload_json,'$.version')=1
            AND (SELECT count(*) FROM json_each(e.payload_json))=4
            AND length(json_extract(e.payload_json,'$.plan_sha256'))=64
            AND json_extract(e.payload_json,'$.plan_sha256') NOT GLOB '*[^0123456789abcdef]*'
            AND json_extract(e.payload_json,'$.before_snapshot_json') IS s.snapshot_json
            AND json_extract(e.payload_json,'$.before_snapshot_sha256') IS s.snapshot_sha256
            AND json_type(s.snapshot_json,'$.version')='integer'
            AND json_extract(s.snapshot_json,'$.version')=1
            AND json_extract(s.snapshot_json,'$.owner.domain')='{domain}'
            AND json_array_length(s.snapshot_json,'$.owner.key')=1
            AND json_type(s.snapshot_json,'$.owner.key[0]')='integer'
            AND json_extract(s.snapshot_json,'$.owner.key[0]') IS {alias}.id
            AND json_extract(s.snapshot_json,'$.root.table')='{table}'
            AND json_array_length(s.snapshot_json,'$.root.key')=1
            AND json_type(s.snapshot_json,'$.root.key[0]')='integer'
            AND json_extract(s.snapshot_json,'$.root.key[0]') IS {alias}.id
            AND (SELECT count(*) FROM json_each(s.snapshot_json,'$.root.row'))={len(columns)}
            AND {matches}
        ELSE 0 END
    )"""
