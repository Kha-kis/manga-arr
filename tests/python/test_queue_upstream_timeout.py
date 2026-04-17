"""Regression tests for the queue-page upstream timeout + concurrency fix.

Production incident: a single rapid-fire test of `/queue` produced outliers
of 61.5s and 30.5s. `_build_queue_rows` was fetching qBittorrent and
SABnzbd status sequentially with 10s timeouts, so a stalled upstream
blocked page rendering for tens of seconds.

This file pins the new behaviour:
  - qBit and SAB are fetched concurrently (asyncio.gather)
  - each fetch respects QUEUE_UPSTREAM_TIMEOUT_SECONDS (2.5s)
  - a slow or failed upstream never blocks the other
  - queue rows render using whatever DB data is available even when both
    upstreams fail
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


def _mk_client(delay: float, *, fail: bool = False, payload: dict | None = None,
               recorder: list | None = None):
    """Return an AsyncClient stand-in whose every call sleeps `delay` and
    then returns the given payload (or raises TimeoutError if `fail`)."""
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
                raise httpx.TimeoutException("simulated")  # type: ignore[name-defined]
            return _MockResp(text="Ok.")
        async def get(self, url, *a, **kw):
            if recorder is not None:
                recorder.append(("GET", url, time.perf_counter()))
            await asyncio.sleep(delay)
            if fail:
                raise httpx.TimeoutException("simulated")  # type: ignore[name-defined]
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


# ───────────────────── the fix: timeout value ────────────────────────────────

def test_timeout_constant_is_at_most_three_seconds():
    """Gate against drift back to 10s. 2.5s is the shipped value; if we
    ever need to raise it, do so deliberately — not silently."""
    from routers.queue_ import QUEUE_UPSTREAM_TIMEOUT_SECONDS
    assert QUEUE_UPSTREAM_TIMEOUT_SECONDS <= 3.0
    assert QUEUE_UPSTREAM_TIMEOUT_SECONDS > 0


def test_render_budget_is_strictly_below_upstream_timeout():
    """The render-path budget caps how long queue pagination waits on
    upstreams; it MUST be below the per-call httpx timeout so a slow
    upstream gets cut off at the render layer rather than dragging the
    page to 2.5s."""
    from routers.queue_ import (QUEUE_UPSTREAM_TIMEOUT_SECONDS,
                                QUEUE_RENDER_UPSTREAM_BUDGET)
    assert 0 < QUEUE_RENDER_UPSTREAM_BUDGET < QUEUE_UPSTREAM_TIMEOUT_SECONDS
    # And the budget should be tight enough to feel responsive — under 1s.
    assert QUEUE_RENDER_UPSTREAM_BUDGET <= 1.0


def test_build_queue_rows_respects_render_budget(fresh_db):
    """When both upstreams hang, _build_queue_rows must return within
    ~QUEUE_RENDER_UPSTREAM_BUDGET seconds and show rows (minus live data)."""
    import time as _time
    import routers.queue_ as q
    db_path = fresh_db

    # Seed one grabbed volume so the page has something to render.
    import sqlite3 as _sqlite
    with _sqlite.connect(db_path) as c:
        c.execute("INSERT INTO series(id, title, search_pattern)"
                  " VALUES(7, 'Test', 'Test')")
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, download_id,"
            " torrent_name, grabbed_at, client)"
            " VALUES(7, 1.0, 'grabbed', 'hang-id', 'rel.torrent',"
            " datetime('now'), 'qbittorrent')"
        )

    class _Hang:
        """Both upstreams sleep past the render budget."""
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw):
            await asyncio.sleep(q.QUEUE_RENDER_UPSTREAM_BUDGET + 2.0)
            class _R: text = "Ok."; status_code = 200
            return _R()
        async def get(self, *a, **kw):
            await asyncio.sleep(q.QUEUE_RENDER_UPSTREAM_BUDGET + 2.0)
            class _R:
                status_code = 200
                def json(self_): return {}
            return _R()

    t0 = _time.perf_counter()
    with patch("routers.queue_.httpx.AsyncClient", new=_Hang):
        rows, *_ = asyncio.run(q._build_queue_rows())
    dt = _time.perf_counter() - t0

    # The render must not wait for the full upstream timeout; budget + a
    # little slack for DB work.
    assert dt < q.QUEUE_RENDER_UPSTREAM_BUDGET + 0.5, (
        f"/queue render took {dt:.2f}s with upstreams hung; "
        f"budget is {q.QUEUE_RENDER_UPSTREAM_BUDGET}s"
    )
    # And the rows still render from DB even though live upstream data
    # was unavailable.
    assert any(r["hash"] == "hang-id" for r in rows), (
        "queue rows must render from DB when upstreams time out"
    )


def test_helpers_pass_constant_as_httpx_timeout():
    """Every AsyncClient instantiation in the queue helpers must use
    QUEUE_UPSTREAM_TIMEOUT_SECONDS, not a literal."""
    import routers.queue_ as q
    rec: list = []
    qc = {"host": "http://qb.local", "username": "u", "password": "p", "category": "manga"}
    cli = _mk_client(0.0, payload=_qbit_payload(), recorder=rec)
    global httpx
    import httpx
    with patch("routers.queue_.httpx.AsyncClient", new=cli):
        asyncio.run(q._fetch_qbit_status(qc))
    assert rec, "no httpx.AsyncClient instantiations captured"
    for kind, ts, *rest in rec:
        if kind == "init":
            assert ts == q.QUEUE_UPSTREAM_TIMEOUT_SECONDS, (
                f"AsyncClient timeout drifted: {ts!r}"
            )


# ───────────────────── concurrency: qBit + SAB run in parallel ───────────────

def test_qbit_and_sab_are_fetched_concurrently(fresh_db):
    """With concurrent execution:
       qBit = POST auth (0.5s) + GET info (0.5s) = 1.0s
       SAB  = GET queue (0.5s)                   = 0.5s
       wall-clock = max(1.0, 0.5) = 1.0s.
    Serial would be 1.0 + 0.5 = 1.5s. Allow 1.3s to catch regression
    with headroom for CI noise."""
    import routers.queue_ as q
    global httpx
    import httpx

    cli = _mk_client(0.5, payload={**_qbit_payload()[0], "queue": {"slots": []}})
    # qbit expects a list; sab expects a dict — the _mk_client payload is
    # a shared response, so use a minimal mix that both parsers tolerate.
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
            if "sab" in url or "mode=queue" in str(kw.get("params", "")):
                return _MockResp(json_data=_sab_payload())
            return _MockResp(json_data={})

    async def _gather_under_patch():
        t0 = time.perf_counter()
        qbit, sab = await asyncio.gather(
            q._fetch_qbit_status({"host": "http://qb.local", "username": "u",
                                   "password": "p", "category": "manga"}),
            q._fetch_sab_status({"host": "http://sab.local", "password": "sabkey"}),
        )
        return time.perf_counter() - t0, qbit, sab

    with patch("routers.queue_.httpx.AsyncClient", new=_Mixed):
        dt, qbit, sab = asyncio.run(_gather_under_patch())

    assert dt < 1.3, (
        f"wall-clock {dt:.3f}s suggests sequential execution "
        "(qbit 1.0s + sab 0.5s = 1.5s); expected concurrent (~1.0s)"
    )
    # Both responses landed.
    assert qbit and "abc123" in qbit
    assert sab  and "SAB-1" in sab


def test_total_wall_clock_when_both_slow_is_close_to_one_timeout():
    """If both upstreams are pinned at 2.5s, total wall-clock must
    approach 2.5s (one timeout) — not 5s+ (two serial timeouts)."""
    import routers.queue_ as q
    global httpx
    import httpx

    cli = _mk_client(q.QUEUE_UPSTREAM_TIMEOUT_SECONDS + 0.5, fail=True)

    async def _run():
        t0 = time.perf_counter()
        qbit, sab = await asyncio.gather(
            q._fetch_qbit_status({"host": "http://qb.local", "username": "u",
                                   "password": "p", "category": "manga"}),
            q._fetch_sab_status({"host": "http://sab.local", "password": "sabkey"}),
        )
        return time.perf_counter() - t0, qbit, sab

    with patch("routers.queue_.httpx.AsyncClient", new=cli):
        dt, qbit, sab = asyncio.run(_run())

    # With concurrent execution and both clients failing at 3.0s, wall
    # clock stays near 3.0s. A serial fallback would be ~6s.
    assert dt < q.QUEUE_UPSTREAM_TIMEOUT_SECONDS + 2.0, (
        f"wall-clock {dt:.3f}s suggests fetches ran serially — "
        "gather or timeout behaviour regressed"
    )
    assert qbit == {}
    assert sab  == {}


# ───────────────────── one-side failure must not block the other ─────────────

def test_slow_qbit_does_not_block_sab(fresh_db):
    """qBit hangs past the timeout; SAB is fast. Wall-clock equals the
    qBit timeout (gather waits for the slower side) but SAB's data must
    still appear in the output."""
    import routers.queue_ as q
    global httpx
    import httpx

    rec: list = []

    # A single client class whose behaviour depends on the URL it's called on.
    class _Mixed:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, *a, **kw):
            # qBit login path — hang to force timeout.
            if "qb.local" in url:
                await asyncio.sleep(q.QUEUE_UPSTREAM_TIMEOUT_SECONDS + 1.0)
                raise httpx.TimeoutException("qbit slow")
            raise AssertionError(f"unexpected POST: {url}")
        async def get(self, url, *a, **kw):
            # SAB queue path — respond fast.
            if "sab.local" in url:
                await asyncio.sleep(0.05)
                return _MockResp(json_data=_sab_payload())
            raise AssertionError(f"unexpected GET: {url}")

    async def _run():
        t0 = time.perf_counter()
        qbit, sab = await asyncio.gather(
            q._fetch_qbit_status({"host": "http://qb.local", "username": "u",
                                   "password": "p", "category": "manga"}),
            q._fetch_sab_status({"host": "http://sab.local", "password": "sabkey"}),
        )
        return time.perf_counter() - t0, qbit, sab

    with patch("routers.queue_.httpx.AsyncClient", new=_Mixed):
        dt, qbit, sab = asyncio.run(_run())

    # qBit hung → returned empty. SAB completed → has data. Wall-clock
    # equals qBit's timeout (gather waits for both), not sum of both.
    assert qbit == {}, "slow qBit should degrade to empty, not succeed"
    assert "SAB-1" in sab, "SAB data should survive slow qBit"
    assert dt < q.QUEUE_UPSTREAM_TIMEOUT_SECONDS + 2.0, (
        f"wall-clock {dt:.3f}s too long — gather or timeout regressed"
    )


def test_slow_sab_does_not_block_qbit(fresh_db):
    """Symmetric check: SAB hangs, qBit is fast."""
    import routers.queue_ as q
    global httpx
    import httpx

    class _Mixed:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, *a, **kw):
            if "qb.local" in url:
                await asyncio.sleep(0.05)
                return _MockResp(text="Ok.")
            raise AssertionError(f"unexpected POST: {url}")
        async def get(self, url, *a, **kw):
            if "qb.local" in url:
                await asyncio.sleep(0.05)
                return _MockResp(json_data=_qbit_payload())
            if "sab.local" in url:
                await asyncio.sleep(q.QUEUE_UPSTREAM_TIMEOUT_SECONDS + 1.0)
                raise httpx.TimeoutException("sab slow")
            raise AssertionError(f"unexpected GET: {url}")

    async def _run():
        t0 = time.perf_counter()
        qbit, sab = await asyncio.gather(
            q._fetch_qbit_status({"host": "http://qb.local", "username": "u",
                                   "password": "p", "category": "manga"}),
            q._fetch_sab_status({"host": "http://sab.local", "password": "sabkey"}),
        )
        return time.perf_counter() - t0, qbit, sab

    with patch("routers.queue_.httpx.AsyncClient", new=_Mixed):
        dt, qbit, sab = asyncio.run(_run())

    assert sab == {}, "slow SAB should degrade to empty"
    assert "abc123" in qbit, "qBit data should survive slow SAB"
    assert dt < q.QUEUE_UPSTREAM_TIMEOUT_SECONDS + 2.0


# ───────────────────── failure modes never raise ─────────────────────────────

def test_fetchers_return_empty_dict_on_connection_error():
    """Any exception in the mock (network error, SSL, DNS) → {}."""
    import routers.queue_ as q

    class _Boom:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw): raise ConnectionError("boom")
        async def get(self, *a, **kw):  raise ConnectionError("boom")

    with patch("routers.queue_.httpx.AsyncClient", new=_Boom):
        qbit = asyncio.run(q._fetch_qbit_status({
            "host": "http://qb.local", "username": "u", "password": "p",
            "category": "manga"
        }))
        sab = asyncio.run(q._fetch_sab_status({
            "host": "http://sab.local", "password": "sabkey"
        }))
    assert qbit == {}
    assert sab  == {}


def test_fetchers_return_empty_when_client_config_missing():
    """Absent download-client config → {} without touching the network."""
    import routers.queue_ as q
    assert asyncio.run(q._fetch_qbit_status(None)) == {}
    assert asyncio.run(q._fetch_qbit_status({})) == {}
    assert asyncio.run(q._fetch_sab_status(None)) == {}
    assert asyncio.run(q._fetch_sab_status({})) == {}
    assert asyncio.run(q._fetch_sab_status({"host": "http://sab.local"})) == {}  # no apikey


def test_qbit_returns_empty_when_auth_fails():
    """qBit returns 'Fails.' body → treat as no torrents (user sees empty
    queue, not a stack trace)."""
    import routers.queue_ as q

    class _AuthFail:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, *a, **kw): return _MockResp(text="Fails.")
        async def get(self, url, *a, **kw):
            raise AssertionError("should never GET after auth fail")

    with patch("routers.queue_.httpx.AsyncClient", new=_AuthFail):
        qbit = asyncio.run(q._fetch_qbit_status({
            "host": "http://qb.local", "username": "u", "password": "bad",
            "category": "manga"
        }))
    assert qbit == {}


# ───────────────────── happy-path mapping preserved ──────────────────────────

def test_qbit_happy_path_maps_fields_correctly():
    """Regression: the field map from the qBit response must match what
    the template consumes."""
    import routers.queue_ as q
    cli = _mk_client(0.0, payload=_qbit_payload())
    with patch("routers.queue_.httpx.AsyncClient", new=cli):
        qbit = asyncio.run(q._fetch_qbit_status({
            "host": "http://qb.local", "username": "u", "password": "p",
            "category": "manga"
        }))
    assert "abc123" in qbit
    t = qbit["abc123"]
    assert t["hash"]       == "abc123"
    assert t["name"]       == "Series Vol.01"
    assert t["state"]      == "downloading"
    assert t["progress"]   == 50.0   # 0.5 * 100
    assert t["dlspeed"]    == 1024
    assert t["eta"]        == 120
    assert t["client"]     == "qbittorrent"


def test_sab_happy_path_maps_fields_correctly():
    import routers.queue_ as q
    cli = _mk_client(0.0, payload=_sab_payload())
    with patch("routers.queue_.httpx.AsyncClient", new=cli):
        sab = asyncio.run(q._fetch_sab_status({
            "host": "http://sab.local", "password": "sabkey"
        }))
    assert "SAB-1" in sab
    s = sab["SAB-1"]
    assert s["hash"]     == "SAB-1"
    assert s["name"]     == "Series Ch.5.nzb"
    assert s["state"]    == "downloading"  # lowercased
    assert s["progress"] == 42.5
    assert s["client"]   == "sabnzbd"


# ───────────────────── end-to-end: page renders without upstreams ────────────

def test_build_queue_rows_renders_from_db_when_both_upstreams_fail(fresh_db):
    """Even when both qBit and SAB are unreachable, the queue page must
    still render whatever DB-level queue data exists. No raise, no block."""
    import routers.queue_ as q

    # Seed a grabbed volume so _build_queue_rows has something to report.
    with sqlite3.connect(fresh_db) as c:
        c.execute("INSERT INTO series(id, title, search_pattern)"
                  " VALUES(7, 'Test', 'Test')")
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, download_id,"
            " torrent_name, grabbed_at, client)"
            " VALUES(7, 1.0, 'grabbed', 'dlid-1', 'rel.torrent',"
            " datetime('now'), 'qbittorrent')"
        )

    class _Boom:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw): raise ConnectionError("boom")
        async def get(self, *a, **kw):  raise ConnectionError("boom")

    with patch("routers.queue_.httpx.AsyncClient", new=_Boom):
        result = asyncio.run(q._build_queue_rows())
    rows = result[0]

    # One row for the grabbed volume, no upstream enrichment.
    assert any(r["hash"] == "dlid-1" for r in rows), (
        f"expected grabbed volume in queue rows, got: {rows}"
    )
