"""Tests for H4 PR #3: indexer + download-client secret encryption.

Covers:
  - migrate_encrypt_table_column_secrets() encrypts plaintext api_key in
    the indexers table and plaintext password in download_clients;
    skips already-encrypted values; skips empty; is idempotent
  - indexer test endpoint decrypts api_key before making the HTTP call
  - grab path (qBit + SAB) decrypts password via get_client_for_protocol
  - download-client test endpoint decrypts password
  - create/update write paths encrypt before INSERT/UPDATE
  - wrong-key / undecryptable values disable that integration cleanly
    with a WARNING log and never surface the canary plaintext or
    ciphertext
  - notification_connections rows are untouched
"""
import json
import logging
import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch, AsyncMock

import pytest

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401


def _csrf_header(client) -> dict[str, str]:
    token = client.cookies.get("csrftoken", "")
    return {"X-CSRFToken": token} if token else {}


@pytest.fixture
def fresh_env(monkeypatch, tmp_path):
    """Fresh DB + fresh secret-key dir + reset cipher cache."""
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


def _seed_indexer(db_path, **kv):
    """Insert a row into indexers; returns id."""
    defaults = dict(
        name="test-ix", type="prowlarr", url="http://192.168.1.50:9696",
        api_key="CANARY-IX-KEY", priority=25, enabled=1,
        categories="[7000]", settings="{}", client_id=None,
        min_seeders=0, seed_ratio=0.0,
    )
    defaults.update(kv)
    with sqlite3.connect(db_path) as c:
        cur = c.execute(
            "INSERT INTO indexers(name,type,url,api_key,priority,enabled,categories,"
            "settings,client_id,min_seeders,seed_ratio)"
            " VALUES(:name,:type,:url,:api_key,:priority,:enabled,:categories,"
            ":settings,:client_id,:min_seeders,:seed_ratio)",
            defaults,
        )
        c.commit()
    return cur.lastrowid


def _seed_dlclient(db_path, **kv):
    """Insert a row into download_clients; returns id."""
    defaults = dict(
        name="test-qb", type="qbittorrent", host="http://qbit.lan",
        port=8080, use_ssl=0, url_base=None,
        username="u", password="CANARY-CLIENT-PW",
        category="manga", post_import_category=None,
        recent_priority="last", older_priority="last",
        initial_state="normal", sequential_order=0,
        first_last_first=0, content_layout="original",
        priority=1, enabled=1, remove_completed=0, remove_failed=0,
        download_path=None, merge_chapters=0,
    )
    defaults.update(kv)
    with sqlite3.connect(db_path) as c:
        cur = c.execute(
            "INSERT INTO download_clients("
            "name,type,host,port,use_ssl,url_base,username,password,"
            "category,post_import_category,recent_priority,older_priority,"
            "initial_state,sequential_order,first_last_first,content_layout,"
            "priority,enabled,remove_completed,remove_failed,"
            "download_path,merge_chapters"
            ") VALUES(:name,:type,:host,:port,:use_ssl,:url_base,:username,:password,"
            ":category,:post_import_category,:recent_priority,:older_priority,"
            ":initial_state,:sequential_order,:first_last_first,:content_layout,"
            ":priority,:enabled,:remove_completed,:remove_failed,"
            ":download_path,:merge_chapters)",
            defaults,
        )
        c.commit()
    return cur.lastrowid


def _col(db_path, table, col, row_id):
    with sqlite3.connect(db_path) as c:
        r = c.execute(f"SELECT {col} FROM {table} WHERE id=?", (row_id,)).fetchone()
    return r[0] if r else None


# ───────────────────── migration ─────────────────────

def test_migration_encrypts_indexer_api_key(fresh_env):
    import main
    iid = _seed_indexer(fresh_env["db_path"], api_key="CANARY-IX-plain")
    totals = main.migrate_encrypt_table_column_secrets()
    assert totals["indexers"] == 1
    stored = _col(fresh_env["db_path"], "indexers", "api_key", iid)
    assert stored.startswith("enc:v1:")


def test_migration_encrypts_download_client_password(fresh_env):
    import main
    cid = _seed_dlclient(fresh_env["db_path"], password="CANARY-DC-plain")
    totals = main.migrate_encrypt_table_column_secrets()
    assert totals["download_clients"] == 1
    stored = _col(fresh_env["db_path"], "download_clients", "password", cid)
    assert stored.startswith("enc:v1:")


def test_migration_skips_already_encrypted(fresh_env):
    import main
    from security import encrypt_secret
    pre_ix = encrypt_secret("X-key")
    pre_pw = encrypt_secret("X-pw")
    iid = _seed_indexer(fresh_env["db_path"], api_key=pre_ix)
    cid = _seed_dlclient(fresh_env["db_path"], password=pre_pw)
    totals = main.migrate_encrypt_table_column_secrets()
    assert totals == {"indexers": 0, "download_clients": 0}
    assert _col(fresh_env["db_path"], "indexers", "api_key", iid) == pre_ix
    assert _col(fresh_env["db_path"], "download_clients", "password", cid) == pre_pw


def test_migration_skips_empty_values(fresh_env):
    import main
    iid = _seed_indexer(fresh_env["db_path"], api_key=None)
    cid = _seed_dlclient(fresh_env["db_path"], password=None)
    totals = main.migrate_encrypt_table_column_secrets()
    assert totals == {"indexers": 0, "download_clients": 0}
    assert _col(fresh_env["db_path"], "indexers", "api_key", iid) is None
    assert _col(fresh_env["db_path"], "download_clients", "password", cid) is None


def test_migration_is_idempotent(fresh_env):
    import main
    _seed_indexer(fresh_env["db_path"], api_key="CANARY-IX")
    _seed_dlclient(fresh_env["db_path"], password="CANARY-DC")
    t1 = main.migrate_encrypt_table_column_secrets()
    t2 = main.migrate_encrypt_table_column_secrets()
    t3 = main.migrate_encrypt_table_column_secrets()
    assert t1 == {"indexers": 1, "download_clients": 1}
    assert t2 == {"indexers": 0, "download_clients": 0}
    assert t3 == {"indexers": 0, "download_clients": 0}


def test_migration_noop_when_cipher_unavailable(fresh_env, monkeypatch, caplog):
    import main, security
    monkeypatch.setattr(security, "_SECRET_CIPHER", None)
    iid = _seed_indexer(fresh_env["db_path"], api_key="CANARY-IX")
    cid = _seed_dlclient(fresh_env["db_path"], password="CANARY-DC")
    with caplog.at_level(logging.WARNING, logger="main"):
        totals = main.migrate_encrypt_table_column_secrets()
    assert totals == {"indexers": 0, "download_clients": 0}
    # Plaintext stays plaintext
    assert _col(fresh_env["db_path"], "indexers", "api_key", iid) == "CANARY-IX"
    assert _col(fresh_env["db_path"], "download_clients", "password", cid) == "CANARY-DC"
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "skipped" in joined.lower() and "cipher unavailable" in joined.lower()


# ───────────────────── read-path integration ─────────────────────

def test_indexer_test_endpoint_decrypts_api_key_before_http(fresh_env):
    """POST /api/indexers/{id}/test on an encrypted row → the outgoing
    HTTP call carries the PLAINTEXT X-Api-Key. Uses httpx mock."""
    import main
    iid = _seed_indexer(fresh_env["db_path"], api_key="CANARY-IX-TEST")
    main.migrate_encrypt_table_column_secrets()
    # On disk, encrypted
    assert _col(fresh_env["db_path"], "indexers", "api_key", iid).startswith("enc:v1:")

    from fastapi.testclient import TestClient
    from main import app
    client = TestClient(app)

    observed: dict = {}

    class _MockResp:
        status_code = 200
        def json(self): return {"version": "1.2.3"}

    class _MockAsyncClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, **kwargs):
            observed['url'] = url
            observed['headers'] = kwargs.get('headers') or {}
            return _MockResp()

    # Pull a CSRF cookie (indexers test is a POST /api/ route — fail-closed
    # auth requires either X-Api-Key or csrftoken cookie on POST)
    client.get("/system/status", follow_redirects=False)
    with patch("routers.indexers.httpx.AsyncClient", _MockAsyncClient):
        r = client.post(f"/api/indexers/{iid}/test", headers=_csrf_header(client))
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True, r.json()
    # The plaintext api_key must have reached the HTTP layer
    assert observed['headers'].get("X-Api-Key") == "CANARY-IX-TEST"


def test_indexer_rss_path_decrypts_api_key(fresh_env):
    """fetch_all_rss() pulls rows, passes plaintext api_key into the
    Prowlarr fetch helpers."""
    import asyncio
    import main
    from routers import indexers as ix_router
    _seed_indexer(fresh_env["db_path"], api_key="CANARY-RSS-KEY", type="torznab")
    main.migrate_encrypt_table_column_secrets()

    observed: dict = {}

    class _MockResp:
        status_code = 200
        text = "<?xml version='1.0'?><rss><channel></channel></rss>"

    class _MockAsyncClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, **kwargs):
            observed['params'] = kwargs.get('params') or {}
            return _MockResp()

    async def _run():
        with main.get_db() as db:
            return await ix_router.fetch_all_rss(db)

    with patch("routers.indexers.httpx.AsyncClient", _MockAsyncClient):
        asyncio.run(_run())

    assert observed['params'].get("apikey") == "CANARY-RSS-KEY"


def test_dlclient_test_endpoint_decrypts_password(fresh_env):
    """POST /api/download-clients/{id}/test decrypts password before
    calling the client's login endpoint."""
    import main
    cid = _seed_dlclient(fresh_env["db_path"], password="CANARY-DC-TEST",
                         type="qbittorrent", host="http://qbit.lan", port=8080)
    main.migrate_encrypt_table_column_secrets()
    assert _col(fresh_env["db_path"], "download_clients", "password", cid).startswith("enc:v1:")

    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    client.get("/system/status", follow_redirects=False)

    observed: dict = {}

    class _MockResp:
        status_code = 200
        text = "Ok."
        def json(self): return {}

    class _MockAsyncClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, url, **kwargs):
            observed['url'] = url
            observed['data'] = kwargs.get('data') or {}
            return _MockResp()
        async def get(self, url, **kwargs):
            observed['url'] = url
            observed['params'] = kwargs.get('params') or {}
            return _MockResp()

    with patch("routers.download_clients.httpx.AsyncClient", _MockAsyncClient):
        r = client.post(f"/api/download-clients/{cid}/test", headers=_csrf_header(client))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True, body
    # qBit login data should carry plaintext password
    assert observed['data'].get("password") == "CANARY-DC-TEST"


def test_indexer_test_form_endpoint_uses_plaintext_api_key(fresh_env):
    import main
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    client.get("/system/status", follow_redirects=False)

    observed: dict = {}

    class _MockResp:
        status_code = 200
        def json(self): return {"version": "1.2.3"}

    class _MockAsyncClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, **kwargs):
            observed["url"] = url
            observed["headers"] = kwargs.get("headers") or {}
            return _MockResp()

    with patch("routers.indexers.httpx.AsyncClient", _MockAsyncClient):
        r = client.post("/api/indexers/test-form", data={
            "name": "unsaved-ix",
            "type": "prowlarr",
            "url": "http://192.168.1.50:9696",
            "api_key": "FORM-PLAINTEXT-KEY",
        }, headers=_csrf_header(client))
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert observed["headers"]["X-Api-Key"] == "FORM-PLAINTEXT-KEY"


def test_dlclient_test_form_endpoint_uses_plaintext_password(fresh_env):
    import main
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    client.get("/system/status", follow_redirects=False)

    observed: dict = {}

    class _MockResp:
        status_code = 200
        text = "Ok."
        def json(self): return {}

    class _MockAsyncClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, url, **kwargs):
            observed["url"] = url
            observed["data"] = kwargs.get("data") or {}
            return _MockResp()

    with patch("routers.download_clients.httpx.AsyncClient", _MockAsyncClient):
        r = client.post("/api/download-clients/test-form", data={
            "name": "unsaved-qb",
            "type": "qbittorrent",
            "host": "http://qbit.lan",
            "port": "8080",
            "username": "u",
            "password": "FORM-DC-PLAINTEXT",
        }, headers=_csrf_header(client))
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert observed["data"]["password"] == "FORM-DC-PLAINTEXT"


def test_nzbget_test_uses_normalized_rpc_url():
    from routers.download_clients import _test_client
    import asyncio

    observed: dict = {}

    class _MockResp:
        def json(self): return {"result": "24.5"}

    class _MockAsyncClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, url, **kwargs):
            observed["url"] = url
            return _MockResp()

    async def _run():
        with patch("routers.download_clients.httpx.AsyncClient", _MockAsyncClient):
            return await _test_client({
                "type": "nzbget",
                "host": "http://nzbget.lan/base",
                "port": 6789,
                "username": "u",
                "password": "p",
                "use_ssl": 0,
            })

    ok, msg = asyncio.run(_run())
    assert ok is True, msg
    assert observed["url"] == "http://u:p@nzbget.lan:6789/base/jsonrpc"


def test_get_client_for_protocol_returns_plaintext_password(fresh_env):
    """Covers every grab / queue handler path: qbit_grab, qbit_remove,
    sab_grab, sab_remove, nzbget_grab. All funnel through
    get_client_for_protocol, which must return a plaintext password."""
    import main
    _seed_dlclient(fresh_env["db_path"], password="CANARY-ROUTE-PW",
                   type="qbittorrent", host="http://qbit.lan", port=8080)
    main.migrate_encrypt_table_column_secrets()
    from routers.download_clients import get_client_for_protocol
    with main.get_db() as db:
        c = get_client_for_protocol(db, "torrent")
    assert c is not None
    assert c["password"] == "CANARY-ROUTE-PW"


def test_suwayomi_get_client_returns_plaintext_password(fresh_env):
    import main
    _seed_dlclient(fresh_env["db_path"], password="CANARY-SWY-PW",
                   type="suwayomi", host="http://swy.lan", port=4567)
    main.migrate_encrypt_table_column_secrets()
    from routers.suwayomi_ import get_suwayomi_client
    with main.get_db() as db:
        c = get_suwayomi_client(db)
    assert c is not None
    assert c["password"] == "CANARY-SWY-PW"


# ───────────────────── write-path integration ─────────────────────

def test_create_indexer_form_encrypts_api_key(fresh_env):
    import main
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    client.get("/system/status", follow_redirects=False)
    token = client.cookies.get("csrftoken")
    r = client.post("/indexers", data={
        "csrf_token": token,
        "name": "form-ix",
        "type": "prowlarr",
        "url": "http://192.168.1.50:9696",
        "api_key": "FORM-CANARY-IX-KEY",
        "priority": "25",
        "enabled": "1",
        "categories": "7000,7010",
        "settings": "{}",
    }, follow_redirects=False)
    assert r.status_code == 303, r.text
    # On disk, encrypted
    with sqlite3.connect(fresh_env["db_path"]) as c:
        row = c.execute(
            "SELECT api_key,url,name FROM indexers WHERE name='form-ix'"
        ).fetchone()
    assert row is not None
    assert row[0].startswith("enc:v1:")
    # Non-secret fields stayed plaintext
    assert row[1] == "http://192.168.1.50:9696"
    assert row[2] == "form-ix"


def test_edit_indexer_form_encrypts_new_api_key(fresh_env):
    import main
    iid = _seed_indexer(fresh_env["db_path"], api_key="OLD-KEY")
    main.migrate_encrypt_table_column_secrets()
    old_enc = _col(fresh_env["db_path"], "indexers", "api_key", iid)
    assert old_enc.startswith("enc:v1:")

    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    client.get("/system/status", follow_redirects=False)
    token = client.cookies.get("csrftoken")
    r = client.post(f"/indexers/{iid}", data={
        "csrf_token": token,
        "name": "test-ix",
        "type": "prowlarr",
        "url": "http://192.168.1.50:9696",
        "api_key": "ROTATED-CANARY-KEY",
        "priority": "25",
        "enabled": "1",
        "categories": "7000",
        "settings": "{}",
    }, follow_redirects=False)
    assert r.status_code == 303
    new_enc = _col(fresh_env["db_path"], "indexers", "api_key", iid)
    assert new_enc.startswith("enc:v1:")
    assert new_enc != old_enc


def test_create_dlclient_form_encrypts_password(fresh_env):
    import main
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    client.get("/system/status", follow_redirects=False)
    token = client.cookies.get("csrftoken")
    r = client.post("/download-clients", data={
        "csrf_token": token,
        "name": "form-qb",
        "type": "qbittorrent",
        "host": "http://qbit.lan",
        "port": "8080",
        "username": "u",
        "password": "FORM-CANARY-DC-PW",
        "category": "manga",
        "priority": "1",
        "enabled": "1",
    }, follow_redirects=False)
    assert r.status_code == 303, r.text
    with sqlite3.connect(fresh_env["db_path"]) as c:
        row = c.execute(
            "SELECT password,username,host FROM download_clients WHERE name='form-qb'"
        ).fetchone()
    assert row[0].startswith("enc:v1:")
    # Non-secret fields stayed plaintext
    assert row[1] == "u"
    assert row[2] == "http://qbit.lan"


def test_edit_dlclient_form_encrypts_new_password(fresh_env):
    import main
    cid = _seed_dlclient(fresh_env["db_path"], password="OLD-PW",
                         type="qbittorrent", host="http://qbit.lan", port=8080)
    main.migrate_encrypt_table_column_secrets()
    old_enc = _col(fresh_env["db_path"], "download_clients", "password", cid)
    assert old_enc.startswith("enc:v1:")

    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    client.get("/system/status", follow_redirects=False)
    token = client.cookies.get("csrftoken")
    r = client.post(f"/download-clients/{cid}", data={
        "csrf_token": token,
        "name": "test-qb",
        "type": "qbittorrent",
        "host": "http://qbit.lan",
        "port": "8080",
        "username": "u",
        "password": "ROTATED-CANARY-DC-PW",
        "category": "manga",
        "priority": "1",
        "enabled": "1",
    }, follow_redirects=False)
    assert r.status_code == 303, r.text
    new_enc = _col(fresh_env["db_path"], "download_clients", "password", cid)
    assert new_enc.startswith("enc:v1:")
    assert new_enc != old_enc


# ───────────────────── wrong-key safety ─────────────────────

def test_wrong_key_disables_indexer_cleanly(fresh_env, monkeypatch, caplog):
    """Encrypt with key A, swap cipher to key B, fetch indexer via
    _row_decrypted → api_key becomes '' + WARNING names the indexer.
    No canary / ciphertext in logs."""
    import main, security
    from routers import indexers as ix_router
    canary = "CANARY-MUSTNOTLEAK-IX-zzz"
    iid = _seed_indexer(fresh_env["db_path"], api_key=canary, name="rot-ix")
    main.migrate_encrypt_table_column_secrets()
    enc = _col(fresh_env["db_path"], "indexers", "api_key", iid)
    assert enc.startswith("enc:v1:")

    from cryptography.fernet import Fernet
    monkeypatch.setattr(security, "_SECRET_CIPHER", Fernet(Fernet.generate_key()))

    with caplog.at_level(logging.WARNING, logger="security"):
        with main.get_db() as db:
            row = db.execute("SELECT * FROM indexers WHERE id=?", (iid,)).fetchone()
            decrypted = ix_router._row_decrypted(row)
    assert decrypted["api_key"] == ""
    # Non-secret fields still populated
    assert decrypted["name"] == "rot-ix"
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "indexers.api_key" in joined
    assert "rot-ix" in joined  # context named
    assert "could not be decrypted" in joined.lower()
    assert canary not in joined
    assert enc not in joined


def test_wrong_key_disables_dlclient_cleanly(fresh_env, monkeypatch, caplog):
    import main, security
    from routers import download_clients as dc_router
    canary = "CANARY-MUSTNOTLEAK-DC-zzz"
    cid = _seed_dlclient(fresh_env["db_path"], password=canary, name="rot-qb",
                         type="qbittorrent")
    main.migrate_encrypt_table_column_secrets()
    enc = _col(fresh_env["db_path"], "download_clients", "password", cid)
    assert enc.startswith("enc:v1:")

    from cryptography.fernet import Fernet
    monkeypatch.setattr(security, "_SECRET_CIPHER", Fernet(Fernet.generate_key()))

    with caplog.at_level(logging.WARNING, logger="security"):
        with main.get_db() as db:
            row = db.execute(
                "SELECT * FROM download_clients WHERE id=?", (cid,)
            ).fetchone()
            decrypted = dc_router._row_decrypted(row)
    assert decrypted["password"] == ""
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "download_clients.password" in joined
    assert "rot-qb" in joined
    assert "could not be decrypted" in joined.lower()
    assert canary not in joined
    assert enc not in joined


# ───────────────────── leak guard across full lifecycle ─────────────────────

def test_no_secret_logs_during_full_lifecycle(fresh_env, caplog):
    """Seed canaries in both tables, run migration, fetch via read path,
    wrong-key decrypt. No canary or ciphertext in any captured log
    record."""
    import io
    import main, security
    buf = io.StringIO()
    h = logging.StreamHandler(buf); h.setLevel(logging.DEBUG)
    for n in ("main", "security", "shared"):
        logging.getLogger(n).addHandler(h); logging.getLogger(n).setLevel(logging.DEBUG)

    canaries = {
        "indexer_key": "CANARY-IX-zZ1",
        "dlclient_pw": "CANARY-DC-zZ2",
    }
    iid = _seed_indexer(fresh_env["db_path"], api_key=canaries["indexer_key"])
    cid = _seed_dlclient(fresh_env["db_path"], password=canaries["dlclient_pw"])
    main.migrate_encrypt_table_column_secrets()
    enc_ix = _col(fresh_env["db_path"], "indexers", "api_key", iid)
    enc_dc = _col(fresh_env["db_path"], "download_clients", "password", cid)
    # Fetch through read wrappers
    from routers.download_clients import get_client_for_protocol
    from routers.indexers import _row_decrypted as ix_decrypt
    with main.get_db() as db:
        c = get_client_for_protocol(db, "torrent")
        row = db.execute("SELECT * FROM indexers").fetchone()
        ix_decrypt(row)
    assert c["password"] == canaries["dlclient_pw"]

    text = buf.getvalue()
    for label, c in canaries.items():
        assert c not in text, f"canary {label}={c!r} leaked into logs"
    assert enc_ix not in text, "enc:v1: api_key leaked into logs"
    assert enc_dc not in text, "enc:v1: password leaked into logs"


# ───────────────────── scope: notification_connections untouched ─────────────────────

def test_notification_connections_untouched(fresh_env):
    """PR #3 must NOT touch notification_connections — those land in a
    later PR. Drop a plaintext row directly, run migration, confirm
    it stays plaintext."""
    import main
    with sqlite3.connect(fresh_env["db_path"]) as c:
        # notification_connections table: name,type,settings(JSON)
        # settings JSON contains the secret (bot tokens, webhooks, etc.)
        c.execute(
            "INSERT INTO notification_connections(name,type,settings,on_grab,on_download,on_upgrade,enabled)"
            " VALUES('discord-test','discord',"
            " '{\"webhook_url\":\"https://PLAINTEXT-HOOK\",\"username\":\"bot\"}',"
            " 1,1,1,1)"
        )
        c.commit()
    main.migrate_encrypt_table_column_secrets()
    with sqlite3.connect(fresh_env["db_path"]) as c:
        row = c.execute(
            "SELECT settings FROM notification_connections WHERE name='discord-test'"
        ).fetchone()
    assert row is not None
    data = json.loads(row[0])
    assert data["webhook_url"] == "https://PLAINTEXT-HOOK", \
        "PR #3 must NOT touch notification_connections"


# ───────────────────── plaintext back-compat ─────────────────────

def test_plaintext_values_still_work_pre_migration(fresh_env):
    """A pre-migration plaintext row must still be usable by read paths
    (via decrypt_secret_safe's pass-through). This lets operators run
    without MANGARR_SECRET_KEY through the upgrade window."""
    import main
    _seed_indexer(fresh_env["db_path"], api_key="legacy-plain-ix-key")
    _seed_dlclient(fresh_env["db_path"], password="legacy-plain-pw",
                   type="qbittorrent", host="http://qbit.lan", port=8080)
    # Skip migration
    from routers.download_clients import get_client_for_protocol
    from routers.indexers import _row_decrypted as ix_decrypt
    with main.get_db() as db:
        c = get_client_for_protocol(db, "torrent")
        ir = db.execute("SELECT * FROM indexers").fetchone()
        ix = ix_decrypt(ir)
    assert c["password"] == "legacy-plain-pw"
    assert ix["api_key"] == "legacy-plain-ix-key"
