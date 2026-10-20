"""SQL-only inventory contracts against the real disposable schema."""

from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from download_identity import (
    DownloadIdentity,
    download_identity_key,
    download_identity_path_token,
)
from file_identity import IdentityMismatch, InvalidIdentity, OwnerRef, RoleKey
from private_file_claim import CarrierRecord, ClaimBinding, FullFileFingerprint
from private_pack_claim import (
    DirectoryProof,
    PackDirectoryClaim,
    PackInventory,
    PackOwnership,
)
from test_file_identity_schema import ledger_db as ledger_db, ledger_path as ledger_path

TOKEN = "a" * 32
FP = FullFileFingerprint(1, 2, 3, 4, "b" * 64)
IDENTITY = DownloadIdentity(1, "torrent", "abc")
KEY = download_identity_key(IDENTITY)
PACK = "/packs/queue-client-1-torrent-" + download_identity_path_token(IDENTITY)


def adapter() -> ModuleType:
    try:
        return importlib.import_module("file_identity_consumers")
    except ModuleNotFoundError as exc:
        if exc.name != "file_identity_consumers":
            raise
        pytest.fail("Task4a disabled SQL/reference inventory is not implemented")


def encoded(value: object) -> str:
    return json.dumps(value, sort_keys=True)


def insert(db: sqlite3.Connection, table: str, **values: object) -> None:
    db.execute(
        f"INSERT INTO {table}({','.join(values)}) VALUES({','.join('?' for _ in values)})",
        tuple(values.values()),
    )


def rows(db: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    cursor = db.execute(f"SELECT * FROM {table}")
    return [
        dict(zip([c[0] for c in cursor.description], row, strict=True))
        for row in cursor
    ]


def all_rows(
    db: sqlite3.Connection,
) -> tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]:
    tables = [
        r[0]
        for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    ]
    return tuple(
        (t, tuple(db.execute(f'SELECT * FROM "{t}" ORDER BY rowid'))) for t in tables
    )


def namespace(db: sqlite3.Connection, parent: str, *, version: int = 1) -> str:
    proof: dict[str, object] = dict(
        version=version,
        owner_token=TOKEN,
        parent_dev=1,
        parent_ino=2,
        root_path=parent + "/.mangarr-claims",
        root_dev=1,
        root_ino=3,
        marker_fingerprint=asdict(FP),
    )
    if version == 2:
        proof["creation_boundary"] = dict(
            kind="exclusive_parent", parent_uid=1000, parent_mode=0o755
        )
    legacy = "  " + encoded(proof) + "\n"
    insert(db, "file_claim_namespaces", parent_path=parent, ownership_json=legacy)
    return legacy


def carrier(
    db: sqlite3.Connection,
    origin: str,
    binding: ClaimBinding,
    *,
    token: str = TOKEN,
    fingerprint: FullFileFingerprint | None = FP,
) -> CarrierRecord:
    parent = origin.rsplit("/", 1)[0]
    if not db.execute(
        "SELECT 1 FROM file_claim_namespaces WHERE parent_path=?", (parent,)
    ).fetchone():
        namespace(db, parent)
    return CarrierRecord(
        1,
        token,
        binding,
        parent + "/.mangarr-claims/" + token,
        1,
        4,
        FP,
        origin,
        "claimed" if fingerprint else "allocated",
        fingerprint,
    )


def context(db: sqlite3.Connection, series: int = 1, volume: int = 1) -> None:
    insert(db, "series", id=series, title="Fixture", search_pattern="Fixture")
    insert(
        db,
        "volumes",
        id=volume,
        series_id=series,
        volume_num=1,
        import_path="/library/source.cbz",
    )


def rescan(
    db: sqlite3.Connection, *, row_id: int = 1, parent: str = "/library"
) -> None:
    import rescan as producer

    source, destination = parent + "/source.cbz", parent + "/destination.cbz"
    rec = carrier(db, source, ClaimBinding("rescan", TOKEN, 1, "source"))
    volume = rows(db, "volumes")[0]
    insert(
        db,
        "rescan_file_operations",
        id=row_id,
        operation_token=TOKEN,
        series_id=1,
        volume_id=1,
        source_path=source,
        destination_path=destination,
        expected_volume_json=encoded({k: volume[k] for k in producer._VOLUME_GUARD}),
        expected_context_json=encoded({"series": rows(db, "series")[0], "tags": []}),
        fingerprints_json=encoded(
            dict(source=asdict(FP), stage=None, publication=None, captured_source=None)
        ),
        carriers_json=encoded(
            dict(source=asdict(rec), stage=None, publication=None, rollback=None)
        ),
        state="rolled_back",
    )


def queue(
    db: sqlite3.Connection,
    *,
    queue_id: int = 1,
    series: int = 1,
    source: str = "/incoming/source.cbz",
) -> None:
    insert(
        db,
        "import_queue",
        id=queue_id,
        series_id=series,
        download_id="abc",
        download_client_id=1,
        download_protocol="torrent",
        src_dir=source.rsplit("/", 1)[0],
        status="imported",
    )
    insert(
        db,
        "import_queue_files",
        id=queue_id,
        queue_id=queue_id,
        filename="source.cbz",
        src_path=source,
        dst_path="/library/final.cbz",
        status="imported",
    )


def publication(db: sqlite3.Connection, *, queue_id: int = 1, series: int = 1) -> None:
    q = next(r for r in rows(db, "import_queue") if r["id"] == queue_id)
    q["status"], q["lease_owner"] = "importing", TOKEN
    q["_file_publication"] = dict(
        version=1, decision="commit", admission=None, artifacts={}
    )
    suffix = hashlib.sha256(TOKEN.encode()).hexdigest()[:32]
    staging = f"/library/.mangarr-publication-{queue_id}-{suffix}"
    insert(
        db,
        "import_publications",
        id=queue_id,
        queue_id=queue_id,
        state="finalized",
        owner_token=TOKEN,
        series_id=series,
        dst_dir="/library",
        import_mode="move",
        staging_dir=staging,
        queue_snapshot_json=encoded(q),
        series_snapshot_json=encoded(rows(db, "series")[0]),
        series_tags_json="[]",
        queue_status="importing",
        queue_download_id="abc",
        queue_download_client_id=1,
        queue_src_dir=q["src_dir"],
        queue_lease_owner=TOKEN,
    )
    source = next(
        r for r in rows(db, "import_queue_files") if r["queue_id"] == queue_id
    )["src_path"]
    insert(
        db,
        "import_publication_files",
        id=queue_id,
        publication_id=queue_id,
        ordinal=0,
        file_id=queue_id,
        src_path=source,
        filename="source.cbz",
        dst_path="/library/final.cbz",
        import_kind="volume",
        file_type="cbz",
        is_special=0,
        has_volume_range=0,
        is_legacy_chapter_stub=0,
        is_legacy_chapter_recheck=0,
        plan_status="ready",
        stage_path=staging + "/final.cbz",
        final_path="/library/final.cbz",
        stage_state="staged",
        publish_state="published",
        cleanup_state="deleted",
        source_dev=1,
        source_inode=2,
        source_size=3,
        source_mtime_ns=4,
        source_sha256=FP.sha256,
        staged_dev=1,
        staged_inode=2,
        staged_size=3,
        staged_mtime_ns=4,
        staged_sha256=FP.sha256,
    )


def deletion(db: sqlite3.Connection) -> None:
    import volume_file_deletion as producer

    target = "/library/source.cbz"
    rec = carrier(db, target, ClaimBinding("deletion", "1", 1, "delete"))
    insert(
        db,
        "volume_file_deletions",
        id=1,
        volume_id=1,
        series_id=1,
        state="completed",
        target_path=target,
        parent_path="/library",
        claim_path=producer._claim_path(1, target),
        target_present=1,
        target_dev=1,
        target_inode=2,
        target_size=3,
        target_mtime_ns=4,
        target_sha256=FP.sha256,
        series_title="Fixture",
        claim_carrier_json=rec.to_json(),
    )


def pack(
    db: sqlite3.Connection, *, version: int = 3, tombstone: bool = False
) -> PackOwnership:
    inventory = PackInventory({"source.cbz": FP}, ())
    directory = DirectoryProof(PACK, 1, 2, FP)
    ownership = PackOwnership(
        KEY,
        TOKEN,
        "attached",
        canonical_directory=directory,
        inventory=inventory,
        version=version,
        queue_receipt_sha256=hashlib.sha256(
            json.dumps(
                {
                    "queue": rows(db, "import_queue")[0],
                    "files": rows(db, "import_queue_files"),
                },
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
        if version == 3
        else None,
    )
    insert(
        db,
        "import_pack_cleanup_reservations",
        download_identity_key=KEY,
        download_client_id=1,
        protocol="torrent",
        normalized_download_id="abc",
        download_id="abc",
        purpose="queueing",
        owner_token="d" * 32,
        queue_id=1,
        publication_id=1,
        pack_path=PACK,
        directory_ownership_json=ownership.to_json(),
        expires_at="2026-10-21 00:00:00",
    )
    if tombstone:
        rec = carrier(
            db,
            PACK,
            ClaimBinding("pack", TOKEN, None, "pack_cleanup"),
            fingerprint=None,
        )
        claim = PackDirectoryClaim(rec, directory, inventory, TOKEN)
        insert(
            db,
            "import_pack_cleanup_tombstones",
            tombstone_path=rec.carrier_path + "/artifact",
            download_identity_key=KEY,
            download_client_id=1,
            protocol="torrent",
            normalized_download_id="abc",
            download_id="abc",
            queue_id=1,
            publication_id=1,
            pack_path=PACK,
            carrier_json=claim.to_json(),
        )
    return ownership


def origin(db: sqlite3.Connection, ownership: PackOwnership) -> None:
    q = json.loads(rows(db, "import_publications")[0]["queue_snapshot_json"])
    q["_pack_source_origins"] = {
        "version": 1,
        "files": {
            "1": dict(
                kind="pack",
                file_id=1,
                src_path=PACK + "/source.cbz",
                queue_id=1,
                download_identity_key=KEY,
                download_client_id=1,
                download_protocol="torrent",
                download_id="abc",
                logical_pack_path=PACK,
                artifact_owner_token=TOKEN,
                relative_path="source.cbz",
                ownership_json=ownership.to_json(),
                source_fingerprint=asdict(FP),
            )
        },
    }
    db.execute("UPDATE import_publications SET queue_snapshot_json=?", (encoded(q),))


def collect(db: sqlite3.Connection, parent: str = "/", /, **bounds: int) -> Any:
    module = adapter()
    return module.collect_references(db, parent, module.InventoryBudget(**bounds))


@pytest.mark.parametrize(
    "domain",
    [
        "namespace",
        "rescan",
        "publication",
        "deletion",
        "pack_reservation",
        "pack_tombstone",
        "queue_source",
    ],
)
def test_each_domain_real_schema_exact_rows_and_legacy_bytes(
    ledger_db: sqlite3.Connection, domain: str
) -> None:
    db = ledger_db
    context(db)
    if domain == "namespace":
        namespace(db, "/library", version=2)
    elif domain == "rescan":
        rescan(db)
    elif domain == "deletion":
        deletion(db)
    else:
        queue(
            db,
            source=PACK + "/source.cbz"
            if domain.startswith("pack")
            else "/incoming/source.cbz",
        )
        if domain != "queue_source":
            publication(db)
        if domain.startswith("pack"):
            pack(db, tombstone=domain == "pack_tombstone")
    db.commit()
    before = all_rows(db)
    closure = collect(db)
    assert domain in {s.owner.domain for s in closure.snapshots}
    assert closure.roles
    assert all(r.owner in {s.owner for s in closure.snapshots} for r in closure.roles)
    assert all_rows(db) == before
    assert not db.in_transaction
    assert collect(db) == closure
    for snapshot in closure.snapshots:
        assert adapter().snapshot_owner(db, snapshot.owner) == snapshot
        assert adapter().roles_for(snapshot) == tuple(
            r for r in closure.roles if r.owner == snapshot.owner
        )


@pytest.mark.parametrize("version", [1, 2, 3])
def test_terminal_origin_resolves_frozen_ownership_when_reservation_changes(
    ledger_db: sqlite3.Connection, version: int
) -> None:
    db = ledger_db
    context(db)
    queue(db, source=PACK + "/source.cbz")
    publication(db)
    ownership = pack(db, version=version, tombstone=True)
    origin(db, ownership)
    db.execute(
        "UPDATE import_pack_cleanup_reservations SET directory_ownership_json=?",
        (replace(ownership, phase="retained", retained_reason="terminal").to_json(),),
    )
    db.commit()
    before = all_rows(db)
    closure = collect(db, "/library")
    assert {s.owner.domain for s in closure.snapshots} >= {
        "publication",
        "queue_source",
        "pack_reservation",
        "pack_tombstone",
        "namespace",
    }
    assert any(r.path == PACK + "/source.cbz" for r in closure.roles)
    assert closure.dependencies and closure.raw_rows
    assert all_rows(db) == before


@pytest.mark.parametrize(
    "mutation",
    ["version", "duplicate", "role", "phase", "escape", "binding", "fingerprint"],
)
def test_malformed_unrelated_rescan_refuses_entire_inventory(
    ledger_db: sqlite3.Connection, mutation: str
) -> None:
    db = ledger_db
    context(db)
    rescan(db, parent="/unrelated")
    row = rows(db, "rescan_file_operations")[0]
    carriers = json.loads(row["carriers_json"])
    if mutation == "version":
        carriers["source"]["version"] = True
    elif mutation == "role":
        carriers["unexpected"] = None
    elif mutation == "phase":
        carriers["source"]["phase"] = "unknown"
    elif mutation == "escape":
        carriers["source"]["carrier_path"] = "/unrelated/../escaped"
    elif mutation == "binding":
        carriers["source"]["binding"]["file_id"] = 2
    elif mutation == "fingerprint":
        carriers["source"]["artifact_fingerprint"]["size"] = 99
    text = encoded(carriers)
    if mutation == "duplicate":
        text = text.replace('"source": {', '"source": null, "source": {', 1)
    db.execute("UPDATE rescan_file_operations SET carriers_json=?", (text,))
    db.commit()
    before = all_rows(db)
    with pytest.raises((InvalidIdentity, IdentityMismatch)):
        collect(db, "/library")
    assert all_rows(db) == before


def test_null_namespace_is_unresolved_not_empty(ledger_db: sqlite3.Connection) -> None:
    insert(
        ledger_db,
        "file_claim_namespaces",
        parent_path="/elsewhere",
        ownership_json=None,
    )
    ledger_db.commit()
    with pytest.raises((InvalidIdentity, IdentityMismatch)):
        collect(ledger_db, "/library")


@pytest.mark.parametrize("inconsistent", [False, True])
def test_publication_duplicate_carriers_semantics_not_encoding(
    ledger_db: sqlite3.Connection, inconsistent: bool
) -> None:
    db = ledger_db
    context(db)
    queue(db)
    publication(db)
    rec = carrier(
        db, "/incoming/source.cbz", ClaimBinding("import_publication", "1", 1, "source")
    )
    q = json.loads(rows(db, "import_publications")[0]["queue_snapshot_json"])
    q["_file_publication"]["artifacts"] = {"1": {"source": rec.to_json()}}
    db.execute("UPDATE import_publications SET queue_snapshot_json=?", (encoded(q),))
    proof = asdict(rec)
    if inconsistent:
        proof["dir_ino"] = 999
    legacy = "  " + encoded(proof) + "\n"
    db.execute(
        "UPDATE import_publication_files SET source_claim_carrier_json=?", (legacy,)
    )
    db.commit()
    before = all_rows(db)
    if inconsistent:
        with pytest.raises((InvalidIdentity, IdentityMismatch)):
            collect(db)
    else:
        result = collect(db)
        assert any(r.legacy_json == legacy for r in result.roles)
    assert all_rows(db) == before


def test_dangling_queue_and_cross_series_context(ledger_db: sqlite3.Connection) -> None:
    db = ledger_db
    context(db)
    context(db, 2, 2)
    queue(db, series=2)
    publication(db, series=1)
    db.commit()
    result = collect(db, "/library")
    assert {r.key for r in result.raw_rows if r.table == "series"} == {(1,), (2,)}
    db.execute("DELETE FROM import_queue_files")
    db.execute("DELETE FROM import_queue")
    db.commit()
    with pytest.raises((InvalidIdentity, IdentityMismatch)):
        collect(db)


@pytest.mark.parametrize(
    "bounds", [{"max_rows": 1}, {"max_objects": 1}, {"max_hash_bytes": 1}]
)
def test_budgets_refuse_without_partial_success(
    ledger_db: sqlite3.Connection, bounds: dict[str, int]
) -> None:
    namespace(ledger_db, "/library")
    ledger_db.commit()
    before = all_rows(ledger_db)
    with pytest.raises(InvalidIdentity):
        collect(ledger_db, **bounds)
    assert all_rows(ledger_db) == before


@pytest.mark.parametrize("value", [True, 0, -1, None, float("inf"), 2**64])
def test_strict_immutable_budgets(value: object) -> None:
    with pytest.raises(InvalidIdentity):
        adapter().InventoryBudget(max_rows=value)
    valid = adapter().InventoryBudget()
    with pytest.raises(FrozenInstanceError):
        valid.max_rows = 1


def test_scope_boundary_and_owner_local_roles(ledger_db: sqlite3.Connection) -> None:
    db = ledger_db
    namespace(db, "/library")
    namespace(db, "/library-other")
    db.commit()
    assert len(collect(db, "/library").snapshots) == 1
    assert not collect(db, "/missing").snapshots
    module = adapter()
    key = RoleKey(None, "root", "directory", None)
    a = module.RoleSpec(
        key,
        "/library",
        None,
        None,
        "intent",
        "parent_identity",
        OwnerRef("namespace", ("/library",)),
    )
    b = replace(a, owner=OwnerRef("namespace", ("/other",)))
    assert a != b and a.role == b.role and a.path == b.path
    with pytest.raises(InvalidIdentity):
        replace(a, presence="absent")
    with pytest.raises(InvalidIdentity):
        collect(db, "/library/../escaped")


def test_no_media_calls_and_no_sql_writes(
    ledger_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    module = adapter()
    namespace(ledger_db, "/library")
    ledger_db.commit()

    def forbidden(*_args: object, **_kwargs: object) -> Any:
        pytest.fail("SQL inventory attempted media I/O")

    for name in ("stat", "lstat", "open", "scandir", "walk"):
        monkeypatch.setattr(os, name, forbidden)

    def authorize(action: int, *_args: object) -> int:
        if action in (
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE,
        ):
            pytest.fail("SQL inventory attempted a write")
        return sqlite3.SQLITE_OK

    ledger_db.set_authorizer(authorize)
    try:
        assert module.collect_references(ledger_db, "/", module.InventoryBudget()).roles
    finally:
        ledger_db.set_authorizer(None)


@pytest.mark.parametrize("same_connection", [False, True])
def test_sql_phantom_or_drift_is_detected(
    ledger_db: sqlite3.Connection,
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    same_connection: bool,
) -> None:
    module = adapter()
    db = ledger_db
    db.execute("PRAGMA journal_mode=WAL")
    namespace(db, "/library")
    db.commit()
    real = module.capture_owner_snapshot
    fired = False

    def capture(connection: sqlite3.Connection, owner: OwnerRef) -> Any:
        nonlocal fired
        snapshot = real(connection, owner)
        if not fired:
            fired = True
            if same_connection:
                namespace(connection, "/phantom")
            else:
                with sqlite3.connect(ledger_path) as other:
                    namespace(other, "/phantom")
        return snapshot

    monkeypatch.setattr(module, "capture_owner_snapshot", capture)
    with pytest.raises(IdentityMismatch):
        collect(db)
    assert fired


def test_keyset_paging_and_all_scanned_rows_budget(
    ledger_db: sqlite3.Connection,
) -> None:
    for i in range(70):
        namespace(ledger_db, f"/unrelated/{i:03}")
    ledger_db.commit()
    result = collect(ledger_db)
    assert len(result.snapshots) == 70
    assert len({s.sha256 for s in result.snapshots}) == 70
    assert not collect(ledger_db, "/empty").snapshots
    with pytest.raises(InvalidIdentity):
        collect(ledger_db, "/empty", max_rows=69)


def test_private_publication_staging_actual_producer_shape(
    ledger_db: sqlite3.Connection,
) -> None:
    from import_publication import StageCarrierRecord

    db = ledger_db
    context(db)
    queue(db)
    publication(db)
    row = rows(db, "import_publications")[0]
    rec = carrier(
        db,
        row["staging_dir"],
        ClaimBinding("import_publication", "1", None, "staging"),
        fingerprint=None,
    )
    q = json.loads(row["queue_snapshot_json"])
    q["_publication_staging"] = StageCarrierRecord(
        1, rec.binding, rec.origin_path, "ready", rec
    ).to_value()
    db.execute(
        "UPDATE import_publications SET staging_dir=?,queue_snapshot_json=?",
        (rec.carrier_path, encoded(q)),
    )
    db.execute(
        "UPDATE import_publication_files SET stage_path=?",
        (rec.carrier_path + "/final.cbz",),
    )
    db.commit()
    before = all_rows(db)
    result = collect(db)
    assert not any(r.role.purpose == "staging_artifact" for r in result.roles)
    stage = next(r for r in result.roles if r.role.purpose == "stage")
    assert stage.parent_role is not None and stage.role.relative == "final.cbz"
    assert any(
        r.path == rec.carrier_path and r.role.purpose == "staging_carrier"
        for r in result.roles
    )
    assert all_rows(db) == before


def test_private_pack_placement_actual_producer_shape(
    ledger_db: sqlite3.Connection,
) -> None:
    db = ledger_db
    context(db)
    queue(db)
    publication(db)
    ownership = pack(db, version=2)
    private = PACK + ".owner-" + TOKEN
    rec = carrier(
        db, private, ClaimBinding("pack", TOKEN, None, "pack_cleanup"), fingerprint=None
    )
    rec = replace(rec, phase="claimed")
    directory = DirectoryProof(rec.carrier_path + "/artifact", 1, 2, FP)
    ownership = replace(
        ownership,
        private_directory=directory,
        canonical_directory=directory,
        placement_carrier=rec,
    )
    db.execute(
        "UPDATE import_pack_cleanup_reservations SET directory_ownership_json=?",
        (ownership.to_json(),),
    )
    db.commit()
    before = all_rows(db)
    assert any(
        r.path == directory.path + "/source.cbz" for r in collect(db, "/packs").roles
    )
    assert all_rows(db) == before


@pytest.mark.parametrize(
    "mutation",
    [
        "version",
        "role",
        "phase",
        "path",
        "origin",
        "dangling",
        "queue_phase",
        "pack_identity",
    ],
)
def test_nested_publication_and_pack_refusals(
    ledger_db: sqlite3.Connection, mutation: str
) -> None:
    db = ledger_db
    context(db)
    queue(db, source=PACK + "/source.cbz")
    publication(db)
    ownership = pack(db)
    origin(db, ownership)
    q = json.loads(rows(db, "import_publications")[0]["queue_snapshot_json"])
    if mutation == "version":
        q["_pack_source_origins"]["version"] = 99
    elif mutation == "role":
        q["_file_publication"]["artifacts"] = {"1": {"surprise": None}}
    elif mutation == "phase":
        q["_file_publication"]["decision"] = "surprise"
    elif mutation == "path":
        q["_pack_source_origins"]["files"]["1"]["relative_path"] = "../escape"
    elif mutation == "origin":
        q["_pack_source_origins"]["files"]["1"]["file_id"] = 2
    elif mutation == "dangling":
        db.execute("DELETE FROM import_pack_cleanup_reservations")
    elif mutation == "queue_phase":
        db.execute("UPDATE import_queue SET status='surprise'")
    elif mutation == "pack_identity":
        db.execute(
            "UPDATE import_pack_cleanup_reservations SET directory_ownership_json=?",
            (replace(ownership, identity_key="wrong").to_json(),),
        )
    db.execute("UPDATE import_publications SET queue_snapshot_json=?", (encoded(q),))
    db.commit()
    before = all_rows(db)
    with pytest.raises((InvalidIdentity, IdentityMismatch)):
        collect(db, "/unrelated")
    assert all_rows(db) == before


def test_path_selected_context_and_quarantine_are_evidence_not_owners(
    ledger_db: sqlite3.Connection,
) -> None:
    db = ledger_db
    context(db)
    insert(
        db,
        "chapter_quarantine",
        id=1,
        operation_token=TOKEN,
        series_id=1,
        volume_id=1,
        chapter_id=1,
        assembly_id=1,
        suwayomi_chapter_id=1,
        state="quarantined",
        original_path="/quarantine/original.cbz",
        volume_path="/other/volume.cbz",
        file_inode="2",
        size_bytes=3,
        mtime_ns=4,
        sha256=FP.sha256,
        chapter_guard_json="{}",
    )
    db.commit()
    before = all_rows(db)
    result = collect(db, "/quarantine")
    assert {r.table for r in result.raw_rows} >= {
        "chapter_quarantine",
        "series",
        "volumes",
    }
    assert not result.snapshots and not result.roles
    assert all_rows(db) == before


def test_clean_connection_required_without_hidden_commit(
    ledger_db: sqlite3.Connection,
) -> None:
    namespace(ledger_db, "/library")
    before = all_rows(ledger_db)
    assert ledger_db.in_transaction
    with pytest.raises(IdentityMismatch):
        collect(ledger_db)
    assert ledger_db.in_transaction and all_rows(ledger_db) == before


def test_snapshot_and_raw_immutable_strict_constructors(
    ledger_db: sqlite3.Connection,
) -> None:
    module = adapter()
    namespace(ledger_db, "/library")
    ledger_db.commit()
    result = collect(ledger_db)
    with pytest.raises(FrozenInstanceError):
        result.raw_rows = ()
    with pytest.raises(InvalidIdentity):
        replace(result, snapshots=result.snapshots * 2)
    with pytest.raises(InvalidIdentity):
        replace(result, roles=result.roles[::-1])
    with pytest.raises(InvalidIdentity):
        module.RawRow("series", (1,), (("id", True),))
    with pytest.raises(InvalidIdentity):
        module.RawRow("series", (1, 2), (("id", 1),))


def producer_admission(db: sqlite3.Connection, q: dict[str, Any]) -> dict[str, Any]:
    from import_plan import _FilePlan, _ImportPlan, publication_admission

    file = _FilePlan(
        file_id=1,
        src_path="/incoming/source.cbz",
        filename="source.cbz",
        dst_path="/library/final.cbz",
        import_kind="volume",
        file_type="cbz",
        proposed_vol=1,
        proposed_chap=None,
        chap_range_end=None,
        vol_range_start=None,
        vol_range_end=None,
        pack_type=None,
        is_special=0,
        special_title=None,
        has_volume_range=False,
        is_legacy_chapter_stub=False,
        is_legacy_chapter_recheck=False,
        plan_status="ready",
        plan_failure_reason="",
    )
    plan = _ImportPlan(
        q, rows(db, "series")[0], [], "/library", "move", None, [file], 1
    )
    db.row_factory = sqlite3.Row
    try:
        admission = publication_admission(db, plan)
    finally:
        db.row_factory = None
    return admission


@pytest.mark.parametrize("escape", [False, True])
def test_admission_nested_static_paths_are_validated(
    ledger_db: sqlite3.Connection, escape: bool
) -> None:
    db = ledger_db
    context(db)
    queue(db)
    publication(db)
    q = json.loads(rows(db, "import_publications")[0]["queue_snapshot_json"])
    admission = producer_admission(db, q)
    if escape:
        admission["files"]["1"]["volumes"][0]["import_path"] = "/elsewhere/../escape"
    q["_file_publication"]["admission"] = admission
    db.execute("UPDATE import_publications SET queue_snapshot_json=?", (encoded(q),))
    db.commit()
    if escape:
        with pytest.raises((InvalidIdentity, IdentityMismatch)):
            collect(db, "/missing")
    else:
        assert collect(db).roles


def test_snapshot_owner_borrows_writer_without_committing_or_rolling_back(
    ledger_db: sqlite3.Connection,
) -> None:
    module = adapter()
    namespace(ledger_db, "/library")
    assert ledger_db.in_transaction
    before = all_rows(ledger_db)
    snapshot = module.snapshot_owner(ledger_db, OwnerRef("namespace", ("/library",)))
    assert snapshot.owner.domain == "namespace"
    assert ledger_db.in_transaction and all_rows(ledger_db) == before
    ledger_db.execute("UPDATE file_claim_namespaces SET ownership_json=NULL")
    malformed = all_rows(ledger_db)
    with pytest.raises(InvalidIdentity):
        module.snapshot_owner(ledger_db, snapshot.owner)
    assert ledger_db.in_transaction and all_rows(ledger_db) == malformed


def test_terminal_frozen_origin_survives_replaced_reservation(
    ledger_db: sqlite3.Connection,
) -> None:
    db = ledger_db
    context(db)
    queue(db, source=PACK + "/source.cbz")
    publication(db)
    admitted = pack(db)
    origin(db, admitted)
    context(db, 2, 2)
    queue(db, queue_id=2, series=2, source=PACK + "/source.cbz")
    replacement = replace(admitted, artifact_owner_token="f" * 32)
    db.execute(
        "UPDATE import_pack_cleanup_reservations SET owner_token=?,queue_id=2,publication_id=NULL,directory_ownership_json=?",
        ("e" * 32, replacement.to_json()),
    )
    db.commit()
    before = all_rows(db)
    result = collect(db, "/library")
    assert {s.owner for s in result.snapshots} >= {
        OwnerRef("queue_source", (1,)),
        OwnerRef("queue_source", (2,)),
    }
    assert any(r.legacy_json == admitted.to_json() for r in result.roles)
    assert any(r.legacy_json == replacement.to_json() for r in result.roles)
    assert all_rows(db) == before


@pytest.mark.parametrize(
    "mutation",
    [
        "namespace_version",
        "namespace_duplicate",
        "pack_version",
        "pack_phase",
        "pack_claim_role",
        "staging_escape",
        "null_pack_path",
    ],
)
def test_each_legacy_parser_refuses_unknown_or_escaped_evidence(
    ledger_db: sqlite3.Connection, mutation: str
) -> None:
    db = ledger_db
    context(db)
    queue(db)
    publication(db)
    proof = pack(db, tombstone=True)
    if mutation.startswith("namespace"):
        column, table = "ownership_json", "file_claim_namespaces"
        raw = rows(db, table)[0][column]
        if mutation == "namespace_version":
            value = json.loads(raw)
            value["version"] = 99
            raw = encoded(value)
        else:
            raw = raw.replace('"version": 1', '"version": 1, "version": 1')
        db.execute(f"UPDATE {table} SET {column}=?", (raw,))
    elif mutation == "staging_escape":
        db.execute("UPDATE import_publications SET staging_dir='/unrelated/stage'")
        db.execute(
            "UPDATE import_publication_files SET stage_path='/unrelated/stage/final.cbz'"
        )
    elif mutation == "null_pack_path":
        db.execute(
            "UPDATE import_pack_cleanup_reservations SET directory_ownership_json=NULL,pack_path='/unrelated/not-a-pack'"
        )
    else:
        value = json.loads(proof.to_json())
        if mutation == "pack_version":
            value["version"] = 99
        elif mutation == "pack_phase":
            value["phase"] = "surprise"
        else:
            claim = json.loads(
                rows(db, "import_pack_cleanup_tombstones")[0]["carrier_json"]
            )
            claim["carrier"]["binding"]["purpose"] = "delete"
            value["claims"] = [claim]
        db.execute(
            "UPDATE import_pack_cleanup_reservations SET directory_ownership_json=?",
            (encoded(value),),
        )
    db.commit()
    before = all_rows(db)
    with pytest.raises((InvalidIdentity, IdentityMismatch)):
        collect(db, "/missing")
    assert all_rows(db) == before


def test_admission_cross_series_dependencies_are_followed(
    ledger_db: sqlite3.Connection,
) -> None:
    db = ledger_db
    context(db)
    context(db, 2, 2)
    queue(db)
    publication(db)
    db.execute("UPDATE volumes SET import_path='/library/final.cbz' WHERE id=2")
    q = json.loads(rows(db, "import_publications")[0]["queue_snapshot_json"])
    q["_file_publication"]["admission"] = producer_admission(db, q)
    db.execute("UPDATE import_publications SET queue_snapshot_json=?", (encoded(q),))
    db.commit()
    result = collect(db, "/incoming")
    assert {r.key for r in result.raw_rows if r.table == "series"} == {(1,), (2,)}


def test_pack_queue_link_cannot_borrow_another_download(
    ledger_db: sqlite3.Connection,
) -> None:
    db = ledger_db
    context(db)
    queue(db)
    publication(db)
    pack(db)
    db.execute("UPDATE import_queue SET download_id='foreign'")
    db.commit()
    before = all_rows(db)
    with pytest.raises((InvalidIdentity, IdentityMismatch)):
        collect(db, "/missing")
    assert all_rows(db) == before


def test_namespace_token_and_carrier_directory_are_bound(
    ledger_db: sqlite3.Connection,
) -> None:
    db = ledger_db
    context(db)
    rescan(db)
    namespace_json = json.loads(rows(db, "file_claim_namespaces")[0]["ownership_json"])
    namespace_json["root_dev"] = 99
    db.execute(
        "UPDATE file_claim_namespaces SET ownership_json=?", (encoded(namespace_json),)
    )
    db.commit()
    with pytest.raises((InvalidIdentity, IdentityMismatch)):
        collect(db)


def test_internal_records_reject_unhashable_scalar_types(
    ledger_db: sqlite3.Connection,
) -> None:
    module = adapter()
    namespace(ledger_db, "/library")
    ledger_db.commit()
    role = collect(ledger_db).roles[0]
    for field in ("presence", "policy"):
        with pytest.raises(InvalidIdentity):
            replace(role, **{field: []})
    with pytest.raises(InvalidIdentity):
        module.RawRow([], (1,), (("id", 1),))
    with pytest.raises(InvalidIdentity):
        module.ReferenceDependency(role.owner, OwnerRef("namespace", ("/other",)), [])


@pytest.mark.parametrize("closed", [False, True])
def test_sql_boundary_failure_is_a_typed_refusal(closed: bool) -> None:
    module = adapter()
    db = sqlite3.connect(":memory:")
    if closed:
        db.close()
    try:
        with pytest.raises((InvalidIdentity, IdentityMismatch)):
            module.snapshot_owner(db, OwnerRef("namespace", ("/library",)))
        with pytest.raises((InvalidIdentity, IdentityMismatch)):
            collect(db)
    finally:
        db.close()


@pytest.mark.parametrize("status", ["imported", "importing"])
def test_i1_exact_external_source_closes_cross_series(
    ledger_db: sqlite3.Connection, status: str
) -> None:
    db = ledger_db
    context(db)
    queue(db, source="/shared/source.cbz")
    publication(db)
    context(db, 2, 2)
    db.execute("UPDATE volumes SET import_path='/elsewhere/volume.cbz' WHERE id=2")
    queue(db, queue_id=2, series=2, source="/shared/source.cbz")
    db.execute(
        "UPDATE import_queue SET status=?,lease_owner='second-lease',lease_expires_at='2026-10-21' WHERE id=2",
        (status,),
    )
    db.execute(
        "UPDATE import_queue_files SET dst_path='/elsewhere/final.cbz' WHERE queue_id=2"
    )
    db.commit()
    before = all_rows(db)
    result = collect(db, "/library")
    assert OwnerRef("queue_source", (2,)) in {s.owner for s in result.snapshots}
    assert {r.key for r in result.raw_rows if r.table == "series"} == {(1,), (2,)}
    assert any(
        dict(r.values).get("lease_owner") == "second-lease"
        for r in result.raw_rows
        if r.table == "import_queue"
    )
    assert all_rows(db) == before


def test_i1_incidental_source_ancestor_does_not_close_owners(
    ledger_db: sqlite3.Connection,
) -> None:
    db = ledger_db
    context(db)
    queue(db, source="/shared/source.cbz")
    publication(db)
    context(db, 2, 2)
    db.execute("UPDATE volumes SET import_path='/elsewhere/volume.cbz' WHERE id=2")
    queue(db, queue_id=2, series=2, source="/shared/different.cbz")
    db.execute(
        "UPDATE import_queue_files SET dst_path='/elsewhere/final.cbz' WHERE queue_id=2"
    )
    db.commit()
    before = all_rows(db)
    assert OwnerRef("queue_source", (2,)) not in {
        s.owner for s in collect(db, "/library").snapshots
    }
    assert all_rows(db) == before


@pytest.mark.parametrize(
    "mismatch", ["none", "token", "inventory", "placement", "missing"]
)
def test_i2_active_origin_requires_current_matching_generation(
    ledger_db: sqlite3.Connection, mismatch: str
) -> None:
    db = ledger_db
    context(db)
    queue(db, source=PACK + "/source.cbz")
    publication(db)
    admitted = pack(db, tombstone=True)
    path = PACK
    if mismatch == "placement":
        rec = carrier(
            db,
            PACK + ".owner-" + TOKEN,
            ClaimBinding("pack", TOKEN, None, "pack_cleanup"),
            token="1" * 32,
            fingerprint=None,
        )
        admitted = replace(
            admitted,
            placement_carrier=rec,
            canonical_directory=DirectoryProof(
                rec.carrier_path + "/artifact", 1, 2, FP
            ),
        )
        path = rec.carrier_path + "/artifact"
        db.execute("UPDATE import_queue SET src_dir=?", (path,))
        db.execute("UPDATE import_queue_files SET src_path=?", (path + "/source.cbz",))
        db.execute(
            "UPDATE import_publication_files SET src_path=?", (path + "/source.cbz",)
        )
        q = json.loads(rows(db, "import_publications")[0]["queue_snapshot_json"])
        q["src_dir"] = path
        db.execute(
            "UPDATE import_publications SET queue_src_dir=?,queue_snapshot_json=?",
            (path, encoded(q)),
        )
    origin(db, admitted)
    if mismatch == "placement":
        q = json.loads(rows(db, "import_publications")[0]["queue_snapshot_json"])
        q["_pack_source_origins"]["files"]["1"]["src_path"] = path + "/source.cbz"
        db.execute(
            "UPDATE import_publications SET queue_snapshot_json=?", (encoded(q),)
        )
    current = admitted
    if mismatch == "token":
        current = replace(admitted, artifact_owner_token="f" * 32)
    elif mismatch == "inventory":
        current = replace(
            admitted, inventory=PackInventory({"source.cbz": replace(FP, size=99)}, ())
        )
    elif mismatch == "placement":
        assert admitted.placement_carrier is not None
        current = replace(
            admitted,
            placement_carrier=replace(
                admitted.placement_carrier, marker_fingerprint=replace(FP, inode=99)
            ),
        )
    if mismatch == "missing":
        db.execute("DELETE FROM import_pack_cleanup_reservations")
    else:
        db.execute(
            "UPDATE import_pack_cleanup_reservations SET directory_ownership_json=?",
            (current.to_json(),),
        )
    db.execute("UPDATE import_publications SET state='publishing'")
    db.execute("UPDATE import_queue SET status='importing'")
    db.commit()
    before = all_rows(db)
    if mismatch == "none":
        assert collect(db).snapshots
    else:
        with pytest.raises((InvalidIdentity, IdentityMismatch)):
            collect(db)
    assert all_rows(db) == before


@pytest.mark.parametrize("phase", ["allocated", "claiming", "claimed"])
@pytest.mark.parametrize("marked", [False, True])
def test_i3_claims_only_cleaning_matches_capture_producer(
    ledger_db: sqlite3.Connection, phase: str, marked: bool
) -> None:
    db = ledger_db
    context(db)
    queue(db)
    publication(db)
    original = pack(db, version=1)
    assert original.canonical_directory is not None and original.inventory is not None
    rec = carrier(
        db, PACK, ClaimBinding("pack", TOKEN, None, "pack_cleanup"), fingerprint=None
    )
    rec = replace(rec, phase=phase)
    directory = replace(
        original.canonical_directory, marker_fingerprint=FP if marked else None
    )
    claim = PackDirectoryClaim(
        rec, directory, original.inventory, TOKEN if marked else None
    )
    cleaning = PackOwnership(KEY, TOKEN, phase="cleaning", claims=(claim,))
    assert PackOwnership.from_json(cleaning.to_json()) == cleaning
    db.execute(
        "UPDATE import_pack_cleanup_reservations SET owner_token=?,purpose='cleanup',directory_ownership_json=?,tombstone_path=?",
        (TOKEN, cleaning.to_json(), rec.carrier_path + "/artifact"),
    )
    db.commit()
    before = all_rows(db)
    result = collect(db)
    assert any(
        r.path == rec.carrier_path + "/artifact/source.cbz" for r in result.roles
    )
    assert any(r.legacy_json == cleaning.to_json() for r in result.roles)
    assert all_rows(db) == before


@pytest.mark.parametrize("small_budget", [True, False])
def test_i5_role_budget_precedes_materialization_and_parses_are_reused(
    ledger_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, small_budget: bool
) -> None:
    module = adapter()
    db = ledger_db
    proof = PackOwnership(
        KEY,
        TOKEN,
        phase="attached",
        canonical_directory=DirectoryProof(PACK, 1, 2, FP),
        inventory=PackInventory({f"{i}.cbz": FP for i in range(24)}, ()),
        version=2,
    )
    legacy = proof.to_json()
    insert(
        db,
        "import_pack_cleanup_reservations",
        download_identity_key=KEY,
        download_client_id=1,
        protocol="torrent",
        normalized_download_id="abc",
        download_id="abc",
        purpose="queueing",
        owner_token="d" * 32,
        pack_path=PACK,
        directory_ownership_json=legacy,
        expires_at="2026-10-21 00:00:00",
    )
    db.commit()
    before = all_rows(db)
    real_load, real_add = module._load, module._Roles.add
    parses = 0
    materialized = 0

    def load(value: object) -> object:
        nonlocal parses
        if value == legacy:
            parses += 1
        return real_load(value)

    def add(self: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal materialized
        count = len(self.values)
        try:
            return real_add(self, *args, **kwargs)
        finally:
            materialized += len(self.values) - count

    monkeypatch.setattr(module, "_load", load)
    monkeypatch.setattr(module._Roles, "add", add)
    if small_budget:
        with pytest.raises(InvalidIdentity, match="budget"):
            collect(db, max_objects=1)
        assert materialized <= 1
    else:
        result = collect(db)
        assert len(result.roles) >= 24
        assert materialized == len(result.roles)
    assert parses == 1
    assert all_rows(db) == before


def test_i5_direct_role_constructor_remains_strict_after_collection(
    ledger_db: sqlite3.Connection,
) -> None:
    module = adapter()
    namespace(ledger_db, "/library")
    ledger_db.commit()
    role = collect(ledger_db).roles[0]
    for malformed in ('{"version":1,"version":1}', '{"x":NaN}', "{"):
        with pytest.raises(InvalidIdentity):
            module.RoleSpec(
                role.role,
                role.path,
                role.parent_role,
                malformed,
                role.presence,
                role.policy,
                role.owner,
            )
    for field, value in (
        ("owner", None),
        ("path", "/../escape"),
        ("policy", "file"),
        ("presence", "absent"),
    ):
        with pytest.raises(InvalidIdentity):
            replace(role, **{field: value})


@pytest.mark.parametrize("proof_kind", ["null", "attached", "claim", "placement"])
@pytest.mark.parametrize("matching", [True, False])
def test_i4_reservation_tombstone_has_phase_specific_provenance(
    ledger_db: sqlite3.Connection, proof_kind: str, matching: bool
) -> None:
    db = ledger_db
    context(db)
    queue(db)
    publication(db)
    ownership = pack(db, version=2)
    purpose = "cleanup"
    tombstone = PACK + ".cleanup-" + "d" * 32
    legacy: str | None = ownership.to_json()
    if proof_kind == "null":
        legacy = None
    elif proof_kind == "claim":
        rec = carrier(
            db,
            PACK,
            ClaimBinding("pack", TOKEN, None, "pack_cleanup"),
            fingerprint=None,
        )
        assert (
            ownership.canonical_directory is not None
            and ownership.inventory is not None
        )
        claim = PackDirectoryClaim(
            rec, ownership.canonical_directory, ownership.inventory, TOKEN
        )
        ownership = PackOwnership(KEY, TOKEN, phase="cleaning", claims=(claim,))
        legacy = ownership.to_json()
        tombstone = rec.carrier_path + "/artifact"
    elif proof_kind == "placement":
        rec = carrier(
            db,
            PACK + ".owner-" + TOKEN,
            ClaimBinding("pack", TOKEN, None, "pack_cleanup"),
            fingerprint=None,
        )
        directory = DirectoryProof(rec.carrier_path + "/artifact", 1, 2, FP)
        ownership = replace(
            ownership, canonical_directory=directory, placement_carrier=rec
        )
        legacy = ownership.to_json()
        tombstone = directory.path
        purpose = "queueing"
    if not matching:
        tombstone = "/unrelated/arbitrary-directory"
    db.execute(
        "UPDATE import_pack_cleanup_reservations SET purpose=?,directory_ownership_json=?,tombstone_path=?",
        (purpose, legacy, tombstone),
    )
    db.commit()
    before = all_rows(db)
    if matching:
        result = collect(db)
        spec = next(r for r in result.roles if r.role.purpose == "tombstone_path")
        assert spec.path == tombstone and spec.presence == "intent"
    else:
        with pytest.raises((InvalidIdentity, IdentityMismatch)):
            collect(db)
    assert all_rows(db) == before


def test_i4_queueing_row_cannot_invent_a_legacy_cleanup_intent(
    ledger_db: sqlite3.Connection,
) -> None:
    db = ledger_db
    context(db)
    queue(db)
    publication(db)
    pack(db)
    db.execute(
        "UPDATE import_pack_cleanup_reservations SET tombstone_path=?",
        (PACK + ".cleanup-" + "d" * 32,),
    )
    db.commit()
    with pytest.raises((InvalidIdentity, IdentityMismatch)):
        collect(db)


@pytest.mark.parametrize("shape", ["ownership", "claim", "origin", "cleanup_intent"])
def test_legacy_pack_token_urlsafe_producer_shape(
    ledger_db: sqlite3.Connection, shape: str
) -> None:
    db = ledger_db
    legacy_token = "A" * 30 + "-_"
    context(db)
    queue(db, source=PACK + "/source.cbz")
    publication(db)
    ownership = replace(pack(db), artifact_owner_token=legacy_token)
    if shape == "claim":
        assert (
            ownership.canonical_directory is not None
            and ownership.inventory is not None
        )
        rec = carrier(
            db,
            PACK,
            ClaimBinding("pack", legacy_token, None, "pack_cleanup"),
            fingerprint=None,
        )
        claim = PackDirectoryClaim(
            rec, ownership.canonical_directory, ownership.inventory, legacy_token
        )
        ownership = PackOwnership(KEY, legacy_token, phase="cleaning", claims=(claim,))
        db.execute(
            "UPDATE import_pack_cleanup_reservations SET purpose='cleanup',tombstone_path=?",
            (rec.carrier_path + "/artifact",),
        )
    elif shape == "origin":
        origin(db, ownership)
        q = json.loads(rows(db, "import_publications")[0]["queue_snapshot_json"])
        q["_pack_source_origins"]["files"]["1"]["artifact_owner_token"] = legacy_token
        db.execute(
            "UPDATE import_publications SET queue_snapshot_json=?", (encoded(q),)
        )
    db.execute(
        "UPDATE import_pack_cleanup_reservations SET directory_ownership_json=?",
        (ownership.to_json(),),
    )
    if shape == "cleanup_intent":
        db.execute(
            "UPDATE import_pack_cleanup_reservations SET purpose='cleanup',owner_token=?,directory_ownership_json=NULL,tombstone_path=?",
            (legacy_token, PACK + ".cleanup-" + legacy_token),
        )
    assert PackOwnership.from_json(ownership.to_json()) == ownership
    db.commit()
    before = all_rows(db)
    assert collect(db).roles
    assert all_rows(db) == before


@pytest.mark.parametrize(
    "label", ["legacy-owner", "original opaque owner", "older.label"]
)
def test_legacy_pack_labels_are_opaque_not_new_identity_ids(
    ledger_db: sqlite3.Connection, label: str
) -> None:
    db = ledger_db
    context(db)
    queue(db)
    publication(db)
    ownership = replace(pack(db), artifact_owner_token=label)
    db.execute(
        "UPDATE import_pack_cleanup_reservations SET directory_ownership_json=?",
        (ownership.to_json(),),
    )
    db.commit()
    before = all_rows(db)
    assert collect(db).roles
    assert all_rows(db) == before


@pytest.mark.parametrize(
    "label", ["", ".", "..", "../outside", "x/y", "x\\y", "x\x00y"]
)
def test_legacy_pack_label_cannot_substitute_paths(
    ledger_db: sqlite3.Connection, label: str
) -> None:
    db = ledger_db
    context(db)
    queue(db)
    publication(db)
    ownership = replace(pack(db), artifact_owner_token=label)
    db.execute(
        "UPDATE import_pack_cleanup_reservations SET directory_ownership_json=?",
        (ownership.to_json(),),
    )
    db.commit()
    before = all_rows(db)
    with pytest.raises((InvalidIdentity, IdentityMismatch)):
        collect(db)
    assert all_rows(db) == before


def test_legacy_pack_token_does_not_relax_true_carrier_hex(
    ledger_db: sqlite3.Connection,
) -> None:
    db = ledger_db
    context(db)
    queue(db)
    publication(db)
    ownership = pack(db)
    rec = carrier(
        db, PACK, ClaimBinding("pack", TOKEN, None, "pack_cleanup"), fingerprint=None
    )
    assert ownership.canonical_directory is not None and ownership.inventory is not None
    claim = PackDirectoryClaim(
        replace(rec, owner_token="A" * 30 + "-_"),
        ownership.canonical_directory,
        ownership.inventory,
        TOKEN,
    )
    changed = PackOwnership(KEY, TOKEN, phase="cleaning", claims=(claim,))
    db.execute(
        "UPDATE import_pack_cleanup_reservations SET purpose='cleanup',directory_ownership_json=?,tombstone_path=?",
        (changed.to_json(), rec.carrier_path + "/artifact"),
    )
    db.commit()
    before = all_rows(db)
    with pytest.raises((InvalidIdentity, IdentityMismatch)):
        collect(db)
    assert all_rows(db) == before


@pytest.mark.parametrize("ownership_present", [False, True])
def test_legacy_cleanup_label_can_outlive_rotated_lease_owner(
    ledger_db: sqlite3.Connection, ownership_present: bool
) -> None:
    db = ledger_db
    context(db)
    queue(db)
    publication(db)
    ownership = pack(db)
    tombstone = PACK + ".cleanup-older.opaque-label"
    db.execute(
        "UPDATE import_pack_cleanup_reservations SET purpose='cleanup',owner_token=?,directory_ownership_json=?,tombstone_path=?",
        (
            "replacement-lease-owner",
            ownership.to_json() if ownership_present else None,
            tombstone,
        ),
    )
    db.commit()
    before = all_rows(db)
    assert any(r.path == tombstone for r in collect(db).roles)
    assert all_rows(db) == before


def private_creation_row(
    db: sqlite3.Connection, label: str, ownership: PackOwnership | None
) -> None:
    insert(
        db,
        "import_pack_cleanup_reservations",
        download_identity_key=KEY,
        download_client_id=1,
        protocol="torrent",
        normalized_download_id="abc",
        download_id="abc",
        purpose="queueing",
        owner_token="rotated-lease-not-artifact-label",
        pack_path=PACK,
        tombstone_path=PACK + ".owner-" + label,
        directory_ownership_json=ownership.to_json() if ownership is not None else None,
        expires_at="2030-01-01 00:00:00",
    )


@pytest.mark.parametrize("label", [TOKEN, "A" * 30 + "-_", "historical opaque.label"])
@pytest.mark.parametrize("checkpoint", ["null", "creating", "placement", "private"])
def test_i4_private_creation_producer_checkpoints(
    ledger_db: sqlite3.Connection, label: str, checkpoint: str
) -> None:
    db = ledger_db
    proof = None
    if checkpoint != "null":
        proof = PackOwnership(KEY, label, retained_reason="creating-private")
    if checkpoint in {"placement", "private"}:
        rec = carrier(
            db,
            PACK + ".owner-" + label,
            ClaimBinding("pack", label, None, "pack_cleanup"),
            fingerprint=None,
        )
        assert proof is not None
        proof = replace(proof, version=2, placement_carrier=rec)
        if checkpoint == "private":
            proof = replace(
                proof,
                private_directory=DirectoryProof(
                    rec.carrier_path + "/artifact", 1, 2, FP
                ),
                placement_carrier=replace(rec, phase="claimed"),
                retained_reason=None,
            )
    private_creation_row(db, label, proof)
    db.commit()
    before = all_rows(db)
    result = collect(db)
    spec = next(r for r in result.roles if r.role.purpose == "tombstone_path")
    assert spec.path == PACK + ".owner-" + label and spec.presence == "intent"
    assert all_rows(db) == before and not db.in_transaction


@pytest.mark.parametrize("checkpoint", ["null", "creating"])
@pytest.mark.parametrize(
    "bad_path",
    [
        "/other/" + PACK.rsplit("/", 1)[1] + ".owner-old",
        PACK + "-other.owner-old",
        PACK + ".owner-",
        PACK + ".owner-.",
        PACK + ".owner-..",
        PACK + ".owner-old/child",
        PACK + ".owner-old/../elsewhere",
        PACK + ".owner-old\\child",
        PACK + ".owner-old\x00label",
    ],
)
def test_i4_private_creation_path_substitution_refused(
    ledger_db: sqlite3.Connection, checkpoint: str, bad_path: str
) -> None:
    db = ledger_db
    proof = (
        PackOwnership(KEY, "old", retained_reason="creating-private")
        if checkpoint == "creating"
        else None
    )
    private_creation_row(db, "old", proof)
    db.execute(
        "UPDATE import_pack_cleanup_reservations SET tombstone_path=?", (bad_path,)
    )
    db.commit()
    before = all_rows(db)
    with pytest.raises((InvalidIdentity, IdentityMismatch)):
        collect(db, "/unrelated")
    assert all_rows(db) == before and not db.in_transaction


@pytest.mark.parametrize(
    "mismatch", ["label", "private", "placement", "phase", "identity"]
)
def test_i4_private_creation_proof_must_bind_exact_intent(
    ledger_db: sqlite3.Connection, mismatch: str
) -> None:
    db = ledger_db
    proof = PackOwnership(KEY, "old", retained_reason="creating-private")
    if mismatch == "label":
        proof = replace(proof, artifact_owner_token="different-original-label")
    elif mismatch == "private":
        proof = replace(
            proof, private_directory=DirectoryProof(PACK + ".owner-other", 1, 2, FP)
        )
    elif mismatch == "placement":
        rec = carrier(
            db,
            PACK + ".owner-other",
            ClaimBinding("pack", "old", None, "pack_cleanup"),
            fingerprint=None,
        )
        proof = replace(proof, version=2, placement_carrier=rec)
    elif mismatch == "phase":
        proof = replace(
            proof,
            phase="attached",
            canonical_directory=DirectoryProof(PACK, 1, 2, FP),
            inventory=PackInventory({}, ()),
        )
    else:
        proof = replace(proof, identity_key="different-identity")
    private_creation_row(db, "old", proof)
    db.commit()
    before = all_rows(db)
    with pytest.raises((InvalidIdentity, IdentityMismatch)):
        collect(db, "/unrelated")
    assert all_rows(db) == before and not db.in_transaction


@pytest.mark.parametrize(
    "phase", ["building", "attaching", "ready", "queued", "retained"]
)
def test_i4_private_creation_intent_survives_attachment_cas(
    ledger_db: sqlite3.Connection, phase: str
) -> None:
    db = ledger_db
    label = "historical-original-label"
    private = DirectoryProof(PACK + ".owner-" + label, 1, 2, FP)
    proof = PackOwnership(KEY, label, private_directory=private)
    if phase != "building":
        proof = replace(
            proof,
            phase=phase,
            canonical_directory=replace(private, path=PACK),
            inventory=PackInventory({"source.cbz": FP}, ()),
            private_directory=private if phase == "attaching" else None,
        )
    if phase in {"queued", "retained"}:
        context(db)
        queue(db)
        proof = replace(
            proof,
            version=3,
            queue_receipt_sha256=hashlib.sha256(
                json.dumps(
                    {
                        "queue": rows(db, "import_queue")[0],
                        "files": rows(db, "import_queue_files"),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode()
            ).hexdigest(),
            retained_reason="queue validation failed" if phase == "retained" else None,
        )
    private_creation_row(db, label, proof)
    db.execute("UPDATE import_pack_cleanup_reservations SET purpose='cleanup'")
    if phase == "queued":
        db.execute("UPDATE import_pack_cleanup_reservations SET queue_id=1")
    db.commit()
    before = all_rows(db)
    assert any(r.path == private.path for r in collect(db).roles)
    assert all_rows(db) == before and not db.in_transaction
