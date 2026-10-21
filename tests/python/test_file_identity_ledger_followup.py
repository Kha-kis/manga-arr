"""Inventory-specific ledger closure regressions; store CAS snapshots stay fixed."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import file_identity
import file_identity_recovery as observation
import file_identity_store as store
from file_identity import (
    IdentityMismatch,
    InvalidIdentity,
    NativeHandle,
    OwnerRef,
    StorageProfile,
)
from file_identity_consumers import (
    InventoryBudget,
    ReferenceClosure,
    collect_references,
    roles_for,
)
from file_mutation_lock import file_mutation_guard
from private_file_claim import ensure_namespace
from test_file_identity_backend import _candidate_contracts
from test_file_identity_observation import (
    ProducerMedia,
    producer_media as producer_media,
    producer_publication,
    producer_queue,
    producer_rescan,
    write_archive,
)
from test_file_identity_observation_sql import inspect
from test_file_identity_schema import canonical, digest, ledger_path as ledger_path
from test_file_identity_store import activate, append_event, object_value, role_value


@dataclass(frozen=True)
class LedgerFixture:
    path: Path
    scope: Path
    other_scope: Path
    proof: store.ProofSet
    other: store.ProofSet


def authority(
    db: sqlite3.Connection, scope: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, server = _candidate_contracts()
    profile = StorageProfile("fixture-only", 1, "a" * 64, client, server)
    monkeypatch.setattr(file_identity, "_QUALIFIED_PROFILES", (profile,))
    db.execute(
        "INSERT INTO file_identity_authorities VALUES(?,?,?,?,?)",
        (
            "a" * 32,
            "b" * 32,
            canonical(
                {
                    "version": 1,
                    "profile_id": profile.profile_id,
                    "revision": 1,
                    "qualification_sha256": profile.qualification_sha256,
                    "client_contract": client,
                    "server_contract": server,
                }
            ),
            canonical(
                {"version": 1, "scope_path": str(scope), "scope_token": "c" * 32}
            ),
            profile.qualification_sha256,
        ),
    )


def epoch(db: sqlite3.Connection, scope: Path, epoch_id: str) -> None:
    db.execute(
        "INSERT INTO file_identity_epochs VALUES(?,?,?,?,?,?)",
        (
            epoch_id,
            "a" * 32,
            str(scope),
            str(scope),
            str(scope / ".mangarr-claims"),
            canonical(
                {
                    "version": 1,
                    "layout": "same_parent",
                    "enrollment": "birth",
                    "legacy_namespace": None,
                    "controlled_birth": {
                        "authority_incarnation": "b" * 32,
                        "allocation_token": "e" * 32,
                        "root_native": NativeHandle(1, b"fixture-root").to_value(),
                        "marker_native": NativeHandle(1, b"fixture-marker").to_value(),
                        "receipt_sha256": "f" * 64,
                    },
                }
            ),
        ),
    )


def proof_for_owner(
    db: sqlite3.Connection,
    owner: OwnerRef,
    set_id: str,
    epoch_id: str,
    dependencies: tuple[store.OwnerSnapshot, ...] = (),
) -> store.ProofSet:
    snapshot = store.capture_owner_snapshot(db, owner)
    objects = []
    for spec in roles_for(snapshot):
        path = Path(spec.path)
        info = path.stat()
        fields: dict[str, object] = {
            "inode": None,
            "size": None,
            "mtime_ns": None,
            "sha256": None,
            "uid": None,
            "mode": None,
            "nlink": None,
        }
        fields["inode"] = info.st_ino
        if spec.policy in {"file", "marker"}:
            fields.update(
                size=info.st_size,
                mtime_ns=info.st_mtime_ns,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        if spec.policy in {"marker", "private_directory", "exclusive_parent"}:
            fields.update(uid=info.st_uid, mode=stat.S_IMODE(info.st_mode))
        if spec.policy == "marker":
            fields["nlink"] = info.st_nlink
        constraints = canonical(
            {
                "version": 1,
                "legacy": None,
                "binding": {
                    "owner": {"domain": owner.domain, "key": list(owner.key)},
                    "role": role_value(spec.role),
                    "operation_key": "fixture-birth",
                    "owner_token": None,
                },
                "policy": spec.policy,
                "non_device": fields,
            }
        )
        objects.append(
            store.ObjectProof(
                spec.role,
                spec.path,
                spec.parent_role,
                NativeHandle(1, b"fixture-object"),
                constraints,
            )
        )
    manifest = canonical(
        {
            "version": 1,
            "snapshot_sha256": snapshot.sha256,
            "phase": "observed",
            "required_roles": [role_value(o.role) for o in objects],
            "roles": [object_value(o) for o in objects],
            "absent_roles": [],
            "required_dependencies": [
                {"domain": s.owner.domain, "key": list(s.owner.key)}
                for s in dependencies
            ],
            "dependencies": [
                {"snapshot_json": s.rows_json, "sha256": s.sha256, "requires": []}
                for s in dependencies
            ],
            "intent": None,
        }
    )
    return store.ProofSet(
        set_id, owner, snapshot, "a" * 32, epoch_id, "birth", tuple(objects), manifest
    )


@pytest.fixture
def ledger_fixture(
    ledger_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> LedgerFixture:
    scope, outside = tmp_path / "media", tmp_path / "outside"
    for path in (scope, outside):
        path.mkdir()
        with file_mutation_guard(str(ledger_path)) as guard:
            with ensure_namespace(guard, str(path)):
                pass
    monkeypatch.setattr(
        observation, "capture_native", lambda _fd: NativeHandle(1, b"fixture-current")
    )
    db = sqlite3.connect(ledger_path)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        authority(db, tmp_path, monkeypatch)
        epoch(db, scope, "d" * 32)
        epoch(db, outside, "e" * 32)
        db.commit()
        proof = proof_for_owner(
            db, OwnerRef("namespace", (str(scope),)), "1" * 32, "d" * 32
        )
        other = proof_for_owner(
            db, OwnerRef("namespace", (str(outside),)), "2" * 32, "e" * 32
        )
        activate(db, proof)
        db.commit()
        activate(db, other)
        db.commit()
        assert active_proof(db, proof.owner).proof == proof
        assert store.capture_owner_snapshot(db, proof.owner) == proof.snapshot
    finally:
        db.close()
    return LedgerFixture(ledger_path, scope, outside, proof, other)


def capture(case: LedgerFixture) -> ReferenceClosure:
    db = sqlite3.connect(case.path)
    try:
        return collect_references(db, str(case.scope), InventoryBudget())
    finally:
        db.close()


def active_proof(db: sqlite3.Connection, owner: OwnerRef) -> store.ActiveProofSet:
    active = store.read_active(db, owner)
    assert active is not None
    return active


def ledger_rows(closure: ReferenceClosure) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for row in closure.raw_rows:
        if row.table.startswith("file_identity_"):
            result.setdefault(row.table, []).append(dict(row.values))
    return result


def test_active_ledger_closure_captures_exact_bound_rows_not_unrelated_history(
    ledger_fixture: LedgerFixture,
) -> None:
    case = ledger_fixture
    closure = capture(case)
    rows = ledger_rows(closure)
    assert set(rows) == {
        "file_identity_authorities",
        "file_identity_epochs",
        "file_identity_sets",
        "file_identity_events",
    }
    assert [r["set_id"] for r in rows["file_identity_sets"]] == [case.proof.set_id]
    assert [r["epoch_id"] for r in rows["file_identity_epochs"]] == [
        case.proof.epoch_id
    ]
    assert [r["authority_id"] for r in rows["file_identity_authorities"]] == [
        case.proof.authority_id
    ]
    assert {r["kind"] for r in rows["file_identity_events"]} == {
        "prepared",
        "activated",
    }
    assert [s.owner for s in closure.snapshots] == [case.proof.owner]
    assert closure.snapshots[0] == case.proof.snapshot


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
def test_pending_and_terminal_history_is_retained_without_resurrecting_activation(
    ledger_fixture: LedgerFixture,
    kind: str,
) -> None:
    case = ledger_fixture
    db = sqlite3.connect(case.path)
    try:
        previous = active_proof(db, case.proof.owner).event_id
        db.execute("BEGIN IMMEDIATE")
        if kind == "prepared":
            next_proof = proof_for_owner(
                db, case.proof.owner, "3" * 32, case.proof.epoch_id
            )
            event = store.prepare_set(db, next_proof, previous)
        else:
            event = append_event(db, case.proof, kind)
        db.commit()
        with pytest.raises(IdentityMismatch):
            store.read_active(db, case.proof.owner)
    finally:
        db.close()
    rows = ledger_rows(capture(case))
    assert any(
        r["id"] == event and r["kind"] == kind
        for r in rows.get("file_identity_events", [])
    )
    assert any(r["kind"] == "activated" for r in rows["file_identity_events"])


@pytest.mark.parametrize("head", ["retired", "activated"])
def test_retired_deleted_business_owner_is_history_not_fabricated_current_snapshot(
    ledger_fixture: LedgerFixture,
    head: str,
) -> None:
    case = ledger_fixture
    db = sqlite3.connect(case.path)
    try:
        active_event = active_proof(db, case.proof.owner).event_id
        db.execute("BEGIN IMMEDIATE")
        event = (
            append_event(db, case.proof, "retired")
            if head == "retired"
            else active_event
        )
        db.execute(
            "DELETE FROM file_claim_namespaces WHERE parent_path=?",
            case.proof.owner.key,
        )
        db.commit()
    finally:
        db.close()
    closure = capture(case)
    assert closure.snapshots == closure.roles == ()
    assert any(
        r["id"] == event for r in ledger_rows(closure).get("file_identity_events", [])
    )


@pytest.mark.parametrize("scope_selection", ["owner", "dependency"])
def test_ledger_manifest_dependency_selects_real_static_owner_outside_initial_scope(
    ledger_fixture: LedgerFixture,
    scope_selection: str,
) -> None:
    case = ledger_fixture
    db = sqlite3.connect(case.path)
    try:
        previous = active_proof(db, case.proof.owner).event_id
        proof = proof_for_owner(
            db, case.proof.owner, "3" * 32, case.proof.epoch_id, (case.other.snapshot,)
        )
        activate(db, proof, previous)
        db.commit()
        assert active_proof(db, proof.owner).proof == proof
    finally:
        db.close()
    db = sqlite3.connect(case.path)
    try:
        closure = collect_references(
            db,
            str(case.scope if scope_selection == "owner" else case.other_scope),
            InventoryBudget(),
        )
    finally:
        db.close()
    assert {s.owner for s in closure.snapshots} == {case.proof.owner, case.other.owner}
    assert {(e.owner, e.requires, e.reason) for e in closure.dependencies} == {
        (case.proof.owner, case.other.owner, "identity_ledger")
    }
    assert {r["set_id"] for r in ledger_rows(closure)["file_identity_sets"]} == {
        case.proof.set_id,
        case.other.set_id,
        proof.set_id,
    }


@pytest.mark.parametrize(
    "relevant", [True, False], ids=["relevant", "unrelated-control"]
)
def test_ledger_only_commit_between_captures_has_scope_sensitive_drift_result(
    ledger_fixture: LedgerFixture,
    monkeypatch: pytest.MonkeyPatch,
    relevant: bool,
) -> None:
    case = ledger_fixture
    original = observation._hash
    committed = False

    def during_hash(*args: Any, **kwargs: Any) -> Any:
        nonlocal committed
        if not committed:
            db = sqlite3.connect(case.path)
            try:
                db.execute("BEGIN IMMEDIATE")
                append_event(db, case.proof if relevant else case.other, "retained")
                db.commit()
                committed = True
            finally:
                db.close()
        return original(*args, **kwargs)

    monkeypatch.setattr(observation, "_hash", during_hash)
    result = inspect(case.path, case.scope)
    assert committed
    assert ("sql_changed" in result.unresolved) is relevant
    if not relevant:
        assert result.unresolved == ()


def test_store_cas_snapshot_does_not_include_its_own_ledger(
    ledger_fixture: LedgerFixture,
) -> None:
    case = ledger_fixture
    db = sqlite3.connect(case.path)
    try:
        before = store.capture_owner_snapshot(db, case.proof.owner)
        db.execute("BEGIN IMMEDIATE")
        append_event(db, case.proof, "retained")
        db.commit()
        assert (
            store.capture_owner_snapshot(db, case.proof.owner)
            == before
            == case.proof.snapshot
        )
    finally:
        db.close()


@pytest.mark.parametrize("later_event", [False, True])
def test_real_superseded_journal_flag_retains_exact_old_chain_and_replacement(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    later_event: bool,
) -> None:
    env = producer_media
    old_id = producer_rescan(env, "prepared")
    write_archive(
        env.library / "converted.cbz", b"fixture current target, not past proof"
    )
    db = sqlite3.connect(env.db_path)
    try:
        authority(db, env.scope, monkeypatch)
        epoch(db, env.library, "d" * 32)
        db.commit()
        owner = OwnerRef("rescan", (old_id,))
        proof = proof_for_owner(db, owner, "1" * 32, "d" * 32)
        activate(db, proof)
        db.commit()
        db.execute("BEGIN IMMEDIATE")
        event = append_event(db, proof, "superseded")
        db.execute(
            "UPDATE rescan_file_operations SET recovery_event_id=? WHERE id=?",
            (event, old_id),
        )
        db.commit()
        if later_event:
            db.execute("BEGIN IMMEDIATE")
            append_event(db, proof, "retired")
            db.commit()
        assert store.capture_owner_snapshot(db, owner) != proof.snapshot
        frozen = json.loads(proof.snapshot.rows_json)["root"]["row"]
        assert frozen["recovery_event_id"] is None
    finally:
        db.close()
    db = sqlite3.connect(env.db_path)
    try:
        # Runtime remains disabled/fenced. Exercise the schema's replacement
        # representation without teaching the unchanged runtime to resume it.
        cursor = db.execute(
            "INSERT INTO rescan_file_operations(operation_token,series_id,volume_id,source_path,destination_path,expected_volume_json,expected_context_json,fingerprints_json,carriers_json,state) "
            "SELECT ?,series_id,volume_id,source_path,destination_path,expected_volume_json,expected_context_json,fingerprints_json,carriers_json,state FROM rescan_file_operations WHERE id=?",
            ("7" * 32, old_id),
        )
        replacement = cursor.lastrowid
        assert replacement is not None and replacement != old_id
        db.commit()
        closure = collect_references(db, str(env.scope), InventoryBudget())
    finally:
        db.close()
    assert {owner, OwnerRef("rescan", (replacement,))} <= {
        s.owner for s in closure.snapshots
    }
    rows = ledger_rows(closure)
    assert any(
        r["id"] == event and r["kind"] == "superseded"
        for r in rows.get("file_identity_events", [])
    )
    assert rows["file_identity_sets"][0]["snapshot_json"] == proof.snapshot.rows_json


@pytest.mark.parametrize(
    "defect",
    [
        "owner_snapshot",
        "snapshot_hash",
        "manifest_hash",
        "epoch_geometry",
        "authority_incarnation",
        "dependency_hash",
    ],
)
def test_relevant_ledger_binding_corruption_is_not_silently_ignored(
    ledger_fixture: LedgerFixture,
    defect: str,
) -> None:
    case = ledger_fixture
    db = sqlite3.connect(case.path)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.row_factory = sqlite3.Row
        row = dict(
            db.execute(
                "SELECT * FROM file_identity_sets WHERE set_id=?", (case.proof.set_id,)
            ).fetchone()
        )
        row.update(set_id="3" * 32, prior_set_id=case.proof.set_id)
        if defect == "owner_snapshot":
            row.update(
                snapshot_json=case.other.snapshot.rows_json,
                snapshot_sha256=case.other.snapshot.sha256,
                manifest_json=case.other.manifest_json,
                manifest_sha256=digest(case.other.manifest_json),
            )
        elif defect == "snapshot_hash":
            row["snapshot_sha256"] = "0" * 64
        elif defect == "manifest_hash":
            row["manifest_sha256"] = "0" * 64
        elif defect in {"epoch_geometry", "authority_incarnation"}:
            epoch(db, case.scope, "f" * 32) if defect == "epoch_geometry" else None
            if defect == "epoch_geometry":
                # Insert-only immutable fixtures: use a separate invalid epoch.
                db.execute(
                    "INSERT INTO file_identity_epochs SELECT ?,authority_id,origin_path,allocation_path,?,birth_json FROM file_identity_epochs WHERE epoch_id=?",
                    (
                        "9" * 32,
                        str(case.scope / "not-a-direct-child" / "root"),
                        "f" * 32,
                    ),
                )
                row["epoch_id"] = "9" * 32
            else:
                db.execute(
                    "INSERT INTO file_identity_authorities SELECT ?,?,profile_json,declaration_json,qualification_sha256 FROM file_identity_authorities WHERE authority_id=?",
                    ("f" * 32, "9" * 32, case.proof.authority_id),
                )
                db.execute(
                    "INSERT INTO file_identity_epochs SELECT ?,?,origin_path,allocation_path,root_path,birth_json FROM file_identity_epochs WHERE epoch_id=?",
                    ("f" * 32, "f" * 32, case.proof.epoch_id),
                )
                row["epoch_id"] = "f" * 32
        else:
            manifest = json.loads(row["manifest_json"])
            manifest["required_dependencies"] = [
                {"domain": case.other.owner.domain, "key": list(case.other.owner.key)}
            ]
            manifest["dependencies"] = [
                {
                    "snapshot_json": case.other.snapshot.rows_json,
                    "sha256": "0" * 64,
                    "requires": [],
                }
            ]
            row["manifest_json"] = canonical(manifest)
            row["manifest_sha256"] = digest(row["manifest_json"])
        columns = tuple(row)
        db.execute(
            f"INSERT INTO file_identity_sets({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
            tuple(row.values()),
        )
        previous = db.execute(
            "SELECT id,revision FROM file_identity_events WHERE set_id=? AND kind='activated'",
            (case.proof.set_id,),
        ).fetchone()
        db.execute(
            "INSERT INTO file_identity_events(owner_domain,owner_key_json,revision,predecessor_event_id,set_id,kind,payload_json) VALUES(?,?,?,?,?,'prepared',?)",
            (
                row["owner_domain"],
                row["owner_key_json"],
                previous["revision"] + 1,
                previous["id"],
                row["set_id"],
                canonical(
                    {
                        "version": 1,
                        "snapshot_sha256": row["snapshot_sha256"],
                        "manifest_sha256": row["manifest_sha256"],
                    }
                ),
            ),
        )
        db.commit()
    finally:
        db.close()
    with pytest.raises((InvalidIdentity, IdentityMismatch)):
        capture(case)


def test_unqualified_registry_does_not_turn_observation_into_activation(
    ledger_fixture: LedgerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = ledger_fixture
    monkeypatch.setattr(file_identity, "_QUALIFIED_PROFILES", ())
    assert inspect(case.path, case.scope).unresolved == ()


def test_eventless_set_is_retained_with_its_prior_epoch_and_authority(
    ledger_fixture: LedgerFixture,
) -> None:
    case = ledger_fixture
    db = sqlite3.connect(case.path)
    try:
        db.execute(
            "INSERT INTO file_identity_sets SELECT ?,owner_domain,owner_key_json,set_id,snapshot_json,snapshot_sha256,origin,epoch_id,manifest_json,manifest_sha256 FROM file_identity_sets WHERE set_id=?",
            ("3" * 32, case.proof.set_id),
        )
        db.commit()
    finally:
        db.close()
    rows = ledger_rows(capture(case))
    assert {r["set_id"] for r in rows["file_identity_sets"]} == {
        case.proof.set_id,
        "3" * 32,
    }
    assert {r["set_id"] for r in rows["file_identity_events"]} == {case.proof.set_id}
    assert rows["file_identity_epochs"][0]["epoch_id"] == case.proof.epoch_id
    assert (
        rows["file_identity_authorities"][0]["authority_id"] == case.proof.authority_id
    )


def test_latest_activation_preimage_is_not_reinterpreted_as_current_cas(
    ledger_fixture: LedgerFixture,
) -> None:
    case = ledger_fixture
    db = sqlite3.connect(case.path)
    try:
        original = db.execute(
            "SELECT ownership_json FROM file_claim_namespaces WHERE parent_path=?",
            case.proof.owner.key,
        ).fetchone()[0]
        db.execute(
            "UPDATE file_claim_namespaces SET ownership_json=? WHERE parent_path=?",
            (" " + original + "\n", *case.proof.owner.key),
        )
        db.commit()
        current = store.capture_owner_snapshot(db, case.proof.owner)
        assert current != case.proof.snapshot
    finally:
        db.close()
    closure = capture(case)
    assert closure.snapshots == (current,)
    assert (
        ledger_rows(closure)["file_identity_sets"][0]["snapshot_json"]
        == case.proof.snapshot.rows_json
    )


def test_ledger_candidates_charge_existing_row_budget(
    ledger_fixture: LedgerFixture,
) -> None:
    import file_identity_consumers as consumers

    case = ledger_fixture
    db = sqlite3.connect(case.path)
    try:
        count = sum(
            db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in consumers._TABLES
            if table not in consumers._LEDGER_KEYS
        )
        with pytest.raises(InvalidIdentity, match="inventory budget exceeded"):
            collect_references(db, str(case.scope), InventoryBudget(max_rows=count + 1))
    finally:
        db.close()


def test_historical_dependency_snapshot_can_seed_scope_without_inventing_its_current_owner(
    ledger_fixture: LedgerFixture,
) -> None:
    case = ledger_fixture
    historical = case.scope.parent / "historical-only"
    historical.mkdir()
    with file_mutation_guard(str(case.path)) as guard:
        with ensure_namespace(guard, str(historical)):
            pass
    owner = OwnerRef("namespace", (str(historical),))
    db = sqlite3.connect(case.path)
    try:
        previous = active_proof(db, case.proof.owner).event_id
        dependency = store.capture_owner_snapshot(db, owner)
        proof = proof_for_owner(
            db, case.proof.owner, "3" * 32, case.proof.epoch_id, (dependency,)
        )
        activate(db, proof, previous)
        db.commit()
        db.execute("DELETE FROM file_claim_namespaces WHERE parent_path=?", owner.key)
        db.commit()
        closure = collect_references(db, str(historical), InventoryBudget())
    finally:
        db.close()
    assert {s.owner for s in closure.snapshots} == {case.proof.owner}
    assert owner not in {r.owner for r in closure.roles}
    assert {r["set_id"] for r in ledger_rows(closure)["file_identity_sets"]} == {
        case.proof.set_id,
        proof.set_id,
    }


def test_ledger_wire_decoder_uses_no_sql_or_filesystem(
    ledger_fixture: LedgerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    case = ledger_fixture
    result = inspect(case.path, case.scope)
    encoded = result.to_json()

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("decoder attempted I/O")

    with monkeypatch.context() as checks:
        checks.setattr(sqlite3, "connect", forbidden)
        checks.setattr(os, "open", forbidden)
        assert observation.OwnershipInventory.from_json(encoded) == result


def test_ledger_row_budget_checks_escaped_bytes_before_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import file_identity_consumers as consumers

    def forbidden(_value: object) -> str:
        raise AssertionError("oversized string reached JSON allocator")

    monkeypatch.setattr(consumers, "_encode", forbidden)
    with pytest.raises(InvalidIdentity, match="inventory budget exceeded"):
        consumers._row_bytes((("manifest_json", "x" * 1024**2),), 1024)


def test_ledger_row_byte_admission_matches_ascii_wire_size() -> None:
    import file_identity_consumers as consumers

    values = (
        ("opaque", 'quoted"\\\n\u96ea\U0001f642'),
        ("nullable", None),
        ("integer", 9),
        ("real", 1.25),
    )
    expected = len(
        json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode()
    )
    assert consumers._row_bytes(values, expected) == expected
    with pytest.raises(InvalidIdentity, match="inventory budget exceeded"):
        consumers._row_bytes(values, expected - 1)


def snapshot_admission_sizes(
    db: sqlite3.Connection,
) -> tuple[int, int, list[tuple[OwnerRef, int, int]]]:
    import file_identity_consumers as consumers

    counter = consumers._Counter(InventoryBudget())
    raw = consumers._scan(db, counter)
    sizes = []
    for domain, (table, _pk) in consumers._ROOTS.items():
        for record in raw[table]:
            owner = OwnerRef(domain, record.key)
            snapshot = store.capture_owner_snapshot(db, owner)
            sizes.append(
                (
                    owner,
                    1 + len(json.loads(snapshot.rows_json)["related"]),
                    len(snapshot.rows_json.encode()),
                )
            )
    return counter.rows, counter.hash_bytes, sizes


def watch_snapshot_admission(
    monkeypatch: pytest.MonkeyPatch, scanned_rows: int
) -> tuple[list[tuple[OwnerRef, int]], list[tuple[int, int, int]]]:
    import file_identity_consumers as consumers

    captures: list[tuple[OwnerRef, int]] = []
    charges: list[tuple[int, int, int]] = []
    original_capture = consumers.capture_owner_snapshot
    original_charge = consumers._Counter.charge

    def capture(db: sqlite3.Connection, owner: OwnerRef) -> store.OwnerSnapshot:
        snapshot = original_capture(db, owner)
        captures.append((owner, len(snapshot.rows_json.encode())))
        return snapshot

    def charge(counter: consumers._Counter, **kwargs: int) -> None:
        if counter.rows >= scanned_rows and captures:
            charges.append((len(captures), counter.rows, counter.hash_bytes))
        original_charge(counter, **kwargs)

    monkeypatch.setattr(consumers, "capture_owner_snapshot", capture)
    monkeypatch.setattr(consumers._Counter, "charge", charge)
    return captures, charges


@pytest.mark.parametrize("bound", ["rows", "bytes"])
def test_snapshot_admission_stops_capture_and_retained_bytes_at_boundary(
    ledger_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bound: str
) -> None:
    for number in range(8):
        parent = tmp_path / f"library-{number}"
        parent.mkdir()
        with file_mutation_guard(str(ledger_path)) as guard:
            with ensure_namespace(guard, str(parent)):
                pass
    db = sqlite3.connect(ledger_path)
    try:
        db.execute(
            "UPDATE file_claim_namespaces SET ownership_json=ownership_json||?",
            (" " * (128 * 1024),),
        )
        db.commit()
        rows, metadata, sizes = snapshot_admission_sizes(db)
        assert len(sizes) == 8 and all(count == 1 for _, count, _ in sizes)
        budget = (
            InventoryBudget(max_rows=rows + 1)
            if bound == "rows"
            else InventoryBudget(max_hash_bytes=metadata + sizes[0][2])
        )
        captures, charges = watch_snapshot_admission(monkeypatch, rows)
        with pytest.raises(InvalidIdentity, match="inventory budget exceeded"):
            collect_references(db, str(tmp_path), budget)
        assert not db.in_transaction
        # One admitted snapshot and at most the refusing boundary capture, not
        # all eight snapshots retained before the first admission check.
        assert len(captures) == 2
        assert sum(size for _, size in captures) == sum(s[2] for s in sizes[:2])
        assert charges and charges[0][0] == 1
    finally:
        db.close()


@pytest.mark.parametrize("bound", ["rows", "bytes"])
def test_snapshot_admission_counts_repeated_publication_queue_related_rows(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, bound: str
) -> None:
    env = producer_media
    queue_id = producer_queue(env)
    producer_publication(env, queue_id, "staging")
    db = sqlite3.connect(env.db_path)
    try:
        db.execute(
            "UPDATE import_publications SET queue_snapshot_json=queue_snapshot_json||?",
            (" " * (128 * 1024),),
        )
        db.commit()
        rows, metadata, sizes = snapshot_admission_sizes(db)
        boundary = next(i for i, (_, count, _) in enumerate(sizes) if count > 1)
        assert sizes[boundary][0].domain == "publication"
        assert sizes[-1][0] == OwnerRef("queue_source", (queue_id,))
        assert boundary + 1 < len(sizes)
        budget = (
            InventoryBudget(max_rows=rows + sum(s[1] for s in sizes[:boundary]) + 1)
            if bound == "rows"
            else InventoryBudget(
                max_hash_bytes=metadata + sum(s[2] for s in sizes[: boundary + 1]) - 1
            )
        )
        captures, charges = watch_snapshot_admission(monkeypatch, rows)
        with pytest.raises(InvalidIdentity, match="inventory budget exceeded"):
            collect_references(db, str(env.scope), budget)
        assert not db.in_transaction
        assert [owner for owner, _ in captures] == [s[0] for s in sizes[: boundary + 1]]
        assert sum(size for _, size in captures) == sum(
            s[2] for s in sizes[: boundary + 1]
        )
        assert charges and charges[0][0] == 1
    finally:
        db.close()


def test_snapshot_admission_exact_rows_and_metadata_are_not_double_charged(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = producer_media
    queue_id = producer_queue(env)
    producer_publication(env, queue_id, "staging")
    db = sqlite3.connect(env.db_path)
    try:
        rows, metadata, sizes = snapshot_admission_sizes(db)
        expected = collect_references(db, str(env.scope), InventoryBudget())
        captures, _ = watch_snapshot_admission(monkeypatch, rows)
        result = collect_references(
            db,
            str(env.scope),
            InventoryBudget(
                max_rows=rows + sum(s[1] for s in sizes),
                max_hash_bytes=metadata + sum(s[2] for s in sizes),
            ),
        )
        assert result == expected
        assert [owner for owner, _ in captures] == [s[0] for s in sizes]
        assert not db.in_transaction
    finally:
        db.close()
