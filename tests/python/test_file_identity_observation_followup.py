"""PR491 expected-correctness wire/stat regressions; no recovery authority."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import errno
import sys
import shutil
from dataclasses import replace
from contextlib import closing
from pathlib import Path
from typing import Any, cast, final

import pytest

import file_identity_recovery as observation
from file_identity import IdentityUnavailable, InvalidIdentity, NativeHandle, OwnerRef
from file_identity_consumers import InventoryBudget, ReferenceClosure, RoleSpec
from file_mutation_lock import FileMutationGuard, file_mutation_guard
from test_file_identity_observation import (
    ProducerMedia,
    fs_observer as fs_observer,
    namespace_wire as namespace_wire,
    producer_media as producer_media,
    producer_publication,
    producer_queue,
    producer_pack,
    captured_fixture,
    media_snapshot,
    control_snapshot,
    write_archive,
)
from test_file_identity_observation_sql import inspect, namespace as namespace
from test_file_identity_schema import ledger_path as ledger_path


def resign(value: dict[str, Any]) -> str:
    value.pop("sha256", None)
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    value["sha256"] = hashlib.sha256(encoded.encode()).hexdigest()
    return json.dumps(value)


def witnessed_inventory(
    env: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> observation.OwnershipInventory:
    monkeypatch.setattr(observation, "capture_native", a2_native)
    with file_mutation_guard(str(env.db_path)) as guard:
        return observation.inspect_ownership(
            guard, str(env.scope), InventoryBudget(), inventory_version=2
        )


def a2_native(_fd: int) -> NativeHandle:
    return NativeHandle(1, b"A2-current")


def test_inventory2_exact_frozen_origin_captured_location(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = producer_media
    _ = producer_pack(env, "attached", tombstone=True, nested=True, frozen=True)
    result = witnessed_inventory(env, monkeypatch)
    assert result.unresolved == (), [
        (r.spec.role.purpose, r.outcome) for r in result.observed if r.unresolved
    ]
    assert {
        r.spec.owner.domain
        for r in result.observed
        if r.outcome == "unreachable_below_absent_root"
    } >= {"publication", "queue_source", "pack_tombstone"}


def test_inventory2_lexical_queue_destination_is_not_a_relationship(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = producer_media
    _ = producer_pack(env, "attached", tombstone=True)
    with closing(sqlite3.connect(env.db_path)) as db:
        _ = db.execute("UPDATE import_queue_files SET dst_path=src_path")
        db.commit()
    result = witnessed_inventory(env, monkeypatch)
    destination = next(
        r for r in result.observed if r.spec.role.purpose == "destination"
    )
    assert destination.outcome == "unreadable"
    assert result.unresolved


def test_inventory2_frozen_origin_cannot_borrow_captured_generation(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = producer_media
    _ = producer_pack(env, "attached", tombstone=True, nested=True, frozen=True)
    from private_pack_claim import PackOwnership

    closure, _ = captured_fixture(env)
    publication = next(s for s in closure.snapshots if s.owner.domain == "publication")
    root = observation._object_record(
        observation._object_record(observation._load(publication.rows_json))["root"]
    )
    row = observation._object_record(root["row"])
    frozen = observation._object_record(observation._load(row["queue_snapshot_json"]))
    origins = observation._object_record(
        observation._object_record(frozen["_pack_source_origins"])["files"]
    )
    for value in origins.values():
        origin = observation._object_record(value)
        proof = PackOwnership.from_json(observation._text(origin["ownership_json"]))
        origin["artifact_owner_token"] = "c" * 32
        origin["ownership_json"] = replace(
            proof, artifact_owner_token="c" * 32
        ).to_json()
    with closing(sqlite3.connect(env.db_path)) as db:
        _ = db.execute(
            "UPDATE import_publications SET queue_snapshot_json=? WHERE id=?",
            (observation._encode(frozen), row["id"]),
        )
        db.commit()
    result = witnessed_inventory(env, monkeypatch)
    assert result.unresolved
    assert not any(
        r.spec.owner.domain == "publication"
        and r.outcome == "unreachable_below_absent_root"
        for r in result.observed
    )


def test_inventory2_codec_validation_is_pure(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = producer_media
    _ = producer_pack(env, "attached", tombstone=True, nested=True)
    inventory = witnessed_inventory(env, monkeypatch)
    text = inventory.to_json()
    with monkeypatch.context() as patch:
        patch.setattr(observation, "_media", object())
        patch.setattr(observation, "capture_native", object())
        patch.setattr(observation, "_capture_sql", object())
        patch.setattr(sqlite3, "connect", object())
        assert observation.OwnershipInventory.from_json(text) == inventory
        assert replace(inventory) == inventory
        assert (
            observation.OwnershipInventory.create(
                inventory.parent,
                inventory.closure,
                inventory.observed,
                inventory.unresolved,
                inventory.budget,
                inventory_version=2,
            )
            == inventory
        )


def test_inventory2_witness_is_location_not_presence(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = producer_media
    _ = producer_pack(env, "attached", tombstone=True, nested=True)
    closure, _ = captured_fixture(env)
    # Build the specified wire independently of the new acquisition path.
    monkeypatch.setattr(observation, "capture_native", a2_native)
    with file_mutation_guard(str(env.db_path)) as guard:
        old = observation._observe_closure(
            guard, str(env.scope), closure, InventoryBudget()
        )
    witness = next(
        r
        for r in old.observed
        if r.outcome == "absent" and r.spec.role.purpose == "tombstone_source"
    )
    dependent = next(
        r
        for r in old.observed
        if r.spec.role.purpose == "tombstone_source_file"
        and r.spec.path.endswith("/nested/deeper/extra.cbz")
    )
    fields = observation._object_record(observation._load(dependent.constraints_json))
    fields.update(
        version=2,
        boundary="verified",
        policy="not_checked",
        ancestor={
            "owner": observation._owner_value(witness.spec.owner),
            "role": observation._role_value(witness.spec.role),
            "observation_sha256": observation._plan_hash(
                observation._observed_value(witness)
            ),
        },
    )
    dependent2 = replace(
        dependent,
        outcome="unreachable_below_absent_root",
        constraints_json=observation._encode(fields),
    )
    records = tuple(dependent2 if r == dependent else r for r in old.observed)
    unresolved = tuple(sorted({"role_" + r.outcome for r in records if r.unresolved}))
    result = observation.OwnershipInventory.create(
        str(env.scope), closure, records, unresolved, old.budget, inventory_version=2
    )
    assert dependent2.spec == dependent.spec
    assert dependent2.spec.legacy_json == dependent.spec.legacy_json
    assert dependent2.native is None and not dependent2.unresolved
    assert fields["current"] is fields["content"] is None
    assert fields["children"] == [] and fields["policy"] == "not_checked"
    assert observation.OwnershipInventory.from_json(result.to_json()) == result
    assert replace(result) == result
    with pytest.raises(InvalidIdentity):
        observation.OwnershipInventory.create(
            str(env.scope), closure, records, unresolved, old.budget
        )
    # Location completeness does not relax plan1's wire or destination absence.
    with pytest.raises(InvalidIdentity):
        replace(result, version=1)


@pytest.mark.parametrize("nested", [False, True])
def test_inventory2_actual_captured_claim_location(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, nested: bool
) -> None:
    import os

    env = producer_media
    _ = producer_pack(env, "attached", tombstone=True, nested=nested)
    before, controls = media_snapshot(env.scope), control_snapshot(env.db_path)
    fds = set(os.listdir("/proc/self/fd"))
    result = witnessed_inventory(env, monkeypatch)
    assert result.unresolved == ()
    dependents = [
        r for r in result.observed if r.outcome == "unreachable_below_absent_root"
    ]
    assert dependents and {r.spec.owner.domain for r in dependents} >= {
        "pack_tombstone",
        "queue_source",
    }
    assert all(r.native is None for r in dependents)
    assert tuple(r.spec for r in result.observed) == result.closure.roles
    assert observation.OwnershipInventory.from_json(result.to_json()) == result
    assert media_snapshot(env.scope) == before
    assert control_snapshot(env.db_path) == controls
    assert set(os.listdir("/proc/self/fd")) == fds


def pack_location_checkpoint(
    env: ProducerMedia, monkeypatch: pytest.MonkeyPatch, checkpoint: str
) -> None:
    import import_pack_cleanup as packs
    from private_pack_claim import PackDirectoryClaim

    if checkpoint == "attaching-lost-reply":
        key, _ = producer_pack(env, "building")
        reservation = packs._read_reservation(key)
        assert reservation is not None
        proof = packs._ownership(reservation)
        assert proof is not None and proof.private_directory is not None
        directory = Path(proof.private_directory.path) / "nested" / "deeper"
        directory.mkdir(parents=True)
        write_archive(directory / "extra.cbz", b"attachment bytes")
        save = packs._save_ownership

        from private_pack_claim import PackOwnership

        def lose_ready(
            guard: FileMutationGuard,
            reservation: packs._PackReservation,
            ownership: PackOwnership,
            *,
            tombstone_path: str | None = None,
        ) -> packs._PackReservation:
            if ownership.phase == "ready":
                raise OSError("A2 lost ready acknowledgement")
            return save(guard, reservation, ownership, tombstone_path=tombstone_path)

        with monkeypatch.context() as patch:
            patch.setattr(packs, "_rename_noreplace", os.rename)
            patch.setattr(packs, "_save_ownership", lose_ready)
            with file_mutation_guard(str(env.db_path)) as guard:
                with closing(sqlite3.connect(env.db_path)) as db:
                    db.row_factory = sqlite3.Row
                    assert packs.begin_pack_queue_attachment(
                        db,
                        reservation.download_id,
                        reservation.owner_token,
                        download_client_id=1,
                        protocol="torrent",
                        _guard=guard,
                    )
                with pytest.raises(OSError, match="A2 lost ready"):
                    _ = packs.durably_attach_pack_queue_directory(
                        reservation.download_id,
                        reservation.owner_token,
                        download_client_id=1,
                        protocol="torrent",
                        _guard=guard,
                    )
        return
    _ = producer_pack(env, "attached", tombstone=True, nested=True)
    closure, _ = captured_fixture(env)
    snapshot = next(s for s in closure.snapshots if s.owner.domain == "pack_tombstone")
    row = observation._object_record(
        observation._object_record(
            observation._object_record(observation._load(snapshot.rows_json))["root"]
        )["row"]
    )
    claim = PackDirectoryClaim.from_json(observation._text(row["carrier_json"]))
    prior = claim.to_json()
    with file_mutation_guard(str(env.db_path)) as guard:

        def persist(current: PackDirectoryClaim) -> None:
            nonlocal prior
            with closing(sqlite3.connect(env.db_path)) as db:
                _ = db.execute("PRAGMA synchronous=FULL")
                assert (
                    db.execute(
                        "UPDATE import_pack_cleanup_tombstones SET carrier_json=? WHERE tombstone_path=? AND carrier_json=?",
                        (current.to_json(), row["tombstone_path"], prior),
                    ).rowcount
                    == 1
                )
                db.commit()
            prior = current.to_json()
            if current.carrier.phase == "discarded" and checkpoint == "discarded":
                raise OSError("A2 lost discarded reply")

        remove = shutil.rmtree

        def partial(_path: str, *, dir_fd: int) -> None:
            remove("artifact/nested", dir_fd=dir_fd)
            raise OSError("A2 partial nested discard")

        with monkeypatch.context() as patch:
            if checkpoint == "partial-discard":
                patch.setattr(shutil, "rmtree", partial)
            if checkpoint in {"partial-discard", "discarded"}:
                with pytest.raises(OSError, match="A2"):
                    _ = packs._discard_pack_claim(guard, claim, persist)
            else:
                _ = packs._discard_pack_claim(guard, claim, persist)


@pytest.mark.parametrize(
    "checkpoint", ["attaching-lost-reply", "partial-discard", "discarded", "post-gc"]
)
def test_inventory2_producer_location_barriers(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, checkpoint: str
) -> None:
    env = producer_media
    pack_location_checkpoint(env, monkeypatch, checkpoint)
    before, controls = media_snapshot(env.scope), control_snapshot(env.db_path)
    fds = set(os.listdir("/proc/self/fd"))
    result = witnessed_inventory(env, monkeypatch)
    assert result.unresolved == (), [
        (r.spec.role.purpose, r.spec.path, r.outcome)
        for r in result.observed
        if r.unresolved
    ]
    assert any(r.outcome == "unreachable_below_absent_root" for r in result.observed)
    assert observation.OwnershipInventory.from_json(result.to_json()) == result
    assert media_snapshot(env.scope) == before
    assert control_snapshot(env.db_path) == controls
    assert set(os.listdir("/proc/self/fd")) == fds


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "missing-witness",
        "owner",
        "role",
        "hash",
        "partial-hash",
        "prefix",
        "parent",
        "cycle",
        "native",
        "current",
        "content",
        "absent",
        "ordinary-absent",
        "constraints1",
        "inventory1",
    ],
)
def test_inventory2_resigned_witness_tampering_refuses(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    env = producer_media
    _ = producer_pack(env, "attached", tombstone=True, nested=True)
    inventory = witnessed_inventory(env, monkeypatch)
    value = observation._object_record(observation._load(inventory.to_json()))
    records = [
        observation._object_record(r) for r in observation._list(value["observed"])
    ]
    record = next(r for r in records if r["outcome"] == "unreachable_below_absent_root")
    fields = observation._object_record(observation._load(record["constraints_json"]))
    ancestor = observation._object_record(fields["ancestor"])
    if change == "missing":
        _ = fields.pop("ancestor")
    elif change == "missing-witness":
        value["observed"] = [
            r
            for r in records
            if not (
                observation._object_record(r["spec"])["owner"] == ancestor["owner"]
                and observation._object_record(r["spec"])["role"] == ancestor["role"]
            )
        ]
        value["unresolved"] = ["reference_uncertain"]
    elif change == "owner":
        ancestor["owner"] = {"domain": "queue_source", "key": [999]}
    elif change == "role":
        ancestor["role"] = observation._role_value(inventory.observed[0].spec.role)
    elif change in {"hash", "partial-hash"}:
        ancestor["observation_sha256"] = (
            "f" * 64 if change == "hash" else observation._plan_hash(ancestor["role"])
        )
    elif change == "prefix":
        unrelated = next(
            r for r in inventory.observed if r.spec.role.purpose == "namespace_parent"
        )
        fields["ancestor"] = {
            "owner": observation._owner_value(unrelated.spec.owner),
            "role": observation._role_value(unrelated.spec.role),
            "observation_sha256": observation._plan_hash(
                observation._observed_value(unrelated)
            ),
        }
    elif change == "parent":
        spec = observation._object_record(record["spec"])
        spec["parent_role"] = None
    elif change == "cycle":
        fields["ancestor"] = {
            "owner": observation._object_record(record["spec"])["owner"],
            "role": observation._object_record(record["spec"])["role"],
            "observation_sha256": observation._plan_hash(record),
        }
    elif change == "native":
        record["native"] = NativeHandle(1, b"forged-present").to_value()
    elif change in {"current", "content"}:
        present = next(
            r
            for r in inventory.observed
            if r.spec.role.kind == "file" and r.outcome == "observed"
        )
        fields[change] = observation._object_record(
            observation._load(present.constraints_json)
        )[change]
    elif change in {"absent", "ordinary-absent"}:
        record["outcome"] = "absent"
        if change == "ordinary-absent":
            _ = fields.pop("ancestor")
            fields.update(version=1, policy="satisfied")
    elif change == "constraints1":
        _ = fields.pop("ancestor")
        fields["version"] = 1
    else:
        value["version"] = 1
    record["constraints_json"] = observation._encode(fields)
    _ = value.pop("sha256")
    value["sha256"] = observation._plan_hash(value)
    with pytest.raises(InvalidIdentity):
        observation.OwnershipInventory.from_json(observation._encode(value))


@pytest.mark.parametrize("version", [True, False, 0, 3, "2", None])
def test_inventory_version_exact_integer_only(
    ledger_path: Path, version: object
) -> None:
    with file_mutation_guard(str(ledger_path)) as guard:
        with pytest.raises(InvalidIdentity):
            observation.inspect_ownership(
                guard,
                str(ledger_path.parent),
                InventoryBudget(),
                inventory_version=cast(int, version),
            )
        with pytest.raises(InvalidIdentity):
            observation.OwnershipInventory.create(
                str(ledger_path.parent),
                ReferenceClosure((), ()),
                (),
                (),
                InventoryBudget(),
                inventory_version=cast(int, version),
            )


@pytest.mark.parametrize("stage", ["hashing", "final"])
@pytest.mark.parametrize(
    "race",
    [
        "reappearance",
        "parent-replacement",
        "mount-drift",
        "EACCES",
        "ESTALE",
        "symlink",
        "unknown-child",
    ],
)
def test_witness_recheck_refuses_changed_ancestor(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    race: str,
) -> None:
    env = producer_media
    _ = producer_pack(env, "attached", tombstone=True, nested=True)
    closure, rows = captured_fixture(env)
    from private_pack_claim import PackDirectoryClaim

    snapshot = next(s for s in closure.snapshots if s.owner.domain == "pack_tombstone")
    root = observation._object_record(
        observation._object_record(observation._load(snapshot.rows_json))["root"]
    )
    claim = PackDirectoryClaim.from_json(
        cast(str, observation._object_record(root["row"])["carrier_json"])
    )
    source = Path(claim.source_directory.path)
    captured = Path(claim.carrier.carrier_path) / "artifact"
    controls = control_snapshot(env.db_path)
    fds = set(os.listdir("/proc/self/fd"))
    fired = False
    expected = media_snapshot(env.scope)
    real_stat, mount = os.stat, observation._mount_id

    def inject() -> None:
        nonlocal fired, expected
        if fired:
            return
        fired = True
        if race == "reappearance":
            source.mkdir()
        elif race == "parent-replacement":
            _ = source.parent.rename(source.parent.with_name("replaced-parent"))
            source.parent.mkdir()
        elif race == "symlink":
            source.symlink_to(captured, target_is_directory=True)
        elif race == "unknown-child":
            (captured / "unexpected").write_bytes(b"race-owned unknown child")
        expected = media_snapshot(env.scope)

    def checked_stat(
        path: object, *, dir_fd: int | None = None, follow_symlinks: bool = True
    ) -> os.stat_result:
        if fired and race in {"EACCES", "ESTALE"} and path == source.name:
            raise OSError(
                errno.EACCES if race == "EACCES" else errno.ESTALE,
                "A2 injected witness error",
            )
        return real_stat(
            cast(str, path), dir_fd=dir_fd, follow_symlinks=follow_symlinks
        )

    def checked_mount(fd: int) -> int:
        return mount(fd) + int(fired and race == "mount-drift")

    from test_file_identity_observation import MediaProxy

    @final
    class RacedMedia(MediaProxy):
        stat = staticmethod(checked_stat)

    monkeypatch.setattr(observation, "_media", RacedMedia())
    monkeypatch.setattr(observation, "_mount_id", checked_mount)
    original_hash = observation._hash
    original_verify = observation._HeldPath._verify_self

    def hashing(
        held: observation._HeldPath,
        fd: int,
        info: os.stat_result,
        budget: observation._ObservationBudget,
    ) -> dict[str, object]:
        if stage == "hashing" and os.readlink(f"/proc/self/fd/{fd}").startswith(
            str(captured) + "/"
        ):
            inject()
        return original_hash(held, fd, info, budget)

    def final_verify(held: observation._HeldPath) -> None:
        if (
            stage == "final"
            and sys._getframe(1).f_code.co_name == "_observe_location_closure"
        ):
            inject()
        original_verify(held)

    monkeypatch.setattr(observation, "_hash", hashing)
    monkeypatch.setattr(observation._HeldPath, "_verify_self", final_verify)
    result = witnessed_inventory(env, monkeypatch)
    assert fired
    assert result.unresolved
    assert not any(
        r.outcome == "unreachable_below_absent_root" for r in result.observed
    )
    assert media_snapshot(env.scope) == expected
    assert control_snapshot(env.db_path) == controls
    assert captured_fixture(env)[1] == rows
    assert set(os.listdir("/proc/self/fd")) == fds


@pytest.mark.parametrize(
    "position", ["artifact", "file", "last-counterpart", "last-media"]
)
@pytest.mark.parametrize("timing", ["before", "after"])
def test_witness_reappearance_during_late_final_counterpart_is_incomplete(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    position: str,
    timing: str,
) -> None:
    env = producer_media
    _ = producer_pack(env, "attached", tombstone=True, nested=True)
    closure, rows = captured_fixture(env)
    from private_pack_claim import PackDirectoryClaim

    snapshot = next(s for s in closure.snapshots if s.owner.domain == "pack_tombstone")
    root = observation._object_record(
        observation._object_record(observation._load(snapshot.rows_json))["root"]
    )
    claim = PackDirectoryClaim.from_json(
        observation._text(observation._object_record(root["row"])["carrier_json"])
    )
    source = Path(claim.source_directory.path)
    captured = Path(claim.carrier.carrier_path) / "artifact"
    assert not source.exists()
    captured_before = media_snapshot(captured)
    controls = control_snapshot(env.db_path)
    fds = set(os.listdir("/proc/self/fd"))
    counterparts: list[tuple[RoleSpec, observation._HeldPath]] = []
    original_observe = observation._observe_role
    original_verify = observation._HeldPath._verify_self
    fired = False
    expected = media_snapshot(env.scope)

    def observe(
        guard: FileMutationGuard,
        spec: RoleSpec,
        scope: str,
        declared: frozenset[str],
        budget: observation._ObservationBudget,
        *,
        _held: observation._HeldPath | None = None,
    ) -> observation.ObservedRole:
        result = original_observe(guard, spec, scope, declared, budget, _held=_held)
        if _held is not None and (
            spec.path == str(captured) or spec.path.startswith(str(captured) + "/")
        ):
            assert result.outcome == "observed"
            counterparts.append((spec, _held))
        return result

    def inject() -> None:
        nonlocal fired, expected
        source.mkdir(mode=0o700)
        fired = True
        expected = media_snapshot(env.scope)

    def final_verify(held: observation._HeldPath) -> None:
        frame = sys._getframe(1)
        if frame.f_code.co_name == "verify":
            frame = frame.f_back
        target: observation._HeldPath | None = None
        if frame is not None and frame.f_code.co_name == "_observe_location_closure":
            records = cast(
                dict[RoleSpec, observation.ObservedRole], frame.f_locals["records"]
            )
            if len(records) == len(closure.roles):
                if position == "last-media":
                    paths = cast(
                        list[observation._HeldPath], frame.f_locals["media_paths"]
                    )
                    target = paths[-1]
                elif position == "last-counterpart":
                    target = counterparts[-1][1]
                elif position == "artifact":
                    target = next(
                        path
                        for spec, path in counterparts
                        if spec.path == str(captured)
                    )
                else:
                    target = next(
                        path for spec, path in counterparts if spec.role.kind == "file"
                    )
        should_inject = not fired and held is target
        if should_inject and timing == "before":
            inject()
        original_verify(held)
        if should_inject and timing == "after":
            inject()

    monkeypatch.setattr(observation, "_observe_role", observe)
    monkeypatch.setattr(observation._HeldPath, "_verify_self", final_verify)
    result = witnessed_inventory(env, monkeypatch)
    assert fired and len(counterparts) >= 3
    assert media_snapshot(captured) == captured_before
    assert media_snapshot(env.scope) == expected
    assert control_snapshot(env.db_path) == controls
    assert captured_fixture(env)[1] == rows
    assert set(os.listdir("/proc/self/fd")) == fds
    assert result.unresolved == ("role_changed",)
    assert not any(
        r.outcome == "unreachable_below_absent_root" for r in result.observed
    )


@pytest.mark.parametrize(
    "outcome",
    [
        "policy_mismatch",
        "boundary_unknown",
        "cross_device",
        "unsafe_path",
        "changed",
        "unreadable",
        "budget_exceeded",
        "unknown_children",
    ],
)
def test_wire_rejects_complete_evidence_relabelled_as_unresolved(
    namespace_wire: dict[str, Any],
    outcome: str,
) -> None:
    value = namespace_wire
    record = next(r for r in value["observed"] if r["spec"]["role"]["kind"] == "marker")
    fields = json.loads(record["constraints_json"])
    assert record["native"] and fields["current"] and fields["content"]
    assert fields["policy"] == "satisfied" and fields["children"] == []
    record["outcome"] = outcome
    value["unresolved"] = ["role_" + outcome]
    with pytest.raises(InvalidIdentity):
        observation.OwnershipInventory.from_json(resign(value))


@pytest.mark.parametrize(
    "outcome",
    [
        "policy_mismatch",
        "boundary_unknown",
        "cross_device",
        "unsafe_path",
        "changed",
        "unreadable",
        "budget_exceeded",
    ],
)
def test_exception_outcome_emitter_shape_remains_roundtrippable(
    ledger_path: Path,
    namespace: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> str:
        raise observation._ObservationFailure(outcome)

    monkeypatch.setattr(observation, "_hash", refuse)
    result = inspect(ledger_path, namespace)
    record = next(r for r in result.observed if r.spec.role.kind == "marker")
    fields = json.loads(record.constraints_json)
    assert record.outcome == outcome and "role_" + outcome in result.unresolved
    assert record.native is None
    assert fields["current"] is fields["content"] is None and fields["children"] == []
    assert fields["policy"] == (
        "mismatch" if outcome == "policy_mismatch" else "satisfied"
    )
    assert fields["boundary"] == (
        "cross_device" if outcome == "cross_device" else "verified"
    )


@pytest.mark.parametrize("native_supported", [True, False])
def test_unknown_children_retains_real_positive_evidence(
    ledger_path: Path,
    namespace: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_supported: bool,
) -> None:
    if not native_supported:

        def unsupported(_fd: int) -> None:
            raise IdentityUnavailable("fixture unsupported native capture")

        monkeypatch.setattr(observation, "capture_native", unsupported)
    child = namespace / ".mangarr-claims" / "unclaimed"
    child.write_bytes(b"not declared")
    result = inspect(ledger_path, namespace)
    record = next(r for r in result.observed if r.spec.role.purpose == "namespace_root")
    fields = json.loads(record.constraints_json)
    assert record.outcome == "unknown_children"
    assert (record.native is not None) is native_supported
    assert fields["current"] is not None and fields["content"] is None
    assert fields["policy"] == "satisfied" and fields["boundary"] == "verified"
    assert [c["path"] for c in fields["children"]] == [str(child)]
    assert "role_unknown_children" in result.unresolved


@pytest.fixture
def producer_wire(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    from file_identity import NativeHandle

    env = producer_media
    producer_publication(env, producer_queue(env), "prepared")
    monkeypatch.setattr(
        observation, "capture_native", lambda _fd: NativeHandle(1, b"fixture-current")
    )
    result = inspect(env.db_path, env.scope)
    assert result.unresolved == ()
    assert result.closure.raw_rows and result.closure.dependencies
    return json.loads(result.to_json())


@pytest.mark.parametrize(
    "omission",
    ["raw_rows", "dependencies", "both", "owner_root", "context", "root_value"],
)
def test_wire_rejects_resigned_incomplete_or_inconsistent_sql_closure(
    producer_wire: dict[str, Any],
    omission: str,
) -> None:
    value = producer_wire
    closure = value["closure"]
    if omission in {"raw_rows", "both"}:
        closure["raw_rows"] = []
    if omission in {"dependencies", "both"}:
        closure["dependencies"] = []
    if omission in {"owner_root", "root_value"}:
        root = json.loads(closure["snapshots"][0]["rows_json"])["root"]
        raw = next(
            r
            for r in closure["raw_rows"]
            if (r["table"], r["key"]) == (root["table"], root["key"])
        )
        if omission == "owner_root":
            closure["raw_rows"].remove(raw)
        else:
            column = next(
                k for k, v in raw["values"] if k != "parent_path" and isinstance(v, str)
            )
            raw["values"] = [
                [k, v + " " if k == column else v] for k, v in raw["values"]
            ]
    if omission == "context":
        assert any(r["table"] == "series" for r in closure["raw_rows"])
        closure["raw_rows"] = [r for r in closure["raw_rows"] if r["table"] != "series"]
    with pytest.raises(InvalidIdentity):
        observation.OwnershipInventory.from_json(resign(value))


def test_real_producer_wire_and_required_fields_control(
    producer_wire: dict[str, Any],
) -> None:
    encoded = resign(producer_wire)
    assert observation.OwnershipInventory.from_json(encoded).unresolved == ()
    del producer_wire["closure"]["raw_rows"]
    with pytest.raises(InvalidIdentity):
        observation.OwnershipInventory.from_json(resign(producer_wire))


@pytest.mark.parametrize("conflicting", [False, True])
def test_wire_rejects_duplicate_raw_table_keys(
    producer_wire: dict[str, Any], conflicting: bool
) -> None:
    rows = producer_wire["closure"]["raw_rows"]
    duplicate = json.loads(json.dumps(rows[0]))
    if conflicting:
        duplicate["values"].append(
            ["zz_extra_scalar", "different row, same primary key"]
        )
    rows.append(duplicate)
    rows.sort(
        key=lambda row: (row["table"], json.dumps(row["key"], separators=(",", ":")))
    )
    with pytest.raises(InvalidIdentity):
        observation.OwnershipInventory.from_json(resign(producer_wire))


def test_wire_preserves_additional_scalar_columns(
    producer_wire: dict[str, Any],
) -> None:
    row = producer_wire["closure"]["raw_rows"][0]
    assert row["table"] == "file_claim_namespaces"
    row["values"].append(["recovery_event_id", 777])
    result = observation.OwnershipInventory.from_json(resign(producer_wire))
    assert dict(result.closure.raw_rows[0].values)["recovery_event_id"] == 777


@pytest.mark.parametrize("field", ["dev", "inode"])
@pytest.mark.parametrize("number", [2**63, 2**64 - 1])
def test_wire_accepts_full_unsigned_stat_identity(
    namespace_wire: dict[str, Any],
    field: str,
    number: int,
) -> None:
    record = namespace_wire["observed"][0]
    fields = json.loads(record["constraints_json"])
    fields["current"][field] = number
    record["constraints_json"] = json.dumps(
        fields, sort_keys=True, separators=(",", ":")
    )
    result = observation.OwnershipInventory.from_json(resign(namespace_wire))
    assert json.loads(result.observed[0].constraints_json)["current"][field] == number


@pytest.mark.parametrize("field", ["dev", "inode"])
@pytest.mark.parametrize("number", [-1, True, 2**64])
def test_wire_unsigned_stat_bounds_remain_strict(
    namespace_wire: dict[str, Any],
    field: str,
    number: int,
) -> None:
    record = namespace_wire["observed"][0]
    fields = json.loads(record["constraints_json"])
    fields["current"][field] = number
    record["constraints_json"] = json.dumps(
        fields, sort_keys=True, separators=(",", ":")
    )
    with pytest.raises(InvalidIdentity):
        observation.OwnershipInventory.from_json(resign(namespace_wire))


def test_stat_unsigned_range_does_not_change_sql_owner_or_signed_time_contract(
    namespace_wire: dict[str, Any],
) -> None:
    valid = observation.OwnershipInventory.from_json(resign(namespace_wire))
    with pytest.raises(InvalidIdentity):
        replace(valid.observed[0].spec, owner=OwnerRef("rescan", (2**63,)))
    record = next(
        r for r in namespace_wire["observed"] if r["spec"]["role"]["kind"] == "marker"
    )
    fields = json.loads(record["constraints_json"])
    fields["current"]["mtime_ns"] = fields["current"]["ctime_ns"] = -1
    record["constraints_json"] = json.dumps(
        fields, sort_keys=True, separators=(",", ":")
    )
    assert (
        observation.OwnershipInventory.from_json(resign(namespace_wire)).unresolved
        == ()
    )
