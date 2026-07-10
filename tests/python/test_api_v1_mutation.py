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
        c.execute(
            "INSERT INTO seen(torrent_url, torrent_name, series_id, volume_num,"
            " grabbed_at, download_id)"
            " VALUES('failed-release-url', 'Failed Release', 5, 3.0,"
            " datetime('now'), 'fail-dl')"
        )
        c.execute(
            "INSERT INTO seen(torrent_url, torrent_name, series_id, volume_num,"
            " grabbed_at, download_id)"
            " VALUES('reset-release-url', 'Reset Release', 5, 4.0,"
            " datetime('now'), 'reset-dl')"
        )
        c.execute(
            "INSERT INTO volumes"
            "(id, series_id, volume_num, status, grabbed_at, source_url,"
            " torrent_name, indexer, protocol, client, download_id,"
            " release_group)"
            " VALUES(501, 5, 3.0, 'grabbed', datetime('now'),"
            " 'failed-release-url', 'Failed Release', 'Nyaa', 'torrent',"
            " 'qBittorrent', 'fail-dl', 'Group')"
        )
        c.execute(
            "INSERT INTO volumes"
            "(id, series_id, volume_num, status, grabbed_at, source_url,"
            " torrent_name, indexer, protocol, client, download_id,"
            " release_group)"
            " VALUES(502, 5, 4.0, 'grabbed', datetime('now'),"
            " 'reset-release-url', 'Reset Release', 'Nyaa', 'torrent',"
            " 'qBittorrent', 'reset-dl', 'Group')"
        )
        c.execute(
            "INSERT INTO volumes"
            "(id, series_id, volume_num, status)"
            " VALUES(503, 5, 5.0, 'wanted')"
        )
        c.execute(
            "INSERT INTO history"
            "(id, event_type, series_id, series_title, volume_label,"
            " source_title, indexer, protocol, client, download_id,"
            " size_bytes, release_group)"
            " VALUES(701, 'grabbed', 5, 'S5', 'Vol 3',"
            " 'Failed Release', 'Nyaa', 'torrent', 'qBittorrent',"
            " 'fail-dl', 12345, 'Group')"
        )
        c.execute(
            "INSERT INTO history"
            "(id, event_type, series_id, series_title, volume_label,"
            " source_title)"
            " VALUES(702, 'import_failed', 5, 'S5', 'Vol 4',"
            " 'Already Failed')"
        )
        c.execute(
            "INSERT INTO pending_releases"
            "(id, series_id, url, title, indexer, protocol, size_bytes)"
            " VALUES(801, 5, 'https://example.invalid/pending',"
            " 'Pending Release', 'Nyaa', 'torrent', 456)"
        )
        c.execute(
            "INSERT INTO blocklist"
            "(id, series_id, torrent_url, torrent_name, reason)"
            " VALUES(601, 5, 'https://example.invalid/bad.torrent',"
            " 'Bad Release', 'Manual')"
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
    assert urls == ["failed-release-url", "recent", "reset-release-url"]


def test_api_v1_command_rejects_unknown_command(env):
    resp = _client().post(
        "/api/v1/command",
        json={"name": "NoSuchCommand"},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


def test_api_v1_delete_blocklist_entry_removes_row(env):
    resp = _client().delete(
        "/api/v1/blocklist/601",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "id": 601}

    with sqlite3.connect(env) as c:
        remaining = c.execute(
            "SELECT COUNT(*) FROM blocklist WHERE id=601"
        ).fetchone()[0]
    assert remaining == 0


def test_api_v1_delete_blocklist_entry_requires_api_key(env):
    resp = _client().delete("/api/v1/blocklist/601")
    assert resp.status_code == 401


def test_api_v1_delete_blocklist_entry_returns_404_for_unknown_id(env):
    resp = _client().delete(
        "/api/v1/blocklist/99999",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "blocklist entry not found"

    with sqlite3.connect(env) as c:
        remaining = c.execute("SELECT COUNT(*) FROM blocklist").fetchone()[0]
    assert remaining == 1


def test_api_v1_history_failed_marks_grabbed_release_failed(env):
    resp = _client().post(
        "/api/v1/history/701/failed",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "id": 701}

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        history = c.execute("SELECT event_type FROM history WHERE id=701").fetchone()
        volume = c.execute(
            "SELECT status, source_url, download_id, indexer, protocol,"
            " client, release_group FROM volumes WHERE id=501"
        ).fetchone()
        seen_count = c.execute(
            "SELECT COUNT(*) FROM seen WHERE download_id='fail-dl'"
        ).fetchone()[0]
        blocklist = c.execute(
            "SELECT series_id, torrent_url, torrent_name, reason, indexer,"
            " protocol, size_bytes FROM blocklist WHERE torrent_url='fail-dl'"
        ).fetchone()

    assert history["event_type"] == "grab_failed"
    assert dict(volume) == {
        "status": "wanted",
        "source_url": None,
        "download_id": None,
        "indexer": None,
        "protocol": None,
        "client": None,
        "release_group": None,
    }
    assert seen_count == 0
    assert dict(blocklist) == {
        "series_id": 5,
        "torrent_url": "fail-dl",
        "torrent_name": "Failed Release",
        "reason": "Marked failed via history",
        "indexer": "Nyaa",
        "protocol": "torrent",
        "size_bytes": 12345,
    }


def test_api_v1_history_failed_requires_api_key(env):
    resp = _client().post("/api/v1/history/701/failed")
    assert resp.status_code == 401


def test_api_v1_history_failed_rejects_unknown_history_id(env):
    resp = _client().post(
        "/api/v1/history/99999/failed",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "history entry not found"


def test_api_v1_history_failed_rejects_non_grabbed_history(env):
    resp = _client().post(
        "/api/v1/history/702/failed",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "history entry is not grabbed"

    with sqlite3.connect(env) as c:
        event_type = c.execute(
            "SELECT event_type FROM history WHERE id=702"
        ).fetchone()[0]
    assert event_type == "import_failed"


def test_api_v1_delete_history_entry_removes_row(env):
    resp = _client().delete(
        "/api/v1/history/702",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "id": 702}

    with sqlite3.connect(env) as c:
        deleted = c.execute("SELECT 1 FROM history WHERE id=702").fetchone()
        kept = c.execute("SELECT 1 FROM history WHERE id=701").fetchone()
    assert deleted is None
    assert kept is not None


def test_api_v1_delete_history_entry_requires_api_key(env):
    resp = _client().delete("/api/v1/history/702")
    assert resp.status_code == 401


def test_api_v1_delete_history_entry_rejects_unknown_id(env):
    resp = _client().delete(
        "/api/v1/history/99999",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "history entry not found"


def test_api_v1_queue_reset_grabbed_volume_returns_wanted(env):
    resp = _client().post(
        "/api/v1/queue/grabbed/502/reset",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "id": 502}

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        volume = c.execute(
            "SELECT status, source_url, download_id, indexer, protocol,"
            " client, release_group FROM volumes WHERE id=502"
        ).fetchone()
        seen_by_url = c.execute(
            "SELECT 1 FROM seen WHERE torrent_url='reset-release-url'"
        ).fetchone()
        seen_by_download = c.execute(
            "SELECT 1 FROM seen WHERE download_id='reset-dl'"
        ).fetchone()

    assert dict(volume) == {
        "status": "wanted",
        "source_url": None,
        "download_id": None,
        "indexer": None,
        "protocol": None,
        "client": None,
        "release_group": None,
    }
    assert seen_by_url is None
    assert seen_by_download is None


def test_api_v1_queue_reset_grabbed_volume_requires_api_key(env):
    resp = _client().post("/api/v1/queue/grabbed/502/reset")
    assert resp.status_code == 401


def test_api_v1_queue_reset_grabbed_volume_rejects_unknown_id(env):
    resp = _client().post(
        "/api/v1/queue/grabbed/99999/reset",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "queue volume not found"


def test_api_v1_queue_reset_grabbed_volume_rejects_non_grabbed_volume(env):
    resp = _client().post(
        "/api/v1/queue/grabbed/503/reset",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "queue volume is not grabbed"

    with sqlite3.connect(env) as c:
        status = c.execute("SELECT status FROM volumes WHERE id=503").fetchone()[0]
    assert status == "wanted"


def test_api_v1_queue_dismiss_pending_release_removes_row(env):
    resp = _client().delete(
        "/api/v1/queue/pending/801",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "id": 801}

    with sqlite3.connect(env) as c:
        remaining = c.execute(
            "SELECT COUNT(*) FROM pending_releases WHERE id=801"
        ).fetchone()[0]
    assert remaining == 0


def test_api_v1_queue_dismiss_pending_release_requires_api_key(env):
    resp = _client().delete("/api/v1/queue/pending/801")
    assert resp.status_code == 401


def test_api_v1_queue_dismiss_pending_release_rejects_unknown_id(env):
    resp = _client().delete(
        "/api/v1/queue/pending/99999",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "pending release not found"

    with sqlite3.connect(env) as c:
        remaining = c.execute("SELECT COUNT(*) FROM pending_releases").fetchone()[0]
    assert remaining == 1
