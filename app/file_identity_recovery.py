"""Disabled readonly observations, never mutation or historical identity authority.

SQLite owns ordinary read-only coordination. No SQL connection spans media I/O;
fresh full captures bracket current observations, not an atomic global snapshot.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import sqlite3
import stat
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Protocol, cast
from urllib.parse import quote

from file_identity import (
    IdentityUnavailable,
    IdentityMismatch,
    InvalidIdentity,
    NativeHandle,
    OwnerRef,
    RoleKey,
    capture_native,
)
from file_identity_consumers import (
    InventoryBudget,
    RawRow,
    ReferenceClosure,
    ReferenceDependency,
    RoleSpec,
    collect_references,
    roles_for,
)
from file_identity_store import OwnerSnapshot
from file_mutation_lock import FileMutationGuard, FileMutationLockError

_media = os
_DOCUMENT_BYTES = 16 * 1024**2
_CHUNK = 1024**2
_OUTCOMES = frozenset(
    {
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
    }
)
_COMPLETE = frozenset({"observed", "absent", "native_unsupported"})
_UNRESOLVED = frozenset(
    {"role_" + outcome for outcome in _OUTCOMES - _COMPLETE}
) | frozenset(
    {
        "reference_uncertain",
        "sql_changed",
        "sql_unavailable",
        "evidence_budget_exceeded",
    }
)


def _text(value: object) -> str:
    if type(value) is not str:
        raise InvalidIdentity("observation text required")
    result = value
    if (
        not result
        or "\x00" in result
        or any(0xD800 <= ord(c) <= 0xDFFF for c in result)
    ):
        raise InvalidIdentity("invalid observation text")
    return result


def _path(value: object) -> str:
    path = _text(value)
    if (
        not path.startswith("/")
        or "\\" in path
        or (path != "/" and any(p in {"", ".", ".."} for p in path[1:].split("/")))
    ):
        raise InvalidIdentity("noncanonical observation path")
    return path


def _within(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def _integer(value: object, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise InvalidIdentity("invalid observation integer")
    return value


def _object(value: object, keys: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(cast(dict[str, object], value)) != keys:
        raise InvalidIdentity("invalid observation object fields")
    return cast(dict[str, object], value)


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidIdentity("duplicate observation JSON key")
        result[key] = value
    return result


def _constant(_value: str) -> object:
    raise InvalidIdentity("nonfinite observation JSON")


def _wire_size(value: object, remaining: int = _DOCUMENT_BYTES, depth: int = 0) -> int:
    """Count canonical ASCII JSON before allocating escaped strings or a report."""
    if depth > 32:
        raise InvalidIdentity("observation nesting exceeds bound")
    if value is None:
        size = 4
    elif type(value) is bool:
        size = 4 if value else 5
    elif type(value) is int:
        size = len(str(value))
    elif type(value) is float:
        if not math.isfinite(value):
            raise InvalidIdentity("nonfinite observation JSON")
        size = len(json.dumps(value))
    elif type(value) is str:
        size = 2
        for character in value:
            code = ord(character)
            if 0xD800 <= code <= 0xDFFF or code == 0:
                raise InvalidIdentity("invalid observation text characters")
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
                raise InvalidIdentity("observation document exceeds byte bound")
    elif type(value) in {list, tuple}:
        values = cast(list[object] | tuple[object, ...], value)
        if len(values) > 10000:
            raise InvalidIdentity("observation collection exceeds bound")
        size = 2 + max(0, len(values) - 1)
        for item in values:
            size += _wire_size(item, remaining - size, depth + 1)
    elif type(value) is dict:
        mapping = cast(dict[str, object], value)
        if len(mapping) > 10000:
            raise InvalidIdentity("observation collection exceeds bound")
        size = 2 + max(0, len(mapping) - 1)
        for key, item in mapping.items():
            if type(key) is not str:
                raise InvalidIdentity("invalid observation key")
            size += _wire_size(key, remaining - size, depth + 1) + 1
            size += _wire_size(item, remaining - size, depth + 1)
    else:
        raise InvalidIdentity("unsupported observation JSON value")
    if size > remaining:
        raise InvalidIdentity("observation document exceeds byte bound")
    return size


def _encode(value: object) -> str:
    _ = _wire_size(value)
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _load(encoded: object) -> object:
    text = _text(encoded)
    if len(text) > _DOCUMENT_BYTES:
        raise InvalidIdentity("observation document exceeds byte bound")
    size = 0
    for offset in range(0, len(text), 65536):
        size += len(text[offset : offset + 65536].encode("utf-8"))
        if size > _DOCUMENT_BYTES:
            raise InvalidIdentity("observation document exceeds byte bound")
    # Bound container depth/counts BEFORE json.loads allocates container objects.
    containers: list[int] = []
    quoted = escaped = False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            containers.append(1)
            if len(containers) > 32:
                raise InvalidIdentity("observation nesting exceeds bound")
        elif character in "]}":
            if not containers:
                raise InvalidIdentity("invalid observation JSON structure")
            _ = containers.pop()
        elif character == "," and containers:
            containers[-1] += 1
            if containers[-1] > 10000:
                raise InvalidIdentity("observation collection exceeds bound")
    try:
        value = cast(
            object, json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
        )
        _ = _wire_size(value)
        return value
    except (ValueError, TypeError, RecursionError) as exc:
        raise InvalidIdentity("invalid observation JSON") from exc


def _constraints(encoded: str, spec: RoleSpec) -> dict[str, object]:
    value = _load(encoded)
    fields = _object(
        value,
        {
            "version",
            "kind",
            "current",
            "content",
            "expected",
            "policy",
            "boundary",
            "children",
        },
    )
    _ = _integer(fields["version"], 1, 1)
    if fields["kind"] != "readonly_observation" or fields["expected"] != (
        "unverified_legacy" if spec.legacy_json is not None else "undeclared"
    ):
        raise InvalidIdentity("invalid observation evidence kind")
    if _text(fields["policy"]) not in {"satisfied", "mismatch", "not_checked"} or _text(
        fields["boundary"]
    ) not in {"verified", "unknown", "cross_device"}:
        raise InvalidIdentity("invalid observation policy or boundary")
    current = fields["current"]
    if current is not None:
        info = _object(
            current,
            {"dev", "inode", "uid", "mode", "nlink", "size", "mtime_ns", "ctime_ns"},
        )
        for key in ("dev", "inode", "uid"):
            _ = _integer(info[key])
        _ = _integer(info["mode"], 0, 0o7777)
        for key in ("nlink", "size", "mtime_ns", "ctime_ns"):
            if spec.role.kind == "directory":
                if info[key] is not None:
                    raise InvalidIdentity(
                        "mutable directory identity is not durable evidence"
                    )
            else:
                _ = _integer(info[key], -(2**63) if key.endswith("_ns") else 0)
    content = fields["content"]
    if content is not None:
        if spec.role.kind == "directory" or current is None:
            raise InvalidIdentity("content lacks regular current object")
        data = _object(content, {"size", "sha256"})
        _ = _integer(data["size"])
        digest = _text(data["sha256"])
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise InvalidIdentity("invalid observed content digest")
        if cast(dict[str, object], current)["size"] != data["size"]:
            raise InvalidIdentity("observed content size mismatch")
    children = fields["children"]
    if type(children) is not list or len(cast(list[object], children)) > 10000:
        raise InvalidIdentity("invalid observation children")
    paths: list[str] = []
    for child in cast(list[object], children):
        item = _object(child, {"path", "outcome"})
        path = _path(item["path"])
        if (
            not _within(path, spec.path)
            or path == spec.path
            or _text(item["outcome"]) not in _OUTCOMES
        ):
            raise InvalidIdentity("invalid observation child")
        paths.append(path)
    if paths != sorted(set(paths)):
        raise InvalidIdentity("observation children duplicate or unordered")
    if encoded != _encode(value):
        raise InvalidIdentity("observation constraints are not canonical")
    return fields


@dataclass(frozen=True, slots=True)
class ObservedRole:
    spec: RoleSpec
    native: NativeHandle | None
    constraints_json: str
    outcome: str

    def __post_init__(self) -> None:
        if type(self.spec) is not RoleSpec:
            raise InvalidIdentity("observation requires role spec")
        self.spec.__post_init__()
        if _text(self.outcome) not in _OUTCOMES:
            raise InvalidIdentity("unknown observation outcome")
        if self.native is not None:
            if type(self.native) is not NativeHandle:
                raise InvalidIdentity("invalid observed native handle")
            self.native.__post_init__()
        fields = _constraints(self.constraints_json, self.spec)
        if self.outcome in _COMPLETE:
            if (
                fields["boundary"] != "verified"
                or fields["policy"] != "satisfied"
                or fields["children"]
            ):
                raise InvalidIdentity("complete observation lacks stable constraints")
            if self.outcome == "absent":
                if (
                    fields["current"] is not None
                    or fields["content"] is not None
                    or self.native is not None
                ):
                    raise InvalidIdentity("absence contains present-object evidence")
            else:
                if fields["current"] is None or (
                    self.spec.role.kind != "directory" and fields["content"] is None
                ):
                    raise InvalidIdentity("complete observation lacks current evidence")
                if (self.outcome == "observed") != (self.native is not None):
                    raise InvalidIdentity("native observation outcome mismatch")

    @property
    def unresolved(self) -> bool:
        """Incomplete acquisition only; complete still grants no action authority."""
        return self.outcome not in _COMPLETE


def _owner_value(owner: OwnerRef) -> dict[str, object]:
    return {"domain": owner.domain, "key": list(owner.key)}


def _role_value(role: RoleKey) -> dict[str, object]:
    return {
        "file_id": role.file_id,
        "purpose": role.purpose,
        "kind": role.kind,
        "relative": role.relative,
    }


def _spec_value(spec: RoleSpec) -> dict[str, object]:
    return {
        "role": _role_value(spec.role),
        "path": spec.path,
        "parent_role": None
        if spec.parent_role is None
        else _role_value(spec.parent_role),
        "legacy_json": spec.legacy_json,
        "presence": spec.presence,
        "policy": spec.policy,
        "owner": _owner_value(spec.owner),
    }


def _observed_value(record: ObservedRole) -> dict[str, object]:
    return {
        "spec": _spec_value(record.spec),
        "native": None if record.native is None else record.native.to_value(),
        "constraints_json": record.constraints_json,
        "outcome": record.outcome,
    }


def _closure_value(closure: ReferenceClosure) -> dict[str, object]:
    return {
        "snapshots": [
            {
                "owner": _owner_value(s.owner),
                "rows_json": s.rows_json,
                "sha256": s.sha256,
            }
            for s in closure.snapshots
        ],
        "roles": [_spec_value(r) for r in closure.roles],
        "dependencies": [
            {
                "owner": _owner_value(d.owner),
                "requires": _owner_value(d.requires),
                "reason": d.reason,
            }
            for d in closure.dependencies
        ],
        "raw_rows": [
            {
                "table": r.table,
                "key": list(r.key),
                "values": [list(pair) for pair in r.values],
            }
            for r in closure.raw_rows
        ],
    }


def _list(value: object) -> list[object]:
    if type(value) is not list or len(cast(list[object], value)) > 10000:
        raise InvalidIdentity("invalid observation collection")
    return cast(list[object], value)


def _owner_from(value: object) -> OwnerRef:
    fields = _object(value, {"domain", "key"})
    return OwnerRef(
        _text(fields["domain"]), tuple(cast(list[str | int], _list(fields["key"])))
    )


def _role_from(value: object) -> RoleKey:
    fields = _object(value, {"file_id", "purpose", "kind", "relative"})
    return RoleKey(
        None if fields["file_id"] is None else _integer(fields["file_id"]),
        _text(fields["purpose"]),
        _text(fields["kind"]),
        None if fields["relative"] is None else _text(fields["relative"]),
    )


def _spec_from(value: object) -> RoleSpec:
    fields = _object(
        value,
        {"role", "path", "parent_role", "legacy_json", "presence", "policy", "owner"},
    )
    return RoleSpec(
        _role_from(fields["role"]),
        _path(fields["path"]),
        None if fields["parent_role"] is None else _role_from(fields["parent_role"]),
        None if fields["legacy_json"] is None else _text(fields["legacy_json"]),
        _text(fields["presence"]),
        _text(fields["policy"]),
        _owner_from(fields["owner"]),
    )


def _closure_from(value: object) -> ReferenceClosure:
    fields = _object(value, {"snapshots", "roles", "dependencies", "raw_rows"})
    snapshots: list[OwnerSnapshot] = []
    dependencies: list[ReferenceDependency] = []
    rows: list[RawRow] = []
    for item in _list(fields["snapshots"]):
        snapshot = _object(item, {"owner", "rows_json", "sha256"})
        snapshots.append(
            OwnerSnapshot(
                _owner_from(snapshot["owner"]),
                _text(snapshot["rows_json"]),
                _text(snapshot["sha256"]),
            )
        )
    for item in _list(fields["dependencies"]):
        dependency = _object(item, {"owner", "requires", "reason"})
        dependencies.append(
            ReferenceDependency(
                _owner_from(dependency["owner"]),
                _owner_from(dependency["requires"]),
                _text(dependency["reason"]),
            )
        )
    for item in _list(fields["raw_rows"]):
        row = _object(item, {"table", "key", "values"})
        pairs: list[tuple[str, str | int | float | None]] = []
        for pair in _list(row["values"]):
            parts = _list(pair)
            if len(parts) != 2:
                raise InvalidIdentity("invalid raw observation pair")
            scalar = parts[1]
            if scalar is not None and type(scalar) not in {str, int, float}:
                raise InvalidIdentity("invalid raw observation scalar")
            pairs.append((_text(parts[0]), cast(str | int | float | None, scalar)))
        rows.append(
            RawRow(
                _text(row["table"]),
                tuple(cast(list[str | int], _list(row["key"]))),
                tuple(pairs),
            )
        )
    return ReferenceClosure(
        tuple(snapshots),
        tuple(_spec_from(r) for r in _list(fields["roles"])),
        tuple(dependencies),
        tuple(rows),
    )


@dataclass(frozen=True, slots=True)
class OwnershipInventory:
    version: int
    parent: str
    closure: ReferenceClosure
    observed: tuple[ObservedRole, ...]
    unresolved: tuple[str, ...]
    budget: InventoryBudget
    sha256: str

    def __post_init__(self) -> None:
        _ = _integer(self.version, 1, 1)
        _ = _path(self.parent)
        if (
            type(self.closure) is not ReferenceClosure
            or type(self.budget) is not InventoryBudget
        ):
            raise InvalidIdentity("invalid observation inventory input")
        self.closure.__post_init__()
        self.budget.__post_init__()
        if (
            type(self.observed) is not tuple
            or len(self.observed) > self.budget.max_objects
            or type(self.unresolved) is not tuple
            or len(self.unresolved) > 10000
        ):
            raise InvalidIdentity("invalid observation inventory collection")
        if any(type(record) is not ObservedRole for record in self.observed):
            raise InvalidIdentity("invalid observation record")
        _ = _wire_size(self._value())
        if self.closure.roles != tuple(
            role for snapshot in self.closure.snapshots for role in roles_for(snapshot)
        ):
            raise InvalidIdentity("observation closure omits or changes static roles")
        for item in self.unresolved:
            if _text(item) not in _UNRESOLVED:
                raise InvalidIdentity("unknown unresolved observation code")
        if list(self.unresolved) != sorted(set(self.unresolved)):
            raise InvalidIdentity("unresolved codes duplicate or unordered")
        observed_specs: list[RoleSpec] = []
        content_bytes = objects = 0
        for record in self.observed:
            if type(record) is not ObservedRole:
                raise InvalidIdentity("invalid observation record")
            record.__post_init__()
            constraints = _constraints(record.constraints_json, record.spec)
            content = constraints["content"]
            if content is not None:
                content_bytes += _integer(cast(dict[str, object], content)["size"])
            objects += 1 + len(cast(list[object], constraints["children"]))
            observed_specs.append(record.spec)
            if record.unresolved and "role_" + record.outcome not in self.unresolved:
                raise InvalidIdentity("incomplete role omitted from unresolved codes")
        if (
            content_bytes > self.budget.max_hash_bytes
            or objects > self.budget.max_objects
            or len(self.closure.roles) > self.budget.max_objects
            or len(self.closure.raw_rows) > self.budget.max_rows
        ):
            raise InvalidIdentity("observation evidence exceeds declared budget")
        present = set(observed_specs)
        if len(present) != len(observed_specs) or observed_specs != [
            r for r in self.closure.roles if r in present
        ]:
            raise InvalidIdentity("observed roles duplicate, unordered or undeclared")
        if len(observed_specs) != len(self.closure.roles) and not set(
            self.unresolved
        ).intersection(
            {
                "reference_uncertain",
                "sql_changed",
                "sql_unavailable",
                "evidence_budget_exceeded",
                "role_budget_exceeded",
            }
        ):
            raise InvalidIdentity("omitted roles lack incomplete evidence")
        digest = _text(self.sha256)
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise InvalidIdentity("invalid observation digest")
        if (
            digest
            != hashlib.sha256(
                _encode(self._value(digest=False)).encode("utf-8")
            ).hexdigest()
        ):
            raise InvalidIdentity("observation digest mismatch")

    def _value(self, *, digest: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "version": self.version,
            "parent": self.parent,
            "closure": _closure_value(self.closure),
            "observed": [_observed_value(r) for r in self.observed],
            "unresolved": list(self.unresolved),
            "budget": {
                "max_rows": self.budget.max_rows,
                "max_objects": self.budget.max_objects,
                "max_hash_bytes": self.budget.max_hash_bytes,
            },
        }
        if digest:
            value["sha256"] = self.sha256
        return value

    @classmethod
    def create(
        cls,
        parent: str,
        closure: ReferenceClosure,
        observed: tuple[ObservedRole, ...],
        unresolved: tuple[str, ...],
        budget: InventoryBudget,
    ) -> OwnershipInventory:
        if (
            type(closure) is not ReferenceClosure
            or type(budget) is not InventoryBudget
            or type(observed) is not tuple
            or type(unresolved) is not tuple
            or len(observed) > 10000
            or len(unresolved) > 10000
            or any(type(record) is not ObservedRole for record in observed)
        ):
            raise InvalidIdentity("invalid observation inventory input")
        # Build shallow wire references, not recursive asdict copies of old JSON.
        value = {
            "version": 1,
            "parent": parent,
            "closure": _closure_value(closure),
            "observed": [_observed_value(r) for r in observed],
            "unresolved": list(unresolved),
            "budget": {
                "max_rows": budget.max_rows,
                "max_objects": budget.max_objects,
                "max_hash_bytes": budget.max_hash_bytes,
            },
        }
        digest = hashlib.sha256(_encode(value).encode("utf-8")).hexdigest()
        return cls(1, parent, closure, observed, unresolved, budget, digest)

    def to_json(self) -> str:
        self.__post_init__()
        return _encode(self._value())

    @classmethod
    def from_json(cls, encoded: str) -> OwnershipInventory:
        fields = _object(
            _load(encoded),
            {
                "version",
                "parent",
                "closure",
                "observed",
                "unresolved",
                "budget",
                "sha256",
            },
        )
        limits = _object(
            fields["budget"], {"max_rows", "max_objects", "max_hash_bytes"}
        )
        budget = InventoryBudget(
            _integer(limits["max_rows"], 1),
            _integer(limits["max_objects"], 1, 10000),
            _integer(limits["max_hash_bytes"], 1),
        )
        records: list[ObservedRole] = []
        for item in _list(fields["observed"]):
            record = _object(item, {"spec", "native", "constraints_json", "outcome"})
            records.append(
                ObservedRole(
                    _spec_from(record["spec"]),
                    None
                    if record["native"] is None
                    else NativeHandle.from_value(record["native"]),
                    _text(record["constraints_json"]),
                    _text(record["outcome"]),
                )
            )
        return cls(
            _integer(fields["version"], 1, 1),
            _path(fields["parent"]),
            _closure_from(fields["closure"]),
            tuple(records),
            tuple(_text(v) for v in _list(fields["unresolved"])),
            budget,
            _text(fields["sha256"]),
        )


class _ObservationFailure(Exception):
    def __init__(self, outcome: str) -> None:
        super().__init__(outcome)
        self.outcome: str = outcome


@dataclass(slots=True)
class _ObservationBudget:
    limits: InventoryBudget
    objects: int = 0
    content: int = 0
    metadata: int = 0
    metadata_limit: int = _DOCUMENT_BYTES - 4096

    def object(self) -> None:
        if self.objects >= self.limits.max_objects:
            raise _ObservationFailure("budget_exceeded")
        self.objects += 1

    def evidence(
        self, encoded: str, *, fragment: bool = False, separator: bool = False
    ) -> None:
        try:
            size = _wire_size(encoded) - (2 if fragment else 0) + int(separator)
        except InvalidIdentity:
            raise _ObservationFailure("budget_exceeded") from None
        if size > self.metadata_limit - self.metadata:
            raise _ObservationFailure("budget_exceeded")
        self.metadata += size


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid


def _stable(info: os.stat_result) -> tuple[int, ...]:
    return (
        *_identity(info),
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _mount_id(fd: int) -> int:
    """Current same-session mount boundary, never durable identity evidence."""
    try:
        descriptor = os.open(
            f"/proc/self/fdinfo/{fd}", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        try:
            data = os.read(descriptor, 65537)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise IdentityUnavailable("mount boundary unavailable") from exc
    if len(data) > 65536:
        raise IdentityUnavailable("mount boundary metadata exceeds bound")
    values = [
        line.split(b":", 1)[1].strip()
        for line in data.splitlines()
        if line.startswith(b"mnt_id:")
    ]
    if len(values) != 1 or not values[0].isdigit():
        raise IdentityUnavailable("mount boundary unavailable")
    return _integer(int(values[0]), 1)


@dataclass(slots=True)
class _HeldPath:
    guard: FileMutationGuard
    stack: ExitStack
    entries: list[tuple[int, int, str, os.stat_result, bool]]
    scope_device: int | None = None
    scope_mount: int | None = None
    enumerated: list[tuple[int, tuple[int, ...]]] = field(default_factory=list)
    control_inodes: dict[str, tuple[int, int]] = field(default_factory=dict)

    def reject_control(
        self, info: os.stat_result | None, path: str | None = None
    ) -> None:
        self.guard.verify()
        database = self.guard._db_path
        reserved = (
            database,
            database + "-wal",
            database + "-shm",
            database + "-journal",
            self.guard._lock_path,
        )
        if path in reserved:
            raise _ObservationFailure("unsafe_path")
        # lstat only: opening even a rejected DB/SHM alias can release SQLite's
        # process-owned POSIX locks. Remember identities seen earlier as well.
        for control in reserved:
            try:
                current = os.lstat(control)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(current.st_mode):
                raise _ObservationFailure("unsafe_path")
            identity = current.st_dev, current.st_ino
            if self.control_inodes.setdefault(control, identity) != identity:
                raise _ObservationFailure("changed")
        if (
            info is not None
            and (info.st_dev, info.st_ino) in self.control_inodes.values()
        ):
            raise _ObservationFailure("unsafe_path")
        self.guard.verify()

    def verify(self) -> None:
        self.guard.verify()
        for fd, parent, name, before, bound in self.entries:
            held = _media.fstat(fd)
            if stat.S_ISREG(held.st_mode):
                self.reject_control(held)
            current = _media.stat(name, dir_fd=parent, follow_symlinks=False)
            evidence = _identity if stat.S_ISDIR(before.st_mode) else _stable
            if evidence(before) != evidence(held) or evidence(before) != evidence(
                current
            ):
                raise _ObservationFailure("changed")
            if bound and (
                held.st_dev != self.scope_device or _mount_id(fd) != self.scope_mount
            ):
                raise _ObservationFailure("cross_device")
        for fd, before_state in self.enumerated:
            if _stable(_media.fstat(fd)) != before_state:
                raise _ObservationFailure("changed")
        self.guard.verify()

    def open(self, path: str, scope: str) -> tuple[int, os.stat_result] | None:
        self.reject_control(None, path)
        root = _media.open(
            "/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        _ = self.stack.callback(_media.close, root)
        root_info = _media.fstat(root)
        self.entries.append((root, root, ".", root_info, scope == "/"))
        if scope == "/":
            self.scope_device, self.scope_mount = root_info.st_dev, _mount_id(root)
        parent = root
        components = path[1:].split("/") if path != "/" else []
        current_path = ""
        for index, name in enumerate(components):
            self.verify()
            current_path += "/" + name
            try:
                before = _media.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                if index != len(components) - 1 or self.scope_mount is None:
                    raise _ObservationFailure("unreadable") from None
                anchor = _media.fstat(parent)
                self.verify()
                try:
                    _ = _media.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    if _stable(anchor) != _stable(_media.fstat(parent)):
                        raise _ObservationFailure("changed") from None
                    return None
                raise _ObservationFailure("changed")
            if stat.S_ISLNK(before.st_mode):
                raise _ObservationFailure("unsafe_path")
            directory = index != len(components) - 1 or stat.S_ISDIR(before.st_mode)
            if directory and not stat.S_ISDIR(before.st_mode):
                raise _ObservationFailure("unsafe_path")
            if not directory and not stat.S_ISREG(before.st_mode):
                raise _ObservationFailure("unsafe_path")
            if not directory:
                self.reject_control(before, current_path)
            flags = (os.O_RDONLY | os.O_DIRECTORY) if directory else os.O_PATH
            descriptor = _media.open(
                name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent
            )
            _ = self.stack.callback(_media.close, descriptor)
            bound = _within(current_path, scope)
            if current_path == scope:
                if not directory:
                    raise _ObservationFailure("boundary_unknown")
                self.scope_device, self.scope_mount = (
                    before.st_dev,
                    _mount_id(descriptor),
                )
            self.entries.append((descriptor, parent, name, before, bound))
            self.verify()
            parent = descriptor
        if self.scope_mount is None:
            raise _ObservationFailure("boundary_unknown")
        return parent, _media.fstat(parent)


def _policy(spec: RoleSpec, info: os.stat_result) -> None:
    directory = spec.role.kind == "directory"
    if (
        stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    ) is False:
        raise _ObservationFailure("policy_mismatch")
    mode = stat.S_IMODE(info.st_mode)
    if spec.policy == "private_directory" and (
        info.st_uid != _media.geteuid() or mode != 0o700
    ):
        raise _ObservationFailure("policy_mismatch")
    if spec.policy == "exclusive_parent" and (
        info.st_uid != _media.geteuid() or mode & 0o022
    ):
        raise _ObservationFailure("policy_mismatch")
    if spec.policy == "marker" and (
        info.st_uid != _media.geteuid() or mode != 0o600 or info.st_nlink != 1
    ):
        raise _ObservationFailure("policy_mismatch")


def _hash(
    held: _HeldPath, fd: int, info: os.stat_result, budget: _ObservationBudget
) -> dict[str, object]:
    if info.st_size > budget.limits.max_hash_bytes - budget.content:
        raise _ObservationFailure("budget_exceeded")
    # Reopen an already pinned, verified regular O_PATH inode, never a pathname
    # which could have raced from regular to FIFO/device between stat and open.
    held.verify()
    held.reject_control(_media.fstat(fd))
    descriptor = _media.open(
        f"/proc/self/fd/{fd}", os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
    )
    _ = held.stack.callback(_media.close, descriptor)
    if _stable(_media.fstat(descriptor)) != _stable(info):
        raise _ObservationFailure("changed")
    digest = hashlib.sha256()
    size = 0
    while size < info.st_size:
        held.verify()
        if _stable(_media.fstat(descriptor)) != _stable(info):
            raise _ObservationFailure("changed")
        chunk = _media.read(descriptor, min(_CHUNK, info.st_size - size))
        budget.content += len(chunk)
        size += len(chunk)
        digest.update(chunk)
        held.verify()
        if not chunk or _stable(_media.fstat(descriptor)) != _stable(info):
            raise _ObservationFailure("changed")
    held.verify()
    return {"size": size, "sha256": digest.hexdigest()}


def _children(
    held: _HeldPath,
    fd: int,
    path: str,
    declared: frozenset[str],
    budget: _ObservationBudget,
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    pending = [(fd, path)]
    while pending:
        directory, parent_path = pending.pop()
        held.verify()
        before = _media.fstat(directory)
        held.enumerated.append((directory, _stable(before)))
        with _media.scandir(directory) as entries:
            for entry in entries:
                held.guard.verify()
                try:
                    child_path = _path(parent_path.rstrip("/") + "/" + entry.name)
                except InvalidIdentity:
                    budget.object()
                    raise _ObservationFailure("unsafe_path") from None
                if child_path in declared:
                    continue
                budget.object()
                child: dict[str, object] = {
                    "path": child_path,
                    "outcome": "unknown_children",
                }
                budget.evidence(_encode(child), fragment=True, separator=bool(result))
                result.append(child)
                info = _media.stat(entry.name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    child_fd = _media.open(
                        entry.name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=directory,
                    )
                    _ = held.stack.callback(_media.close, child_fd)
                    held.entries.append((child_fd, directory, entry.name, info, True))
                    held.verify()
                    pending.append((child_fd, child_path))
        held.verify()
        if _stable(before) != _stable(_media.fstat(directory)):
            raise _ObservationFailure("changed")
    return sorted(result, key=lambda child: cast(str, child["path"]))


def _current(info: os.stat_result, directory: bool) -> dict[str, object]:
    return {
        "dev": info.st_dev,
        "inode": info.st_ino,
        "uid": info.st_uid,
        "mode": stat.S_IMODE(info.st_mode),
        "nlink": None if directory else info.st_nlink,
        "size": None if directory else info.st_size,
        "mtime_ns": None if directory else info.st_mtime_ns,
        "ctime_ns": None if directory else info.st_ctime_ns,
    }


def _initial_constraints(spec: RoleSpec) -> dict[str, object]:
    return {
        "version": 1,
        "kind": "readonly_observation",
        "current": None,
        "content": None,
        "expected": "unverified_legacy"
        if spec.legacy_json is not None
        else "undeclared",
        "policy": "not_checked",
        "boundary": "unknown",
        "children": [],
    }


def _observe_role(
    guard: FileMutationGuard,
    spec: RoleSpec,
    scope: str,
    declared: frozenset[str],
    budget: _ObservationBudget,
) -> ObservedRole:
    """Observe one explicitly scoped role; no SQL, writes, or action permission."""
    guard.verify()
    scope = _path(scope)
    if type(spec) is not RoleSpec or type(budget) is not _ObservationBudget:
        raise InvalidIdentity("invalid observation input")
    spec.__post_init__()
    fields = _initial_constraints(spec)
    native = None
    metadata_before = budget.metadata
    try:
        budget.object()
        if not _within(spec.path, scope):
            raise _ObservationFailure("boundary_unknown")
        with ExitStack() as stack:
            held = _HeldPath(guard, stack, [])
            opened = held.open(spec.path, scope)
            fields["boundary"] = "verified"
            if opened is None:
                fields["policy"] = "satisfied"
                outcome = "absent"
            else:
                fd, info = opened
                _policy(spec, info)
                fields["policy"] = "satisfied"
                directory = spec.role.kind == "directory"
                fields["current"] = _current(info, directory)
                if directory:
                    if spec.role.purpose not in {
                        "source_directory",
                        "destination",
                        "namespace_parent",
                    }:
                        fields["children"] = _children(
                            held, fd, spec.path, declared, budget
                        )
                else:
                    fields["content"] = _hash(held, fd, info, budget)
                held.verify()
                try:
                    native = capture_native(fd)
                    outcome = "observed"
                except IdentityUnavailable:
                    outcome = "native_unsupported"
                held.verify()
                if fields["children"]:
                    outcome = "unknown_children"
            held.verify()
        guard.verify()
        encoded = _encode(fields)
        # Enumeration reserves fragments early; replace, do not double-charge,
        # those reservations with the final constraints envelope.
        budget.metadata = metadata_before
        budget.evidence(encoded)
    except (_ObservationFailure, OSError, IdentityUnavailable) as exc:
        guard.verify()
        native = None
        if isinstance(exc, _ObservationFailure):
            outcome = exc.outcome
        elif isinstance(exc, IdentityUnavailable):
            outcome = "boundary_unknown"
        else:
            outcome = (
                "changed"
                if exc.errno == errno.ENOENT
                else "unsafe_path"
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}
                else "unreadable"
            )
        fields.update(current=None, content=None, children=[])
        if outcome == "policy_mismatch":
            fields["policy"] = "mismatch"
        if outcome == "cross_device":
            fields["boundary"] = "cross_device"
        encoded = _encode(fields)
    return ObservedRole(spec, native, encoded, outcome)


def _observe_closure(
    guard: FileMutationGuard,
    parent: str,
    closure: ReferenceClosure,
    budget: InventoryBudget,
) -> OwnershipInventory:
    """Bound and observe an ALREADY captured closure, without opening SQLite.

    This private layer cannot establish SQL freshness. The future SQL caller
    must independently revalidate the complete closure before using its result.
    """
    guard.verify()
    parent = _path(parent)
    if type(closure) is not ReferenceClosure or type(budget) is not InventoryBudget:
        raise InvalidIdentity("invalid observation closure input")
    closure.__post_init__()
    budget.__post_init__()

    def refusal(code: str) -> OwnershipInventory:
        guard.verify()
        return OwnershipInventory.create(
            parent, ReferenceClosure((), ()), (), (code,), budget
        )

    if len(closure.roles) > budget.max_objects:
        return refusal("role_budget_exceeded")
    if len(closure.raw_rows) > budget.max_rows:
        return refusal("evidence_budget_exceeded")
    # Retain room for fixed unresolved codes and a final bounded native handle.
    limit = _DOCUMENT_BYTES - 4096
    try:
        prototype = OwnershipInventory.create(
            parent, closure, (), ("evidence_budget_exceeded",), budget
        )
        base_size = _wire_size(prototype._value())
    except InvalidIdentity:
        return refusal("evidence_budget_exceeded")
    if base_size > limit:
        return refusal("evidence_budget_exceeded")
    counter = _ObservationBudget(budget)
    declared = frozenset(spec.path for spec in closure.roles)
    observed: list[ObservedRole] = []
    unresolved: set[str] = set()
    observed_size = 0
    for spec in closure.roles:
        guard.verify()
        if counter.objects >= budget.max_objects:
            unresolved.add("role_budget_exceeded")
            break
        wrapper: dict[str, object] = {
            "spec": _spec_value(spec),
            "native": None,
            "constraints_json": "",
            "outcome": "native_unsupported",
        }
        separator = int(bool(observed))
        wrapper_size = _wire_size(wrapper) - 2
        minimum = _wire_size(_encode(_initial_constraints(spec)))
        remaining = limit - base_size - observed_size - separator - wrapper_size
        if minimum > remaining:
            unresolved.add("evidence_budget_exceeded")
            break
        counter.metadata = 0
        counter.metadata_limit = remaining
        record = _observe_role(guard, spec, parent, declared, counter)
        record_size = _wire_size(_observed_value(record)) + separator
        if record_size > limit - base_size - observed_size:
            unresolved.add("evidence_budget_exceeded")
            break
        observed.append(record)
        observed_size += record_size
        if record.unresolved:
            unresolved.add("role_" + record.outcome)
    guard.verify()
    return OwnershipInventory.create(
        parent, closure, tuple(observed), tuple(sorted(unresolved)), budget
    )


# Fixed acquisition shape, including empty tables: a version stamp alone does
# not establish the schema expected by the SQL-only reference adapter.
_SQL_COLUMNS = {
    "file_claim_namespaces": "parent_path ownership_json",
    "rescan_file_operations": "id version operation_token series_id volume_id "
    "source_path destination_path expected_volume_json expected_context_json "
    "fingerprints_json carriers_json publication_receipt_json state recovery_event_id",
    "volume_file_deletions": "id volume_id series_id state target_path parent_path "
    "claim_path target_present target_dev target_inode target_size target_mtime_ns "
    "target_sha256 series_title source_title original_import_path claim_carrier_json "
    "volume_num recovery_event_id",
    "import_pack_cleanup_reservations": "download_identity_key protocol "
    "normalized_download_id download_id purpose owner_token pack_path tombstone_path "
    "directory_ownership_json expires_at download_client_id queue_id publication_id",
    "import_pack_cleanup_tombstones": "tombstone_path download_identity_key protocol "
    "normalized_download_id download_id pack_path carrier_json download_client_id "
    "queue_id publication_id",
    "import_queue": "id series_id download_client_id respect_grab_claims volume_num "
    "download_id download_protocol torrent_name torrent_url src_dir status failed_at "
    "lease_owner lease_expires_at",
    "import_queue_files": "id queue_id proposed_is_special filename src_path dst_path "
    "proposed_import_kind proposed_special_title status file_type proposed_pack_type "
    "proposed_volume proposed_chapter proposed_volume_range_start "
    "proposed_volume_range_end proposed_chapter_range_end",
    "import_publications": "id queue_id series_id queue_download_client_id result_ok "
    "result_imported_count queue_volume_num state owner_token operation_owner "
    "operation_expires_at dst_dir import_mode staging_dir queue_snapshot_json "
    "series_snapshot_json series_tags_json queue_status queue_download_id "
    "queue_torrent_name queue_torrent_url queue_src_dir queue_failed_at queue_lease_owner "
    "queue_lease_expires_at queue_created_at result_queue_status pack_cleanup_state",
    "import_publication_files": "id publication_id ordinal file_id is_special "
    "has_volume_range is_legacy_chapter_stub is_legacy_chapter_recheck stage_ok "
    "source_dev source_inode source_size source_mtime_ns staged_dev staged_inode "
    "staged_size staged_mtime_ns final_expected_absent prepared_final_dev "
    "prepared_final_inode prepared_final_size prepared_final_mtime_ns proposed_vol "
    "proposed_chap chap_range_end vol_range_start vol_range_end src_path filename "
    "dst_path import_kind file_type pack_type special_title plan_status stage_path "
    "final_path source_sha256 source_claim_path staged_sha256 prepared_final_sha256 "
    "final_claim_path stage_state publish_state cleanup_state final_claim_carrier_json "
    "source_claim_carrier_json",
    "series": "id title root_folder_id",
    "volumes": "id series_id volume_num status import_path download_id download_client_id "
    "torrent_name indexer protocol client grabbed_at imported_at source_url release_group "
    "size_bytes quality",
    "chapters": "id series_id volume_id import_path",
    "chapter_quarantine": "id series_id volume_id state original_path volume_path "
    "carrier_path quarantine_path",
    "root_folders": "id path",
    "series_tags": "series_id tag",
    "file_identity_authorities": "authority_id incarnation_token profile_json "
    "declaration_json qualification_sha256",
    "file_identity_epochs": "epoch_id authority_id origin_path allocation_path root_path birth_json",
    "file_identity_sets": "set_id owner_domain owner_key_json prior_set_id snapshot_json "
    "snapshot_sha256 origin epoch_id manifest_json manifest_sha256",
    "file_identity_events": "id owner_domain owner_key_json revision predecessor_event_id "
    "set_id kind payload_json",
}
_SQL_BUDGET_ERRORS = frozenset(
    {
        "inventory budget exceeded",
        "inventory parsed-document budget exceeded",
        "inventory object count exceeds bound",
        "inventory closure exceeds collection bound",
        "snapshot row count exceeds bound",
        "identity JSON exceeds document bound",
    }
)


class _SQLFailure(Exception):
    def __init__(self, code: str) -> None:
        self.code: str = code
        super().__init__(code)


class _SQLParameters(Protocol):
    def __len__(self) -> int: ...

    def __getitem__(self, index: int, /) -> object: ...


class _ReadCursor(sqlite3.Cursor):
    def _checked(self, row: object) -> tuple[object, ...] | None:
        if row is None:
            return None
        values = cast(tuple[object, ...], row)
        names = tuple(column[0] for column in self.description)
        try:
            # Preflight the adapter's escaped raw-row representation before its
            # allocating serializer. The adapter still owns cumulative charging.
            _ = _wire_size(tuple(zip(names, values, strict=True)))
        except InvalidIdentity as exc:
            if str(exc) == "observation document exceeds byte bound":
                raise _SQLFailure("evidence_budget_exceeded") from exc
            raise
        return values

    def __next__(self) -> tuple[object, ...]:
        row = self._checked(super().__next__())
        assert row is not None
        return row

    def fetchone(self) -> tuple[object, ...] | None:
        return self._checked(super().fetchone())

    def fetchmany(self, size: int | None = None) -> list[tuple[object, ...]]:
        rows: list[tuple[object, ...]] = []
        for _ in range(self.arraysize if size is None else size):
            row = self.fetchone()
            if row is None:
                break
            rows.append(row)
        return rows


class _ReadConnection(sqlite3.Connection):
    """Own even adapter cursors whose callers do not explicitly close them."""

    def __init__(
        self,
        database: str,
        *,
        uri: bool = False,
        timeout: float = 0,
        factory: type[sqlite3.Connection] | None = None,
    ) -> None:
        super().__init__(database, uri=uri, timeout=timeout, factory=factory)
        self._owned_cursors: list[sqlite3.Cursor] = []

    def execute(
        self, sql: str, parameters: _SQLParameters | Mapping[str, object] = (), /
    ) -> sqlite3.Cursor:
        cursor = self.cursor(factory=_ReadCursor)
        self._owned_cursors.append(cursor)
        return cursor.execute(sql, parameters)

    def close(self) -> None:
        try:
            for cursor in self._owned_cursors:
                cursor.close()
        finally:
            self._owned_cursors.clear()
            super().close()


def _sql_paths(guard: FileMutationGuard) -> str:
    guard.verify()
    path = _path(guard._db_path)
    component = "/"
    for part in path[1:].split("/")[:-1]:
        component = os.path.join(component, part)
        if not stat.S_ISDIR(os.lstat(component).st_mode):
            raise InvalidIdentity("SQL config ancestry is not canonical no-follow")
    identities: set[tuple[int, int]] = set()
    for control in (
        path,
        path + "-wal",
        path + "-shm",
        path + "-journal",
        guard._lock_path,
    ):
        try:
            info = os.lstat(control)
        except FileNotFoundError:
            if control in {path, guard._lock_path}:
                guard.verify()
                raise
            continue
        identity = (info.st_dev, info.st_ino)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or identity in identities
        ):
            raise InvalidIdentity("unsafe SQL database or coordination path")
        identities.add(identity)
    guard.verify()
    return path


def _sql_schema(db: _ReadConnection) -> None:
    if db.execute("PRAGMA user_version").fetchone()[0] != 6:
        raise _SQLFailure("reference_uncertain")
    for table, columns in _SQL_COLUMNS.items():
        kind = db.execute(
            "SELECT type FROM sqlite_schema WHERE name=?", (table,)
        ).fetchone()
        if kind != ("table",):
            raise _SQLFailure("reference_uncertain")
        actual = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if not set(columns.split()) <= actual:
            raise _SQLFailure("reference_uncertain")


def _sql_error(exc: BaseException) -> str | None:
    if isinstance(
        exc,
        (
            sqlite3.ProgrammingError,
            sqlite3.IntegrityError,
            sqlite3.NotSupportedError,
            sqlite3.InternalError,
            sqlite3.InterfaceError,
        ),
    ):
        return None
    if isinstance(exc, sqlite3.Error):
        if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_TOOBIG:
            return "evidence_budget_exceeded"
        return "sql_unavailable"
    if isinstance(exc, InvalidIdentity) and str(exc) in _SQL_BUDGET_ERRORS:
        return "evidence_budget_exceeded"
    if isinstance(exc, IdentityMismatch):
        return (
            "sql_changed"
            if str(exc)
            in {
                "SQL reference inventory changed during capture",
                "same-connection SQL drift during inventory",
                "snapshot changed during capture",
            }
            else "reference_uncertain"
        )
    if isinstance(exc, InvalidIdentity) and isinstance(exc.__cause__, sqlite3.Error):
        return _sql_error(exc.__cause__)
    return None


def _capture_sql(
    guard: FileMutationGuard,
    parent: str,
    budget: InventoryBudget,
) -> ReferenceClosure:
    db: _ReadConnection | None = None
    try:
        path = _sql_paths(guard)
        db = sqlite3.connect(
            "file:" + quote(path, safe="/") + "?mode=ro&cache=private",
            uri=True,
            timeout=0,
            factory=_ReadConnection,
        )
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _DOCUMENT_BYTES)
        db.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 65536)
        # Include schema validation in this connection's acquisition freshness fence.
        version = cast(int, db.execute("PRAGMA data_version").fetchone()[0])
        _sql_schema(db)
        # Task4a's counter charges SQL evidence, not operator media content.
        sql_budget = InventoryBudget(
            budget.max_rows, budget.max_objects, _DOCUMENT_BYTES
        )
        result = collect_references(db, parent, sql_budget)
        if db.execute("PRAGMA data_version").fetchone()[0] != version:
            raise _SQLFailure("sql_changed")
        _sql_paths(guard)
        return result
    except (sqlite3.Error, InvalidIdentity, IdentityMismatch) as exc:
        if isinstance(exc, InvalidIdentity) and isinstance(
            exc.__cause__, sqlite3.Error
        ):
            if _sql_error(exc.__cause__) is None:
                raise exc.__cause__ from exc
        code = _sql_error(exc)
        if code is None:
            raise
        raise _SQLFailure(code) from exc
    except OSError as exc:
        raise _SQLFailure("sql_unavailable") from exc
    finally:
        try:
            if db is not None:
                db.close()
        finally:
            guard.verify()


def inspect_ownership(
    guard: FileMutationGuard,
    parent: str,
    budget: InventoryBudget,
) -> OwnershipInventory:
    """Observe current ownership references, with no recovery/mutation authority.

    The trusted local config and guard must stay in place. Fresh complete SQL
    recapture detects changed evidence, not every intermediate ABA or a global
    atomic FS/SQL state. Every owned SQL cursor/connection closes before media.
    """
    if not isinstance(guard, FileMutationGuard):
        raise FileMutationLockError("expected a live filesystem mutation guard")
    guard.verify()
    parent = _path(parent)
    if type(budget) is not InventoryBudget:
        raise InvalidIdentity("expected inventory budget")
    budget.__post_init__()
    try:
        try:
            closure = _capture_sql(guard, parent, budget)
        except _SQLFailure as exc:
            return OwnershipInventory.create(
                parent, ReferenceClosure((), ()), (), (exc.code,), budget
            )
        result = _observe_closure(guard, parent, closure, budget)
        unresolved = set(result.unresolved)
        try:
            if _capture_sql(guard, parent, budget) != closure:
                unresolved.add("sql_changed")
        except _SQLFailure as exc:
            unresolved.add(exc.code)
        return OwnershipInventory.create(
            parent, result.closure, result.observed, tuple(sorted(unresolved)), budget
        )
    finally:
        guard.verify()
