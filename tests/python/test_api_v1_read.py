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
    import main, shared, security

    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close()
    os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-api-v1-keys-")
    library_root = tempfile.mkdtemp(prefix="mangarr-api-v1-library-")

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
        c.execute("DELETE FROM import_queue")
        c.execute("DELETE FROM pending_releases")
        c.execute("DELETE FROM history")
        c.execute("DELETE FROM blocklist")
        c.execute("DELETE FROM chapters")
        c.execute("DELETE FROM volumes")
        c.execute("DELETE FROM series_tags")
        c.execute("DELETE FROM series")
        c.execute("DELETE FROM quality_profile_custom_formats")
        c.execute("DELETE FROM quality_profiles")
        c.execute("DELETE FROM root_folders")
        c.execute(
            "INSERT INTO root_folders(id, path, label, is_default)"
            " VALUES(1, ?, 'Library', 1)",
            (library_root,),
        )
        c.execute(
            "INSERT INTO quality_profiles"
            "(id, name, qualities, cutoff, upgrades_allowed,"
            " minimum_custom_format_score, cutoff_format_score,"
            " min_upgrade_format_score, is_default)"
            " VALUES(10, 'Best Available', '[\"cbz\",\"cbr\",\"epub\"]',"
            " 'cbz', 1, 25, 10000, 10, 1)"
        )
        c.execute(
            "INSERT INTO series"
            "(id, title, search_pattern, anilist_id, mangadex_id, status,"
            " description, total_volumes, total_chapters, enabled,"
            " monitored, root_folder_id, quality_profile_id, monitor_mode,"
            " tags, pub_year)"
            " VALUES(5, 'Vinland Saga', 'Vinland Saga', 123, 'mdx-123',"
            " 'releasing', 'Viking manga', 3, 30, 1, 1, 1, 10,"
            " 'missing', '[\"owned\"]', 2005)"
        )
        c.execute(
            "INSERT INTO series_tags(series_id, tag) VALUES(5, 'favorite')"
        )
        c.execute(
            "INSERT INTO volumes"
            "(id, series_id, volume_num, status, monitored, quality,"
            " torrent_name, download_id, grabbed_at, indexer, protocol,"
            " client, size_bytes)"
            " VALUES(101, 5, 1.0, 'downloaded', 1, 'cbz', NULL, NULL,"
            " NULL, NULL, NULL, NULL, 1000)"
        )
        c.execute(
            "INSERT INTO volumes"
            "(id, series_id, volume_num, status, monitored)"
            " VALUES(102, 5, 2.0, 'wanted', 1)"
        )
        c.execute(
            "INSERT INTO volumes"
            "(id, series_id, volume_num, status, monitored, torrent_name,"
            " download_id, grabbed_at, indexer, protocol, client, size_bytes)"
            " VALUES(103, 5, 3.0, 'grabbed', 1, 'Vinland Saga v03',"
            " 'abc123', '2026-01-01T00:00:00Z', 'Nyaa', 'torrent',"
            " 'qBittorrent', 3000)"
        )
        c.execute(
            "INSERT INTO volumes"
            "(id, series_id, volume_num, status, monitored, quality,"
            " import_path, grabbed_at)"
            " VALUES(104, 5, 4.0, 'downloaded', 1, 'pdf',"
            " '/library/Vinland Saga v04.pdf', '2026-01-02T00:00:00Z')"
        )
        c.execute(
            "INSERT INTO chapters"
            "(id, series_id, volume_id, chapter_num, title, status,"
            " monitored, quality, import_path)"
            " VALUES(501, 5, 101, 1.0, 'Somewhere Not Here',"
            " 'downloaded', 1, 'cbz', '/library/Vinland Saga c001.cbz')"
        )
        c.execute(
            "INSERT INTO import_queue"
            "(id, series_id, download_id, torrent_name, volume_num, src_dir, status)"
            " VALUES(201, 5, 'import-1', 'Vinland Saga v02', 2.0,"
            " '/downloads/vinland', 'pending')"
        )
        c.execute(
            "INSERT INTO pending_releases"
            "(id, series_id, url, title, indexer, protocol, size_bytes)"
            " VALUES(301, 5, 'https://example.invalid/release',"
            " 'Vinland Saga v04', 'Nyaa', 'torrent', 4000)"
        )
        c.execute(
            "INSERT INTO history"
            "(id, event_type, series_id, series_title, volume_label,"
            " source_title, indexer, protocol, client, download_id,"
            " size_bytes, release_group, data)"
            " VALUES(401, 'grabbed', 5, 'Vinland Saga', 'Vol 3',"
            " 'Vinland Saga v03', 'Nyaa', 'torrent', 'qBittorrent',"
            " 'abc123', 3000, 'Group', '{\"score\": 10}')"
        )
        c.execute(
            "INSERT INTO blocklist"
            "(id, series_id, torrent_url, torrent_name, reason, indexer,"
            " protocol, size_bytes, added_at)"
            " VALUES(601, 5, 'https://example.invalid/bad.torrent',"
            " 'Bad Release', 'Manual', 'Nyaa', 'torrent', 1234,"
            " '2026-01-03T00:00:00+00:00')"
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


def test_api_v1_requires_api_key(env):
    resp = _client().get("/api/v1/system/status")
    assert resp.status_code == 401


def test_api_v1_system_status(env):
    resp = _client().get(
        "/api/v1/system/status",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["appName"] == "Mangarr"
    assert body["authentication"] == "apikey"
    assert body["databaseType"] == "sqlite"


def test_api_v1_profiles_roots_and_series_contract(env):
    client = _client()
    headers = {"X-Api-Key": _api_key(env)}

    roots = client.get("/api/v1/rootfolder", headers=headers).json()
    assert roots[0]["id"] == 1
    assert roots[0]["name"] == "Library"
    assert roots[0]["isDefault"] is True
    assert roots[0]["isAvailable"] is True

    profiles = client.get("/api/v1/qualityprofile", headers=headers).json()
    assert profiles == [
        {
            "id": 10,
            "name": "Best Available",
            "qualities": ["cbz", "cbr", "epub"],
            "cutoff": "cbz",
            "upgradesAllowed": True,
            "minimumCustomFormatScore": 25,
            "cutoffFormatScore": 10000,
            "minUpgradeFormatScore": 10,
            "isDefault": True,
        }
    ]

    series = client.get("/api/v1/series", headers=headers).json()
    assert len(series) == 1
    item = series[0]
    assert item["id"] == 5
    assert item["title"] == "Vinland Saga"
    assert item["titleSlug"] == "vinland-saga"
    assert item["qualityProfileId"] == 10
    assert item["qualityProfileName"] == "Best Available"
    assert item["rootFolderId"] == 1
    assert item["tags"] == ["favorite", "owned"]
    assert item["statistics"]["volumeCount"] == 4
    assert item["statistics"]["volumeFileCount"] == 2
    assert item["statistics"]["wantedCount"] == 1
    assert item["statistics"]["grabbedCount"] == 1


def test_api_v1_queue_history_and_wanted_contract(env):
    client = _client()
    headers = {"X-Api-Key": _api_key(env)}

    queue = client.get("/api/v1/queue", headers=headers).json()
    statuses = {row["id"]: row["status"] for row in queue}
    assert statuses["volume-103"] == "grabbed"
    assert statuses["import-201"] == "pending"
    assert statuses["pending-301"] == "pending"
    volume_row = next(row for row in queue if row["id"] == "volume-103")
    assert volume_row["volumeLabel"] == "Vol 3"
    assert volume_row["downloadId"] == "abc123"

    history = client.get("/api/v1/history", headers=headers).json()
    assert history["totalRecords"] == 1
    assert history["records"][0]["eventType"] == "grabbed"
    assert history["records"][0]["data"] == {"score": 10}

    wanted = client.get("/api/v1/wanted", headers=headers).json()
    assert wanted == [
        {
            "id": 102,
            "seriesId": 5,
            "seriesTitle": "Vinland Saga",
            "volumeNumber": 2.0,
            "chapterNumber": None,
            "volumeLabel": "Vol 2",
            "monitored": True,
            "status": "wanted",
        }
    ]


def test_api_v1_series_detail_blocklist_commands_and_cutoff(env):
    client = _client()
    headers = {"X-Api-Key": _api_key(env)}

    detail = client.get("/api/v1/series/5", headers=headers).json()
    assert detail["id"] == 5
    assert [v["id"] for v in detail["volumes"]] == [101, 102, 103, 104]
    assert detail["volumes"][0]["label"] == "Vol 1"
    assert detail["volumes"][0]["quality"] == "cbz"
    assert detail["chapters"] == [
        {
            "id": 501,
            "seriesId": 5,
            "volumeId": 101,
            "chapterNumber": 1.0,
            "chapterRangeEnd": None,
            "label": "Ch.001",
            "title": "Somewhere Not Here",
            "status": "downloaded",
            "monitored": True,
            "quality": "cbz",
            "size": 0,
            "sourceTitle": None,
            "indexer": None,
            "protocol": None,
            "downloadClient": None,
            "downloadId": None,
            "importPath": "/library/Vinland Saga c001.cbz",
            "grabbedAt": None,
            "importedAt": None,
        }
    ]
    missing = client.get("/api/v1/series/999", headers=headers)
    assert missing.status_code == 404

    blocklist = client.get("/api/v1/blocklist", headers=headers).json()
    assert blocklist[0]["id"] == 601
    assert blocklist[0]["seriesTitle"] == "Vinland Saga"
    assert blocklist[0]["sourceTitle"] == "Bad Release"
    assert blocklist[0]["expiresAt"].startswith("2026-04-03")

    commands = client.get("/api/v1/command", headers=headers).json()
    assert {cmd["name"] for cmd in commands} >= {"RssSyncAll", "CheckDownloads"}
    assert all("manual" in cmd and "displayName" in cmd for cmd in commands)

    cutoff = client.get("/api/v1/wanted/cutoff", headers=headers).json()
    assert cutoff == [
        {
            "id": 104,
            "seriesId": 5,
            "seriesTitle": "Vinland Saga",
            "volumeNumber": 4.0,
            "volumeLabel": "Vol 4",
            "currentQuality": "pdf",
            "cutoff": "cbz",
            "qualityCutoffSource": "profile",
            "importPath": "/library/Vinland Saga v04.pdf",
            "grabbedAt": "2026-01-02T00:00:00Z",
        }
    ]
