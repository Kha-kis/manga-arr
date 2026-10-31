"""SIMULATED deletion fixtures; no native/profile qualification."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import ast
from collections import Counter
import hashlib
import json
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from test_file_identity_observation import ProducerMedia
    from test_file_identity_provision_wire import SyntheticGraph

import pytest

import file_identity
from file_identity import IdentityMismatch, NativeHandle, OwnerRef
import file_identity_claims as contexts
from file_identity_consumers import InventoryBudget, collect_references
import file_identity_store as ledger
from file_mutation_lock import file_mutation_guard
from maintenance_lock import service_maintenance_guard
import private_file_claim as claims
from test_file_identity_rescan import SimulatedMedia, seed_authority


def has_deletion_predicate_call(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Call)
        and (
            isinstance(child.func, ast.Name)
            and child.func.id == "unresolved_deletion_sql"
            or isinstance(child.func, ast.Attribute)
            and isinstance(child.func.value, ast.Name)
            and child.func.value.id in {"identity_store", "file_identity_store"}
            and child.func.attr == "unresolved_deletion_sql"
        )
        for child in ast.walk(node)
    )


def assert_reviewed_deletion_selectors(source: Path) -> None:
    """Exact reviewed admission plus semantic calls, not digest-only coverage."""
    fixture = Path(__file__).with_name("file_identity_deletion_selectors.json")
    reviewed = cast(list[dict[str, str]], json.loads(fixture.read_text()))
    allowed = {
        "shared-predicate",
        "historical",
        "schema",
        "projection",
        "delegated-replay",
        "other-domain",
    }
    assert all(row["policy"] in allowed for row in reviewed)
    expected = Counter((row["path"], row["symbol"], row["text"]) for row in reviewed)
    actual: Counter[tuple[str, str, str]] = Counter()
    symbols: dict[tuple[str, str], ast.AST] = {}
    for path in sorted((source / "app").rglob("*.py")):
        content = path.read_text()
        tree = ast.parse(content)
        owners = [
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        relative = path.relative_to(source).as_posix()
        for line_number, line in enumerate(content.splitlines(), 1):
            if not any(
                table in line
                for table in (
                    "rescan_file_operations",
                    "volume_file_deletions",
                    "import_publications",
                )
            ):
                continue
            enclosing = [
                n
                for n in owners
                if n.lineno <= line_number <= (n.end_lineno or n.lineno)
            ]
            owner = (
                min(enclosing, key=lambda n: (n.end_lineno or n.lineno) - n.lineno)
                if enclosing
                else None
            )
            name = owner.name if owner is not None else "<module>"
            actual[relative, name, line.strip()] += 1
            if owner is not None:
                symbols[relative, name] = owner
    assert actual == expected, {
        "unreviewed": list((actual - expected).elements()),
        "removed": list((expected - actual).elements()),
    }
    for row in reviewed:
        if row["policy"] == "shared-predicate":
            assert has_deletion_predicate_call(symbols[row["path"], row["symbol"]]), row


def captured_legacy_neighbor(
    db: sqlite3.Connection, source: Path, domain: str, state: str
) -> tuple[str, int]:
    """Captured pre-concurrency-fence states, never a neighbor identity grant.

    Current producers deliberately prohibit concurrent journals. These rows
    model retained legacy operations with real current row/fingerprint guards;
    no claim is made that a modern producer admitted them alongside deletion.
    """
    from dataclasses import asdict
    import import_publication
    import rescan
    import volume_file_deletion
    from test_file_identity_plan import encoded

    db.row_factory = sqlite3.Row
    volume = db.execute("SELECT * FROM volumes WHERE id=11").fetchone()
    series = db.execute("SELECT * FROM series WHERE id=1").fetchone()
    assert volume is not None and series is not None
    if domain == "publication":
        token = "7" * 32
        _ = db.execute(
            "INSERT INTO import_queue(id,series_id,status) VALUES(81,1,'pending')"
        )
        frozen = dict(db.execute("SELECT * FROM import_queue WHERE id=81").fetchone())
        frozen.update(status="importing", lease_owner=token)
        _ = db.execute(
            "INSERT INTO import_publications(id,queue_id,state,owner_token,series_id,dst_dir,import_mode,staging_dir,queue_snapshot_json,series_snapshot_json,series_tags_json,queue_status,queue_lease_owner) VALUES(81,81,?,?,1,?,'copy',?,?,?,'[]','importing',?)",
            (
                state,
                token,
                str(source.parent),
                import_publication.deterministic_staging_dir(
                    str(source.parent), 81, token
                ),
                encoded(frozen),
                encoded(dict(series)),
                token,
            ),
        )
        return "import_publications", 81
    if domain == "rescan":
        fingerprint = asdict(volume_file_deletion._regular_fingerprint(str(source)))
        _ = db.execute(
            "INSERT INTO rescan_file_operations(id,operation_token,series_id,volume_id,source_path,destination_path,expected_volume_json,expected_context_json,fingerprints_json,carriers_json,state) VALUES(81,?,1,11,?,?,?,?,?,?,?)",
            (
                "8" * 32,
                str(source),
                str(source.with_name("retained-rescan.cbz")),
                encoded({key: volume[key] for key in rescan._VOLUME_GUARD}),
                encoded({"series": dict(series), "tags": []}),
                encoded(
                    {
                        "source": fingerprint,
                        "stage": None,
                        "publication": None,
                        "captured_source": None,
                    }
                ),
                encoded(
                    {
                        "source": None,
                        "stage": None,
                        "publication": None,
                        "rollback": None,
                    }
                ),
                state,
            ),
        )
        return "rescan_file_operations", 81
    assert domain == "quarantine"
    info = source.stat()
    chapter = db.execute("SELECT * FROM chapters WHERE id=101").fetchone()
    assert chapter is not None
    _ = db.execute(
        "INSERT INTO chapter_quarantine(id,operation_token,series_id,volume_id,chapter_id,chapter_num,assembly_id,suwayomi_chapter_id,state,original_path,volume_path,file_inode,size_bytes,mtime_ns,sha256,chapter_guard_json) VALUES(81,?,1,11,101,1,81,101,?,?,?,?,?,?,?,?)",
        (
            "9" * 32,
            state,
            str(source.with_name("retained-chapter.cbz")),
            str(source),
            str(info.st_ino),
            info.st_size,
            info.st_mtime_ns,
            hashlib.sha256(source.read_bytes()).hexdigest(),
            encoded(dict(chapter)),
        ),
    )
    return "chapter_quarantine", 81


def synthetic_manifest2_namespace(
    env: ProducerMedia,
    graph: SyntheticGraph,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> Path:
    """Strict schema8 fixture bound to real disposable media, never A8 authority.

    Reuses SyntheticGraph and the SQL/media/SQL test's exact rebinding pattern.
    The test-only profile/native double does not qualify a consumer contract.
    """
    import secrets
    from dataclasses import replace
    from test_file_identity_plan import digest, encoded, parse_document, wire_object
    from test_file_identity_provision_schema import graph_row, insert_row
    from test_file_identity_store import object_value

    allocation = Path(str(graph.owner.key[0]))
    allocation.mkdir(mode=0o700)

    def token(size: int) -> str:
        assert size == 16
        return "1" * 32

    with monkeypatch.context() as patch:
        patch.setattr(secrets, "token_hex", token)
        with file_mutation_guard(str(env.db_path)) as guard:
            with claims.ensure_namespace(guard, str(allocation)):
                pass
    with closing(sqlite3.connect(env.db_path)) as db:
        legacy_json = cast(
            str,
            db.execute(
                "SELECT ownership_json FROM file_claim_namespaces WHERE parent_path=?",
                graph.owner.key,
            ).fetchone()[0],
        )

    def native(fd: int) -> NativeHandle:
        return NativeHandle(1, f"unit-{os.fstat(fd).st_ino}".encode())

    import os

    objects: list[ledger.ObjectProof] = []
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
        objects.append(
            replace(
                obj,
                native=NativeHandle(1, f"unit-{info.st_ino}".encode()),
                constraints_json=encoded(fields),
            )
        )
    birth = parse_document(graph.birth_json)
    controlled = wire_object(birth["controlled_birth"])
    controlled["objects"] = [object_value(o) for o in objects]
    _ = controlled.pop("receipt_sha256")
    controlled["receipt_sha256"] = digest(controlled)
    epoch = graph_row(graph, "file_identity_epochs")
    epoch["birth_json"] = encoded(birth)
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
    authority = graph_row(graph, "file_identity_authorities")
    profile = parse_document(cast(str, authority["profile_json"]))
    candidate = file_identity.StorageProfile(
        cast(str, profile["profile_id"]),
        cast(int, profile["revision"]),
        cast(str, profile["qualification_sha256"]),
        cast(dict[str, object], profile["client_contract"]),
        cast(dict[str, object], profile["server_contract"]),
    )
    monkeypatch.setattr(file_identity, "_QUALIFIED_PROFILES", (candidate,))
    monkeypatch.setattr(file_identity, "capture_native", native)
    assert state in {"prepared-only", "active-only", "grant"}
    count = {"prepared-only": 1, "active-only": 2, "grant": 3}[state]
    with closing(sqlite3.connect(env.db_path)) as db, db:
        _ = db.execute("PRAGMA foreign_keys=ON")
        for table, row in (
            ("file_identity_authorities", authority),
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
            if cast(int, event["id"]) > count:
                continue
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
    return allocation


@contextmanager
def enrolled_namespace(
    db_path: Path, parent: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[SimulatedMedia, None, None]:
    media = SimulatedMedia(parent)
    monkeypatch.setattr(file_identity, "capture_native", media.capture)
    try:
        with service_maintenance_guard(str(db_path), exclusive=True):
            with file_mutation_guard(str(db_path)) as guard:
                with claims.ensure_namespace(guard, str(parent)):
                    pass
                with closing(sqlite3.connect(db_path)) as db:
                    _ = seed_authority(db, parent, monkeypatch)
                    closure = collect_references(db, str(parent), InventoryBudget())
                assert {s.owner for s in closure.snapshots} == {
                    OwnerRef("namespace", (str(parent),))
                }
                assert len(contexts.enroll_exact_legacy(guard, closure, "a" * 32)) == 1
        assert_namespace_live(db_path, parent)
        yield media
    finally:
        media.release()


def assert_namespace_live(db_path: Path, parent: Path) -> None:
    with file_mutation_guard(str(db_path)) as guard:
        with closing(sqlite3.connect(db_path)) as db:
            context = contexts.resolve_context(
                db, guard, OwnerRef("namespace", (str(parent),))
            )
        assert context is not None
        contexts.verify_complete(context)


def change_namespace_native(
    db_path: Path, parent: Path, media: SimulatedMedia, purpose: str
) -> None:
    with closing(sqlite3.connect(db_path)) as db:
        active = ledger.read_active(db, OwnerRef("namespace", (str(parent),)))
    assert active is not None
    obj = next(o for o in active.proof.roles if o.role.purpose == purpose)
    media.native[Path(obj.canonical_path).stat().st_ino] = NativeHandle(
        1, b"independent-different-subvolume"
    )
    with pytest.raises(IdentityMismatch, match="native identity differs"):
        assert_namespace_live(db_path, parent)
