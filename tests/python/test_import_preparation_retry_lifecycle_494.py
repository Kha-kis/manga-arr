"""Retained preparation failures must remain retryable without losing skip intent."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
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


def _assert_settled(case: Case, *, had_failure: bool) -> None:
    import import_queue
    import shared

    state = _rows(case)
    assert state["parent"] is None and state["children"] == []
    assert [row["event_type"] for row in state["history"]] == (
        ["import_failed", "import_skipped"] if had_failure else ["import_skipped"]
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
    case: Case, monkeypatch: pytest.MonkeyPatch, *, had_failure: bool = True
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
    _assert_settled(case, had_failure=had_failure)


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
