"""PR487 adapter regressions, using real schema, producers and SQL purge."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from file_identity import IdentityMismatch, InvalidIdentity, OwnerRef
from file_identity_consumers import (
    ReferenceClosure,
    collect_references,
    InventoryBudget,
)
from import_plan import _FilePlan, _ImportPlan
from private_file_claim import ClaimBinding, LinkReceipt
from test_file_identity_inventory import (
    FP,
    PACK,
    TOKEN,
    all_rows,
    carrier,
    context,
    deletion,
    encoded,
    insert,
    origin,
    pack,
    producer_admission,
    publication,
    queue,
    rescan,
    rows,
)
from test_file_identity_schema import ledger_db as ledger_db, ledger_path as ledger_path


def inventory(db: sqlite3.Connection, parent: str = "/") -> ReferenceClosure:
    before = all_rows(db)
    try:
        return collect_references(db, parent, InventoryBudget())
    finally:
        assert all_rows(db) == before
        assert not db.in_transaction


def purge(db: sqlite3.Connection, series_id: int = 1) -> None:
    from routers.series_ import _prepare_hard_delete_series

    db.execute(
        "UPDATE series SET deleted_at='2026-10-20T00:00:00' WHERE id=?", (series_id,)
    )
    db.commit()
    db.row_factory = sqlite3.Row
    try:
        assert _prepare_hard_delete_series(db, series_id)["status"] == "purged"
        db.commit()
    finally:
        db.row_factory = None


@pytest.mark.parametrize(
    "kind,state",
    [
        ("publication", "finalized"),
        ("publication", "deleted"),
        ("rescan", "rolled_back"),
        ("rescan", "completed"),
        ("deletion", "completed"),
    ],
)
def test_terminal_journal_survives_actual_hard_purge(
    ledger_db: sqlite3.Connection,
    kind: str,
    state: str,
) -> None:
    db = ledger_db
    context(db)
    if kind == "publication":
        queue(db)
        publication(db)
    else:
        {"rescan": rescan, "deletion": deletion}[kind](db)
    table = {
        "publication": "import_publications",
        "rescan": "rescan_file_operations",
        "deletion": "volume_file_deletions",
    }[kind]
    db.execute(f"UPDATE {table} SET state=?", (state,))
    db.commit()
    old = inventory(db)
    journal = next(s for s in old.snapshots if s.owner.domain == kind)
    purge(db)
    result = inventory(db)
    current = next(s for s in result.snapshots if s.owner == journal.owner)
    assert (
        json.loads(current.rows_json)["root"] == json.loads(journal.rows_json)["root"]
    )
    assert not any(s.owner.domain == "queue_source" for s in result.snapshots)
    assert not any(r.table in {"series", "volumes"} for r in result.raw_rows)
    assert all(e.requires.domain != "queue_source" for e in result.dependencies)


@pytest.mark.parametrize("missing_queue", [False, True])
@pytest.mark.parametrize("version", [1, 2, 3])
def test_terminal_pack_origin_is_historical_with_or_without_live_terminal_queue(
    ledger_db: sqlite3.Connection,
    missing_queue: bool,
    version: int,
) -> None:
    db = ledger_db
    context(db)
    queue(db, source=PACK + "/source.cbz")
    publication(db)
    admitted = pack(db, version=version, tombstone=True)
    origin(db, admitted)
    db.execute(
        "UPDATE import_pack_cleanup_reservations SET directory_ownership_json=?",
        (replace(admitted, phase="retained", retained_reason="terminal").to_json(),),
    )
    db.commit()
    if missing_queue:
        purge(db)
    result = inventory(db)
    assert any(r.legacy_json == admitted.to_json() for r in result.roles)
    assert (
        OwnerRef("queue_source", (1,)) in {s.owner for s in result.snapshots}
    ) != missing_queue


@pytest.mark.parametrize(
    "state",
    ["staging", "prepared", "publishing", "published", "db_committed", "cleaning"],
)
def test_active_publication_cannot_use_terminal_missing_queue_exception(
    ledger_db: sqlite3.Connection,
    state: str,
) -> None:
    context(ledger_db)
    queue(ledger_db)
    publication(ledger_db)
    ledger_db.commit()
    purge(ledger_db)
    ledger_db.execute("UPDATE import_publications SET state=?", (state,))
    ledger_db.commit()
    with pytest.raises((InvalidIdentity, IdentityMismatch)):
        inventory(ledger_db)


@pytest.mark.parametrize(
    "domain,state",
    [
        ("rescan", "prepared"),
        ("rescan", "published"),
        ("rescan", "db_committed"),
        ("rescan", "rollback"),
        ("deletion", "active"),
    ],
)
@pytest.mark.parametrize("missing", ["series", "volume"])
def test_active_journal_requires_live_context(
    ledger_db: sqlite3.Connection,
    domain: str,
    state: str,
    missing: str,
) -> None:
    db = ledger_db
    context(db)
    {"rescan": rescan, "deletion": deletion}[domain](db)
    db.commit()
    if missing == "series":
        purge(db)
    else:
        db.execute("DELETE FROM volumes")
    table = "rescan_file_operations" if domain == "rescan" else "volume_file_deletions"
    db.execute(f"UPDATE {table} SET state=?", (state,))
    db.commit()
    with pytest.raises((InvalidIdentity, IdentityMismatch)):
        inventory(db)


def quarantine(db: sqlite3.Connection, state: str) -> None:
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
        state=state,
        original_path="/quarantine/original.cbz",
        volume_path="/other/volume.cbz",
        file_inode="2",
        size_bytes=3,
        mtime_ns=4,
        sha256=FP.sha256,
        chapter_guard_json="{}",
    )


@pytest.mark.parametrize("state", ["quarantined", "rolled_back", "restored"])
def test_inactive_quarantine_outlives_actual_purge_as_evidence_only(
    ledger_db: sqlite3.Connection,
    state: str,
) -> None:
    db = ledger_db
    context(db)
    quarantine(db, state)
    db.commit()
    original = rows(db, "chapter_quarantine")
    purge(db)
    result = inventory(db, "/quarantine")
    assert not result.roles and not result.snapshots
    assert {r.table for r in result.raw_rows} == {"chapter_quarantine"}
    assert rows(db, "chapter_quarantine") == original


@pytest.mark.parametrize(
    "state", ["reserved", "captured", "committed", "restoring", "unknown"]
)
@pytest.mark.parametrize("missing", ["series", "volume"])
def test_active_or_unknown_quarantine_cannot_lose_context(
    ledger_db: sqlite3.Connection,
    state: str,
    missing: str,
) -> None:
    db = ledger_db
    context(db)
    quarantine(db, "quarantined")
    db.commit()
    if missing == "series":
        purge(db)
    else:
        db.execute("DELETE FROM volumes")
    db.execute("PRAGMA ignore_check_constraints=ON")
    db.execute("UPDATE chapter_quarantine SET state=?", (state,))
    db.commit()
    with pytest.raises((InvalidIdentity, IdentityMismatch)):
        inventory(db, "/quarantine")


def test_terminal_cross_series_closure_does_not_hide_active_missing_context(
    ledger_db: sqlite3.Connection,
) -> None:
    db = ledger_db
    context(db)
    context(db, 2, 2)
    queue(db, series=2)
    publication(db, series=1)
    rescan(db)
    db.commit()
    purge(db, 1)
    assert inventory(db).snapshots
    db.execute("UPDATE rescan_file_operations SET state='prepared'")
    db.commit()
    with pytest.raises((InvalidIdentity, IdentityMismatch)):
        inventory(db)


@pytest.mark.parametrize(
    "mutation",
    [
        "none",
        "omit",
        "unknown",
        "nonready",
        "legacy",
        "skip",
        "needs_review",
        "pre_failed",
    ],
)
def test_nonnull_producer_admission_has_exact_ready_file_keys(
    ledger_db: sqlite3.Connection,
    mutation: str,
) -> None:
    db = ledger_db
    context(db)
    queue(db)
    publication(db)
    frozen = json.loads(rows(db, "import_publications")[0]["queue_snapshot_json"])
    admission = producer_admission(db, frozen)
    assert set(admission["files"]) == {"1"}
    if mutation == "omit":
        admission["files"].clear()
    elif mutation == "unknown":
        admission["files"]["999"] = admission["files"].pop("1")
    elif mutation == "nonready":
        db.execute("UPDATE import_publication_files SET plan_status='skip'")
    elif mutation in {"skip", "needs_review", "pre_failed"}:
        db.execute("UPDATE import_publication_files SET plan_status=?", (mutation,))
        admission["files"].clear()
    frozen["_file_publication"]["admission"] = (
        None if mutation == "legacy" else admission
    )
    db.execute(
        "UPDATE import_publications SET queue_snapshot_json=?", (encoded(frozen),)
    )
    db.commit()
    if mutation in {"omit", "unknown", "nonready"}:
        with pytest.raises((InvalidIdentity, IdentityMismatch)):
            inventory(db)
    else:
        assert inventory(db).roles


MIRRORS = (
    "status",
    "download_id",
    "download_client_id",
    "torrent_name",
    "torrent_url",
    "volume_num",
    "src_dir",
    "failed_at",
    "lease_owner",
    "lease_expires_at",
    "created_at",
)


@pytest.mark.parametrize("field", MIRRORS)
@pytest.mark.parametrize("side", ["frozen", "mirror"])
def test_all_fixed_queue_mirrors_match_frozen_not_live_history(
    ledger_db: sqlite3.Connection,
    field: str,
    side: str,
) -> None:
    db = ledger_db
    context(db)
    queue(db)
    publication(db)
    frozen = json.loads(rows(db, "import_publications")[0]["queue_snapshot_json"])
    value = (
        999
        if field in {"download_client_id", "volume_num"}
        else "/changed"
        if field == "src_dir"
        else "changed"
    )
    if side == "frozen":
        frozen[field] = value
        db.execute(
            "UPDATE import_publications SET queue_snapshot_json=?", (encoded(frozen),)
        )
    else:
        db.execute(f"UPDATE import_publications SET queue_{field}=?", (value,))
    db.commit()
    with pytest.raises((InvalidIdentity, IdentityMismatch)):
        inventory(db)


@pytest.mark.parametrize("mismatch", ["none", "receipt", "version"])
def test_actual_pending_claim_preserves_historical_v3_receipt_but_binds_generation(
    ledger_db: sqlite3.Connection,
    mismatch: str,
) -> None:
    from import_lease import claim_import_queue_row
    from import_pack_cleanup import _queue_receipt

    db = ledger_db
    context(db)
    queue(db, source=PACK + "/source.cbz")
    db.execute("UPDATE import_queue SET status='pending'")
    admitted = pack(db)
    assert claim_import_queue_row(db, 1, TOKEN)
    publication(db)
    origin(db, admitted)
    db.execute("UPDATE import_publications SET state='publishing'")
    db.row_factory = sqlite3.Row
    try:
        assert _queue_receipt(db, 1) != admitted.queue_receipt_sha256
    finally:
        db.row_factory = None
    current = (
        replace(admitted, queue_receipt_sha256="e" * 64)
        if mismatch == "receipt"
        else admitted
    )
    if mismatch == "version":
        current = replace(admitted, version=2, queue_receipt_sha256=None)
    db.execute(
        "UPDATE import_pack_cleanup_reservations SET directory_ownership_json=?",
        (current.to_json(),),
    )
    db.commit()
    if mismatch == "none":
        assert inventory(db).roles
    else:
        with pytest.raises((InvalidIdentity, IdentityMismatch)):
            inventory(db)


def producer_plan(db: sqlite3.Connection, destination: Path) -> _ImportPlan:
    file = _FilePlan(
        1,
        "/incoming/source.cbz",
        "source.cbz",
        str(destination / "final.cbz"),
        "volume",
        "cbz",
        1,
        None,
        None,
        None,
        None,
        None,
        0,
        None,
        False,
        False,
        False,
        "ready",
        "",
    )
    return _ImportPlan(
        rows(db, "import_queue")[0],
        rows(db, "series")[0],
        [],
        str(destination),
        "copy",
        None,
        [file],
        1,
    )


@pytest.mark.parametrize(
    "checkpoint",
    ["created", "allocated", "claiming", "ready", "discarded_without_ready"],
)
def test_actual_publication_producer_preparation_checkpoints(
    ledger_db: sqlite3.Connection,
    ledger_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checkpoint: str,
) -> None:
    import import_publication as producer
    from file_mutation_lock import file_mutation_guard
    from import_lease import claim_import_queue_row
    from import_staging import _StageOutcome

    db = ledger_db
    context(db)
    queue(db)
    db.execute("UPDATE import_queue SET status='pending'")
    assert claim_import_queue_row(db, 1, TOKEN)
    plan = producer_plan(db, tmp_path)
    stage = tmp_path / (
        ".mangarr-publication-1-" + hashlib.sha256(TOKEN.encode()).hexdigest()[:32]
    )
    stage.mkdir()
    staged = stage / "final.cbz"
    staged.write_bytes(b"test stage")
    fingerprint = producer._regular_fingerprint(str(staged), include_hash=True)
    db.row_factory = sqlite3.Row
    try:
        pid = producer.create_publication(
            db, plan, TOKEN, str(stage), {1: producer.FileFingerprint(**asdict(FP))}
        )
    finally:
        db.row_factory = None
    db.commit()
    if checkpoint != "created":
        final = plan.files[0].dst_path
        producer.commit_prepared_barrier(
            pid,
            [_StageOutcome(1, True, final, "", str(staged))],
            (producer.PreparedArtifact(1, str(staged), final, fingerprint, None),),
        )
        assert producer._claim_publication_operation(pid, TOKEN)
    with producer.get_db() as read:
        publication_record = producer.load_publication(read, publication_id=pid)
    assert publication_record is not None
    assert publication_record.state == (
        "staging" if checkpoint == "created" else "publishing"
    )
    if checkpoint != "created":
        file = publication_record.files[0]
        with file_mutation_guard(str(ledger_path)) as guard:
            producer._allocate_private(
                publication_record,
                file,
                guard,
                TOKEN,
                "publication",
                file.outcome.final_dst,
                producer._private_full(file.staged_fingerprint),
            )
            if checkpoint == "discarded_without_ready":
                producer._discard_private(
                    publication_record, file, guard, TOKEN, "publication"
                )
            elif checkpoint != "allocated":
                if checkpoint == "claiming":

                    def interrupted(*_args: Any, **_kwargs: Any) -> None:
                        raise OSError("interrupted before private stage link")

                    monkeypatch.setattr(producer.os, "link", interrupted)
                fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    if checkpoint == "claiming":
                        with pytest.raises(OSError, match="interrupted"):
                            producer._prepare_publication_copy_pinned(
                                publication_record, file, guard, TOKEN, fd
                            )
                    else:
                        producer._prepare_publication_copy_pinned(
                            publication_record, file, guard, TOKEN, fd
                        )
                finally:
                    os.close(fd)
    result = inventory(db)
    assert any(s.owner == OwnerRef("publication", (pid,)) for s in result.snapshots)
    if checkpoint != "created":
        entry = json.loads(rows(db, "import_publications")[0]["queue_snapshot_json"])[
            "_file_publication"
        ]["artifacts"]["1"]
        record = json.loads(entry["publication"])
        assert record["artifact_fingerprint"] == asdict(fingerprint)
        assert (
            record["phase"]
            == {
                "allocated": "allocated",
                "claiming": "claiming",
                "ready": "claimed",
                "discarded_without_ready": "discarded",
            }[checkpoint]
        )
        assert ("publication_ready" in entry) == (checkpoint == "ready")


@pytest.mark.parametrize(
    "phase,ready,mutation",
    [
        ("allocated", False, "none"),
        ("claiming", False, "none"),
        ("claimed", True, "none"),
        ("claimed", False, "none"),
        ("restoring", False, "none"),
        ("claimed", True, "carrier"),
        ("claimed", True, "ready"),
        ("claimed", True, "staged"),
        ("allocated", True, "none"),
        ("claiming", True, "none"),
        ("restored", True, "receipt"),
        ("restoring", True, "none"),
        ("restored", True, "none"),
        ("discarding", True, "none"),
        ("discarded", True, "none"),
        ("discarding", False, "none"),
        ("discarded", False, "none"),
    ],
)
def test_publication_carrier_fingerprint_and_readiness_fences(
    ledger_db: sqlite3.Connection,
    phase: str,
    ready: bool,
    mutation: str,
) -> None:
    db = ledger_db
    context(db)
    queue(db)
    publication(db)
    rec = replace(
        carrier(
            db,
            "/library/final.cbz",
            ClaimBinding("import_publication", "1", 1, "publication"),
        ),
        phase=phase,
    )
    if mutation == "carrier":
        rec = replace(rec, artifact_fingerprint=replace(FP, size=99))
    if phase == "restored":
        rec = replace(
            rec,
            restore_receipt=LinkReceipt(
                "/library/final.cbz",
                replace(FP, size=99) if mutation == "receipt" else FP,
            ),
        )
    entry: dict[str, object] = {"publication": rec.to_json()}
    if rec.restore_receipt is not None:
        entry["publication_receipt"] = asdict(rec.restore_receipt)
    if ready:
        entry["publication_ready"] = asdict(
            replace(FP, size=99) if mutation == "ready" else FP
        )
    if mutation == "staged":
        db.execute("UPDATE import_publication_files SET staged_size=99")
    frozen = json.loads(rows(db, "import_publications")[0]["queue_snapshot_json"])
    frozen["_file_publication"]["artifacts"] = {"1": entry}
    db.execute(
        "UPDATE import_publications SET queue_snapshot_json=?", (encoded(frozen),)
    )
    db.commit()
    if (
        mutation != "none"
        or (
            not ready
            and phase not in {"allocated", "claiming", "discarding", "discarded"}
        )
        or (ready and phase in {"allocated", "claiming"})
    ):
        with pytest.raises((InvalidIdentity, IdentityMismatch)):
            inventory(db)
    else:
        assert any(r.role.purpose == "publication_carrier" for r in inventory(db).roles)


@pytest.mark.parametrize(
    "phase",
    [
        "allocated",
        "claiming",
        "claimed",
        "restoring",
        "restored",
        "discarding",
        "discarded",
    ],
)
@pytest.mark.parametrize(
    "acknowledgements",
    [
        "none",
        "matching",
        "carrier_only",
        "publication_only",
        "carrier_path",
        "publication_path",
        "both_path",
        "carrier_fingerprint",
        "publication_fingerprint",
        "both_fingerprint",
    ],
)
def test_publication_acknowledgement_copies_match_phase(
    ledger_db: sqlite3.Connection,
    phase: str,
    acknowledgements: str,
) -> None:
    db = ledger_db
    context(db)
    queue(db)
    publication(db)
    receipt = LinkReceipt("/library/final.cbz", FP)
    carrier_receipt = (
        receipt if acknowledgements not in {"none", "publication_only"} else None
    )
    publication_receipt = (
        receipt if acknowledgements not in {"none", "carrier_only"} else None
    )
    if acknowledgements in {"carrier_path", "both_path"}:
        carrier_receipt = replace(receipt, destination_path="/library/other.cbz")
    if acknowledgements in {"publication_path", "both_path"}:
        publication_receipt = replace(receipt, destination_path="/library/other.cbz")
    if acknowledgements in {"carrier_fingerprint", "both_fingerprint"}:
        carrier_receipt = replace(receipt, fingerprint=replace(FP, size=99))
    if acknowledgements in {"publication_fingerprint", "both_fingerprint"}:
        publication_receipt = replace(receipt, fingerprint=replace(FP, size=99))
    rec = replace(
        carrier(
            db,
            "/library/final.cbz",
            ClaimBinding("import_publication", "1", 1, "publication"),
        ),
        phase=phase,
        restore_receipt=carrier_receipt,
    )
    entry: dict[str, object] = {"publication": rec.to_json()}
    if phase not in {"allocated", "claiming"}:
        entry["publication_ready"] = asdict(FP)
    if publication_receipt is not None:
        entry["publication_receipt"] = asdict(publication_receipt)
    frozen = json.loads(rows(db, "import_publications")[0]["queue_snapshot_json"])
    frozen["_file_publication"]["artifacts"] = {"1": entry}
    db.execute(
        "UPDATE import_publications SET queue_snapshot_json=?", (encoded(frozen),)
    )
    db.commit()
    valid = (acknowledgements == "none" and phase != "restored") or (
        acknowledgements == "matching"
        and phase in {"restored", "discarding", "discarded"}
    )
    if valid:
        assert any(r.role.purpose == "publication_carrier" for r in inventory(db).roles)
    else:
        with pytest.raises((InvalidIdentity, IdentityMismatch)):
            inventory(db)


@pytest.mark.parametrize("phase", ["restored", "discarding", "discarded"])
def test_actual_publication_acknowledgements_survive_disposal(
    ledger_db: sqlite3.Connection,
    ledger_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    import import_publication as producer
    import private_file_claim as claims
    from file_mutation_lock import file_mutation_guard
    from import_lease import claim_import_queue_row
    from import_staging import _StageOutcome

    db = ledger_db
    context(db)
    source = tmp_path / "source.cbz"
    source.write_bytes(b"publication acknowledgement control")
    destination = tmp_path / "library"
    destination.mkdir()
    queue(db, source=str(source))
    db.execute("UPDATE import_queue SET status='pending'")
    assert claim_import_queue_row(db, 1, TOKEN)
    plan = producer_plan(db, destination)
    plan.files[0] = replace(plan.files[0], src_path=str(source), filename=source.name)
    db.execute(
        "UPDATE import_queue_files SET src_path=?,dst_path=?",
        (str(source), plan.files[0].dst_path),
    )
    db.commit()
    stage, _ = producer.initialize_publication_filesystem(plan, TOKEN)
    pid = rows(db, "import_publications")[0]["id"]
    staged = Path(stage) / "final.cbz"
    staged.write_bytes(source.read_bytes())
    fingerprint = producer._regular_fingerprint(str(staged), include_hash=True)
    final = plan.files[0].dst_path
    producer.commit_prepared_barrier(
        pid,
        [_StageOutcome(1, True, final, "", str(staged))],
        (producer.PreparedArtifact(1, str(staged), final, fingerprint, None),),
    )
    assert producer._claim_publication_operation(pid, TOKEN)
    with producer.get_db() as read:
        record = producer.load_publication(read, publication_id=pid)
    assert record is not None

    def unsupported(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EOPNOTSUPP, "select existing hard-link fallback")

    monkeypatch.setattr(producer, "_rename_noreplace", unsupported)
    with file_mutation_guard(str(ledger_path)) as guard:
        producer._publish_private(record, record.files[0], guard, TOKEN)
        assert inventory(db).roles
        if phase == "discarding":

            def interrupted(*_args: object, **_kwargs: object) -> None:
                raise OSError("interrupted after persisted discarding phase")

            monkeypatch.setattr(claims, "discard_private_regular", interrupted)
            with pytest.raises(OSError, match="persisted discarding"):
                producer._discard_private(
                    record, record.files[0], guard, TOKEN, "publication"
                )
        elif phase == "discarded":
            producer._discard_private(
                record, record.files[0], guard, TOKEN, "publication"
            )
    frozen = json.loads(rows(db, "import_publications")[0]["queue_snapshot_json"])
    entry = frozen["_file_publication"]["artifacts"]["1"]
    persisted = json.loads(entry["publication"])
    assert persisted["phase"] == phase
    assert (
        persisted["restore_receipt"]
        == entry["publication_receipt"]
        == asdict(LinkReceipt(final, producer._private_full(fingerprint)))
    )
    assert inventory(db).roles
    assert Path(final).read_bytes() == source.read_bytes()


@pytest.mark.parametrize("domain", ["pack_reservation", "pack_tombstone"])
@pytest.mark.parametrize(
    "mutation", ["missing_publication", "wrong_queue", "wrong_identity"]
)
def test_missing_pack_queue_requires_its_exact_terminal_publication(
    ledger_db: sqlite3.Connection,
    domain: str,
    mutation: str,
) -> None:
    db = ledger_db
    context(db)
    queue(db, source=PACK + "/source.cbz")
    publication(db)
    admitted = pack(db, tombstone=True)
    origin(db, admitted)
    db.commit()
    purge(db)
    table = (
        "import_pack_cleanup_reservations"
        if domain == "pack_reservation"
        else "import_pack_cleanup_tombstones"
    )
    if mutation == "missing_publication":
        db.execute(f"UPDATE {table} SET publication_id=NULL")
    elif mutation == "wrong_queue":
        db.execute(f"UPDATE {table} SET queue_id=999")
    else:
        frozen = json.loads(rows(db, "import_publications")[0]["queue_snapshot_json"])
        frozen["download_id"] = "different"
        db.execute(
            "UPDATE import_publications SET queue_download_id='different',queue_snapshot_json=?",
            (encoded(frozen),),
        )
    db.commit()
    with pytest.raises((InvalidIdentity, IdentityMismatch)):
        inventory(db)


@pytest.mark.parametrize("table", ["volumes", "chapters"])
def test_live_media_cannot_borrow_terminal_missing_series_exception(
    ledger_db: sqlite3.Connection,
    table: str,
) -> None:
    db = ledger_db
    context(db)
    rescan(db)
    db.commit()
    purge(db)
    db.execute("PRAGMA foreign_keys=OFF")
    if table == "volumes":
        insert(
            db, table, id=1, series_id=1, volume_num=1, import_path="/library/live.cbz"
        )
    else:
        insert(
            db, table, id=1, series_id=1, chapter_num=1, import_path="/library/live.cbz"
        )
    db.commit()
    with pytest.raises((InvalidIdentity, IdentityMismatch)):
        inventory(db)


@pytest.mark.parametrize(
    "status", ["pending", "partial", "importing", "imported", "failed", "skipped"]
)
def test_live_queue_still_requires_its_series_even_if_terminal(
    ledger_db: sqlite3.Connection,
    status: str,
) -> None:
    db = ledger_db
    context(db)
    queue(db)
    db.commit()
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("DELETE FROM volumes")
    db.execute("DELETE FROM series")
    db.execute("UPDATE import_queue SET status=?", (status,))
    db.commit()
    with pytest.raises((InvalidIdentity, IdentityMismatch)):
        inventory(db)


@pytest.mark.parametrize("status", ["skip", "needs_review", "pre_failed"])
def test_actual_producer_mixed_ready_admission_and_live_lease_refresh(
    ledger_db: sqlite3.Connection,
    tmp_path: Path,
    status: str,
) -> None:
    import import_publication as producer
    from import_lease import claim_import_queue_row, refresh_import_queue_lease

    db = ledger_db
    context(db)
    queue(db)
    db.execute("UPDATE import_queue SET status='pending'")
    assert claim_import_queue_row(db, 1, TOKEN)
    plan = producer_plan(db, tmp_path)
    other = replace(
        plan.files[0],
        file_id=2,
        plan_status=status,
        src_path="/incoming/skip.cbz",
        dst_path=str(tmp_path / "skip.cbz"),
    )
    insert(
        db,
        "import_queue_files",
        id=2,
        queue_id=1,
        filename="skip.cbz",
        src_path=other.src_path,
        dst_path=other.dst_path,
        status="pending",
    )
    plan.files.append(other)
    stage = str(
        tmp_path
        / (".mangarr-publication-1-" + hashlib.sha256(TOKEN.encode()).hexdigest()[:32])
    )
    db.row_factory = sqlite3.Row
    try:
        producer.create_publication(db, plan, TOKEN, stage, {})
    finally:
        db.row_factory = None
    assert refresh_import_queue_lease(db, 1, TOKEN, lease_seconds=900)
    db.commit()
    frozen = json.loads(rows(db, "import_publications")[0]["queue_snapshot_json"])
    assert set(frozen["_file_publication"]["admission"]["files"]) == {"1"}
    assert frozen["lease_expires_at"] != rows(db, "import_queue")[0]["lease_expires_at"]
    assert inventory(db).roles


@pytest.mark.parametrize("version", [1, 2])
def test_active_legacy_pack_generation_remains_supported(
    ledger_db: sqlite3.Connection,
    version: int,
) -> None:
    from import_lease import claim_import_queue_row

    db = ledger_db
    context(db)
    queue(db, source=PACK + "/source.cbz")
    db.execute("UPDATE import_queue SET status='pending'")
    admitted = pack(db, version=version)
    assert claim_import_queue_row(db, 1, TOKEN)
    publication(db)
    origin(db, admitted)
    db.execute("UPDATE import_publications SET state='publishing'")
    db.commit()
    assert inventory(db).roles
