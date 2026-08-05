"""Focused qBittorrent add-response reconciliation regressions."""

import asyncio
import hashlib
import sys

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
