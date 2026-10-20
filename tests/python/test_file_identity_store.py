"""SQL-only identity ledger contracts. Fixture qualification is never native."""

from __future__ import annotations

import importlib
import json
import sqlite3
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from file_identity import (
    IdentityMismatch,
    IdentityUnavailable,
    InvalidIdentity,
    NativeHandle,
    OwnerRef,
    RoleKey,
    StorageProfile,
)
from test_file_identity_schema import (
    canonical,
    digest,
    insert_rescan,
    ledger_db as ledger_db,
    ledger_path as ledger_path,
)


def store() -> ModuleType:
    try:
        return importlib.import_module("file_identity_store")
    except ModuleNotFoundError:
        pytest.fail("immutable identity store is absent")


@pytest.fixture
def enrolled(
    ledger_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> sqlite3.Connection:
    import file_identity
    from test_file_identity_backend import _candidate_contracts

    client, server = _candidate_contracts()
    profile = StorageProfile("fixture-only", 1, "a" * 64, client, server)
    monkeypatch.setattr(file_identity, "_QUALIFIED_PROFILES", (profile,))
    insert_rescan(ledger_db)
    profile_json = canonical(
        {
            "version": 1,
            "profile_id": profile.profile_id,
            "revision": 1,
            "qualification_sha256": profile.qualification_sha256,
            "client_contract": client,
            "server_contract": server,
        }
    )
    ledger_db.execute(
        "INSERT INTO file_identity_authorities(authority_id,incarnation_token,profile_json,"
        "declaration_json,qualification_sha256) VALUES(?,?,?,?,?)",
        (
            "a" * 32,
            "b" * 32,
            profile_json,
            canonical(
                {"version": 1, "scope_path": "/library", "scope_token": "c" * 32}
            ),
            "a" * 64,
        ),
    )
    ledger_db.execute(
        "INSERT INTO file_identity_epochs(epoch_id,authority_id,origin_path,allocation_path,root_path,birth_json) VALUES(?,?,?,?,?,?)",
        (
            "d" * 32,
            "a" * 32,
            "/library",
            "/library/alloc",
            "/library/alloc/root",
            canonical(
                {
                    "version": 1,
                    "layout": "dedicated_parent",
                    "enrollment": "birth",
                    "legacy_namespace": None,
                    "controlled_birth": {
                        "authority_incarnation": "b" * 32,
                        "allocation_token": "e" * 32,
                        "root_native": NativeHandle(1, b"root").to_value(),
                        "marker_native": NativeHandle(1, b"marker").to_value(),
                        "receipt_sha256": "f" * 64,
                    },
                }
            ),
        ),
    )
    ledger_db.commit()
    return ledger_db


def role_value(role: RoleKey) -> dict[str, object]:
    return {
        "file_id": role.file_id,
        "purpose": role.purpose,
        "kind": role.kind,
        "relative": role.relative,
    }


def object_value(proof: Any) -> dict[str, object]:
    return {
        "role": role_value(proof.role),
        "canonical_path": proof.canonical_path,
        "parent_role": None
        if proof.parent_role is None
        else role_value(proof.parent_role),
        "native": proof.native.to_value(),
        "constraints_json": proof.constraints_json,
    }


def constraints(owner: OwnerRef, role: RoleKey) -> str:
    return canonical(
        {
            "version": 1,
            "legacy": None,
            "binding": {
                "owner": {"domain": owner.domain, "key": list(owner.key)},
                "role": role_value(role),
                "operation_key": "fixture",
                "owner_token": "b" * 32,
            },
            "policy": "private_directory",
            "non_device": {
                "inode": 9,
                "size": None,
                "mtime_ns": None,
                "sha256": None,
                "uid": 1000,
                "mode": 448,
                "nlink": None,
            },
        }
    )


def proof_for(
    db: sqlite3.Connection, owner: OwnerRef | None = None, set_id: str = "1" * 32
) -> Any:
    module = store()
    owner = owner or OwnerRef("rescan", (1,))
    snapshot = module.capture_owner_snapshot(db, owner)
    root = RoleKey(None, "root", "directory", None)
    role = module.ObjectProof(
        root,
        "/library/alloc/root",
        None,
        NativeHandle(1, b"root"),
        constraints(owner, root),
    )
    manifest = canonical(
        {
            "version": 1,
            "snapshot_sha256": snapshot.sha256,
            "phase": "observed",
            "required_roles": [role_value(root)],
            "roles": [object_value(role)],
            "absent_roles": [],
            "required_dependencies": [],
            "dependencies": [],
            "intent": None,
        }
    )
    return module.ProofSet(
        set_id, owner, snapshot, "a" * 32, "d" * 32, "birth", (role,), manifest
    )


def append_event(
    db: sqlite3.Connection,
    proof: Any,
    kind: str = "superseded",
    *,
    payload: str | None = None,
) -> int:
    key = canonical({"version": 1, "key": list(proof.owner.key)})
    row = db.execute(
        "SELECT id,revision FROM file_identity_events WHERE owner_domain=? AND owner_key_json=? ORDER BY revision DESC LIMIT 1",
        (proof.owner.domain, key),
    ).fetchone()
    value = payload or canonical(
        {
            "version": 1,
            "before_snapshot_json": proof.snapshot.rows_json,
            "before_snapshot_sha256": proof.snapshot.sha256,
            "plan_sha256": "9" * 64,
        }
    )
    cursor = db.execute(
        "INSERT INTO file_identity_events(owner_domain,owner_key_json,revision,predecessor_event_id,set_id,kind,payload_json) VALUES(?,?,?,?,?,?,?)",
        (proof.owner.domain, key, row[1] + 1, row[0], proof.set_id, kind, value),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def activate(db: sqlite3.Connection, proof: Any, previous: int | None = None) -> int:
    module = store()
    db.execute("BEGIN IMMEDIATE")
    prepared = module.prepare_set(db, proof, previous)
    activated = module.activate_set(db, proof, proof.snapshot, prepared)
    return activated


def test_prepare_activate_committed_roundtrip_and_frozen_values(
    enrolled: sqlite3.Connection,
) -> None:
    module = store()
    proof = proof_for(enrolled)
    event = activate(enrolled, proof)
    with pytest.raises(IdentityUnavailable):
        module.read_active(enrolled, proof.owner)
    enrolled.commit()
    active = module.read_active(enrolled, proof.owner)
    assert active.proof == proof
    assert active.event_id == event
    with pytest.raises(FrozenInstanceError):
        proof.origin = "recovery"


def test_no_history_is_only_none_case(enrolled: sqlite3.Connection) -> None:
    module = store()
    proof = proof_for(enrolled)
    assert module.read_active(enrolled, proof.owner) is None
    enrolled.execute("BEGIN IMMEDIATE")
    module.prepare_set(enrolled, proof, None)
    enrolled.commit()
    with pytest.raises(IdentityMismatch):
        module.read_active(enrolled, proof.owner)


@pytest.mark.parametrize(
    "kind",
    [
        "prepared",
        "recovery_intent",
        "recovery_step",
        "superseded",
        "retained",
        "retired",
    ],
)
def test_latest_nonactivation_never_resurrects_old_set(
    enrolled: sqlite3.Connection, kind: str
) -> None:
    module = store()
    proof = proof_for(enrolled)
    event = activate(enrolled, proof)
    enrolled.commit()
    enrolled.execute("BEGIN IMMEDIATE")
    if kind == "prepared":
        module.prepare_set(enrolled, replace(proof, set_id="2" * 32), event)
    else:
        append_event(enrolled, proof, kind)
    enrolled.commit()
    with pytest.raises(IdentityMismatch):
        module.read_active(enrolled, proof.owner)


def test_unsupported_prepare_leaves_no_history(
    enrolled: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import file_identity

    module = store()
    proof = proof_for(enrolled)
    monkeypatch.setattr(file_identity, "_QUALIFIED_PROFILES", ())
    enrolled.execute("BEGIN IMMEDIATE")
    with pytest.raises(IdentityUnavailable):
        module.prepare_set(enrolled, proof, None)
    assert enrolled.execute("SELECT * FROM file_identity_sets").fetchall() == []
    assert enrolled.execute("SELECT * FROM file_identity_events").fetchall() == []
    enrolled.commit()
    assert module.read_active(enrolled, proof.owner) is None


def test_unqualified_readback_refuses_not_none(
    enrolled: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import file_identity

    module = store()
    proof = proof_for(enrolled)
    activate(enrolled, proof)
    enrolled.commit()
    monkeypatch.setattr(file_identity, "_QUALIFIED_PROFILES", ())
    with pytest.raises(IdentityUnavailable):
        module.read_active(enrolled, proof.owner)


def test_rollback_and_second_connection_visibility(
    enrolled: sqlite3.Connection, ledger_path: Path
) -> None:
    module = store()
    proof = proof_for(enrolled)
    with sqlite3.connect(ledger_path) as reader:
        activate(enrolled, proof)
        assert module.read_active(reader, proof.owner) is None
        enrolled.rollback()
        assert module.read_active(reader, proof.owner) is None
        event = activate(enrolled, proof)
        enrolled.commit()
        assert module.read_active(reader, proof.owner).event_id == event


def test_failed_commit_rolls_back_all_authority(enrolled: sqlite3.Connection) -> None:
    module = store()
    proof = proof_for(enrolled)
    enrolled.execute("CREATE TABLE commit_parent(id INTEGER PRIMARY KEY)")
    enrolled.execute(
        "CREATE TABLE commit_child(id INTEGER REFERENCES commit_parent(id) DEFERRABLE INITIALLY DEFERRED)"
    )
    activate(enrolled, proof)
    enrolled.execute("INSERT INTO commit_child VALUES(1)")
    with pytest.raises(sqlite3.IntegrityError):
        enrolled.commit()
    with pytest.raises(IdentityUnavailable):
        module.read_active(enrolled, proof.owner)
    enrolled.rollback()
    assert module.read_active(enrolled, proof.owner) is None


def test_stale_snapshot_and_event_cas_refuse(enrolled: sqlite3.Connection) -> None:
    module = store()
    proof = proof_for(enrolled)
    enrolled.execute("BEGIN IMMEDIATE")
    prepared = module.prepare_set(enrolled, proof, None)
    with pytest.raises(IdentityMismatch):
        module.prepare_set(enrolled, replace(proof, set_id="2" * 32), None)
    with pytest.raises(IdentityMismatch):
        module.activate_set(enrolled, proof, proof.snapshot, prepared + 1)
    enrolled.execute(
        "UPDATE rescan_file_operations SET carriers_json='changed' WHERE id=1"
    )
    with pytest.raises(IdentityMismatch):
        module.assert_snapshot(enrolled, proof.snapshot)
    with pytest.raises(IdentityMismatch):
        module.activate_set(enrolled, proof, proof.snapshot, prepared)
    enrolled.commit()
    with pytest.raises(IdentityMismatch):
        module.read_active(enrolled, proof.owner)


def test_conflicting_activation_and_foreign_owner_refuse(
    enrolled: sqlite3.Connection,
) -> None:
    module = store()
    proof = proof_for(enrolled)
    event = activate(enrolled, proof)
    with pytest.raises(IdentityMismatch):
        module.activate_set(enrolled, proof, proof.snapshot, event)
    other = replace(proof, set_id="2" * 32)
    with pytest.raises(IdentityMismatch):
        module.activate_set(enrolled, other, proof.snapshot, event)
    with pytest.raises(InvalidIdentity):
        replace(proof, owner=OwnerRef("deletion", (1,)))
    enrolled.rollback()


@pytest.mark.parametrize(
    "table,column",
    [
        ("authorities", "incarnation_token"),
        ("epochs", "origin_path"),
        ("sets", "origin"),
        ("events", "kind"),
    ],
)
def test_immutable_rows_refuse_update_delete_and_replace(
    enrolled: sqlite3.Connection, table: str, column: str
) -> None:
    proof = proof_for(enrolled)
    activate(enrolled, proof)
    name = "file_identity_" + table
    with pytest.raises(sqlite3.IntegrityError):
        enrolled.execute(f"UPDATE {name} SET {column}={column}")
    with pytest.raises(sqlite3.IntegrityError):
        enrolled.execute(f"DELETE FROM {name}")
    row = enrolled.execute(f"SELECT * FROM {name} LIMIT 1").fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        enrolled.execute(
            f"INSERT OR REPLACE INTO {name} VALUES({','.join('?' for _ in row)})", row
        )
    enrolled.rollback()


@pytest.mark.parametrize(
    "invalid",
    [
        "{}",
        "[]",
        "NaN",
        "Infinity",
        '{"version":1,"version":1}',
        '{"version":true}',
        '{"version":1,"size":0,"sha256":null,"extra":0}',
        '{"version":1,"size":1e999,"sha256":null}',
    ],
)
def test_direct_object_rejects_invalid_constraints(invalid: str) -> None:
    module = store()
    with pytest.raises(InvalidIdentity):
        module.ObjectProof(
            RoleKey(None, "root", "directory", None),
            "/library",
            None,
            NativeHandle(1, b"r"),
            invalid,
        )


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "duplicate",
        "unexpected",
        "snapshot",
        "extra",
        "bool-version",
        "cycle",
        "dependency",
        "absent",
    ],
)
def test_manifest_rejects_incomplete_or_malformed_evidence(
    enrolled: sqlite3.Connection, change: str
) -> None:
    proof = proof_for(enrolled)
    value = json.loads(proof.manifest_json)
    if change == "missing":
        value["roles"] = []
    elif change == "duplicate":
        value["required_roles"] *= 2
    elif change == "unexpected":
        value["required_roles"] = []
    elif change == "snapshot":
        value["snapshot_sha256"] = "0" * 64
    elif change == "extra":
        value["extra"] = None
    elif change == "bool-version":
        value["version"] = True
    elif change == "cycle":
        value["roles"][0]["parent_role"] = value["roles"][0]["role"]
    elif change == "dependency":
        value["required_dependencies"] = [{"domain": "rescan", "key": [2]}]
    else:
        value["absent_roles"] = [
            {
                "role": value["required_roles"][0],
                "canonical_path": "/library/missing",
                "parent_role": value["required_roles"][0],
                "intent_event_id": 1,
            }
        ]
    with pytest.raises(InvalidIdentity):
        replace(proof, manifest_json=canonical(value))


def test_snapshot_direct_hash_key_type_and_column_validation(
    enrolled: sqlite3.Connection,
) -> None:
    module = store()
    snapshot = proof_for(enrolled).snapshot
    with pytest.raises(InvalidIdentity):
        replace(snapshot, sha256="0" * 64)
    for change in ("column", "key", "type", "extra"):
        value = json.loads(snapshot.rows_json)
        if change == "column":
            value["root"]["row"].pop("carriers_json")
        elif change == "key":
            value["root"]["key"] = [2]
        elif change == "type":
            value["root"]["row"]["id"] = True
        else:
            value["extra"] = None
        encoded = canonical(value)
        with pytest.raises(InvalidIdentity):
            module.OwnerSnapshot(snapshot.owner, encoded, digest(encoded))


def test_queue_snapshot_catches_phantom_children_and_keeps_raw_json(
    enrolled: sqlite3.Connection,
) -> None:
    module = store()
    enrolled.execute("INSERT INTO import_queue(id,status) VALUES(1,'pending')")
    enrolled.commit()
    snapshot = module.capture_owner_snapshot(enrolled, OwnerRef("queue_source", (1,)))
    enrolled.execute(
        "INSERT INTO import_queue_files(queue_id,filename) VALUES(1,'new.cbz')"
    )
    with pytest.raises(IdentityMismatch):
        module.assert_snapshot(enrolled, snapshot)
    enrolled.rollback()
    encoded = module.capture_owner_snapshot(
        enrolled, OwnerRef("rescan", (1,))
    ).rows_json
    assert (
        json.loads(encoded)["root"]["row"]["carriers_json"] == '{ "legacy" : [1, 2] }\n'
    )


@pytest.mark.parametrize(
    "failure", ["missing", "foreign", "wrong-kind", "changed-root", "changed-payload"]
)
def test_supersession_refuses_bad_event_or_current_preimage(
    enrolled: sqlite3.Connection, failure: str
) -> None:
    proof = proof_for(enrolled)
    activate(enrolled, proof)
    if failure == "missing":
        event = 999
    elif failure == "foreign":
        insert_rescan(enrolled, 2)
        enrolled.commit()
        foreign = proof_for(enrolled, OwnerRef("rescan", (2,)), "2" * 32)
        activate(enrolled, foreign)
        event = append_event(enrolled, foreign)
    elif failure == "wrong-kind":
        event = append_event(enrolled, proof, "retained")
    elif failure == "changed-payload":
        payload = canonical(
            {
                "version": 1,
                "before_snapshot_json": "{}",
                "before_snapshot_sha256": proof.snapshot.sha256,
                "plan_sha256": "9" * 64,
            }
        )
        with pytest.raises(sqlite3.IntegrityError):
            append_event(enrolled, proof, payload=payload)
        enrolled.rollback()
        return
    else:
        event = append_event(enrolled, proof)
        enrolled.execute(
            "UPDATE rescan_file_operations SET carriers_json='different' WHERE id=1"
        )
    with pytest.raises(sqlite3.IntegrityError):
        enrolled.execute(
            "UPDATE rescan_file_operations SET recovery_event_id=? WHERE id=1", (event,)
        )
    enrolled.rollback()


def test_same_transaction_supersession_atomicity_and_historical_freeze(
    enrolled: sqlite3.Connection, ledger_path: Path
) -> None:
    proof = proof_for(enrolled)
    activate(enrolled, proof)
    enrolled.commit()
    with sqlite3.connect(ledger_path) as reader:
        enrolled.execute("BEGIN IMMEDIATE")
        event = append_event(enrolled, proof)
        enrolled.execute(
            "UPDATE rescan_file_operations SET recovery_event_id=? WHERE id=1", (event,)
        )
        insert_rescan(enrolled, 2)
        assert reader.execute(
            "SELECT recovery_event_id FROM rescan_file_operations WHERE id=1"
        ).fetchone() == (None,)
        assert (
            reader.execute(
                "SELECT id FROM file_identity_events WHERE id=?", (event,)
            ).fetchone()
            is None
        )
        enrolled.rollback()
        assert enrolled.execute(
            "SELECT recovery_event_id FROM rescan_file_operations"
        ).fetchone() == (None,)
        enrolled.execute("BEGIN IMMEDIATE")
        event = append_event(enrolled, proof)
        enrolled.execute(
            "UPDATE rescan_file_operations SET recovery_event_id=? WHERE id=1", (event,)
        )
        # Same volume can now have a replacement active reservation.
        enrolled.execute(
            "INSERT INTO rescan_file_operations(operation_token,series_id,volume_id,source_path,destination_path,expected_volume_json,expected_context_json,fingerprints_json,carriers_json,state) SELECT 'replacement',series_id,volume_id,source_path,destination_path,expected_volume_json,expected_context_json,fingerprints_json,carriers_json,state FROM rescan_file_operations WHERE id=1"
        )
        enrolled.commit()
        assert reader.execute(
            "SELECT recovery_event_id FROM rescan_file_operations WHERE id=1"
        ).fetchone() == (event,)
        for sql in (
            "UPDATE rescan_file_operations SET recovery_event_id=NULL WHERE id=1",
            "UPDATE rescan_file_operations SET state='completed' WHERE id=1",
            "DELETE FROM rescan_file_operations WHERE id=1",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                enrolled.execute(sql)
        with pytest.raises(sqlite3.IntegrityError):
            enrolled.execute("DELETE FROM file_identity_events WHERE id=?", (event,))


def test_store_writes_require_transaction(enrolled: sqlite3.Connection) -> None:
    module = store()
    proof = proof_for(enrolled)
    with pytest.raises(IdentityUnavailable):
        module.prepare_set(enrolled, proof, None)
    assert not enrolled.in_transaction


@pytest.mark.parametrize(
    "domain", ["rescan", "publication", "deletion", "queue_source"]
)
def test_integer_owner_keys_never_accept_sqlite_string_alias(
    enrolled: sqlite3.Connection, domain: str
) -> None:
    module = store()
    with pytest.raises(InvalidIdentity):
        module.capture_owner_snapshot(enrolled, OwnerRef(domain, ("1",)))


def test_authority_snapshot_excludes_incidental_diagnostics(
    enrolled: sqlite3.Connection,
) -> None:
    module = store()
    owner = OwnerRef("rescan", (1,))
    before = module.capture_owner_snapshot(enrolled, owner)
    enrolled.execute(
        "UPDATE rescan_file_operations SET diagnostic='report',updated_at='later' WHERE id=1"
    )
    assert module.capture_owner_snapshot(enrolled, owner) == before
    module.assert_snapshot(enrolled, before)


def test_journal_intent_allows_phase_bound_absence_without_recovery(
    enrolled: sqlite3.Connection,
) -> None:
    module = store()
    proof = proof_for(enrolled)
    value = json.loads(proof.manifest_json)
    absent = RoleKey(7, "artifact", "file", "artifact")
    legacy = json.loads(proof.snapshot.rows_json)["root"]["row"]["carriers_json"]
    intent = {
        "kind": "journal",
        "snapshot_sha256": proof.snapshot.sha256,
        "phase": "allocated",
        "column": "carriers_json",
        "evidence_json": legacy,
        "evidence_sha256": digest(legacy),
    }
    value["phase"] = "allocated"
    value["intent"] = intent
    value["required_roles"] = sorted(
        [*value["required_roles"], role_value(absent)], key=canonical
    )
    value["absent_roles"] = [
        {
            "role": role_value(absent),
            "canonical_path": "/library/alloc/root/artifact",
            "parent_role": value["roles"][0]["role"],
            "intent_sha256": digest(canonical(intent)),
        }
    ]
    proof = replace(proof, manifest_json=canonical(value))
    activate(enrolled, proof)
    enrolled.commit()
    assert module.read_active(enrolled, proof.owner).proof == proof
    value["intent"]["evidence_json"] = "not the current journal intent"
    value["intent"]["evidence_sha256"] = digest(value["intent"]["evidence_json"])
    value["absent_roles"][0]["intent_sha256"] = digest(canonical(value["intent"]))
    with pytest.raises(InvalidIdentity):
        replace(proof, manifest_json=canonical(value))


@pytest.mark.parametrize("version", [1, 2])
def test_exact_legacy_same_parent_epoch_preserves_receipt_not_fake_birth(
    enrolled: sqlite3.Connection, version: int
) -> None:
    module = store()
    proof = proof_for(enrolled)
    legacy = '{ "version": ' + str(version) + ', "opaque": true }\n'
    receipt = canonical(
        {
            "version": 1,
            "layout": "same_parent",
            "enrollment": "exact_legacy",
            "legacy_namespace": {
                "version": version,
                "json": legacy,
                "sha256": digest(legacy),
            },
            "controlled_birth": None,
        }
    )
    enrolled.execute(
        "INSERT INTO file_identity_epochs(epoch_id,authority_id,origin_path,allocation_path,root_path,birth_json) VALUES(?,?,?,?,?,?)",
        (
            "f" * 32,
            "a" * 32,
            "/library",
            "/library",
            "/library/.mangarr-claims",
            receipt,
        ),
    )
    enrolled.commit()
    obj = proof.roles[0]
    evidence = json.loads(obj.constraints_json)
    evidence["legacy"] = {
        "kind": "namespace_v" + str(version),
        "json": legacy,
        "sha256": digest(legacy),
    }
    obj = replace(
        obj,
        canonical_path="/library/.mangarr-claims",
        constraints_json=canonical(evidence),
    )
    manifest = json.loads(proof.manifest_json)
    manifest["roles"] = [object_value(obj)]
    proof = replace(
        proof,
        epoch_id="f" * 32,
        origin="exact_legacy",
        roles=(obj,),
        manifest_json=canonical(manifest),
    )
    activate(enrolled, proof)
    enrolled.commit()
    assert module.read_active(enrolled, proof.owner).proof == proof
    assert enrolled.execute(
        "SELECT birth_json FROM file_identity_epochs WHERE epoch_id=?", ("f" * 32,)
    ).fetchone() == (receipt,)


@pytest.mark.parametrize("field", ["inode", "uid", "mode"])
def test_applicable_policy_fields_cannot_be_dropped(
    enrolled: sqlite3.Connection, field: str
) -> None:
    proof = proof_for(enrolled)
    value = json.loads(proof.roles[0].constraints_json)
    value["non_device"][field] = None
    with pytest.raises(InvalidIdentity):
        replace(proof.roles[0], constraints_json=canonical(value))


def test_long_lived_history_does_not_disable_owner(
    enrolled: sqlite3.Connection,
) -> None:
    module = store()
    proof = proof_for(enrolled)
    activate(enrolled, proof)
    for _ in range(4100):
        append_event(enrolled, proof, "retained")
    enrolled.commit()
    with pytest.raises(IdentityMismatch, match="active|activated"):
        module.read_active(enrolled, proof.owner)


def test_document_bound_refuses_explicitly(enrolled: sqlite3.Connection) -> None:
    proof = proof_for(enrolled)
    with pytest.raises(InvalidIdentity, match="bound|large|limit"):
        replace(proof.snapshot, rows_json=" " * (16 * 1024 * 1024 + 1))


def test_prepare_event_failure_leaves_no_partial_set(
    enrolled: sqlite3.Connection,
) -> None:
    module = store()
    proof = proof_for(enrolled)
    enrolled.execute(
        "CREATE TEMP TRIGGER fail_event BEFORE INSERT ON file_identity_events BEGIN SELECT RAISE(ABORT,'injected event failure'); END"
    )
    enrolled.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError, match="injected event failure"):
        module.prepare_set(enrolled, proof, None)
    assert enrolled.execute("SELECT * FROM file_identity_sets").fetchall() == []
    assert enrolled.execute("SELECT * FROM file_identity_events").fetchall() == []
    assert enrolled.in_transaction
    enrolled.rollback()


def insert_publication(db: sqlite3.Connection) -> None:
    db.execute("INSERT INTO import_queue(id,status) VALUES(1,'importing')")
    db.execute(
        "INSERT INTO import_publications(id,queue_id,state,owner_token,series_id,dst_dir,import_mode,staging_dir,queue_snapshot_json,series_tags_json,queue_status) VALUES(1,1,'staging','token',7,'/library','copy','/library/stage','{}','[]','importing')"
    )


def test_publication_notifier_updates_do_not_change_authority_snapshot(
    enrolled: sqlite3.Connection,
) -> None:
    module = store()
    insert_publication(enrolled)
    before = module.capture_owner_snapshot(enrolled, OwnerRef("publication", (1,)))
    enrolled.execute(
        "UPDATE import_publications SET notification_state='pending',notification_title='changed',diagnostic='report',updated_at='later' WHERE id=1"
    )
    module.assert_snapshot(enrolled, before)
    enrolled.execute(
        "INSERT INTO import_publication_notifications(publication_id,state,idempotency_key,title,label) VALUES(1,'pending','key','title','label')"
    )
    module.assert_snapshot(enrolled, before)
    enrolled.execute(
        "INSERT INTO import_publication_files(publication_id,ordinal,file_id,src_path,filename,dst_path,import_kind,file_type,is_special,has_volume_range,is_legacy_chapter_stub,is_legacy_chapter_recheck,plan_status) VALUES(1,1,1,'/library/a','a','/library/b','volume','archive',0,0,0,0,'ready')"
    )
    with pytest.raises(IdentityMismatch):
        module.assert_snapshot(enrolled, before)


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", 2),
        ("state", "completed"),
        ("target_path", "/library/foreign"),
        ("claim_path", "/library/foreign"),
        ("claim_carrier_json", "foreign"),
        ("target_mtime_ns", 42),
    ],
)
def test_deletion_supersession_checks_current_projected_fields(
    enrolled: sqlite3.Connection, field: str, value: object
) -> None:
    enrolled.execute(
        "INSERT INTO volume_file_deletions(id,volume_id,series_id,state,target_path,parent_path,claim_path,target_present,series_title) VALUES(1,11,7,'active','/library/a','/library','/library/claim',1,'test')"
    )
    enrolled.commit()
    proof = proof_for(enrolled, OwnerRef("deletion", (1,)))
    activate(enrolled, proof)
    event = append_event(enrolled, proof)
    enrolled.execute(f"UPDATE volume_file_deletions SET {field}=? WHERE id=1", (value,))
    with pytest.raises(sqlite3.IntegrityError):
        enrolled.execute(
            "UPDATE volume_file_deletions SET recovery_event_id=?", (event,)
        )
    enrolled.rollback()


def test_deletion_supersession_releases_reservation_and_freezes_history(
    enrolled: sqlite3.Connection,
) -> None:
    enrolled.execute(
        "INSERT INTO volume_file_deletions(id,volume_id,series_id,state,target_path,parent_path,claim_path,target_present,series_title) VALUES(1,11,7,'active','/library/a','/library','/library/claim',1,'test')"
    )
    enrolled.commit()
    proof = proof_for(enrolled, OwnerRef("deletion", (1,)))
    activate(enrolled, proof)
    event = append_event(enrolled, proof)
    enrolled.execute(
        "UPDATE volume_file_deletions SET recovery_event_id=? WHERE id=1", (event,)
    )
    enrolled.execute(
        "INSERT INTO volume_file_deletions(volume_id,series_id,state,target_path,parent_path,claim_path,target_present,series_title) SELECT volume_id,series_id,state,target_path,parent_path,claim_path,target_present,series_title FROM volume_file_deletions WHERE id=1"
    )
    with pytest.raises(sqlite3.IntegrityError):
        enrolled.execute(
            "UPDATE volume_file_deletions SET recovery_event_id=NULL WHERE id=1"
        )
    with pytest.raises(sqlite3.IntegrityError):
        enrolled.execute("DELETE FROM volume_file_deletions WHERE id=1")
    enrolled.rollback()


def test_invalid_durable_manifest_is_a_refusal_not_legacy_fallback(
    enrolled: sqlite3.Connection,
) -> None:
    module = store()
    proof = proof_for(enrolled)
    enrolled.execute("BEGIN IMMEDIATE")
    key = canonical({"version": 1, "key": [1]})
    enrolled.execute(
        "INSERT INTO file_identity_sets(set_id,owner_domain,owner_key_json,snapshot_json,snapshot_sha256,origin,epoch_id,manifest_json,manifest_sha256) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            proof.set_id,
            "rescan",
            key,
            proof.snapshot.rows_json,
            proof.snapshot.sha256,
            "birth",
            proof.epoch_id,
            "{}",
            digest("{}"),
        ),
    )
    payload = canonical(
        {
            "version": 1,
            "snapshot_sha256": proof.snapshot.sha256,
            "manifest_sha256": digest("{}"),
        }
    )
    first = enrolled.execute(
        "INSERT INTO file_identity_events(owner_domain,owner_key_json,revision,set_id,kind,payload_json) VALUES('rescan',?,1,?,'prepared',?)",
        (key, proof.set_id, payload),
    ).lastrowid
    enrolled.execute(
        "INSERT INTO file_identity_events(owner_domain,owner_key_json,revision,predecessor_event_id,set_id,kind,payload_json) VALUES('rescan',?,2,?,?,'activated',?)",
        (key, first, proof.set_id, payload),
    )
    enrolled.commit()
    with pytest.raises(InvalidIdentity):
        module.read_active(enrolled, proof.owner)


@pytest.mark.parametrize("field", ["origin", "legacy-kind", "intent-kind"])
def test_bad_tag_types_raise_categorized_refusal(
    enrolled: sqlite3.Connection, field: str
) -> None:
    proof = proof_for(enrolled)
    with pytest.raises(InvalidIdentity):
        if field == "origin":
            replace(proof, origin=[])
        elif field == "legacy-kind":
            value = json.loads(proof.roles[0].constraints_json)
            value["legacy"] = {"kind": [], "json": "{}", "sha256": digest("{}")}
            replace(proof.roles[0], constraints_json=canonical(value))
        else:
            value = json.loads(proof.manifest_json)
            value["intent"] = {"kind": []}
            replace(proof, manifest_json=canonical(value))


def test_noncanonical_stored_owner_history_cannot_look_unenrolled(
    enrolled: sqlite3.Connection,
) -> None:
    module = store()
    proof = proof_for(enrolled)
    key = '{"version":1, "key":[1]}'
    enrolled.execute(
        "INSERT INTO file_identity_sets(set_id,owner_domain,owner_key_json,snapshot_json,snapshot_sha256,origin,epoch_id,manifest_json,manifest_sha256) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            proof.set_id,
            "rescan",
            key,
            proof.snapshot.rows_json,
            proof.snapshot.sha256,
            "birth",
            proof.epoch_id,
            proof.manifest_json,
            digest(proof.manifest_json),
        ),
    )
    payload = canonical(
        {
            "version": 1,
            "snapshot_sha256": proof.snapshot.sha256,
            "manifest_sha256": digest(proof.manifest_json),
        }
    )
    enrolled.execute(
        "INSERT INTO file_identity_events(owner_domain,owner_key_json,revision,set_id,kind,payload_json) VALUES('rescan',?,1,?,'prepared',?)",
        (key, proof.set_id, payload),
    )
    enrolled.commit()
    with pytest.raises(InvalidIdentity):
        module.read_active(enrolled, proof.owner)


def test_dependency_snapshots_are_current_and_cycles_refuse(
    enrolled: sqlite3.Connection,
) -> None:
    module = store()
    insert_rescan(enrolled, 2)
    insert_rescan(enrolled, 3)
    enrolled.commit()
    proof = proof_for(enrolled)
    snapshots = [
        module.capture_owner_snapshot(enrolled, OwnerRef("rescan", (i,)))
        for i in (2, 3)
    ]
    owners = [{"domain": s.owner.domain, "key": list(s.owner.key)} for s in snapshots]
    value = json.loads(proof.manifest_json)
    value["required_dependencies"] = owners
    value["dependencies"] = [
        {"snapshot_json": s.rows_json, "sha256": s.sha256, "requires": []}
        for s in snapshots
    ]
    valid = replace(proof, manifest_json=canonical(value))
    value["dependencies"][0]["requires"] = [owners[1]]
    value["dependencies"][1]["requires"] = [owners[0]]
    with pytest.raises(InvalidIdentity, match="cyclic"):
        replace(proof, manifest_json=canonical(value))
    enrolled.execute("BEGIN IMMEDIATE")
    enrolled.execute("UPDATE rescan_file_operations SET carriers_json='new' WHERE id=2")
    with pytest.raises(IdentityMismatch):
        module.prepare_set(enrolled, valid, None)
    assert enrolled.execute("SELECT * FROM file_identity_events").fetchall() == []
    enrolled.rollback()


def test_documents_above_one_megabyte_within_approved_limit_are_valid(
    enrolled: sqlite3.Connection,
) -> None:
    module = store()
    legacy = '{"opaque":"' + "x" * (1024 * 1024) + '"}'
    enrolled.execute(
        "UPDATE rescan_file_operations SET carriers_json=? WHERE id=1", (legacy,)
    )
    snapshot = module.capture_owner_snapshot(enrolled, OwnerRef("rescan", (1,)))
    assert len(snapshot.rows_json) > 1024 * 1024
    assert json.loads(snapshot.rows_json)["root"]["row"]["carriers_json"] == legacy


def test_related_row_overflow_refuses(enrolled: sqlite3.Connection) -> None:
    module = store()
    enrolled.execute("INSERT INTO import_queue(id,status) VALUES(1,'pending')")
    enrolled.executemany(
        "INSERT INTO import_queue_files(queue_id,filename) VALUES(1,?)",
        ((str(i),) for i in range(10_000)),
    )
    with pytest.raises(InvalidIdentity, match="bound"):
        module.capture_owner_snapshot(enrolled, OwnerRef("queue_source", (1,)))


def test_sql_preimage_rejects_json_boolean_integer_alias(
    enrolled: sqlite3.Connection,
) -> None:
    module = store()
    proof = proof_for(enrolled)
    enrolled.execute("BEGIN IMMEDIATE")
    first = module.prepare_set(enrolled, proof, None)
    value = json.loads(proof.snapshot.rows_json)
    value["root"]["row"]["id"] = True
    value["root"]["key"] = [True]
    value["owner"]["key"] = [True]
    malformed = canonical(value)
    enrolled.execute(
        "INSERT INTO file_identity_sets SELECT ?,owner_domain,owner_key_json,set_id,?,?,origin,epoch_id,manifest_json,manifest_sha256 FROM file_identity_sets WHERE set_id=?",
        ("2" * 32, malformed, digest(malformed), proof.set_id),
    )
    payload = canonical(
        {
            "version": 1,
            "before_snapshot_json": malformed,
            "before_snapshot_sha256": digest(malformed),
            "plan_sha256": "9" * 64,
        }
    )
    event = enrolled.execute(
        "INSERT INTO file_identity_events(owner_domain,owner_key_json,revision,predecessor_event_id,set_id,kind,payload_json) VALUES('rescan',?,2,?,?,'superseded',?)",
        (canonical({"version": 1, "key": [1]}), first, "2" * 32, payload),
    ).lastrowid
    with pytest.raises(sqlite3.IntegrityError):
        enrolled.execute(
            "UPDATE rescan_file_operations SET recovery_event_id=? WHERE id=1", (event,)
        )
    enrolled.rollback()


def test_scalar_owner_unique_replace_cannot_delete_immutable_event(
    enrolled: sqlite3.Connection,
) -> None:
    module = store()
    proof = proof_for(enrolled)
    assert enrolled.execute("PRAGMA foreign_keys").fetchone() == (1,)
    assert enrolled.execute("PRAGMA recursive_triggers").fetchone() == (0,)
    enrolled.execute("BEGIN IMMEDIATE")
    original_id = module.prepare_set(enrolled, proof, None)
    enrolled.commit()
    before = enrolled.execute(
        "SELECT * FROM file_identity_events ORDER BY id"
    ).fetchall()
    alias_key = '{"version":1, "key":[1]}'
    enrolled.execute(
        "INSERT INTO file_identity_sets SELECT ?,owner_domain,?,NULL,"
        "snapshot_json,snapshot_sha256,origin,epoch_id,manifest_json,manifest_sha256"
        " FROM file_identity_sets WHERE set_id=?",
        ("2" * 32, alias_key, proof.set_id),
    )
    payload = canonical(
        {
            "version": 1,
            "before_snapshot_json": proof.snapshot.rows_json,
            "before_snapshot_sha256": proof.snapshot.sha256,
            "plan_sha256": "9" * 64,
        }
    )
    with pytest.raises(sqlite3.IntegrityError):
        enrolled.execute(
            "INSERT OR REPLACE INTO file_identity_events(id,owner_domain,"
            "owner_key_json,revision,predecessor_event_id,set_id,kind,payload_json)"
            " VALUES(?,'rescan',?,1,NULL,?,'retained',?)",
            (original_id + 100, alias_key, "2" * 32, payload),
        )
    assert (
        enrolled.execute("SELECT * FROM file_identity_events ORDER BY id").fetchall()
        == before
    )
    assert enrolled.execute("PRAGMA foreign_key_check").fetchall() == []


def test_operation_token_replace_cannot_delete_superseded_rescan(
    enrolled: sqlite3.Connection,
) -> None:
    proof = proof_for(enrolled)
    activate(enrolled, proof)
    event = append_event(enrolled, proof)
    enrolled.execute(
        "UPDATE rescan_file_operations SET recovery_event_id=? WHERE id=1", (event,)
    )
    enrolled.commit()
    assert enrolled.execute("PRAGMA foreign_keys").fetchone() == (1,)
    assert enrolled.execute("PRAGMA recursive_triggers").fetchone() == (0,)
    before = enrolled.execute(
        "SELECT * FROM rescan_file_operations ORDER BY id"
    ).fetchall()
    columns = [
        row[1] for row in enrolled.execute("PRAGMA table_info(rescan_file_operations)")
    ]
    values = list(before[0])
    values[columns.index("id")] = 2
    values[columns.index("recovery_event_id")] = None
    with pytest.raises(sqlite3.IntegrityError):
        enrolled.execute(
            f"INSERT OR REPLACE INTO rescan_file_operations VALUES({','.join('?' for _ in values)})",
            values,
        )
    assert (
        enrolled.execute("SELECT * FROM rescan_file_operations ORDER BY id").fetchall()
        == before
    )
    assert enrolled.execute("PRAGMA foreign_key_check").fetchall() == []


def test_operation_token_replace_preserves_unflagged_legacy_behavior(
    enrolled: sqlite3.Connection,
) -> None:
    before = enrolled.execute(
        "SELECT * FROM rescan_file_operations WHERE id=1"
    ).fetchone()
    columns = [
        row[1] for row in enrolled.execute("PRAGMA table_info(rescan_file_operations)")
    ]
    values = list(before)
    values[columns.index("id")] = 2
    enrolled.execute(
        f"INSERT OR REPLACE INTO rescan_file_operations VALUES({','.join('?' for _ in values)})",
        values,
    )
    assert enrolled.execute("SELECT * FROM rescan_file_operations").fetchall() == [
        tuple(values)
    ]
    assert enrolled.execute("PRAGMA foreign_key_check").fetchall() == []
