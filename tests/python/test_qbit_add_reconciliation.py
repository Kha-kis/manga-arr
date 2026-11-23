"""Focused qBittorrent add-response reconciliation regressions."""

import asyncio
import hashlib
import sys
from collections.abc import Callable
from contextlib import nullcontext
from urllib.parse import parse_qs

import httpx
import pytest

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401


class _Response:
    def __init__(
        self,
        status_code: int = 200,
        *,
        text: str = "",
        content: bytes = b"",
        json_data: object = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.content = content
        self._json_data = json_data

    def json(self) -> object:
        return self._json_data


def _torrent_fixture(kind: str = "v1") -> tuple[bytes, str, tuple[str, ...]]:
    common = (
        b"4:name42:One Piece v106 (2024) (Digital) (1r0n).cbz12:piece lengthi16384e"
    )
    file_tree = b"d0:d6:lengthi12345e11:pieces root32:" + b"b" * 32 + b"ee"
    if kind == "v1":
        info = b"d6:lengthi12345e" + common + b"6:pieces20:" + b"a" * 20 + b"e"
    elif kind == "pure-v2":
        info = b"d9:file tree" + file_tree + b"12:meta versioni2e" + common + b"e"
    elif kind == "hybrid":
        info = (
            b"d9:file tree"
            + file_tree
            + b"6:lengthi12345e12:meta versioni2e"
            + common
            + b"6:pieces20:"
            + b"a" * 20
            + b"e"
        )
    else:
        raise AssertionError(f"unknown fixture kind: {kind}")

    payload = b"d8:announce14:http://tracker4:info" + info + b"e"
    v1_hash = hashlib.sha1(info, usedforsecurity=False).hexdigest()
    if kind == "v1":
        return payload, v1_hash, (v1_hash,)

    v2_hash = hashlib.sha256(info, usedforsecurity=False).digest()[:20].hex()
    lookup_hashes = (v2_hash, v1_hash) if kind == "hybrid" else (v2_hash,)
    return payload, v2_hash, lookup_hashes


async def _no_sleep(_seconds: float) -> None:
    return None


def _mock_qbit_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[tuple[str, int]]:
    import clients
    from routers import download_clients

    client_factory = httpx.AsyncClient
    monkeypatch.setattr(
        clients.httpx,
        "AsyncClient",
        lambda **kwargs: client_factory(
            transport=httpx.MockTransport(handler), trust_env=False, **kwargs
        ),
    )
    monkeypatch.setattr(clients.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(clients, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(clients, "get_db", lambda: nullcontext(None))
    monkeypatch.setattr(
        download_clients,
        "get_client_for_protocol",
        lambda *args: {
            "id": 521,
            "name": "Synthetic qBit",
            "type": "qbittorrent",
            "host": "http://qbit.test",
            "category": "manga",
        },
    )
    monkeypatch.setattr(download_clients, "_cb_is_open", lambda _client_id: False)
    health: list[tuple[str, int]] = []
    monkeypatch.setattr(
        download_clients,
        "_cb_record_success",
        lambda client_id: health.append(("success", client_id)),
    )
    monkeypatch.setattr(
        download_clients,
        "_cb_record_failure",
        lambda client_id: health.append(("failure", client_id)),
    )
    return health


@pytest.mark.parametrize(
    "source", ["direct-magnet", "magnet-redirect", "http-redirect"]
)
def test_qbit_prefetch_redirect_preserves_identity_and_client_health(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    import clients

    torrent_bytes, expected_hash, _ = _torrent_fixture()
    magnet = f"magnet:?xt=urn:btih:{expected_hash.upper()}&dn=Provider%20Filename"
    entry = "https://indexer.test/download/521"
    add_urls: list[dict[str, list[str]]] = []
    uploads: list[bytes] = []
    lookups: list[dict[str, str]] = []
    protocol_refusals: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # MockTransport accepts any scheme; mirror the real HTTP transport's
        # refusal while retaining HTTPX's actual redirect construction.
        if request.url.scheme not in {"http", "https"}:
            protocol_refusals.append(str(request.url))
            raise httpx.UnsupportedProtocol(
                "unsupported redirect protocol", request=request
            )
        if request.url.path == "/api/v2/auth/login":
            return httpx.Response(200, text="Ok.")
        if str(request.url) == entry:
            return httpx.Response(302, headers={"Location": "/forward"})
        if str(request.url) == "https://indexer.test/forward":
            target = (
                magnet
                if source == "magnet-redirect"
                else "https://cdn.test/release.torrent"
            )
            return httpx.Response(
                301 if source == "magnet-redirect" else 302,
                headers={"Location": target},
            )
        if str(request.url) == "https://cdn.test/release.torrent":
            return httpx.Response(200, content=torrent_bytes)
        if request.url.path == "/api/v2/torrents/add":
            if request.headers["content-type"].startswith("multipart/form-data"):
                uploads.append(request.content)
            else:
                add_urls.append(parse_qs(request.content.decode()))
            return httpx.Response(200, text="Ok.")
        if request.url.path == "/api/v2/torrents/info":
            lookups.append(dict(request.url.params))
            return httpx.Response(
                200,
                json=[
                    {
                        "hash": expected_hash,
                        "name": "Provider-only filename [different]",
                    }
                ],
            )
        pytest.fail(f"unexpected synthetic request: {request.method} {request.url}")

    health = _mock_qbit_transport(monkeypatch, handler)
    result = asyncio.run(
        clients.grab_url(
            magnet if source == "direct-magnet" else entry,
            "torrent",
            torrent_name="Requested Manga Volume 01",
        )
    )

    assert health == [("success", 521)]
    assert tuple(result) == (True, "qbittorrent", expected_hash, True), {
        "add_urls": add_urls,
        "lookups": lookups,
        "protocol_refusals": protocol_refusals,
        "health": health,
    }
    if source == "http-redirect":
        assert len(uploads) == 1 and torrent_bytes in uploads[0]
        assert add_urls == []
        assert lookups == [{"hashes": expected_hash}]
    else:
        assert add_urls == [{"category": ["manga"], "urls": [magnet]}]
        assert uploads == [] and lookups == []
    assert protocol_refusals == []


@pytest.mark.parametrize("lookup", ["exact", "unrelated", "unavailable"])
def test_qbit_magnet_redirect_timeout_keeps_exact_identity_and_lookup_health(
    monkeypatch: pytest.MonkeyPatch, lookup: str
) -> None:
    import clients

    _, expected_hash, _ = _torrent_fixture()
    magnet = f"magnet:?xt=urn:btih:{expected_hash}"
    queries: list[dict[str, str]] = []
    submitted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.scheme not in {"http", "https"}:
            raise httpx.UnsupportedProtocol(
                "unsupported redirect protocol", request=request
            )
        if request.url.path == "/api/v2/auth/login":
            return httpx.Response(200, text="Ok.")
        if request.url.host == "indexer.test":
            return httpx.Response(307, headers={"Location": magnet})
        if request.url.path == "/api/v2/torrents/add":
            submitted.extend(parse_qs(request.content.decode())["urls"])
            raise httpx.ReadTimeout("synthetic add timeout", request=request)
        if request.url.path == "/api/v2/torrents/info":
            queries.append(dict(request.url.params))
            if lookup == "unavailable":
                return httpx.Response(503)
            return httpx.Response(
                200,
                json=[
                    {
                        "hash": expected_hash if lookup == "exact" else "b" * 40,
                        "name": "Provider filename"
                        if lookup == "exact"
                        else "Requested Manga Volume 01",
                    }
                ],
            )
        pytest.fail(f"unexpected synthetic request: {request.method} {request.url}")

    health = _mock_qbit_transport(monkeypatch, handler)
    result = asyncio.run(
        clients.grab_url(
            "https://indexer.test/download/521",
            "torrent",
            torrent_name="Requested Manga Volume 01",
        )
    )
    expected_id = expected_hash if lookup == "exact" else None
    assert tuple(result) == (
        lookup == "exact",
        "qbittorrent",
        expected_id,
        lookup != "unavailable",
    )
    assert submitted == [magnet]
    assert queries == [{"hashes": expected_hash}] * (1 if lookup == "exact" else 2)
    assert health == [("failure" if lookup == "unavailable" else "success", 521)]


def test_qbit_http_redirect_limit_retains_url_fallback_without_client_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import clients

    entry = "https://indexer.test/loop"
    fetches: list[str] = []
    submitted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return httpx.Response(200, text="Ok.")
        if str(request.url) == entry:
            fetches.append(str(request.url))
            return httpx.Response(302, headers={"Location": entry})
        if request.url.path == "/api/v2/torrents/add":
            submitted.extend(parse_qs(request.content.decode())["urls"])
            return httpx.Response(200, text="Ok.")
        if request.url.path == "/api/v2/torrents/info":
            return httpx.Response(200, json=[])
        pytest.fail(f"unexpected synthetic request: {request.method} {request.url}")

    health = _mock_qbit_transport(monkeypatch, handler)
    result = asyncio.run(
        clients.grab_url(entry, "torrent", torrent_name="Requested Manga Volume 01")
    )
    assert tuple(result) == (False, "qbittorrent", None, True)
    assert len(fetches) == 21
    assert submitted == [entry] and health == [("success", 521)]


@pytest.mark.parametrize("case", ["malformed", "trailing"])
def test_torrent_info_hash_parser_fails_closed(case):
    import clients

    valid, _, _ = _torrent_fixture()
    payload = (
        b"d4:infod6:lengthi12xe4:name1:xee"
        if case == "malformed"
        else valid + b"trailing"
    )

    assert clients._torrent_info_hash(payload) is None


@pytest.mark.parametrize("kind", ["v1", "pure-v2", "hybrid"])
def test_torrent_info_hash_matches_qbit_identity(kind):
    import clients

    torrent_bytes, expected_hash, lookup_hashes = _torrent_fixture(kind)

    identity = clients._torrent_info_identity(torrent_bytes)

    assert clients._torrent_info_hash(torrent_bytes) == expected_hash
    assert identity is not None
    assert identity.lookup_hashes == lookup_hashes


@pytest.mark.parametrize(
    ("kind", "reported_hash_index"),
    [("v1", 0), ("pure-v2", 0), ("hybrid", 0), ("hybrid", 1)],
)
def test_qbit_timeout_after_accepted_upload_recovers_exact_info_hash(
    monkeypatch, kind, reported_hash_index
):
    import clients

    torrent_bytes, _, lookup_hashes = _torrent_fixture(kind)
    reported_hash = lookup_hashes[reported_hash_index]
    requested_title = "One Piece, Vol. 106 by Eiichiro Oda [ENG / CBZ]"
    info_queries: list[dict[str, object]] = []

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            if url.endswith("/auth/login"):
                return _Response(text="Ok.")
            if url.endswith("/torrents/add"):
                assert kwargs["files"]["torrents"][1] == torrent_bytes
                raise httpx.ReadTimeout("", request=httpx.Request("POST", url))
            raise AssertionError(f"unexpected POST {url}")

        async def get(self, url, **kwargs):
            if url == "https://indexer.test/one-piece-106.torrent":
                return _Response(content=torrent_bytes)
            if url.endswith("/torrents/info"):
                info_queries.append(kwargs["params"])
                return _Response(
                    json_data=[
                        {
                            "hash": reported_hash.upper(),
                            "name": "One Piece v106 (2024) (Digital) (1r0n).cbz",
                        }
                    ]
                )
            raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(clients.httpx, "AsyncClient", lambda **kwargs: _Client())
    monkeypatch.setattr(clients.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(clients, "log_event", lambda *args, **kwargs: None)

    result = asyncio.run(
        clients.qbit_grab(
            "https://indexer.test/one-piece-106.torrent",
            client={
                "host": "http://qbit.test",
                "username": "user",
                "password": "secret",
                "category": "manga",
            },
            torrent_name=requested_title,
        )
    )

    assert result == (True, reported_hash, True)
    assert info_queries == [{"hashes": "|".join(lookup_hashes)}]


def test_qbit_already_present_upload_recovers_exact_info_hash(monkeypatch):
    import clients

    torrent_bytes, expected_hash, _ = _torrent_fixture()
    info_queries: list[dict[str, object]] = []

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            if url.endswith("/auth/login"):
                return _Response(text="Ok.")
            if url.endswith("/torrents/add"):
                assert kwargs["files"]["torrents"][1] == torrent_bytes
                return _Response(text="Fails.")
            raise AssertionError(f"unexpected POST {url}")

        async def get(self, url, **kwargs):
            if url == "https://indexer.test/one-piece-106.torrent":
                return _Response(content=torrent_bytes)
            if url.endswith("/torrents/info"):
                info_queries.append(kwargs["params"])
                return _Response(
                    json_data=[
                        {
                            "hash": expected_hash,
                            "name": "One Piece v106 (2024) (Digital) (1r0n).cbz",
                        }
                    ]
                )
            raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(clients.httpx, "AsyncClient", lambda **kwargs: _Client())
    monkeypatch.setattr(clients.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(clients, "log_event", lambda *args, **kwargs: None)

    result = asyncio.run(
        clients.qbit_grab(
            "https://indexer.test/one-piece-106.torrent",
            client={"host": "http://qbit.test", "category": "manga"},
            torrent_name="One Piece, Vol. 106 by Eiichiro Oda [ENG / CBZ]",
        )
    )

    assert result == (True, expected_hash, True)
    assert info_queries == [{"hashes": expected_hash}]


def test_qbit_timeout_without_matching_hash_remains_failure(monkeypatch):
    import clients

    torrent_bytes, expected_hash, _ = _torrent_fixture()

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            if url.endswith("/auth/login"):
                return _Response(text="Ok.")
            raise httpx.ReadTimeout("", request=httpx.Request("POST", url))

        async def get(self, url, **kwargs):
            if url == "https://indexer.test/one-piece-106.torrent":
                return _Response(content=torrent_bytes)
            assert kwargs["params"] == {"hashes": expected_hash}
            return _Response(json_data=[])

    events: list[str] = []
    monkeypatch.setattr(clients.httpx, "AsyncClient", lambda **kwargs: _Client())
    monkeypatch.setattr(clients.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(
        clients, "log_event", lambda _kind, message: events.append(message)
    )

    result = asyncio.run(
        clients.qbit_grab(
            "https://indexer.test/one-piece-106.torrent",
            client={"host": "http://qbit.test", "category": "manga"},
            torrent_name="One Piece, Vol. 106 by Eiichiro Oda [ENG / CBZ]",
        )
    )

    assert result == (False, None, True)
    assert events == ["[qBit] add request ReadTimeout; no matching torrent found"]


@pytest.mark.parametrize(
    ("auth_text", "add_status"),
    [("Fails.", 200), ("Ok.", 500)],
)
def test_qbit_definite_auth_or_add_failures_do_not_reconcile(
    monkeypatch, auth_text, add_status
):
    import clients

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            if url.endswith("/auth/login"):
                return _Response(text=auth_text)
            return _Response(status_code=add_status)

        async def get(self, url, **kwargs):
            raise AssertionError("definite failures must not query torrent info")

    monkeypatch.setattr(clients.httpx, "AsyncClient", lambda **kwargs: _Client())
    monkeypatch.setattr(clients, "log_event", lambda *args, **kwargs: None)

    result = asyncio.run(
        clients.qbit_grab(
            "magnet:?xt=urn:btih:" + "a" * 40,
            client={"host": "http://qbit.test", "category": "manga"},
        )
    )

    assert result == (False, None, False)
