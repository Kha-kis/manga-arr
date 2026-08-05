"""Regression coverage for interactive-search release handoff."""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401


@pytest.fixture
def env(tmp_path):
    import main
    import security
    import shared

    db_path = str(tmp_path / "interactive-grab.db")
    key_dir = str(tmp_path / "keys")
    original_main_db = main.DB_PATH
    original_shared_db = shared.DB_PATH
    original_cipher = security._SECRET_CIPHER
    original_main_config = dict(main.CONFIG)
    original_shared_config = dict(shared.CONFIG)
    main.DB_PATH = db_path
    shared.DB_PATH = db_path
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()
    main.ensure_api_key()

    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO series(id, title, search_pattern, monitored, status,"
            " total_volumes) VALUES(1, 'Berserk', 'Berserk', 1, 'RELEASING', 42)"
        )
        db.execute(
            "INSERT INTO volumes(id, series_id, volume_num, status, monitored)"
            " VALUES(10, 1, 42, 'wanted', 1)"
        )

    try:
        yield {"db_path": db_path}
    finally:
        main.DB_PATH = original_main_db
        shared.DB_PATH = original_shared_db
        security._SECRET_CIPHER = original_cipher
        main.CONFIG.clear()
        main.CONFIG.update(original_main_config)
        shared.CONFIG.clear()
        shared.CONFIG.update(original_shared_config)
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(db_path + suffix)
            except FileNotFoundError:
                pass


def _client():
    import main
    from fastapi.testclient import TestClient

    return TestClient(main.app)


def _api_key(env: dict[str, str]) -> str:
    with sqlite3.connect(env["db_path"]) as db:
        row = db.execute("SELECT value FROM settings WHERE key='api_key'").fetchone()
    assert row is not None
    return str(row[0])


def test_interactive_search_manual_grab_forwards_release_identity(env, monkeypatch):
    import main

    release = {
        "title": "Berserk Vol 42 [Digital]",
        "url": "http://prowlarr.test/download/42",
        "size_bytes": 60_000_000,
        "seeders": 6,
        "guid": "berserk-v42-guid",
        "indexer": "Prowlarr Usenet",
        "protocol": "nzb",
        "preferred_client_id": 23,
        "_score": 100,
    }

    async def fake_search(*args, **kwargs):
        return [release.copy()]

    monkeypatch.setattr(main, "_search_all", fake_search)
    monkeypatch.setattr(main, "matches", lambda pattern, title: True)
    monkeypatch.setattr(
        main,
        "evaluate_release",
        lambda item, series_id, db: {
            "score": 100,
            "status": "would_grab",
            "rejections": [],
            "custom_format_matches": [],
            "quality": "cbz",
            "size_mb": 57.2,
        },
    )

    client = _client()
    search_response = client.get(
        "/api/series/1/volumes/10/search",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert search_response.status_code == 200, search_response.text
    result = search_response.json()["results"][0]
    assert result["seeders"] == 6
    assert result["guid"] == "berserk-v42-guid"
    assert result["preferred_client_id"] == 23

    captured = {}

    async def fake_grab(item, series_id, respect_monitoring=True):
        captured.update(
            item=item,
            series_id=series_id,
            respect_monitoring=respect_monitoring,
        )
        return True

    monkeypatch.setattr(main, "grab_item", fake_grab)
    grab_response = client.post(
        "/api/series/1/volumes/10/grab-release",
        json=result,
        headers={"X-Api-Key": _api_key(env)},
    )

    assert grab_response.status_code == 200, grab_response.text
    assert grab_response.json()["ok"] is True
    assert captured["series_id"] == 1
    assert captured["respect_monitoring"] is False
    assert captured["item"]["seeders"] == 6
    assert captured["item"]["guid"] == "berserk-v42-guid"
    assert captured["item"]["preferred_client_id"] == 23


def test_manual_grab_rejects_non_object_json_without_calling_grab(env, monkeypatch):
    import main

    async def unexpected_grab(*args, **kwargs):
        raise AssertionError("grab_item must not receive malformed request data")

    monkeypatch.setattr(main, "grab_item", unexpected_grab)
    response = _client().post(
        "/api/series/1/volumes/10/grab-release",
        json=["not", "an", "object"],
        headers={"X-Api-Key": _api_key(env)},
    )

    assert response.status_code == 400
    assert response.json() == {
        "ok": False,
        "message": "JSON payload must be an object",
    }
