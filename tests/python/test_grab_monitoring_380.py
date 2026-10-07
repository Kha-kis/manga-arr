"""Caller-level regression tests for monitoring at the grab boundary (#380)."""

import asyncio
import json
import threading
import zipfile
from contextlib import contextmanager
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
import import_download
import import_discovery
import import_execute
import volumes
from clients import GrabResult
from routers import series_ as routes
from rescan import _series_library_dir

# Pytest adds this fixture module's directory to sys.path.
from test_cross_client_download_ownership import ownership_env, _archive  # pyright: ignore[reportImplicitRelativeImport]


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


def chapter_state(chapter_num: float) -> dict[str, Any]:
    with shared.get_db() as db:
        return dict(
            db.execute(
                "SELECT * FROM chapters WHERE series_id=1 AND chapter_num=?",
                (chapter_num,),
            ).fetchone()
        )


def seed_downloaded_chapter(chapter_num: float) -> dict[str, Any]:
    with shared.get_db() as db:
        db.execute(
            "UPDATE chapters SET status='downloaded', import_path='/library/local.cbz',"
            " imported_at='2026-01-01', grabbed_at='2025-12-31',"
            " torrent_name='Local release', torrent_url='https://indexer.test/local',"
            " download_id='local-id', download_client_id=42, indexer='Local indexer',"
            " protocol='torrent', client='Local client', release_group='Local group',"
            " quality='CBZ', size_bytes=12345 WHERE series_id=1 AND chapter_num=?",
            (chapter_num,),
        )
    return chapter_state(chapter_num)


@pytest.mark.parametrize(
    "title", ["Test Series Chapters 10-30", "Test Series c010-c030"]
)
@pytest.mark.parametrize("respect_monitoring", [True, False])
def test_chapter_range_preserves_downloaded_local_observation(
    env, title, respect_monitoring
):
    before = seed_downloaded_chapter(20)
    assert asyncio.run(
        grab_core.grab_item(
            release(title),
            1,
            respect_monitoring=respect_monitoring,
        )
    )
    assert chapter_state(20) == before
    assert chapter_state(10)["status"] == "grabbed"


@pytest.mark.parametrize(
    "title",
    [
        "Test Series Vol 1",
        "Test Series Complete Series",
        "Test Series v01-v03",
    ],
)
@pytest.mark.parametrize("respect_monitoring", [True, False])
def test_volume_grab_preserves_downloaded_child(env, title, respect_monitoring):
    with shared.get_db() as db:
        db.execute(
            "INSERT INTO chapters(series_id,volume_id,chapter_num,status,monitored)"
            " VALUES(1,1,8,'wanted',1)"
        )
    before = seed_downloaded_chapter(8)
    assert asyncio.run(
        grab_core.grab_item(
            release(title),
            1,
            respect_monitoring=respect_monitoring,
        )
    )
    assert chapter_state(8) == before
    assert chapter_state(10)["status"] == "grabbed"


@pytest.mark.parametrize("volume_ids", [None, [1]])
def test_grabbed_cascade_preserves_downloaded_chapter(env, volume_ids):
    before = seed_downloaded_chapter(10)
    with shared.get_db() as db:
        volumes._cascade_chapters(
            db,
            1,
            volume_ids,
            "grabbed",
            download_id="replacement",
        )
    assert chapter_state(10) == before


@pytest.mark.parametrize("volume_ids", [None, [1]])
def test_wanted_cascade_still_resets_downloaded_chapter(env, volume_ids):
    seed_downloaded_chapter(10)
    with shared.get_db() as db:
        volumes._cascade_chapters(
            db,
            1,
            volume_ids,
            "wanted",
            download_id=None,
            import_path=None,
        )
    after = chapter_state(10)
    assert after["status"] == "wanted"
    assert after["download_id"] is None
    assert after["import_path"] is None


def test_shared_manual_cascade_keeps_explicit_override(env):
    seed_downloaded_chapter(10)
    with shared.get_db() as db:
        shared.cascade_chapters(db, 1, [1], "grabbed", download_id="manual")
    assert chapter_state(10)["status"] == "grabbed"
    assert chapter_state(10)["download_id"] == "manual"


@pytest.mark.parametrize(
    "helper", [routes._grab_volume_task, routes._grab_volume_task_sync]
)
@pytest.mark.parametrize(
    "title",
    [
        "Test Series Vol. 1 (c001-c010)",
        "Test Series Vol 1 (Chapters 1-10)",
    ],
)
def test_targeted_search_prefers_explicit_volume_over_chapter_coverage(
    env,
    monkeypatch,
    helper,
    title,
):
    monkeypatch.setattr(main, "_search_all", AsyncMock(return_value=[release(title)]))
    with shared.get_db() as db:
        series = dict(db.execute("SELECT * FROM series WHERE id=1").fetchone())
        volume = dict(db.execute("SELECT * FROM volumes WHERE id=1").fetchone())
    asyncio.run(helper(1, series, volume, "Test Series vol 01"))
    assert rows("volumes", "volume_num") == [
        (1, "grabbed"),
        (2, "wanted"),
        (3, "wanted"),
    ]
    assert chapter_state(10)["status"] == "grabbed"


@pytest.mark.parametrize(
    "title", ["Test Series Complete Series", "Test Series v01-v06"]
)
@pytest.mark.parametrize("respect_monitoring", [True, False])
@pytest.mark.parametrize(
    "protocol,completion_id", [("torrent", "PACK-ID"), ("nzb", "pack-id")]
)
def test_grab_to_pack_completion_uses_exact_claims(
    env,
    title,
    respect_monitoring,
    protocol,
    completion_id,
):
    _, client = env
    client.return_value = GrabResult(True, "Test client", "pack-id", True, 7)
    item = release(title)
    item["protocol"] = protocol
    with shared.get_db() as db:
        db.execute("UPDATE series SET total_volumes=7")
        db.execute(
            "UPDATE volumes SET monitored=1,status='downloaded',download_id='local-id',"
            " download_client_id=42,source_url='https://indexer.test/local' WHERE id=3"
        )
        for num, owner, download_id in [(4, 8, "pack-id"), (5, 7, "other-id")]:
            db.execute(
                "INSERT INTO volumes(id,series_id,volume_num,status,monitored,"
                " source_url,download_id,download_client_id,protocol,torrent_name)"
                " VALUES(?,1,?,'grabbed',1,?,?,?,?, 'Other release')",
                (num, num, item["url"], download_id, owner, protocol),
            )
            db.execute(
                "INSERT INTO chapters(series_id,volume_id,chapter_num,status,monitored,"
                " torrent_url,download_id,download_client_id,protocol,torrent_name)"
                " VALUES(1,?,?,'grabbed',1,?,?,?,?, 'Other release')",
                (num, num * 10, item["url"], download_id, owner, protocol),
            )
        db.execute(
            "INSERT INTO volumes(id,series_id,volume_num,status,monitored,is_special)"
            " VALUES(6,1,6,'wanted',1,1)"
        )
        db.execute(
            "INSERT INTO chapters(series_id,volume_id,chapter_num,status,monitored)"
            " VALUES(1,1,8,'wanted',1)"
        )
    local_child = seed_downloaded_chapter(8)
    local = seed_downloaded_chapter(30)
    with shared.get_db() as db:
        untouched_volumes = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM volumes WHERE id IN (3,4,5) ORDER BY id"
            )
        ]
    untouched_chapters = [chapter_state(num) for num in (9.5, 30, 40, 50)]

    assert asyncio.run(
        grab_core.grab_item(item, 1, respect_monitoring=respect_monitoring)
    )
    with shared.get_db() as db:
        # Changing monitoring after grab must not change this pack's claims.
        db.execute("UPDATE volumes SET monitored=0 WHERE id=1")
        db.execute("UPDATE chapters SET monitored=0 WHERE chapter_num=10")
        db.execute("UPDATE volumes SET monitored=1 WHERE id=2")
        # A new chapter owned by another download in a claimed volume is not ours.
        db.execute(
            "INSERT INTO chapters(series_id,volume_id,chapter_num,status,monitored,"
            " torrent_url,download_id,download_client_id,protocol)"
            " VALUES(1,1,9,'grabbed',1,?,'pack-id',8,?)",
            (item["url"], protocol),
        )
    other_chapter = chapter_state(9)
    with shared.get_db() as db:
        intent = import_download._mark_downloaded(
            db,
            1,
            None,
            str(item["url"]),
            download_id=completion_id,
            download_client_id=7,
            protocol=protocol,
        )
    assert intent is not None
    assert rows("volumes", "volume_num") == [
        (1, "downloaded"),
        (2, "downloaded" if not respect_monitoring else "wanted"),
        (3, "downloaded"),
        (4, "grabbed"),
        (5, "grabbed"),
        (6, "wanted" if respect_monitoring else "downloaded"),
    ]
    assert chapter_state(10)["status"] == "downloaded"
    assert chapter_state(10)["download_client_id"] == 7
    assert chapter_state(20)["status"] == (
        "wanted" if respect_monitoring else "downloaded"
    )
    assert chapter_state(30) == local
    assert chapter_state(8) == local_child
    assert [chapter_state(num) for num in (9.5, 30, 40, 50)] == untouched_chapters
    assert chapter_state(9) == other_chapter
    with shared.get_db() as db:
        assert [
            dict(row)
            for row in db.execute(
                "SELECT * FROM volumes WHERE id IN (3,4,5) ORDER BY id"
            )
        ] == untouched_volumes


@pytest.mark.parametrize(
    "title", ["Test Series Complete Series", "Test Series v01-v03"]
)
def test_completing_one_of_two_overlapping_grabs_preserves_other_download(env, title):
    _, client = env
    first = release(title)
    second = release(title)
    second["url"] = "https://indexer.test/second"
    client.return_value = GrabResult(True, "Test client", "first-id", True, 7)
    assert asyncio.run(grab_core.grab_item(first, 1))
    with shared.get_db() as db:
        db.execute("UPDATE volumes SET monitored=1 WHERE id=2")
    client.return_value = GrabResult(True, "Test client", "second-id", True, 7)
    assert asyncio.run(grab_core.grab_item(second, 1))
    other_chapter = chapter_state(20)
    with shared.get_db() as db:
        other_volume = dict(db.execute("SELECT * FROM volumes WHERE id=2").fetchone())
        assert (
            import_download._mark_downloaded(
                db,
                1,
                None,
                str(first["url"]),
                download_id="first-id",
                download_client_id=7,
                protocol="torrent",
            )
            is not None
        )
        assert (
            dict(db.execute("SELECT * FROM volumes WHERE id=2").fetchone())
            == other_volume
        )
        assert (
            import_download._mark_downloaded(
                db,
                1,
                None,
                str(first["url"]),
                download_id="first-id",
                download_client_id=7,
                protocol="torrent",
            )
            is None
        )
    assert chapter_state(20) == other_chapter
    assert rows("volumes", "volume_num") == [
        (1, "downloaded"),
        (2, "grabbed"),
        (3, "wanted"),
    ]
    with shared.get_db() as db:
        assert (
            import_download._mark_downloaded(
                db,
                1,
                None,
                str(second["url"]),
                download_id="second-id",
                download_client_id=7,
                protocol="torrent",
            )
            is not None
        )
    assert chapter_state(20)["download_id"] == "second-id"
    assert chapter_state(20)["status"] == "downloaded"


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE volumes SET status='wanted' WHERE id=1",
        "UPDATE volumes SET download_id='other-id' WHERE id=1",
        "UPDATE volumes SET download_client_id=8 WHERE id=1",
        "UPDATE volumes SET source_url='https://indexer.test/other' WHERE id=1",
    ],
)
def test_pack_completion_without_current_claim_does_not_mutate(env, mutation):
    _, client = env
    client.return_value = GrabResult(True, "Test client", "pack-id", True, 7)
    item = release("Test Series Complete Series")
    assert asyncio.run(grab_core.grab_item(item, 1))
    with shared.get_db() as db:
        db.execute(mutation)
        before = [dict(row) for row in db.execute("SELECT * FROM volumes ORDER BY id")]
        assert (
            import_download._mark_downloaded(
                db,
                1,
                None,
                str(item["url"]),
                download_id="pack-id",
                download_client_id=7,
                protocol="torrent",
            )
            is None
        )
        assert [
            dict(row) for row in db.execute("SELECT * FROM volumes ORDER BY id")
        ] == before
        assert (
            db.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='download_complete'"
            ).fetchone()[0]
            == 0
        )
    assert chapter_state(10)["status"] == "grabbed"


def test_nzb_pack_completion_rejects_case_variant_id(env):
    _, client = env
    client.return_value = GrabResult(True, "Test client", "NZO-Pack", True, 7)
    item = release("Test Series Complete Series")
    item["protocol"] = "nzb"
    assert asyncio.run(grab_core.grab_item(item, 1))
    with shared.get_db() as db:
        assert (
            import_download._mark_downloaded(
                db,
                1,
                None,
                str(item["url"]),
                download_id="nzo-pack",
                download_client_id=7,
                protocol="nzb",
            )
            is None
        )
    assert chapter_state(10)["status"] == "grabbed"


def test_legacy_pack_completion_derives_claims_without_identity_arguments(env):
    item = release("Test Series Complete Series")
    assert asyncio.run(grab_core.grab_item(item, 1))
    with shared.get_db() as db:
        assert (
            import_download._mark_downloaded(db, 1, None, str(item["url"])) is not None
        )
    assert rows("volumes", "volume_num") == [
        (1, "downloaded"),
        (2, "wanted"),
        (3, "wanted"),
    ]
    assert chapter_state(10)["status"] == "downloaded"
    assert chapter_state(9.5)["status"] == "wanted"


def test_pack_completion_rolls_back_trigger_and_claim_updates(env):
    item = release("Test Series Complete Series")
    assert asyncio.run(grab_core.grab_item(item, 1))
    before = chapter_state(10)
    with pytest.raises(RuntimeError, match="abort completion"):
        with shared.get_db() as db:
            assert (
                import_download._mark_downloaded(db, 1, None, str(item["url"]))
                is not None
            )
            raise RuntimeError("abort completion")
    assert chapter_state(10) == before
    assert chapter_state(9.5)["status"] == "wanted"
    assert rows("volumes", "volume_num") == [
        (1, "grabbed"),
        (2, "wanted"),
        (3, "wanted"),
    ]


@pytest.mark.parametrize(
    "title", ["Test Series v01-v03", "Test Series Complete Series"]
)
def test_volume_pack_does_not_steal_an_existing_chapter_download(env, title):
    _, client = env
    with shared.get_db() as db:
        db.execute("UPDATE volumes SET monitored=0")
    chapter_item = release("Test Series Chapter 010")
    chapter_item["url"] = "https://indexer.test/chapter-pack"
    client.return_value = GrabResult(True, "qbittorrent", "chapter-id", True, 7)
    assert asyncio.run(grab_core.grab_item(chapter_item, 1))
    before = chapter_state(10)
    assert before["status"] == "grabbed"
    with shared.get_db() as db:
        db.execute("UPDATE volumes SET monitored=1 WHERE id=1")
    client.return_value = GrabResult(True, "qbittorrent", "volume-pack-id", True, 8)
    assert asyncio.run(grab_core.grab_item(release(title), 1))
    assert chapter_state(10) == before


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE volumes SET protocol='nzb' WHERE id=1",
        "UPDATE chapters SET protocol='nzb' WHERE chapter_num=10",
    ],
)
def test_protocol_is_part_of_completion_identity(env, mutation):
    _, client = env
    client.return_value = GrabResult(True, "qbittorrent", "pack-id", True, 7)
    item = release("Test Series Complete Series")
    assert asyncio.run(grab_core.grab_item(item, 1))
    with shared.get_db() as db:
        db.execute(mutation)
        before_volume = dict(db.execute("SELECT * FROM volumes WHERE id=1").fetchone())
        before_chapter = dict(
            db.execute("SELECT * FROM chapters WHERE chapter_num=10").fetchone()
        )
        import_download._mark_downloaded(
            db,
            1,
            None,
            str(item["url"]),
            download_id="pack-id",
            download_client_id=7,
            protocol="torrent",
        )
        table = "volumes" if mutation.startswith("UPDATE volumes") else "chapters"
        where = "id=1" if table == "volumes" else "chapter_num=10"
        assert dict(db.execute(f"SELECT * FROM {table} WHERE {where}").fetchone()) == (
            before_volume if table == "volumes" else before_chapter
        )


@pytest.mark.parametrize("combined", [False, True])
def test_real_sab_pack_completion_preserves_unclaimed_children(
    ownership_env, monkeypatch, combined
):
    paths = ownership_env
    monkeypatch.setattr(
        grab_core,
        "grab_url",
        AsyncMock(return_value=GrabResult(True, "sabnzbd", "NZO-pack", True, 7)),
    )
    monkeypatch.setattr(grab_core, "notify_discord", AsyncMock())
    monkeypatch.setattr(grab_core, "score_release", lambda *a, **kw: 0)
    grab_dedup._GRABBING_URLS.clear()
    with shared.get_db() as db:
        db.execute(
            "INSERT INTO series(id,title,search_pattern,total_volumes,root_folder_id,monitored) VALUES(1,'Test Series','Test Series',3,1,1)"
        )
        for num in (1, 2, 3):
            db.execute(
                "INSERT INTO volumes(id,series_id,volume_num,status,monitored) VALUES(?,1,?,'wanted',?)",
                (num, num, int(num == 1)),
            )
        db.execute(
            "INSERT INTO chapters(series_id,volume_id,chapter_num,status,monitored) VALUES(1,1,10,'wanted',1)"
        )
        db.execute(
            "INSERT INTO chapters(series_id,volume_id,chapter_num,status,monitored) VALUES(1,1,9.5,'wanted',0)"
        )
        db.execute(
            "INSERT INTO chapters(series_id,volume_id,chapter_num,status,monitored,download_client_id,download_id,import_path,protocol,torrent_url,torrent_name) VALUES(1,1,8,'downloaded',1,42,'local-id','/local/ch8.cbz','torrent','https://local','Local chapter')"
        )
    untouched = [chapter_state(n) for n in (8, 9.5)]
    item = release("Test Series v01-v03")
    item["protocol"] = "nzb"
    assert asyncio.run(grab_core.grab_item(item, 1))
    _archive(
        paths["downloads"]
        / ("Test Series v01-v03.cbz" if combined else "Test Series v01.cbz")
    )
    qids = import_discovery._sab_process_sync(
        {"NZO-pack": {"storage": str(paths["downloads"])}},
        {"NZO-pack"},
        "http://sab.invalid",
        download_client_id=7,
        include_legacy_ownerless=False,
    )
    assert len(qids) == 1
    result = asyncio.run(import_execute._execute_import(qids[0]))
    assert result
    assert [chapter_state(n) for n in (8, 9.5)] == untouched


@pytest.fixture
def commit_pack_env(ownership_env, monkeypatch):
    monkeypatch.setattr(grab_core, "grab_url", AsyncMock())
    monkeypatch.setattr(grab_core, "notify_discord", AsyncMock())
    monkeypatch.setattr(grab_core, "score_release", lambda *args, **kwargs: 0)
    grab_dedup._GRABBING_URLS.clear()
    with shared.get_db() as db:
        db.execute(
            "INSERT INTO series(id,title,search_pattern,total_volumes,root_folder_id,monitored)"
            " VALUES(1,'Test Series','Test Series',3,1,1)"
        )
        for num in (1, 2, 3):
            db.execute(
                "INSERT INTO volumes(id,series_id,volume_num,status,monitored)"
                " VALUES(?,1,?,'wanted',?)",
                (num, num, int(num == 1)),
            )
        for num, status, monitored in [
            (10, "wanted", 1),
            (9.5, "wanted", 0),
            (8, "wanted", 1),
        ]:
            db.execute(
                "INSERT INTO chapters(series_id,volume_id,chapter_num,status,monitored)"
                " VALUES(1,1,?,?,?)",
                (num, status, monitored),
            )
        db.execute(
            "INSERT INTO chapters(series_id,volume_id,chapter_num,status,monitored,"
            " download_client_id,download_id,torrent_url,protocol,torrent_name,import_path)"
            " VALUES(1,1,7,'grabbed',1,42,'other-id','https://indexer.test/other',"
            " 'torrent','Other release','/other/staging.cbz')"
        )
        db.execute(
            "UPDATE volumes SET status='downloaded', import_path='/library/local-v3.cbz',"
            " download_id='local-v3',download_client_id=42,protocol='torrent',"
            " source_url='https://indexer.test/local-v3',torrent_name='Local v3',"
            " quality='cbz',imported_at='2026-01-01' WHERE id=3"
        )
    seed_downloaded_chapter(8)
    yield ownership_env
    grab_dedup._GRABBING_URLS.clear()


def queue_pack_files(paths, protocol: str, *, manual: bool = False) -> int:
    if protocol == "nzb" and not manual:
        qids = import_discovery._sab_process_sync(
            {"NZO-pack": {"storage": str(paths["downloads"])}},
            {"NZO-pack"},
            "http://sab.invalid",
            download_client_id=7,
            include_legacy_ownerless=False,
        )
        assert len(qids) == 1
        return qids[0]
    with shared.get_db() as db:
        qid, review = import_discovery._queue_import(
            db,
            1,
            "NZO-pack" if protocol == "nzb" else "PACK-HASH",
            "Test Series v01-v03",
            "https://indexer.test/release",
            None,
            str(paths["downloads"]),
            download_client_id=7,
            protocol=protocol,
        )
    assert qid is not None and not review
    return qid


@pytest.mark.parametrize("protocol", ["torrent", "nzb"])
@pytest.mark.parametrize("combined", [False, True])
def test_real_file_plan_commit_preserves_all_unclaimed_observations(
    commit_pack_env, protocol, combined
):
    paths = commit_pack_env
    before_chapters = [chapter_state(n) for n in (7, 8, 9.5)]
    with shared.get_db() as db:
        before_volumes = [
            dict(row)
            for row in db.execute("SELECT * FROM volumes WHERE id IN (2,3) ORDER BY id")
        ]
    download_id = "NZO-pack" if protocol == "nzb" else "pack-hash"
    assert isinstance(grab_core.grab_url, AsyncMock)
    grab_core.grab_url.return_value = GrabResult(
        True,
        "sabnzbd" if protocol == "nzb" else "qbittorrent",
        download_id,
        True,
        7,
    )
    item = release("Test Series v01-v03")
    item["protocol"] = protocol
    assert asyncio.run(grab_core.grab_item(item, 1))
    if combined:
        _archive(paths["downloads"] / "Test Series v01-v03.cbz")
    else:
        for num in (1, 2, 3):
            _archive(paths["downloads"] / f"Test Series v0{num}.cbz")
    qid = queue_pack_files(paths, protocol)
    assert asyncio.run(import_execute._execute_import(qid))
    assert [chapter_state(n) for n in (7, 8, 9.5)] == before_chapters
    with shared.get_db() as db:
        assert [
            dict(row)
            for row in db.execute("SELECT * FROM volumes WHERE id IN (2,3) ORDER BY id")
        ] == before_volumes
        volume = db.execute("SELECT * FROM volumes WHERE id=1").fetchone()
        assert volume["status"] == "downloaded"
        assert volume["download_id"] == download_id
        assert volume["download_client_id"] == 7
        if combined:
            pack = db.execute(
                "SELECT * FROM volumes WHERE volume_num IS NULL AND status='downloaded'"
                " AND vol_range_start=1 AND vol_range_end=3"
            ).fetchone()
            assert pack is not None
            assert pack["download_client_id"] == 7
            pack_path = Path(pack["import_path"])
            assert pack_path.is_dir()
            assert list(pack_path.glob("*.cbz"))
    chapter = chapter_state(10)
    assert chapter["status"] == "downloaded"
    assert chapter["download_id"] == download_id
    assert chapter["download_client_id"] == 7
    assert list(paths["library"].rglob("*.cbz"))


@pytest.mark.parametrize("protocol", ["torrent", "nzb"])
def test_real_commit_explicit_manual_grab_retains_override(commit_pack_env, protocol):
    paths = commit_pack_env
    with shared.get_db() as db:
        db.execute("UPDATE volumes SET monitored=0")
        db.execute("UPDATE series SET monitored=0,monitor_mode='none'")
    assert isinstance(grab_core.grab_url, AsyncMock)
    grab_core.grab_url.return_value = GrabResult(
        True,
        "sabnzbd" if protocol == "nzb" else "qbittorrent",
        "NZO-pack" if protocol == "nzb" else "pack-hash",
        True,
        7,
    )
    item = release("Test Series v01-v03")
    item["protocol"] = protocol
    assert asyncio.run(grab_core.grab_item(item, 1, respect_monitoring=False))
    _archive(paths["downloads"] / "Test Series v01.cbz")
    assert asyncio.run(
        import_execute._execute_import(queue_pack_files(paths, protocol))
    )
    assert chapter_state(8)["download_client_id"] == 7
    assert chapter_state(8)["import_path"] != "/library/local.cbz"
    assert chapter_state(9.5)["status"] == "downloaded"


def test_real_commit_manual_queue_without_grab_retains_override(commit_pack_env):
    paths = commit_pack_env
    _archive(paths["downloads"] / "Test Series v02.cbz")
    assert asyncio.run(
        import_execute._execute_import(queue_pack_files(paths, "torrent", manual=True))
    )
    with shared.get_db() as db:
        row = db.execute("SELECT status,monitored FROM volumes WHERE id=2").fetchone()
        assert tuple(row) == ("downloaded", 0)


def test_real_commit_explicit_mapping_retains_manual_override(commit_pack_env):
    from import_publication import load_publication

    paths = commit_pack_env
    assert isinstance(grab_core.grab_url, AsyncMock)
    grab_core.grab_url.return_value = GrabResult(
        True, "qbittorrent", "pack-hash", True, 7
    )
    assert asyncio.run(grab_core.grab_item(release("Test Series v01-v03"), 1))
    _archive(paths["downloads"] / "Test Series v01.cbz")
    qid = queue_pack_files(paths, "torrent")
    with shared.get_db() as db:
        file_id = db.execute(
            "SELECT id FROM import_queue_files WHERE queue_id=?", (qid,)
        ).fetchone()[0]
    assert asyncio.run(
        import_execute._execute_import(qid, volume_overrides={file_id: 2})
    )
    with shared.get_db() as db:
        assert (
            db.execute("SELECT status FROM volumes WHERE id=2").fetchone()[0]
            == "downloaded"
        )
        publication = load_publication(db, queue_id=qid)
        assert publication is not None
        assert publication.plan.queue["_manual_mapping_files"] == [file_id]


def test_restart_backfill_preserves_other_acquisition_after_pack_commit(
    commit_pack_env,
):
    paths = commit_pack_env
    untouched = [chapter_state(n) for n in (7, 8)]
    assert isinstance(grab_core.grab_url, AsyncMock)
    grab_core.grab_url.return_value = GrabResult(
        True, "qbittorrent", "pack-hash", True, 7
    )
    assert asyncio.run(grab_core.grab_item(release("Test Series v01-v03"), 1))
    _archive(paths["downloads"] / "Test Series v01.cbz")
    assert asyncio.run(
        import_execute._execute_import(queue_pack_files(paths, "torrent"))
    )
    assert [chapter_state(n) for n in (7, 8)] == untouched
    main.init_db()
    assert [chapter_state(n) for n in (7, 8)] == untouched


@pytest.mark.parametrize(
    "download_id,protocol", [(None, None), ("PARENT-HASH", "torrent")]
)
def test_restart_backfill_retains_compatible_legacy_inheritance(
    env, download_id, protocol
):
    with shared.get_db() as db:
        db.execute(
            "UPDATE volumes SET status='downloaded',import_path='/library/parent.cbz',"
            " quality='cbz',download_id='parent-hash',protocol='torrent',"
            " download_client_id=7,indexer='Parent indexer' WHERE id=1"
        )
        db.execute(
            "UPDATE chapters SET download_id=?,protocol=?,download_client_id=?"
            " WHERE chapter_num=10",
            (download_id, protocol, 7 if download_id else None),
        )
    main.init_db()
    chapter = chapter_state(10)
    assert chapter["indexer"] == "Parent indexer"
    assert chapter["import_path"] == "/library/parent.cbz"


@pytest.mark.parametrize("protocol", ["torrent", "nzb"])
@pytest.mark.parametrize("table", ["volumes", "chapters"])
def test_real_file_commit_rejects_incompatible_claim_protocol(
    commit_pack_env, protocol, table
):
    paths = commit_pack_env
    assert isinstance(grab_core.grab_url, AsyncMock)
    grab_core.grab_url.return_value = GrabResult(
        True,
        "sabnzbd" if protocol == "nzb" else "qbittorrent",
        "NZO-pack" if protocol == "nzb" else "pack-hash",
        True,
        7,
    )
    item = release("Test Series v01-v03")
    item["protocol"] = protocol
    assert asyncio.run(grab_core.grab_item(item, 1))
    where = "id=1" if table == "volumes" else "chapter_num=10"
    with shared.get_db() as db:
        db.execute(
            f"UPDATE {table} SET protocol=? WHERE {where}",
            ("torrent" if protocol == "nzb" else "nzb",),
        )
        before = dict(db.execute(f"SELECT * FROM {table} WHERE {where}").fetchone())
    _archive(paths["downloads"] / "Test Series v01.cbz")
    assert asyncio.run(
        import_execute._execute_import(queue_pack_files(paths, protocol))
    )
    with shared.get_db() as db:
        assert (
            dict(db.execute(f"SELECT * FROM {table} WHERE {where}").fetchone())
            == before
        )


@pytest.mark.parametrize(
    "protocol,row_protocol,download_id,owner,url,expected",
    [
        ("torrent", None, "PACK-HASH", 7, "https://release", True),
        ("nzb", None, "pack-hash", 7, "https://release", True),
        ("nzb", None, "PACK-HASH", 7, "https://release", False),
        ("torrent", "nzb", "pack-hash", 7, "https://release", False),
        ("nzb", "torrent", "pack-hash", 7, "https://release", False),
        ("torrent", None, "pack-hash", None, "https://release", False),
        ("torrent", None, "pack-hash", 7, "https://other", False),
        ("torrent", "invalid", "pack-hash", 7, "https://release", False),
        ("torrent", None, "", 7, "https://release", False),
    ],
)
def test_legacy_claim_requires_provable_acquisition(
    protocol, row_protocol, download_id, owner, url, expected
):
    from download_identity import DownloadIdentity

    row = {
        "download_client_id": owner,
        "protocol": row_protocol,
        "download_id": download_id,
        "source_url": url,
    }
    assert (
        import_download._same_acquisition(
            row,
            DownloadIdentity(7, protocol, "pack-hash"),
            "https://release",
            "source_url",
        )
        is expected
    )


@pytest.mark.parametrize("protocol", ["nzb", "torrent"])
@pytest.mark.parametrize(
    "state,quality",
    [
        ("downloaded", None),
        ("downloaded", "cbr"),
        ("wanted", None),
        ("grabbed", "cbz"),
    ],
)
def test_nonclaim_existing_canonical_archive_is_not_replaced(
    commit_pack_env, state, quality, protocol
):
    paths = commit_pack_env
    assert isinstance(grab_core.grab_url, AsyncMock)
    grab_core.grab_url.return_value = GrabResult(
        True,
        "sabnzbd" if protocol == "nzb" else "qbittorrent",
        "NZO-pack" if protocol == "nzb" else "pack-hash",
        True,
        7,
    )
    with shared.get_db() as db:
        db.execute(
            "UPDATE volumes SET status=?,quality=?,source_url='https://other',download_id='other-id',download_client_id=42,protocol='torrent' WHERE id=2",
            (state, quality),
        )
    item = release("Test Series v01-v03")
    item["protocol"] = protocol
    assert asyncio.run(grab_core.grab_item(item, 1))
    _archive(paths["downloads"] / "Test Series v01.cbz")
    _archive(paths["downloads"] / "Test Series v02.cbz")
    qid = queue_pack_files(paths, protocol)
    with shared.get_db() as db:
        row = db.execute(
            "SELECT filename FROM import_queue_files WHERE queue_id=? AND proposed_volume=2",
            (qid,),
        ).fetchone()
        library_dir = _series_library_dir(db, 1)
        assert library_dir is not None
        destination = Path(library_dir) / row["filename"]
        db.execute("UPDATE volumes SET import_path=? WHERE id=2", (str(destination),))
        before = dict(db.execute("SELECT * FROM volumes WHERE id=2").fetchone())
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w") as archive:
        archive.writestr("001.jpg", b"owned original page")
    original = destination.read_bytes()
    assert asyncio.run(import_execute._execute_import(qid))
    with shared.get_db() as db:
        assert dict(db.execute("SELECT * FROM volumes WHERE id=2").fetchone()) == before
        receipt = db.execute(
            "SELECT f.plan_status,f.stage_ok,f.stage_path FROM import_publication_files f"
            " JOIN import_publications p ON p.id=f.publication_id"
            " WHERE p.queue_id=? AND f.proposed_vol=2",
            (qid,),
        ).fetchone()
        assert receipt["plan_status"] == "skip"
        assert not receipt["stage_ok"]
        assert not receipt["stage_path"]
    assert destination.read_bytes() == original


@pytest.mark.parametrize("protocol", ["nzb", "torrent"])
@pytest.mark.parametrize("boundary", ["prepared", "file_publication"])
def test_late_nonclaim_change_preserves_canonical_bytes(
    commit_pack_env, monkeypatch, protocol, boundary
):
    import import_publication

    paths = commit_pack_env
    assert isinstance(grab_core.grab_url, AsyncMock)
    grab_core.grab_url.return_value = GrabResult(
        True,
        "sabnzbd" if protocol == "nzb" else "qbittorrent",
        "NZO-pack" if protocol == "nzb" else "pack-hash",
        True,
        7,
    )
    item = release("Test Series v01-v03")
    item["protocol"] = protocol
    assert asyncio.run(grab_core.grab_item(item, 1))
    _archive(paths["downloads"] / "Test Series v01.cbz")
    qid = queue_pack_files(paths, protocol)
    with shared.get_db() as db:
        filename = db.execute(
            "SELECT filename FROM import_queue_files WHERE queue_id=?", (qid,)
        ).fetchone()[0]
        library_dir = _series_library_dir(db, 1)
        assert library_dir is not None
        destination = Path(library_dir) / filename
        db.execute("UPDATE volumes SET import_path=? WHERE id=1", (str(destination),))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w") as archive:
        archive.writestr("001.jpg", b"original before late ownership change")
    original = destination.read_bytes()
    protected = []

    def mutate():
        with shared.get_db() as db:
            db.execute(
                "UPDATE volumes SET download_client_id=42,download_id='late-other-id',source_url='https://other' WHERE id=1"
            )
            protected.append(
                dict(db.execute("SELECT * FROM volumes WHERE id=1").fetchone())
            )

    if boundary == "prepared":
        real_barrier = import_execute.commit_prepared_barrier

        def barrier(*args, **kwargs):
            real_barrier(*args, **kwargs)
            mutate()

        monkeypatch.setattr(import_execute, "commit_prepared_barrier", barrier)
    else:
        real_publish = import_publication._publish_prepared_file

        def publish(*args, **kwargs):
            mutate()
            return real_publish(*args, **kwargs)

        monkeypatch.setattr(import_publication, "_publish_prepared_file", publish)

    asyncio.run(import_execute._execute_import(qid))
    assert protected
    assert destination.read_bytes() == original
    with shared.get_db() as db:
        assert (
            dict(db.execute("SELECT * FROM volumes WHERE id=1").fetchone())
            == protected[-1]
        )


@pytest.mark.parametrize("protocol", ["nzb", "torrent"])
@pytest.mark.parametrize("manual", ["grab_override", "file_mapping", "untracked_queue"])
def test_manual_override_can_replace_canonical_archive(
    commit_pack_env, protocol, manual
):
    paths = commit_pack_env
    assert isinstance(grab_core.grab_url, AsyncMock)
    grab_core.grab_url.return_value = GrabResult(
        True,
        "sabnzbd" if protocol == "nzb" else "qbittorrent",
        "NZO-pack" if protocol == "nzb" else "pack-hash",
        True,
        7,
    )
    if manual != "untracked_queue":
        item = release("Test Series v01-v03")
        item["protocol"] = protocol
        assert asyncio.run(
            grab_core.grab_item(item, 1, respect_monitoring=manual != "grab_override")
        )
    _archive(paths["downloads"] / "Test Series v02.cbz")
    qid = queue_pack_files(paths, protocol, manual=manual == "untracked_queue")
    with shared.get_db() as db:
        row = db.execute(
            "SELECT id,filename FROM import_queue_files WHERE queue_id=?", (qid,)
        ).fetchone()
        file_id = row["id"]
        library_dir = _series_library_dir(db, 1)
        assert library_dir is not None
        destination = Path(library_dir) / row["filename"]
        db.execute("UPDATE volumes SET import_path=? WHERE id=2", (str(destination),))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w") as archive:
        archive.writestr("001.jpg", b"original explicitly replaceable page")
    original = destination.read_bytes()
    assert asyncio.run(
        import_execute._execute_import(
            qid,
            volume_overrides={file_id: 2} if manual == "file_mapping" else None,
        )
    )
    assert destination.read_bytes() != original
    with shared.get_db() as db:
        assert (
            db.execute("SELECT status FROM volumes WHERE id=2").fetchone()[0]
            == "downloaded"
        )


def test_automatic_publication_does_not_hold_sqlite_writer_during_rename(
    commit_pack_env, monkeypatch
):
    import import_publication

    paths = commit_pack_env
    assert isinstance(grab_core.grab_url, AsyncMock)
    grab_core.grab_url.return_value = GrabResult(
        True, "qbittorrent", "pack-hash", True, 7
    )
    assert asyncio.run(grab_core.grab_item(release("Test Series v01-v03"), 1))
    _archive(paths["downloads"] / "Test Series v01.cbz")
    qid = queue_pack_files(paths, "torrent")
    with shared.get_db() as db:
        filename = db.execute(
            "SELECT filename FROM import_queue_files WHERE queue_id=?", (qid,)
        ).fetchone()[0]
        library_dir = _series_library_dir(db, 1)
        assert library_dir is not None
        destination = Path(library_dir) / filename
        db.execute("UPDATE volumes SET import_path=? WHERE id=1", (str(destination),))
    destination.parent.mkdir(parents=True, exist_ok=True)
    _archive(destination)
    writer_started = threading.Event()
    writer_done = threading.Event()
    responsive = []

    def change_monitoring():
        with shared.get_db() as db:
            writer_started.set()
            db.execute("UPDATE volumes SET monitored=0 WHERE id=1")
        writer_done.set()

    writer = threading.Thread(target=change_monitoring)
    real_rename = import_publication._rename_noreplace

    def rename(source, target):
        if source == str(destination):
            writer.start()
            assert writer_started.wait(2)
            responsive.append(writer_done.wait(2))
        return real_rename(source, target)

    monkeypatch.setattr(import_publication, "_rename_noreplace", rename)
    try:
        asyncio.run(import_execute._execute_import(qid))
    finally:
        if writer.ident is not None:
            writer.join(timeout=5)
    assert writer_done.is_set()
    assert responsive == [True]
    with shared.get_db() as db:
        assert db.execute("SELECT monitored FROM volumes WHERE id=1").fetchone()[0] == 0


@pytest.mark.parametrize("protocol", ["nzb", "torrent"])
@pytest.mark.parametrize("boundary", ["before_plan", "prepared"])
def test_owned_acquisition_import_survives_monitoring_disabled(
    commit_pack_env, monkeypatch, protocol, boundary
):
    paths = commit_pack_env
    assert isinstance(grab_core.grab_url, AsyncMock)
    grab_core.grab_url.return_value = GrabResult(
        True,
        "sabnzbd" if protocol == "nzb" else "qbittorrent",
        "NZO-pack" if protocol == "nzb" else "pack-hash",
        True,
        7,
    )
    item = release("Test Series v01-v03")
    item["protocol"] = protocol
    assert asyncio.run(grab_core.grab_item(item, 1))
    _archive(paths["downloads"] / "Test Series v01.cbz")
    qid = queue_pack_files(paths, protocol)

    def unmonitor():
        with shared.get_db() as db:
            db.execute("UPDATE series SET monitored=0,monitor_mode='none' WHERE id=1")
            db.execute("UPDATE volumes SET monitored=0 WHERE id=1")
            db.execute("UPDATE chapters SET monitored=0 WHERE chapter_num=10")

    if boundary == "before_plan":
        unmonitor()
    else:
        real_barrier = import_execute.commit_prepared_barrier

        def barrier(*args, **kwargs):
            real_barrier(*args, **kwargs)
            unmonitor()

        monkeypatch.setattr(import_execute, "commit_prepared_barrier", barrier)
    assert asyncio.run(import_execute._execute_import(qid))
    with shared.get_db() as db:
        volume = db.execute("SELECT * FROM volumes WHERE id=1").fetchone()
        chapter = db.execute("SELECT * FROM chapters WHERE chapter_num=10").fetchone()
        assert volume["status"] == chapter["status"] == "downloaded"
        assert volume["monitored"] == chapter["monitored"] == 0
        assert (
            volume["download_id"]
            == chapter["download_id"]
            == ("NZO-pack" if protocol == "nzb" else "pack-hash")
        )
        assert Path(volume["import_path"]).is_file()


@pytest.mark.parametrize("protocol", ["nzb", "torrent"])
@pytest.mark.parametrize("respect_monitoring", [True, False])
@pytest.mark.parametrize("winner", ["grab", "mark_downloaded"])
def test_single_volume_grab_after_client_await_preserves_new_observations(
    env, monkeypatch, protocol, respect_monitoring, winner
):
    import import_plan

    before = {}
    item = release("Test Series Vol 1")
    item["protocol"] = protocol
    winning_item = dict(item, url="https://indexer.test/winning-release")

    async def accepted(url, *args, **kwargs):
        if url == winning_item["url"]:
            return GrabResult(True, "winner-client", "winner-id", True, 42)
        await asyncio.sleep(0)
        if winner == "grab":
            assert await grab_core.grab_item(winning_item, 1, respect_monitoring=False)
        else:
            request = Request({"type": "http", "headers": []})
            await routes.mark_volume_downloaded(request, 1, 1)
        with shared.get_db() as db:
            before["volume"] = dict(
                db.execute("SELECT * FROM volumes WHERE id=1").fetchone()
            )
            before["chapters"] = [
                dict(r) for r in db.execute("SELECT * FROM chapters ORDER BY id")
            ]
        return GrabResult(True, "late-client", "late-id", True, 7)

    monkeypatch.setattr(grab_core, "grab_url", AsyncMock(side_effect=accepted))
    assert not asyncio.run(
        grab_core.grab_item(item, 1, respect_monitoring=respect_monitoring)
    )
    with shared.get_db() as db:
        assert (
            dict(db.execute("SELECT * FROM volumes WHERE id=1").fetchone())
            == before["volume"]
        )
        assert [
            dict(r) for r in db.execute("SELECT * FROM chapters ORDER BY id")
        ] == before["chapters"]
        history = db.execute(
            "SELECT * FROM history WHERE event_type='grabbed' AND download_id='late-id'"
        ).fetchone()
        assert history is not None
        assert json.loads(history["data"])["claim_lost"] is True
        assert (
            db.execute(
                "SELECT download_id FROM seen WHERE torrent_url=?", (item["url"],)
            ).fetchone()[0]
            == "late-id"
        )
        assert import_plan._automatic_grab_import(
            db,
            {
                "series_id": 1,
                "download_client_id": 7,
                "download_protocol": protocol,
                "download_id": "late-id",
                "torrent_url": item["url"],
            },
        )


@pytest.mark.parametrize("protocol", ["nzb", "torrent"])
@pytest.mark.parametrize("status", ["wanted", "downloaded"])
def test_single_volume_grab_materializes_observation_before_context_exit(
    env, monkeypatch, protocol, status
):
    from test_import_route_row_lifetime import _ExpiringRow, _RowLifetime  # pyright: ignore[reportImplicitRelativeImport]

    path, client = env
    with shared.get_db() as db:
        db.execute("UPDATE volumes SET status=?,quality='cbr' WHERE id=1", (status,))
    client.return_value = GrabResult(True, "test-client", "new-id", True, 7)
    real_get_db = shared.get_db
    guarded_queries = []

    @contextmanager
    def guarded_get_db():
        lifetime = _RowLifetime()
        with real_get_db() as db:

            class GuardedConnection:
                def execute(self, sql, parameters=()):
                    if (
                        sql
                        != "SELECT * FROM volumes WHERE series_id=? AND volume_num=?"
                    ):
                        return db.execute(sql, parameters)
                    guarded_queries.append(sql)
                    previous_factory = db.row_factory
                    db.row_factory = lambda cursor, values: _ExpiringRow(
                        lifetime, tuple(c[0] for c in cursor.description), values
                    )
                    try:
                        return db.execute(sql, parameters)
                    finally:
                        db.row_factory = previous_factory

            try:
                yield GuardedConnection()
            finally:
                lifetime.active = False

    monkeypatch.setattr(grab_core, "get_db", guarded_get_db)
    item = release("Test Series Vol 1.cbz")
    item["protocol"] = protocol
    assert asyncio.run(grab_core.grab_item(item, 1))
    assert len(guarded_queries) == 2
    client.assert_awaited_once()


@pytest.mark.parametrize("protocol", ["nzb", "torrent"])
@pytest.mark.parametrize("field", ["title", "monitored"])
def test_single_volume_grab_after_await_ignores_nonclaim_annotations(
    env, monkeypatch, protocol, field
):
    async def accepted(*args, **kwargs):
        with shared.get_db() as db:
            if field == "title":
                db.execute(
                    "UPDATE volumes SET title='Provider display annotation' WHERE id=1"
                )
            else:
                db.execute("UPDATE volumes SET monitored=0 WHERE id=1")
        return GrabResult(True, "test-client", "new-id", True, 7)

    monkeypatch.setattr(grab_core, "grab_url", AsyncMock(side_effect=accepted))
    item = release("Test Series Vol 1")
    item["protocol"] = protocol
    assert asyncio.run(grab_core.grab_item(item, 1))
    with shared.get_db() as db:
        row = db.execute("SELECT * FROM volumes WHERE id=1").fetchone()
        assert row["status"] == "grabbed"
        assert row["download_id"] == "new-id"
        assert row["download_client_id"] == 7
        if field == "title":
            assert row["title"] == "Provider display annotation"
        else:
            assert row["monitored"] == 0
