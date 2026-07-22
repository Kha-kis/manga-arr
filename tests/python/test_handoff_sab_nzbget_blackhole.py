"""Mocked handoff coverage for non-qBit download clients.

Pass 1 covered the qBit handoff. This file extends the same pattern to:
  - sab_grab    (SABnzbd /api?mode=addurl)
  - nzbget_grab (NZBGet JSON-RPC)
  - blackhole_grab (writes a .torrent / .magnet file to a folder)

For each: one happy-path test (asserts the outbound request shape and the
returned (ok, dl_id, healthy) triple) plus one failure-path test (asserts
the function reports the failure correctly without raising).
"""
import asyncio
import os
import sys
import tempfile
from unittest.mock import patch

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


# ───────────────────────── shared mock plumbing ──────────────────────────────

class _MockResp:
    def __init__(self, status_code=200, json_data=None, text="", content=b""):
        self.status_code = status_code
        self._json   = json_data
        self.text    = text
        self.content = content
    def json(self):
        return self._json


def _client_factory(*, post_resp=None, get_resp=None, captured=None):
    captured = captured if captured is not None else []
    class _C:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, **kw):
            captured.append({"method": "POST", "url": url, **kw})
            return post_resp or _MockResp()
        async def get(self, url, **kw):
            captured.append({"method": "GET", "url": url, **kw})
            return get_resp or _MockResp()
    return _C, captured


# ───────────────────────── SABnzbd ───────────────────────────────────────────

def test_sab_grab_happy_path_returns_nzo_id():
    """SAB returns {status: True, nzo_ids: [...]}: ok=True, healthy=True."""
    import main
    captured: list = []
    cli, _ = _client_factory(
        post_resp=_MockResp(200, json_data={"status": True, "nzo_ids": ["NZO-001"]}),
        captured=captured,
    )
    with patch("httpx.AsyncClient", new=cli):
        ok, nzo_id, healthy = asyncio.run(main.sab_grab(
            "http://indexer/release.nzb",
            client={"host": "http://sab.local:65080", "password": "sab-key",
                    "category": "manga"},
        ))
    assert (ok, nzo_id, healthy) == (True, "NZO-001", True)
    # Outbound shape: POST to /api with mode=addurl, name=URL, cat, apikey.
    post = captured[0]
    assert post["url"] == "http://sab.local:65080/api"
    params = post["params"]
    assert params["mode"]   == "addurl"
    assert params["name"]   == "http://indexer/release.nzb"
    assert params["cat"]    == "manga"
    assert params["apikey"] == "sab-key"


def test_sab_grab_returns_failure_without_apikey():
    """No apikey configured → fail-fast, do not contact SAB."""
    import main
    captured: list = []
    cli, _ = _client_factory(captured=captured)
    with (
        patch("httpx.AsyncClient", new=cli),
        patch("clients.log_event") as log_event,
    ):
        ok, nzo_id, healthy = asyncio.run(main.sab_grab(
            "http://indexer/release.nzb",
            client={"host": "http://sab.local:65080", "password": ""},
        ))
    assert ok is False
    assert nzo_id is None
    assert healthy is False
    assert captured == []  # never made an HTTP call
    log_event.assert_called_once_with(
        "configuration_error",
        "[SAB] API key is not configured",
        dedup=True,
    )


def test_sab_connection_test_rejects_missing_apikey_without_http():
    from routers.download_clients import _test_client

    cli, captured = _client_factory()
    with patch("routers.download_clients.httpx.AsyncClient", new=cli):
        ok, message = asyncio.run(
            _test_client(
                {
                    "type": "sabnzbd",
                    "host": "http://sab.local",
                    "port": 65080,
                    "url_base": "",
                    "password": "",
                }
            )
        )

    assert ok is False
    assert message == "API key is required for SABnzbd"
    assert captured == []


def test_sab_connection_test_requires_authenticated_queue_payload():
    from routers.download_clients import _test_client

    cli, captured = _client_factory(
        get_resp=_MockResp(200, json_data={"queue": {"version": "5.0.4", "slots": []}})
    )
    with patch("routers.download_clients.httpx.AsyncClient", new=cli):
        ok, message = asyncio.run(
            _test_client(
                {
                    "type": "sabnzbd",
                    "host": "http://sab.local",
                    "port": 65080,
                    "url_base": "",
                    "password": "sab-key",
                }
            )
        )

    assert ok is True
    assert message == "SABnzbd 5.0.4"
    assert captured[0]["params"] == {
        "mode": "queue",
        "start": 0,
        "limit": 0,
        "apikey": "sab-key",
        "output": "json",
    }


def test_sab_connection_test_rejects_http_200_api_error():
    from routers.download_clients import _test_client

    cli, _ = _client_factory(
        get_resp=_MockResp(
            200,
            json_data={"status": False, "error": "API Key Incorrect"},
        )
    )
    with patch("routers.download_clients.httpx.AsyncClient", new=cli):
        ok, message = asyncio.run(
            _test_client(
                {
                    "type": "sabnzbd",
                    "host": "http://sab.local",
                    "port": 65080,
                    "url_base": "",
                    "password": "wrong-key",
                }
            )
        )

    assert ok is False
    assert message == "SABnzbd API error: API Key Incorrect"


def test_sab_grab_returns_failure_when_sab_rejects():
    """SAB reachable but rejects (status=false): ok=False, healthy=True
    (don't trip the circuit breaker on a business-logic rejection)."""
    import main
    captured: list = []
    cli, _ = _client_factory(
        post_resp=_MockResp(200, json_data={"status": False, "error": "duplicate"}),
        captured=captured,
    )
    with patch("httpx.AsyncClient", new=cli):
        ok, nzo_id, healthy = asyncio.run(main.sab_grab(
            "http://indexer/dup.nzb",
            client={"host": "http://sab.local:65080", "password": "sab-key"},
        ))
    assert ok is False
    assert nzo_id is None
    assert healthy is True


def test_sab_grab_connection_error_is_unhealthy():
    """Network exception → ok=False, healthy=False (CB should trip)."""
    import main
    class _BoomClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw):
            raise ConnectionError("boom")
    with patch("httpx.AsyncClient", new=_BoomClient):
        ok, nzo_id, healthy = asyncio.run(main.sab_grab(
            "http://indexer/release.nzb",
            client={"host": "http://sab.local:65080", "password": "sab-key"},
        ))
    assert (ok, nzo_id, healthy) == (False, None, False)


# ───────────────────────── NZBGet ────────────────────────────────────────────

def test_nzbget_grab_happy_path_returns_string_id():
    """NZBGet returns {result: <int_id>}: ok=True, dl_id is str."""
    import main
    captured: list = []
    cli, _ = _client_factory(
        post_resp=_MockResp(200, json_data={"result": 42}),
        captured=captured,
    )
    with patch("httpx.AsyncClient", new=cli):
        ok, dl_id, healthy = asyncio.run(main.nzbget_grab(
            "http://indexer/release.nzb",
            client={"host": "nzb.local", "username": "u", "password": "p",
                    "port": 6789, "category": "manga"},
        ))
    assert ok is True
    assert dl_id == "42"
    assert healthy is True
    # Outbound: POST to http://u:p@nzb.local:6789/jsonrpc with method=append
    post = captured[0]
    assert "@nzb.local:6789/jsonrpc" in post["url"]
    body = post["json"]
    assert body["method"] == "append"
    # params[0] = NZB URL, params[1] = category
    assert body["params"][0] == "http://indexer/release.nzb"
    assert body["params"][1] == "manga"


def test_nzbget_grab_business_failure_stays_healthy():
    """NZBGet reachable but result<=0 means add rejected; healthy=True."""
    import main
    captured: list = []
    cli, _ = _client_factory(
        post_resp=_MockResp(200, json_data={"result": 0}),
        captured=captured,
    )
    with patch("httpx.AsyncClient", new=cli):
        ok, dl_id, healthy = asyncio.run(main.nzbget_grab(
            "http://indexer/release.nzb",
            client={"host": "nzb.local", "username": "u", "password": "p",
                    "port": 6789, "category": "manga"},
        ))
    assert (ok, dl_id, healthy) == (False, None, True)


def test_nzbget_grab_connection_error_is_unhealthy():
    import main
    class _BoomClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw):
            raise ConnectionError("boom")
    with patch("httpx.AsyncClient", new=_BoomClient):
        ok, dl_id, healthy = asyncio.run(main.nzbget_grab(
            "http://indexer/release.nzb",
            client={"host": "nzb.local", "username": "u", "password": "p",
                    "port": 6789, "category": "manga"},
        ))
    assert (ok, dl_id, healthy) == (False, None, False)


# ───────────────────────── blackhole ─────────────────────────────────────────

def test_blackhole_grab_writes_magnet_file(tmp_path):
    """Magnet URL → writes a .magnet file directly, no HTTP."""
    import main
    folder = tmp_path / "blackhole"
    folder.mkdir()
    magnet = "magnet:?xt=urn:btih:" + "a"*40 + "&dn=Foo"
    ok, dl_id, healthy = asyncio.run(main.blackhole_grab(
        magnet,
        client={"host": str(folder)},
        torrent_name="Foo",
    ))
    assert ok is True
    assert healthy is True
    # File written: name was sanitised + .magnet extension
    files = list(folder.iterdir())
    assert len(files) == 1
    assert files[0].suffix == ".magnet"
    assert files[0].read_text() == magnet


def test_blackhole_grab_downloads_torrent_when_url(tmp_path):
    """HTTP torrent URL → fetch and write bytes."""
    import main
    folder = tmp_path / "blackhole"
    folder.mkdir()
    captured: list = []
    cli, _ = _client_factory(
        get_resp=_MockResp(200, content=b"d8:announce..."),  # fake torrent bytes
        captured=captured,
    )
    with patch("httpx.AsyncClient", new=cli):
        ok, dl_id, healthy = asyncio.run(main.blackhole_grab(
            "http://indexer/release.torrent",
            client={"host": str(folder)},
            torrent_name="release",
        ))
    assert ok is True
    assert healthy is True
    files = list(folder.iterdir())
    assert len(files) == 1
    assert files[0].suffix == ".torrent"
    assert files[0].read_bytes() == b"d8:announce..."


def test_blackhole_grab_missing_folder_is_unhealthy(tmp_path):
    """Folder doesn't exist → unhealthy (configuration error)."""
    import main
    nope = tmp_path / "does-not-exist"
    ok, dl_id, healthy = asyncio.run(main.blackhole_grab(
        "magnet:?xt=urn:btih:" + "b"*40,
        client={"host": str(nope)},
    ))
    assert (ok, dl_id, healthy) == (False, None, False)
