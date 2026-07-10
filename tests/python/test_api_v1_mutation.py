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
        c.execute("DELETE FROM root_folders")
        c.execute(
            "INSERT INTO root_folders(id, path, label, is_default)"
            " VALUES(301, '/library/a', 'Library A', 1)"
        )
        c.execute(
            "INSERT INTO root_folders(id, path, label, is_default)"
            " VALUES(302, '/library/b', 'Library B', 0)"
        )
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
            "INSERT INTO seen(torrent_url, torrent_name, series_id, volume_num,"
            " grabbed_at, download_id)"
            " VALUES('import-release-url', 'Import Release', 5, 6.0,"
            " datetime('now'), 'import-dl')"
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
            "INSERT INTO volumes"
            "(id, series_id, volume_num, status, grabbed_at, source_url,"
            " torrent_name, indexer, protocol, client, download_id,"
            " release_group)"
            " VALUES(504, 5, 6.0, 'grabbed', datetime('now'),"
            " 'import-release-url', 'Import Release', 'Nyaa', 'torrent',"
            " 'qBittorrent', 'import-dl', 'Group')"
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
            "INSERT INTO history"
            "(id, event_type, series_id, series_title, volume_label,"
            " source_title)"
            " VALUES(703, 'grab_failed', 5, 'S5', 'Vol 6',"
            " 'Failed Grab')"
        )
        c.execute(
            "INSERT INTO pending_releases"
            "(id, series_id, url, title, indexer, protocol, size_bytes)"
            " VALUES(801, 5, 'https://example.invalid/pending',"
            " 'Pending Release', 'Nyaa', 'torrent', 456)"
        )
        c.execute(
            "INSERT INTO import_queue"
            "(id, series_id, download_id, torrent_name, torrent_url,"
            " volume_num, src_dir, status)"
            " VALUES(901, 5, 'import-dl', 'Import Release',"
            " 'import-release-url', 6.0, '/downloads/import', 'pending')"
        )
        c.execute(
            "INSERT INTO import_queue_files"
            "(id, queue_id, filename, src_path, dst_path, proposed_volume,"
            " status)"
            " VALUES(902, 901, 'Import Release.cbz',"
            " '/downloads/import/Import Release.cbz',"
            " '/library/S5/Import Release.cbz', 6.0, 'pending')"
        )
        c.execute(
            "INSERT INTO import_queue"
            "(id, series_id, download_id, torrent_name, torrent_url,"
            " volume_num, src_dir, status)"
            " VALUES(903, 5, 'failed-import-dl', 'Failed Import',"
            " 'failed-import-url', 7.0, '/downloads/failed-import', 'failed')"
        )
        c.execute(
            "INSERT INTO import_queue_files"
            "(id, queue_id, filename, src_path, dst_path, proposed_volume,"
            " status)"
            " VALUES(904, 903, 'Failed Import.cbz',"
            " '/downloads/failed-import/Failed Import.cbz',"
            " '/library/S5/Failed Import.cbz', 7.0, 'failed')"
        )
        c.execute(
            "INSERT INTO import_queue"
            "(id, series_id, download_id, torrent_name, torrent_url,"
            " volume_num, src_dir, status)"
            " VALUES(905, 5, 'skipped-import-dl', 'Skipped Import',"
            " 'skipped-import-url', 8.0, '/downloads/skipped-import', 'skipped')"
        )
        c.execute(
            "INSERT INTO import_queue_files"
            "(id, queue_id, filename, src_path, dst_path, proposed_volume,"
            " status)"
            " VALUES(906, 905, 'Skipped Import.cbz',"
            " '/downloads/skipped-import/Skipped Import.cbz',"
            " '/library/S5/Skipped Import.cbz', 8.0, 'skipped')"
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


def _series_row_by_title(db_path: str, title: str) -> dict:
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        return dict(
            c.execute("SELECT * FROM series WHERE title=?", (title,)).fetchone()
        )


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


def test_api_v1_create_series_adds_row_stubs_and_history(env):
    resp = _client().post(
        "/api/v1/series",
        json={
            "title": "New Manga",
            "searchPattern": "New Manga Deluxe",
            "anilistId": 1234,
            "malId": 5678,
            "mangaUpdatesId": "mu-1234",
            "coverUrl": "https://example.invalid/cover.jpg",
            "status": "releasing",
            "overview": "Created through API",
            "totalVolumes": 3,
            "totalChapters": 30,
            "rootFolderId": 302,
            "year": 2026,
            "monitored": True,
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "created"
    assert body["series"]["title"] == "New Manga"
    assert body["series"]["searchPattern"] == "New Manga Deluxe"
    assert body["series"]["rootFolderId"] == 302
    assert body["series"]["monitorMode"] == "missing"
    assert body["series"]["statistics"]["volumeCount"] == 3
    series_id = body["series"]["id"]

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT search_pattern, anilist_id, mal_id, mu_id, root_folder_id,"
            " total_volumes, total_chapters, pub_year, monitored, enabled,"
            " monitor_mode, vol_count_source FROM series WHERE id=?",
            (series_id,),
        ).fetchone()
        volumes = c.execute(
            "SELECT volume_num, status, monitored FROM volumes"
            " WHERE series_id=? ORDER BY volume_num",
            (series_id,),
        ).fetchall()
        history = c.execute(
            "SELECT event_type, series_id, series_title, source_title, data"
            " FROM history WHERE event_type='series_added'"
            " AND series_id=?",
            (series_id,),
        ).fetchone()

    assert dict(row) == {
        "search_pattern": "New Manga Deluxe",
        "anilist_id": 1234,
        "mal_id": 5678,
        "mu_id": "mu-1234",
        "root_folder_id": 302,
        "total_volumes": 3,
        "total_chapters": 30,
        "pub_year": 2026,
        "monitored": 1,
        "enabled": 1,
        "monitor_mode": "missing",
        "vol_count_source": "anilist",
    }
    assert [tuple(row) for row in volumes] == [
        (1.0, "wanted", 1),
        (2.0, "wanted", 1),
        (3.0, "wanted", 1),
    ]
    assert dict(history) == {
        "event_type": "series_added",
        "series_id": series_id,
        "series_title": "New Manga",
        "source_title": "New Manga",
        "data": '{"total_volumes": 3, "status": "releasing"}',
    }


def test_api_v1_create_series_returns_existing_active_match(env):
    resp = _client().post(
        "/api/v1/series",
        json={"title": "S5", "editionType": "official_color"},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "exists"
    assert resp.json()["series"]["id"] == 5

    with sqlite3.connect(env) as c:
        count = c.execute("SELECT COUNT(*) FROM series WHERE title='S5'").fetchone()[0]
    assert count == 1


def test_api_v1_create_series_requires_api_key(env):
    resp = _client().post("/api/v1/series", json={"title": "No Auth"})
    assert resp.status_code == 401


def test_api_v1_create_series_rejects_blank_title(env):
    resp = _client().post(
        "/api/v1/series",
        json={"title": "   "},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "title is required"


def test_api_v1_create_series_rejects_negative_counts(env):
    resp = _client().post(
        "/api/v1/series",
        json={"title": "Bad Count", "totalVolumes": -1},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "totalVolumes must be zero or a positive integer"


def test_api_v1_create_series_rejects_unknown_profile_id(env):
    resp = _client().post(
        "/api/v1/series",
        json={"title": "Bad Profile", "qualityProfileId": 99999},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "qualityProfileId not found"

    with sqlite3.connect(env) as c:
        count = c.execute(
            "SELECT COUNT(*) FROM series WHERE title='Bad Profile'"
        ).fetchone()[0]
    assert count == 0


def test_api_v1_create_series_requires_a_root_folder(env):
    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")

    resp = _client().post(
        "/api/v1/series",
        json={"title": "No Root"},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert "No root folder configured" in resp.json()["error"]


def test_api_v1_create_series_suppresses_stubs_for_nonstandard_editions(env):
    resp = _client().post(
        "/api/v1/series",
        json={
            "title": "Omnibus Manga",
            "totalVolumes": 4,
            "editionType": "omnibus",
            "monitored": False,
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "created"
    assert body["series"]["monitorMode"] == "none"

    row = _series_row_by_title(env, "Omnibus Manga")
    with sqlite3.connect(env) as c:
        volume_count = c.execute(
            "SELECT COUNT(*) FROM volumes WHERE series_id=?",
            (row["id"],),
        ).fetchone()[0]
    assert row["edition_type"] == "omnibus"
    assert row["monitored"] == 0
    assert volume_count == 0


def test_api_v1_delete_series_soft_deletes_and_logs_history(env):
    client = _client()
    headers = {"X-Api-Key": _api_key(env)}

    resp = client.delete("/api/v1/series/5", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "id": 5}

    detail = client.get("/api/v1/series/5", headers=headers)
    assert detail.status_code == 404

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT deleted_at, deletion_reason FROM series WHERE id=5"
        ).fetchone()
        history = c.execute(
            "SELECT event_type, series_title, source_title FROM history"
            " WHERE event_type='series_soft_deleted'"
        ).fetchone()

    assert row["deleted_at"] is not None
    assert row["deletion_reason"] == "user_action"
    assert dict(history) == {
        "event_type": "series_soft_deleted",
        "series_title": "S5",
        "source_title": "S5",
    }


def test_api_v1_delete_series_requires_api_key(env):
    resp = _client().delete("/api/v1/series/5")
    assert resp.status_code == 401


def test_api_v1_delete_series_rejects_unknown_id(env):
    resp = _client().delete(
        "/api/v1/series/99999",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "series not found"


def test_api_v1_restore_series_clears_soft_delete_and_logs_history(env):
    client = _client()
    headers = {"X-Api-Key": _api_key(env)}

    delete_resp = client.delete("/api/v1/series/5", headers=headers)
    assert delete_resp.status_code == 200, delete_resp.text

    restore_resp = client.post("/api/v1/series/5/restore", headers=headers)
    assert restore_resp.status_code == 200, restore_resp.text
    assert restore_resp.json() == {"ok": True, "id": 5}

    detail = client.get("/api/v1/series/5", headers=headers)
    assert detail.status_code == 200, detail.text

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT deleted_at, deletion_reason FROM series WHERE id=5"
        ).fetchone()
        history = c.execute(
            "SELECT event_type, series_title, source_title FROM history"
            " WHERE event_type='series_restored'"
        ).fetchone()

    assert row["deleted_at"] is None
    assert row["deletion_reason"] is None
    assert dict(history) == {
        "event_type": "series_restored",
        "series_title": "S5",
        "source_title": "S5",
    }


def test_api_v1_restore_series_requires_api_key(env):
    resp = _client().post("/api/v1/series/5/restore")
    assert resp.status_code == 401


def test_api_v1_restore_series_rejects_unknown_id(env):
    resp = _client().post(
        "/api/v1/series/99999/restore",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "series not found"


def test_api_v1_create_root_folder_adds_row_and_can_default(env):
    resp = _client().post(
        "/api/v1/rootfolder",
        json={
            "path": "/library/new-root/",
            "label": "New Root",
            "isDefault": True,
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "created"
    assert body["rootFolder"]["path"] == "/library/new-root"
    assert body["rootFolder"]["label"] == "New Root"
    assert body["rootFolder"]["isDefault"] is True

    with sqlite3.connect(env) as c:
        rows = c.execute(
            "SELECT path, is_default FROM root_folders ORDER BY id"
        ).fetchall()
    assert rows == [
        ("/library/a", 0),
        ("/library/b", 0),
        ("/library/new-root", 1),
    ]


def test_api_v1_create_root_folder_rejects_blank_path(env):
    resp = _client().post(
        "/api/v1/rootfolder",
        json={"path": "   "},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "path is required"


def test_api_v1_create_root_folder_requires_api_key(env):
    resp = _client().post("/api/v1/rootfolder", json={"path": "/library/new"})
    assert resp.status_code == 401


def test_api_v1_set_default_root_folder_switches_default(env):
    resp = _client().post(
        "/api/v1/rootfolder/302/default",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["rootFolder"]["id"] == 302
    assert body["rootFolder"]["isDefault"] is True

    with sqlite3.connect(env) as c:
        rows = c.execute(
            "SELECT id, is_default FROM root_folders ORDER BY id"
        ).fetchall()
    assert rows == [(301, 0), (302, 1)]


def test_api_v1_set_default_root_folder_rejects_unknown_id(env):
    resp = _client().post(
        "/api/v1/rootfolder/99999/default",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "root folder not found"

    with sqlite3.connect(env) as c:
        rows = c.execute(
            "SELECT id, is_default FROM root_folders ORDER BY id"
        ).fetchall()
    assert rows == [(301, 1), (302, 0)]


def test_api_v1_set_default_root_folder_requires_api_key(env):
    resp = _client().post("/api/v1/rootfolder/302/default")
    assert resp.status_code == 401


def test_api_v1_delete_root_folder_removes_row_and_keeps_default(env):
    resp = _client().delete(
        "/api/v1/rootfolder/301",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "id": 301}

    with sqlite3.connect(env) as c:
        rows = c.execute(
            "SELECT id, is_default FROM root_folders ORDER BY id"
        ).fetchall()
    assert rows == [(302, 1)]


def test_api_v1_delete_root_folder_rejects_unknown_id(env):
    resp = _client().delete(
        "/api/v1/rootfolder/99999",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "root folder not found"


def test_api_v1_delete_root_folder_requires_api_key(env):
    resp = _client().delete("/api/v1/rootfolder/301")
    assert resp.status_code == 401


def test_api_v1_create_notification_adds_row_and_redacts_secret(env):
    from security import decrypt_secret

    resp = _client().post(
        "/api/v1/notification",
        json={
            "name": "API Discord",
            "implementation": "discord",
            "enable": False,
            "settings": {
                "webhook_url": "https://discord.example/webhook-secret",
                "avatar": "mangarr",
            },
            "onGrab": True,
            "onDownload": False,
            "onUpgrade": True,
            "onSeriesAdd": False,
            "onHealthIssue": True,
            "onHealthRestored": True,
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert "webhook-secret" not in resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "created"
    notification = body["notification"]
    assert notification["name"] == "API Discord"
    assert notification["implementation"] == "discord"
    assert notification["enable"] is False
    assert notification["settings"] == {"avatar": "mangarr"}
    assert notification["hasSecretSettings"] == {"webhook_url": True}
    assert notification["onGrab"] is True
    assert notification["onDownload"] is False
    assert notification["onUpgrade"] is True
    assert notification["onSeriesAdd"] is False
    assert notification["onHealthIssue"] is True
    assert notification["onHealthRestored"] is True
    notification_id = notification["id"]

    with sqlite3.connect(env) as c:
        row = c.execute(
            "SELECT settings, enabled, on_download FROM notification_connections"
            " WHERE id=?",
            (notification_id,),
        ).fetchone()
    settings = json.loads(row[0])
    assert settings["webhook_url"] != "https://discord.example/webhook-secret"
    assert decrypt_secret(settings["webhook_url"]) == (
        "https://discord.example/webhook-secret"
    )
    assert settings["avatar"] == "mangarr"
    assert row[1] == 0
    assert row[2] == 0


def test_api_v1_create_notification_requires_api_key(env):
    resp = _client().post(
        "/api/v1/notification",
        json={"name": "No Auth", "implementation": "discord"},
    )
    assert resp.status_code == 401


def test_api_v1_create_notification_rejects_bad_implementation(env):
    resp = _client().post(
        "/api/v1/notification",
        json={"name": "Bad", "implementation": "unsupported"},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "implementation is not supported"


def test_api_v1_read_notification_redacts_stored_secret(env):
    from security import encrypt_if_cipher_available

    settings = {
        "webhook_url": encrypt_if_cipher_available("https://hooks.secret"),
        "username": "bot",
    }
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO notification_connections"
            "(id, name, type, enabled, settings, on_grab, on_download,"
            " on_upgrade, on_series_add, on_health_issue, on_health_restored)"
            " VALUES(930, 'Read Discord', 'discord', 1, ?, 1, 1, 1, 1, 1, 0)",
            (json.dumps(settings),),
        )

    resp = _client().get(
        "/api/v1/notification",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert "hooks.secret" not in resp.text
    item = [entry for entry in resp.json() if entry["id"] == 930][0]
    assert item["settings"] == {"username": "bot"}
    assert item["hasSecretSettings"] == {"webhook_url": True}


def test_api_v1_update_notification_merges_settings_and_preserves_blank_secret(env):
    from security import decrypt_secret, encrypt_if_cipher_available

    old_secret = encrypt_if_cipher_available("https://old.secret/webhook")
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO notification_connections"
            "(id, name, type, enabled, settings, on_grab, on_download,"
            " on_upgrade, on_series_add, on_health_issue, on_health_restored)"
            " VALUES(931, 'Old Discord', 'discord', 1, ?, 1, 1, 1, 1, 1, 0)",
            (json.dumps({"webhook_url": old_secret, "username": "old"}),),
        )

    resp = _client().request(
        "PATCH",
        "/api/v1/notification/931",
        json={
            "name": "Updated Discord",
            "settings": {
                "webhook_url": "",
                "username": "new",
            },
            "onDownload": False,
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert "old.secret" not in resp.text
    notification = resp.json()["notification"]
    assert notification["name"] == "Updated Discord"
    assert notification["settings"] == {"username": "new"}
    assert notification["hasSecretSettings"] == {"webhook_url": True}
    assert notification["onDownload"] is False
    assert notification["onGrab"] is True

    with sqlite3.connect(env) as c:
        row = c.execute(
            "SELECT settings, on_download, on_grab FROM notification_connections"
            " WHERE id=931"
        ).fetchone()
    settings = json.loads(row[0])
    assert decrypt_secret(settings["webhook_url"]) == "https://old.secret/webhook"
    assert settings["username"] == "new"
    assert row[1] == 0
    assert row[2] == 1


def test_api_v1_update_notification_type_change_resets_settings(env):
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO notification_connections"
            "(id, name, type, settings)"
            " VALUES(932, 'Switch Type', 'discord', ?)",
            (json.dumps({"webhook_url": "plain-old", "username": "old"}),),
        )

    resp = _client().request(
        "PATCH",
        "/api/v1/notification/932",
        json={
            "implementation": "ntfy",
            "settings": {"server": "https://ntfy.sh", "topic": "mangarr"},
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    notification = resp.json()["notification"]
    assert notification["implementation"] == "ntfy"
    assert notification["settings"] == {
        "server": "https://ntfy.sh",
        "topic": "mangarr",
    }
    assert notification["hasSecretSettings"] == {}

    with sqlite3.connect(env) as c:
        settings = json.loads(
            c.execute(
                "SELECT settings FROM notification_connections WHERE id=932"
            ).fetchone()[0]
        )
    assert settings == {"server": "https://ntfy.sh", "topic": "mangarr"}


def test_api_v1_update_notification_rejects_unknown_id(env):
    resp = _client().request(
        "PATCH",
        "/api/v1/notification/99999",
        json={"name": "Missing"},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "notification connection not found"


def test_api_v1_delete_notification_removes_row(env):
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO notification_connections(id, name, type, settings)"
            " VALUES(933, 'Delete Notification', 'discord', '{}')"
        )

    resp = _client().delete(
        "/api/v1/notification/933",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "id": 933}

    with sqlite3.connect(env) as c:
        row = c.execute(
            "SELECT 1 FROM notification_connections WHERE id=933"
        ).fetchone()
    assert row is None


def test_api_v1_delete_notification_rejects_unknown_id(env):
    resp = _client().delete(
        "/api/v1/notification/99999",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "notification connection not found"


def test_api_v1_create_quality_profile_adds_row(env):
    resp = _client().post(
        "/api/v1/qualityprofile",
        json={
            "name": "API Quality",
            "qualities": ["cbz", "cbr"],
            "cutoff": "cbr",
            "upgradesAllowed": False,
            "minimumCustomFormatScore": 15,
            "cutoffFormatScore": 250,
            "minUpgradeFormatScore": 20,
            "isDefault": True,
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "created"
    assert body["qualityProfile"]["name"] == "API Quality"
    assert body["qualityProfile"]["qualities"] == ["cbz", "cbr"]
    assert body["qualityProfile"]["cutoff"] == "cbr"
    assert body["qualityProfile"]["upgradesAllowed"] is False
    assert body["qualityProfile"]["minimumCustomFormatScore"] == 15
    assert body["qualityProfile"]["cutoffFormatScore"] == 250
    assert body["qualityProfile"]["minUpgradeFormatScore"] == 20
    assert body["qualityProfile"]["isDefault"] is True

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT qualities, cutoff, upgrades_allowed,"
            " minimum_custom_format_score, cutoff_format_score,"
            " min_upgrade_format_score, is_default FROM quality_profiles"
            " WHERE name='API Quality'"
        ).fetchone()
    assert json.loads(row["qualities"]) == ["cbz", "cbr"]
    assert row["cutoff"] == "cbr"
    assert row["upgrades_allowed"] == 0
    assert row["minimum_custom_format_score"] == 15
    assert row["cutoff_format_score"] == 250
    assert row["min_upgrade_format_score"] == 20
    assert row["is_default"] == 1


def test_api_v1_create_quality_profile_requires_api_key(env):
    resp = _client().post(
        "/api/v1/qualityprofile",
        json={"name": "No Auth"},
    )
    assert resp.status_code == 401


def test_api_v1_create_quality_profile_rejects_bad_qualities(env):
    resp = _client().post(
        "/api/v1/qualityprofile",
        json={"name": "Bad Quality", "qualities": {"cbz": True}},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "qualities must be a list of quality names"


def test_api_v1_update_quality_profile_updates_submitted_fields(env):
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO quality_profiles(id, name, qualities, cutoff,"
            " upgrades_allowed, minimum_custom_format_score)"
            " VALUES(1001, 'Old API Quality', '[\"cbz\"]', 'cbz', 1, 0)"
        )

    resp = _client().request(
        "PATCH",
        "/api/v1/qualityprofile/1001",
        json={
            "name": "Updated API Quality",
            "qualities": ["cbz", "epub"],
            "cutoff": "epub",
            "upgradesAllowed": False,
            "minimumCustomFormatScore": 7,
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["qualityProfile"]["name"] == "Updated API Quality"
    assert body["qualityProfile"]["qualities"] == ["cbz", "epub"]
    assert body["qualityProfile"]["cutoff"] == "epub"
    assert body["qualityProfile"]["upgradesAllowed"] is False
    assert body["qualityProfile"]["minimumCustomFormatScore"] == 7

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT name, qualities, cutoff, upgrades_allowed,"
            " minimum_custom_format_score FROM quality_profiles WHERE id=1001"
        ).fetchone()
    assert row["name"] == "Updated API Quality"
    assert json.loads(row["qualities"]) == ["cbz", "epub"]
    assert row["cutoff"] == "epub"
    assert row["upgrades_allowed"] == 0
    assert row["minimum_custom_format_score"] == 7


def test_api_v1_update_quality_profile_rejects_unknown_id(env):
    resp = _client().request(
        "PATCH",
        "/api/v1/qualityprofile/99999",
        json={"name": "Missing"},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "quality profile not found"


def test_api_v1_set_default_quality_profile_is_unique(env):
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO quality_profiles(id, name, qualities, is_default)"
            " VALUES(1010, 'Default A', '[]', 1)"
        )
        c.execute(
            "INSERT INTO quality_profiles(id, name, qualities, is_default)"
            " VALUES(1011, 'Default B', '[]', 0)"
        )

    resp = _client().post(
        "/api/v1/qualityprofile/1011/default",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["qualityProfile"]["isDefault"] is True

    with sqlite3.connect(env) as c:
        rows = c.execute(
            "SELECT id, is_default FROM quality_profiles"
            " WHERE id IN (1010, 1011) ORDER BY id"
        ).fetchall()
    assert rows == [(1010, 0), (1011, 1)]


def test_api_v1_delete_quality_profile_clears_series_references(env):
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO quality_profiles(id, name, qualities)"
            " VALUES(1020, 'Delete API Quality', '[]')"
        )
        c.execute("UPDATE series SET quality_profile_id=1020 WHERE id=5")

    resp = _client().delete(
        "/api/v1/qualityprofile/1020",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "id": 1020}

    with sqlite3.connect(env) as c:
        profile = c.execute(
            "SELECT 1 FROM quality_profiles WHERE id=1020"
        ).fetchone()
        series_profile = c.execute(
            "SELECT quality_profile_id FROM series WHERE id=5"
        ).fetchone()[0]
    assert profile is None
    assert series_profile is None


def test_api_v1_delete_quality_profile_rejects_unknown_id(env):
    resp = _client().delete(
        "/api/v1/qualityprofile/99999",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "quality profile not found"


def test_api_v1_create_language_profile_adds_row_and_can_default(env):
    resp = _client().post(
        "/api/v1/languageprofile",
        json={
            "name": "API Languages",
            "languages": ["en", "ja", "bogus"],
            "allowAny": False,
            "isDefault": True,
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "created"
    assert body["languageProfile"]["name"] == "API Languages"
    assert body["languageProfile"]["languages"] == ["en", "ja"]
    assert body["languageProfile"]["allowAny"] is False
    assert body["languageProfile"]["isDefault"] is True
    profile_id = body["languageProfile"]["id"]

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT languages, allow_any FROM language_profiles WHERE id=?",
            (profile_id,),
        ).fetchone()
        default_row = c.execute(
            "SELECT value FROM settings WHERE key='default_language_profile_id'"
        ).fetchone()
    assert json.loads(row["languages"]) == ["en", "ja"]
    assert row["allow_any"] == 0
    assert default_row["value"] == str(profile_id)


def test_api_v1_create_language_profile_requires_api_key(env):
    resp = _client().post(
        "/api/v1/languageprofile",
        json={"name": "No Auth"},
    )
    assert resp.status_code == 401


def test_api_v1_create_language_profile_rejects_bad_languages(env):
    resp = _client().post(
        "/api/v1/languageprofile",
        json={"name": "Bad Languages", "languages": {"en": True}},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "languages must be a list of language codes"


def test_api_v1_update_language_profile_updates_submitted_fields(env):
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO language_profiles(id, name, languages, allow_any)"
            " VALUES(1101, 'Old API Languages', '[\"en\"]', 0)"
        )

    resp = _client().request(
        "PATCH",
        "/api/v1/languageprofile/1101",
        json={
            "name": "Updated API Languages",
            "languages": "en,ja,invalid",
            "allowAny": True,
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["languageProfile"]["name"] == "Updated API Languages"
    assert body["languageProfile"]["languages"] == ["en", "ja"]
    assert body["languageProfile"]["allowAny"] is True

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT name, languages, allow_any FROM language_profiles WHERE id=1101"
        ).fetchone()
    assert row["name"] == "Updated API Languages"
    assert json.loads(row["languages"]) == ["en", "ja"]
    assert row["allow_any"] == 1


def test_api_v1_update_language_profile_rejects_unknown_id(env):
    resp = _client().request(
        "PATCH",
        "/api/v1/languageprofile/99999",
        json={"name": "Missing"},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "language profile not found"


def test_api_v1_set_default_language_profile_updates_setting(env):
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO language_profiles(id, name, languages)"
            " VALUES(1110, 'Language Default A', '[\"en\"]')"
        )
        c.execute(
            "INSERT INTO language_profiles(id, name, languages)"
            " VALUES(1111, 'Language Default B', '[\"ja\"]')"
        )
        c.execute(
            "INSERT OR REPLACE INTO settings(key, value)"
            " VALUES('default_language_profile_id', '1110')"
        )

    resp = _client().post(
        "/api/v1/languageprofile/1111/default",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["languageProfile"]["isDefault"] is True

    with sqlite3.connect(env) as c:
        default_row = c.execute(
            "SELECT value FROM settings WHERE key='default_language_profile_id'"
        ).fetchone()
    assert default_row[0] == "1111"


def test_api_v1_delete_language_profile_removes_unused_and_clears_default(env):
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO language_profiles(id, name, languages)"
            " VALUES(1120, 'Delete API Languages', '[\"en\"]')"
        )
        c.execute(
            "INSERT OR REPLACE INTO settings(key, value)"
            " VALUES('default_language_profile_id', '1120')"
        )

    resp = _client().delete(
        "/api/v1/languageprofile/1120",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "id": 1120}

    with sqlite3.connect(env) as c:
        profile = c.execute(
            "SELECT 1 FROM language_profiles WHERE id=1120"
        ).fetchone()
        default_row = c.execute(
            "SELECT value FROM settings WHERE key='default_language_profile_id'"
        ).fetchone()
    assert profile is None
    assert default_row is None


def test_api_v1_delete_language_profile_blocks_in_use(env):
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO language_profiles(id, name, languages)"
            " VALUES(1121, 'Used API Languages', '[\"en\"]')"
        )
        c.execute("UPDATE series SET language_profile_id=1121 WHERE id=5")

    resp = _client().delete(
        "/api/v1/languageprofile/1121",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "language profile is in use"

    with sqlite3.connect(env) as c:
        profile = c.execute(
            "SELECT 1 FROM language_profiles WHERE id=1121"
        ).fetchone()
    assert profile is not None


def test_api_v1_delete_language_profile_rejects_unknown_id(env):
    resp = _client().delete(
        "/api/v1/languageprofile/99999",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "language profile not found"


def test_api_v1_create_custom_format_adds_row_and_scores(env):
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO quality_profiles(id, name, qualities)"
            " VALUES(1400, 'API CF Quality', '[\"cbz\"]')"
        )

    resp = _client().post(
        "/api/v1/customformat",
        json={
            "name": "API Custom Format",
            "specifications": [
                {
                    "name": "official",
                    "implementation": "source_is",
                    "value": "official_digital",
                }
            ],
            "includeCustomFormatWhenRenaming": True,
            "qualityProfileScores": [
                {"qualityProfileId": 1400, "score": 50},
            ],
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "created"
    assert body["customFormat"]["name"] == "API Custom Format"
    assert body["customFormat"]["specifications"] == [
        {
            "name": "official",
            "implementation": "source_is",
            "value": "official_digital",
        }
    ]
    assert body["customFormat"]["includeCustomFormatWhenRenaming"] is True
    assert body["customFormat"]["qualityProfileScores"] == [
        {"qualityProfileId": 1400, "score": 50}
    ]
    format_id = body["customFormat"]["id"]

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT specifications, include_custom_format_when_renaming"
            " FROM custom_formats WHERE id=?",
            (format_id,),
        ).fetchone()
        score = c.execute(
            "SELECT score FROM quality_profile_custom_formats"
            " WHERE profile_id=1400 AND format_id=?",
            (format_id,),
        ).fetchone()
    assert json.loads(row["specifications"]) == [
        {
            "name": "official",
            "implementation": "source_is",
            "value": "official_digital",
        }
    ]
    assert row["include_custom_format_when_renaming"] == 1
    assert score["score"] == 50


def test_api_v1_create_custom_format_requires_api_key(env):
    resp = _client().post(
        "/api/v1/customformat",
        json={"name": "No Auth"},
    )
    assert resp.status_code == 401


def test_api_v1_create_custom_format_rejects_bad_specifications(env):
    resp = _client().post(
        "/api/v1/customformat",
        json={"name": "Bad Specs", "specifications": {"type": "source_is"}},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "specifications must be a list"


def test_api_v1_create_custom_format_rejects_unknown_quality_profile(env):
    resp = _client().post(
        "/api/v1/customformat",
        json={
            "name": "Bad Score Profile",
            "qualityProfileScores": [{"qualityProfileId": 99999, "score": 10}],
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "qualityProfileId 99999 not found"

    with sqlite3.connect(env) as c:
        count = c.execute(
            "SELECT COUNT(*) FROM custom_formats WHERE name='Bad Score Profile'"
        ).fetchone()[0]
    assert count == 0


def test_api_v1_update_custom_format_updates_submitted_fields_and_scores(env):
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO quality_profiles(id, name, qualities)"
            " VALUES(1410, 'API CF Quality A', '[\"cbz\"]')"
        )
        c.execute(
            "INSERT INTO quality_profiles(id, name, qualities)"
            " VALUES(1411, 'API CF Quality B', '[\"epub\"]')"
        )
        c.execute(
            "INSERT INTO custom_formats"
            "(id, name, specifications, include_custom_format_when_renaming)"
            " VALUES(1420, 'Old API Custom Format', '[]', 0)"
        )
        c.execute(
            "INSERT INTO quality_profile_custom_formats"
            "(profile_id, format_id, score) VALUES(1410, 1420, 5)"
        )

    resp = _client().request(
        "PATCH",
        "/api/v1/customformat/1420",
        json={
            "name": "Updated API Custom Format",
            "specifications": [{"type": "release_title_contains", "value": "Deluxe"}],
            "includeCustomFormatWhenRenaming": True,
            "qualityProfileScores": [
                {"qualityProfileId": 1410, "score": 0},
                {"qualityProfileId": 1411, "score": 20},
            ],
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["customFormat"]["name"] == "Updated API Custom Format"
    assert body["customFormat"]["specifications"] == [
        {"type": "release_title_contains", "value": "Deluxe"}
    ]
    assert body["customFormat"]["includeCustomFormatWhenRenaming"] is True
    assert body["customFormat"]["qualityProfileScores"] == [
        {"qualityProfileId": 1411, "score": 20}
    ]

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT name, specifications, include_custom_format_when_renaming"
            " FROM custom_formats WHERE id=1420"
        ).fetchone()
        scores = c.execute(
            "SELECT profile_id, score FROM quality_profile_custom_formats"
            " WHERE format_id=1420 ORDER BY profile_id"
        ).fetchall()
    assert row["name"] == "Updated API Custom Format"
    assert json.loads(row["specifications"]) == [
        {"type": "release_title_contains", "value": "Deluxe"}
    ]
    assert row["include_custom_format_when_renaming"] == 1
    assert [tuple(score) for score in scores] == [(1411, 20)]


def test_api_v1_update_custom_format_rejects_unknown_id(env):
    resp = _client().request(
        "PATCH",
        "/api/v1/customformat/99999",
        json={"name": "Missing"},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "custom format not found"


def test_api_v1_delete_custom_format_removes_row_and_scores(env):
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO quality_profiles(id, name, qualities)"
            " VALUES(1430, 'API CF Delete Quality', '[\"cbz\"]')"
        )
        c.execute(
            "INSERT INTO custom_formats(id, name, specifications)"
            " VALUES(1431, 'Delete API Custom Format', '[]')"
        )
        c.execute(
            "INSERT INTO quality_profile_custom_formats"
            "(profile_id, format_id, score) VALUES(1430, 1431, 15)"
        )

    resp = _client().delete(
        "/api/v1/customformat/1431",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "id": 1431}

    with sqlite3.connect(env) as c:
        custom_format = c.execute(
            "SELECT 1 FROM custom_formats WHERE id=1431"
        ).fetchone()
        score = c.execute(
            "SELECT 1 FROM quality_profile_custom_formats WHERE format_id=1431"
        ).fetchone()
    assert custom_format is None
    assert score is None


def test_api_v1_delete_custom_format_rejects_unknown_id(env):
    resp = _client().delete(
        "/api/v1/customformat/99999",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "custom format not found"


def test_api_v1_create_release_profile_adds_row_and_tags(env):
    resp = _client().post(
        "/api/v1/releaseprofile",
        json={
            "name": "API Release",
            "enabled": False,
            "required": "group",
            "ignored": "raw",
            "preferred": [{"term": "deluxe", "score": 25}],
            "includePreferredWhenRenaming": True,
            "tags": ["favorite", "favorite", "owned"],
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "created"
    assert body["releaseProfile"]["name"] == "API Release"
    assert body["releaseProfile"]["enabled"] is False
    assert body["releaseProfile"]["required"] == "group"
    assert body["releaseProfile"]["ignored"] == "raw"
    assert body["releaseProfile"]["preferred"] == [
        {"term": "deluxe", "score": 25}
    ]
    assert body["releaseProfile"]["includePreferredWhenRenaming"] is True
    assert body["releaseProfile"]["tags"] == ["favorite", "owned"]
    profile_id = body["releaseProfile"]["id"]

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT enabled, required, ignored, preferred,"
            " include_preferred_when_renaming FROM release_profiles WHERE id=?",
            (profile_id,),
        ).fetchone()
        tags = [
            tag[0]
            for tag in c.execute(
                "SELECT tag FROM release_profile_tags"
                " WHERE profile_id=? ORDER BY tag",
                (profile_id,),
            )
        ]
    assert row["enabled"] == 0
    assert row["required"] == "group"
    assert row["ignored"] == "raw"
    assert json.loads(row["preferred"]) == [{"term": "deluxe", "score": 25}]
    assert row["include_preferred_when_renaming"] == 1
    assert tags == ["favorite", "owned"]


def test_api_v1_create_release_profile_requires_api_key(env):
    resp = _client().post(
        "/api/v1/releaseprofile",
        json={"name": "No Auth"},
    )
    assert resp.status_code == 401


def test_api_v1_create_release_profile_rejects_bad_preferred(env):
    resp = _client().post(
        "/api/v1/releaseprofile",
        json={"name": "Bad Preferred", "preferred": {"term": "deluxe"}},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "preferred must be a list"


def test_api_v1_update_release_profile_updates_submitted_fields_and_tags(env):
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO release_profiles"
            "(id, name, enabled, required, ignored, preferred,"
            " include_preferred_when_renaming)"
            " VALUES(1201, 'Old API Release', 1, 'old', 'raw',"
            " '[{\"term\":\"old\",\"score\":1}]', 0)"
        )
        c.execute(
            "INSERT INTO release_profile_tags(profile_id, tag)"
            " VALUES(1201, 'old-tag')"
        )

    resp = _client().request(
        "PATCH",
        "/api/v1/releaseprofile/1201",
        json={
            "name": "Updated API Release",
            "enabled": False,
            "preferred": [{"term": "new", "score": 10}],
            "includePreferredWhenRenaming": True,
            "tags": "favorite,owned",
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["releaseProfile"]["name"] == "Updated API Release"
    assert body["releaseProfile"]["enabled"] is False
    assert body["releaseProfile"]["required"] == "old"
    assert body["releaseProfile"]["ignored"] == "raw"
    assert body["releaseProfile"]["preferred"] == [{"term": "new", "score": 10}]
    assert body["releaseProfile"]["includePreferredWhenRenaming"] is True
    assert body["releaseProfile"]["tags"] == ["favorite", "owned"]

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT name, enabled, required, ignored, preferred,"
            " include_preferred_when_renaming FROM release_profiles WHERE id=1201"
        ).fetchone()
        tags = [
            tag[0]
            for tag in c.execute(
                "SELECT tag FROM release_profile_tags"
                " WHERE profile_id=1201 ORDER BY tag"
            )
        ]
    assert row["name"] == "Updated API Release"
    assert row["enabled"] == 0
    assert row["required"] == "old"
    assert row["ignored"] == "raw"
    assert json.loads(row["preferred"]) == [{"term": "new", "score": 10}]
    assert row["include_preferred_when_renaming"] == 1
    assert tags == ["favorite", "owned"]


def test_api_v1_update_release_profile_rejects_unknown_id(env):
    resp = _client().request(
        "PATCH",
        "/api/v1/releaseprofile/99999",
        json={"name": "Missing"},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "release profile not found"


def test_api_v1_delete_release_profile_removes_row_and_tags(env):
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO release_profiles(id, name)"
            " VALUES(1210, 'Delete API Release')"
        )
        c.execute(
            "INSERT INTO release_profile_tags(profile_id, tag)"
            " VALUES(1210, 'favorite')"
        )

    resp = _client().delete(
        "/api/v1/releaseprofile/1210",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "id": 1210}

    with sqlite3.connect(env) as c:
        profile = c.execute(
            "SELECT 1 FROM release_profiles WHERE id=1210"
        ).fetchone()
        tag = c.execute(
            "SELECT 1 FROM release_profile_tags WHERE profile_id=1210"
        ).fetchone()
    assert profile is None
    assert tag is None


def test_api_v1_delete_release_profile_rejects_unknown_id(env):
    resp = _client().delete(
        "/api/v1/releaseprofile/99999",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "release profile not found"


def test_api_v1_create_delay_profile_adds_row_and_tags(env):
    resp = _client().post(
        "/api/v1/delayprofile",
        json={
            "name": "API Delay",
            "enableUsenet": False,
            "enableTorrent": True,
            "usenetDelay": 0,
            "torrentDelay": 45,
            "bypassIfHighestQuality": True,
            "isDefault": True,
            "tags": ["favorite", "favorite", "owned"],
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "created"
    assert body["delayProfile"]["name"] == "API Delay"
    assert body["delayProfile"]["enableUsenet"] is False
    assert body["delayProfile"]["enableTorrent"] is True
    assert body["delayProfile"]["usenetDelay"] == 0
    assert body["delayProfile"]["torrentDelay"] == 45
    assert body["delayProfile"]["bypassIfHighestQuality"] is True
    assert body["delayProfile"]["isDefault"] is True
    assert body["delayProfile"]["tags"] == ["favorite", "owned"]
    profile_id = body["delayProfile"]["id"]

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT enable_usenet, enable_torrent, usenet_delay,"
            " torrent_delay, bypass_if_highest_quality, is_default"
            " FROM delay_profiles WHERE id=?",
            (profile_id,),
        ).fetchone()
        tags = [
            tag[0]
            for tag in c.execute(
                "SELECT tag FROM delay_profile_tags"
                " WHERE profile_id=? ORDER BY tag",
                (profile_id,),
            )
        ]
    assert row["enable_usenet"] == 0
    assert row["enable_torrent"] == 1
    assert row["usenet_delay"] == 0
    assert row["torrent_delay"] == 45
    assert row["bypass_if_highest_quality"] == 1
    assert row["is_default"] == 1
    assert tags == ["favorite", "owned"]


def test_api_v1_create_delay_profile_requires_api_key(env):
    resp = _client().post(
        "/api/v1/delayprofile",
        json={"name": "No Auth"},
    )
    assert resp.status_code == 401


def test_api_v1_create_delay_profile_rejects_negative_delay(env):
    resp = _client().post(
        "/api/v1/delayprofile",
        json={"name": "Bad Delay", "torrentDelay": -1},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "torrentDelay must be zero or a positive integer"


def test_api_v1_update_delay_profile_updates_submitted_fields_and_tags(env):
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO delay_profiles"
            "(id, name, order_num, enable_usenet, enable_torrent,"
            " usenet_delay, torrent_delay, bypass_if_highest_quality,"
            " is_default)"
            " VALUES(1301, 'Old API Delay', 4, 1, 1, 5, 10, 0, 0)"
        )
        c.execute(
            "INSERT INTO delay_profile_tags(profile_id, tag)"
            " VALUES(1301, 'old-tag')"
        )

    resp = _client().request(
        "PATCH",
        "/api/v1/delayprofile/1301",
        json={
            "name": "Updated API Delay",
            "order": 2,
            "enableUsenet": False,
            "torrentDelay": 30,
            "bypassIfHighestQuality": True,
            "tags": "favorite,owned",
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["delayProfile"]["name"] == "Updated API Delay"
    assert body["delayProfile"]["order"] == 2
    assert body["delayProfile"]["enableUsenet"] is False
    assert body["delayProfile"]["enableTorrent"] is True
    assert body["delayProfile"]["usenetDelay"] == 5
    assert body["delayProfile"]["torrentDelay"] == 30
    assert body["delayProfile"]["bypassIfHighestQuality"] is True
    assert body["delayProfile"]["tags"] == ["favorite", "owned"]

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT name, order_num, enable_usenet, enable_torrent,"
            " usenet_delay, torrent_delay, bypass_if_highest_quality"
            " FROM delay_profiles WHERE id=1301"
        ).fetchone()
        tags = [
            tag[0]
            for tag in c.execute(
                "SELECT tag FROM delay_profile_tags"
                " WHERE profile_id=1301 ORDER BY tag"
            )
        ]
    assert row["name"] == "Updated API Delay"
    assert row["order_num"] == 2
    assert row["enable_usenet"] == 0
    assert row["enable_torrent"] == 1
    assert row["usenet_delay"] == 5
    assert row["torrent_delay"] == 30
    assert row["bypass_if_highest_quality"] == 1
    assert tags == ["favorite", "owned"]


def test_api_v1_update_delay_profile_rejects_unknown_id(env):
    resp = _client().request(
        "PATCH",
        "/api/v1/delayprofile/99999",
        json={"name": "Missing"},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "delay profile not found"


def test_api_v1_delete_delay_profile_removes_row_and_tags(env):
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO delay_profiles(id, name, order_num, is_default)"
            " VALUES(1310, 'Delete API Delay', 5, 0)"
        )
        c.execute(
            "INSERT INTO delay_profile_tags(profile_id, tag)"
            " VALUES(1310, 'favorite')"
        )

    resp = _client().delete(
        "/api/v1/delayprofile/1310",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "id": 1310}

    with sqlite3.connect(env) as c:
        profile = c.execute(
            "SELECT 1 FROM delay_profiles WHERE id=1310"
        ).fetchone()
        tag = c.execute(
            "SELECT 1 FROM delay_profile_tags WHERE profile_id=1310"
        ).fetchone()
    assert profile is None
    assert tag is None


def test_api_v1_delete_delay_profile_blocks_default(env):
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO delay_profiles(id, name, order_num, is_default)"
            " VALUES(1320, 'Default API Delay', 0, 1)"
        )

    resp = _client().delete(
        "/api/v1/delayprofile/1320",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "Cannot delete the default delay profile"

    with sqlite3.connect(env) as c:
        profile = c.execute(
            "SELECT 1 FROM delay_profiles WHERE id=1320"
        ).fetchone()
    assert profile is not None


def test_api_v1_delete_delay_profile_rejects_unknown_id(env):
    resp = _client().delete(
        "/api/v1/delayprofile/99999",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "delay profile not found"


def test_api_v1_create_import_list_adds_row(env):
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO quality_profiles(id, name, qualities)"
            " VALUES(1600, 'API Import Quality', '[\"cbz\"]')"
        )

    resp = _client().post(
        "/api/v1/importlist",
        json={
            "name": "API Import List",
            "implementation": "anilist_user",
            "enable": False,
            "qualityProfileId": 1600,
            "rootFolderId": 302,
            "monitorMode": "missing",
            "settings": {"username": "vinland"},
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "created"
    assert body["importList"]["name"] == "API Import List"
    assert body["importList"]["implementation"] == "anilist_user"
    assert body["importList"]["enable"] is False
    assert body["importList"]["qualityProfileId"] == 1600
    assert body["importList"]["rootFolderId"] == 302
    assert body["importList"]["monitorMode"] == "missing"
    assert body["importList"]["settings"] == {"username": "vinland"}
    list_id = body["importList"]["id"]

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT type, enabled, quality_profile_id, root_folder_id,"
            " monitor_mode, settings FROM import_lists WHERE id=?",
            (list_id,),
        ).fetchone()
    assert row["type"] == "anilist_user"
    assert row["enabled"] == 0
    assert row["quality_profile_id"] == 1600
    assert row["root_folder_id"] == 302
    assert row["monitor_mode"] == "missing"
    assert json.loads(row["settings"]) == {"username": "vinland"}


def test_api_v1_create_import_list_requires_api_key(env):
    resp = _client().post(
        "/api/v1/importlist",
        json={"name": "No Auth", "implementation": "anilist_user"},
    )
    assert resp.status_code == 401


def test_api_v1_create_import_list_rejects_unknown_root_folder(env):
    resp = _client().post(
        "/api/v1/importlist",
        json={
            "name": "Bad Root",
            "implementation": "anilist_user",
            "rootFolderId": 99999,
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "rootFolderId not found"

    with sqlite3.connect(env) as c:
        count = c.execute(
            "SELECT COUNT(*) FROM import_lists WHERE name='Bad Root'"
        ).fetchone()[0]
    assert count == 0


def test_api_v1_create_import_list_rejects_bad_settings(env):
    resp = _client().post(
        "/api/v1/importlist",
        json={
            "name": "Bad Settings",
            "implementation": "anilist_user",
            "settings": ["not", "an", "object"],
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "settings must be an object"


def test_api_v1_update_import_list_updates_submitted_fields(env):
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO quality_profiles(id, name, qualities)"
            " VALUES(1610, 'Old Import Quality', '[\"cbz\"]')"
        )
        c.execute(
            "INSERT INTO quality_profiles(id, name, qualities)"
            " VALUES(1611, 'New Import Quality', '[\"epub\"]')"
        )
        c.execute(
            "INSERT INTO import_lists"
            "(id, name, type, enabled, quality_profile_id, root_folder_id,"
            " monitor_mode, settings)"
            " VALUES(1620, 'Old Import List', 'anilist_user', 1,"
            " 1610, 301, 'all', '{\"username\":\"old\"}')"
        )

    resp = _client().request(
        "PATCH",
        "/api/v1/importlist/1620",
        json={
            "name": "Updated Import List",
            "implementation": "custom_rss",
            "enable": False,
            "qualityProfileId": 1611,
            "rootFolderId": 302,
            "monitorMode": "missing",
            "settings": {"url": "https://example.invalid/feed.xml"},
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["importList"]["name"] == "Updated Import List"
    assert body["importList"]["implementation"] == "custom_rss"
    assert body["importList"]["enable"] is False
    assert body["importList"]["qualityProfileId"] == 1611
    assert body["importList"]["rootFolderId"] == 302
    assert body["importList"]["monitorMode"] == "missing"
    assert body["importList"]["settings"] == {
        "url": "https://example.invalid/feed.xml"
    }

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT name, type, enabled, quality_profile_id, root_folder_id,"
            " monitor_mode, settings FROM import_lists WHERE id=1620"
        ).fetchone()
    assert row["name"] == "Updated Import List"
    assert row["type"] == "custom_rss"
    assert row["enabled"] == 0
    assert row["quality_profile_id"] == 1611
    assert row["root_folder_id"] == 302
    assert row["monitor_mode"] == "missing"
    assert json.loads(row["settings"]) == {
        "url": "https://example.invalid/feed.xml"
    }


def test_api_v1_update_import_list_can_clear_optional_fks(env):
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO quality_profiles(id, name, qualities)"
            " VALUES(1630, 'Clear Import Quality', '[\"cbz\"]')"
        )
        c.execute(
            "INSERT INTO import_lists"
            "(id, name, type, quality_profile_id, root_folder_id, settings)"
            " VALUES(1631, 'Clear Import List', 'anilist_user',"
            " 1630, 301, '{}')"
        )

    resp = _client().request(
        "PATCH",
        "/api/v1/importlist/1631",
        json={"qualityProfileId": "", "rootFolderId": ""},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["importList"]["qualityProfileId"] is None
    assert resp.json()["importList"]["rootFolderId"] is None

    with sqlite3.connect(env) as c:
        row = c.execute(
            "SELECT quality_profile_id, root_folder_id"
            " FROM import_lists WHERE id=1631"
        ).fetchone()
    assert row == (None, None)


def test_api_v1_update_import_list_rejects_unknown_id(env):
    resp = _client().request(
        "PATCH",
        "/api/v1/importlist/99999",
        json={"name": "Missing"},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "import list not found"


def test_api_v1_sync_import_lists_schedules_background_task(env, monkeypatch):
    import main

    scheduled: list[str] = []

    def fake_create_background_task(coro, name: str):
        scheduled.append(name)
        coro.close()

    monkeypatch.setattr(main, "create_background_task", fake_create_background_task)

    resp = _client().post(
        "/api/v1/importlist/sync",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "ok": True,
        "message": "Sync started in background",
    }
    assert scheduled == ["import_lists:sync_all"]


def test_api_v1_sync_import_list_schedules_background_task(env, monkeypatch):
    import main

    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO import_lists(id, name, type, settings)"
            " VALUES(1640, 'Sync Import List', 'anilist_user', '{}')"
        )
    scheduled: list[str] = []

    def fake_create_background_task(coro, name: str):
        scheduled.append(name)
        coro.close()

    monkeypatch.setattr(main, "create_background_task", fake_create_background_task)

    resp = _client().post(
        "/api/v1/importlist/1640/sync",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "ok": True,
        "message": "Sync started for Sync Import List",
    }
    assert scheduled == ["import_lists:sync:1640"]


def test_api_v1_sync_import_list_rejects_unknown_id(env):
    resp = _client().post(
        "/api/v1/importlist/99999/sync",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "import list not found"


def test_api_v1_delete_import_list_removes_row(env):
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO import_lists(id, name, type, settings)"
            " VALUES(1650, 'Delete Import List', 'anilist_user', '{}')"
        )

    resp = _client().delete(
        "/api/v1/importlist/1650",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "id": 1650}

    with sqlite3.connect(env) as c:
        row = c.execute("SELECT 1 FROM import_lists WHERE id=1650").fetchone()
    assert row is None


def test_api_v1_delete_import_list_rejects_unknown_id(env):
    resp = _client().delete(
        "/api/v1/importlist/99999",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "import list not found"


def test_api_v1_create_indexer_adds_row_tags_and_secret(env):
    from security import decrypt_secret

    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO download_clients(id, name, type)"
            " VALUES(1710, 'API Client', 'qbittorrent')"
        )

    resp = _client().post(
        "/api/v1/indexer",
        json={
            "name": "API Nyaa",
            "implementation": "torznab",
            "baseUrl": "https://nyaa.example/torznab",
            "apiKey": "INDEXER-SECRET",
            "priority": 11,
            "enable": False,
            "categories": [7000, "7010", 7010, 7020],
            "settings": {"animeStandardFormatSearch": True},
            "downloadClientId": 1710,
            "minimumSeeders": 4,
            "seedRatio": 2.5,
            "parentProwlarrId": 22,
            "prowlarrIndexerId": 33,
            "enableRss": False,
            "enableAutomaticSearch": True,
            "enableInteractiveSearch": False,
            "minimumSize": 10,
            "maximumSize": 500,
            "tags": ["private", "private", "owned"],
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert "INDEXER-SECRET" not in resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "created"
    indexer = body["indexer"]
    assert indexer["name"] == "API Nyaa"
    assert indexer["implementation"] == "torznab"
    assert indexer["baseUrl"] == "https://nyaa.example/torznab"
    assert indexer["hasApiKey"] is True
    assert indexer["priority"] == 11
    assert indexer["enable"] is False
    assert indexer["categories"] == [7000, 7010, 7020]
    assert indexer["settings"] == {"animeStandardFormatSearch": True}
    assert indexer["downloadClientId"] == 1710
    assert indexer["minimumSeeders"] == 4
    assert indexer["seedRatio"] == 2.5
    assert indexer["parentProwlarrId"] == 22
    assert indexer["prowlarrIndexerId"] == 33
    assert indexer["enableRss"] is False
    assert indexer["enableAutomaticSearch"] is True
    assert indexer["enableInteractiveSearch"] is False
    assert indexer["minimumSize"] == 10
    assert indexer["maximumSize"] == 500
    assert indexer["tags"] == ["owned", "private"]
    indexer_id = indexer["id"]

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT api_key, enabled, categories, use_rss,"
            " use_interactive_search FROM indexers WHERE id=?",
            (indexer_id,),
        ).fetchone()
        tags = [
            tag[0]
            for tag in c.execute(
                "SELECT tag FROM indexer_tags WHERE indexer_id=? ORDER BY tag",
                (indexer_id,),
            )
        ]
    assert row["api_key"] != "INDEXER-SECRET"
    assert decrypt_secret(row["api_key"]) == "INDEXER-SECRET"
    assert row["enabled"] == 0
    assert json.loads(row["categories"]) == [7000, 7010, 7020]
    assert row["use_rss"] == 0
    assert row["use_interactive_search"] == 0
    assert tags == ["owned", "private"]


def test_api_v1_create_indexer_requires_api_key(env):
    resp = _client().post(
        "/api/v1/indexer",
        json={"name": "No Auth", "implementation": "torznab"},
    )
    assert resp.status_code == 401


def test_api_v1_create_indexer_rejects_bad_categories(env):
    resp = _client().post(
        "/api/v1/indexer",
        json={
            "name": "Bad Categories",
            "implementation": "torznab",
            "categories": ["books"],
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "categories entries must be category ids"


def test_api_v1_create_indexer_rejects_unknown_download_client(env):
    resp = _client().post(
        "/api/v1/indexer",
        json={
            "name": "Bad Client",
            "implementation": "torznab",
            "downloadClientId": 99999,
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "downloadClientId not found"


def test_api_v1_update_indexer_updates_submitted_fields_tags_and_secret(env):
    from security import decrypt_secret, encrypt_if_cipher_available

    old_secret = encrypt_if_cipher_available("OLD-INDEXER-SECRET")
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO download_clients(id, name, type)"
            " VALUES(1720, 'Updated Client', 'qbittorrent')"
        )
        c.execute(
            "INSERT INTO indexers"
            "(id, name, type, url, api_key, priority, enabled, categories,"
            " settings, min_seeders, seed_ratio, use_rss,"
            " use_auto_search, use_interactive_search, min_size_mb,"
            " max_size_mb)"
            " VALUES(1730, 'Old Indexer', 'prowlarr', 'https://old', ?,"
            " 25, 1, '[7000]', '{}', 0, 0, 1, 1, 1, 0, 0)",
            (old_secret,),
        )
        c.execute(
            "INSERT INTO indexer_tags(indexer_id, tag)"
            " VALUES(1730, 'old-tag')"
        )

    resp = _client().request(
        "PATCH",
        "/api/v1/indexer/1730",
        json={
            "name": "Updated Indexer",
            "implementation": "torznab",
            "baseUrl": "https://new",
            "apiKey": "NEW-INDEXER-SECRET",
            "priority": 3,
            "enable": False,
            "categories": "7000,7010",
            "settings": {"search": True},
            "downloadClientId": 1720,
            "minimumSeeders": 6,
            "seedRatio": "1.5",
            "enableRss": False,
            "enableAutomaticSearch": False,
            "enableInteractiveSearch": True,
            "minimumSize": 20,
            "maximumSize": 900,
            "tags": "private,owned",
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert "NEW-INDEXER-SECRET" not in resp.text
    indexer = resp.json()["indexer"]
    assert indexer["name"] == "Updated Indexer"
    assert indexer["implementation"] == "torznab"
    assert indexer["baseUrl"] == "https://new"
    assert indexer["priority"] == 3
    assert indexer["enable"] is False
    assert indexer["categories"] == [7000, 7010]
    assert indexer["settings"] == {"search": True}
    assert indexer["downloadClientId"] == 1720
    assert indexer["minimumSeeders"] == 6
    assert indexer["seedRatio"] == 1.5
    assert indexer["enableRss"] is False
    assert indexer["enableAutomaticSearch"] is False
    assert indexer["enableInteractiveSearch"] is True
    assert indexer["minimumSize"] == 20
    assert indexer["maximumSize"] == 900
    assert indexer["tags"] == ["owned", "private"]

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT api_key, client_id, use_rss, use_auto_search"
            " FROM indexers WHERE id=1730"
        ).fetchone()
        tags = [
            tag[0]
            for tag in c.execute(
                "SELECT tag FROM indexer_tags WHERE indexer_id=1730 ORDER BY tag"
            )
        ]
    assert decrypt_secret(row["api_key"]) == "NEW-INDEXER-SECRET"
    assert row["client_id"] == 1720
    assert row["use_rss"] == 0
    assert row["use_auto_search"] == 0
    assert tags == ["owned", "private"]


def test_api_v1_update_indexer_preserves_api_key_when_blank(env):
    from security import decrypt_secret, encrypt_if_cipher_available

    old_secret = encrypt_if_cipher_available("OLD-INDEXER-SECRET")
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type, api_key)"
            " VALUES(1740, 'Keep Secret', 'torznab', ?)",
            (old_secret,),
        )

    resp = _client().request(
        "PATCH",
        "/api/v1/indexer/1740",
        json={"name": "Keep Secret Renamed", "apiKey": ""},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    with sqlite3.connect(env) as c:
        api_key = c.execute(
            "SELECT api_key FROM indexers WHERE id=1740"
        ).fetchone()[0]
    assert decrypt_secret(api_key) == "OLD-INDEXER-SECRET"


def test_api_v1_update_indexer_rejects_unknown_id(env):
    resp = _client().request(
        "PATCH",
        "/api/v1/indexer/99999",
        json={"name": "Missing"},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "indexer not found"


def test_api_v1_delete_indexer_removes_row_and_tags(env):
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type)"
            " VALUES(1750, 'Delete Indexer', 'torznab')"
        )
        c.execute(
            "INSERT INTO indexer_tags(indexer_id, tag)"
            " VALUES(1750, 'private')"
        )

    resp = _client().delete(
        "/api/v1/indexer/1750",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "id": 1750}

    with sqlite3.connect(env) as c:
        indexer = c.execute("SELECT 1 FROM indexers WHERE id=1750").fetchone()
        tag = c.execute(
            "SELECT 1 FROM indexer_tags WHERE indexer_id=1750"
        ).fetchone()
    assert indexer is None
    assert tag is None


def test_api_v1_create_download_client_adds_row_tags_and_secret(env):
    from security import decrypt_secret

    resp = _client().post(
        "/api/v1/downloadclient",
        json={
            "name": "API qBit",
            "implementation": "qbittorrent",
            "host": "http://qbittorrent",
            "port": 8080,
            "useSsl": False,
            "urlBase": "/qb",
            "username": "manga",
            "password": "CLIENT-SECRET",
            "category": "manga",
            "postImportCategory": "imported",
            "recentPriority": "first",
            "olderPriority": "last",
            "initialState": "paused",
            "sequentialOrder": True,
            "firstLastFirst": True,
            "contentLayout": "series",
            "priority": 7,
            "enable": False,
            "removeCompletedDownloads": True,
            "removeFailedDownloads": True,
            "sourceId": "source-1",
            "downloadPath": "/downloads/manga",
            "mergeChapters": False,
            "tags": ["favorite", "favorite", "owned"],
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert "CLIENT-SECRET" not in resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "created"
    client = body["downloadClient"]
    assert client["name"] == "API qBit"
    assert client["implementation"] == "qbittorrent"
    assert client["host"] == "http://qbittorrent"
    assert client["port"] == 8080
    assert client["useSsl"] is False
    assert client["urlBase"] == "/qb"
    assert client["username"] == "manga"
    assert client["hasPassword"] is True
    assert client["postImportCategory"] == "imported"
    assert client["recentPriority"] == "first"
    assert client["initialState"] == "paused"
    assert client["sequentialOrder"] is True
    assert client["firstLastFirst"] is True
    assert client["contentLayout"] == "series"
    assert client["priority"] == 7
    assert client["enable"] is False
    assert client["removeCompletedDownloads"] is True
    assert client["removeFailedDownloads"] is True
    assert client["sourceId"] == "source-1"
    assert client["downloadPath"] == "/downloads/manga"
    assert client["mergeChapters"] is False
    assert client["tags"] == ["favorite", "owned"]
    client_id = client["id"]

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT password, enabled, remove_completed, merge_chapters"
            " FROM download_clients WHERE id=?",
            (client_id,),
        ).fetchone()
        tags = [
            tag[0]
            for tag in c.execute(
                "SELECT tag FROM download_client_tags"
                " WHERE client_id=? ORDER BY tag",
                (client_id,),
            )
        ]
    assert row["password"] != "CLIENT-SECRET"
    assert decrypt_secret(row["password"]) == "CLIENT-SECRET"
    assert row["enabled"] == 0
    assert row["remove_completed"] == 1
    assert row["merge_chapters"] == 0
    assert tags == ["favorite", "owned"]


def test_api_v1_create_download_client_requires_api_key(env):
    resp = _client().post(
        "/api/v1/downloadclient",
        json={"name": "No Auth", "implementation": "qbittorrent"},
    )
    assert resp.status_code == 401


def test_api_v1_create_download_client_rejects_bad_priority(env):
    resp = _client().post(
        "/api/v1/downloadclient",
        json={
            "name": "Bad Priority",
            "implementation": "qbittorrent",
            "priority": -1,
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "priority must be zero or a positive integer"


def test_api_v1_update_download_client_updates_submitted_fields_tags_and_secret(env):
    from security import decrypt_secret, encrypt_if_cipher_available

    old_secret = encrypt_if_cipher_available("OLD-SECRET")
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO download_clients"
            "(id, name, type, host, port, username, password, category,"
            " priority, enabled, remove_completed, merge_chapters)"
            " VALUES(1680, 'Old Client', 'qbittorrent', 'old-host', 8080,"
            " 'old-user', ?, 'old-cat', 1, 1, 0, 1)",
            (old_secret,),
        )
        c.execute(
            "INSERT INTO download_client_tags(client_id, tag)"
            " VALUES(1680, 'old-tag')"
        )

    resp = _client().request(
        "PATCH",
        "/api/v1/downloadclient/1680",
        json={
            "name": "Updated Client",
            "implementation": "sabnzbd",
            "host": "new-host",
            "port": 9090,
            "username": "new-user",
            "password": "NEW-SECRET",
            "category": "",
            "priority": 3,
            "enable": False,
            "removeCompletedDownloads": True,
            "mergeChapters": False,
            "tags": "favorite,owned",
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert "NEW-SECRET" not in resp.text
    client = resp.json()["downloadClient"]
    assert client["name"] == "Updated Client"
    assert client["implementation"] == "sabnzbd"
    assert client["host"] == "new-host"
    assert client["port"] == 9090
    assert client["username"] == "new-user"
    assert client["category"] == "manga"
    assert client["priority"] == 3
    assert client["enable"] is False
    assert client["removeCompletedDownloads"] is True
    assert client["mergeChapters"] is False
    assert client["tags"] == ["favorite", "owned"]

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT type, password, category, enabled, remove_completed,"
            " merge_chapters FROM download_clients WHERE id=1680"
        ).fetchone()
        tags = [
            tag[0]
            for tag in c.execute(
                "SELECT tag FROM download_client_tags"
                " WHERE client_id=1680 ORDER BY tag"
            )
        ]
    assert row["type"] == "sabnzbd"
    assert decrypt_secret(row["password"]) == "NEW-SECRET"
    assert row["category"] == "manga"
    assert row["enabled"] == 0
    assert row["remove_completed"] == 1
    assert row["merge_chapters"] == 0
    assert tags == ["favorite", "owned"]


def test_api_v1_update_download_client_preserves_password_when_blank(env):
    from security import decrypt_secret, encrypt_if_cipher_available

    old_secret = encrypt_if_cipher_available("OLD-SECRET")
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO download_clients(id, name, type, password)"
            " VALUES(1681, 'Keep Secret', 'qbittorrent', ?)",
            (old_secret,),
        )

    resp = _client().request(
        "PATCH",
        "/api/v1/downloadclient/1681",
        json={"name": "Keep Secret Renamed", "password": ""},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    with sqlite3.connect(env) as c:
        password = c.execute(
            "SELECT password FROM download_clients WHERE id=1681"
        ).fetchone()[0]
    assert decrypt_secret(password) == "OLD-SECRET"


def test_api_v1_update_download_client_rejects_unknown_id(env):
    resp = _client().request(
        "PATCH",
        "/api/v1/downloadclient/99999",
        json={"name": "Missing"},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "download client not found"


def test_api_v1_delete_download_client_removes_row_and_tags(env):
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO download_clients(id, name, type)"
            " VALUES(1690, 'Delete Client', 'qbittorrent')"
        )
        c.execute(
            "INSERT INTO download_client_tags(client_id, tag)"
            " VALUES(1690, 'favorite')"
        )

    resp = _client().delete(
        "/api/v1/downloadclient/1690",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "id": 1690}

    with sqlite3.connect(env) as c:
        client = c.execute(
            "SELECT 1 FROM download_clients WHERE id=1690"
        ).fetchone()
        tag = c.execute(
            "SELECT 1 FROM download_client_tags WHERE client_id=1690"
        ).fetchone()
    assert client is None
    assert tag is None


def test_api_v1_delete_download_client_rejects_unknown_id(env):
    resp = _client().delete(
        "/api/v1/downloadclient/99999",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "download client not found"


def test_api_v1_create_remote_path_mapping_adds_row(env):
    resp = _client().post(
        "/api/v1/downloadclient/remotepathmapping",
        json={
            "host": "qbittorrent",
            "remotePath": "/remote/downloads",
            "localPath": "/downloads",
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "created"
    assert body["remotePathMapping"]["host"] == "qbittorrent"
    assert body["remotePathMapping"]["remotePath"] == "/remote/downloads"
    assert body["remotePathMapping"]["localPath"] == "/downloads"
    mapping_id = body["remotePathMapping"]["id"]

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT host, remote_path, local_path"
            " FROM remote_path_mappings WHERE id=?",
            (mapping_id,),
        ).fetchone()
    assert dict(row) == {
        "host": "qbittorrent",
        "remote_path": "/remote/downloads",
        "local_path": "/downloads",
    }


def test_api_v1_create_remote_path_mapping_requires_api_key(env):
    resp = _client().post(
        "/api/v1/downloadclient/remotepathmapping",
        json={"remotePath": "/remote", "localPath": "/local"},
    )
    assert resp.status_code == 401


def test_api_v1_create_remote_path_mapping_rejects_missing_paths(env):
    resp = _client().post(
        "/api/v1/downloadclient/remotepathmapping",
        json={"host": "qbittorrent", "localPath": "/downloads"},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "remotePath is required"


def test_api_v1_update_remote_path_mapping_updates_submitted_fields(env):
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO remote_path_mappings"
            "(id, host, remote_path, local_path)"
            " VALUES(1660, 'qbittorrent', '/old-remote', '/old-local')"
        )

    resp = _client().request(
        "PATCH",
        "/api/v1/downloadclient/remotepathmapping/1660",
        json={
            "host": "",
            "remotePath": "/new-remote",
            "localPath": "/new-local",
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["remotePathMapping"] == {
        "id": 1660,
        "host": "",
        "remotePath": "/new-remote",
        "localPath": "/new-local",
    }

    with sqlite3.connect(env) as c:
        row = c.execute(
            "SELECT host, remote_path, local_path"
            " FROM remote_path_mappings WHERE id=1660"
        ).fetchone()
    assert row == ("", "/new-remote", "/new-local")


def test_api_v1_update_remote_path_mapping_rejects_unknown_id(env):
    resp = _client().request(
        "PATCH",
        "/api/v1/downloadclient/remotepathmapping/99999",
        json={"remotePath": "/missing"},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "remote path mapping not found"


def test_api_v1_delete_remote_path_mapping_removes_row(env):
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO remote_path_mappings"
            "(id, host, remote_path, local_path)"
            " VALUES(1670, 'qbittorrent', '/remote', '/local')"
        )

    resp = _client().delete(
        "/api/v1/downloadclient/remotepathmapping/1670",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "id": 1670}

    with sqlite3.connect(env) as c:
        row = c.execute(
            "SELECT 1 FROM remote_path_mappings WHERE id=1670"
        ).fetchone()
    assert row is None


def test_api_v1_delete_remote_path_mapping_rejects_unknown_id(env):
    resp = _client().delete(
        "/api/v1/downloadclient/remotepathmapping/99999",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "remote path mapping not found"


def test_api_v1_create_import_list_exclusion_adds_row(env):
    resp = _client().post(
        "/api/v1/importlistexclusion",
        json={
            "source": "anilist_user",
            "externalId": "42",
            "title": "Blocked Manga",
            "reason": "already owned elsewhere",
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "created"
    assert body["importListExclusion"]["source"] == "anilist_user"
    assert body["importListExclusion"]["externalId"] == "42"
    assert body["importListExclusion"]["title"] == "Blocked Manga"
    assert body["importListExclusion"]["titleNormalized"] == "blocked manga"
    assert body["importListExclusion"]["reason"] == "already owned elsewhere"
    assert body["importListExclusion"]["addedAt"] is not None

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT source, external_id, title, title_normalized, reason"
            " FROM import_list_exclusions WHERE source='anilist_user'"
        ).fetchone()
    assert dict(row) == {
        "source": "anilist_user",
        "external_id": "42",
        "title": "Blocked Manga",
        "title_normalized": "blocked manga",
        "reason": "already owned elsewhere",
    }


def test_api_v1_create_import_list_exclusion_is_idempotent(env):
    payload = {
        "source": "anilist_user",
        "externalId": "42",
        "title": "Blocked Manga",
    }
    first = _client().post(
        "/api/v1/importlistexclusion",
        json=payload,
        headers={"X-Api-Key": _api_key(env)},
    )
    assert first.status_code == 200, first.text
    second = _client().post(
        "/api/v1/importlistexclusion",
        json=payload,
        headers={"X-Api-Key": _api_key(env)},
    )
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "exists"
    assert (
        second.json()["importListExclusion"]["id"]
        == first.json()["importListExclusion"]["id"]
    )

    with sqlite3.connect(env) as c:
        count = c.execute(
            "SELECT COUNT(*) FROM import_list_exclusions"
            " WHERE source='anilist_user' AND external_id='42'"
        ).fetchone()[0]
    assert count == 1


def test_api_v1_create_import_list_exclusion_requires_api_key(env):
    resp = _client().post(
        "/api/v1/importlistexclusion",
        json={"source": "anilist_user", "externalId": "42"},
    )
    assert resp.status_code == 401


def test_api_v1_create_import_list_exclusion_rejects_missing_key(env):
    resp = _client().post(
        "/api/v1/importlistexclusion",
        json={"source": "anilist_user"},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == (
        "source plus either externalId or title is required"
    )


def test_api_v1_update_import_list_exclusion_updates_normalized_title(env):
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO import_list_exclusions"
            "(id, source, external_id, title, title_normalized, reason)"
            " VALUES(1501, 'anilist_user', '42', 'Old Title',"
            " 'old title', 'old')"
        )

    resp = _client().request(
        "PATCH",
        "/api/v1/importlistexclusion/1501",
        json={
            "source": "mal_user",
            "externalId": "",
            "title": "  New   Blocked   Manga  ",
            "reason": "not wanted",
        },
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["importListExclusion"]["source"] == "mal_user"
    assert body["importListExclusion"]["externalId"] is None
    assert body["importListExclusion"]["title"] == "New   Blocked   Manga"
    assert body["importListExclusion"]["titleNormalized"] == "new blocked manga"
    assert body["importListExclusion"]["reason"] == "not wanted"

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT source, external_id, title, title_normalized, reason"
            " FROM import_list_exclusions WHERE id=1501"
        ).fetchone()
    assert dict(row) == {
        "source": "mal_user",
        "external_id": None,
        "title": "New   Blocked   Manga",
        "title_normalized": "new blocked manga",
        "reason": "not wanted",
    }


def test_api_v1_update_import_list_exclusion_rejects_duplicate(env):
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO import_list_exclusions"
            "(id, source, external_id, title, title_normalized)"
            " VALUES(1510, 'anilist_user', '42', 'A', 'a')"
        )
        c.execute(
            "INSERT INTO import_list_exclusions"
            "(id, source, external_id, title, title_normalized)"
            " VALUES(1511, 'anilist_user', '43', 'B', 'b')"
        )

    resp = _client().request(
        "PATCH",
        "/api/v1/importlistexclusion/1511",
        json={"externalId": "42"},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "import list exclusion already exists"


def test_api_v1_update_import_list_exclusion_rejects_unknown_id(env):
    resp = _client().request(
        "PATCH",
        "/api/v1/importlistexclusion/99999",
        json={"title": "Missing"},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "import list exclusion not found"


def test_api_v1_delete_import_list_exclusion_removes_row(env):
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO import_list_exclusions"
            "(id, source, external_id, title, title_normalized)"
            " VALUES(1520, 'anilist_user', '42', 'Blocked', 'blocked')"
        )

    resp = _client().delete(
        "/api/v1/importlistexclusion/1520",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "id": 1520}

    with sqlite3.connect(env) as c:
        row = c.execute(
            "SELECT 1 FROM import_list_exclusions WHERE id=1520"
        ).fetchone()
    assert row is None


def test_api_v1_delete_import_list_exclusion_rejects_unknown_id(env):
    resp = _client().delete(
        "/api/v1/importlistexclusion/99999",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "import list exclusion not found"


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
    assert urls == [
        "failed-release-url",
        "import-release-url",
        "recent",
        "reset-release-url",
    ]


def test_api_v1_command_rejects_unknown_command(env):
    resp = _client().post(
        "/api/v1/command",
        json={"name": "NoSuchCommand"},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


def test_api_v1_clear_blocklist_removes_all_entries(env):
    resp = _client().delete(
        "/api/v1/blocklist",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "deleted": 1}

    with sqlite3.connect(env) as c:
        remaining = c.execute("SELECT COUNT(*) FROM blocklist").fetchone()[0]
    assert remaining == 0


def test_api_v1_clear_blocklist_requires_api_key(env):
    resp = _client().delete("/api/v1/blocklist")
    assert resp.status_code == 401


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


def test_api_v1_clear_failed_history_removes_only_failures(env):
    resp = _client().delete(
        "/api/v1/history/failed",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "deleted": 2}

    with sqlite3.connect(env) as c:
        rows = c.execute(
            "SELECT id, event_type FROM history ORDER BY id"
        ).fetchall()
    assert rows == [(701, "grabbed")]


def test_api_v1_clear_failed_history_requires_api_key(env):
    resp = _client().delete("/api/v1/history/failed")
    assert resp.status_code == 401


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


def test_api_v1_queue_dismiss_import_entry_resets_grabbed_state(env):
    resp = _client().delete(
        "/api/v1/queue/import/901",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "id": 901}

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        queue = c.execute("SELECT 1 FROM import_queue WHERE id=901").fetchone()
        queue_files = c.execute(
            "SELECT COUNT(*) FROM import_queue_files WHERE queue_id=901"
        ).fetchone()[0]
        volume = c.execute(
            "SELECT status, source_url, download_id, indexer, protocol,"
            " client, release_group FROM volumes WHERE id=504"
        ).fetchone()
        seen_by_url = c.execute(
            "SELECT 1 FROM seen WHERE torrent_url='import-release-url'"
        ).fetchone()
        seen_by_download = c.execute(
            "SELECT 1 FROM seen WHERE download_id='import-dl'"
        ).fetchone()

    assert queue is None
    assert queue_files == 0
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


def test_api_v1_queue_dismiss_import_entry_requires_api_key(env):
    resp = _client().delete("/api/v1/queue/import/901")
    assert resp.status_code == 401


def test_api_v1_queue_dismiss_import_entry_rejects_unknown_id(env):
    resp = _client().delete(
        "/api/v1/queue/import/99999",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "import queue entry not found"

    with sqlite3.connect(env) as c:
        remaining = c.execute(
            "SELECT COUNT(*) FROM import_queue WHERE id IN (901, 903)"
        ).fetchone()[0]
    assert remaining == 2


def test_api_v1_queue_clear_failed_imports_removes_inactive_entries(env):
    resp = _client().delete(
        "/api/v1/queue/import/failed",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "deleted": 2, "deletedFiles": 2}

    with sqlite3.connect(env) as c:
        queues = c.execute(
            "SELECT id, status FROM import_queue ORDER BY id"
        ).fetchall()
        queue_files = c.execute(
            "SELECT queue_id, status FROM import_queue_files ORDER BY queue_id"
        ).fetchall()
    assert queues == [(901, "pending")]
    assert queue_files == [(901, "pending")]


def test_api_v1_queue_clear_failed_imports_requires_api_key(env):
    resp = _client().delete("/api/v1/queue/import/failed")
    assert resp.status_code == 401


def test_api_v1_queue_skip_import_entry_marks_queue_and_files_skipped(env):
    resp = _client().post(
        "/api/v1/queue/import/901/skip",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "id": 901}

    with sqlite3.connect(env) as c:
        queue_status = c.execute(
            "SELECT status FROM import_queue WHERE id=901"
        ).fetchone()[0]
        file_statuses = [
            row[0]
            for row in c.execute(
                "SELECT status FROM import_queue_files WHERE queue_id=901"
            )
        ]
    assert queue_status == "skipped"
    assert file_statuses == ["skipped"]


def test_api_v1_queue_skip_import_entry_requires_api_key(env):
    resp = _client().post("/api/v1/queue/import/901/skip")
    assert resp.status_code == 401


def test_api_v1_queue_skip_import_entry_rejects_unknown_id(env):
    resp = _client().post(
        "/api/v1/queue/import/99999/skip",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "import queue entry not found"


def test_api_v1_queue_skip_import_entry_rejects_non_pending_status(env):
    resp = _client().post(
        "/api/v1/queue/import/903/skip",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "import queue entry is not pending or partial"

    with sqlite3.connect(env) as c:
        queue_status = c.execute(
            "SELECT status FROM import_queue WHERE id=903"
        ).fetchone()[0]
        file_status = c.execute(
            "SELECT status FROM import_queue_files WHERE queue_id=903"
        ).fetchone()[0]
    assert queue_status == "failed"
    assert file_status == "failed"


def test_api_v1_queue_retry_import_entry_resets_failed_to_pending(env):
    from unittest.mock import patch
    import main

    async def _noop(*args, **kwargs):
        return None

    with patch.object(main, "_process_auto_import", _noop):
        resp = _client().post(
            "/api/v1/queue/import/903/retry",
            headers={"X-Api-Key": _api_key(env)},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "id": 903, "queued": True}

    with sqlite3.connect(env) as c:
        queue_status = c.execute(
            "SELECT status FROM import_queue WHERE id=903"
        ).fetchone()[0]
        file_status = c.execute(
            "SELECT status FROM import_queue_files WHERE queue_id=903"
        ).fetchone()[0]
    assert queue_status == "pending"
    assert file_status == "pending"


def test_api_v1_queue_retry_import_entry_requires_api_key(env):
    resp = _client().post("/api/v1/queue/import/903/retry")
    assert resp.status_code == 401


def test_api_v1_queue_retry_import_entry_rejects_unknown_id(env):
    resp = _client().post(
        "/api/v1/queue/import/99999/retry",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "import queue entry not found"


def test_api_v1_queue_retry_import_entry_rejects_non_retryable_status(env):
    resp = _client().post(
        "/api/v1/queue/import/901/retry",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "import queue entry is not failed or partial"

    with sqlite3.connect(env) as c:
        queue_status = c.execute(
            "SELECT status FROM import_queue WHERE id=901"
        ).fetchone()[0]
    assert queue_status == "pending"
