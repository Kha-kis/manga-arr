"""Regression tests for /health upstream probe behaviour.

Issue #31 follow-up B: after the DB-layer fix in PR #32, `/health` still
had p50 ~3-7s and worst ~30s. Profiling traced the cause to the page
opening ~10 separate `get_db()` connections (one per check), each
exposed to the busy_timeout during background-writer contention — not
the upstream HTTP probes themselves.

These tests pin:
  - DB snapshot is fetched exactly once for the whole page
  - independent upstream probes run concurrently
  - one slow provider does not block the others
  - optional (warning-severity) probes get the shorter timeout
  - core app health still returns quickly when upstreams are dead
  - response semantics unchanged (200 + rendered HTML with check rows)
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
def fresh_app():
    """Boot main:app against a fresh DB + small fixture data."""
    import main, shared, security
    from fastapi.testclient import TestClient

    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-health-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()
    main.ensure_api_key()

    # Seed minimal data so every check has something to report on.
    with sqlite3.connect(db.name) as c:
        c.execute("INSERT INTO indexers(name, type, url, enabled) VALUES('idx', 'prowlarr', 'http://192.0.2.1', 1)")
        c.execute(
            "INSERT INTO download_clients(name, type, host, port, enabled, password, category, priority)"
            " VALUES('qb', 'qbittorrent', 'http://192.0.2.99', 8080, 1, 'p', 'manga', 1)"
        )
        c.execute(
            "INSERT INTO download_clients(name, type, host, port, enabled, password, category, priority)"
            " VALUES('sab', 'sabnzbd', 'http://192.0.2.98', 8080, 1, 'sabkey', 'manga', 2)"
        )

    client = TestClient(main.app, follow_redirects=False)
    client.get("/")  # prime CSRF cookie

    try:
        yield client, db.name
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


# ────────────────────── constants pinned ─────────────────────────────────────

def test_optional_upstream_timeout_is_short():
    """Warning-severity upstreams must have a noticeably shorter timeout
    than high-severity ones — otherwise a slow optional provider stalls
    the endpoint."""
    from routers.health_ import HEALTH_TIMEOUT_HIGH, HEALTH_TIMEOUT_WARNING
    assert HEALTH_TIMEOUT_WARNING <= 3.0
    assert HEALTH_TIMEOUT_HIGH >= HEALTH_TIMEOUT_WARNING


def test_high_severity_timeout_is_bounded():
    """Even high-severity timeouts should be bounded — 30s would make
    page navigation unusable under real network trouble."""
    from routers.health_ import HEALTH_TIMEOUT_HIGH
    assert HEALTH_TIMEOUT_HIGH <= 10.0


# ────────────────────── snapshot is taken once ───────────────────────────────

def test_snapshot_helper_is_called_once_per_page(fresh_app):
    """Each /health hit must invoke _health_db_snapshot exactly once.
    Regression guard against a future refactor reintroducing per-check
    get_db connections."""
    import routers.health_ as h
    client, _ = fresh_app
    call_count = {"n": 0}
    real = h._health_db_snapshot
    def _counting(*a, **kw):
        call_count["n"] += 1
        return real(*a, **kw)
    with patch.object(h, "_health_db_snapshot", side_effect=_counting):
        # Mock away the async upstream probes so the test doesn't hang on
        # the hardcoded 192.0.2.x TEST-NET-1 addresses.
        class _FailClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **kw): raise ConnectionError("no upstream")
            async def get(self,  *a, **kw): raise ConnectionError("no upstream")
        with patch("routers.health_.httpx.AsyncClient", new=_FailClient):
            r = client.get("/health")
    assert r.status_code == 200
    assert call_count["n"] == 1, (
        f"_health_db_snapshot called {call_count['n']} times — page should only open DB once"
    )


def test_snapshot_contains_expected_keys(fresh_app):
    """Shape guard: each check expects specific keys. Missing keys surface
    at runtime as KeyError."""
    import routers.health_ as h
    snap = h._health_db_snapshot()
    expected = {
        'indexers_enabled', 'download_clients_enabled', 'quality_profiles',
        'root_folders', 'orphan_series_rf', 'wanted_volumes',
        'last_grab', 'last_rss_poll', 'qbit_client', 'sab_client',
    }
    assert expected.issubset(snap.keys()), (
        f"snapshot missing keys: {expected - set(snap.keys())}"
    )


# ────────────────────── one slow provider doesn't block the rest ─────────────

def test_slow_komga_does_not_block_other_probes(fresh_app):
    """If Komga hangs, /health must return when Komga's short timeout
    expires (~2s), not when the remaining probes have all finished."""
    import routers.health_ as h
    client, db_path = fresh_app

    # Configure Komga so _komga() actually fires (otherwise it returns
    # 'Not configured' immediately).
    with sqlite3.connect(db_path) as c:
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('komga_url', 'http://192.0.2.50')")
    import main
    main.load_config()

    import httpx

    class _MixedClient:
        """qBit/SAB answer fast; Komga simulates the httpx timeout by
        sleeping exactly `timeout` seconds and then raising TimeoutException.

        We simulate the timeout inside the mock because real httpx timeout
        logic doesn't run when we patch out AsyncClient entirely."""
        def __init__(self, *a, **kw):
            self.timeout = float(kw.get("timeout", 0)) or 0
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, *a, **kw):
            class _R: status_code = 200; text = "Ok."
            await asyncio.sleep(0.01)
            return _R()
        async def get(self, url, *a, **kw):
            if "192.0.2.50" in url:  # komga — simulate timeout
                await asyncio.sleep(self.timeout)
                raise httpx.TimeoutException("simulated komga timeout")
            class _R:
                status_code = 200; text = "4.6.0"
                def json(self_): return {"version": "4.0"}
            await asyncio.sleep(0.01)
            return _R()

    t0 = time.perf_counter()
    with patch("routers.health_.httpx.AsyncClient", new=_MixedClient):
        r = client.get("/health")
    dt = time.perf_counter() - t0

    assert r.status_code == 200
    # Under the old 6s-everywhere regime this would be 6s minimum. With
    # the 2s warning timeout on Komga, /health returns within a small
    # multiple of 2s. Allow 4s to catch regressions with CI headroom.
    assert dt < 4.0, (
        f"/health took {dt:.2f}s with only Komga slow; expected <4s — "
        "optional-provider timeout may have drifted"
    )


# ────────────────────── core health remains fast when all upstreams dead ─────

def test_core_health_fast_when_all_upstreams_unreachable(fresh_app):
    """All upstream probes raise ConnectionError immediately — endpoint
    must still return quickly (under 2s) with the DB-only check results."""
    import routers.health_ as h
    client, _ = fresh_app

    class _Boom:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw): raise ConnectionError("boom")
        async def get(self,  *a, **kw): raise ConnectionError("boom")

    t0 = time.perf_counter()
    with patch("routers.health_.httpx.AsyncClient", new=_Boom):
        r = client.get("/health")
    dt = time.perf_counter() - t0

    assert r.status_code == 200
    assert dt < 2.0, (
        f"/health took {dt:.2f}s with upstreams dead; "
        "should be fast since exceptions bail out immediately"
    )
    # Page still renders with checks.
    body = r.text
    assert "Root Folders" in body or "Indexers" in body


# ────────────────────── response semantics preserved ─────────────────────────

def test_health_page_returns_200_and_html(fresh_app):
    import routers.health_ as h
    client, _ = fresh_app

    class _Boom:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw): raise ConnectionError("boom")
        async def get(self,  *a, **kw): raise ConnectionError("boom")

    with patch("routers.health_.httpx.AsyncClient", new=_Boom):
        r = client.get("/health")
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("text/html")


def test_each_check_function_uses_snapshot_not_get_db():
    """Source-level guard: the per-check functions inside build_health_payload
    must not re-open get_db() individually. They should consume the
    pre-fetched snap[...] dict."""
    import inspect, routers.health_ as h
    src = inspect.getsource(h.build_health_payload)
    # Count actual `with get_db() as` blocks, not prose references. After
    # the fix all route data comes from `_health_db_snapshot`, which
    # already includes the aggregate rows for the bottom of the page.
    usages = src.count("with get_db() as")
    assert usages == 0, (
        f"build_health_payload reopens get_db {usages} times — should consolidate "
        "into the snapshot. Each extra open exposes the page to the 5s "
        "busy_timeout during write-lock contention (issue #31)."
    )
    assert "_health_db_snapshot()" in src


def test_docker_healthcheck_still_uses_root_not_health():
    """Regression guard: Docker's container health should remain tied to
    `/` (the library page), not `/health`. Binding Docker health to
    /health would make container status depend on optional upstream
    integrations (Komga, SAB, qBit), which isn't the intent."""
    with open(os.path.join(os.path.dirname(__file__), "..", "..",
                           "docker-compose.yml")) as f:
        compose = f.read()
    # Extract the healthcheck block and confirm it probes / not /health.
    assert "urlopen('http://localhost:8000/')" in compose
    assert "urlopen('http://localhost:8000/health')" not in compose
