"""Real search/admission connection counts and candidate-local block evidence."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
import sqlite3
from typing import Any
from xml.sax.saxutils import escape

import httpx
import pytest

import grab_backlog
import grab_core
import main
import shared
from release_identity import SourceContext, add_block, identity_for
from routers import series_
from test_blocklist_source_flow import Flow, flow as flow

SURFACES = ("backlog", "gap", "volume", "volume-sync", "chapter")


@dataclass
class Reads:
    connections: list[sqlite3.Connection] = field(default_factory=list)
    block_reads: list[int] = field(default_factory=list)
    active: int = 0

    @contextmanager
    def get_db(self) -> Generator[sqlite3.Connection, None, None]:
        with shared.get_db() as db:
            index = len(self.connections)
            self.connections.append(db)
            self.block_reads.append(0)
            self.active += 1

            def trace(sql: str) -> None:
                if "FROM blocklist" in sql:
                    self.block_reads[index] += 1

            db.set_trace_callback(trace)
            try:
                yield db
            finally:
                db.set_trace_callback(None)
                self.active -= 1

    def assert_closed(self) -> None:
        assert self.active == 0
        for db in self.connections:
            with pytest.raises(sqlite3.ProgrammingError):
                db.execute("SELECT 1")


@dataclass
class Search:
    flow: Flow
    count: int = 1
    chapter: bool = False
    gap_only: bool = False

    def transport(self, request: httpx.Request) -> httpx.Response:
        self.flow.requests.append(request)
        query = request.url.params.get("q", "")
        count = self.count if not self.gap_only or " vol " in query else 0
        title = "Test Series chapter 1" if self.chapter else "Test Series Vol 1"
        items = "".join(
            "<item><title>" + escape(title) + "</title>"
            f"<link>https://batch.test/{i}</link><guid>Batch-GUID-{i}</guid>"
            '<torznab:attr name="seeders" value="10"/>'
            '<torznab:attr name="size" value="1024"/></item>'
            for i in range(count)
        )
        return httpx.Response(
            200,
            text='<rss xmlns:torznab="http://torznab.com/api/2015/feed"><channel>'
            + items
            + "</channel></rss>",
        )

    def run(self, surface: str) -> Any:
        self.chapter = surface == "chapter"
        self.gap_only = surface == "gap"
        if surface == "backlog":
            return asyncio.run(
                grab_backlog._grab_existing_inner(1, "Test Series", "Test Series")
            )
        if surface == "gap":
            return asyncio.run(grab_backlog.search_complete_pack(1, "Test Series", 1))
        with shared.get_db() as db:
            series = dict(db.execute("SELECT * FROM series WHERE id=1").fetchone())
            volume = dict(db.execute("SELECT * FROM volumes WHERE id=1").fetchone())
            chapter = dict(db.execute("SELECT * FROM chapters WHERE id=1").fetchone())
        if surface == "chapter":
            return asyncio.run(series_._grab_chapter_task(1, series, chapter))
        task = (
            series_._grab_volume_task_sync
            if surface == "volume-sync"
            else series_._grab_volume_task
        )
        return asyncio.run(task(1, series, volume, "Test Series"))


@pytest.fixture
def search(flow: Flow, monkeypatch: pytest.MonkeyPatch) -> Search:
    flow.create()
    shared.CONFIG["ddl_grab_mode"] = "off"
    with shared.get_db() as db:
        db.execute(
            "INSERT INTO chapters(id,series_id,chapter_num,status,monitored) VALUES(1,1,1,'wanted',1)"
        )
    state = Search(flow)
    monkeypatch.setattr(flow, "transport", state.transport)
    return state


def track(surface: str, monkeypatch: pytest.MonkeyPatch) -> Reads:
    reads = Reads()
    module = grab_backlog if surface in ("backlog", "gap") else series_
    monkeypatch.setattr(module, "get_db", reads.get_db)
    return reads


@pytest.mark.parametrize("surface", SURFACES)
def test_completed_batch_connection_cycles_do_not_scale_with_candidate_count(
    search: Search, monkeypatch: pytest.MonkeyPatch, surface: str
) -> None:
    with shared.get_db() as db:
        db.executemany(
            "INSERT INTO blocklist(series_id,torrent_url,torrent_name,reason) VALUES(1,?,'blocked','test')",
            ((f"https://batch.test/{i}",) for i in range(100)),
        )
    measured: list[tuple[int, int, int]] = []
    for count in (1, 100):
        search.count = count
        reads = track(surface, monkeypatch)
        before = len(search.flow.requests)
        search.run(surface)
        reads.assert_closed()
        expected_requests = 4 if surface == "gap" else 1
        assert len(search.flow.requests) - before == expected_requests
        block_contexts = sum(n > 0 for n in reads.block_reads)
        assert sum(reads.block_reads) == count
        measured.append((len(reads.connections), block_contexts, count))
    print({"surface": surface, "measurements": measured})
    assert measured[1][:2] == measured[0][:2], (
        "candidate count multiplied real get_db open/close cycles"
    )
    assert measured[1][1] == 1, (
        "one completed result batch needs one blocklist read context"
    )
    assert search.flow.submitted == []


@pytest.mark.parametrize("surface", SURFACES)
def test_batch_filters_url_identity_and_seen_before_grab_with_closed_connections(
    search: Search, monkeypatch: pytest.MonkeyPatch, surface: str
) -> None:
    search.count = 4
    search.chapter = surface == "chapter"
    observed = search.flow.search()
    assert len(observed) == 4
    scoped = identity_for(observed[1], 1)
    assert scoped is not None
    with shared.get_db() as db:
        add_block(
            db,
            series_id=1,
            url=observed[0]["url"],
            title="blocked",
            reason="url",
            indexer=None,
            protocol=None,
            size_bytes=None,
        )
        add_block(
            db,
            series_id=1,
            url="https://old.test/scoped",
            title="blocked",
            reason="identity",
            indexer=None,
            protocol=None,
            size_bytes=None,
            identities=[scoped],
        )
        db.execute(
            "INSERT INTO seen(torrent_url,series_id) VALUES(?,1)", (observed[3]["url"],)
        )
    reads = track(surface, monkeypatch)
    admitted: list[dict[str, Any]] = []
    real_grab = grab_core.grab_item

    async def grab(item: dict[str, Any], series_id: int, **kwargs: Any) -> bool:
        reads.assert_closed()
        admitted.append(item)
        return await real_grab(item, series_id, **kwargs)

    monkeypatch.setattr(grab_backlog, "grab_item", grab)
    monkeypatch.setattr(main, "grab_item", grab)
    search.run(surface)
    assert [i["url"] for i in admitted] == ["https://batch.test/2"]
    assert search.flow.submitted == ["https://batch.test/2"]
    assert identity_for(admitted[0], 1) == identity_for(observed[2], 1)
    assert isinstance(admitted[0]["_source_context"], SourceContext)


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("blocked_first", [True, False])
def test_batch_keeps_same_url_observations_independent(
    search: Search,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    blocked_first: bool,
) -> None:
    search.chapter = surface == "chapter"
    first = search.flow.search()[0]
    with shared.get_db() as db:
        db.execute("UPDATE indexers SET enabled=0")
    search.flow.create()
    second = search.flow.search()[0]
    assert first["url"] == second["url"]
    assert identity_for(first, 1) != identity_for(second, 1)
    blocked = identity_for(first, 1)
    assert blocked is not None
    with shared.get_db() as db:
        db.execute("UPDATE indexers SET enabled=1")
        add_block(
            db,
            series_id=1,
            url="https://old.test/source",
            title="blocked",
            reason="identity",
            indexer=None,
            protocol=None,
            size_bytes=None,
            identities=[blocked],
        )
    batch = [first, second] if blocked_first else [second, first]
    calls: list[str] = []

    async def completed(query: str, **_kwargs: Any) -> list[dict[str, Any]]:
        calls.append(query)
        if surface == "gap" and " vol " not in query:
            return []
        return batch

    # Both entries are actual independently fetched SourceContexts. Inject the
    # completed batch here because the current search merger dedupes URLs; the
    # admission boundary must not introduce an additional URL-keyed authority.
    monkeypatch.setattr(grab_backlog, "_search_all", completed)
    monkeypatch.setattr(main, "_search_all", completed)
    reads = track(surface, monkeypatch)
    admitted: list[dict[str, Any]] = []
    real_grab = grab_core.grab_item

    async def grab(item: dict[str, Any], series_id: int, **kwargs: Any) -> bool:
        reads.assert_closed()
        admitted.append(item)
        return await real_grab(item, series_id, **kwargs)

    monkeypatch.setattr(grab_backlog, "grab_item", grab)
    monkeypatch.setattr(main, "grab_item", grab)
    requests_before = len(search.flow.requests)
    search.run(surface)
    assert len(search.flow.requests) == requests_before
    assert len(calls) == (4 if surface == "gap" else 1)
    assert len(admitted) == 1 and admitted[0] is second
    assert search.flow.submitted == [second["url"]]
