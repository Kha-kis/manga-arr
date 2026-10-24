"""Pure review plans; synthetic native observations are unit data, not qualification."""

from __future__ import annotations

import file_identity_recovery as recovery
import file_identity_consumers as consumers
import pytest
import json
import hashlib
import errno
import sqlite3
import sys
from pathlib import Path
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import closing, contextmanager
from typing import Literal, NoReturn, Protocol, cast
from types import ModuleType, TracebackType
from dataclasses import FrozenInstanceError, asdict, replace
from file_identity import IdentityMismatch, InvalidIdentity, OwnerRef, RoleKey
from file_identity_consumers import InventoryBudget, ReferenceClosure
from private_pack_claim import PackDirectoryClaim, PackOwnership
from test_file_identity_schema import ledger_path as ledger_path
from test_file_identity_observation import (
    ProducerMedia,
    producer_media as producer_media,
    producer_rescan,
    producer_deletion,
    producer_pack,
    captured_fixture,
    observe_fixture as producer_observe_fixture,
    media_snapshot,
    control_snapshot,
    producer_queue,
    producer_publication,
    write_archive,
)
from test_file_identity_backend import _candidate_contracts
from file_mutation_lock import file_mutation_guard
from test_file_identity_ledger_followup import (
    LedgerFixture,
    ledger_fixture as ledger_fixture,
)
from test_file_identity_observation_sql import inspect as inspect_fixture


def encoded(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def digest(value: object) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def observe_fixture(
    module: ModuleType,
    env: ProducerMedia,
    closure: ReferenceClosure,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[recovery.ObservedRole, ...]:
    values = cast(
        tuple[object, ...], producer_observe_fixture(module, env, closure, monkeypatch)
    )
    assert all(type(value) is recovery.ObservedRole for value in values)
    return cast(tuple[recovery.ObservedRole, ...], values)


def empty_inventory() -> recovery.OwnershipInventory:
    return recovery.OwnershipInventory.create(
        "/media", ReferenceClosure((), (), (), ()), (), (), InventoryBudget()
    )


def documents(inventory: recovery.OwnershipInventory) -> tuple[str, str, str]:
    selection: dict[str, object] = {
        "version": 1,
        "inventory_sha256": inventory.sha256,
        "decisions": [],
        "epoch_bindings": {
            "version": 1,
            "role_bindings": [],
            "existing": [],
            "provision": [],
            "rollback_outputs": [],
        },
    }
    declaration: dict[str, object] = {
        "version": 1,
        "inventory_sha256": inventory.sha256,
        "selection_sha256": digest(selection),
        "authorities": [],
        "owner_reviews": [],
        "role_reviews": [],
    }
    receipt: dict[str, object] = {
        "version": 1,
        "inventory_sha256": inventory.sha256,
        "selection_sha256": digest(selection),
        "declaration_sha256": digest(declaration),
        "archive": {
            "path": "/backup/review.zip",
            "sha256": "a" * 64,
            "format": "mangarr-backup",
            "format_version": 1,
            "schema_version": 7,
            "self_contained": True,
            "components": {"database": True, "configuration": True, "key": True},
            "source_image_digest": "sha256:" + "b" * 64,
        },
        "owner_snapshots": [],
        "media": [],
    }
    return encoded(selection), encoded(declaration), encoded(receipt)


def linked_documents(
    inputs: tuple[str, str, str], effects: list[dict[str, object]]
) -> tuple[str, str, str]:
    selection, declaration, receipt = (parse_document(d) for d in inputs)
    selection["version"] = declaration["version"] = receipt["version"] = 2
    epoch = wire_object(selection["epoch_bindings"])
    epoch.update(
        version=2,
        linked_effects=sorted(effects, key=encoded),
        allocation_capabilities=[],
    )
    declaration["linked_reviews"] = sorted(
        [
            {"effect_sha256": digest(effect), "authority_id": "a" * 32}
            for effect in effects
        ],
        key=lambda item: item["effect_sha256"],
    )
    affected = {
        (encoded(effect["affected_owner"]), encoded(effect["affected_role"]))
        for effect in effects
    }
    for value in wire_array(declaration["role_reviews"]):
        review = wire_object(value)
        if (encoded(review["owner"]), encoded(review["role"])) in affected:
            review.update(assertion="reviewed-linked-reference", authority_id="a" * 32)
    for value in wire_array(epoch["role_bindings"]):
        binding = wire_object(value)
        if (encoded(binding["owner"]), encoded(binding["role"])) in affected:
            binding["authority_id"] = "a" * 32
    for value in wire_array(receipt["media"]):
        media = wire_object(value)
        if (
            media["handling"] == "retain-in-place"
            and (encoded(media["owner"]), encoded(media["role"])) in affected
        ):
            media.update(
                handling="snapshot", recovery_reference="external-unit-linked-snapshot"
            )
    return resign_documents(selection, declaration, receipt)


@pytest.mark.parametrize("inventory_version", [1, 2])
def test_plan2_explicit_version_matrix_and_rebuild(inventory_version: int) -> None:
    inventory = recovery.OwnershipInventory.create(
        "/media",
        ReferenceClosure((), (), (), ()),
        (),
        (),
        InventoryBudget(),
        inventory_version=inventory_version,
    )
    inputs = linked_documents(documents(inventory), [])
    plan = recovery.build_recovery_plan(inventory, *inputs)
    assert plan.version == 2 and plan.inventory.version == inventory_version
    assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan
    assert recovery.build_recovery_plan(inventory, *inputs).to_json() == plan.to_json()


@pytest.mark.parametrize("document", ["selection", "declaration", "receipt", "epoch"])
def test_plan2_mixed_versions_refuse(document: str) -> None:
    inventory = empty_inventory()
    selection, declaration, receipt = (
        parse_document(d) for d in linked_documents(documents(inventory), [])
    )
    target = {
        "selection": selection,
        "declaration": declaration,
        "receipt": receipt,
        "epoch": wire_object(selection["epoch_bindings"]),
    }[document]
    target["version"] = 1
    with pytest.raises(InvalidIdentity, match="^ownership_wire$"):
        _ = recovery.build_recovery_plan(
            inventory, *resign_documents(selection, declaration, receipt)
        )


def publication_linked_effects(
    inventory: recovery.OwnershipInventory,
    owner: OwnerRef,
    decisions: tuple[recovery.RecoveryDecision, ...],
) -> list[dict[str, object]]:
    decision = next(d for d in decisions if d.owner == owner)
    original = next(r for r in decision.roles if r.purpose == "original_artifact")
    file = next(
        dict(r.values)
        for r in inventory.closure.raw_rows
        if r.table == "import_publication_files"
        and dict(r.values)["publication_id"] == owner.key[0]
    )
    publication = next(
        dict(r.values)
        for r in inventory.closure.raw_rows
        if r.table == "import_publications" and dict(r.values)["id"] == owner.key[0]
    )
    affected = next(
        o
        for o in inventory.observed
        if o.spec.owner
        == OwnerRef("queue_source", (cast(int, publication["queue_id"]),))
        and o.spec.role
        == RoleKey(cast(int, file["file_id"]), "destination", "file", None)
    )
    return [
        {
            "decision_sha256": digest(decision.to_value()),
            "original_role": recovery._role_value(original),
            "affected_owner": recovery._owner_value(affected.spec.owner),
            "affected_role": recovery._role_value(affected.spec.role),
            "observation_sha256": digest(recovery._observed_value(affected)),
            "relationship": {
                "kind": "publication-destination",
                "publication_id": publication["id"],
                "publication_file_id": file["id"],
                "queue_id": publication["queue_id"],
                "file_id": file["file_id"],
            },
            "effect": "create-name"
            if affected.outcome == "absent"
            else "replace-acknowledged-name",
            "handling": "retain-reference-non-replayable",
        }
    ]


@pytest.mark.parametrize("replace_public", [False, True])
def test_linked_publication_create_and_replace_require_review(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    replace_public: bool,
) -> None:
    env = producer_media
    unused = producer_queue(env)
    with closing(sqlite3.connect(env.db_path)) as db:
        _ = db.execute("DELETE FROM import_queue_files WHERE queue_id=?", (unused,))
        _ = db.execute("DELETE FROM import_queue WHERE id=?", (unused,))
        db.commit()
    identifier = acknowledged_publication(env, monkeypatch, "original")
    if replace_public:
        import import_publication as producer

        def unsupported(*_args: object, **_kwargs: object) -> NoReturn:
            raise OSError(errno.EOPNOTSUPP, "unit real hardlink fallback")

        with monkeypatch.context() as patch:
            patch.setattr(producer, "_rename_noreplace", unsupported)
            assert producer.publish_publication(identifier, "a" * 32)
    inventory = capture_inventory(env, monkeypatch)
    owner = OwnerRef("publication", (identifier,))
    if replace_public:
        legacy = rollback_documents(inventory, owner)
        decisions = tuple(
            recovery.RecoveryDecision.from_value(v)
            for v in wire_array(parse_document(legacy[0])["decisions"])
        )
    else:
        decisions = chosen(
            inventory,
            owner,
            "restore-original",
            {"original_artifact"},
            str(env.library / "Matrix v02.cbz"),
        )
        legacy = review_documents(inventory, decisions)
    effects = publication_linked_effects(inventory, owner, decisions)
    relationship = wire_object(effects[0]["relationship"])
    assert relationship["publication_file_id"] != relationship["file_id"]
    assert recovery._required_linked_effects(inventory, decisions) == tuple(effects)
    for inputs in (legacy, linked_documents(legacy, [])):
        with pytest.raises(InvalidIdentity, match="^ownership_phase$"):
            _ = recovery.build_recovery_plan(inventory, *inputs)
    if replace_public:
        successor = rollback_documents(inventory, owner, linked=True)
        decisions = tuple(
            recovery.RecoveryDecision.from_value(v)
            for v in wire_array(parse_document(successor[0])["decisions"])
        )
        effects = publication_linked_effects(inventory, owner, decisions)
    else:
        successor = legacy
    inputs = linked_documents(successor, effects)
    before = (
        captured_fixture(env)[1],
        media_snapshot(env.scope),
        control_snapshot(env.db_path),
        inventory.to_json(),
    )
    with pack_validation_boundaries(monkeypatch):
        plan = recovery.build_recovery_plan(inventory, *inputs)
        assert plan.version == 2 and plan.decisions == decisions
        assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan
        assert recovery.build_recovery_plan(inventory, *inputs) == plan
        assert (
            wire_object(parse_document(plan.epoch_bindings_json))["linked_effects"]
            == effects
        )
        assert len(
            wire_array(parse_document(plan.epoch_bindings_json)["rollback_outputs"])
        ) == int(replace_public)
    assert (
        captured_fixture(env)[1],
        media_snapshot(env.scope),
        control_snapshot(env.db_path),
        inventory.to_json(),
    ) == before
    if replace_public:
        selection, declaration, receipt = (parse_document(d) for d in inputs)
        media = next(
            wire_object(v)
            for v in wire_array(receipt["media"])
            if wire_object(wire_object(v)["role"])["purpose"] == "admission_destination"
        )
        media.update(handling="retain-in-place", recovery_reference=None)
        assert_linked_refusal(
            inventory,
            resign_documents(selection, declaration, receipt),
            "ownership_binding",
        )


def witnessed_inventory(
    env: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> recovery.OwnershipInventory:
    from file_identity import NativeHandle

    closure, _ = captured_fixture(env)

    def native(_fd: int) -> NativeHandle:
        return NativeHandle(1, b"A3-unit-only")

    monkeypatch.setattr(recovery, "capture_native", native)
    with file_mutation_guard(str(env.db_path)) as guard:
        inventory = recovery._observe_closure(
            guard, str(env.scope), closure, InventoryBudget(), inventory_version=2
        )
    assert inventory.unresolved == ()
    return inventory


def pack_claim_decisions(
    inventory: recovery.OwnershipInventory, owner: OwnerRef, claim: PackDirectoryClaim
) -> tuple[recovery.RecoveryDecision, ...]:
    captured = claim.carrier.carrier_path + "/artifact"
    subjects = {
        s.role
        for s in inventory.closure.roles
        if s.owner == owner
        and (s.path == captured or s.path.startswith(captured + "/"))
    }
    return tuple(
        recovery.RecoveryDecision(
            s.owner,
            "restore-original" if s.owner == owner else "retain-blocked",
            tuple(
                r.role
                for r in inventory.closure.roles
                if r.owner == s.owner and r.role in subjects
            )
            if s.owner == owner
            else (),
            claim.source_directory.path if s.owner == owner else None,
            tuple(
                r.role
                for r in inventory.closure.roles
                if r.owner == s.owner and (s.owner != owner or r.role not in subjects)
            ),
        )
        for s in inventory.closure.snapshots
    )


def expected_pack_effects(
    inventory: recovery.OwnershipInventory,
    decisions: tuple[recovery.RecoveryDecision, ...],
    claim: PackDirectoryClaim,
    raw_ownership: str,
) -> list[dict[str, object]]:
    decision = next(d for d in decisions if d.action == "restore-original")
    primary = next(
        r
        for r in decision.roles
        if r.purpose in {"tombstone_captured", "pack_claim_0_captured"}
    )
    root = claim.source_directory.path
    row = next(
        dict(r.values)
        for r in inventory.closure.raw_rows
        if r.table
        == (
            "import_pack_cleanup_tombstones"
            if decision.owner.domain == "pack_tombstone"
            else "import_pack_cleanup_reservations"
        )
    )
    queue_id = cast(int, row["queue_id"])
    effects: list[dict[str, object]] = []
    for observation in inventory.observed:
        spec = observation.spec
        if spec.owner.domain not in {"queue_source", "publication"} or not (
            spec.path == root or spec.path.startswith(root + "/")
        ):
            continue
        if spec.owner.domain == "queue_source" and spec.owner.key != (queue_id,):
            continue
        if spec.owner.domain == "queue_source" and spec.role.purpose in {
            "source",
            "source_directory",
        }:
            relative = spec.path[len(root) + 1 :] if spec.path != root else None
            relationship: dict[str, object] = {
                "kind": "pack-source-file"
                if relative is not None
                else "pack-source-root",
                "queue_id": queue_id,
                "queue_file_id": spec.role.file_id,
                "identity_key": row["download_identity_key"],
                "artifact_owner_token": claim.artifact_owner_token,
                "relative": relative,
                "origin_ownership_sha256": hashlib.sha256(
                    raw_ownership.encode()
                ).hexdigest(),
            }
        else:
            publication = next(
                dict(r.values)
                for r in inventory.closure.raw_rows
                if r.table == "import_publications"
                and (
                    spec.owner.domain != "publication"
                    or dict(r.values)["id"] == spec.owner.key[0]
                )
            )
            frozen = parse_document(cast(str, publication["queue_snapshot_json"]))
            origins = wire_object(wire_object(frozen["_pack_source_origins"])["files"])
            origin = wire_object(next(iter(origins.values())))
            relationship = {
                "kind": "frozen-pack-origin",
                "publication_id": publication["id"]
                if spec.owner.domain == "publication"
                else None,
                "queue_id": queue_id,
                "file_id": origin["file_id"],
                "identity_key": row["download_identity_key"],
                "artifact_owner_token": claim.artifact_owner_token,
                "origin_sha256": digest(origin),
                "origin_ownership_sha256": hashlib.sha256(
                    cast(str, origin["ownership_json"]).encode()
                ).hexdigest(),
                "relative": spec.role.relative,
            }
        effects.append(
            {
                "decision_sha256": digest(decision.to_value()),
                "original_role": recovery._role_value(primary),
                "affected_owner": recovery._owner_value(spec.owner),
                "affected_role": recovery._role_value(spec.role),
                "observation_sha256": digest(recovery._observed_value(observation)),
                "relationship": relationship,
                "effect": "create-name",
                "handling": "retain-reference-non-replayable",
            }
        )
    return sorted(effects, key=encoded)


def pack_linked_inputs(
    inventory: recovery.OwnershipInventory,
    decisions: tuple[recovery.RecoveryDecision, ...],
    effects: list[dict[str, object]],
) -> tuple[str, str, str]:
    selection, declaration, receipt = (
        parse_document(d)
        for d in linked_documents(review_documents(inventory, decisions), effects)
    )
    unreachable = {
        (
            encoded(recovery._owner_value(o.spec.owner)),
            encoded(recovery._role_value(o.spec.role)),
        )
        for o in inventory.observed
        if o.outcome == "unreachable_below_absent_root"
    }
    for value in wire_array(receipt["media"]):
        item = wire_object(value)
        if (encoded(item["owner"]), encoded(item["role"])) in unreachable:
            item.update(handling="observed-unreachable", recovery_reference=None)
    return resign_documents(selection, declaration, receipt)


@pytest.mark.parametrize("incoming_root", [False, True])
def test_pack_restore_reviews_only_actual_source_root(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, incoming_root: bool
) -> None:
    import import_pack_cleanup as packs

    env = producer_media

    def defer_transfer(*_args: object) -> bool:
        return True

    with monkeypatch.context() as patch:
        patch.setattr(packs, "_record_owned_tombstone", defer_transfer)
        key, queue_id = producer_pack(env, "attached", tombstone=True, nested=True)
    acquired = packs._acquire_cleanup_reservation(
        queue_id=cast(int, queue_id),
        download_id="matrix-generated",
        download_client_id=1,
        protocol="torrent",
        publication_id=None,
        lease_seconds=None,
    )
    assert acquired is not None
    reservation = packs._read_reservation(key)
    assert reservation is not None and reservation.purpose == "cleanup"
    ownership = packs._ownership(reservation)
    assert ownership is not None and ownership.claims
    with file_mutation_guard(str(env.db_path)) as guard:
        _ = packs._save_ownership(
            guard,
            reservation,
            ownership,
            tombstone_path=ownership.claims[0].carrier.carrier_path + "/artifact",
        )
    if incoming_root:
        with closing(sqlite3.connect(env.db_path)) as db:
            _ = db.execute(
                "UPDATE import_queue SET src_dir=? WHERE id=?",
                (str(env.incoming), queue_id),
            )
            db.commit()
    inventory = witnessed_inventory(env, monkeypatch)
    row = next(
        dict(r.values)
        for r in inventory.closure.raw_rows
        if r.table == "import_pack_cleanup_reservations"
    )
    raw = cast(str, row["directory_ownership_json"])
    ownership = PackOwnership.from_json(raw)
    claim = ownership.claims[0]
    decisions = pack_claim_decisions(
        inventory, OwnerRef("pack_reservation", (key,)), claim
    )
    effects = expected_pack_effects(inventory, decisions, claim, raw)
    assert sum(
        wire_object(e["relationship"])["kind"] == "pack-source-root" for e in effects
    ) == int(not incoming_root)
    assert (
        sum(
            wire_object(e["relationship"])["kind"] == "pack-source-file"
            for e in effects
        )
        == 1
    )
    before = (
        captured_fixture(env)[1],
        media_snapshot(env.scope),
        control_snapshot(env.db_path),
    )
    inputs = pack_linked_inputs(inventory, decisions, effects)
    with pack_validation_boundaries(monkeypatch):
        plan = recovery.build_recovery_plan(inventory, *inputs)
        assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan
        assert parse_document(plan.epoch_bindings_json)["linked_effects"] == effects
    assert (
        captured_fixture(env)[1],
        media_snapshot(env.scope),
        control_snapshot(env.db_path),
    ) == before


def producer_bound_frozen_pack(
    env: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, int | None]:
    import import_pack_cleanup as packs

    original_transfer = packs._record_owned_tombstone

    def transfer(reservation: packs._PackReservation, path: str) -> bool:
        assert reservation.queue_id is not None
        with closing(sqlite3.connect(env.db_path)) as db:
            ids = cast(
                list[tuple[int]],
                db.execute(
                    "SELECT id FROM import_publications WHERE queue_id=? AND state='finalized'",
                    (reservation.queue_id,),
                ).fetchall(),
            )
        assert len(ids) == 1
        publication_id = ids[0][0]
        acquired = packs._acquire_cleanup_reservation(
            queue_id=reservation.queue_id,
            download_id=reservation.download_id,
            download_client_id=reservation.download_client_id,
            protocol=reservation.protocol,
            publication_id=publication_id,
            lease_seconds=None,
        )
        assert acquired is not None
        bound = packs._read_reservation(reservation.download_identity_key)
        assert bound is not None and bound.publication_id == publication_id
        return original_transfer(bound, path)

    with monkeypatch.context() as patch:
        patch.setattr(packs, "_record_owned_tombstone", transfer)
        return producer_pack(env, "attached", tombstone=True, nested=True, frozen=True)


@pytest.mark.parametrize("live_queue", [False, True])
def test_frozen_publication_tree_restore_without_live_queue(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, live_queue: bool
) -> None:
    env = producer_media
    _, queue_id = producer_bound_frozen_pack(env, monkeypatch)
    if not live_queue:
        with closing(sqlite3.connect(env.db_path)) as db:
            _ = db.execute(
                "DELETE FROM import_queue_files WHERE queue_id=?", (queue_id,)
            )
            _ = db.execute("DELETE FROM import_queue WHERE id=?", (queue_id,))
            db.commit()
    inventory = witnessed_inventory(env, monkeypatch)
    snapshot = next(
        s for s in inventory.closure.snapshots if s.owner.domain == "pack_tombstone"
    )
    row = wire_object(wire_object(parse_document(snapshot.rows_json)["root"])["row"])
    claim = PackDirectoryClaim.from_json(cast(str, row["carrier_json"]))
    publication = next(
        dict(r.values)
        for r in inventory.closure.raw_rows
        if r.table == "import_publications"
    )
    origin = wire_object(
        next(
            iter(
                wire_object(
                    wire_object(
                        parse_document(cast(str, publication["queue_snapshot_json"]))[
                            "_pack_source_origins"
                        ]
                    )["files"]
                ).values()
            )
        )
    )
    decisions = pack_claim_decisions(inventory, snapshot.owner, claim)
    effects = expected_pack_effects(
        inventory, decisions, claim, cast(str, origin["ownership_json"])
    )
    assert {wire_object(e["affected_role"])["kind"] for e in effects} >= {
        "directory",
        "file",
        "marker",
    }
    assert (
        any(s.owner.domain == "queue_source" for s in inventory.closure.snapshots)
        is live_queue
    )
    assert {wire_object(e["affected_owner"])["domain"] for e in effects} == (
        {"publication", "queue_source"} if live_queue else {"publication"}
    )
    before = (
        captured_fixture(env)[1],
        media_snapshot(env.scope),
        control_snapshot(env.db_path),
        inventory.to_json(),
    )
    inputs = pack_linked_inputs(inventory, decisions, effects)
    with pack_validation_boundaries(monkeypatch):
        plan = recovery.build_recovery_plan(inventory, *inputs)
        assert (
            plan.version == 2
            and recovery.RecoveryPlan.from_json(plan.to_json()) == plan
        )
        assert parse_document(plan.epoch_bindings_json)["linked_effects"] == effects
    assert (
        captured_fixture(env)[1],
        media_snapshot(env.scope),
        control_snapshot(env.db_path),
        inventory.to_json(),
    ) == before


@pytest.mark.parametrize(
    "checkpoint", ["claimed", "partial-discard", "discarded", "post-gc"]
)
def test_pack_claim_cleanup_real_discard_and_gc_windows(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, checkpoint: str
) -> None:
    from test_file_identity_observation_followup import pack_location_checkpoint

    env = producer_media
    if checkpoint == "claimed":
        _ = producer_pack(env, "attached", tombstone=True, nested=True)
    else:
        pack_location_checkpoint(env, monkeypatch, checkpoint)
    inventory = witnessed_inventory(env, monkeypatch)
    snapshot = next(
        s for s in inventory.closure.snapshots if s.owner.domain == "pack_tombstone"
    )
    row = wire_object(wire_object(parse_document(snapshot.rows_json)["root"])["row"])
    claim = PackDirectoryClaim.from_json(cast(str, row["carrier_json"]))
    assert (
        claim.inventory.files
        and claim.carrier.phase
        == {
            "claimed": "claimed",
            "partial-discard": "discarding",
            "discarded": "discarded",
            "post-gc": "discarded",
        }[checkpoint]
    )
    captured = claim.carrier.carrier_path + "/artifact"
    decisions = tuple(
        recovery.RecoveryDecision(
            s.owner,
            "cleanup-committed" if s.owner == snapshot.owner else "retain-blocked",
            tuple(
                r.role
                for r in inventory.closure.roles
                if r.owner == s.owner and recovery._within(r.path, captured)
            )
            if s.owner == snapshot.owner
            else (),
            None,
            tuple(
                r.role
                for r in inventory.closure.roles
                if r.owner == s.owner
                and (
                    s.owner != snapshot.owner or not recovery._within(r.path, captured)
                )
            ),
        )
        for s in inventory.closure.snapshots
    )
    inputs = pack_linked_inputs(inventory, decisions, [])
    before = (
        captured_fixture(env)[1],
        media_snapshot(env.scope),
        control_snapshot(env.db_path),
        inventory.to_json(),
    )
    with pack_validation_boundaries(monkeypatch):
        plan = recovery.build_recovery_plan(inventory, *inputs)
        assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan
    assert (
        captured_fixture(env)[1],
        media_snapshot(env.scope),
        control_snapshot(env.db_path),
        inventory.to_json(),
    ) == before
    restore = pack_claim_decisions(inventory, snapshot.owner, claim)
    if checkpoint != "claimed":
        with pytest.raises(InvalidIdentity, match="^ownership_phase$"):
            _ = recovery.build_recovery_plan(
                inventory, *pack_linked_inputs(inventory, restore, [])
            )


def assert_linked_refusal(
    inventory: recovery.OwnershipInventory, inputs: tuple[str, str, str], code: str
) -> None:
    selection = parse_document(inputs[0])
    decisions = tuple(
        recovery.RecoveryDecision.from_value(v)
        for v in wire_array(selection["decisions"])
    )
    wire: dict[str, object] = {
        "version": selection["version"],
        "inventory": parse_document(inventory.to_json()),
        "decisions": selection["decisions"],
        "declaration_json": inputs[1],
        "backup_receipt_json": inputs[2],
        "epoch_bindings_json": encoded(selection["epoch_bindings"]),
    }
    sha = digest(wire)
    wire["sha256"] = sha
    with pytest.raises(InvalidIdentity, match="^" + code + "$"):
        _ = recovery.build_recovery_plan(inventory, *inputs)
    with pytest.raises(InvalidIdentity, match="^" + code + "$"):
        _ = recovery.RecoveryPlan.from_json(encoded(wire))
    with pytest.raises(InvalidIdentity, match="^" + code + "$"):
        _ = recovery.RecoveryPlan(
            cast(int, selection["version"]),
            inventory,
            decisions,
            inputs[1],
            inputs[2],
            encoded(selection["epoch_bindings"]),
            sha,
        )


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "duplicate",
        "extra",
        "decision",
        "original",
        "affected_owner",
        "affected_role",
        "observation",
        "publication",
        "publication_file",
        "queue",
        "file",
        "boolean_id",
        "effect",
        "handling",
        "review_missing",
        "review_extra",
        "role_authority",
        "linked_authority",
        "media",
        "v1_assertion",
    ],
)
def test_linked_publication_resigned_effects_refuse(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    env = producer_media
    identifier = acknowledged_publication(env, monkeypatch, "original")
    inventory = capture_inventory(env, monkeypatch)
    owner = OwnerRef("publication", (identifier,))
    decisions = chosen(
        inventory,
        owner,
        "restore-original",
        {"original_artifact"},
        str(env.library / "Matrix v02.cbz"),
    )
    effects = publication_linked_effects(inventory, owner, decisions)
    selection, declaration, receipt = (
        parse_document(d)
        for d in linked_documents(review_documents(inventory, decisions), effects)
    )
    effect = wire_object(
        wire_array(wire_object(selection["epoch_bindings"])["linked_effects"])[0]
    )
    relationship = wire_object(effect["relationship"])
    code = "ownership_binding"
    if change == "missing":
        wire_object(selection["epoch_bindings"])["linked_effects"] = []
        code = "ownership_phase"
    elif change == "duplicate":
        wire_array(wire_object(selection["epoch_bindings"])["linked_effects"]).append(
            dict(effect)
        )
        code = "ownership_wire"
    elif change == "extra":
        effect["extra"] = None
    elif change in {"publication", "publication_file", "queue", "file"}:
        key = {
            "publication": "publication_id",
            "publication_file": "publication_file_id",
            "queue": "queue_id",
            "file": "file_id",
        }[change]
        relationship[key] = cast(int, relationship[key]) + 1
    elif change == "boolean_id":
        relationship["publication_file_id"] = True
    elif change == "decision":
        effect["decision_sha256"] = "0" * 64
    elif change == "original":
        wire_object(effect["original_role"])["purpose"] = "source_artifact"
    elif change == "affected_owner":
        wire_object(effect["affected_owner"])["key"] = [999]
    elif change == "affected_role":
        wire_object(effect["affected_role"])["purpose"] = "source"
    elif change == "observation":
        effect["observation_sha256"] = "0" * 64
    elif change == "effect":
        effect["effect"] = "replace-acknowledged-name"
    elif change == "handling":
        effect["handling"] = "retain-in-place"
    elif change == "review_missing":
        declaration["linked_reviews"] = []
    elif change == "review_extra":
        wire_object(wire_array(declaration["linked_reviews"])[0])["effect_sha256"] = (
            "0" * 64
        )
    elif change == "role_authority":
        for value in wire_array(declaration["role_reviews"]):
            review = wire_object(value)
            if review["assertion"] == "reviewed-linked-reference":
                review["authority_id"] = None
        code = "ownership_authority"
    elif change == "linked_authority":
        wire_object(wire_array(declaration["linked_reviews"])[0])["authority_id"] = (
            "b" * 32
        )
    elif change == "media":
        for value in wire_array(receipt["media"]):
            media = wire_object(value)
            if (
                media["owner"] == effect["affected_owner"]
                and media["role"] == effect["affected_role"]
            ):
                media.update(
                    handling="snapshot", recovery_reference="nonexistent bytes"
                )
    elif change == "v1_assertion":
        selection, declaration, receipt = (
            parse_document(d) for d in review_documents(inventory, decisions)
        )
        for value in wire_array(declaration["role_reviews"]):
            review = wire_object(value)
            if review["assertion"] == "retain-only":
                review["assertion"] = "reviewed-linked-reference"
                break
    before = (
        captured_fixture(env)[1],
        media_snapshot(env.scope),
        control_snapshot(env.db_path),
        inventory.to_json(),
    )
    with pack_validation_boundaries(monkeypatch):
        assert_linked_refusal(
            inventory, resign_documents(selection, declaration, receipt), code
        )
    assert (
        captured_fixture(env)[1],
        media_snapshot(env.scope),
        control_snapshot(env.db_path),
        inventory.to_json(),
    ) == before


@pytest.mark.parametrize("checkpoint", ["missing-uncommitted", "missing-committed"])
def test_pack_claim_cleanup_exact_committed_missing_file(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, checkpoint: str
) -> None:
    env = producer_media
    _ = producer_pack(env, "attached", tombstone=True, nested=True, frozen=True)
    closure, _ = captured_fixture(env)
    row = next(
        dict(r.values)
        for r in closure.raw_rows
        if r.table == "import_pack_cleanup_tombstones"
    )
    claim = PackDirectoryClaim.from_json(cast(str, row["carrier_json"]))
    (Path(claim.carrier.carrier_path) / "artifact" / "Matrix v02.cbz").unlink()
    if checkpoint == "missing-committed":
        with closing(sqlite3.connect(env.db_path)) as db:
            _ = db.execute(
                "UPDATE import_publication_files SET cleanup_state='deleted'"
            )
            db.commit()
    inventory = witnessed_inventory(env, monkeypatch)
    owner = next(
        s.owner
        for s in inventory.closure.snapshots
        if s.owner.domain == "pack_tombstone"
    )
    restores = pack_claim_decisions(inventory, owner, claim)
    decisions = tuple(
        replace(d, action="cleanup-committed", destination=None)
        if d.owner == owner
        else d
        for d in restores
    )
    inputs = pack_linked_inputs(inventory, decisions, [])
    before = (
        captured_fixture(env)[1],
        media_snapshot(env.scope),
        control_snapshot(env.db_path),
        inventory.to_json(),
    )
    with pack_validation_boundaries(monkeypatch):
        if checkpoint == "missing-committed":
            plan = recovery.build_recovery_plan(inventory, *inputs)
            assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan
        else:
            assert_linked_refusal(inventory, inputs, "ownership_phase")
    assert (
        captured_fixture(env)[1],
        media_snapshot(env.scope),
        control_snapshot(env.db_path),
        inventory.to_json(),
    ) == before


def test_pack_claim_cleanup_individual_leaf_keeps_its_tree(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = producer_media
    _ = producer_pack(env, "attached", tombstone=True, nested=True)
    inventory = witnessed_inventory(env, monkeypatch)
    owner = next(
        s.owner
        for s in inventory.closure.snapshots
        if s.owner.domain == "pack_tombstone"
    )
    decisions = chosen(
        inventory, owner, "cleanup-committed", {"tombstone_captured_file"}
    )
    assert all(r.kind == "file" for d in decisions for r in d.roles)
    with pack_validation_boundaries(monkeypatch):
        plan = recovery.build_recovery_plan(
            inventory, *pack_linked_inputs(inventory, decisions, [])
        )
        assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan


@pytest.mark.parametrize("checkpoint", ["discarded", "post-gc"])
def test_pack_placement_cleanup_real_discard_barrier(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, checkpoint: str
) -> None:
    import import_pack_cleanup as packs

    env = producer_media
    key, _ = terminal_pack_candidate(env, nested=True)
    reservation = packs._read_reservation(key)
    assert reservation is not None

    def interrupted(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError("A3 stop before placement GC")

    with file_mutation_guard(str(env.db_path)) as guard, monkeypatch.context() as patch:
        if checkpoint == "discarded":
            patch.setattr(packs, "gc_discarded_carrier", interrupted)
            with pytest.raises(OSError, match="A3 stop"):
                _ = packs._discard_pack_placement(guard, reservation)
        else:
            _ = packs._discard_pack_placement(guard, reservation)
    inventory = witnessed_inventory(env, monkeypatch)
    row = next(
        dict(r.values)
        for r in inventory.closure.raw_rows
        if r.table == "import_pack_cleanup_reservations"
    )
    ownership = PackOwnership.from_json(cast(str, row["directory_ownership_json"]))
    assert (
        ownership.placement_carrier is not None
        and ownership.placement_carrier.phase == "discarded"
    )
    assert ownership.canonical_directory is not None
    owner = OwnerRef("pack_reservation", (key,))
    subjects = {
        o.spec.role
        for o in inventory.observed
        if o.spec.owner == owner
        and recovery._within(o.spec.path, ownership.canonical_directory.path)
    }
    decisions = tuple(
        recovery.RecoveryDecision(
            s.owner,
            "cleanup-committed" if s.owner == owner else "retain-blocked",
            tuple(
                r.role
                for r in inventory.closure.roles
                if r.owner == s.owner and r.role in subjects
            )
            if s.owner == owner
            else (),
            None,
            tuple(
                r.role
                for r in inventory.closure.roles
                if r.owner == s.owner and (s.owner != owner or r.role not in subjects)
            ),
        )
        for s in inventory.closure.snapshots
    )
    before = (
        captured_fixture(env)[1],
        media_snapshot(env.scope),
        control_snapshot(env.db_path),
        inventory.to_json(),
    )
    with pack_validation_boundaries(monkeypatch):
        plan = recovery.build_recovery_plan(
            inventory, *pack_linked_inputs(inventory, decisions, [])
        )
        assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan
    assert (
        captured_fixture(env)[1],
        media_snapshot(env.scope),
        control_snapshot(env.db_path),
        inventory.to_json(),
    ) == before


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "extra",
        "duplicate",
        "reordered",
        "conflicting",
        "generation",
        "origin_hash",
        "relative",
        "file",
        "queue",
        "missing_marker_effect",
        "unreachable_snapshot",
        "captured_snapshot",
    ],
)
def test_frozen_pack_resigned_complete_effect_set_refuses(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    env = producer_media
    _ = producer_pack(env, "attached", tombstone=True, nested=True, frozen=True)
    inventory = witnessed_inventory(env, monkeypatch)
    owner = next(
        s.owner
        for s in inventory.closure.snapshots
        if s.owner.domain == "pack_tombstone"
    )
    row = next(
        dict(r.values)
        for r in inventory.closure.raw_rows
        if r.table == "import_pack_cleanup_tombstones"
    )
    claim = PackDirectoryClaim.from_json(cast(str, row["carrier_json"]))
    publication = next(
        dict(r.values)
        for r in inventory.closure.raw_rows
        if r.table == "import_publications"
    )
    origin = wire_object(
        next(
            iter(
                wire_object(
                    wire_object(
                        parse_document(cast(str, publication["queue_snapshot_json"]))[
                            "_pack_source_origins"
                        ]
                    )["files"]
                ).values()
            )
        )
    )
    decisions = pack_claim_decisions(inventory, owner, claim)
    effects = expected_pack_effects(
        inventory, decisions, claim, cast(str, origin["ownership_json"])
    )
    selection, declaration, receipt = (
        parse_document(d) for d in pack_linked_inputs(inventory, decisions, effects)
    )
    supplied = wire_array(wire_object(selection["epoch_bindings"])["linked_effects"])
    effect = wire_object(
        next(
            v
            for v in supplied
            if wire_object(wire_object(v)["relationship"])["kind"]
            == "frozen-pack-origin"
        )
    )
    code = "ownership_binding"
    if change == "missing":
        _ = supplied.pop()
    elif change == "extra":
        added = dict(effect)
        added["decision_sha256"] = "0" * 64
        supplied.append(added)
        supplied.sort(key=encoded)
    elif change == "duplicate":
        supplied.append(dict(effect))
        supplied.sort(key=encoded)
        code = "ownership_wire"
    elif change == "reordered":
        supplied.reverse()
        code = "ownership_wire"
    elif change == "conflicting":
        added = dict(effect)
        added["effect"] = "replace-acknowledged-name"
        supplied.append(added)
        supplied.sort(key=encoded)
    elif change in {"generation", "origin_hash", "relative", "file", "queue"}:
        relationship = wire_object(effect["relationship"])
        key = {
            "generation": "origin_ownership_sha256",
            "origin_hash": "origin_sha256",
            "relative": "relative",
            "file": "file_id",
            "queue": "queue_id",
        }[change]
        relationship[key] = (
            "0" * 64
            if change in {"generation", "origin_hash"}
            else "foreign/file.cbz"
            if change == "relative"
            else 999
        )
        supplied.sort(key=encoded)
    elif change == "missing_marker_effect":
        supplied[:] = [
            v
            for v in supplied
            if wire_object(wire_object(v)["affected_role"])["kind"] != "marker"
        ]
    elif change == "unreachable_snapshot":
        target = next(
            o
            for o in inventory.observed
            if o.outcome == "unreachable_below_absent_root"
        )
        media = next(
            wire_object(v)
            for v in wire_array(receipt["media"])
            if wire_object(v)["owner"] == recovery._owner_value(target.spec.owner)
            and wire_object(v)["role"] == recovery._role_value(target.spec.role)
        )
        media.update(
            handling="snapshot", recovery_reference="cannot snapshot unreachable bytes"
        )
    elif change == "captured_snapshot":
        media = next(
            wire_object(v)
            for v in wire_array(receipt["media"])
            if wire_object(v)["owner"] == recovery._owner_value(owner)
            and wire_object(wire_object(v)["role"])["purpose"]
            == "tombstone_captured_file"
        )
        media.update(handling="retain-in-place", recovery_reference=None)
    before = (
        captured_fixture(env)[1],
        media_snapshot(env.scope),
        control_snapshot(env.db_path),
        inventory.to_json(),
    )
    with pack_validation_boundaries(monkeypatch):
        assert_linked_refusal(
            inventory, resign_documents(selection, declaration, receipt), code
        )
    assert (
        captured_fixture(env)[1],
        media_snapshot(env.scope),
        control_snapshot(env.db_path),
        inventory.to_json(),
    ) == before


@pytest.mark.parametrize(
    "change",
    [
        "present_source",
        "active_sibling",
        "independent_source",
        "terminal_lease",
        "retained_skip",
        "missing_marker",
        "changed_marker",
    ],
)
def test_linked_pack_independent_fences_and_current_bytes_refuse(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    env = producer_media
    sibling = (
        producer_queue(env)
        if change in {"active_sibling", "independent_source"}
        else None
    )
    _, queue_id = producer_pack(
        env, "attached", tombstone=True, nested=True, frozen=True
    )
    closure, _ = captured_fixture(env)
    row = next(
        dict(r.values)
        for r in closure.raw_rows
        if r.table == "import_pack_cleanup_tombstones"
    )
    claim = PackDirectoryClaim.from_json(cast(str, row["carrier_json"]))
    if change == "present_source":
        Path(claim.source_directory.path).mkdir()
    elif change in {"active_sibling", "independent_source"}:
        assert sibling is not None
        with closing(sqlite3.connect(env.db_path)) as db:
            if change == "active_sibling":
                _ = db.execute(
                    "UPDATE import_queue SET download_id='matrix-generated',torrent_url='magnet:matrix-generated' WHERE id=?",
                    (sibling,),
                )
            else:
                _ = db.execute(
                    "UPDATE import_queue_files SET src_path=? WHERE queue_id=?",
                    (claim.carrier.carrier_path + "/artifact/Matrix v02.cbz", sibling),
                )
            db.commit()
    elif change in {"terminal_lease", "retained_skip"}:
        with closing(sqlite3.connect(env.db_path)) as db:
            if change == "terminal_lease":
                _ = db.execute(
                    "UPDATE import_queue SET lease_owner='independent-lease' WHERE id=?",
                    (queue_id,),
                )
            else:
                _ = db.execute(
                    "UPDATE import_queue_files SET status='skipped' WHERE queue_id=?",
                    (queue_id,),
                )
            db.commit()
    else:
        marker = Path(claim.carrier.carrier_path) / "artifact" / "mangarr-pack-owner"
        if change == "missing_marker":
            marker.unlink()
        else:
            _ = marker.write_bytes(b"changed pack marker")
    inventory = witnessed_inventory(env, monkeypatch)
    owner = next(
        s.owner
        for s in inventory.closure.snapshots
        if s.owner.domain == "pack_tombstone"
    )
    publication = next(
        dict(r.values)
        for r in inventory.closure.raw_rows
        if r.table == "import_publications"
    )
    origin = wire_object(
        next(
            iter(
                wire_object(
                    wire_object(
                        parse_document(cast(str, publication["queue_snapshot_json"]))[
                            "_pack_source_origins"
                        ]
                    )["files"]
                ).values()
            )
        )
    )
    decisions = pack_claim_decisions(inventory, owner, claim)
    effects = expected_pack_effects(
        inventory, decisions, claim, cast(str, origin["ownership_json"])
    )
    inputs = pack_linked_inputs(inventory, decisions, effects)
    code = (
        "ownership_context"
        if change in {"active_sibling", "terminal_lease", "retained_skip"}
        else "ownership_phase"
    )
    before = (
        captured_fixture(env)[1],
        media_snapshot(env.scope),
        control_snapshot(env.db_path),
        inventory.to_json(),
    )
    with pack_validation_boundaries(monkeypatch):
        assert_linked_refusal(inventory, inputs, code)
    assert (
        captured_fixture(env)[1],
        media_snapshot(env.scope),
        control_snapshot(env.db_path),
        inventory.to_json(),
    ) == before


@pytest.mark.parametrize(
    "change",
    [
        "admission_identity",
        "admission_effective",
        "admission_kind",
        "mapping",
        "source",
        "lease",
        "destination",
        "download",
    ],
)
def test_linked_publication_exact_captured_context_refuses(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    env = producer_media
    identifier = acknowledged_publication(env, monkeypatch, "original")
    with closing(sqlite3.connect(env.db_path)) as db:
        row = db.execute(
            "SELECT queue_id,queue_snapshot_json FROM import_publications WHERE id=?",
            (identifier,),
        ).fetchone()
        assert row is not None
        queue_id, frozen_text = cast(tuple[int, str], row)
        if change.startswith("admission_") or change == "mapping":
            frozen = parse_document(frozen_text)
            admission = wire_object(
                wire_object(frozen["_file_publication"])["admission"]
            )
            file = wire_object(next(iter(wire_object(admission["files"]).values())))
            if change == "admission_identity":
                wire_array(admission["identity"])[2] = "foreign-download"
            elif change == "admission_effective":
                wire_object(file["effective"])["dst_path"] = str(
                    env.library / "foreign.cbz"
                )
            elif change == "admission_kind":
                wire_object(file["effective"])["import_kind"] = "chapter"
            else:
                wire_object(file["mapping"])["filename"] = "foreign.cbz"
            _ = db.execute(
                "UPDATE import_publications SET queue_snapshot_json=? WHERE id=?",
                (encoded(frozen), identifier),
            )
        elif change == "source":
            _ = db.execute(
                "UPDATE import_queue_files SET src_path=? WHERE queue_id=?",
                (str(env.source), queue_id),
            )
        elif change == "destination":
            _ = db.execute(
                "UPDATE import_queue_files SET dst_path=? WHERE queue_id=?",
                (str(env.library / "foreign.cbz"), queue_id),
            )
        elif change == "lease":
            _ = db.execute(
                "UPDATE import_queue SET lease_owner='foreign-lease' WHERE id=?",
                (queue_id,),
            )
        else:
            _ = db.execute(
                "UPDATE import_queue SET download_id='foreign-download' WHERE id=?",
                (queue_id,),
            )
        db.commit()
    inventory = capture_inventory(env, monkeypatch)
    owner = OwnerRef("publication", (identifier,))
    decisions = chosen(
        inventory,
        owner,
        "restore-original",
        {"original_artifact"},
        str(env.library / "Matrix v02.cbz"),
    )
    inputs = linked_documents(
        review_documents(inventory, decisions),
        publication_linked_effects(inventory, owner, decisions),
    )
    before = (
        captured_fixture(env)[1],
        media_snapshot(env.scope),
        control_snapshot(env.db_path),
        inventory.to_json(),
    )
    with pack_validation_boundaries(monkeypatch):
        assert_linked_refusal(inventory, inputs, "ownership_context")
    assert (
        captured_fixture(env)[1],
        media_snapshot(env.scope),
        control_snapshot(env.db_path),
        inventory.to_json(),
    ) == before


def test_transferred_pack_without_raw_ownership_retains_without_fabrication(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = producer_media
    _ = producer_pack(env, "attached", tombstone=True, nested=True)
    inventory = witnessed_inventory(env, monkeypatch)
    assert not any(
        r.table == "import_pack_cleanup_reservations"
        for r in inventory.closure.raw_rows
    )
    owner = next(
        s.owner
        for s in inventory.closure.snapshots
        if s.owner.domain == "pack_tombstone"
    )
    row = next(
        dict(r.values)
        for r in inventory.closure.raw_rows
        if r.table == "import_pack_cleanup_tombstones"
    )
    assert row["carrier_json"] is not None and "ownership_json" not in row
    claim = PackDirectoryClaim.from_json(cast(str, row["carrier_json"]))
    retained = tuple(
        recovery.RecoveryDecision(
            s.owner,
            "retain-blocked",
            (),
            None,
            tuple(r.role for r in inventory.closure.roles if r.owner == s.owner),
        )
        for s in inventory.closure.snapshots
    )
    before = (
        captured_fixture(env)[1],
        media_snapshot(env.scope),
        control_snapshot(env.db_path),
        inventory.to_json(),
    )
    with pack_validation_boundaries(monkeypatch):
        plan = recovery.build_recovery_plan(
            inventory, *pack_linked_inputs(inventory, retained, [])
        )
        assert all(d.action == "retain-blocked" for d in plan.decisions)
        decisions = pack_claim_decisions(inventory, owner, claim)
        assert_linked_refusal(
            inventory, pack_linked_inputs(inventory, decisions, []), "ownership_phase"
        )
    assert (
        captured_fixture(env)[1],
        media_snapshot(env.scope),
        control_snapshot(env.db_path),
        inventory.to_json(),
    ) == before


@contextmanager
def transferred_claim_conservation(
    env: ProducerMedia, monkeypatch: pytest.MonkeyPatch, *, pure: bool = True
) -> Iterator[None]:
    import os
    import threading
    from test_file_identity_inventory import all_rows

    def state() -> tuple[object, ...]:
        with closing(sqlite3.connect(env.db_path)) as db:
            rows = all_rows(db)
        return (
            rows,
            media_snapshot(env.scope),
            control_snapshot(env.db_path),
            set(os.listdir("/proc/self/fd")),
            tuple(threading.enumerate()),
        )

    before = state()
    try:
        if pure:
            with pack_validation_boundaries(monkeypatch):
                yield
        else:
            yield
    finally:
        assert state() == before


def transferred_claim(
    inventory: recovery.OwnershipInventory,
) -> tuple[OwnerRef, PackDirectoryClaim, str]:
    snapshot = next(
        s for s in inventory.closure.snapshots if s.owner.domain == "pack_tombstone"
    )
    row = wire_object(wire_object(parse_document(snapshot.rows_json)["root"])["row"])
    raw = cast(str, row["carrier_json"])
    return snapshot.owner, PackDirectoryClaim.from_json(raw), raw


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("reserialized", [False, True])
def test_transferred_claim_derives_exact_file_and_root_references(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    nested: bool,
    reserialized: bool,
) -> None:
    env = producer_media
    key, queue_id = producer_pack(env, "attached", tombstone=True, nested=nested)
    if reserialized:
        with closing(sqlite3.connect(env.db_path)) as db, db:
            raw_claim = cast(
                str,
                db.execute(
                    "SELECT carrier_json FROM import_pack_cleanup_tombstones"
                ).fetchone()[0],
            )
            _ = db.execute(
                "UPDATE import_pack_cleanup_tombstones SET carrier_json=?",
                (json.dumps(parse_document(raw_claim), indent=2),),
            )
    inventory = witnessed_inventory(env, monkeypatch)
    owner, claim, raw = transferred_claim(inventory)
    assert (raw != claim.to_json()) == reserialized
    assert claim.source_directory.marker_fingerprint is not None
    assert len(claim.inventory.files) == (2 if nested else 1)
    assert not any(
        r.table in {"import_pack_cleanup_reservations", "import_publications"}
        for r in inventory.closure.raw_rows
    )
    queue_snapshot = next(
        s for s in inventory.closure.snapshots if s.owner.domain == "queue_source"
    )
    queue_document = parse_document(queue_snapshot.rows_json)
    assert "_pack_source_origins" not in wire_object(
        wire_object(queue_document["root"])["row"]
    )
    assert not any(
        wire_object(r)["table"] == "import_publications"
        for r in wire_array(queue_document["related"])
    )
    decisions = pack_claim_decisions(inventory, owner, claim)
    decision = next(d for d in decisions if d.owner == owner)
    primary = RoleKey(None, "tombstone_captured", "directory", None)
    assert primary in decision.roles
    assert decision.destination == claim.source_directory.path
    queue_owner = OwnerRef("queue_source", (cast(int, queue_id),))
    snapshots = {s.owner: s for s in inventory.closure.snapshots}
    file = next(
        dict(r.values)
        for r in inventory.closure.raw_rows
        if r.table == "import_queue_files" and dict(r.values)["queue_id"] == queue_id
    )
    common: dict[str, object] = {
        "claim_owner": {"domain": owner.domain, "key": list(owner.key)},
        "claim_role": {
            "file_id": None,
            "purpose": "tombstone_captured",
            "kind": "directory",
            "relative": None,
        },
        "claim_snapshot_sha256": snapshots[owner].sha256,
        "claim_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "queue_snapshot_sha256": snapshots[queue_owner].sha256,
        "queue_id": queue_id,
        "identity_key": key,
        "artifact_owner_token": claim.artifact_owner_token,
    }
    expected = {
        RoleKey(cast(int, file["id"]), "source", "file", None): {
            **common,
            "kind": "transferred-pack-claim-source-file",
            "queue_file_id": file["id"],
            "relative": "Matrix v02.cbz",
            "source_fingerprint": asdict(claim.inventory.files["Matrix v02.cbz"]),
        },
        RoleKey(None, "source_directory", "directory", None): {
            **common,
            "kind": "transferred-pack-claim-source-root",
            "queue_file_id": None,
            "relative": None,
            "source_fingerprint": None,
        },
    }
    serialized = inventory.to_json()
    with transferred_claim_conservation(env, monkeypatch):
        references = consumers._pack_restore_references(
            inventory.closure, owner, claim, None
        )
        assert {
            spec.role: relationship for spec, relationship in references
        } == expected
        assert tuple(spec for spec, _ in references) == tuple(
            sorted((spec for spec, _ in references), key=consumers._role_order)
        )
        assert all(
            spec.owner == queue_owner and spec in inventory.closure.roles
            for spec, _ in references
        )
        assert all(
            o.outcome in {"absent", "unreachable_below_absent_root"}
            for o in inventory.observed
            if o.spec in {spec for spec, _ in references}
        )
        assert inventory.to_json() == serialized


@pytest.mark.parametrize(
    "mutation",
    [
        "owner",
        "domain",
        "queue",
        "file",
        "client",
        "protocol",
        "download",
        "normalized",
        "key",
        "token",
        "marker",
        "original",
        "capture",
        "extra-entry",
        "fingerprint",
        "missing-marker",
        "suffix",
        "cleanup-suffix",
        "binding-key",
        "binding-file",
        "unsafe-token",
        "null-queue",
        "bool-queue",
        "bool-file",
        "absent-queue",
        "no-file",
    ],
)
def test_transferred_claim_binding_mutations_refuse(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    env = producer_media
    _, queue_id = producer_pack(env, "attached", tombstone=True, nested=True)
    original = witnessed_inventory(env, monkeypatch)
    owner, claim, raw = transferred_claim(original)
    changed = parse_document(raw)
    carrier = wire_object(changed["carrier"])
    source = wire_object(changed["source_directory"])
    binding = wire_object(carrier["binding"])
    argument_only = mutation in {
        "owner",
        "extra-entry",
        "fingerprint",
        "bool-queue",
        "bool-file",
        "null-queue",
    }
    with closing(sqlite3.connect(env.db_path)) as db, db:
        if mutation == "queue":
            _ = db.execute(
                "UPDATE import_pack_cleanup_tombstones SET queue_id=?",
                (cast(int, queue_id) + 100,),
            )
        elif mutation == "file":
            _ = db.execute(
                "UPDATE import_queue_files SET queue_id=? WHERE queue_id=?",
                (cast(int, queue_id) + 100, queue_id),
            )
        elif mutation == "client":
            _ = db.execute(
                "UPDATE import_queue SET download_client_id=2 WHERE id=?", (queue_id,)
            )
        elif mutation == "protocol":
            _ = db.execute(
                "UPDATE import_queue SET download_protocol='nzb' WHERE id=?",
                (queue_id,),
            )
        elif mutation == "download":
            _ = db.execute(
                "UPDATE import_queue SET download_id='other-download' WHERE id=?",
                (queue_id,),
            )
        elif mutation == "normalized":
            _ = db.execute(
                "UPDATE import_pack_cleanup_tombstones SET normalized_download_id='other-download'"
            )
        elif mutation == "key":
            _ = db.execute(
                "UPDATE import_pack_cleanup_tombstones SET download_identity_key='foreign-key'"
            )
        elif mutation == "capture":
            _ = db.execute(
                "UPDATE import_pack_cleanup_tombstones SET tombstone_path=tombstone_path||'-foreign'"
            )
        elif mutation == "absent-queue":
            _ = db.execute(
                "DELETE FROM import_queue_files WHERE queue_id=?", (queue_id,)
            )
            _ = db.execute("DELETE FROM import_queue WHERE id=?", (queue_id,))
        elif mutation == "no-file":
            _ = db.execute(
                "UPDATE import_queue_files SET src_path=? WHERE queue_id=?",
                (str(env.source), queue_id),
            )
        elif mutation == "domain":
            binding["domain"] = "publication"
        elif mutation == "token":
            changed["artifact_owner_token"] = binding["operation_key"] = (
                "foreign-generation"
            )
        elif mutation == "binding-key":
            binding["operation_key"] = "foreign-generation"
        elif mutation == "binding-file":
            binding["file_id"] = 1
        elif mutation == "unsafe-token":
            changed["artifact_owner_token"] = binding["operation_key"] = "unsafe/token"
        elif mutation == "marker":
            wire_object(source["marker_fingerprint"])["sha256"] = "0" * 64
        elif mutation in {"original", "suffix", "cleanup-suffix"}:
            source["path"] = carrier["origin_path"] = (
                claim.source_directory.path
                + (
                    {
                        "suffix": ".owner-other",
                        "cleanup-suffix": ".cleanup-other",
                        "original": "/foreign",
                    }[mutation]
                )
            )
        elif mutation == "missing-marker":
            source["marker_fingerprint"] = None
        if not argument_only:
            _ = db.execute(
                "UPDATE import_pack_cleanup_tombstones SET carrier_json=?",
                (encoded(changed),),
            )
    with transferred_claim_conservation(env, monkeypatch, pure=False):
        with pytest.raises((InvalidIdentity, IdentityMismatch)):
            closure = captured_fixture(env)[0]
            if mutation == "owner":
                owner = OwnerRef("queue_source", (cast(int, queue_id),))
            elif mutation in {"extra-entry", "fingerprint"}:
                files = dict(claim.inventory.files)
                if mutation == "extra-entry":
                    files["unrecorded.cbz"] = files["Matrix v02.cbz"]
                else:
                    files["Matrix v02.cbz"] = replace(
                        files["Matrix v02.cbz"], sha256="0" * 64
                    )
                claim = replace(claim, inventory=replace(claim.inventory, files=files))
            elif mutation in {"bool-queue", "bool-file", "null-queue"}:
                from file_identity_store import OwnerSnapshot

                snapshot = next(
                    s
                    for s in closure.snapshots
                    if s.owner.domain
                    == (
                        "pack_tombstone" if mutation == "null-queue" else "queue_source"
                    )
                )
                document = parse_document(snapshot.rows_json)
                if mutation == "null-queue":
                    wire_object(wire_object(document["root"])["row"])["queue_id"] = None
                elif mutation == "bool-queue":
                    wire_object(wire_object(document["root"])["row"])["id"] = True
                else:
                    record = next(
                        wire_object(r)
                        for r in wire_array(document["related"])
                        if wire_object(r)["table"] == "import_queue_files"
                    )
                    wire_object(record["row"])["id"] = True
                text = encoded(document)
                altered = OwnerSnapshot(
                    snapshot.owner, text, hashlib.sha256(text.encode()).hexdigest()
                )
                closure = replace(
                    closure,
                    snapshots=tuple(
                        altered if s == snapshot else s for s in closure.snapshots
                    ),
                )
            elif not argument_only:
                snapshot = next(
                    s for s in closure.snapshots if s.owner.domain == "pack_tombstone"
                )
                owner = snapshot.owner
                row = wire_object(
                    wire_object(parse_document(snapshot.rows_json)["root"])["row"]
                )
                claim = PackDirectoryClaim.from_json(cast(str, row["carrier_json"]))
            with pack_validation_boundaries(monkeypatch):
                _ = consumers._pack_restore_references(closure, owner, claim, None)


@pytest.mark.parametrize(
    "case",
    ["aliases", "incoming", "nested-source", "unknown-descendant", "root-without-file"],
)
def test_transferred_claim_aliases_and_incoming_root_stay_independent(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    env = producer_media
    _, queue_id = producer_pack(env, "attached", tombstone=True, nested=True)
    with closing(sqlite3.connect(env.db_path)) as db, db:
        if case == "aliases":
            columns = [
                cast(str, row[1])
                for row in db.execute("PRAGMA table_info(import_queue_files)")
                if row[1] != "id"
            ]
            names = ",".join(columns)
            _ = db.execute(
                f"INSERT INTO import_queue_files ({names}) SELECT {names} FROM import_queue_files WHERE queue_id=?",
                (queue_id,),
            )
        elif case == "incoming":
            _ = db.execute(
                "UPDATE import_queue SET src_dir=? WHERE id=?",
                (str(env.incoming), queue_id),
            )
        else:
            root = cast(
                str,
                db.execute(
                    "SELECT src_dir FROM import_queue WHERE id=?", (queue_id,)
                ).fetchone()[0],
            )
            _ = db.execute(
                "UPDATE import_queue_files SET src_path=? WHERE queue_id=?",
                (
                    root + "/unknown.cbz"
                    if case == "unknown-descendant"
                    else root + "/nested/deeper/extra.cbz"
                    if case == "nested-source"
                    else str(env.source),
                    queue_id,
                ),
            )
    if case in {"unknown-descendant", "root-without-file"}:
        closure = captured_fixture(env)[0]
        snapshot = next(
            s for s in closure.snapshots if s.owner.domain == "pack_tombstone"
        )
        owner = snapshot.owner
        row = wire_object(
            wire_object(parse_document(snapshot.rows_json)["root"])["row"]
        )
        claim = PackDirectoryClaim.from_json(cast(str, row["carrier_json"]))
    else:
        inventory = witnessed_inventory(env, monkeypatch)
        closure = inventory.closure
        owner, claim, _ = transferred_claim(inventory)
    with transferred_claim_conservation(env, monkeypatch):
        if case in {"unknown-descendant", "root-without-file"}:
            with pytest.raises(InvalidIdentity, match="ownership_context"):
                _ = consumers._pack_restore_references(closure, owner, claim, None)
        else:
            references = consumers._pack_restore_references(closure, owner, claim, None)
            files = [(s, r) for s, r in references if s.role.purpose == "source"]
            roots = [
                (s, r) for s, r in references if s.role.purpose == "source_directory"
            ]
            assert len(files) == (2 if case == "aliases" else 1)
            assert len(roots) == (0 if case == "incoming" else 1)
            assert len({s.role.file_id for s, _ in files}) == len(files)
            if case == "aliases":
                assert files[0][0].path == files[1][0].path
                assert files[0][1]["queue_file_id"] != files[1][1]["queue_file_id"]
            if case == "nested-source":
                assert files[0][1]["relative"] == "nested/deeper/extra.cbz"
                assert files[0][1]["source_fingerprint"] == asdict(
                    claim.inventory.files["nested/deeper/extra.cbz"]
                )
            assert all(s in closure.roles for s, _ in references)


@pytest.mark.parametrize("case", ["frozen", "generation", "invalid", "partial"])
def test_transferred_claim_preserves_stronger_origin_precedence(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    env = producer_media
    _, queue_id = producer_bound_frozen_pack(env, monkeypatch)
    original = witnessed_inventory(env, monkeypatch)
    owner, claim, _ = transferred_claim(original)
    if case != "frozen":
        with closing(sqlite3.connect(env.db_path)) as db, db:
            raw = cast(
                str,
                db.execute(
                    "SELECT queue_snapshot_json FROM import_publications WHERE queue_id=?",
                    (queue_id,),
                ).fetchone()[0],
            )
            frozen = parse_document(raw)
            origins = wire_object(wire_object(frozen["_pack_source_origins"])["files"])
            origin = wire_object(next(iter(origins.values())))
            if case == "generation":
                proof = parse_document(cast(str, origin["ownership_json"]))
                proof["artifact_owner_token"] = origin["artifact_owner_token"] = (
                    "foreign-generation"
                )
                origin["ownership_json"] = encoded(proof)
            elif case == "invalid":
                origin["ownership_json"] = "{}"
            else:
                _ = db.execute(
                    "UPDATE import_queue_files SET src_path=src_path||'-other' WHERE queue_id=?",
                    (queue_id,),
                )
            _ = db.execute(
                "UPDATE import_publications SET queue_snapshot_json=? WHERE queue_id=?",
                (encoded(frozen), queue_id),
            )
    with transferred_claim_conservation(env, monkeypatch, pure=False):
        if case == "frozen":
            with pack_validation_boundaries(monkeypatch):
                references = consumers._pack_restore_references(
                    original.closure, owner, claim, None
                )
            kinds = {r["kind"] for _, r in references}
            assert kinds == {
                "frozen-pack-origin",
                "pack-source-file",
                "pack-source-root",
            }
        else:
            with pytest.raises((InvalidIdentity, IdentityMismatch)):
                closure = captured_fixture(env)[0]
                with pack_validation_boundaries(monkeypatch):
                    _ = consumers._pack_restore_references(closure, owner, claim, None)


@contextmanager
def transferred_plan_conservation(
    env: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    inventory: recovery.OwnershipInventory,
    *,
    pure: bool = True,
) -> Generator[None, None, None]:
    import os
    from file_mutation_lock import FileMutationLockError
    from private_pack_claim import open_directory

    serialized = inventory.to_json()
    with (
        file_mutation_guard(str(env.db_path)) as guard,
        open_directory(str(env.scope)) as fd,
    ):
        handles = (os.fstat(fd), os.fstat(guard.subprocess_fd))
        with pytest.raises(FileMutationLockError):
            with file_mutation_guard(str(env.db_path)):
                pytest.fail("independent guard acquired an already held lock")
        with transferred_claim_conservation(env, monkeypatch, pure=pure):
            yield
        guard.verify()
        assert (os.fstat(fd), os.fstat(guard.subprocess_fd)) == handles
        with pytest.raises(FileMutationLockError):
            with file_mutation_guard(str(env.db_path)):
                pytest.fail("planner released the held mutation lock")
    assert inventory.to_json() == serialized


def transferred_claim_effects(
    inventory: recovery.OwnershipInventory,
    decisions: tuple[recovery.RecoveryDecision, ...],
) -> list[dict[str, object]]:
    owner, claim, raw = transferred_claim(inventory)
    snapshots = {s.owner: s for s in inventory.closure.snapshots}
    decision = next(d for d in decisions if d.owner == owner)
    row = wire_object(
        wire_object(parse_document(snapshots[owner].rows_json)["root"])["row"]
    )
    queue_owner = OwnerRef("queue_source", (cast(int, row["queue_id"]),))
    root = claim.source_directory.path
    effects: list[dict[str, object]] = []
    for observation in inventory.observed:
        spec = observation.spec
        if spec.owner != queue_owner or spec.role.purpose not in {
            "source",
            "source_directory",
        }:
            continue
        if spec.path != root and not spec.path.startswith(root + "/"):
            continue
        relative = spec.path[len(root) + 1 :] if spec.path != root else None
        effects.append(
            {
                "decision_sha256": digest(decision.to_value()),
                "original_role": recovery._role_value(
                    RoleKey(None, "tombstone_captured", "directory", None)
                ),
                "affected_owner": recovery._owner_value(queue_owner),
                "affected_role": recovery._role_value(spec.role),
                "observation_sha256": digest(recovery._observed_value(observation)),
                "relationship": {
                    "kind": "transferred-pack-claim-source-file"
                    if relative is not None
                    else "transferred-pack-claim-source-root",
                    "claim_owner": recovery._owner_value(owner),
                    "claim_role": recovery._role_value(
                        RoleKey(None, "tombstone_captured", "directory", None)
                    ),
                    "claim_snapshot_sha256": snapshots[owner].sha256,
                    "claim_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                    "queue_snapshot_sha256": snapshots[queue_owner].sha256,
                    "queue_id": queue_owner.key[0],
                    "queue_file_id": spec.role.file_id,
                    "identity_key": row["download_identity_key"],
                    "artifact_owner_token": claim.artifact_owner_token,
                    "relative": relative,
                    "source_fingerprint": asdict(claim.inventory.files[relative])
                    if relative is not None
                    else None,
                },
                "effect": "create-name",
                "handling": "retain-reference-non-replayable",
            }
        )
    return sorted(effects, key=encoded)


def transferred_claim_layout(env: ProducerMedia, queue_id: int, layout: str) -> None:
    with closing(sqlite3.connect(env.db_path)) as db, db:
        if layout == "aliases":
            metadata = cast(
                list[tuple[object, ...]],
                db.execute("PRAGMA table_info(import_queue_files)").fetchall(),
            )
            columns = [cast(str, row[1]) for row in metadata if row[1] != "id"]
            names = ",".join(columns)
            _ = db.execute(
                f"INSERT INTO import_queue_files ({names}) SELECT {names} FROM import_queue_files WHERE queue_id=?",
                (queue_id,),
            )
        elif layout == "incoming":
            _ = db.execute(
                "UPDATE import_queue SET src_dir=? WHERE id=?",
                (str(env.incoming), queue_id),
            )
        elif layout == "nested-source":
            _ = db.execute(
                "UPDATE import_queue_files SET src_path=(SELECT src_dir FROM import_queue WHERE id=?)||'/nested/deeper/extra.cbz' WHERE queue_id=?",
                (queue_id, queue_id),
            )


@pytest.mark.parametrize(
    "layout", ["simple", "nested", "aliases", "incoming", "nested-source"]
)
@pytest.mark.parametrize("status", ["failed", "imported", "skipped"])
def test_transferred_claim_restore_requires_explicit_review(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    layout: str,
    status: str,
) -> None:
    env = producer_media
    _, queue_id = producer_pack(
        env, "attached", tombstone=True, nested=layout != "simple"
    )
    assert queue_id is not None
    transferred_claim_layout(env, queue_id, layout)
    with closing(sqlite3.connect(env.db_path)) as db, db:
        _ = db.execute(
            "UPDATE import_queue SET status=? WHERE id=?", (status, queue_id)
        )
    inventory = witnessed_inventory(env, monkeypatch)
    owner, claim, _ = transferred_claim(inventory)
    assert not any(
        r.table in {"import_pack_cleanup_reservations", "import_publications"}
        for r in inventory.closure.raw_rows
    )
    decisions = pack_claim_decisions(inventory, owner, claim)
    effects = transferred_claim_effects(inventory, decisions)
    assert len(effects) == (
        3 if layout == "aliases" else 1 if layout == "incoming" else 2
    )
    with transferred_plan_conservation(env, monkeypatch, inventory):
        inputs = pack_linked_inputs(inventory, decisions, effects)
        plan = recovery.build_recovery_plan(inventory, *inputs)
        assert plan.version == 2
        assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan
        assert recovery.build_recovery_plan(inventory, *inputs) == plan
        restored = [d for d in plan.decisions if d.action == "restore-original"]
        assert len(restored) == 1 and restored[0].owner == owner
        assert restored[0].destination == claim.source_directory.path
        queue = next(
            d
            for d in plan.decisions
            if d.owner == OwnerRef("queue_source", (queue_id,))
        )
        assert queue.action == "retain-blocked" and not queue.roles
        assert set(queue.retained_roles) == {
            r.role for r in inventory.closure.roles if r.owner == queue.owner
        }
        epoch = parse_document(plan.epoch_bindings_json)
        assert (
            epoch["rollback_outputs"] == [] and epoch["allocation_capabilities"] == []
        )
        assert epoch["linked_effects"] == effects
        declaration, receipt = parse_document(inputs[1]), parse_document(inputs[2])
        assert len(wire_array(declaration["role_reviews"])) == len(inventory.observed)
        assert len(wire_array(receipt["media"])) == len(inventory.observed)
        assert len(wire_array(receipt["owner_snapshots"])) == len(
            inventory.closure.snapshots
        )
        assert {
            encoded(wire_object(v)["owner"]): wire_object(v)["sha256"]
            for v in wire_array(receipt["owner_snapshots"])
        } == {
            encoded(recovery._owner_value(s.owner)): s.sha256
            for s in inventory.closure.snapshots
        }
        for effect in effects:
            role = recovery._role_from(effect["affected_role"])
            assert role in queue.retained_roles
            review = next(
                wire_object(v)
                for v in wire_array(declaration["role_reviews"])
                if wire_object(v)["owner"] == effect["affected_owner"]
                and wire_object(v)["role"] == effect["affected_role"]
            )
            assert review["assertion"] == "reviewed-linked-reference"
            assert review["authority_id"] == "a" * 32
            assert review["observation_sha256"] == effect["observation_sha256"]
            assert {
                "effect_sha256": digest(effect),
                "authority_id": "a" * 32,
            } in wire_array(declaration["linked_reviews"])
            binding = next(
                wire_object(v)
                for v in wire_array(epoch["role_bindings"])
                if wire_object(v)["owner"] == effect["affected_owner"]
                and wire_object(v)["role"] == effect["affected_role"]
            )
            assert binding["authority_id"] == review["authority_id"]
            media = next(
                wire_object(v)
                for v in wire_array(receipt["media"])
                if wire_object(v)["owner"] == effect["affected_owner"]
                and wire_object(v)["role"] == effect["affected_role"]
            )
            observation = next(
                o
                for o in inventory.observed
                if o.spec.owner == queue.owner and o.spec.role == role
            )
            assert media["observation_sha256"] == effect["observation_sha256"]
            assert media["handling"] == (
                "observed-absent"
                if observation.outcome == "absent"
                else "observed-unreachable"
            )
            assert media["recovery_reference"] is None
        if layout == "aliases":
            files = [
                wire_object(e["relationship"])
                for e in effects
                if wire_object(e["relationship"])["relative"] is not None
            ]
            assert len({f["queue_file_id"] for f in files}) == 2
            assert files[0]["source_fingerprint"] == files[1]["source_fingerprint"]
        if layout == "incoming":
            assert all(
                wire_object(e["relationship"])["kind"]
                == "transferred-pack-claim-source-file"
                for e in effects
            )
        assert_linked_refusal(
            inventory, review_documents(inventory, decisions), "ownership_wire"
        )
        assert_linked_refusal(
            inventory, pack_linked_inputs(inventory, decisions, []), "ownership_phase"
        )
        retained = recovery.build_recovery_plan(
            inventory,
            *pack_linked_inputs(
                inventory,
                tuple(
                    replace(
                        d,
                        action="retain-blocked",
                        roles=(),
                        destination=None,
                        retained_roles=tuple(
                            r.role
                            for r in inventory.closure.roles
                            if r.owner == d.owner
                        ),
                    )
                    for d in decisions
                ),
                [],
            ),
        )
        assert all(d.action == "retain-blocked" for d in retained.decisions)


@pytest.mark.parametrize(
    "change,code",
    [
        pytest.param(change, code, id=change)
        for change, code in (
            ("missing", "ownership_binding"),
            ("root-effect", "ownership_binding"),
            ("extra", "ownership_binding"),
            ("competing", "ownership_binding"),
            ("reordered", "ownership_wire"),
            ("bool-queue", "ownership_binding"),
            ("bool-file", "ownership_binding"),
            ("null-queue", "ownership_binding"),
            ("null-file", "ownership_binding"),
            ("root-file", "ownership_binding"),
            ("root-relative", "ownership_binding"),
            ("root-fingerprint", "ownership_binding"),
            ("file-relative", "ownership_binding"),
            ("file-fingerprint", "ownership_binding"),
            ("fingerprint-bool", "ownership_binding"),
            ("fingerprint-extra", "ownership_binding"),
            ("kind", "ownership_binding"),
            ("unknown-key", "ownership_binding"),
            ("missing-key", "ownership_binding"),
            ("claim-owner", "ownership_binding"),
            ("claim-role", "ownership_binding"),
            ("claim-sha", "ownership_binding"),
            ("claim-snapshot", "ownership_binding"),
            ("queue-snapshot", "ownership_binding"),
            ("observation", "ownership_binding"),
            ("identity", "ownership_binding"),
            ("token", "ownership_binding"),
            ("queue-id", "ownership_binding"),
            ("file-id", "ownership_binding"),
            ("decision", "ownership_binding"),
            ("original", "ownership_binding"),
            ("affected-owner", "ownership_binding"),
            ("affected-role", "ownership_binding"),
            ("effect", "ownership_binding"),
            ("handling", "ownership_binding"),
            ("linked-review", "ownership_binding"),
            ("root-linked-review", "ownership_binding"),
            ("role-review", "ownership_binding"),
            ("root-review", "ownership_binding"),
            ("alias-review", "ownership_binding"),
            ("owner-review", "ownership_binding"),
            ("owner-backup", "ownership_binding"),
            ("role-authority", "ownership_authority"),
            ("linked-authority", "ownership_binding"),
            ("binding-authority", "ownership_binding"),
            ("authority-scope", "ownership_authority"),
            ("media-absent", "ownership_binding"),
            ("media-unreachable", "ownership_binding"),
            ("media-captured", "ownership_binding"),
            ("media-support", "ownership_binding"),
            ("media-omit", "ownership_binding"),
            ("raw-equivalent", "ownership_binding"),
        )
    ],
)
def test_transferred_claim_resigned_effects_and_reviews_refuse(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    code: str,
) -> None:
    env = producer_media
    _, queue_id = producer_pack(env, "attached", tombstone=True, nested=True)
    assert queue_id is not None
    transferred_claim_layout(env, queue_id, "aliases")
    inventory = witnessed_inventory(env, monkeypatch)
    owner, claim, raw = transferred_claim(inventory)
    decisions = pack_claim_decisions(inventory, owner, claim)
    effects = transferred_claim_effects(inventory, decisions)
    with transferred_plan_conservation(env, monkeypatch, inventory):
        base = recovery.build_recovery_plan(
            inventory, *pack_linked_inputs(inventory, decisions, effects)
        )
        assert recovery.RecoveryPlan.from_json(base.to_json()) == base
        assert replace(base) == base
    if change == "raw-equivalent":
        with closing(sqlite3.connect(env.db_path)) as db, db:
            _ = db.execute(
                "UPDATE import_pack_cleanup_tombstones SET carrier_json=?",
                (json.dumps(parse_document(raw), indent=2),),
            )
        inventory = witnessed_inventory(env, monkeypatch)
        new_owner, new_claim, new_raw = transferred_claim(inventory)
        assert new_owner == owner and new_claim == claim and new_raw != raw
        decisions = pack_claim_decisions(inventory, owner, new_claim)
        fresh = transferred_claim_effects(inventory, decisions)
        with transferred_plan_conservation(env, monkeypatch, inventory):
            base = recovery.build_recovery_plan(
                inventory, *pack_linked_inputs(inventory, decisions, fresh)
            )
            assert recovery.RecoveryPlan.from_json(base.to_json()) == base
            assert replace(base) == base
        assert all(
            wire_object(e["relationship"])["claim_sha256"]
            != wire_object(f["relationship"])["claim_sha256"]
            for e, f in zip(effects, fresh, strict=True)
        )
        # Fresh snapshots/reviews with only stale opaque-claim consent, not an outer hash failure.
        old_sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        effects = fresh
        for effect in effects:
            wire_object(effect["relationship"])["claim_sha256"] = old_sha
    selection, declaration, receipt = (
        parse_document(d) for d in pack_linked_inputs(inventory, decisions, effects)
    )
    supplied = wire_array(wire_object(selection["epoch_bindings"])["linked_effects"])
    file = next(
        wire_object(v)
        for v in supplied
        if wire_object(wire_object(v)["relationship"])["kind"]
        == "transferred-pack-claim-source-file"
    )
    root = next(
        wire_object(v)
        for v in supplied
        if wire_object(wire_object(v)["relationship"])["kind"]
        == "transferred-pack-claim-source-root"
    )
    relationship = wire_object(file["relationship"])
    if change in {"missing", "root-effect"}:
        supplied.remove(root if change == "root-effect" else file)
    elif change in {"extra", "competing"}:
        added = dict(file)
        added["decision_sha256" if change == "extra" else "effect"] = (
            "0" * 64 if change == "extra" else "replace-acknowledged-name"
        )
        supplied.append(added)
    elif change == "reordered":
        supplied.reverse()
    elif change in {
        "bool-queue",
        "bool-file",
        "null-queue",
        "null-file",
        "queue-id",
        "file-id",
    }:
        key = (
            "queue_id"
            if change.endswith("queue") or change == "queue-id"
            else "queue_file_id"
        )
        relationship[key] = (
            True
            if change.startswith("bool")
            else None
            if change.startswith("null")
            else 99999
        )
    elif change in {"root-file", "root-relative", "root-fingerprint"}:
        wire_object(root["relationship"])[
            {
                "root-file": "queue_file_id",
                "root-relative": "relative",
                "root-fingerprint": "source_fingerprint",
            }[change]
        ] = relationship[
            {
                "root-file": "queue_file_id",
                "root-relative": "relative",
                "root-fingerprint": "source_fingerprint",
            }[change]
        ]
    elif change in {"file-relative", "file-fingerprint"}:
        relationship[
            "relative" if change == "file-relative" else "source_fingerprint"
        ] = None
    elif change == "fingerprint-bool":
        wire_object(relationship["source_fingerprint"])["size"] = True
    elif change == "fingerprint-extra":
        wire_object(relationship["source_fingerprint"])["extra"] = None
    elif change == "kind":
        relationship["kind"] = "pack-source-file"
    elif change == "unknown-key":
        relationship["extra"] = None
    elif change == "missing-key":
        del relationship["claim_sha256"]
    elif change == "claim-owner":
        wire_object(relationship["claim_owner"])["domain"] = "pack_reservation"
    elif change == "claim-role":
        wire_object(relationship["claim_role"])["purpose"] = "tombstone_source"
    elif change in {"claim-sha", "claim-snapshot", "queue-snapshot"}:
        relationship[
            {
                "claim-sha": "claim_sha256",
                "claim-snapshot": "claim_snapshot_sha256",
                "queue-snapshot": "queue_snapshot_sha256",
            }[change]
        ] = "0" * 64
    elif change in {"identity", "token"}:
        relationship[
            "identity_key" if change == "identity" else "artifact_owner_token"
        ] = "foreign"
    elif change in {"decision", "observation"}:
        file["decision_sha256" if change == "decision" else "observation_sha256"] = (
            "0" * 64
        )
    elif change in {"original", "affected-role"}:
        wire_object(file["original_role" if change == "original" else "affected_role"])[
            "purpose"
        ] = "foreign"
    elif change == "affected-owner":
        wire_object(file["affected_owner"])["key"] = [99999]
    elif change in {"effect", "handling"}:
        file[change] = (
            "replace-acknowledged-name" if change == "effect" else "retain-in-place"
        )
    elif change in {"linked-review", "root-linked-review"}:
        target_effect = root if change == "root-linked-review" else file
        declaration["linked_reviews"] = [
            v
            for v in wire_array(declaration["linked_reviews"])
            if wire_object(v)["effect_sha256"] != digest(target_effect)
        ]
    elif change in {"role-review", "root-review", "alias-review", "role-authority"}:
        target = (
            root
            if change == "root-review"
            else file
            if change != "alias-review"
            else next(
                wire_object(v)
                for v in supplied
                if v != file
                and wire_object(wire_object(v)["relationship"])["kind"]
                == "transferred-pack-claim-source-file"
            )
        )
        reviews = wire_array(declaration["role_reviews"])
        review = next(
            wire_object(v)
            for v in reviews
            if wire_object(v)["owner"] == target["affected_owner"]
            and wire_object(v)["role"] == target["affected_role"]
        )
        if change == "role-authority":
            review["authority_id"] = None
        else:
            reviews.remove(review)
    elif change in {"owner-review", "owner-backup"}:
        document, key = (
            (declaration, "owner_reviews")
            if change == "owner-review"
            else (receipt, "owner_snapshots")
        )
        document[key] = [
            v
            for v in wire_array(document[key])
            if wire_object(v)["owner"] != file["affected_owner"]
        ]
    elif change == "linked-authority":
        wire_object(wire_array(declaration["linked_reviews"])[0])["authority_id"] = (
            "b" * 32
        )
    elif change == "binding-authority":
        binding = next(
            wire_object(v)
            for v in wire_array(
                wire_object(selection["epoch_bindings"])["role_bindings"]
            )
            if wire_object(v)["owner"] == file["affected_owner"]
            and wire_object(v)["role"] == file["affected_role"]
        )
        binding["authority_id"] = None
    elif change == "authority-scope":
        wire_object(wire_object(wire_array(declaration["authorities"])[0])["scope"])[
            "scope_path"
        ] = str(env.library)
    elif change.startswith("media-"):
        media = wire_array(receipt["media"])
        if change in {"media-captured", "media-support"}:
            target_media = next(
                wire_object(v)
                for v in media
                if wire_object(v)["owner"] == recovery._owner_value(owner)
                and wire_object(wire_object(v)["role"])["purpose"]
                == (
                    "tombstone_captured_file"
                    if change == "media-captured"
                    else "tombstone_carrier"
                )
            )
        else:
            target_effect = root if change == "media-absent" else file
            target_media = next(
                wire_object(v)
                for v in media
                if wire_object(v)["owner"] == target_effect["affected_owner"]
                and wire_object(v)["role"] == target_effect["affected_role"]
            )
        if change in {"media-omit", "media-support"}:
            media.remove(target_media)
        elif change in {"media-absent", "media-unreachable"}:
            target_media.update(
                handling="snapshot", recovery_reference="nonexistent bytes"
            )
        else:
            target_media.update(handling="retain-in-place", recovery_reference=None)
    if change != "reordered":
        supplied.sort(key=encoded)
    # Re-sign consent as well as enclosing documents so semantic equality is exercised.
    if change not in {"linked-review", "root-linked-review", "linked-authority"}:
        declaration["linked_reviews"] = sorted(
            [{"effect_sha256": digest(v), "authority_id": "a" * 32} for v in supplied],
            key=lambda v: v["effect_sha256"],
        )
    with transferred_plan_conservation(env, monkeypatch, inventory):
        inputs = resign_documents(selection, declaration, receipt)
        assert inventory.unresolved == ()
        assert tuple(o.spec for o in inventory.observed) == inventory.closure.roles
        assert selection["decisions"] == [d.to_value() for d in decisions]
        assert wire_object(selection["epoch_bindings"])["version"] == 2
        for document in (selection, declaration, receipt):
            assert document["version"] == 2
            assert document["inventory_sha256"] == inventory.sha256
        assert declaration["selection_sha256"] == digest(selection)
        assert receipt["selection_sha256"] == digest(selection)
        assert receipt["declaration_sha256"] == digest(declaration)
        assert_linked_refusal(
            inventory,
            inputs,
            code,
        )


@pytest.mark.parametrize(
    "change,expected_unresolved,code",
    [
        pytest.param(change, unresolved, code, id=change)
        for change, unresolved, code in (
            ("pending", (), "ownership_context"),
            ("partial", (), "ownership_context"),
            ("importing", (), "ownership_context"),
            ("lease", (), "ownership_context"),
            ("publication", (), "active origin lacks its current reservation"),
            ("sibling", (), "ownership_context"),
            ("retained-skip", (), "ownership_context"),
            ("missing-file", (), "ownership_phase"),
            ("missing-directory", ("role_unreadable",), "ownership_incomplete"),
            ("unknown-descendant", ("role_unknown_children",), "ownership_incomplete"),
            ("missing-marker", (), "ownership_phase"),
            ("missing-support", (), "ownership_phase"),
            ("partial-capture", (), "ownership_phase"),
            ("discarded-capture", (), "ownership_phase"),
            ("present-root", (), "ownership_phase"),
            ("present-source", (), "ownership_phase"),
            ("independent-destination", (), "ownership_phase"),
            ("unknown-source", (), "ownership_context"),
            ("symlink", ("role_unsafe_path",), "ownership_incomplete"),
        )
    ],
)
def test_transferred_claim_terminal_and_tree_vetoes_remain(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    expected_unresolved: tuple[str, ...],
    code: str,
) -> None:
    env = producer_media
    sibling = producer_queue(env) if change == "sibling" else None
    if change in {"partial-capture", "discarded-capture"}:
        from test_file_identity_observation_followup import pack_location_checkpoint

        pack_location_checkpoint(
            env,
            monkeypatch,
            "partial-discard" if change == "partial-capture" else "discarded",
        )
        with closing(sqlite3.connect(env.db_path)) as db:
            queue_id = cast(
                int,
                db.execute(
                    "SELECT queue_id FROM import_pack_cleanup_tombstones"
                ).fetchone()[0],
            )
    else:
        _, queue_id = producer_pack(
            env, "attached", tombstone=True, nested=True, frozen=change == "publication"
        )
    assert queue_id is not None
    baseline = witnessed_inventory(env, monkeypatch)
    _, claim, _ = transferred_claim(baseline)
    artifact = Path(claim.carrier.carrier_path) / "artifact"
    if change in {
        "pending",
        "partial",
        "importing",
        "lease",
        "retained-skip",
        "sibling",
        "unknown-source",
    }:
        with closing(sqlite3.connect(env.db_path)) as db, db:
            if change in {"pending", "partial", "importing"}:
                _ = db.execute(
                    "UPDATE import_queue SET status=? WHERE id=?", (change, queue_id)
                )
            elif change == "lease":
                _ = db.execute(
                    "UPDATE import_queue SET lease_owner='foreign-lease' WHERE id=?",
                    (queue_id,),
                )
            elif change == "retained-skip":
                _ = db.execute(
                    "UPDATE import_queue_files SET status='skipped' WHERE queue_id=?",
                    (queue_id,),
                )
            elif change == "unknown-source":
                _ = db.execute(
                    "UPDATE import_queue_files SET src_path=? WHERE queue_id=?",
                    (str(env.source), queue_id),
                )
            else:
                assert sibling is not None
                _ = db.execute(
                    "UPDATE import_queue SET download_id='matrix-generated',torrent_url='magnet:matrix-generated' WHERE id=?",
                    (sibling,),
                )
    elif change == "publication":
        with closing(sqlite3.connect(env.db_path)) as db, db:
            # A producer publication necessarily retains its stronger frozen proof.
            _ = db.execute(
                "UPDATE import_publications SET state='cleaning' WHERE queue_id=?",
                (queue_id,),
            )
            _ = db.execute(
                "UPDATE import_queue SET status='failed',lease_owner=NULL,lease_expires_at=NULL WHERE id=?",
                (queue_id,),
            )
    elif change in {"partial-capture", "discarded-capture"}:
        assert claim.carrier.phase == (
            "discarding" if change == "partial-capture" else "discarded"
        )
    elif change == "missing-file":
        (artifact / "Matrix v02.cbz").unlink()
    elif change == "missing-directory":
        (artifact / "nested/deeper/extra.cbz").unlink()
        (artifact / "nested/deeper").rmdir()
    elif change == "unknown-descendant":
        write_archive(artifact / "unknown.cbz", b"unrecorded bytes")
    elif change == "symlink":
        (artifact / "Matrix v02.cbz").unlink()
        (artifact / "Matrix v02.cbz").symlink_to(env.source)
    elif change in {"missing-marker", "missing-support"}:
        target = (
            artifact / "mangarr-pack-owner"
            if change == "missing-marker"
            else Path(claim.carrier.carrier_path) / "owner.json"
        )
        target.unlink()
    elif change in {"present-root", "present-source"}:
        Path(claim.source_directory.path).mkdir()
        if change == "present-source":
            write_archive(
                Path(claim.source_directory.path) / "Matrix v02.cbz", b"foreign source"
            )
    elif change == "independent-destination":
        # A retained queue destination may independently reference the restored tree.
        with closing(sqlite3.connect(env.db_path)) as db, db:
            _ = db.execute(
                "UPDATE import_queue_files SET dst_path=? WHERE queue_id=?",
                (claim.source_directory.path, queue_id),
            )

    if change == "publication":
        # Active frozen origins already fail closed at SQL closure acquisition.
        with transferred_plan_conservation(env, monkeypatch, baseline, pure=False):
            with pytest.raises(IdentityMismatch, match="^" + code + "$"):
                _ = captured_fixture(env)
        return

    # Capture real changed rows/media before invoking the pure validation boundary.
    closure, _ = captured_fixture(env)
    with file_mutation_guard(str(env.db_path)) as guard:
        inventory = recovery._observe_closure(
            guard, str(env.scope), closure, InventoryBudget(), inventory_version=2
        )
    assert inventory.unresolved == expected_unresolved
    assert tuple(o.spec for o in inventory.observed) == closure.roles
    owner, current_claim, _ = transferred_claim(inventory)
    expected_records: dict[RoleKey, tuple[str, str]] = {}
    if change == "missing-directory":
        expected_records[
            RoleKey(None, "tombstone_captured_file", "file", "nested/deeper/extra.cbz")
        ] = ("unreadable", str(artifact / "nested/deeper/extra.cbz"))
    elif change == "unknown-descendant":
        expected_records = {
            RoleKey(None, purpose, "directory", relative): (
                "unknown_children",
                str(artifact),
            )
            for purpose, relative in (
                ("tombstone_artifact", "artifact"),
                ("tombstone_captured", None),
                ("tombstone_path", None),
            )
        }
    elif change == "symlink":
        expected_records[
            RoleKey(None, "tombstone_captured_file", "file", "Matrix v02.cbz")
        ] = ("unsafe_path", str(artifact / "Matrix v02.cbz"))
    unresolved_records = {
        (o.spec.owner, o.spec.role): o for o in inventory.observed if o.unresolved
    }
    assert set(unresolved_records) == {(owner, role) for role in expected_records}
    for role, (outcome, path) in expected_records.items():
        record = unresolved_records[(owner, role)]
        assert record.spec.path == path and record.outcome == outcome
        fields = parse_document(record.constraints_json)
        assert fields["version"] == 1 and fields["kind"] == "readonly_observation"
        assert fields["content"] is None
        if outcome == "unknown_children":
            assert fields["boundary"] == "verified" and fields["policy"] == "satisfied"
            assert fields["current"] is not None and record.native is not None
            assert fields["children"] == [
                {"path": str(artifact / "unknown.cbz"), "outcome": "unknown_children"}
            ]
        else:
            assert fields["boundary"] == "unknown" and fields["policy"] == "not_checked"
            assert fields["expected"] == "unverified_legacy"
            assert fields["current"] is None and record.native is None
            assert fields["children"] == []
    decisions = pack_claim_decisions(inventory, owner, current_claim)
    # Unclaimed queue sources have no inventory fingerprint and cannot receive consent.
    effects = (
        []
        if change == "unknown-source"
        else transferred_claim_effects(inventory, decisions)
    )
    if change in {"unknown-source", "independent-destination"}:
        reference = next(
            o
            for o in inventory.observed
            if o.spec.owner == OwnerRef("queue_source", (queue_id,))
            and o.spec.role.purpose
            == ("source" if change == "unknown-source" else "destination")
        )
        assert reference.spec.path == (
            str(env.source)
            if change == "unknown-source"
            else claim.source_directory.path
        )
        assert reference.outcome == (
            "observed" if change == "unknown-source" else "absent"
        )
        assert all(
            e["affected_role"] != recovery._role_value(reference.spec.role)
            for e in effects
        )
    with transferred_plan_conservation(env, monkeypatch, inventory):
        assert_linked_refusal(
            inventory,
            pack_linked_inputs(inventory, decisions, effects),
            code,
        )


@pytest.mark.parametrize("nested", [False, True])
def test_transferred_claim_all_constructors_revalidate(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, nested: bool
) -> None:
    env = producer_media
    _ = producer_pack(env, "attached", tombstone=True, nested=nested)
    inventory = witnessed_inventory(env, monkeypatch)
    owner, claim, _ = transferred_claim(inventory)
    decisions = pack_claim_decisions(inventory, owner, claim)
    effects = transferred_claim_effects(inventory, decisions)
    inputs = pack_linked_inputs(inventory, decisions, effects)
    with transferred_plan_conservation(env, monkeypatch, inventory):
        plan = recovery.build_recovery_plan(inventory, *inputs)
        assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan
        assert replace(plan) == plan
        assert (
            tuple(recovery.RecoveryDecision.from_value(d.to_value()) for d in decisions)
            == decisions
        )
        for document in ("selection", "declaration", "receipt", "epoch", "all-v1"):
            selection, declaration, receipt = (parse_document(d) for d in inputs)
            if document == "all-v1":
                selection["version"] = declaration["version"] = receipt["version"] = 1
                wire_object(selection["epoch_bindings"])["version"] = 1
            else:
                target = {
                    "selection": selection,
                    "declaration": declaration,
                    "receipt": receipt,
                    "epoch": wire_object(selection["epoch_bindings"]),
                }[document]
                target["version"] = 1
            assert_linked_refusal(
                inventory,
                resign_documents(selection, declaration, receipt),
                "ownership_wire",
            )
        for version in (True, None, 0, 3):
            wire = parse_document(plan.to_json())
            wire["version"] = version
            _ = wire.pop("sha256")
            wire["sha256"] = digest(wire)
            with pytest.raises(InvalidIdentity, match="^invalid observation integer$"):
                _ = recovery.RecoveryPlan.from_json(encoded(wire))
            with pytest.raises(InvalidIdentity, match="^invalid observation integer$"):
                _ = replace(plan, version=cast(int, version), sha256=wire["sha256"])
        wire = parse_document(plan.to_json())
        wire["extra"] = None
        _ = wire.pop("sha256")
        wire["sha256"] = digest(wire)
        with pytest.raises(
            InvalidIdentity, match="^invalid observation object fields$"
        ):
            _ = recovery.RecoveryPlan.from_json(encoded(wire))
        bad_decision = decisions[0].to_value()
        bad_decision["extra"] = None
        with pytest.raises(
            InvalidIdentity, match="^invalid observation object fields$"
        ):
            _ = recovery.RecoveryDecision.from_value(bad_decision)
        for value in ([0] * 10001, '\\"' * (8 * 1024**2)):
            selection, declaration, receipt = (parse_document(d) for d in inputs)
            supplied = wire_array(
                wire_object(selection["epoch_bindings"])["linked_effects"]
            )
            wire_object(wire_object(supplied[0])["relationship"])["extra"] = value
            bad_inputs = resign_documents(selection, declaration, receipt)
            if type(value) is list:
                assert_linked_refusal(inventory, bad_inputs, "ownership_wire")
            else:
                fields = plan._value(digest=False)
                fields.update(
                    declaration_json=bad_inputs[1],
                    backup_receipt_json=bad_inputs[2],
                    epoch_bindings_json=encoded(selection["epoch_bindings"]),
                )
                sha = digest(fields)
                fields["sha256"] = sha
                with pytest.raises(InvalidIdentity, match="^ownership_budget$"):
                    _ = recovery.build_recovery_plan(inventory, *bad_inputs)
                with pytest.raises(
                    InvalidIdentity, match="^observation document exceeds byte bound$"
                ):
                    _ = recovery.RecoveryPlan.from_json(encoded(fields))
                with pytest.raises(InvalidIdentity, match="^ownership_budget$"):
                    _ = replace(
                        plan,
                        declaration_json=bad_inputs[1],
                        backup_receipt_json=bad_inputs[2],
                        epoch_bindings_json=encoded(selection["epoch_bindings"]),
                        sha256=sha,
                    )
        deep: object = 0
        for _ in range(33):
            deep = [deep]
        selection, declaration, receipt = (parse_document(d) for d in inputs)
        supplied = wire_array(
            wire_object(selection["epoch_bindings"])["linked_effects"]
        )
        wire_object(wire_object(supplied[0])["relationship"])["extra"] = deep
        assert_linked_refusal(
            inventory,
            resign_documents(selection, declaration, receipt),
            "ownership_(wire|budget)",
        )


def capture_inventory(
    env: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> recovery.OwnershipInventory:
    closure, _ = captured_fixture(env)
    observed = observe_fixture(recovery, env, closure, monkeypatch)
    assert all(not r.unresolved for r in observed)
    return recovery.OwnershipInventory.create(
        str(env.scope), closure, observed, (), InventoryBudget()
    )


def review_documents(
    inventory: recovery.OwnershipInventory,
    decisions: tuple[recovery.RecoveryDecision, ...] | None = None,
) -> tuple[str, str, str]:
    selection, declaration, receipt = (parse_document(d) for d in documents(inventory))
    if decisions is None:
        decisions = tuple(
            recovery.RecoveryDecision(
                s.owner,
                "retain-blocked",
                (),
                None,
                tuple(r.role for r in inventory.closure.roles if r.owner == s.owner),
            )
            for s in inventory.closure.snapshots
        )
    selection["decisions"] = [d.to_value() for d in decisions]
    selected = {(d.owner, r) for d in decisions for r in d.roles}
    client, server = _candidate_contracts()
    authority = {
        "authority_id": "a" * 32,
        "incarnation_token": "b" * 32,
        "scope": {
            "version": 1,
            "scope_path": inventory.parent,
            "scope_token": "c" * 32,
        },
        "profile": {
            "version": 1,
            "profile_id": "unit-only",
            "revision": 1,
            "qualification_sha256": "d" * 64,
            "client_contract": client,
            "server_contract": server,
        },
        "original_storage_statement": "Disposable original unit storage",
        "administration_statement": "No storage administration in fixture",
    }
    declaration["authorities"] = [authority] if selected else []
    for snapshot in inventory.closure.snapshots:
        wire_array(declaration["owner_reviews"]).append(
            {
                "owner": recovery._owner_value(snapshot.owner),
                "snapshot_sha256": snapshot.sha256,
                "intended_original_authority_id": "a" * 32
                if any(o == snapshot.owner for o, _ in selected)
                else None,
            }
        )
        wire_array(receipt["owner_snapshots"]).append(
            {"owner": recovery._owner_value(snapshot.owner), "sha256": snapshot.sha256}
        )
    namespace_paths = {
        s.owner.key[0]: s.owner
        for s in inventory.closure.snapshots
        if s.owner.domain == "namespace"
    }
    for record in inventory.observed:
        spec = record.spec
        is_subject = (spec.owner, spec.role) in selected
        base = {
            "owner": recovery._owner_value(spec.owner),
            "role": recovery._role_value(spec.role),
        }
        wire_array(declaration["role_reviews"]).append(
            {
                **base,
                "observation_sha256": digest(recovery._observed_value(record)),
                "authority_id": "a" * 32 if is_subject else None,
                "assertion": "reviewed-recovery-subject"
                if is_subject
                else "retain-only",
            }
        )
        namespace = next(
            (
                owner
                for parent, owner in namespace_paths.items()
                if spec.path == str(parent) + "/.mangarr-claims"
                or spec.path.startswith(str(parent) + "/.mangarr-claims/")
            ),
            None,
        )
        wire_array(wire_object(selection["epoch_bindings"])["role_bindings"]).append(
            {
                **base,
                "namespace_owner": recovery._owner_value(namespace)
                if namespace
                else None,
                "epoch_id": None,
                "authority_id": "a" * 32 if is_subject else None,
            }
        )
        wire_array(receipt["media"]).append(
            {
                **base,
                "observation_sha256": digest(recovery._observed_value(record)),
                "handling": "observed-absent"
                if record.outcome == "absent"
                else "snapshot"
                if is_subject
                else "retain-in-place",
                "recovery_reference": "external-unit-snapshot"
                if is_subject and record.outcome != "absent"
                else None,
            }
        )
    declaration["selection_sha256"] = receipt["selection_sha256"] = digest(selection)
    receipt["declaration_sha256"] = digest(declaration)
    return encoded(selection), encoded(declaration), encoded(receipt)


@pytest.mark.parametrize(
    "domain,phase",
    [
        ("rescan", "prepared"),
        ("rescan", "published"),
        ("deletion", "claimed"),
        ("pack", "queued"),
    ],
)
def test_retain_complete_real_producers_and_every_role_review(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    domain: str,
    phase: str,
) -> None:
    if domain == "rescan":
        _ = producer_rescan(producer_media, phase)
    elif domain == "deletion":
        _ = producer_deletion(producer_media, phase)
    else:
        _ = producer_pack(producer_media, phase)
    inventory = capture_inventory(producer_media, monkeypatch)
    inputs = review_documents(inventory)
    before = (
        media_snapshot(producer_media.scope),
        control_snapshot(producer_media.db_path),
    )
    plan = recovery.build_recovery_plan(inventory, *inputs)
    assert plan.inventory == inventory
    assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan
    assert (
        media_snapshot(producer_media.scope),
        control_snapshot(producer_media.db_path),
    ) == before


def test_missing_api_control() -> None:
    for name in ("RecoveryDecision", "RecoveryPlan", "build_recovery_plan"):
        assert hasattr(recovery, name), name


@pytest.mark.parametrize(
    "changes",
    [
        {"action": "apply"},
        {"owner": OwnerRef("rescan", ("1",))},
        {"roles": []},
        {"roles": (RoleKey(1, "source", "file", None),) * 2},
        {"destination": "/media/../file"},
        {"action": "retain-blocked", "roles": (RoleKey(1, "source", "file", None),)},
        {"action": "keep-current", "destination": None},
    ],
)
def test_decision_constructor_rejects_invalid_fields(
    changes: dict[str, object],
) -> None:
    fields: dict[str, object] = dict(
        owner=OwnerRef("rescan", (1,)),
        action="cancel-unmutated",
        roles=(RoleKey(1, "source", "file", None),),
        destination=None,
        retained_roles=(),
    )
    fields.update(changes)
    with pytest.raises(InvalidIdentity):
        # Deliberately cross the typed interface with invalid runtime values.
        _ = cast(Callable[..., recovery.RecoveryDecision], recovery.RecoveryDecision)(
            **fields
        )


def test_decision_is_frozen_slotted_and_strict_roundtrip() -> None:
    decision = recovery.RecoveryDecision(
        OwnerRef("rescan", (1,)),
        "cancel-unmutated",
        (RoleKey(1, "source", "file", None),),
        None,
        (),
    )
    assert not hasattr(decision, "__dict__")
    with pytest.raises(FrozenInstanceError):
        setattr(decision, "action", "retain-blocked")
    assert recovery.RecoveryDecision.from_json(decision.to_json()) == decision


def test_plan_direct_constructor_rejects_unbound_authority() -> None:
    with pytest.raises(InvalidIdentity):
        _ = recovery.RecoveryPlan(1, empty_inventory(), (), "{}", "{}", "{}", "a" * 64)


def test_empty_plan_strict_roundtrip_and_exact_digest() -> None:
    inventory = empty_inventory()
    plan = recovery.build_recovery_plan(inventory, *documents(inventory))
    value = parse_document(plan.to_json())
    assert value.pop("sha256") == digest(value) == plan.sha256
    assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan
    assert (
        recovery.build_recovery_plan(inventory, *documents(inventory)).to_json()
        == plan.to_json()
    )


def chosen(
    inventory: recovery.OwnershipInventory,
    owner: OwnerRef,
    action: str,
    purposes: set[str],
    destination: str | None = None,
) -> tuple[recovery.RecoveryDecision, ...]:
    return tuple(
        recovery.RecoveryDecision(
            s.owner,
            action if s.owner == owner else "retain-blocked",
            tuple(
                r.role
                for r in inventory.closure.roles
                if r.owner == s.owner
                and s.owner == owner
                and r.role.purpose in purposes
            ),
            destination if s.owner == owner else None,
            tuple(
                r.role
                for r in inventory.closure.roles
                if r.owner == s.owner
                and not (s.owner == owner and r.role.purpose in purposes)
            ),
        )
        for s in inventory.closure.snapshots
    )


@pytest.mark.parametrize("domain", ["rescan", "deletion"])
def test_cancel_preallocation_with_complete_namespace_preserves_rows(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    domain: str,
) -> None:
    import private_file_claim as claims

    identifier = (
        producer_rescan(producer_media, "prepared")
        if domain == "rescan"
        else producer_deletion(producer_media, "active")
    )
    with file_mutation_guard(str(producer_media.db_path)) as guard:
        with claims.ensure_namespace(guard, str(producer_media.library)):
            pass
    inventory = capture_inventory(producer_media, monkeypatch)
    decision = chosen(
        inventory,
        OwnerRef(domain, (identifier,)),
        "cancel-unmutated",
        {"source" if domain == "rescan" else "target"},
    )
    plan = recovery.build_recovery_plan(
        inventory, *review_documents(inventory, decision)
    )
    assert any(d.action == "cancel-unmutated" for d in plan.decisions)
    assert plan.inventory.to_json() == inventory.to_json()


@pytest.mark.parametrize("domain", ["rescan", "deletion"])
def test_restore_captured_original_requires_absent_exact_destination(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    domain: str,
) -> None:
    if domain == "deletion":
        identifier = producer_deletion(producer_media, "claimed")
    else:
        import rescan_file_recovery as producer

        identifier = producer_rescan(producer_media, "prepared")
        operation = producer.load_operation(identifier)
        assert operation is not None
        with file_mutation_guard(str(producer_media.db_path)) as guard:
            producer._allocate(operation, guard, "stage")
            with producer._open(operation, guard, "stage") as stage:
                write_archive(Path(stage.artifact_path), b"ready stage")
                operation.fingerprints["stage"] = asdict(
                    producer.fingerprint_path(stage.artifact_path)
                )
                producer._store(operation)
            _ = producer._capture_source(operation, guard)
    inventory = capture_inventory(producer_media, monkeypatch)
    owner = OwnerRef(domain, (identifier,))
    decision = chosen(
        inventory,
        owner,
        "restore-original",
        {"source_artifact" if domain == "rescan" else "delete_artifact"},
        str(producer_media.source),
    )
    plan = recovery.build_recovery_plan(
        inventory, *review_documents(inventory, decision)
    )
    assert plan.decisions == decision


@pytest.mark.parametrize("action", ["resume-commit", "cleanup-committed"])
def test_rescan_acknowledged_resume_and_committed_private_cleanup(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    identifier = producer_rescan(
        producer_media, "published" if action == "resume-commit" else "db_committed"
    )
    inventory = capture_inventory(producer_media, monkeypatch)
    owner = OwnerRef("rescan", (identifier,))
    purposes = (
        {"destination"}
        if action == "resume-commit"
        else {
            r.role.purpose
            for r in inventory.closure.roles
            if r.owner == owner
            and r.parent_role is not None
            or r.owner == owner
            and r.role.purpose.endswith("_carrier")
        }
    )
    decision = chosen(inventory, owner, action, purposes)
    plan = recovery.build_recovery_plan(
        inventory, *review_documents(inventory, decision)
    )
    assert plan.decisions == decision


def test_keep_current_unacknowledged_rescan_link_never_repairs_receipt(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rescan_file_recovery as producer

    original = producer._store

    def crash(
        operation: producer.Operation,
        *,
        state: str | None = None,
        diagnostic: str | None = None,
    ) -> None:
        if state == "published":
            raise OSError("unit crash before acknowledgement")
        return original(operation, state=state, diagnostic=diagnostic)

    with monkeypatch.context() as crash_patch:
        crash_patch.setattr(producer, "_store", crash)
        with pytest.raises(OSError, match="before acknowledgement"):
            _ = producer_rescan(producer_media, "published")
    inventory = capture_inventory(producer_media, monkeypatch)
    snapshot = next(
        s for s in inventory.closure.snapshots if s.owner.domain == "rescan"
    )
    assert (
        json.loads(snapshot.rows_json)["root"]["row"]["publication_receipt_json"]
        is None
    )
    decision = chosen(
        inventory,
        snapshot.owner,
        "keep-current",
        {"destination"},
        str(producer_media.library / "converted.cbz"),
    )
    plan = recovery.build_recovery_plan(
        inventory, *review_documents(inventory, decision)
    )
    assert plan.inventory == inventory


def acknowledged_publication(
    env: ProducerMedia, monkeypatch: pytest.MonkeyPatch, checkpoint: str = "published"
) -> int:
    import import_publication as producer
    from import_staging import _StageOutcome

    queue_id = producer_queue(env)
    if checkpoint == "original":
        write_archive(env.library / "Matrix v02.cbz", b"old final")
    identifier = producer_publication(env, queue_id, "staging")
    with producer.get_db() as db:
        publication = producer.load_publication(db, publication_id=identifier)
    assert publication is not None
    file = publication.files[0]
    path = Path(publication.staging_dir) / file.plan.filename
    write_archive(path, b"staged real publication")
    fp = producer._regular_fingerprint(str(path), include_hash=True)
    producer.commit_prepared_barrier(
        identifier,
        [_StageOutcome(file.plan.file_id, True, file.plan.dst_path, "", str(path))],
        (
            producer.PreparedArtifact(
                file.plan.file_id,
                str(path),
                file.plan.dst_path,
                fp,
                producer._regular_fingerprint(file.plan.dst_path, include_hash=True)
                if checkpoint == "original"
                else None,
            ),
        ),
    )
    assert producer._claim_publication_operation(identifier, "a" * 32)
    with producer.get_db() as db:
        publication = producer.load_publication(db, publication_id=identifier)
    assert publication is not None

    def unsupported(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EOPNOTSUPP, "unit uses real hardlink fallback")

    with monkeypatch.context() as patch:
        patch.setattr(producer, "_rename_noreplace", unsupported)
        if checkpoint == "original":

            def interrupted(*_args: object, **_kwargs: object) -> None:
                raise OSError(errno.EIO, "unit stops before publication link")

            patch.setattr(producer.claims, "link_private_regular", interrupted)
            with file_mutation_guard(str(env.db_path)) as guard:
                with pytest.raises(OSError, match="before publication"):
                    producer._publish_private(
                        publication, publication.files[0], guard, "a" * 32
                    )
        elif checkpoint == "unacknowledged":
            original = producer._private_phase

            def crash(
                publication: producer.ImportPublication,
                file_record: producer.PublicationFile,
                owner: str,
                purpose: str,
                carrier: producer.claims.CarrierHandle,
                phase: str,
                receipt: producer.claims.LinkReceipt | None = None,
            ) -> None:
                if phase == "restored":
                    raise OSError("unit crash before persisted acknowledgement")
                return original(
                    publication, file_record, owner, purpose, carrier, phase, receipt
                )

            patch.setattr(producer, "_private_phase", crash)
            with file_mutation_guard(str(env.db_path)) as guard:
                with pytest.raises(OSError, match="persisted acknowledgement"):
                    producer._publish_private(
                        publication, publication.files[0], guard, "a" * 32
                    )
        else:
            assert producer.publish_publication(identifier, "a" * 32)
    return identifier


@pytest.mark.parametrize(
    "checkpoint,action,purpose",
    [
        ("published", "resume-commit", "final"),
        ("unacknowledged", "keep-current", "final"),
        ("original", "restore-original", "original_artifact"),
    ],
)
def test_publication_actual_receipts_admission_and_original(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    checkpoint: str,
    action: str,
    purpose: str,
) -> None:
    identifier = acknowledged_publication(producer_media, monkeypatch, checkpoint)
    inventory = capture_inventory(producer_media, monkeypatch)
    owner = OwnerRef("publication", (identifier,))
    destination = (
        str(producer_media.library / "Matrix v02.cbz")
        if action != "resume-commit"
        else None
    )
    decisions = chosen(inventory, owner, action, {purpose}, destination)
    if (checkpoint, action, purpose) == (
        "original",
        "restore-original",
        "original_artifact",
    ):
        legacy = review_documents(inventory, decisions)
        with pytest.raises(InvalidIdentity, match="^ownership_phase$"):
            _ = recovery.build_recovery_plan(inventory, *legacy)
        plan = recovery.build_recovery_plan(
            inventory,
            *linked_documents(
                legacy, publication_linked_effects(inventory, owner, decisions)
            ),
        )
        assert plan.version == 2 and plan.decisions == decisions
        return
    plan = recovery.build_recovery_plan(
        inventory, *review_documents(inventory, decisions)
    )
    assert plan.decisions == decisions


@pytest.mark.parametrize("phase", ["claimed", "discarded"])
def test_deletion_resume_is_its_own_durable_deletion_decision(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    identifier = producer_deletion(producer_media, phase)
    inventory = capture_inventory(producer_media, monkeypatch)
    decisions = chosen(
        inventory,
        OwnerRef("deletion", (identifier,)),
        "resume-commit",
        {"delete_artifact"},
    )
    plan = recovery.build_recovery_plan(
        inventory, *review_documents(inventory, decisions)
    )
    assert plan.decisions == decisions


@pytest.mark.parametrize("inventory_version", [1, 2])
def test_contract_captured_pack_restore_has_complete_current_inventory(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    inventory_version: int,
) -> None:
    """Contract control: a real captured nonempty pack must be representable."""
    from private_pack_claim import PackDirectoryClaim

    producer_pack(producer_media, "attached", tombstone=True)
    closure, _ = captured_fixture(producer_media)
    tombstone = next(s for s in closure.snapshots if s.owner.domain == "pack_tombstone")
    row = json.loads(tombstone.rows_json)["root"]["row"]
    claim = PackDirectoryClaim.from_json(row["carrier_json"])
    captured = Path(claim.carrier.carrier_path) / "artifact"
    assert not Path(claim.source_directory.path).exists()
    assert claim.inventory.files
    assert all((captured / relative).is_file() for relative in claim.inventory.files)
    from file_identity import NativeHandle

    def current_native(_fd: int) -> NativeHandle:
        return NativeHandle(1, b"A2-location")

    monkeypatch.setattr(recovery, "capture_native", current_native)
    with file_mutation_guard(str(producer_media.db_path)) as guard:
        inventory = recovery._observe_closure(
            guard,
            str(producer_media.scope),
            closure,
            InventoryBudget(),
            inventory_version=inventory_version,
        )
    observed = inventory.observed
    incomplete = [
        (o.spec.owner.domain, o.spec.role.purpose, o.spec.path, o.outcome)
        for o in observed
        if o.unresolved
    ]
    if inventory_version == 1:
        assert incomplete and inventory.unresolved
        assert not any(o.outcome == "unreachable_below_absent_root" for o in observed)
    else:
        assert incomplete == [], encoded(incomplete)
        assert inventory.unresolved == ()
        assert any(o.outcome == "unreachable_below_absent_root" for o in observed)


def wire_object(value: object) -> dict[str, object]:
    assert type(value) is dict
    return cast(dict[str, object], value)


def test_plan1_refuses_inventory2_location_evidence() -> None:
    inventory = recovery.OwnershipInventory.create(
        "/media",
        ReferenceClosure((), ()),
        (),
        (),
        InventoryBudget(),
        inventory_version=2,
    )
    with pytest.raises(InvalidIdentity, match="^ownership_wire$"):
        recovery.build_recovery_plan(inventory, *documents(inventory))
    base = empty_inventory()
    plan = recovery.build_recovery_plan(base, *documents(base))
    selection, declaration, receipt = documents(inventory)
    value = wire_object(cast(object, json.loads(plan.to_json())))
    value.update(
        inventory=inventory._value(),
        declaration_json=declaration,
        backup_receipt_json=receipt,
        epoch_bindings_json=encoded(
            wire_object(cast(object, json.loads(selection)))["epoch_bindings"]
        ),
    )
    value.pop("sha256")
    value["sha256"] = digest(value)
    with pytest.raises(InvalidIdentity, match="^ownership_wire$"):
        recovery.RecoveryPlan.from_json(encoded(value))


def wire_array(value: object) -> list[object]:
    assert type(value) is list
    return cast(list[object], value)


def parse_document(text: str) -> dict[str, object]:
    return wire_object(cast(object, json.loads(text)))


def resign_documents(
    selection: dict[str, object],
    declaration: dict[str, object],
    receipt: dict[str, object],
) -> tuple[str, str, str]:
    declaration["selection_sha256"] = receipt["selection_sha256"] = digest(selection)
    receipt["declaration_sha256"] = digest(declaration)
    return encoded(selection), encoded(declaration), encoded(receipt)


def existing_epoch_documents(
    inventory: recovery.OwnershipInventory,
) -> tuple[str, str, str]:
    selection, declaration, receipt = (
        parse_document(d) for d in review_documents(inventory)
    )
    raw = {
        r.table: dict(r.values)
        for r in inventory.closure.raw_rows
        if r.table == "file_identity_authorities"
    }
    authority = raw["file_identity_authorities"]
    declaration["authorities"] = [
        {
            "authority_id": authority["authority_id"],
            "incarnation_token": authority["incarnation_token"],
            "scope": parse_document(str(authority["declaration_json"])),
            "profile": parse_document(str(authority["profile_json"])),
            "original_storage_statement": "Disposable ledger fixture original storage",
            "administration_statement": "No fixture storage transition",
        }
    ]
    for key in ("owner_reviews", "role_reviews"):
        for value in wire_array(declaration[key]):
            wire_object(value)[
                "intended_original_authority_id"
                if key == "owner_reviews"
                else "authority_id"
            ] = authority["authority_id"]
    epochs = [
        dict(r.values)
        for r in inventory.closure.raw_rows
        if r.table == "file_identity_epochs"
    ]
    epoch = wire_object(selection["epoch_bindings"])
    epoch["existing"] = sorted(epochs, key=lambda r: str(r["epoch_id"]))
    for value in wire_array(epoch["role_bindings"]):
        binding = wire_object(value)
        owner = wire_object(binding["owner"])
        parent = wire_array(owner["key"])[0]
        binding["epoch_id"] = next(
            r["epoch_id"] for r in epochs if r["origin_path"] == parent
        )
        binding["authority_id"] = authority["authority_id"]
    return resign_documents(selection, declaration, receipt)


def test_existing_epochs_bind_captured_ledger_bytes_and_roles(
    ledger_fixture: LedgerFixture,
) -> None:
    candidate = cast(object, inspect_fixture(ledger_fixture.path, ledger_fixture.scope))
    assert type(candidate) is recovery.OwnershipInventory
    inventory = candidate
    inputs = existing_epoch_documents(inventory)
    plan = recovery.build_recovery_plan(inventory, *inputs)
    assert plan.epoch_bindings_json == encoded(
        parse_document(inputs[0])["epoch_bindings"]
    )
    assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan


@pytest.mark.parametrize(
    "change",
    [
        "omit_epoch",
        "duplicate_epoch",
        "birth_bytes",
        "origin",
        "incarnation",
        "scope",
        "profile",
        "missing_role_epoch",
        "foreign_role_epoch",
        "namespace",
    ],
)
def test_existing_epoch_resigned_linkage_refuses(
    ledger_fixture: LedgerFixture, change: str
) -> None:
    candidate = cast(object, inspect_fixture(ledger_fixture.path, ledger_fixture.scope))
    assert type(candidate) is recovery.OwnershipInventory
    selection, declaration, receipt = (
        parse_document(d) for d in existing_epoch_documents(candidate)
    )
    epoch = wire_object(selection["epoch_bindings"])
    existing = wire_array(epoch["existing"])
    binding = wire_object(wire_array(epoch["role_bindings"])[0])
    authority = wire_object(wire_array(declaration["authorities"])[0])
    if change == "omit_epoch":
        existing.clear()
    elif change == "duplicate_epoch":
        existing.append(existing[0])
    elif change == "birth_bytes":
        item = wire_object(existing[0])
        item["birth_json"] = str(item["birth_json"]) + " "
    elif change == "origin":
        wire_object(existing[0])["origin_path"] = "/different"
    elif change == "incarnation":
        authority["incarnation_token"] = "f" * 32
    elif change == "scope":
        wire_object(authority["scope"])["scope_token"] = "f" * 32
    elif change == "profile":
        wire_object(authority["profile"])["qualification_sha256"] = "f" * 64
    elif change == "missing_role_epoch":
        binding["epoch_id"] = None
    elif change == "foreign_role_epoch":
        binding["epoch_id"] = "f" * 32
    else:
        binding["namespace_owner"] = {"domain": "namespace", "key": ["/unrelated"]}
    with pytest.raises(InvalidIdentity):
        _ = recovery.build_recovery_plan(
            candidate, *resign_documents(selection, declaration, receipt)
        )


def provision_documents(
    inventory: recovery.OwnershipInventory,
    decisions: tuple[recovery.RecoveryDecision, ...] | None = None,
) -> tuple[str, str, str]:
    selection, declaration, receipt = (
        parse_document(d) for d in review_documents(inventory, decisions)
    )
    client, server = _candidate_contracts()
    scope = inventory.parent
    if not wire_array(declaration["authorities"]):
        declaration["authorities"] = [
            {
                "authority_id": "a" * 32,
                "incarnation_token": "b" * 32,
                "scope": {"version": 1, "scope_path": scope, "scope_token": "c" * 32},
                "profile": {
                    "version": 1,
                    "profile_id": "unit-only",
                    "revision": 1,
                    "qualification_sha256": "d" * 64,
                    "client_contract": client,
                    "server_contract": server,
                },
                "original_storage_statement": "Disposable original storage",
                "administration_statement": "No unit storage administration",
            }
        ]
    old = [
        s.owner for s in inventory.closure.snapshots if s.owner.domain == "namespace"
    ]
    assert len(old) == 1
    origin = str(old[0].key[0])
    epoch = wire_object(selection["epoch_bindings"])
    eid = "e" * 32
    epoch["provision"] = [
        {
            "epoch_id": eid,
            "authority_id": "a" * 32,
            "origin_parent": origin,
            "allocation_parent": origin + "/.mangarr-epoch-" + eid,
            "root_path": origin + "/.mangarr-epoch-" + eid + "/.mangarr-claims",
            "old_namespace_owners": [recovery._owner_value(o) for o in old],
            "handling": "retain-old-disable-new-allocation",
        }
    ]
    for values in (
        wire_array(declaration["role_reviews"]),
        wire_array(epoch["role_bindings"]),
    ):
        for value in values:
            record = wire_object(value)
            if wire_object(record["role"])["purpose"] == "namespace_parent":
                record["authority_id"] = "a" * 32
    return resign_documents(selection, declaration, receipt)


def test_provision_is_exact_geometry_and_review_only_intent(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = producer_deletion(producer_media, "claimed")
    inventory = capture_inventory(producer_media, monkeypatch)
    inputs = provision_documents(inventory)
    before = (
        media_snapshot(producer_media.scope),
        control_snapshot(producer_media.db_path),
    )
    plan = recovery.build_recovery_plan(inventory, *inputs)
    assert plan.epoch_bindings_json == encoded(
        parse_document(inputs[0])["epoch_bindings"]
    )
    assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan
    assert (
        media_snapshot(producer_media.scope),
        control_snapshot(producer_media.db_path),
    ) == before


@pytest.mark.parametrize(
    "change",
    [
        "old_omission",
        "duplicate",
        "geometry",
        "old_role_rebind",
        "birth_injection",
        "foreign_authority",
        "handling",
        "old_root",
    ],
)
def test_provision_resigned_constraints_refuse(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    _ = producer_deletion(producer_media, "claimed")
    inventory = capture_inventory(producer_media, monkeypatch)
    selection, declaration, receipt = (
        parse_document(d) for d in provision_documents(inventory)
    )
    epoch = wire_object(selection["epoch_bindings"])
    intents = wire_array(epoch["provision"])
    intent = wire_object(intents[0])
    if change == "old_omission":
        intent["old_namespace_owners"] = []
    elif change == "duplicate":
        intents.append(dict(intent))
    elif change == "geometry":
        intent["allocation_parent"] = str(intent["allocation_parent"]) + "-other"
    elif change == "old_role_rebind":
        wire_object(wire_array(epoch["role_bindings"])[0])["epoch_id"] = intent[
            "epoch_id"
        ]
    elif change == "birth_injection":
        intent["birth_json"] = "not an actual receipt"
    elif change == "foreign_authority":
        intent["authority_id"] = "f" * 32
    elif change == "handling":
        intent["handling"] = "allocate-now"
    else:
        intent["root_path"] = str(producer_media.library / ".mangarr-claims")
    with pytest.raises(InvalidIdentity):
        _ = recovery.build_recovery_plan(
            inventory, *resign_documents(selection, declaration, receipt)
        )


def rescan_acknowledged_fixture(
    env: ProducerMedia, monkeypatch: pytest.MonkeyPatch, same_path: bool
) -> int:
    import rescan
    import rescan_file_recovery as producer

    with closing(sqlite3.connect(env.db_path)) as db, db:
        db.row_factory = sqlite3.Row
        row = cast(
            sqlite3.Row | None,
            db.execute(rescan._ENRICHMENT_VOLUME_SELECT, (1,)).fetchone(),
        )
        assert row is not None
        volume = {key: cast(object, row[key]) for key in row.keys()}
    info = env.source.stat()
    target = rescan._EnrichmentTarget(
        volume,
        1,
        str(env.source),
        rescan.FileFingerprint(
            info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino
        ),
    )
    context = rescan._current_enrichment_context(target)
    assert context is not None
    operation = producer._reserve(
        target,
        context,
        producer.fingerprint_path(str(env.source)),
        str(env.source if same_path else env.library / "converted.cbz"),
    )
    assert operation is not None
    with file_mutation_guard(str(env.db_path)) as guard:
        producer._allocate(operation, guard, "stage")
        stage = producer._record(operation, "stage")
        assert stage is not None
        stage_path = Path(stage.carrier_path) / "artifact"
        write_archive(stage_path, b"replacement unit publication")
        operation.fingerprints["stage"] = asdict(
            producer.fingerprint_path(str(stage_path))
        )
        producer._store(operation)
        _ = producer._capture_source(operation, guard)

        def unsupported(*_args: object, **_kwargs: object) -> None:
            raise OSError(errno.EOPNOTSUPP, "unit uses actual hardlink fallback")

        with monkeypatch.context() as patch:
            patch.setattr(rescan, "_rename_noreplace", unsupported)
            receipt = producer._publish(operation, guard)
        assert receipt.destination_path == operation.row["destination_path"]
    identifier = cast(object, operation.row["id"])
    assert type(identifier) is int
    return identifier


def rollback_documents(
    inventory: recovery.OwnershipInventory, owner: OwnerRef, *, linked: bool = False
) -> tuple[str, str, str]:
    original_purpose, public_purpose = (
        ("source_artifact", "destination")
        if owner.domain == "rescan"
        else ("original_artifact", "final")
    )
    original = next(
        r
        for r in inventory.closure.roles
        if r.owner == owner and r.role.purpose == original_purpose
    )
    public = next(
        r
        for r in inventory.closure.roles
        if r.owner == owner and r.role.purpose == public_purpose
    )
    snapshot = next(s for s in inventory.closure.snapshots if s.owner == owner)
    row = wire_object(wire_object(parse_document(snapshot.rows_json)["root"])["row"])
    destination = str(row["source_path"]) if owner.domain == "rescan" else public.path
    purposes = {original_purpose, public_purpose}
    if linked and owner.domain == "publication":
        purposes.add("admission_destination")
    if owner.domain == "rescan" and destination == public.path:
        purposes.add("source")
    decisions = chosen(inventory, owner, "restore-original", purposes, destination)
    selection, declaration, receipt = (
        parse_document(d) for d in provision_documents(inventory, decisions)
    )
    wire_object(selection["epoch_bindings"])["rollback_outputs"] = [
        {
            "version": 1,
            "owner": recovery._owner_value(owner),
            "original_role": recovery._role_value(original.role),
            "publication_role": recovery._role_value(public.role),
            "epoch_id": "e" * 32,
            "carrier_token": "f" * 32,
            "handling": "retain-non-replayable",
        }
    ]
    return resign_documents(selection, declaration, receipt)


@pytest.mark.parametrize(
    "domain,same_path", [("rescan", True), ("rescan", False), ("publication", True)]
)
def test_acknowledged_rollback_output_exact_pair_and_aliases(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    domain: str,
    same_path: bool,
) -> None:
    if domain == "rescan":
        identifier = rescan_acknowledged_fixture(producer_media, monkeypatch, same_path)
    else:
        import import_publication as producer

        identifier = acknowledged_publication(producer_media, monkeypatch, "original")

        def unsupported(*_args: object, **_kwargs: object) -> None:
            raise OSError(errno.EOPNOTSUPP, "unit uses actual hardlink fallback")

        with monkeypatch.context() as patch:
            patch.setattr(producer, "_rename_noreplace", unsupported)
            assert producer.publish_publication(identifier, "a" * 32)
    inventory = capture_inventory(producer_media, monkeypatch)
    inputs = rollback_documents(inventory, OwnerRef(domain, (identifier,)))
    if domain == "publication":
        public = next(
            o
            for o in inventory.observed
            if o.spec.owner == OwnerRef(domain, (identifier,))
            and o.spec.role.purpose == "final"
        )
        context = {
            (o.spec.owner.domain, o.spec.role.purpose)
            for o in inventory.observed
            if o.spec.owner != public.spec.owner and o.spec.path == public.spec.path
        }
        assert context == {("queue_source", "destination")}
    before = (
        inventory.to_json(),
        media_snapshot(producer_media.scope),
        control_snapshot(producer_media.db_path),
    )
    if (domain, same_path) == ("publication", True):
        with pytest.raises(InvalidIdentity, match="^ownership_phase$"):
            _ = recovery.build_recovery_plan(inventory, *inputs)
        inputs = rollback_documents(
            inventory, OwnerRef(domain, (identifier,)), linked=True
        )
        decisions = tuple(
            recovery.RecoveryDecision.from_value(v)
            for v in wire_array(parse_document(inputs[0])["decisions"])
        )
        inputs = linked_documents(
            inputs,
            publication_linked_effects(
                inventory, OwnerRef(domain, (identifier,)), decisions
            ),
        )
    plan = recovery.build_recovery_plan(inventory, *inputs)
    assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan
    assert recovery.build_recovery_plan(inventory, *inputs).to_json() == plan.to_json()
    assert (
        inventory.to_json(),
        media_snapshot(producer_media.scope),
        control_snapshot(producer_media.db_path),
    ) == before


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "duplicate",
        "owner",
        "file",
        "original",
        "publication",
        "epoch",
        "token_collision",
        "token_shape",
        "handling",
        "version_bool",
        "extra_receipt",
        "missing_public_subject",
        "missing_alias",
        "extra_subject",
        "wrong_destination",
        "media_in_place",
    ],
)
def test_rollback_output_resigned_tampering_refuses(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    identifier = rescan_acknowledged_fixture(producer_media, monkeypatch, True)
    inventory = capture_inventory(producer_media, monkeypatch)
    selection, declaration, receipt = (
        parse_document(d)
        for d in rollback_documents(inventory, OwnerRef("rescan", (identifier,)))
    )
    epoch = wire_object(selection["epoch_bindings"])
    outputs = wire_array(epoch["rollback_outputs"])
    output = wire_object(outputs[0])
    if change == "missing":
        outputs.clear()
    elif change == "duplicate":
        outputs.append(dict(output))
    elif change == "owner":
        output["owner"] = {"domain": "rescan", "key": [identifier + 1]}
    elif change == "file":
        wire_object(output["publication_role"])["file_id"] = 99
    elif change == "original":
        output["original_role"] = dict(wire_object(output["publication_role"]))
    elif change == "publication":
        wire_object(output["publication_role"])["purpose"] = "source"
    elif change == "epoch":
        output["epoch_id"] = "0" * 32
    elif change == "token_collision":
        snapshot = next(
            s for s in inventory.closure.snapshots if s.owner.domain == "rescan"
        )
        carriers = parse_document(
            str(
                wire_object(
                    wire_object(parse_document(snapshot.rows_json)["root"])["row"]
                )["carriers_json"]
            )
        )
        output["carrier_token"] = wire_object(carriers["source"])["owner_token"]
    elif change == "token_shape":
        output["carrier_token"] = "invalid"
    elif change == "handling":
        output["handling"] = "discard"
    elif change == "version_bool":
        output["version"] = True
    elif change == "extra_receipt":
        output["capture_receipt"] = {}
    elif change == "wrong_destination":
        decision = next(
            wire_object(d)
            for d in wire_array(selection["decisions"])
            if wire_object(d)["action"] == "restore-original"
        )
        decision["destination"] = str(producer_media.library / "wrong.cbz")
    elif change == "media_in_place":
        for value in wire_array(receipt["media"]):
            item = wire_object(value)
            if item["role"] == output["publication_role"]:
                item["handling"], item["recovery_reference"] = "retain-in-place", None
    else:
        decision = next(
            wire_object(d)
            for d in wire_array(selection["decisions"])
            if wire_object(d)["action"] == "restore-original"
        )
        roles, retained = (
            wire_array(decision["roles"]),
            wire_array(decision["retained_roles"]),
        )
        purpose = (
            "destination"
            if change == "missing_public_subject"
            else "source"
            if change == "missing_alias"
            else "stage_artifact"
        )
        source, target = (
            (retained, roles) if change == "extra_subject" else (roles, retained)
        )
        moved = next(r for r in source if wire_object(r)["purpose"] == purpose)
        source.remove(moved)
        target.append(moved)
        target.sort(key=encoded)
    with pytest.raises(InvalidIdentity):
        _ = recovery.build_recovery_plan(
            inventory, *resign_documents(selection, declaration, receipt)
        )


def test_provision_object_intents_count_against_existing_object_budget(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = producer_deletion(producer_media, "claimed")
    inventory = capture_inventory(producer_media, monkeypatch)
    limited = recovery.OwnershipInventory.create(
        inventory.parent,
        inventory.closure,
        inventory.observed,
        (),
        InventoryBudget(1000, len(inventory.observed), 68719476736),
    )
    with pytest.raises(InvalidIdentity, match="ownership_budget"):
        _ = recovery.build_recovery_plan(limited, *provision_documents(limited))


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("rollback", [False, True])
@pytest.mark.parametrize("headroom_delta", [-1, 0])
@pytest.mark.parametrize("entry", ["builder", "direct", "from_json"])
def test_provision_complete_typed_birth_budget_boundary(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    version: int,
    rollback: bool,
    headroom_delta: int,
    entry: str,
) -> None:
    if rollback:
        identifier = rescan_acknowledged_fixture(producer_media, monkeypatch, True)
    else:
        _ = producer_deletion(producer_media, "claimed")
        identifier = 0
    inventory = capture_inventory(producer_media, monkeypatch)

    def inputs_for(current: recovery.OwnershipInventory) -> tuple[str, str, str]:
        inputs = (
            rollback_documents(current, OwnerRef("rescan", (identifier,)))
            if rollback
            else provision_documents(current)
        )
        return linked_documents(inputs, []) if version == 2 else inputs

    baseline = recovery.build_recovery_plan(inventory, *inputs_for(inventory))
    epoch = parse_document(baseline.epoch_bindings_json)
    assert len(wire_array(epoch["provision"])) == 1
    assert len(wire_array(epoch["rollback_outputs"])) == int(rollback)
    current_objects = sum(
        1 + len(wire_array(parse_document(o.constraints_json)["children"]))
        for o in inventory.observed
    )
    # The old owner's origin path is still an independent new birth binding.
    provision = wire_object(wire_array(epoch["provision"])[0])
    assert any(o.spec.path == provision["origin_parent"] for o in inventory.observed)
    complete_headroom = 4 + 3 * int(rollback)
    limited = recovery.OwnershipInventory.create(
        inventory.parent,
        inventory.closure,
        inventory.observed,
        (),
        replace(
            inventory.budget,
            max_objects=current_objects + complete_headroom + headroom_delta,
        ),
        inventory_version=inventory.version,
    )
    inputs = inputs_for(limited)
    selection, declaration, receipt = (parse_document(d) for d in inputs)
    wire: dict[str, object] = {
        "version": version,
        "inventory": parse_document(limited.to_json()),
        "decisions": selection["decisions"],
        "declaration_json": encoded(declaration),
        "backup_receipt_json": encoded(receipt),
        "epoch_bindings_json": encoded(selection["epoch_bindings"]),
    }
    wire["sha256"] = digest(wire)

    def construct() -> recovery.RecoveryPlan:
        if entry == "builder":
            return recovery.build_recovery_plan(limited, *inputs)
        if entry == "from_json":
            return recovery.RecoveryPlan.from_json(encoded(wire))
        return recovery.RecoveryPlan(
            version,
            limited,
            baseline.decisions,
            encoded(declaration),
            encoded(receipt),
            encoded(selection["epoch_bindings"]),
            cast(str, wire["sha256"]),
        )

    with transferred_plan_conservation(producer_media, monkeypatch, limited):
        if headroom_delta < 0:
            with pytest.raises(InvalidIdentity, match="^ownership_budget$"):
                _ = construct()
        else:
            plan = construct()
            assert plan.version == version
            assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan
            assert recovery.build_recovery_plan(limited, *inputs) == plan


@pytest.mark.parametrize(
    "target,fail_at",
    [("helper", 0), ("helper", 1), ("purity", 0), ("purity", 1), ("purity", 2)],
)
def test_new_planner_sqlite_contexts_close_on_normal_and_error(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    fail_at: int,
) -> None:
    real_connect = sqlite3.connect
    connections: list[sqlite3.Connection] = []
    exits: list[tuple[type[BaseException] | None, bool, int]] = []

    class InjectedReadError(RuntimeError):
        pass

    class SQLParameters(Protocol):
        def __len__(self) -> int: ...

        def __getitem__(self, key: int, /) -> object: ...

    class TrackedConnection(sqlite3.Connection):
        def __enter__(self) -> sqlite3.Connection:
            _ = super().__enter__()
            _ = super().execute("CREATE TEMP TABLE lifetime_probe(value INTEGER)")
            _ = super().execute("INSERT INTO lifetime_probe VALUES (1)")
            return self

        def execute(
            self,
            sql: str,
            parameters: SQLParameters | Mapping[str, object] = (),
            /,
        ) -> sqlite3.Cursor:
            if fail_at == len(connections):
                raise InjectedReadError("owned read boundary")
            return super().execute(sql, parameters)

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: TracebackType | None,
        ) -> Literal[False]:
            result = super().__exit__(exc_type, exc_value, traceback)
            count = cast(
                tuple[int],
                super().execute("SELECT COUNT(*) FROM lifetime_probe").fetchone(),
            )[0]
            exits.append((exc_type, self.in_transaction, count))
            return result

    class FixtureSQL:
        Row: type[sqlite3.Row] = sqlite3.Row

        @staticmethod
        def connect(path: Path) -> sqlite3.Connection:
            connection = real_connect(path, factory=TrackedConnection)
            connections.append(connection)
            return connection

    def invoke() -> None:
        if target == "helper":
            _ = rescan_acknowledged_fixture(producer_media, monkeypatch, False)
        else:
            test_epoch_output_builder_is_pure_and_conserves_fixture(
                producer_media, monkeypatch, "provision", False
            )

    try:
        with monkeypatch.context() as patch:
            patch.setattr(sys.modules[__name__], "sqlite3", FixtureSQL)
            if fail_at:
                with pytest.raises(InjectedReadError, match="^owned read boundary$"):
                    invoke()
            else:
                invoke()
        assert len(connections) == (fail_at or (1 if target == "helper" else 2))
        assert exits == [
            (
                InjectedReadError if fail_at and index == fail_at else None,
                False,
                0 if fail_at and index == fail_at else 1,
            )
            for index in range(1, len(connections) + 1)
        ]
        for connection in connections:
            with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
                _ = sqlite3.Connection.execute(connection, "SELECT 1")
    finally:
        for connection in connections:
            connection.close()


@pytest.mark.parametrize("bad", [object(), None, [], (object(),)])
def test_plan_direct_constructor_rejects_wrong_decision_values(bad: object) -> None:
    inventory = empty_inventory()
    inputs = documents(inventory)
    plan = recovery.build_recovery_plan(inventory, *inputs)
    with pytest.raises(InvalidIdentity):
        _ = replace(plan, decisions=cast(tuple[recovery.RecoveryDecision, ...], bad))


def test_composite_budget_refuses_before_canonical_string_encoding(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = producer_deletion(producer_media, "claimed")
    inventory = capture_inventory(producer_media, monkeypatch)
    selection, declaration, receipt = (
        parse_document(d) for d in provision_documents(inventory)
    )
    wire_object(wire_array(declaration["authorities"])[0])[
        "original_storage_statement"
    ] = "x" * 3_000_000
    wire_object(receipt["archive"])["path"] = "/" + "y" * 3_000_000
    # A reduced ceiling exercises real escaped-size accounting without a large allocation.
    seen: list[int] = []
    original = recovery._plan_fragments

    def fragments(value: object) -> Iterator[str]:
        generated = original(value)
        if type(value) is dict and (
            "authorities" in cast(dict[str, object], value)
            or "archive" in cast(dict[str, object], value)
        ):
            seen.append(1)
        yield from generated

    with monkeypatch.context() as patch:
        patch.setattr(recovery, "_plan_fragments", fragments)
        patch.setattr(recovery, "_DOCUMENT_BYTES", 5_000_000)
        with pytest.raises(InvalidIdentity):
            _ = recovery.build_recovery_plan(
                inventory, *resign_documents(selection, declaration, receipt)
            )
    assert seen == []


def test_direct_plan_budget_precedes_decision_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = empty_inventory()
    plan = recovery.build_recovery_plan(inventory, *documents(inventory))
    decision = recovery.RecoveryDecision(
        OwnerRef("rescan", (1,)),
        "cancel-unmutated",
        (RoleKey(1, "p" * 3000, "file", None),),
        None,
        (),
    )

    def forbidden_projection(_self: recovery.RecoveryDecision) -> dict[str, object]:
        raise AssertionError("projection before whole-plan reservation")

    with monkeypatch.context() as patch:
        patch.setattr(recovery.RecoveryDecision, "to_value", forbidden_projection)
        with pytest.raises(InvalidIdentity, match="ownership_budget"):
            _ = replace(plan, decisions=(decision,) * 6000)


def test_direct_decision_budget_precedes_role_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roles = (RoleKey(1, "a" * 6000, "file", None), RoleKey(1, "b" * 6000, "file", None))

    def forbidden_encoding(_value: object) -> str:
        raise AssertionError("encoding before decision reservation")

    with monkeypatch.context() as patch:
        patch.setattr(recovery, "_DOCUMENT_BYTES", 10000)
        patch.setattr(recovery, "_encode", forbidden_encoding)
        with pytest.raises(InvalidIdentity, match="ownership_budget"):
            _ = recovery.RecoveryDecision(
                OwnerRef("rescan", (1,)), "cancel-unmutated", roles, None, ()
            )


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        -(2**63),
        {
            "a": [
                'quote"',
                "back\\slash",
                "\b\t\n\f\r",
                "\x01",
                "\x7f",
                "\u00e9",
                "\U0001f600",
            ]
        },
    ],
)
@pytest.mark.parametrize("ascii_wire", [True, False])
def test_scanner_counts_canonical_and_double_escaped_bytes(
    value: object, ascii_wire: bool
) -> None:
    text = json.dumps(value, ensure_ascii=ascii_wire, indent=2)
    plain, escapes = recovery._PlanScanner(text, 16 * 1024**2).scan()
    canonical = encoded(value)
    assert plain == len(canonical)
    assert 2 + plain + escapes == len(encoded(canonical))


@pytest.mark.parametrize(
    "text",
    [
        '{"version":1,"version":1}',
        "[NaN]",
        "[Infinity]",
        "[1e400]",
        "[1.0]",
        "[true,]",
        '["\\ud800"]',
        '["\\udc00"]',
        '["\\u0000"]',
        '["\\x20"]',
        "[" * 33 + "0" + "]" * 33,
        "[" + ",".join(["0"] * 10001) + "]",
    ],
)
def test_new_wire_refuses_before_container_materialization(
    monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    def forbidden_decode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("decoder reached for refused preflight")

    with monkeypatch.context() as patch:
        patch.setattr(json, "loads", forbidden_decode)
        with pytest.raises(InvalidIdentity):
            _ = recovery._plan_input(text)


@pytest.mark.parametrize("version", [1, 2])
def test_exact_16mib_plan_boundary_and_overflow(version: int) -> None:
    inventory = empty_inventory()
    inputs = documents(inventory)
    if version == 2:
        inputs = linked_documents(inputs, [])
    selection, declaration, receipt = (parse_document(d) for d in inputs)
    baseline = recovery.build_recovery_plan(
        inventory, *resign_documents(selection, declaration, receipt)
    )
    archive = wire_object(receipt["archive"])
    count = 16 * 1024**2 - len(baseline.to_json()) + len(str(archive["path"])) - 1
    archive["path"] = "/" + "a" * count
    inputs = resign_documents(selection, declaration, receipt)
    plan = recovery.build_recovery_plan(inventory, *inputs)
    assert len(plan.to_json()) == 16 * 1024**2
    assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan
    archive["path"] = str(archive["path"]) + "a"
    with pytest.raises(InvalidIdentity, match="ownership_budget"):
        _ = recovery.build_recovery_plan(
            inventory, *resign_documents(selection, declaration, receipt)
        )


@contextmanager
def trapped_external_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None, None, None]:
    import builtins
    import datetime
    import io
    import os
    import random
    import secrets
    import time
    import uuid
    import backups
    import file_identity
    import file_identity_consumers
    import file_identity_store
    import import_plan
    import private_file_claim
    import rescan
    import shared
    from file_mutation_lock import FileMutationGuard

    def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("external boundary called by pure planner")

    class NoClock:
        @staticmethod
        def now(_tz: datetime.tzinfo | None = None) -> NoReturn:
            forbidden()

        @staticmethod
        def utcnow() -> NoReturn:
            forbidden()

    with monkeypatch.context() as patch:
        for module, names in (
            (builtins, ("open",)),
            (io, ("open",)),
            (
                os,
                (
                    "open",
                    "stat",
                    "lstat",
                    "fstat",
                    "readlink",
                    "listdir",
                    "scandir",
                    "urandom",
                ),
            ),
            (time, ("time", "time_ns", "monotonic", "monotonic_ns", "perf_counter")),
            (random, ("random", "getrandbits", "_urandom")),
            (secrets, ("token_bytes", "token_hex", "token_urlsafe", "randbits")),
            (uuid, ("uuid4",)),
            (sqlite3, ("connect",)),
            (shared, ("get_db",)),
            (rescan, ("get_db", "_current_enrichment_context")),
            (import_plan, ("publication_admission",)),
            (backups, ("validate_backup_archive",)),
            (file_identity, ("capture_native", "validate_profile")),
            (recovery, ("capture_native", "collect_references", "inspect_ownership")),
            (file_identity_consumers, ("collect_references", "snapshot_owner")),
            (
                file_identity_store,
                (
                    "capture_owner_snapshot",
                    "prepare_set",
                    "activate_set",
                    "read_active",
                ),
            ),
            (
                private_file_claim,
                (
                    "ensure_namespace",
                    "allocate_carrier",
                    "claim_into_empty",
                    "link_private_regular",
                    "discard_private_regular",
                    "gc_discarded_carrier",
                ),
            ),
        ):
            for name in names:
                patch.setattr(module, name, forbidden)
        patch.setattr(datetime, "datetime", NoClock)
        patch.setattr(Path, "resolve", forbidden)
        patch.setattr(FileMutationGuard, "verify", forbidden)
        yield


@pytest.mark.parametrize("feature", ["provision", "rollback"])
@pytest.mark.parametrize("refused", [False, True])
def test_epoch_output_builder_is_pure_and_conserves_fixture(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    feature: str,
    refused: bool,
) -> None:
    if feature == "provision":
        _ = producer_deletion(producer_media, "claimed")
    else:
        _ = rescan_acknowledged_fixture(producer_media, monkeypatch, True)
    inventory = capture_inventory(producer_media, monkeypatch)
    inputs = (
        provision_documents(inventory)
        if feature == "provision"
        else rollback_documents(
            inventory,
            next(
                s.owner
                for s in inventory.closure.snapshots
                if s.owner.domain == "rescan"
            ),
        )
    )
    if refused:
        selection, declaration, receipt = (parse_document(d) for d in inputs)
        wire_object(receipt["archive"])["schema_version"] = 8
        inputs = resign_documents(selection, declaration, receipt)
    from test_file_identity_inventory import all_rows

    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        rows_before = cast(object, all_rows(db))
    before = (
        media_snapshot(producer_media.scope),
        control_snapshot(producer_media.db_path),
        inventory.to_json(),
    )
    error: BaseException | None = None
    result: recovery.RecoveryPlan | None = None
    with trapped_external_boundaries(monkeypatch):
        try:
            result = recovery.build_recovery_plan(inventory, *inputs)
            _ = result.to_json()
        except BaseException as exc:
            error = exc
    if refused:
        assert isinstance(error, InvalidIdentity), error
        assert result is None
    else:
        assert error is None, error
        assert result is not None
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        assert cast(object, all_rows(db)) == rows_before
    assert (
        media_snapshot(producer_media.scope),
        control_snapshot(producer_media.db_path),
        inventory.to_json(),
    ) == before


@pytest.mark.parametrize("revision", [6, 8, True])
def test_plan_backup_revision_is_exact_seven(revision: object) -> None:
    inventory = empty_inventory()
    selection, declaration, receipt = (parse_document(d) for d in documents(inventory))
    wire_object(receipt["archive"])["schema_version"] = revision
    with pytest.raises(InvalidIdentity):
        _ = recovery.build_recovery_plan(
            inventory, *resign_documents(selection, declaration, receipt)
        )


def test_constructor_cycles_are_typed_refusals() -> None:
    cycle: list[object] = []
    cycle.append(cycle)
    with pytest.raises(InvalidIdentity):
        _ = recovery.RecoveryDecision(
            OwnerRef("rescan", (1,)),
            "cancel-unmutated",
            cast(tuple[RoleKey, ...], (cycle,)),
            None,
            (),
        )
    with pytest.raises(InvalidIdentity):
        _ = recovery._plan_encode(cycle)


@pytest.mark.parametrize("refused", [False, True])
def test_existing_epoch_purity_and_frozen_birth_conservation(
    ledger_fixture: LedgerFixture,
    monkeypatch: pytest.MonkeyPatch,
    refused: bool,
) -> None:
    candidate = cast(object, inspect_fixture(ledger_fixture.path, ledger_fixture.scope))
    assert type(candidate) is recovery.OwnershipInventory
    inputs = existing_epoch_documents(candidate)
    if refused:
        selection, declaration, receipt = (parse_document(d) for d in inputs)
        wire_object(wire_array(declaration["authorities"])[0])["incarnation_token"] = (
            "f" * 32
        )
        inputs = resign_documents(selection, declaration, receipt)
    before = (
        candidate.to_json(),
        control_snapshot(ledger_fixture.path),
        media_snapshot(ledger_fixture.scope),
        media_snapshot(ledger_fixture.other_scope),
    )
    error: BaseException | None = None
    with trapped_external_boundaries(monkeypatch):
        try:
            _ = recovery.build_recovery_plan(candidate, *inputs)
        except BaseException as exc:
            error = exc
    if refused:
        assert isinstance(error, InvalidIdentity), error
    else:
        assert error is None, error
    assert (
        candidate.to_json(),
        control_snapshot(ledger_fixture.path),
        media_snapshot(ledger_fixture.scope),
        media_snapshot(ledger_fixture.other_scope),
    ) == before


@pytest.mark.parametrize(
    "record", [{}, {"owner": None}, {"role": None}, {"epoch_id": None}]
)
def test_epoch_bad_record_keys_are_typed_constructor_and_parser_refusals(
    record: dict[str, object],
) -> None:
    inventory = empty_inventory()
    selection, declaration, receipt = (parse_document(d) for d in documents(inventory))
    wire_object(selection["epoch_bindings"])["role_bindings"] = [record]
    with pytest.raises(InvalidIdentity):
        _ = recovery.build_recovery_plan(
            inventory, *resign_documents(selection, declaration, receipt)
        )


def test_rollback_projection_is_charged_at_exact_composite_boundary(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = rescan_acknowledged_fixture(producer_media, monkeypatch, False)
    inventory = capture_inventory(producer_media, monkeypatch)
    inputs = rollback_documents(inventory, OwnerRef("rescan", (identifier,)))
    plan = recovery.build_recovery_plan(inventory, *inputs)
    epoch = parse_document(plan.epoch_bindings_json)
    output = wire_object(wire_array(epoch["rollback_outputs"])[0])
    root = str(wire_object(wire_array(epoch["provision"])[0])["root_path"])
    carrier = root + "/" + str(output["carrier_token"])
    fid = wire_object(output["publication_role"])["file_id"]
    carrier_role = {
        "file_id": fid,
        "purpose": "recovery_rollback_carrier",
        "kind": "directory",
        "relative": None,
    }
    projections = [
        {
            "owner": output["owner"],
            "role": carrier_role,
            "path": carrier,
            "parent_role": None,
            "epoch_id": output["epoch_id"],
            "handling": "retain-non-replayable",
        },
        *[
            {
                "owner": output["owner"],
                "role": {
                    "file_id": fid,
                    "purpose": "recovery_rollback_" + purpose,
                    "kind": kind,
                    "relative": relative,
                },
                "path": carrier + "/" + relative,
                "parent_role": carrier_role,
                "epoch_id": output["epoch_id"],
                "handling": "retain-non-replayable",
            }
            for purpose, kind, relative in (
                ("marker", "marker", "owner.json"),
                ("artifact", "file", "artifact"),
            )
        ],
    ]
    ceiling = len(plan.to_json()) + len(encoded(projections))
    with monkeypatch.context() as patch:
        patch.setattr(recovery, "_DOCUMENT_BYTES", ceiling)
        assert recovery.build_recovery_plan(inventory, *inputs).sha256 == plan.sha256
        patch.setattr(recovery, "_DOCUMENT_BYTES", ceiling - 1)
        with pytest.raises(InvalidIdentity):
            _ = recovery.build_recovery_plan(inventory, *inputs)


def test_rollback_projection_reserved_before_embedded_documents_are_encoded(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = rescan_acknowledged_fixture(producer_media, monkeypatch, False)
    inventory = capture_inventory(producer_media, monkeypatch)
    inputs = rollback_documents(inventory, OwnerRef("rescan", (identifier,)))
    plan = recovery.build_recovery_plan(inventory, *inputs)
    ceiling = len(plan.to_json()) + 2
    seen: list[str] = []
    original = recovery._plan_fragments

    def fragments(value: object) -> Iterator[str]:
        generated = original(value)
        if type(value) is dict:
            fields = cast(dict[str, object], value)
            if "authorities" in fields or "archive" in fields:
                seen.append("embedded document encoded before output reservation")
        yield from generated

    with monkeypatch.context() as patch:
        patch.setattr(recovery, "_plan_fragments", fragments)
        patch.setattr(recovery, "_DOCUMENT_BYTES", ceiling)
        with pytest.raises(InvalidIdentity):
            _ = recovery.build_recovery_plan(inventory, *inputs)
    assert seen == []


def test_direct_plan_reserves_output_before_canonical_encoding(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = rescan_acknowledged_fixture(producer_media, monkeypatch, False)
    inventory = capture_inventory(producer_media, monkeypatch)
    inputs = rollback_documents(inventory, OwnerRef("rescan", (identifier,)))
    plan = recovery.build_recovery_plan(inventory, *inputs)
    ceiling = len(plan.to_json()) + 2

    def forbidden(_value: object) -> Iterator[str]:
        raise AssertionError("encoding before output reservation")

    with monkeypatch.context() as patch:
        patch.setattr(recovery, "_plan_fragments", forbidden)
        patch.setattr(recovery, "_DOCUMENT_BYTES", ceiling)
        with pytest.raises(InvalidIdentity, match="ownership_budget"):
            _ = replace(plan)


def domain_unacknowledged_deletion(
    env: ProducerMedia, *, acknowledge: bool = False
) -> int:
    import private_file_claim as claims
    import volume_file_deletion as deletion

    identifier = producer_deletion(env, "claimed")
    journal = deletion._load_journal(identifier)
    assert journal is not None and journal.claim_carrier_json is not None
    record = claims.CarrierRecord.from_json(journal.claim_carrier_json)
    with file_mutation_guard(str(env.db_path)) as guard:
        with claims.ensure_namespace(guard, journal.parent_path) as namespace:
            with claims.open_carrier(namespace, record.binding, record) as carrier:
                record = replace(record, phase="restoring")
                journal = deletion._store_carrier(journal, guard, record)
                carrier.record = record
                assert record.artifact_fingerprint is not None
                receipt = claims.link_private_regular(
                    guard,
                    carrier,
                    namespace.parent_fd,
                    Path(journal.target_path).name,
                    record.artifact_fingerprint,
                )
                assert receipt.destination_path == journal.target_path
                # Real link completed, but no restored/receipt persistence occurred.
                if acknowledge:
                    _ = deletion._store_carrier(
                        journal,
                        guard,
                        replace(record, phase="restored", restore_receipt=receipt),
                    )
    return identifier


def test_domain_deletion_keep_current_preserves_unacknowledged_link_and_reset(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = domain_unacknowledged_deletion(producer_media)
    inventory = capture_inventory(producer_media, monkeypatch)
    owner = OwnerRef("deletion", (identifier,))
    decisions = chosen(
        inventory, owner, "keep-current", {"target"}, str(producer_media.source)
    )
    before = (
        inventory.to_json(),
        media_snapshot(producer_media.scope),
        control_snapshot(producer_media.db_path),
    )
    plan = recovery.build_recovery_plan(
        inventory, *review_documents(inventory, decisions)
    )
    assert plan.decisions == decisions
    assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        row = cast(
            sqlite3.Row | None,
            db.execute(
                "SELECT state,claim_carrier_json FROM volume_file_deletions WHERE id=?",
                (identifier,),
            ).fetchone(),
        )
        assert row is not None and row[0] == "active"
        assert wire_object(parse_document(cast(str, row[1])))["restore_receipt"] is None
        assert db.execute(
            "SELECT status,import_path,download_id FROM volumes WHERE id=1"
        ).fetchone() == ("wanted", None, None)
        assert db.execute("SELECT count(*) FROM history").fetchone() == (0,)
    assert (
        inventory.to_json(),
        media_snapshot(producer_media.scope),
        control_snapshot(producer_media.db_path),
    ) == before


@pytest.mark.parametrize(
    "change", ["download_id", "chapter_reset", "chapter_acquisition"]
)
def test_domain_deletion_keep_current_rejects_changed_reset_context(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    identifier = domain_unacknowledged_deletion(producer_media)
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        if change == "download_id":
            _ = db.execute(
                "UPDATE volumes SET download_id='foreign-acquisition' WHERE id=1"
            )
        elif change == "chapter_reset":
            _ = db.execute(
                "INSERT INTO chapters(id,series_id,volume_id,chapter_num,status,monitored) VALUES(81,7,1,1,'downloaded',1)"
            )
        else:
            _ = db.execute(
                "INSERT INTO chapters(id,series_id,volume_id,chapter_num,status,monitored,download_id) VALUES(81,7,1,1,'wanted',1,'stale-chapter-acquisition')"
            )
    inventory = capture_inventory(producer_media, monkeypatch)
    decisions = chosen(
        inventory,
        OwnerRef("deletion", (identifier,)),
        "keep-current",
        {"target"},
        str(producer_media.source),
    )
    with pytest.raises(InvalidIdentity, match="ownership_context"):
        _ = recovery.build_recovery_plan(
            inventory, *review_documents(inventory, decisions)
        )


def test_domain_deletion_keep_current_with_real_reset_chapter_is_representable(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        _ = db.execute(
            "INSERT INTO chapters(id,series_id,volume_id,chapter_num,status,monitored) VALUES(81,7,1,1,'downloaded',1)"
        )
    identifier = domain_unacknowledged_deletion(producer_media)
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        assert db.execute(
            "SELECT status,import_path FROM chapters WHERE id=81"
        ).fetchone() == ("wanted", None)
    inventory = capture_inventory(producer_media, monkeypatch)
    chapters = [
        dict(r.values) for r in inventory.closure.raw_rows if r.table == "chapters"
    ]
    assert (
        chapters and chapters[0]["status"] == "wanted" and chapters[0]["monitored"] == 1
    )
    decisions = chosen(
        inventory,
        OwnerRef("deletion", (identifier,)),
        "keep-current",
        {"target"},
        str(producer_media.source),
    )
    plan = recovery.build_recovery_plan(
        inventory, *review_documents(inventory, decisions)
    )
    assert plan.decisions == decisions


def domain_committed_publication(
    env: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> int:
    import import_publication as publication
    from import_commit import _commit_import
    from import_lease import IMPORT_LEASE_SECONDS

    identifier = acknowledged_publication(env, monkeypatch)
    with publication.get_db() as db:
        recorded = publication.load_publication(db, publication_id=identifier)
        assert recorded is not None
        result = _commit_import(
            db,
            recorded.plan,
            [f.outcome for f in recorded.files],
            fs_committed=True,
            commit_failure_reason="",
            lease_owner="a" * 32,
            lease_seconds=IMPORT_LEASE_SECONDS,
            publication_id=identifier,
        )
        assert result == (True, 1, "imported"), result
    with publication.get_db() as db:
        recorded = publication.load_publication(db, publication_id=identifier)
        assert recorded is not None and recorded.state == "db_committed"
        assert (
            wire_object(
                cast(dict[str, object], recorded.plan.queue)["_file_publication"]
            )["decision"]
            == "commit"
        )
    return identifier


def test_domain_publication_committed_private_cleanup_keeps_public_and_context(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = domain_committed_publication(producer_media, monkeypatch)
    inventory = capture_inventory(producer_media, monkeypatch)
    decisions = chosen(
        inventory,
        OwnerRef("publication", (identifier,)),
        "cleanup-committed",
        {"publication_artifact"},
    )
    before = (
        inventory.to_json(),
        media_snapshot(producer_media.scope),
        control_snapshot(producer_media.db_path),
    )
    plan = recovery.build_recovery_plan(
        inventory, *review_documents(inventory, decisions)
    )
    assert plan.decisions == decisions
    assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan
    assert (
        inventory.to_json(),
        media_snapshot(producer_media.scope),
        control_snapshot(producer_media.db_path),
    ) == before


def test_domain_deletion_keep_current_preserves_independent_loose_chapter(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loose = producer_media.library / "Matrix Ch001.cbz"
    write_archive(loose, b"independent chapter remains downloaded")
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        _ = db.execute(
            "INSERT INTO chapters(id,series_id,volume_id,chapter_num,status,monitored,import_path,download_id,quality) VALUES(81,7,1,1,'downloaded',1,?,'independent-acquisition','manual-quality')",
            (str(loose),),
        )
    identifier = domain_unacknowledged_deletion(producer_media)
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        before = cast(
            tuple[object, ...],
            db.execute("SELECT * FROM chapters WHERE id=81").fetchone(),
        )
        assert db.execute(
            "SELECT status,import_path,download_id FROM chapters WHERE id=81"
        ).fetchone() == ("downloaded", str(loose), "independent-acquisition")
    inventory = capture_inventory(producer_media, monkeypatch)
    decisions = chosen(
        inventory,
        OwnerRef("deletion", (identifier,)),
        "keep-current",
        {"target"},
        str(producer_media.source),
    )
    _ = recovery.build_recovery_plan(inventory, *review_documents(inventory, decisions))
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        assert (
            cast(
                tuple[object, ...],
                db.execute("SELECT * FROM chapters WHERE id=81").fetchone(),
            )
            == before
        )


@pytest.mark.parametrize(
    "change",
    [
        "state",
        "result_count",
        "queue_status",
        "mapping",
        "volume_status",
        "volume_path",
        "final",
        "stage",
        "missing_private",
    ],
)
def test_domain_publication_cleanup_refuses_resigned_phase_support_or_current_context(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    identifier = domain_committed_publication(producer_media, monkeypatch)
    import import_publication as publication

    with publication.get_db() as db:
        recorded = publication.load_publication(db, publication_id=identifier)
        assert recorded is not None
        if change == "state":
            _ = db.execute(
                "UPDATE import_publications SET state='published' WHERE id=?",
                (identifier,),
            )
        elif change == "result_count":
            _ = db.execute(
                "UPDATE import_publications SET result_imported_count=0 WHERE id=?",
                (identifier,),
            )
        elif change == "queue_status":
            _ = db.execute("UPDATE import_queue SET status='importing'")
        elif change == "mapping":
            _ = db.execute(
                "UPDATE import_queue_files SET filename='changed-mapping.cbz'"
            )
        elif change == "volume_status":
            _ = db.execute("UPDATE volumes SET status='wanted' WHERE volume_num=2")
        elif change == "volume_path":
            _ = db.execute("UPDATE volumes SET import_path=NULL WHERE volume_num=2")
        elif change == "final":
            write_archive(Path(recorded.files[0].outcome.final_dst), b"changed final")
        elif change == "stage":
            write_archive(Path(recorded.files[0].outcome.stage_path), b"changed stage")
        else:
            record = publication._private_record(
                recorded, recorded.files[0], "publication"
            )
            assert record is not None
            (Path(record.carrier_path) / "artifact").unlink()
    inventory = capture_inventory(producer_media, monkeypatch)
    decisions = chosen(
        inventory,
        OwnerRef("publication", (identifier,)),
        "cleanup-committed",
        {"publication_artifact"},
    )
    with pytest.raises(InvalidIdentity):
        _ = recovery.build_recovery_plan(
            inventory, *review_documents(inventory, decisions)
        )


def domain_publication_discard_window(
    env: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> int:
    import import_publication as publication

    identifier = domain_committed_publication(env, monkeypatch)

    def interrupted(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError("unit crash before private carrier GC")

    with file_mutation_guard(str(env.db_path)) as guard:
        with publication.get_db() as db:
            assert publication.claim_publication_cleanup(db, identifier, "a" * 32)
            recorded = publication.load_publication(db, publication_id=identifier)
        assert recorded is not None
        with monkeypatch.context() as patch:
            patch.setattr(publication.claims, "gc_discarded_carrier", interrupted)
            with pytest.raises(OSError, match="before private carrier GC"):
                publication._discard_private(
                    recorded, recorded.files[0], guard, "a" * 32, "publication"
                )
    return identifier


def test_domain_publication_cleanup_anchored_discard_window_is_not_missing_receipt_repair(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = domain_publication_discard_window(producer_media, monkeypatch)
    inventory = capture_inventory(producer_media, monkeypatch)
    private = next(
        o
        for o in inventory.observed
        if o.spec.owner == OwnerRef("publication", (identifier,))
        and o.spec.role.purpose == "publication_artifact"
    )
    assert private.outcome == "absent"
    decisions = chosen(
        inventory, private.spec.owner, "cleanup-committed", {"publication_artifact"}
    )
    plan = recovery.build_recovery_plan(
        inventory, *review_documents(inventory, decisions)
    )
    assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan


def test_domain_normal_completed_deletion_gc_refuses_incomplete_cleanup(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = producer_deletion(producer_media, "completed")
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        assert db.execute(
            "SELECT state FROM volume_file_deletions WHERE id=?", (identifier,)
        ).fetchone() == ("completed",)
    closure, _ = captured_fixture(producer_media)
    observed = observe_fixture(recovery, producer_media, closure, monkeypatch)
    incomplete = [(o.spec.role.purpose, o.outcome) for o in observed if o.unresolved]
    assert incomplete == [
        ("delete_artifact", "unreadable"),
        ("delete_marker", "unreadable"),
    ]
    inventory = recovery.OwnershipInventory.create(
        str(producer_media.scope),
        closure,
        observed,
        ("role_unreadable",),
        InventoryBudget(),
    )
    with pytest.raises(InvalidIdentity, match="ownership_incomplete"):
        _ = recovery.build_recovery_plan(inventory, *review_documents(inventory))


def test_domain_normal_publication_allocating_intent_refuses_cancel(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_publication as publication

    queue_id = producer_queue(producer_media)

    def interrupted(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError("unit crash before staging namespace allocation")

    with monkeypatch.context() as patch:
        patch.setattr(publication.claims, "ensure_namespace", interrupted)
        with pytest.raises(OSError, match="before staging namespace allocation"):
            _ = producer_publication(producer_media, queue_id, "staging")
    with publication.get_db() as db:
        recorded = publication.load_publication(db, queue_id=queue_id)
        assert recorded is not None and recorded.state == "staging"
        staging = wire_object(
            cast(dict[str, object], recorded.plan.queue)["_publication_staging"]
        )
        assert staging["phase"] == "allocating" and staging["carrier"] is None
        assert not Path(recorded.staging_dir).exists()
    closure, _ = captured_fixture(producer_media)
    observed = observe_fixture(recovery, producer_media, closure, monkeypatch)
    incomplete = [(o.spec.role.purpose, o.outcome) for o in observed if o.unresolved]
    assert incomplete == [("stage", "unreadable")]
    inventory = recovery.OwnershipInventory.create(
        str(producer_media.scope),
        closure,
        observed,
        ("role_unreadable",),
        InventoryBudget(),
    )
    with pytest.raises(InvalidIdentity, match="ownership_incomplete"):
        _ = recovery.build_recovery_plan(inventory, *review_documents(inventory))


@pytest.mark.parametrize(
    "column,value",
    [
        ("quality", "foreign-quality"),
        ("download_client_id", 99),
        ("indexer", "stale-indexer"),
    ],
)
def test_domain_publication_cleanup_refuses_changed_committed_metadata(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    column: str,
    value: object,
) -> None:
    identifier = domain_committed_publication(producer_media, monkeypatch)
    assert column in {"quality", "download_client_id", "indexer"}
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        _ = db.execute(
            "UPDATE volumes SET " + column + "=? WHERE volume_num=2", (value,)
        )
    inventory = capture_inventory(producer_media, monkeypatch)
    decisions = chosen(
        inventory,
        OwnerRef("publication", (identifier,)),
        "cleanup-committed",
        {"publication_artifact"},
    )
    with pytest.raises(InvalidIdentity, match="ownership_context"):
        _ = recovery.build_recovery_plan(
            inventory, *review_documents(inventory, decisions)
        )


def test_domain_deletion_completed_discard_barrier_with_retained_support(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import volume_file_deletion as deletion

    identifier = producer_deletion(producer_media, "discarded")
    journal = deletion._load_journal(identifier)
    assert journal is not None
    # Exercise the real durable completion API against a fully persisted discard
    # checkpoint, while retaining support. This is not the normal post-GC order.
    assert deletion._complete_journal(journal, deleted=True)
    inventory = capture_inventory(producer_media, monkeypatch)
    owner = OwnerRef("deletion", (identifier,))
    decisions = chosen(inventory, owner, "cleanup-committed", {"delete_artifact"})
    plan = recovery.build_recovery_plan(
        inventory, *review_documents(inventory, decisions)
    )
    assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan


def test_domain_publication_cleanup_cannot_remove_proof_marker_while_retaining_artifact(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = domain_committed_publication(producer_media, monkeypatch)
    inventory = capture_inventory(producer_media, monkeypatch)
    decisions = chosen(
        inventory,
        OwnerRef("publication", (identifier,)),
        "cleanup-committed",
        {"publication_marker"},
    )
    with pytest.raises(InvalidIdentity, match="ownership_phase"):
        _ = recovery.build_recovery_plan(
            inventory, *review_documents(inventory, decisions)
        )


def domain_unallocated_nonready_publication(
    env: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    *,
    empty_namespaces: bool = True,
) -> int:
    import import_publication as publication
    from import_plan import _ImportPlan

    queue_id = producer_queue(env)
    if empty_namespaces:
        with file_mutation_guard(str(env.db_path)) as guard:
            for parent in (env.incoming, env.library):
                with publication.claims.ensure_namespace(guard, str(parent)):
                    pass

    def persist_only(
        plan: _ImportPlan, owner_token: str
    ) -> tuple[str, dict[int, publication.FileFingerprint]]:
        plan.files[:] = [replace(file, plan_status="skip") for file in plan.files]
        fingerprints = {
            file.file_id: publication._regular_fingerprint(
                file.src_path, include_hash=True
            )
            for file in plan.files
        }
        queue = cast(dict[str, object], plan.queue)
        qid = queue["id"]
        assert type(qid) is int
        staging = publication.deterministic_staging_dir(plan.dst_dir, qid, owner_token)
        with publication.get_db() as db:
            _ = publication.create_publication(
                db, plan, owner_token, staging, fingerprints
            )
        return staging, fingerprints

    # Use the real journal-only producer API, not an allocation stub/receipt.
    # This intentionally does not claim the normal initializer's later checkpoint.
    with monkeypatch.context() as patch:
        patch.setattr(publication, "initialize_publication_filesystem", persist_only)
        identifier = producer_publication(env, queue_id, "staging")
    return identifier


def test_domain_publication_cancel_unallocated_nonready_sources_are_unchanged(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = domain_unallocated_nonready_publication(producer_media, monkeypatch)
    inventory = capture_inventory(producer_media, monkeypatch)
    decisions = chosen(
        inventory,
        OwnerRef("publication", (identifier,)),
        "cancel-unmutated",
        {"source"},
    )
    plan = recovery.build_recovery_plan(
        inventory, *review_documents(inventory, decisions)
    )
    assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan


@pytest.mark.parametrize(
    "change",
    ["acknowledged", "phase", "target_missing", "private_missing", "changed_bytes"],
)
def test_domain_deletion_keep_current_receipt_phase_and_support_refusals(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    import private_file_claim as claims
    import volume_file_deletion as deletion

    identifier = domain_unacknowledged_deletion(
        producer_media, acknowledge=change == "acknowledged"
    )
    journal = deletion._load_journal(identifier)
    assert journal is not None and journal.claim_carrier_json is not None
    record = claims.CarrierRecord.from_json(journal.claim_carrier_json)
    if change == "phase":
        with file_mutation_guard(str(producer_media.db_path)) as guard:
            _ = deletion._store_carrier(
                journal, guard, replace(record, phase="claimed")
            )
    elif change == "target_missing":
        producer_media.source.unlink()
    elif change == "private_missing":
        (Path(record.carrier_path) / "artifact").unlink()
    elif change == "changed_bytes":
        write_archive(producer_media.source, b"changed restored outcome")
    inventory = capture_inventory(producer_media, monkeypatch)
    decisions = chosen(
        inventory,
        OwnerRef("deletion", (identifier,)),
        "keep-current",
        {"target"},
        str(producer_media.source),
    )
    with pytest.raises(InvalidIdentity):
        _ = recovery.build_recovery_plan(
            inventory, *review_documents(inventory, decisions)
        )


def domain_completed_deletion(env: ProducerMedia) -> int:
    import volume_file_deletion as deletion

    identifier = producer_deletion(env, "discarded")
    journal = deletion._load_journal(identifier)
    assert journal is not None and deletion._complete_journal(journal, deleted=True)
    return identifier


@pytest.mark.parametrize(
    "change", ["active", "target_recreated", "volume_changed", "artifact_recreated"]
)
def test_domain_deletion_cleanup_requires_commit_own_discard_and_reset(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    identifier = domain_completed_deletion(producer_media)
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        if change == "active":
            _ = db.execute(
                "UPDATE volume_file_deletions SET state='active' WHERE id=?",
                (identifier,),
            )
        elif change == "volume_changed":
            _ = db.execute("UPDATE volumes SET status='downloaded' WHERE id=1")
        elif change == "target_recreated":
            write_archive(producer_media.source, b"recreated target")
        else:
            raw = cast(
                tuple[object, ...],
                db.execute(
                    "SELECT claim_carrier_json FROM volume_file_deletions WHERE id=?",
                    (identifier,),
                ).fetchone(),
            )
            record = parse_document(cast(str, raw[0]))
            write_archive(
                Path(str(record["carrier_path"])) / "artifact",
                b"unknown post-discard bytes",
            )
    inventory = capture_inventory(producer_media, monkeypatch)
    decisions = chosen(
        inventory,
        OwnerRef("deletion", (identifier,)),
        "cleanup-committed",
        {"delete_artifact"},
    )
    with pytest.raises(InvalidIdentity):
        _ = recovery.build_recovery_plan(
            inventory, *review_documents(inventory, decisions)
        )


@pytest.mark.parametrize("change", ["source", "staging", "state", "context"])
def test_domain_publication_cancel_refuses_staged_changed_or_foreign_context(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    import import_publication as publication

    identifier = domain_unallocated_nonready_publication(producer_media, monkeypatch)
    with publication.get_db() as db:
        recorded = publication.load_publication(db, publication_id=identifier)
        assert recorded is not None
        if change == "source":
            write_archive(
                Path(recorded.files[0].plan.src_path), b"changed original source"
            )
        elif change == "staging":
            Path(recorded.staging_dir).mkdir()
        elif change == "state":
            _ = db.execute(
                "UPDATE import_publications SET state='prepared' WHERE id=?",
                (identifier,),
            )
        else:
            _ = db.execute("UPDATE import_queue SET download_id='different-identity'")
    inventory = capture_inventory(producer_media, monkeypatch)
    decisions = chosen(
        inventory,
        OwnerRef("publication", (identifier,)),
        "cancel-unmutated",
        {"source"},
    )
    with pytest.raises(InvalidIdentity):
        _ = recovery.build_recovery_plan(
            inventory, *review_documents(inventory, decisions)
        )


@contextmanager
def domain_external_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None, None, None]:
    import import_publication
    import import_commit
    import volume_file_deletion

    def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("new domain planner called a producer/external boundary")

    with trapped_external_boundaries(monkeypatch), monkeypatch.context() as patch:
        for module, names in (
            (
                import_publication,
                (
                    "get_db",
                    "create_publication",
                    "_complete_publication_unit",
                    "_discard_private",
                    "mark_publication_db_committed",
                ),
            ),
            (
                volume_file_deletion,
                (
                    "get_db",
                    "_complete_journal",
                    "_store_carrier",
                    "replay_volume_file_deletion",
                ),
            ),
            (import_commit, ("_commit_import",)),
        ):
            for name in names:
                patch.setattr(module, name, forbidden)
        yield


@pytest.mark.parametrize(
    "feature",
    ["publication_cancel", "publication_cleanup", "deletion_keep", "deletion_cleanup"],
)
@pytest.mark.parametrize("refused", [False, True])
def test_domain_valid_and_refused_plans_trap_real_boundaries_and_conserve_every_row(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    feature: str,
    refused: bool,
) -> None:
    if feature == "publication_cancel":
        identifier = domain_unallocated_nonready_publication(
            producer_media, monkeypatch
        )
        domain, action, purposes, destination = (
            "publication",
            "cancel-unmutated",
            {"source"},
            None,
        )
    elif feature == "publication_cleanup":
        identifier = domain_committed_publication(producer_media, monkeypatch)
        domain, action, purposes, destination = (
            "publication",
            "cleanup-committed",
            {"publication_artifact"},
            None,
        )
    elif feature == "deletion_keep":
        identifier = domain_unacknowledged_deletion(producer_media)
        domain, action, purposes, destination = (
            "deletion",
            "keep-current",
            {"target"},
            str(producer_media.source),
        )
    else:
        identifier = domain_completed_deletion(producer_media)
        domain, action, purposes, destination = (
            "deletion",
            "cleanup-committed",
            {"delete_artifact"},
            None,
        )
    if refused:
        with closing(sqlite3.connect(producer_media.db_path)) as db, db:
            if feature == "publication_cancel":
                _ = db.execute(
                    "UPDATE import_publications SET state='prepared' WHERE id=?",
                    (identifier,),
                )
            elif feature == "publication_cleanup":
                _ = db.execute("UPDATE volumes SET status='wanted' WHERE volume_num=2")
            elif feature == "deletion_keep":
                _ = db.execute(
                    "UPDATE volumes SET download_id='foreign-acquisition' WHERE id=1"
                )
            else:
                _ = db.execute(
                    "UPDATE volume_file_deletions SET state='active' WHERE id=?",
                    (identifier,),
                )
    inventory = capture_inventory(producer_media, monkeypatch)
    decisions = chosen(
        inventory, OwnerRef(domain, (identifier,)), action, purposes, destination
    )
    inputs = review_documents(inventory, decisions)
    from test_file_identity_inventory import all_rows

    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        rows = cast(object, all_rows(db))
    before = (
        inventory.to_json(),
        media_snapshot(producer_media.scope),
        control_snapshot(producer_media.db_path),
    )
    error: BaseException | None = None
    with domain_external_boundaries(monkeypatch):
        try:
            plan = recovery.build_recovery_plan(inventory, *inputs)
            assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan
        except BaseException as exc:
            error = exc
    assert isinstance(error, InvalidIdentity) if refused else error is None, error
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        assert cast(object, all_rows(db)) == rows
    assert (
        inventory.to_json(),
        media_snapshot(producer_media.scope),
        control_snapshot(producer_media.db_path),
    ) == before


def test_domain_publication_cancel_null_carriers_do_not_replace_private_inventory(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = domain_unallocated_nonready_publication(
        producer_media, monkeypatch, empty_namespaces=False
    )
    inventory = capture_inventory(producer_media, monkeypatch)
    decisions = chosen(
        inventory,
        OwnerRef("publication", (identifier,)),
        "cancel-unmutated",
        {"source"},
    )
    with pytest.raises(InvalidIdentity, match="ownership_phase"):
        _ = recovery.build_recovery_plan(
            inventory, *review_documents(inventory, decisions)
        )


def test_domain_missing_chapter_action_guard_does_not_change_generic_retention(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import file_identity_consumers as adapter

    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        _ = db.execute(
            "INSERT INTO chapters(id,series_id,volume_id,chapter_num,status,monitored) VALUES(81,7,1,1,'downloaded',1)"
        )
    identifier = domain_unacknowledged_deletion(producer_media)
    inventory = capture_inventory(producer_media, monkeypatch)
    raw: dict[str, list[adapter.RawRow]] = {table: [] for table in adapter._TABLES}
    for record in inventory.closure.raw_rows:
        if record.table == "chapters":
            record = replace(
                record,
                values=tuple(pair for pair in record.values if pair[0] != "monitored"),
            )
        raw[record.table].append(record)
    closure = adapter._bind_references(raw, inventory.parent, inventory.budget)
    assert closure.roles == inventory.closure.roles
    candidate = recovery.OwnershipInventory.create(
        inventory.parent, closure, inventory.observed, (), inventory.budget
    )
    _ = recovery.build_recovery_plan(candidate, *review_documents(candidate))
    decisions = chosen(
        candidate,
        OwnerRef("deletion", (identifier,)),
        "keep-current",
        {"target"},
        str(producer_media.source),
    )
    with pytest.raises(InvalidIdentity, match="ownership_context"):
        _ = recovery.build_recovery_plan(
            candidate, *review_documents(candidate, decisions)
        )


@pytest.mark.parametrize("remove_ready", [False, True])
def test_domain_publication_cleanup_missing_receipts_refuse_after_resigning(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    remove_ready: bool,
) -> None:
    identifier = domain_publication_discard_window(producer_media, monkeypatch)
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        row = cast(
            tuple[object, ...],
            db.execute(
                "SELECT queue_snapshot_json FROM import_publications WHERE id=?",
                (identifier,),
            ).fetchone(),
        )
        queue = parse_document(cast(str, row[0]))
        entry = wire_object(
            next(
                iter(
                    wire_object(
                        wire_object(queue["_file_publication"])["artifacts"]
                    ).values()
                )
            )
        )
        _ = entry.pop("publication_receipt")
        carrier = parse_document(cast(str, entry["publication"]))
        carrier["restore_receipt"] = None
        entry["publication"] = encoded(carrier)
        if remove_ready:
            _ = entry.pop("publication_ready")
        _ = db.execute(
            "UPDATE import_publications SET queue_snapshot_json=? WHERE id=?",
            (encoded(queue), identifier),
        )
    inventory = capture_inventory(producer_media, monkeypatch)
    decisions = chosen(
        inventory,
        OwnerRef("publication", (identifier,)),
        "cleanup-committed",
        {"publication_artifact"},
    )
    with pytest.raises(InvalidIdentity):
        _ = recovery.build_recovery_plan(
            inventory, *review_documents(inventory, decisions)
        )


def epoch_replacement_candidate(
    env: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> tuple[recovery.OwnershipInventory, tuple[str, str, str]]:
    """Captured unit candidate, NOT proof of replacement-allocation eligibility."""
    import file_identity_store as store
    import test_file_identity_ledger_followup as ledger
    from test_file_identity_store import activate, object_value
    from private_file_claim import ensure_namespace

    identifier = rescan_acknowledged_fixture(env, monkeypatch, False)
    eid = "9" * 32
    allocation = env.library / (".mangarr-epoch-" + eid)
    allocation.mkdir(mode=0o700)
    with file_mutation_guard(str(env.db_path)) as guard:
        with ensure_namespace(guard, str(allocation)):
            pass
    initial = capture_inventory(env, monkeypatch)
    namespace = OwnerRef("namespace", (str(allocation),))
    current = {o.spec.role: o for o in initial.observed if o.spec.owner == namespace}
    root = next(o for o in current.values() if o.spec.role.purpose == "namespace_root")
    marker = next(
        o for o in current.values() if o.spec.role.purpose == "namespace_marker"
    )
    assert root.native is not None and marker.native is not None
    legacy = root.spec.legacy_json
    assert legacy is not None
    namespace_proof = parse_document(legacy)
    birth = {
        "version": 1,
        "layout": "dedicated_parent",
        "enrollment": "birth",
        "legacy_namespace": None,
        "controlled_birth": {
            "authority_incarnation": "b" * 32,
            "allocation_token": namespace_proof["owner_token"],
            "root_native": root.native.to_value(),
            "marker_native": marker.native.to_value(),
            "receipt_sha256": hashlib.sha256(legacy.encode()).hexdigest(),
        },
    }
    # Existing canonical preparation is unit evidence, not Task8 qualification.
    with monkeypatch.context() as patch:
        with closing(sqlite3.connect(env.db_path)) as db, db:
            _ = db.execute("PRAGMA foreign_keys=ON")
            ledger.authority(db, env.scope, patch)
            _ = db.execute(
                "INSERT INTO file_identity_epochs VALUES(?,?,?,?,?,?)",
                (
                    eid,
                    "a" * 32,
                    str(env.library),
                    str(allocation),
                    root.spec.path,
                    encoded(birth),
                ),
            )
            db.commit()
            proof = ledger.proof_for_owner(db, namespace, "8" * 32, eid)
            objects: list[store.ObjectProof] = []
            for obj in proof.roles:
                observation = current[obj.role]
                native = observation.native
                assert native is not None
                constraints = parse_document(obj.constraints_json)
                constraints["legacy"] = {
                    "kind": "namespace_v2",
                    "json": legacy,
                    "sha256": hashlib.sha256(legacy.encode()).hexdigest(),
                }
                objects.append(
                    replace(obj, native=native, constraints_json=encoded(constraints))
                )
            manifest = parse_document(proof.manifest_json)
            manifest["roles"] = [object_value(obj) for obj in objects]
            proof = replace(
                proof, roles=tuple(objects), manifest_json=encoded(manifest)
            )
            _ = activate(db, proof)
            db.commit()
            active = store.read_active(db, namespace)
            assert active is not None and active.proof == proof
    inventory = capture_inventory(env, monkeypatch)
    assert {
        o.spec.role: o.native for o in inventory.observed if o.spec.owner == namespace
    } == {r: o.native for r, o in current.items()}
    owner = OwnerRef("rescan", (identifier,))
    decisions = chosen(
        inventory,
        owner,
        "restore-original",
        {"source_artifact", "destination"},
        str(env.source),
    )
    selection, declaration, receipt = (
        parse_document(d) for d in review_documents(inventory, decisions)
    )
    raw_authority = next(
        dict(r.values)
        for r in inventory.closure.raw_rows
        if r.table == "file_identity_authorities"
    )
    authority = wire_object(wire_array(declaration["authorities"])[0])
    authority["scope"] = parse_document(str(raw_authority["declaration_json"]))
    authority["profile"] = parse_document(str(raw_authority["profile_json"]))
    epoch = wire_object(selection["epoch_bindings"])
    epoch["provision"] = []
    epoch["existing"] = [
        dict(r.values)
        for r in inventory.closure.raw_rows
        if r.table == "file_identity_epochs"
    ]
    original = next(
        r
        for r in inventory.closure.roles
        if r.owner == owner and r.role.purpose == "source_artifact"
    )
    public = next(
        r
        for r in inventory.closure.roles
        if r.owner == owner and r.role.purpose == "destination"
    )
    epoch["rollback_outputs"] = [
        {
            "version": 1,
            "owner": recovery._owner_value(owner),
            "original_role": recovery._role_value(original.role),
            "publication_role": recovery._role_value(public.role),
            "epoch_id": eid,
            "carrier_token": "f" * 32,
            "handling": "retain-non-replayable",
        }
    ]
    for value in wire_array(declaration["role_reviews"]):
        wire_object(value)["authority_id"] = "a" * 32
    for value in wire_array(epoch["role_bindings"]):
        binding = wire_object(value)
        binding["authority_id"] = "a" * 32
        if binding["owner"] == recovery._owner_value(namespace):
            binding["epoch_id"] = eid
    return inventory, resign_documents(selection, declaration, receipt)


def test_epoch_registered_replacement_candidate_reaches_namespace_root_guard(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Location diagnostic, not an eligible-action assertion RED or qualification."""
    inventory, inputs = epoch_replacement_candidate(producer_media, monkeypatch)
    before_rows = captured_fixture(producer_media)[1]
    before_media = media_snapshot(producer_media.scope)
    before_control = control_snapshot(producer_media.db_path)
    with domain_external_boundaries(monkeypatch):
        with pytest.raises(InvalidIdentity, match="ownership_binding") as error:
            _ = recovery.build_recovery_plan(inventory, *inputs)
    traceback = error.value.__traceback__
    frames: list[dict[str, object]] = []
    while traceback is not None:
        if traceback.tb_frame.f_code.co_name == "_validate_rollback_outputs":
            frames.append(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next
    assert len(frames) == 1
    context = frames[0]
    assert context["root"] in cast(set[str], context["old_roots"])
    assert cast(set[str], context["required"]) <= cast(set[str], context["observed"])
    pair = cast(recovery._RollbackPair, context["pair"])
    assert context["origin"] == pair.publication.path.rsplit("/", 1)[0]
    assert context["captured"] is not None and context["intent"] is None
    assert captured_fixture(producer_media)[1] == before_rows
    assert media_snapshot(producer_media.scope) == before_media
    assert control_snapshot(producer_media.db_path) == before_control


@pytest.mark.parametrize("purpose", ["source_marker", "source_carrier"])
def test_epoch_rescan_cleanup_refuses_support_loss_with_retained_artifact(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, purpose: str
) -> None:
    identifier = producer_rescan(producer_media, "db_committed")
    inventory = capture_inventory(producer_media, monkeypatch)
    owner = OwnerRef("rescan", (identifier,))
    artifact = next(
        o
        for o in inventory.observed
        if o.spec.owner == owner and o.spec.role.purpose == "source_artifact"
    )
    assert artifact.outcome in {"observed", "native_unsupported"}
    decisions = chosen(inventory, owner, "cleanup-committed", {purpose})
    with pytest.raises(InvalidIdentity, match="ownership_phase"):
        _ = recovery.build_recovery_plan(
            inventory, *review_documents(inventory, decisions)
        )


@pytest.mark.parametrize(
    "purposes",
    [
        {"source_artifact"},
        {"source_artifact", "source_marker"},
        {"source_artifact", "source_marker", "source_carrier"},
    ],
)
def test_epoch_rescan_cleanup_valid_private_selections_are_pure_and_conserved(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, purposes: set[str]
) -> None:
    identifier = producer_rescan(producer_media, "db_committed")
    inventory = capture_inventory(producer_media, monkeypatch)
    decisions = chosen(
        inventory, OwnerRef("rescan", (identifier,)), "cleanup-committed", purposes
    )
    inputs = review_documents(inventory, decisions)
    before_rows = captured_fixture(producer_media)[1]
    before_media = media_snapshot(producer_media.scope)
    before_control = control_snapshot(producer_media.db_path)
    before_inventory = inventory.to_json()
    with domain_external_boundaries(monkeypatch):
        plan = recovery.build_recovery_plan(inventory, *inputs)
        assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan
        assert replace(plan) == plan
        assert (
            recovery.build_recovery_plan(inventory, *inputs).to_json() == plan.to_json()
        )
    assert inventory.to_json() == before_inventory
    assert captured_fixture(producer_media)[1] == before_rows
    assert media_snapshot(producer_media.scope) == before_media
    assert control_snapshot(producer_media.db_path) == before_control


@pytest.mark.parametrize(
    "retain",
    [{"source_artifact"}, {"source_marker"}, {"source_artifact", "source_marker"}],
)
def test_epoch_rescan_cleanup_resigned_support_loss_refuses_builder_constructor_parser(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, retain: set[str]
) -> None:
    identifier = producer_rescan(producer_media, "db_committed")
    inventory = capture_inventory(producer_media, monkeypatch)
    owner = OwnerRef("rescan", (identifier,))
    decisions = chosen(
        inventory,
        owner,
        "cleanup-committed",
        {"source_artifact", "source_marker", "source_carrier"},
    )
    inputs = review_documents(inventory, decisions)
    valid = recovery.build_recovery_plan(inventory, *inputs)
    selection, declaration, receipt = (parse_document(d) for d in inputs)
    decision = next(
        wire_object(d)
        for d in wire_array(selection["decisions"])
        if wire_object(d)["action"] == "cleanup-committed"
    )
    subjects, retained = (
        wire_array(decision["roles"]),
        wire_array(decision["retained_roles"]),
    )
    moved = [r for r in subjects if wire_object(r)["purpose"] in retain]
    subjects[:] = [r for r in subjects if r not in moved]
    retained.extend(moved)
    retained.sort(key=encoded)
    for value in wire_array(declaration["role_reviews"]):
        review = wire_object(value)
        if review["owner"] == recovery._owner_value(owner) and review["role"] in moved:
            review["assertion"] = "retain-only"
    changed_inputs = resign_documents(selection, declaration, receipt)
    wire = parse_document(valid.to_json())
    wire["decisions"] = selection["decisions"]
    wire["declaration_json"], wire["backup_receipt_json"] = changed_inputs[1:]
    wire["sha256"] = digest({k: v for k, v in wire.items() if k != "sha256"})
    changed_decisions = tuple(
        recovery.RecoveryDecision.from_value(d)
        for d in wire_array(selection["decisions"])
    )
    before_rows = captured_fixture(producer_media)[1]
    before_media = media_snapshot(producer_media.scope)
    before_control = control_snapshot(producer_media.db_path)
    with domain_external_boundaries(monkeypatch):
        with pytest.raises(InvalidIdentity, match="ownership_phase"):
            _ = recovery.build_recovery_plan(inventory, *changed_inputs)
        with pytest.raises(InvalidIdentity, match="ownership_phase"):
            _ = recovery.RecoveryPlan.from_json(encoded(wire))
        with pytest.raises(InvalidIdentity, match="ownership_phase"):
            _ = replace(
                valid,
                decisions=changed_decisions,
                declaration_json=changed_inputs[1],
                backup_receipt_json=changed_inputs[2],
                sha256=wire["sha256"],
            )
    assert captured_fixture(producer_media)[1] == before_rows
    assert media_snapshot(producer_media.scope) == before_media
    assert control_snapshot(producer_media.db_path) == before_control


def retref_queue_reference(
    env: ProducerMedia, path: str, *, directory: bool = False
) -> OwnerRef:
    """A real queue with one explicit supported legacy reference-column edit."""
    identifier = producer_queue(env)
    with closing(sqlite3.connect(env.db_path)) as db, db:
        if directory:
            changed = db.execute(
                "UPDATE import_queue SET src_dir=? WHERE id=?", (path, identifier)
            )
        else:
            changed = db.execute(
                "UPDATE import_queue_files SET src_path=? WHERE queue_id=?",
                (path, identifier),
            )
        assert changed.rowcount == 1
    return OwnerRef("queue_source", (identifier,))


def test_retref_rescan_cleanup_preserves_other_owner_retained_source(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    identifier = producer_rescan(producer_media, "db_committed")
    before = capture_inventory(producer_media, monkeypatch)
    owner = OwnerRef("rescan", (identifier,))
    original = next(
        o
        for o in before.observed
        if o.spec.owner == owner and o.spec.role.purpose == "source_artifact"
    )
    queue = retref_queue_reference(producer_media, original.spec.path)
    inventory = capture_inventory(producer_media, monkeypatch)
    reference = next(
        o
        for o in inventory.observed
        if o.spec.owner == queue and o.spec.role.purpose == "source"
    )
    assert reference.outcome == "observed" and reference.spec.path == original.spec.path
    decisions = chosen(inventory, owner, "cleanup-committed", {"source_artifact"})
    assert (
        reference.spec.role
        in next(d for d in decisions if d.owner == queue).retained_roles
    )
    with pytest.raises(InvalidIdentity, match="ownership_phase"):
        _ = recovery.build_recovery_plan(
            inventory, *review_documents(inventory, decisions)
        )


def test_retref_deletion_resume_preserves_other_owner_retained_source(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    identifier = producer_deletion(producer_media, "claimed")
    owner = OwnerRef("deletion", (identifier,))
    before = capture_inventory(producer_media, monkeypatch)
    original = next(
        o
        for o in before.observed
        if o.spec.owner == owner and o.spec.role.purpose == "delete_artifact"
    )
    _ = retref_queue_reference(producer_media, original.spec.path)
    inventory = capture_inventory(producer_media, monkeypatch)
    decisions = chosen(inventory, owner, "resume-commit", {"delete_artifact"})
    with pytest.raises(InvalidIdentity, match="ownership_phase"):
        _ = recovery.build_recovery_plan(
            inventory, *review_documents(inventory, decisions)
        )


@pytest.mark.parametrize("reference", ["destination", "original"])
def test_retref_deletion_restore_distinguishes_created_name_from_retained_original(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, reference: str
) -> None:
    identifier = producer_deletion(producer_media, "claimed")
    owner = OwnerRef("deletion", (identifier,))
    before = capture_inventory(producer_media, monkeypatch)
    original = next(
        o
        for o in before.observed
        if o.spec.owner == owner and o.spec.role.purpose == "delete_artifact"
    )
    path = (
        str(producer_media.source) if reference == "destination" else original.spec.path
    )
    queue = retref_queue_reference(producer_media, path)
    inventory = capture_inventory(producer_media, monkeypatch)
    observed = next(
        o
        for o in inventory.observed
        if o.spec.owner == queue and o.spec.role.purpose == "source"
    )
    assert observed.outcome == ("absent" if reference == "destination" else "observed")
    decisions = chosen(
        inventory,
        owner,
        "restore-original",
        {"delete_artifact"},
        str(producer_media.source),
    )
    if reference == "destination":
        with pytest.raises(InvalidIdentity, match="ownership_phase"):
            _ = recovery.build_recovery_plan(
                inventory, *review_documents(inventory, decisions)
            )
    else:
        plan = recovery.build_recovery_plan(
            inventory, *review_documents(inventory, decisions)
        )
        assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan


def retref_enqueue_archive(env: ProducerMedia, path: str) -> int | None:
    import import_queue
    from typing import Protocol

    class Enqueue(Protocol):
        def __call__(
            self,
            db: sqlite3.Connection,
            series_id: int,
            download_id: str,
            torrent_name: str,
            torrent_url: str,
            volume_num: float | None,
            content_path: str,
            *,
            download_client_id: int | None = None,
            protocol: str | None = None,
            respect_grab_claims: bool | None = None,
        ) -> tuple[int | None, bool]: ...

    # The existing producer's db parameter is unannotated; bind this exact call.
    enqueue = cast(Enqueue, getattr(import_queue, "_queue_import"))

    with closing(sqlite3.connect(env.db_path)) as db, db:
        db.row_factory = sqlite3.Row
        identifier, _ = enqueue(
            db,
            7,
            "retref-isolated",
            "Matrix v02",
            "magnet:retref-isolated",
            2,
            path,
            download_client_id=1,
            protocol="torrent",
            respect_grab_claims=False,
        )
        assert identifier is None or type(identifier) is int
        return identifier


def test_retref_enqueue_api_does_not_claim_extensionless_artifact_is_archive(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    identifier = producer_rescan(producer_media, "db_committed")
    inventory = capture_inventory(producer_media, monkeypatch)
    original = next(
        o
        for o in inventory.observed
        if o.spec.owner == OwnerRef("rescan", (identifier,))
        and o.spec.role.purpose == "source_artifact"
    )
    assert retref_enqueue_archive(producer_media, original.spec.path) is None
    # The conflict fixtures explicitly seed a supported legacy path column;
    # they never claim the normal extension-filtered enqueue API produced it.


@pytest.mark.parametrize("kind", ["marker", "carrier", "descendant"])
def test_retref_cleanup_exact_marker_container_and_retained_descendant_refuse(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    identifier = producer_rescan(producer_media, "db_committed")
    owner = OwnerRef("rescan", (identifier,))
    before = capture_inventory(producer_media, monkeypatch)
    purpose = "source_" + (
        "marker" if kind == "marker" else "carrier" if kind == "carrier" else "artifact"
    )
    reference = next(
        o
        for o in before.observed
        if o.spec.owner == owner and o.spec.role.purpose == purpose
    )
    _ = retref_queue_reference(
        producer_media, reference.spec.path, directory=kind == "carrier"
    )
    inventory = capture_inventory(producer_media, monkeypatch)
    subjects = (
        {"source_marker"}
        if kind == "marker"
        else {"source_artifact", "source_marker", "source_carrier"}
    )
    decisions = chosen(inventory, owner, "cleanup-committed", subjects)
    with pytest.raises(InvalidIdentity, match="ownership_phase"):
        _ = recovery.build_recovery_plan(
            inventory, *review_documents(inventory, decisions)
        )


def test_retref_actual_hardlink_publication_cleanup_preserves_distinct_queued_name(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    identifier = producer_rescan(producer_media, "db_committed")
    queue_id = retref_enqueue_archive(
        producer_media, str(producer_media.library / "converted.cbz")
    )
    assert queue_id is not None
    inventory = capture_inventory(producer_media, monkeypatch)
    owner = OwnerRef("rescan", (identifier,))
    private = next(
        o
        for o in inventory.observed
        if o.spec.owner == owner and o.spec.role.purpose == "publication_artifact"
    )
    queued = next(
        o
        for o in inventory.observed
        if o.spec.owner == OwnerRef("queue_source", (queue_id,))
        and o.spec.role.purpose == "source"
    )
    assert private.spec.path != queued.spec.path and private.native == queued.native
    left, right = (
        wire_object(parse_document(o.constraints_json)["current"])
        for o in (private, queued)
    )
    assert (left["dev"], left["inode"]) == (right["dev"], right["inode"])
    assert Path(private.spec.path).stat().st_nlink >= 2
    inputs = review_documents(
        inventory,
        chosen(inventory, owner, "cleanup-committed", {"publication_artifact"}),
    )
    before_rows = captured_fixture(producer_media)[1]
    before_media, before_control = (
        media_snapshot(producer_media.scope),
        control_snapshot(producer_media.db_path),
    )
    with domain_external_boundaries(monkeypatch):
        plan = recovery.build_recovery_plan(inventory, *inputs)
        assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan
        assert replace(plan) == plan
    assert captured_fixture(producer_media)[1] == before_rows
    assert media_snapshot(producer_media.scope) == before_media
    assert control_snapshot(producer_media.db_path) == before_control


@pytest.mark.parametrize(
    "action", ["retain-blocked", "cancel-unmutated", "keep-current", "resume-commit"]
)
def test_retref_selected_readonly_subjects_do_not_become_deletions(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    if action == "keep-current":
        identifier = domain_unacknowledged_deletion(producer_media)
        owner, purpose = OwnerRef("deletion", (identifier,)), "target"
    else:
        identifier = producer_rescan(
            producer_media, "prepared" if action == "cancel-unmutated" else "published"
        )
        owner, purpose = (
            OwnerRef("rescan", (identifier,)),
            "source" if action == "cancel-unmutated" else "destination",
        )
    if action == "cancel-unmutated":
        from private_file_claim import ensure_namespace

        with file_mutation_guard(str(producer_media.db_path)) as guard:
            with ensure_namespace(guard, str(producer_media.library)):
                pass
    first = capture_inventory(producer_media, monkeypatch)
    reference = next(
        o
        for o in first.observed
        if o.spec.owner == owner and o.spec.role.purpose == purpose
    )
    _ = retref_queue_reference(producer_media, reference.spec.path)
    inventory = capture_inventory(producer_media, monkeypatch)
    decisions = (
        None
        if action == "retain-blocked"
        else chosen(
            inventory,
            owner,
            action,
            {purpose},
            reference.spec.path if action == "keep-current" else None,
        )
    )
    plan = recovery.build_recovery_plan(
        inventory, *review_documents(inventory, decisions)
    )
    assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan


def test_retref_namespace_support_owner_has_no_cleanup_exemption(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    from private_file_claim import ensure_namespace

    identifier = producer_rescan(producer_media, "db_committed")
    owner = OwnerRef("rescan", (identifier,))
    first = capture_inventory(producer_media, monkeypatch)
    carrier = next(
        o
        for o in first.observed
        if o.spec.owner == owner and o.spec.role.purpose == "source_carrier"
    )
    with file_mutation_guard(str(producer_media.db_path)) as guard:
        with ensure_namespace(guard, carrier.spec.path):
            pass
    inventory = capture_inventory(producer_media, monkeypatch)
    namespace = OwnerRef("namespace", (carrier.spec.path,))
    assert any(
        o.spec.owner == namespace
        and o.spec.path == carrier.spec.path
        and o.outcome == "observed"
        for o in inventory.observed
    )
    decisions = chosen(
        inventory,
        owner,
        "cleanup-committed",
        {"source_artifact", "source_marker", "source_carrier"},
    )
    with pytest.raises(InvalidIdentity, match="ownership_phase"):
        _ = recovery.build_recovery_plan(
            inventory, *review_documents(inventory, decisions)
        )


def test_retref_discarded_deletion_absence_is_not_a_consumed_file(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    identifier = producer_deletion(producer_media, "discarded")
    owner = OwnerRef("deletion", (identifier,))
    first = capture_inventory(producer_media, monkeypatch)
    original = next(
        o
        for o in first.observed
        if o.spec.owner == owner and o.spec.role.purpose == "delete_artifact"
    )
    assert original.outcome == "absent"
    _ = retref_queue_reference(producer_media, original.spec.path)
    inventory = capture_inventory(producer_media, monkeypatch)
    inputs = review_documents(
        inventory, chosen(inventory, owner, "resume-commit", {"delete_artifact"})
    )
    plan = recovery.build_recovery_plan(inventory, *inputs)
    assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan


@pytest.mark.parametrize(
    "subject", ["source_artifact", "source_marker", "source_carrier"]
)
def test_retref_resigned_cleanup_refusals_bind_all_owners_and_conserve(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, subject: str
) -> None:
    identifier = producer_rescan(producer_media, "db_committed")
    owner = OwnerRef("rescan", (identifier,))
    first = capture_inventory(producer_media, monkeypatch)
    original = next(
        o
        for o in first.observed
        if o.spec.owner == owner and o.spec.role.purpose == "source_artifact"
    )
    queue = retref_queue_reference(producer_media, original.spec.path)
    inventory = capture_inventory(producer_media, monkeypatch)
    good = chosen(inventory, owner, "cleanup-committed", {"stage_artifact"})
    inputs = review_documents(inventory, good)
    valid = recovery.build_recovery_plan(inventory, *inputs)
    bad = chosen(
        inventory,
        owner,
        "cleanup-committed",
        {"source_artifact", "source_marker", "source_carrier"}
        if subject == "source_carrier"
        else {subject},
    )
    # Marker-only first uses the existing per-owner retention refusal; the
    # artifact/coherent carrier variants exercise the new cross-owner guard.
    selection, declaration, receipt = (parse_document(d) for d in inputs)
    selection["decisions"] = [d.to_value() for d in bad]
    selected = {(d.owner, r) for d in bad for r in d.roles}
    for value in wire_array(declaration["role_reviews"]):
        review = wire_object(value)
        identity = (
            recovery._owner_from(review["owner"]),
            recovery._role_from(review["role"]),
        )
        review["assertion"] = (
            "reviewed-recovery-subject" if identity in selected else "retain-only"
        )
        if identity in selected:
            review["authority_id"] = "a" * 32
    for value in wire_array(wire_object(selection["epoch_bindings"])["role_bindings"]):
        binding = wire_object(value)
        if (
            recovery._owner_from(binding["owner"]),
            recovery._role_from(binding["role"]),
        ) in selected:
            binding["authority_id"] = "a" * 32
    for value in wire_array(receipt["media"]):
        medium = wire_object(value)
        if (
            recovery._owner_from(medium["owner"]),
            recovery._role_from(medium["role"]),
        ) in selected:
            medium["handling"], medium["recovery_reference"] = (
                "snapshot",
                "unit-reviewed-media-snapshot",
            )
    changed = resign_documents(selection, declaration, receipt)
    wire = parse_document(valid.to_json())
    wire.update(
        decisions=selection["decisions"],
        declaration_json=changed[1],
        backup_receipt_json=changed[2],
        epoch_bindings_json=encoded(selection["epoch_bindings"]),
    )
    wire["sha256"] = digest({k: v for k, v in wire.items() if k != "sha256"})
    assert next(d for d in bad if d.owner == queue) == next(
        d for d in good if d.owner == queue
    )
    before_rows = captured_fixture(producer_media)[1]
    before_media, before_control = (
        media_snapshot(producer_media.scope),
        control_snapshot(producer_media.db_path),
    )
    before_inventory = inventory.to_json()
    with domain_external_boundaries(monkeypatch):
        assert recovery.build_recovery_plan(inventory, *inputs) == valid
        assert recovery.RecoveryPlan.from_json(valid.to_json()) == valid
        with pytest.raises(InvalidIdentity, match="ownership_phase") as error:
            _ = recovery.build_recovery_plan(inventory, *changed)
        if subject != "source_marker":
            traceback = error.value.__traceback__
            functions: list[str] = []
            while traceback is not None:
                functions.append(traceback.tb_frame.f_code.co_name)
                traceback = traceback.tb_next
            assert "_validate_retained_references" in functions
        with pytest.raises(InvalidIdentity, match="ownership_phase"):
            _ = recovery.RecoveryPlan.from_json(encoded(wire))
        with pytest.raises(InvalidIdentity, match="ownership_phase"):
            _ = replace(
                valid,
                decisions=bad,
                declaration_json=changed[1],
                backup_receipt_json=changed[2],
                epoch_bindings_json=encoded(selection["epoch_bindings"]),
                sha256=wire["sha256"],
            )
    assert inventory.to_json() == before_inventory
    assert captured_fixture(producer_media)[1] == before_rows
    assert media_snapshot(producer_media.scope) == before_media
    assert control_snapshot(producer_media.db_path) == before_control


@pytest.mark.parametrize("action", ["resume-commit", "restore-original"])
@pytest.mark.parametrize("conflict", [False, True])
def test_retref_deletion_effects_are_pure_and_conserved(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    conflict: bool,
) -> None:
    identifier = producer_deletion(producer_media, "claimed")
    owner = OwnerRef("deletion", (identifier,))
    first = capture_inventory(producer_media, monkeypatch)
    original = next(
        o
        for o in first.observed
        if o.spec.owner == owner and o.spec.role.purpose == "delete_artifact"
    )
    if action == "resume-commit":
        reference = original.spec.path if conflict else str(producer_media.incoming)
    else:
        reference = str(producer_media.source) if conflict else original.spec.path
    _ = retref_queue_reference(
        producer_media, reference, directory=action == "resume-commit" and not conflict
    )
    inventory = capture_inventory(producer_media, monkeypatch)
    decisions = chosen(
        inventory,
        owner,
        action,
        {"delete_artifact"},
        str(producer_media.source) if action == "restore-original" else None,
    )
    inputs = review_documents(inventory, decisions)
    selection = parse_document(inputs[0])
    wire: dict[str, object] = {
        "version": 1,
        "inventory": parse_document(inventory.to_json()),
        "decisions": [d.to_value() for d in decisions],
        "declaration_json": inputs[1],
        "backup_receipt_json": inputs[2],
        "epoch_bindings_json": encoded(selection["epoch_bindings"]),
    }
    sha = digest(wire)
    wire["sha256"] = sha
    before_rows = captured_fixture(producer_media)[1]
    before_media, before_control = (
        media_snapshot(producer_media.scope),
        control_snapshot(producer_media.db_path),
    )
    before_inventory = inventory.to_json()
    with domain_external_boundaries(monkeypatch):
        if conflict:
            with pytest.raises(InvalidIdentity, match="ownership_phase"):
                _ = recovery.build_recovery_plan(inventory, *inputs)
            with pytest.raises(InvalidIdentity, match="ownership_phase"):
                _ = recovery.RecoveryPlan.from_json(encoded(wire))
            with pytest.raises(InvalidIdentity, match="ownership_phase"):
                _ = recovery.RecoveryPlan(
                    1,
                    inventory,
                    decisions,
                    inputs[1],
                    inputs[2],
                    encoded(selection["epoch_bindings"]),
                    sha,
                )
        else:
            plan = recovery.build_recovery_plan(inventory, *inputs)
            assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan
            assert replace(plan) == plan
    assert len(inventory.closure.snapshots) == len(decisions)
    assert inventory.to_json() == before_inventory
    assert captured_fixture(producer_media)[1] == before_rows
    assert media_snapshot(producer_media.scope) == before_media
    assert control_snapshot(producer_media.db_path) == before_control


def test_retref_publication_restore_retained_absent_queue_destination_is_explicit(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    identifier = acknowledged_publication(producer_media, monkeypatch, "original")
    inventory = capture_inventory(producer_media, monkeypatch)
    owner = OwnerRef("publication", (identifier,))
    destination = str(producer_media.library / "Matrix v02.cbz")
    aliases = [
        o
        for o in inventory.observed
        if o.spec.owner != owner and o.spec.path == destination
    ]
    assert len(aliases) == 1
    assert (
        aliases[0].spec.owner.domain == "queue_source"
        and aliases[0].spec.role.purpose == "destination"
    )
    assert aliases[0].outcome == "absent"
    decisions = chosen(
        inventory, owner, "restore-original", {"original_artifact"}, destination
    )
    assert (
        aliases[0].spec.role
        in next(d for d in decisions if d.owner == aliases[0].spec.owner).retained_roles
    )
    inputs = review_documents(inventory, decisions)
    with domain_external_boundaries(monkeypatch):
        with pytest.raises(InvalidIdentity, match="ownership_phase") as error:
            _ = recovery.build_recovery_plan(inventory, *inputs)
    traceback = error.value.__traceback__
    functions: list[str] = []
    while traceback is not None:
        functions.append(traceback.tb_frame.f_code.co_name)
        traceback = traceback.tb_next
    assert "_validate_retained_references" in functions


def pack_queue_decisions(
    inventory: recovery.OwnershipInventory, identity_key: str
) -> tuple[recovery.RecoveryDecision, ...]:
    from private_pack_claim import PackOwnership

    owner = OwnerRef("pack_reservation", (identity_key,))
    rows = [
        dict(r.values)
        for r in inventory.closure.raw_rows
        if r.table == "import_pack_cleanup_reservations"
        and dict(r.values)["download_identity_key"] == identity_key
    ]
    assert len(rows) == 1
    ownership = PackOwnership.from_json(cast(str, rows[0]["directory_ownership_json"]))
    proof, entries = ownership.canonical_directory, ownership.inventory
    assert proof is not None and entries is not None
    roles = [r for r in inventory.closure.roles if r.owner == owner]
    root = next(r for r in roles if r.role.purpose == "pack_canonical")
    assert root.path == proof.path and root.role.kind == "directory"
    expected = {proof.path: "directory"}
    expected.update({proof.path + "/" + p: "directory" for p in entries.directories})
    expected.update({proof.path + "/" + p: "file" for p in entries.files})
    subjects = {root.role}
    for path, kind in expected.items():
        if path == proof.path:
            continue
        matches = [r for r in roles if r.parent_role == root.role and r.path == path]
        assert len(matches) == 1 and matches[0].role.kind == kind
        subjects.add(matches[0].role)
    return tuple(
        recovery.RecoveryDecision(
            s.owner,
            "resume-commit" if s.owner == owner else "retain-blocked",
            tuple(
                r.role
                for r in inventory.closure.roles
                if r.owner == s.owner and r.role in subjects
            )
            if s.owner == owner
            else (),
            None,
            tuple(
                r.role
                for r in inventory.closure.roles
                if r.owner == s.owner and (s.owner != owner or r.role not in subjects)
            ),
        )
        for s in inventory.closure.snapshots
    )


@contextmanager
def pack_queue_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None, None, None]:
    import import_pack_cleanup as packs
    import private_pack_source as sources
    import import_queue

    def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("pure pack planner called runtime authority or a mutator")

    with domain_external_boundaries(monkeypatch), monkeypatch.context() as patch:
        for module, names in (
            (
                packs,
                (
                    "get_db",
                    "_read_reservation",
                    "_sql_queue_authority",
                    "_queue_receipt",
                    "_finish_generated_pack_queue",
                    "_commit_generated_pack_queue",
                    "_save_ownership",
                    "_capture_pack_directory",
                    "_discard_reservation_claims",
                ),
            ),
            (sources, ("get_db", "_current", "_freeze_pack_file_origin")),
            (import_queue, ("_has_terminal_download_receipt",)),
        ):
            for name in names:
                patch.setattr(module, name, forbidden)
        yield


def test_pack_queue_recorded_generation_resumes_without_new_rows_or_receipts(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    import import_pack_cleanup as packs
    from private_pack_claim import PackOwnership

    identity_key, queue_id = producer_pack(producer_media, "queued")
    assert queue_id is not None
    inventory = capture_inventory(producer_media, monkeypatch)
    decisions = pack_queue_decisions(inventory, identity_key)
    row = next(
        dict(r.values)
        for r in inventory.closure.raw_rows
        if r.table == "import_pack_cleanup_reservations"
    )
    ownership = PackOwnership.from_json(cast(str, row["directory_ownership_json"]))
    assert ownership.version == 3 and ownership.phase == "queued"
    assert row["purpose"] == "cleanup" and row["queue_id"] == queue_id
    with closing(sqlite3.connect(producer_media.db_path)) as db:
        db.row_factory = sqlite3.Row
        assert packs._queue_receipt(db, queue_id) == ownership.queue_receipt_sha256
    inputs = review_documents(inventory, decisions)
    before_rows = captured_fixture(producer_media)[1]
    before_media = media_snapshot(producer_media.scope)
    before_control = control_snapshot(producer_media.db_path)
    before_wire = inventory.to_json()
    with pack_queue_boundaries(monkeypatch):
        plan = recovery.build_recovery_plan(inventory, *inputs)
        assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan
        assert replace(plan) == plan
    assert plan.decisions == decisions
    assert len(decisions) == len(inventory.closure.snapshots)
    assert sum(len(d.roles) + len(d.retained_roles) for d in decisions) == len(
        inventory.closure.roles
    )
    assert inventory.to_json() == before_wire
    assert captured_fixture(producer_media)[1] == before_rows
    assert media_snapshot(producer_media.scope) == before_media
    assert control_snapshot(producer_media.db_path) == before_control


def pack_queue_refusal(
    env: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    inventory: recovery.OwnershipInventory,
    decisions: tuple[recovery.RecoveryDecision, ...],
) -> None:
    inputs = review_documents(inventory, decisions)
    selection = parse_document(inputs[0])
    wire: dict[str, object] = {
        "version": 1,
        "inventory": parse_document(inventory.to_json()),
        "decisions": [d.to_value() for d in decisions],
        "declaration_json": inputs[1],
        "backup_receipt_json": inputs[2],
        "epoch_bindings_json": encoded(selection["epoch_bindings"]),
    }
    sha = digest(wire)
    wire["sha256"] = sha
    rows, media, control = (
        captured_fixture(env)[1],
        media_snapshot(env.scope),
        control_snapshot(env.db_path),
    )
    original = inventory.to_json()
    with pack_queue_boundaries(monkeypatch):
        with pytest.raises(InvalidIdentity, match="ownership_phase|ownership_context"):
            _ = recovery.build_recovery_plan(inventory, *inputs)
        with pytest.raises(InvalidIdentity, match="ownership_phase|ownership_context"):
            _ = recovery.RecoveryPlan.from_json(encoded(wire))
        with pytest.raises(InvalidIdentity, match="ownership_phase|ownership_context"):
            _ = recovery.RecoveryPlan(
                1,
                inventory,
                decisions,
                inputs[1],
                inputs[2],
                encoded(selection["epoch_bindings"]),
                sha,
            )
    assert inventory.to_json() == original
    assert captured_fixture(env)[1] == rows
    assert media_snapshot(env.scope) == media
    assert control_snapshot(env.db_path) == control


@pytest.mark.parametrize(
    "drift",
    [
        "legacy_receiptless",
        "changed_receipt",
        "queue_row",
        "file_row",
        "executing",
        "lease_owner",
        "lease_expiry",
        "failed_at",
        "queue_identity",
        "reservation_publication",
    ],
)
def test_pack_queue_pending_receipt_and_execution_context_are_independent(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    import import_pack_cleanup as packs

    identity_key, queue_id = producer_pack(producer_media, "queued")
    assert queue_id is not None
    publication_id = None
    if drift == "reservation_publication":
        other_queue = producer_queue(producer_media)
        publication_id = producer_publication(producer_media, other_queue, "staging")
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        db.row_factory = sqlite3.Row
        row = cast(
            sqlite3.Row | None,
            db.execute(
                "SELECT * FROM import_pack_cleanup_reservations WHERE download_identity_key=?",
                (identity_key,),
            ).fetchone(),
        )
        assert row is not None
        ownership = parse_document(cast(str, row["directory_ownership_json"]))
        if drift == "legacy_receiptless":
            ownership["version"] = 2
            del ownership["queue_receipt_sha256"]
        elif drift == "changed_receipt":
            ownership["queue_receipt_sha256"] = "0" * 64
        elif drift == "queue_row":
            _ = db.execute(
                "UPDATE import_queue SET torrent_name='changed frozen title' WHERE id=?",
                (queue_id,),
            )
        elif drift == "file_row":
            _ = db.execute(
                "UPDATE import_queue_files SET file_type='changed' WHERE queue_id=?",
                (queue_id,),
            )
        elif drift == "reservation_publication":
            _ = db.execute(
                "UPDATE import_pack_cleanup_reservations SET publication_id=? WHERE download_identity_key=?",
                (publication_id, identity_key),
            )
        else:
            sql = {
                "executing": "UPDATE import_queue SET status='importing' WHERE id=?",
                "lease_owner": "UPDATE import_queue SET lease_owner='changed' WHERE id=?",
                "lease_expiry": "UPDATE import_queue SET lease_expires_at='2099-01-01' WHERE id=?",
                "failed_at": "UPDATE import_queue SET failed_at='2099-01-01' WHERE id=?",
                "queue_identity": "UPDATE import_queue SET download_id='other-generation' WHERE id=?",
            }[drift]
            _ = db.execute(sql, (queue_id,))
            # An adversarial, self-consistent hash must not replace the separate
            # pending execution/identity predicates. This is never a positive receipt.
            ownership["queue_receipt_sha256"] = packs._queue_receipt(db, queue_id)
        _ = db.execute(
            "UPDATE import_pack_cleanup_reservations SET directory_ownership_json=? WHERE download_identity_key=?",
            (encoded(ownership), identity_key),
        )
    if drift == "queue_identity":
        from file_identity import IdentityMismatch

        with pytest.raises(
            IdentityMismatch, match="pack queue download identity differs"
        ):
            _ = captured_fixture(producer_media)
        return
    inventory = capture_inventory(producer_media, monkeypatch)
    decisions = pack_queue_decisions(inventory, identity_key)
    pack_queue_refusal(producer_media, monkeypatch, inventory, decisions)


@pytest.mark.parametrize(
    "drift",
    [
        "partial",
        "support_selected",
        "file_missing",
        "file_changed",
        "pack_marker_missing",
        "pack_marker_changed",
        "carrier_marker_missing",
        "carrier_marker_changed",
        "logical_occupied",
        "generation",
        "namespace_marker_missing",
        "namespace_marker_changed",
        "placement_phase",
        "private_leftover",
    ],
)
def test_pack_queue_whole_generation_and_current_original_support(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    from private_pack_claim import PackOwnership

    identity_key, _ = producer_pack(producer_media, "queued")
    initial = capture_inventory(producer_media, monkeypatch)
    decisions = pack_queue_decisions(initial, identity_key)
    owner = OwnerRef("pack_reservation", (identity_key,))

    def role(purpose: str) -> recovery.ObservedRole:
        return next(
            o
            for o in initial.observed
            if o.spec.owner == owner and o.spec.role.purpose == purpose
        )

    if drift in {"partial", "support_selected"}:
        decision = next(d for d in decisions if d.owner == owner)
        if drift == "partial":
            subject = role("pack_canonical_file").spec.role
            selected = set(decision.roles) - {subject}
        else:
            selected = set(decision.roles) | {role("pack_canonical_marker").spec.role}
        decisions = tuple(
            replace(
                d,
                roles=tuple(
                    r.role
                    for r in initial.closure.roles
                    if r.owner == owner and r.role in selected
                ),
                retained_roles=tuple(
                    r.role
                    for r in initial.closure.roles
                    if r.owner == owner and r.role not in selected
                ),
            )
            if d.owner == owner
            else d
            for d in decisions
        )
        pack_queue_refusal(producer_media, monkeypatch, initial, decisions)
        return
    if drift in {
        "file_missing",
        "file_changed",
        "pack_marker_missing",
        "pack_marker_changed",
        "carrier_marker_missing",
        "carrier_marker_changed",
    }:
        purpose = (
            "pack_canonical_file"
            if drift.startswith("file_")
            else "pack_canonical_marker"
            if drift.startswith("pack_")
            else "pack_placement_marker"
        )
        path = Path(role(purpose).spec.path)
        if drift.endswith("missing"):
            path.unlink()
        else:
            _ = path.write_bytes(b"changed current original proof")
    elif drift == "logical_occupied":
        Path(role("pack_logical").spec.path).mkdir()
    elif drift in {"namespace_marker_missing", "namespace_marker_changed"}:
        path = Path(
            next(
                o.spec.path
                for o in initial.observed
                if o.spec.role.purpose == "namespace_marker"
            )
        )
        if drift.endswith("missing"):
            path.unlink()
        else:
            _ = path.write_bytes(b"changed namespace original support")
    else:
        with closing(sqlite3.connect(producer_media.db_path)) as db, db:
            row = cast(
                tuple[str] | None,
                db.execute(
                    "SELECT directory_ownership_json FROM import_pack_cleanup_reservations WHERE download_identity_key=?",
                    (identity_key,),
                ).fetchone(),
            )
            assert row is not None
            ownership = PackOwnership.from_json(row[0])
            placement = ownership.placement_carrier
            assert placement is not None
            token = ownership.artifact_owner_token + "-different-generation"
            changed = replace(
                ownership,
                artifact_owner_token=token,
                placement_carrier=replace(
                    placement,
                    binding=replace(placement.binding, operation_key=token),
                    origin_path=placement.origin_path + "-different-generation",
                ),
            )
            if drift == "placement_phase":
                changed = replace(
                    ownership, placement_carrier=replace(placement, phase="discarding")
                )
            elif drift == "private_leftover":
                changed = replace(
                    ownership, private_directory=ownership.canonical_directory
                )
            _ = db.execute(
                "UPDATE import_pack_cleanup_reservations SET directory_ownership_json=? WHERE download_identity_key=?",
                (changed.to_json(), identity_key),
            )
            if drift == "generation":
                _ = db.execute(
                    "UPDATE import_pack_cleanup_reservations SET tombstone_path=? WHERE download_identity_key=?",
                    (placement.origin_path + "-different-generation", identity_key),
                )
    inventory = capture_inventory(producer_media, monkeypatch)
    pack_queue_refusal(
        producer_media,
        monkeypatch,
        inventory,
        pack_queue_decisions(inventory, identity_key),
    )


def pack_queue_nested_checkpoint(env: ProducerMedia) -> tuple[str, int]:
    import import_pack_cleanup as packs
    from private_pack_claim import inventory_tree, open_directory

    identity_key, queue_id = producer_pack(env, "ready")
    assert queue_id is None
    reservation = packs._read_reservation(identity_key)
    assert reservation is not None
    ownership = packs._ownership(reservation)
    assert ownership is not None and ownership.canonical_directory is not None
    root = Path(ownership.canonical_directory.path)
    (root / "extras").mkdir()
    (root / "empty").mkdir()
    write_archive(
        root / "extras" / "retained.cbz", b"unselected by generated queue parser"
    )
    with open_directory(str(root)) as fd:
        entries = inventory_tree(fd)
    with file_mutation_guard(str(env.db_path)) as guard:
        reservation = packs._save_ownership(
            guard, reservation, replace(ownership, inventory=entries)
        )
        source = root / "Matrix v02.cbz"
        queue_id = packs._commit_generated_pack_queue(
            guard,
            reservation,
            (
                7,
                "matrix-generated",
                1,
                "torrent",
                "Matrix v02",
                "magnet:matrix-generated",
                2,
                str(root),
            ),
            [
                (
                    source.name,
                    str(source),
                    str(env.library / source.name),
                    2,
                    None,
                    None,
                    None,
                    None,
                    None,
                    0,
                    "volume",
                    None,
                    "cbz",
                    "pending",
                )
            ],
            respect_grab_claims=False,
        )
    return identity_key, queue_id


@pytest.mark.parametrize("missing", [None, "directory", "file"])
def test_pack_queue_nested_inventory_is_complete_even_outside_generated_file_rows(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, missing: str | None
) -> None:
    identity_key, _ = pack_queue_nested_checkpoint(producer_media)
    initial = capture_inventory(producer_media, monkeypatch)
    owner = OwnerRef("pack_reservation", (identity_key,))
    paths = {
        r.role.relative: r.path
        for r in initial.closure.roles
        if r.owner == owner
        and r.role.purpose in {"pack_canonical_file", "pack_canonical_directory"}
    }
    assert set(paths) == {"Matrix v02.cbz", "extras", "extras/retained.cbz", "empty"}
    if missing == "directory":
        Path(paths["empty"]).rmdir()
    elif missing == "file":
        Path(paths["extras/retained.cbz"]).unlink()
    inventory = capture_inventory(producer_media, monkeypatch)
    decisions = pack_queue_decisions(inventory, identity_key)
    if missing is not None:
        pack_queue_refusal(producer_media, monkeypatch, inventory, decisions)
    else:
        inputs = review_documents(inventory, decisions)
        rows, media, control = (
            captured_fixture(producer_media)[1],
            media_snapshot(producer_media.scope),
            control_snapshot(producer_media.db_path),
        )
        with pack_queue_boundaries(monkeypatch):
            plan = recovery.build_recovery_plan(inventory, *inputs)
            assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan
            assert replace(plan) == plan
        assert captured_fixture(producer_media)[1] == rows
        assert media_snapshot(producer_media.scope) == media
        assert control_snapshot(producer_media.db_path) == control


def test_pack_queue_real_expired_lease_rotation_does_not_change_artifact_generation(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    import import_pack_cleanup as packs

    identity_key, _ = producer_pack(producer_media, "queued")
    before = packs._read_reservation(identity_key)
    assert before is not None
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        _ = db.execute(
            "UPDATE import_pack_cleanup_reservations SET expires_at='2000-01-01' WHERE download_identity_key=?",
            (identity_key,),
        )
    rotated = packs._claim_expired_reservation(before)
    assert rotated is not None and rotated.owner_token != before.owner_token
    assert rotated.artifact_owner_token == before.artifact_owner_token
    inventory = capture_inventory(producer_media, monkeypatch)
    inputs = review_documents(inventory, pack_queue_decisions(inventory, identity_key))
    with pack_queue_boundaries(monkeypatch):
        plan = recovery.build_recovery_plan(inventory, *inputs)
        assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan


def test_pack_queue_terminal_live_row_drift_remains_retention_not_pending_cas(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity_key, queue_id = producer_pack(producer_media, "attached")
    assert queue_id is not None
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        _ = db.execute(
            "UPDATE import_queue SET status='imported',torrent_name='terminal refresh' WHERE id=?",
            (queue_id,),
        )
    inventory = capture_inventory(producer_media, monkeypatch)
    with pack_queue_boundaries(monkeypatch):
        retained = recovery.build_recovery_plan(inventory, *review_documents(inventory))
        assert recovery.RecoveryPlan.from_json(retained.to_json()) == retained
    pack_queue_refusal(
        producer_media,
        monkeypatch,
        inventory,
        pack_queue_decisions(inventory, identity_key),
    )


@pytest.mark.parametrize("missing", ["field", "null"])
def test_pack_queue_v3_missing_receipt_is_already_invalid_inventory(
    producer_media: ProducerMedia, missing: str
) -> None:
    identity_key, _ = producer_pack(producer_media, "queued")
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        row = cast(
            tuple[str] | None,
            db.execute(
                "SELECT directory_ownership_json FROM import_pack_cleanup_reservations WHERE download_identity_key=?",
                (identity_key,),
            ).fetchone(),
        )
        assert row is not None
        ownership = parse_document(row[0])
        if missing == "field":
            del ownership["queue_receipt_sha256"]
        else:
            ownership["queue_receipt_sha256"] = None
        _ = db.execute(
            "UPDATE import_pack_cleanup_reservations SET directory_ownership_json=? WHERE download_identity_key=?",
            (encoded(ownership), identity_key),
        )
    with pytest.raises(
        InvalidIdentity, match="invalid persisted domain reference evidence"
    ):
        _ = captured_fixture(producer_media)


@pytest.mark.parametrize("drift", ["directory", "source"])
def test_pack_queue_incoming_receipt_and_generated_source_are_independent(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    import import_pack_cleanup as packs

    identity_key, queue_id = producer_pack(producer_media, "queued")
    assert queue_id is not None
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        db.row_factory = sqlite3.Row
        if drift == "directory":
            _ = db.execute(
                "UPDATE import_queue SET src_dir=? WHERE id=?",
                (str(producer_media.incoming), queue_id),
            )
        else:
            _ = db.execute(
                "UPDATE import_queue_files SET src_path=? WHERE queue_id=?",
                (str(producer_media.source), queue_id),
            )
        row = cast(
            sqlite3.Row | None,
            db.execute(
                "SELECT directory_ownership_json FROM import_pack_cleanup_reservations WHERE download_identity_key=?",
                (identity_key,),
            ).fetchone(),
        )
        assert row is not None
        ownership = parse_document(cast(str, row[0]))
        if drift == "source":
            ownership["queue_receipt_sha256"] = packs._queue_receipt(db, queue_id)
        _ = db.execute(
            "UPDATE import_pack_cleanup_reservations SET directory_ownership_json=? WHERE download_identity_key=?",
            (encoded(ownership), identity_key),
        )
    inventory = capture_inventory(producer_media, monkeypatch)
    pack_queue_refusal(
        producer_media,
        monkeypatch,
        inventory,
        pack_queue_decisions(inventory, identity_key),
    )


def selected_reference_rescan(
    env: ProducerMedia, source: str, *, series_id: int = 8, volume_id: int = 2
) -> OwnerRef:
    """Real reservation on an explicitly seeded separate-series legacy mapping."""
    import private_file_claim as claims
    import rescan
    import rescan_file_recovery as rescans

    info = Path(source).stat()
    with closing(sqlite3.connect(env.db_path)) as db, db:
        db.row_factory = sqlite3.Row
        root = cast(
            sqlite3.Row | None,
            db.execute("SELECT root_folder_id FROM series WHERE id=7").fetchone(),
        )
        assert root is not None
        _ = db.execute(
            "INSERT INTO series(id,title,search_pattern,folder_name,root_folder_id) VALUES(?,'Selected legacy reference','','SelectedLegacy',?)",
            (series_id, cast(int, root[0])),
        )
        _ = db.execute(
            "INSERT INTO volumes(id,series_id,volume_num,status,import_path,size_bytes,quality) VALUES(?,?,2,'downloaded',?,?,'cbz')",
            (volume_id, series_id, source, info.st_size),
        )
        raw = cast(
            sqlite3.Row | None,
            db.execute(rescan._ENRICHMENT_VOLUME_SELECT, (volume_id,)).fetchone(),
        )
        assert raw is not None
        volume = cast(dict[str, object], dict(raw))
        assert not rescans.active_for_series(db, series_id)
        assert not rescans.competing_for_series(db, series_id)
    target = rescan._EnrichmentTarget(
        volume,
        2,
        source,
        rescan.FileFingerprint(
            info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino
        ),
    )
    context = rescan._current_enrichment_context(target)
    assert context is not None
    destination = str(
        Path(source).parent / ("cancel-destination-" + str(volume_id) + ".cbz")
    )
    operation = rescans._reserve(
        target, context, rescans.fingerprint_path(source), destination
    )
    assert operation is not None and operation.row["state"] == "prepared"
    assert rescan._current_enrichment_context(target) == context
    with file_mutation_guard(str(env.db_path)) as guard:
        with claims.ensure_namespace(guard, str(Path(source).parent)):
            pass
    return OwnerRef("rescan", (cast(int, operation.row["id"]),))


def selected_reference_decisions(
    inventory: recovery.OwnershipInventory,
    deletion: OwnerRef,
    rescan: OwnerRef,
    *,
    destructive: bool = True,
) -> tuple[recovery.RecoveryDecision, ...]:
    deletion_records = (
        chosen(inventory, deletion, "resume-commit", {"delete_artifact"})
        if destructive
        else tuple(
            recovery.RecoveryDecision(
                s.owner,
                "retain-blocked",
                (),
                None,
                tuple(r.role for r in inventory.closure.roles if r.owner == s.owner),
            )
            for s in inventory.closure.snapshots
        )
    )
    cancellation = next(
        d
        for d in chosen(inventory, rescan, "cancel-unmutated", {"source"})
        if d.owner == rescan
    )
    return tuple(cancellation if d.owner == rescan else d for d in deletion_records)


def test_selected_reference_real_deletion_and_separate_rescan_cancel_conflict(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    deletion = OwnerRef("deletion", (producer_deletion(producer_media, "claimed"),))
    initial = capture_inventory(producer_media, monkeypatch)
    first = chosen(initial, deletion, "resume-commit", {"delete_artifact"})
    with pack_queue_boundaries(monkeypatch):
        _ = recovery.build_recovery_plan(initial, *review_documents(initial, first))
    artifact = next(
        o
        for o in initial.observed
        if o.spec.owner == deletion and o.spec.role.purpose == "delete_artifact"
    )
    rescan = selected_reference_rescan(producer_media, artifact.spec.path)
    inventory = capture_inventory(producer_media, monkeypatch)
    source = next(
        o
        for o in inventory.observed
        if o.spec.owner == rescan and o.spec.role.purpose == "source"
    )
    assert source.outcome == artifact.outcome == "observed"
    assert source.spec.path == artifact.spec.path
    support = [
        o
        for o in inventory.observed
        if o.spec.owner
        == OwnerRef("namespace", (str(Path(artifact.spec.path).parent),))
    ]
    assert len(support) == 3 and all(o.outcome == "observed" for o in support)
    cancellation = selected_reference_decisions(
        inventory, deletion, rescan, destructive=False
    )
    selected_reference_valid_plan(producer_media, monkeypatch, inventory, cancellation)
    decisions = selected_reference_decisions(inventory, deletion, rescan)
    assert source.spec.role in next(d for d in decisions if d.owner == rescan).roles
    assert len(decisions) == len(inventory.closure.snapshots)
    assert sum(len(d.roles) + len(d.retained_roles) for d in decisions) == len(
        inventory.closure.roles
    )
    pack_queue_refusal(producer_media, monkeypatch, inventory, decisions)
    with selected_reference_boundaries(monkeypatch):
        with pytest.raises(InvalidIdentity, match="ownership_phase") as error:
            _ = recovery.build_recovery_plan(
                inventory, *review_documents(inventory, decisions)
            )
    trace = error.value.__traceback__
    functions: list[str] = []
    while trace is not None:
        functions.append(trace.tb_frame.f_code.co_name)
        trace = trace.tb_next
    assert functions[-1] == "_validate_retained_references"


@contextmanager
def selected_reference_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None, None, None]:
    import rescan_file_recovery as rescans

    def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("selected-reference planner called real rescan runtime")

    with pack_queue_boundaries(monkeypatch), monkeypatch.context() as patch:
        for name in (
            "get_db",
            "_reserve",
            "_store",
            "_allocate",
            "_capture_source",
            "_publish",
            "_commit",
            "_settle",
        ):
            patch.setattr(rescans, name, forbidden)
        yield


def selected_reference_valid_plan(
    env: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    inventory: recovery.OwnershipInventory,
    decisions: tuple[recovery.RecoveryDecision, ...],
) -> None:
    rows, media, control = (
        captured_fixture(env)[1],
        media_snapshot(env.scope),
        control_snapshot(env.db_path),
    )
    original = inventory.to_json()
    inputs = review_documents(inventory, decisions)
    with selected_reference_boundaries(monkeypatch):
        plan = recovery.build_recovery_plan(inventory, *inputs)
        assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan
        assert replace(plan) == plan
    assert len(decisions) == len(inventory.closure.snapshots)
    assert sum(len(d.roles) + len(d.retained_roles) for d in decisions) == len(
        inventory.closure.roles
    )
    assert inventory.to_json() == original
    assert captured_fixture(env)[1] == rows
    assert media_snapshot(env.scope) == media
    assert control_snapshot(env.db_path) == control


@pytest.mark.parametrize("layout", ["disjoint", "hardlink"])
def test_selected_reference_disjoint_mixed_actions_preserve_unchanged_name(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    import os

    deletion = OwnerRef("deletion", (producer_deletion(producer_media, "claimed"),))
    initial = capture_inventory(producer_media, monkeypatch)
    artifact = next(
        o
        for o in initial.observed
        if o.spec.owner == deletion and o.spec.role.purpose == "delete_artifact"
    )
    source = producer_media.incoming / "unchanged.cbz"
    if layout == "hardlink":
        os.link(artifact.spec.path, source)
        assert source.stat().st_ino == Path(artifact.spec.path).stat().st_ino
    else:
        write_archive(source, b"independent selected unchanged source")
    rescan = selected_reference_rescan(producer_media, str(source))
    inventory = capture_inventory(producer_media, monkeypatch)
    assert str(source) != artifact.spec.path
    selected_reference_valid_plan(
        producer_media,
        monkeypatch,
        inventory,
        selected_reference_decisions(inventory, deletion, rescan),
    )


def test_selected_reference_shared_unchanged_rescan_subjects_do_not_conflict(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = selected_reference_rescan(producer_media, str(producer_media.source))
    second = selected_reference_rescan(
        producer_media, str(producer_media.source), series_id=9, volume_id=3
    )
    inventory = capture_inventory(producer_media, monkeypatch)
    decisions = chosen(inventory, first, "cancel-unmutated", {"source"})
    other = next(
        d
        for d in chosen(inventory, second, "cancel-unmutated", {"source"})
        if d.owner == second
    )
    decisions = tuple(other if d.owner == second else d for d in decisions)
    sources = [
        o
        for o in inventory.observed
        if o.spec.owner in {first, second} and o.spec.role.purpose == "source"
    ]
    assert len(sources) == 2 and sources[0].spec.path == sources[1].spec.path
    assert all(o.outcome == "observed" for o in sources)
    selected_reference_valid_plan(producer_media, monkeypatch, inventory, decisions)


def ready_placement_candidate(env: ProducerMedia, *, nested: bool) -> str:
    import import_pack_cleanup as packs
    from private_pack_claim import inventory_tree, open_directory

    identity_key, queue_id = producer_pack(env, "ready")
    assert queue_id is None
    if nested:
        reservation = packs._read_reservation(identity_key)
        assert reservation is not None
        ownership = packs._ownership(reservation)
        assert ownership is not None and ownership.canonical_directory is not None
        root = Path(ownership.canonical_directory.path)
        (root / "nested").mkdir()
        (root / "empty").mkdir()
        write_archive(root / "nested" / "extra.cbz", b"nested ready placement")
        with open_directory(str(root)) as fd:
            entries = inventory_tree(fd)
        with file_mutation_guard(str(env.db_path)) as guard:
            _ = packs._save_ownership(
                guard, reservation, replace(ownership, inventory=entries)
            )
    return identity_key


def ready_placement_restore_decisions(
    inventory: recovery.OwnershipInventory, identity_key: str
) -> tuple[recovery.RecoveryDecision, ...]:
    owner = OwnerRef("pack_reservation", (identity_key,))
    destination = next(
        o.spec.path
        for o in inventory.observed
        if o.spec.owner == owner and o.spec.role.purpose == "tombstone_path"
    )
    return chosen(
        inventory,
        owner,
        "restore-original",
        {
            "pack_placement_artifact",
            "pack_canonical",
            "pack_canonical_marker",
            "pack_canonical_directory",
            "pack_canonical_file",
        },
        destination,
    )


@pytest.mark.parametrize("nested", [False, True])
def test_ready_placement_restore_selects_two_bindings_one_original(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, nested: bool
) -> None:
    import private_file_claim as claims
    from private_pack_claim import (
        PackOwnership,
        open_directory,
        verify_directory,
        verify_inventory,
    )

    key = ready_placement_candidate(producer_media, nested=nested)
    inventory = capture_inventory(producer_media, monkeypatch)
    owner = OwnerRef("pack_reservation", (key,))
    row = next(
        dict(r.values)
        for r in inventory.closure.raw_rows
        if r.table == "import_pack_cleanup_reservations"
        and dict(r.values)["download_identity_key"] == key
    )
    before_legacy_json = row["directory_ownership_json"]
    proof = PackOwnership.from_json(cast(str, before_legacy_json))
    carrier, directory, entries = (
        proof.placement_carrier,
        proof.canonical_directory,
        proof.inventory,
    )
    assert proof.phase == "ready" and row["purpose"] == "cleanup"
    assert row["queue_id"] is None and row["publication_id"] is None
    assert proof.private_directory is None and not proof.claims
    assert carrier is not None and carrier.phase == "claimed"
    assert carrier.restore_receipt is None and carrier.artifact_fingerprint is None
    assert directory is not None and entries is not None and entries.files
    assert directory.path == carrier.carrier_path + "/artifact"
    assert row["tombstone_path"] == carrier.origin_path
    namespace_row = next(
        dict(r.values)
        for r in inventory.closure.raw_rows
        if r.table == "file_claim_namespaces"
        and dict(r.values)["parent_path"] == str(Path(carrier.origin_path).parent)
    )
    with file_mutation_guard(str(producer_media.db_path)) as guard:
        with claims.open_namespace(
            guard,
            str(Path(carrier.origin_path).parent),
            cast(str, namespace_row["ownership_json"]),
        ) as namespace:
            with claims.open_carrier(namespace, carrier.binding, carrier) as opened:
                opened.verify()
        with open_directory(directory.path) as fd:
            verify_directory(fd, directory)
            verify_inventory(fd, entries)
    roles = [o for o in inventory.observed if o.spec.owner == owner]
    placement = next(
        o.spec for o in roles if o.spec.role.purpose == "pack_placement_artifact"
    )
    canonical = next(o.spec for o in roles if o.spec.role.purpose == "pack_canonical")
    children = {o.spec.role for o in roles if o.spec.parent_role == canonical.role}
    assert placement.path == canonical.path == directory.path
    assert placement.role != canonical.role
    assert len(children) == 1 + len(entries.directories) + len(entries.files)
    assert all(not o.unresolved for o in inventory.observed)
    decisions = ready_placement_restore_decisions(inventory, key)
    inputs = review_documents(inventory, decisions)
    reviews: list[bool] = []
    validate_reviews = recovery._validate_reviews

    def reviewed(
        plan: recovery.RecoveryPlan,
        declaration: dict[str, object],
        receipt: dict[str, object],
        epoch: dict[str, object],
        selected: set[tuple[OwnerRef, RoleKey]],
    ) -> None:
        validate_reviews(plan, declaration, receipt, epoch, selected)
        reviews.append(True)

    before = (
        captured_fixture(producer_media)[1],
        media_snapshot(producer_media.scope),
        control_snapshot(producer_media.db_path),
    )
    with pack_queue_boundaries(monkeypatch), monkeypatch.context() as patch:
        patch.setattr(recovery, "_validate_reviews", reviewed)
        try:
            plan = recovery.build_recovery_plan(inventory, *inputs)
        finally:
            assert reviews == [True], (
                "action RED requires completed review/media validation"
            )
        assert plan.version == 1
        restores = [d for d in plan.decisions if d.action == "restore-original"]
        assert len(restores) == 1
        restore = restores[0]
        assert set(restore.roles) == {placement.role, canonical.role} | children
        assert restore.destination == carrier.origin_path == row["tombstone_path"]
        assert all(r not in restore.roles for r in restore.retained_roles)
        assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan
        assert replace(plan) == plan
    assert before == (
        captured_fixture(producer_media)[1],
        media_snapshot(producer_media.scope),
        control_snapshot(producer_media.db_path),
    )
    after_legacy_json = next(
        dict(r.values)["directory_ownership_json"]
        for r in captured_fixture(producer_media)[0].raw_rows
        if r.table == "import_pack_cleanup_reservations"
        and dict(r.values)["download_identity_key"] == key
    )
    assert after_legacy_json == before_legacy_json


@pytest.mark.parametrize(
    "purpose",
    [
        "pack_placement_artifact",
        "pack_canonical",
        "pack_canonical_file",
        "pack_canonical_directory",
        "pack_canonical_marker",
        "pack_placement_carrier",
        "pack_placement_marker",
    ],
)
def test_ready_placement_restore_requires_exact_subjects(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, purpose: str
) -> None:
    key = ready_placement_candidate(producer_media, nested=True)
    inventory = capture_inventory(producer_media, monkeypatch)
    decisions = ready_placement_restore_decisions(inventory, key)
    owner = OwnerRef("pack_reservation", (key,))
    decision = next(d for d in decisions if d.owner == owner)
    role = next(
        r.role
        for r in inventory.closure.roles
        if r.owner == owner and r.role.purpose == purpose
    )
    subjects = set(decision.roles) ^ {role}
    decisions = tuple(
        replace(
            d,
            roles=tuple(
                r.role
                for r in inventory.closure.roles
                if r.owner == owner and r.role in subjects
            ),
            retained_roles=tuple(
                r.role
                for r in inventory.closure.roles
                if r.owner == owner and r.role not in subjects
            ),
        )
        if d.owner == owner
        else d
        for d in decisions
    )
    pack_queue_refusal(producer_media, monkeypatch, inventory, decisions)


@pytest.mark.parametrize(
    "drift",
    [
        "file_missing",
        "directory_missing",
        "pack_marker_missing",
        "carrier_marker_missing",
        "namespace_marker_missing",
        "namespace_marker_changed",
        "file_changed",
        "destination_occupied",
        "logical_occupied",
        "generation",
        "directory_proof",
        "placement_phase",
        "private_leftover",
        "building",
        "queued",
    ],
)
def test_ready_placement_restore_refuses_ineligible_current_evidence(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    from file_identity import IdentityMismatch
    from private_pack_claim import PackOwnership

    key = (
        producer_pack(producer_media, "queued")[0]
        if drift == "queued"
        else ready_placement_candidate(producer_media, nested=True)
    )
    initial = capture_inventory(producer_media, monkeypatch)
    owner = OwnerRef("pack_reservation", (key,))

    def role(purpose: str) -> recovery.ObservedRole:
        return next(
            o
            for o in initial.observed
            if o.spec.owner == owner and o.spec.role.purpose == purpose
        )

    if drift in {
        "file_missing",
        "directory_missing",
        "pack_marker_missing",
        "carrier_marker_missing",
        "file_changed",
    }:
        purpose = {
            "file_missing": "pack_canonical_file",
            "file_changed": "pack_canonical_file",
            "directory_missing": "pack_canonical_directory",
            "pack_marker_missing": "pack_canonical_marker",
            "carrier_marker_missing": "pack_placement_marker",
        }[drift]
        path = Path(role(purpose).spec.path)
        if drift == "directory_missing":
            Path(role("pack_canonical").spec.path, "empty").rmdir()
        elif drift == "file_changed":
            _ = path.write_bytes(b"changed ready original")
        else:
            path.unlink()
    elif drift.startswith("namespace_marker"):
        path = Path(
            next(
                o.spec.path
                for o in initial.observed
                if o.spec.role.purpose == "namespace_marker"
            )
        )
        if drift.endswith("missing"):
            path.unlink()
        else:
            _ = path.write_bytes(b"changed namespace original")
    elif drift in {"destination_occupied", "logical_occupied"}:
        Path(
            role(
                "tombstone_path" if drift == "destination_occupied" else "pack_logical"
            ).spec.path
        ).mkdir()
    elif drift != "queued":
        with closing(sqlite3.connect(producer_media.db_path)) as db, db:
            row = cast(
                tuple[str],
                db.execute(
                    "SELECT directory_ownership_json FROM import_pack_cleanup_reservations WHERE download_identity_key=?",
                    (key,),
                ).fetchone(),
            )
            proof = PackOwnership.from_json(row[0])
            carrier, directory = proof.placement_carrier, proof.canonical_directory
            assert carrier is not None and directory is not None
            if drift == "generation":
                token = proof.artifact_owner_token + "-other"
                changed = replace(
                    proof,
                    artifact_owner_token=token,
                    placement_carrier=replace(
                        carrier,
                        binding=replace(carrier.binding, operation_key=token),
                        origin_path=carrier.origin_path + "-other",
                    ),
                )
                _ = db.execute(
                    "UPDATE import_pack_cleanup_reservations SET tombstone_path=? WHERE download_identity_key=?",
                    (carrier.origin_path + "-other", key),
                )
            elif drift == "directory_proof":
                changed = replace(
                    proof,
                    canonical_directory=replace(directory, inode=directory.inode + 1),
                )
            elif drift == "placement_phase":
                changed = replace(
                    proof, placement_carrier=replace(carrier, phase="discarding")
                )
            elif drift == "private_leftover":
                changed = replace(proof, private_directory=directory)
            else:
                changed = replace(proof, phase="building")
            _ = db.execute(
                "UPDATE import_pack_cleanup_reservations SET directory_ownership_json=? WHERE download_identity_key=?",
                (changed.to_json(), key),
            )
    if drift == "building":
        with pytest.raises(
            IdentityMismatch,
            match="reservation tombstone lacks phase-specific provenance",
        ):
            _ = captured_fixture(producer_media)
        return
    inventory = capture_inventory(producer_media, monkeypatch)
    # Complete acquisition alone never grants mutation permission.
    assert not inventory.unresolved
    selected_reference_valid_plan(
        producer_media,
        monkeypatch,
        inventory,
        tuple(
            recovery.RecoveryDecision(
                s.owner,
                "retain-blocked",
                (),
                None,
                tuple(r.role for r in inventory.closure.roles if r.owner == s.owner),
            )
            for s in inventory.closure.snapshots
        ),
    )
    pack_queue_refusal(
        producer_media,
        monkeypatch,
        inventory,
        ready_placement_restore_decisions(inventory, key),
    )


@pytest.mark.parametrize(
    "location", ["root", "child", "nested", "destination", "destination_child"]
)
def test_ready_placement_restore_independent_retained_consumer_veto(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    key = ready_placement_candidate(producer_media, nested=True)
    initial = capture_inventory(producer_media, monkeypatch)
    owner = OwnerRef("pack_reservation", (key,))
    purpose = {
        "root": "pack_canonical",
        "child": "pack_canonical_file",
        "nested": "pack_canonical_file",
        "destination": "tombstone_path",
        "destination_child": "tombstone_path",
    }[location]
    path = next(
        o.spec.path
        for o in initial.observed
        if o.spec.owner == owner
        and o.spec.role.purpose == purpose
        and (o.spec.role.relative == "nested/extra.cbz") == (location == "nested")
    )
    if location == "destination_child":
        path += "/nested/extra.cbz"
    consumer = retref_queue_reference(
        producer_media, path, directory=location in {"root", "destination"}
    )
    if location == "destination_child":
        closure, _ = captured_fixture(producer_media)
        observed = observe_fixture(recovery, producer_media, closure, monkeypatch)
        assert any(o.spec.owner == consumer and o.unresolved for o in observed)
        incomplete = recovery.OwnershipInventory.create(
            str(producer_media.scope),
            closure,
            observed,
            tuple(sorted({"role_" + o.outcome for o in observed if o.unresolved})),
            InventoryBudget(),
        )
        with selected_reference_boundaries(monkeypatch):
            with pytest.raises(InvalidIdentity, match="ownership_incomplete"):
                _ = recovery.build_recovery_plan(
                    incomplete,
                    *review_documents(
                        incomplete, ready_placement_restore_decisions(incomplete, key)
                    ),
                )
        return
    inventory = capture_inventory(producer_media, monkeypatch)
    decisions = ready_placement_restore_decisions(inventory, key)
    assert next(d for d in decisions if d.owner == consumer).action == "retain-blocked"
    pack_queue_refusal(producer_media, monkeypatch, inventory, decisions)
    with selected_reference_boundaries(monkeypatch):
        with pytest.raises(InvalidIdentity, match="ownership_phase") as error:
            _ = recovery.build_recovery_plan(
                inventory, *review_documents(inventory, decisions)
            )
    trace = error.value.__traceback__
    functions: list[str] = []
    while trace is not None:
        functions.append(trace.tb_frame.f_code.co_name)
        trace = trace.tb_next
    assert functions[-1] == "_validate_retained_references"


@pytest.mark.parametrize(
    "purpose",
    [
        "pack_placement_artifact",
        "pack_canonical",
        "pack_canonical_marker",
        "pack_canonical_directory",
        "pack_canonical_file",
    ],
)
def test_ready_placement_restore_cannot_retain_moved_original_in_place(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, purpose: str
) -> None:
    key = ready_placement_candidate(producer_media, nested=True)
    inventory = capture_inventory(producer_media, monkeypatch)
    decisions = ready_placement_restore_decisions(inventory, key)
    selection, declaration, receipt = (
        parse_document(d) for d in review_documents(inventory, decisions)
    )
    record = next(
        r
        for r in map(wire_object, wire_array(receipt["media"]))
        if wire_object(r["role"])["purpose"] == purpose
    )
    record["handling"], record["recovery_reference"] = "retain-in-place", None
    with pack_queue_boundaries(monkeypatch):
        with pytest.raises(InvalidIdentity, match="ownership_binding") as error:
            _ = recovery.build_recovery_plan(
                inventory, *resign_documents(selection, declaration, receipt)
            )
    trace = error.value.__traceback__
    functions: list[str] = []
    while trace is not None:
        functions.append(trace.tb_frame.f_code.co_name)
        trace = trace.tb_next
    assert functions[-1] == "_validate_reviews"


def test_ready_placement_restore_receipt_is_rejected_by_typed_proof(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    from private_pack_claim import PackOwnership, PackProofError

    key = ready_placement_candidate(producer_media, nested=False)
    rescan_id = producer_rescan(producer_media, "published")
    inventory = capture_inventory(producer_media, monkeypatch)
    row = next(
        dict(r.values)
        for r in inventory.closure.raw_rows
        if r.table == "import_pack_cleanup_reservations"
    )
    proof = parse_document(cast(str, row["directory_ownership_json"]))
    carrier = wire_object(proof["placement_carrier"])
    carrier["phase"] = "restored"
    # Reuse a real regular-file receipt as malformed input, never a directory receipt.
    carrier["restore_receipt"] = parse_document(
        cast(
            str,
            next(
                dict(r.values)["publication_receipt_json"]
                for r in inventory.closure.raw_rows
                if r.table == "rescan_file_operations"
                and dict(r.values)["id"] == rescan_id
            ),
        )
    )
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        _ = db.execute(
            "UPDATE import_pack_cleanup_reservations SET directory_ownership_json=? WHERE download_identity_key=?",
            (encoded(proof), key),
        )
    with pytest.raises(PackProofError, match="invalid pack placement carrier binding"):
        _ = PackOwnership.from_json(encoded(proof))
    with pytest.raises(
        InvalidIdentity, match="invalid persisted domain reference evidence"
    ):
        _ = captured_fixture(producer_media)


@pytest.mark.parametrize("nested", [False, True])
def test_ready_placement_candidate_full_evidence_and_exact_root_role_projection(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    nested: bool,
) -> None:
    import private_file_claim as claims
    import import_pack_cleanup as packs
    from download_identity import DownloadIdentity
    from private_pack_claim import (
        PackOwnership,
        open_directory,
        verify_directory,
        verify_inventory,
    )

    identity_key = ready_placement_candidate(producer_media, nested=nested)
    inventory = capture_inventory(producer_media, monkeypatch)
    owner = OwnerRef("pack_reservation", (identity_key,))
    row = next(
        dict(r.values)
        for r in inventory.closure.raw_rows
        if r.table == "import_pack_cleanup_reservations"
        and dict(r.values)["download_identity_key"] == identity_key
    )
    ownership = PackOwnership.from_json(cast(str, row["directory_ownership_json"]))
    placement, proof, entries = (
        ownership.placement_carrier,
        ownership.canonical_directory,
        ownership.inventory,
    )
    assert ownership.phase == "ready" and row["purpose"] == "cleanup"
    assert row["queue_id"] is None and row["publication_id"] is None
    assert ownership.private_directory is None and ownership.claims == ()
    assert placement is not None and placement.phase == "claimed"
    assert placement.restore_receipt is None and placement.artifact_fingerprint is None
    assert proof is not None and entries is not None and entries.files
    assert proof.path == placement.carrier_path + "/artifact"
    assert row["tombstone_path"] == placement.origin_path
    assert placement.origin_path != row["pack_path"]
    assert proof.marker_fingerprint is not None
    payload = packs._pack_owner_marker_payload(
        DownloadIdentity(1, "torrent", "matrix-generated"),
        ownership.artifact_owner_token,
    )
    assert proof.marker_fingerprint.sha256 == hashlib.sha256(payload).hexdigest()
    roles = [o for o in inventory.observed if o.spec.owner == owner]
    placement_root = next(
        o for o in roles if o.spec.role.purpose == "pack_placement_artifact"
    )
    canonical_root = next(o for o in roles if o.spec.role.purpose == "pack_canonical")
    destination = next(o for o in roles if o.spec.role.purpose == "tombstone_path")
    assert placement_root.spec.path == canonical_root.spec.path == proof.path
    assert placement_root.spec.role != canonical_root.spec.role
    assert (
        placement_root.spec.parent_role is not None
        and canonical_root.spec.parent_role is None
    )
    assert placement_root.outcome == canonical_root.outcome == "observed"
    assert (
        destination.spec.path == placement.origin_path
        and destination.outcome == "absent"
    )
    assert Path(destination.spec.path).parent.is_dir()
    namespace_owner = OwnerRef("namespace", (str(Path(destination.spec.path).parent),))
    namespace_roles = [o for o in inventory.observed if o.spec.owner == namespace_owner]
    assert len(namespace_roles) == 3 and all(
        o.outcome == "observed" for o in namespace_roles
    )
    namespace_row = next(
        dict(r.values)
        for r in inventory.closure.raw_rows
        if r.table == "file_claim_namespaces"
        and dict(r.values)["parent_path"] == namespace_owner.key[0]
    )
    with file_mutation_guard(str(producer_media.db_path)) as guard:
        with claims.open_namespace(
            guard,
            cast(str, namespace_owner.key[0]),
            cast(str, namespace_row["ownership_json"]),
        ) as namespace:
            with claims.open_carrier(
                namespace, placement.binding, placement
            ) as carrier:
                carrier.verify()
        with open_directory(proof.path) as fd:
            verify_directory(fd, proof)
            verify_inventory(fd, entries)
    descendants = [o for o in roles if o.spec.parent_role == canonical_root.spec.role]
    expected = {
        proof.path + "/mangarr-pack-owner",
        *(proof.path + "/" + p for p in entries.directories),
        *(proof.path + "/" + p for p in entries.files),
    }
    assert {o.spec.path for o in descendants} == expected
    assert all(o.outcome == "observed" for o in descendants)
    # Record the actual projection facts before attributing any dispatcher refusal.
    literal_subjects = {placement_root.spec.role, *(o.spec.role for o in descendants)}
    assert canonical_root.spec.role not in literal_subjects
    assert canonical_root.spec.path not in expected
    facts: dict[str, object] = {
        "candidate": "real-ready-placement",
        "nested": nested,
        "inventory_sha256": inventory.sha256,
        "destination": destination.spec.path,
        "placement_root": asdict(placement_root.spec),
        "canonical_root": asdict(canonical_root.spec),
        "descendants": [asdict(o.spec) for o in descendants],
        "literal_c4_subjects": [
            recovery._role_value(o.spec.role)
            for o in roles
            if o.spec.role in literal_subjects
        ],
        "co_located_canonical_is_distinct_root_not_descendant": True,
        "eligible_action_red_claimed": False,
    }
    encodings: dict[str, object] = {}
    for name, roots in (
        ("placement_only", {placement_root.spec.role}),
        ("canonical_only", {canonical_root.spec.role}),
        ("both_roots", {placement_root.spec.role, canonical_root.spec.role}),
    ):
        subjects = roots | {o.spec.role for o in descendants}
        decisions = tuple(
            recovery.RecoveryDecision(
                snapshot.owner,
                "restore-original" if snapshot.owner == owner else "retain-blocked",
                tuple(
                    r.role
                    for r in inventory.closure.roles
                    if r.owner == owner and r.role in subjects
                )
                if snapshot.owner == owner
                else (),
                placement.origin_path if snapshot.owner == owner else None,
                tuple(
                    r.role
                    for r in inventory.closure.roles
                    if r.owner == snapshot.owner
                    and (snapshot.owner != owner or r.role not in subjects)
                ),
            )
            for snapshot in inventory.closure.snapshots
        )
        assert sum(len(d.roles) + len(d.retained_roles) for d in decisions) == len(
            inventory.closure.roles
        )
        assert all(
            recovery.RecoveryDecision.from_json(d.to_json()) == d for d in decisions
        )
        _, declaration_json, receipt_json = review_documents(inventory, decisions)
        declaration, receipt = (
            parse_document(declaration_json),
            parse_document(receipt_json),
        )
        assert len(wire_array(declaration["role_reviews"])) == len(
            inventory.closure.roles
        )
        assert len(wire_array(receipt["media"])) == len(inventory.closure.roles)
        retained_roots = [
            o.spec.role.purpose
            for o in (placement_root, canonical_root)
            if o.spec.role not in roots
        ]
        assert bool(retained_roots) == (name != "both_roots")
        encodings[name] = {
            "selected_roots": [
                o.spec.role.purpose
                for o in (placement_root, canonical_root)
                if o.spec.role in roots
            ],
            "retained_roots_at_consumed_path": retained_roots,
            "omits_placement_original_root": placement_root.spec.role not in roots,
            "adds_non_descendant_canonical_root": canonical_root.spec.role in roots,
            "all_roles_and_reviews_preserved": True,
            "structural_decision_roundtrip_only_not_action_eligibility": True,
        }
    facts["encodings"] = encodings
    _ = (tmp_path / "ready-placement-role-facts.json").write_text(encoded(facts) + "\n")
    selected_reference_valid_plan(
        producer_media,
        monkeypatch,
        inventory,
        tuple(
            recovery.RecoveryDecision(
                s.owner,
                "retain-blocked",
                (),
                None,
                tuple(r.role for r in inventory.closure.roles if r.owner == s.owner),
            )
            for s in inventory.closure.snapshots
        ),
    )


def terminal_pack_sql_rows(
    db: sqlite3.Connection, query: str, parameters: tuple[object, ...] = ()
) -> list[dict[str, object]]:
    rows = cast(list[sqlite3.Row], db.execute(query, parameters).fetchall())
    return [{name: cast(object, row[name]) for name in row.keys()} for row in rows]


def terminal_pack_candidate(env: ProducerMedia, *, nested: bool) -> tuple[str, int]:
    import import_pack_cleanup as packs
    from import_lease import claim_import_queue_row, transition_import_queue_row
    from private_pack_claim import inventory_tree, open_directory

    key = ready_placement_candidate(env, nested=nested)
    reservation = packs._read_reservation(key)
    assert reservation is not None
    ownership = packs._ownership(reservation)
    assert ownership is not None and ownership.canonical_directory is not None
    root = Path(ownership.canonical_directory.path)
    _ = (root / "unreferenced.data").write_bytes(b"private unreferenced leaf")
    with open_directory(str(root)) as fd:
        entries = inventory_tree(fd)
    source = root / "Matrix v02.cbz"
    with file_mutation_guard(str(env.db_path)) as guard:
        reservation = packs._save_ownership(
            guard, reservation, replace(ownership, inventory=entries)
        )
        queue_id = packs._commit_generated_pack_queue(
            guard,
            reservation,
            (
                7,
                "matrix-generated",
                1,
                "torrent",
                "Matrix v02",
                "magnet:matrix-generated",
                2,
                str(root),
            ),
            [
                (
                    source.name,
                    str(source),
                    str(env.library / source.name),
                    2,
                    None,
                    None,
                    None,
                    None,
                    None,
                    0,
                    "volume",
                    None,
                    "cbz",
                    "pending",
                )
            ],
            respect_grab_claims=False,
        )
        packs._finish_generated_pack_queue(guard, reservation, queue_id)
    promoted = packs._read_reservation(key)
    assert promoted is not None and promoted.purpose == "queueing"
    with closing(sqlite3.connect(env.db_path)) as db, db:
        db.row_factory = sqlite3.Row
        assert claim_import_queue_row(db, queue_id, "unit-terminal-lease")
        assert transition_import_queue_row(
            db, queue_id, "unit-terminal-lease", "failed"
        )
    acquired = packs._acquire_cleanup_reservation(
        queue_id=queue_id,
        download_id="matrix-generated",
        download_client_id=1,
        protocol="torrent",
        publication_id=None,
        lease_seconds=None,
    )
    assert acquired is not None
    current = packs._read_reservation(key)
    assert current is not None and current.purpose == "cleanup"
    assert current.owner_token != promoted.owner_token
    assert current.directory_ownership_json == promoted.directory_ownership_json
    assert current.artifact_owner_token == promoted.artifact_owner_token
    return key, queue_id


def terminal_pack_decisions(
    inventory: recovery.OwnershipInventory, key: str, paths: set[str]
) -> tuple[recovery.RecoveryDecision, ...]:
    owner = OwnerRef("pack_reservation", (key,))
    subjects = {
        r.role for r in inventory.closure.roles if r.owner == owner and r.path in paths
    }
    assert len(subjects) == len(paths)
    return tuple(
        recovery.RecoveryDecision(
            s.owner,
            "cleanup-committed" if s.owner == owner else "retain-blocked",
            tuple(
                r.role
                for r in inventory.closure.roles
                if r.owner == owner and r.role in subjects
            )
            if s.owner == owner
            else (),
            None,
            tuple(
                r.role
                for r in inventory.closure.roles
                if r.owner == s.owner and (s.owner != owner or r.role not in subjects)
            ),
        )
        for s in inventory.closure.snapshots
    )


@contextmanager
def terminal_pack_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None, None, None]:
    import import_pack_cleanup as packs
    import download_identity

    def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("pure terminal pack planner called runtime authority")

    with pack_queue_boundaries(monkeypatch), monkeypatch.context() as patch:
        for name in (
            "_terminal_cleanup_eligible",
            "_retained_skipped_pack_uses_source",
            "_acquire_cleanup_reservation",
            "_committed_missing",
            "remove_pack_queue_private_artifacts",
            "_discard_pack_placement",
        ):
            patch.setattr(packs, name, forbidden)
        patch.setattr(download_identity, "resolve_download_protocol", forbidden)
        patch.setattr(packs, "resolve_download_protocol", forbidden)
        yield


@pytest.mark.parametrize("nested", [False, True])
def test_terminal_pack_real_generation_individual_private_cleanup(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    nested: bool,
) -> None:
    import import_pack_cleanup as packs
    from download_identity import DownloadIdentity
    from private_pack_claim import (
        open_directory,
        verify_directory,
        verify_inventory,
    )

    key, queue_id = terminal_pack_candidate(producer_media, nested=nested)
    reservation = packs._read_reservation(key)
    assert reservation is not None
    ownership = packs._ownership(reservation)
    assert (
        ownership is not None
        and ownership.phase == "attached"
        and ownership.version == 3
    )
    assert ownership.inventory is not None and ownership.canonical_directory is not None
    assert ownership.private_directory is None and ownership.claims == ()
    assert ownership.artifact_owner_token != reservation.owner_token
    with closing(sqlite3.connect(producer_media.db_path)) as db:
        db.row_factory = sqlite3.Row
        assert packs._terminal_cleanup_eligible(
            db,
            queue_id=queue_id,
            identity=DownloadIdentity(1, "torrent", "matrix-generated"),
        )
        assert packs._queue_receipt(db, queue_id) != ownership.queue_receipt_sha256
        queue = terminal_pack_sql_rows(
            db, "SELECT * FROM import_queue WHERE id=?", (queue_id,)
        )[0]
        assert (
            queue["status"] == "failed"
            and queue["lease_owner"] is None
            and queue["failed_at"] is not None
        )
        assert not db.execute(
            "SELECT 1 FROM import_queue WHERE id!=?", (queue_id,)
        ).fetchall()
        assert not db.execute("SELECT 1 FROM import_publications").fetchall()
        assert {
            r["status"]
            for r in terminal_pack_sql_rows(
                db,
                "SELECT status FROM import_queue_files WHERE queue_id=?",
                (queue_id,),
            )
        } == {"pending"}
    proof = ownership.canonical_directory
    with open_directory(proof.path) as fd:
        verify_directory(fd, proof)
        verify_inventory(fd, ownership.inventory)
    inventory = capture_inventory(producer_media, monkeypatch)
    assert "seen" not in {r.table for r in inventory.closure.raw_rows}
    assert all(o.outcome in {"observed", "absent"} for o in inventory.observed)
    paths = {proof.path + "/unreferenced.data"}
    if nested:
        paths |= {
            proof.path + "/nested",
            proof.path + "/nested/extra.cbz",
            proof.path + "/empty",
        }
    assert all(
        not (r.owner.domain == "queue_source" and r.path in paths)
        for r in inventory.closure.roles
    )
    decisions = terminal_pack_decisions(inventory, key, paths)
    _ = (tmp_path / "terminal-eligibility.json").write_text(
        encoded(
            {
                "key": key,
                "queue_id": queue_id,
                "ownership": ownership.to_json(),
                "runtime_terminal_eligible": True,
                "historical_receipt_differs_from_current": True,
                "selected_paths": sorted(paths),
                "no_siblings_or_publications": True,
                "all_skipped_veto_inapplicable": True,
                "inventory_sha256": inventory.sha256,
            }
        )
        + "\n"
    )
    inputs = review_documents(inventory, decisions)
    rows, media, control, original = (
        captured_fixture(producer_media)[1],
        media_snapshot(producer_media.scope),
        control_snapshot(producer_media.db_path),
        inventory.to_json(),
    )
    with terminal_pack_boundaries(monkeypatch):
        plan = recovery.build_recovery_plan(inventory, *inputs)
        assert plan.decisions == decisions
        assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan
        assert replace(plan) == plan
    assert captured_fixture(producer_media)[1] == rows
    assert media_snapshot(producer_media.scope) == media
    assert control_snapshot(producer_media.db_path) == control
    assert inventory.to_json() == original


def terminal_pack_refusal(
    env: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    inventory: recovery.OwnershipInventory,
    decisions: tuple[recovery.RecoveryDecision, ...],
) -> None:
    inputs = review_documents(inventory, decisions)
    selection = parse_document(inputs[0])
    wire: dict[str, object] = {
        "version": 1,
        "inventory": parse_document(inventory.to_json()),
        "decisions": [d.to_value() for d in decisions],
        "declaration_json": inputs[1],
        "backup_receipt_json": inputs[2],
        "epoch_bindings_json": encoded(selection["epoch_bindings"]),
    }
    sha = digest(wire)
    wire["sha256"] = sha
    rows, media, control, original = (
        captured_fixture(env)[1],
        media_snapshot(env.scope),
        control_snapshot(env.db_path),
        inventory.to_json(),
    )
    with terminal_pack_boundaries(monkeypatch):
        with pytest.raises(InvalidIdentity, match="ownership_phase|ownership_context"):
            _ = recovery.build_recovery_plan(inventory, *inputs)
        with pytest.raises(InvalidIdentity, match="ownership_phase|ownership_context"):
            _ = recovery.RecoveryPlan.from_json(encoded(wire))
        with pytest.raises(InvalidIdentity, match="ownership_phase|ownership_context"):
            _ = recovery.RecoveryPlan(
                1,
                inventory,
                decisions,
                inputs[1],
                inputs[2],
                encoded(selection["epoch_bindings"]),
                sha,
            )
    assert captured_fixture(env)[1] == rows
    assert media_snapshot(env.scope) == media
    assert control_snapshot(env.db_path) == control
    assert inventory.to_json() == original


def test_terminal_pack_all_skipped_retained_source_veto_is_not_a_phase_hint(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_pack_cleanup as packs
    from download_identity import DownloadIdentity

    key, queue_id = terminal_pack_candidate(producer_media, nested=False)
    # Disclosed legacy terminal child refresh; the parent failure was real owner-CAS.
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        db.row_factory = sqlite3.Row
        assert (
            db.execute(
                "UPDATE import_queue_files SET status='skipped' WHERE queue_id=?",
                (queue_id,),
            ).rowcount
            == 1
        )
        assert packs._retained_skipped_pack_uses_source(
            db,
            queue_id=queue_id,
            identity=DownloadIdentity(1, "torrent", "matrix-generated"),
        )
        assert not packs._terminal_cleanup_eligible(
            db,
            queue_id=queue_id,
            identity=DownloadIdentity(1, "torrent", "matrix-generated"),
        )
    inventory = capture_inventory(producer_media, monkeypatch)
    path = next(
        r.path
        for r in inventory.closure.roles
        if r.owner == OwnerRef("pack_reservation", (key,))
        and r.role.relative == "unreferenced.data"
    )
    terminal_pack_refusal(
        producer_media,
        monkeypatch,
        inventory,
        terminal_pack_decisions(inventory, key, {path}),
    )


def test_terminal_pack_explicit_disjoint_sibling_is_not_a_global_queue_veto(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_pack_cleanup as packs
    from download_identity import DownloadIdentity

    key, queue_id = terminal_pack_candidate(producer_media, nested=False)
    sibling_id = producer_queue(producer_media)
    assert sibling_id != queue_id
    with closing(sqlite3.connect(producer_media.db_path)) as db:
        db.row_factory = sqlite3.Row
        assert packs._terminal_cleanup_eligible(
            db,
            queue_id=queue_id,
            identity=DownloadIdentity(1, "torrent", "matrix-generated"),
        )
    inventory = capture_inventory(producer_media, monkeypatch)
    path = next(
        r.path
        for r in inventory.closure.roles
        if r.owner == OwnerRef("pack_reservation", (key,))
        and r.role.relative == "unreferenced.data"
    )
    inputs = review_documents(
        inventory, terminal_pack_decisions(inventory, key, {path})
    )
    with terminal_pack_boundaries(monkeypatch):
        plan = recovery.build_recovery_plan(inventory, *inputs)
        assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan


def test_terminal_pack_current_mapping_must_still_belong_to_recorded_generation(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key, queue_id = terminal_pack_candidate(producer_media, nested=False)
    # A supported, fully observable legacy refresh outside the recorded pack.
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        assert (
            db.execute(
                "UPDATE import_queue_files SET src_path=? WHERE queue_id=?",
                (str(producer_media.source), queue_id),
            ).rowcount
            == 1
        )
    inventory = capture_inventory(producer_media, monkeypatch)
    path = next(
        r.path
        for r in inventory.closure.roles
        if r.owner == OwnerRef("pack_reservation", (key,))
        and r.role.relative == "unreferenced.data"
    )
    terminal_pack_refusal(
        producer_media,
        monkeypatch,
        inventory,
        terminal_pack_decisions(inventory, key, {path}),
    )


@pytest.mark.parametrize(
    "sibling",
    [
        "pending",
        "partial",
        "importing",
        "leased_terminal",
        "active_publication",
        "unknown_protocol",
        "null_client",
        "torrent_case",
        "other_owner",
        "other_protocol",
        "other_id",
        "terminal_unleased",
    ],
)
def test_terminal_pack_sibling_predicates_use_exact_captured_identity(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    sibling: str,
) -> None:
    import import_pack_cleanup as packs
    from download_identity import DownloadIdentity

    key, queue_id = terminal_pack_candidate(producer_media, nested=False)
    sibling_id = producer_queue(producer_media)
    if sibling == "active_publication":
        _ = producer_publication(producer_media, sibling_id, "staging")
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        db.row_factory = sqlite3.Row
        _ = db.execute(
            "UPDATE import_queue SET download_id='matrix-generated',download_protocol='torrent',download_client_id=1 WHERE id=?",
            (sibling_id,),
        )
        if sibling in {"partial", "importing"}:
            _ = db.execute(
                "UPDATE import_queue SET status=? WHERE id=?", (sibling, sibling_id)
            )
        elif sibling in {"leased_terminal", "active_publication", "terminal_unleased"}:
            _ = db.execute(
                "UPDATE import_queue SET status='failed' WHERE id=?", (sibling_id,)
            )
            if sibling == "leased_terminal":
                _ = db.execute(
                    "UPDATE import_queue SET lease_owner='legacy-unexpired-irrelevant' WHERE id=?",
                    (sibling_id,),
                )
        elif sibling == "unknown_protocol":
            _ = db.execute(
                "UPDATE import_queue SET download_protocol=NULL WHERE id=?",
                (sibling_id,),
            )
        elif sibling == "null_client":
            _ = db.execute(
                "UPDATE import_queue SET download_client_id=NULL WHERE id=?",
                (sibling_id,),
            )
        elif sibling == "torrent_case":
            _ = db.execute(
                "UPDATE import_queue SET download_id=' MATRIX-GENERATED ' WHERE id=?",
                (sibling_id,),
            )
        elif sibling == "other_owner":
            # Fixture already has client1; concrete independent owner needs no config lookup.
            _ = db.execute(
                "UPDATE import_queue SET download_client_id=2 WHERE id=?", (sibling_id,)
            )
        elif sibling == "other_protocol":
            _ = db.execute(
                "UPDATE import_queue SET download_protocol='nzb' WHERE id=?",
                (sibling_id,),
            )
        elif sibling == "other_id":
            _ = db.execute(
                "UPDATE import_queue SET download_id='independent-pack' WHERE id=?",
                (sibling_id,),
            )
        allowed = sibling in {
            "other_owner",
            "other_protocol",
            "other_id",
            "terminal_unleased",
        }
        actual = packs._terminal_cleanup_eligible(
            db,
            queue_id=queue_id,
            identity=DownloadIdentity(1, "torrent", "matrix-generated"),
        )
        assert actual is allowed
    inventory = capture_inventory(producer_media, monkeypatch)
    path = next(
        r.path
        for r in inventory.closure.roles
        if r.owner == OwnerRef("pack_reservation", (key,))
        and r.role.relative == "unreferenced.data"
    )
    decisions = terminal_pack_decisions(inventory, key, {path})
    if allowed:
        inputs = review_documents(inventory, decisions)
        with terminal_pack_boundaries(monkeypatch):
            plan = recovery.build_recovery_plan(inventory, *inputs)
            assert replace(plan) == recovery.RecoveryPlan.from_json(plan.to_json())
    else:
        terminal_pack_refusal(producer_media, monkeypatch, inventory, decisions)


@pytest.mark.parametrize(
    "selected",
    [
        "referenced_file",
        "whole_tree",
        "partial_subtree",
        "marker",
        "carrier",
        "carrier_marker",
        "namespace",
    ],
)
def test_terminal_pack_selection_never_delegates_private_support_or_queue_authority(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    selected: str,
) -> None:
    key, _ = terminal_pack_candidate(producer_media, nested=True)
    inventory = capture_inventory(producer_media, monkeypatch)
    owner = OwnerRef("pack_reservation", (key,))
    roles = [r for r in inventory.closure.roles if r.owner == owner]
    root = next(r.path for r in roles if r.role.purpose == "pack_canonical")
    if selected == "referenced_file":
        paths = {root + "/Matrix v02.cbz"}
    elif selected == "whole_tree":
        paths = {r.path for r in roles if r.role.purpose.startswith("pack_canonical")}
        # This projection has two separately retained/selected root bindings.
        subjects = {r.role for r in roles if r.path in paths}
        decisions = tuple(
            recovery.RecoveryDecision(
                s.owner,
                "cleanup-committed" if s.owner == owner else "retain-blocked",
                tuple(r.role for r in roles if r.role in subjects)
                if s.owner == owner
                else (),
                None,
                tuple(
                    r.role
                    for r in inventory.closure.roles
                    if r.owner == s.owner
                    and (s.owner != owner or r.role not in subjects)
                ),
            )
            for s in inventory.closure.snapshots
        )
        terminal_pack_refusal(producer_media, monkeypatch, inventory, decisions)
        return
    elif selected == "partial_subtree":
        paths = {root + "/nested"}
    elif selected == "namespace":
        namespace = next(
            s.owner
            for s in inventory.closure.snapshots
            if s.owner.domain == "namespace"
        )
        decisions = chosen(
            inventory, namespace, "cleanup-committed", {"namespace_marker"}
        )
        terminal_pack_refusal(producer_media, monkeypatch, inventory, decisions)
        return
    else:
        purpose = {
            "marker": "pack_canonical_marker",
            "carrier": "pack_placement_carrier",
            "carrier_marker": "pack_placement_marker",
        }[selected]
        paths = {next(r.path for r in roles if r.role.purpose == purpose)}
    terminal_pack_refusal(
        producer_media,
        monkeypatch,
        inventory,
        terminal_pack_decisions(inventory, key, paths),
    )


@pytest.mark.parametrize(
    "drift",
    [
        "file_missing",
        "file_changed",
        "retained_file_changed",
        "pack_marker_missing",
        "pack_marker_changed",
        "carrier_marker_missing",
        "carrier_marker_changed",
        "namespace_marker_missing",
        "namespace_marker_changed",
        "logical_occupied",
        "generation",
        "placement_phase",
        "live_queue",
        "leased_queue",
    ],
)
def test_terminal_pack_changed_private_generation_or_support_refuses(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    from private_pack_claim import PackOwnership

    key, queue_id = terminal_pack_candidate(producer_media, nested=False)
    initial = capture_inventory(producer_media, monkeypatch)
    owner = OwnerRef("pack_reservation", (key,))
    path = next(
        r.path
        for r in initial.closure.roles
        if r.owner == owner and r.role.relative == "unreferenced.data"
    )
    decisions = terminal_pack_decisions(initial, key, {path})
    roles = [o for o in initial.observed if o.spec.owner == owner]
    if drift in {"file_missing", "file_changed", "retained_file_changed"}:
        changed = (
            Path(path)
            if drift != "retained_file_changed"
            else Path(
                next(
                    o.spec.path
                    for o in roles
                    if o.spec.role.relative == "Matrix v02.cbz"
                )
            )
        )
        if drift == "file_missing":
            changed.unlink()
        else:
            _ = changed.write_bytes(b"changed private tree contents")
    elif "marker" in drift:
        purpose = (
            "namespace_marker"
            if drift.startswith("namespace")
            else "pack_placement_marker"
            if drift.startswith("carrier")
            else "pack_canonical_marker"
        )
        changed = Path(
            next(
                o.spec.path for o in initial.observed if o.spec.role.purpose == purpose
            )
        )
        if drift.endswith("missing"):
            changed.unlink()
        else:
            _ = changed.write_bytes(b"changed required support marker")
    elif drift == "logical_occupied":
        Path(
            next(o.spec.path for o in roles if o.spec.role.purpose == "pack_logical")
        ).mkdir()
    else:
        with closing(sqlite3.connect(producer_media.db_path)) as db, db:
            if drift in {"live_queue", "leased_queue"}:
                sql = (
                    "UPDATE import_queue SET status='pending' WHERE id=?"
                    if drift == "live_queue"
                    else "UPDATE import_queue SET lease_owner='unit-foreign-owner' WHERE id=?"
                )
                _ = db.execute(sql, (queue_id,))
            else:
                row = cast(
                    tuple[str],
                    db.execute(
                        "SELECT directory_ownership_json FROM import_pack_cleanup_reservations WHERE download_identity_key=?",
                        (key,),
                    ).fetchone(),
                )
                ownership = PackOwnership.from_json(row[0])
                placement = ownership.placement_carrier
                assert placement is not None
                if drift == "placement_phase":
                    changed_ownership = replace(
                        ownership,
                        placement_carrier=replace(placement, phase="discarding"),
                    )
                else:
                    token = ownership.artifact_owner_token + "-different-generation"
                    changed_ownership = replace(
                        ownership,
                        artifact_owner_token=token,
                        placement_carrier=replace(
                            placement,
                            binding=replace(placement.binding, operation_key=token),
                            origin_path=placement.origin_path + "-different-generation",
                        ),
                    )
                _ = db.execute(
                    "UPDATE import_pack_cleanup_reservations SET directory_ownership_json=? WHERE download_identity_key=?",
                    (changed_ownership.to_json(), key),
                )
    inventory = capture_inventory(producer_media, monkeypatch)
    terminal_pack_refusal(producer_media, monkeypatch, inventory, decisions)


@pytest.mark.parametrize("receipt", ["field", "null", "malformed"])
def test_terminal_pack_v3_mandatory_receipt_fails_at_inventory_not_dispatch(
    producer_media: ProducerMedia,
    receipt: str,
) -> None:
    key, _ = terminal_pack_candidate(producer_media, nested=False)
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        row = cast(
            tuple[str],
            db.execute(
                "SELECT directory_ownership_json FROM import_pack_cleanup_reservations WHERE download_identity_key=?",
                (key,),
            ).fetchone(),
        )
        value = parse_document(row[0])
        if receipt == "field":
            del value["queue_receipt_sha256"]
        else:
            value["queue_receipt_sha256"] = (
                None if receipt == "null" else "not-a-receipt"
            )
        _ = db.execute(
            "UPDATE import_pack_cleanup_reservations SET directory_ownership_json=? WHERE download_identity_key=?",
            (encoded(value), key),
        )
    with pytest.raises(
        InvalidIdentity, match="invalid persisted domain reference evidence"
    ):
        _ = captured_fixture(producer_media)


@pytest.mark.parametrize(
    "refresh",
    ["opaque_receipt", "torrent_case", "external_input_directory", "not_all_skipped"],
)
def test_terminal_pack_historical_bytes_do_not_become_pending_queue_cas(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    refresh: str,
) -> None:
    key, queue_id = terminal_pack_candidate(producer_media, nested=False)
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        if refresh == "opaque_receipt":
            row = cast(
                tuple[str],
                db.execute(
                    "SELECT directory_ownership_json FROM import_pack_cleanup_reservations WHERE download_identity_key=?",
                    (key,),
                ).fetchone(),
            )
            value = parse_document(row[0])
            # With no independently persisted preimage, another well-formed
            # opaque hash cannot be detected as changed. This is not provenance.
            value["queue_receipt_sha256"] = "a" * 64
            _ = db.execute(
                "UPDATE import_pack_cleanup_reservations SET directory_ownership_json=? WHERE download_identity_key=?",
                (encoded(value), key),
            )
        elif refresh == "torrent_case":
            _ = db.execute(
                "UPDATE import_queue SET download_id=' MATRIX-GENERATED ' WHERE id=?",
                (queue_id,),
            )
        elif refresh == "external_input_directory":
            _ = db.execute(
                "UPDATE import_queue SET src_dir=? WHERE id=?",
                (str(producer_media.incoming), queue_id),
            )
        else:
            _ = db.execute(
                "UPDATE import_queue_files SET status='failed' WHERE queue_id=?",
                (queue_id,),
            )
    inventory = capture_inventory(producer_media, monkeypatch)
    path = next(
        r.path
        for r in inventory.closure.roles
        if r.owner == OwnerRef("pack_reservation", (key,))
        and r.role.relative == "unreferenced.data"
    )
    inputs = review_documents(
        inventory, terminal_pack_decisions(inventory, key, {path})
    )
    with terminal_pack_boundaries(monkeypatch):
        plan = recovery.build_recovery_plan(inventory, *inputs)
        assert replace(plan) == recovery.RecoveryPlan.from_json(plan.to_json())


def test_terminal_pack_uncaptured_seen_cannot_prove_nullable_sibling_disjoint(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_pack_cleanup as packs
    from download_identity import DownloadIdentity

    key, queue_id = terminal_pack_candidate(producer_media, nested=False)
    sibling_id = producer_queue(producer_media)
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        db.row_factory = sqlite3.Row
        _ = db.execute(
            "UPDATE import_queue SET download_id='matrix-generated',download_protocol=NULL WHERE id=?",
            (sibling_id,),
        )
        _ = db.execute(
            "INSERT INTO seen(torrent_url,series_id,protocol,download_client_id,download_id) VALUES('unit-only-source',7,'nzb',1,'matrix-generated')"
        )
        assert packs._terminal_cleanup_eligible(
            db,
            queue_id=queue_id,
            identity=DownloadIdentity(1, "torrent", "matrix-generated"),
        )
    inventory = capture_inventory(producer_media, monkeypatch)
    assert "seen" not in {r.table for r in inventory.closure.raw_rows}
    path = next(
        r.path
        for r in inventory.closure.roles
        if r.owner == OwnerRef("pack_reservation", (key,))
        and r.role.relative == "unreferenced.data"
    )
    # Captured facts cannot establish the live resolver's opposite protocol.
    # Refusal is uncertainty, not runtime equivalence or a permanent NULL ban.
    terminal_pack_refusal(
        producer_media,
        monkeypatch,
        inventory,
        terminal_pack_decisions(inventory, key, {path}),
    )


def test_terminal_pack_transferred_claim_source_remains_represented_veto_evidence(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil
    import import_pack_cleanup as packs
    import file_identity_consumers as consumers
    from download_identity import DownloadIdentity
    from private_pack_claim import PackDirectoryClaim

    key, queue_id = producer_pack(producer_media, "attached", tombstone=True)
    assert queue_id is not None
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        db.row_factory = sqlite3.Row
        rows = terminal_pack_sql_rows(
            db,
            "SELECT * FROM import_pack_cleanup_tombstones WHERE download_identity_key=?",
            (key,),
        )
        assert len(rows) == 1
        row = rows[0]
        claim = PackDirectoryClaim.from_json(cast(str, row["carrier_json"]))
        assert not db.execute(
            "SELECT 1 FROM import_pack_cleanup_reservations"
        ).fetchall()
        # Existing producer's legacy terminal seed plus explicit skipped-child
        # refresh is negative evidence, not the genuine owner-CAS positive route.
        _ = db.execute(
            "UPDATE import_queue_files SET status='skipped' WHERE queue_id=?",
            (queue_id,),
        )
        assert packs._retained_skipped_pack_uses_source(
            db,
            queue_id=queue_id,
            identity=DownloadIdentity(1, "torrent", "matrix-generated"),
        )
    # Deliberately reoccupied original name, NOT a restore acknowledgement.
    # This allows every original/source role to be observed without v2 rules.
    _ = shutil.copytree(
        claim.carrier.carrier_path + "/artifact", claim.source_directory.path
    )
    inventory = capture_inventory(producer_media, monkeypatch)
    inputs = review_documents(inventory)
    snapshot = next(
        s for s in inventory.closure.snapshots if s.owner.domain == "pack_tombstone"
    )
    domain_inventory = consumers._inventory(snapshot)
    root, records = consumers._snapshot_records(snapshot, domain_inventory.evidence)
    original, media, control, wire = (
        captured_fixture(producer_media)[1],
        media_snapshot(producer_media.scope),
        control_snapshot(producer_media.db_path),
        inventory.to_json(),
    )
    with terminal_pack_boundaries(monkeypatch):
        plan = recovery.build_recovery_plan(inventory, *inputs)
        owner = recovery._PlanOwner(
            plan,
            snapshot,
            root,
            records,
            {
                o.spec.role: o
                for o in inventory.observed
                if o.spec.owner == snapshot.owner
            },
            domain_inventory.carriers,
        )
        queue = next(
            dict[str, object](r.values)
            for r in inventory.closure.raw_rows
            if r.table == "import_queue" and dict(r.values)["id"] == queue_id
        )
        assert recovery._pack_retained_source_veto(owner, queue)
        assert recovery.RecoveryPlan.from_json(plan.to_json()) == plan
    assert captured_fixture(producer_media)[1] == original
    assert media_snapshot(producer_media.scope) == media
    assert control_snapshot(producer_media.db_path) == control
    assert inventory.to_json() == wire


@pytest.mark.parametrize("bound_identity", [False, True])
def test_terminal_pack_real_queue_builder_keeps_external_input_directory(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    bound_identity: bool,
) -> None:
    import import_queue
    import import_pack_cleanup as packs
    import private_file_claim as claims
    from download_identity import DownloadIdentity, download_identity_key
    from import_lease import claim_import_queue_row, transition_import_queue_row
    from private_pack_claim import inventory_tree, open_directory
    from typing import Protocol

    class QueueBuilder(Protocol):
        def __call__(
            self,
            db: sqlite3.Connection,
            series_id: int,
            download_id: str,
            torrent_name: str,
            torrent_url: str,
            volume_num: float | None,
            content_path: str,
            *,
            download_client_id: int | None,
            protocol: str | None,
            respect_grab_claims: bool,
        ) -> tuple[int | None, bool]: ...

    queue_builder = cast(QueueBuilder, getattr(import_queue, "_queue_import"))

    source = producer_media.incoming / "actual-builder"
    chapter = source / "Matrix c002"
    chapter.mkdir(parents=True)
    _ = (chapter / "001.jpg").write_bytes(
        b"fixture page bytes packed without image decoding"
    )
    pack_root = producer_media.scope / "packs"
    pack_root.mkdir(mode=0o750)
    with file_mutation_guard(str(producer_media.db_path)) as guard:
        with claims.ensure_namespace(guard, str(pack_root)):
            pass
    original_pack = import_queue._pack_image_dir_to_cbz

    def pack_with_private_leaf(
        src: str, dst: str, checkpoint: Callable[[], None]
    ) -> int | None:
        size = original_pack(src, dst, checkpoint)
        # Actual additional private producer bytes before inventory persistence,
        # not another file row, receipt, fake schema or mixed-source assertion.
        _ = (Path(dst).parent / "builder-private.data").write_bytes(
            b"private auxiliary output"
        )
        return size

    def unsupported(_source: str, _destination: str) -> NoReturn:
        raise OSError(
            errno.EOPNOTSUPP, "unit unsupported placement; no native qualification"
        )

    client_id = 1 if bound_identity else None
    protocol = "torrent" if bound_identity else None
    with monkeypatch.context() as patch:
        patch.setattr(import_queue, "_pack_image_dir_to_cbz", pack_with_private_leaf)
        patch.setattr(packs, "_rename_noreplace", unsupported)
        with closing(sqlite3.connect(producer_media.db_path)) as db, db:
            db.row_factory = sqlite3.Row
            queue_id, _ = queue_builder(
                db,
                7,
                "actual-builder",
                "Matrix c002",
                "magnet:actual-builder",
                None,
                str(source),
                download_client_id=client_id,
                protocol=protocol,
                respect_grab_claims=False,
            )
    assert queue_id is not None
    identity = DownloadIdentity(client_id, protocol, "actual-builder")
    key = download_identity_key(identity)
    reservation = packs._read_reservation(key)
    assert reservation is not None and reservation.purpose == "queueing"
    ownership = packs._ownership(reservation)
    assert (
        ownership is not None
        and ownership.canonical_directory is not None
        and ownership.inventory is not None
    )
    proof = ownership.canonical_directory
    with open_directory(proof.path) as fd:
        assert inventory_tree(fd) == ownership.inventory
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        db.row_factory = sqlite3.Row
        queue = terminal_pack_sql_rows(
            db, "SELECT * FROM import_queue WHERE id=?", (queue_id,)
        )[0]
        files = terminal_pack_sql_rows(
            db, "SELECT * FROM import_queue_files WHERE queue_id=?", (queue_id,)
        )
        assert queue["src_dir"] == str(source) != proof.path
        assert (
            len(files) == 1 and files[0]["src_path"] == proof.path + "/Matrix c002.cbz"
        )
        assert claim_import_queue_row(db, queue_id, "real-builder-terminal")
        assert transition_import_queue_row(
            db, queue_id, "real-builder-terminal", "failed"
        )
    assert (
        packs._acquire_cleanup_reservation(
            queue_id=queue_id,
            download_id="actual-builder",
            download_client_id=client_id,
            protocol=protocol,
            publication_id=None,
            lease_seconds=None,
        )
        is not None
    )
    with closing(sqlite3.connect(producer_media.db_path)) as db:
        db.row_factory = sqlite3.Row
        assert packs._terminal_cleanup_eligible(
            db, queue_id=queue_id, identity=identity
        )
    inventory = capture_inventory(producer_media, monkeypatch)
    paths = {proof.path + "/builder-private.data"}
    decisions = terminal_pack_decisions(inventory, key, paths)
    inputs = review_documents(inventory, decisions)
    rows, media, control, wire = (
        captured_fixture(producer_media)[1],
        media_snapshot(producer_media.scope),
        control_snapshot(producer_media.db_path),
        inventory.to_json(),
    )
    _ = (tmp_path / "real-builder-provenance.json").write_text(
        encoded(
            {
                "queue_id": queue_id,
                "src_dir": queue["src_dir"],
                "canonical_path": proof.path,
                "files": files,
                "input": "image-only; no mixed-payload expectation",
                "generated_only_output_with_external_input_directory": True,
                "ownership_json": ownership.to_json(),
                "selected_paths": sorted(paths),
                "bound_identity": bound_identity,
            }
        )
        + "\n"
    )
    with terminal_pack_boundaries(monkeypatch):
        plan = recovery.build_recovery_plan(inventory, *inputs)
        assert replace(plan) == recovery.RecoveryPlan.from_json(plan.to_json())
    assert captured_fixture(producer_media)[1] == rows
    assert media_snapshot(producer_media.scope) == media
    assert control_snapshot(producer_media.db_path) == control
    assert inventory.to_json() == wire


@pytest.mark.parametrize("media_error", ["omit", "retain", "blank_reference"])
def test_terminal_pack_each_destructive_subject_requires_snapshot_coverage(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    media_error: str,
) -> None:
    key, _ = terminal_pack_candidate(producer_media, nested=True)
    inventory = capture_inventory(producer_media, monkeypatch)
    owner = OwnerRef("pack_reservation", (key,))
    paths = {
        r.path
        for r in inventory.closure.roles
        if r.owner == owner
        and r.role.relative
        in {"nested", "nested/extra.cbz", "empty", "unreferenced.data"}
    }
    decisions = terminal_pack_decisions(inventory, key, paths)
    inputs = review_documents(inventory, decisions)
    before_rows, media, control, original = (
        captured_fixture(producer_media)[1],
        media_snapshot(producer_media.scope),
        control_snapshot(producer_media.db_path),
        inventory.to_json(),
    )
    with terminal_pack_boundaries(monkeypatch):
        valid = recovery.build_recovery_plan(inventory, *inputs)
    selected = next(d.roles for d in decisions if d.owner == owner)
    assert len(selected) == 4
    for role in selected:
        selection, declaration, receipt = (parse_document(d) for d in inputs)
        records = wire_array(receipt["media"])
        record = next(
            wire_object(r)
            for r in records
            if wire_object(r)["owner"] == recovery._owner_value(owner)
            and wire_object(r)["role"] == recovery._role_value(role)
        )
        assert record["handling"] == "snapshot" and record["recovery_reference"]
        if media_error == "omit":
            records.remove(record)
        elif media_error == "retain":
            record["handling"] = "retain-in-place"
            record["recovery_reference"] = None
        else:
            record["recovery_reference"] = ""
        changed = resign_documents(selection, declaration, receipt)
        wire = parse_document(valid.to_json())
        wire["declaration_json"], wire["backup_receipt_json"] = changed[1:]
        sha = digest({k: v for k, v in wire.items() if k != "sha256"})
        wire["sha256"] = sha
        with terminal_pack_boundaries(monkeypatch):
            with pytest.raises(InvalidIdentity):
                _ = recovery.build_recovery_plan(inventory, *changed)
            with pytest.raises(InvalidIdentity):
                _ = recovery.RecoveryPlan.from_json(encoded(wire))
            with pytest.raises(InvalidIdentity):
                _ = replace(
                    valid,
                    declaration_json=changed[1],
                    backup_receipt_json=changed[2],
                    sha256=sha,
                )
    assert captured_fixture(producer_media)[1] == before_rows
    assert media_snapshot(producer_media.scope) == media
    assert control_snapshot(producer_media.db_path) == control
    assert inventory.to_json() == original


def test_pack_validation_legacy_v2_terminal_retains_but_cannot_cleanup(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key, _ = terminal_pack_candidate(producer_media, nested=False)
    with closing(sqlite3.connect(producer_media.db_path)) as db, db:
        row = cast(
            tuple[str],
            db.execute(
                "SELECT directory_ownership_json FROM import_pack_cleanup_reservations WHERE download_identity_key=?",
                (key,),
            ).fetchone(),
        )
        value = parse_document(row[0])
        assert value["version"] == 3 and value["queue_receipt_sha256"] is not None
        # Explicit negative legacy seed, never a successful producer downgrade.
        value["version"] = 2
        del value["queue_receipt_sha256"]
        _ = db.execute(
            "UPDATE import_pack_cleanup_reservations SET directory_ownership_json=? WHERE download_identity_key=?",
            (encoded(value), key),
        )
    inventory = capture_inventory(producer_media, monkeypatch)
    inputs = review_documents(inventory)
    rows, media, control, wire = (
        captured_fixture(producer_media)[1],
        media_snapshot(producer_media.scope),
        control_snapshot(producer_media.db_path),
        inventory.to_json(),
    )
    with terminal_pack_boundaries(monkeypatch):
        retained = recovery.build_recovery_plan(inventory, *inputs)
        assert all(d.action == "retain-blocked" for d in retained.decisions)
        assert replace(retained) == recovery.RecoveryPlan.from_json(retained.to_json())
    assert captured_fixture(producer_media)[1] == rows
    assert media_snapshot(producer_media.scope) == media
    assert control_snapshot(producer_media.db_path) == control
    assert inventory.to_json() == wire
    owner = OwnerRef("pack_reservation", (key,))
    path = next(
        r.path
        for r in inventory.closure.roles
        if r.owner == owner and r.role.relative == "unreferenced.data"
    )
    terminal_pack_refusal(
        producer_media,
        monkeypatch,
        inventory,
        terminal_pack_decisions(inventory, key, {path}),
    )


def pack_validation_actual_image_queue(
    env: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    *,
    bound_identity: bool,
) -> tuple[str, int]:
    import import_queue
    import import_pack_cleanup as packs
    import private_file_claim as claims
    from download_identity import DownloadIdentity, download_identity_key
    from import_pack_cleanup import _PackReservation
    from file_mutation_lock import FileMutationGuard
    from private_pack_claim import inventory_tree, open_directory
    from typing import Protocol

    class QueueBuilder(Protocol):
        def __call__(
            self,
            db: sqlite3.Connection,
            series_id: int,
            download_id: str,
            torrent_name: str,
            torrent_url: str,
            volume_num: float | None,
            content_path: str,
            *,
            download_client_id: int | None,
            protocol: str | None,
            respect_grab_claims: bool,
        ) -> tuple[int | None, bool]: ...

    queue_builder = cast(QueueBuilder, getattr(import_queue, "_queue_import"))
    source = env.incoming / "queued-real-images"
    leaf = source / "Matrix c002"
    leaf.mkdir(parents=True)
    _ = (leaf / "001.jpg").write_bytes(b"real image packer unit page")
    root = env.scope / "packs"
    root.mkdir(mode=0o750)
    with file_mutation_guard(str(env.db_path)) as guard:
        with claims.ensure_namespace(guard, str(root)):
            pass
    owner_id = 1 if bound_identity else None
    protocol = "torrent" if bound_identity else None
    key = download_identity_key(
        DownloadIdentity(owner_id, protocol, "queued-real-images")
    )
    calls: list[str] = []
    known_fenced = packs._known_fenced_generated_queue

    def finish_after_commit(
        _guard: FileMutationGuard, reservation: _PackReservation, queue_id: int
    ) -> NoReturn:
        current = packs._read_reservation(reservation.download_identity_key)
        assert (
            current is not None
            and current.queue_id == queue_id
            and current.purpose == "cleanup"
        )
        ownership = packs._ownership(current)
        assert (
            ownership is not None
            and ownership.phase == "queued"
            and ownership.version == 3
        )
        with closing(sqlite3.connect(env.db_path)) as db:
            db.row_factory = sqlite3.Row
            assert packs._queue_receipt(db, queue_id) == ownership.queue_receipt_sha256
        calls.append("finish-after-real-commit")
        raise OSError(errno.EIO, "unit bounded finish checkpoint")

    def checked_fence(reservation: _PackReservation, queue_id: int) -> bool:
        assert calls == ["finish-after-real-commit"]
        valid = known_fenced(reservation, queue_id)
        assert valid is True
        calls.append("real-known-fenced-verified")
        return valid

    def unsupported(_source: str, _destination: str) -> NoReturn:
        raise OSError(errno.EOPNOTSUPP, "unit fallback, not a native trial")

    with monkeypatch.context() as patch:
        patch.setattr(packs, "_rename_noreplace", unsupported)
        patch.setattr(import_queue, "_finish_generated_pack_queue", finish_after_commit)
        patch.setattr(import_queue, "_known_fenced_generated_queue", checked_fence)
        with closing(sqlite3.connect(env.db_path)) as db, db:
            db.row_factory = sqlite3.Row
            queue_id, _ = queue_builder(
                db,
                7,
                "queued-real-images",
                "Matrix c002",
                "magnet:queued-real-images",
                None,
                str(source),
                download_client_id=owner_id,
                protocol=protocol,
                respect_grab_claims=False,
            )
    assert queue_id is not None and calls == [
        "finish-after-real-commit",
        "real-known-fenced-verified",
    ]
    current = packs._read_reservation(key)
    assert current is not None and current.purpose == "cleanup"
    ownership = packs._ownership(current)
    assert (
        ownership is not None and ownership.phase == "queued" and ownership.version == 3
    )
    assert ownership.private_directory is None and ownership.claims == ()
    assert ownership.canonical_directory is not None and ownership.inventory is not None
    assert (
        ownership.placement_carrier is not None
        and ownership.placement_carrier.phase == "claimed"
    )
    with open_directory(ownership.canonical_directory.path) as fd:
        assert inventory_tree(fd) == ownership.inventory
    with closing(sqlite3.connect(env.db_path)) as db:
        db.row_factory = sqlite3.Row
        queue = terminal_pack_sql_rows(
            db, "SELECT * FROM import_queue WHERE id=?", (queue_id,)
        )[0]
        assert queue["status"] == "pending" and queue["lease_owner"] is None
        assert queue["src_dir"] == str(source) != ownership.canonical_directory.path
        assert packs._queue_receipt(db, queue_id) == ownership.queue_receipt_sha256
    return key, queue_id


@contextmanager
def pack_validation_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None, None, None]:
    import import_queue

    def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("pure plan used live queued-generation settlement")

    with terminal_pack_boundaries(monkeypatch), monkeypatch.context() as patch:
        patch.setattr(import_queue, "_finish_generated_pack_queue", forbidden)
        patch.setattr(import_queue, "_known_fenced_generated_queue", forbidden)
        yield


@pytest.mark.parametrize("bound_identity", [False, True])
def test_pack_validation_actual_queued_image_generation_keeps_external_root(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    bound_identity: bool,
) -> None:
    key, queue_id = pack_validation_actual_image_queue(
        producer_media, monkeypatch, bound_identity=bound_identity
    )
    inventory = capture_inventory(producer_media, monkeypatch)
    decisions = pack_queue_decisions(inventory, key)
    owner = OwnerRef("pack_reservation", (key,))
    canonical = next(
        o
        for o in inventory.observed
        if o.spec.owner == owner and o.spec.role.purpose == "pack_canonical"
    )
    source = next(
        o
        for o in inventory.observed
        if o.spec.owner == OwnerRef("queue_source", (queue_id,))
        and o.spec.role.purpose == "source_directory"
    )
    assert (
        canonical.outcome == source.outcome == "observed"
        and canonical.spec.path != source.spec.path
    )
    assert all(o.outcome in {"observed", "absent"} for o in inventory.observed)
    inputs = review_documents(inventory, decisions)
    rows, media, control, wire = (
        captured_fixture(producer_media)[1],
        media_snapshot(producer_media.scope),
        control_snapshot(producer_media.db_path),
        inventory.to_json(),
    )
    _ = (tmp_path / "queued-real-proof.json").write_text(
        encoded(
            {
                "key": key,
                "queue_id": queue_id,
                "bound_identity": bound_identity,
                "canonical": canonical.spec.path,
                "external_input": source.spec.path,
                "real_commit_before_injected_finish_error": True,
                "real_known_fenced_verified": True,
                "inventory_sha256": inventory.sha256,
            }
        )
        + "\n"
    )
    with pack_validation_boundaries(monkeypatch):
        plan = recovery.build_recovery_plan(inventory, *inputs)
        assert plan.decisions == decisions
        assert replace(plan) == recovery.RecoveryPlan.from_json(plan.to_json())
    assert captured_fixture(producer_media)[1] == rows
    assert media_snapshot(producer_media.scope) == media
    assert control_snapshot(producer_media.db_path) == control
    assert inventory.to_json() == wire


@pytest.mark.parametrize(
    "drift",
    [
        "incoming_old_receipt",
        "partial",
        "file_changed",
        "pack_marker_changed",
        "carrier_marker_changed",
        "executing",
    ],
)
def test_pack_validation_real_queued_guards_survive_external_root_correction(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    import import_pack_cleanup as packs
    import import_queue

    key, queue_id = pack_validation_actual_image_queue(
        producer_media, monkeypatch, bound_identity=True
    )
    initial = capture_inventory(producer_media, monkeypatch)
    decisions = pack_queue_decisions(initial, key)
    owner = OwnerRef("pack_reservation", (key,))
    if drift == "partial":
        removed = next(
            r.role
            for r in initial.closure.roles
            if r.owner == owner and r.role.purpose == "pack_canonical_file"
        )
        decisions = tuple(
            replace(
                d,
                roles=tuple(r for r in d.roles if r != removed),
                retained_roles=tuple(
                    r.role
                    for r in initial.closure.roles
                    if r.owner == owner
                    and (r.role in d.retained_roles or r.role == removed)
                ),
            )
            if d.owner == owner
            else d
            for d in decisions
        )
    elif drift in {"incoming_old_receipt", "executing"}:
        alternate = producer_media.scope / "alternate-incoming"
        alternate.mkdir()
        with closing(sqlite3.connect(producer_media.db_path)) as db, db:
            db.row_factory = sqlite3.Row
            before = packs._queue_receipt(db, queue_id)
            if drift == "incoming_old_receipt":
                _ = db.execute(
                    "UPDATE import_queue SET src_dir=? WHERE id=?",
                    (str(alternate), queue_id),
                )
            else:
                _ = db.execute(
                    "UPDATE import_queue SET status='importing' WHERE id=?", (queue_id,)
                )
            assert packs._queue_receipt(db, queue_id) != before
    else:
        purpose = {
            "file_changed": "pack_canonical_file",
            "pack_marker_changed": "pack_canonical_marker",
            "carrier_marker_changed": "pack_placement_marker",
        }[drift]
        path = next(
            r.path
            for r in initial.closure.roles
            if r.owner == owner and r.role.purpose == purpose
        )
        _ = Path(path).write_bytes(b"changed currently observed original support")
    inventory = capture_inventory(producer_media, monkeypatch)

    def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("refused plan attempted live queue settlement")

    with monkeypatch.context() as patch:
        patch.setattr(import_queue, "_finish_generated_pack_queue", forbidden)
        patch.setattr(import_queue, "_known_fenced_generated_queue", forbidden)
        pack_queue_refusal(producer_media, monkeypatch, inventory, decisions)


def pack_action_checkpoint(env: ProducerMedia, phase: str) -> tuple[str, int | None]:
    """Use producer writers; ready/retained retain the fixture's phase projections."""
    if phase == "intent":
        # A pre-existing cache parent permits anchored leaf absence, not proof
        # that an unrecorded allocation or original file ever existed.
        (env.scope / "packs").mkdir(mode=0o755)
    if phase == "ready":
        return ready_placement_candidate(env, nested=True), None
    return producer_pack(env, phase)


def pack_action_retention_roundtrip(
    env: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    inventory: recovery.OwnershipInventory,
) -> recovery.RecoveryPlan:
    assert inventory.unresolved == ()
    assert tuple(o.spec for o in inventory.observed) == inventory.closure.roles
    assert all(
        not o.unresolved and o.outcome in {"observed", "absent"}
        for o in inventory.observed
    )
    inputs = review_documents(inventory)
    rows, media, control, wire = (
        captured_fixture(env)[1],
        media_snapshot(env.scope),
        control_snapshot(env.db_path),
        inventory.to_json(),
    )
    with pack_validation_boundaries(monkeypatch):
        plan = recovery.build_recovery_plan(inventory, *inputs)
        assert plan.inventory == inventory
        assert (
            recovery.RecoveryPlan(
                plan.version,
                inventory,
                plan.decisions,
                plan.declaration_json,
                plan.backup_receipt_json,
                plan.epoch_bindings_json,
                plan.sha256,
            )
            == recovery.RecoveryPlan.from_json(plan.to_json())
            == plan
        )
    assert tuple(d.owner for d in plan.decisions) == tuple(
        s.owner for s in inventory.closure.snapshots
    )
    for decision in plan.decisions:
        assert decision.action == "retain-blocked"
        assert decision.roles == () and decision.destination is None
        assert decision.retained_roles == tuple(
            r.role for r in inventory.closure.roles if r.owner == decision.owner
        )
    assert sum(len(d.retained_roles) for d in plan.decisions) == len(
        inventory.closure.roles
    )
    assert inventory.to_json() == wire
    assert captured_fixture(env)[1] == rows
    assert media_snapshot(env.scope) == media
    assert control_snapshot(env.db_path) == control
    return plan


@pytest.mark.parametrize(
    "phase", ["intent", "building", "ready", "attached", "retained"]
)
def test_pack_action_real_checkpoint_retains_every_role(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    phase: str,
) -> None:
    from private_pack_claim import PackOwnership, inventory_tree, open_directory

    key, queue_id = pack_action_checkpoint(producer_media, phase)
    inventory = capture_inventory(producer_media, monkeypatch)
    assert inventory.unresolved == ()
    assert tuple(o.spec for o in inventory.observed) == inventory.closure.roles
    assert all(not o.unresolved for o in inventory.observed)
    row = next(
        dict(r.values)
        for r in inventory.closure.raw_rows
        if r.table == "import_pack_cleanup_reservations"
        and dict(r.values)["download_identity_key"] == key
    )
    assert row["queue_id"] == queue_id and row["publication_id"] is None
    owner = OwnerRef("pack_reservation", (key,))
    observed = [o for o in inventory.observed if o.spec.owner == owner]
    assert observed
    if phase == "intent":
        assert row["directory_ownership_json"] is None and row["purpose"] == "queueing"
        assert {o.spec.role.purpose for o in observed} == {
            "pack_logical",
            "tombstone_path",
        }
        assert all(o.outcome == "absent" for o in observed)
    else:
        ownership = PackOwnership.from_json(cast(str, row["directory_ownership_json"]))
        assert ownership.phase == phase and ownership.claims == ()
        placement = ownership.placement_carrier
        assert placement is not None and placement.phase == "claimed"
        assert placement.restore_receipt is None
        proof = (
            ownership.private_directory
            if phase == "building"
            else ownership.canonical_directory
        )
        assert proof is not None and proof.path == placement.carrier_path + "/artifact"
        assert proof.marker_fingerprint is not None
        if phase == "building":
            assert ownership.inventory is None and ownership.version == 2
            assert row["purpose"] == "queueing" and queue_id is None
        else:
            assert ownership.inventory is not None
            assert ownership.inventory.files
            with open_directory(proof.path) as fd:
                assert inventory_tree(fd) == ownership.inventory
            assert row["purpose"] == ("cleanup" if phase == "ready" else "queueing")
            assert (queue_id is None) == (phase == "ready")
            assert ownership.version == (2 if phase == "ready" else 3)
            if phase == "ready":
                assert ownership.inventory.directories == ("empty", "nested")
                assert len(ownership.inventory.files) == 2
            else:
                assert ownership.queue_receipt_sha256 is not None
    plan = pack_action_retention_roundtrip(producer_media, monkeypatch, inventory)
    _ = (tmp_path / "pack-action-retention-proof.json").write_text(
        encoded(
            {
                "phase": phase,
                "key": key,
                "queue_id": queue_id,
                "qualification": "existing fixture ready phase projection"
                if phase == "ready"
                else "existing fixture ready and retained phase projections"
                if phase == "retained"
                else "real commit/finish after existing fixture ready phase projection"
                if phase == "attached"
                else "real reservation/allocation writer",
                "inventory_sha256": inventory.sha256,
                "plan_sha256": plan.sha256,
                "owners": len(plan.decisions),
                "roles": len(inventory.closure.roles),
                "observations": [
                    recovery._observed_value(o) for o in inventory.observed
                ],
                "decisions": [d.to_value() for d in plan.decisions],
            }
        )
        + "\n"
    )


@pytest.mark.parametrize(
    "phase,action,purpose",
    [
        ("intent", "cancel-unmutated", "pack_logical"),
        ("building", "cancel-unmutated", "pack_private"),
        ("ready", "cancel-unmutated", "pack_canonical_file"),
        ("ready", "keep-current", "pack_canonical_file"),
        ("attached", "keep-current", "pack_canonical_file"),
        ("retained", "keep-current", "pack_canonical_file"),
    ],
)
def test_pack_action_current_generation_has_no_cancel_or_adoption(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    phase: str,
    action: str,
    purpose: str,
) -> None:
    key, _ = pack_action_checkpoint(producer_media, phase)
    inventory = capture_inventory(producer_media, monkeypatch)
    _ = pack_action_retention_roundtrip(producer_media, monkeypatch, inventory)
    owner = OwnerRef("pack_reservation", (key,))
    subject = next(
        o
        for o in inventory.observed
        if o.spec.owner == owner and o.spec.role.purpose == purpose
    )
    assert subject.outcome == ("absent" if phase == "intent" else "observed")
    if action == "keep-current":
        assert subject.spec.role.kind == "file"
        assert subject.spec.role.purpose == "pack_canonical_file"
    decisions = tuple(
        recovery.RecoveryDecision(
            snapshot.owner,
            action if snapshot.owner == owner else "retain-blocked",
            (subject.spec.role,) if snapshot.owner == owner else (),
            subject.spec.path
            if snapshot.owner == owner and action == "keep-current"
            else None,
            tuple(
                r.role
                for r in inventory.closure.roles
                if r.owner == snapshot.owner
                and (snapshot.owner != owner or r.role != subject.spec.role)
            ),
        )
        for snapshot in inventory.closure.snapshots
    )
    assert sum(len(d.roles) for d in decisions) == 1
    # This helper re-signs selection, declaration, receipt and the complete plan;
    # every entry point must reach phase/context validation, not a hash failure.
    pack_queue_refusal(producer_media, monkeypatch, inventory, decisions)
    _ = (tmp_path / "pack-action-refusal-proof.json").write_text(
        encoded(
            {
                "phase": phase,
                "action": action,
                "inventory_sha256": inventory.sha256,
                "subject": recovery._observed_value(subject),
                "decisions": [d.to_value() for d in decisions],
                "entry_points": ["builder", "constructor", "parser"],
                "expected_error": "ownership_phase|ownership_context",
            }
        )
        + "\n"
    )


def test_pack_action_unknown_building_child_is_not_retention_success(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from private_pack_claim import PackOwnership

    key, _ = pack_action_checkpoint(producer_media, "building")
    closure, _ = captured_fixture(producer_media)
    row = next(
        dict(r.values)
        for r in closure.raw_rows
        if r.table == "import_pack_cleanup_reservations"
    )
    ownership = PackOwnership.from_json(cast(str, row["directory_ownership_json"]))
    assert ownership.private_directory is not None
    _ = (Path(ownership.private_directory.path) / "unclassified.cbz").write_bytes(
        b"not a recorded inventory entry"
    )
    observed = observe_fixture(recovery, producer_media, closure, monkeypatch)
    assert any(
        o.spec.owner == OwnerRef("pack_reservation", (key,))
        and o.outcome == "unknown_children"
        and o.unresolved
        for o in observed
    )
    inventory = recovery.OwnershipInventory.create(
        str(producer_media.scope),
        closure,
        observed,
        tuple(sorted({"role_" + o.outcome for o in observed if o.unresolved})),
        InventoryBudget(),
    )
    rows, media, control, wire = (
        captured_fixture(producer_media)[1],
        media_snapshot(producer_media.scope),
        control_snapshot(producer_media.db_path),
        inventory.to_json(),
    )
    inputs = review_documents(inventory)
    with pack_validation_boundaries(monkeypatch):
        with pytest.raises(InvalidIdentity, match="ownership_incomplete"):
            _ = recovery.build_recovery_plan(inventory, *inputs)
    assert inventory.to_json() == wire
    assert captured_fixture(producer_media)[1] == rows
    assert media_snapshot(producer_media.scope) == media
    assert control_snapshot(producer_media.db_path) == control


def test_pack_action_transferred_claim_preserves_actual_inventory_limit(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from private_pack_claim import PackDirectoryClaim, inventory_tree, open_directory

    key, queue_id = producer_pack(producer_media, "attached", tombstone=True)
    assert queue_id is not None
    closure, rows = captured_fixture(producer_media)
    assert not any(s.owner.domain == "pack_reservation" for s in closure.snapshots)
    snapshot = next(s for s in closure.snapshots if s.owner.domain == "pack_tombstone")
    row = wire_object(wire_object(parse_document(snapshot.rows_json)["root"])["row"])
    claim = PackDirectoryClaim.from_json(cast(str, row["carrier_json"]))
    assert row["download_identity_key"] == key and row["queue_id"] == queue_id
    assert claim.carrier.phase == "claimed" and claim.carrier.restore_receipt is None
    assert claim.inventory.files and not Path(claim.source_directory.path).exists()
    captured = claim.carrier.carrier_path + "/artifact"
    assert row["tombstone_path"] == captured
    with open_directory(captured) as fd:
        assert inventory_tree(fd) == claim.inventory
    observed = observe_fixture(recovery, producer_media, closure, monkeypatch)
    assert tuple(o.spec for o in observed) == closure.roles
    incomplete = [o for o in observed if o.unresolved]
    assert incomplete and all(o.outcome == "unreadable" for o in incomplete)
    assert {o.spec.owner.domain for o in incomplete} == {
        "pack_tombstone",
        "queue_source",
    }
    assert all(
        o.spec.path.startswith(claim.source_directory.path + "/") for o in incomplete
    )
    for relative in claim.inventory.files:
        assert any(
            o.spec.owner == snapshot.owner
            and o.spec.path == captured + "/" + relative
            and o.outcome == "observed"
            and not o.unresolved
            for o in observed
        )
        assert any(
            o.spec.owner == snapshot.owner
            and o.spec.path == claim.source_directory.path + "/" + relative
            and o in incomplete
            for o in observed
        )
    inventory = recovery.OwnershipInventory.create(
        str(producer_media.scope),
        closure,
        observed,
        ("role_unreadable",),
        InventoryBudget(),
    )
    assert recovery.OwnershipInventory.from_json(inventory.to_json()) == inventory
    media, control, wire = (
        media_snapshot(producer_media.scope),
        control_snapshot(producer_media.db_path),
        inventory.to_json(),
    )
    # The complete-inventory prerequisite is unavailable. Do not pretend an
    # incomplete rejection qualifies D resume-commit's semantic action guard.
    with pack_validation_boundaries(monkeypatch):
        with pytest.raises(InvalidIdentity, match="ownership_incomplete"):
            _ = recovery.build_recovery_plan(inventory, *review_documents(inventory))
    _ = (tmp_path / "pack-action-transferred-incomplete.json").write_text(
        encoded(
            {
                "key": key,
                "queue_id": queue_id,
                "qualification": "real capture/transfer; fixture failed-queue seed; no restore",
                "inventory": parse_document(wire),
                "incomplete": [recovery._observed_value(o) for o in incomplete],
                "resume_commit_action_qualified": False,
            }
        )
        + "\n"
    )
    assert inventory.to_json() == wire
    assert captured_fixture(producer_media)[1] == rows
    assert media_snapshot(producer_media.scope) == media
    assert control_snapshot(producer_media.db_path) == control
