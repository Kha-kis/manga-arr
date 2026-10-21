"""Real writer -> durable block -> fresh observed admission and client effects."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from starlette.requests import Request

import grab_core
import import_discovery
import main
import shared
from clients import GrabResult
from release_identity import add_block, identity_for, is_blocked
from routers import blocklist_, history_, queue_
from test_blocklist_source_flow import Flow, flow as flow


@dataclass
class Writers:
    flow: Flow
    generation: int = 0
    removed: int = 0
    title: str = "Test Series Vol 1"

    def item(self) -> dict[str, Any]:
        return self.flow.search()[0]

    def reset_acquisition(self) -> None:
        with shared.get_db() as db:
            db.execute("DELETE FROM seen")
            db.execute(
                "UPDATE volumes SET status='wanted',download_id=NULL,download_client_id=NULL,source_url=NULL,grabbed_at=NULL"
            )
        self.flow.submitted.clear()

    def acquire(self) -> dict[str, Any]:
        item = self.item()
        assert asyncio.run(grab_core.grab_item(item, 1))
        with shared.get_db() as db:
            row = db.execute("SELECT * FROM seen").fetchone()
            assert row is not None
            assert (row["release_guid"], row["download_client_id"]) == (
                "Release-GUID",
                7,
            )
            assert row["release_source_scope"] == item["_source_context"].source_scope
        return item

    def write(self, producer: str, item: dict[str, Any]) -> None:
        request = Request(
            {"type": "http", "method": "POST", "path": "/queue", "headers": []}
        )
        if producer == "manual":
            asyncio.run(
                blocklist_.add_to_blocklist(
                    series_id=1,
                    torrent_url=item["url"],
                    torrent_name=item["title"],
                    reason="Manual",
                )
            )
        elif producer == "history":
            with shared.get_db() as db:
                history_id = db.execute(
                    "SELECT id FROM history WHERE event_type='grabbed' ORDER BY id DESC LIMIT 1"
                ).fetchone()[0]
            assert history_.mark_history_failed(history_id)["ok"]
        elif producer == "queue-remove":
            asyncio.run(
                queue_._remove_from_queue(
                    request,
                    "flow-hash",
                    download_client_id=7,
                    remove_from_client="0",
                    delete_files="0",
                    blocklist="1",
                    change_category="",
                )
            )
        elif producer == "queue-block":
            asyncio.run(
                queue_._block_and_remove(
                    request, "flow-hash", download_client_id=7, delete_files="1"
                )
            )
        else:
            assert producer == "automatic"
            shared.CONFIG.update(
                failed_download_handling="1", redownload_failed_interactive="1"
            )
            asyncio.run(
                import_discovery._poll_qbit_partition(
                    import_discovery._ClientPollPartition(
                        7, "Writer qBit", "qbittorrent", False
                    ),
                    {
                        "host": "http://qbit.test",
                        "name": "Writer qBit",
                        "remove_failed": True,
                    },
                )
            )


@pytest.fixture
def writers(flow: Flow, monkeypatch: pytest.MonkeyPatch) -> Writers:
    state = Writers(flow)
    flow.create()
    flow.result = GrabResult(True, "qbittorrent", "flow-hash", True, 7)
    with shared.get_db() as db:
        db.execute(
            "INSERT INTO download_clients(id,name,type,host,enabled) VALUES(7,'Writer qBit','qbittorrent','http://qbit.test',1),(8,'Other qBit','qbittorrent','http://other.test',1)"
        )
    original = flow.transport

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.host == "qbit.test":
            if request.url.path.endswith("/auth/login"):
                return httpx.Response(200, text="Ok.")
            assert request.url.path.endswith("/torrents/info")
            return httpx.Response(
                200,
                json=[
                    {
                        "hash": "flow-hash",
                        "state": "error",
                        "progress": 0.1,
                        "name": "Test Series Vol 1",
                    }
                ],
            )
        response = original(request)
        return httpx.Response(
            200,
            text=response.text.replace(
                "</link>", f"?rotation={state.generation}</link>"
            ).replace("Test Series Vol 1</title>", state.title + "</title>"),
        )

    async def remove(*args: Any, **kwargs: Any) -> bool:
        state.removed += 1
        # This would fail if a writer reservation crossed the client boundary.
        with shared.get_db() as db:
            db.execute("UPDATE settings SET value=value WHERE key='api_key'")
        return True

    def schedule(coro: Any, **kwargs: Any) -> None:
        coro.close()

    import clients

    monkeypatch.setattr(flow, "transport", transport)
    monkeypatch.setattr(main, "qbit_remove", remove)
    monkeypatch.setattr(clients, "qbit_remove", remove)
    monkeypatch.setattr(main, "create_background_task", schedule)
    return state


@pytest.mark.parametrize("respect_monitoring", [True, False])
@pytest.mark.parametrize(
    "producer", ["manual", "history", "queue-remove", "queue-block", "automatic"]
)
def test_real_writer_blocks_known_guid_rotated_url(
    writers: Writers, producer: str, respect_monitoring: bool
) -> None:
    old = writers.acquire()
    writers.write(producer, old)
    with shared.get_db() as db:
        assert db.execute(
            "SELECT 1 FROM blocklist WHERE torrent_url=?", (old["url"],)
        ).fetchone()
        if producer != "manual":
            assert db.execute("SELECT count(*) FROM seen").fetchone()[0] == 0
    writers.reset_acquisition()
    writers.generation = 1
    fresh = writers.item()
    assert fresh["url"] != old["url"] and fresh["guid"] == old["guid"]
    accepted = asyncio.run(
        grab_core.grab_item(fresh, 1, respect_monitoring=respect_monitoring)
    )
    with shared.get_db() as db:
        seen = [dict(row) for row in db.execute("SELECT * FROM seen")]
        identities = [
            tuple(row)
            for row in db.execute(
                "SELECT series_id,source_scope,protocol,release_guid FROM blocklist_release_identities"
            )
        ]
    assert (accepted, writers.flow.submitted, seen) == (False, [], []), (
        "rotated observed release reached the real downloader boundary"
    )
    assert identities == [
        (1, old["_source_context"].source_scope, "torrent", "Release-GUID")
    ]
    assert writers.removed == (1 if producer in ("queue-block", "automatic") else 0)


@pytest.mark.parametrize("respect_monitoring", [True, False])
@pytest.mark.parametrize("kind", ["url", "identity"])
def test_block_inserted_after_early_admission_is_checked_at_handoff(
    writers: Writers,
    monkeypatch: pytest.MonkeyPatch,
    respect_monitoring: bool,
    kind: str,
) -> None:
    item = writers.item()
    original = grab_core.score_release
    inserted = False

    def score(*args: Any, **kwargs: Any) -> Any:
        nonlocal inserted
        if not inserted:
            inserted = True
            with shared.get_db() as db:
                identity = identity_for(item, 1)
                assert identity is not None
                add_block(
                    db,
                    series_id=1,
                    url=item["url"] if kind == "url" else "https://old.test/anchor",
                    title="blocked",
                    reason="intervening writer",
                    indexer=None,
                    protocol=None,
                    size_bytes=None,
                    identities=[identity] if kind == "identity" else [],
                )
        return original(*args, **kwargs)

    monkeypatch.setattr(grab_core, "score_release", score)
    accepted = asyncio.run(
        grab_core.grab_item(item, 1, respect_monitoring=respect_monitoring)
    )
    assert inserted
    with shared.get_db() as db:
        seen = list(db.execute("SELECT * FROM seen"))
    assert (accepted, writers.flow.submitted, seen) == (False, [], [])


@pytest.mark.parametrize("producer", ["queue-remove", "queue-block"])
def test_queue_conserves_every_owned_row_not_colliding_owner(
    writers: Writers, producer: str
) -> None:
    item = writers.acquire()
    scope = item["_source_context"].source_scope
    with shared.get_db() as db:
        db.execute(
            "INSERT INTO seen(torrent_url,series_id,torrent_name,download_id,download_client_id,client,protocol,release_guid,release_source_scope) VALUES('https://second.test',1,'second','FLOW-HASH',7,'qbittorrent','torrent','Second-GUID',?),('https://foreign.test',1,'foreign','flow-hash',8,'qbittorrent','torrent','Foreign-GUID',?)",
            (scope, scope),
        )
    writers.write(producer, item)
    with shared.get_db() as db:
        assert {row[0] for row in db.execute("SELECT torrent_url FROM blocklist")} == {
            item["url"],
            "https://second.test",
        }
        assert {
            row[0]
            for row in db.execute(
                "SELECT release_guid FROM blocklist_release_identities"
            )
        } == {"Release-GUID", "Second-GUID"}
        assert [row[0] for row in db.execute("SELECT torrent_url FROM seen")] == [
            "https://foreign.test"
        ]


@pytest.mark.parametrize(
    "producer", ["history", "queue-remove", "queue-block", "automatic"]
)
@pytest.mark.parametrize("fence", ["importing", "lease", "publication"])
def test_denied_cleanup_creates_no_block(
    writers: Writers, producer: str, fence: str
) -> None:
    item = writers.acquire()
    with shared.get_db() as db:
        db.execute(
            "INSERT INTO import_queue(id,series_id,download_id,download_client_id,download_protocol,torrent_url,status,lease_owner) VALUES(90,1,'flow-hash',7,'torrent',?,?,?)",
            (
                item["url"],
                "importing" if fence == "importing" else "failed",
                "worker" if fence == "lease" else None,
            ),
        )
        if fence == "publication":
            db.execute(
                "INSERT INTO import_publications(queue_id,series_id,state,owner_token,dst_dir,import_mode,staging_dir,queue_snapshot_json,series_tags_json,queue_status) VALUES(90,1,'prepared','writer-owner','/private/dst','copy','/private/stage','{}','[]','failed')"
            )
        before = dict(db.execute("SELECT * FROM seen").fetchone())
    if producer == "history":
        with shared.get_db() as db:
            hid = db.execute(
                "SELECT id FROM history WHERE event_type='grabbed'"
            ).fetchone()[0]
        assert history_.mark_history_failed(hid) == {
            "ok": False,
            "status": "in_progress",
        }
    else:
        writers.write(producer, item)
    with shared.get_db() as db:
        assert db.execute("SELECT count(*) FROM blocklist").fetchone()[0] == 0
        assert (
            db.execute("SELECT count(*) FROM blocklist_release_identities").fetchone()[
                0
            ]
            == 0
        )
        assert dict(db.execute("SELECT * FROM seen").fetchone()) == before
    assert writers.removed == 0


@pytest.mark.parametrize("evidence", ["valid", "absent", "invalid"])
@pytest.mark.parametrize(
    "difference", ["none", "owner", "protocol", "id", "series", "url", "ownerless"]
)
def test_history_tri_state_and_exact_fallback(
    writers: Writers, evidence: str, difference: str
) -> None:
    item = writers.acquire()
    with shared.get_db() as db:
        hid = db.execute(
            "SELECT id FROM history WHERE event_type='grabbed'"
        ).fetchone()[0]
        if evidence != "valid":
            db.execute(
                "UPDATE history SET data=? WHERE id=?",
                (
                    None
                    if evidence == "absent"
                    else json.dumps({"release_identity": None}),
                    hid,
                ),
            )
        if difference == "owner":
            db.execute("UPDATE seen SET download_client_id=8")
        elif difference == "protocol":
            db.execute("UPDATE seen SET protocol='nzb'")
        elif difference == "id":
            db.execute("UPDATE seen SET download_id='other'")
        elif difference == "series":
            db.execute(
                "INSERT INTO series(id,title,search_pattern) VALUES(2,'Other','Other')"
            )
            db.execute("UPDATE seen SET series_id=2")
        elif difference == "url":
            db.execute("UPDATE seen SET torrent_url='https://other.test'")
        elif difference == "ownerless":
            db.execute("UPDATE history SET download_client_id=NULL WHERE id=?", (hid,))
        if evidence == "valid":
            db.execute("DELETE FROM seen")
    assert history_.mark_history_failed(hid)["ok"]
    with shared.get_db() as db:
        identities = list(db.execute("SELECT * FROM blocklist_release_identities"))
        assert len(identities) == int(
            evidence == "valid" or (evidence == "absent" and difference == "none")
        )
        assert (
            db.execute("SELECT torrent_url FROM blocklist").fetchone()[0] == item["url"]
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("release_guid", "changed"),
        ("release_source_scope", "bs1:d:" + "b" * 64),
        ("protocol", "nzb"),
        ("download_client_id", 8),
        ("download_id", "other"),
        ("torrent_url", "https://changed.test"),
        ("series_id", 2),
    ],
)
def test_automatic_refuses_changed_polled_row(
    writers: Writers, monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    item = writers.acquire()
    with shared.get_db() as db:
        db.execute(
            "INSERT INTO series(id,title,search_pattern) VALUES(2,'Other','Other')"
        )
    original = asyncio.to_thread
    changed = False

    async def barrier(func: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal changed
        if func.__name__ == "_mark_failed":
            assert not changed
            changed = True
            with shared.get_db() as db:
                db.execute(f"UPDATE seen SET {field}=?", (value,))
        return await original(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", barrier)
    writers.write("automatic", item)
    assert changed
    with shared.get_db() as db:
        assert db.execute("SELECT count(*) FROM blocklist").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM seen").fetchone()[0] == 1
        assert (
            db.execute("SELECT status FROM volumes WHERE id=1").fetchone()[0]
            == "grabbed"
        )
    assert writers.removed == 0


@pytest.mark.parametrize(
    "surface", ["search", "rss-delay", "backlog", "volume", "volume-sync"]
)
def test_real_admission_rejects_identity_before_scheduling(
    writers: Writers, monkeypatch: pytest.MonkeyPatch, surface: str
) -> None:
    item = writers.acquire()
    writers.write("manual", item)
    writers.reset_acquisition()
    writers.generation = 1
    if surface == "search":
        response = writers.flow.client.get(
            "/api/series/1/volumes/1/search", headers=writers.flow.headers
        )
        assert response.status_code == 200
        result = response.json()["results"][0]
        assert result["blocked"] is True
        assert "selection_receipt" not in result
    elif surface == "rss-delay":
        import grab_rss

        shared.CONFIG["grab_delay_minutes"] = "60"
        asyncio.run(grab_rss.poll_rss())
        with shared.get_db() as db:
            assert (
                db.execute("SELECT count(*) FROM pending_releases").fetchone()[0] == 0
            )
    else:
        import grab_backlog
        from routers import series_

        calls: list[str] = []
        original = grab_core.grab_item

        async def record(
            candidate: dict[str, Any], series_id: int, **kwargs: Any
        ) -> bool:
            calls.append(candidate["url"])
            return await original(candidate, series_id, **kwargs)

        monkeypatch.setattr(grab_backlog, "grab_item", record)
        monkeypatch.setattr(main, "grab_item", record)
        if surface == "backlog":
            assert (
                asyncio.run(grab_backlog.grab_existing(1, "Test Series", "Test Series"))
                == 0
            )
        else:
            with shared.get_db() as db:
                series = dict(db.execute("SELECT * FROM series WHERE id=1").fetchone())
                volume = dict(db.execute("SELECT * FROM volumes WHERE id=1").fetchone())
            task = (
                series_._grab_volume_task_sync
                if surface == "volume-sync"
                else series_._grab_volume_task
            )
            asyncio.run(task(1, series, volume, "Test Series"))
        assert calls == []
    assert writers.flow.submitted == []


@pytest.mark.parametrize("respect_monitoring", [True, False])
@pytest.mark.parametrize(
    "difference",
    ["direct-source", "sibling", "protocol", "series", "guid-case", "rename"],
)
def test_actual_observation_boundaries(
    writers: Writers, difference: str, respect_monitoring: bool
) -> None:
    flow = writers.flow
    if difference in ("sibling", "protocol"):
        with shared.get_db() as db:
            db.execute("DELETE FROM indexers")
        flow.create("prowlarr")
        flow.subs = [{"id": 17, "name": "Same label", "protocol": "torrent"}]
    old = writers.acquire()
    writers.write("manual", old)
    writers.reset_acquisition()
    writers.generation = 1
    target_series = 1
    if difference == "direct-source":
        with shared.get_db() as db:
            db.execute("UPDATE indexers SET enabled=0")
        flow.create()
    elif difference == "sibling":
        flow.subs = [{"id": 23, "name": "Same label", "protocol": "torrent"}]
    elif difference == "protocol":
        flow.subs = [{"id": 17, "name": "Same label", "protocol": "usenet"}]
    elif difference == "series":
        target_series = 2
        with shared.get_db() as db:
            db.execute(
                "INSERT INTO series(id,title,search_pattern,monitored) VALUES(2,'Test Series','Test Series',1)"
            )
            db.execute(
                "INSERT INTO volumes(id,series_id,volume_num,status,monitored) VALUES(2,2,1,'wanted',1)"
            )
    elif difference == "guid-case":
        flow.guid = "release-guid"
    else:
        flow.edit(old["_source_context"].indexer_id, name="Renamed source")
    candidate = flow.search(series_id=target_series)[0]
    accepted = asyncio.run(
        grab_core.grab_item(
            candidate, target_series, respect_monitoring=respect_monitoring
        )
    )
    blocked = difference == "rename"
    assert accepted is not blocked
    assert flow.submitted == ([] if blocked else [candidate["url"]])
    with shared.get_db() as db:
        assert db.execute("SELECT count(*) FROM seen").fetchone()[0] == int(not blocked)
    if blocked:
        assert candidate["indexer"] != old["indexer"]
        assert (
            candidate["_source_context"].source_scope
            == old["_source_context"].source_scope
        )


@pytest.mark.parametrize(
    "guid",
    [None, 17, "", " \t ", "x" * 4097],
    ids=["null", "integer", "empty", "blank", "oversized"],
)
def test_unavailable_seen_guid_only_blocks_exact_url(
    writers: Writers, guid: object
) -> None:
    item = writers.item()
    item["guid"] = guid
    assert asyncio.run(grab_core.grab_item(item, 1))
    writers.write("manual", item)
    writers.reset_acquisition()
    assert not asyncio.run(grab_core.grab_item(item, 1))
    writers.generation = 1
    candidate = writers.item()
    assert asyncio.run(grab_core.grab_item(candidate, 1))
    assert writers.flow.submitted == [candidate["url"]]
    with shared.get_db() as db:
        assert (
            db.execute("SELECT count(*) FROM blocklist_release_identities").fetchone()[
                0
            ]
            == 0
        )


@pytest.mark.parametrize("requested_series", [0, 2])
def test_manual_requires_exact_positive_series(
    writers: Writers, requested_series: int
) -> None:
    item = writers.acquire()
    with shared.get_db() as db:
        db.execute(
            "INSERT INTO series(id,title,search_pattern) VALUES(2,'Other','Other')"
        )
    asyncio.run(
        blocklist_.add_to_blocklist(
            series_id=requested_series,
            torrent_url=item["url"],
            torrent_name=item["title"],
            reason="Manual",
        )
    )
    with shared.get_db() as db:
        assert db.execute("SELECT count(*) FROM blocklist").fetchone()[0] == 1
        assert (
            db.execute("SELECT count(*) FROM blocklist_release_identities").fetchone()[
                0
            ]
            == 0
        )


@pytest.mark.parametrize(
    "action", ["delete", "clear", "owner-series", "child-series", "ttl", "ttl-disabled"]
)
def test_real_writer_repeated_anchor_and_lifecycle(
    writers: Writers, action: str
) -> None:
    item = writers.acquire()
    writers.write("manual", item)
    with shared.get_db() as db:
        db.execute("UPDATE blocklist SET added_at='2000-01-01'")
        before = dict(db.execute("SELECT * FROM blocklist").fetchone())
        db.execute(
            "INSERT INTO series(id,title,search_pattern) VALUES(2,'Other','Other')"
        )
        db.execute("UPDATE seen SET series_id=2,release_guid='Second-GUID'")
    asyncio.run(
        blocklist_.add_to_blocklist(
            series_id=2,
            torrent_url=item["url"],
            torrent_name="replacement",
            reason="replacement",
        )
    )
    with shared.get_db() as db:
        db.execute("UPDATE seen SET release_source_scope=NULL")
    asyncio.run(
        blocklist_.add_to_blocklist(
            series_id=2,
            torrent_url=item["url"],
            torrent_name="unknown",
            reason="unknown",
        )
    )
    identity = identity_for(item, 1)
    assert identity is not None
    with shared.get_db() as db:
        assert dict(db.execute("SELECT * FROM blocklist").fetchone()) == before
        assert {
            row[0]
            for row in db.execute(
                "SELECT release_guid FROM blocklist_release_identities"
            )
        } == {"Release-GUID", "Second-GUID"}
        assert is_blocked(db, url="rotated", identity=identity)
    if action == "delete":
        request = Request({"type": "http", "headers": []})
        asyncio.run(blocklist_.delete_blocklist_entry(request, before["id"]))
    elif action == "clear":
        assert blocklist_.clear_blocklist_entries()["deleted"] == 1
    elif action in ("owner-series", "child-series"):
        with shared.get_db() as db:
            db.execute(
                "DELETE FROM series WHERE id=?", (1 if action == "owner-series" else 2,)
            )
    else:
        shared.CONFIG.update(
            blocklist_ttl_days="0" if action == "ttl-disabled" else "90"
        )
        asyncio.run(import_discovery._check_download_status_impl())
    with shared.get_db() as db:
        retained = action in ("child-series", "ttl-disabled")
        assert bool(db.execute("SELECT 1 FROM blocklist").fetchone()) is retained
        assert is_blocked(db, url="rotated", identity=identity) is retained
        assert db.execute(
            "SELECT count(*) FROM blocklist_release_identities"
        ).fetchone()[0] == (1 if action == "child-series" else 2 if retained else 0)


def test_automatic_conserves_additional_owned_rows(writers: Writers) -> None:
    item = writers.acquire()
    with shared.get_db() as db:
        db.execute(
            "INSERT INTO seen(torrent_url,series_id,torrent_name,download_id,download_client_id,client,protocol,release_guid,release_source_scope) SELECT 'https://second.test',series_id,'Second',download_id,download_client_id,client,protocol,'Second-GUID',release_source_scope FROM seen"
        )
    writers.write("automatic", item)
    with shared.get_db() as db:
        assert {row[0] for row in db.execute("SELECT torrent_url FROM blocklist")} == {
            item["url"],
            "https://second.test",
        }
        assert {
            row[0]
            for row in db.execute(
                "SELECT release_guid FROM blocklist_release_identities"
            )
        } == {"Release-GUID", "Second-GUID"}
        assert db.execute("SELECT count(*) FROM seen").fetchone()[0] == 0


def test_changed_automatic_row_rolls_back_reservation_transitions(
    writers: Writers, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = writers.acquire()
    with shared.get_db() as db:
        db.execute(
            "INSERT INTO import_queue(id,series_id,download_id,download_client_id,download_protocol,torrent_url,status) VALUES(90,1,'flow-hash',7,'torrent',?,'failed')",
            (item["url"],),
        )
        before = dict(db.execute("SELECT * FROM import_queue").fetchone())
    original = asyncio.to_thread

    async def barrier(func: Any, *args: Any, **kwargs: Any) -> Any:
        if func.__name__ == "_mark_failed":
            with shared.get_db() as db:
                db.execute("UPDATE seen SET release_guid='replacement'")
        return await original(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", barrier)
    writers.write("automatic", item)
    with shared.get_db() as db:
        assert dict(db.execute("SELECT * FROM import_queue").fetchone()) == before
        assert db.execute("SELECT count(*) FROM blocklist").fetchone()[0] == 0
    assert writers.removed == 0


def test_complete_pack_admission_uses_observation(
    writers: Writers, monkeypatch: pytest.MonkeyPatch
) -> None:
    import grab_backlog

    writers.title = "Test Series Complete"
    item = writers.acquire()
    writers.write("manual", item)
    writers.reset_acquisition()
    writers.generation = 1
    calls: list[str] = []
    original = grab_core.grab_item

    async def record(candidate: dict[str, Any], series_id: int) -> bool:
        calls.append(candidate["url"])
        return await original(candidate, series_id)

    monkeypatch.setattr(grab_backlog, "grab_item", record)
    assert asyncio.run(grab_backlog.search_complete_pack(1, "Test Series", 1)) == 0
    assert calls == [] and writers.flow.submitted == []


def test_chapter_admission_uses_observation(
    writers: Writers, monkeypatch: pytest.MonkeyPatch
) -> None:
    from routers import series_

    writers.title = "Test Series chapter 1"
    shared.CONFIG["ddl_grab_mode"] = "off"
    with shared.get_db() as db:
        db.execute(
            "INSERT INTO chapters(id,series_id,chapter_num,status,monitored) VALUES(1,1,1,'wanted',1)"
        )
    item = writers.acquire()
    writers.write("manual", item)
    writers.reset_acquisition()
    writers.generation = 1
    calls: list[str] = []
    original = grab_core.grab_item

    async def record(candidate: dict[str, Any], series_id: int, **kwargs: Any) -> bool:
        calls.append(candidate["url"])
        return await original(candidate, series_id, **kwargs)

    monkeypatch.setattr(main, "grab_item", record)
    with shared.get_db() as db:
        series = dict(db.execute("SELECT * FROM series WHERE id=1").fetchone())
        chapter = dict(db.execute("SELECT * FROM chapters WHERE id=1").fetchone())
    asyncio.run(series_._grab_chapter_task(1, series, chapter))
    assert calls == [] and writers.flow.submitted == []


@pytest.mark.parametrize(
    "missing", ["protocol", "download_id", "torrent_url", "download_client_id"]
)
def test_history_fallback_needs_positive_outer_download_evidence(
    writers: Writers, missing: str
) -> None:
    writers.acquire()
    with shared.get_db() as db:
        hid = db.execute(
            "SELECT id FROM history WHERE event_type='grabbed'"
        ).fetchone()[0]
        db.execute(f"UPDATE history SET data=NULL,{missing}=NULL WHERE id=?", (hid,))
    assert history_.mark_history_failed(hid)["ok"]
    with shared.get_db() as db:
        assert (
            db.execute("SELECT count(*) FROM blocklist_release_identities").fetchone()[
                0
            ]
            == 0
        )
