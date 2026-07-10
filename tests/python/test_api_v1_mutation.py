import json
import os
import sqlite3
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


@pytest.fixture
def env():
    import main
    import security
    import shared

    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close()
    os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-api-v1-mutation-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    orig_cipher = security._SECRET_CIPHER
    orig_main_config = dict(main.CONFIG)
    orig_shared_config = dict(shared.CONFIG)
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()
    main.ensure_api_key()

    with sqlite3.connect(db.name) as c:
        c.execute("DELETE FROM seen")
        c.execute("DELETE FROM volumes")
        c.execute("DELETE FROM series")
        c.execute(
            "INSERT INTO series(id, title, search_pattern, edition_type,"
            " omnibus_preference, update_strategy, source_type,"
            " quality_cutoff, total_volumes, enabled, monitored, monitor_mode)"
            " VALUES(5, 'S5', 'S5', 'official_color', 'prefer_omnibus',"
            " 'once', 'official_only', 'cbz', 42, 1, 1, 'missing')"
        )
        c.execute(
            "INSERT INTO seen(torrent_url, torrent_name, series_id, volume_num,"
            " grabbed_at, download_id)"
            " VALUES('stale', 'Old Release', 5, 1.0,"
            " datetime('now', '-120 days'), 'gone')"
        )
        c.execute(
            "INSERT INTO seen(torrent_url, torrent_name, series_id, volume_num,"
            " grabbed_at, download_id)"
            " VALUES('recent', 'Recent Release', 5, 2.0,"
            " datetime('now', '-1 day'), 'keep')"
        )

    try:
        yield db.name
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        security._SECRET_CIPHER = orig_cipher
        main.CONFIG.clear()
        main.CONFIG.update(orig_main_config)
        shared.CONFIG.clear()
        shared.CONFIG.update(orig_shared_config)
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


def _client():
    import main

    return TestClient(main.app)


def _api_key(db_path: str) -> str:
    from security import decrypt_secret

    with sqlite3.connect(db_path) as c:
        raw = c.execute(
            "SELECT value FROM settings WHERE key='api_key'"
        ).fetchone()[0]
    return decrypt_secret(raw)


def _series_row(db_path: str) -> dict:
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        return dict(c.execute("SELECT * FROM series WHERE id=5").fetchone())


def test_api_v1_patch_series_preserves_unsubmitted_fields(env):
    resp = _client().request(
        "PATCH",
        "/api/v1/series/5",
        json={"title": "S5 Renamed"},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "updated": ["title"]}

    row = _series_row(env)
    assert row["title"] == "S5 Renamed"
    assert row["edition_type"] == "official_color"
    assert row["omnibus_preference"] == "prefer_omnibus"
    assert row["update_strategy"] == "once"
    assert row["source_type"] == "official_only"
    assert row["quality_cutoff"] == "cbz"
    assert row["total_volumes"] == 42
    assert row["monitor_mode"] == "missing"


def test_api_v1_patch_series_rejects_unknown_fields(env):
    resp = _client().request(
        "PATCH",
        "/api/v1/series/5",
        json={"not_a_field": "oops"},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert "unknown" in resp.json()["error"]


def test_api_v1_patch_series_sets_manual_volume_count_source(env):
    resp = _client().request(
        "PATCH",
        "/api/v1/series/5",
        json={"total_volumes": 50},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    row = _series_row(env)
    assert row["total_volumes"] == 50
    assert row["vol_count_source"] == "manual"


def test_api_v1_patch_series_stores_group_lists_as_json(env):
    resp = _client().request(
        "PATCH",
        "/api/v1/series/5",
        json={"preferred_groups": ["ScanGroupA", "ScanGroupB"]},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    stored = json.loads(_series_row(env)["preferred_groups"])
    assert stored == ["ScanGroupA", "ScanGroupB"]


def test_api_v1_patch_series_requires_api_key(env):
    resp = _client().request(
        "PATCH",
        "/api/v1/series/5",
        json={"title": "no-auth"},
    )
    assert resp.status_code == 401


def test_api_v1_command_cleanup_seen_mutates_stale_rows(env):
    client = _client()
    headers = {"X-Api-Key": _api_key(env)}

    resp = client.post("/api/v1/command", json={"name": "CleanupSeen"}, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert "Removed 1 stale" in body["message"]

    with sqlite3.connect(env) as c:
        urls = [
            row[0]
            for row in c.execute("SELECT torrent_url FROM seen ORDER BY torrent_url")
        ]
    assert urls == ["recent"]


def test_api_v1_command_rejects_unknown_command(env):
    resp = _client().post(
        "/api/v1/command",
        json={"name": "NoSuchCommand"},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert resp.json()["ok"] is False
