"""Regression tests for download-client status concurrency + timeouts.

Historical context: a single rapid-fire test of `/queue` produced outliers
of 61.5s and 30.5s. The original fix moved qBit+SAB fetches concurrent
with a 2.5s per-call timeout and a 0.8s render budget.

The follow-up fix (this PR) moved those fetches off the request path
entirely: the queue page renders from `status_cache.DOWNLOAD_STATUS_CACHE`
and a background loop refreshes the snapshots every 20s. The per-call
timeout still exists in the cache layer — it just bounds the background
poll instead of the page render.

This file now pins the cache-layer behaviour:
  - status_cache._fetch_qbit / _fetch_sab are invoked concurrently by
    DownloadStatusCache.refresh so one slow upstream can't block the other
  - each fetch respects STATUS_UPSTREAM_TIMEOUT_SECONDS
  - a failed fetch is surfaced as a raised exception (recorded by the
    cache's _merge_snapshot, not silently swallowed)
  - _build_queue_rows renders from the cache only — it makes no live
    httpx calls, so a dead upstream cannot affect render latency
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


@pytest.fixture
def fresh_db():
    """Temp DB with one qBit + one SAB download client seeded."""
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-qtimeout-keys-")

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


# ───────────────────── mock upstream plumbing ────────────────────────────────

class _MockResp:
    def __init__(self, status_code=200, text="Ok.", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data or {}
    def json(self):
        return self._json


def _mk_client(delay: float, *, fail: bool = False, payload=None,
               recorder: list | None = None):
    """Return an AsyncClient stand-in whose every call sleeps `delay` and
    then returns the given payload (or raises on `fail`)."""
    class _C:
        def __init__(self, *a, **kw):
            if recorder is not None:
                recorder.append(("init", kw.get("timeout")))
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, *a, **kw):
            if recorder is not None:
                recorder.append(("POST", url, time.perf_counter()))
            await asyncio.sleep(delay)
            if fail:
                import httpx
                raise httpx.TimeoutException("simulated")
            return _MockResp(text="Ok.")
        async def get(self, url, *a, **kw):
            if recorder is not None:
                recorder.append(("GET", url, time.perf_counter()))
            await asyncio.sleep(delay)
            if fail:
                import httpx
                raise httpx.TimeoutException("simulated")
            return _MockResp(json_data=payload or {})
    return _C


def _qbit_payload():
    """Canonical qBit /torrents/info response shape."""
    return [{
        "hash": "abc123",
        "name": "Series Vol.01",
        "state": "downloading",
        "progress": 0.5,
        "dlspeed": 1024,
        "eta": 120,
        "stateMessage": "",
    }]


def _sab_payload():
    """Canonical SAB queue response shape."""
    return {"queue": {"slots": [{
        "nzo_id": "SAB-1",
        "filename": "Series Ch.5.nzb",
        "status": "Downloading",
        "percentage": 42.5,
        "timeleft": "0:10:00",
    }]}}


# ───────────────────── cache-layer upstream timeout ──────────────────────────

def test_status_cache_upstream_timeout_is_bounded():
    """The background refresh must not hang forever on a dead client.
    STATUS_UPSTREAM_TIMEOUT_SECONDS bounds per-call wall clock; it can be
    more generous than the old 2.5s (refresh is off the request path) but
    must still be finite and reasonable."""
    from status_cache import STATUS_UPSTREAM_TIMEOUT_SECONDS
    assert 0 < STATUS_UPSTREAM_TIMEOUT_SECONDS <= 10.0


def test_status_cache_fetchers_pass_timeout_to_httpx():
    """Every AsyncClient instantiation in the cache fetchers must use
    STATUS_UPSTREAM_TIMEOUT_SECONDS, not a literal."""
    import status_cache as sc
    rec: list = []
    qc = {"host": "http://qb.local", "username": "u", "password": "p", "category": "manga"}
    cli = _mk_client(0.0, payload=_qbit_payload(), recorder=rec)
    with patch("status_cache.httpx.AsyncClient", new=cli):
        asyncio.run(sc._fetch_qbit(qc))
    assert rec, "no httpx.AsyncClient instantiations captured"
    for item in rec:
        if item[0] == "init":
            assert item[1] == sc.STATUS_UPSTREAM_TIMEOUT_SECONDS, (
                f"AsyncClient timeout drifted: {item[1]!r}"
            )


# ───────────────────── concurrency: qBit + SAB run in parallel ───────────────

def test_qbit_and_sab_are_fetched_concurrently(fresh_db):
    """DownloadStatusCache.refresh must run the two polls concurrently.
    qBit = POST auth (0.5s) + GET info (0.5s) = 1.0s
    SAB  = GET queue (0.5s)                   = 0.5s
    wall-clock = max(1.0, 0.5) = 1.0s. Serial would be 1.5s."""
    import status_cache as sc
    from status_cache import DownloadStatusCache

    class _Mixed:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, *a, **kw):
            await asyncio.sleep(0.5)
            return _MockResp(text="Ok.")
        async def get(self, url, *a, **kw):
            await asyncio.sleep(0.5)
            if "torrents/info" in url:
                return _MockResp(json_data=_qbit_payload())
            return _MockResp(json_data=_sab_payload())

    cache = DownloadStatusCache()
    with patch("status_cache.httpx.AsyncClient", new=_Mixed):
        t0 = time.perf_counter()
        asyncio.run(cache.refresh())
        dt = time.perf_counter() - t0

    assert dt < 1.3, (
        f"wall-clock {dt:.3f}s suggests sequential execution "
        "(qbit 1.0s + sab 0.5s = 1.5s); expected concurrent (~1.0s)"
    )
    assert cache.snapshot_qbit().items.get("abc123")
    assert cache.snapshot_sab().items.get("SAB-1")


def test_slow_qbit_does_not_block_sab(fresh_db):
    """qBit fetcher hangs past its timeout; SAB responds fast. The cache
    must surface fresh SAB data and record a qBit error — not block the
    whole refresh on the slow side."""
    import status_cache as sc
    from status_cache import DownloadStatusCache

    class _Mixed:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, *a, **kw):
            # qBit auth path — hang to force timeout.
            if "qb.local" in url or "qbit.local" in url:
                import httpx
                await asyncio.sleep(sc.STATUS_UPSTREAM_TIMEOUT_SECONDS + 1.0)
                raise httpx.TimeoutException("qbit slow")
            raise AssertionError(f"unexpected POST: {url}")
        async def get(self, url, *a, **kw):
            if "sab.local" in url:
                await asyncio.sleep(0.05)
                return _MockResp(json_data=_sab_payload())
            raise AssertionError(f"unexpected GET: {url}")

    cache = DownloadStatusCache()
    with patch("status_cache.httpx.AsyncClient", new=_Mixed):
        t0 = time.perf_counter()
        asyncio.run(cache.refresh())
        dt = time.perf_counter() - t0

    # SAB must have succeeded despite qBit hanging.
    assert cache.snapshot_sab().items.get("SAB-1"), "SAB data lost to slow qBit"
    # qBit snapshot records the failure.
    assert cache.snapshot_qbit().error is not None
    assert cache.snapshot_qbit().last_success_at is None
    # Wall-clock equals the qBit timeout (gather waits for both).
    assert dt < sc.STATUS_UPSTREAM_TIMEOUT_SECONDS + 2.0


def test_slow_sab_does_not_block_qbit(fresh_db):
    """Symmetric check: SAB hangs, qBit is fast."""
    import status_cache as sc
    from status_cache import DownloadStatusCache

    class _Mixed:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, *a, **kw):
            if "qbit.local" in url or "qb.local" in url:
                await asyncio.sleep(0.05)
                return _MockResp(text="Ok.")
            raise AssertionError(f"unexpected POST: {url}")
        async def get(self, url, *a, **kw):
            if "qbit.local" in url or "qb.local" in url:
                await asyncio.sleep(0.05)
                return _MockResp(json_data=_qbit_payload())
            if "sab.local" in url:
                import httpx
                await asyncio.sleep(sc.STATUS_UPSTREAM_TIMEOUT_SECONDS + 1.0)
                raise httpx.TimeoutException("sab slow")
            raise AssertionError(f"unexpected GET: {url}")

    cache = DownloadStatusCache()
    with patch("status_cache.httpx.AsyncClient", new=_Mixed):
        t0 = time.perf_counter()
        asyncio.run(cache.refresh())
        dt = time.perf_counter() - t0

    assert cache.snapshot_qbit().items.get("abc123"), "qBit data lost to slow SAB"
    assert cache.snapshot_sab().error is not None
    assert cache.snapshot_sab().last_success_at is None
    assert dt < sc.STATUS_UPSTREAM_TIMEOUT_SECONDS + 2.0


# ───────────────────── queue render is decoupled from upstreams ──────────────

def test_build_queue_rows_makes_no_live_http_calls(fresh_db):
    """The whole point of the cache: /queue rendering must NOT make any
    httpx.AsyncClient() calls. A dead upstream can't block the page."""
    import routers.queue_ as q
    import httpx as _httpx

    with sqlite3.connect(fresh_db) as c:
        c.execute("INSERT INTO series(id, title, search_pattern)"
                  " VALUES(7, 'Test', 'Test')")
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, download_id,"
            " torrent_name, grabbed_at, client)"
            " VALUES(7, 1.0, 'grabbed', 'dlid-1', 'rel.torrent',"
            " datetime('now'), 'qbittorrent')"
        )

    created: list = []

    class _Tripwire:
        def __init__(self, *a, **kw):
            created.append(("AsyncClient", kw))
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw):
            raise AssertionError("queue render issued a live POST")
        async def get(self, *a, **kw):
            raise AssertionError("queue render issued a live GET")

    # Patch the httpx.AsyncClient symbol in the queue router's module
    # namespace. If queue_ no longer imports httpx, AttributeError would
    # fire — which is fine and is itself a sign we're decoupled.
    with patch.object(_httpx, "AsyncClient", new=_Tripwire):
        rows, *_ = asyncio.run(q._build_queue_rows())

    assert created == [], (
        f"_build_queue_rows must not instantiate httpx clients; got {created}"
    )
    assert any(r["hash"] == "dlid-1" for r in rows)


def test_build_queue_rows_renders_without_cache_data(fresh_db):
    """Before the first cache refresh, the queue page must still render
    whatever DB data exists. No raise, no block, no wait."""
    import routers.queue_ as q
    from status_cache import DownloadStatusCache
    import status_cache

    with sqlite3.connect(fresh_db) as c:
        c.execute("INSERT INTO series(id, title, search_pattern)"
                  " VALUES(7, 'Test', 'Test')")
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, download_id,"
            " torrent_name, grabbed_at, client)"
            " VALUES(7, 1.0, 'grabbed', 'dlid-1', 'rel.torrent',"
            " datetime('now'), 'qbittorrent')"
        )

    # Swap in a brand-new (empty) cache so no prior test's snapshot leaks in.
    orig = status_cache.DOWNLOAD_STATUS_CACHE
    status_cache.DOWNLOAD_STATUS_CACHE = DownloadStatusCache()
    try:
        t0 = time.perf_counter()
        rows, *_ = asyncio.run(q._build_queue_rows())
        dt = time.perf_counter() - t0
    finally:
        status_cache.DOWNLOAD_STATUS_CACHE = orig

    # Cold cache → render is pure DB work. Should be well under 100ms even
    # on a loaded CI runner.
    assert dt < 1.0, f"cold-cache render took {dt:.3f}s — too slow"
    assert any(r["hash"] == "dlid-1" for r in rows), (
        f"expected grabbed volume in queue rows, got: {rows}"
    )


# ───────────────────── happy-path mapping preserved ──────────────────────────

def test_qbit_happy_path_maps_fields_correctly():
    """Field map from the qBit response must match what the template
    consumes — regardless of whether the fetcher lives in queue_ or
    status_cache."""
    import status_cache as sc
    cli = _mk_client(0.0, payload=_qbit_payload())
    with patch("status_cache.httpx.AsyncClient", new=cli):
        qbit = asyncio.run(sc._fetch_qbit({
            "host": "http://qb.local", "username": "u", "password": "p",
            "category": "manga"
        }))
    assert "abc123" in qbit
    t = qbit["abc123"]
    assert t["hash"]     == "abc123"
    assert t["name"]     == "Series Vol.01"
    assert t["state"]    == "downloading"
    assert t["progress"] == 50.0   # 0.5 * 100
    assert t["dlspeed"]  == 1024
    assert t["eta"]      == 120
    assert t["client"]   == "qbittorrent"


def test_sab_happy_path_maps_fields_correctly():
    import status_cache as sc
    cli = _mk_client(0.0, payload=_sab_payload())
    with patch("status_cache.httpx.AsyncClient", new=cli):
        sab = asyncio.run(sc._fetch_sab({
            "host": "http://sab.local", "password": "sabkey"
        }))
    assert "SAB-1" in sab
    s = sab["SAB-1"]
    assert s["hash"]     == "SAB-1"
    assert s["name"]     == "Series Ch.5.nzb"
    assert s["state"]    == "downloading"  # lowercased
    assert s["progress"] == 42.5
    assert s["client"]   == "sabnzbd"


# ───────────────────── config-absent shortcuts ───────────────────────────────

def test_fetchers_return_empty_when_client_config_missing():
    """Absent download-client config → {} without touching the network."""
    import status_cache as sc
    assert asyncio.run(sc._fetch_qbit(None)) == {}
    assert asyncio.run(sc._fetch_qbit({})) == {}
    assert asyncio.run(sc._fetch_sab(None)) == {}
    assert asyncio.run(sc._fetch_sab({})) == {}
    assert asyncio.run(sc._fetch_sab({"host": "http://sab.local"})) == {}  # no apikey
