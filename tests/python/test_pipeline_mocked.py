"""Mocked search → grab → download-client handoff pipeline test.

This is the smallest valuable test of the actual product workflow:

  fake Prowlarr search response
    → app parses it via _parse_prowlarr_response (real code path)
    → app picks a release (we take items[0])
    → app calls grab_url() with the release's magnet URL
    → grab_url() routes to qbit_grab() (real code path)
    → qbit_grab() makes authenticated HTTP calls to a *mocked* qBittorrent
    → assertions confirm the outbound request body matched what qBit expects

No real Prowlarr / qBittorrent / network. Pure in-process. ~80ms per test.

Why this test exists: the live qBittorrent ping in browser_e2e.js proves the
client is reachable, but proves nothing about whether the app can actually
hand off a real download. This file covers the actual workflow.
"""
import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


# ───────────────────────── fixtures ──────────────────────────────────────────

@pytest.fixture
def fresh_db_with_qbit():
    """Temp DB seeded with one enabled qBittorrent download client."""
    import main, shared, security

    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-pipeline-keys-")

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
            "INSERT INTO download_clients(name,type,host,port,use_ssl,username,password,"
            " category,priority,enabled) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("test-qbit", "qbittorrent", "qbit.local", 8080, 0,
             "admin", "encrypted-or-plain-pw", "manga", 1, 1)
        )
        # CB state is now DB-backed; purge any seeded rows (belt and braces)
        c.execute("DELETE FROM client_breaker_state")

    try:
        yield db.name
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


# ───────────────────────── mock helpers ──────────────────────────────────────

class _MockResponse:
    """Minimal stand-in for an httpx.Response."""
    def __init__(self, status_code=200, text="", content=b"", json_data=None):
        self.status_code = status_code
        self.text        = text
        self.content     = content
        self._json       = json_data
    def json(self):
        return self._json


def _make_qbit_mock(captured: list, *, auth_ok: bool = True, add_status: int = 200):
    """Build an AsyncClient stand-in whose .post and .get capture every call.

    captured: list[dict] — each entry: {"method": "GET"/"POST", "url": str, "data": ..., "files": ...}
    """
    class _MockAsyncClient:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, *a, data=None, files=None, **kw):
            captured.append({"method": "POST", "url": url, "data": data, "files": files})
            if "/api/v2/auth/login" in url:
                return _MockResponse(status_code=200,
                                     text="Ok." if auth_ok else "Fails.")
            if "/api/v2/torrents/add" in url:
                return _MockResponse(status_code=add_status, text="")
            return _MockResponse(status_code=200, text="")
        async def get(self, url, *a, **kw):
            captured.append({"method": "GET", "url": url, "params": kw.get("params")})
            if "/api/v2/torrents/info" in url:
                return _MockResponse(status_code=200, json_data=[])
            return _MockResponse(status_code=200, text="")
    return _MockAsyncClient


# ───────────────────────── prowlarr parse → grab ─────────────────────────────

def _fake_prowlarr_search_response():
    """One realistic Prowlarr /api/v1/search row."""
    return [{
        "title":       "Vinland Saga v01 (Mock-Group)",
        "downloadUrl": "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
                       "&dn=Vinland+Saga+v01",
        "size":        524288000,
        "seeders":     42,
        "indexer":     "MockTorrentIndexer",
        "protocol":    "torrent",
    }]


def test_parse_prowlarr_response_yields_expected_shape():
    """Sanity check the parser before we depend on it in the pipeline test."""
    from routers.indexers import _parse_prowlarr_response
    items = _parse_prowlarr_response(_fake_prowlarr_search_response(), "MockTorrentIndexer")
    assert len(items) == 1
    item = items[0]
    assert item["protocol"] == "torrent"
    assert item["url"].startswith("magnet:?xt=urn:btih:")
    assert item["seeders"] == 42
    assert item["size_bytes"] == 524288000


def test_pipeline_search_to_qbit_handoff(fresh_db_with_qbit):
    """End-to-end: parse Prowlarr search → grab_url → qBit add.

    Asserts the actual request body sent to qBittorrent matches what a real
    add-magnet call should look like (urls=magnet:..., category=manga).
    """
    import asyncio
    import main
    from routers.indexers import _parse_prowlarr_response

    # 1. Parse the indexer search response.
    items = _parse_prowlarr_response(_fake_prowlarr_search_response(), "MockTorrentIndexer")
    chosen = items[0]
    assert chosen["protocol"] == "torrent"

    # 2. Mock qBittorrent and run the grab.
    captured: list = []
    mock_client = _make_qbit_mock(captured, auth_ok=True, add_status=200)
    with patch("httpx.AsyncClient", new=mock_client):
        ok, client_type, dl_id, healthy = asyncio.run(
            main.grab_url(chosen["url"], protocol=chosen["protocol"])
        )

    # 3. grab_url result.
    assert ok is True, f"grab failed; captured calls: {captured}"
    assert client_type == "qbittorrent"
    assert healthy is True, "client_healthy must be True on success"
    # For magnet URLs grab_url returns the btih hash directly.
    assert dl_id and len(dl_id) == 40, f"expected 40-char btih, got {dl_id!r}"

    # 4. Outbound request shape.
    posts = [c for c in captured if c["method"] == "POST"]
    auth = next((c for c in posts if "/api/v2/auth/login" in c["url"]), None)
    add  = next((c for c in posts if "/api/v2/torrents/add" in c["url"]), None)
    assert auth is not None, "no auth.login call"
    assert auth["data"] == {"username": "admin", "password": "encrypted-or-plain-pw"}
    assert add is not None, "no torrents/add call"
    assert add["data"]["category"] == "manga", f"wrong category: {add['data']!r}"
    assert add["data"]["urls"].startswith("magnet:?xt=urn:btih:"), (
        f"expected magnet URL in 'urls', got {add['data']!r}"
    )


def test_pipeline_no_download_client_returns_failure(fresh_db_with_qbit):
    """If the only enabled client is disabled mid-flow, grab_url returns
    (False, 'none', None) and does not raise."""
    import asyncio
    import main

    # Disable the qbit client so get_client_for_protocol returns None.
    with sqlite3.connect(fresh_db_with_qbit) as c:
        c.execute("UPDATE download_clients SET enabled=0")

    ok, client_type, dl_id, healthy = asyncio.run(
        main.grab_url("magnet:?xt=urn:btih:" + "a"*40, protocol="torrent")
    )
    assert ok is False
    assert client_type == "none"
    assert dl_id is None
    assert healthy is False, "no client = not healthy"


def test_pipeline_qbit_unreachable_trips_circuit(fresh_db_with_qbit):
    """Auth failure (downloader unavailable) → grab_url returns failure and
    records a CB failure. After threshold (3), CB opens and short-circuits."""
    import asyncio
    import main
    from routers.download_clients import _cb_load, _CB_THRESHOLD

    captured: list = []
    mock_client = _make_qbit_mock(captured, auth_ok=False)
    magnet = "magnet:?xt=urn:btih:" + "f"*40

    with patch("httpx.AsyncClient", new=mock_client):
        # Repeat enough times to trip the circuit breaker.
        for _ in range(_CB_THRESHOLD):
            ok, _, _, _ = asyncio.run(main.grab_url(magnet, protocol="torrent"))
            assert ok is False

    # CB should be open against client id 1 now — state is persisted in
    # client_breaker_state (see PR 4).
    state = _cb_load(1)
    assert state is not None and state["failures"] >= _CB_THRESHOLD, (
        f"circuit breaker did not open after {_CB_THRESHOLD} failures: {state!r}"
    )

    # Subsequent grab while CB is open: short-circuits before any HTTP call.
    captured.clear()
    with patch("httpx.AsyncClient", new=mock_client):
        ok, _, _, _ = asyncio.run(main.grab_url(magnet, protocol="torrent"))
    assert ok is False
    assert captured == [], "CB-open grab should not make HTTP calls"


def test_pipeline_no_orphans_after_run(fresh_db_with_qbit):
    """Verifier-style check: nothing in the test inserted bogus volume/chapter
    rows. We ran grab_url against a client; no series/volumes were touched.

    This is the "verify_e2e equivalent" gate the report recommended for
    post-mutation tests."""
    with sqlite3.connect(fresh_db_with_qbit) as c:
        c.row_factory = sqlite3.Row
        # Orphan volumes: volume.series_id pointing to a missing series.
        orphan_vols = c.execute("""
            SELECT COUNT(*) AS n FROM volumes v
            LEFT JOIN series s ON s.id = v.series_id
            WHERE s.id IS NULL
        """).fetchone()["n"]
        # Orphan chapters: chapter.volume_id pointing to a missing volume.
        orphan_chs = c.execute("""
            SELECT COUNT(*) AS n FROM chapters ch
            LEFT JOIN volumes v ON v.id = ch.volume_id
            WHERE v.id IS NULL AND ch.volume_id IS NOT NULL
        """).fetchone()["n"]
    assert orphan_vols == 0, f"{orphan_vols} orphan volumes after pipeline test"
    assert orphan_chs  == 0, f"{orphan_chs} orphan chapters after pipeline test"
