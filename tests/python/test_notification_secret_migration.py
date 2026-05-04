"""Tests for H4 PR #4: notification_connections.settings JSON secret encryption.

Covers:
  - migration encrypts each type's secret fields inside the JSON blob
  - non-secret JSON keys are preserved unchanged
  - migration skips missing/empty values, already-encrypted values
  - migration is idempotent
  - send_connection decrypts before handing the dict to each _send_* helper
  - create/update forms store encrypted secret fields in the JSON blob
  - wrong key disables that connection cleanly — fanout continues
  - notification_connections.settings stays valid JSON
  - no canary plaintext / enc:v1 ciphertext appears in logs
"""
import io
import json
import logging
import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch

import pytest

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401


def _csrf_header(client) -> dict[str, str]:
    token = client.cookies.get("csrftoken", "")
    return {"X-CSRFToken": token} if token else {}


@pytest.fixture
def fresh_env(monkeypatch, tmp_path):
    import main, shared, security

    db_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_tmp.close(); os.unlink(db_tmp.name)
    monkeypatch.setattr(main, "DB_PATH", db_tmp.name)
    monkeypatch.setattr(shared, "DB_PATH", db_tmp.name)
    monkeypatch.delenv("MANGARR_SECRET_KEY", raising=False)
    monkeypatch.setattr(security, "_SECRET_CIPHER", None)

    key_dir = str(tmp_path / "config")
    os.makedirs(key_dir, exist_ok=True)
    security.load_or_create_secret_cipher(key_dir)

    main.init_db()
    main.load_config()

    yield {"db_path": db_tmp.name, "key_dir": key_dir}

    for ext in ("", "-wal", "-shm"):
        p = db_tmp.name + ext
        if os.path.exists(p):
            os.unlink(p)


def _seed(db_path, *, name, type, settings, enabled=1):
    with sqlite3.connect(db_path) as c:
        cur = c.execute(
            "INSERT INTO notification_connections(name,type,enabled,settings,"
            " on_grab,on_download,on_upgrade,on_series_add,on_health_issue,on_health_restored)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (name, type, enabled, json.dumps(settings),
             1, 1, 1, 1, 1, 0),
        )
        c.commit()
    return cur.lastrowid


def _settings(db_path, conn_id):
    with sqlite3.connect(db_path) as c:
        r = c.execute(
            "SELECT settings FROM notification_connections WHERE id=?", (conn_id,)
        ).fetchone()
    return r[0] if r else None


def _blob(db_path, conn_id):
    raw = _settings(db_path, conn_id)
    assert raw is not None
    return json.loads(raw)


# ───────────────────── migration: per-type encryption ─────────────────────

def test_migration_encrypts_discord_webhook(fresh_env):
    import main
    cid = _seed(fresh_env["db_path"], name="disc-1", type="discord",
                settings={"webhook_url": "https://CANARY-discord/webhook/abc",
                          "mention": "@channel"})
    n = main.migrate_encrypt_notification_connection_secrets()
    assert n == 1
    blob = _blob(fresh_env["db_path"], cid)
    assert blob["webhook_url"].startswith("enc:v1:")
    # Non-secret field preserved
    assert blob["mention"] == "@channel"


def test_migration_encrypts_slack_webhook(fresh_env):
    import main
    cid = _seed(fresh_env["db_path"], name="slk", type="slack",
                settings={"webhook_url": "https://hooks.slack.com/services/CANARY",
                          "channel": "#alerts"})
    n = main.migrate_encrypt_notification_connection_secrets()
    assert n == 1
    b = _blob(fresh_env["db_path"], cid)
    assert b["webhook_url"].startswith("enc:v1:")
    assert b["channel"] == "#alerts"


def test_migration_encrypts_telegram_bot_token(fresh_env):
    import main
    cid = _seed(fresh_env["db_path"], name="tg", type="telegram",
                settings={"bot_token": "CANARY-tg-1234:abcdef",
                          "chat_id": "987654321"})
    main.migrate_encrypt_notification_connection_secrets()
    b = _blob(fresh_env["db_path"], cid)
    assert b["bot_token"].startswith("enc:v1:")
    # chat_id is NOT a secret — stays plaintext
    assert b["chat_id"] == "987654321"


def test_migration_encrypts_ntfy_token(fresh_env):
    import main
    cid = _seed(fresh_env["db_path"], name="ntfy", type="ntfy",
                settings={"server": "https://ntfy.sh", "topic": "mangarr",
                          "token": "CANARY-ntfy-tk"})
    main.migrate_encrypt_notification_connection_secrets()
    b = _blob(fresh_env["db_path"], cid)
    assert b["token"].startswith("enc:v1:")
    assert b["server"] == "https://ntfy.sh"
    assert b["topic"] == "mangarr"


def test_migration_encrypts_gotify_app_token(fresh_env):
    import main
    cid = _seed(fresh_env["db_path"], name="got", type="gotify",
                settings={"server": "http://gotify.lan",
                          "app_token": "CANARY-gotify-tk"})
    main.migrate_encrypt_notification_connection_secrets()
    b = _blob(fresh_env["db_path"], cid)
    assert b["app_token"].startswith("enc:v1:")
    assert b["server"] == "http://gotify.lan"


def test_migration_encrypts_both_pushover_keys(fresh_env):
    import main
    cid = _seed(fresh_env["db_path"], name="po", type="pushover",
                settings={"user_key": "CANARY-po-user",
                          "api_token": "CANARY-po-app",
                          "priority": 0})
    main.migrate_encrypt_notification_connection_secrets()
    b = _blob(fresh_env["db_path"], cid)
    assert b["user_key"].startswith("enc:v1:")
    assert b["api_token"].startswith("enc:v1:")
    assert b["priority"] == 0


def test_migration_encrypts_webhook_url(fresh_env):
    import main
    cid = _seed(fresh_env["db_path"], name="wh", type="webhook",
                settings={"url": "https://CANARY-webhook.example/hook",
                          "method": "POST"})
    main.migrate_encrypt_notification_connection_secrets()
    b = _blob(fresh_env["db_path"], cid)
    assert b["url"].startswith("enc:v1:")
    assert b["method"] == "POST"


def test_migration_encrypts_email_password(fresh_env):
    import main
    cid = _seed(fresh_env["db_path"], name="em", type="email",
                settings={"host": "smtp.example.com", "port": 587,
                          "username": "u", "password": "CANARY-smtp-pw",
                          "from": "mangarr@x", "to": "me@x"})
    main.migrate_encrypt_notification_connection_secrets()
    b = _blob(fresh_env["db_path"], cid)
    assert b["password"].startswith("enc:v1:")
    # Non-secrets preserved
    assert b["host"] == "smtp.example.com"
    assert b["port"] == 587
    assert b["username"] == "u"
    assert b["from"] == "mangarr@x"
    assert b["to"] == "me@x"


def test_migration_encrypts_apprise_url_and_config_key(fresh_env):
    import main
    cid = _seed(fresh_env["db_path"], name="ap", type="apprise",
                settings={"url": "https://CANARY-apprise.lan",
                          "config_key": "CANARY-apprise-cfg",
                          "tags": "manga"})
    main.migrate_encrypt_notification_connection_secrets()
    b = _blob(fresh_env["db_path"], cid)
    assert b["url"].startswith("enc:v1:")
    assert b["config_key"].startswith("enc:v1:")
    assert b["tags"] == "manga"


def test_migration_encrypts_pushbullet_access_token(fresh_env):
    import main
    cid = _seed(fresh_env["db_path"], name="pb", type="pushbullet",
                settings={"access_token": "CANARY-pb-tk"})
    main.migrate_encrypt_notification_connection_secrets()
    b = _blob(fresh_env["db_path"], cid)
    assert b["access_token"].startswith("enc:v1:")


# ───────────────────── migration: skip / idempotent ─────────────────────

def test_migration_skips_already_encrypted(fresh_env):
    import main
    from security import encrypt_secret
    pre = encrypt_secret("https://hook/abc")
    cid = _seed(fresh_env["db_path"], name="d", type="discord",
                settings={"webhook_url": pre, "mention": "@everyone"})
    n = main.migrate_encrypt_notification_connection_secrets()
    assert n == 0
    b = _blob(fresh_env["db_path"], cid)
    assert b["webhook_url"] == pre
    assert b["mention"] == "@everyone"


def test_migration_skips_empty_and_missing(fresh_env):
    import main
    cid_empty   = _seed(fresh_env["db_path"], name="e", type="discord",
                        settings={"webhook_url": ""})
    cid_missing = _seed(fresh_env["db_path"], name="m", type="discord",
                        settings={"mention": "@x"})
    n = main.migrate_encrypt_notification_connection_secrets()
    assert n == 0
    assert _blob(fresh_env["db_path"], cid_empty)["webhook_url"] == ""
    assert "webhook_url" not in _blob(fresh_env["db_path"], cid_missing)


def test_migration_is_idempotent(fresh_env):
    import main
    _seed(fresh_env["db_path"], name="d", type="discord",
          settings={"webhook_url": "https://CANARY-x"})
    _seed(fresh_env["db_path"], name="t", type="telegram",
          settings={"bot_token": "CANARY-tg", "chat_id": "1"})
    n1 = main.migrate_encrypt_notification_connection_secrets()
    n2 = main.migrate_encrypt_notification_connection_secrets()
    n3 = main.migrate_encrypt_notification_connection_secrets()
    assert n1 == 2
    assert n2 == 0
    assert n3 == 0


def test_migration_preserves_unknown_keys(fresh_env):
    import main
    cid = _seed(fresh_env["db_path"], name="wh", type="webhook",
                settings={"url": "https://CANARY-wh",
                          "method": "PUT",
                          "custom_field": {"nested": "value"},
                          "flags": [1, 2, 3]})
    main.migrate_encrypt_notification_connection_secrets()
    b = _blob(fresh_env["db_path"], cid)
    assert b["url"].startswith("enc:v1:")
    assert b["method"] == "PUT"
    assert b["custom_field"] == {"nested": "value"}
    assert b["flags"] == [1, 2, 3]


def test_migration_skips_malformed_json(fresh_env, caplog):
    """A row with broken JSON in settings is skipped with a WARNING —
    never aborts the migration of sibling rows."""
    import main
    # Inject malformed JSON directly
    with sqlite3.connect(fresh_env["db_path"]) as c:
        c.execute(
            "INSERT INTO notification_connections(name,type,enabled,settings,"
            " on_grab,on_download,on_upgrade,on_series_add,on_health_issue,on_health_restored)"
            " VALUES('bad','discord',1,'{not valid',1,1,1,1,1,0)"
        )
        c.commit()
    cid_ok = _seed(fresh_env["db_path"], name="ok", type="discord",
                   settings={"webhook_url": "https://CANARY-ok"})
    with caplog.at_level(logging.WARNING, logger="main"):
        n = main.migrate_encrypt_notification_connection_secrets()
    # Only the good row migrated
    assert n == 1
    assert _blob(fresh_env["db_path"], cid_ok)["webhook_url"].startswith("enc:v1:")
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "malformed JSON" in joined


def test_migration_noop_when_cipher_unavailable(fresh_env, monkeypatch, caplog):
    import main, security
    monkeypatch.setattr(security, "_SECRET_CIPHER", None)
    cid = _seed(fresh_env["db_path"], name="d", type="discord",
                settings={"webhook_url": "https://CANARY-disc"})
    with caplog.at_level(logging.WARNING, logger="main"):
        n = main.migrate_encrypt_notification_connection_secrets()
    assert n == 0
    # Plaintext preserved
    assert _blob(fresh_env["db_path"], cid)["webhook_url"] == "https://CANARY-disc"
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "skipped" in joined.lower() and "cipher unavailable" in joined.lower()


# ───────────────────── send-path decryption ─────────────────────

def _run_send(conn_dict, message="hi"):
    """Sync runner so each test can assert on the observed HTTP call.

    Patches validate_outbound_url to a no-op so DNS lookups don't turn
    the test into an integration check. The purpose here is to verify
    the decrypted secret reaches the HTTP layer, not to re-test SSRF
    (covered by test_ssrf.py).
    """
    import asyncio
    from routers.notification_connections import send_connection
    with patch("routers.notification_connections.validate_outbound_url",
               lambda *a, **kw: None):
        return asyncio.run(send_connection(conn_dict, message))


def test_send_discord_gets_plaintext_webhook(fresh_env):
    import main
    cid = _seed(fresh_env["db_path"], name="d1", type="discord",
                settings={"webhook_url": "https://CANARY-disc/wh",
                          "mention": "@here"})
    main.migrate_encrypt_notification_connection_secrets()
    # Reload row from DB so it carries the encrypted blob
    with sqlite3.connect(fresh_env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        row = dict(c.execute(
            "SELECT * FROM notification_connections WHERE id=?", (cid,)
        ).fetchone())
    assert json.loads(row["settings"])["webhook_url"].startswith("enc:v1:")

    observed = {}

    class _Resp:
        status_code = 204
        text = ""

    class _AsyncCli:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, url, **kw):
            observed['url'] = url
            observed['json'] = kw.get('json') or {}
            return _Resp()

    with patch("routers.notification_connections.httpx.AsyncClient", _AsyncCli):
        ok, msg = _run_send(row, "hello")
    assert ok, msg
    assert observed['url'] == "https://CANARY-disc/wh"


def test_send_telegram_gets_plaintext_bot_token(fresh_env):
    import main
    cid = _seed(fresh_env["db_path"], name="tg", type="telegram",
                settings={"bot_token": "CANARY-botkey-xyz",
                          "chat_id": "123456"})
    main.migrate_encrypt_notification_connection_secrets()
    with sqlite3.connect(fresh_env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        row = dict(c.execute(
            "SELECT * FROM notification_connections WHERE id=?", (cid,)
        ).fetchone())

    observed = {}

    class _Resp:
        status_code = 200
        def json(self): return {"ok": True}

    class _AsyncCli:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, url, **kw):
            observed['url'] = url
            observed['json'] = kw.get('json') or {}
            return _Resp()

    with patch("routers.notification_connections.httpx.AsyncClient", _AsyncCli):
        ok, _ = _run_send(row, "hi")
    assert ok
    # Telegram URL embeds the bot token; assert the plaintext reached the HTTP layer
    assert "CANARY-botkey-xyz" in observed['url']
    assert observed['json']['chat_id'] == "123456"


def test_send_email_gets_plaintext_password(fresh_env):
    import main
    cid = _seed(fresh_env["db_path"], name="em", type="email",
                settings={"host": "smtp.example.com", "port": 587,
                          "username": "u",
                          "password": "CANARY-smtp-pw-xyz",
                          "from": "a@b", "to": "c@d"})
    main.migrate_encrypt_notification_connection_secrets()
    with sqlite3.connect(fresh_env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        row = dict(c.execute(
            "SELECT * FROM notification_connections WHERE id=?", (cid,)
        ).fetchone())

    calls: list = []

    class _FakeSMTP:
        def __init__(self, host, port, timeout=10):
            calls.append(('ctor', host, port))
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def login(self, user, pw):
            calls.append(('login', user, pw))
        def sendmail(self, *a, **kw):
            calls.append(('sendmail',))

    import smtplib as _smtplib
    with patch.object(_smtplib, "SMTP", _FakeSMTP):
        ok, msg = _run_send(row, "hi")
    assert ok, msg
    logins = [c for c in calls if c[0] == 'login']
    assert logins, f"no SMTP login call: {calls}"
    assert logins[0][1] == "u"
    assert logins[0][2] == "CANARY-smtp-pw-xyz"


def test_send_apprise_gets_plaintext_url_and_config_key(fresh_env):
    import main
    cid = _seed(fresh_env["db_path"], name="ap", type="apprise",
                settings={"url": "https://CANARY-apprise.lan",
                          "config_key": "CANARY-cfgkey"})
    main.migrate_encrypt_notification_connection_secrets()
    with sqlite3.connect(fresh_env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        row = dict(c.execute(
            "SELECT * FROM notification_connections WHERE id=?", (cid,)
        ).fetchone())

    observed = {}

    class _Resp:
        status_code = 200

    class _AsyncCli:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, url, **kw):
            observed['url'] = url
            return _Resp()

    with patch("routers.notification_connections.httpx.AsyncClient", _AsyncCli):
        ok, msg = _run_send(row, "hi")
    assert ok, msg
    # Apprise target is f"{url}/notify/{key}"; both pieces must be plaintext
    assert observed['url'] == "https://CANARY-apprise.lan/notify/CANARY-cfgkey"


# ───────────────────── create/update form encryption ─────────────────────

def test_create_form_encrypts_discord_webhook(fresh_env):
    import main
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    client.get("/system/status", follow_redirects=False)
    token = client.cookies.get("csrftoken")
    r = client.post("/notifications", data={
        "csrf_token": token,
        "name": "form-disc",
        "type": "discord",
        "enabled": "1",
        "settings": json.dumps({"webhook_url": "https://FORM-CANARY-disc",
                                "mention": "@here"}),
        "on_grab": "1",
    }, follow_redirects=False)
    assert r.status_code == 303, r.text
    with sqlite3.connect(fresh_env["db_path"]) as c:
        row = c.execute(
            "SELECT settings FROM notification_connections WHERE name='form-disc'"
        ).fetchone()
    blob = json.loads(row[0])
    assert blob["webhook_url"].startswith("enc:v1:")
    assert blob["mention"] == "@here"


def test_create_form_accepts_structured_fields_without_json(fresh_env):
    import main
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    client.get("/system/status", follow_redirects=False)
    token = client.cookies.get("csrftoken")
    r = client.post("/notifications", data={
        "csrf_token": token,
        "name": "structured-disc",
        "type": "discord",
        "enabled": "1",
        "settings_mode": "structured",
        "webhook_url": "https://STRUCTURED-CANARY-disc",
        "on_grab": "1",
    }, follow_redirects=False)
    assert r.status_code == 303, r.text
    with sqlite3.connect(fresh_env["db_path"]) as c:
        row = c.execute(
            "SELECT settings FROM notification_connections WHERE name='structured-disc'"
        ).fetchone()
    blob = json.loads(row[0])
    assert blob["webhook_url"].startswith("enc:v1:")


def test_edit_form_encrypts_new_telegram_bot_token(fresh_env):
    import main
    cid = _seed(fresh_env["db_path"], name="tg-edit", type="telegram",
                settings={"bot_token": "OLD", "chat_id": "1"})
    main.migrate_encrypt_notification_connection_secrets()
    old_blob = _blob(fresh_env["db_path"], cid)
    assert old_blob["bot_token"].startswith("enc:v1:")

    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    client.get("/system/status", follow_redirects=False)
    token = client.cookies.get("csrftoken")
    r = client.post(f"/notifications/{cid}", data={
        "csrf_token": token,
        "name": "tg-edit",
        "type": "telegram",
        "enabled": "1",
        "settings": json.dumps({"bot_token": "ROTATED-CANARY-tg",
                                "chat_id": "999"}),
    }, follow_redirects=False)
    assert r.status_code == 303
    new_blob = _blob(fresh_env["db_path"], cid)
    assert new_blob["bot_token"].startswith("enc:v1:")
    assert new_blob["bot_token"] != old_blob["bot_token"]
    assert new_blob["chat_id"] == "999"


def test_edit_structured_form_preserves_unknown_keys_for_same_type(fresh_env):
    import main
    cid = _seed(
        fresh_env["db_path"],
        name="discord-extra",
        type="discord",
        settings={"webhook_url": "https://OLD.example/hook", "custom_label": "keep-me"},
    )
    main.migrate_encrypt_notification_connection_secrets()
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    client.get("/system/status", follow_redirects=False)
    token = client.cookies.get("csrftoken")
    r = client.post(f"/notifications/{cid}", data={
        "csrf_token": token,
        "name": "discord-extra",
        "type": "discord",
        "enabled": "1",
        "settings_mode": "structured",
        "original_type": "discord",
        "settings_base": json.dumps({"webhook_url": "https://OLD.example/hook", "custom_label": "keep-me"}),
        "webhook_url": "https://NEW.example/hook",
    }, follow_redirects=False)
    assert r.status_code == 303, r.text
    blob = _blob(fresh_env["db_path"], cid)
    assert blob["webhook_url"].startswith("enc:v1:")
    assert blob["custom_label"] == "keep-me"


def test_edit_form_passes_through_existing_encrypted_value(fresh_env):
    """If the operator edits without touching the secret — the textarea
    still contains the enc:v1:... value — the row shouldn't get double-
    wrapped (encrypt_if_cipher_available is idempotent)."""
    import main
    cid = _seed(fresh_env["db_path"], name="em-idem", type="email",
                settings={"host": "smtp.x", "port": 25, "password": "OLD-PW",
                          "to": "a@b"})
    main.migrate_encrypt_notification_connection_secrets()
    before = _blob(fresh_env["db_path"], cid)
    enc_pw_before = before["password"]
    assert enc_pw_before.startswith("enc:v1:")

    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    client.get("/system/status", follow_redirects=False)
    token = client.cookies.get("csrftoken")
    # Submit the existing (enc:v1:) value unchanged — simulates operator
    # editing a non-secret field without touching the textarea's secret.
    r = client.post(f"/notifications/{cid}", data={
        "csrf_token": token,
        "name": "em-idem",
        "type": "email",
        "enabled": "1",
        "settings": json.dumps(before),
    }, follow_redirects=False)
    assert r.status_code == 303
    after = _blob(fresh_env["db_path"], cid)
    assert after["password"] == enc_pw_before, "idempotency violated — double-wrap"


def test_create_form_email_encrypts_password_only(fresh_env):
    import main
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    client.get("/system/status", follow_redirects=False)
    token = client.cookies.get("csrftoken")
    r = client.post("/notifications", data={
        "csrf_token": token,
        "name": "form-email",
        "type": "email",
        "enabled": "1",
        "settings": json.dumps({
            "host": "smtp.x", "port": 587, "username": "u",
            "password": "FORM-EMAIL-CANARY",
            "from": "f@x", "to": "t@x",
        }),
    }, follow_redirects=False)
    assert r.status_code == 303
    with sqlite3.connect(fresh_env["db_path"]) as c:
        row = c.execute(
            "SELECT settings FROM notification_connections WHERE name='form-email'"
        ).fetchone()
    b = json.loads(row[0])
    assert b["password"].startswith("enc:v1:")
    # Non-secrets stay plaintext
    assert b["host"] == "smtp.x"
    assert b["port"] == 587
    assert b["username"] == "u"
    assert b["from"] == "f@x"
    assert b["to"] == "t@x"


def test_create_form_apprise_encrypts_url_and_config_key(fresh_env):
    import main
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    client.get("/system/status", follow_redirects=False)
    token = client.cookies.get("csrftoken")
    r = client.post("/notifications", data={
        "csrf_token": token,
        "name": "form-apprise",
        "type": "apprise",
        "enabled": "1",
        "settings": json.dumps({
            "url": "https://FORM-APPRISE-CANARY",
            "config_key": "FORM-APPRISE-CFG-CANARY",
            "tags": "manga",
        }),
    }, follow_redirects=False)
    assert r.status_code == 303
    with sqlite3.connect(fresh_env["db_path"]) as c:
        row = c.execute(
            "SELECT settings FROM notification_connections WHERE name='form-apprise'"
        ).fetchone()
    b = json.loads(row[0])
    assert b["url"].startswith("enc:v1:")
    assert b["config_key"].startswith("enc:v1:")
    assert b["tags"] == "manga"


def test_notifications_page_renders_plaintext_for_edit_not_ciphertext(fresh_env):
    import main
    cid = _seed(
        fresh_env["db_path"],
        name="page-disc",
        type="discord",
        settings={"webhook_url": "https://PAGE-CANARY.example/hook", "mention": "@here"},
    )
    main.migrate_encrypt_notification_connection_secrets()
    stored = _blob(fresh_env["db_path"], cid)["webhook_url"]
    assert stored.startswith("enc:v1:")

    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    r = client.get("/notifications")
    assert r.status_code == 200
    assert "https://PAGE-CANARY.example/hook" in r.text
    assert stored not in r.text


def test_notification_test_form_uses_plaintext_secret_fields(fresh_env):
    import main
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    client.get("/system/status", follow_redirects=False)

    observed = {}

    class _Resp:
        status_code = 204
        text = ""

    class _AsyncCli:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, url, **kw):
            observed["url"] = url
            return _Resp()

    with patch("routers.notification_connections.httpx.AsyncClient", _AsyncCli), \
         patch("routers.notification_connections.validate_outbound_url", lambda *a, **kw: None):
        r = client.post("/api/notifications/test-form", data={
            "name": "unsaved-discord",
            "type": "discord",
            "settings": json.dumps({"webhook_url": "https://FORM-NOTIFY.example/hook"}),
        }, headers=_csrf_header(client))
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert observed["url"] == "https://FORM-NOTIFY.example/hook"


def test_notification_test_form_accepts_structured_fields(fresh_env):
    import main
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    client.get("/system/status", follow_redirects=False)

    observed = {}

    class _Resp:
        status_code = 204
        text = ""

    class _AsyncCli:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, url, **kw):
            observed["url"] = url
            return _Resp()

    with patch("routers.notification_connections.httpx.AsyncClient", _AsyncCli), \
         patch("routers.notification_connections.validate_outbound_url", lambda *a, **kw: None):
        r = client.post("/api/notifications/test-form", data={
            "name": "structured-discord",
            "type": "discord",
            "settings_mode": "structured",
            "webhook_url": "https://STRUCTURED-NOTIFY.example/hook",
        }, headers=_csrf_header(client))
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert observed["url"] == "https://STRUCTURED-NOTIFY.example/hook"


# ───────────────────── wrong-key: fanout continues ─────────────────────

def test_wrong_key_disables_only_that_connection_in_fanout(fresh_env, monkeypatch):
    """Two Discord connections, one whose ciphertext can't be decrypted
    with the active cipher. Fire a fanout — the good one sends; the bad
    one fails cleanly; the send returns (ok=False, msg=<reason>) for the
    bad one but the gather doesn't abort."""
    import asyncio
    import main, security
    from routers.notification_connections import fire_notifications
    canary_bad = "https://CANARY-BAD-MUST-NOT-LEAK/x"
    cid_bad = _seed(fresh_env["db_path"], name="bad-disc", type="discord",
                    settings={"webhook_url": canary_bad})
    cid_good = _seed(fresh_env["db_path"], name="good-disc", type="discord",
                     settings={"webhook_url": "https://GOOD-WEBHOOK/x"})
    main.migrate_encrypt_notification_connection_secrets()
    # Rotate cipher — now bad-disc's ciphertext can't be decrypted with
    # the new key, but we re-encrypt good-disc after the rotation so it
    # IS readable.
    from cryptography.fernet import Fernet
    enc_bad_before = _blob(fresh_env["db_path"], cid_bad)["webhook_url"]
    monkeypatch.setattr(security, "_SECRET_CIPHER", Fernet(Fernet.generate_key()))
    # Re-encrypt good-disc with the new cipher by running the migration
    # on a reset plaintext value.
    with sqlite3.connect(fresh_env["db_path"]) as c:
        c.execute(
            "UPDATE notification_connections SET settings=? WHERE id=?",
            (json.dumps({"webhook_url": "https://GOOD-WEBHOOK/x"}), cid_good),
        )
        c.commit()
    main.migrate_encrypt_notification_connection_secrets()
    assert _blob(fresh_env["db_path"], cid_good)["webhook_url"].startswith("enc:v1:")

    posted: list = []

    class _Resp:
        status_code = 204
        text = ""

    class _AsyncCli:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, url, **kw):
            posted.append(url)
            return _Resp()

    with patch("routers.notification_connections.httpx.AsyncClient", _AsyncCli), \
         patch("routers.notification_connections.validate_outbound_url",
               lambda *a, **kw: None):
        asyncio.run(fire_notifications("on_grab", "hello"))

    # The good connection posted; the bad one was silently disabled
    # (empty webhook_url → sender returns "No webhook URL"). No post
    # went to the bad canary URL.
    assert "https://GOOD-WEBHOOK/x" in posted
    assert canary_bad not in posted
    assert enc_bad_before not in posted


def test_settings_remains_valid_json_after_migration(fresh_env):
    import main
    cid = _seed(fresh_env["db_path"], name="val", type="pushover",
                settings={"user_key": "CANARY-u", "api_token": "CANARY-t",
                          "priority": 1, "meta": {"x": [1, 2, 3]}})
    main.migrate_encrypt_notification_connection_secrets()
    raw = _settings(fresh_env["db_path"], cid)
    # Must parse cleanly and preserve structure
    b = json.loads(raw)
    assert b["user_key"].startswith("enc:v1:")
    assert b["api_token"].startswith("enc:v1:")
    assert b["priority"] == 1
    assert b["meta"] == {"x": [1, 2, 3]}


# ───────────────────── no-leak guard ─────────────────────

def test_no_secret_or_ciphertext_in_logs(fresh_env):
    import main, security
    buf = io.StringIO()
    h = logging.StreamHandler(buf); h.setLevel(logging.DEBUG)
    for n in ("main", "security", "shared", "routers.notification_connections"):
        logging.getLogger(n).addHandler(h)
        logging.getLogger(n).setLevel(logging.DEBUG)

    canaries = {
        "discord":    "https://CANARY-disc/WH-ZZ",
        "telegram":   "CANARY-tg-bot-XX:YYY",
        "gotify":     "CANARY-gotify-AA",
        "email_pw":   "CANARY-email-pw-BB",
        "apprise_k":  "CANARY-apprise-cfg-CC",
    }
    _seed(fresh_env["db_path"], name="d", type="discord",
          settings={"webhook_url": canaries["discord"]})
    _seed(fresh_env["db_path"], name="t", type="telegram",
          settings={"bot_token": canaries["telegram"], "chat_id": "1"})
    _seed(fresh_env["db_path"], name="g", type="gotify",
          settings={"server": "http://g.x", "app_token": canaries["gotify"]})
    _seed(fresh_env["db_path"], name="e", type="email",
          settings={"host": "smtp.x", "port": 587, "password": canaries["email_pw"],
                    "to": "a@b"})
    cid_ap = _seed(fresh_env["db_path"], name="a", type="apprise",
                   settings={"url": "https://aprs.x", "config_key": canaries["apprise_k"]})
    main.migrate_encrypt_notification_connection_secrets()

    # Capture ciphertexts for the leak check
    enc_values: list[str] = []
    with sqlite3.connect(fresh_env["db_path"]) as c:
        for r in c.execute("SELECT settings FROM notification_connections").fetchall():
            d = json.loads(r[0])
            for v in d.values():
                if isinstance(v, str) and v.startswith("enc:v1:"):
                    enc_values.append(v)

    # Trigger the wrong-key path too, to exercise the WARNING log
    from cryptography.fernet import Fernet
    security._SECRET_CIPHER = Fernet(Fernet.generate_key())
    with sqlite3.connect(fresh_env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        apr = dict(c.execute(
            "SELECT * FROM notification_connections WHERE id=?", (cid_ap,)
        ).fetchone())
    from routers.notification_connections import send_connection
    import asyncio as _asyncio

    class _AsyncCli:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, url, **kw):
            class _R:
                status_code = 200
            return _R()

    with patch("routers.notification_connections.httpx.AsyncClient", _AsyncCli):
        _asyncio.run(send_connection(apr, "x"))

    text = buf.getvalue()
    for label, v in canaries.items():
        assert v not in text, f"canary {label}={v!r} leaked into logs"
    for enc in enc_values:
        assert enc not in text, "enc:v1: ciphertext leaked into logs"
