"""ASGI middleware: request-size guard, API-key gate, and CSRF double-submit.

The middleware classes perform no DB writes or CONFIG mutation. The API-key
gate consults settings via `get_cfg`, but that remains a read-only path.

Loading order in main.py (bottom-up because Starlette wraps in
reverse):

  app.add_middleware(CSRFMiddleware)
  app.add_middleware(ApiKeyMiddleware)
  app.add_middleware(ApiVersionAliasMiddleware)
  app.add_middleware(RequestBodyLimitMiddleware)  # outermost request guard

So: RequestBodyLimitMiddleware rejects oversized bodies before buffering,
ApiKeyMiddleware decides 401 vs continue for API clients, then CSRFMiddleware
validates browser-delegated mutating API requests that carry only the
in-session CSRF cookie.
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse
import logging

from shared import get_cfg


DEFAULT_MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024


class _RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """Reject request bodies that exceed the configured byte limit.

    ``Content-Length`` lets us reject before reading. The receive wrapper is
    still required because clients may omit the header or stream more bytes
    than they declared. This middleware must remain outside CSRF middleware so
    oversized forms are stopped before CSRF buffers them.
    """

    def __init__(self, app, max_bytes: int = DEFAULT_MAX_REQUEST_BODY_BYTES):
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name.lower() != b"content-length":
                continue
            try:
                declared_size = int(value)
            except (TypeError, ValueError):
                break
            if declared_size > self.max_bytes:
                await self._reject(scope, receive, send)
                return
            break

        received_size = 0

        async def limited_receive():
            nonlocal received_size
            message = await receive()
            if message.get("type") == "http.request":
                received_size += len(message.get("body", b""))
                if received_size > self.max_bytes:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            await self._reject(scope, receive, send)

    async def _reject(self, scope, receive, send):
        response = JSONResponse(
            {
                "detail": "Request body too large.",
                "maxBytes": self.max_bytes,
            },
            status_code=413,
        )
        await response(scope, receive, send)


# ── API version compatibility ────────────────────────────────────────────────


class ApiVersionAliasMiddleware:
    """Rewrite Sonarr-style /api/v3 routes to Mangarr's /api/v1 surface."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/api/v3" or path.startswith("/api/v3/"):
            rewritten = "/api/v1" + path[len("/api/v3"):]
            scope = dict(scope)
            scope["path"] = rewritten
            raw_path = scope.get("raw_path")
            if isinstance(raw_path, (bytes, bytearray)):
                scope["raw_path"] = b"/api/v1" + bytes(raw_path)[len(b"/api/v3"):]
        await self.app(scope, receive, send)


# ── API Key middleware ───────────────────────────────────────────────────────


class ApiKeyMiddleware:
    _warned_no_key = False
    """Require an X-Api-Key header (or ?apikey= query param) on /api/
    routes. Exempts the SSE endpoint and health check. In-session
    browser requests (POST/PUT/DELETE/PATCH with a csrftoken cookie)
    may proceed without an API key, but only by delegating enforcement
    to CSRFMiddleware. API-key-authenticated requests mark the ASGI
    scope so CSRFMiddleware can skip duplicate CSRF checks for API
    clients.

    Fails closed: if the configured key is blank/missing (bad import,
    manual edit, partial migration), refuses the request. Never
    silently exposes /api/ to requests with no key.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        path = scope.get("path", "")
        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return
        if path in ("/api/queue-events", "/api/health"):
            await self.app(scope, receive, send)
            return
        api_key = (get_cfg("api_key", "") or "").strip()
        if not api_key:
            if not getattr(ApiKeyMiddleware, "_warned_no_key", False):
                logging.getLogger(__name__).error(
                    "[ERROR] /api/ routes denied — settings.api_key is blank. "
                    "Restart the app to auto-seed, or set one via Settings."
                )
                ApiKeyMiddleware._warned_no_key = True
            await JSONResponse(
                {
                    "message": "Unauthorized",
                    "description": "API key not configured on the server",
                },
                status_code=401,
            )(scope, receive, send)
            return
        provided = (
            request.headers.get("X-Api-Key") or request.query_params.get("apikey") or ""
        )
        if provided:
            if provided != api_key:
                await JSONResponse(
                    {
                        "message": "Unauthorized",
                        "description": "Invalid or missing API key",
                    },
                    status_code=401,
                )(scope, receive, send)
                return
            request.scope["mangarr_api_key_authenticated"] = True
            await self.app(scope, receive, send)
            return

        if request.method in ("POST", "PUT", "DELETE", "PATCH") and request.cookies.get(
            "csrftoken"
        ):
            request.scope["mangarr_api_browser_csrf_required"] = True
            await self.app(scope, receive, send)
            return

        await JSONResponse(
            {"message": "Unauthorized", "description": "Invalid or missing API key"},
            status_code=401,
        )(scope, receive, send)


# ── CSRF middleware ──────────────────────────────────────────────────────────

_CSRF_COOKIE = "csrftoken"
_CSRF_HEADER = "X-CSRFToken"
_CSRF_FIELD = "csrf_token"
_CSRF_SKIP_PREFIXES = ("/static/", "/covers/")


def _should_secure_cookie(scope) -> bool:
    """Return True iff the inbound request arrived over HTTPS.

    Checks (in order):
      - ASGI scope scheme == "https"  (TLS-terminating uvicorn)
      - X-Forwarded-Proto: https       (reverse proxy, standard)
      - X-Forwarded-Ssl: on            (some older proxies)

    Returning False on plain HTTP keeps local development working —
    browsers silently drop Secure-flagged cookies on http:// origins.
    """
    if scope.get("scheme") == "https":
        return True
    for k, v in scope.get("headers", []):
        kl = k.lower()
        if kl == b"x-forwarded-proto" and v.strip().lower() == b"https":
            return True
        if kl == b"x-forwarded-ssl" and v.strip().lower() == b"on":
            return True
    return False


class CSRFMiddleware:
    """Pure ASGI CSRF middleware.

    When the CSRF token must be read from a form body, we buffer the
    raw bytes and hand a replay-receive callable to the downstream app
    so the route handler can still parse the same body.
    BaseHTTPMiddleware drains the receive channel and does not replay
    it, which caused 422 errors.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        from starlette.requests import Request as _Req
        from urllib.parse import parse_qs

        req = _Req(scope, receive)
        token = req.cookies.get(_CSRF_COOKIE) or secrets.token_hex(32)

        # Expose token for templates via request.state
        req.state.csrf_token = token

        path = scope.get("path", "")
        is_exempt = any(path.startswith(p) for p in _CSRF_SKIP_PREFIXES) or (
            path.startswith("/api/") and scope.get("mangarr_api_key_authenticated")
        )
        method = scope.get("method", "GET")

        # Receive callable that will be forwarded to the app (may be
        # replaced with a replay version if we had to buffer the body
        # for CSRF checking).
        forward_receive = receive

        if method not in ("GET", "HEAD", "OPTIONS", "TRACE") and not is_exempt:
            valid = False

            # 1. Header check — no body read needed
            hdr = ""
            for k, v in scope.get("headers", []):
                if k.lower() == b"x-csrftoken":
                    hdr = v.decode()
                    break
            if hdr and token:
                valid = hmac.compare_digest(token, hdr)

            # 2. Form-field check — must buffer body, then replay for route handler
            if not valid:
                ct = ""
                for k, v in scope.get("headers", []):
                    if k.lower() == b"content-type":
                        ct = v.decode().lower()
                        break

                if "urlencoded" in ct or "multipart" in ct:
                    chunks = []
                    while True:
                        msg = await receive()
                        body_chunk = msg.get("body", b"")
                        if body_chunk:
                            chunks.append(body_chunk)
                        if not msg.get("more_body", False):
                            break
                    raw_body = b"".join(chunks)

                    try:
                        if "urlencoded" in ct:
                            params = parse_qs(
                                raw_body.decode("latin-1"), keep_blank_values=True
                            )
                            fv = params.get(_CSRF_FIELD, [""])[0]
                        else:
                            _replayed_once = False

                            async def _tmp_receive():
                                nonlocal _replayed_once
                                if not _replayed_once:
                                    _replayed_once = True
                                    return {
                                        "type": "http.request",
                                        "body": raw_body,
                                        "more_body": False,
                                    }
                                return {"type": "http.disconnect"}

                            _tmp_req = _Req(scope, _tmp_receive)
                            fd = await _tmp_req.form()
                            fv = fd.get(_CSRF_FIELD, "")
                        if fv and token:
                            valid = hmac.compare_digest(token, fv)
                    except Exception:
                        pass

                    # Replay the body so the route handler can read it too
                    _replayed = False

                    async def _replay_receive():
                        nonlocal _replayed
                        if not _replayed:
                            _replayed = True
                            return {
                                "type": "http.request",
                                "body": raw_body,
                                "more_body": False,
                            }
                        return {"type": "http.disconnect"}

                    forward_receive = _replay_receive

            if not valid:
                resp = JSONResponse(
                    {"detail": "CSRF token missing or invalid."}, status_code=403
                )
                await resp(scope, forward_receive, send)
                return

        has_cookie = bool(req.cookies.get(_CSRF_COOKIE))
        secure_cookie = _should_secure_cookie(scope)

        async def send_with_cookie(message):
            nonlocal has_cookie
            if not has_cookie and message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                # SameSite=Strict: cookie not sent on cross-site navigation.
                #   Safe for a self-hosted admin UI; middleware regenerates a
                #   fresh token on re-entry when needed.
                # HttpOnly: JS cannot read document.cookie for csrftoken. The
                #   token is exposed to the frontend via a <meta name="csrf-token">
                #   tag (see base.html) so htmx and plain-form CSRF injection
                #   continue to work without document.cookie access.
                # Secure: only set when the request arrived over HTTPS.
                parts = [
                    f"{_CSRF_COOKIE}={token}",
                    "Path=/",
                    "SameSite=Strict",
                    "HttpOnly",
                ]
                if secure_cookie:
                    parts.append("Secure")
                cookie_val = "; ".join(parts)
                headers.append((b"set-cookie", cookie_val.encode()))
                message = {**message, "headers": headers}
                has_cookie = True
            await send(message)

        await self.app(scope, forward_receive, send_with_cookie)
