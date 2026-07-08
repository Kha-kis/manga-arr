"""PR 4b: grab_item wraps grab_url in asyncio.wait_for so an indexer
or client that hangs can't pin a URL in _GRABBING_URLS indefinitely.
On timeout, the URL is released and a grab_timeout event is logged."""

import asyncio
import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


@pytest.fixture
def env():
    import main, shared, security

    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close()
    os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-grab-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()

    # Seed a series and volume so grab_item reaches the grab_url call
    with sqlite3.connect(db.name) as c:
        c.execute(
            "INSERT INTO series(id, title, search_pattern, edition_type,"
            " enabled, monitored, monitor_mode)"
            " VALUES(9, 'HangTest', 'HangTest', 'standard', 1, 1, 'all')"
        )
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, monitored)"
            " VALUES(9, 1.0, 'wanted', 1)"
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


def test_grab_url_timeout_returns_false_and_releases_url(env):
    import main
    import grab_core

    async def _hanging_grab(*a, **kw):
        await asyncio.sleep(60)

    item = {
        "url": "https://example.test/hang.torrent",
        "title": "HangTest vol 01",
        "protocol": "torrent",
    }

    async def _run():
        original = asyncio.wait_for

        async def _short_wait_for(coro, timeout=None):
            return await original(coro, timeout=0.1)

        with (
            patch.object(grab_core, "grab_url", _hanging_grab),
            patch.object(asyncio, "wait_for", _short_wait_for),
        ):
            return await main.grab_item(item, series_id=9)

    result = asyncio.run(_run())
    assert result is False

    # URL must be released from the in-flight dedup set
    assert item["url"] not in main._GRABBING_URLS

    # A grab_timeout event must be logged
    with sqlite3.connect(env) as c:
        evs = c.execute(
            "SELECT event_type, message FROM events WHERE event_type='grab_timeout'"
        ).fetchall()
    assert len(evs) == 1
    assert "HangTest vol 01" in evs[0][1]


def test_successful_grab_still_works(env):
    import main
    import grab_core

    async def _ok_grab(*a, **kw):
        return (True, "qbit-client", "dl-id-123", True)

    item = {
        "url": "https://example.test/ok.torrent",
        "title": "HangTest vol 01",
        "protocol": "torrent",
    }

    async def _run():
        with patch.object(grab_core, "grab_url", _ok_grab):
            return await main.grab_item(item, series_id=9)

    result = asyncio.run(_run())
    assert result is True
