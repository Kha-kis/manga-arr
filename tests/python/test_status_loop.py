"""status_loop tick test — focused on the auto-recovery side effects.

Pass 2 covered rss_loop. status_loop is the next-most-important loop because
it runs the auto-reset paths that are *supposed* to clear the residue
reconcile.py now reports. If those auto-reset queries silently regress, the
operator's stuck-grabbed pile grows again.

What this file asserts:
  - check_download_status auto-resets a stuck-grabbed volume that is NOT
    in the import_queue (mirrors the live behaviour reconcile.py respects)
  - check_download_status DOES NOT touch a stuck-grabbed volume that IS in
    the import_queue (the partner safety check)
  - check_download_status auto-prunes blocklist entries past the TTL
  - check_download_status is idempotent: running twice doesn't double-act
  - status_loop survives one check_download_status exception and keeps going
  - status_loop registers via create_background_task in lifespan

The qBit poll branch in check_download_status is mocked to a no-op auth
failure so we don't exercise the long completion code paths — those have
their own coverage in the e2e suite.
"""
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
def fresh_db():
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-statusloop-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()

    try:
        yield db.name
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


# qBit branch in check_download_status fires httpx.AsyncClient.post against
# the configured download client. We don't test that path here — give it a
# stub that returns "auth failed" so the function returns early.
class _AuthFailClient:
    def __init__(self, *a, **kw): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, *a, **kw):
        class _R: status_code = 200; text = "Fails."
        return _R()
    async def get(self, *a, **kw):
        class _R: status_code = 401
        return _R()


# ─────────────────────── auto-reset side effects ────────────────────────────

def test_check_download_status_resets_stuck_grabbed_when_not_in_queue(fresh_db):
    """Volume stuck in 'grabbed' >2d with no active import_queue row →
    cleared back to 'wanted' with all download metadata blanked."""
    import main
    with sqlite3.connect(fresh_db) as c:
        c.execute("INSERT INTO series(id, title, search_pattern) VALUES(1, 'X', 'X')")
        c.execute(
            "INSERT INTO volumes(id, series_id, volume_num, status, grabbed_at,"
            " download_id, source_url, torrent_name, indexer, protocol, client)"
            " VALUES(1, 1, 1, 'grabbed', datetime('now', '-5 days'),"
            " 'orphan-dl-id', 'magnet:x', 'name.torrent', 'idx', 'torrent', 'qbit')"
        )

    with patch("httpx.AsyncClient", new=_AuthFailClient):
        asyncio.run(main.check_download_status())

    with sqlite3.connect(fresh_db) as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT * FROM volumes WHERE id=1").fetchone()
    assert row["status"]       == "wanted"
    assert row["grabbed_at"]   is None
    assert row["download_id"]  is None
    assert row["torrent_name"] is None
    assert row["indexer"]      is None


def test_check_download_status_leaves_stuck_grabbed_when_in_queue(fresh_db):
    """Same volume but with an active import_queue row → must be left alone.
    This is the safety check that prevents racing the importer."""
    import main
    with sqlite3.connect(fresh_db) as c:
        c.execute("INSERT INTO series(id, title, search_pattern) VALUES(1, 'X', 'X')")
        c.execute(
            "INSERT INTO volumes(id, series_id, volume_num, status, grabbed_at, download_id)"
            " VALUES(1, 1, 1, 'grabbed', datetime('now', '-5 days'), 'dl-active')"
        )
        c.execute(
            "INSERT INTO import_queue(download_id, status, created_at)"
            " VALUES('dl-active', 'pending', datetime('now', '-5 days'))"
        )

    with patch("httpx.AsyncClient", new=_AuthFailClient):
        asyncio.run(main.check_download_status())

    with sqlite3.connect(fresh_db) as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT * FROM volumes WHERE id=1").fetchone()
    assert row["status"]      == "grabbed"
    assert row["download_id"] == "dl-active"


def test_check_download_status_prunes_expired_blocklist_entries(fresh_db):
    """blocklist entries older than blocklist_ttl_days must be deleted."""
    import main
    with sqlite3.connect(fresh_db) as c:
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('blocklist_ttl_days', '7')")
        # One expired (>7d), one fresh.
        c.execute("INSERT INTO blocklist(torrent_url, added_at)"
                  " VALUES('expired', datetime('now', '-30 days'))")
        c.execute("INSERT INTO blocklist(torrent_url, added_at)"
                  " VALUES('fresh', datetime('now', '-1 day'))")
    main.load_config()

    with patch("httpx.AsyncClient", new=_AuthFailClient):
        asyncio.run(main.check_download_status())

    with sqlite3.connect(fresh_db) as c:
        urls = {r[0] for r in c.execute("SELECT torrent_url FROM blocklist")}
    assert urls == {"fresh"}


def test_check_download_status_is_idempotent(fresh_db):
    """Running twice in a row must not double-act on the same residue."""
    import main
    with sqlite3.connect(fresh_db) as c:
        c.execute("INSERT INTO series(id, title, search_pattern) VALUES(1, 'X', 'X')")
        c.execute(
            "INSERT INTO volumes(id, series_id, volume_num, status, grabbed_at, download_id)"
            " VALUES(1, 1, 1, 'grabbed', datetime('now', '-5 days'), 'dl-x')"
        )

    with patch("httpx.AsyncClient", new=_AuthFailClient):
        asyncio.run(main.check_download_status())
        asyncio.run(main.check_download_status())

    with sqlite3.connect(fresh_db) as c:
        # Still 'wanted' — the second tick saw nothing to do.
        n_grabbed = c.execute("SELECT COUNT(*) FROM volumes WHERE status='grabbed'").fetchone()[0]
        n_wanted  = c.execute("SELECT COUNT(*) FROM volumes WHERE status='wanted'").fetchone()[0]
    assert n_grabbed == 0
    assert n_wanted  == 1


# ─────────────────────── status_loop wrapping ────────────────────────────────

def test_status_loop_survives_check_exception_and_cancels_cleanly(fresh_db):
    """Same shape as the rss_loop test in pass 2: an exception inside
    check_download_status must not kill the surrounding loop."""
    import main

    calls = 0
    async def _flaky_check():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated failure")
        return None

    real_sleep = asyncio.sleep
    async def _instant(_n):
        await real_sleep(0)

    async def _runner():
        import tasks
        with patch.object(tasks, "check_download_status", new=_flaky_check), \
             patch.object(tasks.asyncio, "sleep", new=_instant):
            task = asyncio.create_task(main.status_loop())
            for _ in range(50):
                await real_sleep(0)
                if calls >= 2:
                    break
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_runner())
    assert calls >= 2, (
        f"status_loop did not survive the first check exception; calls={calls}"
    )


def test_status_loop_registered_in_lifespan():
    """Static check that lifespan still spawns status_loop via the tracked
    helper. A regression here = a leaked task on shutdown."""
    import main, inspect
    src = inspect.getsource(main.lifespan)
    assert "create_background_task(status_loop()" in src, (
        "lifespan no longer registers status_loop via create_background_task — "
        "the loop would leak on shutdown without lifecycle tracking"
    )
