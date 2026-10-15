"""NZBGet RPC destinations through real builders and normalized client loading."""
from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

import clients
import shared
from routers import download_clients


NZB_URL = "https://indexer.invalid/release.nzb"
USERNAME = "qa-user"
PASSWORD = "qa-pass"
RESERVED_USERNAME = "qa@user/"
RESERVED_PASSWORD = "p:a@ss/?#%+"
ENCODED_AUTH = "qa%40user%2F:p%3Aa%40ss%2F%3F%23%25%2B@"

RPC_CASES = [
    pytest.param("nzb.example", None, 0, "http://nzb.example:6789/jsonrpc", id="bare-default"),
    pytest.param("192.0.2.10", 6790, 0, "http://192.0.2.10:6790/jsonrpc", id="bare-ipv4"),
    pytest.param("nzb.example/rpc/", 6791, 1, "https://nzb.example:6791/rpc/jsonrpc", id="bare-tls-path"),
    pytest.param("https://nzb.example:6799/nzb/", 6790, 0, "https://nzb.example:6799/nzb/jsonrpc", id="dns-embedded"),
    pytest.param("https://192.0.2.10/rpc/", None, 0, "https://192.0.2.10:6789/rpc/jsonrpc", id="ipv4-tls-default"),
    pytest.param("https://[2001:db8::1]:6799/rpc/", 6790, 0, "https://[2001:db8::1]:6799/rpc/jsonrpc", id="ipv6-embedded"),
    pytest.param("http://[2001:db8::1]/api/", 6791, 0, "http://[2001:db8::1]:6791/api/jsonrpc", id="ipv6-separate"),
    pytest.param("https://[2001:db8::1]", None, 0, "https://[2001:db8::1]:6789/jsonrpc", id="ipv6-default"),
    pytest.param("[2001:db8::1]", 6791, 1, "https://[2001:db8::1]:6791/jsonrpc", id="bare-ipv6-tls"),
]


def _client(host: str, port: int | None = None, use_ssl: int = 0) -> dict[str, Any]:
    return {
        "type": "nzbget", "name": "NZBGet QA", "id": 17,
        "host": host, "port": port, "use_ssl": use_ssl,
        "username": USERNAME, "password": PASSWORD, "category": "comics",
    }


def _transport(
    monkeypatch: pytest.MonkeyPatch, *, result: int = 57, offline: bool = False,
) -> list[httpx.Request]:
    original = httpx.AsyncClient
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if offline:
            raise httpx.ConnectError("synthetic offline endpoint", request=request)
        method = json.loads(request.content)["method"]
        return httpx.Response(200, json={"result": "21.1" if method == "version" else result})

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return original(*args, **kwargs, transport=httpx.MockTransport(handle), trust_env=False)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setattr(clients, "log_event", lambda *args, **kwargs: None)
    return requests


def _assert_request(
    request: httpx.Request, public_url: str, payload: dict[str, Any],
    *, username: str = USERNAME, password: str = PASSWORD,
) -> None:
    expected = httpx.URL(public_url)
    assert request.method == "POST"
    assert (request.url.scheme, request.url.host, request.url.port, request.url.path) == (
        expected.scheme, expected.host, expected.port, expected.path,
    )
    assert request.url.query == b""
    assert json.loads(request.content) == payload
    assert base64.b64decode(request.headers["Authorization"].removeprefix("Basic ")) == (
        f"{username}:{password}".encode()
    )


@pytest.mark.parametrize("host,port,use_ssl,expected", RPC_CASES)
def test_rpc_builder_preserves_destination(
    host: str, port: int | None, use_ssl: int, expected: str,
) -> None:
    client = _client(host, port, use_ssl)
    client.update(username="", password="")
    assert download_clients._nzbget_rpc_url(client) == expected


def test_rpc_builder_percent_encodes_separate_credentials() -> None:
    client = _client("https://nzb.example/rpc/", 6791)
    client.update(username=RESERVED_USERNAME, password=RESERVED_PASSWORD)
    assert download_clients._nzbget_rpc_url(client) == (
        f"https://{ENCODED_AUTH}nzb.example:6791/rpc/jsonrpc"
    )


@pytest.mark.parametrize("operation", ["version", "append"])
def test_reserved_credentials_reach_httpx_as_basic_auth(
    monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    requests = _transport(monkeypatch)
    client = _client("https://nzb.example/rpc/", 6791)
    client.update(username=RESERVED_USERNAME, password=RESERVED_PASSWORD)
    if operation == "version":
        assert asyncio.run(download_clients._test_client(client)) == (True, "NZBGet 21.1")
        payload = {"method": "version", "params": []}
    else:
        assert asyncio.run(clients.nzbget_grab(NZB_URL, client)) == (True, "57", True)
        payload = {"method": "append", "params": [NZB_URL, "comics", 0, False, "", 0, "SCORE"]}
    assert len(requests) == 1
    _assert_request(
        requests[0], "https://nzb.example:6791/rpc/jsonrpc", payload,
        username=RESERVED_USERNAME, password=RESERVED_PASSWORD,
    )


@pytest.mark.parametrize("host,port,use_ssl,expected", RPC_CASES)
def test_connection_test_posts_version_to_exact_rpc_url(
    monkeypatch: pytest.MonkeyPatch,
    host: str, port: int | None, use_ssl: int, expected: str,
) -> None:
    requests = _transport(monkeypatch)
    outcome = asyncio.run(download_clients._test_client(_client(host, port, use_ssl)))
    assert outcome == (True, "NZBGet 21.1")
    assert len(requests) == 1
    _assert_request(requests[0], expected, {"method": "version", "params": []})


@pytest.mark.parametrize("host,port,use_ssl,expected", RPC_CASES)
def test_grab_posts_unchanged_append_payload_to_exact_rpc_url(
    monkeypatch: pytest.MonkeyPatch,
    host: str, port: int | None, use_ssl: int, expected: str,
) -> None:
    requests = _transport(monkeypatch)
    assert asyncio.run(clients.nzbget_grab(NZB_URL, _client(host, port, use_ssl))) == (True, "57", True)
    assert len(requests) == 1
    _assert_request(requests[0], expected, {
        "method": "append", "params": [NZB_URL, "comics", 0, False, "", 0, "SCORE"],
    })


@pytest.mark.parametrize("result,offline,expected", [
    (0, False, (False, None, True)),
    (-1, False, (False, None, True)),
    (57, True, (False, None, False)),
])
def test_grab_preserves_business_rejection_and_connection_health(
    monkeypatch: pytest.MonkeyPatch, result: int, offline: bool,
    expected: tuple[bool, str | None, bool],
) -> None:
    requests = _transport(monkeypatch, result=result, offline=offline)
    assert asyncio.run(clients.nzbget_grab(NZB_URL, _client("https://nzb.example/rpc", 6791))) == expected
    assert len(requests) == 1
    assert requests[0].url.host == "nzb.example"


def test_url_builder_error_is_caught_as_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _transport(monkeypatch)
    assert asyncio.run(clients.nzbget_grab(NZB_URL, _client("https://nzb.example:invalid"))) == (False, None, False)
    assert requests == []


def test_grab_keeps_category_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _transport(monkeypatch)
    monkeypatch.setitem(shared.CONFIG, "category", "fallback-category")
    client = _client("nzb.example")
    client["category"] = ""
    assert asyncio.run(clients.nzbget_grab(NZB_URL, client)) == (True, "57", True)
    assert json.loads(requests[0].content)["params"][1] == "fallback-category"


@pytest.fixture
def client_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import main

    path = tmp_path / "clients.db"
    monkeypatch.setattr(main, "DB_PATH", str(path))
    monkeypatch.setattr(shared, "DB_PATH", str(path))
    main.init_db()
    return path


@pytest.mark.parametrize("host,port,normalized,rpc_url", [
    ("https://[2001:db8::4]/nzbget/", 6791, "https://[2001:db8::4]:6791/nzbget", "https://[2001:db8::4]:6791/nzbget/jsonrpc"),
    ("http://nzb.example/rpc/", 6791, "http://nzb.example:6791/rpc", "http://nzb.example:6791/rpc/jsonrpc"),
    ("https://192.0.2.10:6799/rpc/", 6791, "https://192.0.2.10:6799/rpc", "https://192.0.2.10:6799/rpc/jsonrpc"),
])
def test_real_client_selection_and_bound_loading_reach_normalized_rpc(
    client_db: Path, monkeypatch: pytest.MonkeyPatch,
    host: str, port: int, normalized: str, rpc_url: str,
) -> None:
    with sqlite3.connect(client_db) as db:
        db.execute(
            "INSERT INTO download_clients(id,name,type,host,port,use_ssl,username,password,category,enabled) "
            "VALUES(17,'NZBGet QA','nzbget',?,?,1,?,?,'comics',1)",
            (host, port, USERNAME, PASSWORD),
        )
        db.execute(
            "INSERT INTO client_breaker_state(client_id,failures,open_until) VALUES(17,2,0)"
        )
    with shared.get_db() as db:
        selected = download_clients.get_client_for_protocol(db, "nzb")
    assert selected is not None and selected["host"] == normalized
    bound = clients.load_bound_download_client(17, expected_type="nzbget", expected_name="NZBGet QA")
    assert bound.reason == "matched" and bound.client is not None
    assert bound.client["host"] == normalized

    requests = _transport(monkeypatch)
    result = asyncio.run(clients.grab_url(NZB_URL, protocol="nzb"))
    assert tuple(result) == (True, "nzbget", "57", True)
    assert result.download_client_id == 17
    assert asyncio.run(clients.nzbget_grab(NZB_URL, bound.client)) == (True, "57", True)
    assert len(requests) == 2
    for request in requests:
        _assert_request(request, rpc_url, {
            "method": "append", "params": [NZB_URL, "comics", 0, False, "", 0, "SCORE"],
        })
    with shared.get_db() as db:
        assert db.execute("SELECT * FROM client_breaker_state WHERE client_id=17").fetchone() is None
