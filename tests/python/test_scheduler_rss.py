"""RSS scheduler tests.

Mocks the HTTP fetch layer and checks that grab_item is called and
that its `seen` dedup works across poll cycles.
"""

import asyncio
import os
import sqlite3
import tempfile
from unittest.mock import patch

import pytest


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


@pytest.fixture
def fresh_db_with_series(monkeypatch):
    import main, shared

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.unlink(tmp.name)
    monkeypatch.setattr(main, "DB_PATH", tmp.name)
    monkeypatch.setattr(shared, "DB_PATH", tmp.name)
    main.init_db()
    main.load_config()
    with sqlite3.connect(tmp.name) as c:
        c.execute(
            "INSERT INTO series(id,title,search_pattern,monitored,root_folder_id)"
            " VALUES(1,'Vinland Saga','Vinland Saga',1,1)"
        )
        c.execute(
            "INSERT OR IGNORE INTO root_folders(id,path,label,is_default) VALUES(1,'/tmp','Manga',1)"
        )
    try:
        yield tmp.name
    finally:
        for ext in ("", "-wal", "-shm"):
            p = tmp.name + ext
            if os.path.exists(p):
                os.unlink(p)


def _mock_rss_item(
    url: str = "magnet:?xt=urn:btih:" + "a" * 40,
    title: str = "Vinland Saga v01 (Mock-Group)",
) -> dict:
    return {
        "url": url,
        "title": title,
        "indexer": "MockIndexer",
        "protocol": "torrent",
        "size_bytes": 100_000_000,
        "seeders": 50,
    }


def _patch_fetch(items):
    """Patch routers.indexers.fetch_all_rss to return a fixed list."""

    async def _fake_fetch(_db):
        return list(items)

    return patch("routers.indexers.fetch_all_rss", new=_fake_fetch)


# ───────────────────── empty + happy path ───────────────────────────────────


def test_poll_rss_empty_does_no_work(fresh_db_with_series):
    """Empty RSS → no grab attempts, no rows in `seen`."""
    import main

    grab_calls: list = []

    async def _stub_grab(item, sid, **kw):
        grab_calls.append((sid, item["url"]))
        return True

    with _patch_fetch([]), patch.object(main, "grab_item", new=_stub_grab):
        asyncio.run(main.poll_rss())

    assert grab_calls == []
    with sqlite3.connect(fresh_db_with_series) as c:
        seen = c.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
    assert seen == 0


def test_poll_rss_one_matching_item_grabs_once(fresh_db_with_series):
    """One mocked RSS hit matching the seeded series → exactly one grab."""
    import main

    item = _mock_rss_item()
    grab_calls: list = []

    async def _stub_grab(it, sid, **kw):
        grab_calls.append((sid, it["url"]))
        with sqlite3.connect(fresh_db_with_series) as c:
            c.execute("INSERT OR IGNORE INTO seen(torrent_url) VALUES(?)", (it["url"],))
        return True

    import grab_rss

    with _patch_fetch([item]), patch.object(grab_rss, "grab_item", new=_stub_grab):
        asyncio.run(main.poll_rss())

    assert len(grab_calls) == 1, f"expected 1 grab, got {grab_calls}"
    sid, url = grab_calls[0]
    assert sid == 1
    assert url == item["url"]


# ───────────────────── idempotency ───────────────────────────────────────────


def test_poll_rss_duplicate_item_is_idempotent(fresh_db_with_series):
    """Second tick with the same RSS item → no second grab."""
    import main

    item = _mock_rss_item()
    with sqlite3.connect(fresh_db_with_series) as c:
        c.execute("INSERT INTO seen(torrent_url) VALUES(?)", (item["url"],))

    grab_calls: list = []

    async def _stub_grab(it, sid, **kw):
        grab_calls.append((sid, it["url"]))
        return True

    with _patch_fetch([item, item]), patch.object(main, "grab_item", new=_stub_grab):
        asyncio.run(main.poll_rss())

    assert grab_calls == [], f"duplicate item should not re-grab: {grab_calls}"


def test_poll_rss_two_ticks_no_double_grab(fresh_db_with_series):
    """Tick 1 grabs the item; tick 2 sees the same item, skips it."""
    import main

    item = _mock_rss_item()
    grab_calls: list = []

    async def _stub_grab(it, sid, **kw):
        grab_calls.append((sid, it["url"]))
        with sqlite3.connect(fresh_db_with_series) as c:
            c.execute("INSERT OR IGNORE INTO seen(torrent_url) VALUES(?)", (it["url"],))
        return True

    import grab_rss

    with _patch_fetch([item]), patch.object(grab_rss, "grab_item", new=_stub_grab):
        asyncio.run(main.poll_rss())
        asyncio.run(main.poll_rss())

    assert len(grab_calls) == 1, (
        f"second tick re-grabbed; this means `seen` filtering is broken: {grab_calls}"
    )


# ───────────────────── rss_loop wrapping ─────────────────────────────────────


def test_rss_loop_survives_poll_exception_and_cancels_cleanly(fresh_db_with_series):
    """rss_loop runs poll_rss inside a try/except so a single failure does
    not kill the loop, and exits cleanly on cancellation."""
    import main

    poll_calls = 0

    async def _flaky_poll():
        nonlocal poll_calls
        poll_calls += 1
        if poll_calls == 1:
            raise RuntimeError("simulated upstream blip")
        return None

    real_sleep = asyncio.sleep

    async def _instant(_n):
        await real_sleep(0)

    async def _runner():
        import tasks

        with (
            patch.object(tasks, "poll_rss", new=_flaky_poll),
            patch.object(tasks.asyncio, "sleep", new=_instant),
        ):
            task = asyncio.create_task(main.rss_loop())
            for _ in range(50):
                await real_sleep(0)
                if poll_calls >= 2:
                    break
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_runner())
    assert poll_calls >= 2, (
        f"rss_loop did not survive the first poll exception; poll_calls={poll_calls}"
    )


def test_rss_loop_uses_create_background_task_pattern():
    """Soft check: lifespan registers exactly one rss_loop task at startup
    via create_background_task, not a bare asyncio.create_task."""
    import main, inspect

    src = inspect.getsource(main.lifespan)
    assert "create_background_task(rss_loop()" in src, (
        "lifespan no longer registers rss_loop via create_background_task — "
    )
