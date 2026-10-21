"""Preparation failures are not terminal success or claim-settlement authority."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from download_identity import DownloadIdentity
from test_import_skip_settlement_482 import (
    GUID,
    SOURCE,
    TITLE,
    Pack,
    _assert_guid_dedup,
    _plan_skip,
    _queue,
    _snapshot,
    pack as pack,
)

if TYPE_CHECKING:
    from import_plan import _ImportPlan
    from import_staging import _StageOutcome


SPACE_REASON = "Import blocked: insufficient free space (injected guard result)"


@dataclass(frozen=True)
class FailedPreparation:
    pack: Pack
    queue_id: int
    result: tuple[bool, int, str]
    transitions: list[str]
    queue: dict[str, Any] | None
    children: list[dict[str, Any]]
    history: list[dict[str, Any]]
    events: list[str]
    reason: str
    terminal_receipt: bool
    before: dict[str, list[dict[str, Any]]]
    after: dict[str, list[dict[str, Any]]]
    seen_before: list[dict[str, Any]]
    seen_after: list[dict[str, Any]]
    archives_before: dict[Path, bytes]


@pytest.fixture(autouse=True)
def no_downloader_network(monkeypatch: pytest.MonkeyPatch) -> None:
    real_client = httpx.AsyncClient

    def reject(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected external request: {request.method}")

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(reject), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)


@pytest.fixture(
    params=[
        "executor_directory",
        "executor_space_guard_injected",
        "direct_directory",
        "direct_space",
    ]
)
def failed_preparation(
    request: pytest.FixtureRequest,
    pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
) -> FailedPreparation:
    import import_commit
    import import_execute
    import import_queue
    import shared

    queue_id = _queue(pack)
    with shared.get_db() as db:
        db.execute("UPDATE volumes SET import_path=? WHERE id=3", (str(pack.original),))
        db.execute(
            "UPDATE chapters SET import_path=? WHERE id=24", (str(pack.original),)
        )
        db.execute(
            "UPDATE volumes SET indexer='retained-indexer',release_group='retained-group',"
            " language='en',quality='cbz' WHERE id=2"
        )
    before = _snapshot()
    archives_before = {
        path: path.read_bytes() for path in (pack.original, pack.candidate)
    }
    identity = DownloadIdentity(7, pack.protocol, pack.download_id)
    with shared.get_db() as db:
        seen_before = [
            dict(row) for row in db.execute("SELECT * FROM seen ORDER BY torrent_url")
        ]
        assert not import_queue._has_terminal_download_receipt(
            db, series_id=1, torrent_url=SOURCE, identity=identity
        )

    real_commit = import_commit._commit_import
    real_transition = import_commit.transition_import_queue_row
    results: list[tuple[bool, int, str]] = []
    reasons: list[str] = []
    transitions: list[str] = []

    def transition(db: sqlite3.Connection, *args: Any, **kwargs: Any) -> bool:
        changed = real_transition(db, *args, **kwargs)
        if changed:
            row = db.execute(
                "SELECT status FROM import_queue WHERE id=?", (queue_id,)
            ).fetchone()
            assert row is not None
            transitions.append(row["status"])
        return changed

    def commit(
        db: sqlite3.Connection,
        plan: _ImportPlan,
        outcomes: list[_StageOutcome],
        fs_committed: bool,
        commit_failure_reason: str,
        **kwargs: Any,
    ) -> tuple[bool, int, str]:
        assert [file.plan_status for file in plan.files] == ["skip"]
        assert not fs_committed and outcomes == [] and commit_failure_reason
        reasons.append(commit_failure_reason)
        result = real_commit(
            db, plan, outcomes, fs_committed, commit_failure_reason, **kwargs
        )
        results.append(result)
        return result

    monkeypatch.setattr(import_commit, "transition_import_queue_row", transition)
    monkeypatch.setattr(import_execute, "_split_commit_import", commit)
    if request.param.startswith("executor"):
        if request.param == "executor_directory":

            def deny_directory(path: str) -> None:
                assert path == str(pack.original.parent)
                raise PermissionError("issue494 destination preparation denied")

            monkeypatch.setattr(
                import_execute, "ensure_durable_directory", deny_directory
            )
        else:
            # The real guard bypasses all-skip plans. Inject only its return,
            # leaving executor/planner/lease/preparer/commit and DB writes real.
            monkeypatch.setattr(
                import_execute,
                "_check_minimum_free_space",
                lambda _plan, _mode: (False, SPACE_REASON),
            )
        assert asyncio.run(import_execute._execute_import(queue_id)) is False
    else:
        plan = _plan_skip(pack, queue_id=queue_id)
        reason = (
            SPACE_REASON
            if request.param == "direct_space"
            else "issue494 destination preparation denied"
        )
        with shared.get_db() as db:
            commit(
                db,
                plan,
                [],
                False,
                reason,
                lease_owner="fixture-owner",
                lease_seconds=300,
            )
    assert len(results) == len(reasons) == 1
    with shared.get_db() as db:
        queue = db.execute(
            "SELECT * FROM import_queue WHERE id=?", (queue_id,)
        ).fetchone()
        children = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM import_queue_files WHERE queue_id=? ORDER BY id",
                (queue_id,),
            )
        ]
        history = [dict(row) for row in db.execute("SELECT * FROM history ORDER BY id")]
        seen_after = [
            dict(row) for row in db.execute("SELECT * FROM seen ORDER BY torrent_url")
        ]
        terminal_receipt = import_queue._has_terminal_download_receipt(
            db, series_id=1, torrent_url=SOURCE, identity=identity
        )
        events = [
            row[0] for row in db.execute("SELECT message FROM events ORDER BY id")
        ]
        assert db.execute("SELECT 1 FROM import_publications").fetchone() is None
    return FailedPreparation(
        pack,
        queue_id,
        results[0],
        transitions,
        dict(queue) if queue else None,
        children,
        history,
        events,
        reasons[0],
        terminal_receipt,
        before,
        _snapshot(),
        seen_before,
        seen_after,
        archives_before,
    )


def test_preparation_failure_commit_returns_failure(
    failed_preparation: FailedPreparation,
) -> None:
    assert failed_preparation.result == (False, 0, "failed")


def test_preparation_failure_retains_failed_queue_and_skipped_decisions(
    failed_preparation: FailedPreparation,
) -> None:
    outcome = failed_preparation
    assert outcome.transitions == ["failed"]
    assert outcome.queue is not None, "failed work must survive for retry/review"
    assert outcome.queue["status"] == "failed"
    assert outcome.queue["failed_at"]
    assert outcome.queue["lease_owner"] is None
    assert outcome.queue["lease_expires_at"] is None
    assert outcome.queue["torrent_url"] == SOURCE
    assert outcome.queue["download_client_id"] == 7
    assert outcome.queue["download_protocol"] == outcome.pack.protocol
    assert len(outcome.children) == 1
    assert outcome.children[0]["status"] == "skipped"
    assert outcome.children[0]["src_path"] == str(outcome.pack.candidate)
    assert any(outcome.reason in message for message in outcome.events)


def test_preparation_failure_persists_only_exact_failed_history(
    failed_preparation: FailedPreparation,
) -> None:
    outcome = failed_preparation
    assert [row["event_type"] for row in outcome.history] == ["import_failed"]
    row = outcome.history[0]
    assert row["series_id"] == 1
    assert row["torrent_url"] == SOURCE
    assert row["source_title"] == TITLE
    assert row["download_client_id"] == 7
    assert row["protocol"] == outcome.pack.protocol
    assert row["download_id"] == (
        outcome.pack.download_id.lower()
        if outcome.pack.protocol == "torrent"
        else outcome.pack.download_id
    )


def test_preparation_failure_is_not_terminal_admission(
    failed_preparation: FailedPreparation,
) -> None:
    assert failed_preparation.terminal_receipt is False


def test_preparation_failure_conserves_claims_seen_guid_and_archive_bytes(
    failed_preparation: FailedPreparation,
) -> None:
    outcome = failed_preparation
    assert outcome.after == outcome.before
    assert outcome.seen_after == outcome.seen_before
    assert outcome.seen_after[0]["release_guid"] == GUID
    for path, contents in outcome.archives_before.items():
        assert path.read_bytes() == contents
    _assert_guid_dedup(outcome.pack)


def test_real_all_skip_bypasses_free_space_and_remains_terminal_success(
    pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_execute
    import import_queue
    import shared

    queue_id = _queue(pack)
    monkeypatch.setitem(shared.CONFIG, "minimum_free_space_mb", "999999999")

    def unexpected_disk_usage(_path: str) -> None:
        raise AssertionError("all-skip has no ready bytes to reserve")

    monkeypatch.setattr(import_execute.shutil, "disk_usage", unexpected_disk_usage)
    before = {path: path.read_bytes() for path in (pack.original, pack.candidate)}
    assert asyncio.run(import_execute._execute_import(queue_id)) is True
    with shared.get_db() as db:
        assert (
            db.execute("SELECT 1 FROM import_queue WHERE id=?", (queue_id,)).fetchone()
            is None
        )
        assert (
            db.execute(
                "SELECT 1 FROM import_queue_files WHERE queue_id=?", (queue_id,)
            ).fetchone()
            is None
        )
        assert [row[0] for row in db.execute("SELECT event_type FROM history")] == [
            "import_skipped"
        ]
        assert import_queue._has_terminal_download_receipt(
            db,
            series_id=1,
            torrent_url=SOURCE,
            identity=DownloadIdentity(7, pack.protocol, pack.download_id),
        )
    state = _snapshot()
    assert [row["status"] for row in state["volumes"] if row["id"] in (2, 3)] == [
        "wanted",
        "wanted",
    ]
    assert [
        row["status"] for row in state["chapters"] if row["id"] in (20, 24, 30)
    ] == ["wanted"] * 3
    for path, contents in before.items():
        assert path.read_bytes() == contents
    _assert_guid_dedup(pack)


@pytest.mark.parametrize("failure", ["directory", "space"])
def test_real_ready_file_preparation_failure_retains_existing_reset_behavior(
    pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    import import_execute
    import shared

    queue_id = _queue(pack)
    with shared.get_db() as db:
        db.execute(
            "UPDATE import_queue_files SET proposed_volume=2 WHERE queue_id=?",
            (queue_id,),
        )
    before = {path: path.read_bytes() for path in (pack.original, pack.candidate)}
    if failure == "directory":

        def deny_directory(_path: str) -> None:
            raise PermissionError("issue494 ready destination denied")

        monkeypatch.setattr(import_execute, "ensure_durable_directory", deny_directory)
    else:
        monkeypatch.setitem(shared.CONFIG, "minimum_free_space_mb", "100")

        @dataclass(frozen=True)
        class Usage:
            total: int = 200 * 1024 * 1024
            used: int = 175 * 1024 * 1024
            free: int = 25 * 1024 * 1024

        monkeypatch.setattr(import_execute.shutil, "disk_usage", lambda _path: Usage())
    assert asyncio.run(import_execute._execute_import(queue_id)) is False
    with shared.get_db() as db:
        row = db.execute(
            "SELECT status,lease_owner FROM import_queue WHERE id=?", (queue_id,)
        ).fetchone()
        assert tuple(row) == ("failed", None)
        assert [
            row[0]
            for row in db.execute(
                "SELECT status FROM import_queue_files WHERE queue_id=?", (queue_id,)
            )
        ] == ["failed"]
        assert [row[0] for row in db.execute("SELECT event_type FROM history")] == [
            "import_failed"
        ]
        assert db.execute("SELECT 1 FROM import_publications").fetchone() is None
        assert db.execute(
            "SELECT status,download_id FROM volumes WHERE id=2"
        ).fetchone()[:] == ("wanted", None)
    for path, contents in before.items():
        assert path.read_bytes() == contents
