"""Schema8 SQL boundaries. Synthetic native evidence is not qualification."""

from __future__ import annotations

import sqlite3
import os
import secrets
import hashlib
import subprocess
import sys
from dataclasses import replace
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import cast
from collections.abc import Mapping, Sequence

import pytest

import file_identity_recovery as recovery
import file_identity_store as store
import schema
import shared
from file_identity_consumers import InventoryBudget, ReferenceClosure
from file_identity import NativeHandle, InvalidIdentity, OwnerRef
from file_identity_schema7 import initialize_schema7
from file_mutation_lock import FileMutationGuard, file_mutation_guard
from test_file_identity_schema import insert_rescan
from test_file_identity_schema import ledger_path as ledger_path
from test_file_identity_observation import (
    ProducerMedia,
    producer_media as producer_media,
)
from test_file_identity_provision_wire import SyntheticGraph, synthetic_intent
from test_file_identity_provision_wire import graph_inventory, follow_on
from test_file_identity_provision_wire import resigned_historical_intent
from test_file_identity_plan import digest, encoded, parse_document, wire_object


def insert_row(db: sqlite3.Connection, table: str, row: Mapping[str, object]) -> None:
    _ = db.execute(
        f"INSERT INTO {table}({','.join(row)}) VALUES({','.join('?' for _ in row)})",
        tuple(row.values()),
    )


@pytest.fixture
def candidate_graph(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> SyntheticGraph:
    graph = synthetic_intent(producer_media, monkeypatch)
    schema.init_db()
    return graph


@pytest.fixture
def sql_graph(
    producer_media: ProducerMedia, candidate_graph: SyntheticGraph
) -> SyntheticGraph:
    graph = candidate_graph
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        _ = db.execute("PRAGMA foreign_keys=ON")
        for table in (
            "file_identity_authorities",
            "file_identity_provision_intents",
            "file_identity_epochs",
            "file_claim_namespaces",
            "file_identity_sets",
        ):
            for item in graph.rows:
                if item["table"] == table:
                    insert_row(db, table, wire_object(item["row"]))
    return graph


def test_schema8_eligible_intent_activation_grant_revoke(
    producer_media: ProducerMedia, sql_graph: SyntheticGraph
) -> None:
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        _ = db.execute("PRAGMA foreign_keys=ON")
        for item in sql_graph.rows:
            if item["table"] == "file_identity_events":
                insert_row(db, cast(str, item["table"]), wire_object(item["row"]))
        assert db.execute(
            "SELECT kind FROM file_identity_events ORDER BY id"
        ).fetchall() == [("prepared",), ("activated",), ("recovery_step",)]
        grant = wire_object(sql_graph.rows[-1]["row"])
        payload = parse_document(cast(str, grant["payload_json"]))
        payload.update(
            state="revoke", prior_capability_event_id=3, plan_sha256="f" * 64
        )
        insert_row(
            db,
            "file_identity_events",
            {
                **grant,
                "id": 4,
                "revision": 4,
                "predecessor_event_id": 3,
                "payload_json": encoded(payload),
            },
        )
        with pytest.raises(sqlite3.IntegrityError):
            insert_row(
                db,
                "file_identity_events",
                {
                    **grant,
                    "id": 5,
                    "revision": 5,
                    "predecessor_event_id": 4,
                },
            )


def graph_row(graph: SyntheticGraph, table: str) -> dict[str, object]:
    return next(
        wire_object(item["row"]) for item in graph.rows if item["table"] == table
    ).copy()


@pytest.mark.parametrize("revision", [7, 8])
def test_new_ledger_requires_receipt8(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, revision: int
) -> None:
    graph = synthetic_intent(producer_media, monkeypatch)
    inventory = graph_inventory(graph)
    selection, declaration, receipt = (
        parse_document(s) for s in follow_on(graph, inventory, rollback=False)
    )
    wire_object(receipt["archive"])["schema_version"] = revision
    from test_file_identity_plan import resign_documents

    if revision == 7:
        with pytest.raises(InvalidIdentity):
            _ = recovery.build_recovery_plan(
                inventory, *resign_documents(selection, declaration, receipt)
            )
    else:
        assert (
            recovery.build_recovery_plan(
                inventory, *resign_documents(selection, declaration, receipt)
            ).version
            == 2
        )


def test_sql_fs_sql_snapshot2_capture(
    producer_media: ProducerMedia,
    candidate_graph: SyntheticGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real SQL/media/SQL, with only native handle acquisition doubled.

    This local test assembles ledger evidence, not the future Task8 producer.
    """
    import private_file_claim
    from test_file_identity_store import object_value

    graph = candidate_graph
    allocation = Path(cast(str, graph.owner.key[0]))
    allocation.mkdir(mode=0o700)

    def intent_token(size: int) -> str:
        assert size == 16
        return "1" * 32

    with monkeypatch.context() as patch:
        patch.setattr(secrets, "token_hex", intent_token)
        with file_mutation_guard(str(producer_media.db_path)) as guard:
            with private_file_claim.ensure_namespace(guard, str(allocation)):
                pass
    with closing(sqlite3.connect(producer_media.db_path)) as db:
        legacy_json = cast(
            str,
            db.execute(
                "SELECT ownership_json FROM file_claim_namespaces WHERE parent_path=?",
                graph.owner.key,
            ).fetchone()[0],
        )
    objects: list[store.ObjectProof] = []
    for obj in graph.objects:
        info = os.stat(obj.canonical_path, follow_symlinks=False)
        fields = parse_document(obj.constraints_json)
        non_device = wire_object(fields["non_device"])
        non_device["inode"] = info.st_ino
        if obj.role.purpose != "epoch_origin_parent":
            non_device.update(uid=info.st_uid, mode=info.st_mode & 0o7777)
            fields["legacy"] = {
                "kind": "namespace_v2",
                "json": legacy_json,
                "sha256": hashlib.sha256(legacy_json.encode()).hexdigest(),
            }
        if obj.role.kind == "marker":
            non_device.update(
                size=info.st_size,
                mtime_ns=info.st_mtime_ns,
                sha256=hashlib.sha256(
                    Path(obj.canonical_path).read_bytes()
                ).hexdigest(),
                nlink=info.st_nlink,
            )
        objects.append(replace(obj, constraints_json=encoded(fields)))
    birth = parse_document(graph.birth_json)
    controlled = wire_object(birth["controlled_birth"])
    controlled["objects"] = [object_value(o) for o in objects]
    _ = controlled.pop("receipt_sha256")
    controlled["receipt_sha256"] = digest(controlled)
    birth_json = encoded(birth)
    epoch = graph_row(graph, "file_identity_epochs")
    epoch["birth_json"] = birth_json
    snapshot = parse_document(graph.snapshot_json)
    wire_object(wire_object(snapshot["root"])["row"])["ownership_json"] = legacy_json
    related = cast(list[dict[str, object]], snapshot["related"])
    related[0]["row"] = epoch
    snapshot_json = encoded(snapshot)
    proof = graph_row(graph, "file_identity_sets")
    manifest = parse_document(cast(str, proof["manifest_json"]))
    manifest.update(
        snapshot_sha256=digest(snapshot), roles=[object_value(o) for o in objects]
    )
    wire_object(manifest["intent"])["snapshot_sha256"] = digest(snapshot)
    proof.update(
        snapshot_json=snapshot_json,
        snapshot_sha256=digest(snapshot),
        manifest_json=encoded(manifest),
        manifest_sha256=digest(manifest),
    )
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        _ = db.execute("PRAGMA foreign_keys=ON")
        for table, row in (
            (
                "file_identity_authorities",
                graph_row(graph, "file_identity_authorities"),
            ),
            (
                "file_identity_provision_intents",
                graph_row(graph, "file_identity_provision_intents"),
            ),
            ("file_identity_epochs", epoch),
            ("file_identity_sets", proof),
        ):
            insert_row(db, table, row)
        for item in graph.rows:
            if item["table"] != "file_identity_events":
                continue
            event = wire_object(item["row"]).copy()
            payload = parse_document(cast(str, event["payload_json"]))
            if payload["version"] == 1:
                payload.update(
                    snapshot_sha256=digest(snapshot), manifest_sha256=digest(manifest)
                )
            else:
                payload.update(
                    before_snapshot_json=snapshot_json,
                    before_snapshot_sha256=digest(snapshot),
                    birth_sha256=digest(birth),
                )
            event["payload_json"] = encoded(payload)
            insert_row(db, "file_identity_events", event)
        before = catalog(db)
        rows_before = db.execute(
            "SELECT * FROM file_identity_events ORDER BY id"
        ).fetchall()
    phases: list[str] = []
    closed: list[recovery._ReadConnection] = []
    real_capture = recovery._capture_sql
    real_observe = recovery._observe_closure
    real_close = recovery._ReadConnection.close

    def close(db: recovery._ReadConnection) -> None:
        real_close(db)
        closed.append(db)

    def capture(
        guard: FileMutationGuard,
        parent: str,
        budget: InventoryBudget,
        *,
        inventory_version: int = 1,
    ) -> ReferenceClosure:
        phases.append("SQL")
        return real_capture(guard, parent, budget, inventory_version=inventory_version)

    def observe(
        guard: FileMutationGuard,
        parent: str,
        closure: ReferenceClosure,
        budget: InventoryBudget,
        *,
        inventory_version: int = 1,
    ) -> recovery.OwnershipInventory:
        phases.append("FS")
        assert len(closed) == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            _ = closed[0].execute("SELECT 1")
        return real_observe(
            guard, parent, closure, budget, inventory_version=inventory_version
        )

    monkeypatch.setattr(recovery, "_capture_sql", capture)
    monkeypatch.setattr(recovery, "_observe_closure", observe)
    monkeypatch.setattr(recovery._ReadConnection, "close", close)

    def native(fd: int) -> NativeHandle:
        return NativeHandle(1, f"unit-{os.fstat(fd).st_ino}".encode())

    monkeypatch.setattr(recovery, "capture_native", native)
    with file_mutation_guard(str(producer_media.db_path)) as guard:
        result = recovery.inspect_ownership(
            guard, str(producer_media.scope), InventoryBudget(), inventory_version=2
        )
    assert phases == ["SQL", "FS", "SQL"]
    assert len(closed) == 2
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        _ = closed[1].execute("SELECT 1")
    assert not result.unresolved
    captured = next(s for s in result.closure.snapshots if s.owner == graph.owner)
    assert captured.rows_json == snapshot_json
    assert len([r for r in result.closure.roles if r.owner == graph.owner]) == 4
    assert {s.owner for s in graph.prior.inventory.closure.snapshots} <= {
        s.owner for s in result.closure.snapshots
    }
    assert recovery.OwnershipInventory.from_json(result.to_json()) == result
    with closing(sqlite3.connect(producer_media.db_path)) as db:
        assert catalog(db) == before
        assert (
            db.execute("SELECT * FROM file_identity_events ORDER BY id").fetchall()
            == rows_before
        )


def change(document: dict[str, object], path: str, value: object) -> None:
    names = path.split(".")
    target = document
    for name in names[:-1]:
        target = wire_object(target[name])
    target[names[-1]] = value


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", True),
        ("version", 2),
        ("kind", "other"),
        ("extra", 1),
        ("epoch_id", "E" * 32),
        ("authority_id", "f" * 32),
        ("authority_incarnation", "f" * 32),
        ("allocation_token", "f" * 32),
        ("plan_sha256", "z" * 64),
        ("inventory_sha256", "f" * 64),
        ("origin_path", None),
        ("allocation_path", "/foreign"),
        ("root_path", "/foreign"),
        ("namespace_owner.domain", "rescan"),
        ("namespace_owner.key", ["/foreign"]),
        ("namespace_owner.extra", 1),
        ("scope.version", True),
        ("scope.scope_token", "f" * 32),
        ("scope.scope_path", "/foreign"),
        ("scope.extra", 1),
        ("profile.version", True),
        ("profile.revision", True),
        ("profile.revision", 0),
        ("profile.profile_id", ""),
        ("profile.qualification_sha256", "f" * 64),
        ("profile.extra", 1),
        ("profile.client_contract.extra", 1),
        ("profile.client_contract.platforms", []),
        ("profile.client_contract.platforms", [{"arch": "x"}]),
        ("profile.client_contract.mount.hard", 1),
        ("profile.client_contract.mount.extra", 1),
        ("profile.server_contract.extra", 1),
        ("profile.server_contract.allowed_transitions", [True]),
        ("profile.server_contract.allowed_transitions", ["z", "a"]),
        ("profile.server_contract.recovery_transitions", ["a", "a"]),
    ],
)
def test_intent_direct_sql_refusal_matrix(
    producer_media: ProducerMedia,
    candidate_graph: SyntheticGraph,
    field: str,
    value: object,
) -> None:
    authority = graph_row(candidate_graph, "file_identity_authorities")
    intent = graph_row(candidate_graph, "file_identity_provision_intents")
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        _ = db.execute("PRAGMA foreign_keys=ON")
        insert_row(db, "file_identity_authorities", authority)
        _ = db.execute("SAVEPOINT eligible")
        insert_row(db, "file_identity_provision_intents", intent)
        _ = db.execute("ROLLBACK TO eligible")
        payload = parse_document(cast(str, intent["payload_json"]))
        change(payload, field, value)
        # Independently re-sign: a matching syntactic digest is not permission.
        intent.update(payload_json=encoded(payload), payload_sha256=digest(payload))
        with pytest.raises(sqlite3.IntegrityError):
            insert_row(db, "file_identity_provision_intents", intent)


@pytest.mark.parametrize(
    "field", ["scope", "profile", "client", "server", "mount", "platform"]
)
def test_intent_rejects_stringified_objects(
    producer_media: ProducerMedia, candidate_graph: SyntheticGraph, field: str
) -> None:
    authority = graph_row(candidate_graph, "file_identity_authorities")
    intent = graph_row(candidate_graph, "file_identity_provision_intents")
    payload = parse_document(cast(str, intent["payload_json"]))
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        _ = db.execute("PRAGMA foreign_keys=ON")
        _ = db.execute("SAVEPOINT eligible_object")
        insert_row(db, "file_identity_authorities", authority)
        insert_row(db, "file_identity_provision_intents", intent)
        assert db.execute(
            "SELECT json_type(payload_json,'$.plan_json') FROM file_identity_provision_intents"
        ).fetchone() == ("text",)
        _ = db.execute("ROLLBACK TO eligible_object")
        if field in {"scope", "profile"}:
            # Original authority and plan still match the extracted string bytes.
            payload[field] = encoded(payload[field])
        else:
            profile = wire_object(payload["profile"])
            client = wire_object(profile["client_contract"])
            if field in {"client", "server"}:
                name = field + "_contract"
                profile[name] = encoded(profile[name])
            elif field == "mount":
                client["mount"] = encoded(client["mount"])
            else:
                platforms = cast(list[object], client["platforms"])
                platforms[0] = encoded(platforms[0])
            authority["profile_json"] = encoded(profile)
            plan = parse_document(cast(str, payload["plan_json"]))
            declaration = parse_document(cast(str, plan["declaration_json"]))
            declared = cast(list[dict[str, object]], declaration["authorities"])
            declared[0]["profile"] = profile
            plan["declaration_json"] = encoded(declaration)
            # Independently recompute declaration/receipt/plan digests, not just I.
            payload = resigned_historical_intent(candidate_graph, plan)
            payload["profile"] = profile
        intent.update(payload_json=encoded(payload), payload_sha256=digest(payload))
        insert_row(db, "file_identity_authorities", authority)
        assert (
            payload["plan_sha256"]
            == parse_document(cast(str, payload["plan_json"]))["sha256"]
        )
        with pytest.raises(sqlite3.IntegrityError, match="invalid provisioning intent"):
            insert_row(db, "file_identity_provision_intents", intent)


@pytest.mark.parametrize(
    "mode",
    [
        "plan-version",
        "plan-digest",
        "inventory-digest",
        "duplicate-provision",
        "wrong-geometry",
        "already-born",
        "foreign-authority",
        "foreign-profile",
    ],
)
def test_intent_embedded_plan_refusals(
    producer_media: ProducerMedia, candidate_graph: SyntheticGraph, mode: str
) -> None:
    intent = graph_row(candidate_graph, "file_identity_provision_intents")
    payload = parse_document(cast(str, intent["payload_json"]))
    plan = parse_document(cast(str, payload["plan_json"]))
    epochs = parse_document(cast(str, plan["epoch_bindings_json"]))
    declaration = parse_document(cast(str, plan["declaration_json"]))
    provision = cast(list[dict[str, object]], epochs["provision"])
    if mode == "plan-version":
        plan["version"] = True
    elif mode == "plan-digest":
        plan["sha256"] = "f" * 64
    elif mode == "inventory-digest":
        wire_object(plan["inventory"])["sha256"] = "f" * 64
    elif mode == "duplicate-provision":
        provision.append(provision[0].copy())
    elif mode == "wrong-geometry":
        provision[0]["root_path"] = "/foreign"
    elif mode == "already-born":
        epochs["existing"] = [{"epoch_id": payload["epoch_id"]}]
    else:
        authority = cast(list[dict[str, object]], declaration["authorities"])[0]
        if mode == "foreign-authority":
            authority["incarnation_token"] = "f" * 32
        else:
            wire_object(authority["profile"])["profile_id"] = "other"
    plan.update(
        epoch_bindings_json=encoded(epochs), declaration_json=encoded(declaration)
    )
    payload["plan_json"] = encoded(plan)
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        insert_row(
            db,
            "file_identity_authorities",
            graph_row(candidate_graph, "file_identity_authorities"),
        )
        _ = db.execute("SAVEPOINT eligible")
        insert_row(db, "file_identity_provision_intents", intent)
        _ = db.execute("ROLLBACK TO eligible")
        intent.update(payload_json=encoded(payload), payload_sha256=digest(payload))
        with pytest.raises(sqlite3.IntegrityError):
            insert_row(db, "file_identity_provision_intents", intent)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", True),
        ("kind", "other"),
        ("extra", 1),
        ("before_snapshot_json", "{}"),
        ("before_snapshot_sha256", "f" * 64),
        ("plan_sha256", "f" * 64),
        ("intent_id", "f" * 32),
        ("activation_event_id", True),
        ("activation_event_id", 1),
        ("epoch_id", "f" * 32),
        ("authority_id", "f" * 32),
        ("authority_incarnation", "f" * 32),
        ("origin_path", "/foreign"),
        ("allocation_path", "/foreign"),
        ("root_path", "/foreign"),
        ("birth_sha256", "z" * 64),
        ("consumer_contract_sha256", "z" * 64),
        ("state", "other"),
        ("prior_capability_event_id", True),
        ("prior_capability_event_id", 1),
    ],
)
def test_capability_direct_sql_refusal_matrix(
    producer_media: ProducerMedia, sql_graph: SyntheticGraph, field: str, value: object
) -> None:
    events = [
        wire_object(item["row"]).copy()
        for item in sql_graph.rows
        if item["table"] == "file_identity_events"
    ]
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        for event in events[:2]:
            insert_row(db, "file_identity_events", event)
        grant = events[2]
        _ = db.execute("SAVEPOINT eligible")
        insert_row(db, "file_identity_events", grant)
        _ = db.execute("ROLLBACK TO eligible")
        payload = parse_document(cast(str, grant["payload_json"]))
        change(payload, field, value)
        grant["payload_json"] = encoded(payload)
        with pytest.raises(sqlite3.IntegrityError):
            insert_row(db, "file_identity_events", grant)


@pytest.mark.parametrize(
    "mode",
    [
        "birth-authority",
        "birth-epoch",
        "birth-origin",
        "birth-owner",
        "snapshot-related",
        "snapshot-epoch",
        "snapshot-intent",
        "manifest-intent",
    ],
)
def test_capability_rejects_foreign_birth_snapshot_manifest(
    producer_media: ProducerMedia, candidate_graph: SyntheticGraph, mode: str
) -> None:
    authority = graph_row(candidate_graph, "file_identity_authorities")
    intent = graph_row(candidate_graph, "file_identity_provision_intents")
    epoch = graph_row(candidate_graph, "file_identity_epochs")
    proof = graph_row(candidate_graph, "file_identity_sets")
    birth = parse_document(cast(str, epoch["birth_json"]))
    controlled = wire_object(birth["controlled_birth"])
    snapshot = parse_document(cast(str, proof["snapshot_json"]))
    manifest = parse_document(cast(str, proof["manifest_json"]))
    if mode.startswith("birth-"):
        field, value = {
            "birth-authority": ("authority_id", "f" * 32),
            "birth-epoch": ("epoch_id", "f" * 32),
            "birth-origin": ("origin_path", "/foreign"),
            "birth-owner": (
                "namespace_owner",
                {"domain": "namespace", "key": ["/foreign"]},
            ),
        }[mode]
        controlled[field] = value
        controlled.pop("receipt_sha256")
        controlled["receipt_sha256"] = digest(controlled)
        epoch["birth_json"] = encoded(birth)
    elif mode == "snapshot-related":
        snapshot["related"] = []
    elif mode.startswith("snapshot-"):
        index = 0 if mode == "snapshot-epoch" else 1
        related = cast(list[dict[str, object]], snapshot["related"])
        wire_object(related[index]["row"])["authority_id"] = "f" * 32
    else:
        wire_object(manifest["intent"])["intent_id"] = "f" * 32
    proof.update(snapshot_json=encoded(snapshot), snapshot_sha256=digest(snapshot))
    manifest["snapshot_sha256"] = digest(snapshot)
    wire_object(manifest["intent"])["snapshot_sha256"] = digest(snapshot)
    proof.update(manifest_json=encoded(manifest), manifest_sha256=digest(manifest))
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        insert_row(db, "file_identity_authorities", authority)
        insert_row(db, "file_identity_provision_intents", intent)
        # This matrix targets the event predicate independently of the epoch guard.
        insert_row(
            db,
            "file_identity_epochs",
            graph_row(candidate_graph, "file_identity_epochs"),
        )
        insert_row(
            db, "file_identity_sets", graph_row(candidate_graph, "file_identity_sets")
        )
        events = [
            wire_object(item["row"]).copy()
            for item in candidate_graph.rows
            if item["table"] == "file_identity_events"
        ]
        for event in events[:2]:
            insert_row(db, "file_identity_events", event)
        _ = db.execute("SAVEPOINT eligible")
        insert_row(db, "file_identity_events", events[2])
        _ = db.execute("ROLLBACK TO eligible")
        # Immutable rows cannot be edited, so use a second eligible set/epoch graph
        # in a clean transaction after rolling back all setup.
        db.rollback()
        insert_row(db, "file_identity_authorities", authority)
        insert_row(db, "file_identity_provision_intents", intent)
        if mode.startswith("birth-"):
            with pytest.raises(sqlite3.IntegrityError):
                insert_row(db, "file_identity_epochs", epoch)
            return
        insert_row(db, "file_identity_epochs", epoch)
        insert_row(db, "file_identity_sets", proof)
        for event in events:
            payload = parse_document(cast(str, event["payload_json"]))
            if payload["version"] == 1:
                payload.update(
                    snapshot_sha256=proof["snapshot_sha256"],
                    manifest_sha256=proof["manifest_sha256"],
                )
            else:
                payload.update(
                    before_snapshot_json=proof["snapshot_json"],
                    before_snapshot_sha256=proof["snapshot_sha256"],
                    birth_sha256=digest(birth),
                )
            event["payload_json"] = encoded(payload)
        for event in events[:2]:
            insert_row(db, "file_identity_events", event)
        with pytest.raises(sqlite3.IntegrityError):
            insert_row(db, "file_identity_events", events[2])


@pytest.mark.parametrize(
    "operation",
    [
        "update",
        "delete",
        "replace",
        "duplicate-token",
        "duplicate-epoch",
        "foreign-epoch",
    ],
)
@pytest.mark.parametrize("recursive", [False, True])
def test_intent_is_immutable_and_reserves_unresolved_tuple(
    producer_media: ProducerMedia,
    candidate_graph: SyntheticGraph,
    operation: str,
    recursive: bool,
) -> None:
    intent = graph_row(candidate_graph, "file_identity_provision_intents")
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        _ = db.execute(f"PRAGMA recursive_triggers={int(recursive)}")
        insert_row(
            db,
            "file_identity_authorities",
            graph_row(candidate_graph, "file_identity_authorities"),
        )
        insert_row(db, "file_identity_provision_intents", intent)
        before = db.execute("SELECT * FROM file_identity_provision_intents").fetchall()
        with pytest.raises(sqlite3.IntegrityError):
            if operation == "update":
                _ = db.execute(
                    "UPDATE file_identity_provision_intents SET payload_sha256=?",
                    ("f" * 64,),
                )
            elif operation == "delete":
                _ = db.execute("DELETE FROM file_identity_provision_intents")
            elif operation == "replace":
                _ = db.execute(
                    "INSERT OR REPLACE INTO file_identity_provision_intents VALUES(?,?,?,?)",
                    tuple(intent.values()),
                )
            elif operation in {"duplicate-token", "duplicate-epoch"}:
                payload = parse_document(cast(str, intent["payload_json"]))
                if operation == "duplicate-epoch":
                    intent["intent_id"] = "f" * 32
                    payload["allocation_token"] = intent["intent_id"]
                intent.update(
                    payload_json=encoded(payload), payload_sha256=digest(payload)
                )
                insert_row(db, "file_identity_provision_intents", intent)
            else:
                epoch = graph_row(candidate_graph, "file_identity_epochs")
                epoch.update(epoch_id="f" * 32, birth_json='{"version":1}')
                insert_row(db, "file_identity_epochs", epoch)
        assert (
            db.execute("SELECT * FROM file_identity_provision_intents").fetchall()
            == before
        )


@pytest.mark.parametrize(
    "field", ["intent_id", "authority_id", "payload_json", "payload_sha256"]
)
@pytest.mark.parametrize("value", [None, "", 1])
def test_intent_row_types_are_closed(
    producer_media: ProducerMedia,
    candidate_graph: SyntheticGraph,
    field: str,
    value: object,
) -> None:
    intent = graph_row(candidate_graph, "file_identity_provision_intents")
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        _ = db.execute("PRAGMA foreign_keys=ON")
        insert_row(
            db,
            "file_identity_authorities",
            graph_row(candidate_graph, "file_identity_authorities"),
        )
        _ = db.execute("SAVEPOINT positive")
        insert_row(db, "file_identity_provision_intents", intent)
        _ = db.execute("ROLLBACK TO positive")
        intent[field] = value
        error = (
            sqlite3.OperationalError
            if field == "payload_json" and value == ""
            else sqlite3.IntegrityError
        )
        with pytest.raises(error):
            insert_row(db, "file_identity_provision_intents", intent)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", True),
        ("intent_id", "f" * 32),
        ("allocation_token", "f" * 32),
        ("epoch_id", "f" * 32),
        ("authority_id", "f" * 32),
        ("authority_incarnation", "f" * 32),
        ("origin_path", "/foreign"),
        ("allocation_path", "/foreign"),
        ("root_path", "/foreign"),
        ("namespace_owner", {"domain": "namespace", "key": ["/foreign"]}),
        ("plan_sha256", "f" * 64),
    ],
)
def test_reserved_epoch_completes_only_its_own_intent(
    producer_media: ProducerMedia,
    candidate_graph: SyntheticGraph,
    field: str,
    value: object,
) -> None:
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        insert_row(
            db,
            "file_identity_authorities",
            graph_row(candidate_graph, "file_identity_authorities"),
        )
        insert_row(
            db,
            "file_identity_provision_intents",
            graph_row(candidate_graph, "file_identity_provision_intents"),
        )
        epoch = graph_row(candidate_graph, "file_identity_epochs")
        _ = db.execute("SAVEPOINT positive")
        insert_row(db, "file_identity_epochs", epoch)
        _ = db.execute("ROLLBACK TO positive")
        birth = parse_document(cast(str, epoch["birth_json"]))
        controlled = wire_object(birth["controlled_birth"])
        controlled[field] = value
        _ = controlled.pop("receipt_sha256")
        controlled["receipt_sha256"] = digest(controlled)
        epoch["birth_json"] = encoded(birth)
        with pytest.raises(sqlite3.IntegrityError):
            insert_row(db, "file_identity_epochs", epoch)


@pytest.mark.parametrize(
    "mode",
    [
        "no-preparation",
        "prepared-only",
        "foreign-set",
        "wrong-kind",
        "stale-cas",
        "intervening",
        "repeat-revoke",
        "foreign-prior",
        "changed-consumer",
    ],
)
def test_capability_chain_refusals(
    producer_media: ProducerMedia, sql_graph: SyntheticGraph, mode: str
) -> None:
    events = [
        wire_object(i["row"]).copy()
        for i in sql_graph.rows
        if i["table"] == "file_identity_events"
    ]
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        for event in events[:2]:
            insert_row(db, "file_identity_events", event)
        _ = db.execute("SAVEPOINT eligible")
        insert_row(db, "file_identity_events", events[2])
        _ = db.execute("ROLLBACK TO eligible")
        grant = events[2]
        if mode in {"repeat-revoke", "foreign-prior", "changed-consumer"}:
            insert_row(db, "file_identity_events", grant)
            payload = parse_document(cast(str, grant["payload_json"]))
            payload.update(
                state="revoke", prior_capability_event_id=3, plan_sha256="f" * 64
            )
            grant.update(id=4, revision=4, predecessor_event_id=3)
            if mode == "foreign-prior":
                payload["prior_capability_event_id"] = 2
            elif mode == "changed-consumer":
                payload["consumer_contract_sha256"] = "f" * 64
            else:
                grant["payload_json"] = encoded(payload)
                insert_row(db, "file_identity_events", grant)
                grant.update(id=5, revision=5, predecessor_event_id=4)
            grant["payload_json"] = encoded(payload)
        elif mode == "foreign-set":
            proof = graph_row(sql_graph, "file_identity_sets")
            proof["set_id"] = "f" * 32
            insert_row(db, "file_identity_sets", proof)
            grant["set_id"] = proof["set_id"]
        elif mode == "wrong-kind":
            grant["kind"] = "superseded"
        elif mode == "stale-cas":
            grant["predecessor_event_id"] = 1
        elif mode == "intervening":
            retained = {
                **grant,
                "kind": "retained",
                "payload_json": encoded(
                    {
                        "version": 1,
                        "before_snapshot_json": parse_document(
                            cast(str, grant["payload_json"])
                        )["before_snapshot_json"],
                        "before_snapshot_sha256": parse_document(
                            cast(str, grant["payload_json"])
                        )["before_snapshot_sha256"],
                        "plan_sha256": "f" * 64,
                    }
                ),
            }
            insert_row(db, "file_identity_events", retained)
            grant.update(id=4, revision=4, predecessor_event_id=3)
        else:
            db.rollback()
            # Keep the set/epoch fixture, which was committed before this test.
            if mode == "prepared-only":
                insert_row(db, "file_identity_events", events[0])
            grant.update(
                id=2 if mode == "prepared-only" else 1,
                revision=2 if mode == "prepared-only" else 1,
                predecessor_event_id=1 if mode == "prepared-only" else None,
            )
        with pytest.raises(sqlite3.IntegrityError):
            insert_row(db, "file_identity_events", grant)


def test_schema7_writer_rejects8_without_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_file_identity_schema import _V5_VALIDATOR

    path = tmp_path / "old-writer.db"
    monkeypatch.setattr(shared, "DB_PATH", str(path))
    schema.init_db()
    before = path.read_bytes()
    program = (
        "import sqlite3,sys\nfrom typing import cast\n_SUPPORTED_SCHEMA_VERSION=7\n"
        + _V5_VALIDATOR
        + "\nwith sqlite3.connect(sys.argv[1]) as db:\n try:\n  _validate_schema_version(db)\n except RuntimeError:\n  sys.exit(0)\n raise AssertionError('old writer accepted schema8')\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", program, str(path)],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert path.read_bytes() == before


def test_exact_intent_and_capability_key_sets(
    producer_media: ProducerMedia, candidate_graph: SyntheticGraph
) -> None:
    intent = graph_row(candidate_graph, "file_identity_provision_intents")
    events = [
        wire_object(i["row"]).copy()
        for i in candidate_graph.rows
        if i["table"] == "file_identity_events"
    ]
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        insert_row(
            db,
            "file_identity_authorities",
            graph_row(candidate_graph, "file_identity_authorities"),
        )
        _ = db.execute("SAVEPOINT intent_positive")
        insert_row(db, "file_identity_provision_intents", intent)
        _ = db.execute("ROLLBACK TO intent_positive")
        payload = parse_document(cast(str, intent["payload_json"]))
        for field in payload:
            partial = {k: v for k, v in payload.items() if k != field}
            with pytest.raises(sqlite3.IntegrityError):
                insert_row(
                    db,
                    "file_identity_provision_intents",
                    {
                        **intent,
                        "payload_json": encoded(partial),
                        "payload_sha256": digest(partial),
                    },
                )
        insert_row(db, "file_identity_provision_intents", intent)
        insert_row(
            db,
            "file_identity_epochs",
            graph_row(candidate_graph, "file_identity_epochs"),
        )
        insert_row(
            db, "file_identity_sets", graph_row(candidate_graph, "file_identity_sets")
        )
        for event in events[:2]:
            insert_row(db, "file_identity_events", event)
        _ = db.execute("SAVEPOINT grant_positive")
        insert_row(db, "file_identity_events", events[2])
        _ = db.execute("ROLLBACK TO grant_positive")
        payload = parse_document(cast(str, events[2]["payload_json"]))
        for field in payload:
            partial = {k: v for k, v in payload.items() if k != field}
            with pytest.raises(sqlite3.IntegrityError):
                insert_row(
                    db,
                    "file_identity_events",
                    {**events[2], "payload_json": encoded(partial)},
                )


def test_intent_payload_must_be_sql_text(
    producer_media: ProducerMedia, candidate_graph: SyntheticGraph
) -> None:
    intent = graph_row(candidate_graph, "file_identity_provision_intents")
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        insert_row(
            db,
            "file_identity_authorities",
            graph_row(candidate_graph, "file_identity_authorities"),
        )
        _ = db.execute("SAVEPOINT positive")
        insert_row(db, "file_identity_provision_intents", intent)
        _ = db.execute("ROLLBACK TO positive")
        intent["payload_json"] = cast(str, intent["payload_json"]).encode()
        with pytest.raises(sqlite3.IntegrityError):
            insert_row(db, "file_identity_provision_intents", intent)


def test_revoke_does_not_assume_event_id_chronology(
    producer_media: ProducerMedia, sql_graph: SyntheticGraph
) -> None:
    events = [
        wire_object(i["row"]).copy()
        for i in sql_graph.rows
        if i["table"] == "file_identity_events"
    ]
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        for event in events[:2]:
            insert_row(db, "file_identity_events", event)
        grant = events[2]
        grant["id"] = 20
        insert_row(db, "file_identity_events", grant)
        payload = parse_document(cast(str, grant["payload_json"]))
        payload.update(
            state="revoke", prior_capability_event_id=20, plan_sha256="f" * 64
        )
        revoke = {
            **grant,
            "id": 10,
            "revision": 4,
            "predecessor_event_id": 20,
            "payload_json": encoded(payload),
        }
        insert_row(db, "file_identity_events", revoke)
        with pytest.raises(sqlite3.IntegrityError):
            insert_row(
                db,
                "file_identity_events",
                {**revoke, "id": 30, "revision": 5, "predecessor_event_id": 10},
            )


@pytest.mark.parametrize("collision", ["epoch_id", "allocation_path", "root_path"])
def test_intent_reservation_checks_preexisting_epochs(
    producer_media: ProducerMedia, candidate_graph: SyntheticGraph, collision: str
) -> None:
    intent = graph_row(candidate_graph, "file_identity_provision_intents")
    proposed = graph_row(candidate_graph, "file_identity_epochs")
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        insert_row(
            db,
            "file_identity_authorities",
            graph_row(candidate_graph, "file_identity_authorities"),
        )
        _ = db.execute("SAVEPOINT eligible")
        insert_row(db, "file_identity_provision_intents", intent)
        _ = db.execute("ROLLBACK TO eligible")
        existing = {
            **proposed,
            "epoch_id": "f" * 32,
            "origin_path": "/legacy",
            "allocation_path": "/legacy/allocation",
            "root_path": "/legacy/allocation/root",
            "birth_json": '{"version":1}',
        }
        existing[collision] = proposed[collision]
        insert_row(db, "file_identity_epochs", existing)
        with pytest.raises(sqlite3.IntegrityError, match="reserved provisioning tuple"):
            insert_row(db, "file_identity_provision_intents", intent)


@pytest.mark.parametrize("revoked", [False, True])
def test_no_regrant_even_after_a_fresh_complete_activation(
    producer_media: ProducerMedia, sql_graph: SyntheticGraph, revoked: bool
) -> None:
    events = [
        wire_object(i["row"]).copy()
        for i in sql_graph.rows
        if i["table"] == "file_identity_events"
    ]
    original = graph_row(sql_graph, "file_identity_sets")
    successor = {**original, "set_id": "f" * 32, "prior_set_id": original["set_id"]}

    def fresh_activation(db: sqlite3.Connection, previous: int) -> dict[str, object]:
        insert_row(db, "file_identity_sets", successor)
        payload = {
            "version": 1,
            "snapshot_sha256": successor["snapshot_sha256"],
            "manifest_sha256": successor["manifest_sha256"],
        }
        for offset, kind in enumerate(("prepared", "activated"), 1):
            insert_row(
                db,
                "file_identity_events",
                {
                    **events[0],
                    "id": previous + offset,
                    "revision": previous + offset,
                    "predecessor_event_id": previous + offset - 1,
                    "set_id": successor["set_id"],
                    "kind": kind,
                    "payload_json": encoded(payload),
                },
            )
        grant = events[2].copy()
        capability = parse_document(cast(str, grant["payload_json"]))
        capability["activation_event_id"] = previous + 2
        grant.update(
            id=previous + 3,
            revision=previous + 3,
            predecessor_event_id=previous + 2,
            set_id=successor["set_id"],
            payload_json=encoded(capability),
        )
        return grant

    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        _ = db.execute("SAVEPOINT positive")
        for event in events[:2]:
            insert_row(db, "file_identity_events", event)
        insert_row(db, "file_identity_events", fresh_activation(db, 2))
        _ = db.execute("ROLLBACK TO positive")
        for event in events:
            insert_row(db, "file_identity_events", event)
        previous = 3
        if revoked:
            capability = parse_document(cast(str, events[2]["payload_json"]))
            capability.update(state="revoke", prior_capability_event_id=3)
            insert_row(
                db,
                "file_identity_events",
                {
                    **events[2],
                    "id": 4,
                    "revision": 4,
                    "predecessor_event_id": 3,
                    "payload_json": encoded(capability),
                },
            )
            previous = 4
        regrant = fresh_activation(db, previous)
        with pytest.raises(
            sqlite3.IntegrityError, match="invalid identity event set/payload"
        ):
            insert_row(db, "file_identity_events", regrant)


@pytest.mark.parametrize("scope", ["/", "/a%_", "/a-b"])
def test_scope_containment_is_component_exact(
    producer_media: ProducerMedia, candidate_graph: SyntheticGraph, scope: str
) -> None:
    """SQL syntax/equality controls; only Python validates complete plan hashes."""
    intent = graph_row(candidate_graph, "file_identity_provision_intents")
    authority = graph_row(candidate_graph, "file_identity_authorities")
    payload = parse_document(cast(str, intent["payload_json"]))
    plan = parse_document(cast(str, payload["plan_json"]))
    declaration = parse_document(cast(str, plan["declaration_json"]))
    epochs = parse_document(cast(str, plan["epoch_bindings_json"]))
    new_scope = {**wire_object(payload["scope"]), "scope_path": scope}
    payload["scope"] = new_scope
    authority["declaration_json"] = encoded(new_scope)
    cast(list[dict[str, object]], declaration["authorities"])[0]["scope"] = new_scope
    for origin in (scope.rstrip("/") + "/child", scope + "-outside"):
        allocation = origin + "/.mangarr-epoch-" + cast(str, payload["epoch_id"])
        payload.update(
            origin_path=origin,
            allocation_path=allocation,
            root_path=allocation + "/.mangarr-claims",
            namespace_owner={"domain": "namespace", "key": [allocation]},
        )
        selected = cast(list[dict[str, object]], epochs["provision"])[0]
        selected.update(
            origin_parent=origin,
            allocation_parent=allocation,
            root_path=payload["root_path"],
        )
        plan.update(
            declaration_json=encoded(declaration), epoch_bindings_json=encoded(epochs)
        )
        payload["plan_json"] = encoded(plan)
        candidate = {
            **intent,
            "payload_json": encoded(payload),
            "payload_sha256": digest(payload),
        }
        with closing(sqlite3.connect(producer_media.db_path)) as db, db:
            insert_row(db, "file_identity_authorities", authority)
            if origin.endswith("-outside") and scope != "/":
                with pytest.raises(sqlite3.IntegrityError):
                    insert_row(db, "file_identity_provision_intents", candidate)
            else:
                insert_row(db, "file_identity_provision_intents", candidate)
            db.rollback()


@pytest.mark.parametrize("revision", [7, 8])
def test_ordinary_inventory2_receipt_revision_is_explicit(revision: int) -> None:
    from test_file_identity_plan import (
        empty_inventory,
        linked_documents,
        review_documents,
        resign_documents,
    )

    original = empty_inventory()
    inventory = recovery.OwnershipInventory.create(
        original.parent, original.closure, (), (), original.budget, inventory_version=2
    )
    selection, declaration, receipt = (
        parse_document(s) for s in linked_documents(review_documents(inventory), [])
    )
    wire_object(receipt["archive"])["schema_version"] = revision
    assert (
        recovery.build_recovery_plan(
            inventory, *resign_documents(selection, declaration, receipt)
        ).version
        == 2
    )


@pytest.mark.parametrize("inventory_version", [1, 2])
@pytest.mark.parametrize("revision", [7, 8])
def test_historical_ordinary_receipt_schema_bound(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    inventory_version: int,
    revision: int,
) -> None:
    graph = synthetic_intent(producer_media, monkeypatch)
    prior = parse_document(graph.prior.to_json())
    wire_object(prior["inventory"])["version"] = inventory_version
    receipt = parse_document(cast(str, prior["backup_receipt_json"]))
    wire_object(receipt["archive"])["schema_version"] = revision
    prior["backup_receipt_json"] = encoded(receipt)
    payload = resigned_historical_intent(graph, prior)
    if inventory_version == 1 and revision == 8:
        with pytest.raises(InvalidIdentity):
            _ = store._provision_intent(encoded(payload))
    else:
        assert (
            store._provision_intent(encoded(payload))["plan_json"]
            == payload["plan_json"]
        )


@pytest.mark.parametrize("revision", [7, 8])
def test_historical_new_ledger_requires_receipt8(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    revision: int,
) -> None:
    from test_file_identity_plan import (
        linked_documents,
        review_documents,
        resign_documents,
    )

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
    selection, declaration, receipt = (
        parse_document(s) for s in linked_documents(review_documents(inventory), [])
    )
    wire_object(receipt["archive"])["schema_version"] = 8
    approved = recovery.build_recovery_plan(
        inventory, *resign_documents(selection, declaration, receipt)
    )
    prior = parse_document(approved.to_json())
    receipt = parse_document(cast(str, prior["backup_receipt_json"]))
    wire_object(receipt["archive"])["schema_version"] = revision
    prior["backup_receipt_json"] = encoded(receipt)
    payload = resigned_historical_intent(graph, prior)
    plan_json = cast(str, payload["plan_json"])
    if revision == 7:
        with pytest.raises(InvalidIdentity):
            _ = store._prior_plan(plan_json)
    else:
        assert store._prior_plan(plan_json)["inventory"] == prior["inventory"]


@pytest.mark.parametrize("stage", ["table", "drop-trigger", "create-trigger", "stamp"])
def test_schema8_failed_migration_restores_original_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    path = tmp_path / "rollback.db"
    initialize_schema7(path, monkeypatch)
    with closing(sqlite3.connect(path)) as db, db:
        insert_rescan(db)
        seed_v1_sql_history(db)
        original_schema7_catalog = catalog(db)
        original_rows = db.execute("SELECT * FROM rescan_file_operations").fetchall()
        original_ledger = legacy_rows(db)

    class FailureConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters: object = (), /) -> sqlite3.Cursor:
            match = {
                "table": "CREATE TABLE file_identity_provision_intents",
                "drop-trigger": "DROP TRIGGER file_identity_events_validate",
                "create-trigger": "CREATE TRIGGER file_identity_events_validate BEFORE",
                "stamp": "PRAGMA user_version=8",
            }[stage] in sql
            if match and stage == "stamp":
                raise RuntimeError("injected migration failure")
            result = super().execute(
                sql, cast(Sequence[object] | Mapping[str, object], parameters)
            )
            if match:
                raise RuntimeError("injected migration failure")
            return result

    @contextmanager
    def failing_db() -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(path, factory=FailureConnection)) as db:
            db.row_factory = sqlite3.Row
            yield db

    monkeypatch.setattr(schema, "get_db", failing_db)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        schema.init_db()
    with closing(sqlite3.connect(path)) as db:
        failed_migration_catalog = catalog(db)
        assert failed_migration_catalog == original_schema7_catalog
        assert db.execute("PRAGMA user_version").fetchone() == (7,)
        assert (
            db.execute("SELECT * FROM rescan_file_operations").fetchall()
            == original_rows
        )
        assert legacy_rows(db) == original_ledger
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            _ = db.execute("UPDATE file_identity_events SET payload_json='{}'")
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def catalog(db: sqlite3.Connection) -> list[tuple[object, ...]]:
    return db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    ).fetchall()


def legacy_rows(db: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    return {
        table: db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        for table in (
            "file_identity_authorities",
            "file_identity_epochs",
            "file_identity_sets",
            "file_identity_events",
            "file_claim_namespaces",
            "rescan_file_operations",
        )
    }


def seed_v1_sql_history(db: sqlite3.Connection) -> None:
    """Historical SQL acceptance fixture, not a native/profile qualification."""
    snapshot = store.capture_owner_snapshot(db, OwnerRef("rescan", (1,)))
    insert_row(
        db,
        "file_identity_authorities",
        {
            "authority_id": "a" * 32,
            "incarnation_token": "b" * 32,
            "profile_json": '{ "historical" : "profile bytes" }\n',
            "declaration_json": '{ "version":1, "scope_path":"/library", "scope_token":"'
            + "c" * 32
            + '" }\n',
            "qualification_sha256": "d" * 64,
        },
    )
    insert_row(
        db,
        "file_identity_epochs",
        {
            "epoch_id": "e" * 32,
            "authority_id": "a" * 32,
            "origin_path": "/library",
            "allocation_path": "/library/old-allocation",
            "root_path": "/library/old-allocation/root",
            "birth_json": '{ "version":1, "receipt":"raw legacy bytes" }\n',
        },
    )
    insert_row(
        db,
        "file_identity_sets",
        {
            "set_id": "1" * 32,
            "owner_domain": "rescan",
            "owner_key_json": encoded({"version": 1, "key": [1]}),
            "prior_set_id": None,
            "snapshot_json": snapshot.rows_json,
            "snapshot_sha256": snapshot.sha256,
            "origin": "birth",
            "epoch_id": "e" * 32,
            "manifest_json": '{ "historical":true }\n',
            "manifest_sha256": hashlib.sha256(b'{ "historical":true }\n').hexdigest(),
        },
    )
    for revision, kind in enumerate(("prepared", "activated"), 1):
        insert_row(
            db,
            "file_identity_events",
            {
                "id": revision,
                "owner_domain": "rescan",
                "owner_key_json": encoded({"version": 1, "key": [1]}),
                "revision": revision,
                "predecessor_event_id": None if revision == 1 else 1,
                "set_id": "1" * 32,
                "kind": kind,
                "payload_json": encoded(
                    {
                        "version": 1,
                        "snapshot_sha256": snapshot.sha256,
                        "manifest_sha256": hashlib.sha256(
                            b'{ "historical":true }\n'
                        ).hexdigest(),
                    }
                ),
            },
        )


@pytest.mark.parametrize(
    ("mode", "accepted"),
    [
        (kind, True)
        for kind in (
            "prepared",
            "activated",
            "recovery_intent",
            "recovery_step",
            "superseded",
            "retained",
            "retired",
        )
    ]
    + [
        (mode, False)
        for mode in (
            "owner-version-bool",
            "owner-key-bool",
            "owner-key-zero",
            "owner-extra",
            "owner-domain",
            "owner-mismatch",
            "stale-predecessor",
            "skipped-revision",
            "foreign-set",
            "payload-version-bool",
            "payload-extra",
            "payload-array",
            "plan-digest",
            "snapshot-bytes",
            "snapshot-digest",
            "manifest-digest",
            "standard-extra",
            "standard-snapshot",
            "preparation-prior",
            "activation-unprepared",
            "duplicate-prepared",
            "duplicate-activated",
        )
    ],
)
def test_v1_event_guards_schema7_schema8_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, accepted: bool
) -> None:
    """Preserve predecessor SQL acceptance, not a new native authority proof."""
    results: list[bool] = []
    for revision in (7, 8):
        path = tmp_path / f"parity-{revision}.db"
        initialize_schema7(path, monkeypatch)
        monkeypatch.setattr(shared, "DB_PATH", str(path))
        with closing(sqlite3.connect(path)) as db, db:
            _ = db.execute("PRAGMA foreign_keys=ON")
            insert_rescan(db)
            seed_v1_sql_history(db)
            original_catalog = catalog(db)
            original_rows = legacy_rows(db)
            trigger = next(
                row[3]
                for row in original_catalog
                if row[1] == "file_identity_events_validate"
            )
            assert (
                hashlib.sha256(cast(str, trigger).encode()).hexdigest()
                == "fd7a0970fe0aed5973684e90e2213520ca7e869030275d5f33d5ce4cd0eb0f17"
            )
        if revision == 8:
            schema.init_db()
        with closing(sqlite3.connect(path)) as db, db:
            _ = db.execute("PRAGMA foreign_keys=ON")
            assert db.execute("PRAGMA user_version").fetchone() == (revision,)
            assert legacy_rows(db) == original_rows
            current_catalog = {row[1]: row for row in catalog(db)}
            for row in original_catalog:
                if row[1] != "file_identity_events_validate" or revision == 7:
                    assert current_catalog[row[1]] == row
            db.row_factory = sqlite3.Row
            raw = cast(
                sqlite3.Row, db.execute("SELECT * FROM file_identity_sets").fetchone()
            )
            proof = {name: cast(object, raw[name]) for name in raw.keys()}
            db.row_factory = None
            payload: dict[str, object] = {
                "version": 1,
                "before_snapshot_json": proof["snapshot_json"],
                "before_snapshot_sha256": proof["snapshot_sha256"],
                "plan_sha256": "f" * 64,
            }
            event: dict[str, object] = {
                "id": 3,
                "owner_domain": "rescan",
                "owner_key_json": proof["owner_key_json"],
                "revision": 3,
                "predecessor_event_id": 2,
                "set_id": proof["set_id"],
                "kind": mode if accepted else "retained",
                "payload_json": encoded(payload),
            }
            # Every refusal has an eligible recovery insertion on this same DB.
            _ = db.execute("SAVEPOINT eligible_v1")
            insert_row(db, "file_identity_events", {**event, "kind": "retained"})
            _ = db.execute("ROLLBACK TO eligible_v1")
            if mode in {
                "prepared",
                "activated",
                "preparation-prior",
                "activation-unprepared",
                "standard-extra",
                "standard-snapshot",
                "manifest-digest",
            }:
                proof.update(
                    set_id="2" * 32,
                    prior_set_id=None if mode == "preparation-prior" else "1" * 32,
                )
                insert_row(db, "file_identity_sets", proof)
                payload = {
                    "version": 1,
                    "snapshot_sha256": proof["snapshot_sha256"],
                    "manifest_sha256": proof["manifest_sha256"],
                }
                event.update(
                    set_id=proof["set_id"],
                    kind="prepared",
                    payload_json=encoded(payload),
                )
                if mode == "activated":
                    insert_row(db, "file_identity_events", event)
                    event.update(
                        id=4, revision=4, predecessor_event_id=3, kind="activated"
                    )
                elif mode == "activation-unprepared":
                    event["kind"] = "activated"
                elif mode == "standard-extra":
                    payload["extra"] = 1
                elif mode == "standard-snapshot":
                    payload["snapshot_sha256"] = "f" * 64
                elif mode == "manifest-digest":
                    payload["manifest_sha256"] = "f" * 64
            owner = {"version": 1, "key": [1]}
            if mode == "owner-version-bool":
                event["owner_key_json"] = encoded({**owner, "version": True})
            elif mode in {"owner-key-bool", "owner-key-zero"}:
                event["owner_key_json"] = encoded(
                    {**owner, "key": [True if mode.endswith("bool") else 0]}
                )
            elif mode == "owner-extra":
                event["owner_key_json"] = encoded({**owner, "extra": 1})
            elif mode in {"owner-domain", "owner-mismatch"}:
                event.update(revision=1, predecessor_event_id=None)
                if mode == "owner-domain":
                    event.update(
                        owner_domain="namespace",
                        owner_key_json=encoded({"version": 1, "key": ["/other"]}),
                    )
                else:
                    event["owner_key_json"] = encoded({**owner, "key": [2]})
            elif mode == "stale-predecessor":
                event["predecessor_event_id"] = 1
            elif mode == "skipped-revision":
                event["revision"] = 4
            elif mode == "foreign-set":
                event["set_id"] = "f" * 32
            elif mode == "payload-version-bool":
                payload["version"] = True
            elif mode == "payload-extra":
                payload["extra"] = 1
            elif mode == "plan-digest":
                payload["plan_sha256"] = "g" * 64
            elif mode == "snapshot-bytes":
                payload["before_snapshot_json"] = (
                    cast(str, payload["before_snapshot_json"]) + "\n"
                )
            elif mode == "snapshot-digest":
                payload["before_snapshot_sha256"] = "f" * 64
            elif mode in {"duplicate-prepared", "duplicate-activated"}:
                event["kind"] = mode.removeprefix("duplicate-")
                payload = {
                    "version": 1,
                    "snapshot_sha256": proof["snapshot_sha256"],
                    "manifest_sha256": proof["manifest_sha256"],
                }
            event["payload_json"] = (
                "[]" if mode == "payload-array" else encoded(payload)
            )
            before = legacy_rows(db)
            if accepted:
                insert_row(db, "file_identity_events", event)
                results.append(True)
            else:
                with pytest.raises(sqlite3.IntegrityError):
                    insert_row(db, "file_identity_events", event)
                assert legacy_rows(db) == before
                results.append(False)
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert results == [accepted, accepted]


def test_real_schema7_predecessor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "predecessor.db"
    initialize_schema7(path, monkeypatch)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (7,)
        trigger = cast(
            str,
            db.execute(
                "SELECT sql FROM sqlite_master WHERE name='file_identity_events_validate'"
            ).fetchone()[0],
        )
        assert "allocation-capability" not in trigger
        assert (
            hashlib.sha256(trigger.encode()).hexdigest()
            == "fd7a0970fe0aed5973684e90e2213520ca7e869030275d5f33d5ce4cd0eb0f17"
        )
        assert "json_extract(NEW.payload_json,'$.version')=1" in trigger
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='file_identity_provision_intents'"
            ).fetchall()
            == []
        )


def test_fresh_schema8(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "fresh.db"
    monkeypatch.setattr(shared, "DB_PATH", str(path))
    schema.init_db()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (8,)
        columns = cast(
            list[tuple[object, ...]],
            db.execute("PRAGMA table_info(file_identity_provision_intents)").fetchall(),
        )
        assert [r[1] for r in columns] == [
            "intent_id",
            "authority_id",
            "payload_json",
            "payload_sha256",
        ]
        for table in ("authorities", "epochs", "sets", "events", "provision_intents"):
            assert db.execute(f"SELECT * FROM file_identity_{table}").fetchall() == []


def test_schema8_preserves_v1_rows_and_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "upgrade.db"
    initialize_schema7(path, monkeypatch)
    monkeypatch.setattr(shared, "DB_PATH", str(path))
    with closing(sqlite3.connect(path)) as db, db:
        insert_rescan(db)
        _ = db.execute(
            "INSERT INTO file_claim_namespaces VALUES('/library',?)",
            ('{ "old":true }\n',),
        )
        seed_v1_sql_history(db)
        before_legacy_rows = legacy_rows(db)
        before = catalog(db)
        before_foreign_keys = {
            table: db.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            for table in before_legacy_rows
        }
        before_indexes = {
            table: db.execute(f"PRAGMA index_list({table})").fetchall()
            for table in before_legacy_rows
        }
    schema.init_db()
    schema.init_db()
    schema._migrate_schema_constraints()
    with closing(sqlite3.connect(path)) as db, db:
        schema._ensure_file_identity_schema(db)
        schema._ensure_blocklist_source_schema(db)
        schema._migrate_history_download_ownership(db)
        assert db.execute("PRAGMA user_version").fetchone() == (8,)
        after_legacy_rows = legacy_rows(db)
        assert after_legacy_rows == before_legacy_rows
        assert {
            table: db.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            for table in after_legacy_rows
        } == before_foreign_keys
        assert {
            table: db.execute(f"PRAGMA index_list({table})").fetchall()
            for table in after_legacy_rows
        } == before_indexes
        assert db.execute(
            "SELECT ownership_json FROM file_claim_namespaces"
        ).fetchone() == ('{ "old":true }\n',)
        after = {r[1]: r for r in catalog(db)}
        for item in before:
            if item[1] != "file_identity_events_validate":
                assert after[item[1]] == item
        for suffix, primary in (
            ("authorities", "authority_id"),
            ("epochs", "epoch_id"),
            ("sets", "set_id"),
            ("events", "id"),
        ):
            table = "file_identity_" + suffix
            for sql in (
                f"UPDATE {table} SET {primary}={primary}",
                f"DELETE FROM {table}",
                f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}",
            ):
                with pytest.raises(sqlite3.IntegrityError):
                    _ = db.execute(sql)
        snapshot = store.capture_owner_snapshot(db, OwnerRef("rescan", (1,)))
        retained = {
            "id": 3,
            "owner_domain": "rescan",
            "owner_key_json": encoded({"version": 1, "key": [1]}),
            "revision": 3,
            "predecessor_event_id": 2,
            "set_id": "1" * 32,
            "kind": "retained",
            "payload_json": encoded(
                {
                    "version": 1,
                    "before_snapshot_json": snapshot.rows_json,
                    "before_snapshot_sha256": snapshot.sha256,
                    "plan_sha256": "f" * 64,
                }
            ),
        }
        insert_row(db, "file_identity_events", retained)
        payload = parse_document(cast(str, retained["payload_json"]))
        payload["version"] = True
        with pytest.raises(sqlite3.IntegrityError):
            insert_row(
                db,
                "file_identity_events",
                {
                    **retained,
                    "id": 4,
                    "revision": 4,
                    "predecessor_event_id": 3,
                    "payload_json": encoded(payload),
                },
            )


def test_empty_schema8_requires_explicit_inventory2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "empty.db"
    parent = tmp_path / "media"
    parent.mkdir()
    monkeypatch.setattr(shared, "DB_PATH", str(path))
    schema.init_db()
    with file_mutation_guard(str(path)) as guard:
        default = recovery.inspect_ownership(guard, str(parent), InventoryBudget())
        explicit_v2 = recovery.inspect_ownership(
            guard, str(parent), InventoryBudget(), inventory_version=2
        )
    assert default.version == 1
    assert default.unresolved == ("reference_uncertain",)
    assert explicit_v2.version == 2
    assert explicit_v2.unresolved == ()
    assert explicit_v2.closure.raw_rows == ()
