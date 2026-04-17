"""Tests for app/status_cache.py — the in-memory download-client cache.

These tests lock in the guarantees that let /queue render without live
httpx calls:

  - /build_queue_rows reads from DOWNLOAD_STATUS_CACHE, not httpx
  - DownloadStatusCache.refresh populates snapshots from the fetchers
  - A failed refresh preserves last-known-good items + records an error
  - Concurrent refresh() callers collapse to a single poll (single-flight)
  - freshness_label transitions through warming_up → live → stale → unavailable
  - The manual /api/queue/refresh endpoint returns immediately (does NOT
    block on the poll completing)
  - The queue page renders "warming up" before the first refresh
  - download_status_refresh_loop keeps looping even when refresh() raises
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


# ───────────────────── test scaffolding ──────────────────────────────────────

@pytest.fixture
def fresh_db():
    """Temp DB seeded with one qBit + one SAB download client."""
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-sc-keys-")

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
            "INSERT INTO download_clients(id, name, type, host, port, enabled,"
            " username, password, category, priority)"
            " VALUES(1, 'qb', 'qbittorrent', 'http://qbit.local', 8080, 1,"
            " 'u', 'p', 'manga', 1)"
        )
        c.execute(
            "INSERT INTO download_clients(id, name, type, host, port, enabled,"
            " username, password, category, priority)"
            " VALUES(2, 'sab', 'sabnzbd', 'http://sab.local', 8080, 1,"
            " NULL, 'sabkey', 'manga', 2)"
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


@pytest.fixture
def isolated_cache():
    """Swap in a fresh DownloadStatusCache as the module singleton and
    restore the original afterwards. Keeps tests from leaking state."""
    import status_cache
    from status_cache import DownloadStatusCache
    orig = status_cache.DOWNLOAD_STATUS_CACHE
    status_cache.DOWNLOAD_STATUS_CACHE = DownloadStatusCache()
    try:
        yield status_cache.DOWNLOAD_STATUS_CACHE
    finally:
        status_cache.DOWNLOAD_STATUS_CACHE = orig


class _MockResp:
    def __init__(self, status_code=200, text="Ok.", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data or {}
    def json(self):
        return self._json


def _mixed_ok_client(*, qbit_payload=None, sab_payload=None):
    """Mock httpx.AsyncClient that replies OK for both qBit and SAB.

    qBit lowercases its hashes at parse time; we use a literal lowercase
    here so we can compare keys directly in assertions.
    """
    qp = qbit_payload if qbit_payload is not None else [{
        "hash": "h1", "name": "N1", "state": "downloading",
        "progress": 0.25, "dlspeed": 100, "eta": 60,
    }]
    sp = sab_payload if sab_payload is not None else {
        "queue": {"slots": [{"nzo_id": "S1", "filename": "F1.nzb",
                              "status": "Downloading", "percentage": 10.0,
                              "timeleft": "0:05:00"}]}
    }
    class _C:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, *a, **kw):
            return _MockResp(text="Ok.")
        async def get(self, url, *a, **kw):
            if "torrents/info" in url:
                return _MockResp(json_data=qp)
            return _MockResp(json_data=sp)
    return _C


# ───────────────────── 1. /queue reads from cache, not httpx ─────────────────

def test_build_queue_rows_uses_cache_not_httpx(fresh_db, isolated_cache):
    """_build_queue_rows must read download-client state from the cache.
    We pre-populate a snapshot, then patch httpx to explode — if the
    function works, it isn't calling httpx."""
    import routers.queue_ as q
    import status_cache
    from status_cache import DownloadClientSnapshot

    # Seed a grabbed volume whose hash matches the cached snapshot.
    with sqlite3.connect(fresh_db) as c:
        c.execute("INSERT INTO series(id, title, search_pattern)"
                  " VALUES(1, 'S', 'S')")
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, download_id,"
            " torrent_name, grabbed_at, client)"
            " VALUES(1, 1.0, 'grabbed', 'abc', 'rel', datetime('now'),"
            " 'qbittorrent')"
        )

    now = datetime.now(timezone.utc)
    isolated_cache._qbit = DownloadClientSnapshot(
        items={"abc": {"hash": "abc", "name": "live-name", "state": "downloading",
                       "progress": 77.0, "dlspeed": 999, "eta": 42,
                       "client": "qbittorrent", "error_message": ""}},
        fetched_at=now,
        last_success_at=now,
    )

    class _Explode:
        def __init__(self, *a, **kw):
            raise AssertionError("queue render touched httpx")
    import httpx
    with patch.object(httpx, "AsyncClient", new=_Explode):
        rows, *_ = asyncio.run(q._build_queue_rows())

    row = next(r for r in rows if r["hash"] == "abc")
    assert row["progress"] == 77.0, "row progress should come from cached snapshot"
    assert row["dlspeed"]  == 999


# ───────────────────── 2. refresh populates snapshots ────────────────────────

def test_cache_refresh_populates_snapshots(fresh_db, isolated_cache):
    """A successful refresh writes fresh items + last_success_at into both
    qBit and SAB snapshots."""
    import status_cache
    cli = _mixed_ok_client()
    with patch("status_cache.httpx.AsyncClient", new=cli):
        ran = asyncio.run(isolated_cache.refresh())

    assert ran is True
    qs = isolated_cache.snapshot_qbit()
    ss = isolated_cache.snapshot_sab()
    assert qs is not None and "h1" in qs.items
    assert ss is not None and "S1" in ss.items
    assert qs.last_success_at is not None
    assert ss.last_success_at is not None
    assert qs.error is None and ss.error is None


# ───────────────────── 3. failed refresh preserves last-known ────────────────

def test_cache_refresh_preserves_last_known_on_failure(fresh_db, isolated_cache):
    """After a successful refresh then a failing refresh, the snapshot
    must still carry the last-known items (so the UI keeps showing them)
    plus an `error` field describing what went wrong."""
    import status_cache

    # Round 1 — both upstreams OK.
    with patch("status_cache.httpx.AsyncClient", new=_mixed_ok_client()):
        asyncio.run(isolated_cache.refresh())
    prev_items = dict(isolated_cache.snapshot_qbit().items)
    prev_success = isolated_cache.snapshot_qbit().last_success_at
    assert prev_items

    # Round 2 — everything explodes.
    class _Boom:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw): raise ConnectionError("nope")
        async def get(self, *a, **kw):  raise ConnectionError("nope")

    with patch("status_cache.httpx.AsyncClient", new=_Boom):
        asyncio.run(isolated_cache.refresh())

    snap = isolated_cache.snapshot_qbit()
    assert snap.items == prev_items, "items should survive a failed refresh"
    assert snap.last_success_at == prev_success, (
        "last_success_at must not advance on failure"
    )
    assert snap.error and "ConnectionError" in snap.error


# ───────────────────── 4. refresh is single-flight ───────────────────────────

def test_cache_refresh_is_single_flight(fresh_db, isolated_cache):
    """Two concurrent refresh() calls must collapse: one actually polls,
    the other returns immediately with False."""
    import status_cache

    call_count = {"n": 0}
    class _SlowOnce:
        def __init__(self, *a, **kw):
            call_count["n"] += 1
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw):
            await asyncio.sleep(0.2)
            return _MockResp(text="Ok.")
        async def get(self, url, *a, **kw):
            await asyncio.sleep(0.2)
            if "torrents/info" in url:
                return _MockResp(json_data=[])
            return _MockResp(json_data={"queue": {"slots": []}})

    async def _two():
        with patch("status_cache.httpx.AsyncClient", new=_SlowOnce):
            return await asyncio.gather(
                isolated_cache.refresh(),
                isolated_cache.refresh(),
            )

    results = asyncio.run(_two())
    # Exactly one caller ran; the other collapsed.
    assert sorted(results) == [False, True], f"expected [False, True], got {results}"


# ───────────────────── 5. freshness_label transitions ────────────────────────

def test_freshness_label_transitions(isolated_cache):
    """warming_up (no snap) → live (<60s) → stale (60-300s) → unavailable (>300s)."""
    from status_cache import DownloadClientSnapshot

    # None snapshot → warming_up.
    assert isolated_cache.freshness_label(None) == "warming_up"

    now = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)
    def _snap(age_s: int | None):
        return DownloadClientSnapshot(
            items={},
            fetched_at=now,
            last_success_at=None if age_s is None else now - timedelta(seconds=age_s),
        )

    # Never succeeded → unavailable.
    assert isolated_cache.freshness_label(_snap(None), now=now) == "unavailable"
    # 5s ago → live; 59s → live.
    assert isolated_cache.freshness_label(_snap(5),  now=now) == "live"
    assert isolated_cache.freshness_label(_snap(59), now=now) == "live"
    # 61s → stale; 299s → stale.
    assert isolated_cache.freshness_label(_snap(61),  now=now) == "stale"
    assert isolated_cache.freshness_label(_snap(299), now=now) == "stale"
    # 301s → unavailable.
    assert isolated_cache.freshness_label(_snap(301), now=now) == "unavailable"


# ───────────────────── 6. manual refresh returns immediately ─────────────────

def test_manual_refresh_endpoint_returns_immediately(fresh_db, isolated_cache):
    """POST /api/queue/refresh must kick off a refresh and return without
    waiting for it. We replace the cache's refresh() with one that sleeps
    5s; the endpoint must still respond in <500ms (fire-and-forget)."""
    from fastapi.testclient import TestClient
    import main

    refresh_started = {"when": None}

    async def _slow_refresh():
        refresh_started["when"] = time.perf_counter()
        await asyncio.sleep(5.0)
        return True

    isolated_cache.refresh = _slow_refresh

    # Fetch the seeded API key so the /api/ middleware lets us in.
    api_key = main.get_cfg("api_key") or main.ensure_api_key()

    with TestClient(main.app) as client:
        t0 = time.perf_counter()
        r = client.post(
            "/api/queue/refresh",
            headers={"X-Api-Key": api_key},
        )
        dt = time.perf_counter() - t0

    assert r.status_code == 202, f"expected 202, got {r.status_code}: {r.text}"
    assert dt < 0.5, (
        f"/api/queue/refresh should be fire-and-forget; took {dt:.3f}s"
    )
    assert refresh_started["when"] is not None, (
        "endpoint returned without even scheduling the refresh"
    )


# ───────────────────── 7. queue renders "warming up" pre-refresh ─────────────

def test_queue_renders_warming_up_before_first_refresh(fresh_db, isolated_cache):
    """Before the background loop has run once, the queue page must still
    render — and the freshness context must say 'warming_up' for any
    configured client."""
    from routers.queue_ import _build_queue_rows, _queue_status_context

    rows, *_ = asyncio.run(_build_queue_rows())
    # No crash. Rows may be empty (no grabbed volumes seeded here), but
    # the call completed without touching any live upstream.
    assert isinstance(rows, list)

    ctx = _queue_status_context()
    # Both clients are seeded in fresh_db → both should be warming_up.
    assert ctx["qbit"]["label"] == "warming_up"
    assert ctx["sab"]["label"]  == "warming_up"
    assert ctx["qbit"]["age_seconds"] is None
    assert ctx["sab"]["age_seconds"] is None


# ───────────────────── 8. refresh loop survives exceptions ───────────────────

def test_refresh_loop_survives_exception(isolated_cache, monkeypatch):
    """download_status_refresh_loop must keep looping even if one
    refresh() raises. We replace refresh with one that raises on the
    first call and succeeds on the second, then let the loop spin
    briefly and assert we got >=2 calls."""
    import status_cache

    # Shorten the sleep interval so the test finishes fast.
    monkeypatch.setattr(status_cache, "STATUS_REFRESH_INTERVAL_SECONDS", 0.05)

    calls = {"n": 0}
    async def _fake_refresh():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first call boom")
        return True

    monkeypatch.setattr(isolated_cache, "refresh", _fake_refresh)

    async def _run():
        task = asyncio.create_task(status_cache.download_status_refresh_loop())
        # Loop has a 2s startup delay — bypass it by sleeping, then give
        # the loop time for several iterations.
        await asyncio.sleep(2.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
    assert calls["n"] >= 2, (
        f"loop stopped after first exception; only {calls['n']} refresh(es) ran"
    )
