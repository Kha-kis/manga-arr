"""ASGI middleware: API-key gate and CSRF double-submit.

Fourth module extracted from main.py. Both middleware classes are
pure — no DB writes, no CONFIG mutation. They consult settings via
`get_cfg` (API key read) but that's a read-only path.

Loading order in main.py (bottom-up because Starlette wraps in
reverse):

  app.add_middleware(CSRFMiddleware)      # outer
  app.add_middleware(ApiKeyMiddleware)    # inner — runs first on request

So: ApiKeyMiddleware decides 401 vs continue, then CSRFMiddleware
validates the token. The API-key middleware's "exempt POST with
csrftoken cookie" branch delegates enforcement to the CSRF layer
so plain `<form action="/api/...">` submissions from the UI work
without manual header injection.

Pure move — no behaviour changes.
"""
from __future__ import annotations

import hmac
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from shared import get_cfg


# ── API Key middleware ───────────────────────────────────────────────────────

class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Require an X-Api-Key header (or ?apikey= query param) on /api/
    routes. Exempts the SSE endpoint and health check. In-session
    browser requests (POST/PUT/DELETE/PATCH with a csrftoken cookie)
    are exempt because the CSRF middleware validates the token
    separately — this lets `<form action="/api/...">` submissions
    work without JS header injection.

    Fails closed: if the configured key is blank/missing (bad import,
    manual edit, partial migration), refuses the request. Never
    silently exposes /api/ to requests with no key.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith('/api/'):
            return await call_next(request)
        if path in ('/api/queue-events', '/api/health'):
            return await call_next(request)
        if (request.method in ('POST', 'PUT', 'DELETE', 'PATCH')
                and request.cookies.get('csrftoken')):
            return await call_next(request)

        api_key = (get_cfg('api_key', '') or '').strip()
        if not api_key:
            if not getattr(ApiKeyMiddleware, '_warned_no_key', False):
                print("[ERROR] /api/ routes denied — settings.api_key is blank. "
                      "Restart the app to auto-seed, or set one via Settings.")
                ApiKeyMiddleware._warned_no_key = True
            return JSONResponse(
                {"message": "Unauthorized",
                 "description": "API key not configured on the server"},
                status_code=401,
            )
        provided = (request.headers.get('X-Api-Key') or
                    request.query_params.get('apikey') or '')
        if provided != api_key:
            return JSONResponse(
                {"message": "Unauthorized",
                 "description": "Invalid or missing API key"},
                status_code=401,
            )
        return await call_next(request)


# ── CSRF middleware ──────────────────────────────────────────────────────────

_CSRF_COOKIE  = "csrftoken"
_CSRF_HEADER  = "X-CSRFToken"
_CSRF_FIELD   = "csrf_token"
_CSRF_SKIP_PREFIXES = ("/api/", "/static/", "/covers/")


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
        is_exempt = any(path.startswith(p) for p in _CSRF_SKIP_PREFIXES)
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
                            params = parse_qs(raw_body.decode("latin-1"), keep_blank_values=True)
                            fv = params.get(_CSRF_FIELD, [""])[0]
                        else:
                            _replayed_once = False
                            async def _tmp_receive():
                                nonlocal _replayed_once
                                if not _replayed_once:
                                    _replayed_once = True
                                    return {"type": "http.request", "body": raw_body, "more_body": False}
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
                            return {"type": "http.request", "body": raw_body, "more_body": False}
                        return {"type": "http.disconnect"}
                    forward_receive = _replay_receive

            if not valid:
                resp = JSONResponse({"detail": "CSRF token missing or invalid."}, status_code=403)
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
                parts = [f"{_CSRF_COOKIE}={token}", "Path=/", "SameSite=Strict", "HttpOnly"]
                if secure_cookie:
                    parts.append("Secure")
                cookie_val = "; ".join(parts)
                headers.append((b"set-cookie", cookie_val.encode()))
                message = {**message, "headers": headers}
                has_cookie = True
            await send(message)

        await self.app(scope, forward_receive, send_with_cookie)
