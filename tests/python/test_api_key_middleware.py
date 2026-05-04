"""Tests for H2: API-key middleware fails closed when api_key is blank,
and ensure_api_key() guarantees a non-empty key after startup.
"""
import os
import sqlite3
import tempfile

import pytest


# ───────────────────── ensure_api_key seeding ─────────────────────

@pytest.fixture
def fresh_db(monkeypatch):
    """Point main.DB_PATH at an empty tmp file; init_db creates the schema."""
    import main
    import shared
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.unlink(tmp.name)
    monkeypatch.setattr(main, "DB_PATH", tmp.name)
    monkeypatch.setattr(shared, "DB_PATH", tmp.name)
    try:
        yield tmp.name
    finally:
        for ext in ("", "-wal", "-shm"):
            p = tmp.name + ext
            if os.path.exists(p):
                os.unlink(p)


def test_init_db_seeds_api_key_when_missing(fresh_db):
    """Fresh DB has no settings.api_key row at all → init_db must create one."""
    import main
    main.init_db()
    with sqlite3.connect(fresh_db) as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT value FROM settings WHERE key='api_key'").fetchone()
    assert row is not None
    assert row["value"] and len(row["value"]) >= 32


def test_ensure_api_key_replaces_blank_value(fresh_db):
    """Row exists but value is blank/whitespace → ensure_api_key replaces it."""
    import main
    main.init_db()
    # Null the row to simulate a bad import / manual edit / partial migration.
    with sqlite3.connect(fresh_db) as c:
        c.execute("UPDATE settings SET value='' WHERE key='api_key'")
        c.commit()
    main.load_config()
    assert main.get_cfg("api_key") == ""

    new_key = main.ensure_api_key()
    assert new_key and len(new_key) >= 32

    # Persisted in DB
    with sqlite3.connect(fresh_db) as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT value FROM settings WHERE key='api_key'").fetchone()
    assert row["value"] == new_key

    # Cached in CONFIG so middleware sees it without a separate load_config()
    assert main.get_cfg("api_key") == new_key


def test_ensure_api_key_replaces_whitespace_only_value(fresh_db):
    """Whitespace-only counts as blank — must be replaced, not preserved."""
    import main
    main.init_db()
    with sqlite3.connect(fresh_db) as c:
        c.execute("UPDATE settings SET value='   ' WHERE key='api_key'")
        c.commit()
    new_key = main.ensure_api_key()
    assert new_key.strip() != "   "
    assert len(new_key) >= 32


def test_ensure_api_key_is_idempotent_when_set(fresh_db):
    """If a real key is already present, ensure_api_key returns it unchanged."""
    import main
    main.init_db()
    with sqlite3.connect(fresh_db) as c:
        c.row_factory = sqlite3.Row
        original = c.execute("SELECT value FROM settings WHERE key='api_key'").fetchone()["value"]
    same = main.ensure_api_key()
    assert same == original


# ───────────────────── middleware fail-closed ─────────────────────

def _make_test_app(api_key_value):
    """Build a minimal FastAPI app with the real ApiKeyMiddleware applied
    to a /api/ route plus a /non-api/ route, and stub get_cfg to return the
    requested api_key value. Avoids depending on the full main app + DB."""
    import main
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/api/ping")
    async def ping():
        return {"ok": True}

    @app.post("/api/ping")
    async def ping_post():
        return {"ok": True}

    @app.get("/non-api/ping")
    async def non_api_ping():
        return {"ok": True}

    app.add_middleware(main.ApiKeyMiddleware)

    # Stub get_cfg in main and shared so the middleware reads our value.
    import shared
    main.CONFIG["api_key"] = api_key_value
    shared.CONFIG["api_key"] = api_key_value
    # Reset the once-per-process warning flag so tests are independent.
    if hasattr(main.ApiKeyMiddleware, "_warned_no_key"):
        delattr(main.ApiKeyMiddleware, "_warned_no_key")
    return app


def _make_csrf_api_test_app(api_key_value):
    """Build an app with the production API-key + CSRF middleware stack."""
    import main
    from fastapi import FastAPI

    app = FastAPI()

    @app.post("/api/mutate")
    async def mutate():
        return {"ok": True}

    app.add_middleware(main.CSRFMiddleware)
    app.add_middleware(main.ApiKeyMiddleware)

    import shared
    main.CONFIG["api_key"] = api_key_value
    shared.CONFIG["api_key"] = api_key_value
    if hasattr(main.ApiKeyMiddleware, "_warned_no_key"):
        delattr(main.ApiKeyMiddleware, "_warned_no_key")
    return app


def test_api_route_fails_closed_when_api_key_blank():
    """The bug being fixed: blank server-side api_key must NOT allow the
    request through. Must return 401 with a clear message."""
    from fastapi.testclient import TestClient
    app = _make_test_app("")
    client = TestClient(app)
    r = client.get("/api/ping")
    assert r.status_code == 401, f"FAIL OPEN: got {r.status_code} {r.text!r}"
    assert "not configured" in r.json()["description"].lower()


def test_api_route_fails_closed_when_api_key_whitespace():
    """Whitespace-only is treated as blank by the middleware."""
    from fastapi.testclient import TestClient
    app = _make_test_app("   ")
    client = TestClient(app)
    r = client.get("/api/ping")
    assert r.status_code == 401


def test_api_route_rejects_missing_request_key():
    """Server has a key configured; request sends none → 401."""
    from fastapi.testclient import TestClient
    app = _make_test_app("server-secret")
    client = TestClient(app)
    r = client.get("/api/ping")
    assert r.status_code == 401
    assert "invalid or missing" in r.json()["description"].lower()


def test_api_route_rejects_wrong_request_key():
    """Server has a key configured; request sends a different one → 401."""
    from fastapi.testclient import TestClient
    app = _make_test_app("server-secret")
    client = TestClient(app)
    r = client.get("/api/ping", headers={"X-Api-Key": "wrong-key"})
    assert r.status_code == 401


def test_api_route_accepts_correct_request_key():
    """Server key + matching client key → 200."""
    from fastapi.testclient import TestClient
    app = _make_test_app("server-secret")
    client = TestClient(app)
    r = client.get("/api/ping", headers={"X-Api-Key": "server-secret"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_api_route_accepts_correct_key_via_query_param():
    """The middleware also accepts ?apikey=… on the query string."""
    from fastapi.testclient import TestClient
    app = _make_test_app("server-secret")
    client = TestClient(app)
    r = client.get("/api/ping?apikey=server-secret")
    assert r.status_code == 200


def test_mutating_api_route_with_cookie_but_no_api_key_requires_csrf():
    """A csrftoken cookie alone must not bypass both API key and CSRF checks."""
    from fastapi.testclient import TestClient
    app = _make_csrf_api_test_app("server-secret")
    client = TestClient(app)
    r = client.post("/api/mutate", cookies={"csrftoken": "x" * 64})
    assert r.status_code == 403


def test_mutating_api_route_accepts_cookie_delegate_with_valid_csrf():
    """Browser API form submissions may use the CSRF path instead of API key."""
    from fastapi.testclient import TestClient
    app = _make_csrf_api_test_app("server-secret")
    client = TestClient(app)
    token = "x" * 64
    r = client.post(
        "/api/mutate",
        cookies={"csrftoken": token},
        headers={"X-CSRFToken": token},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_mutating_api_route_with_api_key_does_not_require_csrf():
    """External API clients authenticate by API key and do not need CSRF."""
    from fastapi.testclient import TestClient
    app = _make_csrf_api_test_app("server-secret")
    client = TestClient(app)
    r = client.post("/api/mutate", headers={"X-Api-Key": "server-secret"})
    assert r.status_code == 200


def test_mutating_api_route_prefers_valid_api_key_over_cookie_delegate():
    """Browser-context API calls may carry cookies; a valid API key still wins."""
    from fastapi.testclient import TestClient
    app = _make_csrf_api_test_app("server-secret")
    client = TestClient(app)
    r = client.post(
        "/api/mutate",
        headers={"X-Api-Key": "server-secret"},
        cookies={"csrftoken": "x" * 64},
    )
    assert r.status_code == 200


def test_mutating_api_route_rejects_wrong_api_key_even_with_cookie():
    """A CSRF cookie must not mask an explicitly wrong API key."""
    from fastapi.testclient import TestClient
    app = _make_csrf_api_test_app("server-secret")
    client = TestClient(app)
    r = client.post(
        "/api/mutate",
        headers={"X-Api-Key": "wrong"},
        cookies={"csrftoken": "x" * 64},
    )
    assert r.status_code == 401


def test_non_api_route_unaffected_when_api_key_blank():
    """Non-/api/ routes must NOT be touched by the api-key middleware,
    regardless of whether the key is configured. (CSRF / other middleware
    handles those routes; this test only asserts the /api/ guard isn't
    accidentally broadened.)"""
    from fastapi.testclient import TestClient
    app = _make_test_app("")  # blank — would 401 a /api/ route
    client = TestClient(app)
    r = client.get("/non-api/ping")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_non_api_route_unaffected_when_api_key_set():
    from fastapi.testclient import TestClient
    app = _make_test_app("server-secret")
    client = TestClient(app)
    r = client.get("/non-api/ping")  # no key sent
    assert r.status_code == 200
