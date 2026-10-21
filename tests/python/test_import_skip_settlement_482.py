"""Prospective all-skipped commit settlement, not legacy orphan repair."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
import zipfile

import httpx
import pytest

from download_identity import DownloadIdentity, DownloadProtocol

if TYPE_CHECKING:
    from import_download import DownloadNotificationIntent
    from import_plan import _ImportPlan


SOURCE = "https://indexer.invalid/release?token=old"
GUID = "stable-pack-guid-482"
TITLE = "Owned Pack v01-v02"


@dataclass(frozen=True)
class Pack:
    root: Path
    protocol: DownloadProtocol
    download_id: str
    client_type: Literal["qbittorrent", "sabnzbd"]
    original: Path
    candidate: Path


def _archive(path: Path, page: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.jpg", page)


@pytest.fixture(params=["torrent", "nzb"])
def pack(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Pack:
    import main
    import security
    import shared

    protocol: DownloadProtocol = request.param
    client_type: Literal["qbittorrent", "sabnzbd"] = (
        "qbittorrent" if protocol == "torrent" else "sabnzbd"
    )
    download_id = "PACK-HASH" if protocol == "torrent" else "NZO-Pack"
    db_path = str(tmp_path / "lifecycle.db")
    monkeypatch.setattr(shared, "DB_PATH", db_path)
    monkeypatch.setattr(main, "DB_PATH", db_path)
    config: dict[str, Any] = {}
    monkeypatch.setattr(main, "CONFIG", config)
    monkeypatch.setattr(shared, "CONFIG", config)
    monkeypatch.setattr(security, "_SECRET_CIPHER", None)
    security.load_or_create_secret_cipher(str(tmp_path / "keys"))
    main.init_db()
    main.load_config()
    config.update(
        import_mode="copy", remove_completed="false", failed_download_handling="0"
    )
    library = tmp_path / "library"
    original = library / "Owned Pack" / "Owned Pack v01.cbz"
    candidate = tmp_path / "downloads" / "Owned Pack v01.cbz"
    _archive(original, b"original library pages")
    _archive(candidate, b"candidate must never replace original")
    with shared.get_db() as db:
        db.execute("DELETE FROM root_folders")
        db.execute(
            "INSERT INTO root_folders(id,path,label,is_default) VALUES(1,?,'Fixture',1)",
            (str(library),),
        )
        db.executemany(
            "INSERT INTO series(id,title,search_pattern,root_folder_id,total_volumes)"
            " VALUES(?,?,?,1,10)",
            [(1, "Owned Pack", "Owned Pack"), (2, "Other Series", "Other Series")],
        )
        db.execute(
            "INSERT INTO volumes(id,series_id,volume_num,status,quality,import_path,"
            " source_url,download_id,download_client_id,protocol,client,indexer,release_group,language)"
            " VALUES(1,1,1,'downloaded','cbz',?,'https://original.invalid','original-id',"
            " 42,'torrent','qbittorrent','original-indexer','original-group','en')",
            (str(original),),
        )
        for row_id, number, owner, row_protocol, url, series_id in (
            (2, 2, 7, protocol, SOURCE, 1),
            (3, 2.5, 7, protocol, SOURCE, 1),
            (4, 4, 8, protocol, SOURCE, 1),
            (5, 5, 7, "nzb" if protocol == "torrent" else "torrent", SOURCE, 1),
            (6, 6, 7, protocol, "https://other.invalid", 1),
            (7, 7, None, protocol, SOURCE, 1),
            (8, 2, 7, protocol, SOURCE, 2),
        ):
            db.execute(
                "INSERT INTO volumes(id,series_id,volume_num,status,source_url,"
                " download_id,download_client_id,protocol,client,torrent_name,grabbed_at)"
                " VALUES(?,?,?,'grabbed',?,?,?,?,?,?,'2026-10-01')",
                (
                    row_id,
                    series_id,
                    number,
                    url,
                    download_id,
                    owner,
                    row_protocol,
                    client_type,
                    TITLE,
                ),
            )
        for row_id, volume_id, status, owner, monitored in (
            (20, 2, "grabbed", 7, 1),
            (21, 2, "downloaded", 42, 1),
            (22, 2, "grabbed", 8, 1),
            (23, 2, "wanted", None, 1),
            (24, 2, "grabbed", 7, 0),
            (30, None, "grabbed", 7, 1),
        ):
            db.execute(
                "INSERT INTO chapters(id,series_id,volume_id,chapter_num,status,"
                " download_client_id,protocol,download_id,torrent_url,monitored,import_path)"
                " VALUES(?,1,?,?,?,?,?,?,?,?,?)",
                (
                    row_id,
                    volume_id,
                    row_id,
                    status,
                    owner,
                    protocol,
                    download_id,
                    SOURCE,
                    monitored,
                    str(original) if status == "downloaded" else None,
                ),
            )
        db.execute(
            "INSERT INTO seen(torrent_url,torrent_name,release_guid,series_id,"
            " download_client_id,protocol,client,download_id,respect_grab_claims)"
            " VALUES(?,?,?,1,7,?,?,?,1)",
            (SOURCE, TITLE, GUID, protocol, client_type, download_id),
        )
    return Pack(tmp_path, protocol, download_id, client_type, original, candidate)


@pytest.fixture
def prospective_pack(pack: Pack) -> Pack:
    import shared

    # Independent colliding orphan selectors stay in the frozen QA follow-up.
    # Commit-only tests deliberately continue to use the full negative fixture.
    with shared.get_db() as db:
        db.execute("DELETE FROM volumes WHERE id IN (5,6,8)")
    return pack


def _queue(pack: Pack) -> int:
    import import_queue
    import shared

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
            str(pack.candidate.parent),
            download_client_id=7,
            protocol=pack.protocol,
        )
    assert queue_id is not None and not review
    return queue_id


def _plan_skip(
    pack: Pack, reason: str = "quality", *, queue_id: int | None = None
) -> _ImportPlan:
    import import_lease
    import import_plan
    import shared

    if queue_id is None:
        queue_id = _queue(pack)
    with shared.get_db() as db:
        file_id = db.execute(
            "SELECT id FROM import_queue_files WHERE queue_id=?", (queue_id,)
        ).fetchone()[0]
        if reason != "quality":
            db.execute(
                "UPDATE import_queue_files SET proposed_volume=2 WHERE id=?", (file_id,)
            )
        if reason == "lost_claim":
            db.execute("UPDATE volumes SET download_client_id=8 WHERE id=2")
        assert import_lease.claim_import_queue_row(db, queue_id, "fixture-owner")
        plan = import_plan._plan_import(
            db,
            queue_id,
            "fixture-owner",
            {},
            {},
            {file_id} if reason == "operator" else set(),
            "copy",
            lease_seconds=300,
        )
        assert plan is not None
        assert [file.plan_status for file in plan.files] == ["skip"]
    return plan


def _commit_skip(
    pack: Pack, reason: str = "quality", *, queue_id: int | None = None
) -> int:
    import import_commit
    import shared

    plan = _plan_skip(pack, reason, queue_id=queue_id)
    intents: list[DownloadNotificationIntent] = []
    with shared.get_db() as db:
        assert import_commit._commit_import(
            db,
            plan,
            [],
            True,
            "",
            lease_owner="fixture-owner",
            lease_seconds=300,
            post_commit_intents=intents,
        ) == (True, 0, "imported")
    assert intents == []
    return int(plan.queue["id"])


def _snapshot() -> dict[str, list[dict[str, Any]]]:
    import shared

    with shared.get_db() as db:
        return {
            table: [
                dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY id")
            ]
            for table in ("series", "volumes", "chapters")
        }


def _poll_removed(pack: Pack, monkeypatch: pytest.MonkeyPatch) -> None:
    import import_discovery as discovery
    import shared

    real_client = httpx.AsyncClient
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "downloader.invalid"
        requests.append(request.url.path)
        if pack.protocol == "torrent":
            if request.url.path == "/api/v2/auth/login":
                return httpx.Response(200, text="Ok.")
            assert request.url.path == "/api/v2/torrents/info"
            return httpx.Response(200, json=[])
        assert request.url.path == "/api"
        mode = request.url.params["mode"]
        assert mode in ("history", "queue")
        return httpx.Response(200, json={mode: {"slots": []}})

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(respond), **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(discovery.httpx, "AsyncClient", client)
        partition = discovery._ClientPollPartition(
            7, "fixture", pack.client_type, False
        )
        poll = (
            discovery._poll_qbit_partition
            if pack.protocol == "torrent"
            else discovery._poll_sab_partition
        )
        asyncio.run(
            poll(
                partition, {"host": "http://downloader.invalid", "password": "fixture"}
            )
        )
    assert len(requests) == 2
    with shared.get_db() as db:
        assert (
            db.execute("SELECT message FROM events WHERE event_type='error'").fetchall()
            == []
        )


def _assert_guid_dedup(pack: Pack) -> None:
    import grab_core

    # No client can be reached: the real guard must stop at persisted GUID evidence.
    assert not asyncio.run(
        grab_core.grab_item(
            {
                "title": TITLE,
                "url": "https://indexer.invalid/release?token=new",
                "guid": GUID,
                "protocol": pack.protocol,
            },
            1,
        )
    )


def test_skipped_commit_preserves_library_and_nonclaim_metadata(pack: Pack) -> None:
    import shared

    before = _snapshot()
    with shared.get_db() as db:
        seen_before = dict(
            db.execute("SELECT * FROM seen WHERE torrent_url=?", (SOURCE,)).fetchone()
        )
    original_bytes = pack.original.read_bytes()
    candidate_bytes = pack.candidate.read_bytes()
    _commit_skip(pack)
    after = _snapshot()
    assert after["series"] == before["series"]
    assert [r for r in after["volumes"] if r["id"] not in (2, 3)] == [
        r for r in before["volumes"] if r["id"] not in (2, 3)
    ]
    assert [r for r in after["chapters"] if r["id"] not in (20, 24, 30)] == [
        r for r in before["chapters"] if r["id"] not in (20, 24, 30)
    ]
    assert pack.original.read_bytes() == original_bytes
    assert pack.candidate.read_bytes() == candidate_bytes
    with shared.get_db() as db:
        assert (
            dict(
                db.execute(
                    "SELECT * FROM seen WHERE torrent_url=?", (SOURCE,)
                ).fetchone()
            )
            == seen_before
        )


@pytest.mark.parametrize("reason", ["quality", "operator", "lost_claim"])
def test_terminal_skip_releases_only_still_owned_unresolved_claims(
    pack: Pack, reason: str
) -> None:
    _commit_skip(pack, reason)
    state = _snapshot()
    owned_volumes = [
        r
        for r in state["volumes"]
        if r["id"] in ((3,) if reason == "lost_claim" else (2, 3))
    ]
    assert [
        (r["status"], r["download_id"], r["download_client_id"], r["import_path"])
        for r in owned_volumes
    ] == [("wanted", None, None, None)] * len(owned_volumes)
    owned_chapters = [r for r in state["chapters"] if r["id"] in (20, 24, 30)]
    assert [
        (r["status"], r["download_id"], r["download_client_id"], r["import_path"])
        for r in owned_chapters
    ] == [("wanted", None, None, None)] * 3
    if reason == "lost_claim":
        assert (
            next(r for r in state["volumes"] if r["id"] == 2)["download_client_id"] == 8
        )


@pytest.mark.parametrize("purge_history", [False, True])
def test_prospective_terminal_skip_client_removal_retains_seen_guid_dedup(
    prospective_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    purge_history: bool,
) -> None:
    import shared

    pack = prospective_pack
    _commit_skip(pack)
    _assert_guid_dedup(pack)
    with shared.get_db() as db:
        seen = dict(
            db.execute("SELECT * FROM seen WHERE torrent_url=?", (SOURCE,)).fetchone()
        )
        if purge_history:
            db.execute("DELETE FROM history")
    _poll_removed(pack, monkeypatch)
    _poll_removed(pack, monkeypatch)
    with shared.get_db() as db:
        row = db.execute("SELECT * FROM seen WHERE torrent_url=?", (SOURCE,)).fetchone()
        assert row is not None, (
            "handled acquisition lost URL/GUID dedup on independent client removal"
        )
        assert dict(row) == seen
    _assert_guid_dedup(pack)


def test_prospective_terminal_skip_client_removal_is_not_a_grab_failure(
    prospective_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shared

    pack = prospective_pack
    _commit_skip(pack)
    _poll_removed(pack, monkeypatch)
    _poll_removed(pack, monkeypatch)
    with shared.get_db() as db:
        assert (
            db.execute(
                "SELECT event_type FROM history WHERE series_id=1 AND event_type='grab_failed'"
            ).fetchall()
            == []
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM history WHERE event_type='import_skipped'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "status", ["pending", "partial", "importing", "lease", "publication"]
)
def test_skipped_commit_defers_until_final_terminal_sibling(
    pack: Pack, status: str
) -> None:
    import shared

    queue_id = _queue(pack)
    # Seed a pre-existing sibling; normal queue dedup prevents creating one now.
    with shared.get_db() as db:
        sibling_id = db.execute(
            "INSERT INTO import_queue(series_id,download_id,download_client_id,"
            " download_protocol,torrent_url,torrent_name,status) VALUES(1,?,7,?,?,?,?)",
            (
                pack.download_id,
                pack.protocol,
                SOURCE,
                TITLE,
                status if status in ("pending", "partial", "importing") else "failed",
            ),
        ).lastrowid
        assert sibling_id is not None
        if status == "lease":
            db.execute(
                "UPDATE import_queue SET lease_owner='other' WHERE id=?", (sibling_id,)
            )
        if status == "publication":
            db.execute(
                "INSERT INTO import_publications(queue_id,state,owner_token,series_id,"
                " dst_dir,import_mode,staging_dir,queue_snapshot_json,series_tags_json,queue_status)"
                " VALUES(?,'published','other',1,?,'copy',?,'{}','[]','importing')",
                (sibling_id, str(pack.original.parent), str(pack.root / "staging")),
            )
    import import_commit
    import import_lease
    import import_plan

    with shared.get_db() as db:
        assert import_lease.claim_import_queue_row(db, queue_id, "fixture-owner")
        plan = import_plan._plan_import(
            db, queue_id, "fixture-owner", {}, {}, set(), "copy", lease_seconds=300
        )
        assert plan is not None
        assert all(file.plan_status == "skip" for file in plan.files)
        assert import_commit._commit_import(
            db, plan, [], True, "", lease_owner="fixture-owner", lease_seconds=300
        ) == (True, 0, "imported")
    state = _snapshot()
    assert [r["status"] for r in state["volumes"] if r["id"] in (2, 3)] == [
        "grabbed"
    ] * 2
    assert [r["status"] for r in state["chapters"] if r["id"] in (20, 24, 30)] == [
        "grabbed"
    ] * 3
    with shared.get_db() as db:
        db.execute(
            "UPDATE import_publications SET state='finalized' WHERE queue_id=?",
            (sibling_id,),
        )
        db.execute(
            "UPDATE import_queue SET status='pending',lease_owner=NULL WHERE id=?",
            (sibling_id,),
        )
        db.execute(
            "INSERT INTO import_queue_files(queue_id,filename,src_path,proposed_volume,file_type,status)"
            " VALUES(?,?,?,1,'volume','pending')",
            (sibling_id, pack.candidate.name, str(pack.candidate)),
        )
    _commit_skip(pack, queue_id=sibling_id)
    final = _snapshot()
    assert [r["status"] for r in final["volumes"] if r["id"] in (2, 3)] == [
        "wanted"
    ] * 2
    assert [r["status"] for r in final["chapters"] if r["id"] in (20, 24, 30)] == [
        "wanted"
    ] * 3


def test_skipped_commit_with_lost_lease_does_not_settle_claims(pack: Pack) -> None:
    import import_commit
    import import_lease
    import import_plan
    import shared

    queue_id = _queue(pack)
    with shared.get_db() as db:
        assert import_lease.claim_import_queue_row(db, queue_id, "fixture-owner")
        plan = import_plan._plan_import(
            db, queue_id, "fixture-owner", {}, {}, set(), "copy", lease_seconds=300
        )
        assert plan is not None
        db.execute(
            "UPDATE import_queue SET lease_owner='successor' WHERE id=?", (queue_id,)
        )
    before = _snapshot()
    with shared.get_db() as db:
        assert import_commit._commit_import(
            db, plan, [], True, "", lease_owner="fixture-owner", lease_seconds=300
        ) == (False, 0, "lease_lost")
        assert (
            db.execute(
                "SELECT 1 FROM history WHERE event_type='import_skipped'"
            ).fetchone()
            is None
        )
    assert _snapshot() == before


@pytest.mark.parametrize(
    ("status", "partial_receipt"), [(None, False), ("failed", False), ("failed", True)]
)
def test_unhandled_or_failed_orphan_remains_retryable(
    pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    status: str | None,
    partial_receipt: bool,
) -> None:
    from events import add_history
    import shared

    queue_id = _queue(pack) if status else None
    with shared.get_db() as db:
        # Isolate retry semantics from independent cross-protocol/child conservation cases.
        db.execute("DELETE FROM chapters")
        db.execute("DELETE FROM volumes WHERE id NOT IN (1,2,3)")
        if queue_id is not None:
            db.execute(
                "UPDATE import_queue SET status='failed' WHERE id=?", (queue_id,)
            )
        if partial_receipt:
            # A previous partial batch imported one file; its next attempt failed.
            # History alone must not override that explicit unresolved outcome.
            add_history(
                db,
                "imported",
                1,
                "Owned Pack",
                "",
                protocol=pack.protocol,
                torrent_url=SOURCE,
                download_id=pack.download_id,
                download_client_id=7,
                data={"count": 1},
            )
    _poll_removed(pack, monkeypatch)
    _poll_removed(pack, monkeypatch)
    with shared.get_db() as db:
        assert [
            tuple(r)
            for r in db.execute(
                "SELECT status,download_id FROM volumes WHERE id IN (2,3)"
            )
        ] == [("wanted", None)] * 2
        assert (
            db.execute("SELECT 1 FROM seen WHERE torrent_url=?", (SOURCE,)).fetchone()
            is None
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM history WHERE event_type='grab_failed'"
            ).fetchone()[0]
            == 1
        )
        if queue_id is not None:
            assert (
                db.execute(
                    "SELECT status FROM import_queue WHERE id=?", (queue_id,)
                ).fetchone()[0]
                == "skipped"
            )


@pytest.mark.parametrize(
    "fence", ["pending", "partial", "importing", "lease", "publication"]
)
def test_client_removal_preserves_active_work_fences(
    pack: Pack, monkeypatch: pytest.MonkeyPatch, fence: str
) -> None:
    import shared

    queue_id = _queue(pack)
    with shared.get_db() as db:
        status = fence if fence in ("pending", "partial", "importing") else "failed"
        db.execute(
            "UPDATE import_queue SET status=?,lease_owner=? WHERE id=?",
            (status, "another-owner" if fence == "lease" else None, queue_id),
        )
        if fence == "publication":
            db.execute(
                "INSERT INTO import_publications(queue_id,state,owner_token,series_id,"
                " dst_dir,import_mode,staging_dir,queue_snapshot_json,series_tags_json,queue_status)"
                " VALUES(?,'published','another-owner',1,?,'copy',?,'{}','[]','importing')",
                (queue_id, str(pack.original.parent), str(pack.root / "staging")),
            )
    before = _snapshot()
    _poll_removed(pack, monkeypatch)
    assert _snapshot() == before
    with shared.get_db() as db:
        assert (
            db.execute(
                "SELECT status FROM import_queue WHERE id=?", (queue_id,)
            ).fetchone()[0]
            == status
        )
        assert (
            db.execute("SELECT 1 FROM seen WHERE torrent_url=?", (SOURCE,)).fetchone()
            is not None
        )


@pytest.mark.parametrize("difference", ["same", "case", "client", "protocol", "series"])
def test_terminal_receipt_uses_exact_identity(pack: Pack, difference: str) -> None:
    import import_queue
    import shared

    _commit_skip(pack)
    protocol: DownloadProtocol = pack.protocol
    if difference == "protocol":
        protocol = "nzb" if protocol == "torrent" else "torrent"
    identity = DownloadIdentity(
        8 if difference == "client" else 7,
        protocol,
        pack.download_id.swapcase() if difference == "case" else pack.download_id,
    )
    with shared.get_db() as db:
        found = import_queue._has_terminal_download_receipt(
            db,
            series_id=2 if difference == "series" else 1,
            torrent_url=SOURCE,
            identity=identity,
        )
    assert found is (
        difference == "same" or (difference == "case" and pack.protocol == "torrent")
    )


@pytest.mark.parametrize(
    ("table", "row_id"), [("volumes", 2), ("chapters", 20), ("chapters", 30)]
)
def test_path_bearing_claim_is_preserved_without_download_credit(
    pack: Pack,
    table: str,
    row_id: int,
) -> None:
    import shared

    with shared.get_db() as db:
        db.execute(
            f"UPDATE {table} SET import_path=?,quality='cbz',size_bytes=1234,"
            " imported_at='2024-01-01' WHERE id=?",
            (str(pack.original), row_id),
        )
    before = _snapshot()
    _commit_skip(pack)
    after = _snapshot()
    assert next(r for r in after[table] if r["id"] == row_id) == next(
        r for r in before[table] if r["id"] == row_id
    )
    assert next(r for r in after["volumes"] if r["id"] == 3)["status"] == "wanted"
    assert [r["id"] for r in after["volumes"] if r["status"] == "downloaded"] == [1]


@pytest.mark.parametrize(
    "mode", ["empty", "review", "prefailed", "stage_failed", "ready_uncommitted"]
)
def test_zero_imports_alone_do_not_authorize_new_settlement(
    pack: Pack, mode: str
) -> None:
    import import_commit
    from import_staging import _StageOutcome
    import shared

    plan = _plan_skip(pack)
    file = plan.files[0]
    outcomes: list[_StageOutcome] = []
    if mode == "empty":
        plan.files = []
    else:
        status = {"review": "needs_review", "prefailed": "pre_failed"}.get(
            mode, "ready"
        )
        plan.files = [
            replace(file, plan_status=status, plan_failure_reason="fixture failure")
        ]
        with shared.get_db() as db:
            db.execute(
                "UPDATE import_queue_files SET status=? WHERE id=?",
                ("needs_review" if mode == "review" else "pending", file.file_id),
            )
        if status == "ready":
            outcomes = [
                _StageOutcome(
                    file.file_id,
                    mode == "ready_uncommitted",
                    str(pack.candidate),
                    "fixture failure",
                )
            ]
    before = _snapshot()
    expected_status = (
        "partial"
        if mode == "review"
        else "failed"
        if mode in ("prefailed", "stage_failed")
        else "imported"
    )
    with shared.get_db() as db:
        result = import_commit._commit_import(
            db,
            plan,
            outcomes,
            False,
            "",
            lease_owner="fixture-owner",
            lease_seconds=300,
        )
        assert result == (expected_status != "failed", 0, expected_status)
        if expected_status != "imported":
            assert (
                db.execute(
                    "SELECT 1 FROM history WHERE event_type='import_skipped'"
                ).fetchone()
                is None
            )
    after = _snapshot()
    assert after["chapters"] == before["chapters"]
    # Existing failed-import volume reset is intentionally not changed by this PR.
    if expected_status != "failed":
        assert after == before


@pytest.mark.parametrize("second_status", ["needs_review", "pre_failed"])
def test_partial_batch_does_not_apply_zero_import_settlement(
    pack: Pack, second_status: str
) -> None:
    import import_commit
    from import_staging import _StageOutcome
    import shared

    plan = _plan_skip(pack)
    destination = pack.original.parent / "Owned Pack v02.cbz"
    _archive(destination, b"published second volume")
    with shared.get_db() as db:
        second_id = db.execute(
            "INSERT INTO import_queue_files(queue_id,filename,status) VALUES(?,'review.cbz',?)",
            (
                plan.queue["id"],
                "needs_review" if second_status == "needs_review" else "pending",
            ),
        ).lastrowid
    assert second_id is not None
    first = replace(
        plan.files[0], plan_status="ready", proposed_vol=2, dst_path=str(destination)
    )
    plan.files = [
        first,
        replace(
            first,
            file_id=second_id,
            plan_status=second_status,
            plan_failure_reason="fixture failure",
        ),
    ]
    before = _snapshot()
    with shared.get_db() as db:
        assert import_commit._commit_import(
            db,
            plan,
            [_StageOutcome(first.file_id, True, str(destination), "")],
            True,
            "",
            lease_owner="fixture-owner",
            lease_seconds=300,
        ) == (second_status != "pre_failed", 1, "partial")
    after = _snapshot()
    for table, row_id in (("volumes", 3), ("chapters", 30)):
        assert next(r for r in after[table] if r["id"] == row_id) == next(
            r for r in before[table] if r["id"] == row_id
        )


@pytest.mark.parametrize("field", ["download_client_id", "download_id", "torrent_url"])
def test_unproven_queue_identity_does_not_release_claims(
    pack: Pack, field: str
) -> None:
    import import_commit
    import shared

    plan = _plan_skip(pack)
    value = None if field == "download_client_id" else ""
    plan.queue[field] = value
    with shared.get_db() as db:
        db.execute(
            f"UPDATE import_queue SET {field}=? WHERE id=?", (value, plan.queue["id"])
        )
    before = _snapshot()
    with shared.get_db() as db:
        assert import_commit._commit_import(
            db, plan, [], True, "", lease_owner="fixture-owner", lease_seconds=300
        ) == (True, 0, "imported")
    assert _snapshot() == before


@pytest.mark.parametrize(
    "evidence", ["null_protocol", "invalid_protocol", "other_case"]
)
def test_settlement_uses_existing_exact_acquisition_proof(
    pack: Pack, evidence: str
) -> None:
    import shared

    with shared.get_db() as db:
        if evidence == "other_case":
            db.execute(
                "UPDATE volumes SET download_id=? WHERE id=3",
                (pack.download_id.swapcase(),),
            )
        else:
            db.execute(
                "UPDATE volumes SET protocol=? WHERE id=3",
                (None if evidence == "null_protocol" else "invalid",),
            )
        before = dict(db.execute("SELECT * FROM volumes WHERE id=3").fetchone())
    _commit_skip(pack)
    after = next(r for r in _snapshot()["volumes"] if r["id"] == 3)
    if evidence == "null_protocol" or (
        evidence == "other_case" and pack.protocol == "torrent"
    ):
        assert after["status"] == "wanted"
    else:
        assert after == before


@pytest.mark.parametrize("authority", ["published", "lost", "admission_changed"])
def test_publication_authority_fences_terminal_skip_settlement(
    pack: Pack, authority: str
) -> None:
    import import_commit
    import import_publication
    import shared

    plan = _plan_skip(pack)
    with shared.get_db() as db:
        publication_id = import_publication.create_publication(
            db, plan, "fixture-owner", str(pack.root / "staging"), {}
        )
        db.execute(
            "UPDATE import_publications SET state='published' WHERE id=?",
            (publication_id,),
        )
        if authority == "lost":
            db.execute(
                "UPDATE import_publications SET operation_owner='successor',operation_expires_at=datetime('now','+5 minutes') WHERE id=?",
                (publication_id,),
            )
        if authority == "admission_changed":
            db.execute(
                "UPDATE import_queue SET respect_grab_claims=0 WHERE id=?",
                (plan.queue["id"],),
            )
        # A published journal, not the expired queue lease, is Phase 3 authority.
        db.execute(
            "UPDATE import_queue SET lease_expires_at=datetime('now','-1 hour') WHERE id=?",
            (plan.queue["id"],),
        )
    before = _snapshot()
    with shared.get_db() as db:
        intents = []
        result = import_commit._commit_import(
            db,
            plan,
            [],
            True,
            "",
            lease_owner="fixture-owner",
            lease_seconds=300,
            publication_id=publication_id,
            post_commit_intents=intents,
        )
        assert intents == []
        if authority == "published":
            assert result == (True, 0, "imported")
            assert tuple(
                db.execute(
                    "SELECT state,result_imported_count,notification_state FROM import_publications WHERE id=?",
                    (publication_id,),
                ).fetchone()
            ) == ("db_committed", 0, "none")
            assert (
                db.execute(
                    "SELECT 1 FROM import_publication_notifications WHERE publication_id=?",
                    (publication_id,),
                ).fetchone()
                is None
            )
        else:
            assert result == (
                False,
                0,
                "journal_claim_lost" if authority == "lost" else "admission_changed",
            )
    after = _snapshot()
    if authority == "published":
        assert [r["status"] for r in after["volumes"] if r["id"] in (2, 3)] == [
            "wanted"
        ] * 2
        assert [r["status"] for r in after["chapters"] if r["id"] in (20, 24, 30)] == [
            "wanted"
        ] * 3
    else:
        assert after == before


@pytest.mark.parametrize("reason", ["operator", "lost_claim"])
def test_terminal_skip_does_not_claim_library_satisfaction(
    pack: Pack, reason: str
) -> None:
    import shared

    _commit_skip(pack, reason)
    with shared.get_db() as db:
        messages = [
            r[0]
            for r in db.execute("SELECT message FROM events WHERE event_type='import'")
        ]
        assert messages and all(
            "already satisfied" not in message for message in messages
        )
        assert (
            db.execute("SELECT 1 FROM history WHERE event_type='imported'").fetchone()
            is None
        )
        assert (
            db.execute(
                "SELECT 1 FROM events WHERE event_type='download_complete'"
            ).fetchone()
            is None
        )


def test_late_successor_claims_survive_terminal_settlement(pack: Pack) -> None:
    import import_commit
    import shared

    plan = _plan_skip(pack)
    with shared.get_db() as db:
        db.execute(
            "UPDATE volumes SET download_client_id=8,source_url='https://successor.invalid' WHERE id=2"
        )
        db.execute(
            "UPDATE chapters SET download_client_id=8,torrent_url='https://successor.invalid' WHERE id=30"
        )
    before = _snapshot()
    with shared.get_db() as db:
        assert import_commit._commit_import(
            db, plan, [], True, "", lease_owner="fixture-owner", lease_seconds=300
        ) == (True, 0, "imported")
    after = _snapshot()
    for table, row_id in (("volumes", 2), ("chapters", 30)):
        assert next(r for r in after[table] if r["id"] == row_id) == next(
            r for r in before[table] if r["id"] == row_id
        )
    assert next(r for r in after["volumes"] if r["id"] == 3)["status"] == "wanted"


def test_settled_claim_retains_non_acquisition_metadata(pack: Pack) -> None:
    import shared

    values = {
        "title": "Manual label",
        "edition_type": "light_novel",
        "language": "en",
        "quality": "cbz",
        "size_bytes": 1234,
        "imported_at": "2024-01-01",
        "monitored": 0,
    }
    with shared.get_db() as db:
        db.execute(
            "UPDATE volumes SET title=?,edition_type=?,language=?,quality=?,size_bytes=?,imported_at=?,monitored=? WHERE id=3",
            tuple(values.values()),
        )
    _commit_skip(pack)
    row = next(r for r in _snapshot()["volumes"] if r["id"] == 3)
    assert {key: row[key] for key in values} == values
    assert row["status"] == "wanted" and row["download_id"] is None
