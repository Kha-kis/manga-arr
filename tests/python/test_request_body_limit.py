"""Request-size guard coverage for known-length and streamed bodies."""

import asyncio

import pytest
from starlette.responses import PlainTextResponse

from middleware import DEFAULT_MAX_REQUEST_BODY_BYTES, RequestBodyLimitMiddleware


def _scope(*headers: tuple[bytes, bytes]) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/submit",
        "raw_path": b"/submit",
        "query_string": b"",
        "headers": list(headers),
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }


def _run(app, scope: dict, messages: list[dict]) -> list[dict]:
    sent: list[dict] = []

    async def receive():
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


def _status(sent: list[dict]) -> int:
    return next(message["status"] for message in sent if message["type"] == "http.response.start")


def test_rejects_oversized_content_length_without_reading_body():
    downstream_called = False

    async def downstream(scope, receive, send):
        nonlocal downstream_called
        downstream_called = True

    app = RequestBodyLimitMiddleware(downstream, max_bytes=8)
    sent = _run(app, _scope((b"content-length", b"9")), [])

    assert _status(sent) == 413
    assert downstream_called is False
    assert b'"maxBytes":8' in sent[-1]["body"]


def test_rejects_streamed_body_without_content_length():
    downstream_completed = False

    async def downstream(scope, receive, send):
        nonlocal downstream_completed
        while True:
            message = await receive()
            if not message.get("more_body", False):
                break
        downstream_completed = True

    app = RequestBodyLimitMiddleware(downstream, max_bytes=8)
    sent = _run(
        app,
        _scope(),
        [
            {"type": "http.request", "body": b"12345678", "more_body": True},
            {"type": "http.request", "body": b"9", "more_body": False},
        ],
    )

    assert _status(sent) == 413
    assert downstream_completed is False


def test_rejects_more_streamed_bytes_than_declared():
    async def downstream(scope, receive, send):
        while True:
            message = await receive()
            if not message.get("more_body", False):
                break

    app = RequestBodyLimitMiddleware(downstream, max_bytes=8)
    sent = _run(
        app,
        _scope((b"content-length", b"8")),
        [
            {"type": "http.request", "body": b"12345678", "more_body": True},
            {"type": "http.request", "body": b"9", "more_body": False},
        ],
    )

    assert _status(sent) == 413


def test_allows_body_at_exact_limit():
    received = b""

    async def downstream(scope, receive, send):
        nonlocal received
        message = await receive()
        received = message["body"]
        await PlainTextResponse("ok")(scope, receive, send)

    app = RequestBodyLimitMiddleware(downstream, max_bytes=8)
    sent = _run(
        app,
        _scope((b"content-length", b"8")),
        [{"type": "http.request", "body": b"12345678", "more_body": False}],
    )

    assert _status(sent) == 200
    assert received == b"12345678"


def test_invalid_content_length_still_uses_streamed_limit():
    async def downstream(scope, receive, send):
        await receive()

    app = RequestBodyLimitMiddleware(downstream, max_bytes=8)
    sent = _run(
        app,
        _scope((b"content-length", b"invalid")),
        [{"type": "http.request", "body": b"123456789", "more_body": False}],
    )

    assert _status(sent) == 413


def test_default_limit_is_two_mebibytes():
    assert DEFAULT_MAX_REQUEST_BODY_BYTES == 2 * 1024 * 1024


def test_limit_must_be_positive():
    with pytest.raises(ValueError, match="positive"):
        RequestBodyLimitMiddleware(lambda *_args: None, max_bytes=0)


def test_production_stack_keeps_limit_outside_csrf():
    import main

    middleware_classes = [entry.cls for entry in main.app.user_middleware]
    assert middleware_classes.index(RequestBodyLimitMiddleware) < middleware_classes.index(
        main.CSRFMiddleware
    )
