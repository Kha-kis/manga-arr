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
        c.execute("DELETE FROM custom_formats")
        c.execute("DELETE FROM release_profile_tags")
        c.execute("DELETE FROM release_profiles")
        c.execute("DELETE FROM language_profiles")
        c.execute("DELETE FROM root_folders")
        c.execute(
            "INSERT INTO root_folders(id, path, label, is_default)"
            " VALUES(1, ?, 'Library', 1)",
            (library_root,),
        )
        c.execute(
            "INSERT INTO root_folders(id, path, label, is_default)"
            " VALUES(2, ?, 'Archive', 0)",
            (os.path.join(library_root, "archive"),),
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
            "INSERT INTO language_profiles(id, name, languages, allow_any)"
            " VALUES(20, 'English', '[\"en\"]', 0)"
        )
        c.execute(
            "INSERT OR REPLACE INTO settings(key,value)"
            " VALUES('default_language_profile_id', '20')"
        )
        c.execute(
            "INSERT INTO custom_formats"
            "(id, name, specifications, include_custom_format_when_renaming)"
            " VALUES(30, 'Official Digital',"
            " '[{\"name\":\"official\",\"implementation\":\"source_is\","
            " \"value\":\"official_digital\"}]', 1)"
        )
        c.execute(
            "INSERT INTO quality_profile_custom_formats"
            "(profile_id, format_id, score) VALUES(10, 30, 50)"
        )
        c.execute(
            "INSERT INTO release_profiles"
            "(id, name, enabled, required, ignored, preferred,"
            " include_preferred_when_renaming)"
            " VALUES(40, 'Trusted Groups', 1, 'group', 'raw',"
            " '[{\"term\":\"deluxe\",\"score\":25}]', 1)"
        )
        c.execute(
            "INSERT INTO release_profile_tags(profile_id, tag)"
            " VALUES(40, 'favorite')"
        )
        c.execute(
            "INSERT INTO series"
            "(id, title, search_pattern, anilist_id, mangadex_id, status,"
            " description, total_volumes, total_chapters, enabled,"
            " monitored, root_folder_id, quality_profile_id, language_profile_id,"
            " monitor_mode, tags, pub_year)"
            " VALUES(5, 'Vinland Saga', 'Vinland Saga', 123, 'mdx-123',"
            " 'releasing', 'Viking manga', 3, 30, 1, 1, 1, 10,"
            " 20, 'missing', '[\"owned\"]', 2005)"
        )
        c.execute(
            "INSERT INTO series"
            "(id, title, search_pattern, status, description, total_volumes,"
            " enabled, monitored, root_folder_id, quality_profile_id,"
            " language_profile_id, monitor_mode, tags, pub_year)"
            " VALUES(6, 'Berserk', 'Berserk Deluxe', 'ended',"
            " 'Dark fantasy manga', 1, 1, 0, 2, 10, 20, 'none',"
            " '[\"archived\"]', 1989)"
        )
        c.execute(
            "INSERT INTO series_tags(series_id, tag) VALUES(5, 'favorite')"
        )
        c.execute(
            "INSERT INTO series_tags(series_id, tag) VALUES(6, 'dark')"
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
    import main

    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES('url_base','/mangarr')"
        )
    main.load_config()

    resp = _client().get(
        "/api/v1/system/status",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["appName"] == "Mangarr"
    assert body["authentication"] == "apikey"
    assert body["databaseType"] == "sqlite"
    assert body["urlBase"] == "/mangarr"


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

    language_profiles = client.get(
        "/api/v1/languageprofile", headers=headers
    ).json()
    assert language_profiles == [
        {
            "id": 20,
            "name": "English",
            "languages": ["en"],
            "allowAny": False,
            "isDefault": True,
        }
    ]

    custom_formats = client.get("/api/v1/customformat", headers=headers).json()
    assert custom_formats == [
        {
            "id": 30,
            "name": "Official Digital",
            "specifications": [
                {
                    "name": "official",
                    "implementation": "source_is",
                    "value": "official_digital",
                }
            ],
            "includeCustomFormatWhenRenaming": True,
            "qualityProfileScores": [
                {"qualityProfileId": 10, "score": 50},
            ],
        }
    ]

    release_profiles = client.get("/api/v1/releaseprofile", headers=headers).json()
    assert release_profiles == [
        {
            "id": 40,
            "name": "Trusted Groups",
            "enabled": True,
            "required": "group",
            "ignored": "raw",
            "preferred": [{"term": "deluxe", "score": 25}],
            "includePreferredWhenRenaming": True,
            "tags": ["favorite"],
        }
    ]

    series = client.get("/api/v1/series", headers=headers).json()
    assert [row["id"] for row in series] == [6, 5]
    item = next(row for row in series if row["id"] == 5)
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


def test_api_v1_series_filters_sort_and_paging(env):
    client = _client()
    headers = {"X-Api-Key": _api_key(env)}

    by_term = client.get(
        "/api/v1/series",
        params={"term": "deluxe"},
        headers=headers,
    )
    assert by_term.status_code == 200, by_term.text
    assert [row["id"] for row in by_term.json()] == [6]
    assert by_term.headers["X-Total-Count"] == "1"

    monitored = client.get(
        "/api/v1/series",
        params={"monitored": "true"},
        headers=headers,
    )
    assert [row["id"] for row in monitored.json()] == [5]

    by_tag = client.get(
        "/api/v1/series",
        params={"tag": "dark"},
        headers=headers,
    )
    assert [row["id"] for row in by_tag.json()] == [6]

    by_root = client.get(
        "/api/v1/series",
        params={"rootFolderId": 1},
        headers=headers,
    )
    assert [row["id"] for row in by_root.json()] == [5]

    paged = client.get(
        "/api/v1/series",
        params={"sortKey": "year", "sortDirection": "desc", "page": 1, "pageSize": 1},
        headers=headers,
    )
    assert paged.status_code == 200, paged.text
    assert [row["id"] for row in paged.json()] == [5]
    assert paged.headers["X-Total-Count"] == "2"
    assert paged.headers["X-Page"] == "1"
    assert paged.headers["X-Page-Size"] == "1"

    bad_bool = client.get(
        "/api/v1/series",
        params={"monitored": "sometimes"},
        headers=headers,
    )
    assert bad_bool.status_code == 400
    assert bad_bool.json()["error"] == "monitored must be a boolean"


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


def test_api_v1_calendar_contract(env):
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO series"
            "(id, title, search_pattern, status, total_volumes, enabled,"
            " monitored, root_folder_id, quality_profile_id, language_profile_id,"
            " monitor_mode, pub_year)"
            " VALUES(7, 'Witch Hat Atelier', 'Witch Hat Atelier',"
            " 'not_yet_released', 12, 1, 1, 1, 10, 20, 'future', 2027)"
        )
        c.execute(
            "INSERT INTO series"
            "(id, title, search_pattern, status, total_volumes, enabled,"
            " monitored, root_folder_id, quality_profile_id, language_profile_id,"
            " monitor_mode)"
            " VALUES(8, 'Nana', 'Nana', 'hiatus', 2, 1, 1, 1, 10, 20, 'all')"
        )
        c.execute(
            "INSERT INTO volumes(id, series_id, volume_num, status, monitored)"
            " VALUES(801, 8, 1.0, 'downloaded', 1)"
        )
        c.execute(
            "INSERT INTO volumes(id, series_id, volume_num, status, monitored)"
            " VALUES(802, 8, 2.0, 'wanted', 1)"
        )

    resp = _client().get(
        "/api/v1/calendar",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["releasing"] == [
        {
            "seriesId": 5,
            "seriesTitle": "Vinland Saga",
            "status": "releasing",
            "coverUrl": None,
            "totalVolumes": 3,
            "have": 2,
            "missing": 2,
            "wantedVolumes": [2.0],
            "grabbedVolumes": [3.0],
        }
    ]
    assert body["upcoming"] == [
        {
            "seriesId": 7,
            "seriesTitle": "Witch Hat Atelier",
            "status": "not_yet_released",
            "coverUrl": None,
            "totalVolumes": 12,
            "year": 2027,
        }
    ]
    assert body["hiatus"] == [
        {
            "seriesId": 8,
            "seriesTitle": "Nana",
            "status": "hiatus",
            "coverUrl": None,
            "totalVolumes": 2,
            "have": 1,
            "volumeCount": 2,
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
