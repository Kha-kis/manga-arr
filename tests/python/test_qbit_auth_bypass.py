"""Regression coverage for qBittorrent authentication-bypass sessions."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
from collections.abc import Callable
from typing import Any

import httpx
import pytest

import conftest  # noqa: F401


class _Response:
    def __init__(
        self,
        status_code: int,
        text: str = "",
        *,
        json_data: object = None,
        content: bytes = b"",
    ) -> None:
        self.status_code = status_code
        self.text = text
        self._json_data = json_data
        self.content = content

    def json(self) -> object:
        return self._json_data


def _scripted_client(
    responses: dict[tuple[str, str], _Response | BaseException],
    calls: list[tuple[str, str]],
) -> type:
    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *args: object) -> bool:
            del args
            return False

        async def _request(self, method: str, url: str) -> _Response:
            calls.append((method, url))
            result = responses[(method, url)]
            if isinstance(result, BaseException):
                raise result
            return result

        async def post(self, url: str, **kwargs: object) -> _Response:
            del kwargs
            return await self._request("POST", url)

        async def get(self, url: str, **kwargs: object) -> _Response:
            del kwargs
            return await self._request("GET", url)

    return _Client


@pytest.mark.parametrize(
    ("status", "body", "expected_name"),
    [
        (200, "Ok.", "NORMAL_AUTH"),
        (200, "Fails.", "REJECTED"),
        (403, "Forbidden", "REJECTED"),
        (204, "", "BYPASS_PROBE_REQUIRED"),
        (204, " \t\r\n", "BYPASS_PROBE_REQUIRED"),
        (204, "unexpected", "REJECTED"),
        (201, "", "REJECTED"),
        (202, "", "REJECTED"),
        (500, "Ok.", "REJECTED"),
        (200, "Not Ok.", "REJECTED"),
    ],
)
def test_qbit_login_response_is_classified_fail_closed(
    status: int,
    body: str,
    expected_name: str,
) -> None:
    import qbit_auth

    assert qbit_auth.classify_qbit_login(status, body).name == expected_name


@pytest.mark.parametrize(
    "body",
    [
        "v5.2.3",
        "5.2.3",
        " v5.2.3\n",
        "v5.2.3.1",
        "v5.3.0alpha1",
        "v5.3.0beta2",
        "v5.3.0rc1",
        "v5.3.0-rc1",
        "v5.2.3+git.abc123",
        "v5.3.0-rc1+git.abc123",
    ],
)
def test_qbit_version_response_accepts_official_version_forms(body: str) -> None:
    import qbit_auth

    assert qbit_auth.is_valid_qbit_version_response(200, body) is True


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (204, "v5.2.3"),
        (401, "v5.2.3"),
        (500, "v5.2.3"),
        (200, ""),
        (200, " \t\r\n"),
        (200, "<html>not qBittorrent</html>"),
        (200, "not qBittorrent"),
        (200, "qBittorrent v5.2.3"),
        (200, "v5.2.3\n<html>error</html>"),
    ],
)
def test_qbit_version_response_rejects_non_version_responses(
    status: int,
    body: str,
) -> None:
    import qbit_auth

    assert qbit_auth.is_valid_qbit_version_response(status, body) is False


def test_connection_test_accepts_bypass_only_after_read_only_version_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from routers import download_clients

    host = "http://qbit.test"
    calls: list[tuple[str, str]] = []
    fake_client = _scripted_client(
        {
            ("POST", f"{host}/api/v2/auth/login"): _Response(204),
            ("GET", f"{host}/api/v2/app/version"): _Response(200, "5.2.3"),
        },
        calls,
    )
    monkeypatch.setattr(download_clients.httpx, "AsyncClient", fake_client)

    result = asyncio.run(
        download_clients._test_client(
            {
                "type": "qbittorrent",
                "host": host,
                "port": None,
                "username": "user",
                "password": "password",
            }
        )
    )

    assert result == (True, "Connected to qBittorrent")
    assert calls == [
        ("POST", f"{host}/api/v2/auth/login"),
        ("GET", f"{host}/api/v2/app/version"),
    ]
    assert all("/torrents/" not in url for _, url in calls)


@pytest.mark.parametrize(
    ("login_response", "expected_message"),
    [
        (_Response(200, "Fails."), "Wrong username or password"),
        (_Response(403, "Forbidden"), "IP banned by qBittorrent"),
        (_Response(204, "unexpected"), "HTTP 204"),
        (_Response(201), "HTTP 201"),
        (_Response(202), "HTTP 202"),
    ],
)
def test_connection_test_preserves_rejection_diagnostics_without_probing(
    monkeypatch: pytest.MonkeyPatch,
    login_response: _Response,
    expected_message: str,
) -> None:
    from routers import download_clients

    host = "http://qbit.test"
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        download_clients.httpx,
        "AsyncClient",
        _scripted_client(
            {("POST", f"{host}/api/v2/auth/login"): login_response},
            calls,
        ),
    )

    result = asyncio.run(
        download_clients._test_client(
            {
                "type": "qbittorrent",
                "host": host,
                "port": None,
                "username": "user",
                "password": "password",
            }
        )
    )

    assert result[0] is False
    assert expected_message in result[1]
    assert calls == [("POST", f"{host}/api/v2/auth/login")]


@pytest.mark.parametrize(
    "proof_response",
    [
        _Response(401, "Unauthorized"),
        _Response(403, "Forbidden"),
        _Response(500, "error"),
        _Response(200, ""),
        _Response(200, "<html>not qBittorrent</html>"),
        _Response(200, "not qBittorrent"),
    ],
)
def test_connection_test_rejects_failed_or_malformed_bypass_proof(
    monkeypatch: pytest.MonkeyPatch,
    proof_response: _Response,
) -> None:
    from routers import download_clients

    host = "http://qbit.test"
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        download_clients.httpx,
        "AsyncClient",
        _scripted_client(
            {
                ("POST", f"{host}/api/v2/auth/login"): _Response(204),
                ("GET", f"{host}/api/v2/app/version"): proof_response,
            },
            calls,
        ),
    )

    ok, message = asyncio.run(
        download_clients._test_client(
            {
                "type": "qbittorrent",
                "host": host,
                "port": None,
                "username": "user",
                "password": "password",
            }
        )
    )

    assert ok is False
    assert "proof" in message.lower()
    assert calls[-1] == ("GET", f"{host}/api/v2/app/version")


@pytest.mark.parametrize(
    "error_factory",
    [
        lambda request: httpx.ConnectError("offline", request=request),
        lambda request: httpx.ReadTimeout("timed out", request=request),
    ],
)
def test_connection_test_rejects_transport_failures_and_timeouts(
    monkeypatch: pytest.MonkeyPatch,
    error_factory: Callable[[httpx.Request], httpx.RequestError],
) -> None:
    from routers import download_clients

    host = "http://qbit.test"
    request = httpx.Request("POST", f"{host}/api/v2/auth/login")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        download_clients.httpx,
        "AsyncClient",
        _scripted_client(
            {
                ("POST", f"{host}/api/v2/auth/login"): error_factory(request),
            },
            calls,
        ),
    )

    ok, _ = asyncio.run(
        download_clients._test_client(
            {
                "type": "qbittorrent",
                "host": host,
                "port": None,
                "username": "user",
                "password": "password",
            }
        )
    )

    assert ok is False


@pytest.fixture
def breaker_db() -> str:
    import main
    import security
    import shared

    database = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    database.close()
    os.unlink(database.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-qbit-auth-keys-")
    original_main_db = main.DB_PATH
    original_shared_db = shared.DB_PATH
    main.DB_PATH = database.name
    shared.DB_PATH = database.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    with sqlite3.connect(database.name) as connection:
        connection.execute(
            "INSERT INTO download_clients("
            "id,name,type,host,username,password,enabled,priority"
            ") VALUES(1,'qBit','qbittorrent','http://qbit.test','u','p',1,1)"
        )
    try:
        yield database.name
    finally:
        main.DB_PATH = original_main_db
        shared.DB_PATH = original_shared_db
        for suffix in ("", "-wal", "-shm"):
            path = database.name + suffix
            if os.path.exists(path):
                os.unlink(path)


def test_saved_connection_test_clears_open_breaker_after_bypass_proof(
    breaker_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from routers import download_clients

    for _ in range(download_clients._CB_THRESHOLD):
        download_clients._cb_record_failure(1)
    assert download_clients._cb_is_open(1) is True

    host = "http://qbit.test"
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        download_clients.httpx,
        "AsyncClient",
        _scripted_client(
            {
                ("POST", f"{host}/api/v2/auth/login"): _Response(204),
                ("GET", f"{host}/api/v2/app/version"): _Response(200, "5.2.3"),
            },
            calls,
        ),
    )

    response = asyncio.run(download_clients.test_download_client(1))

    assert json.loads(response.body) == {
        "ok": True,
        "message": "Connected to qBittorrent",
    }
    assert download_clients._cb_load(1) is None


def test_qbit_grab_bypass_proof_precedes_add_and_preserves_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import clients

    host = "http://qbit.test"
    magnet_hash = "a" * 40
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        clients.httpx,
        "AsyncClient",
        _scripted_client(
            {
                ("POST", f"{host}/api/v2/auth/login"): _Response(204),
                ("GET", f"{host}/api/v2/app/version"): _Response(200, "5.2.3"),
                ("POST", f"{host}/api/v2/torrents/add"): _Response(200, "Ok."),
            },
            calls,
        ),
    )

    result = asyncio.run(
        clients.qbit_grab(
            f"magnet:?xt=urn:btih:{magnet_hash}",
            client={"host": host, "username": "u", "password": "p"},
        )
    )

    assert result == (True, magnet_hash, True)
    assert calls == [
        ("POST", f"{host}/api/v2/auth/login"),
        ("GET", f"{host}/api/v2/app/version"),
        ("POST", f"{host}/api/v2/torrents/add"),
    ]


@pytest.mark.parametrize(
    "proof_response",
    [
        _Response(401),
        _Response(403),
        _Response(500),
        _Response(200, ""),
        _Response(200, "<html>not qBittorrent</html>"),
        _Response(200, "not qBittorrent"),
    ],
)
def test_qbit_grab_failed_bypass_proof_never_fetches_or_mutates(
    monkeypatch: pytest.MonkeyPatch,
    proof_response: _Response,
) -> None:
    import clients

    host = "http://qbit.test"
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        clients.httpx,
        "AsyncClient",
        _scripted_client(
            {
                ("POST", f"{host}/api/v2/auth/login"): _Response(204),
                ("GET", f"{host}/api/v2/app/version"): proof_response,
            },
            calls,
        ),
    )

    result = asyncio.run(
        clients.qbit_grab(
            "https://indexer.test/release.torrent",
            client={"host": host, "username": "u", "password": "p"},
        )
    )

    assert result == (False, None, False)
    assert calls == [
        ("POST", f"{host}/api/v2/auth/login"),
        ("GET", f"{host}/api/v2/app/version"),
    ]


def test_qbit_grab_normal_auth_preserves_existing_add_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import clients

    host = "http://qbit.test"
    magnet_hash = "b" * 40
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        clients.httpx,
        "AsyncClient",
        _scripted_client(
            {
                ("POST", f"{host}/api/v2/auth/login"): _Response(200, "Ok."),
                ("POST", f"{host}/api/v2/torrents/add"): _Response(200, "Ok."),
            },
            calls,
        ),
    )

    result = asyncio.run(
        clients.qbit_grab(
            f"magnet:?xt=urn:btih:{magnet_hash}",
            client={"host": host, "username": "u", "password": "p"},
        )
    )

    assert result == (True, magnet_hash, True)
    assert all("app/version" not in url for _, url in calls)


def test_qbit_remove_requires_bypass_proof_before_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import clients

    host = "http://qbit.test"
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        clients.httpx,
        "AsyncClient",
        _scripted_client(
            {
                ("POST", f"{host}/api/v2/auth/login"): _Response(204),
                ("GET", f"{host}/api/v2/app/version"): _Response(200, "5.2.3"),
                ("POST", f"{host}/api/v2/torrents/delete"): _Response(200),
            },
            calls,
        ),
    )

    result = asyncio.run(
        clients.qbit_remove(
            "deadbeef",
            client={"host": host, "username": "u", "password": "p"},
        )
    )

    assert result is True
    assert calls == [
        ("POST", f"{host}/api/v2/auth/login"),
        ("GET", f"{host}/api/v2/app/version"),
        ("POST", f"{host}/api/v2/torrents/delete"),
    ]


def test_status_cache_accepts_bypass_when_torrent_list_is_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import status_cache

    host = "http://qbit.test"
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        status_cache.httpx,
        "AsyncClient",
        _scripted_client(
            {
                ("POST", f"{host}/api/v2/auth/login"): _Response(204),
                ("GET", f"{host}/api/v2/torrents/info"): _Response(
                    200,
                    json_data=[
                        {
                            "hash": "ABCDEF",
                            "name": "Release",
                            "state": "downloading",
                            "progress": 0.5,
                        }
                    ],
                ),
            },
            calls,
        ),
    )

    result = asyncio.run(
        status_cache._fetch_qbit(
            {"host": host, "username": "u", "password": "p", "category": "manga"}
        )
    )

    assert result["abcdef"]["progress"] == 50.0
    assert calls == [
        ("POST", f"{host}/api/v2/auth/login"),
        ("GET", f"{host}/api/v2/torrents/info"),
    ]


@pytest.mark.parametrize(
    "info_response",
    [_Response(401), _Response(403), _Response(500), _Response(200, json_data={})],
)
def test_status_cache_rejects_failed_or_malformed_bypass_torrent_proof(
    monkeypatch: pytest.MonkeyPatch,
    info_response: _Response,
) -> None:
    import status_cache

    host = "http://qbit.test"
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        status_cache.httpx,
        "AsyncClient",
        _scripted_client(
            {
                ("POST", f"{host}/api/v2/auth/login"): _Response(204),
                ("GET", f"{host}/api/v2/torrents/info"): info_response,
            },
            calls,
        ),
    )

    with pytest.raises(RuntimeError):
        asyncio.run(
            status_cache._fetch_qbit(
                {
                    "host": host,
                    "username": "u",
                    "password": "p",
                    "category": "manga",
                }
            )
        )

    assert calls[-1] == ("GET", f"{host}/api/v2/torrents/info")


def test_status_refresh_bypass_failure_preserves_last_known_good_snapshot(
    breaker_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import status_cache

    host = "http://qbit.test"
    cache = status_cache.DownloadStatusCache()
    success_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        status_cache.httpx,
        "AsyncClient",
        _scripted_client(
            {
                ("POST", f"{host}/api/v2/auth/login"): _Response(204),
                ("GET", f"{host}/api/v2/torrents/info"): _Response(
                    200,
                    json_data=[{"hash": "ABC", "name": "Known", "progress": 1.0}],
                ),
            },
            success_calls,
        ),
    )
    assert asyncio.run(cache.refresh()) is True
    previous = cache.snapshot_qbit()
    assert previous is not None and "abc" in previous.items
    assert previous.last_success_at is not None

    failure_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        status_cache.httpx,
        "AsyncClient",
        _scripted_client(
            {
                ("POST", f"{host}/api/v2/auth/login"): _Response(204),
                ("GET", f"{host}/api/v2/torrents/info"): _Response(403),
            },
            failure_calls,
        ),
    )
    assert asyncio.run(cache.refresh()) is True

    failed = cache.snapshot_qbit()
    assert failed is not None
    assert failed.items == previous.items
    assert failed.last_success_at == previous.last_success_at
    assert failed.error and "HTTP 403" in failed.error


def test_import_discovery_failed_bypass_proof_never_reaches_database_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_discovery

    host = "http://qbit.test"
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        import_discovery.httpx,
        "AsyncClient",
        _scripted_client(
            {
                ("POST", f"{host}/api/v2/auth/login"): _Response(204),
                ("GET", f"{host}/api/v2/torrents/info"): _Response(403),
            },
            calls,
        ),
    )

    def _database_access_is_a_bug() -> Any:
        raise AssertionError("failed bypass proof reached database mutation logic")

    monkeypatch.setattr(import_discovery, "get_db", _database_access_is_a_bug)
    monkeypatch.setattr(import_discovery, "log_event", lambda *args, **kwargs: None)
    partition = import_discovery._ClientPollPartition(
        client_id=7,
        name="qBit",
        client_type="qbittorrent",
        include_legacy_ownerless=False,
    )

    asyncio.run(
        import_discovery._poll_qbit_partition(
            partition,
            {"host": host, "username": "u", "password": "p", "category": "manga"},
        )
    )

    assert calls == [
        ("POST", f"{host}/api/v2/auth/login"),
        ("GET", f"{host}/api/v2/torrents/info"),
    ]


def test_import_discovery_bypass_proof_reaches_owner_partition_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_discovery

    host = "http://qbit.test"
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        import_discovery.httpx,
        "AsyncClient",
        _scripted_client(
            {
                ("POST", f"{host}/api/v2/auth/login"): _Response(204),
                ("GET", f"{host}/api/v2/torrents/info"): _Response(
                    200,
                    json_data=[
                        {
                            "hash": "ABCDEF",
                            "name": "Completed release",
                            "progress": 1.0,
                            "state": "uploading",
                        }
                    ],
                ),
            },
            calls,
        ),
    )
    threaded_steps: list[str] = []

    async def _run_partition_step(function: Callable[[], object]) -> object:
        threaded_steps.append(function.__name__)
        if function.__name__ == "_process_completed":
            return [91]
        return None

    scheduled: list[int] = []
    monkeypatch.setattr(import_discovery.asyncio, "to_thread", _run_partition_step)
    monkeypatch.setattr(import_discovery, "schedule_import_worker", scheduled.append)
    monkeypatch.setattr(import_discovery, "get_cfg", lambda *args: "0")
    partition = import_discovery._ClientPollPartition(
        client_id=7,
        name="qBit owner",
        client_type="qbittorrent",
        include_legacy_ownerless=False,
    )

    asyncio.run(
        import_discovery._poll_qbit_partition(
            partition,
            {"host": host, "username": "u", "password": "p", "category": "manga"},
        )
    )

    assert threaded_steps == ["_process_completed", "_orphan_cleanup"]
    assert scheduled == [91]


def test_health_check_accepts_bypass_only_after_version_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from routers import health_

    host = "http://qbit.test"
    snapshot = {
        "indexers_enabled": 0,
        "download_clients_enabled": 1,
        "quality_profiles": 0,
        "root_folders": [],
        "orphan_series_rf": 0,
        "wanted_volumes": 0,
        "last_grab": None,
        "last_rss_poll": None,
        "qbit_client": {"host": host, "username": "u", "password": "p"},
        "sab_client": None,
        "stale_series": [],
        "stale_grabs": [],
        "stuck_imports": [],
        "recent_errors": [],
        "last_backlog": None,
        "stats": {},
    }
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(health_, "_health_db_snapshot", lambda: snapshot)
    monkeypatch.setattr(health_, "get_cfg", lambda *args: "")
    monkeypatch.setattr(
        health_.httpx,
        "AsyncClient",
        _scripted_client(
            {
                ("POST", f"{host}/api/v2/auth/login"): _Response(204),
                ("GET", f"{host}/api/v2/app/version"): _Response(200, "5.2.3"),
            },
            calls,
        ),
    )

    payload = asyncio.run(health_.build_health_payload())

    qbit_check = next(check for check in payload["checks"] if check["name"] == "qBittorrent")
    assert qbit_check["ok"] is True
    assert qbit_check["message"] == "qBittorrent 5.2.3"
    assert calls == [
        ("POST", f"{host}/api/v2/auth/login"),
        ("GET", f"{host}/api/v2/app/version"),
    ]
