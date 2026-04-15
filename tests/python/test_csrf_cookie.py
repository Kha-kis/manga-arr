"""Tests for M1: CSRF cookie flag hardening.

Covers:
  - SameSite=Strict on every csrftoken Set-Cookie
  - HttpOnly on every csrftoken Set-Cookie
  - Secure set when the request arrived over HTTPS (direct or proxied)
  - Secure NOT set on plain HTTP (local development stays working)
  - An existing CSRF-protected POST still round-trips end-to-end
"""
import os
import sys
import tempfile

import pytest


@pytest.fixture
def app_client(monkeypatch):
    """Set up the real FastAPI app against a fresh tmp DB + api_key."""
    sys.path.insert(0, "tests/python")
    sys.path.insert(0, "app")
    import conftest  # noqa: F401 (applies /config + StaticFiles monkeypatches)
    import main
    import shared

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.unlink(tmp.name)
    monkeypatch.setattr(main, "DB_PATH", tmp.name)
    monkeypatch.setattr(shared, "DB_PATH", tmp.name)

    # Full schema — the library index ("/") queries the series table,
    # and init_db seeds a fresh api_key automatically.
    main.init_db()
    main.load_config()

    # Attach a minimal CSRF-covered GET route that doesn't depend on
    # templates (which live at /app/templates inside the container and
    # aren't reachable from the host test runner). Middleware runs on
    # every non-/api/ path, so this is enough to exercise the Set-Cookie.
    from fastapi.responses import PlainTextResponse as _Plain

    @main.app.get("/__csrf_probe__")
    async def _csrf_probe():
        return _Plain("ok")

    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    try:
        yield client, main
    finally:
        for ext in ("", "-wal", "-shm"):
            p = tmp.name + ext
            if os.path.exists(p):
                os.unlink(p)


def _csrf_cookie_header(resp):
    """Extract the Set-Cookie line for csrftoken from a response."""
    for raw in resp.headers.get_list("set-cookie"):
        if raw.lower().startswith("csrftoken="):
            return raw
    return None


# ───────────────────── cookie flag assertions ─────────────────────

def test_csrftoken_is_samesite_strict(app_client):
    client, _ = app_client
    # Fresh client, any GET to a non-exempt path triggers the cookie set.
    r = client.get("/__csrf_probe__", follow_redirects=False)
    cookie = _csrf_cookie_header(r)
    assert cookie is not None, f"no csrftoken Set-Cookie; headers: {r.headers.get_list('set-cookie')}"
    assert "SameSite=Strict" in cookie, f"expected SameSite=Strict, got: {cookie}"


def test_csrftoken_is_httponly(app_client):
    client, _ = app_client
    r = client.get("/__csrf_probe__", follow_redirects=False)
    cookie = _csrf_cookie_header(r)
    assert cookie is not None
    assert "HttpOnly" in cookie, f"HttpOnly missing; cookie: {cookie}"


def test_csrftoken_has_path_root(app_client):
    """Sanity: Path=/ preserved (was there before the hardening)."""
    client, _ = app_client
    r = client.get("/__csrf_probe__", follow_redirects=False)
    cookie = _csrf_cookie_header(r)
    assert "Path=/" in cookie


# ───────────────────── Secure flag, scheme-dependent ─────────────────────

def test_csrftoken_not_secure_on_plain_http(app_client):
    """TestClient issues http:// requests by default. Secure MUST NOT be
    set — the browser would silently drop a Secure cookie on http, breaking
    local development completely."""
    client, _ = app_client
    r = client.get("/__csrf_probe__", follow_redirects=False)
    cookie = _csrf_cookie_header(r)
    assert cookie is not None
    # Case-insensitive 'Secure' check but guard against 'SameSite=Strict'
    # containing "str" — anchor on the actual attribute.
    attrs = [p.strip() for p in cookie.split(";")]
    assert "Secure" not in attrs, f"Secure unexpectedly set on HTTP: {cookie}"


def test_csrftoken_secure_via_x_forwarded_proto(app_client):
    """A reverse proxy that terminated TLS tells us via X-Forwarded-Proto: https.
    That request must get Secure on its Set-Cookie."""
    client, _ = app_client
    r = client.get("/__csrf_probe__", headers={"X-Forwarded-Proto": "https"}, follow_redirects=False)
    cookie = _csrf_cookie_header(r)
    assert cookie is not None
    attrs = [p.strip() for p in cookie.split(";")]
    assert "Secure" in attrs, f"Secure missing behind X-Forwarded-Proto: {cookie}"


def test_csrftoken_secure_via_x_forwarded_ssl(app_client):
    """Some older proxies use X-Forwarded-Ssl: on instead."""
    client, _ = app_client
    r = client.get("/__csrf_probe__", headers={"X-Forwarded-Ssl": "on"}, follow_redirects=False)
    cookie = _csrf_cookie_header(r)
    assert cookie is not None
    attrs = [p.strip() for p in cookie.split(";")]
    assert "Secure" in attrs, f"Secure missing behind X-Forwarded-Ssl: {cookie}"


def test_csrftoken_respects_forwarded_proto_http(app_client):
    """X-Forwarded-Proto: http should NOT enable Secure, even if some other
    header suggests otherwise."""
    client, _ = app_client
    r = client.get("/__csrf_probe__", headers={"X-Forwarded-Proto": "http"}, follow_redirects=False)
    cookie = _csrf_cookie_header(r)
    assert cookie is not None
    attrs = [p.strip() for p in cookie.split(";")]
    assert "Secure" not in attrs


# ───────────────────── CSRF flow still works ─────────────────────

def test_csrf_protected_post_still_round_trips(app_client):
    """An existing CSRF-protected POST flow must succeed with the hardened
    cookie. We use /settings/general which is a plain form POST that the
    CSRFMiddleware protects."""
    client, main = app_client

    # First request: GET the settings page to receive a csrftoken cookie.
    # We skip the actual page render to avoid template dependency noise;
    # any non-/api/ GET sets the cookie.
    r = client.get("/__csrf_probe__", follow_redirects=False)
    # TestClient persists cookies on the session client (client.cookies).
    token = client.cookies.get("csrftoken")
    assert token, "client should have a csrftoken cookie after the first GET"

    # Now POST with the token in BOTH the cookie (auto, via client.cookies)
    # and the form field (double-submit check the middleware performs).
    r = client.post(
        "/settings/general",
        data={"csrf_token": token, "rss_interval": "900", "refresh_interval": "86400"},
        follow_redirects=False,
    )
    assert r.status_code != 403, f"CSRF-protected POST rejected: {r.status_code}"


def test_csrf_post_without_token_is_rejected(app_client):
    """Sanity: the middleware still rejects a POST that doesn't carry the
    token — the hardened cookie hasn't weakened protection."""
    client, _ = app_client
    # No CSRF cookie, no csrf_token form field
    client.cookies.clear()
    r = client.post("/settings/general", data={"rss_interval": "900"},
                    follow_redirects=False)
    assert r.status_code == 403


# ───────────────────── should_secure_cookie unit ─────────────────────

def test_should_secure_cookie_direct_https():
    import main
    scope = {"scheme": "https", "headers": []}
    assert main._should_secure_cookie(scope) is True


def test_should_secure_cookie_direct_http():
    import main
    scope = {"scheme": "http", "headers": []}
    assert main._should_secure_cookie(scope) is False


def test_should_secure_cookie_x_forwarded_proto():
    import main
    scope = {"scheme": "http", "headers": [(b"x-forwarded-proto", b"https")]}
    assert main._should_secure_cookie(scope) is True


def test_should_secure_cookie_x_forwarded_ssl():
    import main
    scope = {"scheme": "http", "headers": [(b"x-forwarded-ssl", b"on")]}
    assert main._should_secure_cookie(scope) is True


def test_should_secure_cookie_ignores_unrelated_headers():
    import main
    scope = {"scheme": "http", "headers": [
        (b"x-forwarded-proto", b"http"),
        (b"x-forwarded-ssl", b"off"),
        (b"user-agent", b"TestClient/1.0"),
    ]}
    assert main._should_secure_cookie(scope) is False
