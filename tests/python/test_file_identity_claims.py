"""SIMULATED complete context negative paths, not native qualification."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import os
import sqlite3

import pytest

from file_identity import IdentityMismatch, OwnerRef
from file_mutation_lock import file_mutation_guard
import file_identity_claims as contexts
import private_file_claim as claims
from test_file_identity_rescan import SimulatedMedia, seed_authority, seed_complete
from test_rescan_transactions import rescan_env as rescan_env
from test_file_identity_schema import ledger_path as ledger_path
from test_file_identity_schema import ledger_db as ledger_db
from test_file_identity_observation import (
    ProducerMedia,
    producer_media as producer_media,
)
from test_file_identity_provision_schema import (
    candidate_graph as candidate_graph,
    sql_graph as sql_graph,
    insert_row,
    graph_row,
)
from test_file_identity_provision_wire import SyntheticGraph


@pytest.mark.parametrize("domain", ["publication", "deletion", "pack"])
def test_exact_enrollment_refuses_other_domains_as_a_whole(
    ledger_path: Path,
    ledger_db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    domain: str,
) -> None:
    from file_identity import IdentityUnavailable
    from file_identity_consumers import InventoryBudget, collect_references
    from maintenance_lock import service_maintenance_guard
    from test_file_identity_inventory import (
        PACK,
        all_rows,
        context,
        deletion,
        pack,
        publication,
        queue,
    )

    context(ledger_db)
    if domain == "deletion":
        deletion(ledger_db)
    else:
        queue(
            ledger_db,
            source=PACK + "/source.cbz" if domain == "pack" else "/incoming/source.cbz",
        )
        publication(ledger_db)
        if domain == "pack":
            pack(ledger_db)
    ledger_db.commit()
    closure = collect_references(ledger_db, "/", InventoryBudget())
    before = all_rows(ledger_db)

    def no_native(fd: int) -> None:
        raise AssertionError("unintegrated domain reached native capture")

    monkeypatch.setattr("file_identity.capture_native", no_native)
    with service_maintenance_guard(str(ledger_path), exclusive=True):
        with file_mutation_guard(str(ledger_path)) as guard:
            with pytest.raises(IdentityUnavailable, match="unintegrated domain"):
                contexts.enroll_exact_legacy(guard, closure, "a" * 32)
    assert all_rows(ledger_db) == before


@pytest.mark.parametrize("change", ["native", "digest", "mtime", "mode", "name"])
def test_each_namespace_role_refuses_independent_changes(
    rescan_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    parent = Path(str(rescan_env["series_dir"]))
    media = SimulatedMedia(parent)
    try:
        with file_mutation_guard(str(rescan_env["db_path"])) as guard:
            with claims.ensure_namespace(guard, str(parent)):
                pass
            with closing(sqlite3.connect(str(rescan_env["db_path"]))) as db:
                epoch = seed_authority(db, parent, monkeypatch)
                seed_complete(db, OwnerRef("namespace", (str(parent),)), media, epoch)
                context = contexts.resolve_context(
                    db, guard, OwnerRef("namespace", (str(parent),))
                )
            assert context is not None
            monkeypatch.setattr(contexts.native, "capture_native", media.capture)
            marker = parent / ".mangarr-claims" / "owner.json"
            original_artifact_bytes = marker.read_bytes()
            if change == "native":
                from file_identity import NativeHandle

                media.native[marker.stat().st_ino] = NativeHandle(
                    1, b"different-subvolume"
                )
            elif change == "digest":
                marker.write_bytes(
                    original_artifact_bytes.replace(b"version", b"Version")
                )
            elif change == "mtime":
                info = marker.stat()
                os.utime(marker, ns=(info.st_atime_ns, info.st_mtime_ns + 1))
            elif change == "mode":
                marker.chmod(0o640)
            else:
                marker.rename(marker.with_name("old-marker"))
                marker.write_bytes(original_artifact_bytes)
                marker.chmod(0o600)
            with pytest.raises((IdentityMismatch, FileNotFoundError)):
                contexts.verify_complete(context)
            retained_artifact_bytes = marker.read_bytes()
            assert retained_artifact_bytes == (
                original_artifact_bytes.replace(b"version", b"Version")
                if change == "digest"
                else original_artifact_bytes
            )
    finally:
        media.release()


def test_unenrolled_legacy_namespace_still_refuses_drift(
    rescan_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = Path(str(rescan_env["series_dir"]))
    with file_mutation_guard(str(rescan_env["db_path"])) as guard:
        with claims.ensure_namespace(guard, str(parent)):
            pass
        media = SimulatedMedia(parent)
        media.drift = 1000
        monkeypatch.setattr(claims, "os", media)
        with pytest.raises(
            claims.PrivateClaimError, match="private namespace parent identity changed"
        ):
            with claims.ensure_namespace(guard, str(parent)):
                pass


def test_closed_and_transferred_context_guards_refuse(
    rescan_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    parent = Path(str(rescan_env["series_dir"]))
    media = SimulatedMedia(parent)
    try:
        with file_mutation_guard(str(rescan_env["db_path"])) as guard:
            with claims.ensure_namespace(guard, str(parent)):
                pass
            with closing(sqlite3.connect(str(rescan_env["db_path"]))) as db:
                epoch = seed_authority(db, parent, monkeypatch)
                seed_complete(db, OwnerRef("namespace", (str(parent),)), media, epoch)
                context = contexts.resolve_context(
                    db, guard, OwnerRef("namespace", (str(parent),))
                )
            assert context is not None
            errors: list[Exception] = []

            def transfer() -> None:
                try:
                    contexts.verify_complete(context)
                except Exception as exc:
                    errors.append(exc)

            worker = threading.Thread(target=transfer)
            worker.start()
            worker.join(timeout=5)
            assert not worker.is_alive()
            assert len(errors) == 1 and isinstance(errors[0], IdentityMismatch)
        with pytest.raises(IdentityMismatch):
            contexts.verify_complete(context)
    finally:
        media.release()


def test_snapshot2_committed_activation_readback_with_supplemental_grant(
    producer_media: ProducerMedia,
    sql_graph: SyntheticGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Synthetic closed wire graph, not a birth producer or allocation permit."""
    import file_identity
    import file_identity_store as store
    from typing import cast
    from test_file_identity_plan import parse_document, wire_object

    authority = graph_row(sql_graph, "file_identity_authorities")
    profile = parse_document(cast(str, authority["profile_json"]))
    candidate = file_identity.StorageProfile(
        cast(str, profile["profile_id"]),
        cast(int, profile["revision"]),
        cast(str, profile["qualification_sha256"]),
        cast(dict[str, object], profile["client_contract"]),
        cast(dict[str, object], profile["server_contract"]),
    )
    monkeypatch.setattr(file_identity, "_QUALIFIED_PROFILES", (candidate,))
    with closing(sqlite3.connect(producer_media.db_path)) as db:
        for item in sql_graph.rows:
            if item["table"] == "file_identity_events":
                insert_row(db, "file_identity_events", wire_object(item["row"]))
        db.commit()
        active = store.read_active(db, sql_graph.owner)
        assert active is not None
        assert active.proof.snapshot.rows_json == sql_graph.snapshot_json
        assert active.event_id == 2
        with file_mutation_guard(str(producer_media.db_path)) as guard:
            context = contexts.resolve_context(db, guard, sql_graph.owner)
        assert context is not None


@pytest.mark.parametrize(
    "mode",
    [
        "grant",
        "revoke",
        "ordinary-step",
        "wrong-activation",
        "foreign-epoch",
        "malformed-capability",
        "malformed-legacy",
        "wrong-capability-kind",
    ],
)
def test_namespace_step_readback_keeps_wire_categories_and_history(
    producer_media: ProducerMedia,
    sql_graph: SyntheticGraph,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """Synthetic allocation wire; forged read results do not alter SQL guards."""
    import file_identity
    import file_identity_store as store
    from typing import cast
    from file_identity import InvalidIdentity
    from test_file_identity_plan import digest, encoded, parse_document, wire_object

    authority = graph_row(sql_graph, "file_identity_authorities")
    profile = parse_document(cast(str, authority["profile_json"]))
    candidate = file_identity.StorageProfile(
        cast(str, profile["profile_id"]),
        cast(int, profile["revision"]),
        cast(str, profile["qualification_sha256"]),
        cast(dict[str, object], profile["client_contract"]),
        cast(dict[str, object], profile["server_contract"]),
    )
    monkeypatch.setattr(file_identity, "_QUALIFIED_PROFILES", (candidate,))
    with closing(sqlite3.connect(producer_media.db_path)) as db:
        events = [
            wire_object(item["row"])
            for item in sql_graph.rows
            if item["table"] == "file_identity_events"
        ]
        for event in events:
            insert_row(db, "file_identity_events", event)
        grant = events[-1]
        if mode == "revoke":
            revoked = parse_document(cast(str, grant["payload_json"]))
            revoked.update(
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
                    "payload_json": encoded(revoked),
                },
            )
        db.commit()
        before = {
            table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in (
                "file_identity_events",
                "file_identity_sets",
                "file_identity_epochs",
                "file_identity_provision_intents",
                "file_identity_authorities",
                "file_claim_namespaces",
            )
        }
        if mode not in {"grant", "revoke"}:
            payload = parse_document(cast(str, grant["payload_json"]))
            if mode in {"ordinary-step", "malformed-legacy"}:
                payload = {
                    "version": 1,
                    "before_snapshot_json": sql_graph.snapshot_json,
                    "before_snapshot_sha256": digest(sql_graph.snapshot_json),
                    "plan_sha256": "9" * 64,
                }
                if mode == "malformed-legacy":
                    payload["extra"] = True
            elif mode == "wrong-activation":
                payload["activation_event_id"] = 1
            elif mode == "foreign-epoch":
                payload["epoch_id"] = "f" * 32
            elif mode == "malformed-capability":
                del payload["consumer_contract_sha256"]
            else:
                payload["kind"] = "not-allocation-capability"
            real_latest = store._latest

            def changed_read(
                connection: sqlite3.Connection, owner: OwnerRef
            ) -> dict[str, object] | None:
                latest = real_latest(connection, owner)
                if latest is not None and owner == sql_graph.owner:
                    return {**latest, "payload_json": encoded(payload)}
                return latest

            monkeypatch.setattr(store, "_latest", changed_read)
            expected = (
                InvalidIdentity
                if mode
                in {"malformed-capability", "malformed-legacy", "wrong-capability-kind"}
                else IdentityMismatch
            )
            with pytest.raises(expected):
                store.read_active(db, sql_graph.owner)
        else:
            active = store.read_active(db, sql_graph.owner)
            assert active is not None and active.event_id == 2
            assert active.proof.snapshot.rows_json == sql_graph.snapshot_json
        for table, rows in before.items():
            assert (
                db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() == rows
            )


def test_context_rejects_wrong_db_and_writer_before_guard_io(
    rescan_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from file_identity import IdentityUnavailable
    from file_mutation_lock import FileMutationGuard

    other = Path(str(rescan_env["db_path"])).with_name("other.db")
    with closing(sqlite3.connect(other)) as db:
        db.execute("CREATE TABLE unrelated(value INTEGER)")
        db.commit()
        with file_mutation_guard(str(rescan_env["db_path"])) as guard:
            with pytest.raises(IdentityMismatch, match="another SQLite"):
                contexts.resolve_context(db, guard, OwnerRef("rescan", (1,)))
            db.execute("BEGIN IMMEDIATE")

            def no_guard_io(self: FileMutationGuard) -> None:
                pytest.fail("guard I/O ran in a SQL writer")

            with monkeypatch.context() as patch:
                patch.setattr(FileMutationGuard, "verify", no_guard_io)
                with pytest.raises(IdentityUnavailable, match="ended SQL"):
                    contexts.resolve_context(db, guard, OwnerRef("rescan", (1,)))
            db.rollback()


def test_borrowed_verified_fd_never_survives_current_name_substitution(
    rescan_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = Path(str(rescan_env["series_dir"]))
    media = SimulatedMedia(parent)
    try:
        with file_mutation_guard(str(rescan_env["db_path"])) as guard:
            with claims.ensure_namespace(guard, str(parent)):
                pass
            with closing(sqlite3.connect(str(rescan_env["db_path"]))) as db:
                epoch = seed_authority(db, parent, monkeypatch)
                seed_complete(db, OwnerRef("namespace", (str(parent),)), media, epoch)
                context = contexts.resolve_context(
                    db, guard, OwnerRef("namespace", (str(parent),))
                )
            assert context is not None
            monkeypatch.setattr(contexts.native, "capture_native", media.capture)
            marker = parent / ".mangarr-claims" / "owner.json"
            original = marker.read_bytes()
            with contexts._opened(str(marker)) as (fd, parent_fd, name):
                verified = contexts.verify_role(
                    context, contexts.role_at(context, str(marker)), fd, parent_fd, name
                )
                contexts.verify_live(verified, context)
                marker.rename(marker.with_name("retained-marker"))
                marker.write_bytes(original)
                marker.chmod(0o600)
                with pytest.raises(IdentityMismatch, match="FD/name"):
                    contexts.verify_live(verified, context)
                # Restore only our injected test substitution so the fixture's
                # descriptor context can perform its own exact exit check.
                marker.unlink()
                marker.with_name("retained-marker").rename(marker)
            assert marker.read_bytes() == original
    finally:
        media.release()


@pytest.mark.parametrize("change", ["missing-marker", "wrong-token"])
def test_activation_cannot_authorize_an_incomplete_or_wrong_binding_set(
    rescan_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    from dataclasses import replace
    from typing import cast
    import file_identity_store as store
    from test_file_identity_schema import canonical

    parent = Path(str(rescan_env["series_dir"]))
    media = SimulatedMedia(parent)
    try:
        with file_mutation_guard(str(rescan_env["db_path"])) as guard:
            with claims.ensure_namespace(guard, str(parent)):
                pass
            with closing(sqlite3.connect(str(rescan_env["db_path"]))) as db:
                epoch = seed_authority(db, parent, monkeypatch)
                active = seed_complete(
                    db, OwnerRef("namespace", (str(parent),)), media, epoch
                )
            roles = list(active.proof.roles)
            manifest = contexts._document(active.proof.manifest_json)
            if change == "missing-marker":
                roles = [o for o in roles if o.role.purpose != "namespace_marker"]
                manifest["required_roles"] = [store._role_value(o.role) for o in roles]
            else:
                obj = roles[0]
                constraints = contexts._document(obj.constraints_json)
                cast(dict[str, object], constraints["binding"])["owner_token"] = (
                    "f" * 32
                )
                roles[0] = replace(obj, constraints_json=canonical(constraints))
            manifest["roles"] = [
                {
                    "role": store._role_value(o.role),
                    "canonical_path": o.canonical_path,
                    "parent_role": store._role_value(o.parent_role)
                    if o.parent_role
                    else None,
                    "native": o.native.to_value(),
                    "constraints_json": o.constraints_json,
                }
                for o in roles
            ]
            proof = replace(
                active.proof, roles=tuple(roles), manifest_json=canonical(manifest)
            )
            with pytest.raises(IdentityMismatch):
                contexts.IdentityContext(replace(active, proof=proof), guard)
    finally:
        media.release()
