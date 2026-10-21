"""Retained preparation failures must remain retryable without losing skip intent."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from download_identity import DownloadIdentity
from test_import_skip_settlement_482 import (
    SOURCE,
    TITLE,
    Pack,
    _assert_guid_dedup,
    _queue,
    _snapshot,
    pack as pack,
)


@dataclass(frozen=True)
class Case:
    pack: Pack
    queue_id: int
    file_id: int
    source: Path
    source_bytes: bytes
    original_bytes: bytes
    candidate_bytes: bytes
    raw_page: Path | None
    before: dict[str, list[dict[str, Any]]]
    seen: list[dict[str, Any]]


@pytest.fixture(autouse=True)
def private_execution(pack: Pack, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    import import_execute
    import import_pipeline

    monkeypatch.setattr(import_execute, "_IMPORT_SEM", None)
    monkeypatch.setattr(
        import_pipeline, "PACK_STAGING_ROOT", str(pack.root / "managed-packs")
    )
    real_client = httpx.AsyncClient
    requests: list[str] = []

    def reject(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        raise AssertionError("retry/skip must not contact a downloader")

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(reject), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    yield
    assert requests == []


def _rows(case: Case) -> dict[str, Any]:
    import shared

    with shared.get_db() as db:
        parent = db.execute(
            "SELECT * FROM import_queue WHERE id=?", (case.queue_id,)
        ).fetchone()
        return {
            "parent": dict(parent) if parent is not None else None,
            "children": [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM import_queue_files WHERE queue_id=? ORDER BY id",
                    (case.queue_id,),
                )
            ],
            "history": [dict(row) for row in db.execute("SELECT * FROM history")],
        }


def _case(pack: Pack, *, generated: bool, reason: str) -> Case:
    import import_queue
    import shared

    raw_page = None
    if generated:
        raw_page = pack.root / "image-download" / "Owned Pack v01" / "001.jpg"
        raw_page.parent.mkdir(parents=True)
        raw_page.write_bytes(b"generated pack source page")
        with shared.get_db() as db:
            queue_id, review = import_queue._queue_import(
                db,
                1,
                pack.download_id.lower()
                if pack.protocol == "torrent"
                else pack.download_id,
                TITLE,
                SOURCE,
                None,
                str(raw_page.parent.parent),
                download_client_id=7,
                protocol=pack.protocol,
            )
        assert queue_id is not None and review is False
    else:
        queue_id = _queue(pack)
    with shared.get_db() as db:
        children = db.execute(
            "SELECT * FROM import_queue_files WHERE queue_id=?", (queue_id,)
        ).fetchall()
        assert len(children) == 1 and children[0]["status"] == "pending"
        file_id = children[0]["id"]
        source = Path(children[0]["src_path"])
        if reason == "operator":
            # A valid writable wanted volume, so replay must preserve the skip,
            # not merely reset it and accidentally publish an eligible file.
            db.execute(
                "UPDATE import_queue_files SET proposed_volume=2 WHERE id=?",
                (file_id,),
            )
        seen = [dict(row) for row in db.execute("SELECT * FROM seen")]
        if generated:
            reservation = db.execute(
                "SELECT * FROM import_pack_cleanup_reservations WHERE queue_id=?",
                (queue_id,),
            ).fetchone()
            assert reservation is not None
            proof = json.loads(reservation["directory_ownership_json"])
            assert proof["phase"] == "attached"
            assert str(source.parent) in (
                reservation["pack_path"],
                proof["canonical_directory"]["path"],
            )
            assert proof["artifact_owner_token"]
    return Case(
        pack,
        queue_id,
        file_id,
        source,
        source.read_bytes(),
        pack.original.read_bytes(),
        pack.candidate.read_bytes(),
        raw_page,
        _snapshot(),
        seen,
    )


def _fail_preparation(case: Case, reason: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import import_execute
    import import_queue
    import shared

    def deny(path: str) -> None:
        assert path == str(case.pack.original.parent)
        raise PermissionError("issue494 guarded destination preparation denied")

    with monkeypatch.context() as fault:
        fault.setattr(import_execute, "ensure_durable_directory", deny)
        assert (
            asyncio.run(
                import_execute._guarded_execute_import(
                    case.queue_id,
                    skip_ids={case.file_id} if reason == "operator" else None,
                )
            )
            is False
        )
    state = _rows(case)
    (case.pack.root / "after-failure.json").write_text(json.dumps(state, indent=2))
    parent = state["parent"]
    assert parent is not None and parent["status"] == "failed"
    assert parent["failed_at"] and parent["lease_owner"] is None
    assert parent["lease_expires_at"] is None
    assert parent["download_client_id"] == 7
    assert parent["download_protocol"] == case.pack.protocol
    assert parent["torrent_url"] == SOURCE
    assert [row["status"] for row in state["children"]] == ["skipped"]
    assert state["children"][0]["src_path"] == str(case.source)
    assert [row["event_type"] for row in state["history"]] == ["import_failed"]
    assert _snapshot() == case.before
    with shared.get_db() as db:
        assert [dict(row) for row in db.execute("SELECT * FROM seen")] == case.seen
        assert db.execute("SELECT 1 FROM import_publications").fetchone() is None
        assert not import_queue._has_terminal_download_receipt(
            db,
            series_id=1,
            torrent_url=SOURCE,
            identity=DownloadIdentity(7, case.pack.protocol, case.pack.download_id),
        )
        assert any(
            "issue494 guarded destination preparation denied" in row[0]
            for row in db.execute("SELECT message FROM events")
        )


def _assert_settled(case: Case, *, had_failure: bool, failure_count: int = 1) -> None:
    import import_queue
    import shared

    state = _rows(case)
    assert state["parent"] is None and state["children"] == []
    assert [row["event_type"] for row in state["history"]] == (
        ["import_failed"] * failure_count + ["import_skipped"]
        if had_failure
        else ["import_skipped"]
    )
    receipt = state["history"][-1]
    assert receipt["series_id"] == 1 and receipt["torrent_url"] == SOURCE
    assert receipt["download_client_id"] == 7
    assert receipt["protocol"] == case.pack.protocol
    assert receipt["download_id"] == (
        case.pack.download_id.lower()
        if case.pack.protocol == "torrent"
        else case.pack.download_id
    )
    with shared.get_db() as db:
        assert [dict(row) for row in db.execute("SELECT * FROM seen")] == case.seen
        assert db.execute("SELECT 1 FROM import_publications").fetchone() is None
        assert (
            db.execute("SELECT 1 FROM import_pack_cleanup_tombstones").fetchone()
            is None
        )
        assert import_queue._has_terminal_download_receipt(
            db,
            series_id=1,
            torrent_url=SOURCE,
            identity=DownloadIdentity(7, case.pack.protocol, case.pack.download_id),
        )
    after = _snapshot()
    assert after["series"] == case.before["series"]
    assert [row["status"] for row in after["volumes"] if row["id"] in (2, 3)] == [
        "wanted"
    ] * 2
    assert [
        row["status"] for row in after["chapters"] if row["id"] in (20, 24, 30)
    ] == ["wanted"] * 3
    for table, owned in (("volumes", {2, 3}), ("chapters", {20, 24, 30})):
        assert [row for row in after[table] if row["id"] not in owned] == [
            row for row in case.before[table] if row["id"] not in owned
        ]
    assert case.pack.original.read_bytes() == case.original_bytes
    if case.raw_page is not None:
        assert case.raw_page.read_bytes() == b"generated pack source page"
        assert not case.source.exists(), "settled managed pack should be cleaned"
    else:
        assert case.source.read_bytes() == case.source_bytes
    _assert_guid_dedup(case.pack)


def _retry(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    *,
    had_failure: bool = True,
    failure_count: int = 1,
) -> None:
    import import_execute
    import main
    from routers.import_ import retry_import_queue_entry

    # Hold only background scheduling; execute the real scheduled worker below.
    monkeypatch.setattr(main, "schedule_import_worker", lambda _queue_id: None)
    result = retry_import_queue_entry(case.queue_id)
    assert result["ok"] is True and result["status"] == "pending"
    retry_result = asyncio.run(import_execute._guarded_execute_import(case.queue_id))
    state = _rows(case)
    (case.pack.root / "after-retry.json").write_text(
        json.dumps(
            {
                "retry_action": result,
                "worker_result": retry_result,
                **state,
            },
            indent=2,
        )
    )
    assert retry_result is True, f"repaired Retry stranded retained decisions: {state}"
    _assert_settled(case, had_failure=had_failure, failure_count=failure_count)


@pytest.mark.parametrize("reason", ["quality", "operator"])
def test_repaired_retry_replays_retained_skip_without_fresh_skip_ids(
    pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    case = _case(pack, generated=False, reason=reason)
    _fail_preparation(case, reason, monkeypatch)
    assert case.source.read_bytes() == case.source_bytes
    _retry(case, monkeypatch)


def _retained_managed_case(pack: Pack, monkeypatch: pytest.MonkeyPatch) -> Case:
    """Stop after the real leased executor, before the guarded cleanup caller."""
    import import_execute
    import import_lease
    import shared

    case = _case(pack, generated=True, reason="quality")
    with shared.get_db() as db:
        assert import_lease.claim_import_queue_row(db, case.queue_id, "owned-worker")

    def deny(_path: str) -> None:
        raise PermissionError("retained source preparation denied")

    with monkeypatch.context() as fault:
        fault.setattr(import_execute, "ensure_durable_directory", deny)
        assert (
            asyncio.run(
                import_execute._execute_import(
                    case.queue_id, lease_owner="owned-worker"
                )
            )
            is False
        )
    assert _rows(case)["parent"]["status"] == "failed"
    assert [row["status"] for row in _rows(case)["children"]] == ["skipped"]
    assert case.source.read_bytes() == case.source_bytes
    return case


def test_direct_cleanup_protects_structural_consumer_without_history_or_events(
    pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_pack_cleanup
    import shared

    case = _retained_managed_case(pack, monkeypatch)
    with shared.get_db() as db:
        db.execute("DELETE FROM history")
        db.execute("DELETE FROM events")
    assert not import_pack_cleanup.cleanup_terminal_pack_staging(
        case.queue_id,
        pack.download_id,
        download_client_id=7,
        protocol=pack.protocol,
    )
    assert case.source.read_bytes() == case.source_bytes
    _retry(case, monkeypatch, had_failure=False)


def test_private_placement_skipped_source_survives_cleanup_and_repaired_retry(
    pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import errno
    import import_pack_cleanup

    def unsupported_rename(_source: str, _destination: str) -> None:
        raise OSError(errno.EOPNOTSUPP, "injected unavailable atomic directory rename")

    monkeypatch.setattr(import_pack_cleanup, "_rename_noreplace", unsupported_rename)
    case = _retained_managed_case(pack, monkeypatch)
    assert ".mangarr-claims" in case.source.parts
    assert not import_pack_cleanup.cleanup_terminal_pack_staging(
        case.queue_id,
        pack.download_id,
        download_client_id=7,
        protocol=pack.protocol,
    )
    assert case.source.read_bytes() == case.source_bytes
    _retry(case, monkeypatch)


@pytest.mark.parametrize("entry", ["queueing", "cleanup", "tombstone"])
def test_later_cleanup_recovery_retains_exact_skipped_consumer(
    pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
) -> None:
    import import_pack_cleanup
    import shared

    case = _retained_managed_case(pack, monkeypatch)
    artifact = case.source
    if entry != "queueing":
        # Model an existing cleanup attempt whose consumer changed before replay.
        # Acquire/capture with ordinary failed-child eligibility, using real IO.
        with shared.get_db() as db:
            db.execute(
                "UPDATE import_queue_files SET status='failed' WHERE id=?",
                (case.file_id,),
            )
        if entry == "cleanup":
            assert (
                import_pack_cleanup._acquire_cleanup_reservation(
                    queue_id=case.queue_id,
                    download_id=pack.download_id,
                    download_client_id=7,
                    protocol=pack.protocol,
                    publication_id=None,
                    lease_seconds=300,
                )
                is not None
            )
        else:
            with monkeypatch.context() as pause:
                pause.setattr(
                    import_pack_cleanup,
                    "_remove_tracked_tombstone",
                    lambda *_args, **_kwargs: False,
                )
                assert not import_pack_cleanup.cleanup_terminal_pack_staging(
                    case.queue_id,
                    pack.download_id,
                    download_client_id=7,
                    protocol=pack.protocol,
                )
            with shared.get_db() as db:
                row = db.execute(
                    "SELECT tombstone_path FROM import_pack_cleanup_tombstones WHERE queue_id=?",
                    (case.queue_id,),
                ).fetchone()
                assert row is not None
                artifact = Path(row[0]) / case.source.name
        with shared.get_db() as db:
            db.execute(
                "UPDATE import_queue_files SET status='skipped' WHERE id=?",
                (case.file_id,),
            )
    with shared.get_db() as db:
        db.execute(
            "UPDATE import_pack_cleanup_reservations SET expires_at=datetime('now','-1 hour') WHERE queue_id=?",
            (case.queue_id,),
        )
    assert artifact.read_bytes() == case.source_bytes
    before = _rows(case)
    result = import_pack_cleanup.recover_pack_cleanup_state()
    assert result.tombstones_removed == 0
    assert artifact.is_file(), "future recovery discarded a retained skipped source"
    assert artifact.read_bytes() == case.source_bytes
    assert _rows(case) == before


def test_contradictory_normalized_journal_refuses_owned_consumer_cleanup(
    pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    import import_pack_cleanup as cleanup
    import shared
    from download_identity import download_identity_key
    from file_mutation_lock import file_mutation_guard
    from private_pack_claim import PackDirectoryClaim

    case = _retained_managed_case(pack, monkeypatch)
    identity = DownloadIdentity(7, pack.protocol, pack.download_id)
    with shared.get_db() as db:
        db.execute(
            "UPDATE import_queue_files SET status='failed' WHERE id=?", (case.file_id,)
        )
    with file_mutation_guard(shared.DB_PATH) as guard:
        assert (
            cleanup._acquire_cleanup_reservation(
                queue_id=case.queue_id,
                download_id=pack.download_id,
                download_client_id=7,
                protocol=pack.protocol,
                publication_id=None,
                lease_seconds=300,
            )
            is not None
        )
        reservation = cleanup._read_reservation(download_identity_key(identity))
        assert reservation is not None
        assert cleanup._detach_terminal_cleanup(reservation, _guard=guard) == "tracked"
    with shared.get_db() as db:
        db.execute(
            "UPDATE import_queue_files SET status='skipped' WHERE id=?", (case.file_id,)
        )
        row = db.execute(
            "SELECT * FROM import_pack_cleanup_tombstones WHERE queue_id=?",
            (case.queue_id,),
        ).fetchone()
        assert row is not None
        journal = dict(row)
        claim = PackDirectoryClaim.from_json(journal["carrier_json"])
        assert claim.artifact_owner_token == claim.carrier.binding.operation_key
        assert claim.artifact_owner_token is not None
        assert claim.source_directory.marker_fingerprint is not None
        assert (
            claim.source_directory.path
            == journal["pack_path"]
            == str(case.source.parent)
        )
        artifact = Path(journal["tombstone_path"]) / case.source.name
        assert artifact.read_bytes() == case.source_bytes
        assert not case.source.exists()
        assert (
            db.execute(
                "UPDATE import_pack_cleanup_tombstones SET normalized_download_id=?"
                " WHERE tombstone_path=?",
                ("contradictory-normalized-id", journal["tombstone_path"]),
            ).rowcount
            == 1
        )
        changed = dict(
            db.execute(
                "SELECT * FROM import_pack_cleanup_tombstones WHERE queue_id=?",
                (case.queue_id,),
            ).fetchone()
        )
        assert changed.pop("normalized_download_id") != journal.pop(
            "normalized_download_id"
        )
        assert changed == journal

    tables = (
        "import_queue",
        "import_queue_files",
        "import_pack_cleanup_reservations",
        "import_pack_cleanup_tombstones",
        "import_publications",
        "import_publication_files",
        "history",
        "seen",
        "series",
        "volumes",
        "chapters",
        "events",
    )

    def rows() -> dict[str, list[dict[str, Any]]]:
        with shared.get_db() as db:
            return {
                table: [
                    dict(row)
                    for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")
                ]
                for table in tables
            }

    before = rows()
    result = cleanup.recover_pack_cleanup_state()
    assert result.tombstones_removed == 0 and result.tombstones_retained == 1
    assert artifact.read_bytes() == case.source_bytes
    assert not case.source.exists(), "refusal does not restore a moved source name"
    assert rows() == before
    assert _snapshot() == case.before
    assert before["seen"] == case.seen
    assert pack.original.read_bytes() == case.original_bytes

    # Model terminal dismissal, not a router call or a repair of the journal ID.
    with shared.get_db() as db:
        db.execute(
            "UPDATE import_queue SET status='skipped' WHERE id=?", (case.queue_id,)
        )
    settled = cleanup.recover_pack_cleanup_state()
    assert settled.tombstones_removed == 1 and settled.tombstones_retained == 0
    assert not artifact.parent.exists()
    assert _snapshot() == case.before
    assert pack.original.read_bytes() == case.original_bytes


@pytest.mark.parametrize(
    "boundary",
    [
        "empty",
        "failed",
        "imported",
        "pending",
        "needs_review",
        "wrong_owner",
        "expired",
    ],
)
def test_skipped_replay_preserves_planner_eligibility_boundaries(
    pack: Pack,
    boundary: str,
) -> None:
    import import_lease
    import import_plan
    import shared

    case = _case(pack, generated=False, reason="operator")
    with shared.get_db() as db:
        assert import_lease.claim_import_queue_row(db, case.queue_id, "actual-owner")
        first = import_plan._plan_import(
            db,
            case.queue_id,
            "actual-owner",
            {},
            {},
            {case.file_id},
            "copy",
            lease_seconds=300,
        )
        assert first is not None and first.files[0].plan_status == "skip"
        added = None
        if boundary == "empty":
            db.execute(
                "DELETE FROM import_queue_files WHERE queue_id=?", (case.queue_id,)
            )
        elif boundary in ("failed", "imported", "pending", "needs_review"):
            added = db.execute(
                "INSERT INTO import_queue_files(queue_id,filename,src_path,status,proposed_volume,file_type) VALUES(?,?,?, ?,2,'volume')",
                (case.queue_id, "Owned Pack v02.cbz", str(case.source), boundary),
            ).lastrowid
        elif boundary == "expired":
            db.execute(
                "UPDATE import_queue SET lease_expires_at=datetime('now','-1 hour') WHERE id=?",
                (case.queue_id,),
            )
        children = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM import_queue_files WHERE queue_id=? ORDER BY id",
                (case.queue_id,),
            )
        ]
        plan = import_plan._plan_import(
            db,
            case.queue_id,
            "other-owner" if boundary == "wrong_owner" else "actual-owner",
            {},
            {},
            set(),
            "copy",
            lease_seconds=300,
        )
        if boundary in ("pending", "needs_review"):
            assert plan is not None
            assert [file.file_id for file in plan.files] == [added]
        else:
            assert plan is None
        assert [
            dict(row)
            for row in db.execute(
                "SELECT * FROM import_queue_files WHERE queue_id=? ORDER BY id",
                (case.queue_id,),
            )
        ] == children
    assert _snapshot() == case.before
    assert case.source.read_bytes() == case.source_bytes


@pytest.mark.parametrize("shape", ["empty", "mixed"])
def test_retained_source_veto_does_not_change_other_failed_cleanup(
    pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    import import_pack_cleanup
    import shared

    case = _retained_managed_case(pack, monkeypatch)
    with shared.get_db() as db:
        if shape == "empty":
            db.execute(
                "DELETE FROM import_queue_files WHERE queue_id=?", (case.queue_id,)
            )
        else:
            db.execute(
                "INSERT INTO import_queue_files(queue_id,filename,status) VALUES(?,'other.cbz','failed')",
                (case.queue_id,),
            )
    assert import_pack_cleanup.cleanup_terminal_pack_staging(
        case.queue_id, pack.download_id, download_client_id=7, protocol=pack.protocol
    )
    assert not case.source.exists()


def test_foreign_client_source_does_not_protect_or_delete_an_unrelated_pack(
    pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_pack_cleanup
    import import_queue
    import shared

    case = _retained_managed_case(pack, monkeypatch)
    images = pack.root / "other-download" / "Owned Pack v01"
    images.mkdir(parents=True)
    (images / "001.jpg").write_bytes(b"other client page")
    with shared.get_db() as db:
        other, _ = import_queue._queue_import(
            db,
            1,
            pack.download_id,
            TITLE,
            SOURCE,
            None,
            str(images.parent),
            download_client_id=8,
            protocol=pack.protocol,
        )
        assert other is not None and other != case.queue_id
        source = Path(
            db.execute(
                "SELECT src_path FROM import_queue_files WHERE queue_id=?", (other,)
            ).fetchone()[0]
        )
        db.execute(
            "UPDATE import_queue_files SET src_path=? WHERE id=?",
            (str(source), case.file_id),
        )
    contents = source.read_bytes()
    assert import_pack_cleanup.cleanup_terminal_pack_staging(
        case.queue_id, pack.download_id, download_client_id=7, protocol=pack.protocol
    )
    assert not case.source.exists()
    assert source.read_bytes() == contents
    assert _rows(case)["children"][0]["src_path"] == str(source)


@pytest.mark.parametrize("malformed", ["json", "identity", "path", "inventory"])
def test_retained_cleanup_malformed_ownership_never_grants_deletion(
    pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    malformed: str,
) -> None:
    import import_pack_cleanup
    import shared

    case = _retained_managed_case(pack, monkeypatch)
    with shared.get_db() as db:
        encoded = db.execute(
            "SELECT directory_ownership_json FROM import_pack_cleanup_reservations WHERE queue_id=?",
            (case.queue_id,),
        ).fetchone()[0]
        proof = json.loads(encoded)
        if malformed == "identity":
            proof["identity_key"] = "foreign-owner"
        elif malformed == "path":
            proof["canonical_directory"]["path"] = str(pack.root / "unowned")
        elif malformed == "inventory":
            proof["inventory"] = None
        db.execute(
            "UPDATE import_pack_cleanup_reservations SET directory_ownership_json=? WHERE queue_id=?",
            ("{" if malformed == "json" else json.dumps(proof), case.queue_id),
        )
    assert not import_pack_cleanup.cleanup_terminal_pack_staging(
        case.queue_id, pack.download_id, download_client_id=7, protocol=pack.protocol
    )
    assert case.source.is_file(), "malformed proof allowed detaching retained src_path"
    assert case.source.read_bytes() == case.source_bytes


@pytest.mark.parametrize("reason", ["quality", "operator"])
def test_guarded_failed_preparation_retains_managed_source_until_repaired_retry(
    pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    case = _case(pack, generated=True, reason=reason)
    _fail_preparation(case, reason, monkeypatch)
    assert case.source.is_file(), "guarded failed all-skip deleted retained src_path"
    assert case.source.read_bytes() == case.source_bytes
    _retry(case, monkeypatch)


@pytest.mark.parametrize("reason", ["quality", "operator"])
def test_successful_managed_skip_still_settles_then_cleans(
    pack: Pack,
    reason: str,
) -> None:
    import import_execute

    case = _case(pack, generated=True, reason=reason)
    assert (
        asyncio.run(
            import_execute._guarded_execute_import(
                case.queue_id, skip_ids={case.file_id} if reason == "operator" else None
            )
        )
        is True
    )
    _assert_settled(case, had_failure=False)


def test_ordinary_failed_ready_file_retains_terminal_cleanup_eligibility(
    pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_execute
    import shared

    case = _case(pack, generated=True, reason="operator")

    def deny(_path: str) -> None:
        raise PermissionError("ordinary ready preparation denied")

    monkeypatch.setattr(import_execute, "ensure_durable_directory", deny)
    assert asyncio.run(import_execute._guarded_execute_import(case.queue_id)) is False
    state = _rows(case)
    assert state["parent"]["status"] == "failed"
    assert [row["status"] for row in state["children"]] == ["failed"]
    assert not case.source.exists()
    with shared.get_db() as db:
        assert db.execute(
            "SELECT status,download_id FROM volumes WHERE id=2"
        ).fetchone()[:] == ("wanted", None)


def test_dismissed_managed_pack_remains_cleanup_eligible(pack: Pack) -> None:
    import import_pack_cleanup
    import shared
    from routers.import_ import dismiss_import_queue_entry

    # Dismissal requires the persisted owner's current client configuration.
    # The base claim-conservation fixture intentionally does not create one.
    with shared.get_db() as db:
        db.execute(
            "INSERT INTO download_clients(id,name,type,host,enabled)"
            " VALUES(7,'fixture',?,'http://downloader.invalid',1)",
            (pack.client_type,),
        )
    case = _case(pack, generated=True, reason="quality")
    assert dismiss_import_queue_entry(case.queue_id)["status"] == "dismissed"
    assert import_pack_cleanup.cleanup_terminal_pack_staging(
        case.queue_id,
        pack.download_id,
        download_client_id=7,
        protocol=pack.protocol,
    )
    assert not case.source.exists()


@pytest.mark.parametrize("fence", ["lease", "publication"])
def test_managed_pack_cleanup_and_retry_preserve_live_authority_fences(
    pack: Pack,
    fence: str,
) -> None:
    import import_lease
    import import_pack_cleanup
    import import_plan
    import import_publication
    import shared
    from routers.import_ import retry_import_queue_entry

    case = _case(pack, generated=True, reason="quality")
    with shared.get_db() as db:
        assert import_lease.claim_import_queue_row(db, case.queue_id, "live-owner")
        if fence == "publication":
            plan = import_plan._plan_import(
                db,
                case.queue_id,
                "live-owner",
                {},
                {},
                set(),
                "copy",
                lease_seconds=300,
            )
            assert plan is not None
            import_publication.create_publication(
                db,
                plan,
                "live-owner",
                str(pack.root / "publication-stage"),
                {},
            )
            # A terminal queue without a lease is still fenced by its journal.
            db.execute(
                "UPDATE import_queue SET status='failed',lease_owner=NULL,lease_expires_at=NULL WHERE id=?",
                (case.queue_id,),
            )
    before = _rows(case)
    assert not import_pack_cleanup.cleanup_terminal_pack_staging(
        case.queue_id,
        pack.download_id,
        download_client_id=7,
        protocol=pack.protocol,
    )
    assert retry_import_queue_entry(case.queue_id)["ok"] is False
    assert _rows(case) == before
    assert case.source.read_bytes() == case.source_bytes


def _contract_rows() -> dict[str, list[dict[str, Any]]]:
    """Conserve every fixture table, including private authority and metadata."""
    import shared

    with shared.get_db() as db:
        tables = [
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
                " AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {
            table: [
                dict(row)
                for row in db.execute(
                    'SELECT * FROM "' + table.replace('"', '""') + '" ORDER BY rowid'
                )
            ]
            for table in tables
        }


def _assert_source_names_and_bytes(case: Case) -> None:
    assert case.source.is_file(), "cleanup moved or discarded retained source name"
    assert case.source.read_bytes() == case.source_bytes
    assert case.pack.original.read_bytes() == case.original_bytes
    assert case.pack.candidate.read_bytes() == case.candidate_bytes
    if case.raw_page is not None:
        assert case.raw_page.read_bytes() == b"generated pack source page"


def _reservation_case(
    pack: Pack, monkeypatch: pytest.MonkeyPatch, layout: str, purpose: str
) -> Case:
    import errno
    import import_pack_cleanup as cleanup
    import shared

    def unsupported(_source: str, _destination: str) -> None:
        raise OSError(errno.EOPNOTSUPP, "injected unavailable atomic directory rename")

    with monkeypatch.context() as placement:
        if layout == "private":
            placement.setattr(cleanup, "_rename_noreplace", unsupported)
        case = _retained_managed_case(pack, monkeypatch)
        assert (".mangarr-claims" in case.source.parts) == (layout == "private")
        if purpose == "cleanup":
            with shared.get_db() as db:
                db.execute(
                    "UPDATE import_queue_files SET status='failed' WHERE id=?",
                    (case.file_id,),
                )
            assert (
                cleanup._acquire_cleanup_reservation(
                    queue_id=case.queue_id,
                    download_id=pack.download_id,
                    download_client_id=7,
                    protocol=pack.protocol,
                    publication_id=None,
                    lease_seconds=300,
                )
                is not None
            )
            with shared.get_db() as db:
                db.execute(
                    "UPDATE import_queue_files SET status='skipped' WHERE id=?",
                    (case.file_id,),
                )
    return case


def _contradict_reservation(case: Case, *, expired: bool) -> None:
    import shared

    with shared.get_db() as db:
        if expired:
            db.execute(
                "UPDATE import_pack_cleanup_reservations"
                " SET expires_at=datetime('now','-1 hour') WHERE queue_id=?",
                (case.queue_id,),
            )
        before = dict(
            db.execute(
                "SELECT * FROM import_pack_cleanup_reservations WHERE queue_id=?",
                (case.queue_id,),
            ).fetchone()
        )
        assert db.execute(
            "SELECT expires_at <= CURRENT_TIMESTAMP"
            " FROM import_pack_cleanup_reservations WHERE queue_id=?",
            (case.queue_id,),
        ).fetchone()[0] == int(expired)
        assert (
            db.execute(
                "UPDATE import_pack_cleanup_reservations SET normalized_download_id=?"
                " WHERE queue_id=?",
                ("contradictory-normalized-id", case.queue_id),
            ).rowcount
            == 1
        )
        after = dict(
            db.execute(
                "SELECT * FROM import_pack_cleanup_reservations WHERE queue_id=?",
                (case.queue_id,),
            ).fetchone()
        )
        assert before.pop("normalized_download_id") != after.pop(
            "normalized_download_id"
        )
        assert after == before


@pytest.mark.parametrize("layout", ["native", "private"])
def test_w1_direct_reservation_contradiction_refuses_before_mutation(
    pack: Pack, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    import import_pack_cleanup as cleanup

    case = _reservation_case(pack, monkeypatch, layout, "queueing")
    _contradict_reservation(case, expired=False)
    before = _contract_rows()
    result = cleanup.cleanup_terminal_pack_staging(
        case.queue_id, pack.download_id, download_client_id=7, protocol=pack.protocol
    )
    _assert_source_names_and_bytes(case)
    assert _contract_rows() == before
    assert result is False


@pytest.mark.parametrize("layout", ["native", "private"])
@pytest.mark.parametrize("purpose", ["queueing", "cleanup"])
def test_w2_recovery_reservation_contradiction_refuses_before_takeover(
    pack: Pack, monkeypatch: pytest.MonkeyPatch, layout: str, purpose: str
) -> None:
    import import_pack_cleanup as cleanup

    case = _reservation_case(pack, monkeypatch, layout, purpose)
    _contradict_reservation(case, expired=True)
    before = _contract_rows()
    result = cleanup.recover_pack_cleanup_state()
    _assert_source_names_and_bytes(case)
    assert _contract_rows() == before
    assert result.reservations_recovered == 0 and result.tombstones_removed == 0


def _admit_retry(case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    import main
    from routers.import_ import retry_import_queue_entry

    monkeypatch.setattr(main, "schedule_import_worker", lambda _queue_id: None)
    assert retry_import_queue_entry(case.queue_id) == {
        "ok": True,
        "status": "pending",
        "queued": False,
        "retried_files": 0,
    }


def _assert_failed_replay(
    case: Case,
    before: dict[str, list[dict[str, Any]]],
    *,
    fresh: bool = False,
    failure_count: int = 1,
) -> None:
    import import_queue
    import shared

    _assert_source_names_and_bytes(case)
    after = _contract_rows()
    parent = _rows(case)["parent"]
    assert parent["status"] == "failed" and parent["failed_at"]
    assert parent["lease_owner"] is None and parent["lease_expires_at"] is None
    mutable_parent = {"status", "failed_at", "lease_owner", "lease_expires_at"}
    assert [
        {key: value for key, value in row.items() if key not in mutable_parent}
        for row in after["import_queue"]
    ] == [
        {key: value for key, value in row.items() if key not in mutable_parent}
        for row in before["import_queue"]
    ]
    assert after["import_queue_files"] == before["import_queue_files"]
    assert after["history"][: len(before["history"])] == before["history"]
    assert [row["event_type"] for row in after["history"]] == (
        [] if fresh else ["import_failed"] * failure_count
    )
    with shared.get_db() as db:
        assert not import_queue._has_terminal_download_receipt(
            db,
            series_id=1,
            torrent_url=SOURCE,
            identity=DownloadIdentity(7, case.pack.protocol, case.pack.download_id),
        )
    # Parent failure/lease and diagnostics are intended; no other row is mutable.
    for table in before.keys() - {"import_queue", "history", "events"}:
        if fresh and table == "volumes":
            expected = [dict(row) for row in before[table]]
            reset = (
                "grabbed_at",
                "download_id",
                "source_url",
                "torrent_name",
                "indexer",
                "protocol",
                "client",
                "download_client_id",
                "release_group",
                "import_path",
            )
            for row in expected:
                if (
                    row["series_id"] == 1
                    and row["download_client_id"] == 7
                    and row["status"] == "grabbed"
                ):
                    row.update(dict.fromkeys(reset))
                    row["status"] = "wanted"
            assert after[table] == expected
        else:
            assert after[table] == before[table], table


@pytest.mark.parametrize("retained", [True, False], ids=["retained", "fresh"])
def test_w3_destination_none_retained_conservation_and_fresh_reset(
    pack: Pack, monkeypatch: pytest.MonkeyPatch, retained: bool
) -> None:
    import import_execute
    import import_plan
    import shared

    # This fixture has conflicting protocol negatives; configure the exact owner
    # so the old resolver/reset branch is exercised for both protocols.
    with shared.get_db() as db:
        db.execute(
            "INSERT INTO download_clients(id,name,type,host,enabled)"
            " VALUES(7,'fixture',?,'http://downloader.invalid',1)",
            (pack.client_type,),
        )
    case = _case(pack, generated=retained, reason="quality")
    if retained:
        _fail_preparation(case, "quality", monkeypatch)
        _admit_retry(case, monkeypatch)
    before = _contract_rows()
    monkeypatch.setattr(import_plan, "_series_library_dir", lambda _db, _series: None)
    assert asyncio.run(import_execute._guarded_execute_import(case.queue_id)) is False
    _assert_failed_replay(case, before, fresh=not retained)


def test_w4_real_root_exception_preserves_retained_replay(
    pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    import import_execute
    import shared

    case = _case(pack, generated=True, reason="quality")
    _fail_preparation(case, "quality", monkeypatch)
    _admit_retry(case, monkeypatch)
    with shared.get_db() as db:
        db.execute("DELETE FROM root_folders")
    before = _contract_rows()
    assert asyncio.run(import_execute._guarded_execute_import(case.queue_id)) is False
    _assert_failed_replay(case, before)
    assert any(
        "No root folders configured" in row["message"]
        for row in _contract_rows()["events"]
    )


def test_w5_repeated_preparation_failure_preserves_then_settles_retained_replay(
    pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    import import_execute

    case = _case(pack, generated=True, reason="quality")
    _fail_preparation(case, "quality", monkeypatch)
    _admit_retry(case, monkeypatch)
    before = _contract_rows()

    def deny(path: str) -> None:
        assert path == str(pack.original.parent)
        raise PermissionError("repeated retained destination preparation denied")

    with monkeypatch.context() as fault:
        fault.setattr(import_execute, "ensure_durable_directory", deny)
        assert (
            asyncio.run(import_execute._guarded_execute_import(case.queue_id)) is False
        )
    _assert_failed_replay(case, before, failure_count=2)
    assert len(_rows(case)["history"]) == 2
    _retry(case, monkeypatch, failure_count=2)


def test_retained_reservation_renewal_advances_bounded_recovery(
    pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    import import_pack_cleanup as cleanup
    import shared

    retained = _retained_managed_case(pack, monkeypatch)
    later = _retained_managed_case(
        replace(pack, root=pack.root / "later", download_id="Later-Recoverable-Pack"),
        monkeypatch,
    )
    with shared.get_db() as db:
        db.execute(
            "UPDATE import_queue_files SET status='failed' WHERE id=?",
            (later.file_id,),
        )
        db.execute(
            "UPDATE import_pack_cleanup_reservations"
            " SET expires_at=datetime('now','-1 hour'),"
            " updated_at=CASE WHEN queue_id=? THEN '2000-01-01' ELSE '2000-01-02' END",
            (retained.queue_id,),
        )
        assert [
            row[0]
            for row in db.execute(
                "SELECT queue_id FROM import_pack_cleanup_reservations"
                " WHERE expires_at <= CURRENT_TIMESTAMP"
                " ORDER BY updated_at,normalized_download_id"
            )
        ] == [retained.queue_id, later.queue_id]
    before = _contract_rows()
    first = cleanup.recover_pack_cleanup_state(max_rows=1)
    _assert_source_names_and_bytes(retained)
    _assert_source_names_and_bytes(later)
    assert first.reservations_recovered == first.tombstones_removed == 0
    after_first = _contract_rows()
    for table in before.keys() - {"import_pack_cleanup_reservations"}:
        assert after_first[table] == before[table], table
    assert (
        after_first["import_pack_cleanup_reservations"][1]
        == before["import_pack_cleanup_reservations"][1]
    )

    second = cleanup.recover_pack_cleanup_state(max_rows=1)
    _assert_source_names_and_bytes(retained)
    assert not later.source.exists(), (
        "retained first page starved later recoverable pack"
    )
    assert second.reservations_recovered == second.tombstones_removed == 1
    after = _contract_rows()
    reservations = after["import_pack_cleanup_reservations"]
    assert [row["queue_id"] for row in reservations] == [
        retained.queue_id,
        later.queue_id,
    ]
    # Completed generated cleanup keeps its ordinary provenance fence.
    completed = reservations[1]
    assert completed["purpose"] == "queueing"
    assert json.loads(completed["directory_ownership_json"])["retained_reason"] == (
        "terminal-discarded"
    )
    original = before["import_pack_cleanup_reservations"][0]
    renewed = reservations[0]
    assert renewed["owner_token"] != original["owner_token"]
    assert renewed["updated_at"] != original["updated_at"]
    mutable = {"owner_token", "expires_at", "updated_at"}
    assert {key: value for key, value in renewed.items() if key not in mutable} == {
        key: value for key, value in original.items() if key not in mutable
    }
    assert renewed == after_first["import_pack_cleanup_reservations"][0]
    with shared.get_db() as db:
        assert (
            db.execute(
                "SELECT expires_at > CURRENT_TIMESTAMP"
                " FROM import_pack_cleanup_reservations WHERE queue_id=?",
                (retained.queue_id,),
            ).fetchone()[0]
            == 1
        )
    # Later ordinary cleanup may register its private namespace, but no domain
    # rows or retained source proofs are disposable merely because a page expired.
    for table in before.keys() - {
        "import_pack_cleanup_reservations",
        "file_claim_namespaces",
    }:
        assert after[table] == before[table], table
