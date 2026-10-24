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
from bisect import bisect_left
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Protocol, cast
from urllib.parse import quote

import file_identity_consumers as _consumers
from private_file_claim import CarrierRecord, FullFileFingerprint, LinkReceipt

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
    _validate_closure,
    _owner as _validate_owner,
)
from file_identity_store import OwnerSnapshot, _dependencies as _proof_dependencies
import file_identity_store as _store
from file_mutation_lock import FileMutationGuard, FileMutationLockError

_media = os
_DOCUMENT_BYTES = 16 * 1024**2
_CHUNK = 1024**2
_UNREACHABLE = "unreachable_below_absent_root"
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
        _UNREACHABLE,
    }
)
_COMPLETE = frozenset({"observed", "absent", "native_unsupported"})
_UNRESOLVED = frozenset(
    {"role_" + outcome for outcome in _OUTCOMES - _COMPLETE - {_UNREACHABLE}}
) | frozenset(
    {
        "reference_uncertain",
        "sql_changed",
        "sql_unavailable",
        "evidence_budget_exceeded",
    }
)

# Wire semantics only, not operational consumer integration or profile approval.
_ALLOCATION_CONSUMER_CONTRACT = (
    b"mangarr/allocation-consumer-wire/2\n"
    b"intent1->birth2->snapshot2/manifest2->prepared1->activated1->grant2\n"
    b"namespace(allocation_path);latest-owner-chain;no-regrant\n"
)
_ALLOCATION_CONSUMER_SHA256 = hashlib.sha256(_ALLOCATION_CONSUMER_CONTRACT).hexdigest()


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    owner: OwnerRef
    action: str
    roles: tuple[RoleKey, ...]
    destination: str | None
    retained_roles: tuple[RoleKey, ...]

    def __post_init__(self) -> None:
        _validate_owner(self.owner)
        if _text(self.action) not in {
            "cancel-unmutated",
            "restore-original",
            "resume-commit",
            "keep-current",
            "cleanup-committed",
            "retain-blocked",
        }:
            raise InvalidIdentity("ownership_wire")
        for roles in (self.roles, self.retained_roles):
            if type(roles) is not tuple or len(roles) > 10000:
                raise InvalidIdentity("ownership_wire")
            for role in roles:
                if type(role) is not RoleKey:
                    raise InvalidIdentity("ownership_wire")
                role.__post_init__()
        _ = _decision_wire_size(self, _DOCUMENT_BYTES)
        for roles in (self.roles, self.retained_roles):
            order = [_encode(_role_value(r)) for r in roles]
            if order != sorted(set(order)):
                raise InvalidIdentity("ownership_wire")
        if set(self.roles).intersection(self.retained_roles):
            raise InvalidIdentity("ownership_wire")
        if (self.destination is not None) != (
            self.action in {"restore-original", "keep-current"}
        ):
            raise InvalidIdentity("ownership_wire")
        if self.destination is not None:
            _ = _path(self.destination)
        if self.action == "retain-blocked" and self.roles:
            raise InvalidIdentity("ownership_wire")
        if self.action != "retain-blocked" and not self.roles:
            raise InvalidIdentity("ownership_wire")

    def to_value(self) -> dict[str, object]:
        self.__post_init__()
        return {
            "owner": _owner_value(self.owner),
            "action": self.action,
            "roles": [_role_value(r) for r in self.roles],
            "destination": self.destination,
            "retained_roles": [_role_value(r) for r in self.retained_roles],
        }

    def to_json(self) -> str:
        return _encode(self.to_value())

    @classmethod
    def from_value(cls, value: object) -> RecoveryDecision:
        fields = _object(
            value, {"owner", "action", "roles", "destination", "retained_roles"}
        )
        return cls(
            _owner_from(fields["owner"]),
            _text(fields["action"]),
            tuple(_role_from(r) for r in _list(fields["roles"])),
            None if fields["destination"] is None else _path(fields["destination"]),
            tuple(_role_from(r) for r in _list(fields["retained_roles"])),
        )

    @classmethod
    def from_json(cls, encoded: str) -> RecoveryDecision:
        return cls.from_value(_load(encoded))


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    version: int
    inventory: OwnershipInventory
    decisions: tuple[RecoveryDecision, ...]
    declaration_json: str
    backup_receipt_json: str
    epoch_bindings_json: str
    sha256: str

    def __post_init__(self) -> None:
        _ = _integer(self.version, 1, 2)
        _validate_plan(self)
        if _plan_hash(self._value(digest=False)) != _plan_hex(self.sha256):
            raise InvalidIdentity("ownership_binding")

    def _value(self, *, digest: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "version": self.version,
            "inventory": self.inventory._value(),
            "decisions": [d.to_value() for d in self.decisions],
            "declaration_json": self.declaration_json,
            "backup_receipt_json": self.backup_receipt_json,
            "epoch_bindings_json": self.epoch_bindings_json,
        }
        if digest:
            value["sha256"] = self.sha256
        return value

    def to_json(self) -> str:
        self.__post_init__()
        return _plan_encode(self._value())

    @classmethod
    def from_json(cls, encoded: str) -> RecoveryPlan:
        fields = _object(
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
        return cls(
            _integer(fields["version"], 1, 2),
            OwnershipInventory.from_json(_plan_encode(fields["inventory"])),
            tuple(RecoveryDecision.from_value(d) for d in _list(fields["decisions"])),
            _text(fields["declaration_json"]),
            _text(fields["backup_receipt_json"]),
            _text(fields["epoch_bindings_json"]),
            _plan_hex(fields["sha256"]),
        )


def _plan_hex(value: object, size: int = 64) -> str:
    result = _text(value)
    if len(result) != size or any(c not in "0123456789abcdef" for c in result):
        raise InvalidIdentity("ownership_wire")
    return result


def _plan_fragments(value: object) -> Iterator[str]:
    """Only called after exact preflight; escape strings in bounded fragments."""
    if type(value) is dict:
        mapping = cast(dict[str, object], value)
        yield "{"
        for index, key in enumerate(sorted(mapping)):
            if index:
                yield ","
            yield from _plan_fragments(key)
            yield ":"
            yield from _plan_fragments(mapping[key])
        yield "}"
    elif type(value) in {list, tuple}:
        yield "["
        for index, item in enumerate(cast(list[object] | tuple[object, ...], value)):
            if index:
                yield ","
            yield from _plan_fragments(item)
        yield "]"
    elif type(value) is str:
        yield '"'
        for offset in range(0, len(value), 4096):
            yield json.dumps(value[offset : offset + 4096], ensure_ascii=True)[1:-1]
        yield '"'
    else:
        yield json.dumps(value, allow_nan=False, ensure_ascii=True)


def _plan_encode(value: object) -> str:
    size = _wire_size(value, _DOCUMENT_BYTES)
    fragments: list[str] = []
    emitted = 0
    for fragment in _plan_fragments(value):
        emitted += len(fragment)
        if emitted > size:
            raise InvalidIdentity("ownership_budget")
        fragments.append(fragment)
    if emitted != size:
        raise InvalidIdentity("ownership_budget")
    return "".join(fragments)


def _plan_hash(value: object) -> str:
    size = _wire_size(value, _DOCUMENT_BYTES)
    digest = hashlib.sha256()
    emitted = 0
    for fragment in _plan_fragments(value):
        emitted += len(fragment)
        if emitted > size:
            raise InvalidIdentity("ownership_budget")
        digest.update(fragment.encode("ascii"))
    if emitted != size:
        raise InvalidIdentity("ownership_budget")
    return digest.hexdigest()


def _plan_record(
    value: object, keys: set[str], *, version: int = 1
) -> dict[str, object]:
    if type(value) is not dict:
        raise InvalidIdentity("ownership_wire")
    fields = cast(dict[str, object], value)
    if set(fields) != keys | {"version"}:
        raise InvalidIdentity("ownership_wire")
    if type(fields["version"]) is not int or fields["version"] != version:
        raise InvalidIdentity("ownership_wire")
    return fields


def _plan_document(
    encoded: str, keys: set[str], *, version: int = 1
) -> dict[str, object]:
    fields = _plan_record(_plan_input(encoded), keys, version=version)
    if encoded != _plan_encode(fields):
        raise InvalidIdentity("ownership_wire")
    return fields


def _epoch_record(value: object, version: int) -> dict[str, object]:
    keys = {"role_bindings", "existing", "provision", "rollback_outputs"}
    if version == 2:
        keys |= {"linked_effects", "allocation_capabilities"}
    fields = _plan_record(value, keys, version=version)
    if version == 2:
        _ = _list(fields["allocation_capabilities"])
    return fields


def _decision_order(decision: RecoveryDecision) -> tuple[str, str, str, str, str]:
    return (
        _encode(_owner_value(decision.owner)),
        decision.action,
        "" if decision.destination is None else decision.destination,
        _encode([_role_value(r) for r in decision.roles]),
        _encode([_role_value(r) for r in decision.retained_roles]),
    )


def _plan_subject(record: dict[str, object]) -> tuple[OwnerRef, RoleKey]:
    owner = _owner_from(record["owner"])
    _validate_owner(owner)
    return owner, _role_from(record["role"])


def _decision_wire_size(decision: RecoveryDecision, remaining: int) -> int:
    size = _plan_size(
        {
            "owner": _owner_value(decision.owner),
            "action": decision.action,
            "roles": [],
            "destination": decision.destination,
            "retained_roles": [],
        },
        remaining,
    )
    for roles in (decision.roles, decision.retained_roles):
        for index, role in enumerate(roles):
            comma = int(index > 0)
            size += comma + _plan_size(_role_value(role), remaining - size - comma)
    return size


def _plan_constructor_size(plan: RecoveryPlan) -> int:
    size = _plan_size(
        {
            "version": plan.version,
            "inventory": plan.inventory._value(),
            "decisions": [],
            "declaration_json": plan.declaration_json,
            "backup_receipt_json": plan.backup_receipt_json,
            "epoch_bindings_json": plan.epoch_bindings_json,
            "sha256": plan.sha256,
        },
        _DOCUMENT_BYTES,
    )
    for index, decision in enumerate(plan.decisions):
        comma = int(index > 0)
        size += comma + _decision_wire_size(decision, _DOCUMENT_BYTES - size - comma)
    return size


def _plan_size(value: object, remaining: int) -> int:
    try:
        return _wire_size(value, remaining)
    except InvalidIdentity as exc:
        if str(exc) == "observation document exceeds byte bound":
            raise InvalidIdentity("ownership_budget") from exc
        raise


def _plan_ordered(
    records: list[object],
    identity: Callable[[dict[str, object]], str | tuple[str, str]],
) -> None:
    try:
        keys = [identity(_object_record(r)) for r in records]
    except (KeyError, TypeError) as exc:
        raise InvalidIdentity("ownership_wire") from exc
    if keys != sorted(set(keys)):
        raise InvalidIdentity("ownership_wire")


def _object_record(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise InvalidIdentity("ownership_wire")
    return cast(dict[str, object], value)


def _subject_order(record: dict[str, object]) -> tuple[str, str]:
    owner, role = _plan_subject(record)
    return _encode(_owner_value(owner)), _encode(_role_value(role))


def _validate_plan(plan: RecoveryPlan) -> None:
    if (
        type(plan.inventory) is not OwnershipInventory
        or (plan.version == 1 and plan.inventory.version != 1)
        or type(plan.decisions) is not tuple
    ):
        raise InvalidIdentity("ownership_wire")
    if len(plan.decisions) > 10000 or any(
        type(d) is not RecoveryDecision for d in plan.decisions
    ):
        raise InvalidIdentity("ownership_wire")
    _ = _plan_hex(plan.sha256)
    plan.inventory.__post_init__()
    if plan.inventory.unresolved or len(plan.inventory.observed) != len(
        plan.inventory.closure.roles
    ):
        raise InvalidIdentity("ownership_incomplete")
    plan_size = _plan_constructor_size(plan)
    epoch = _epoch_record(_plan_input(plan.epoch_bindings_json), plan.version)
    _ = _rollback_projection_size(plan.inventory, epoch, _DOCUMENT_BYTES - plan_size)
    expected = {s.owner: roles_for(s) for s in plan.inventory.closure.snapshots}
    coverage: dict[OwnerRef, list[RoleKey]] = {}
    selected: set[tuple[OwnerRef, RoleKey]] = set()
    for decision in plan.decisions:
        if type(decision) is not RecoveryDecision:
            raise InvalidIdentity("ownership_wire")
        decision.__post_init__()
        coverage.setdefault(decision.owner, []).extend(
            (*decision.roles, *decision.retained_roles)
        )
        selected.update((decision.owner, r) for r in decision.roles)
        if (
            not decision.roles
            and not decision.retained_roles
            and expected.get(decision.owner)
        ):
            raise InvalidIdentity("ownership_incomplete")
    order = [_decision_order(d) for d in plan.decisions]
    if order != sorted(set(order)):
        raise InvalidIdentity("ownership_wire")
    if set(coverage) != set(expected):
        raise InvalidIdentity("ownership_incomplete")
    for owner, specs in expected.items():
        roles = coverage[owner]
        if len(roles) != len(set(roles)) or set(roles) != {s.role for s in specs}:
            raise InvalidIdentity("ownership_incomplete")
        if not specs and sum(d.owner == owner for d in plan.decisions) != 1:
            raise InvalidIdentity("ownership_incomplete")
    if plan.epoch_bindings_json != _plan_encode(epoch):
        raise InvalidIdentity("ownership_wire")
    selection = {
        "version": plan.version,
        "inventory_sha256": plan.inventory.sha256,
        "decisions": [d.to_value() for d in plan.decisions],
        "epoch_bindings": epoch,
    }
    selection_hash = _plan_hash(selection)
    declaration = _plan_document(
        plan.declaration_json,
        {
            "inventory_sha256",
            "selection_sha256",
            "authorities",
            "owner_reviews",
            "role_reviews",
        }
        | ({"linked_reviews"} if plan.version == 2 else set()),
        version=plan.version,
    )
    receipt = _plan_document(
        plan.backup_receipt_json,
        {
            "inventory_sha256",
            "selection_sha256",
            "declaration_sha256",
            "archive",
            "owner_snapshots",
            "media",
        },
        version=plan.version,
    )
    for document in (declaration, receipt):
        if (
            _plan_hex(document["inventory_sha256"]) != plan.inventory.sha256
            or _plan_hex(document["selection_sha256"]) != selection_hash
        ):
            raise InvalidIdentity("ownership_binding")
    if _plan_hex(receipt["declaration_sha256"]) != _plan_hash(declaration):
        raise InvalidIdentity("ownership_binding")
    snapshots = {s.owner: s.sha256 for s in plan.inventory.closure.snapshots}
    for document, key, hash_key, fields in (
        (
            declaration,
            "owner_reviews",
            "snapshot_sha256",
            {"owner", "snapshot_sha256", "intended_original_authority_id"},
        ),
        (receipt, "owner_snapshots", "sha256", {"owner", "sha256"}),
    ):
        records = _list(document[key])
        _plan_ordered(records, lambda r: _encode(r["owner"]))
        seen: dict[OwnerRef, str] = {}
        for value in records:
            item = _object(value, fields)
            owner = _owner_from(item["owner"])
            _validate_owner(owner)
            seen[owner] = _plan_hex(item[hash_key])
        if seen != snapshots:
            raise InvalidIdentity("ownership_binding")
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
    _ = _plan_hex(archive["sha256"])
    _ = _integer(archive["format_version"], 1, 1)
    _ = _integer(archive["schema_version"], 7, 7)
    if archive["format"] != "mangarr-backup" or archive["self_contained"] is not True:
        raise InvalidIdentity("ownership_binding")
    components = _object(archive["components"], {"database", "configuration", "key"})
    if any(v is not True for v in components.values()):
        raise InvalidIdentity("ownership_binding")
    image = _text(archive["source_image_digest"])
    if not image.startswith("sha256:"):
        raise InvalidIdentity("ownership_wire")
    _ = _plan_hex(image[7:])
    _validate_linked_effects(plan)
    _validate_reviews(plan, declaration, receipt, epoch, selected)
    _validate_actions(plan)
    _validate_retained_references(plan)


def _required_linked_effects(
    inventory: OwnershipInventory, decisions: tuple[RecoveryDecision, ...]
) -> tuple[dict[str, object], ...]:
    """Derive consent requirements from captured producer bindings, not claims."""
    snapshots = {s.owner: s for s in inventory.closure.snapshots}
    observations = {(o.spec.owner, o.spec.role): o for o in inventory.observed}
    effects: list[dict[str, object]] = []
    for decision in decisions:
        if decision.action != "restore-original":
            continue
        if decision.owner.domain in {"pack_reservation", "pack_tombstone"}:
            projection = _consumers._inventory(snapshots[decision.owner])
            row, _ = _consumers._snapshot_records(
                snapshots[decision.owner], projection.evidence
            )
            if decision.owner.domain == "pack_tombstone":
                claims = [
                    ("tombstone", projection.evidence.claim(_text(row["carrier_json"])))
                ]
                raw_ownership = None
            else:
                raw_ownership = _text(row["directory_ownership_json"])
                proof = projection.evidence.pack(raw_ownership)
                claims = [
                    (f"pack_claim_{i}", claim) for i, claim in enumerate(proof.claims)
                ]
            for purpose, claim in claims:
                primary = RoleKey(None, purpose + "_captured", "directory", None)
                if primary not in decision.roles:
                    continue
                for spec, relationship in _consumers._pack_restore_references(
                    inventory.closure, decision.owner, claim, raw_ownership
                ):
                    observation = observations[(spec.owner, spec.role)]
                    if observation.outcome not in {"absent", _UNREACHABLE}:
                        raise InvalidIdentity("ownership_phase")
                    effects.append(
                        {
                            "decision_sha256": _plan_hash(decision.to_value()),
                            "original_role": _role_value(primary),
                            "affected_owner": _owner_value(spec.owner),
                            "affected_role": _role_value(spec.role),
                            "observation_sha256": _plan_hash(
                                _observed_value(observation)
                            ),
                            "relationship": relationship,
                            "effect": "create-name",
                            "handling": "retain-reference-non-replayable",
                        }
                    )
            continue
        if decision.owner.domain != "publication":
            continue
        snapshot = snapshots[decision.owner]
        projection = _consumers._inventory(snapshot)
        row, related = _consumers._snapshot_records(snapshot, projection.evidence)
        frozen = _object_record(_load(row["queue_snapshot_json"]))
        queue_owner = OwnerRef("queue_source", (_integer(row["queue_id"], 1),))
        queue_snapshot = snapshots.get(queue_owner)
        if queue_snapshot is None:
            raise InvalidIdentity("ownership_context")
        queue, _ = _consumers._snapshot_records(queue_snapshot, projection.evidence)
        # Frozen source/download/lease context must still describe this operation.
        if any(
            k not in frozen or type(frozen[k]) is not type(v) or frozen[k] != v
            for k, v in queue.items()
        ):
            raise InvalidIdentity("ownership_context")
        from download_identity import normalize_download_id

        protocol = _object_record(frozen["_file_publication"])
        admission = _object_record(protocol["admission"])
        download = _consumers._identity(frozen, "download_protocol")
        if _plan_encode(admission["identity"]) != _plan_encode(
            [
                download.download_client_id,
                download.protocol,
                normalize_download_id(download.download_id, download.protocol),
            ]
        ):
            raise InvalidIdentity("ownership_context")
        files = [
            _object_record(r["row"])
            for r in related
            if r["table"] == "import_publication_files"
        ]
        queue_files = {
            _integer(dict(r.values)["id"], 1): dict(r.values)
            for r in inventory.closure.raw_rows
            if r.table == "import_queue_files"
        }
        for original in decision.roles:
            if original.purpose != "original_artifact":
                continue
            matches = [
                f
                for f in files
                if f["file_id"] == original.file_id and f["publication_id"] == row["id"]
            ]
            if len(matches) != 1:
                raise InvalidIdentity("ownership_context")
            file = matches[0]
            mapped = queue_files.get(_integer(file["file_id"], 1))
            final_path = (
                file["final_path"]
                if file["final_path"] is not None
                else file["dst_path"]
            )
            admitted = _object_record(
                _object_record(admission["files"])[str(file["file_id"])]
            )
            effective = _object_record(admitted["effective"])
            expected_effective = {
                k: file[k]
                for k in (
                    "import_kind",
                    "file_type",
                    "proposed_vol",
                    "proposed_chap",
                    "chap_range_end",
                    "vol_range_start",
                    "vol_range_end",
                    "pack_type",
                    "is_special",
                    "special_title",
                )
            }
            expected_effective["dst_path"] = final_path
            if effective != expected_effective:
                raise InvalidIdentity("ownership_context")
            if (
                mapped is None
                or mapped["queue_id"] != row["queue_id"]
                or mapped["src_path"] != file["src_path"]
                or mapped["dst_path"] != final_path
            ):
                raise InvalidIdentity("ownership_context")
            affected = observations.get(
                (queue_owner, RoleKey(original.file_id, "destination", "file", None))
            )
            public_roles = [
                o
                for (owner, role), o in observations.items()
                if owner == decision.owner
                and role.file_id == original.file_id
                and role.purpose == "final"
            ]
            public = public_roles[0] if len(public_roles) == 1 else None
            if (
                affected is None
                or public is None
                or affected.spec.path != final_path
                or decision.destination != final_path
            ):
                raise InvalidIdentity("ownership_context")
            if affected.outcome == "absent" and public.outcome == "absent":
                effect = "create-name"
            elif (
                affected.outcome == public.outcome == "observed"
                and public.spec.role in decision.roles
            ):
                fields = _constraints(affected.constraints_json, affected.spec)
                public_fields = _constraints(public.constraints_json, public.spec)
                if affected.native != public.native or any(
                    fields[k] != public_fields[k] for k in ("current", "content")
                ):
                    raise InvalidIdentity("ownership_phase")
                effect = "replace-acknowledged-name"
            else:
                raise InvalidIdentity("ownership_phase")
            effects.append(
                {
                    "decision_sha256": _plan_hash(decision.to_value()),
                    "original_role": _role_value(original),
                    "affected_owner": _owner_value(queue_owner),
                    "affected_role": _role_value(affected.spec.role),
                    "observation_sha256": _plan_hash(_observed_value(affected)),
                    "relationship": {
                        "kind": "publication-destination",
                        "publication_id": _integer(row["id"], 1),
                        "publication_file_id": _integer(file["id"], 1),
                        "queue_id": _integer(row["queue_id"], 1),
                        "file_id": _integer(file["file_id"], 1),
                    },
                    "effect": effect,
                    "handling": "retain-reference-non-replayable",
                }
            )
    return tuple(sorted(effects, key=_plan_encode))


def _validate_linked_effects(plan: RecoveryPlan) -> None:
    if plan.version == 1:
        return
    epoch = _epoch_record(_plan_input(plan.epoch_bindings_json), 2)
    supplied = _list(epoch["linked_effects"])
    _plan_ordered(supplied, _plan_encode)
    required = _required_linked_effects(plan.inventory, plan.decisions)
    if _plan_encode(supplied) != _plan_encode(list(required)):
        raise InvalidIdentity(
            "ownership_phase" if not supplied and required else "ownership_binding"
        )
    retained = {
        (d.owner, r)
        for d in plan.decisions
        if d.action == "retain-blocked"
        for r in d.retained_roles
    }
    identities: set[tuple[OwnerRef, RoleKey]] = set()
    for effect in required:
        identity = (
            _owner_from(effect["affected_owner"]),
            _role_from(effect["affected_role"]),
        )
        if identity in identities or identity not in retained:
            raise InvalidIdentity("ownership_phase")
        identities.add(identity)


def _validate_retained_references(plan: RecoveryPlan) -> None:
    """Check named effects only after exact domain/action validation."""
    observed = {(o.spec.owner, o.spec.role): o for o in plan.inventory.observed}
    references: dict[str, set[tuple[OwnerRef, RoleKey]]] = {}
    linked = (
        _required_linked_effects(plan.inventory, plan.decisions)
        if plan.version == 2
        else ()
    )
    action_owners = {_plan_hash(d.to_value()): d.owner for d in plan.decisions}
    permitted = {
        (
            action_owners[_text(e["decision_sha256"])],
            _owner_from(e["affected_owner"]),
            _role_from(e["affected_role"]),
        )
        for e in linked
    }
    effects: set[tuple[OwnerRef, str, bool]] = set()
    for decision in plan.decisions:
        # Selection does not stop another owner's binding from being a reference.
        for roles in (decision.roles, decision.retained_roles):
            for role in roles:
                path = observed[(decision.owner, role)].spec.path
                references.setdefault(path, set()).add((decision.owner, role))
        if decision.action == "cleanup-committed" or (
            decision.action == "resume-commit" and decision.owner.domain == "deletion"
        ):
            for role in decision.roles:
                observation = observed[(decision.owner, role)]
                if observation.outcome not in {"absent", _UNREACHABLE}:
                    effects.add(
                        (
                            decision.owner,
                            observation.spec.path,
                            role.kind == "directory",
                        )
                    )
        elif decision.action == "restore-original":
            if decision.destination is None:
                raise InvalidIdentity("ownership_phase")
            effects.add(
                (
                    decision.owner,
                    decision.destination,
                    decision.owner.domain in {"pack_reservation", "pack_tombstone"},
                )
            )
            if decision.owner.domain in {"pack_reservation", "pack_tombstone"}:
                for role in decision.roles:
                    effects.add(
                        (
                            decision.owner,
                            observed[(decision.owner, role)].spec.path,
                            role.kind == "directory",
                        )
                    )
    for pair in _rollback_pairs(plan).values():
        effects.add((pair.owner, pair.publication.path, False))
    paths = sorted(references)
    for owner, path, subtree in effects:
        if any(
            other != owner and (owner, other, role) not in permitted
            for other, role in references.get(path, ())
        ):
            raise InvalidIdentity("ownership_phase")
        if subtree:
            prefix = path.rstrip("/") + "/"
            index = bisect_left(paths, prefix)
            while index < len(paths) and paths[index].startswith(prefix):
                if any(
                    other != owner and (owner, other, role) not in permitted
                    for other, role in references[paths[index]]
                ):
                    raise InvalidIdentity("ownership_phase")
                index += 1


@dataclass(slots=True)
class _PlanOwner:
    plan: RecoveryPlan
    snapshot: OwnerSnapshot
    root: dict[str, object]
    records: list[dict[str, object]]
    observed: dict[RoleKey, ObservedRole]
    carriers: list[CarrierRecord]

    def roles(self, purpose: str) -> tuple[RoleKey, ...]:
        return tuple(r for r in self.observed if r.purpose == purpose)

    def one(self, purpose: str, file_id: int | None = None) -> RoleKey:
        values = [
            r for r in self.roles(purpose) if file_id is None or r.file_id == file_id
        ]
        if len(values) != 1:
            raise InvalidIdentity("ownership_phase")
        return values[0]

    def present(
        self, role: RoleKey, fingerprint: FullFileFingerprint | None = None
    ) -> None:
        observation = self.observed[role]
        if observation.outcome != "observed":
            raise InvalidIdentity("ownership_phase")
        fields = _constraints(observation.constraints_json, observation.spec)
        if fingerprint is not None:
            current = _object_record(fields["current"])
            content = _object_record(fields["content"])
            if (current["size"], current["mtime_ns"], content["sha256"]) != (
                fingerprint.size,
                fingerprint.mtime_ns,
                fingerprint.sha256,
            ):
                raise InvalidIdentity("ownership_phase")

    def absent(self, role: RoleKey) -> None:
        if self.observed[role].outcome != "absent":
            raise InvalidIdentity("ownership_phase")

    def complete_namespace(self, path: str) -> None:
        parent = path.rsplit("/", 1)[0] or "/"
        namespaces = [
            s
            for s in self.plan.inventory.closure.snapshots
            if s.owner == OwnerRef("namespace", (parent,))
        ]
        if len(namespaces) != 1:
            raise InvalidIdentity("ownership_phase")
        roles = [
            o
            for o in self.plan.inventory.observed
            if o.spec.owner == namespaces[0].owner
        ]
        if len(roles) != 3 or any(o.outcome != "observed" for o in roles):
            raise InvalidIdentity("ownership_phase")


def _validate_actions(plan: RecoveryPlan) -> None:
    for snapshot in plan.inventory.closure.snapshots:
        decisions = [d for d in plan.decisions if d.owner == snapshot.owner]
        actions = {d.action for d in decisions}
        if actions == {"retain-blocked"}:
            continue
        if len(actions) != 1 or "retain-blocked" in actions:
            raise InvalidIdentity("ownership_phase")
        inventory = _consumers._inventory(snapshot)
        root, related = _consumers._snapshot_records(snapshot, inventory.evidence)
        owner = _PlanOwner(
            plan,
            snapshot,
            root,
            related,
            {
                o.spec.role: o
                for o in plan.inventory.observed
                if o.spec.owner == snapshot.owner
            },
            inventory.carriers,
        )
        action = decisions[0].action
        subjects = {r for d in decisions for r in d.roles}
        if action == "cancel-unmutated":
            _cancel_plan(owner, subjects)
        elif snapshot.owner.domain == "rescan":
            _rescan_plan(owner, decisions, subjects)
        elif snapshot.owner.domain == "deletion":
            _deletion_plan(owner, decisions, subjects)
        elif snapshot.owner.domain == "publication":
            _publication_plan(owner, decisions, subjects)
        elif snapshot.owner.domain == "pack_reservation":
            proof = (
                inventory.evidence.pack(_text(root["directory_ownership_json"]))
                if root["directory_ownership_json"] is not None
                else None
            )
            if proof is not None and proof.claims:
                _pack_claim_plan(owner, decisions, subjects)
                continue
            if action == "restore-original":
                if len(decisions) != 1 or decisions[0].destination is None:
                    raise InvalidIdentity("ownership_phase")
                _pack_restore_plan(owner, subjects, decisions[0].destination)
            elif action == "cleanup-committed":
                _pack_cleanup_plan(owner, decisions, subjects)
            else:
                _pack_queue_plan(owner, decisions, subjects)
        elif snapshot.owner.domain == "pack_tombstone":
            _pack_claim_plan(owner, decisions, subjects)
        else:
            raise InvalidIdentity("ownership_phase")


def _pack_restore_subjects(owner: _PlanOwner, primary: RoleKey) -> frozenset[RoleKey]:
    from private_pack_claim import PackOwnership

    ownership = PackOwnership.from_json(_text(owner.root["directory_ownership_json"]))
    placement, proof = ownership.placement_carrier, ownership.canonical_directory
    canonical = owner.one("pack_canonical")
    if (
        primary != owner.one("pack_placement_artifact")
        or placement is None
        or proof is None
        or proof.path != placement.carrier_path + "/artifact"
        or owner.observed[primary].spec.path != proof.path
        or owner.observed[canonical].spec.path != proof.path
    ):
        raise InvalidIdentity("ownership_phase")
    # These are two producer bindings of one original, not a same-path waiver.
    return frozenset(
        {primary, canonical}
        | {s.role for s in roles_for(owner.snapshot) if s.parent_role == canonical}
    )


def _pack_restore_plan(
    owner: _PlanOwner, subjects: set[RoleKey], destination: str
) -> None:
    from import_pack_cleanup import _pack_owner_marker_payload
    from private_pack_claim import PackOwnership

    ownership = PackOwnership.from_json(_text(owner.root["directory_ownership_json"]))
    placement, proof, entries = (
        ownership.placement_carrier,
        ownership.canonical_directory,
        ownership.inventory,
    )
    if (
        owner.root["purpose"] != "cleanup"
        or owner.root["queue_id"] is not None
        or owner.root["publication_id"] is not None
        or ownership.phase != "ready"
        or ownership.private_directory is not None
        or ownership.claims
        or ownership.queue_receipt_sha256 is not None
        or placement is None
        or placement.phase != "claimed"
        or placement.restore_receipt is not None
        or proof is None
        or proof.marker_fingerprint is None
        or entries is None
        or not entries.files
        or destination != placement.origin_path
        or destination != owner.root["tombstone_path"]
    ):
        raise InvalidIdentity("ownership_phase")
    primary = owner.one("pack_placement_artifact")
    if frozenset(subjects) != _pack_restore_subjects(owner, primary):
        raise InvalidIdentity("ownership_phase")
    for role in (primary, owner.one("pack_canonical")):
        owner.present(role)
        current = _object_record(
            _constraints(
                owner.observed[role].constraints_json, owner.observed[role].spec
            )["current"]
        )
        if (current["dev"], current["inode"]) != (proof.dev, proof.inode):
            raise InvalidIdentity("ownership_phase")
    for relative in entries.directories:
        owner.present(RoleKey(None, "pack_canonical_directory", "directory", relative))
    for relative, fingerprint in entries.files.items():
        owner.present(
            RoleKey(None, "pack_canonical_file", "file", relative), fingerprint
        )
    marker = _pack_owner_marker_payload(
        _consumers._identity(owner.root), ownership.artifact_owner_token
    )
    if (
        proof.marker_fingerprint.size != len(marker)
        or proof.marker_fingerprint.sha256 != hashlib.sha256(marker).hexdigest()
        or _owner_carrier(owner, "pack_cleanup") != placement
    ):
        raise InvalidIdentity("ownership_phase")
    owner.present(owner.one("pack_canonical_marker"), proof.marker_fingerprint)
    owner.absent(owner.one("tombstone_path"))
    owner.absent(owner.one("pack_logical"))
    owner.complete_namespace(destination)
    namespace_owner = OwnerRef("namespace", (destination.rsplit("/", 1)[0] or "/",))
    snapshot = next(
        s for s in owner.plan.inventory.closure.snapshots if s.owner == namespace_owner
    )
    inventory = _consumers._inventory(snapshot)
    row, related = _consumers._snapshot_records(snapshot, inventory.evidence)
    support = _PlanOwner(
        owner.plan,
        snapshot,
        row,
        related,
        {
            o.spec.role: o
            for o in owner.plan.inventory.observed
            if o.spec.owner == namespace_owner
        },
        inventory.carriers,
    )
    namespace = inventory.evidence.namespace(_text(row["ownership_json"]))
    support.present(support.one("namespace_marker"), namespace.marker_fingerprint)


def _pack_carrier_support(owner: _PlanOwner, record: CarrierRecord) -> None:
    owner.complete_namespace(record.origin_path)
    namespace_owner = OwnerRef(
        "namespace", (record.origin_path.rsplit("/", 1)[0] or "/",)
    )
    snapshot = next(
        s for s in owner.plan.inventory.closure.snapshots if s.owner == namespace_owner
    )
    projection = _consumers._inventory(snapshot)
    root, related = _consumers._snapshot_records(snapshot, projection.evidence)
    support = _PlanOwner(
        owner.plan,
        snapshot,
        root,
        related,
        {
            o.spec.role: o
            for o in owner.plan.inventory.observed
            if o.spec.owner == namespace_owner
        },
        projection.carriers,
    )
    namespace = projection.evidence.namespace(_text(root["ownership_json"]))
    support.present(support.one("namespace_marker"), namespace.marker_fingerprint)
    carrier_roles = [
        r for r, o in owner.observed.items() if o.spec.path == record.carrier_path
    ]
    if len(carrier_roles) != 1:
        raise InvalidIdentity("ownership_phase")
    if (
        record.phase == "discarded"
        and owner.observed[carrier_roles[0]].outcome == "absent"
    ):
        if any(
            o.outcome not in {"absent", _UNREACHABLE}
            for o in owner.observed.values()
            if _within(o.spec.path, record.carrier_path)
        ):
            raise InvalidIdentity("ownership_phase")
        return
    for path, fingerprint in (
        (record.carrier_path, None),
        (record.carrier_path + "/owner.json", record.marker_fingerprint),
    ):
        matches = [r for r, o in owner.observed.items() if o.spec.path == path]
        if len(matches) != 1:
            raise InvalidIdentity("ownership_phase")
        owner.present(matches[0], fingerprint)


def _pack_claim_plan(
    owner: _PlanOwner, decisions: list[RecoveryDecision], subjects: set[RoleKey]
) -> None:
    from import_pack_cleanup import _pack_owner_marker_payload
    from download_identity import download_identity_key

    if (
        len(decisions) != 1
        or decisions[0].action not in {"restore-original", "cleanup-committed"}
        or owner.plan.version != 2
    ):
        raise InvalidIdentity("ownership_phase")
    if owner.snapshot.owner.domain == "pack_tombstone":
        claims = [
            (
                "tombstone",
                _consumers.PackDirectoryClaim.from_json(
                    _text(owner.root["carrier_json"])
                ),
            )
        ]
    else:
        proof = _consumers.PackOwnership.from_json(
            _text(owner.root["directory_ownership_json"])
        )
        if owner.root["purpose"] != "cleanup" or proof.phase not in {
            "cleaning",
            "retained",
        }:
            raise InvalidIdentity("ownership_phase")
        claims = [(f"pack_claim_{i}", c) for i, c in enumerate(proof.claims)]
    if decisions[0].action == "cleanup-committed":
        covered: set[RoleKey] = set()
        for purpose, claim in claims:
            local = {
                r
                for r in subjects
                if _within(
                    owner.observed[r].spec.path,
                    claim.carrier.carrier_path + "/artifact",
                )
            }
            if local:
                _pack_claim_cleanup(owner, local, purpose, claim)
                if covered.intersection(local):
                    raise InvalidIdentity("ownership_phase")
                covered.update(local)
        if covered != subjects:
            raise InvalidIdentity("ownership_phase")
        return
    selected = [
        (purpose, claim)
        for purpose, claim in claims
        if RoleKey(None, purpose + "_captured", "directory", None) in subjects
    ]
    if len(selected) != 1:
        raise InvalidIdentity("ownership_phase")
    purpose, claim = selected[0]
    if (
        claim.carrier.phase != "claimed"
        or claim.carrier.restore_receipt is not None
        or claim.artifact_owner_token is None
        or not claim.inventory.files
        or decisions[0].destination != claim.source_directory.path
    ):
        raise InvalidIdentity("ownership_phase")
    captured = claim.carrier.carrier_path + "/artifact"
    expected = {r for r, o in owner.observed.items() if _within(o.spec.path, captured)}
    if subjects != expected:
        raise InvalidIdentity("ownership_phase")
    _pack_terminal_context(
        owner, download_identity_key(_consumers._identity(owner.root))
    )
    _pack_carrier_support(owner, claim.carrier)
    root = owner.one(purpose + "_captured")
    owner.present(root)
    current = _object_record(
        _constraints(owner.observed[root].constraints_json, owner.observed[root].spec)[
            "current"
        ]
    )
    if (current["dev"], current["inode"]) != (
        claim.source_directory.dev,
        claim.source_directory.inode,
    ):
        raise InvalidIdentity("ownership_phase")
    for relative in claim.inventory.directories:
        owner.present(
            RoleKey(None, purpose + "_captured_directory", "directory", relative)
        )
    for relative, fingerprint in claim.inventory.files.items():
        owner.present(
            RoleKey(None, purpose + "_captured_file", "file", relative), fingerprint
        )
    marker = claim.source_directory.marker_fingerprint
    payload = _pack_owner_marker_payload(
        _consumers._identity(owner.root), claim.artifact_owner_token
    )
    if (
        marker is None
        or marker.size != len(payload)
        or marker.sha256 != hashlib.sha256(payload).hexdigest()
    ):
        raise InvalidIdentity("ownership_phase")
    owner.present(owner.one(purpose + "_captured_marker"), marker)
    owner.absent(owner.one(purpose + "_source"))
    for observation in owner.observed.values():
        if _within(
            observation.spec.path, claim.source_directory.path
        ) and observation.outcome not in {"absent", _UNREACHABLE}:
            raise InvalidIdentity("ownership_phase")


def _pack_committed_missing(
    owner: _PlanOwner, claim: _consumers.PackDirectoryClaim
) -> frozenset[str]:
    publications = {
        p["id"]
        for p in _raw_records(owner, "import_publications")
        if p["queue_id"] == owner.root["queue_id"]
        and p["state"] in {"db_committed", "cleaning", "finalized", "deleted"}
    }
    allowed: set[str] = set()
    for file in _raw_records(owner, "import_publication_files"):
        if file["publication_id"] not in publications or file["cleanup_state"] not in {
            "deleted",
            "missing",
        }:
            continue
        source = _path(file["src_path"])
        base = claim.source_directory.path
        if not _within(source, base) or source == base:
            continue
        relative = source[len(base.rstrip("/")) + 1 :]
        fingerprint = claim.inventory.files.get(relative)
        if fingerprint is not None and tuple(
            file["source_" + k] for k in ("dev", "inode", "size", "mtime_ns", "sha256")
        ) == (
            fingerprint.dev,
            fingerprint.inode,
            fingerprint.size,
            fingerprint.mtime_ns,
            fingerprint.sha256,
        ):
            allowed.add(relative)
    return frozenset(allowed)


def _pack_claim_cleanup(
    owner: _PlanOwner,
    subjects: set[RoleKey],
    purpose: str,
    claim: _consumers.PackDirectoryClaim,
) -> None:
    from download_identity import download_identity_key
    from import_pack_cleanup import _pack_owner_marker_payload

    phase = claim.carrier.phase
    if (
        phase not in {"claimed", "discarding", "discarded"}
        or claim.carrier.restore_receipt is not None
    ):
        raise InvalidIdentity("ownership_phase")
    _pack_terminal_context(
        owner, download_identity_key(_consumers._identity(owner.root))
    )
    _pack_carrier_support(owner, claim.carrier)
    owner.absent(owner.one(purpose + "_source"))
    captured = claim.carrier.carrier_path + "/artifact"
    candidates = {
        r for r, o in owner.observed.items() if _within(o.spec.path, captured)
    }
    if not subjects <= candidates:
        raise InvalidIdentity("ownership_phase")
    allowed_missing = _pack_committed_missing(owner, claim)
    root = owner.one(purpose + "_captured")
    root_observation = owner.observed[root]
    missing_root = root_observation.outcome in {"absent", _UNREACHABLE}
    if missing_root and phase not in {"discarding", "discarded"}:
        raise InvalidIdentity("ownership_phase")
    if not missing_root:
        owner.present(root)
        current = _object_record(
            _constraints(root_observation.constraints_json, root_observation.spec)[
                "current"
            ]
        )
        if phase == "discarded" or (current["dev"], current["inode"]) != (
            claim.source_directory.dev,
            claim.source_directory.inode,
        ):
            raise InvalidIdentity("ownership_phase")
    for relative in claim.inventory.directories:
        role = RoleKey(None, purpose + "_captured_directory", "directory", relative)
        if owner.observed[role].outcome in {"absent", _UNREACHABLE}:
            if phase not in {"discarding", "discarded"}:
                raise InvalidIdentity("ownership_phase")
        else:
            owner.present(role)
    for relative, fingerprint in claim.inventory.files.items():
        role = RoleKey(None, purpose + "_captured_file", "file", relative)
        if owner.observed[role].outcome in {"absent", _UNREACHABLE}:
            if (
                phase not in {"discarding", "discarded"}
                and relative not in allowed_missing
            ):
                raise InvalidIdentity("ownership_phase")
        else:
            owner.present(role, fingerprint)
    marker = claim.source_directory.marker_fingerprint
    if marker is not None:
        role = owner.one(purpose + "_captured_marker")
        if owner.observed[role].outcome in {"absent", _UNREACHABLE}:
            if phase not in {"discarding", "discarded"}:
                raise InvalidIdentity("ownership_phase")
        else:
            if claim.artifact_owner_token is None:
                raise InvalidIdentity("ownership_phase")
            payload = _pack_owner_marker_payload(
                _consumers._identity(owner.root), claim.artifact_owner_token
            )
            if (
                marker.size != len(payload)
                or marker.sha256 != hashlib.sha256(payload).hexdigest()
            ):
                raise InvalidIdentity("ownership_phase")
            owner.present(role, marker)
    _cleanup_subjects(owner, subjects, candidates)
    for role in subjects:
        observation = owner.observed[role]
        if (
            observation.spec.role.kind == "marker"
            and observation.spec.parent_role not in subjects
        ):
            raise InvalidIdentity("ownership_phase")
        if any(
            r not in subjects
            and o.outcome not in {"absent", _UNREACHABLE}
            and o.spec.path == observation.spec.path
            for r, o in owner.observed.items()
        ):
            raise InvalidIdentity("ownership_phase")


def _pack_retained_source_veto(owner: _PlanOwner, queue: dict[str, object]) -> bool:
    from private_pack_claim import PackDirectoryClaim, PackOwnership

    if queue["status"] != "failed":
        return False
    children = [
        r
        for r in _raw_records(owner, "import_queue_files")
        if r["queue_id"] == queue["id"]
    ]
    if not children or any(r["status"] != "skipped" for r in children):
        return False
    sources = {r["src_path"] for r in children if r["src_path"]}
    for row in _raw_records(owner, "import_pack_cleanup_reservations"):
        if (
            row["download_identity_key"] != owner.root["download_identity_key"]
            or row["queue_id"] != queue["id"]
        ):
            continue
        if row["directory_ownership_json"] is None:
            continue
        proof = PackOwnership.from_json(_text(row["directory_ownership_json"]))
        if proof.inventory is None and not proof.claims:
            raise InvalidIdentity("ownership_context")
        if proof.inventory is not None:
            for directory in (proof.canonical_directory, proof.private_directory):
                if directory is not None and directory.marker_fingerprint is not None:
                    if any(
                        directory.path + "/" + relative in sources
                        for relative in proof.inventory.files
                    ):
                        return True
        for claim in proof.claims:
            if claim.artifact_owner_token == proof.artifact_owner_token and any(
                claim.source_directory.path + "/" + relative in sources
                for relative in claim.inventory.files
            ):
                return True
    for row in _raw_records(owner, "import_pack_cleanup_tombstones"):
        if (
            row["download_identity_key"] != owner.root["download_identity_key"]
            or row["queue_id"] != queue["id"]
            or row["pack_path"] != owner.root["pack_path"]
            or row["carrier_json"] is None
        ):
            continue
        claim = PackDirectoryClaim.from_json(_text(row["carrier_json"]))
        if any(
            base + "/" + relative in sources
            for base in {claim.source_directory.path, _path(row["pack_path"])}
            for relative in claim.inventory.files
        ):
            return True
    return False


def _pack_terminal_context(owner: _PlanOwner, identity_key: str) -> None:
    from download_identity import download_identities_match, download_identity_key
    from import_pack_cleanup import _ACTIVE_PUBLICATION_STATES, _TERMINAL_QUEUE_STATUSES

    queue_id = _integer(owner.root["queue_id"], 1)
    queues = _raw_records(owner, "import_queue")
    current = [q for q in queues if q["id"] == queue_id]
    if len(current) > 1:
        raise InvalidIdentity("ownership_context")
    publications = _raw_records(owner, "import_publications")
    if (
        not current
        and not any(
            p["queue_id"] == queue_id and p["state"] in {"finalized", "deleted"}
            for p in publications
        )
    ) or any(
        p["queue_id"] == queue_id and p["state"] in _ACTIVE_PUBLICATION_STATES
        for p in publications
    ):
        raise InvalidIdentity("ownership_context")
    if current:
        queue = current[0]
        if (
            queue["status"] not in _TERMINAL_QUEUE_STATUSES
            or queue.get("lease_owner", "missing") is not None
            or download_identity_key(_consumers._identity(queue, "download_protocol"))
            != identity_key
            or _pack_retained_source_veto(owner, queue)
        ):
            raise InvalidIdentity("ownership_context")
    identity = _consumers._identity(owner.root)
    for sibling in queues:
        if sibling["id"] == queue_id or sibling["download_id"] is None:
            continue
        active = (
            sibling["status"] in {"pending", "partial", "importing"}
            or sibling.get("lease_owner", "missing") is not None
            or any(
                p["queue_id"] == sibling["id"]
                and p["state"] in _ACTIVE_PUBLICATION_STATES
                for p in publications
            )
        )
        # seen is not captured. Keep an unresolved protocol unresolved; the
        # producer's conservative identity matcher can veto, not resolve it.
        if active and download_identities_match(
            identity, _consumers._identity(sibling, "download_protocol")
        ):
            raise InvalidIdentity("ownership_context")


def _pack_cleanup_plan(
    owner: _PlanOwner, decisions: list[RecoveryDecision], subjects: set[RoleKey]
) -> None:
    from import_pack_cleanup import _pack_owner_marker_payload
    from private_pack_claim import PackOwnership

    ownership = PackOwnership.from_json(_text(owner.root["directory_ownership_json"]))
    if (
        len(decisions) != 1
        or decisions[0].action != "cleanup-committed"
        or ownership.version != 3
        or owner.root["purpose"] != "cleanup"
        or owner.root["publication_id"] is not None
        or ownership.phase not in {"attached", "cleaning", "retained"}
        or ownership.private_directory is not None
        or ownership.claims
    ):
        raise InvalidIdentity("ownership_phase")
    _pack_terminal_context(owner, ownership.identity_key)
    proof, entries = ownership.canonical_directory, ownership.inventory
    if proof is None or entries is None or proof.marker_fingerprint is None:
        raise InvalidIdentity("ownership_phase")
    files = [
        f
        for f in _raw_records(owner, "import_queue_files")
        if f["queue_id"] == owner.root["queue_id"]
    ]
    sources = {proof.path + "/" + relative for relative in entries.files}
    if not files or any(f["src_path"] not in sources for f in files):
        raise InvalidIdentity("ownership_context")
    placement = ownership.placement_carrier
    if (
        owner.plan.version == 2
        and placement is not None
        and placement.phase == "discarded"
    ):
        if proof.path != placement.carrier_path + "/artifact":
            raise InvalidIdentity("ownership_phase")
        _pack_carrier_support(owner, placement)
        candidates = {
            r for r, o in owner.observed.items() if _within(o.spec.path, proof.path)
        }
        if any(
            o.outcome not in {"absent", _UNREACHABLE}
            for r, o in owner.observed.items()
            if r in candidates
        ):
            raise InvalidIdentity("ownership_phase")
        for purpose in ("pack_logical", "tombstone_path"):
            for role in owner.roles(purpose):
                if role not in candidates:
                    owner.absent(role)
        _cleanup_subjects(owner, subjects, candidates)
        return
    root = owner.one("pack_canonical")
    owner.present(root)
    candidates = {root, owner.one("pack_canonical_marker")}
    for relative in entries.directories:
        role = RoleKey(None, "pack_canonical_directory", "directory", relative)
        owner.present(role)
        candidates.add(role)
    for relative, fingerprint in entries.files.items():
        role = RoleKey(None, "pack_canonical_file", "file", relative)
        owner.present(role, fingerprint)
        candidates.add(role)
    marker = _pack_owner_marker_payload(
        _consumers._identity(owner.root), ownership.artifact_owner_token
    )
    if (
        proof.marker_fingerprint.size != len(marker)
        or proof.marker_fingerprint.sha256 != hashlib.sha256(marker).hexdigest()
    ):
        raise InvalidIdentity("ownership_phase")
    owner.present(owner.one("pack_canonical_marker"), proof.marker_fingerprint)
    placement = ownership.placement_carrier
    if placement is not None:
        if (
            placement.phase != "claimed"
            or proof.path != placement.carrier_path + "/artifact"
        ):
            raise InvalidIdentity("ownership_phase")
        owner.complete_namespace(placement.origin_path)
        namespace_owner = OwnerRef(
            "namespace", (placement.origin_path.rsplit("/", 1)[0] or "/",)
        )
        snapshot = next(
            s
            for s in owner.plan.inventory.closure.snapshots
            if s.owner == namespace_owner
        )
        namespace_inventory = _consumers._inventory(snapshot)
        namespace_row, related = _consumers._snapshot_records(
            snapshot, namespace_inventory.evidence
        )
        support = _PlanOwner(
            owner.plan,
            snapshot,
            namespace_row,
            related,
            {
                o.spec.role: o
                for o in owner.plan.inventory.observed
                if o.spec.owner == namespace_owner
            },
            namespace_inventory.carriers,
        )
        namespace_proof = namespace_inventory.evidence.namespace(
            _text(namespace_row["ownership_json"])
        )
        support.present(
            support.one("namespace_marker"), namespace_proof.marker_fingerprint
        )
        _ = _owner_carrier(owner, "pack_cleanup")
        artifact = owner.one("pack_placement_artifact")
        owner.present(artifact)
        candidates.add(artifact)
    if proof.path != owner.root["pack_path"]:
        owner.absent(owner.one("pack_logical"))
    if owner.root["tombstone_path"] is not None:
        owner.absent(owner.one("tombstone_path"))
    _cleanup_subjects(owner, subjects, candidates)
    for role in subjects:
        selected = owner.observed[role]
        if role.kind == "marker" and selected.spec.parent_role not in subjects:
            raise InvalidIdentity("ownership_phase")
        if any(
            r not in subjects
            and o.outcome != "absent"
            and o.spec.path == selected.spec.path
            for r, o in owner.observed.items()
        ):
            raise InvalidIdentity("ownership_phase")


def _pack_queue_plan(
    owner: _PlanOwner, decisions: list[RecoveryDecision], subjects: set[RoleKey]
) -> None:
    from download_identity import download_identity_key
    from import_pack_cleanup import _pack_owner_marker_payload
    from private_pack_claim import PackOwnership

    ownership = PackOwnership.from_json(_text(owner.root["directory_ownership_json"]))
    if (
        len(decisions) != 1
        or decisions[0].action != "resume-commit"
        or ownership.phase != "queued"
        or ownership.version != 3
        or owner.root["purpose"] != "cleanup"
        or owner.root["publication_id"] is not None
    ):
        raise InvalidIdentity("ownership_phase")
    queue_id = _integer(owner.root["queue_id"], 1)
    queues = [q for q in _raw_records(owner, "import_queue") if q["id"] == queue_id]
    files = sorted(
        (
            f
            for f in _raw_records(owner, "import_queue_files")
            if f["queue_id"] == queue_id
        ),
        key=lambda f: _integer(f["id"], 1),
    )
    if len(queues) != 1 or not files:
        raise InvalidIdentity("ownership_context")
    queue = queues[0]
    if (
        queue["status"] != "pending"
        or any(
            queue.get(k, "missing") is not None
            for k in ("lease_owner", "lease_expires_at", "failed_at")
        )
        or download_identity_key(_consumers._identity(queue, "download_protocol"))
        != ownership.identity_key
        or any(
            p["queue_id"] == queue_id
            for p in _raw_records(owner, "import_publications")
        )
        or _plan_hash({"queue": queue, "files": files})
        != ownership.queue_receipt_sha256
    ):
        raise InvalidIdentity("ownership_context")
    proof, inventory = ownership.canonical_directory, ownership.inventory
    if (
        proof is None
        or inventory is None
        or proof.marker_fingerprint is None
        or ownership.private_directory is not None
        or ownership.claims
    ):
        raise InvalidIdentity("ownership_phase")
    canonical = owner.one("pack_canonical")
    owner.present(canonical)
    expected = {canonical}
    for relative in inventory.directories:
        role = RoleKey(None, "pack_canonical_directory", "directory", relative)
        owner.present(role)
        expected.add(role)
    for relative, fingerprint in inventory.files.items():
        role = RoleKey(None, "pack_canonical_file", "file", relative)
        owner.present(role, fingerprint)
        expected.add(role)
    if subjects != expected:
        raise InvalidIdentity("ownership_phase")
    marker = _pack_owner_marker_payload(
        _consumers._identity(owner.root), ownership.artifact_owner_token
    )
    if (
        proof.marker_fingerprint.size != len(marker)
        or proof.marker_fingerprint.sha256 != hashlib.sha256(marker).hexdigest()
    ):
        raise InvalidIdentity("ownership_phase")
    owner.present(owner.one("pack_canonical_marker"), proof.marker_fingerprint)
    placement = ownership.placement_carrier
    if placement is not None:
        if (
            placement.phase != "claimed"
            or proof.path != placement.carrier_path + "/artifact"
        ):
            # The other producer checkpoints dispose placement/private claims;
            # retaining them here cannot silently authorize that cleanup.
            raise InvalidIdentity("ownership_phase")
        owner.complete_namespace(placement.origin_path)
        namespace_owner = OwnerRef(
            "namespace", (placement.origin_path.rsplit("/", 1)[0] or "/",)
        )
        snapshot = next(
            s
            for s in owner.plan.inventory.closure.snapshots
            if s.owner == namespace_owner
        )
        namespace_inventory = _consumers._inventory(snapshot)
        namespace_row, related = _consumers._snapshot_records(
            snapshot, namespace_inventory.evidence
        )
        support = _PlanOwner(
            owner.plan,
            snapshot,
            namespace_row,
            related,
            {
                o.spec.role: o
                for o in owner.plan.inventory.observed
                if o.spec.owner == namespace_owner
            },
            namespace_inventory.carriers,
        )
        namespace_proof = namespace_inventory.evidence.namespace(
            _text(namespace_row["ownership_json"])
        )
        support.present(
            support.one("namespace_marker"), namespace_proof.marker_fingerprint
        )
        _ = _owner_carrier(owner, "pack_cleanup")
        owner.present(owner.one("pack_placement_artifact"))
    if proof.path != owner.root["pack_path"]:
        owner.absent(owner.one("pack_logical"))
    if owner.root["tombstone_path"] is not None:
        intent = owner.one("tombstone_path")
        if owner.root["tombstone_path"] == proof.path:
            owner.present(intent)
        else:
            owner.absent(intent)
    sources = {proof.path + "/" + relative for relative in inventory.files}
    if any(f["src_path"] not in sources for f in files):
        raise InvalidIdentity("ownership_context")


def _cancel_plan(owner: _PlanOwner, subjects: set[RoleKey]) -> None:
    domain, row = owner.snapshot.owner.domain, owner.root
    if owner.carriers:
        raise InvalidIdentity("ownership_phase")
    if domain == "rescan":
        fingerprints = _object_record(_load(row["fingerprints_json"]))
        source = owner.one("source")
        if (
            row["state"] != "prepared"
            or row["publication_receipt_json"] is not None
            or any(
                fingerprints[k] is not None
                for k in ("stage", "publication", "captured_source")
            )
            or subjects != {source}
        ):
            raise InvalidIdentity("ownership_phase")
        owner.complete_namespace(_path(row["source_path"]))
        owner.present(source, FullFileFingerprint.from_value(fingerprints["source"]))
        if row["source_path"] != row["destination_path"]:
            owner.absent(owner.one("destination"))
    elif domain == "deletion":
        target = owner.one("target")
        if (
            row["state"] != "active"
            or row["target_present"] != 1
            or subjects != {target}
        ):
            raise InvalidIdentity("ownership_phase")
        owner.complete_namespace(_path(row["target_path"]))
        fingerprint = _consumers._columns_fingerprint(row, "target_")
        owner.present(target, fingerprint)
        owner.absent(owner.one("claim"))
    elif domain == "publication":
        queue = _object_record(_load(row["queue_snapshot_json"]))
        protocol = _object_record(queue.get("_file_publication"))
        if (
            row["state"] != "staging"
            or queue.get("_publication_staging") is not None
            or protocol["decision"] != "pending"
            or protocol["artifacts"] != {}
            or protocol.get("allocating") is not None
            or protocol["admission"] is None
        ):
            raise InvalidIdentity("ownership_phase")
        owner.absent(owner.one("staging"))
        owner.complete_namespace(_path(row["staging_dir"]))
        files = [
            _object_record(r["row"])
            for r in owner.records
            if r["table"] == "import_publication_files"
        ]
        expected: set[RoleKey] = set()
        for file in files:
            fid = _integer(file["file_id"], 1)
            source = owner.one("source", fid)
            fingerprint = _consumers._columns_fingerprint(file, "source_")
            if (
                fingerprint is None
                or file["stage_state"] not in {"pending", "skipped"}
                or file["publish_state"] not in {"pending", "not_applicable"}
                or _consumers._columns_fingerprint(file, "staged_") is not None
                or file["source_claim_carrier_json"] is not None
                or file["final_claim_carrier_json"] is not None
            ):
                raise InvalidIdentity("ownership_phase")
            owner.present(source, fingerprint)
            owner.complete_namespace(_path(file["src_path"]))
            if file["stage_path"] is not None:
                owner.absent(owner.one("stage", fid))
            if file["plan_status"] == "ready":
                owner.absent(owner.one("final", fid))
            expected.add(source)
        if not files or subjects != expected:
            raise InvalidIdentity("ownership_phase")
        _publication_context(owner, _object_record(protocol["admission"]))
    else:
        raise InvalidIdentity("ownership_phase")


def _owner_carrier(
    owner: _PlanOwner, purpose: str, file_id: int | None = None
) -> CarrierRecord:
    records = [
        c
        for c in owner.carriers
        if c.binding.purpose == purpose
        and (file_id is None or c.binding.file_id == file_id)
    ]
    if len(records) != 1:
        raise InvalidIdentity("ownership_phase")
    record = records[0]
    roles = {o.spec.path: r for r, o in owner.observed.items()}
    for path, fingerprint in (
        (record.carrier_path, None),
        (record.carrier_path + "/owner.json", record.marker_fingerprint),
    ):
        if path not in roles:
            raise InvalidIdentity("ownership_incomplete")
        owner.present(roles[path], fingerprint)
    return record


def _raw_records(owner: _PlanOwner, table: str) -> list[dict[str, object]]:
    return [
        cast(dict[str, object], dict(r.values))
        for r in owner.plan.inventory.closure.raw_rows
        if r.table == table
    ]


def _rescan_context(owner: _PlanOwner, committed: bool) -> None:
    from rescan import _VOLUME_GUARD

    row = owner.root
    expected = _object_record(_load(row["expected_volume_json"]))
    volumes = [
        v
        for v in _raw_records(owner, "volumes")
        if v["id"] == row["volume_id"] and v["series_id"] == row["series_id"]
    ]
    if len(volumes) != 1:
        raise InvalidIdentity("ownership_context")
    current = volumes[0]
    for field in _VOLUME_GUARD:
        desired = expected[field]
        if committed and row["source_path"] != row["destination_path"]:
            if field == "import_path":
                desired = row["destination_path"]
            elif field == "size_bytes":
                desired = _object_record(
                    _object_record(_load(row["fingerprints_json"]))["stage"]
                )["size"]
            elif field == "quality":
                desired = "cbz"
        if field not in current or current[field] != desired:
            raise InvalidIdentity("ownership_context")
    context = _object_record(_load(row["expected_context_json"]))
    expected_series = _object_record(context["series"])
    series = [s for s in _raw_records(owner, "series") if s["id"] == row["series_id"]]
    if len(series) != 1 or series[0].get("deleted_at") is not None:
        raise InvalidIdentity("ownership_context")
    current_series = series[0]
    roots = [
        r
        for r in _raw_records(owner, "root_folders")
        if r["id"] == current_series["root_folder_id"]
    ]
    for key, value in expected_series.items():
        actual = (
            roots[0]["path"]
            if key == "rescan_root_path" and len(roots) == 1
            else current_series.get(key)
        )
        if (key != "rescan_root_path" and key not in current_series) or value != actual:
            raise InvalidIdentity("ownership_context")
    tags = sorted(
        _text(t["tag"])
        for t in _raw_records(owner, "series_tags")
        if t["series_id"] == row["series_id"]
    )
    if tags != context["tags"]:
        raise InvalidIdentity("ownership_context")


def _rescan_plan(
    owner: _PlanOwner, decisions: list[RecoveryDecision], subjects: set[RoleKey]
) -> None:
    row, action = owner.root, decisions[0].action
    fp = _object_record(_load(row["fingerprints_json"]))
    destination = owner.one("destination")
    if action == "restore-original":
        original = owner.one("source_artifact")
        carrier = _owner_carrier(owner, "source")
        if (
            row["state"] not in {"prepared", "published", "rollback"}
            or carrier.phase not in {"claimed", "restoring"}
            or carrier.artifact_fingerprint
            != FullFileFingerprint.from_value(fp["source"])
            or len(decisions) != 1
            or decisions[0].destination != row["source_path"]
        ):
            raise InvalidIdentity("ownership_phase")
        owner.present(original, FullFileFingerprint.from_value(fp["source"]))
        expected_subjects = {original}
        if destination in subjects:
            if row["publication_receipt_json"] is None or row["state"] not in {
                "published",
                "rollback",
            }:
                raise InvalidIdentity("ownership_phase")
            receipt = LinkReceipt.from_value(_load(row["publication_receipt_json"]))
            owner.present(destination, receipt.fingerprint)
            expected_subjects.add(destination)
            if row["destination_path"] == row["source_path"]:
                alias = owner.one("source")
                owner.present(alias, receipt.fingerprint)
                expected_subjects.add(alias)
            else:
                owner.absent(owner.one("source"))
            for purpose in ("stage", "publication"):
                _ = _owner_carrier(owner, purpose)
                owner.present(
                    owner.one(purpose + "_artifact"),
                    FullFileFingerprint.from_value(fp[purpose]),
                )
        else:
            owner.absent(owner.one("source"))
            if row["destination_path"] != row["source_path"]:
                owner.absent(destination)
        if subjects != expected_subjects:
            raise InvalidIdentity("ownership_phase")
    elif action == "keep-current":
        if (
            row["state"] not in {"prepared", "published", "rollback"}
            or row["publication_receipt_json"] is not None
            or subjects != {destination}
            or len(decisions) != 1
            or decisions[0].destination != row["destination_path"]
            or fp["publication"] is None
        ):
            raise InvalidIdentity("ownership_phase")
        owner.present(destination)
        for purpose in ("source", "stage", "publication"):
            record = _owner_carrier(owner, purpose)
            key = owner.one(purpose + "_artifact")
            if record.phase not in {"allocated", "claimed", "claiming", "restoring"}:
                raise InvalidIdentity("ownership_phase")
            owner.present(key, FullFileFingerprint.from_value(fp[purpose]))
    elif action in {"resume-commit", "cleanup-committed"}:
        committed = action == "cleanup-committed"
        if (
            row["state"]
            not in ({"db_committed", "completed"} if committed else {"published"})
            or row["publication_receipt_json"] is None
        ):
            raise InvalidIdentity("ownership_phase")
        receipt = LinkReceipt.from_value(_load(row["publication_receipt_json"]))
        owner.present(destination, receipt.fingerprint)
        _rescan_context(owner, committed)
        if not committed and subjects != {destination}:
            raise InvalidIdentity("ownership_phase")
        private: set[RoleKey] = set()
        for purpose in ("source", "stage", "publication", "rollback"):
            if not any(c.binding.purpose == purpose for c in owner.carriers):
                if purpose in {"source", "stage"}:
                    raise InvalidIdentity("ownership_phase")
                continue
            record = _owner_carrier(owner, purpose)
            artifact = owner.one(purpose + "_artifact")
            for role in owner.observed:
                if owner.observed[role].spec.path in {
                    record.carrier_path,
                    record.carrier_path + "/owner.json",
                    record.carrier_path + "/artifact",
                }:
                    private.add(role)
            if owner.observed[artifact].outcome == "absent":
                if not committed or record.phase not in {"discarding", "discarded"}:
                    raise InvalidIdentity("ownership_phase")
            else:
                expected = fp[
                    "source"
                    if purpose == "source"
                    else "publication"
                    if purpose == "rollback"
                    else purpose
                ]
                owner.present(artifact, FullFileFingerprint.from_value(expected))
        if committed:
            _cleanup_subjects(owner, subjects, private)
    else:
        raise InvalidIdentity("ownership_phase")


def _deletion_context(owner: _PlanOwner) -> None:
    row = owner.root
    volumes = [
        v
        for v in _raw_records(owner, "volumes")
        if v["id"] == row["volume_id"] and v["series_id"] == row["series_id"]
    ]
    if (
        len(volumes) != 1
        or volumes[0]["status"] != "wanted"
        or volumes[0]["import_path"] not in {None, ""}
    ):
        raise InvalidIdentity("ownership_context")


def _deletion_reset_context(owner: _PlanOwner) -> None:
    from rescan import _CHAPTER_GUARD, _VOLUME_GUARD

    _deletion_context(owner)
    row = owner.root
    volumes = _raw_records(owner, "volumes")
    volume = next(v for v in volumes if v["id"] == row["volume_id"])
    if not set(_VOLUME_GUARD) <= volume.keys():
        raise InvalidIdentity("ownership_context")
    if any(
        volume[field] is not None
        for field in (
            "grabbed_at",
            "imported_at",
            "source_url",
            "download_id",
            "download_client_id",
            "torrent_name",
            "indexer",
            "protocol",
            "client",
            "release_group",
            "size_bytes",
            "quality",
        )
    ):
        raise InvalidIdentity("ownership_context")
    for chapter in _raw_records(owner, "chapters"):
        if (
            chapter["volume_id"] != row["volume_id"]
            or chapter["series_id"] != row["series_id"]
        ):
            continue
        if (
            not set(_CHAPTER_GUARD) <= chapter.keys()
            or type(chapter["monitored"]) is not int
            or chapter["monitored"] not in {0, 1}
        ):
            raise InvalidIdentity("ownership_context")
        if not chapter["monitored"]:
            continue
        path = chapter["import_path"]
        if chapter["status"] == "downloaded" and path not in {None, ""}:
            independent = _path(path)
            if (
                not _within(independent, owner.plan.inventory.parent)
                or independent == row["target_path"]
                or any(v["import_path"] == independent for v in volumes)
                or any(
                    o.spec.path == independent for o in owner.plan.inventory.observed
                )
            ):
                raise InvalidIdentity("ownership_context")
            continue
        if (
            chapter["status"] != "wanted"
            or path not in {None, ""}
            or any(
                chapter[field] is not None
                for field in (
                    "grabbed_at",
                    "torrent_name",
                    "torrent_url",
                    "indexer",
                    "protocol",
                    "client",
                    "download_id",
                    "download_client_id",
                    "release_group",
                )
            )
        ):
            raise InvalidIdentity("ownership_context")


def _deletion_plan(
    owner: _PlanOwner, decisions: list[RecoveryDecision], subjects: set[RoleKey]
) -> None:
    row, action = owner.root, decisions[0].action
    original = owner.one("delete_artifact")
    record = _owner_carrier(owner, "delete")
    expected = _consumers._columns_fingerprint(row, "target_")
    if action == "keep-current":
        target = owner.one("target")
        if (
            row["state"] != "active"
            or expected is None
            or record.phase != "restoring"
            or record.restore_receipt is not None
            or record.artifact_fingerprint != expected
            or subjects != {target}
            or len(decisions) != 1
            or decisions[0].destination != row["target_path"]
        ):
            raise InvalidIdentity("ownership_phase")
        owner.present(target, expected)
        owner.present(original, expected)
        owner.absent(owner.one("claim"))
        _deletion_reset_context(owner)
        return
    if action == "cleanup-committed":
        if (
            row["state"] != "completed"
            or expected is None
            or record.artifact_fingerprint != expected
            or record.restore_receipt is not None
            or record.phase not in {"claimed", "discarding", "discarded"}
        ):
            raise InvalidIdentity("ownership_phase")
        owner.absent(owner.one("target"))
        owner.absent(owner.one("claim"))
        if owner.observed[original].outcome == "absent":
            if record.phase not in {"discarding", "discarded"}:
                raise InvalidIdentity("ownership_phase")
        else:
            owner.present(original, expected)
        _deletion_reset_context(owner)
        candidates = {
            r
            for r, o in owner.observed.items()
            if _within(o.spec.path, record.carrier_path)
        }
        _cleanup_subjects(owner, subjects, candidates)
        return
    if action == "resume-commit":
        if (
            row["state"] != "active"
            or expected is None
            or record.artifact_fingerprint != expected
            or subjects != {original}
            or record.phase not in {"claimed", "discarding", "discarded"}
            or record.restore_receipt is not None
        ):
            raise InvalidIdentity("ownership_phase")
        owner.absent(owner.one("target"))
        owner.absent(owner.one("claim"))
        if owner.observed[original].outcome == "absent":
            if record.phase not in {"discarding", "discarded"}:
                raise InvalidIdentity("ownership_phase")
        else:
            owner.present(original, expected)
        _deletion_context(owner)
        return
    if action != "restore-original":
        raise InvalidIdentity("ownership_phase")
    if (
        row["state"] != "active"
        or expected is None
        or record.artifact_fingerprint != expected
        or record.phase not in {"claimed", "restoring"}
        or subjects != {original}
        or len(decisions) != 1
        or decisions[0].destination != row["target_path"]
    ):
        raise InvalidIdentity("ownership_phase")
    owner.present(original, expected)
    owner.absent(owner.one("target"))
    owner.absent(owner.one("claim"))
    _deletion_context(owner)
    # version1 restore-original binds U4 fresh verification/adoption before
    # fence release. No legacy completion/file_deleted or history reconstruction.


def _publication_context(owner: _PlanOwner, admission: dict[str, object]) -> None:
    from import_plan import (
        _PUBLICATION_MAPPING_FIELDS,
        _PUBLICATION_VOLUME_FIELDS,
        _PUBLICATION_CHAPTER_FIELDS,
    )

    for table, key, identity in (
        ("series", "series", owner.root["series_id"]),
        ("import_queue", "queue", owner.root["queue_id"]),
    ):
        expected = _object_record(admission[key])
        current = [r for r in _raw_records(owner, table) if r["id"] == identity]
        if len(current) != 1 or any(
            k not in current[0] or current[0][k] != v for k, v in expected.items()
        ):
            raise InvalidIdentity("ownership_context")
    for value in _object_record(admission["files"]).values():
        file = _object_record(value)
        mapping = _object_record(file["mapping"])
        current = [
            r
            for r in _raw_records(owner, "import_queue_files")
            if r["id"] == mapping.get("id")
        ]
        if len(current) != 1 or any(
            k not in mapping or current[0].get(k) != mapping[k]
            for k in _PUBLICATION_MAPPING_FIELDS
        ):
            raise InvalidIdentity("ownership_context")
        for table, fields in (
            ("volumes", _PUBLICATION_VOLUME_FIELDS),
            ("chapters", _PUBLICATION_CHAPTER_FIELDS),
        ):
            raw = {r["id"]: r for r in _raw_records(owner, table)}
            other = _object_record(file["other_path_claims"])
            for record in (*_list(file[table]), *_list(other[table])):
                expected = _object_record(record)
                row = raw.get(expected["id"])
                if row is None or any(
                    k not in row or row[k] != expected[k] for k in fields
                ):
                    raise InvalidIdentity("ownership_context")
    # seen/history-derived acquisition metadata is frozen review evidence only.
    # Task9 must rerun real publication_admission plus owner/CAS before execution.


def _cleanup_subjects(
    owner: _PlanOwner, subjects: set[RoleKey], candidates: set[RoleKey]
) -> None:
    if not subjects or not subjects <= candidates:
        raise InvalidIdentity("ownership_phase")
    for role in subjects:
        observation = owner.observed[role]
        if role.kind == "directory" and any(
            r not in subjects
            for r, o in owner.observed.items()
            if o.spec.path != observation.spec.path
            and _within(o.spec.path, observation.spec.path)
        ):
            raise InvalidIdentity("ownership_phase")
        if role.kind == "marker" and any(
            r not in subjects and o.outcome != "absent"
            for r, o in owner.observed.items()
            if o.spec.parent_role == observation.spec.parent_role and r != role
        ):
            raise InvalidIdentity("ownership_phase")


def _publication_committed_context(
    owner: _PlanOwner,
    admission: dict[str, object],
    files: list[dict[str, object]],
) -> None:
    from import_plan import _PUBLICATION_MAPPING_FIELDS

    for table, key, identity in (
        ("series", "series", owner.root["series_id"]),
        ("import_queue", "queue", owner.root["queue_id"]),
    ):
        expected = _object_record(admission[key])
        rows = [r for r in _raw_records(owner, table) if r["id"] == identity]
        if len(rows) != 1:
            raise InvalidIdentity("ownership_context")
        current = rows[0]
        changed: set[str] = (
            {"status", "lease_owner", "lease_expires_at"}
            if table == "import_queue"
            else set()
        )
        if any(
            k not in current or current[k] != v
            for k, v in expected.items()
            if k not in changed
        ):
            raise InvalidIdentity("ownership_context")
        if table == "import_queue" and (
            current["status"] != owner.root["result_queue_status"]
            or current["lease_owner"] is not None
            or current["lease_expires_at"] is not None
        ):
            raise InvalidIdentity("ownership_context")
    admitted = _object_record(admission["files"])
    ready = [f for f in files if f["plan_status"] == "ready"]
    if set(admitted) != {str(f["file_id"]) for f in ready}:
        raise InvalidIdentity("ownership_context")
    for file in ready:
        context = _object_record(admitted[str(file["file_id"])])
        mapping = _object_record(context["mapping"])
        current = [
            r
            for r in _raw_records(owner, "import_queue_files")
            if r["id"] == file["file_id"]
        ]
        if len(current) != 1 or any(
            current[0].get(k) != mapping.get(k) for k in _PUBLICATION_MAPPING_FIELDS
        ):
            raise InvalidIdentity("ownership_context")
        if (
            current[0]["status"] != "imported"
            or current[0]["dst_path"] != file["final_path"]
        ):
            raise InvalidIdentity("ownership_context")
        effective = _object_record(context["effective"])
        if effective["dst_path"] != file["final_path"]:
            raise InvalidIdentity("ownership_context")
        matches = [
            r
            for table in ("volumes", "chapters")
            for r in _raw_records(owner, table)
            if r["series_id"] == owner.root["series_id"]
            and r["import_path"] == file["final_path"]
        ]
        if not matches or any(r["status"] != "downloaded" for r in matches):
            raise InvalidIdentity("ownership_context")
        if (
            file["import_kind"] == "volume"
            and file["proposed_vol"] is not None
            and not any(r.get("volume_num") == file["proposed_vol"] for r in matches)
        ):
            raise InvalidIdentity("ownership_context")
        metadata = _object_record(admission["acquisition_metadata"])
        frozen_queue = _object_record(admission["queue"])
        protocol = _object_record(
            _object_record(_load(owner.root["queue_snapshot_json"]))[
                "_file_publication"
            ]
        )
        entry = _object_record(
            _object_record(protocol["artifacts"])[str(file["file_id"])]
        )
        for current_row in matches:
            if "volume_num" not in current_row:
                continue
            if (
                "quality" not in entry
                or current_row["quality"] != entry["quality"]
                or current_row["download_client_id"]
                != frozen_queue["download_client_id"]
                or not current_row["imported_at"]
                or any(
                    current_row[k] != metadata.get(k)
                    for k in (
                        "torrent_name",
                        "indexer",
                        "protocol",
                        "client",
                        "release_group",
                        "size_bytes",
                    )
                )
            ):
                raise InvalidIdentity("ownership_context")
        for table in ("volumes", "chapters"):
            for value in _list(_object_record(context["other_path_claims"])[table]):
                previous = _object_record(value)
                current_rows = [
                    r for r in _raw_records(owner, table) if r["id"] == previous["id"]
                ]
                if len(current_rows) != 1 or any(
                    current_rows[0].get(k) != v for k, v in previous.items()
                ):
                    raise InvalidIdentity("ownership_context")


def _publication_cleanup_plan(
    owner: _PlanOwner,
    subjects: set[RoleKey],
    protocol: dict[str, object],
    files: list[dict[str, object]],
) -> None:
    ready = [f for f in files if f["plan_status"] == "ready"]
    if (
        owner.root["state"] not in {"db_committed", "cleaning", "finalized", "deleted"}
        or protocol["decision"] != "commit"
        or protocol["admission"] is None
        or protocol.get("allocating") is not None
        or owner.root["result_imported_count"] != len(ready)
        or owner.root["result_queue_status"] not in {"imported", "partial"}
    ):
        raise InvalidIdentity("ownership_phase")
    _publication_committed_context(owner, _object_record(protocol["admission"]), files)
    artifacts = _object_record(protocol["artifacts"])
    candidates: set[RoleKey] = set()
    staging = _owner_carrier(owner, "staging")
    if staging.phase not in {"allocated", "discarding", "discarded"}:
        raise InvalidIdentity("ownership_phase")
    stage_roles = {
        r
        for r, o in owner.observed.items()
        if _within(o.spec.path, staging.carrier_path)
    }
    candidates.update(stage_roles)
    for file in ready:
        fid = _integer(file["file_id"], 1)
        entry = _object_record(artifacts.get(str(fid)))
        if (
            file["stage_ok"] != 1
            or file["stage_state"] != "staged"
            or file["publish_state"] != "published"
        ):
            raise InvalidIdentity("ownership_phase")
        if (
            entry.get("publication_receipt") is None
            or entry.get("publication_ready") is None
        ):
            raise InvalidIdentity("ownership_phase")
        receipt = LinkReceipt.from_value(entry.get("publication_receipt"))
        published = FullFileFingerprint.from_value(entry.get("publication_ready"))
        staged = _consumers._columns_fingerprint(file, "staged_")
        if (
            receipt.fingerprint != published
            or staged is None
            or (staged.size, staged.sha256) != (published.size, published.sha256)
        ):
            raise InvalidIdentity("ownership_phase")
        owner.present(owner.one("final", fid), receipt.fingerprint)
        stage = owner.one("stage", fid)
        if owner.observed[stage].outcome == "absent":
            if staging.phase not in {"discarding", "discarded"}:
                raise InvalidIdentity("ownership_phase")
        else:
            owner.present(stage, staged)
        for purpose in ("source", "original", "publication"):
            records = [
                c
                for c in owner.carriers
                if c.binding.file_id == fid and c.binding.purpose == purpose
            ]
            if not records:
                if purpose == "publication" or (
                    purpose == "original" and entry.get("captured_original") is not None
                ):
                    raise InvalidIdentity("ownership_phase")
                continue
            record = _owner_carrier(owner, purpose, fid)
            if purpose == "publication":
                if record.restore_receipt != receipt or record.phase not in {
                    "restored",
                    "discarding",
                    "discarded",
                }:
                    raise InvalidIdentity("ownership_phase")
                expected = published
            else:
                expected = FullFileFingerprint.from_value(
                    entry.get("captured_" + purpose)
                )
                if (
                    record.phase not in {"claimed", "discarding", "discarded"}
                    or record.artifact_fingerprint != expected
                    or record.restore_receipt is not None
                ):
                    raise InvalidIdentity("ownership_phase")
            artifact = owner.one(purpose + "_artifact", fid)
            if owner.observed[artifact].outcome == "absent":
                if record.phase not in {"discarding", "discarded"}:
                    raise InvalidIdentity("ownership_phase")
            else:
                owner.present(artifact, expected)
            candidates.update(
                r
                for r, o in owner.observed.items()
                if _within(o.spec.path, record.carrier_path)
            )
    _cleanup_subjects(owner, subjects, candidates)


def _publication_plan(
    owner: _PlanOwner, decisions: list[RecoveryDecision], subjects: set[RoleKey]
) -> None:
    row, action = owner.root, decisions[0].action
    queue = _object_record(_load(row["queue_snapshot_json"]))
    protocol = _object_record(queue.get("_file_publication"))
    artifacts = _object_record(protocol["artifacts"])
    files = [
        _object_record(r["row"])
        for r in owner.records
        if r["table"] == "import_publication_files"
    ]
    ready = [f for f in files if f["plan_status"] == "ready"]
    if not ready:
        raise InvalidIdentity("ownership_phase")
    if action == "cleanup-committed":
        _publication_cleanup_plan(owner, subjects, protocol, files)
        return
    expected_subjects: set[RoleKey] = set()
    if action == "resume-commit":
        if (
            row["state"] != "published"
            or protocol["decision"] != "pending"
            or protocol["admission"] is None
            or protocol.get("allocating") is not None
        ):
            raise InvalidIdentity("ownership_phase")
        _publication_context(owner, _object_record(protocol["admission"]))
    elif action in {"restore-original", "keep-current"}:
        if (
            row["state"] not in {"prepared", "publishing", "published"}
            or protocol["decision"] not in {"pending", "compensate"}
            or protocol.get("allocating") is not None
        ):
            raise InvalidIdentity("ownership_phase")
        if action == "restore-original" and owner.plan.version == 2:
            _publication_context(owner, _object_record(protocol["admission"]))
    else:
        raise InvalidIdentity("ownership_phase")
    for file in ready:
        fid = _integer(file["file_id"], 1)
        entry = _object_record(artifacts.get(str(fid), {}))
        final = owner.one("final", fid)
        acknowledgement = entry.get("publication_receipt")
        if action == "restore-original":
            original = owner.one("original_artifact", fid)
            carrier = _owner_carrier(owner, "original", fid)
            proof = entry.get("captured_original")
            if (
                proof is None
                or carrier.phase not in {"claimed", "restoring"}
                or carrier.artifact_fingerprint != FullFileFingerprint.from_value(proof)
            ):
                raise InvalidIdentity("ownership_phase")
            owner.present(original, FullFileFingerprint.from_value(proof))
            if final in subjects:
                if acknowledgement is None:
                    raise InvalidIdentity("ownership_phase")
                owner.present(
                    final, LinkReceipt.from_value(acknowledgement).fingerprint
                )
                expected_subjects.add(final)
                if owner.plan.version == 2:
                    admission_final = owner.one("admission_destination", fid)
                    if (
                        owner.observed[admission_final].spec.path
                        != owner.observed[final].spec.path
                    ):
                        raise InvalidIdentity("ownership_phase")
                    owner.present(
                        admission_final,
                        LinkReceipt.from_value(acknowledgement).fingerprint,
                    )
                    expected_subjects.add(admission_final)
            else:
                owner.absent(final)
                if acknowledgement is not None:
                    raise InvalidIdentity("ownership_phase")
            expected_subjects.add(original)
            matching = [d for d in decisions if original in d.roles]
            if (
                len(matching) != 1
                or matching[0].destination != owner.observed[final].spec.path
            ):
                raise InvalidIdentity("ownership_phase")
        else:
            expected_subjects.add(final)
            if action == "resume-commit":
                if (
                    acknowledgement is None
                    or file["publish_state"] != "published"
                    or file["stage_state"] != "staged"
                ):
                    raise InvalidIdentity("ownership_phase")
                receipt = LinkReceipt.from_value(acknowledgement)
                owner.present(final, receipt.fingerprint)
            else:
                if acknowledgement is not None:
                    raise InvalidIdentity("ownership_phase")
                owner.present(final)
                matching = [d for d in decisions if final in d.roles]
                if (
                    len(matching) != 1
                    or matching[0].roles != (final,)
                    or matching[0].destination != owner.observed[final].spec.path
                ):
                    raise InvalidIdentity("ownership_phase")
        staged = _consumers._columns_fingerprint(file, "staged_")
        if staged is None:
            raise InvalidIdentity("ownership_phase")
        owner.present(owner.one("stage", fid), staged)
        publication = _owner_carrier(owner, "publication", fid)
        private = owner.one("publication_artifact", fid)
        if owner.observed[private].outcome == "absent":
            if (
                action not in {"resume-commit", "restore-original"}
                or publication.phase != "restored"
                or publication.restore_receipt is None
            ):
                raise InvalidIdentity("ownership_phase")
        else:
            owner.present(private, staged)
        for purpose in ("source", "original"):
            records = [
                c
                for c in owner.carriers
                if c.binding.file_id == fid and c.binding.purpose == purpose
            ]
            if records:
                record = _owner_carrier(owner, purpose, fid)
                owner.present(
                    owner.one(purpose + "_artifact", fid), record.artifact_fingerprint
                )
            elif purpose == "source":
                source = _consumers._columns_fingerprint(file, "source_")
                if source is None:
                    raise InvalidIdentity("ownership_phase")
                owner.present(owner.one("source", fid), source)
    if subjects != expected_subjects:
        raise InvalidIdentity("ownership_phase")


def _review_text(value: object) -> str:
    result = _text(value)
    if not result.strip():
        raise InvalidIdentity("ownership_wire")
    return result


def _review_profile(value: object) -> None:
    profile = _plan_record(
        value,
        {
            "profile_id",
            "revision",
            "qualification_sha256",
            "client_contract",
            "server_contract",
        },
    )
    _ = _review_text(profile["profile_id"])
    _ = _integer(profile["revision"], 1)
    _ = _plan_hex(profile["qualification_sha256"])
    client = _object(profile["client_contract"], {"platforms", "mount"})
    platforms = _list(client["platforms"])
    if not platforms:
        raise InvalidIdentity("ownership_wire")
    _plan_ordered(platforms, _encode)
    for platform in platforms:
        for field in _object(
            platform, {"arch", "kernel_release", "libc_version", "encoder_revision"}
        ).values():
            _ = _review_text(field)
    mount = _object(
        client["mount"], {"filesystem", "nfs_version", "transport", "security", "hard"}
    )
    if type(mount["hard"]) is not bool:
        raise InvalidIdentity("ownership_wire")
    for key in ("filesystem", "nfs_version", "transport", "security"):
        _ = _review_text(mount[key])
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
    for key in ("vendor", "version", "filesystem", "exporter", "handle_contract"):
        _ = _review_text(server[key])
    for key in ("allowed_transitions", "recovery_transitions"):
        values = [_review_text(v) for v in _list(server[key])]
        if values != sorted(set(values)):
            raise InvalidIdentity("ownership_wire")


def _validate_reviews(
    plan: RecoveryPlan,
    declaration: dict[str, object],
    receipt: dict[str, object],
    epoch: dict[str, object],
    selected: set[tuple[OwnerRef, RoleKey]],
) -> None:
    observations = {(o.spec.owner, o.spec.role): o for o in plan.inventory.observed}
    authorities: dict[str, dict[str, object]] = {}
    linked = {
        (_owner_from(e["affected_owner"]), _role_from(e["affected_role"])): e
        for e in (
            _required_linked_effects(plan.inventory, plan.decisions)
            if plan.version == 2
            else ()
        )
    }
    authority_records = _list(declaration["authorities"])
    _plan_ordered(authority_records, lambda r: _plan_hex(r["authority_id"], 32))
    for value in authority_records:
        authority = _object(
            value,
            {
                "authority_id",
                "incarnation_token",
                "scope",
                "profile",
                "original_storage_statement",
                "administration_statement",
            },
        )
        aid = _plan_hex(authority["authority_id"], 32)
        _ = _plan_hex(authority["incarnation_token"], 32)
        scope = _plan_record(authority["scope"], {"scope_path", "scope_token"})
        _ = _path(scope["scope_path"])
        _ = _plan_hex(scope["scope_token"], 32)
        _review_profile(authority["profile"])
        _ = _review_text(authority["original_storage_statement"])
        _ = _review_text(authority["administration_statement"])
        authorities[aid] = authority
    owner_authorities: dict[OwnerRef, str | None] = {}
    used: set[str] = set()
    for value in _list(declaration["owner_reviews"]):
        review = _object_record(value)
        owner = _owner_from(review["owner"])
        aid = review["intended_original_authority_id"]
        if aid is not None:
            aid = _plan_hex(aid, 32)
            if aid not in authorities:
                raise InvalidIdentity("ownership_authority")
            used.add(aid)
        owner_authorities[owner] = aid
    role_authorities: dict[tuple[OwnerRef, RoleKey], str | None] = {}
    reviews = _list(declaration["role_reviews"])
    _plan_ordered(reviews, _subject_order)
    for value in reviews:
        review = _object(
            value, {"owner", "role", "observation_sha256", "authority_id", "assertion"}
        )
        identity = _plan_subject(review)
        observed = observations.get(identity)
        if observed is None or _plan_hex(review["observation_sha256"]) != _plan_hash(
            _observed_value(observed)
        ):
            raise InvalidIdentity("ownership_binding")
        if review["assertion"] != (
            "reviewed-recovery-subject"
            if identity in selected
            else "reviewed-linked-reference"
            if identity in linked
            else "retain-only"
        ):
            raise InvalidIdentity("ownership_binding")
        aid = review["authority_id"]
        if aid is not None:
            aid = _plan_hex(aid, 32)
            authority = authorities.get(aid)
            if authority is None or not _within(
                observed.spec.path,
                _path(_object_record(authority["scope"])["scope_path"]),
            ):
                raise InvalidIdentity("ownership_authority")
            used.add(aid)
        if identity in selected and (
            aid is None or owner_authorities[identity[0]] != aid
        ):
            raise InvalidIdentity("ownership_authority")
        role_authorities[identity] = aid
    if set(role_authorities) != set(observations):
        raise InvalidIdentity("ownership_binding")
    if plan.version == 2:
        linked_reviews = _list(declaration["linked_reviews"])
        _plan_ordered(linked_reviews, lambda r: _plan_hex(r["effect_sha256"]))
        expected_reviews: dict[str, str] = {}
        decisions = {_plan_hash(d.to_value()): d for d in plan.decisions}
        for identity, effect in linked.items():
            aid = role_authorities[identity]
            decision = decisions[_text(effect["decision_sha256"])]
            original = _role_from(effect["original_role"])
            if (
                aid is None
                or aid != owner_authorities[decision.owner]
                or aid != role_authorities[(decision.owner, original)]
            ):
                raise InvalidIdentity("ownership_authority")
            expected_reviews[_plan_hash(effect)] = aid
        actual_reviews: dict[str, str] = {}
        for value in linked_reviews:
            review = _object(value, {"effect_sha256", "authority_id"})
            actual_reviews[_plan_hex(review["effect_sha256"])] = _plan_hex(
                review["authority_id"], 32
            )
        if actual_reviews != expected_reviews:
            raise InvalidIdentity("ownership_binding")
    media_records = _list(receipt["media"])
    _plan_ordered(media_records, _subject_order)
    media: set[tuple[OwnerRef, RoleKey]] = set()
    destructive = {
        (d.owner, r)
        for d in plan.decisions
        for r in d.roles
        if d.action == "cleanup-committed"
        or (d.action == "resume-commit" and d.owner.domain == "deletion")
        or (
            d.action == "restore-original"
            and (
                r.purpose in {"destination", "final", "source", "publication_artifact"}
                or (plan.version == 2 and r.purpose == "admission_destination")
                or (plan.version == 2 and d.owner.domain == "pack_tombstone")
                or d.owner.domain == "pack_reservation"
            )
        )
    }
    destructive.update(linked)
    for value in media_records:
        record = _object(
            value,
            {"owner", "role", "observation_sha256", "handling", "recovery_reference"},
        )
        identity = _plan_subject(record)
        observation = observations.get(identity)
        if observation is None or _plan_hex(record["observation_sha256"]) != _plan_hash(
            _observed_value(observation)
        ):
            raise InvalidIdentity("ownership_binding")
        handling, reference = record["handling"], record["recovery_reference"]
        if observation.outcome == "absent":
            valid = handling == "observed-absent" and reference is None
        elif observation.outcome == _UNREACHABLE:
            valid = (
                plan.version == 2
                and handling == "observed-unreachable"
                and reference is None
            )
        elif handling == "snapshot":
            _ = _review_text(reference)
            valid = True
        else:
            valid = (
                handling == "retain-in-place"
                and reference is None
                and identity not in destructive
            )
        if not valid:
            raise InvalidIdentity("ownership_binding")
        media.add(identity)
    if media != set(observations):
        raise InvalidIdentity("ownership_binding")
    used.update(_validate_epoch_bindings(plan, epoch, authorities, role_authorities))
    if used != set(authorities):
        raise InvalidIdentity("ownership_binding")


def _validate_epoch_bindings(
    plan: RecoveryPlan,
    epoch: dict[str, object],
    authorities: dict[str, dict[str, object]],
    role_authorities: dict[tuple[OwnerRef, RoleKey], str | None],
) -> set[str]:
    """Bind captured history without allocating, activating, or rebinding roles."""
    observations = {(o.spec.owner, o.spec.role): o for o in plan.inventory.observed}
    raw_epochs = {
        _plan_hex(dict(r.values)["epoch_id"], 32): dict(r.values)
        for r in plan.inventory.closure.raw_rows
        if r.table == "file_identity_epochs"
    }
    raw_authorities = {
        _plan_hex(dict(r.values)["authority_id"], 32): dict(r.values)
        for r in plan.inventory.closure.raw_rows
        if r.table == "file_identity_authorities"
    }
    records = _list(epoch["existing"])
    _plan_ordered(records, lambda r: _plan_hex(r["epoch_id"], 32))
    existing: dict[str, dict[str, object]] = {}
    used: set[str] = set()
    for value in records:
        fields = _object(
            value,
            {
                "epoch_id",
                "authority_id",
                "origin_path",
                "allocation_path",
                "root_path",
                "birth_json",
            },
        )
        eid = _plan_hex(fields["epoch_id"], 32)
        aid = _plan_hex(fields["authority_id"], 32)
        for key in ("origin_path", "allocation_path", "root_path"):
            _ = _path(fields[key])
        _ = _text(fields["birth_json"])
        captured = raw_epochs.get(eid)
        if captured is None or any(fields[key] != captured[key] for key in fields):
            raise InvalidIdentity("ownership_binding")
        authority, raw = authorities.get(aid), raw_authorities.get(aid)
        if (
            authority is None
            or raw is None
            or (
                authority["incarnation_token"] != raw["incarnation_token"]
                or authority["scope"] != _load(raw["declaration_json"])
                or authority["profile"] != _load(raw["profile_json"])
            )
        ):
            raise InvalidIdentity("ownership_authority")
        existing[eid] = fields
        used.add(aid)
    if set(existing) != set(raw_epochs):
        raise InvalidIdentity("ownership_binding")
    candidates: dict[tuple[OwnerRef, RoleKey], set[tuple[str, str]]] = {}
    affected = {d.owner for d in plan.decisions if d.action != "retain-blocked"}
    for record in plan.inventory.closure.raw_rows:
        if record.table != "file_identity_sets":
            continue
        row = dict(record.values)
        snapshot_value = _object_record(_load(row["snapshot_json"]))
        snapshot = OwnerSnapshot(
            _owner_from(snapshot_value["owner"]),
            _text(row["snapshot_json"]),
            _plan_hex(row["snapshot_sha256"]),
        )
        manifest = _object_record(_load(row["manifest_json"]))
        declared = {_role_from(r) for r in _list(manifest["required_roles"])}
        for historical in (snapshot, *_proof_dependencies(manifest, snapshot.owner)):
            for spec in roles_for(historical):
                current = observations.get((spec.owner, spec.role))
                if historical.owner in affected and (
                    current is None or current.spec.path != spec.path
                ):
                    raise InvalidIdentity("ownership_incomplete")
                if (
                    historical is snapshot
                    and current is not None
                    and current.spec == spec
                    and spec.role in declared
                ):
                    candidates.setdefault((spec.owner, spec.role), set()).add(
                        (
                            _plan_hex(row["epoch_id"], 32),
                            _plan_hex(
                                raw_epochs[_plan_hex(row["epoch_id"], 32)][
                                    "authority_id"
                                ],
                                32,
                            ),
                        )
                    )
    bindings = _list(epoch["role_bindings"])
    _plan_ordered(bindings, _subject_order)
    seen: set[tuple[OwnerRef, RoleKey]] = set()
    namespaces = {
        s.owner: s
        for s in plan.inventory.closure.snapshots
        if s.owner.domain == "namespace"
    }
    for value in bindings:
        binding = _object(
            value, {"owner", "role", "namespace_owner", "epoch_id", "authority_id"}
        )
        identity = _plan_subject(binding)
        observation = observations.get(identity)
        if observation is None or binding["authority_id"] != role_authorities[identity]:
            raise InvalidIdentity("ownership_binding")
        applicable = [
            owner
            for owner in namespaces
            if _within(observation.spec.path, str(owner.key[0]) + "/.mangarr-claims")
        ]
        namespace = (
            None
            if binding["namespace_owner"] is None
            else _owner_from(binding["namespace_owner"])
        )
        if (namespace is None and applicable) or (
            namespace is not None and namespace not in applicable
        ):
            raise InvalidIdentity("ownership_binding")
        captured = candidates.get(identity, set())
        eid = (
            None if binding["epoch_id"] is None else _plan_hex(binding["epoch_id"], 32)
        )
        aid = (
            None
            if binding["authority_id"] is None
            else _plan_hex(binding["authority_id"], 32)
        )
        if (eid is None and captured) or (
            eid is not None and (eid, aid) not in captured
        ):
            raise InvalidIdentity("ownership_binding")
        seen.add(identity)
    if seen != set(observations):
        raise InvalidIdentity("ownership_binding")
    provision = _validate_provision_intents(
        plan, epoch, authorities, role_authorities, existing
    )
    used.update(_plan_hex(item["authority_id"], 32) for item in provision.values())
    _validate_allocation_capabilities(plan)
    _validate_rollback_outputs(plan, epoch, role_authorities, existing, provision)
    return used


def _validate_allocation_capabilities(plan: RecoveryPlan) -> None:
    if plan.version != 2:
        return
    epoch = _epoch_record(_load(plan.epoch_bindings_json), 2)
    provision = {
        _plan_hex(_object_record(i)["epoch_id"], 32) for i in _list(epoch["provision"])
    }
    required = {
        _plan_hex(_object_record(i)["epoch_id"], 32)
        for i in _list(epoch["rollback_outputs"])
    } - provision
    references = _list(epoch["allocation_capabilities"])
    _plan_ordered(references, lambda r: _plan_hex(r["epoch_id"], 32))
    if {_plan_hex(_object_record(i)["epoch_id"], 32) for i in references} != required:
        raise InvalidIdentity("ownership_phase")
    raw = {(r.table, r.key): r for r in plan.inventory.closure.raw_rows}
    observations = {(o.spec.owner, o.spec.role): o for o in plan.inventory.observed}
    snapshots = {s.owner: s for s in plan.inventory.closure.snapshots}
    for item in references:
        reference = _object(
            item,
            {"epoch_id", "owner", "set_id", "event_id", "event_sha256", "birth_sha256"},
        )
        eid, sid, event_id = (
            _plan_hex(reference["epoch_id"], 32),
            _plan_hex(reference["set_id"], 32),
            _integer(reference["event_id"], 1),
        )
        owner = _owner_from(reference["owner"])
        epoch_raw, set_raw, event_raw = (
            raw.get((table, (key,)))
            for table, key in (
                ("file_identity_epochs", eid),
                ("file_identity_sets", sid),
                ("file_identity_events", event_id),
            )
        )
        if epoch_raw is None or set_raw is None or event_raw is None:
            raise InvalidIdentity("ownership_binding")
        epoch_row, set_row, event = (
            dict(epoch_raw.values),
            dict(set_raw.values),
            dict(event_raw.values),
        )
        current_snapshot = snapshots.get(owner)
        if current_snapshot is None or (
            set_row["snapshot_json"] != current_snapshot.rows_json
            or set_row["snapshot_sha256"] != current_snapshot.sha256
        ):
            raise InvalidIdentity("ownership_binding")
        manifest = _object_record(_load(set_row["manifest_json"]))
        for value in _list(manifest["dependencies"]):
            dependency = _object_record(value)
            captured = _object_record(_load(dependency["snapshot_json"]))
            current = snapshots.get(_owner_from(captured["owner"]))
            if (
                current is None
                or dependency["snapshot_json"] != current.rows_json
                or dependency["sha256"] != current.sha256
            ):
                raise InvalidIdentity("ownership_binding")
        birth = _store._epoch_birth(_text(epoch_row["birth_json"]))
        if birth["version"] != 2 or owner != OwnerRef(
            "namespace", (_path(epoch_row["allocation_path"]),)
        ):
            raise InvalidIdentity("ownership_phase")
        if reference["birth_sha256"] != _plan_hash(birth) or reference[
            "event_sha256"
        ] != _plan_hash(
            {
                "table": event_raw.table,
                "key": list(event_raw.key),
                "values": [list(pair) for pair in event_raw.values],
            }
        ):
            raise InvalidIdentity("ownership_binding")
        key_json = _plan_encode({"version": 1, "key": list(owner.key)})
        if (
            event["owner_domain"] != owner.domain
            or event["owner_key_json"] != key_json
            or event["set_id"] != sid
            or set_row["owner_domain"] != owner.domain
            or set_row["owner_key_json"] != key_json
            or set_row["epoch_id"] != eid
        ):
            raise InvalidIdentity("ownership_binding")
        owner_events = [
            dict(r.values)
            for r in plan.inventory.closure.raw_rows
            if r.table == "file_identity_events"
            and dict(r.values)["owner_domain"] == owner.domain
            and dict(r.values)["owner_key_json"] == key_json
        ]
        if (
            not owner_events
            or max(owner_events, key=lambda e: _integer(e["revision"], 1)) != event
            or event["kind"] != "recovery_step"
        ):
            raise InvalidIdentity("ownership_phase")
        payload = _store._allocation_payload(_text(event["payload_json"]))
        if (
            payload["state"] != "grant"
            or payload["consumer_contract_sha256"] != _ALLOCATION_CONSUMER_SHA256
            or payload["epoch_id"] != eid
        ):
            raise InvalidIdentity("ownership_phase")
        # Reconstruction already validates the entire chain. Check the precise
        # recorded activation again here; never scan backward for usable grants.
        activation_raw = raw.get(
            ("file_identity_events", (_integer(payload["activation_event_id"], 1),))
        )
        if activation_raw is None:
            raise InvalidIdentity("ownership_phase")
        activation = dict(activation_raw.values)
        if (
            event["predecessor_event_id"] != activation["id"]
            or activation["kind"] != "activated"
            or activation["set_id"] != sid
        ):
            raise InvalidIdentity("ownership_phase")
        receipt = _object_record(birth["controlled_birth"])
        for value in _list(receipt["objects"]):
            obj = _store._object_from(value)
            observed = observations.get((owner, obj.role))
            if (
                observed is None
                or observed.outcome != "observed"
                or observed.native != obj.native
                or observed.spec.path != obj.canonical_path
                or observed.spec.parent_role != obj.parent_role
            ):
                raise InvalidIdentity("ownership_authority")
            frozen = _store._constraints(obj.constraints_json, obj.role)
            current = _constraints(observed.constraints_json, observed.spec)
            fields = _object_record(frozen["non_device"])
            info = _object_record(current["current"])
            content = current["content"]
            if (
                observed.spec.policy != frozen["policy"]
                or any(
                    info[name] != scalar
                    for name, scalar in fields.items()
                    if scalar is not None and name != "sha256"
                )
                or (
                    fields["sha256"] is not None
                    and _object_record(content)["sha256"] != fields["sha256"]
                )
            ):
                raise InvalidIdentity("ownership_authority")


def _validate_provision_intents(
    plan: RecoveryPlan,
    epoch: dict[str, object],
    authorities: dict[str, dict[str, object]],
    role_authorities: dict[tuple[OwnerRef, RoleKey], str | None],
    existing: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    records = _list(epoch["provision"])
    _plan_ordered(records, lambda r: _plan_hex(r["epoch_id"], 32))
    namespaces = {
        s.owner: roles_for(s)
        for s in plan.inventory.closure.snapshots
        if s.owner.domain == "namespace"
    }
    roots = {
        owner: next(r.path for r in specs if r.role.purpose == "namespace_root")
        for owner, specs in namespaces.items()
    }
    planned: dict[str, dict[str, object]] = {}
    allocations: list[str] = []
    reserved = [
        _object_record(_load(dict(row.values)["payload_json"]))
        for row in plan.inventory.closure.raw_rows
        if row.table == "file_identity_provision_intents"
    ]
    for value in records:
        intent = _object(
            value,
            {
                "epoch_id",
                "authority_id",
                "origin_parent",
                "allocation_parent",
                "root_path",
                "old_namespace_owners",
                "handling",
            },
        )
        eid = _plan_hex(intent["epoch_id"], 32)
        aid = _plan_hex(intent["authority_id"], 32)
        origin = _path(intent["origin_parent"])
        allocation, root = (
            _path(intent["allocation_parent"]),
            _path(intent["root_path"]),
        )
        if (
            intent["handling"] != "retain-old-disable-new-allocation"
            or allocation != origin.rstrip("/") + "/.mangarr-epoch-" + eid
            or root != allocation + "/.mangarr-claims"
            or eid in existing
            or any(
                item["epoch_id"] == eid
                or _within(allocation, _path(item["allocation_path"]))
                or _within(_path(item["allocation_path"]), allocation)
                for item in reserved
            )
        ):
            raise InvalidIdentity("ownership_binding")
        authority = authorities.get(aid)
        if authority is None or not _within(
            allocation, _path(_object_record(authority["scope"])["scope_path"])
        ):
            raise InvalidIdentity("ownership_authority")
        origin_roles = [
            o
            for o in plan.inventory.observed
            if o.spec.path == origin
            and o.spec.role.kind == "directory"
            and o.spec.role.purpose in {"namespace_parent", "destination"}
            and o.outcome == "observed"
            and role_authorities[(o.spec.owner, o.spec.role)] == aid
        ]
        if not origin_roles or any(
            _within(origin, old_root) for old_root in roots.values()
        ):
            raise InvalidIdentity("ownership_authority")
        expected_old = {
            owner
            for owner, old_root in roots.items()
            if owner.key == (origin,)
            or any(
                e["origin_path"] == origin and e["root_path"] == old_root
                for e in existing.values()
            )
        }
        owners = [_owner_from(o) for o in _list(intent["old_namespace_owners"])]
        for owner in owners:
            _validate_owner(owner)
            if owner.domain != "namespace":
                raise InvalidIdentity("ownership_binding")
        order = [_encode(_owner_value(o)) for o in owners]
        if order != sorted(set(order)) or set(owners) != expected_old:
            raise InvalidIdentity("ownership_binding")
        if (
            any(_within(o.spec.path, allocation) for o in plan.inventory.observed)
            or any(
                _within(allocation, p) or _within(p, allocation) for p in allocations
            )
            or any(
                _within(allocation, _path(e["root_path"]))
                or allocation in {e["allocation_path"], e["root_path"]}
                for e in existing.values()
            )
        ):
            raise InvalidIdentity("ownership_binding")
        allocations.append(allocation)
        planned[eid] = intent
    return planned


@dataclass(frozen=True, slots=True)
class _RollbackPair:
    owner: OwnerRef
    original: RoleSpec
    publication: RoleSpec
    aliases: tuple[RoleSpec, ...]


def _rollback_pairs(
    plan: RecoveryPlan,
) -> dict[tuple[OwnerRef, RoleKey], _RollbackPair]:
    pairs: dict[tuple[OwnerRef, RoleKey], _RollbackPair] = {}
    specs = {(s.owner, s.role): s for s in plan.inventory.closure.roles}
    for decision in plan.decisions:
        if decision.action != "restore-original" or decision.owner.domain not in {
            "rescan",
            "publication",
        }:
            continue
        original_purpose, public_purpose = (
            ("source_artifact", "destination")
            if decision.owner.domain == "rescan"
            else ("original_artifact", "final")
        )
        public = [r for r in decision.roles if r.purpose == public_purpose]
        if not public:
            continue
        originals = [r for r in decision.roles if r.purpose == original_purpose]
        if (
            len(public) != 1
            or len(originals) != 1
            or originals[0].file_id != public[0].file_id
        ):
            raise InvalidIdentity("ownership_phase")
        original, publication = (
            specs[(decision.owner, originals[0])],
            specs[(decision.owner, public[0])],
        )
        aliases: tuple[RoleSpec, ...] = ()
        if (
            decision.owner.domain == "rescan"
            and decision.destination == publication.path
        ):
            aliases = tuple(
                s
                for (owner, _), s in specs.items()
                if owner == decision.owner
                and s.role.purpose == "source"
                and s.role.file_id == publication.role.file_id
                and s.path == publication.path
            )
            if len(aliases) != 1 or aliases[0].role not in decision.roles:
                raise InvalidIdentity("ownership_phase")
        elif decision.owner.domain == "publication" and plan.version == 2:
            aliases = tuple(
                s
                for (owner, role), s in specs.items()
                if owner == decision.owner
                and role.purpose == "admission_destination"
                and role.file_id == publication.role.file_id
                and s.path == publication.path
            )
            if len(aliases) != 1 or aliases[0].role not in decision.roles:
                raise InvalidIdentity("ownership_phase")
        pairs[(decision.owner, publication.role)] = _RollbackPair(
            decision.owner, original, publication, aliases
        )
    return pairs


def _rollback_projection_size(
    inventory: OwnershipInventory,
    epoch: dict[str, object],
    remaining: int,
) -> int:
    provisions = _list(epoch["provision"])
    outputs = _list(epoch["rollback_outputs"])
    objects = (
        sum(
            1 + len(_list(_constraints(o.constraints_json, o.spec)["children"]))
            for o in inventory.observed
        )
        # Charge all four typed birth bindings, including the independent origin.
        + 4 * len(provisions)
        + 3 * len(outputs)
    )
    if objects > inventory.budget.max_objects:
        raise InvalidIdentity("ownership_budget")
    if not outputs:
        return 0
    roots: dict[str, str] = {}
    for value in [*_list(epoch["existing"]), *provisions]:
        record = _object_record(value)
        eid = _plan_hex(record.get("epoch_id"), 32)
        if eid in roots:
            raise InvalidIdentity("ownership_binding")
        roots[eid] = _path(record.get("root_path"))
    size = 2
    count = 0
    for value in outputs:
        output = _plan_record(
            value,
            {
                "owner",
                "original_role",
                "publication_role",
                "epoch_id",
                "carrier_token",
                "handling",
            },
        )
        owner = _owner_from(output["owner"])
        public = _role_from(output["publication_role"])
        eid = _plan_hex(output["epoch_id"], 32)
        _ = _plan_hex(output["carrier_token"], 32)
        if eid not in roots:
            raise InvalidIdentity("ownership_binding")
        carrier_role = RoleKey(
            public.file_id, "recovery_rollback_carrier", "directory", None
        )
        for purpose, kind, relative in (
            ("carrier", "directory", None),
            ("marker", "marker", "owner.json"),
            ("artifact", "file", "artifact"),
        ):
            # Charge ASCII suffixes without constructing any proposed long paths.
            suffix = 33 + (0 if relative is None else 1 + len(relative))
            projection = {
                "owner": _owner_value(owner),
                "role": _role_value(
                    RoleKey(
                        public.file_id, "recovery_rollback_" + purpose, kind, relative
                    )
                ),
                "path": roots[eid],
                "parent_role": None if relative is None else _role_value(carrier_role),
                "epoch_id": eid,
                "handling": "retain-non-replayable",
            }
            comma = int(count > 0)
            size += (
                comma
                + suffix
                + _plan_size(projection, remaining - size - comma - suffix)
            )
            count += 1
    return size


def _validate_rollback_outputs(
    plan: RecoveryPlan,
    epoch: dict[str, object],
    role_authorities: dict[tuple[OwnerRef, RoleKey], str | None],
    existing: dict[str, dict[str, object]],
    provision: dict[str, dict[str, object]],
) -> None:
    pairs = _rollback_pairs(plan)
    values = _list(epoch["rollback_outputs"])
    _plan_ordered(
        values, lambda r: (_encode(r["owner"]), _encode(r["publication_role"]))
    )
    seen: set[tuple[OwnerRef, RoleKey]] = set()
    tokens = {
        c.owner_token
        for snapshot in plan.inventory.closure.snapshots
        for c in _consumers._inventory(snapshot).carriers
    }
    placements: set[str] = set()
    origins: set[str] = set()
    _ = _rollback_projection_size(
        plan.inventory, epoch, _DOCUMENT_BYTES - _plan_constructor_size(plan)
    )
    observations = {(o.spec.owner, o.spec.role): o for o in plan.inventory.observed}
    old_roots = {
        s.path
        for s in plan.inventory.closure.roles
        if s.owner.domain == "namespace" and s.role.purpose == "namespace_root"
    }
    for value in values:
        output = _plan_record(
            value,
            {
                "owner",
                "original_role",
                "publication_role",
                "epoch_id",
                "carrier_token",
                "handling",
            },
        )
        owner = _owner_from(output["owner"])
        _validate_owner(owner)
        public = _role_from(output["publication_role"])
        identity = owner, public
        pair = pairs.get(identity)
        if (
            pair is None
            or _role_from(output["original_role"]) != pair.original.role
            or output["handling"] != "retain-non-replayable"
        ):
            raise InvalidIdentity("ownership_binding")
        eid, token = (
            _plan_hex(output["epoch_id"], 32),
            _plan_hex(output["carrier_token"], 32),
        )
        intent = provision.get(eid)
        captured = existing.get(eid)
        aid = role_authorities[identity]
        if aid is None or role_authorities[(owner, pair.original.role)] != aid:
            raise InvalidIdentity("ownership_authority")
        if intent is not None:
            root, origin = _path(intent["root_path"]), _path(intent["origin_parent"])
            if intent["authority_id"] != aid:
                raise InvalidIdentity("ownership_authority")
        elif captured is not None:
            root, origin = _path(captured["root_path"]), _path(captured["origin_path"])
            birth = _object_record(_load(captured["birth_json"]))
            if (
                captured["authority_id"] != aid
                or birth["controlled_birth"] is None
                or birth["legacy_namespace"] is not None
            ):
                raise InvalidIdentity("ownership_authority")
            required = {root, origin, _path(captured["allocation_path"])}
            observed = {
                o.spec.path
                for o in plan.inventory.observed
                if o.outcome == "observed"
                and o.spec.role.kind == "directory"
                and role_authorities[(o.spec.owner, o.spec.role)] == aid
            }
            if not required <= observed:
                raise InvalidIdentity("ownership_authority")
        else:
            raise InvalidIdentity("ownership_binding")
        if origin != pair.publication.path.rsplit("/", 1)[0] or any(
            _within(root, old)
            and not (
                plan.version == 2
                and captured is not None
                and _object_record(_load(captured["birth_json"]))["version"] == 2
                and old == root
            )
            for old in old_roots
        ):
            raise InvalidIdentity("ownership_binding")
        carrier = root + "/" + token
        if (
            token in tokens
            or carrier in placements
            or pair.publication.path in origins
            or any(_within(o.spec.path, carrier) for o in plan.inventory.observed)
        ):
            raise InvalidIdentity("ownership_binding")
        expected_aliases = {pair.publication.role, *(s.role for s in pair.aliases)}
        linked_aliases = {
            (_owner_from(e["affected_owner"]), _role_from(e["affected_role"]))
            for e in (
                _required_linked_effects(plan.inventory, plan.decisions)
                if plan.version == 2
                else ()
            )
            if e["effect"] == "replace-acknowledged-name"
            and e["decision_sha256"]
            in {
                _plan_hash(d.to_value())
                for d in plan.decisions
                if d.owner == owner and pair.original.role in d.roles
            }
        }
        for observation in plan.inventory.observed:
            if (
                observation.spec.path != pair.publication.path
                or observation.outcome == "absent"
            ):
                continue
            linked_alias = (
                observation.spec.owner,
                observation.spec.role,
            ) in linked_aliases
            if observation.spec.owner != owner and not linked_alias:
                raise InvalidIdentity("ownership_phase")
            if (
                observation.spec.owner == owner
                and observation.spec.role in expected_aliases
            ) or linked_alias:
                source = observations[identity]
                current = _constraints(observation.constraints_json, observation.spec)
                public_current = _constraints(source.constraints_json, source.spec)
                if observation.native != source.native or any(
                    current[k] != public_current[k] for k in ("current", "content")
                ):
                    raise InvalidIdentity("ownership_phase")
            else:
                raise InvalidIdentity("ownership_phase")
        tokens.add(token)
        placements.add(carrier)
        origins.add(pair.publication.path)
        seen.add(identity)
    if seen != set(pairs):
        raise InvalidIdentity("ownership_binding")


def build_recovery_plan(
    inventory: OwnershipInventory,
    decisions_json: str,
    declaration_json: str,
    backup_receipt_json: str,
) -> RecoveryPlan:
    """Bind an offline reviewed proposal; never authorize or perform recovery."""
    if type(inventory) is not OwnershipInventory:
        raise InvalidIdentity("ownership_wire")
    inventory.__post_init__()
    wrapper: dict[str, object] = {
        "version": 1,
        "inventory": inventory._value(),
        "decisions": [],
        "declaration_json": "",
        "backup_receipt_json": "",
        "epoch_bindings_json": "",
        "sha256": "0" * 64,
    }
    reserved = _wire_size(wrapper, _DOCUMENT_BYTES)
    scanner = _PlanScanner(_text(decisions_json), _DOCUMENT_BYTES)
    _ = scanner.scan()
    if set(scanner.members) != {
        "version",
        "inventory_sha256",
        "decisions",
        "epoch_bindings",
    }:
        raise InvalidIdentity("ownership_wire")
    decisions_size = scanner.members["decisions"][0]
    epoch_plain, epoch_escape = scanner.members["epoch_bindings"]
    reserved += decisions_size - 2 + epoch_plain + epoch_escape
    if reserved > _DOCUMENT_BYTES:
        raise InvalidIdentity("ownership_budget")
    selection_value = _object_record(_load(decisions_json))
    version = _integer(selection_value.get("version"), 1, 2)
    selection = _plan_record(
        selection_value,
        {"inventory_sha256", "decisions", "epoch_bindings"},
        version=version,
    )
    if _plan_hex(selection["inventory_sha256"]) != inventory.sha256:
        raise InvalidIdentity("ownership_binding")
    epoch_fields = _epoch_record(selection["epoch_bindings"], version)
    reserved += _rollback_projection_size(
        inventory, epoch_fields, _DOCUMENT_BYTES - reserved
    )
    # Reserve escaped canonical bytes BEFORE decoding containers or strings.
    declaration_scan = _PlanScanner(
        _text(declaration_json), _DOCUMENT_BYTES - reserved + 2
    )
    plain, escapes = declaration_scan.scan(quoted=True)
    reserved += plain + escapes
    receipt_scan = _PlanScanner(
        _text(backup_receipt_json), _DOCUMENT_BYTES - reserved + 2
    )
    _ = receipt_scan.scan(quoted=True)
    declaration = _plan_encode(_load(declaration_json))
    receipt = _plan_encode(_load(backup_receipt_json))
    epoch = _plan_encode(selection["epoch_bindings"])
    value = {
        "version": version,
        "inventory": inventory._value(),
        "decisions": selection["decisions"],
        "declaration_json": declaration,
        "backup_receipt_json": receipt,
        "epoch_bindings_json": epoch,
        "sha256": "0" * 64,
    }
    _ = _wire_size(value, _DOCUMENT_BYTES)
    decisions = tuple(
        RecoveryDecision.from_value(d) for d in _list(selection["decisions"])
    )
    del value["sha256"]
    return RecoveryPlan(
        version, inventory, decisions, declaration, receipt, epoch, _plan_hash(value)
    )


@dataclass(slots=True)
class _PlanScanner:
    """Preflight canonical and embedded-string bytes without retaining values.

    The shared decoder remains the JSON implementation. This bounded scanner
    reserves container records and escaped strings before that decoder runs.
    """

    text: str
    limit: int
    offset: int = 0
    quoted: bool = False
    members: dict[str, tuple[int, int]] = field(default_factory=dict)

    def check(self, plain: int, escapes: int) -> None:
        if plain + (escapes + 2 if self.quoted else 0) > self.limit:
            raise InvalidIdentity("ownership_budget")

    def whitespace(self) -> None:
        while self.offset < len(self.text) and self.text[self.offset] in " \r\n\t":
            self.offset += 1

    def take(self, expected: str) -> None:
        self.whitespace()
        if self.offset >= len(self.text) or self.text[self.offset] != expected:
            raise InvalidIdentity("ownership_wire")
        self.offset += 1

    def string(self, key: bool = False) -> tuple[int, int, str | None]:
        self.take('"')
        plain = escapes = 2
        name: list[str] = []
        simple = {
            '"': '"',
            "\\": "\\",
            "/": "/",
            "b": "\b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
        }
        while self.offset < len(self.text):
            character = self.text[self.offset]
            self.offset += 1
            if character == '"':
                self.check(plain, escapes)
                return plain, escapes, "".join(name) if key else None
            if character == "\\":
                if self.offset >= len(self.text):
                    raise InvalidIdentity("ownership_wire")
                token = self.text[self.offset]
                self.offset += 1
                if token == "u":
                    code = self.unicode_escape()
                    if 0xD800 <= code <= 0xDBFF:
                        if self.text[self.offset : self.offset + 2] != "\\u":
                            raise InvalidIdentity("ownership_wire")
                        self.offset += 2
                        low = self.unicode_escape()
                        if not 0xDC00 <= low <= 0xDFFF:
                            raise InvalidIdentity("ownership_wire")
                        code = 0x10000 + ((code - 0xD800) << 10) + low - 0xDC00
                    elif 0xDC00 <= code <= 0xDFFF:
                        raise InvalidIdentity("ownership_wire")
                    character = chr(code)
                elif token in simple:
                    character = simple[token]
                else:
                    raise InvalidIdentity("ownership_wire")
            elif ord(character) < 32:
                raise InvalidIdentity("ownership_wire")
            code = ord(character)
            if code == 0 or 0xD800 <= code <= 0xDFFF:
                raise InvalidIdentity("ownership_wire")
            if character in '\\"':
                plain += 2
                escapes += 2
            elif character in "\b\t\n\f\r":
                plain += 2
                escapes += 1
            elif code < 32 or 127 <= code <= 0xFFFF:
                plain += 6
                escapes += 1
            elif code > 0xFFFF:
                plain += 12
                escapes += 2
            else:
                plain += 1
            self.check(plain, escapes)
            if key:
                if len(name) >= 64:
                    raise InvalidIdentity("ownership_wire")
                name.append(character)
        raise InvalidIdentity("ownership_wire")

    def unicode_escape(self) -> int:
        token = self.text[self.offset : self.offset + 4]
        self.offset += 4
        if len(token) != 4 or any(c not in "0123456789abcdefABCDEF" for c in token):
            raise InvalidIdentity("ownership_wire")
        return int(token, 16)

    def value(self, depth: int = 0) -> tuple[int, int]:
        self.whitespace()
        if depth > 32 or self.offset >= len(self.text):
            raise InvalidIdentity("ownership_wire")
        character = self.text[self.offset]
        if character == '"':
            plain, escapes, _ = self.string()
            return plain, escapes
        if character in "[{":
            if depth >= 32:
                raise InvalidIdentity("ownership_wire")
            mapping = character == "{"
            end = "}" if mapping else "]"
            self.offset += 1
            plain, escapes, count = 2, 0, 0
            keys: set[str] = set()
            self.whitespace()
            if self.offset < len(self.text) and self.text[self.offset] == end:
                self.offset += 1
                return plain, escapes
            while True:
                if count >= 10000:
                    raise InvalidIdentity("ownership_wire")
                name = None
                if mapping:
                    key_plain, key_escapes, name = self.string(key=True)
                    if name is None or name in keys:
                        raise InvalidIdentity("ownership_wire")
                    keys.add(name)
                    plain += key_plain + 1
                    escapes += key_escapes
                    self.take(":")
                size, overhead = self.value(depth + 1)
                plain += size + int(count > 0)
                escapes += overhead
                if mapping and depth == 0 and name is not None:
                    self.members[name] = size, overhead
                self.check(plain, escapes)
                count += 1
                self.whitespace()
                if self.offset < len(self.text) and self.text[self.offset] == end:
                    self.offset += 1
                    return plain, escapes
                self.take(",")
        start = self.offset
        while (
            self.offset < len(self.text) and self.text[self.offset] not in " \r\n\t,]}:"
        ):
            self.offset += 1
        token = self.text[start : self.offset]
        if token in {"true", "false", "null"}:
            return len(token), 0
        digits = token[1:] if token.startswith("-") else token
        if (
            not digits
            or len(token) > 20
            or any(c not in "0123456789" for c in digits)
            or (len(digits) > 1 and digits[0] == "0")
        ):
            raise InvalidIdentity("ownership_wire")
        value = _integer(int(token), -(2**63))
        return len(str(value)), 0

    def scan(self, *, quoted: bool = False) -> tuple[int, int]:
        self.quoted = quoted
        if len(self.text) > _DOCUMENT_BYTES:
            raise InvalidIdentity("ownership_budget")
        metrics = self.value()
        self.whitespace()
        if self.offset != len(self.text):
            raise InvalidIdentity("ownership_wire")
        self.check(*metrics)
        return metrics


def _plan_input(encoded: str) -> object:
    _ = _PlanScanner(_text(encoded), _DOCUMENT_BYTES).scan()
    return _load(encoded)


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
    if type(value) is not dict:
        raise InvalidIdentity("invalid observation constraints")
    document = cast(dict[str, object], value)
    version = _integer(document.get("version"), 1, 2)
    fields = _object(
        document,
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
        _ = _plan_hex(ancestor["observation_sha256"])
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
        for key in ("dev", "inode"):
            _ = _integer(info[key], 0, 2**64 - 1)
        _ = _integer(info["uid"])
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
            or _text(item["outcome"]) not in _OUTCOMES - {_UNREACHABLE}
        ):
            raise InvalidIdentity("invalid observation child")
        paths.append(path)
    if paths != sorted(set(paths)):
        raise InvalidIdentity("observation children duplicate or unordered")
    if encoded != _encode(document):
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
        if (fields["version"] == 2) != (self.outcome == _UNREACHABLE):
            raise InvalidIdentity("witness constraints/outcome mismatch")
        if self.outcome == _UNREACHABLE:
            if (
                self.native is not None
                or fields["current"] is not None
                or fields["content"] is not None
                or fields["children"]
                or fields["policy"] != "not_checked"
                or fields["boundary"] != "verified"
            ):
                raise InvalidIdentity("witness contains present-object evidence")
        elif self.outcome in _COMPLETE:
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
        elif self.outcome == "unknown_children":
            if (
                self.spec.role.kind != "directory"
                or fields["current"] is None
                or fields["content"] is not None
                or fields["policy"] != "satisfied"
                or fields["boundary"] != "verified"
                or not fields["children"]
            ):
                raise InvalidIdentity("unknown children lack stable directory evidence")
        elif (
            self.native is not None
            or fields["current"] is not None
            or fields["content"] is not None
            or fields["children"]
            or fields["policy"]
            not in (
                {"mismatch"}
                if self.outcome == "policy_mismatch"
                else {"not_checked", "satisfied"}
            )
            or fields["boundary"]
            not in (
                {"cross_device"}
                if self.outcome == "cross_device"
                else {"unknown", "verified"}
            )
        ):
            raise InvalidIdentity(
                "failure outcome contradicts retained object evidence"
            )

    @property
    def unresolved(self) -> bool:
        """Incomplete acquisition only; complete still grants no action authority."""
        return self.outcome not in _COMPLETE and self.outcome != _UNREACHABLE


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
    if any(r.table == "file_identity_provision_intents" for r in closure.raw_rows):
        return _provision_closure_value(closure)
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


def _provision_closure_value(closure: ReferenceClosure) -> dict[str, object]:
    """Charge the complete successor projection before retaining each record."""
    groups: dict[str, list[object]] = {
        "snapshots": [],
        "roles": [],
        "dependencies": [],
        "raw_rows": [],
    }
    remaining = _DOCUMENT_BYTES - _plan_size(groups, _DOCUMENT_BYTES)
    for records in (
        closure.snapshots,
        closure.roles,
        closure.dependencies,
        closure.raw_rows,
    ):
        if len(records) > 10000:
            raise InvalidIdentity("ownership_budget")

    def append(name: str, record: dict[str, object]) -> None:
        nonlocal remaining
        remaining -= int(bool(groups[name]))
        remaining -= _plan_size(record, remaining)
        groups[name].append(record)

    # Raw text can contain an entire prior plan. Charge it before producing the
    # role projections and before accumulating any raw values array.
    for row in closure.raw_rows:
        if len(row.values) > 10000 or len(row.key) > 10000:
            raise InvalidIdentity("ownership_budget")
        pairs: list[object] = []
        record: dict[str, object] = {
            "table": row.table,
            "key": list(row.key),
            "values": pairs,
        }
        remaining -= int(bool(groups["raw_rows"]))
        remaining -= _plan_size(record, remaining)
        for name, scalar in row.values:
            remaining -= int(bool(pairs))
            pair: list[object] = [name, scalar]
            remaining -= _plan_size(pair, remaining)
            pairs.append(pair)
        groups["raw_rows"].append(record)
    for snapshot in closure.snapshots:
        append(
            "snapshots",
            {
                "owner": _owner_value(snapshot.owner),
                "rows_json": snapshot.rows_json,
                "sha256": snapshot.sha256,
            },
        )
    for role in closure.roles:
        append("roles", _spec_value(role))
    for dependency in closure.dependencies:
        append(
            "dependencies",
            {
                "owner": _owner_value(dependency.owner),
                "requires": _owner_value(dependency.requires),
                "reason": dependency.reason,
            },
        )
    return {name: records for name, records in groups.items()}


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
        _ = _integer(self.version, 1, 2)
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
        if self.version == 1:
            for snapshot in self.closure.snapshots:
                if _object_record(_load(snapshot.rows_json))["version"] != 1:
                    raise InvalidIdentity("ownership_wire")
            for row in self.closure.raw_rows:
                values = dict(row.values)
                if row.table == "file_identity_provision_intents":
                    raise InvalidIdentity("ownership_wire")
                for table, column in (
                    ("file_identity_epochs", "birth_json"),
                    ("file_identity_sets", "snapshot_json"),
                    ("file_identity_sets", "manifest_json"),
                    ("file_identity_events", "payload_json"),
                ):
                    if (
                        row.table == table
                        and _object_record(_load(values[column]))["version"] != 1
                    ):
                        raise InvalidIdentity("ownership_wire")
        _validate_closure(self.closure, self.parent, self.budget)
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
        _validate_location_witnesses(self)
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
        *,
        inventory_version: int = 1,
    ) -> OwnershipInventory:
        _ = _integer(inventory_version, 1, 2)
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
            "version": inventory_version,
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
        return cls(
            inventory_version, parent, closure, observed, unresolved, budget, digest
        )

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
            _integer(fields["version"], 1, 2),
            _path(fields["parent"]),
            _closure_from(fields["closure"]),
            tuple(records),
            tuple(_text(v) for v in _list(fields["unresolved"])),
            budget,
            _text(fields["sha256"]),
        )


def _validate_location_witnesses(inventory: OwnershipInventory) -> None:
    """Pure same-inventory validation; location is not presence or ownership."""
    dependents = [r for r in inventory.observed if r.outcome == _UNREACHABLE]
    if inventory.version != 2:
        if dependents:
            raise InvalidIdentity("witness requires inventory2")
        return
    if not inventory.observed:
        return
    cohorts = _consumers._location_cohorts(inventory.closure)
    for group in cohorts:
        absent = [
            r for r in inventory.observed if r.spec in group and r.outcome == "absent"
        ]
        for record in absent:
            if any(
                r.spec.path != record.spec.path
                and _within(record.spec.path, r.spec.path)
                for r in absent
                if r.spec.role.kind == "directory"
            ):
                raise InvalidIdentity("ordinary absence below absent witness")
    by_key = {(r.spec.owner, r.spec.role): r for r in inventory.observed}
    for record in dependents:
        fields = _constraints(record.constraints_json, record.spec)
        ancestor = _object(fields["ancestor"], {"owner", "role", "observation_sha256"})
        witness = by_key.get(
            (_owner_from(ancestor["owner"]), _role_from(ancestor["role"]))
        )
        if (
            witness is None
            or witness.outcome != "absent"
            or witness.spec.role.kind != "directory"
            or _plan_hash(_observed_value(witness)) != ancestor["observation_sha256"]
        ):
            raise InvalidIdentity("invalid location witness")
        candidates = {
            candidate.spec
            for group in cohorts
            if record.spec in group
            for candidate in inventory.observed
            if candidate.spec in group
            and candidate.outcome == "absent"
            and candidate.spec.role.kind == "directory"
            and candidate.spec.path != record.spec.path
            and _within(record.spec.path, candidate.spec.path)
        }
        if (
            not candidates
            or witness.spec not in candidates
            or len(witness.spec.path.split("/"))
            != min(len(s.path.split("/")) for s in candidates)
        ):
            raise InvalidIdentity("unsupported location witness relationship")


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
    absent_names: list[tuple[int, str, os.stat_result]] = field(default_factory=list)
    witnesses: list[_HeldPath] = field(default_factory=list)

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
        for witness in self.witnesses:
            witness._verify_self()
        self._verify_self()

    def _verify_self(self) -> None:
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
        for parent, name, before in self.absent_names:
            try:
                _ = _media.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                if _stable(before) != _stable(_media.fstat(parent)):
                    raise _ObservationFailure("changed") from None
            else:
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
                    self.absent_names.append((parent, name, anchor))
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
    *,
    _held: _HeldPath | None = None,
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
            held = _held if _held is not None else _HeldPath(guard, stack, [])
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
    *,
    inventory_version: int = 1,
) -> OwnershipInventory:
    """Bound and observe an ALREADY captured closure, without opening SQLite.

    This private layer cannot establish SQL freshness. The future SQL caller
    must independently revalidate the complete closure before using its result.
    """
    guard.verify()
    _ = _integer(inventory_version, 1, 2)
    parent = _path(parent)
    if type(closure) is not ReferenceClosure or type(budget) is not InventoryBudget:
        raise InvalidIdentity("invalid observation closure input")
    closure.__post_init__()
    budget.__post_init__()

    def refusal(code: str) -> OwnershipInventory:
        guard.verify()
        return OwnershipInventory.create(
            parent,
            ReferenceClosure((), ()),
            (),
            (code,),
            budget,
            inventory_version=inventory_version,
        )

    if len(closure.roles) > budget.max_objects:
        return refusal("role_budget_exceeded")
    if len(closure.raw_rows) > budget.max_rows:
        return refusal("evidence_budget_exceeded")
    # Retain room for fixed unresolved codes and a final bounded native handle.
    limit = _DOCUMENT_BYTES - 4096
    try:
        prototype = OwnershipInventory.create(
            parent,
            closure,
            (),
            ("evidence_budget_exceeded",),
            budget,
            inventory_version=inventory_version,
        )
        base_size = _wire_size(prototype._value())
    except InvalidIdentity:
        return refusal("evidence_budget_exceeded")
    if base_size > limit:
        return refusal("evidence_budget_exceeded")
    counter = _ObservationBudget(budget)
    if inventory_version == 2:
        return _observe_location_closure(
            guard, parent, closure, counter, base_size, limit
        )
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


def _observe_location_closure(
    guard: FileMutationGuard,
    parent: str,
    closure: ReferenceClosure,
    counter: _ObservationBudget,
    base_size: int,
    limit: int,
) -> OwnershipInventory:
    cohorts = _consumers._location_cohorts(closure)
    eligible = frozenset(r for group in cohorts for r in group)
    order = {spec: i for i, spec in enumerate(closure.roles)}
    acquisition = sorted(
        closure.roles,
        key=lambda r: (
            0 if r in eligible and r.role.kind == "directory" else 1,
            len(r.path.split("/"))
            if r in eligible and r.role.kind == "directory"
            else 0,
            order[r],
        ),
    )
    declared = frozenset(r.path for r in closure.roles)
    records: dict[RoleSpec, ObservedRole] = {}
    witnesses: list[_HeldPath] = []
    media_paths: list[_HeldPath] = []
    unresolved: set[str] = set()
    observed_size = 0
    with ExitStack() as stack:
        for spec in acquisition:
            guard.verify()
            if counter.objects >= counter.limits.max_objects:
                unresolved.add("role_budget_exceeded")
                break
            candidates = {
                r.spec: r
                for group in cohorts
                if spec in group
                for r in records.values()
                if r.spec in group
                and r.outcome == "absent"
                and r.spec.role.kind == "directory"
                and r.spec.path != spec.path
                and _within(spec.path, r.spec.path)
            }
            witness = min(
                candidates.values(),
                key=lambda r: (len(r.spec.path.split("/")), order[r.spec]),
                default=None,
            )
            fields = _initial_constraints(spec)
            if witness is not None:
                fields.update(
                    version=2,
                    boundary="verified",
                    ancestor={
                        "owner": _owner_value(witness.spec.owner),
                        "role": _role_value(witness.spec.role),
                        "observation_sha256": _plan_hash(_observed_value(witness)),
                    },
                )
            wrapper = {
                "spec": _spec_value(spec),
                "native": None,
                "constraints_json": "",
                "outcome": _UNREACHABLE
                if witness is not None
                else "native_unsupported",
            }
            remaining = (
                limit
                - base_size
                - observed_size
                - int(bool(records))
                - (_wire_size(wrapper) - 2)
            )
            if _wire_size(_encode(fields)) > remaining:
                unresolved.add("evidence_budget_exceeded")
                break
            counter.metadata = 0
            counter.metadata_limit = remaining
            held = _HeldPath(guard, stack, [], witnesses=witnesses)
            media_paths.append(held)
            if witness is not None:
                counter.object()
                try:
                    held.verify()
                    encoded = _encode(fields)
                    counter.evidence(encoded)
                    record = ObservedRole(spec, None, encoded, _UNREACHABLE)
                except (_ObservationFailure, OSError, IdentityUnavailable):
                    record = ObservedRole(
                        spec, None, _encode(_initial_constraints(spec)), "changed"
                    )
            else:
                record = _observe_role(
                    guard, spec, parent, declared, counter, _held=held
                )
                if (
                    record.outcome == "absent"
                    and spec in eligible
                    and spec.role.kind == "directory"
                ):
                    witnesses.append(held)
            size = _wire_size(_observed_value(record)) + int(bool(records))
            if size > limit - base_size - observed_size:
                unresolved.add("evidence_budget_exceeded")
                break
            records[spec] = record
            observed_size += size
        try:
            for held in witnesses:
                held._verify_self()
            for held in media_paths:
                held.verify()
            # The last counterpart can race after its witness precheck.
            for held in witnesses:
                held._verify_self()
        except (_ObservationFailure, OSError, IdentityUnavailable):
            # A cohort cannot retain earlier complete witnesses after a late race.
            for spec, record in records.items():
                records[spec] = ObservedRole(
                    spec, None, _encode(_initial_constraints(spec)), "changed"
                )
    observed = tuple(records[r] for r in closure.roles if r in records)
    unresolved.update("role_" + r.outcome for r in observed if r.unresolved)
    guard.verify()
    return OwnershipInventory.create(
        parent,
        closure,
        observed,
        tuple(sorted(unresolved)),
        counter.limits,
        inventory_version=2,
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
    # Revision 7 adds release evidence only; every ownership projection below
    # remains required. Keep this standalone adapter exact, not forward-open.
    if db.execute("PRAGMA user_version").fetchone()[0] != 7:
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
    *,
    inventory_version: int = 1,
) -> OwnershipInventory:
    """Observe current ownership references, with no recovery/mutation authority.

    The trusted local config and guard must stay in place. Fresh complete SQL
    recapture detects changed evidence, not every intermediate ABA or a global
    atomic FS/SQL state. Every owned SQL cursor/connection closes before media.
    """
    if not isinstance(guard, FileMutationGuard):
        raise FileMutationLockError("expected a live filesystem mutation guard")
    guard.verify()
    _ = _integer(inventory_version, 1, 2)
    parent = _path(parent)
    if type(budget) is not InventoryBudget:
        raise InvalidIdentity("expected inventory budget")
    budget.__post_init__()
    try:
        try:
            closure = _capture_sql(guard, parent, budget)
        except _SQLFailure as exc:
            return OwnershipInventory.create(
                parent,
                ReferenceClosure((), ()),
                (),
                (exc.code,),
                budget,
                inventory_version=inventory_version,
            )
        result = (
            _observe_closure(guard, parent, closure, budget)
            if inventory_version == 1
            else _observe_closure(guard, parent, closure, budget, inventory_version=2)
        )
        unresolved = set(result.unresolved)
        try:
            if _capture_sql(guard, parent, budget) != closure:
                unresolved.add("sql_changed")
        except _SQLFailure as exc:
            unresolved.add(exc.code)
        return OwnershipInventory.create(
            parent,
            result.closure,
            result.observed,
            tuple(sorted(unresolved)),
            budget,
            inventory_version=inventory_version,
        )
    finally:
        guard.verify()
