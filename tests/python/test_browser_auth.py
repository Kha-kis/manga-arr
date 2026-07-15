"""Single-admin browser authentication and session security coverage."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient


ADMIN_USERNAME = "test-admin"
ADMIN_PASSWORD = "correct horse battery staple"


@pytest.fixture
def auth_env(monkeypatch, tmp_path):
    import auth
    import main
    import shared

    db_path = str(tmp_path / "auth.db")
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir, mode=0o700)
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(shared, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "_CONFIG_DIR", config_dir)
    auth.set_test_auth_bypass(False)
    auth.LOGIN_THROTTLE.reset()
    main.init_db()
    main.load_config()
    main.ensure_api_key()
    yield {"db_path": db_path, "config_dir": config_dir}
    auth.LOGIN_THROTTLE.reset()
    auth.set_test_auth_bypass(True)


def _csrf(client: TestClient) -> str:
    return client.cookies.get("csrftoken")


def _setup(client: TestClient):
    page = client.get("/setup")
    assert page.status_code == 200
    response = client.post(
        "/setup",
        data={
            "csrf_token": _csrf(client),
            "username": ADMIN_USERNAME,
            "password": ADMIN_PASSWORD,
            "password_confirm": ADMIN_PASSWORD,
            "next": "/",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response


def _login(client: TestClient, password: str = ADMIN_PASSWORD):
    page = client.get("/login")
    assert page.status_code == 200
    return client.post(
        "/login",
        data={
            "csrf_token": _csrf(client),
            "username": ADMIN_USERNAME,
            "password": password,
            "next": "/",
        },
        follow_redirects=False,
    )


def test_unauthenticated_install_redirects_to_browser_setup(auth_env):
    import main

    client = TestClient(main.app)
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/setup?next=")
    setup = client.get("/setup")
    assert setup.status_code == 200
    assert "Create administrator" in setup.text
    assert 'name="setup_token"' not in setup.text
    assert 'id="setup-username"' in setup.text
    assert "autofocus" in setup.text


def test_legacy_setup_token_is_removed(auth_env):
    import auth

    path = os.path.join(auth_env["config_dir"], ".mangarr-setup-token")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("obsolete")

    auth.remove_legacy_setup_token()

    assert not os.path.exists(path)


def test_concurrent_admin_claim_has_one_winner(auth_env):
    import auth

    password_hash = auth.hash_password(ADMIN_PASSWORD)

    def claim(index):
        try:
            return auth.create_admin(f"admin-{index}", password_hash)
        except RuntimeError:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(claim, range(4)))

    assert len([result for result in results if result]) == 1
    assert auth.is_admin_configured() is True


def test_setup_rejects_invalid_account_details_without_creating_admin(auth_env):
    import auth
    import main

    client = TestClient(main.app)
    client.get("/setup")
    response = client.post(
        "/setup",
        data={
            "csrf_token": _csrf(client),
            "username": "x",
            "password": ADMIN_PASSWORD,
            "password_confirm": ADMIN_PASSWORD,
        },
    )

    assert response.status_code == 400
    assert "username must be" in response.text.lower()
    assert auth.is_admin_configured() is False


def test_non_ascii_credentials_fail_cleanly(auth_env):
    import main

    setup_client = TestClient(main.app)
    _setup(setup_client)
    client = TestClient(main.app)
    client.get("/login")
    response = client.post(
        "/login",
        data={
            "csrf_token": _csrf(client),
            "username": "tést-admin",
            "password": ADMIN_PASSWORD,
        },
    )
    assert response.status_code == 401


def test_setup_stores_argon2id_hash_and_starts_secure_session(auth_env):
    import auth
    import main

    client = TestClient(main.app)
    response = _setup(client)

    assert "mangarr_session=" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    with sqlite3.connect(auth_env["db_path"]) as db:
        row = db.execute(
            "SELECT username,password_hash FROM auth_admin WHERE id=1"
        ).fetchone()
        session_hash = db.execute("SELECT token_hash FROM auth_sessions").fetchone()[0]
    assert row[0] == ADMIN_USERNAME
    assert row[1].startswith("$argon2id$")
    assert ADMIN_PASSWORD not in row[1]
    cookie_token = client.cookies.get("mangarr_session")
    assert session_hash == hashlib.sha256(cookie_token.encode()).hexdigest()
    assert cookie_token not in session_hash
    assert client.get("/").status_code == 200


def test_session_cookie_is_secure_behind_https_proxy(auth_env):
    import main

    client = TestClient(main.app, base_url="https://testserver")
    client.get("/setup")
    response = client.post(
        "/setup",
        data={
            "csrf_token": _csrf(client),
            "username": ADMIN_USERNAME,
            "password": ADMIN_PASSWORD,
            "password_confirm": ADMIN_PASSWORD,
        },
        follow_redirects=False,
    )
    assert "Secure" in response.headers["set-cookie"]


def test_logout_revokes_server_session(auth_env):
    import main

    client = TestClient(main.app)
    _setup(client)
    response = client.post(
        "/logout",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert (
        client.get("/", follow_redirects=False).headers["location"].startswith("/login")
    )


def test_login_failure_is_generic_and_success_restores_access(auth_env):
    import main

    setup_client = TestClient(main.app)
    _setup(setup_client)
    client = TestClient(main.app)
    failed = _login(client, "definitely-not-the-password")
    assert failed.status_code == 401
    assert "Invalid username or password" in failed.text
    success = _login(client)
    assert success.status_code == 303
    assert client.get("/").status_code == 200


def test_login_throttle_returns_retry_after(auth_env, monkeypatch):
    import main
    import routers.auth as auth_router

    setup_client = TestClient(main.app)
    _setup(setup_client)
    monkeypatch.setattr(auth_router, "verify_admin_credentials", lambda *_args: None)
    client = TestClient(main.app)
    for _ in range(5):
        response = _login(client, "wrong-password")
        assert response.status_code == 401
    throttled = _login(client, "wrong-password")
    assert throttled.status_code == 429
    assert int(throttled.headers["retry-after"]) > 0


def test_idle_session_expires_and_is_deleted(auth_env):
    import main

    client = TestClient(main.app)
    _setup(client)
    stale = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    with sqlite3.connect(auth_env["db_path"]) as db:
        db.execute("UPDATE auth_sessions SET last_seen_at=?", (stale,))
        db.commit()
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
    with sqlite3.connect(auth_env["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0] == 0


def test_absolute_session_expiry_is_enforced(auth_env):
    import main

    client = TestClient(main.app)
    _setup(client)
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(auth_env["db_path"]) as db:
        db.execute("UPDATE auth_sessions SET expires_at=?", (expired,))
        db.commit()

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_api_key_auth_remains_independent_of_browser_session(auth_env):
    import main
    import shared

    client = TestClient(main.app)
    _setup(client)
    api_key = shared.get_cfg("api_key")
    api_client = TestClient(main.app)
    denied = api_client.get("/api/v1/health")
    allowed = api_client.get("/api/v1/health", headers={"X-Api-Key": api_key})
    assert denied.status_code == 401
    assert allowed.status_code == 200


def test_password_change_revokes_other_sessions_and_keeps_current(auth_env):
    import main

    first = TestClient(main.app)
    _setup(first)
    second = TestClient(main.app)
    assert _login(second).status_code == 303
    assert second.get("/").status_code == 200

    first.get("/settings/security")
    replacement = "a newer correct horse battery staple"
    changed = first.post(
        "/settings/security/password",
        data={
            "csrf_token": _csrf(first),
            "current_password": ADMIN_PASSWORD,
            "new_password": replacement,
            "new_password_confirm": replacement,
        },
        follow_redirects=False,
    )
    assert changed.status_code == 303
    assert first.get("/").status_code == 200
    assert second.get("/", follow_redirects=False).status_code == 303
    third = TestClient(main.app)
    assert _login(third, ADMIN_PASSWORD).status_code == 401
    assert _login(third, replacement).status_code == 303


def test_revoke_other_sessions_keeps_current_session(auth_env):
    import main

    first = TestClient(main.app)
    _setup(first)
    second = TestClient(main.app)
    assert _login(second).status_code == 303

    first.get("/settings/security")
    response = first.post(
        "/settings/security/sessions/revoke",
        data={"csrf_token": _csrf(first)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert first.get("/").status_code == 200
    assert second.get("/", follow_redirects=False).status_code == 303


def test_local_recovery_reset_removes_admin_and_revokes_sessions(auth_env):
    import auth
    import main

    client = TestClient(main.app)
    _setup(client)

    auth.reset_admin_for_recovery()

    assert auth.is_admin_configured() is False
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/setup")


def test_external_next_url_is_never_used_for_redirect(auth_env):
    import main

    client = TestClient(main.app)
    _setup(client)
    client.post("/logout", data={"csrf_token": _csrf(client)})
    client.get("/login?next=https://attacker.example/")
    response = client.post(
        "/login",
        data={
            "csrf_token": _csrf(client),
            "username": ADMIN_USERNAME,
            "password": ADMIN_PASSWORD,
            "next": "https://attacker.example/",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_healthz_is_public_but_database_backed(auth_env):
    import main

    response = TestClient(main.app).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_auth_middleware_order_and_public_path_boundary(auth_env):
    import main

    from middleware import (
        ApiKeyMiddleware,
        ApiVersionAliasMiddleware,
        BrowserAuthMiddleware,
        CSRFMiddleware,
        RequestBodyLimitMiddleware,
    )

    middleware = [entry.cls for entry in main.app.user_middleware]
    expected_order = [
        RequestBodyLimitMiddleware,
        ApiVersionAliasMiddleware,
        BrowserAuthMiddleware,
        ApiKeyMiddleware,
        CSRFMiddleware,
    ]
    positions = [middleware.index(item) for item in expected_order]
    assert positions == sorted(positions)
    assert "/static/" in BrowserAuthMiddleware._PUBLIC_PREFIXES

    client = TestClient(main.app)
    assert client.get("/healthz").status_code == 200
    assert client.get("/covers/missing.jpg", follow_redirects=False).status_code == 303
    assert client.get("/docs", follow_redirects=False).status_code == 303


def test_auth_pages_and_security_settings_are_not_cacheable(auth_env):
    import main

    client = TestClient(main.app)
    assert client.get("/setup").headers["cache-control"] == "no-store"
    _setup(client)
    assert client.get("/settings/security").headers["cache-control"] == "no-store"


def test_database_failure_is_not_treated_as_first_run(auth_env, monkeypatch):
    import auth
    import main

    def fail_db_open():
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(auth, "get_db", fail_db_open)
    client = TestClient(main.app, raise_server_exceptions=False)

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 500
    assert "location" not in response.headers
