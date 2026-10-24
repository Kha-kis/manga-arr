"""Synthetic schema8-shaped wire evidence, NOT a Task8 producer or native proof."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, replace
from typing import cast

import pytest

import file_identity_consumers as consumers
import file_identity_recovery as recovery
import file_identity_store as store
import test_file_identity_plan as plan_fixtures
from file_identity import InvalidIdentity, NativeHandle, OwnerRef, RoleKey
from file_identity_consumers import InventoryBudget, RawRow
from test_file_identity_observation import (
    ProducerMedia,
    producer_media as producer_media,
    producer_pack,
)
from test_file_identity_plan import (
    capture_inventory,
    chosen,
    digest,
    domain_external_boundaries,
    encoded,
    linked_documents,
    parse_document,
    provision_documents,
    rescan_acknowledged_fixture,
    resign_documents,
    review_documents,
    transferred_plan_conservation,
    wire_array,
    wire_object,
)
from test_file_identity_store import object_value
from test_file_identity_schema import ledger_path as ledger_path

EID = "e" * 32
IID = "1" * 32
SID = "2" * 32
CONTRACT = hashlib.sha256(
    b"mangarr/allocation-consumer-wire/2\n"
    b"intent1->birth2->snapshot2/manifest2->prepared1->activated1->grant2\n"
    b"namespace(allocation_path);latest-owner-chain;no-regrant\n"
).hexdigest()


@dataclass(frozen=True)
class SyntheticGraph:
    prior: recovery.RecoveryPlan
    owner: OwnerRef
    rows: tuple[dict[str, object], ...]
    snapshot_json: str
    birth_json: str
    objects: tuple[store.ObjectProof, ...]


def raw(table: str, row: dict[str, object]) -> RawRow:
    pk = {
        "file_identity_provision_intents": "intent_id",
        "file_identity_authorities": "authority_id",
        "file_identity_epochs": "epoch_id",
        "file_identity_sets": "set_id",
        "file_identity_events": "id",
        "file_claim_namespaces": "parent_path",
    }[table]
    key = row[pk]
    assert type(key) in (str, int)
    return RawRow(
        table,
        (cast(str | int, key),),
        tuple(sorted(cast(dict[str, consumers.SQLScalar], row).items())),
    )


def synthetic_intent(
    env: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> SyntheticGraph:
    """Real old producer preimages; all successor rows/observations are synthetic."""
    _ = rescan_acknowledged_fixture(env, monkeypatch, False)
    initial = capture_inventory(env, monkeypatch)
    inputs = linked_documents(provision_documents(initial), [])
    prior = recovery.build_recovery_plan(initial, *inputs)
    declaration = parse_document(prior.declaration_json)
    authority = wire_object(wire_array(declaration["authorities"])[0])
    intent = {
        "version": 1,
        "kind": "epoch-provision-intent",
        "plan_json": prior.to_json(),
        "plan_sha256": prior.sha256,
        "inventory_sha256": prior.inventory.sha256,
        "epoch_id": EID,
        "authority_id": "a" * 32,
        "authority_incarnation": "b" * 32,
        "scope": authority["scope"],
        "profile": authority["profile"],
        "origin_path": str(env.library),
        "allocation_path": str(env.library / (".mangarr-epoch-" + EID)),
        "root_path": str(env.library / (".mangarr-epoch-" + EID) / ".mangarr-claims"),
        "namespace_owner": {
            "domain": "namespace",
            "key": [str(env.library / (".mangarr-epoch-" + EID))],
        },
        "allocation_token": IID,
    }
    owner = OwnerRef("namespace", (cast(str, intent["allocation_path"]),))
    old = next(s for s in initial.closure.snapshots if s.owner.domain == "namespace")
    old_root = wire_object(wire_object(parse_document(old.rows_json)["root"])["row"])
    legacy = parse_document(cast(str, old_root["ownership_json"]))
    legacy.update(owner_token=IID, root_path=intent["root_path"])
    legacy.update(parent_ino=123, root_ino=123)
    legacy["marker_fingerprint"] = {
        "dev": 1,
        "inode": 123,
        "size": 12,
        "mtime_ns": 42,
        "sha256": "9" * 64,
    }
    legacy["creation_boundary"] = {
        "kind": "exclusive_parent",
        "parent_uid": 1000,
        "parent_mode": 0o700,
    }
    # Existing namespace2 registry grammar, including its real receipt strings.
    assert legacy["version"] == 2
    legacy_json = encoded(legacy)
    paths = {
        "epoch_origin_parent": (
            cast(str, intent["origin_path"]),
            None,
            None,
            "parent_identity",
        ),
        "namespace_parent": (
            cast(str, intent["allocation_path"]),
            None,
            None,
            "exclusive_parent",
        ),
        "namespace_root": (
            cast(str, intent["root_path"]),
            "namespace_parent",
            ".mangarr-claims",
            "private_directory",
        ),
        "namespace_marker": (
            cast(str, intent["root_path"]) + "/owner.json",
            "namespace_root",
            "owner.json",
            "marker",
        ),
    }
    keys = {
        name: RoleKey(
            None, name, "marker" if name.endswith("marker") else "directory", relative
        )
        for name, (_, _, relative, _) in paths.items()
    }
    objects: list[store.ObjectProof] = []
    for name, (path, parent, _, policy) in paths.items():
        fields: dict[str, object] = {
            key: None
            for key in ("inode", "size", "mtime_ns", "sha256", "uid", "mode", "nlink")
        }
        fields["inode"] = 123
        if policy != "parent_identity":
            fields.update(uid=1000, mode=0o600 if policy == "marker" else 0o700)
        if policy == "marker":
            fields.update(size=12, mtime_ns=42, sha256="9" * 64, nlink=1)
        constraints = encoded(
            {
                "version": 1,
                "legacy": None
                if name == "epoch_origin_parent"
                else {
                    "kind": "namespace_v2",
                    "json": legacy_json,
                    "sha256": hashlib.sha256(legacy_json.encode()).hexdigest(),
                },
                "binding": {
                    "owner": recovery._owner_value(owner),
                    "role": recovery._role_value(keys[name]),
                    "operation_key": EID,
                    "owner_token": IID,
                },
                "policy": policy,
                "non_device": fields,
            }
        )
        objects.append(
            store.ObjectProof(
                keys[name],
                path,
                keys[parent] if parent else None,
                NativeHandle(1, ("synthetic-" + name).encode()),
                constraints,
            )
        )
    objects.sort(key=lambda o: encoded(recovery._role_value(o.role)))
    controlled: dict[str, object] = {
        "version": 2,
        "authority_incarnation": "b" * 32,
        "allocation_token": IID,
        "namespace_owner": recovery._owner_value(owner),
        "intent_id": IID,
        "plan_sha256": prior.sha256,
        "epoch_id": EID,
        "authority_id": "a" * 32,
        "origin_path": intent["origin_path"],
        "allocation_path": intent["allocation_path"],
        "root_path": intent["root_path"],
        "objects": [object_value(o) for o in objects],
    }
    controlled["receipt_sha256"] = digest(controlled)
    birth_json = encoded(
        {
            "version": 2,
            "layout": "dedicated_parent",
            "enrollment": "birth",
            "legacy_namespace": None,
            "controlled_birth": controlled,
        }
    )
    epoch: dict[str, object] = {
        "epoch_id": EID,
        "authority_id": "a" * 32,
        "origin_path": intent["origin_path"],
        "allocation_path": intent["allocation_path"],
        "root_path": intent["root_path"],
        "birth_json": birth_json,
    }
    intent_row: dict[str, object] = {
        "intent_id": IID,
        "authority_id": "a" * 32,
        "payload_json": encoded(intent),
        "payload_sha256": digest(intent),
    }
    registry: dict[str, object] = {
        "parent_path": intent["allocation_path"],
        "ownership_json": legacy_json,
    }
    snapshot_json = encoded(
        {
            "version": 2,
            "owner": recovery._owner_value(owner),
            "root": {
                "table": "file_claim_namespaces",
                "key": list(owner.key),
                "row": registry,
            },
            "related": [
                {"table": "file_identity_epochs", "key": [EID], "row": epoch},
                {
                    "table": "file_identity_provision_intents",
                    "key": [IID],
                    "row": intent_row,
                },
            ],
        }
    )
    snapshots = sorted(
        initial.closure.snapshots, key=lambda s: encoded(recovery._owner_value(s.owner))
    )
    manifest: dict[str, object] = {
        "version": 2,
        "snapshot_sha256": hashlib.sha256(snapshot_json.encode()).hexdigest(),
        "phase": "epoch-provisioned",
        "required_roles": [recovery._role_value(o.role) for o in objects],
        "roles": [object_value(o) for o in objects],
        "absent_roles": [],
        "required_dependencies": [recovery._owner_value(s.owner) for s in snapshots],
        "dependencies": [
            {"snapshot_json": s.rows_json, "sha256": s.sha256, "requires": []}
            for s in snapshots
        ],
        "intent": {
            "kind": "epoch-provision",
            "snapshot_sha256": hashlib.sha256(snapshot_json.encode()).hexdigest(),
            "intent_id": IID,
            "plan_sha256": prior.sha256,
        },
    }
    set_row: dict[str, object] = {
        "set_id": SID,
        "owner_domain": "namespace",
        "owner_key_json": encoded({"version": 1, "key": list(owner.key)}),
        "prior_set_id": None,
        "snapshot_json": snapshot_json,
        "snapshot_sha256": manifest["snapshot_sha256"],
        "origin": "birth",
        "epoch_id": EID,
        "manifest_json": encoded(manifest),
        "manifest_sha256": digest(manifest),
    }
    payload: dict[str, object] = {
        "version": 2,
        "kind": "allocation-capability",
        "before_snapshot_json": snapshot_json,
        "before_snapshot_sha256": manifest["snapshot_sha256"],
        "plan_sha256": prior.sha256,
        "intent_id": IID,
        "activation_event_id": 2,
        "epoch_id": EID,
        "authority_id": "a" * 32,
        "authority_incarnation": "b" * 32,
        "origin_path": intent["origin_path"],
        "allocation_path": intent["allocation_path"],
        "root_path": intent["root_path"],
        "birth_sha256": digest(parse_document(birth_json)),
        "state": "grant",
        "prior_capability_event_id": None,
        "consumer_contract_sha256": CONTRACT,
    }
    events: list[dict[str, object]] = [
        {
            "id": i,
            "owner_domain": "namespace",
            "owner_key_json": set_row["owner_key_json"],
            "revision": i,
            "predecessor_event_id": i - 1 if i > 1 else None,
            "set_id": SID,
            "kind": kind,
            "payload_json": encoded(
                payload
                if i == 3
                else {
                    "version": 1,
                    "snapshot_sha256": manifest["snapshot_sha256"],
                    "manifest_sha256": digest(manifest),
                }
            ),
        }
        for i, kind in enumerate(("prepared", "activated", "recovery_step"), 1)
    ]
    authority_row: dict[str, object] = {
        "authority_id": "a" * 32,
        "incarnation_token": "b" * 32,
        "profile_json": encoded(authority["profile"]),
        "declaration_json": encoded(authority["scope"]),
        "qualification_sha256": "d" * 64,
    }
    rows: tuple[dict[str, object], ...] = tuple(
        {"table": table, "row": row}
        for table, row in (
            ("file_identity_authorities", authority_row),
            ("file_identity_epochs", epoch),
            ("file_identity_provision_intents", intent_row),
            ("file_claim_namespaces", registry),
            ("file_identity_sets", set_row),
            *(("file_identity_events", event) for event in events),
        )
    )
    return SyntheticGraph(prior, owner, rows, snapshot_json, birth_json, tuple(objects))


def graph_inventory(graph: SyntheticGraph) -> recovery.OwnershipInventory:
    by_table: dict[str, list[RawRow]] = {name: [] for name in consumers._TABLES}
    by_table["file_identity_provision_intents"] = []
    for row in graph.prior.inventory.closure.raw_rows:
        by_table[row.table].append(row)
    for item in graph.rows:
        table = cast(str, item["table"])
        by_table[table].append(raw(table, wire_object(item["row"])))
    closure = consumers._bind_references(
        by_table, graph.prior.inventory.parent, InventoryBudget()
    )
    prior = {(o.spec.owner, o.spec.role): o for o in graph.prior.inventory.observed}
    objects = {o.role: o for o in graph.objects}
    observed: list[recovery.ObservedRole] = []
    for spec in closure.roles:
        if spec.owner != graph.owner:
            observed.append(prior[(spec.owner, spec.role)])
            continue
        obj = objects[spec.role]
        fields = wire_object(parse_document(obj.constraints_json)["non_device"])
        directory = spec.role.kind == "directory"
        current = {
            "dev": 1,
            "inode": fields["inode"],
            "uid": fields["uid"] if fields["uid"] is not None else 1000,
            "mode": fields["mode"] if fields["mode"] is not None else 0o755,
            "nlink": None if directory else 1,
            "size": None if directory else fields["size"],
            "mtime_ns": None if directory else fields["mtime_ns"],
            "ctime_ns": None if directory else 42,
        }
        constraints: dict[str, object] = {
            "version": 1,
            "kind": "readonly_observation",
            "current": current,
            "content": None
            if directory
            else {"size": fields["size"], "sha256": fields["sha256"]},
            "expected": "unverified_legacy"
            if spec.legacy_json is not None
            else "undeclared",
            "policy": "satisfied",
            "boundary": "verified",
            "children": [],
        }
        observed.append(
            recovery.ObservedRole(spec, obj.native, encoded(constraints), "observed")
        )
    return recovery.OwnershipInventory.create(
        graph.prior.inventory.parent,
        closure,
        tuple(observed),
        (),
        InventoryBudget(),
        inventory_version=2,
    )


def follow_on(
    graph: SyntheticGraph, inventory: recovery.OwnershipInventory, *, rollback: bool
) -> tuple[str, str, str]:
    owner = next(
        s.owner for s in inventory.closure.snapshots if s.owner.domain == "rescan"
    )
    decisions = (
        chosen(
            inventory,
            owner,
            "restore-original",
            {"source_artifact", "destination"},
            next(
                r.path
                for r in inventory.closure.roles
                if r.owner == owner and r.role.purpose == "source"
            ),
        )
        if rollback
        else None
    )
    selection, declaration, receipt = (
        parse_document(t)
        for t in linked_documents(review_documents(inventory, decisions), [])
    )
    declaration["authorities"] = parse_document(graph.prior.declaration_json)[
        "authorities"
    ]
    for item in wire_array(declaration["owner_reviews"]):
        wire_object(item)["intended_original_authority_id"] = "a" * 32
    for item in wire_array(declaration["role_reviews"]):
        wire_object(item)["authority_id"] = "a" * 32
    epoch = wire_object(selection["epoch_bindings"])
    epoch["existing"] = [
        wire_object(i["row"])
        for i in graph.rows
        if i["table"] == "file_identity_epochs"
    ]
    for item in wire_array(epoch["role_bindings"]):
        binding = wire_object(item)
        binding["authority_id"] = "a" * 32
        if binding["owner"] == recovery._owner_value(graph.owner):
            binding["epoch_id"] = EID
    if rollback:
        roles = {
            r.role.purpose: r.role for r in inventory.closure.roles if r.owner == owner
        }
        epoch["rollback_outputs"] = [
            {
                "version": 1,
                "owner": recovery._owner_value(owner),
                "original_role": recovery._role_value(roles["source_artifact"]),
                "publication_role": recovery._role_value(roles["destination"]),
                "epoch_id": EID,
                "carrier_token": "f" * 32,
                "handling": "retain-non-replayable",
            }
        ]
        event = next(
            raw("file_identity_events", wire_object(i["row"]))
            for i in graph.rows
            if i["table"] == "file_identity_events" and wire_object(i["row"])["id"] == 3
        )
        epoch["allocation_capabilities"] = [
            {
                "epoch_id": EID,
                "owner": recovery._owner_value(graph.owner),
                "set_id": SID,
                "event_id": 3,
                "event_sha256": digest(
                    {
                        "table": event.table,
                        "key": list(event.key),
                        "values": [list(pair) for pair in event.values],
                    }
                ),
                "birth_sha256": digest(parse_document(graph.birth_json)),
            }
        ]
    return resign_documents(selection, declaration, receipt)


def test_successor_complete_snapshot_decoder(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = synthetic_intent(producer_media, monkeypatch)
    snapshot = store.OwnerSnapshot(
        graph.owner,
        graph.snapshot_json,
        hashlib.sha256(graph.snapshot_json.encode()).hexdigest(),
    )
    specs = consumers.roles_for(snapshot)
    assert len(specs) == 4
    assert {s.role.purpose for s in specs} == {
        "epoch_origin_parent",
        "namespace_parent",
        "namespace_root",
        "namespace_marker",
    }
    assert len({s.path for s in specs}) == 4


@pytest.mark.parametrize("rollback", [False, True])
def test_complete_synthetic_provision_graph_supports_plan2(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, rollback: bool
) -> None:
    graph = synthetic_intent(producer_media, monkeypatch)
    inventory = graph_inventory(graph)
    inputs = follow_on(graph, inventory, rollback=rollback)
    before = graph.prior.to_json(), inventory.to_json()
    with transferred_plan_conservation(producer_media, monkeypatch, inventory):
        plan = recovery.build_recovery_plan(inventory, *inputs)
        assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan
        assert replace(plan) == plan
        assert recovery.build_recovery_plan(inventory, *inputs) == plan
    assert plan.version == 2 and plan.inventory.version == 2
    assert graph.owner == OwnerRef(
        "namespace", (str(producer_media.library / (".mangarr-epoch-" + EID)),)
    )
    assert (
        len(graph.objects) == 4 and len({o.canonical_path for o in graph.objects}) == 4
    )
    assert (graph.prior.to_json(), inventory.to_json()) == before


def event_graph(graph: SyntheticGraph, change: str) -> SyntheticGraph:
    rows = [parse_document(encoded(i)) for i in graph.rows]
    events = [
        wire_object(i["row"]) for i in rows if i["table"] == "file_identity_events"
    ]
    grant = events[-1]
    payload = parse_document(cast(str, grant["payload_json"]))
    if change in {"prepared-only", "active-only"}:
        ceiling = 1 if change == "prepared-only" else 2
        rows = [
            i
            for i in rows
            if i["table"] != "file_identity_events"
            or cast(int, wire_object(i["row"])["id"]) <= ceiling
        ]
    elif change == "activation-id":
        payload["activation_event_id"] = 1
    elif change == "borrowed-generation":
        payload["epoch_id"] = "7" * 32
    elif change == "authority":
        payload["authority_incarnation"] = "7" * 32
    elif change == "contract":
        payload["consumer_contract_sha256"] = "7" * 64
    elif change == "bool-activation":
        payload["activation_event_id"] = True
    elif change == "birth-digest":
        payload["birth_sha256"] = "7" * 64
    elif change == "changed-predecessor":
        grant["predecessor_event_id"] = 1
    elif change == "missing-prepared":
        events[0]["kind"] = "retained"
        events[0]["payload_json"] = encoded(
            {
                "version": 1,
                "before_snapshot_json": graph.snapshot_json,
                "before_snapshot_sha256": hashlib.sha256(
                    graph.snapshot_json.encode()
                ).hexdigest(),
                "plan_sha256": "7" * 64,
            }
        )
    elif change in {"intervening", "repeated", "revoked", "regrant"}:
        extra = dict(grant)
        extra.update(id=4, revision=4, predecessor_event_id=3)
        if change == "intervening":
            extra.update(
                kind="retained",
                payload_json=encoded(
                    {
                        "version": 1,
                        "before_snapshot_json": graph.snapshot_json,
                        "before_snapshot_sha256": hashlib.sha256(
                            graph.snapshot_json.encode()
                        ).hexdigest(),
                        "plan_sha256": "7" * 64,
                    }
                ),
            )
        elif change in {"revoked", "regrant"}:
            revoked = dict(payload)
            revoked.update(
                state="revoke", prior_capability_event_id=3, plan_sha256="7" * 64
            )
            extra["payload_json"] = encoded(revoked)
        rows.append({"table": "file_identity_events", "row": extra})
        if change == "regrant":
            regrant = dict(grant)
            regrant.update(id=5, revision=5, predecessor_event_id=4)
            rows.append({"table": "file_identity_events", "row": regrant})
    grant["payload_json"] = encoded(payload)
    return replace(graph, rows=tuple(rows))


@pytest.mark.parametrize(
    "change,code",
    [
        ("prepared-only", "ownership_binding"),
        ("active-only", "ownership_binding"),
        ("activation-id", "ownership_phase"),
        ("borrowed-generation", "ownership_binding"),
        ("authority", "ownership_authority"),
        ("contract", "ownership_phase"),
        ("bool-activation", "ownership_wire"),
        ("birth-digest", "ownership_binding"),
        ("intervening", "ownership_phase"),
        ("repeated", "ownership_phase"),
        ("revoked", "ownership_phase"),
        ("regrant", "ownership_phase"),
        ("changed-predecessor", "ownership_phase"),
        ("missing-prepared", "ownership_phase"),
    ],
)
def test_capability_requires_exact_committed_activation_and_latest_grant(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    code: str,
) -> None:
    graph = synthetic_intent(producer_media, monkeypatch)
    changed = event_graph(graph, change)
    with domain_external_boundaries(monkeypatch):
        with pytest.raises(InvalidIdentity, match="^" + code + "$"):
            inventory = graph_inventory(changed)
            # A missing event cannot be invented by rebuilding the reference.
            inputs = follow_on(
                graph if change.endswith("only") else changed, inventory, rollback=True
            )
            _ = recovery.build_recovery_plan(inventory, *inputs)


def test_capability_reference_all_entry_points_and_full_raw_event_digest(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = synthetic_intent(producer_media, monkeypatch)
    inventory = graph_inventory(graph)
    inputs = follow_on(graph, inventory, rollback=True)
    plan = recovery.build_recovery_plan(inventory, *inputs)
    selection, declaration, receipt = (parse_document(t) for t in inputs)
    reference = wire_object(
        wire_array(wire_object(selection["epoch_bindings"])["allocation_capabilities"])[
            0
        ]
    )
    event = next(
        wire_object(i["row"])
        for i in graph.rows
        if i["table"] == "file_identity_events" and wire_object(i["row"])["id"] == 3
    )
    reference["event_sha256"] = digest(parse_document(cast(str, event["payload_json"])))
    bad_inputs = resign_documents(selection, declaration, receipt)
    value = parse_document(plan.to_json())
    value.update(
        declaration_json=bad_inputs[1],
        backup_receipt_json=bad_inputs[2],
        epoch_bindings_json=encoded(selection["epoch_bindings"]),
    )
    del value["sha256"]
    value["sha256"] = digest(value)
    with transferred_plan_conservation(producer_media, monkeypatch, inventory):
        with pytest.raises(InvalidIdentity, match="^ownership_binding$"):
            _ = recovery.build_recovery_plan(inventory, *bad_inputs)
        with pytest.raises(InvalidIdentity, match="^ownership_binding$"):
            _ = recovery.RecoveryPlan.from_json(encoded(value))
        with pytest.raises(InvalidIdentity, match="^ownership_binding$"):
            _ = replace(
                plan,
                declaration_json=bad_inputs[1],
                backup_receipt_json=bad_inputs[2],
                epoch_bindings_json=cast(str, value["epoch_bindings_json"]),
                sha256=cast(str, value["sha256"]),
            )


def test_inventory1_rejects_successor_evidence_on_every_entry_even_retained(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = synthetic_intent(producer_media, monkeypatch)
    inventory = graph_inventory(graph)
    value = parse_document(inventory.to_json())
    value["version"] = 1
    del value["sha256"]
    value["sha256"] = digest(value)
    with domain_external_boundaries(monkeypatch):
        for construct in (
            lambda: recovery.OwnershipInventory.create(
                inventory.parent,
                inventory.closure,
                inventory.observed,
                (),
                inventory.budget,
            ),
            lambda: replace(inventory, version=1, sha256=cast(str, value["sha256"])),
            lambda: recovery.OwnershipInventory.from_json(encoded(value)),
        ):
            with pytest.raises(InvalidIdentity, match="^ownership_wire$"):
                _ = construct()
    assert "file_identity_provision_intents" not in consumers._TABLES
    assert "file_identity_provision_intents" not in store._PROJECTIONS


def test_successor_wire_rejects_resigned_versions_and_budget_overflow(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = synthetic_intent(producer_media, monkeypatch)
    intent_row = next(
        wire_object(i["row"])
        for i in graph.rows
        if i["table"] == "file_identity_provision_intents"
    )
    intent = parse_document(cast(str, intent_row["payload_json"]))
    with domain_external_boundaries(monkeypatch):
        assert store._provision_intent(encoded(intent)) == intent
        for field, bad, code in (
            ("version", True, "ownership_wire"),
            ("version", 2, "ownership_wire"),
            ("allocation_token", "opaque label", "ownership_wire"),
            ("origin_path", "/elsewhere", "ownership_binding"),
            (
                "namespace_owner",
                {"domain": "namespace", "key": [str(producer_media.library)]},
                "ownership_binding",
            ),
            ("authority_incarnation", "7" * 32, "ownership_authority"),
            ("plan_json", cast(str, intent["plan_json"]) + " ", "ownership_wire"),
            ("inventory_sha256", "7" * 64, "ownership_binding"),
            ("root_path", "/bad\x00", "ownership_wire"),
            ("root_path", "/bad\ud800", "ownership_wire"),
        ):
            changed = dict(intent)
            changed[field] = bad
            with pytest.raises(InvalidIdentity, match="^" + code + "$"):
                _ = store._provision_intent(encoded(changed))
        # Re-sign a structurally invalid prior plan: hashes are not schema checks.
        prior = parse_document(cast(str, intent["plan_json"]))
        wire_object(prior["inventory"])["budget"] = {
            "max_rows": True,
            "max_objects": 10000,
            "max_hash_bytes": 10000,
        }
        inv = wire_object(prior["inventory"])
        del inv["sha256"]
        inv["sha256"] = digest(inv)
        prior["sha256"] = digest({k: v for k, v in prior.items() if k != "sha256"})
        changed = dict(intent)
        changed.update(
            plan_json=encoded(prior),
            plan_sha256=prior["sha256"],
            inventory_sha256=inv["sha256"],
        )
        with pytest.raises(InvalidIdentity, match="^ownership_wire$"):
            _ = store._provision_intent(encoded(changed))
        oversized = dict(intent)
        oversized["plan_json"] = "\\" * (16 * 1024**2 // 2)
        with pytest.raises(InvalidIdentity, match="^ownership_budget$"):
            _ = store._provision_intent(encoded(oversized))
        at_depth: object = None
        for _ in range(32):
            at_depth = [at_depth]
        assert store._successor_size(at_depth) == 68
        with pytest.raises(InvalidIdentity, match="^ownership_budget$"):
            _ = store._successor_size([at_depth])
        with pytest.raises(InvalidIdentity, match="^ownership_budget$"):
            _ = store._successor_size([None] * 10001)
        assert store._successor_size("\\" * ((16 * 1024**2 - 2) // 2)) == 16 * 1024**2
        with pytest.raises(InvalidIdentity, match="^ownership_budget$"):
            _ = store._successor_size("\\" * (16 * 1024**2 // 2))


def test_birth_receipt_cannot_rebind_registry_uid_or_marker_evidence(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = synthetic_intent(producer_media, monkeypatch)
    for change in ("uid", "marker-hash"):
        birth = parse_document(graph.birth_json)
        receipt = wire_object(birth["controlled_birth"])
        for value in wire_array(receipt["objects"]):
            obj = wire_object(value)
            constraints = parse_document(cast(str, obj["constraints_json"]))
            policy = constraints["policy"]
            if change == "uid" and policy != "parent_identity":
                wire_object(constraints["non_device"])["uid"] = 99
            elif change == "marker-hash" and policy == "marker":
                wire_object(constraints["non_device"])["sha256"] = "8" * 64
            obj["constraints_json"] = encoded(constraints)
        receipt["receipt_sha256"] = digest(
            {k: v for k, v in receipt.items() if k != "receipt_sha256"}
        )
        with pytest.raises(InvalidIdentity, match="^ownership_binding$"):
            _ = store._epoch_birth(encoded(birth))


def test_intent_only_history_is_inventory2_evidence_not_birth_or_permission(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = synthetic_intent(producer_media, monkeypatch)
    history = replace(
        graph,
        rows=tuple(
            i
            for i in graph.rows
            if i["table"]
            in {"file_identity_authorities", "file_identity_provision_intents"}
        ),
    )
    inventory = graph_inventory(history)
    assert inventory.version == 2
    assert {r.table for r in inventory.closure.raw_rows} >= {
        "file_identity_provision_intents",
        "file_identity_authorities",
    }
    assert inventory.closure.roles == graph.prior.inventory.closure.roles
    inputs = linked_documents(review_documents(inventory), [])
    with transferred_plan_conservation(producer_media, monkeypatch, inventory):
        plan = recovery.build_recovery_plan(inventory, *inputs)
        assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan
        with pytest.raises(InvalidIdentity, match="^ownership_wire$"):
            _ = replace(inventory, version=1)


def test_snapshot2_manifest2_and_birth_are_exact_closed_projections(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = synthetic_intent(producer_media, monkeypatch)
    snapshot = store.OwnerSnapshot(
        graph.owner,
        graph.snapshot_json,
        hashlib.sha256(graph.snapshot_json.encode()).hexdigest(),
    )
    set_row = next(
        wire_object(i["row"]) for i in graph.rows if i["table"] == "file_identity_sets"
    )
    manifest = parse_document(cast(str, set_row["manifest_json"]))
    proof = store.ProofSet(
        SID,
        graph.owner,
        snapshot,
        "a" * 32,
        EID,
        "birth",
        graph.objects,
        encoded(manifest),
    )
    for change in ("missing", "duplicate", "reordered", "foreign", "version", "extra"):
        value = parse_document(graph.snapshot_json)
        related = wire_array(value["related"])
        if change == "missing":
            related.pop()
        elif change == "duplicate":
            related.append(related[0])
        elif change == "reordered":
            related.reverse()
        elif change == "foreign":
            wire_object(wire_object(related[1])["row"])["authority_id"] = "7" * 32
        elif change == "version":
            value["version"] = 1
        else:
            value["extra"] = None
        text = encoded(value)
        with pytest.raises(InvalidIdentity):
            _ = store.OwnerSnapshot(
                graph.owner, text, hashlib.sha256(text.encode()).hexdigest()
            )
    for change in (
        "missing-dependency",
        "missing-object",
        "reordered",
        "absent",
        "wrong-intent",
        "snapshot1",
        "version-bool",
    ):
        value = parse_document(proof.manifest_json)
        objects = graph.objects
        if change == "missing-dependency":
            wire_array(value["required_dependencies"]).clear()
            wire_array(value["dependencies"]).clear()
        elif change == "missing-object":
            wire_array(value["roles"]).pop()
            wire_array(value["required_roles"]).pop()
            objects = objects[:-1]
        elif change == "reordered":
            wire_array(value["roles"]).reverse()
            objects = tuple(reversed(objects))
        elif change == "absent":
            value["absent_roles"] = [
                {
                    "role": recovery._role_value(objects[0].role),
                    "canonical_path": objects[0].canonical_path,
                    "parent_role": None,
                    "intent_sha256": digest(value["intent"]),
                }
            ]
        elif change == "wrong-intent":
            wire_object(value["intent"])["intent_id"] = "7" * 32
        elif change == "snapshot1":
            value["version"] = 1
        else:
            value["version"] = True
        with pytest.raises(InvalidIdentity):
            _ = replace(proof, roles=objects, manifest_json=encoded(value))
    for change in (
        "duplicate",
        "missing",
        "reordered",
        "origin-legacy",
        "mode",
        "token",
        "operation",
        "parent",
    ):
        value = parse_document(graph.birth_json)
        receipt = wire_object(value["controlled_birth"])
        objects = wire_array(receipt["objects"])
        first = wire_object(objects[0])
        constraints = parse_document(cast(str, first["constraints_json"]))
        if change == "duplicate":
            objects.append(first)
        elif change == "missing":
            objects.pop()
        elif change == "reordered":
            objects.reverse()
        elif change == "origin-legacy":
            registry = next(
                o for o in graph.objects if o.role.purpose == "namespace_root"
            )
            constraints["legacy"] = parse_document(registry.constraints_json)["legacy"]
        elif change == "mode":
            marker = wire_object(
                next(
                    o
                    for o in objects
                    if wire_object(wire_object(o)["role"])["purpose"]
                    == "namespace_marker"
                )
            )
            marker_constraints = parse_document(cast(str, marker["constraints_json"]))
            wire_object(marker_constraints["non_device"])["mode"] = 0o644
            marker["constraints_json"] = encoded(marker_constraints)
        elif change == "token":
            wire_object(constraints["binding"])["owner_token"] = "7" * 32
        elif change == "operation":
            wire_object(constraints["binding"])["operation_key"] = (
                "unrelated-opaque-operation"
            )
        else:
            first["parent_role"] = recovery._role_value(
                RoleKey(None, "namespace_parent", "directory", None)
            )
        first["constraints_json"] = encoded(constraints)
        receipt["receipt_sha256"] = digest(
            {k: v for k, v in receipt.items() if k != "receipt_sha256"}
        )
        with pytest.raises(InvalidIdentity):
            _ = store._epoch_birth(encoded(value))


def test_inventory1_refuses_each_new_raw_history_shape_before_reconstruction(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = synthetic_intent(producer_media, monkeypatch)
    base = graph.prior.inventory
    for item in graph.rows:
        if item["table"] not in {
            "file_identity_provision_intents",
            "file_identity_epochs",
            "file_identity_sets",
            "file_identity_events",
        }:
            continue
        record = raw(cast(str, item["table"]), wire_object(item["row"]))
        if record.table == "file_identity_events" and dict(record.values)["id"] != 3:
            continue
        closure = replace(
            base.closure,
            raw_rows=tuple(
                sorted((*base.closure.raw_rows, record), key=consumers._raw_order)
            ),
        )
        with pytest.raises(InvalidIdentity, match="^ownership_wire$"):
            _ = recovery.OwnershipInventory.create(
                base.parent, closure, base.observed, (), base.budget
            )


def test_grant_cannot_borrow_a_re_signed_birth_from_outside_activated_snapshot(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = synthetic_intent(producer_media, monkeypatch)
    rows = [parse_document(encoded(i)) for i in graph.rows]
    epoch = next(
        wire_object(i["row"]) for i in rows if i["table"] == "file_identity_epochs"
    )
    birth = parse_document(cast(str, epoch["birth_json"]))
    receipt = wire_object(birth["controlled_birth"])
    objects: list[store.ObjectProof] = []
    for value in wire_array(receipt["objects"]):
        obj = wire_object(value)
        obj["native"] = NativeHandle(1, b"synthetic-borrowed-birth").to_value()
        objects.append(store._object_from(obj))
    receipt["receipt_sha256"] = digest(
        {k: v for k, v in receipt.items() if k != "receipt_sha256"}
    )
    epoch["birth_json"] = encoded(birth)
    grant = next(
        wire_object(i["row"])
        for i in rows
        if i["table"] == "file_identity_events" and wire_object(i["row"])["id"] == 3
    )
    payload = parse_document(cast(str, grant["payload_json"]))
    payload["birth_sha256"] = digest(birth)
    grant["payload_json"] = encoded(payload)
    changed = replace(
        graph, rows=tuple(rows), birth_json=encoded(birth), objects=tuple(objects)
    )
    inventory = graph_inventory(changed)
    inputs = follow_on(changed, inventory, rollback=True)
    with transferred_plan_conservation(producer_media, monkeypatch, inventory):
        with pytest.raises(InvalidIdentity, match="^ownership_binding$"):
            _ = recovery.build_recovery_plan(inventory, *inputs)


def test_manifest1_dependencies_never_gain_snapshot2_grammar(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = synthetic_intent(producer_media, monkeypatch)
    old = graph.prior.inventory.closure.snapshots[0].owner
    manifest: dict[str, object] = {
        "version": 1,
        "required_dependencies": [recovery._owner_value(graph.owner)],
        "dependencies": [
            {
                "snapshot_json": graph.snapshot_json,
                "sha256": hashlib.sha256(graph.snapshot_json.encode()).hexdigest(),
                "requires": [],
            }
        ],
    }
    with pytest.raises(InvalidIdentity, match="^ownership_wire$"):
        _ = store._dependencies(manifest, old)


@pytest.mark.parametrize("text", ["\x7f", "\u00e9", "\U0001f600", '\n\\"'])
def test_successor_precharge_matches_complete_escaped_encoding(text: str) -> None:
    import json

    assert store._successor_size(text) == len(json.dumps(text))


@pytest.mark.parametrize("family", ["frozen", "transferred"])
def test_intent_preserves_eligible_opaque_pack_label(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    family: str,
) -> None:
    label = "legacy-pack-A"

    def opaque_label(_size: int) -> str:
        return label

    with monkeypatch.context() as patch:
        patch.setattr(secrets, "token_urlsafe", opaque_label)
        if family == "frozen":
            _ = plan_fixtures.producer_bound_frozen_pack(producer_media, patch)
        else:
            _ = producer_pack(producer_media, "attached", tombstone=True)
    inventory = plan_fixtures.witnessed_inventory(producer_media, monkeypatch)
    owner, claim, raw_claim = plan_fixtures.transferred_claim(inventory)
    assert claim.artifact_owner_token == label
    decisions = plan_fixtures.pack_claim_decisions(inventory, owner, claim)
    if family == "frozen":
        publication = next(
            dict(r.values)
            for r in inventory.closure.raw_rows
            if r.table == "import_publications"
        )
        origins = wire_object(
            wire_object(
                parse_document(cast(str, publication["queue_snapshot_json"]))[
                    "_pack_source_origins"
                ]
            )["files"]
        )
        origin = wire_object(next(iter(origins.values())))
        effects = plan_fixtures.expected_pack_effects(
            inventory, decisions, claim, cast(str, origin["ownership_json"])
        )
        expected = {"pack-source-file", "pack-source-root", "frozen-pack-origin"}
    else:
        assert raw_claim == claim.to_json()
        effects = plan_fixtures.transferred_claim_effects(inventory, decisions)
        expected = {
            "transferred-pack-claim-source-file",
            "transferred-pack-claim-source-root",
        }
    assert {wire_object(e["relationship"])["kind"] for e in effects} == expected
    assert all(
        wire_object(e["relationship"])["artifact_owner_token"] == label for e in effects
    )
    selection, declaration, receipt = (
        parse_document(d)
        for d in plan_fixtures.pack_linked_inputs(inventory, decisions, effects)
    )
    epoch = wire_object(selection["epoch_bindings"])
    origin_parent = str(producer_media.scope / "packs")
    old_owner = {"domain": "namespace", "key": [origin_parent]}
    assert any(
        s.owner == OwnerRef("namespace", (origin_parent,))
        for s in inventory.closure.snapshots
    )
    epoch["provision"] = [
        {
            "epoch_id": EID,
            "authority_id": "a" * 32,
            "origin_parent": origin_parent,
            "allocation_parent": origin_parent + "/.mangarr-epoch-" + EID,
            "root_path": origin_parent + "/.mangarr-epoch-" + EID + "/.mangarr-claims",
            "old_namespace_owners": [old_owner],
            "handling": "retain-old-disable-new-allocation",
        }
    ]
    for values in (
        wire_array(declaration["role_reviews"]),
        wire_array(epoch["role_bindings"]),
    ):
        for value in values:
            record = wire_object(value)
            if (
                record["owner"] == old_owner
                and wire_object(record["role"])["purpose"] == "namespace_parent"
            ):
                record["authority_id"] = "a" * 32
    inputs = resign_documents(selection, declaration, receipt)
    with transferred_plan_conservation(producer_media, monkeypatch, inventory):
        prior = recovery.build_recovery_plan(inventory, *inputs)
        assert prior.version == 2
        assert recovery.RecoveryPlan.from_json(prior.to_json()) == prior
        provision = wire_object(wire_array(epoch["provision"])[0])
        authority = wire_object(wire_array(declaration["authorities"])[0])
        payload: dict[str, object] = {
            "version": 1,
            "kind": "epoch-provision-intent",
            "plan_json": prior.to_json(),
            "plan_sha256": prior.sha256,
            "inventory_sha256": inventory.sha256,
            "epoch_id": provision["epoch_id"],
            "authority_id": provision["authority_id"],
            "authority_incarnation": authority["incarnation_token"],
            "scope": authority["scope"],
            "profile": authority["profile"],
            "origin_path": provision["origin_parent"],
            "allocation_path": provision["allocation_parent"],
            "root_path": provision["root_path"],
            "namespace_owner": {
                "domain": "namespace",
                "key": [provision["allocation_parent"]],
            },
            "allocation_token": IID,
        }
        assert store._provision_intent(encoded(payload)) == payload

        for bad_label in ("", ".", "..", "a/b", "a\\b", "a\x00b", "\ud800"):
            for kind in sorted(expected):
                changed_selection, changed_declaration, changed_receipt = (
                    parse_document(d) for d in inputs
                )
                changed_epoch = wire_object(changed_selection["epoch_bindings"])
                changed_effects = wire_array(changed_epoch["linked_effects"])
                relationship = next(
                    wire_object(wire_object(e)["relationship"])
                    for e in changed_effects
                    if wire_object(wire_object(e)["relationship"])["kind"] == kind
                )
                relationship["artifact_owner_token"] = bad_label
                changed_effects.sort(key=encoded)
                changed_declaration["linked_reviews"] = sorted(
                    [
                        {"effect_sha256": digest(e), "authority_id": "a" * 32}
                        for e in changed_effects
                    ],
                    key=lambda e: e["effect_sha256"],
                )
                _, changed_declaration_json, changed_receipt_json = resign_documents(
                    changed_selection, changed_declaration, changed_receipt
                )
                changed_plan = parse_document(cast(str, payload["plan_json"]))
                changed_plan.update(
                    epoch_bindings_json=encoded(changed_epoch),
                    declaration_json=changed_declaration_json,
                    backup_receipt_json=changed_receipt_json,
                )
                del changed_plan["sha256"]
                changed_plan["sha256"] = digest(changed_plan)
                changed_payload = dict(payload)
                changed_payload.update(
                    plan_json=encoded(changed_plan), plan_sha256=changed_plan["sha256"]
                )
                with pytest.raises(InvalidIdentity, match="^ownership_wire$"):
                    _ = store._provision_intent(encoded(changed_payload))
        for field in (
            "epoch_id",
            "authority_id",
            "authority_incarnation",
            "allocation_token",
        ):
            changed_payload = dict(payload)
            changed_payload[field] = label
            with pytest.raises(InvalidIdentity, match="^ownership_wire$"):
                _ = store._provision_intent(encoded(changed_payload))


def test_intent_preserves_eligible_prior_linked_plan_without_executing_it(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    identifier = plan_fixtures.acknowledged_publication(
        producer_media, monkeypatch, "original"
    )
    inventory = capture_inventory(producer_media, monkeypatch)
    owner = OwnerRef("publication", (identifier,))
    decisions = chosen(
        inventory,
        owner,
        "restore-original",
        {"original_artifact"},
        str(producer_media.library / "Matrix v02.cbz"),
    )
    inputs = linked_documents(
        provision_documents(inventory, decisions),
        plan_fixtures.publication_linked_effects(inventory, owner, decisions),
    )
    prior = recovery.build_recovery_plan(inventory, *inputs)
    provision = wire_object(
        wire_array(parse_document(prior.epoch_bindings_json)["provision"])[0]
    )
    authority = wire_object(
        wire_array(parse_document(prior.declaration_json)["authorities"])[0]
    )
    payload: dict[str, object] = {
        "version": 1,
        "kind": "epoch-provision-intent",
        "plan_json": prior.to_json(),
        "plan_sha256": prior.sha256,
        "inventory_sha256": inventory.sha256,
        "epoch_id": provision["epoch_id"],
        "authority_id": provision["authority_id"],
        "authority_incarnation": authority["incarnation_token"],
        "scope": authority["scope"],
        "profile": authority["profile"],
        "origin_path": provision["origin_parent"],
        "allocation_path": provision["allocation_parent"],
        "root_path": provision["root_path"],
        "namespace_owner": {
            "domain": "namespace",
            "key": [provision["allocation_parent"]],
        },
        "allocation_token": IID,
    }

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("stored historical plan was executed as fresh planning")

    with (
        transferred_plan_conservation(producer_media, monkeypatch, inventory),
        monkeypatch.context() as patch,
    ):
        patch.setattr(recovery, "build_recovery_plan", forbidden)
        assert store._provision_intent(encoded(payload))["plan_json"] == prior.to_json()


def test_successor_closure_precharges_raw_text_before_role_projection(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = synthetic_intent(producer_media, monkeypatch)
    inventory = graph_inventory(graph)
    rows: list[RawRow] = []
    for row in inventory.closure.raw_rows:
        if row.table == "file_identity_provision_intents":
            values = cast(dict[str, object], dict(row.values))
            values["payload_json"] = "\\" * (16 * 1024**2 // 2)
            row = raw(row.table, values)
        rows.append(row)
    closure = replace(inventory.closure, raw_rows=tuple(rows))

    def forbidden(_spec: consumers.RoleSpec) -> dict[str, object]:
        raise AssertionError("role projection accumulated before raw text charge")

    with monkeypatch.context() as patch:
        patch.setattr(recovery, "_spec_value", forbidden)
        with pytest.raises(InvalidIdentity, match="^ownership_budget$"):
            _ = recovery.OwnershipInventory.create(
                inventory.parent,
                closure,
                inventory.observed,
                (),
                inventory.budget,
                inventory_version=2,
            )


@pytest.mark.parametrize(
    "change", ["prepared-only", "active-only", "contract", "revoked", "intervening"]
)
def test_retaining_successor_history_requires_no_allocation_grant(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    graph = event_graph(synthetic_intent(producer_media, monkeypatch), change)
    inventory = graph_inventory(graph)
    with transferred_plan_conservation(producer_media, monkeypatch, inventory):
        plan = recovery.build_recovery_plan(
            inventory, *follow_on(graph, inventory, rollback=False)
        )
        assert all(d.action == "retain-blocked" for d in plan.decisions)
        assert parse_document(plan.epoch_bindings_json)["allocation_capabilities"] == []


def test_capability_requires_each_current_observation_and_no_extra_reference(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = synthetic_intent(producer_media, monkeypatch)
    inventory = graph_inventory(graph)
    for obj in graph.objects:
        observed = tuple(
            replace(o, native=NativeHandle(1, b"synthetic-current-drift"))
            if o.spec.owner == graph.owner and o.spec.role == obj.role
            else o
            for o in inventory.observed
        )
        changed = recovery.OwnershipInventory.create(
            inventory.parent,
            inventory.closure,
            observed,
            (),
            inventory.budget,
            inventory_version=2,
        )
        with pytest.raises(InvalidIdentity, match="^ownership_authority$"):
            _ = recovery.build_recovery_plan(
                changed, *follow_on(graph, changed, rollback=True)
            )
    selected = parse_document(follow_on(graph, inventory, rollback=True)[0])
    references = wire_array(
        wire_object(selected["epoch_bindings"])["allocation_capabilities"]
    )
    selection, declaration, receipt = (
        parse_document(t) for t in follow_on(graph, inventory, rollback=False)
    )
    wire_object(selection["epoch_bindings"])["allocation_capabilities"] = references
    with pytest.raises(InvalidIdentity, match="^ownership_phase$"):
        _ = recovery.build_recovery_plan(
            inventory, *resign_documents(selection, declaration, receipt)
        )


@pytest.mark.parametrize("entry", ["builder", "intent-codec", "malformed-history"])
def test_pending_intent_reservation_is_not_fresh_provisioning_permission(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    graph = synthetic_intent(producer_media, monkeypatch)
    history = replace(
        graph,
        rows=tuple(
            i
            for i in graph.rows
            if i["table"]
            in {"file_identity_authorities", "file_identity_provision_intents"}
        ),
    )
    inventory = graph_inventory(history)
    inputs = linked_documents(provision_documents(inventory), [])
    if entry == "builder":
        with pytest.raises(InvalidIdentity, match="^ownership_binding$"):
            _ = recovery.build_recovery_plan(inventory, *inputs)
    else:
        # Adversarially re-sign the entire envelope, not just its outer digest.
        selection = parse_document(inputs[0])
        prior: dict[str, object] = {
            "version": 2,
            "inventory": inventory._value(),
            "decisions": selection["decisions"],
            "declaration_json": inputs[1],
            "backup_receipt_json": inputs[2],
            "epoch_bindings_json": encoded(selection["epoch_bindings"]),
        }
        if entry == "malformed-history":
            value = wire_object(prior["inventory"])
            record = next(
                wire_object(r)
                for r in wire_array(wire_object(value["closure"])["raw_rows"])
                if wire_object(r)["table"] == "file_identity_provision_intents"
            )
            columns = {
                cast(str, wire_array(p)[0]): wire_array(p)[1]
                for p in wire_array(record["values"])
            }
            columns.update(intent_id="9" * 32, payload_json="[]")
            record.update(
                key=["9" * 32], values=[[k, v] for k, v in sorted(columns.items())]
            )
            del value["sha256"]
            value["sha256"] = digest(value)
            selection, declaration, receipt = (parse_document(t) for t in inputs)
            for document in (selection, declaration, receipt):
                document["inventory_sha256"] = value["sha256"]
            re_signed = resign_documents(selection, declaration, receipt)
            prior.update(
                declaration_json=re_signed[1], backup_receipt_json=re_signed[2]
            )
        prior["sha256"] = digest(prior)
        row = next(
            wire_object(i["row"])
            for i in graph.rows
            if i["table"] == "file_identity_provision_intents"
        )
        payload = parse_document(cast(str, row["payload_json"]))
        payload.update(
            plan_json=encoded(prior),
            plan_sha256=prior["sha256"],
            inventory_sha256=wire_object(prior["inventory"])["sha256"],
        )
        code = "ownership_wire" if entry == "malformed-history" else "ownership_binding"
        with pytest.raises(InvalidIdentity, match="^" + code + "$"):
            _ = store._provision_intent(encoded(payload))


def resigned_historical_intent(
    graph: SyntheticGraph, prior: dict[str, object]
) -> dict[str, object]:
    """Re-sign every envelope independently of the historical store validator."""
    inventory = wire_object(prior["inventory"])
    _ = inventory.pop("sha256")
    inventory["sha256"] = digest(inventory)
    selection = {
        "version": 2,
        "inventory_sha256": inventory["sha256"],
        "decisions": prior["decisions"],
        "epoch_bindings": parse_document(cast(str, prior["epoch_bindings_json"])),
    }
    declaration = parse_document(cast(str, prior["declaration_json"]))
    declaration.update(
        inventory_sha256=inventory["sha256"], selection_sha256=digest(selection)
    )
    receipt = parse_document(cast(str, prior["backup_receipt_json"]))
    # Preserve the existing media subjects while re-signing their changed leaves.
    old_observed = wire_array(
        wire_object(parse_document(graph.prior.to_json())["inventory"])["observed"]
    )
    for before, after in zip(
        old_observed, wire_array(inventory["observed"]), strict=True
    ):
        subject = wire_object(wire_object(before)["spec"])
        for value in wire_array(receipt["media"]):
            media = wire_object(value)
            if (media["owner"], media["role"]) == (subject["owner"], subject["role"]):
                media["observation_sha256"] = digest(after)
    receipt.update(
        inventory_sha256=inventory["sha256"],
        selection_sha256=digest(selection),
        declaration_sha256=digest(declaration),
    )
    prior.update(
        declaration_json=encoded(declaration), backup_receipt_json=encoded(receipt)
    )
    _ = prior.pop("sha256")
    prior["sha256"] = digest(prior)
    row = next(
        wire_object(r["row"])
        for r in graph.rows
        if r["table"] == "file_identity_provision_intents"
    )
    payload = parse_document(cast(str, row["payload_json"]))
    payload.update(
        plan_json=encoded(prior),
        plan_sha256=prior["sha256"],
        inventory_sha256=inventory["sha256"],
    )
    return payload


@pytest.mark.parametrize(
    "change",
    [
        "owner",
        "role",
        "path",
        "parent",
        "legacy",
        "presence",
        "policy-kind",
        "proof-without-legacy",
        "native",
        "constraints-array",
        "outcome",
        "version-bool",
        "version-unknown",
        "kind",
        "expected",
        "policy",
        "boundary",
        "current",
        "mode-bool",
        "directory-size",
        "content",
        "children-path",
        "children-outcome",
        "children-duplicate",
        "ancestor",
        "outcome-evidence",
    ],
)
def test_historical_observation_leaves_require_closed_wire(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    graph = synthetic_intent(producer_media, monkeypatch)
    prior = parse_document(graph.prior.to_json())
    observations = wire_array(wire_object(prior["inventory"])["observed"])
    observation = wire_object(observations[0])
    spec = wire_object(observation["spec"])
    constraints = parse_document(cast(str, observation["constraints_json"]))
    if change in {
        "owner",
        "role",
        "path",
        "parent",
        "legacy",
        "presence",
        "policy-kind",
        "proof-without-legacy",
    }:
        field, bad = {
            "owner": ("owner", None),
            "role": ("role", None),
            "path": ("path", "relative/path"),
            "parent": ("parent_role", spec["role"]),
            "legacy": ("legacy_json", "not JSON"),
            "presence": ("presence", "unknown"),
            "policy-kind": (
                "policy",
                "marker"
                if wire_object(spec["role"])["kind"] == "directory"
                else "private_directory",
            ),
            "proof-without-legacy": ("legacy_json", None),
        }[change]
        spec[field] = bad
        if change == "proof-without-legacy":
            spec["presence"] = "proof"
    elif change == "native":
        observation["native"] = False
    elif change == "constraints-array":
        observation["constraints_json"] = "[]"
    elif change == "outcome":
        observation["outcome"] = "operator-approved"
    else:
        if change in {
            "version-bool",
            "version-unknown",
            "kind",
            "expected",
            "policy",
            "boundary",
            "current",
            "content",
        }:
            changes: dict[str, tuple[str, object]] = {
                "version-bool": ("version", True),
                "version-unknown": ("version", 3),
                "kind": ("kind", "approved"),
                "expected": ("expected", "approved"),
                "policy": ("policy", "approved"),
                "boundary": ("boundary", "approved"),
                "current": ("current", []),
                "content": ("content", {"size": 0, "sha256": "9" * 64}),
            }
            field, bad = changes[change]
            constraints[field] = bad
        elif change == "mode-bool":
            wire_object(constraints["current"])["mode"] = True
        elif change == "directory-size":
            assert wire_object(spec["role"])["kind"] == "directory"
            wire_object(constraints["current"])["size"] = 0
        elif change.startswith("children-"):
            constraints["children"] = [{"path": spec["path"], "outcome": "observed"}]
            if change != "children-path":
                child: dict[str, object] = {
                    "path": cast(str, spec["path"]) + "/child",
                    "outcome": "unreachable_below_absent_root"
                    if change == "children-outcome"
                    else "observed",
                }
                constraints["children"] = (
                    [child, child] if change == "children-duplicate" else [child]
                )
        elif change == "ancestor":
            constraints.update(
                version=2,
                ancestor={
                    "owner": None,
                    "role": spec["role"],
                    "observation_sha256": "9" * 64,
                },
            )
            observation["outcome"] = "unreachable_below_absent_root"
        else:
            observation["outcome"] = "absent"
        observation["constraints_json"] = encoded(constraints)
    payload = resigned_historical_intent(graph, prior)
    with domain_external_boundaries(monkeypatch):
        with pytest.raises(InvalidIdentity, match="^ownership_wire$"):
            _ = store._provision_intent(encoded(payload))


@pytest.mark.parametrize(
    "change",
    [
        "table",
        "key-empty",
        "key-path",
        "key-type",
        "key-bool",
        "key-id32",
        "key-pair",
        "columns-order",
        "columns-duplicate",
        "key-missing",
        "key-value",
        "scalar-bool",
        "scalar-container",
    ],
)
def test_historical_raw_rows_require_complete_row_grammar(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    graph = synthetic_intent(producer_media, monkeypatch)
    prior = parse_document(graph.prior.to_json())
    rows = wire_array(
        wire_object(wire_object(prior["inventory"])["closure"])["raw_rows"]
    )
    record = wire_object(rows[0])
    pairs = wire_array(record["values"])
    if change == "table":
        record["table"] = "unreviewed_table"
    elif change == "key-empty":
        record["key"] = []
    elif change == "key-path":
        record.update(
            table="file_claim_namespaces",
            key=["relative/path"],
            values=[["parent_path", "relative/path"]],
        )
    elif change == "key-type":
        record["key"] = [None]
    elif change == "key-bool":
        record.update(table="series", key=[True], values=[["id", True]])
    elif change == "key-id32":
        record.update(
            table="file_identity_authorities",
            key=["opaque"],
            values=[["authority_id", "opaque"]],
        )
    elif change == "key-pair":
        record.update(
            table="series_tags", key=[1], values=[["series_id", 1], ["tag", "tag"]]
        )
    elif change == "columns-order":
        pairs.reverse()
    elif change == "columns-duplicate":
        pairs.append(pairs[-1])
    elif change == "key-missing":
        record["values"] = []
    elif change == "key-value":
        record["key"] = ["/different/key"]
    else:
        bad_scalar: object = True if change == "scalar-bool" else list[object]()
        pairs.append(["zz_extra", bad_scalar])
    payload = resigned_historical_intent(graph, prior)
    with domain_external_boundaries(monkeypatch):
        with pytest.raises(InvalidIdentity, match="^ownership_wire$"):
            _ = store._provision_intent(encoded(payload))


def test_historical_observation_vocabulary_and_extra_sql_columns_are_retained(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = synthetic_intent(producer_media, monkeypatch)
    base = parse_document(graph.prior.to_json())
    original = wire_object(wire_array(wire_object(base["inventory"])["observed"])[0])
    source = parse_document(cast(str, original["constraints_json"]))
    spec = recovery._spec_from(original["spec"])
    for outcome in sorted(recovery._OUTCOMES):
        prior = parse_document(graph.prior.to_json())
        observation = wire_object(
            wire_array(wire_object(prior["inventory"])["observed"])[0]
        )
        constraints = dict(source)
        native = None
        if outcome == "observed":
            native = graph.objects[0].native
        elif outcome == "unknown_children":
            constraints["children"] = [
                {"path": spec.path + "/child", "outcome": "unreadable"}
            ]
        elif outcome != "native_unsupported":
            constraints.update(
                current=None,
                content=None,
                children=[],
                policy="mismatch" if outcome == "policy_mismatch" else "not_checked",
                boundary="cross_device" if outcome == "cross_device" else "verified",
            )
            if outcome == "absent":
                constraints["policy"] = "satisfied"
            elif outcome == "unreachable_below_absent_root":
                constraints.update(
                    version=2,
                    ancestor={
                        "owner": wire_object(original["spec"])["owner"],
                        "role": recovery._role_value(spec.role),
                        "observation_sha256": digest(original),
                    },
                )
        # The live model is only a grammar oracle in this test, not executed P.
        observed = recovery.ObservedRole(spec, native, encoded(constraints), outcome)
        observation.update(
            native=None if native is None else native.to_value(),
            constraints_json=observed.constraints_json,
            outcome=outcome,
        )
        rows = wire_array(
            wire_object(wire_object(prior["inventory"])["closure"])["raw_rows"]
        )
        for value in rows:
            record = wire_object(value)
            pairs = wire_array(record["values"])
            pairs.append(["zz_legitimate_extra", ""])
        payload = resigned_historical_intent(graph, prior)
        with domain_external_boundaries(monkeypatch):
            assert store._provision_intent(encoded(payload)) == payload


def test_historical_leaf_grammar_matches_raw_row_and_legacy_wire(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = synthetic_intent(producer_media, monkeypatch)
    prior = parse_document(graph.prior.to_json())
    observation = wire_object(
        wire_array(wire_object(prior["inventory"])["observed"])[0]
    )
    spec = wire_object(observation["spec"])
    legacy = " \n" + cast(str, spec["legacy_json"]) + "\n "
    spec["legacy_json"] = legacy
    with domain_external_boundaries(monkeypatch):
        _ = recovery._spec_from(spec)
        assert store._historical_role(spec)["legacy_json"] == legacy
        for table, primary in consumers._PURE_TABLES.items():
            columns = ("series_id", "tag") if table == "series_tags" else (primary,)
            key: list[str | int] = []
            for column in columns:
                key.append(
                    "/historical/path"
                    if column in {"parent_path", "tombstone_path"}
                    else "opaque label"
                    if column in {"download_identity_key", "tag"}
                    else "a" * 32
                    if table.startswith("file_identity_") and column != "id"
                    else 1
                )
            values: dict[str, consumers.SQLScalar] = dict(
                zip(columns, key, strict=True)
            )
            values.update(
                extra_null=None, extra_text="", extra_float=1e308, extra_integer=1 << 70
            )
            row = RawRow(table, tuple(key), tuple(sorted(values.items())))
            document: dict[str, object] = {
                "table": row.table,
                "key": list(row.key),
                "values": [list(p) for p in row.values],
            }
            store._historical_raw_row(document)
            for nonfinite in (float("inf"), float("-inf"), float("nan")):
                invalid = dict(document)
                invalid["values"] = [
                    ["extra_float", nonfinite],
                    *[list(p) for p in row.values if p[0] != "extra_float"],
                ]
                with pytest.raises(
                    InvalidIdentity, match="^identity JSON requires finite values$"
                ):
                    store._historical_raw_row(invalid)
