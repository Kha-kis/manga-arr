"""Task6 RED: real deletion operations, SIMULATED namespace authority only."""

from __future__ import annotations

from contextlib import closing
from contextlib import contextmanager
from collections.abc import Generator
import os
import ast
import inspect
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from file_identity import OwnerRef
import file_identity_store as ledger
from file_identity_deletion_helpers import (
    assert_reviewed_deletion_selectors,
    assert_namespace_live,
    captured_legacy_neighbor,
    change_namespace_native,
    enrolled_namespace,
    has_deletion_predicate_call,
    synthetic_manifest2_namespace,
)
from test_volume_file_deletion_journal import deletion_env as deletion_env
from test_file_identity_schema import ledger_path as ledger_path
from test_file_identity_observation import (
    ProducerMedia,
    producer_media as producer_media,
)
from test_file_identity_provision_schema import candidate_graph as candidate_graph
from test_file_identity_provision_wire import SyntheticGraph


class InterruptedDeletion(BaseException):
    pass


def test_manifest1_deletion_birth_after_enrolled_namespace_device_drift(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIMULATED F1, adapted from independent review test_birth_drift.py.

    Provenance: review6a-20261024-rkDGcqg4/test_birth_drift.py, SHA256
    1f981e9af0d01e884f2673ac273cf34531c7e82cbf37e7d7bfffaf61057ae3c9.
    """
    import file_identity_claims as contexts
    import private_file_claim as claims
    import volume_file_deletion as deletion

    db_path = Path(str(deletion_env["db_path"]))
    source = Path(str(deletion_env["file_path"]))
    original = source.read_bytes()
    with enrolled_namespace(db_path, source.parent, monkeypatch) as media:
        with closing(sqlite3.connect(db_path)) as db:
            namespace_before = db.execute(
                "SELECT ownership_json FROM file_claim_namespaces WHERE parent_path=?",
                (str(source.parent),),
            ).fetchone()
        marker = source.parent / ".mangarr-claims" / "owner.json"
        marker_before = marker.read_bytes()
        media.drift = 1000
        monkeypatch.setattr(contexts, "_media", media)
        monkeypatch.setattr(claims, "os", media)
        monkeypatch.setattr(deletion, "os", media)
        assert_namespace_live(db_path, source.parent)
        reservation = deletion.reserve_volume_file_deletion(1, 11)
        assert reservation.status == "reserved", reservation
        assert reservation.journal_id is not None
        proof = active(db_path, reservation.journal_id).proof
        assert {o.role.purpose for o in proof.roles} == {"operation_parent", "target"}
        assert source.read_bytes() == original
        assert (
            deletion.replay_volume_file_deletion(reservation.journal_id) == "completed"
        )
        assert not source.exists()
        active(db_path, reservation.journal_id)
        assert (
            deletion.replay_volume_file_deletion(reservation.journal_id) == "terminal"
        )
        with closing(sqlite3.connect(db_path)) as db:
            assert (
                db.execute(
                    "SELECT ownership_json FROM file_claim_namespaces WHERE parent_path=?",
                    (str(source.parent),),
                ).fetchone()
                == namespace_before
            )
            assert db.execute(
                "SELECT COUNT(*) FROM history WHERE event_type='file_deleted'"
            ).fetchone() == (1,)
        assert marker.read_bytes() == marker_before


def test_unrecorded_enrolled_carrier_creation_retains_original_and_fence(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIMULATED F2, adapted from independent review test_carrier_intent.py.

    Provenance: review6a-20261024-rkDGcqg4/test_carrier_intent.py, SHA256
    8b342a14752b75a6810fa62b30716c347b477593d3ad8de801e38486b81a2840.
    """
    import volume_file_deletion as deletion

    db_path = Path(str(deletion_env["db_path"]))
    source = Path(str(deletion_env["file_path"]))
    original = source.read_bytes()
    namespace = source.parent / ".mangarr-claims"
    with enrolled_namespace(db_path, source.parent, monkeypatch):
        reservation = deletion.reserve_volume_file_deletion(1, 11)
        assert reservation.journal_id is not None
        before = {path.name for path in namespace.iterdir()}

        def stop(*args: object, **kwargs: object) -> None:
            raise InterruptedDeletion()

        with monkeypatch.context() as patch:
            patch.setattr(deletion, "_store_carrier", stop)
            with pytest.raises(InterruptedDeletion):
                deletion.replay_volume_file_deletion(reservation.journal_id)
        created = [path for path in namespace.iterdir() if path.name not in before]
        assert len(created) == 1
        child = created[0]
        assert {path.name for path in child.iterdir()} == {"owner.json"}
        marker = (child / "owner.json").read_bytes()
        assert source.read_bytes() == original
        with closing(sqlite3.connect(db_path)) as db:
            assert db.execute(
                "SELECT state,claim_carrier_json FROM volume_file_deletions WHERE id=?",
                (reservation.journal_id,),
            ).fetchone() == ("active", None)
        assert deletion.replay_volume_file_deletion(reservation.journal_id) == "blocked"
        assert source.read_bytes() == original
        assert (child / "owner.json").read_bytes() == marker
        assert {path.name for path in namespace.iterdir()} == before | {child.name}
        assert deletion.active_deletion_journal_ids() == [reservation.journal_id]
        with closing(sqlite3.connect(db_path)) as db:
            assert db.execute(
                "SELECT state,claim_carrier_json FROM volume_file_deletions WHERE id=?",
                (reservation.journal_id,),
            ).fetchone() == ("active", None)
            assert db.execute(
                "SELECT COUNT(*) FROM history WHERE event_type='file_deleted'"
            ).fetchone() == (0,)


@pytest.mark.parametrize("boundary", ["before-media", "child-fsync"])
def test_enrolled_carrier_creation_is_durably_fenced_before_media(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    import stat
    import private_file_claim as claims
    import volume_file_deletion as deletion
    from file_identity import IdentityMismatch

    db_path = Path(str(deletion_env["db_path"]))
    source = Path(str(deletion_env["file_path"]))
    namespace = source.parent / ".mangarr-claims"
    original = source.read_bytes()
    with enrolled_namespace(db_path, source.parent, monkeypatch):
        reservation = deletion.reserve_volume_file_deletion(1, 11)
        assert reservation.journal_id is not None
        journal_id = reservation.journal_id
        real_allocate = claims.allocate_carrier
        checked = False

        @contextmanager
        def allocate(
            namespace_handle: claims.NamespaceHandle,
            binding: claims.ClaimBinding,
            origin_path: str,
            artifact_fingerprint: claims.FullFileFingerprint | None = None,
        ) -> Generator[claims.CarrierHandle, None, None]:
            nonlocal checked
            with closing(sqlite3.connect(db_path)) as db:
                assert db.execute(
                    "SELECT kind FROM file_identity_events WHERE owner_domain='deletion' ORDER BY revision DESC LIMIT 1"
                ).fetchone() == ("prepared",)
                with pytest.raises(
                    IdentityMismatch,
                    match="latest identity history is not an activated set",
                ):
                    ledger.read_active(db, OwnerRef("deletion", (journal_id,)))
            checked = True
            if boundary == "before-media":
                raise InterruptedDeletion()
            with real_allocate(
                namespace_handle, binding, origin_path, artifact_fingerprint
            ) as carrier:
                yield carrier

        class CrashMedia:
            def __getattr__(self, name: str) -> object:
                return getattr(os, name)

            def fsync(self, fd: int) -> None:
                os.fsync(fd)
                path = Path(os.readlink(f"/proc/self/fd/{fd}"))
                if stat.S_ISDIR(os.fstat(fd).st_mode) and path.parent == namespace:
                    raise InterruptedDeletion()

        with monkeypatch.context() as patch:
            patch.setattr(claims, "allocate_carrier", allocate)
            if boundary == "child-fsync":
                patch.setattr(claims, "os", CrashMedia())
            with pytest.raises(InterruptedDeletion):
                deletion.replay_volume_file_deletion(journal_id)
        assert checked
        markers = {
            p.name: (p / "owner.json").read_bytes()
            for p in namespace.iterdir()
            if p.is_dir()
        }
        assert len(markers) == (0 if boundary == "before-media" else 1)
        assert deletion.replay_volume_file_deletion(journal_id) == "blocked"
        assert source.read_bytes() == original
        assert {
            p.name: (p / "owner.json").read_bytes()
            for p in namespace.iterdir()
            if p.is_dir()
        } == markers
        assert deletion.active_deletion_journal_ids() == [journal_id]


@pytest.mark.parametrize("committed", [False, True])
def test_carrier_creation_barrier_uncertain_commit_never_starts_media(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    committed: bool,
) -> None:
    import volume_file_deletion as deletion
    import private_file_claim as claims

    db_path = Path(str(deletion_env["db_path"]))
    source = Path(str(deletion_env["file_path"]))
    original = source.read_bytes()
    namespace = source.parent / ".mangarr-claims"
    with enrolled_namespace(db_path, source.parent, monkeypatch):
        reservation = deletion.reserve_volume_file_deletion(1, 11)
        assert reservation.journal_id is not None
        real_prepare = ledger.prepare_set
        calls: list[int] = []

        def uncertain(
            db: sqlite3.Connection,
            proof: ledger.ProofSet,
            expected_event_id: int | None,
        ) -> int:
            event = real_prepare(db, proof, expected_event_id)
            calls.append(event)
            if committed:
                db.commit()
            raise sqlite3.OperationalError(
                "simulated creation barrier commit uncertainty"
            )

        def no_allocate(*args: object, **kwargs: object) -> None:
            raise AssertionError("uncertain creation barrier reached media allocation")

        with monkeypatch.context() as patch:
            patch.setattr(ledger, "prepare_set", uncertain)
            patch.setattr(claims, "allocate_carrier", no_allocate)
            assert (
                deletion.replay_volume_file_deletion(reservation.journal_id)
                == "blocked"
            )
        assert len(calls) == 1
        assert source.read_bytes() == original
        assert not [p for p in namespace.iterdir() if p.is_dir()]
        assert deletion.active_deletion_journal_ids() == [reservation.journal_id]
        assert deletion.replay_volume_file_deletion(reservation.journal_id) == (
            "blocked" if committed else "completed"
        )
        assert source.exists() is committed


def test_drift_birth_does_not_borrow_namespace_authority_for_replaced_target(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import file_identity_claims as contexts
    import private_file_claim as claims
    import volume_file_deletion as deletion
    from file_mutation_lock import FileMutationGuard

    db_path = Path(str(deletion_env["db_path"]))
    source = Path(str(deletion_env["file_path"]))
    original = source.read_bytes()
    retained = source.with_name("retained-before-birth")
    with enrolled_namespace(db_path, source.parent, monkeypatch) as media:
        media.drift = 1000
        monkeypatch.setattr(contexts, "_media", media)
        monkeypatch.setattr(claims, "os", media)
        monkeypatch.setattr(deletion, "os", media)
        real_bind = deletion._bind_identity

        def substituted(
            journal: deletion._DeletionJournal,
            guard: FileMutationGuard,
            *,
            birth: bool = False,
        ) -> deletion._DeletionJournal:
            assert birth
            source.rename(retained)
            _ = source.write_bytes(b"independent replacement target")
            return real_bind(journal, guard, birth=birth)

        with monkeypatch.context() as patch:
            patch.setattr(deletion, "_bind_identity", substituted)
            outcome = deletion.reserve_volume_file_deletion(1, 11)
        assert (
            outcome.status == "unsafe"
            and outcome.diagnostic == "exact legacy claim evidence differs"
        )
        assert retained.read_bytes() == original
        assert source.read_bytes() == b"independent replacement target"
        ids = deletion.active_deletion_journal_ids()
        assert len(ids) == 1
        with closing(sqlite3.connect(db_path)) as db:
            assert ledger.read_active(db, OwnerRef("deletion", (ids[0],))) is None


@pytest.mark.parametrize("state", ["prepared-only", "active-only", "grant"])
def test_deletion_birth_refuses_real_synthetic_manifest2_namespace(
    producer_media: ProducerMedia,
    candidate_graph: SyntheticGraph,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    """Synthetic typed metadata with real context and real deletion producer."""
    import file_identity_claims as contexts
    import volume_file_deletion as deletion
    from file_identity import IdentityMismatch, IdentityUnavailable
    from file_mutation_lock import file_mutation_guard

    env = producer_media
    allocation = synthetic_manifest2_namespace(env, candidate_graph, monkeypatch, state)
    source = allocation / "Epoch v01.cbz"
    original = b"manifest2 deletion refusal payload"
    _ = source.write_bytes(original)
    # The strict provision fixture intentionally retains its old active rescan.
    # A distinct series avoids that unrelated admission fence without deleting
    # or terminalizing any protected legacy dependency snapshot.
    with closing(sqlite3.connect(env.db_path)) as db, db:
        root = db.execute(
            "INSERT INTO root_folders(path,label) VALUES(?,'Epoch fixture')",
            (str(env.library),),
        ).lastrowid
        _ = db.execute(
            "INSERT INTO series(id,title,search_pattern,folder_name,root_folder_id) VALUES(8,'Epoch','Epoch',?,?)",
            (allocation.name, root),
        )
        _ = db.execute(
            "INSERT INTO volumes(id,series_id,volume_num,status,import_path) VALUES(12,8,1,'downloaded',?)",
            (str(source),),
        )
    with file_mutation_guard(str(env.db_path)) as guard:
        with closing(sqlite3.connect(env.db_path)) as db:
            if state == "prepared-only":
                with pytest.raises(
                    IdentityMismatch,
                    match="latest identity history is not an activated set",
                ):
                    contexts.resolve_context(db, guard, candidate_graph.owner)
            else:
                context = contexts.resolve_context(db, guard, candidate_graph.owner)
                assert context is not None
                assert (
                    contexts._document(context.active.proof.manifest_json)["version"]
                    == 2
                )
                contexts.verify_complete(context)
                with pytest.raises(
                    IdentityUnavailable,
                    match="new epoch allocation requires completed consumer qualification",
                ):
                    context.assert_allocation_supported()
    outcome = deletion.reserve_volume_file_deletion(8, 12)
    assert outcome.status == "unsafe", outcome
    expected = (
        "latest identity history is not an activated set"
        if state == "prepared-only"
        else "new epoch allocation requires completed consumer qualification"
    )
    assert outcome.diagnostic == expected
    assert source.read_bytes() == original
    with closing(sqlite3.connect(env.db_path)) as db:
        rows = db.execute(
            "SELECT id,claim_carrier_json FROM volume_file_deletions WHERE series_id=8"
        ).fetchall()
        assert len(rows) == 1 and rows[0][1] is None
        assert ledger.read_active(db, OwnerRef("deletion", (rows[0][0],))) is None
        assert db.execute(
            "SELECT COUNT(*) FROM file_identity_events WHERE owner_domain='deletion'"
        ).fetchone() == (0,)
    assert deletion.active_deletion_journal_ids() == [rows[0][0]]


def test_deletion_mutation_selector_coverage_is_closed() -> None:
    from routers.series_ import _prepare_hard_delete_series

    assert_reviewed_deletion_selectors(Path(__file__).resolve().parents[2])
    # Inspect only the affected purge symbol, never the imported monolith text.
    assert has_deletion_predicate_call(
        ast.parse(inspect.getsource(_prepare_hard_delete_series))
    )


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("unresolved_deletion_sql()", True),
        ("identity_store.unresolved_deletion_sql('deletion')", True),
        ("unresolved_deletion_sql", False),
        ("'unresolved_deletion_sql()'", False),
        ("foreign.unresolved_deletion_sql()", False),
    ],
)
def test_deletion_selector_guard_requires_actual_call(
    expression: str, expected: bool
) -> None:
    assert has_deletion_predicate_call(ast.parse(expression)) is expected


@pytest.mark.parametrize("module", ["new_consumer.py", "import_lease.py"])
def test_deletion_selector_admission_rejects_unreviewed_app_references(
    tmp_path: Path, module: str
) -> None:
    import shutil

    source = Path(__file__).resolve().parents[2]
    for path in (source / "app").rglob("*.py"):
        target = tmp_path / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = shutil.copyfile(path, target)
    target = tmp_path / "app" / module
    original = target.read_text() if target.exists() else ""
    _ = target.write_text(
        original
        + "\ndef unreviewed_selector():\n    return 'SELECT id FROM volume_file_deletions'\n"
    )
    with pytest.raises(AssertionError) as failure:
        assert_reviewed_deletion_selectors(tmp_path)
    assert (
        "app/" + module,
        "unreviewed_selector",
        "return 'SELECT id FROM volume_file_deletions'",
    ) in failure.value.args[0]["unreviewed"]


@pytest.mark.parametrize(
    "domain,state",
    [
        ("publication", state)
        for state in (
            "staging",
            "prepared",
            "publishing",
            "published",
            "db_committed",
            "cleaning",
        )
    ]
    + [
        ("rescan", state)
        for state in ("prepared", "published", "db_committed", "rollback")
    ]
    + [
        ("quarantine", state)
        for state in ("reserved", "captured", "committed", "restoring")
    ],
)
def test_deletion_supersession_preserves_each_independent_neighbor_fence(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    domain: str,
    state: str,
) -> None:
    import import_publication
    import rescan_file_recovery
    import volume_file_deletion as deletion
    from import_lease import claim_import_queue_row
    from routers.series_ import _prepare_hard_delete_series
    from test_file_identity_store import append_event

    db_path = Path(str(deletion_env["db_path"]))
    source = Path(str(deletion_env["file_path"]))
    original = source.read_bytes()
    with enrolled_namespace(db_path, source.parent, monkeypatch):
        reservation = deletion.reserve_volume_file_deletion(1, 11)
        assert reservation.journal_id is not None
        proof = active(db_path, reservation.journal_id).proof
        with closing(sqlite3.connect(db_path)) as db:
            event = append_event(db, proof)
            _ = db.execute(
                "UPDATE volume_file_deletions SET recovery_event_id=? WHERE id=?",
                (event, reservation.journal_id),
            )
            table, key = captured_legacy_neighbor(db, source, domain, state)
            before = tuple(
                db.execute(f"SELECT * FROM {table} WHERE id=?", (key,)).fetchone()
            )
            _ = db.execute(
                "INSERT INTO import_queue(id,series_id,volume_num,status,respect_grab_claims) VALUES(71,1,1,'pending',0)"
            )
            db.commit()
        assert deletion.active_deletion_journal_ids() == []
        assert (
            deletion.replay_volume_file_deletion(reservation.journal_id) == "terminal"
        )
        with closing(sqlite3.connect(db_path)) as db:
            assert db.execute(
                "SELECT COUNT(*) FROM volume_file_deletions WHERE state='active' AND recovery_event_id IS NULL"
            ).fetchone() == (0,)
            with pytest.raises(
                import_publication.PublicationBlocked,
                match="library file mutation is reserved by active recovery",
            ):
                import_publication.assert_library_file_mutation_available(db, 1)
            assert not claim_import_queue_row(
                db, 81 if domain == "publication" else 71, "neighbor-owner"
            )
            if domain == "publication":
                # Publication admission is queue-specific, not a series lease.
                assert claim_import_queue_row(db, 71, "unrelated-queue-owner")
            assert (
                rescan_file_recovery.active_for_series(db, 1)
                if domain == "rescan"
                else rescan_file_recovery.competing_for_series(db, 1)
            )
            db.rollback()
            _ = db.execute("UPDATE series SET deleted_at=CURRENT_TIMESTAMP WHERE id=1")
            db.commit()
            db.row_factory = sqlite3.Row
            assert _prepare_hard_delete_series(db, 1)["status"] == "import_in_progress"
            db.rollback()
            _ = db.execute("UPDATE series SET deleted_at=NULL WHERE id=1")
            db.commit()
        assert (
            deletion.reserve_volume_file_deletion(1, 11).status == "import_in_progress"
        )
        with closing(sqlite3.connect(db_path)) as db:
            assert (
                db.execute(f"SELECT * FROM {table} WHERE id=?", (key,)).fetchone()
                == before
            )
            assert db.execute(
                "SELECT lease_owner FROM import_queue WHERE id=71"
            ).fetchone() == (None,)
        assert source.read_bytes() == original


def active(db_path: Path, journal_id: int) -> ledger.ActiveProofSet:
    with closing(sqlite3.connect(db_path)) as db:
        result = ledger.read_active(db, OwnerRef("deletion", (journal_id,)))
        assert result is not None
        assert result.proof.snapshot == ledger.capture_owner_snapshot(
            db, result.proof.owner
        )
    return result


@pytest.mark.parametrize("missing", [False, True])
def test_exact_legacy_deletion_enrollment_replays_simulated_drift(
    deletion_env: dict[str, object], monkeypatch: pytest.MonkeyPatch, missing: bool
) -> None:
    import file_identity
    import file_identity_claims as contexts
    from file_identity_consumers import InventoryBudget, collect_references
    from file_mutation_lock import file_mutation_guard
    from maintenance_lock import service_maintenance_guard
    import private_file_claim as claims
    from test_file_identity_rescan import SimulatedMedia, seed_authority
    import volume_file_deletion as deletion

    db_path = Path(str(deletion_env["db_path"]))
    parent = Path(str(deletion_env["library_root"]))
    source = Path(str(deletion_env["file_path"]))
    if missing:
        source.unlink()
    media = SimulatedMedia(parent)
    try:
        with file_mutation_guard(str(db_path)) as guard:
            with claims.ensure_namespace(guard, str(parent)):
                pass
        reservation = deletion.reserve_volume_file_deletion(1, 11)
        assert reservation.journal_id is not None
        monkeypatch.setattr(file_identity, "capture_native", media.capture)
        with service_maintenance_guard(str(db_path), exclusive=True):
            with file_mutation_guard(str(db_path)) as guard:
                with closing(sqlite3.connect(db_path)) as db:
                    _ = seed_authority(db, parent, monkeypatch)
                    closure = collect_references(db, str(parent), InventoryBudget())
                assert {s.owner.domain for s in closure.snapshots} == {
                    "namespace",
                    "deletion",
                }
                assert len(contexts.enroll_exact_legacy(guard, closure, "a" * 32)) == 2
        active(db_path, reservation.journal_id)
        media.drift = 1000
        monkeypatch.setattr(contexts, "_media", media)
        monkeypatch.setattr(claims, "os", media)
        monkeypatch.setattr(deletion, "os", media)
        assert (
            deletion.replay_volume_file_deletion(reservation.journal_id) == "completed"
        )
        assert not source.exists()
        active(db_path, reservation.journal_id)
        assert (
            deletion.replay_volume_file_deletion(reservation.journal_id) == "terminal"
        )
    finally:
        media.release()


@pytest.mark.parametrize(
    "purpose", ["target", "delete_carrier", "delete_marker", "delete_artifact"]
)
def test_independent_deletion_native_role_mismatch_retains_private_bytes(
    deletion_env: dict[str, object], monkeypatch: pytest.MonkeyPatch, purpose: str
) -> None:
    from file_identity import NativeHandle
    import private_file_claim as claims
    import volume_file_deletion as deletion

    db_path = Path(str(deletion_env["db_path"]))
    parent = Path(str(deletion_env["library_root"]))
    source = Path(str(deletion_env["file_path"]))
    original = source.read_bytes()
    with enrolled_namespace(db_path, parent, monkeypatch) as media:
        reservation = deletion.reserve_volume_file_deletion(1, 11)
        assert reservation.journal_id is not None
        if purpose != "target":

            def stop(*args: object) -> None:
                raise InterruptedDeletion()

            with monkeypatch.context() as patch:
                patch.setattr(claims, "discard_private_regular", stop)
                with pytest.raises(InterruptedDeletion):
                    deletion.replay_volume_file_deletion(reservation.journal_id)
        before = active(db_path, reservation.journal_id)
        obj = next(o for o in before.proof.roles if o.role.purpose == purpose)
        path = Path(obj.canonical_path)
        media.native[path.stat().st_ino] = NativeHandle(
            1, b"same-inode-other-subvolume"
        )
        assert deletion.replay_volume_file_deletion(reservation.journal_id) == "blocked"
        retained = [
            Path(o.canonical_path) for o in before.proof.roles if o.role.kind == "file"
        ]
        assert any(p.is_file() and p.read_bytes() == original for p in retained)
        assert deletion.active_deletion_journal_ids() == [reservation.journal_id]


def test_unenrolled_legacy_deletion_does_not_adopt_device_drift(
    deletion_env: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    import volume_file_deletion as deletion
    from test_file_identity_rescan import SimulatedMedia

    source = Path(str(deletion_env["file_path"]))
    original = source.read_bytes()
    reservation = deletion.reserve_volume_file_deletion(1, 11)
    assert reservation.journal_id is not None
    media = SimulatedMedia(source.parent)
    media.drift = 1000
    monkeypatch.setattr(deletion, "os", media)
    assert deletion.replay_volume_file_deletion(reservation.journal_id) == "blocked"
    assert source.read_bytes() == original


def test_inactive_birth_intent_is_not_enrolled_by_startup_replay(
    deletion_env: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    from file_identity import IdentityUnavailable
    import file_identity_claims as contexts
    import volume_file_deletion as deletion

    db_path = Path(str(deletion_env["db_path"]))
    source = Path(str(deletion_env["file_path"]))
    original = source.read_bytes()
    with enrolled_namespace(db_path, source.parent, monkeypatch):

        def unavailable(*args: object, **kwargs: object) -> ledger.ProofSet:
            raise IdentityUnavailable("injected before deletion capture")

        with monkeypatch.context() as patch:
            patch.setattr(contexts, "capture_set", unavailable)
            assert deletion.reserve_volume_file_deletion(1, 11).status == "unsafe"
        ids = deletion.active_deletion_journal_ids()
        assert len(ids) == 1
        assert deletion.replay_volume_file_deletion(ids[0]) == "blocked"
        assert source.read_bytes() == original
        with closing(sqlite3.connect(db_path)) as db:
            assert ledger.read_active(db, OwnerRef("deletion", (ids[0],))) is None


def test_enrolled_namespace_disabled_allocation_is_not_bypassed(
    deletion_env: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    from file_identity import IdentityUnavailable
    import file_identity_claims as contexts
    import volume_file_deletion as deletion

    db_path = Path(str(deletion_env["db_path"]))
    source = Path(str(deletion_env["file_path"]))
    with enrolled_namespace(db_path, source.parent, monkeypatch):

        def unsupported(self: contexts.IdentityContext) -> None:
            raise IdentityUnavailable(
                "new epoch allocation requires completed consumer qualification"
            )

        monkeypatch.setattr(
            contexts.IdentityContext, "assert_allocation_supported", unsupported
        )
        result = deletion.reserve_volume_file_deletion(1, 11)
        assert result.status == "unsafe"
        assert "new epoch allocation" in result.diagnostic
        assert source.is_file()


def test_completed_legacy_deletion_exact_enrollment_preserves_receipt_bytes(
    deletion_env: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    import file_identity
    import file_identity_claims as contexts
    from file_identity_consumers import InventoryBudget, collect_references
    from file_mutation_lock import file_mutation_guard
    from maintenance_lock import service_maintenance_guard
    from test_file_identity_rescan import SimulatedMedia, seed_authority
    import volume_file_deletion as deletion

    db_path = Path(str(deletion_env["db_path"]))
    source = Path(str(deletion_env["file_path"]))
    reservation = deletion.reserve_volume_file_deletion(1, 11)
    assert reservation.journal_id is not None
    assert deletion.replay_volume_file_deletion(reservation.journal_id) == "completed"
    media = SimulatedMedia(source.parent)
    monkeypatch.setattr(file_identity, "capture_native", media.capture)
    try:
        with service_maintenance_guard(str(db_path), exclusive=True):
            with file_mutation_guard(str(db_path)) as guard:
                with closing(sqlite3.connect(db_path)) as db:
                    before = db.execute(
                        "SELECT claim_carrier_json FROM volume_file_deletions"
                    ).fetchone()[0]
                    _ = seed_authority(db, source.parent, monkeypatch)
                    closure = collect_references(
                        db, str(source.parent), InventoryBudget()
                    )
                assert len(contexts.enroll_exact_legacy(guard, closure, "a" * 32)) == 2
                with closing(sqlite3.connect(db_path)) as db:
                    assert (
                        db.execute(
                            "SELECT claim_carrier_json FROM volume_file_deletions"
                        ).fetchone()[0]
                        == before
                    )
        assert (
            deletion.replay_volume_file_deletion(reservation.journal_id) == "terminal"
        )
        active(db_path, reservation.journal_id)
    finally:
        media.release()


@pytest.mark.parametrize("boundary", ["capture", "discard", "gc"])
def test_enrolled_deletion_replays_durable_action_after_crash(
    deletion_env: dict[str, object], monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    import private_file_claim as claims
    import volume_file_deletion as deletion

    db_path = Path(str(deletion_env["db_path"]))
    parent = Path(str(deletion_env["library_root"]))
    with enrolled_namespace(db_path, parent, monkeypatch):
        reservation = deletion.reserve_volume_file_deletion(1, 11)
        assert reservation.journal_id is not None
        name = {
            "capture": "claim_into_empty",
            "discard": "discard_private_regular",
            "gc": "gc_discarded_carrier",
        }[boundary]
        real = getattr(claims, name)

        def crash(*args: object, **kwargs: object) -> None:
            real(*args, **kwargs)
            raise InterruptedDeletion()

        with monkeypatch.context() as patch:
            patch.setattr(claims, name, crash)
            with pytest.raises(InterruptedDeletion):
                deletion.replay_volume_file_deletion(reservation.journal_id)
        assert (
            deletion.replay_volume_file_deletion(reservation.journal_id) == "completed"
        )
        active(db_path, reservation.journal_id)
        assert (
            deletion.replay_volume_file_deletion(reservation.journal_id) == "terminal"
        )
        with closing(sqlite3.connect(db_path)) as db:
            assert db.execute(
                "SELECT COUNT(*) FROM history WHERE event_type='file_deleted'"
            ).fetchone() == (1,)


def test_enrolled_target_current_fd_name_substitution_is_retained(
    deletion_env: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    import file_identity
    import volume_file_deletion as deletion

    db_path = Path(str(deletion_env["db_path"]))
    source = Path(str(deletion_env["file_path"]))
    original = source.read_bytes()
    with enrolled_namespace(db_path, source.parent, monkeypatch) as media:
        reservation = deletion.reserve_volume_file_deletion(1, 11)
        assert reservation.journal_id is not None
        retained = source.with_name("retained-owned-original")
        replaced = False

        def raced(fd: int) -> file_identity.NativeHandle:
            nonlocal replaced
            result = media.capture(fd)
            if os.readlink(f"/proc/self/fd/{fd}") == str(source) and not replaced:
                replaced = True
                source.rename(retained)
                source.write_bytes(original)
            return result

        monkeypatch.setattr(file_identity, "capture_native", raced)
        assert deletion.replay_volume_file_deletion(reservation.journal_id) == "blocked"
        assert (
            replaced
            and retained.read_bytes() == original
            and source.read_bytes() == original
        )


@pytest.mark.parametrize("private", [False, True])
def test_enrolled_content_mismatch_never_discards_changed_bytes(
    deletion_env: dict[str, object], monkeypatch: pytest.MonkeyPatch, private: bool
) -> None:
    import private_file_claim as claims
    import volume_file_deletion as deletion

    db_path = Path(str(deletion_env["db_path"]))
    source = Path(str(deletion_env["file_path"]))
    with enrolled_namespace(db_path, source.parent, monkeypatch):
        reservation = deletion.reserve_volume_file_deletion(1, 11)
        assert reservation.journal_id is not None
        if private:

            def stop(*args: object) -> None:
                raise InterruptedDeletion()

            with monkeypatch.context() as patch:
                patch.setattr(claims, "discard_private_regular", stop)
                with pytest.raises(InterruptedDeletion):
                    deletion.replay_volume_file_deletion(reservation.journal_id)
            proof = active(db_path, reservation.journal_id)
            source = Path(
                next(
                    o.canonical_path
                    for o in proof.proof.roles
                    if o.role.purpose == "delete_artifact"
                )
            )
        source.write_bytes(b"changed untrusted bytes")
        assert deletion.replay_volume_file_deletion(reservation.journal_id) == "blocked"
        assert source.read_bytes() == b"changed untrusted bytes"
        assert deletion.active_deletion_journal_ids() == [reservation.journal_id]


@pytest.mark.parametrize("commit", [False, True])
def test_deletion_activation_failure_or_uncertain_commit_reloads_durable_authority(
    deletion_env: dict[str, object], monkeypatch: pytest.MonkeyPatch, commit: bool
) -> None:
    import volume_file_deletion as deletion
    import private_file_claim as claims

    db_path = Path(str(deletion_env["db_path"]))
    source = Path(str(deletion_env["file_path"]))
    with enrolled_namespace(db_path, source.parent, monkeypatch):
        reservation = deletion.reserve_volume_file_deletion(1, 11)
        assert reservation.journal_id is not None
        original = source.read_bytes()
        real_get_db = deletion.get_db
        failed = False

        @contextmanager
        def uncertain_db() -> Generator[sqlite3.Connection, None, None]:
            nonlocal failed
            inject = False
            with real_get_db() as db:
                yield db
                row = db.execute(
                    "SELECT claim_carrier_json FROM volume_file_deletions"
                ).fetchone()
                if row is not None and row[0] is not None and not failed:
                    record = claims.CarrierRecord.from_json(row[0])
                    inject = record.phase == "claimed" and db.in_transaction
                    if inject:
                        failed = True
                        if not commit:
                            raise sqlite3.OperationalError(
                                "injected rollback before activation commit"
                            )
            if inject:
                raise sqlite3.OperationalError(
                    "injected acknowledgement lost after commit"
                )

        with monkeypatch.context() as patch:
            patch.setattr(deletion, "get_db", uncertain_db)
            assert (
                deletion.replay_volume_file_deletion(reservation.journal_id)
                == "blocked"
            )
        assert failed
        journal = deletion._load_journal(reservation.journal_id)
        assert journal is not None and journal.claim_carrier_json is not None
        record = claims.CarrierRecord.from_json(journal.claim_carrier_json)
        assert record.phase == ("claimed" if commit else "claiming")
        assert Path(record.carrier_path, "artifact").read_bytes() == original
        active(db_path, reservation.journal_id)
        assert (
            deletion.replay_volume_file_deletion(reservation.journal_id) == "completed"
        )


def test_deletion_native_observation_never_runs_under_sql_writer(
    deletion_env: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    import file_identity
    import file_identity_claims as contexts
    import private_file_claim as claims
    import volume_file_deletion as deletion

    db_path = Path(str(deletion_env["db_path"]))
    source = Path(str(deletion_env["file_path"]))
    with enrolled_namespace(db_path, source.parent, monkeypatch) as media:
        observations = 0
        calls: set[str] = set()

        def assert_no_writer() -> None:
            with closing(sqlite3.connect(db_path, timeout=0)) as db:
                db.execute("BEGIN IMMEDIATE")
                db.rollback()

        class CheckedMedia:
            def __getattr__(self, name: str) -> object:
                member = getattr(os, name)
                if name not in {
                    "open",
                    "stat",
                    "fstat",
                    "lstat",
                    "read",
                    "write",
                    "mkdir",
                    "rename",
                    "link",
                    "unlink",
                    "rmdir",
                    "fsync",
                }:
                    return member

                def checked(*args: object, **kwargs: object) -> object:
                    assert_no_writer()
                    calls.add(name)
                    return member(*args, **kwargs)

                return checked

        def capture(fd: int) -> file_identity.NativeHandle:
            nonlocal observations
            assert_no_writer()
            observations += 1
            return media.capture(fd)

        monkeypatch.setattr(file_identity, "capture_native", capture)
        checked_media = CheckedMedia()
        monkeypatch.setattr(contexts, "_media", checked_media)
        monkeypatch.setattr(claims, "os", checked_media)
        monkeypatch.setattr(deletion, "os", checked_media)
        reservation = deletion.reserve_volume_file_deletion(1, 11)
        assert reservation.journal_id is not None
        assert (
            deletion.replay_volume_file_deletion(reservation.journal_id) == "completed"
        )
        assert observations > 0
        assert {
            "open",
            "read",
            "stat",
            "fstat",
            "rename",
            "unlink",
            "rmdir",
            "fsync",
        } <= calls


@contextmanager
def enrolled_restoration(
    env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[tuple[int, Path, bytes], None, None]:
    import file_identity
    import file_identity_claims as contexts
    from file_identity_consumers import InventoryBudget, collect_references
    from file_mutation_lock import file_mutation_guard
    from maintenance_lock import service_maintenance_guard
    import private_file_claim as claims
    from test_file_identity_rescan import SimulatedMedia, seed_authority
    import volume_file_deletion as deletion

    db_path = Path(str(env["db_path"]))
    source = Path(str(env["file_path"]))
    reservation = deletion.reserve_volume_file_deletion(1, 11)
    assert reservation.journal_id is not None
    journal = deletion._load_journal(reservation.journal_id)
    assert journal is not None
    source.rename(journal.claim_path)
    restored_bytes = b"legacy changed flat claim retained for restoration"
    Path(journal.claim_path).write_bytes(restored_bytes)

    def stop(*args: object) -> None:
        raise InterruptedDeletion()

    with monkeypatch.context() as patch:
        patch.setattr(claims, "link_private_regular", stop)
        with pytest.raises(InterruptedDeletion):
            deletion.replay_volume_file_deletion(reservation.journal_id)
    media = SimulatedMedia(source.parent)
    monkeypatch.setattr(file_identity, "capture_native", media.capture)
    try:
        with service_maintenance_guard(str(db_path), exclusive=True):
            with file_mutation_guard(str(db_path)) as guard:
                with closing(sqlite3.connect(db_path)) as db:
                    _ = seed_authority(db, source.parent, monkeypatch)
                    closure = collect_references(
                        db, str(source.parent), InventoryBudget()
                    )
                assert len(contexts.enroll_exact_legacy(guard, closure, "a" * 32)) == 2
        yield reservation.journal_id, source, restored_bytes
    finally:
        media.release()


@pytest.mark.parametrize("occupied", ["absent", "different", "same-inode", "lost-ack"])
def test_enrolled_restore_requires_no_clobber_and_durable_ack(
    deletion_env: dict[str, object], monkeypatch: pytest.MonkeyPatch, occupied: str
) -> None:
    import private_file_claim as claims
    import volume_file_deletion as deletion

    db_path = Path(str(deletion_env["db_path"]))
    with enrolled_restoration(deletion_env, monkeypatch) as (
        journal_id,
        source,
        expected,
    ):
        journal = deletion._load_journal(journal_id)
        assert journal is not None and journal.claim_carrier_json is not None
        record = claims.CarrierRecord.from_json(journal.claim_carrier_json)
        artifact = Path(record.carrier_path, "artifact")
        if occupied == "different":
            source.write_bytes(b"independent destination")
        elif occupied == "same-inode":
            os.link(artifact, source)
        real_link = claims.link_private_regular

        from file_mutation_lock import FileMutationGuard

        def lost_ack(
            guard: FileMutationGuard,
            carrier: claims.CarrierHandle,
            destination_parent_fd: int,
            destination_name: str,
            expected: claims.FullFileFingerprint,
        ) -> claims.LinkReceipt:
            real_link(guard, carrier, destination_parent_fd, destination_name, expected)
            raise InterruptedDeletion()

        if occupied == "lost-ack":
            with monkeypatch.context() as patch:
                patch.setattr(claims, "link_private_regular", lost_ack)
                with pytest.raises(InterruptedDeletion):
                    deletion.replay_volume_file_deletion(journal_id)
        assert deletion.replay_volume_file_deletion(journal_id) == "blocked"
        if occupied == "absent":
            assert source.read_bytes() == expected
            assert not artifact.exists()
            active(db_path, journal_id)
        else:
            assert artifact.read_bytes() == expected
            assert source.read_bytes() == (
                b"independent destination" if occupied == "different" else expected
            )
        assert deletion.active_deletion_journal_ids() == [journal_id]
        with closing(sqlite3.connect(db_path)) as db:
            assert db.execute(
                "SELECT COUNT(*) FROM history WHERE event_type='file_deleted'"
            ).fetchone() == (0,)


@pytest.mark.parametrize("payload_valid", [False, True])
def test_exact_deletion_supersession_releases_only_own_fence(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    payload_valid: bool,
) -> None:
    import import_publication
    import rescan_file_recovery
    from import_lease import claim_import_queue_row
    from test_file_identity_store import append_event
    from test_file_identity_schema import canonical
    import volume_file_deletion as deletion

    db_path = Path(str(deletion_env["db_path"]))
    source = Path(str(deletion_env["file_path"]))
    original = source.read_bytes()
    with enrolled_namespace(db_path, source.parent, monkeypatch):
        reservation = deletion.reserve_volume_file_deletion(1, 11)
        assert reservation.journal_id is not None
        proof = active(db_path, reservation.journal_id).proof
        with closing(sqlite3.connect(db_path)) as db:
            payload = canonical(
                {
                    "version": 1,
                    "before_snapshot_json": proof.snapshot.rows_json,
                    "before_snapshot_sha256": proof.snapshot.sha256,
                    "plan_sha256": "9" * (64 if payload_valid else 63),
                }
            )
            if payload_valid:
                event = append_event(db, proof, payload=payload)
                db.execute(
                    "UPDATE volume_file_deletions SET recovery_event_id=? WHERE id=?",
                    (event, reservation.journal_id),
                )
            else:
                with pytest.raises(
                    sqlite3.IntegrityError, match="invalid identity event set/payload"
                ):
                    append_event(db, proof, payload=payload)
                db.rollback()
            db.execute(
                "INSERT INTO import_queue(id,series_id,volume_num,status,respect_grab_claims) VALUES(71,1,1,'pending',0)"
            )
            db.commit()
        assert deletion.active_deletion_journal_ids() == (
            [] if payload_valid else [reservation.journal_id]
        )
        if payload_valid:
            assert (
                deletion.replay_volume_file_deletion(reservation.journal_id)
                == "terminal"
            )
        assert source.read_bytes() == original
        from shared import get_db

        with get_db() as db:
            assert rescan_file_recovery.competing_for_series(db, 1) is not payload_valid
            if payload_valid:
                import_publication.assert_library_file_mutation_available(db, 1)
            else:
                with pytest.raises(import_publication.PublicationBlocked):
                    import_publication.assert_library_file_mutation_available(db, 1)
            assert claim_import_queue_row(db, 71, "new-owner") is payload_valid
        if payload_valid:
            # Resolving the deletion never revokes a live queue owner's fence.
            with get_db() as db:
                assert rescan_file_recovery.competing_for_series(db, 1)
                with pytest.raises(import_publication.PublicationBlocked):
                    import_publication.assert_library_file_mutation_available(db, 1)
            assert (
                deletion.reserve_volume_file_deletion(1, 11).status
                == "import_in_progress"
            )


def test_superseded_deletion_releases_own_volume_index_not_successor_fence(
    deletion_env: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_file_identity_store import append_event
    from shared import get_db
    from import_lease import claim_import_queue_row
    import volume_file_deletion as deletion

    db_path = Path(str(deletion_env["db_path"]))
    source = Path(str(deletion_env["file_path"]))
    with enrolled_namespace(db_path, source.parent, monkeypatch):
        first = deletion.reserve_volume_file_deletion(1, 11)
        assert first.journal_id is not None
        proof = active(db_path, first.journal_id).proof
        with closing(sqlite3.connect(db_path)) as db:
            event = append_event(db, proof)
            db.execute(
                "UPDATE volume_file_deletions SET recovery_event_id=? WHERE id=?",
                (event, first.journal_id),
            )
            # Fixture owner makes the retained file the current volume again;
            # the subsequent reservation is the actual product producer.
            db.execute(
                "UPDATE volumes SET status='downloaded',import_path=? WHERE id=11",
                (str(source),),
            )
            db.commit()
        successor = deletion.reserve_volume_file_deletion(1, 11)
        assert successor.status == "reserved" and successor.journal_id is not None
        assert successor.journal_id != first.journal_id
        assert deletion.active_deletion_journal_ids() == [successor.journal_id]
        assert deletion.replay_volume_file_deletion(first.journal_id) == "terminal"
        with get_db() as db:
            db.execute(
                "INSERT INTO import_queue(id,series_id,volume_num,status,respect_grab_claims) VALUES(71,1,1,'pending',0)"
            )
            assert not claim_import_queue_row(db, 71, "neighbor")
        assert source.is_file()


@pytest.mark.parametrize(
    "change", ["deletion-prepared-only", "namespace-prepared-only", "stale-row"]
)
def test_drift_inventory_requires_both_exact_committed_activations(
    deletion_env: dict[str, object], monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from file_identity import IdentityMismatch
    import file_identity_claims as contexts
    import file_identity_consumers as consumers
    import private_file_claim as claims
    import volume_file_deletion as deletion

    db_path = Path(str(deletion_env["db_path"]))
    source = Path(str(deletion_env["file_path"]))
    with enrolled_namespace(db_path, source.parent, monkeypatch) as media:
        reservation = deletion.reserve_volume_file_deletion(1, 11)
        assert reservation.journal_id is not None
        media.drift = 1000
        monkeypatch.setattr(contexts, "_media", media)
        monkeypatch.setattr(claims, "os", media)
        monkeypatch.setattr(deletion, "os", media)

        def stop(*args: object) -> None:
            raise InterruptedDeletion()

        with monkeypatch.context() as patch:
            patch.setattr(claims, "claim_into_empty", stop)
            with pytest.raises(InterruptedDeletion):
                deletion.replay_volume_file_deletion(reservation.journal_id)
        budget = consumers.InventoryBudget()
        with closing(sqlite3.connect(db_path)) as db:
            assert consumers.collect_references(
                db, str(source.parent), budget
            ).snapshots
            counter = consumers._Counter(budget)
            raw = consumers._scan(db, counter)
        if change.endswith("prepared-only"):
            domain = "namespace" if change.startswith("namespace") else "deletion"
            events = [
                r
                for r in raw["file_identity_events"]
                if dict(r.values)["owner_domain"] == domain
            ]
            latest = max(events, key=lambda r: int(str(dict(r.values)["revision"])))
            assert dict(latest.values)["kind"] == "activated"
            raw["file_identity_events"].remove(latest)
        else:
            row = raw["volume_file_deletions"][0]
            values = dict(row.values)
            record = claims.CarrierRecord.from_json(str(values["claim_carrier_json"]))
            raw["volume_file_deletions"][0] = replace(
                row,
                values=tuple(
                    (
                        key,
                        replace(record, phase="claimed").to_json()
                        if key == "claim_carrier_json"
                        else value,
                    )
                    for key, value in row.values
                ),
            )
        with pytest.raises(
            IdentityMismatch, match="carrier and namespace device evidence differs"
        ):
            consumers._bind_references(raw, str(source.parent), budget, counter)
        assert source.is_file()


def test_deletion_birth_activates_completed_phase(
    deletion_env: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    import volume_file_deletion as deletion

    db_path = Path(str(deletion_env["db_path"]))
    parent = Path(str(deletion_env["library_root"]))
    with enrolled_namespace(db_path, parent, monkeypatch):
        reservation = deletion.reserve_volume_file_deletion(1, 11)
        assert reservation.status == "reserved" and reservation.journal_id is not None
        assert (
            deletion.replay_volume_file_deletion(reservation.journal_id) == "completed"
        )
        assert not Path(str(deletion_env["file_path"])).exists()
        with closing(sqlite3.connect(db_path)) as db:
            active = ledger.read_active(
                db, OwnerRef("deletion", (reservation.journal_id,))
            )
        assert active is not None, "completed deletion omitted its own phase activation"


@pytest.mark.parametrize(
    "purpose", ["namespace_parent", "namespace_root", "namespace_marker"]
)
def test_deletion_replay_refuses_changed_enrolled_namespace_role(
    deletion_env: dict[str, object], monkeypatch: pytest.MonkeyPatch, purpose: str
) -> None:
    import volume_file_deletion as deletion

    db_path = Path(str(deletion_env["db_path"]))
    parent = Path(str(deletion_env["library_root"]))
    source = Path(str(deletion_env["file_path"]))
    original_bytes = source.read_bytes()
    with enrolled_namespace(db_path, parent, monkeypatch) as media:
        reservation = deletion.reserve_volume_file_deletion(1, 11)
        assert reservation.status == "reserved" and reservation.journal_id is not None
        assert_namespace_live(db_path, parent)
        change_namespace_native(db_path, parent, media, purpose)
        outcome = deletion.replay_volume_file_deletion(reservation.journal_id)
        assert outcome == "blocked", (
            "deletion ignored an independently changed enrolled role"
        )
        journal = deletion._load_journal(reservation.journal_id)
        assert journal is not None and journal.state == "active"
        retained = [source, Path(journal.claim_path)]
        if journal.claim_carrier_json is not None:
            import private_file_claim as claims

            record = claims.CarrierRecord.from_json(journal.claim_carrier_json)
            retained.append(Path(record.carrier_path) / "artifact")
        assert any(
            path.is_file() and path.read_bytes() == original_bytes for path in retained
        )


@pytest.mark.parametrize("boundary", ["capture", "discard", "restore", "gc"])
@pytest.mark.parametrize("category", ["unsupported", "permission", "malformed"])
def test_deletion_action_translates_native_errors_and_retains_fences(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    category: str,
) -> None:
    import file_identity
    import private_file_claim as claims
    import volume_file_deletion as deletion
    from file_mutation_lock import FileMutationGuard
    from import_lease import claim_import_queue_row

    db_path = Path(str(deletion_env["db_path"]))
    source = Path(str(deletion_env["file_path"]))
    expected = source.read_bytes()
    manager = (
        enrolled_restoration(deletion_env, monkeypatch)
        if boundary == "restore"
        else enrolled_namespace(db_path, source.parent, monkeypatch)
    )
    with manager as seeded:
        if boundary == "restore":
            assert isinstance(seeded, tuple)
            journal_id, source, expected = seeded
        else:
            reservation = deletion.reserve_volume_file_deletion(1, 11)
            assert reservation.journal_id is not None
            journal_id = reservation.journal_id
        real_native = file_identity.capture_native
        real_action = claims._action
        real_gc = claims.gc_discarded_carrier
        entered = False
        causes: list[type[BaseException]] = []
        message = "simulated-native-" + category
        error_type = (
            file_identity.InvalidIdentity
            if category == "malformed"
            else file_identity.IdentityUnavailable
        )
        phases = {
            "capture": {"claiming"},
            "discard": {"discarding"},
            "restore": {"restoring"},
        }

        def native(fd: int) -> file_identity.NativeHandle:
            if entered:
                raise error_type(message)
            return real_native(fd)

        def action(
            guard: FileMutationGuard, carrier: claims.CarrierHandle, allowed: set[str]
        ) -> None:
            nonlocal entered
            if boundary != "gc" and allowed == phases[boundary]:
                entered = True
                with pytest.raises(claims.PrivateClaimError, match=message) as failure:
                    real_action(guard, carrier, allowed)
                assert type(failure.value.__cause__) is error_type
                causes.append(type(failure.value.__cause__))
                raise failure.value
            real_action(guard, carrier, allowed)

        def gc(
            namespace: claims.NamespaceHandle,
            binding: claims.ClaimBinding,
            record: claims.CarrierRecord,
            *,
            identity_context: claims._IdentityContext | None = None,
        ) -> None:
            nonlocal entered
            entered = True
            with pytest.raises(claims.PrivateClaimError, match=message) as failure:
                real_gc(namespace, binding, record, identity_context=identity_context)
            assert type(failure.value.__cause__) is error_type
            causes.append(type(failure.value.__cause__))
            raise failure.value

        monkeypatch.setattr(file_identity, "capture_native", native)
        monkeypatch.setattr(claims, "_action", action)
        if boundary == "gc":
            monkeypatch.setattr(claims, "gc_discarded_carrier", gc)
        assert deletion.replay_volume_file_deletion(journal_id) == "blocked"
        assert entered and causes == [error_type]
        journal = deletion._load_journal(journal_id)
        assert journal is not None and journal.state == "active"
        assert journal.diagnostic == message
        assert journal.claim_carrier_json is not None
        record = claims.CarrierRecord.from_json(journal.claim_carrier_json)
        carrier = Path(record.carrier_path)
        assert carrier.is_dir() and (carrier / "owner.json").is_file()
        if boundary == "capture":
            assert (
                source.read_bytes() == expected and not (carrier / "artifact").exists()
            )
        elif boundary == "gc":
            assert record.phase == "discarded" and not (carrier / "artifact").exists()
        else:
            assert (carrier / "artifact").read_bytes() == expected
            assert not source.exists()
        assert deletion.active_deletion_journal_ids() == [journal_id]
        with closing(sqlite3.connect(db_path)) as db:
            _ = db.execute(
                "INSERT INTO import_queue(id,series_id,status) VALUES(71,1,'pending')"
            )
            assert not claim_import_queue_row(db, 71, "next-owner")
            assert db.execute(
                "SELECT lease_owner FROM import_queue WHERE id=71"
            ).fetchone() == (None,)


@pytest.mark.parametrize(
    "purpose", ["delete_carrier", "delete_marker", "delete_artifact", "restore"]
)
def test_deletion_action_rejects_held_private_or_restore_name_substitution(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    purpose: str,
) -> None:
    import file_identity
    import private_file_claim as claims
    import volume_file_deletion as deletion
    from file_mutation_lock import FileMutationGuard
    from import_lease import claim_import_queue_row

    db_path = Path(str(deletion_env["db_path"]))
    source = Path(str(deletion_env["file_path"]))
    expected = source.read_bytes()
    manager = (
        enrolled_restoration(deletion_env, monkeypatch)
        if purpose == "restore"
        else enrolled_namespace(db_path, source.parent, monkeypatch)
    )
    with manager as seeded:
        if purpose == "restore":
            assert isinstance(seeded, tuple)
            journal_id, source, expected = seeded
            path = source
        else:
            reservation = deletion.reserve_volume_file_deletion(1, 11)
            assert reservation.journal_id is not None
            journal_id = reservation.journal_id

            def stop(
                guard: FileMutationGuard,
                carrier: claims.CarrierHandle,
                expected: claims.FullFileFingerprint,
            ) -> None:
                raise InterruptedDeletion()

            with monkeypatch.context() as patch:
                patch.setattr(claims, "discard_private_regular", stop)
                with pytest.raises(InterruptedDeletion):
                    deletion.replay_volume_file_deletion(journal_id)
            path = Path(
                next(
                    o.canonical_path
                    for o in active(db_path, journal_id).proof.roles
                    if o.role.purpose == purpose
                )
            )
        retained = path.with_name(path.name + "-retained")
        real_native = file_identity.capture_native
        replaced = False
        typed: list[str] = []

        def raced(fd: int) -> file_identity.NativeHandle:
            nonlocal replaced
            observed = real_native(fd)
            if os.readlink(f"/proc/self/fd/{fd}") == str(path) and not replaced:
                replaced = True
                if path.is_dir():
                    path.rename(retained)
                    path.mkdir(mode=0o700)
                else:
                    data = path.read_bytes()
                    mode = path.stat().st_mode & 0o7777
                    path.rename(retained)
                    _ = path.write_bytes(data)
                    path.chmod(mode)
            return observed

        real_discard = claims.discard_private_regular
        real_restore_check = deletion._verify_restore_receipt

        def discard(
            guard: FileMutationGuard,
            carrier: claims.CarrierHandle,
            fingerprint: claims.FullFileFingerprint,
        ) -> None:
            with monkeypatch.context() as patch:
                patch.setattr(file_identity, "capture_native", raced)
                with pytest.raises(
                    claims.PrivateClaimError, match="current claim FD/name changed"
                ) as failure:
                    real_discard(guard, carrier, fingerprint)
                assert isinstance(
                    failure.value.__cause__, file_identity.IdentityMismatch
                )
                typed.append(str(failure.value))
                raise failure.value

        def restore_check(
            journal: deletion._DeletionJournal, record: claims.CarrierRecord
        ) -> claims.FullFileFingerprint:
            with monkeypatch.context() as patch:
                patch.setattr(file_identity, "capture_native", raced)
                with pytest.raises(
                    claims.PrivateClaimError, match="current claim FD/name changed"
                ) as failure:
                    real_restore_check(journal, record)
                assert isinstance(
                    failure.value.__cause__, file_identity.IdentityMismatch
                )
                typed.append(str(failure.value))
                raise failure.value

        if purpose == "restore":
            monkeypatch.setattr(deletion, "_verify_restore_receipt", restore_check)
        else:
            monkeypatch.setattr(claims, "discard_private_regular", discard)
        assert deletion.replay_volume_file_deletion(journal_id) == "blocked"
        assert replaced and typed == ["current claim FD/name changed"]
        journal = deletion._load_journal(journal_id)
        assert journal is not None and journal.state == "active"
        assert journal.claim_carrier_json is not None
        record = claims.CarrierRecord.from_json(journal.claim_carrier_json)
        artifact = (
            retained if purpose == "delete_carrier" else Path(record.carrier_path)
        ) / "artifact"
        assert artifact.read_bytes() == expected
        if purpose in {"delete_artifact", "restore"}:
            assert retained.read_bytes() == expected and path.read_bytes() == expected
        elif purpose == "delete_marker":
            assert retained.read_bytes() == path.read_bytes()
        else:
            assert retained.is_dir() and path.is_dir()
        assert deletion.active_deletion_journal_ids() == [journal_id]
        with closing(sqlite3.connect(db_path)) as db:
            _ = db.execute(
                "INSERT INTO import_queue(id,series_id,status) VALUES(71,1,'pending')"
            )
            assert not claim_import_queue_row(db, 71, "next-owner")
