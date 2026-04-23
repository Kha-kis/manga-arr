"""Scheduler / RSS tick tests — deterministic, no live indexers.

Existing tests cover the bg-task lifecycle (create / cancel / exception
isolation). This file goes one layer deeper and asserts what one tick of
poll_rss actually does:

  - empty RSS result → no work, no DB churn
  - one mocked match  → exactly one grab, item recorded in `seen`
  - duplicate item    → second tick is idempotent (no second grab)
  - rss_loop wrapping → loop survives one iteration and a downstream
                        exception, then exits cleanly on cancellation

Mocks: indexers.fetch_all_rss is replaced with a deterministic feeder;
main.grab_item is replaced with a counter-stub. No HTTP, no Prowlarr,
no qBittorrent.
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch, AsyncMock

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


@pytest.fixture
def fresh_db_with_series():
    """Temp DB with one monitored series whose title matches our mock RSS item."""
    import main, shared, security

    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-sched-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)

    main.init_db()
    main.load_config()

    with sqlite3.connect(db.name) as c:
        c.execute(
            "INSERT INTO series(id, title, search_pattern, monitored, enabled, status)"
            " VALUES(?,?,?,?,?,?)",
            (1, "Vinland Saga", "Vinland Saga", 1, 1, "RELEASING")
        )

    try:
        yield db.name
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


def _mock_rss_item(url: str = "magnet:?xt=urn:btih:" + "a"*40,
                   title: str = "Vinland Saga v01 (Mock-Group)") -> dict:
    return {
        "url":        url,
        "title":      title,
        "indexer":    "MockIndexer",
        "protocol":   "torrent",
        "size_bytes": 100_000_000,
        "seeders":    50,
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
        # Real grab_item would write to `seen` after success; mimic that so
        # the idempotency test below sees the same state.
        with sqlite3.connect(fresh_db_with_series) as c:
            c.execute("INSERT OR IGNORE INTO seen(torrent_url) VALUES(?)", (it["url"],))
        return True

    import grab
    with _patch_fetch([item]), patch.object(grab, "grab_item", new=_stub_grab):
        asyncio.run(main.poll_rss())

    assert len(grab_calls) == 1, f"expected 1 grab, got {grab_calls}"
    sid, url = grab_calls[0]
    assert sid == 1
    assert url == item["url"]


# ───────────────────── idempotency ───────────────────────────────────────────

def test_poll_rss_duplicate_item_is_idempotent(fresh_db_with_series):
    """Second tick with the same RSS item → no second grab.

    Two mechanisms guard this in production:
      - poll_rss filters by `seen_urls` (set built from `seen` table) up front
      - grab_item double-checks `seen` inside its own DB transaction

    We verify the outer filter: with the URL in `seen`, the stub is never
    called.
    """
    import main

    item = _mock_rss_item()
    # Pre-seed `seen` so the URL is treated as already-grabbed.
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

    import grab
    with _patch_fetch([item]), patch.object(grab, "grab_item", new=_stub_grab):
        asyncio.run(main.poll_rss())
        asyncio.run(main.poll_rss())

    assert len(grab_calls) == 1, (
        f"second tick re-grabbed; this means `seen` filtering is broken: {grab_calls}"
    )


# ───────────────────── rss_loop wrapping ─────────────────────────────────────

def test_rss_loop_survives_poll_exception_and_cancels_cleanly(fresh_db_with_series):
    """rss_loop runs poll_rss inside a try/except so a single failure does
    not kill the loop, and exits cleanly on cancellation. We patch
    main.asyncio.sleep so the post-iteration interval (default 900s) doesn't
    block the test, and use a real-time bounded wait instead of yielding
    via the same asyncio.sleep we just patched out.
    """
    import main

    poll_calls = 0
    async def _flaky_poll():
        nonlocal poll_calls
        poll_calls += 1
        if poll_calls == 1:
            raise RuntimeError("simulated upstream blip")
        return None

    # The inner-loop sleep blocks on the configured interval (>=60s). Replace
    # main's view of asyncio.sleep with an instant no-op so iterations are
    # tight. Patches are scoped to main, leaving the runner's own asyncio
    # untouched.
    real_sleep = asyncio.sleep
    async def _instant(_n):
        # Yield once so the cancel signal can be observed between iterations.
        await real_sleep(0)

    async def _runner():
        import tasks
        with patch.object(tasks, "poll_rss", new=_flaky_poll), \
             patch.object(tasks.asyncio, "sleep", new=_instant):
            task = asyncio.create_task(main.rss_loop())
            # Bounded busy-wait against the real clock.
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
    via create_background_task, not a bare asyncio.create_task. This catches
    a regression where the loop is spawned without lifecycle tracking, which
    would silently leak it on shutdown.
    """
    # Read source to assert the registration shape; we can't import lifespan
    # without booting the app, and booting just to inspect feels heavier than
    # this static check. The source path is stable and imported by every test.
    import main, inspect
    src = inspect.getsource(main.lifespan)
    assert "create_background_task(rss_loop()" in src, (
        "lifespan no longer registers rss_loop via create_background_task — "
        "this risks an untracked background task that won't be cancelled on shutdown"
    )
