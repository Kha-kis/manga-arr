"""SIMULATED continuity through real rescan producers, never native QA."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import closing, contextmanager
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import cast

import pytest

import file_identity
from file_identity import NativeHandle, OwnerRef, RoleKey, StorageProfile
import file_identity_store as ledger
from file_identity_consumers import ReferenceClosure, RoleSpec, roles_for
from file_mutation_lock import FileMutationGuard, file_mutation_guard
import private_file_claim as claims
import rescan_file_recovery as recovery
from test_file_identity_backend import _candidate_contracts
from test_file_identity_schema import canonical, digest
from test_rescan_recovery_protocol import _fixture
from test_rescan_transactions import rescan_env as rescan_env
from test_file_identity_schema import (
    ledger_db as ledger_db,
    ledger_path as ledger_path,
    insert_rescan,
)
from test_file_identity_store import (
    enrolled as enrolled,
    proof_for,
    activate,
    append_event,
)


class SimulatedMedia:
    """Module-local device observations; native IDs do not depend on paths/data."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.drift = 0
        self.native: dict[int, NativeHandle] = {}
        self.held: list[int] = []
        self.uids: dict[int, int] = {}

    def capture(self, fd: int) -> NativeHandle:
        inode = os.fstat(fd).st_ino
        if inode not in self.native:
            self.native[inode] = NativeHandle(1, os.urandom(24))
            self.held.append(os.dup(fd))
        return self.native[inode]

    def _stat(self, info: os.stat_result, path: str) -> os.stat_result:
        if not Path(path).is_relative_to(self.root):
            return info
        values = list(info)
        values[2] += self.drift
        if info.st_ino in self.uids:
            values[4] = self.uids[info.st_ino]
        return os.stat_result(
            values,
            {
                key: getattr(info, key)
                for key in (
                    "st_atime",
                    "st_mtime",
                    "st_ctime",
                    "st_atime_ns",
                    "st_mtime_ns",
                    "st_ctime_ns",
                )
            },
        )

    def fstat(self, fd: int) -> os.stat_result:
        return self._stat(os.fstat(fd), os.readlink(f"/proc/self/fd/{fd}"))

    def lstat(self, path: str) -> os.stat_result:
        return self._stat(os.lstat(path), path)

    def stat(
        self, path: str, *, dir_fd: int | None = None, follow_symlinks: bool = True
    ) -> os.stat_result:
        full = (
            path
            if dir_fd is None
            else os.path.join(os.readlink(f"/proc/self/fd/{dir_fd}"), path)
        )
        return self._stat(
            os.stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks), full
        )

    def __getattr__(self, name: str) -> object:
        return getattr(os, name)

    def release(self) -> None:
        for fd in self.held:
            os.close(fd)
        self.held.clear()


def claim_specs(snapshot: ledger.OwnerSnapshot) -> tuple[RoleSpec, ...]:
    specs = list(roles_for(snapshot))
    if snapshot.owner.domain == "rescan":
        parent = RoleSpec(
            RoleKey(None, "operation_parent", "directory", None),
            str(Path(specs[0].path).parent),
            None,
            snapshot.rows_json,
            "proof",
            "parent_identity",
            snapshot.owner,
        )
        source = next(s for s in specs if s.role.purpose == "source")
        parent = replace(parent, path=str(Path(source.path).parent))
        specs.append(parent)
        specs = [
            replace(
                s,
                role=replace(s.role, relative=Path(s.path).name),
                parent_role=parent.role,
            )
            if s.role.purpose in {"source", "destination"}
            else s
            for s in specs
        ]
    return tuple(sorted(specs, key=lambda s: canonical(asdict(s.role))))


def seed_authority(
    db: sqlite3.Connection,
    parent: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    scope: Path | None = None,
) -> str:
    client, server = _candidate_contracts()
    profile = StorageProfile("SIMULATED-A7", 1, "a" * 64, client, server)
    monkeypatch.setattr(file_identity, "_QUALIFIED_PROFILES", (profile,))
    db.execute(
        "INSERT INTO file_identity_authorities(authority_id,incarnation_token,profile_json,declaration_json,qualification_sha256) VALUES(?,?,?,?,?)",
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
                {
                    "version": 1,
                    "scope_path": str(scope or parent),
                    "scope_token": "c" * 32,
                }
            ),
            "a" * 64,
        ),
    )
    ownership = cast(
        str,
        db.execute(
            "SELECT ownership_json FROM file_claim_namespaces WHERE parent_path=?",
            (str(parent),),
        ).fetchone()[0],
    )
    db.execute(
        "INSERT INTO file_identity_epochs(epoch_id,authority_id,origin_path,allocation_path,root_path,birth_json) VALUES(?,?,?,?,?,?)",
        (
            "d" * 32,
            "a" * 32,
            str(parent),
            str(parent),
            str(parent / ".mangarr-claims"),
            canonical(
                {
                    "version": 1,
                    "layout": "same_parent",
                    "enrollment": "exact_legacy",
                    "legacy_namespace": {
                        "version": json.loads(ownership)["version"],
                        "json": ownership,
                        "sha256": digest(ownership),
                    },
                    "controlled_birth": None,
                }
            ),
        ),
    )
    db.commit()
    return "d" * 32


@contextmanager
def paired_legacy_rescans(
    env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[
    tuple[
        FileMutationGuard,
        "ReferenceClosure",
        SimulatedMedia,
        tuple[recovery.Operation, ...],
    ],
    None,
    None,
]:
    import rescan
    from file_identity_consumers import InventoryBudget, collect_references
    from maintenance_lock import service_maintenance_guard

    source, _, first_target, first_context = _fixture(env)
    scope = Path(str(env["library_root"]))
    second_parent = scope / "Independent Manga"
    second_parent.mkdir()
    second_source = second_parent / "Independent Manga v01.cbz"
    second_source.write_bytes(source.read_bytes() + b"independent-original")
    with rescan.get_db() as db:
        db.execute(
            "INSERT INTO series(id,title,search_pattern,root_folder_id,folder_name) VALUES(8,'Independent Manga','Independent Manga',1,'Independent Manga')"
        )
        cursor = db.execute(
            "INSERT INTO volumes(series_id,volume_num,status) VALUES(8,1,'wanted')"
        )
        second_id = cursor.lastrowid
        volume = db.execute(rescan._ENRICHMENT_VOLUME_SELECT, (second_id,)).fetchone()
        assert volume is not None
        second_target = rescan._EnrichmentTarget(
            dict(volume),
            1.0,
            str(second_source),
            rescan._fingerprint(second_source.stat()),
        )
        second_context = rescan._current_enrichment_context(second_target, db)
        assert second_context is not None
    media = SimulatedMedia(scope)
    try:
        with service_maintenance_guard(str(env["db_path"]), exclusive=True):
            with file_mutation_guard(str(env["db_path"])) as guard:
                operations: list[recovery.Operation] = []
                for target, context in (
                    (first_target, first_context),
                    (second_target, second_context),
                ):
                    operation = recovery._reserve(
                        target,
                        context,
                        recovery.fingerprint_path(target.source_path),
                        target.source_path + ".enriched",
                    )
                    assert operation is not None
                    recovery._allocate(operation, guard, "stage")
                    ready = Path(str(env["db_path"])).parent / (
                        str(operation.row["id"]) + "-ready.cbz"
                    )
                    ready.write_bytes(Path(target.source_path).read_bytes() + b"stage")
                    with recovery._open(operation, guard, "stage") as carrier:
                        recovery._copy_prepared(operation, str(ready), carrier, "stage")
                    operations.append(operation)
                with closing(sqlite3.connect(str(env["db_path"]))) as db:
                    seed_authority(db, source.parent, monkeypatch, scope=scope)
                    closure = collect_references(db, str(scope), InventoryBudget())
                assert [s.owner.domain for s in closure.snapshots].count(
                    "namespace"
                ) == 2
                assert [s.owner.domain for s in closure.snapshots].count("rescan") == 2
                monkeypatch.setattr(file_identity, "capture_native", media.capture)
                yield guard, closure, media, tuple(operations)
    finally:
        media.release()


def test_two_namespace_exact_enrollment_has_independent_epochs_and_replay(
    rescan_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import file_identity_claims as contexts

    with paired_legacy_rescans(rescan_env, monkeypatch) as (
        guard,
        closure,
        media,
        operations,
    ):
        originals = {s.owner: s.rows_json for s in closure.snapshots}
        events = contexts.enroll_exact_legacy(guard, closure, "a" * 32)
        assert len(events) == 4
        epochs: set[str] = set()
        roots: dict[str, NativeHandle] = {}
        with closing(sqlite3.connect(str(rescan_env["db_path"]))) as db:
            for operation in operations:
                parent = str(Path(str(operation.row["source_path"])).parent)
                namespace = contexts.resolve_context(
                    db, guard, OwnerRef("namespace", (parent,))
                )
                context = contexts.resolve_context(
                    db, guard, OwnerRef("rescan", (int(operation.row["id"]),))
                )
                assert namespace is not None and context is not None
                assert context._dependencies == (namespace,)
                assert context.active.proof.epoch_id == namespace.active.proof.epoch_id
                epoch = db.execute(
                    "SELECT origin_path,allocation_path,root_path FROM file_identity_epochs WHERE epoch_id=?",
                    (namespace.active.proof.epoch_id,),
                ).fetchone()
                assert tuple(epoch) == (
                    parent,
                    parent,
                    str(Path(parent) / ".mangarr-claims"),
                )
                epochs.add(namespace.active.proof.epoch_id)
                roots[parent] = next(
                    o.native
                    for o in namespace.active.proof.roles
                    if o.role.purpose == "namespace_root"
                )
            assert len(epochs) == len(set(roots.values())) == 2
            for owner, encoded in originals.items():
                assert ledger.capture_owner_snapshot(db, owner).rows_json == encoded
        media.drift = 1700
        monkeypatch.setattr(claims, "os", media)
        monkeypatch.setattr(contexts, "_media", media)
        for operation in operations:
            with recovery._open(operation, guard, "stage") as carrier:
                assert carrier.artifact_path
                parent = str(Path(str(operation.row["source_path"])).parent)
                assert media.capture(carrier.namespace.fd) == roots[parent]
            durable = recovery.load_operation(int(operation.row["id"]))
            assert durable is not None
            assert recovery._settle_safely(durable, guard) == "rolled_back"
        with closing(sqlite3.connect(str(rescan_env["db_path"]))) as db:
            for encoded in originals.values():
                assert (
                    db.execute(
                        "SELECT count(*) FROM file_identity_sets WHERE snapshot_json=?",
                        (encoded,),
                    ).fetchone()[0]
                    > 0
                )


def enrollment_counts(db: sqlite3.Connection) -> tuple[int, ...]:
    return tuple(
        cast(int, db.execute("SELECT count(*) FROM " + table).fetchone()[0])
        for table in (
            "file_identity_epochs",
            "file_identity_sets",
            "file_identity_events",
        )
    )


@pytest.mark.parametrize(
    "change", ["stale", "foreign", "omitted", "late_cas", "context_race"]
)
def test_multi_namespace_enrollment_refuses_whole_without_partial_authority(
    rescan_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    import file_identity_claims as contexts

    with paired_legacy_rescans(rescan_env, monkeypatch) as (
        guard,
        closure,
        media,
        operations,
    ):
        namespaces = [s for s in closure.snapshots if s.owner.domain == "namespace"]
        authority = "a" * 32
        db_path = str(rescan_env["db_path"])
        calls = 0
        if change == "stale":
            with closing(sqlite3.connect(db_path)) as db:
                row = cast(
                    dict[str, object],
                    json.loads(namespaces[-1].rows_json)["root"]["row"],
                )
                proof = claims._NamespaceProof.from_json(
                    cast(str, row["ownership_json"])
                )
                db.execute(
                    "UPDATE file_claim_namespaces SET ownership_json=? WHERE parent_path=?",
                    (
                        canonical(asdict(replace(proof, owner_token="f" * 32))),
                        namespaces[-1].owner.key[0],
                    ),
                )
                db.commit()
        elif change == "foreign":
            authority = "e" * 32
            with closing(sqlite3.connect(db_path)) as db:
                profile, qualification = db.execute(
                    "SELECT profile_json,qualification_sha256 FROM file_identity_authorities WHERE authority_id=?",
                    ("a" * 32,),
                ).fetchone()
                db.execute(
                    "INSERT INTO file_identity_authorities(authority_id,incarnation_token,profile_json,declaration_json,qualification_sha256) VALUES(?,?,?,?,?)",
                    (
                        authority,
                        "f" * 32,
                        profile,
                        canonical(
                            {
                                "version": 1,
                                "scope_path": str(namespaces[0].owner.key[0]),
                                "scope_token": "c" * 32,
                            }
                        ),
                        qualification,
                    ),
                )
                db.commit()
        elif change == "omitted":
            omitted = namespaces[-1].owner
            closure = replace(
                closure,
                snapshots=tuple(s for s in closure.snapshots if s.owner != omitted),
                roles=tuple(r for r in closure.roles if r.owner != omitted),
                dependencies=tuple(
                    d
                    for d in closure.dependencies
                    if d.owner != omitted and d.requires != omitted
                ),
            )
        elif change == "late_cas":
            prepare = ledger.prepare_set

            def late_prepare(
                db: sqlite3.Connection,
                proof: ledger.ProofSet,
                expected_event_id: int | None,
            ) -> int:
                nonlocal calls
                calls += 1
                if calls == len(closure.snapshots):
                    assert enrollment_counts(db)[-1] > 0
                    db.execute(
                        "UPDATE rescan_file_operations SET state='rollback' WHERE id=?",
                        (proof.owner.key[0],),
                    )
                return prepare(db, proof, expected_event_id)

            monkeypatch.setattr(ledger, "prepare_set", late_prepare)
        elif change == "context_race":
            capture = contexts.capture_set
            captures = 0

            def race_capture(
                snapshot: ledger.OwnerSnapshot,
                authority_id: str,
                epoch_id: str,
                guard: FileMutationGuard,
                *,
                previous: contexts.IdentityContext | None = None,
                dependencies: tuple[ledger.OwnerSnapshot, ...] = (),
                _budget: contexts._CaptureBudget | None = None,
            ) -> ledger.ProofSet:
                nonlocal captures
                proof = capture(
                    snapshot,
                    authority_id,
                    epoch_id,
                    guard,
                    previous=previous,
                    dependencies=dependencies,
                    _budget=_budget,
                )
                captures += 1
                if captures == len(closure.snapshots):
                    with closing(sqlite3.connect(db_path)) as db:
                        db.execute(
                            "UPDATE volumes SET status='grabbed' WHERE id=?",
                            (operations[-1].row["volume_id"],),
                        )
                        db.commit()
                return proof

            monkeypatch.setattr(contexts, "capture_set", race_capture)
        files = {
            r.path: Path(r.path).read_bytes()
            for r in closure.roles
            if Path(r.path).is_file()
        }
        with closing(sqlite3.connect(db_path)) as db:
            counts = enrollment_counts(db)
            rows = db.execute(
                "SELECT parent_path,ownership_json FROM file_claim_namespaces ORDER BY parent_path"
            ).fetchall()
            journals = db.execute(
                "SELECT id,carriers_json,fingerprints_json,state FROM rescan_file_operations ORDER BY id"
            ).fetchall()
        with pytest.raises(file_identity.IdentityMismatch):
            contexts.enroll_exact_legacy(guard, closure, authority)
        if change == "late_cas":
            assert calls == len(closure.snapshots)
        assert {path: Path(path).read_bytes() for path in files} == files
        with closing(sqlite3.connect(db_path)) as db:
            assert enrollment_counts(db) == counts
            assert (
                db.execute(
                    "SELECT parent_path,ownership_json FROM file_claim_namespaces ORDER BY parent_path"
                ).fetchall()
                == rows
            )
            assert (
                db.execute(
                    "SELECT id,carriers_json,fingerprints_json,state FROM rescan_file_operations ORDER BY id"
                ).fetchall()
                == journals
            )
        assert recovery.active_operation_ids() == [int(o.row["id"]) for o in operations]


def test_multi_namespace_enrollment_verifies_media_outside_sql_writer(
    rescan_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import file_identity_claims as contexts

    with paired_legacy_rescans(rescan_env, monkeypatch) as (
        guard,
        closure,
        media,
        operations,
    ):
        real_verify = FileMutationGuard.verify
        calls = 0

        def checked_verify(held: FileMutationGuard) -> None:
            nonlocal calls
            with closing(
                sqlite3.connect(str(rescan_env["db_path"]), timeout=0)
            ) as probe:
                probe.execute("BEGIN IMMEDIATE")
                probe.rollback()
            calls += 1
            real_verify(held)

        monkeypatch.setattr(FileMutationGuard, "verify", checked_verify)

        def checked_capture(fd: int) -> NativeHandle:
            with closing(
                sqlite3.connect(str(rescan_env["db_path"]), timeout=0)
            ) as probe:
                probe.execute("BEGIN IMMEDIATE")
                probe.rollback()
            return media.capture(fd)

        monkeypatch.setattr(file_identity, "capture_native", checked_capture)
        assert len(contexts.enroll_exact_legacy(guard, closure, "a" * 32)) == 4
        assert calls > 4


@pytest.mark.parametrize("budget_kind", ["hash", "wire", "objects", "rows"])
def test_multi_namespace_enrollment_uses_one_global_budget(
    rescan_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    budget_kind: str,
) -> None:
    import file_identity_claims as contexts
    from file_identity_consumers import InventoryBudget, claim_roles_for

    with paired_legacy_rescans(rescan_env, monkeypatch) as (
        guard,
        closure,
        media,
        operations,
    ):
        if budget_kind == "hash":
            per_owner = [
                sum(
                    Path(r.path).stat().st_size
                    for r in claim_roles_for(s)
                    if r.role.kind != "directory" and Path(r.path).is_file()
                )
                for s in closure.snapshots
            ]
            assert sum(per_owner) > max(per_owner)
            monkeypatch.setattr(contexts, "_MAX_HASH_BYTES", max(per_owner))
        elif budget_kind == "wire":
            per_owner = []
            namespaces = {
                str(s.owner.key[0]): s
                for s in closure.snapshots
                if s.owner.domain == "namespace"
            }
            for snapshot in closure.snapshots:
                budget = contexts._CaptureBudget()
                dependencies = (
                    ()
                    if snapshot.owner.domain == "namespace"
                    else (
                        namespaces[
                            str(
                                Path(
                                    str(
                                        json.loads(snapshot.rows_json)["root"]["row"][
                                            "source_path"
                                        ]
                                    )
                                ).parent
                            )
                        ],
                    )
                )
                contexts.capture_set(
                    snapshot,
                    "a" * 32,
                    os.urandom(16).hex(),
                    guard,
                    dependencies=dependencies,
                    _budget=budget,
                )
                per_owner.append(budget.wire_bytes)
            assert sum(per_owner) > max(per_owner)
            monkeypatch.setattr(contexts, "_MAX_WIRE_BYTES", max(per_owner))
        else:

            def inventory_budget() -> InventoryBudget:
                return (
                    InventoryBudget(max_objects=len(closure.roles) - 1)
                    if budget_kind == "objects"
                    else InventoryBudget(max_rows=len(closure.raw_rows) - 1)
                )

            monkeypatch.setattr(contexts, "InventoryBudget", inventory_budget)
        with closing(sqlite3.connect(str(rescan_env["db_path"]))) as db:
            counts = enrollment_counts(db)
        files = {
            r.path: Path(r.path).read_bytes()
            for r in closure.roles
            if Path(r.path).is_file()
        }
        with pytest.raises(file_identity.InvalidIdentity, match="budget"):
            contexts.enroll_exact_legacy(guard, closure, "a" * 32)
        with closing(sqlite3.connect(str(rescan_env["db_path"]))) as db:
            assert enrollment_counts(db) == counts
        assert {path: Path(path).read_bytes() for path in files} == files
        assert recovery.active_operation_ids() == [int(o.row["id"]) for o in operations]


def seed_complete(
    db: sqlite3.Connection,
    owner: OwnerRef,
    media: SimulatedMedia,
    epoch: str,
    dependencies: tuple[ledger.OwnerSnapshot, ...] = (),
) -> ledger.ActiveProofSet:
    snapshot = ledger.capture_owner_snapshot(db, owner)
    objects: list[ledger.ObjectProof] = []
    absent: list[dict[str, object]] = []
    intent = None
    if owner.domain == "rescan":
        row = cast(dict[str, object], json.loads(snapshot.rows_json)["root"]["row"])
        intent = {
            "kind": "journal",
            "snapshot_sha256": snapshot.sha256,
            "phase": row["state"],
            "column": "carriers_json",
            "evidence_json": row["carriers_json"],
            "evidence_sha256": digest(cast(str, row["carriers_json"])),
        }
    specs = claim_specs(snapshot)
    for spec in specs:
        if not Path(spec.path).exists():
            assert intent is not None and spec.parent_role is not None
            absent.append(
                {
                    "role": asdict(spec.role),
                    "canonical_path": spec.path,
                    "parent_role": asdict(spec.parent_role),
                    "intent_sha256": digest(canonical(intent)),
                }
            )
            continue
        fd = os.open(spec.path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            fields: dict[str, object] = {
                key: None
                for key in (
                    "inode",
                    "size",
                    "mtime_ns",
                    "sha256",
                    "uid",
                    "mode",
                    "nlink",
                )
            }
            fields["inode"] = info.st_ino
            if spec.role.kind != "directory":
                fields.update(
                    size=info.st_size,
                    mtime_ns=info.st_mtime_ns,
                    sha256=hashlib.sha256(Path(spec.path).read_bytes()).hexdigest(),
                )
            if spec.policy in {
                "file",
                "marker",
                "private_directory",
                "exclusive_parent",
            }:
                fields.update(uid=info.st_uid, mode=stat.S_IMODE(info.st_mode))
            if spec.policy in {"file", "marker"}:
                fields["nlink"] = info.st_nlink
            legacy = cast(str, spec.legacy_json)
            row = cast(dict[str, object], json.loads(snapshot.rows_json)["root"]["row"])
            if owner.domain == "namespace":
                operation_key = str(owner.key[0])
                owner_token = claims._NamespaceProof.from_json(
                    cast(str, row["ownership_json"])
                ).owner_token
            else:
                operation_key = cast(str, row["operation_token"])
                if spec.role.purpose in {"source", "destination", "operation_parent"}:
                    owner_token = operation_key
                else:
                    key = spec.role.purpose.rsplit("_", 1)[0]
                    owner_token = claims.CarrierRecord.from_json(
                        canonical(json.loads(cast(str, row["carriers_json"]))[key])
                    ).owner_token
            constraints = canonical(
                {
                    "version": 1,
                    "legacy": {
                        "kind": "journal",
                        "json": legacy,
                        "sha256": digest(legacy),
                    },
                    "binding": {
                        "owner": asdict(owner),
                        "role": asdict(spec.role),
                        "operation_key": operation_key,
                        "owner_token": owner_token,
                    },
                    "policy": spec.policy,
                    "non_device": fields,
                }
            )
            objects.append(
                ledger.ObjectProof(
                    spec.role,
                    spec.path,
                    spec.parent_role,
                    media.capture(fd),
                    constraints,
                )
            )
        finally:
            os.close(fd)
    manifest = canonical(
        {
            "version": 1,
            "snapshot_sha256": snapshot.sha256,
            "phase": "prepared" if intent else "namespace",
            "required_roles": [asdict(s.role) for s in specs],
            "roles": [
                {
                    "role": asdict(o.role),
                    "canonical_path": o.canonical_path,
                    "parent_role": asdict(o.parent_role) if o.parent_role else None,
                    "native": o.native.to_value(),
                    "constraints_json": o.constraints_json,
                }
                for o in objects
            ],
            "absent_roles": sorted(absent, key=lambda a: canonical(a["role"])),
            "required_dependencies": [asdict(s.owner) for s in dependencies],
            "dependencies": [
                {"snapshot_json": s.rows_json, "sha256": s.sha256, "requires": []}
                for s in dependencies
            ],
            "intent": intent,
        }
    )
    proof = ledger.ProofSet(
        os.urandom(16).hex(),
        owner,
        snapshot,
        "a" * 32,
        epoch,
        "exact_legacy",
        tuple(objects),
        manifest,
    )
    db.execute("BEGIN IMMEDIATE")
    prepared = ledger.prepare_set(db, proof, None)
    ledger.activate_set(db, proof, snapshot, prepared)
    db.commit()
    active = ledger.read_active(db, owner)
    assert active is not None
    return active


def test_existing_enrolled_rescan_reopen_after_simulated_device_drift(
    rescan_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _, target, context = _fixture(rescan_env)
    source = cast(Path, source)
    parent = source.parent
    media = SimulatedMedia(parent)
    try:
        with file_mutation_guard(str(rescan_env["db_path"])) as guard:
            operation = recovery._reserve(
                target,
                context,
                recovery.fingerprint_path(str(source)),
                str(source.with_name("enriched.cbz")),
            )
            assert operation is not None
            recovery._allocate(operation, guard, "stage")
            with recovery._open(operation, guard, "stage") as carrier:
                recovery._copy_prepared(operation, str(source), carrier, "stage")
            with closing(sqlite3.connect(str(rescan_env["db_path"]))) as db:
                epoch = seed_authority(db, parent, monkeypatch)
                namespace = seed_complete(
                    db, OwnerRef("namespace", (str(parent),)), media, epoch
                )
                active = seed_complete(
                    db,
                    OwnerRef("rescan", (cast(int, operation.row["id"]),)),
                    media,
                    epoch,
                    (namespace.proof.snapshot,),
                )
                original_proof_json = db.execute(
                    "SELECT carriers_json FROM rescan_file_operations WHERE id=?",
                    (operation.row["id"],),
                ).fetchone()[0]
            enrolled_native = next(
                o.native
                for o in active.proof.roles
                if o.role.purpose == "stage_artifact"
            )
            media.drift = 1000
            monkeypatch.setattr(claims, "os", media)
            monkeypatch.setattr(file_identity, "capture_native", media.capture)
            import file_identity_claims as contexts

            monkeypatch.setattr(contexts, "_media", media)
            with recovery._open(operation, guard, "stage") as reopened:
                fd = os.open(reopened.artifact_path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    reopened_native = media.capture(fd)
                finally:
                    os.close(fd)
                assert reopened_native == enrolled_native
            with closing(sqlite3.connect(str(rescan_env["db_path"]))) as db:
                retained_proof_json = db.execute(
                    "SELECT carriers_json FROM rescan_file_operations WHERE id=?",
                    (operation.row["id"],),
                ).fetchone()[0]
            assert original_proof_json == retained_proof_json
    finally:
        media.release()


def test_committed_own_supersession_releases_only_its_rescan_fence(
    rescan_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _, target, context = _fixture(rescan_env)
    source = cast(Path, source)
    media = SimulatedMedia(source.parent)
    try:
        with file_mutation_guard(str(rescan_env["db_path"])) as guard:
            operation = recovery._reserve(
                target,
                context,
                recovery.fingerprint_path(str(source)),
                str(source.with_name("new.cbz")),
            )
            assert operation is not None
            recovery._allocate(operation, guard, "stage")
            with recovery._open(operation, guard, "stage") as carrier:
                recovery._copy_prepared(operation, str(source), carrier, "stage")
            with closing(sqlite3.connect(str(rescan_env["db_path"]))) as db:
                epoch = seed_authority(db, source.parent, monkeypatch)
                namespace = seed_complete(
                    db, OwnerRef("namespace", (str(source.parent),)), media, epoch
                )
                active = seed_complete(
                    db,
                    OwnerRef("rescan", (cast(int, operation.row["id"]),)),
                    media,
                    epoch,
                    (namespace.proof.snapshot,),
                )
                before = db.execute(
                    "SELECT carriers_json,state FROM rescan_file_operations WHERE id=?",
                    (operation.row["id"],),
                ).fetchone()
                latest = db.execute(
                    "SELECT revision FROM file_identity_events WHERE id=?",
                    (active.event_id,),
                ).fetchone()[0]
                event = db.execute(
                    "INSERT INTO file_identity_events(owner_domain,owner_key_json,revision,predecessor_event_id,set_id,kind,payload_json) VALUES(?,?,?,?,?,?,?)",
                    (
                        "rescan",
                        canonical({"version": 1, "key": [operation.row["id"]]}),
                        latest + 1,
                        active.event_id,
                        active.proof.set_id,
                        "superseded",
                        canonical(
                            {
                                "version": 1,
                                "before_snapshot_json": active.proof.snapshot.rows_json,
                                "before_snapshot_sha256": active.proof.snapshot.sha256,
                                "plan_sha256": "9" * 64,
                            }
                        ),
                    ),
                ).lastrowid
                db.execute(
                    "UPDATE rescan_file_operations SET recovery_event_id=? WHERE id=?",
                    (event, operation.row["id"]),
                )
                db.commit()
                assert not recovery.active_for_series(db, 7)
                assert not recovery.active_for_path(db, str(source.parent))
                assert (
                    db.execute(
                        "SELECT carriers_json,state FROM rescan_file_operations WHERE id=?",
                        (operation.row["id"],),
                    ).fetchone()
                    == before
                )
                from import_publication import (
                    assert_library_file_mutation_available,
                    PublicationBlocked,
                )
                from import_lease import claim_import_queue_row

                assert_library_file_mutation_available(db, 7)
                queue = db.execute(
                    "INSERT INTO import_queue(series_id,status) VALUES(7,'pending')"
                ).lastrowid
                assert queue is not None
                assert claim_import_queue_row(db, queue, "new-owner")
                assert recovery.competing_for_series(db, 7)
                with pytest.raises(PublicationBlocked):
                    assert_library_file_mutation_available(db, 7)
                db.execute(
                    "UPDATE import_queue SET status='skipped',lease_owner=NULL WHERE id=?",
                    (queue,),
                )
                db.commit()
                from routers.series_ import _prepare_hard_delete_series

                db.execute("UPDATE series SET deleted_at=CURRENT_TIMESTAMP WHERE id=7")
                db.commit()
                db.row_factory = sqlite3.Row
                assert _prepare_hard_delete_series(db, 7)["status"] == "purged"
                db.rollback()
                db.execute("UPDATE series SET deleted_at=NULL WHERE id=7")
                db.commit()
            assert recovery.active_operation_ids(series_id=7) == []
            assert (
                recovery._reserve(
                    target,
                    context,
                    recovery.fingerprint_path(str(source)),
                    str(source.with_name("new.cbz")),
                )
                is not None
            )
        assert (
            recovery.replay_rescan_file_operation(cast(int, operation.row["id"]))
            == "superseded"
        )
    finally:
        media.release()


def test_source_qualified_registry_remains_disabled() -> None:
    assert file_identity._QUALIFIED_PROFILES == ()


def test_identical_unenrolled_rescan_operation_retains_exact_device_refusal(
    rescan_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _, target, context = _fixture(rescan_env)
    media = SimulatedMedia(source.parent)
    with file_mutation_guard(str(rescan_env["db_path"])) as guard:
        operation = recovery._reserve(
            target,
            context,
            recovery.fingerprint_path(str(source)),
            str(source.with_name("enriched.cbz")),
        )
        assert operation is not None
        recovery._allocate(operation, guard, "stage")
        with recovery._open(operation, guard, "stage") as carrier:
            recovery._copy_prepared(operation, str(source), carrier, "stage")
            artifact = Path(carrier.artifact_path)
            original_artifact_bytes = artifact.read_bytes()
        media.drift = 1000
        monkeypatch.setattr(claims, "os", media)
        with pytest.raises(
            claims.PrivateClaimError, match="private namespace parent identity changed"
        ):
            with recovery._open(operation, guard, "stage"):
                pytest.fail("unenrolled rescan accepted device drift")
        assert artifact.read_bytes() == original_artifact_bytes


def test_rescan_selector_coverage_is_closed() -> None:
    import ast
    import inspect
    import import_lease
    import import_publication
    from routers.series_ import _prepare_hard_delete_series

    expected = {
        "rescan_file_recovery": {
            "active_for_series",
            "active_for_path",
            "load_operation",
            "_store",
            "_reserve",
            "_commit",
            "_settle_safely",
            "active_operation_ids",
        },
        "import_lease": {"claim_import_queue_row"},
        "import_publication": {
            "assert_library_file_mutation_available",
            "create_publication",
        },
    }
    for module in (recovery, import_lease, import_publication):
        functions = {
            node.name: node
            for node in ast.parse(inspect.getsource(module)).body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        actual = {
            name
            for name, node in functions.items()
            if any(
                isinstance(child, ast.Constant)
                and isinstance(child.value, str)
                and "rescan_file_operations" in child.value
                for child in ast.walk(node)
            )
        }
        assert actual == expected[module.__name__]
        selectors = actual - {"load_operation", "_store", "_reserve", "_commit"}
        for name in selectors:
            assert any(
                isinstance(node, ast.Attribute)
                and node.attr == "unresolved_rescan_sql"
                or isinstance(node, ast.Name)
                and node.id == "unresolved_rescan_sql"
                for node in ast.walk(functions[name])
            )
    assert "unresolved_rescan_sql" in inspect.getsource(_prepare_hard_delete_series)


@pytest.mark.parametrize(
    "phase", ["prepared", "claimed", "published", "committed", "published_changed"]
)
@pytest.mark.parametrize("same_path", [False, True])
def test_enrolled_real_producer_replay_after_simulated_device_drift(
    rescan_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    same_path: bool,
) -> None:
    import file_identity_claims as contexts
    from file_identity_consumers import InventoryBudget, collect_references
    from maintenance_lock import service_maintenance_guard

    source, _, target, context = _fixture(rescan_env)
    source = cast(Path, source)
    original = source.read_bytes()
    destination = source if same_path else source.with_name("enriched.cbz")
    ready = Path(str(rescan_env["db_path"])).parent / "ready.cbz"
    prepared_bytes = original + b"enriched-stage"
    ready.write_bytes(prepared_bytes)
    media = SimulatedMedia(source.parent)
    try:
        with service_maintenance_guard(str(rescan_env["db_path"]), exclusive=True):
            with file_mutation_guard(str(rescan_env["db_path"])) as guard:
                operation = recovery._reserve(
                    target,
                    context,
                    recovery.fingerprint_path(str(source)),
                    str(destination),
                )
                assert operation is not None
                recovery._allocate(operation, guard, "stage")
                with recovery._open(operation, guard, "stage") as carrier:
                    recovery._copy_prepared(operation, str(ready), carrier, "stage")
                with closing(sqlite3.connect(str(rescan_env["db_path"]))) as db:
                    seed_authority(db, source.parent, monkeypatch)
                    closure = collect_references(
                        db, str(source.parent), InventoryBudget()
                    )
                monkeypatch.setattr(file_identity, "capture_native", media.capture)
                events = contexts.enroll_exact_legacy(guard, closure, "a" * 32)
                assert len(events) == 2
                media.drift = 1000
                monkeypatch.setattr(claims, "os", media)
                monkeypatch.setattr(contexts, "_media", media)
                if phase != "prepared":
                    recovery._capture_source(operation, guard)
                if phase in {"published", "committed", "published_changed"}:
                    recovery._publish(operation, guard)
                if phase == "committed":
                    assert recovery._commit(operation, guard)
                if phase == "published_changed":
                    media.native[destination.stat().st_ino] = NativeHandle(
                        1, b"foreign-public-object"
                    )
        result = recovery.replay_rescan_file_operation(cast(int, operation.row["id"]))
        if phase == "published_changed":
            assert result == "blocked"
            record = recovery._record(operation, "source")
            assert record is not None
            assert (Path(record.carrier_path) / "artifact").read_bytes() == original
            return
        assert result == ("completed" if phase == "committed" else "rolled_back")
        if phase != "committed":
            assert source.read_bytes() == original
        else:
            assert destination.read_bytes() == prepared_bytes
        assert recovery.active_operation_ids(series_id=7) == []
    finally:
        media.release()


@pytest.mark.parametrize(
    "purpose",
    [
        "namespace_parent",
        "namespace_root",
        "namespace_marker",
        "stage_carrier",
        "stage_marker",
        "stage_artifact",
        "source",
    ],
)
def test_one_changed_native_role_blocks_the_whole_operation(
    rescan_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    purpose: str,
) -> None:
    import file_identity_claims as contexts

    source, _, target, context = _fixture(rescan_env)
    media = SimulatedMedia(source.parent)
    try:
        with file_mutation_guard(str(rescan_env["db_path"])) as guard:
            operation = recovery._reserve(
                target,
                context,
                recovery.fingerprint_path(str(source)),
                str(source.with_name("enriched.cbz")),
            )
            assert operation is not None
            recovery._allocate(operation, guard, "stage")
            with recovery._open(operation, guard, "stage") as carrier:
                recovery._copy_prepared(operation, str(source), carrier, "stage")
            artifact = (
                Path(
                    cast(
                        claims.CarrierRecord, recovery._record(operation, "stage")
                    ).carrier_path
                )
                / "artifact"
            )
            original_artifact_bytes = artifact.read_bytes()
            with closing(sqlite3.connect(str(rescan_env["db_path"]))) as db:
                epoch = seed_authority(db, source.parent, monkeypatch)
                namespace = seed_complete(
                    db, OwnerRef("namespace", (str(source.parent),)), media, epoch
                )
                active = seed_complete(
                    db,
                    OwnerRef("rescan", (cast(int, operation.row["id"]),)),
                    media,
                    epoch,
                    (namespace.proof.snapshot,),
                )
                resolved = contexts.resolve_context(db, guard, active.proof.owner)
            assert resolved is not None
            objects = (
                namespace.proof.roles
                if purpose.startswith("namespace_")
                else active.proof.roles
            )
            changed = next(o for o in objects if o.role.purpose == purpose)
            inode = Path(changed.canonical_path).stat().st_ino
            media.native[inode] = NativeHandle(1, b"same-inode-different-subvolume")
            media.drift = 1000
            monkeypatch.setattr(file_identity, "capture_native", media.capture)
            monkeypatch.setattr(contexts, "_media", media)
            monkeypatch.setattr(claims, "os", media)
            with pytest.raises(file_identity.IdentityMismatch):
                contexts.verify_complete(resolved)
            with pytest.raises(claims.PrivateClaimError) as failure:
                with recovery._open(operation, guard, "stage"):
                    pytest.fail("a partial role set granted authority")
            assert isinstance(failure.value.__cause__, file_identity.IdentityMismatch)
            retained_artifact_bytes = artifact.read_bytes()
            assert retained_artifact_bytes == original_artifact_bytes
    finally:
        media.release()


def test_enrolled_claim_intent_replays_after_capture_before_phase_ack(
    rescan_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import file_identity_claims as contexts
    from file_identity_consumers import InventoryBudget, collect_references
    from maintenance_lock import service_maintenance_guard

    source, _, target, context = _fixture(rescan_env)
    original = source.read_bytes()
    media = SimulatedMedia(source.parent)
    try:
        with service_maintenance_guard(str(rescan_env["db_path"]), exclusive=True):
            with file_mutation_guard(str(rescan_env["db_path"])) as guard:
                operation = recovery._reserve(
                    target,
                    context,
                    recovery.fingerprint_path(str(source)),
                    str(source.with_name("enriched.cbz")),
                )
                assert operation is not None
                recovery._allocate(operation, guard, "stage")
                with recovery._open(operation, guard, "stage") as carrier:
                    recovery._copy_prepared(operation, str(source), carrier, "stage")
                with closing(sqlite3.connect(str(rescan_env["db_path"]))) as db:
                    seed_authority(db, source.parent, monkeypatch)
                    closure = collect_references(
                        db, str(source.parent), InventoryBudget()
                    )
                monkeypatch.setattr(file_identity, "capture_native", media.capture)
                contexts.enroll_exact_legacy(guard, closure, "a" * 32)
                media.drift = 1000
                monkeypatch.setattr(claims, "os", media)
                monkeypatch.setattr(contexts, "_media", media)
                recovery._allocate(operation, guard, "source")
                with recovery._open(operation, guard, "source") as carrier:
                    recovery._phase(operation, "source", carrier, "claiming")
                    claims.claim_into_empty(
                        guard, carrier, carrier.namespace.parent_fd, source.name
                    )
                assert not source.exists()
        assert (
            recovery.replay_rescan_file_operation(cast(int, operation.row["id"]))
            == "rolled_back"
        )
        assert source.read_bytes() == original
    finally:
        media.release()


@contextmanager
def enrolled_operation(
    env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    *,
    same_path: bool = False,
    stage_suffix: bytes = b"",
) -> Generator[
    tuple[recovery.Operation, FileMutationGuard, Path, SimulatedMedia], None, None
]:
    import file_identity_claims as contexts
    from file_identity_consumers import InventoryBudget, collect_references
    from maintenance_lock import service_maintenance_guard

    source, _, target, context = _fixture(env)
    media = SimulatedMedia(source.parent)
    try:
        with service_maintenance_guard(str(env["db_path"]), exclusive=True):
            with file_mutation_guard(str(env["db_path"])) as guard:
                operation = recovery._reserve(
                    target,
                    context,
                    recovery.fingerprint_path(str(source)),
                    str(source if same_path else source.with_name("enriched.cbz")),
                )
                assert operation is not None
                recovery._allocate(operation, guard, "stage")
                ready = Path(str(env["db_path"])).parent / "enrolled-ready.cbz"
                ready.write_bytes(source.read_bytes() + stage_suffix)
                with recovery._open(operation, guard, "stage") as carrier:
                    recovery._copy_prepared(operation, str(ready), carrier, "stage")
                with closing(sqlite3.connect(str(env["db_path"]))) as db:
                    seed_authority(db, source.parent, monkeypatch)
                    closure = collect_references(
                        db, str(source.parent), InventoryBudget()
                    )
                monkeypatch.setattr(file_identity, "capture_native", media.capture)
                monkeypatch.setattr(contexts, "_media", media)
                contexts.enroll_exact_legacy(guard, closure, "a" * 32)
                yield operation, guard, source, media
    finally:
        media.release()


@pytest.mark.parametrize("field", ["digest", "mtime", "owner", "mode"])
def test_native_equality_never_excuses_changed_artifact_constraints(
    rescan_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    import file_identity_claims as contexts

    with enrolled_operation(rescan_env, monkeypatch) as (operation, guard, _, media):
        with closing(sqlite3.connect(str(rescan_env["db_path"]))) as db:
            context = contexts.resolve_context(
                db, guard, OwnerRef("rescan", (cast(int, operation.row["id"]),))
            )
        assert context is not None
        obj = next(
            o for o in context.active.proof.roles if o.role.purpose == "stage_artifact"
        )
        artifact = Path(obj.canonical_path)
        original_artifact_bytes = artifact.read_bytes()
        info = artifact.stat()
        if field == "digest":
            artifact.write_bytes(b"x" * len(original_artifact_bytes))
            os.utime(artifact, ns=(info.st_atime_ns, info.st_mtime_ns))
        elif field == "mtime":
            os.utime(artifact, ns=(info.st_atime_ns, info.st_mtime_ns + 1))
        elif field == "owner":
            media.uids[info.st_ino] = info.st_uid + 1
        else:
            artifact.chmod(stat.S_IMODE(info.st_mode) ^ 0o040)
        with pytest.raises(file_identity.IdentityMismatch):
            contexts.verify_complete(context)
        assert artifact.read_bytes() == (
            b"x" * len(original_artifact_bytes)
            if field == "digest"
            else original_artifact_bytes
        )


def test_phase_and_proof_activation_roll_back_atomically(
    rescan_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with enrolled_operation(rescan_env, monkeypatch) as (operation, guard, _, _):
        recovery._bind_identity(operation, guard)
        with closing(sqlite3.connect(str(rescan_env["db_path"]))) as db:
            before_row = db.execute(
                "SELECT state,carriers_json FROM rescan_file_operations"
            ).fetchall()
            before_events = db.execute("SELECT * FROM file_identity_events").fetchall()
            before_sets = db.execute("SELECT * FROM file_identity_sets").fetchall()

        def fail_activation(
            db: sqlite3.Connection,
            proof: ledger.ProofSet,
            expected: ledger.OwnerSnapshot,
            expected_event_id: int,
        ) -> int:
            assert db.in_transaction
            raise file_identity.IdentityMismatch("injected activation failure")

        with monkeypatch.context() as patch:
            patch.setattr(ledger, "activate_set", fail_activation)
            with pytest.raises(
                file_identity.IdentityMismatch, match="injected activation failure"
            ):
                recovery._store(operation, state="rollback")
        with closing(sqlite3.connect(str(rescan_env["db_path"]))) as db:
            assert (
                db.execute(
                    "SELECT state,carriers_json FROM rescan_file_operations"
                ).fetchall()
                == before_row
            )
            assert (
                db.execute("SELECT * FROM file_identity_events").fetchall()
                == before_events
            )
            assert (
                db.execute("SELECT * FROM file_identity_sets").fetchall() == before_sets
            )


def test_uncertain_committed_phase_reloads_durable_authority(
    rescan_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with enrolled_operation(rescan_env, monkeypatch) as (operation, guard, source, _):
        recovery._bind_identity(operation, guard)
        original = source.read_bytes()
        real_db = recovery.get_db

        @contextmanager
        def uncertain_commit() -> Generator[sqlite3.Connection, None, None]:
            with real_db() as db:
                yield db
                written = db.in_transaction
            if written:
                raise RuntimeError("injected post-commit uncertainty")

        with monkeypatch.context() as patch:
            patch.setattr(recovery, "get_db", uncertain_commit)
            with pytest.raises(RuntimeError, match="post-commit uncertainty"):
                recovery._store(operation, state="rollback")
        assert operation.row["state"] == "prepared"
        with closing(sqlite3.connect(str(rescan_env["db_path"]))) as db:
            assert db.execute(
                "SELECT state FROM rescan_file_operations"
            ).fetchone() == ("rollback",)
        assert recovery._settle_safely(operation, guard) == "rolled_back"
        assert source.read_bytes() == original


@pytest.mark.parametrize(
    "failure", ["unknown", "foreign", "malformed", "stale", "changed-preimage"]
)
def test_unverified_supersession_never_releases_consumer_fences(
    enrolled: sqlite3.Connection,
    failure: str,
) -> None:
    """Corruption fixtures are SQL-only and confined to this owned QA database."""
    proof = proof_for(enrolled)
    assert isinstance(proof, ledger.ProofSet)
    activate(enrolled, proof)
    event = append_event(enrolled, proof)
    enrolled.execute(
        "UPDATE rescan_file_operations SET recovery_event_id=? WHERE id=1", (event,)
    )
    enrolled.commit()
    assert not recovery.active_for_series(enrolled, 7)
    if failure == "stale":
        append_event(enrolled, proof, "retained")
    elif failure == "malformed":
        triggers = enrolled.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='file_identity_events' AND sql LIKE '%BEFORE UPDATE%'"
        ).fetchall()
        for (name,) in triggers:
            assert isinstance(name, str) and name.isidentifier()
            enrolled.execute(f'DROP TRIGGER "{name}"')
        enrolled.execute(
            "UPDATE file_identity_events SET payload_json='{}' WHERE id=?", (event,)
        )
    else:
        enrolled.execute("DROP TRIGGER file_identity_rescan_file_operations_update")
        if failure == "changed-preimage":
            enrolled.execute(
                "UPDATE rescan_file_operations SET source_path='/foreign/changed' WHERE id=1"
            )
        elif failure == "foreign":
            insert_rescan(enrolled, 2)
            enrolled.commit()
            foreign = proof_for(enrolled, OwnerRef("rescan", (2,)), "2" * 32)
            assert isinstance(foreign, ledger.ProofSet)
            activate(enrolled, foreign)
            foreign_event = append_event(enrolled, foreign)
            enrolled.execute(
                "UPDATE rescan_file_operations SET recovery_event_id=? WHERE id=1",
                (foreign_event,),
            )
        else:
            enrolled.execute("PRAGMA foreign_keys=OFF")
            enrolled.execute(
                "UPDATE rescan_file_operations SET recovery_event_id=999999 WHERE id=1"
            )
    enrolled.commit()
    assert recovery.active_for_series(enrolled, 7)
    assert recovery.active_for_path(enrolled, "/library")


def test_enrolled_publication_link_fallback_keeps_complete_phase_authority(
    rescan_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import errno
    from test_rescan_nfs_recovery import _unsupported_renameat2

    with enrolled_operation(rescan_env, monkeypatch) as (
        operation,
        guard,
        source,
        media,
    ):
        import file_identity_claims as contexts

        original = source.read_bytes()
        media.drift = 1000
        monkeypatch.setattr(claims, "os", media)
        monkeypatch.setattr(contexts, "_media", media)
        _unsupported_renameat2(monkeypatch, errno.EOPNOTSUPP)
        recovery._capture_source(operation, guard)
        recovery._publish(operation, guard)
        assert recovery._commit(operation, guard)
        assert recovery._settle_safely(operation, guard) == "completed"
        assert source.with_name("enriched.cbz").read_bytes() == original


def _journal_evidence(db_path: str) -> dict[str, list[tuple[object, ...]]]:
    with closing(sqlite3.connect(db_path)) as db:
        return {
            table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in (
                "rescan_file_operations",
                "file_identity_sets",
                "file_identity_events",
                "file_claim_namespaces",
            )
        }


def _interrupt_enrolled_discard(
    operation: recovery.Operation,
    guard: FileMutationGuard,
    monkeypatch: pytest.MonkeyPatch,
    purpose: str,
) -> None:
    real_discard = claims.discard_private_regular

    def unlink_before_ack(
        held: FileMutationGuard,
        carrier: claims.CarrierHandle,
        expected: claims.FullFileFingerprint,
    ) -> None:
        real_discard(held, carrier, expected)
        if carrier.record.binding.purpose == purpose:
            assert carrier.record.phase == "discarding"
            assert not Path(carrier.artifact_path).exists()
            raise RuntimeError("injected unlink-before-ack interruption")

    with monkeypatch.context() as patch:
        patch.setattr(claims, "discard_private_regular", unlink_before_ack)
        with pytest.raises(RuntimeError, match="unlink-before-ack interruption"):
            recovery._settle(operation, guard)


@pytest.mark.parametrize("same_path", [False, True])
@pytest.mark.parametrize("case", ["fallback_rollback", "source_gap", "publication_gap"])
def test_enrolled_hardlink_discard_lifecycle(
    rescan_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    same_path: bool,
    case: str,
) -> None:
    import errno
    from test_rescan_nfs_recovery import _unsupported_renameat2

    db_path = str(rescan_env["db_path"])
    with enrolled_operation(
        rescan_env, monkeypatch, same_path=same_path, stage_suffix=b"enriched"
    ) as (operation, guard, source, media):
        original = source.read_bytes()
        media.drift = 1000
        monkeypatch.setattr(claims, "os", media)
        if case != "source_gap":
            _unsupported_renameat2(monkeypatch, errno.EOPNOTSUPP)
        recovery._capture_source(operation, guard)
        recovery._publish(operation, guard)
        if case == "publication_gap":
            assert recovery._commit(operation, guard)
        before = _journal_evidence(db_path)
        if case == "fallback_rollback":
            assert recovery._settle(operation, guard) == "rolled_back"
        else:
            _interrupt_enrolled_discard(
                operation,
                guard,
                monkeypatch,
                "source" if case == "source_gap" else "publication",
            )
            durable = recovery.load_operation(cast(int, operation.row["id"]))
            assert durable is not None
            record = recovery._record(
                durable, "source" if case == "source_gap" else "publication"
            )
            assert record is not None and record.phase == "discarding"
            if case == "source_gap":
                assert record.restore_receipt is not None
            assert not (Path(record.carrier_path) / "artifact").exists()
    try:
        expected = "completed" if case == "publication_gap" else "rolled_back"
        assert (
            recovery.replay_rescan_file_operation(cast(int, operation.row["id"]))
            == expected
        )
        destination = Path(operation.row["destination_path"])
        public = destination if expected == "completed" else source
        assert public.read_bytes() == (
            original + b"enriched" if expected == "completed" else original
        )
        if not same_path:
            assert not (source if expected == "completed" else destination).exists()
        assert recovery.active_operation_ids(series_id=7) == []
        after = _journal_evidence(db_path)
        for table in ("file_identity_sets", "file_identity_events"):
            assert after[table][: len(before[table])] == before[table]
        assert after["file_claim_namespaces"] == before["file_claim_namespaces"]
        durable = recovery.load_operation(cast(int, operation.row["id"]))
        assert durable is not None and durable.row["state"] == expected
        assert durable.fingerprints == operation.fingerprints
        assert (
            durable.row["publication_receipt_json"]
            == operation.row["publication_receipt_json"]
        )
        assert all(
            value is None or value["phase"] == "discarded"
            for value in durable.carriers.values()
        )
    finally:
        media.release()


@pytest.mark.parametrize("purpose", ["source", "publication"])
@pytest.mark.parametrize("tamper", ["extra-link", "native-object"])
def test_enrolled_discard_gap_refuses_unrelated_link_or_native_change(
    rescan_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    purpose: str,
    tamper: str,
) -> None:
    import errno
    from test_rescan_nfs_recovery import _unsupported_renameat2

    db_path = str(rescan_env["db_path"])
    with enrolled_operation(rescan_env, monkeypatch) as (
        operation,
        guard,
        source,
        media,
    ):
        if purpose == "publication":
            _unsupported_renameat2(monkeypatch, errno.EOPNOTSUPP)
        recovery._capture_source(operation, guard)
        recovery._publish(operation, guard)
        if purpose == "publication":
            assert recovery._commit(operation, guard)
        _interrupt_enrolled_discard(operation, guard, monkeypatch, purpose)
        public = (
            source if purpose == "source" else Path(operation.row["destination_path"])
        )
        original_public = public.read_bytes()
        assert public.stat().st_nlink == 1
        if tamper == "extra-link":
            os.link(public, public.with_name("unrelated-hardlink"))
        else:
            media.native[public.stat().st_ino] = NativeHandle(
                1, b"foreign-after-unlink"
            )
        before = _journal_evidence(db_path)
    try:
        assert (
            recovery.replay_rescan_file_operation(cast(int, operation.row["id"]))
            == "blocked"
        )
        after = _journal_evidence(db_path)
        assert public.read_bytes() == original_public
        for table in (
            "file_identity_sets",
            "file_identity_events",
            "file_claim_namespaces",
        ):
            assert after[table] == before[table]
        durable = recovery.load_operation(cast(int, operation.row["id"]))
        assert durable is not None and durable.row["state"] == operation.row["state"]
        assert durable.fingerprints == operation.fingerprints
        assert durable.carriers == operation.carriers
        assert (
            durable.row["publication_receipt_json"]
            == operation.row["publication_receipt_json"]
        )
    finally:
        media.release()


@pytest.mark.parametrize(
    "tamper", ["receipt-clear", "receipt-replace", "fingerprints", "carriers", "state"]
)
def test_context_free_enrolled_store_compares_durable_authority(
    rescan_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    with enrolled_operation(rescan_env, monkeypatch) as (operation, guard, _, _):
        recovery._capture_source(operation, guard)
        recovery._publish(operation, guard)
        fresh = recovery.load_operation(cast(int, operation.row["id"]))
        assert fresh is not None and fresh._identity is None
        before = _journal_evidence(str(rescan_env["db_path"]))
        if tamper.startswith("receipt"):
            fresh.row["publication_receipt_json"] = (
                None if tamper == "receipt-clear" else "{}"
            )
        elif tamper == "fingerprints":
            fresh.fingerprints["source"]["sha256"] = "f" * 64
            fresh.row["fingerprints_json"] = recovery._encode(fresh.fingerprints)
        elif tamper == "carriers":
            fresh.carriers["publication"]["phase"] = "discarded"
            fresh.row["carriers_json"] = recovery._encode(fresh.carriers)
        else:
            fresh.row["state"] = "rollback"
        with pytest.raises(claims.PrivateClaimError):
            recovery._store(fresh, diagnostic="identity refusal")
        assert _journal_evidence(str(rescan_env["db_path"])) == before
        unchanged = recovery.load_operation(cast(int, operation.row["id"]))
        assert unchanged is not None and unchanged._identity is None
        recovery._store(unchanged, diagnostic="identity refusal")
        after = _journal_evidence(str(rescan_env["db_path"]))
        for table in (
            "file_identity_sets",
            "file_identity_events",
            "file_claim_namespaces",
        ):
            assert after[table] == before[table]
        loaded = recovery.load_operation(cast(int, operation.row["id"]))
        assert loaded is not None and loaded.row["diagnostic"] == "identity refusal"
        recovery._bind_identity(loaded, guard)


def test_settled_restore_receipt_does_not_authorize_another_link(
    rescan_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with enrolled_operation(rescan_env, monkeypatch) as (
        operation,
        guard,
        source,
        media,
    ):
        recovery._capture_source(operation, guard)
        recovery._publish(operation, guard)
        assert recovery._settle(operation, guard) == "rolled_back"
        assert source.stat().st_nlink == 1
        os.link(source, source.with_name("unrelated-post-restore-link"))
        before = _journal_evidence(str(rescan_env["db_path"]))
    try:
        assert (
            recovery.replay_rescan_file_operation(cast(int, operation.row["id"]))
            == "blocked"
        )
        after = _journal_evidence(str(rescan_env["db_path"]))
        for table in (
            "file_identity_sets",
            "file_identity_events",
            "file_claim_namespaces",
        ):
            assert after[table] == before[table]
    finally:
        media.release()


@pytest.mark.parametrize("fail_activation", [False, True])
def test_contextual_publication_receipt_activates_atomically(
    rescan_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    fail_activation: bool,
) -> None:
    import file_identity_claims as contexts

    with enrolled_operation(rescan_env, monkeypatch) as (operation, guard, _, _):
        recovery._capture_source(operation, guard)
        real_store = recovery._store
        real_activate = ledger.activate_set
        before: dict[str, list[tuple[object, ...]]] = {}
        seen: list[str] = []

        def store(
            op: recovery.Operation,
            *,
            state: str | None = None,
            diagnostic: str | None = None,
        ) -> None:
            if state == "published":
                before.update(_journal_evidence(str(rescan_env["db_path"])))
            real_store(op, state=state, diagnostic=diagnostic)

        def activate_receipt(
            db: sqlite3.Connection,
            proof: ledger.ProofSet,
            expected: ledger.OwnerSnapshot,
            expected_event_id: int,
        ) -> int:
            if contexts._row(proof.snapshot)["state"] == "published":
                assert db.in_transaction
                receipt = db.execute(
                    "SELECT publication_receipt_json FROM rescan_file_operations WHERE id=?",
                    (operation.row["id"],),
                ).fetchone()[0]
                assert (
                    receipt is not None
                    and receipt
                    == contexts._row(proof.snapshot)["publication_receipt_json"]
                )
                seen.append(receipt)
                if fail_activation:
                    raise file_identity.IdentityMismatch(
                        "injected receipt activation failure"
                    )
            return real_activate(db, proof, expected, expected_event_id)

        with monkeypatch.context() as patch:
            patch.setattr(recovery, "_store", store)
            patch.setattr(ledger, "activate_set", activate_receipt)
            if fail_activation:
                with pytest.raises(
                    file_identity.IdentityMismatch, match="receipt activation failure"
                ):
                    recovery._publish(operation, guard)
            else:
                recovery._publish(operation, guard)
        assert len(seen) == 1
        after = _journal_evidence(str(rescan_env["db_path"]))
        if fail_activation:
            assert after == before
        else:
            assert (
                len(after["file_identity_sets"])
                == len(before["file_identity_sets"]) + 1
            )
            assert (
                len(after["file_identity_events"])
                == len(before["file_identity_events"]) + 2
            )
            assert operation._identity is not None
            assert (
                contexts._row(operation._identity.active.proof.snapshot)[
                    "publication_receipt_json"
                ]
                == seen[0]
            )
            contexts.verify_complete(operation._identity)
