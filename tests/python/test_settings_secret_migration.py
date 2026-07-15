"""Tests for H4 PR #2: settings table secret encryption.

Covers:
  - migrate_encrypt_settings_secrets() encrypts plaintext, skips
    encrypted, skips empty, is idempotent
  - load_config() returns decrypted plaintext in CONFIG for
    SETTINGS_SECRET_KEYS
  - non-secret settings are left plaintext on disk
  - api_key middleware still authenticates with plaintext request key
    when the DB value is encrypted
  - settings form save encrypts on disk while CONFIG remains plaintext
  - wrong-key / malformed-token decrypt is treated as
    "credential unavailable" with a safe WARNING log
  - canary plaintext never appears in any captured log line
  - cipher unavailable → migration is a no-op WARNING; existing
    plaintext stays plaintext (no behaviour change for that boot)
"""
import logging
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401


@pytest.fixture
def fresh_env(monkeypatch, tmp_path):
    """Fresh DB + fresh secret-key directory + reset cipher cache."""
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


def _seed_plaintext(db_path, **kv):
    with sqlite3.connect(db_path) as c:
        for k, v in kv.items():
            c.execute(
                "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, v),
            )
        c.commit()


def _row(db_path, key):
    with sqlite3.connect(db_path) as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else None


# ───────────────────── migration ─────────────────────

def test_migration_encrypts_plaintext_secrets(fresh_env):
    import main
    _seed_plaintext(
        fresh_env["db_path"],
        komga_pass="CANARY-pw",
        komga_user="CANARY-user",
        google_books_api_key="CANARY-gb-key",
        save_path="/manga",  # non-secret control
    )
    n = main.migrate_encrypt_settings_secrets()
    # 3 from the seed + the api_key auto-seeded by init_db = 4
    assert n == 4
    for k in ("komga_pass", "komga_user", "google_books_api_key", "api_key"):
        assert _row(fresh_env["db_path"], k).startswith("enc:v1:"), \
            f"{k} should be encrypted on disk"
    # Non-secret stayed plaintext
    assert _row(fresh_env["db_path"], "save_path") == "/manga"


def test_migration_skips_already_encrypted(fresh_env):
    import main
    from security import encrypt_secret
    pre_encrypted = encrypt_secret("CANARY-pw")
    _seed_plaintext(fresh_env["db_path"], komga_pass=pre_encrypted)
    # First migration encrypts the api_key only (komga_pass already enc)
    n1 = main.migrate_encrypt_settings_secrets()
    # api_key auto-seeded → 1
    assert n1 == 1
    assert _row(fresh_env["db_path"], "komga_pass") == pre_encrypted, \
        "pre-encrypted value should not be re-encrypted"


def test_migration_skips_empty_values(fresh_env):
    import main
    _seed_plaintext(fresh_env["db_path"], komga_pass="", google_books_api_key="")
    n = main.migrate_encrypt_settings_secrets()
    # api_key auto-seeded → 1
    assert n == 1
    # Empty values stay empty (NOT encrypted to "enc:v1:<empty>")
    assert _row(fresh_env["db_path"], "komga_pass") == ""
    assert _row(fresh_env["db_path"], "google_books_api_key") == ""


def test_migration_is_idempotent(fresh_env):
    import main
    _seed_plaintext(
        fresh_env["db_path"], komga_pass="CANARY-pw", google_books_api_key="CANARY-gb",
    )
    n1 = main.migrate_encrypt_settings_secrets()
    n2 = main.migrate_encrypt_settings_secrets()
    n3 = main.migrate_encrypt_settings_secrets()
    assert n1 > 0
    assert n2 == 0
    assert n3 == 0


def test_migration_noop_when_cipher_unavailable(fresh_env, monkeypatch, caplog):
    import main, security
    monkeypatch.setattr(security, "_SECRET_CIPHER", None)
    _seed_plaintext(fresh_env["db_path"], komga_pass="CANARY-pw")
    with caplog.at_level(logging.WARNING, logger="main"):
        n = main.migrate_encrypt_settings_secrets()
    assert n == 0
    # Plaintext stays plaintext
    assert _row(fresh_env["db_path"], "komga_pass") == "CANARY-pw"
    # Warning emitted naming the skip
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "skipped" in joined.lower() and "cipher unavailable" in joined.lower()


# ───────────────────── load_config decrypts ─────────────────────

def test_load_config_decrypts_secret_keys(fresh_env):
    import main
    _seed_plaintext(
        fresh_env["db_path"],
        komga_pass="CANARY-pw", komga_user="CANARY-user",
        google_books_api_key="CANARY-gb",
    )
    main.migrate_encrypt_settings_secrets()
    # After migration, on-disk values are encrypted
    assert _row(fresh_env["db_path"], "komga_pass").startswith("enc:v1:")
    main.load_config()
    # CONFIG holds plaintext
    assert main.get_cfg("komga_pass") == "CANARY-pw"
    assert main.get_cfg("komga_user") == "CANARY-user"
    assert main.get_cfg("google_books_api_key") == "CANARY-gb"


def test_load_config_passes_through_plaintext_unchanged(fresh_env):
    """Pre-migration plaintext values must still be readable (back-compat)."""
    import main
    _seed_plaintext(fresh_env["db_path"], komga_pass="legacy-plain-pw")
    # Don't run migration — value stays plaintext
    main.load_config()
    assert main.get_cfg("komga_pass") == "legacy-plain-pw"


# ───────────────────── api_key middleware integration ─────────────────────

def test_api_key_middleware_accepts_request_key_when_db_encrypted(fresh_env):
    """End-to-end: encrypted api_key in DB → middleware decrypts via
    load_config → request with the plaintext value authenticates."""
    import main
    main.migrate_encrypt_settings_secrets()
    main.load_config()
    plain_api_key = main.get_cfg("api_key")
    assert plain_api_key, "api_key should be populated post-migration"
    # Verify on-disk is encrypted, in-memory is plaintext
    assert _row(fresh_env["db_path"], "api_key").startswith("enc:v1:")
    # Hit a /api/ route (api_key middleware only guards /api/* paths)
    from fastapi.testclient import TestClient
    from fastapi.responses import PlainTextResponse
    @main.app.get("/api/__h4b_probe")
    async def _probe(): return PlainTextResponse("ok")
    client = TestClient(main.app)
    r = client.get("/api/__h4b_probe", headers={"X-Api-Key": plain_api_key})
    assert r.status_code == 200
    # Wrong key → 401
    r2 = client.get("/api/__h4b_probe", headers={"X-Api-Key": "wrong"})
    assert r2.status_code == 401


# ───────────────────── settings form save encrypts ─────────────────────

def test_settings_form_save_encrypts_secret(fresh_env):
    """POST /settings with komga_pass=... should encrypt the value
    on disk while CONFIG holds plaintext for use by Komga callers."""
    import main
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    # Get a CSRF cookie
    r0 = client.get("/system/status", follow_redirects=False)
    token = client.cookies.get("csrftoken")
    assert token

    r = client.post(
        "/settings",
        data={
            "csrf_token": token,
            "save_path": "/manga",
            "category": "manga",
            "komga_url": "http://komga.lan:25600",
            "komga_user": "form-user",
            "komga_pass": "FORM-CANARY-PW",
            "google_books_api_key": "FORM-CANARY-GB",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    # On disk: encrypted
    assert _row(fresh_env["db_path"], "komga_pass").startswith("enc:v1:")
    assert _row(fresh_env["db_path"], "komga_user").startswith("enc:v1:")
    assert _row(fresh_env["db_path"], "google_books_api_key").startswith("enc:v1:")
    # Non-secret settings stayed plaintext.
    # (save_path is no longer exposed via the /settings form after PR D;
    # library destination now flows through root folders. The form
    # silently drops the field — test that category/komga_url land.)
    assert _row(fresh_env["db_path"], "category") == "manga"
    assert _row(fresh_env["db_path"], "komga_url") == "http://komga.lan:25600"
    # In-memory: plaintext usable by Komga callers
    assert main.get_cfg("komga_pass") == "FORM-CANARY-PW"
    assert main.get_cfg("komga_user") == "form-user"
    assert main.get_cfg("google_books_api_key") == "FORM-CANARY-GB"


def test_settings_general_form_encrypts_api_key(fresh_env):
    """POST /settings/general with api_key=... must encrypt."""
    import main
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    r0 = client.get("/system/status", follow_redirects=False)
    token = client.cookies.get("csrftoken")

    NEW_KEY = "x" * 64  # a plausible 64-char API key
    r = client.post(
        "/settings/general",
        data={
            "csrf_token": token,
            "instance_name": "test",
            "log_level": "INFO",
            "backup_folder": "/config/backups/",
            "backup_interval_days": "7",
            "backup_retention": "10",
            "ui_date_format": "relative",
            "blocklist_ttl_days": "90",
            "api_key": NEW_KEY,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert _row(fresh_env["db_path"], "api_key").startswith("enc:v1:")
    assert main.get_cfg("api_key") == NEW_KEY


def test_settings_general_page_decrypts_api_key(fresh_env):
    """The settings page must never render the encrypted DB wire value."""
    import main
    from fastapi.testclient import TestClient

    plain_api_key = "VISIBLE-PLAINTEXT-API-KEY"
    _seed_plaintext(fresh_env["db_path"], api_key=plain_api_key)
    main.migrate_encrypt_settings_secrets()
    stored_api_key = _row(fresh_env["db_path"], "api_key")
    assert stored_api_key.startswith("enc:v1:")

    response = TestClient(main.app).get("/settings/general")

    assert response.status_code == 200
    assert f'value="{plain_api_key}"' in response.text
    assert stored_api_key not in response.text


# ───────────────────── wrong-key safety ─────────────────────

def test_wrong_key_treats_secret_as_unavailable_with_safe_log(
    fresh_env, monkeypatch, caplog,
):
    """Encrypt with key A, swap cipher to key B, load_config →
    that secret becomes empty in CONFIG; WARNING names the field;
    canary plaintext NEVER appears in the log."""
    import main, security
    canary = "CANARY-PW-MUST-NOT-LEAK-zzz"
    _seed_plaintext(fresh_env["db_path"], komga_pass=canary)
    main.migrate_encrypt_settings_secrets()
    # Encrypted on disk
    assert _row(fresh_env["db_path"], "komga_pass").startswith("enc:v1:")

    # Swap in a different cipher
    from cryptography.fernet import Fernet
    monkeypatch.setattr(security, "_SECRET_CIPHER", Fernet(Fernet.generate_key()))

    with caplog.at_level(logging.WARNING, logger="main"):
        main.load_config()

    # Secret is now empty in CONFIG
    assert main.get_cfg("komga_pass") == ""
    # Warning names the field
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "komga_pass" in joined
    assert "could not be decrypted" in joined.lower()
    # Canary plaintext NEVER appears
    assert canary not in joined


def test_settings_general_page_shows_wrong_key_recovery_banner(fresh_env, monkeypatch):
    import main, security
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient

    _seed_plaintext(fresh_env["db_path"], api_key="CANARY-banner-api-key")
    main.migrate_encrypt_settings_secrets()
    stored_api_key = _row(fresh_env["db_path"], "api_key")
    monkeypatch.setattr(security, "_SECRET_CIPHER", Fernet(Fernet.generate_key()))

    client = TestClient(main.app)
    r = client.get("/settings/general")
    assert r.status_code == 200
    assert "Encrypted credentials need recovery" in r.text
    assert "re-enter the affected credentials" in r.text
    assert "/config/.mangarr-secret-key" in r.text
    assert stored_api_key not in r.text


def test_settings_general_page_shows_first_run_backup_callout(fresh_env):
    import main
    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    r = client.get("/settings/general")
    assert r.status_code == 200
    assert "First boot generated an API key" in r.text
    assert "/config/.mangarr-secret-key" in r.text


# ───────────────────── secret-leak guard across full lifecycle ─────────────────────

def test_no_secret_logs_during_full_lifecycle(fresh_env, caplog):
    """Seed canaries, migrate, load_config, save via form, decrypt round
    trip. Captured log stream must contain no canary string and no
    enc:v1:<token> ciphertext."""
    import main, security
    import io
    buf = io.StringIO()
    h = logging.StreamHandler(buf); h.setLevel(logging.DEBUG)
    for n in ("main", "security", "shared"):
        logging.getLogger(n).addHandler(h); logging.getLogger(n).setLevel(logging.DEBUG)

    canaries = {
        "komga_pass":           "CANARY-pw-zZ1",
        "komga_user":           "CANARY-user-zZ2",
        "google_books_api_key": "CANARY-gb-zZ3",
    }
    _seed_plaintext(fresh_env["db_path"], **canaries)
    main.migrate_encrypt_settings_secrets()
    main.load_config()
    # And the encrypted on-disk value too
    enc_value = _row(fresh_env["db_path"], "komga_pass")

    text = buf.getvalue()
    for label, c in canaries.items():
        assert c not in text, f"canary {label}={c!r} leaked into logs"
    assert enc_value not in text, "enc:v1: ciphertext leaked into logs"


# ───────────────────── ensure_api_key behaviour ─────────────────────

def test_ensure_api_key_does_not_overwrite_encrypted_existing(fresh_env):
    """If the api_key is already encrypted in DB, ensure_api_key must
    decrypt it, return the plaintext, and not regenerate."""
    import main
    main.migrate_encrypt_settings_secrets()
    encrypted_disk = _row(fresh_env["db_path"], "api_key")
    assert encrypted_disk.startswith("enc:v1:")

    plain1 = main.ensure_api_key()
    plain2 = main.ensure_api_key()
    # Same plaintext returned both times; on-disk encrypted value unchanged
    assert plain1 == plain2 and plain1
    assert _row(fresh_env["db_path"], "api_key") == encrypted_disk


def test_ensure_api_key_returns_empty_when_existing_undecryptable(
    fresh_env, monkeypatch, caplog,
):
    """If a stored api_key can't be decrypted (wrong key / corruption),
    ensure_api_key must NOT overwrite it (operator may want to recover)
    and must NOT silently regenerate. Returns empty string so the
    middleware fails closed (per H2)."""
    import main, security
    main.migrate_encrypt_settings_secrets()
    enc_before = _row(fresh_env["db_path"], "api_key")
    # Swap cipher
    from cryptography.fernet import Fernet
    monkeypatch.setattr(security, "_SECRET_CIPHER", Fernet(Fernet.generate_key()))

    with caplog.at_level(logging.WARNING, logger="main"):
        result = main.ensure_api_key()

    assert result == ""
    # On-disk value preserved (not overwritten with a new key)
    assert _row(fresh_env["db_path"], "api_key") == enc_before
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "could not be decrypted" in joined.lower()


# ───────────────────── confirmation: untouched tables ─────────────────────

def test_h4b_does_not_touch_other_secret_tables(fresh_env):
    """Sanity: the indexers / download_clients / notification_connections
    schemas exist and contain plaintext columns we explicitly are NOT
    encrypting in this PR. Future PRs handle them."""
    with sqlite3.connect(fresh_env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        # Insert plaintext rows directly to confirm migration doesn't touch them
        c.execute(
            "INSERT INTO indexers(name,type,url,api_key,enabled,categories,settings)"
            " VALUES('test','prowlarr','http://x:9696','PLAINTEXT-API-KEY',1,'[7000]','{}')"
        )
        c.execute(
            "INSERT INTO download_clients(name,type,host,username,password,category,enabled)"
            " VALUES('qbit','qbittorrent','http://x:8080','u','PLAINTEXT-PW','manga',1)"
        )
        c.commit()
    import main
    main.migrate_encrypt_settings_secrets()
    # Verify: those rows are STILL plaintext
    with sqlite3.connect(fresh_env["db_path"]) as c:
        ix = c.execute("SELECT api_key FROM indexers WHERE name='test'").fetchone()[0]
        dc = c.execute("SELECT password FROM download_clients WHERE name='qbit'").fetchone()[0]
    assert ix == "PLAINTEXT-API-KEY", "PR #2 must not touch indexers"
    assert dc == "PLAINTEXT-PW", "PR #2 must not touch download_clients"
