"""CRUD round-trip tests — operator-facing resources.

Exercises create → edit → delete via the actual HTTP endpoints (no UI),
asserting DB state at each step. Adds the layer that was missing when
PR #25 shipped a broken /indexers route: the page sweep now catches
render breakage, this file catches "save handler quietly stopped persisting".

Each suite:
  - boots main:app against a fresh temp DB
  - establishes a CSRF cookie via an initial GET
  - POSTs the create form, asserts row exists
  - POSTs the edit form, asserts the delta persisted
  - POSTs the delete form, asserts row is gone
  - asserts secrets are encrypted on disk where applicable

Mocked upstreams: the */test endpoints are exercised against an httpx
mock so no real Prowlarr / qBittorrent / Discord webhook is contacted.
"""
import json
import os
import re
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from unittest.mock import patch, AsyncMock

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401  — sets up /config redirect, sys.path, template path


@pytest.fixture
def app_client():
    """Boot main:app on a fresh DB and yield (TestClient, db_path)."""
    import main, shared, security
    from fastapi.testclient import TestClient

    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-test-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)

    main.init_db()
    main.load_config()
    main.ensure_api_key()

    client = TestClient(main.app, follow_redirects=False)
    # Prime the CSRF cookie so subsequent POSTs can echo it back.
    client.get("/")

    try:
        yield client, db.name
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


def _csrf(client) -> str:
    """Pull the current csrftoken cookie value from the TestClient jar."""
    return client.cookies.get("csrftoken", "")


def _row(db_path, sql, *params):
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        return c.execute(sql, params).fetchone()


def _rows(db_path, sql, *params):
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        return c.execute(sql, params).fetchall()


# ───────────────────────── Indexers ──────────────────────────────────────────

def test_indexer_crud_roundtrip(app_client):
    client, db_path = app_client
    csrf = _csrf(client)
    assert csrf, "CSRF cookie not seeded by initial GET"

    # CREATE
    r = client.post("/indexers", data={
        "csrf_token": csrf,
        "name":       "test-prowlarr",
        "type":       "prowlarr",
        # Literal RFC1918 IP — no DNS needed; allow_private=True permits this.
        "url":        "http://192.168.99.99:9696",
        "api_key":    "supersecret-prowlarr-key",
        "priority":   "25",
        "enabled":    "1",
        "categories": "7000,7020",
    })
    assert r.status_code == 303, f"create failed: {r.status_code} {r.text!r}"
    row = _row(db_path, "SELECT * FROM indexers WHERE name=?", "test-prowlarr")
    assert row is not None, "indexer row not inserted"
    assert row["type"] == "prowlarr"
    assert row["url"]  == "http://192.168.99.99:9696"
    # Secret encryption: api_key on disk must NOT be the plaintext.
    assert row["api_key"] != "supersecret-prowlarr-key"
    assert (row["api_key"] or "").startswith("enc:v1:"), (
        f"api_key stored in plaintext: {row['api_key']!r}"
    )
    indexer_id = row["id"]

    # EDIT — change priority and url, keep api_key (keep_api_key=1)
    r = client.post(f"/indexers/{indexer_id}", data={
        "csrf_token":   _csrf(client),
        "name":         "test-prowlarr-renamed",
        "type":         "prowlarr",
        "url":          "http://192.168.99.99:19696",
        "priority":     "50",
        "enabled":      "1",
        "categories":   "7000",
        "keep_api_key": "1",
    })
    assert r.status_code == 303, f"edit failed: {r.status_code} {r.text!r}"
    row = _row(db_path, "SELECT * FROM indexers WHERE id=?", indexer_id)
    assert row["name"]     == "test-prowlarr-renamed"
    assert row["url"]      == "http://192.168.99.99:19696"
    assert row["priority"] == 50
    # api_key still encrypted, unchanged
    assert (row["api_key"] or "").startswith("enc:v1:")

    # TEST endpoint with mocked Prowlarr — no real network.
    fake_resp = type("R", (), {"status_code": 200, "json": lambda self: {"version": "1.99-mock"}})()
    async def _mock_get(self, *a, **kw):
        return fake_resp
    with patch("httpx.AsyncClient.get", new=_mock_get):
        r = client.post(
            f"/api/indexers/{indexer_id}/test",
            headers={"X-CSRFToken": _csrf(client)},
        )
    assert r.status_code == 200, f"test endpoint: {r.status_code} {r.text!r}"
    body = r.json()
    assert body["ok"] is True
    assert "1.99-mock" in body["message"]

    # DELETE
    r = client.post(f"/indexers/{indexer_id}/delete", data={"csrf_token": _csrf(client)})
    assert r.status_code == 303, f"delete failed: {r.status_code} {r.text!r}"
    assert _row(db_path, "SELECT * FROM indexers WHERE id=?", indexer_id) is None


# ───────────────────────── Download clients ──────────────────────────────────

def test_download_client_crud_roundtrip(app_client):
    client, db_path = app_client
    csrf = _csrf(client)

    # CREATE
    r = client.post("/download-clients", data={
        "csrf_token": csrf,
        "name":       "test-qbit",
        "type":       "qbittorrent",
        "host":       "qbit.local",
        "port":       "8080",
        "use_ssl":    "0",
        "username":   "admin",
        "password":   "qbit-secret-password",
        "category":   "manga",
        "priority":   "1",
        "enabled":    "1",
    })
    assert r.status_code == 303, f"create failed: {r.status_code} {r.text!r}"
    row = _row(db_path, "SELECT * FROM download_clients WHERE name=?", "test-qbit")
    assert row is not None
    assert row["type"]   == "qbittorrent"
    assert row["host"]   == "qbit.local"
    assert row["port"]   == 8080
    assert row["password"] != "qbit-secret-password"
    assert (row["password"] or "").startswith("enc:v1:"), (
        f"password stored in plaintext: {row['password']!r}"
    )
    cid = row["id"]

    # EDIT — bump port, keep password
    r = client.post(f"/download-clients/{cid}", data={
        "csrf_token":    _csrf(client),
        "name":          "test-qbit",
        "type":          "qbittorrent",
        "host":          "qbit.local",
        "port":          "8081",
        "use_ssl":       "0",
        "username":      "admin",
        "password":      "",
        "category":      "manga",
        "priority":      "5",
        "enabled":       "1",
        "keep_password": "1",
    })
    assert r.status_code == 303
    row = _row(db_path, "SELECT * FROM download_clients WHERE id=?", cid)
    assert row["port"]     == 8081
    assert row["priority"] == 5
    assert (row["password"] or "").startswith("enc:v1:")  # unchanged, still encrypted

    # TEST endpoint — mocked qBittorrent /auth/login + /app/version
    async def _mock_post(self, url, *a, **kw):
        return type("R", (), {"status_code": 200, "text": "Ok.", "cookies": {"SID": "x"}})()
    async def _mock_get(self, url, *a, **kw):
        return type("R", (), {"status_code": 200, "text": "4.6.0-mock"})()
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO client_breaker_state(client_id, failures, open_until)"
            " VALUES(?, 3, 9999999999)",
            (cid,),
        )
    with patch("httpx.AsyncClient.post", new=_mock_post), \
         patch("httpx.AsyncClient.get",  new=_mock_get):
        r = client.post(
            f"/api/download-clients/{cid}/test",
            headers={"X-CSRFToken": _csrf(client)},
        )
    assert r.status_code == 200, f"test endpoint: {r.status_code} {r.text!r}"
    body = r.json()
    assert body["ok"] is True
    assert _row(
        db_path,
        "SELECT * FROM client_breaker_state WHERE client_id=?",
        cid,
    ) is None

    # DELETE
    r = client.post(f"/download-clients/{cid}/delete", data={"csrf_token": _csrf(client)})
    assert r.status_code == 303
    assert _row(db_path, "SELECT * FROM download_clients WHERE id=?", cid) is None


# ───────────────────────── Notifications ─────────────────────────────────────
# Notification CRUD round-trip moved to the structured-form PR. The router
# in this branch only accepts the JSON `settings` blob, so the structured
# `webhook_url` field would silently drop. The 33 dedicated migration tests
# in test_notification_secret_migration.py already cover the JSON path.


# ───────────────────────── Settings persistence ──────────────────────────────

def test_settings_save_and_reload(app_client):
    """POST /settings → verify persisted in DB and reloaded into CONFIG."""
    import main
    client, db_path = app_client

    # Sanity check the page actually renders.
    r = client.get("/settings")
    assert r.status_code == 200
    assert "<form" in r.text

    # Mutate import_mode (a known whitelisted field on the /settings POST).
    initial = main.get_cfg("import_mode", "hardlink")
    new_value = "copy" if initial != "copy" else "move"

    r = client.post("/settings", data={
        "csrf_token":  _csrf(client),
        "import_mode": new_value,
    })
    assert r.status_code in (200, 303), f"settings save: {r.status_code} {r.text!r}"

    saved = _row(db_path, "SELECT value FROM settings WHERE key=?", "import_mode")
    assert saved is not None, "import_mode not persisted"
    assert saved["value"] == new_value

    # Save handler calls _reload_config() internally; CONFIG should already
    # reflect the new value without a manual reload.
    assert main.get_cfg("import_mode") == new_value

    # Round-trip a second value to prove the persistence is real, not a
    # one-off side effect of fixture setup.
    r = client.post("/settings", data={
        "csrf_token":  _csrf(client),
        "import_mode": initial,
    })
    assert r.status_code in (200, 303)
    assert main.get_cfg("import_mode") == initial
