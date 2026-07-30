"""Notification fanout must report aggregate provider delivery outcomes."""

from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from typing import Iterator

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401, E402


def _connections(*names: str) -> list[dict[str, object]]:
    return [
        {
            "id": index,
            "name": name,
            "type": "webhook",
            "enabled": 1,
            "settings": "{}",
            "on_download": 1,
        }
        for index, name in enumerate(names, start=1)
    ]


def _install_connections(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, object]],
) -> None:
    import main
    from routers import notification_connections

    class _Result:
        def fetchall(self) -> list[dict[str, object]]:
            return rows

    class _Database:
        def execute(self, query: str) -> _Result:
            assert "enabled=1 AND on_download=1" in query
            return _Result()

    @contextmanager
    def _get_db() -> Iterator[_Database]:
        yield _Database()

    monkeypatch.setattr(notification_connections, "get_db", _get_db)
    monkeypatch.setattr(main, "log_event", lambda *args, **kwargs: None)


def test_notification_fanout_all_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from routers import notification_connections

    rows = _connections("one", "two")
    _install_connections(monkeypatch, rows)
    called: list[str] = []

    async def _send(
        connection: dict[str, object],
        message: str,
        event: str = "",
        embed: dict | None = None,
    ) -> tuple[bool, str]:
        del message, event, embed
        called.append(str(connection["name"]))
        return True, "sent"

    monkeypatch.setattr(notification_connections, "send_connection", _send)

    assert asyncio.run(
        notification_connections.fire_notifications("on_download", "done")
    )
    assert set(called) == {"one", "two"}


@pytest.mark.parametrize(
    ("results", "failed"),
    (
        ({"one": True, "two": False}, {"webhook — two"}),
        ({"one": False, "two": False}, {"webhook — one", "webhook — two"}),
    ),
    ids=("partial-failure", "all-failure"),
)
def test_notification_fanout_raises_aggregate_failure(
    monkeypatch: pytest.MonkeyPatch,
    results: dict[str, bool],
    failed: set[str],
) -> None:
    from routers import notification_connections

    _install_connections(monkeypatch, _connections("one", "two"))
    called: list[str] = []

    async def _send(
        connection: dict[str, object],
        message: str,
        event: str = "",
        embed: dict | None = None,
    ) -> tuple[bool, str]:
        del message, event, embed
        name = str(connection["name"])
        called.append(name)
        return results[name], "provider rejected"

    monkeypatch.setattr(notification_connections, "send_connection", _send)

    with pytest.raises(
        notification_connections.NotificationDeliveryError
    ) as raised:
        asyncio.run(
            notification_connections.fire_notifications("on_download", "done")
        )

    assert set(called) == {"one", "two"}
    assert set(raised.value.failed_providers) == failed


def test_notification_fanout_aggregates_exception_after_attempting_siblings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from routers import notification_connections

    _install_connections(monkeypatch, _connections("raises", "succeeds"))
    called: list[str] = []

    async def _send(
        connection: dict[str, object],
        message: str,
        event: str = "",
        embed: dict | None = None,
    ) -> tuple[bool, str]:
        del message, event, embed
        name = str(connection["name"])
        called.append(name)
        if name == "raises":
            raise TimeoutError("provider timeout")
        return True, "sent"

    monkeypatch.setattr(notification_connections, "send_connection", _send)

    with pytest.raises(
        notification_connections.NotificationDeliveryError
    ) as raised:
        asyncio.run(
            notification_connections.fire_notifications("on_download", "done")
        )

    assert set(called) == {"raises", "succeeds"}
    assert raised.value.failed_providers == ("webhook — raises",)


def test_notification_fanout_without_enabled_provider_is_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from routers import notification_connections

    _install_connections(monkeypatch, [])
    monkeypatch.setattr(
        notification_connections,
        "send_connection",
        lambda *args, **kwargs: pytest.fail("disabled provider was called"),
    )

    assert asyncio.run(
        notification_connections.fire_notifications("on_download", "done")
    )
