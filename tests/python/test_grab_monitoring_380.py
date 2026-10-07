"""Caller-level regression tests for monitoring at the grab boundary (#380)."""

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

import grab_backlog
import grab_core
import grab_dedup
import main
import shared
from routers import series_ as routes


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "monitoring.db"
    monkeypatch.setattr(main, "DB_PATH", str(path))
    monkeypatch.setattr(shared, "DB_PATH", str(path))
    monkeypatch.setattr(shared, "CONFIG", {})
    main.init_db()
    with shared.get_db() as db:
        db.execute(
            "INSERT INTO series(id,title,search_pattern,monitored,status,"
            "total_volumes,total_chapters)"
            " VALUES(1,'Test Series','Test Series',1,'RELEASING',3,30)"
        )
        for num in (1, 2, 3):
            db.execute(
                "INSERT INTO volumes(id,series_id,volume_num,status,monitored)"
                " VALUES(?,1,?,'wanted',?)",
                (num, num, int(num == 1)),
            )
            db.execute(
                "INSERT INTO chapters(series_id,volume_id,chapter_num,status,monitored)"
                " VALUES(1,?,?,'wanted',1)",
                (num, num * 10),
            )
        db.execute(
            "INSERT INTO chapters(series_id,volume_id,chapter_num,status,monitored)"
            " VALUES(1,1,9.5,'wanted',0)"
        )
    client = AsyncMock(return_value=(True, "qbittorrent", "test-hash", True))
    monkeypatch.setattr(grab_core, "grab_url", client)
    monkeypatch.setattr(grab_core, "notify_discord", AsyncMock())
    monkeypatch.setattr(grab_core, "score_release", lambda *a, **kw: 0)
    grab_dedup._GRABBING_URLS.clear()
    yield path, client
    grab_dedup._GRABBING_URLS.clear()


def release(title: str) -> dict[str, object]:
    return {
        "title": title,
        "url": "https://indexer.test/release",
        "protocol": "torrent",
    }


def rows(table: str, order: str) -> list[tuple[Any, ...]]:
    with shared.get_db() as db:
        return [
            tuple(row)
            for row in db.execute(
                f"SELECT {order},status FROM {table} WHERE series_id=1 ORDER BY {order}"
            )
            if row[0] is not None
        ]


@pytest.mark.parametrize(
    "title",
    [
        "Test Series Complete",
        "Test Series",
        "Test Series Volumes 1-3 Complete",
        "Test Series v01-v03",
        "Test Series Vol 1",
    ],
)
def test_backlog_rejects_no_monitored_work_before_client(env, monkeypatch, title):
    _, client = env
    with shared.get_db() as db:
        db.execute("UPDATE volumes SET monitored=0")
    monkeypatch.setattr(
        grab_backlog, "_search_all", AsyncMock(return_value=[release(title)])
    )
    assert (
        asyncio.run(grab_backlog._grab_existing_inner(1, "Test Series", "Test Series"))
        == 0
    )
    client.assert_not_awaited()
    assert rows("volumes", "volume_num") == [
        (1, "wanted"),
        (2, "wanted"),
        (3, "wanted"),
    ]
    assert not grab_dedup._GRABBING_URLS
    with shared.get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0


@pytest.mark.parametrize(
    "title",
    [
        "Test Series Volumes 1-3 Complete",
        "Test Series v01-v02",
        "Test Series 010-030",
    ],
)
def test_pack_updates_only_monitored_wanted_volumes(env, title):
    assert asyncio.run(grab_core.grab_item(release(title), 1))
    assert rows("volumes", "volume_num") == [
        (1, "grabbed"),
        (2, "wanted"),
        (3, "wanted"),
    ]
    chapters = rows("chapters", "chapter_num")
    if title == "Test Series 010-030":
        # A chapter release still updates its own monitored chapter coverage.
        assert chapters == [
            (9.5, "wanted"),
            (10, "grabbed"),
            (20, "grabbed"),
            (30, "grabbed"),
        ]
    else:
        assert chapters == [
            (9.5, "wanted"),
            (10, "grabbed"),
            (20, "wanted"),
            (30, "wanted"),
        ]


@pytest.mark.parametrize(
    "title", ["Test Series Volumes 1-3 Complete", "Test Series v01-v02"]
)
def test_pack_does_not_cascade_into_downloaded_volume(env, title):
    with shared.get_db() as db:
        db.execute("UPDATE volumes SET monitored=1,status='downloaded' WHERE id=3")
        db.execute("UPDATE chapters SET status='downloaded' WHERE volume_id=3")
    assert asyncio.run(grab_core.grab_item(release(title), 1))
    assert rows("chapters", "chapter_num") == [
        (9.5, "wanted"),
        (10, "grabbed"),
        (20, "wanted"),
        (30, "downloaded"),
    ]


def test_chapter_only_release_survives_volume_monitor_gate(env):
    with shared.get_db() as db:
        db.execute("UPDATE volumes SET monitored=0")
    assert asyncio.run(grab_core.grab_item(release("Test Series Chapter 010"), 1))
    assert rows("volumes", "volume_num") == [
        (1, "wanted"),
        (2, "wanted"),
        (3, "wanted"),
    ]
    assert rows("chapters", "chapter_num") == [
        (9.5, "wanted"),
        (10, "grabbed"),
        (20, "wanted"),
        (30, "wanted"),
    ]


@pytest.mark.parametrize("field", ["series", "volumes"])
def test_backlog_rechecks_monitoring_after_search(env, monkeypatch, field):
    _, client = env

    async def search(*args, **kwargs):
        with shared.get_db() as db:
            db.execute(f"UPDATE {field} SET monitored=0")
        return [release("Test Series Vol 1")]

    monkeypatch.setattr(grab_backlog, "_search_all", search)
    assert (
        asyncio.run(grab_backlog._grab_existing_inner(1, "Test Series", "Test Series"))
        == 0
    )
    client.assert_not_awaited()
    assert not grab_dedup._GRABBING_URLS


def test_complete_search_empty_monitored_set_has_no_phantom_fallback(env, monkeypatch):
    with shared.get_db() as db:
        db.execute("UPDATE volumes SET monitored=0")
    monkeypatch.setattr(
        grab_backlog,
        "_search_all",
        AsyncMock(return_value=[release("Test Series v01-v03")]),
    )
    grab = AsyncMock(return_value=True)
    monkeypatch.setattr(grab_backlog, "grab_item", grab)
    assert asyncio.run(grab_backlog.search_complete_pack(1, "Test Series", 3)) == 0
    grab.assert_not_awaited()


def test_complete_search_gaps_exclude_unmonitored_wanted(env, monkeypatch):
    search = AsyncMock(return_value=[])
    monkeypatch.setattr(grab_backlog, "_search_all", search)
    assert asyncio.run(grab_backlog.search_complete_pack(1, "Test Series", 3)) == 0
    gap_queries = [
        call.args[0] for call in search.await_args_list if " vol " in call.args[0]
    ]
    assert gap_queries == ["Test Series vol 1"]


def test_finished_backlog_threshold_counts_only_monitored_wanted(env, monkeypatch):
    with shared.get_db() as db:
        db.execute("UPDATE series SET status='FINISHED'")
    complete_search = AsyncMock(return_value=0)
    monkeypatch.setattr(grab_backlog, "search_complete_pack", complete_search)
    monkeypatch.setattr(grab_backlog, "_search_all", AsyncMock(return_value=[]))
    assert (
        asyncio.run(grab_backlog._grab_existing_inner(1, "Test Series", "Test Series"))
        == 0
    )
    complete_search.assert_not_awaited()


@pytest.mark.parametrize(
    "helper", [routes._grab_volume_task, routes._grab_volume_task_sync]
)
@pytest.mark.parametrize(
    "title",
    ["Test Series", "Test Series Complete", "Test Series v02-v03", "Test Series Vol 2"],
)
def test_targeted_auto_selection_rejects_unknown_or_unrelated(
    env, monkeypatch, helper, title
):
    _, client = env
    monkeypatch.setattr(main, "_search_all", AsyncMock(return_value=[release(title)]))
    with shared.get_db() as db:
        series = dict(db.execute("SELECT * FROM series WHERE id=1").fetchone())
        volume = dict(db.execute("SELECT * FROM volumes WHERE id=1").fetchone())
    asyncio.run(helper(1, series, volume, "Test Series vol 01"))
    client.assert_not_awaited()


@pytest.mark.parametrize(
    "helper", [routes._grab_volume_task, routes._grab_volume_task_sync]
)
@pytest.mark.parametrize("field", ["series", "volumes"])
def test_targeted_auto_selection_rechecks_monitoring(env, monkeypatch, helper, field):
    _, client = env

    async def search(*args, **kwargs):
        with shared.get_db() as db:
            db.execute(f"UPDATE {field} SET monitored=0")
        return [release("Test Series Vol 1")]

    monkeypatch.setattr(main, "_search_all", search)
    with shared.get_db() as db:
        series = dict(db.execute("SELECT * FROM series WHERE id=1").fetchone())
        volume = dict(db.execute("SELECT * FROM volumes WHERE id=1").fetchone())
    asyncio.run(helper(1, series, volume, "Test Series vol 01"))
    client.assert_not_awaited()


def test_targeted_auto_selection_allows_covering_pack(env, monkeypatch):
    monkeypatch.setattr(
        main, "_search_all", AsyncMock(return_value=[release("Test Series v01-v03")])
    )
    with shared.get_db() as db:
        series = dict(db.execute("SELECT * FROM series WHERE id=1").fetchone())
        volume = dict(db.execute("SELECT * FROM volumes WHERE id=1").fetchone())
    assert asyncio.run(
        routes._grab_volume_task_sync(1, series, volume, "Test Series vol 01")
    )
    assert rows("volumes", "volume_num") == [
        (1, "grabbed"),
        (2, "wanted"),
        (3, "wanted"),
    ]


@pytest.mark.parametrize("title", ["Test Series Complete", "Test Series v01-v03"])
def test_manual_selected_release_overrides_monitoring(env, title):
    with shared.get_db() as db:
        db.execute("UPDATE series SET monitored=0,monitor_mode='none'")
        db.execute("UPDATE volumes SET monitored=0")

    async def receive():
        return {"type": "http.request", "body": json.dumps(release(title)).encode()}

    request = Request({"type": "http", "method": "POST", "path": "/"}, receive)
    response = asyncio.run(routes.grab_volume_release(1, 1, request))
    assert json.loads(bytes(response.body))["ok"] is True
    env[1].assert_awaited_once()


@pytest.mark.parametrize(
    "title", ["Test Series Complete", "Test Series v01-v02", "Test Series Vol 1"]
)
def test_monitoring_rejection_then_reenable_can_retry_same_url(env, title):
    _, client = env
    with shared.get_db() as db:
        db.execute("UPDATE volumes SET monitored=0")
    item = release(title)
    assert not asyncio.run(grab_core.grab_item(item, 1))
    client.assert_not_awaited()
    assert item["url"] not in grab_dedup._GRABBING_URLS
    with shared.get_db() as db:
        db.execute("UPDATE volumes SET monitored=1 WHERE id=1")
    assert asyncio.run(grab_core.grab_item(item, 1))
    client.assert_awaited_once()


@pytest.mark.parametrize(
    "title",
    [
        "Test Series Chapter 010",
        "Test Series 010-030",
        "Test Series Chapters 10-30",
        "Test Series Chapters [Digital]",
    ],
)
@pytest.mark.parametrize("no_work", ["unmonitored", "downloaded"])
def test_chapter_grab_rejects_no_eligible_chapters(env, title, no_work):
    _, client = env
    with shared.get_db() as db:
        if no_work == "unmonitored":
            db.execute("UPDATE chapters SET monitored=0")
        else:
            db.execute("UPDATE chapters SET status='downloaded'")
    assert not asyncio.run(grab_core.grab_item(release(title), 1))
    client.assert_not_awaited()
    assert not grab_dedup._GRABBING_URLS


def test_chapter_grab_requires_work_in_parsed_coverage(env):
    _, client = env
    with shared.get_db() as db:
        db.execute("UPDATE chapters SET monitored=0 WHERE chapter_num>=10")
        db.execute("UPDATE chapters SET monitored=1 WHERE chapter_num=9.5")
    assert not asyncio.run(
        grab_core.grab_item(release("Test Series Chapters 10-30"), 1)
    )
    client.assert_not_awaited()


@pytest.mark.parametrize(
    "helper", [routes._grab_volume_task, routes._grab_volume_task_sync]
)
@pytest.mark.parametrize(
    "pack_type,start,end,title,accepted",
    [
        ("volume", 1, 2, "Test Series v01-v02", True),
        ("volume", 1, 2, "Test Series v02-v03", False),
        ("volume", 1, 2, "Test Series", False),
        ("complete", None, None, "Test Series v01-v03", True),
        ("complete", None, None, "Test Series v01-v02", False),
    ],
)
def test_targeted_pack_row_requires_known_covering_release(
    env, monkeypatch, helper, pack_type, start, end, title, accepted
):
    _, client = env
    with shared.get_db() as db:
        db.execute(
            "INSERT INTO volumes(id,series_id,pack_type,vol_range_start,vol_range_end,status,monitored)"
            " VALUES(4,1,?,?,?,'wanted',1)",
            (pack_type, start, end),
        )
        series = dict(db.execute("SELECT * FROM series WHERE id=1").fetchone())
        volume = dict(db.execute("SELECT * FROM volumes WHERE id=4").fetchone())
    monkeypatch.setattr(main, "_search_all", AsyncMock(return_value=[release(title)]))
    asyncio.run(helper(1, series, volume, "Test Series"))
    assert client.await_count == int(accepted)


@pytest.mark.parametrize("chapter", ["010", "010.5", "099", "099.5"])
def test_c_prefixed_chapter_requires_eligible_target(env, chapter):
    _, client = env
    with shared.get_db() as db:
        db.execute("UPDATE chapters SET monitored=0 WHERE chapter_num=10")
        db.execute(
            "INSERT INTO chapters(series_id,volume_id,chapter_num,status,monitored)"
            " VALUES(1,1,10.5,'wanted',0)"
        )
    assert not asyncio.run(grab_core.grab_item(release(f"Test Series c{chapter}"), 1))
    client.assert_not_awaited()
    assert not grab_dedup._GRABBING_URLS


@pytest.mark.parametrize("chapter", ["010", "010.5"])
def test_c_prefixed_chapter_persists_exact_parsed_target(env, chapter):
    with shared.get_db() as db:
        db.execute("UPDATE volumes SET monitored=0")
        db.execute(
            "INSERT INTO chapters(series_id,volume_id,chapter_num,status,monitored)"
            " VALUES(1,1,10.5,'wanted',1)"
        )
    assert asyncio.run(grab_core.grab_item(release(f"Test Series c{chapter}"), 1))
    with shared.get_db() as db:
        grabbed = db.execute(
            "SELECT chapter_num FROM chapters WHERE status='grabbed'"
        ).fetchall()
    assert [row[0] for row in grabbed] == [float(chapter)]
    assert rows("volumes", "volume_num") == [
        (1, "wanted"),
        (2, "wanted"),
        (3, "wanted"),
    ]


@pytest.fixture
def ddl_boundary(env, monkeypatch):
    from routers import suwayomi_

    ddl = AsyncMock(return_value=True)
    monkeypatch.setattr(suwayomi_, "get_suwayomi_client", lambda db: {"id": 7})
    monkeypatch.setattr(
        suwayomi_, "_get_series_source", lambda *args: {"source": "test"}
    )
    monkeypatch.setattr(suwayomi_, "suwayomi_grab", ddl)
    search = AsyncMock(return_value=[])
    monkeypatch.setattr(main, "_search_all", search)
    return ddl, search


def disable_grab_monitoring(field: str) -> None:
    statements = {
        "series": "UPDATE series SET monitored=0 WHERE id=1",
        "volume": "UPDATE volumes SET monitored=0 WHERE id=1",
        "mode": "UPDATE series SET monitor_mode='none' WHERE id=1",
    }
    with shared.get_db() as db:
        db.execute(statements[field])


def post_volume_grab(htmx: bool) -> None:
    from fastapi.testclient import TestClient

    token = "csrf-monitoring-380-" + "x" * 30
    client = TestClient(main.app)
    client.cookies.set("csrftoken", token)
    headers = {"X-CSRFToken": token}
    if htmx:
        headers["HX-Request"] = "true"
    try:
        response = client.post(
            "/series/1/volumes/1/grab", headers=headers, follow_redirects=False
        )
        if htmx:
            assert response.status_code == 200, response.text
            assert "<tr" in response.text
        else:
            assert response.status_code == 303, response.text
            assert response.headers["location"] == "/series/1"
    finally:
        client.close()


@pytest.mark.parametrize("field", ["series", "volume", "mode"])
@pytest.mark.parametrize("htmx", [False, True])
def test_endpoint_ddl_fallback_rechecks_monitoring_after_search(
    env, ddl_boundary, monkeypatch, field, htmx
):
    shared.CONFIG["ddl_grab_mode"] = "fallback"

    async def search(*args, **kwargs):
        disable_grab_monitoring(field)
        return []

    monkeypatch.setattr(main, "_search_all", search)
    post_volume_grab(htmx)
    env[1].assert_not_awaited()
    ddl_boundary[0].assert_not_awaited()
    assert rows("volumes", "volume_num") == [
        (1, "wanted"),
        (2, "wanted"),
        (3, "wanted"),
    ]


@pytest.mark.parametrize("mode", ["only", "prefer"])
@pytest.mark.parametrize("field", ["series", "volume", "mode"])
@pytest.mark.parametrize("htmx", [False, True])
def test_endpoint_ddl_initial_dispatch_requires_monitoring(
    env, ddl_boundary, field, mode, htmx
):
    shared.CONFIG["ddl_grab_mode"] = mode
    disable_grab_monitoring(field)
    post_volume_grab(htmx)
    env[1].assert_not_awaited()
    ddl_boundary[0].assert_not_awaited()
    ddl_boundary[1].assert_not_awaited()


@pytest.mark.parametrize("htmx", [False, True])
def test_endpoint_monitored_no_result_still_falls_back_to_ddl(env, ddl_boundary, htmx):
    shared.CONFIG["ddl_grab_mode"] = "fallback"
    post_volume_grab(htmx)
    env[1].assert_not_awaited()
    ddl_boundary[0].assert_awaited_once_with(1, 1.0)
    assert ddl_boundary[1].await_count == 2
