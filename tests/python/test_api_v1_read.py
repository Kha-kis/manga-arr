import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

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
        c.execute("DELETE FROM import_list_exclusions")
        c.execute("DELETE FROM import_lists")
        c.execute("DELETE FROM chapters")
        c.execute("DELETE FROM volumes")
        c.execute("DELETE FROM series_tags")
        c.execute("DELETE FROM series")
        c.execute("DELETE FROM delay_profile_tags")
        c.execute("DELETE FROM delay_profiles")
        c.execute("DELETE FROM quality_profile_custom_formats")
        c.execute("DELETE FROM quality_profiles")
        c.execute("DELETE FROM quality_definitions")
        c.execute("DELETE FROM custom_formats")
        c.execute("DELETE FROM release_profile_tags")
        c.execute("DELETE FROM release_profiles")
        c.execute("DELETE FROM language_profiles")
        c.execute("DELETE FROM indexer_tags")
        c.execute("DELETE FROM indexer_backoff")
        c.execute("DELETE FROM indexers")
        c.execute("DELETE FROM download_client_tags")
        c.execute("DELETE FROM client_breaker_state")
        c.execute("DELETE FROM download_clients")
        c.execute("DELETE FROM remote_path_mappings")
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
            "INSERT INTO quality_definitions"
            "(quality, title, min_size, max_size, order_num)"
            " VALUES('cbz', 'Comic Book Zip', 1.0, 500.0, 1)"
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
            "INSERT INTO delay_profiles"
            "(id, name, order_num, enable_usenet, enable_torrent,"
            " usenet_delay, torrent_delay, bypass_if_highest_quality,"
            " is_default)"
            " VALUES(45, 'Torrent Delay', 1, 0, 1, 0, 30, 1, 0)"
        )
        c.execute(
            "INSERT INTO delay_profile_tags(profile_id, tag)"
            " VALUES(45, 'favorite')"
        )
        c.execute(
            "INSERT INTO download_clients"
            "(id, name, type, host, port, use_ssl, url_base, username,"
            " password, category, priority, enabled, remove_completed,"
            " post_import_category, recent_priority, older_priority,"
            " initial_state, sequential_order, first_last_first,"
            " content_layout, remove_failed, source_id, download_path,"
            " merge_chapters)"
            " VALUES(50, 'qBittorrent', 'qbittorrent',"
            " 'http://qbittorrent', 8080, 0, '/qb', 'user',"
            " 'CLIENT-SECRET', 'manga', 1, 1, 1, 'manga-imported',"
            " 'first', 'last', 'paused', 1, 0, 'original', 1,"
            " 'qbit-main', '/downloads/manga', 1)"
        )
        c.execute(
            "INSERT INTO download_client_tags(client_id, tag)"
            " VALUES(50, 'favorite')"
        )
        c.execute(
            "INSERT INTO remote_path_mappings(id, host, remote_path, local_path)"
            " VALUES(60, 'qbittorrent', '/remote/downloads', '/downloads')"
        )
        c.execute(
            "INSERT INTO indexers"
            "(id, name, type, url, api_key, priority, enabled, categories,"
            " settings, client_id, min_seeders, seed_ratio,"
            " parent_prowlarr_id, prowlarr_indexer_id, use_rss,"
            " use_auto_search, use_interactive_search, min_size_mb,"
            " max_size_mb)"
            " VALUES(70, 'Nyaa', 'torznab', 'https://nyaa.example/torznab',"
            " 'INDEXER-SECRET', 25, 1, '[7000,7010,7020]',"
            " '{\"animeStandardFormatSearch\":true}', 50, 5, 2.5,"
            " NULL, NULL, 1, 1, 0, 10, 500)"
        )
        c.execute(
            "INSERT INTO indexer_tags(indexer_id, tag)"
            " VALUES(70, 'favorite')"
        )
        c.execute(
            "INSERT INTO import_lists"
            "(id, name, type, enabled, quality_profile_id, root_folder_id,"
            " monitor_mode, settings, last_sync)"
            " VALUES(80, 'AniList Favorites', 'anilist_user', 1, 10, 1,"
            " 'all', '{\"username\":\"vinland\"}',"
            " '2026-01-04T00:00:00+00:00')"
        )
        c.execute(
            "INSERT INTO import_list_exclusions"
            "(id, source, external_id, title, title_normalized, reason, added_at)"
            " VALUES(90, 'anilist', '12345', 'Excluded Manga',"
            " 'excluded manga', 'Not wanted', '2026-01-05T00:00:00+00:00')"
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


def test_api_v1_system_tasks_include_schedule_state(env, monkeypatch):
    import routers.system as system_router

    last_run = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    next_run = datetime(2026, 1, 2, 3, 19, 5, tzinfo=timezone.utc)
    state = dict(system_router.TASK_STATE["RssSyncAll"])
    state.update({"last_run": last_run, "next_run": next_run})
    monkeypatch.setitem(system_router.TASK_STATE, "RssSyncAll", state)

    resp = _client().get(
        "/api/v1/system/task",
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    tasks = resp.json()
    rss = [task for task in tasks if task["name"] == "RssSyncAll"][0]
    assert rss == {
        "name": "RssSyncAll",
        "displayName": "RSS Sync",
        "interval": "15 min",
        "manual": False,
        "lastRun": "2026-01-02T03:04:05+00:00",
        "nextRun": "2026-01-02T03:19:05+00:00",
    }
    assert {task["name"] for task in tasks} >= {"CheckDownloads", "Backup"}


def test_api_v1_system_tasks_match_command_list_contract(env):
    client = _client()
    headers = {"X-Api-Key": _api_key(env)}
    tasks = client.get("/api/v1/system/task", headers=headers)
    commands = client.get("/api/v1/command", headers=headers)
    assert tasks.status_code == 200, tasks.text
    assert commands.status_code == 200, commands.text
    assert tasks.json() == commands.json()


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


def test_api_v1_indexers_download_clients_and_remote_paths_contract(env):
    client = _client()
    headers = {"X-Api-Key": _api_key(env)}

    indexers_resp = client.get("/api/v1/indexer", headers=headers)
    assert indexers_resp.status_code == 200, indexers_resp.text
    indexers = indexers_resp.json()
    assert indexers == [
        {
            "id": 70,
            "name": "Nyaa",
            "implementation": "torznab",
            "implementationName": "torznab",
            "configContract": "torznab",
            "enable": True,
            "priority": 25,
            "baseUrl": "https://nyaa.example/torznab",
            "categories": [7000, 7010, 7020],
            "settings": {"animeStandardFormatSearch": True},
            "downloadClientId": 50,
            "minimumSeeders": 5,
            "seedRatio": 2.5,
            "minimumSize": 10,
            "maximumSize": 500,
            "enableRss": True,
            "enableAutomaticSearch": True,
            "enableInteractiveSearch": False,
            "parentProwlarrId": None,
            "prowlarrIndexerId": None,
            "hasApiKey": True,
            "tags": ["favorite"],
        }
    ]
    assert "apiKey" not in indexers[0]
    assert "INDEXER-SECRET" not in indexers_resp.text

    clients_resp = client.get("/api/v1/downloadclient", headers=headers)
    assert clients_resp.status_code == 200, clients_resp.text
    clients = clients_resp.json()
    assert clients == [
        {
            "id": 50,
            "name": "qBittorrent",
            "implementation": "qbittorrent",
            "implementationName": "qbittorrent",
            "configContract": "qbittorrent",
            "enable": True,
            "priority": 1,
            "host": "http://qbittorrent",
            "port": 8080,
            "useSsl": False,
            "urlBase": "/qb",
            "username": "user",
            "hasPassword": True,
            "category": "manga",
            "postImportCategory": "manga-imported",
            "removeCompletedDownloads": True,
            "removeFailedDownloads": True,
            "recentPriority": "first",
            "olderPriority": "last",
            "initialState": "paused",
            "sequentialOrder": True,
            "firstLastFirst": False,
            "contentLayout": "original",
            "sourceId": "qbit-main",
            "downloadPath": "/downloads/manga",
            "mergeChapters": True,
            "tags": ["favorite"],
        }
    ]
    assert "password" not in clients[0]
    assert "CLIENT-SECRET" not in clients_resp.text

    mappings = client.get(
        "/api/v1/downloadclient/remotepathmapping", headers=headers
    ).json()
    assert mappings == [
        {
            "id": 60,
            "host": "qbittorrent",
            "remotePath": "/remote/downloads",
            "localPath": "/downloads",
        }
    ]


def test_api_v1_remaining_config_read_contracts(env):
    client = _client()
    headers = {"X-Api-Key": _api_key(env)}

    delay_profiles = client.get("/api/v1/delayprofile", headers=headers).json()
    assert delay_profiles == [
        {
            "id": 45,
            "name": "Torrent Delay",
            "order": 1,
            "enableUsenet": False,
            "enableTorrent": True,
            "usenetDelay": 0,
            "torrentDelay": 30,
            "bypassIfHighestQuality": True,
            "isDefault": False,
            "tags": ["favorite"],
        }
    ]

    import_lists = client.get("/api/v1/importlist", headers=headers).json()
    assert import_lists == [
        {
            "id": 80,
            "name": "AniList Favorites",
            "implementation": "anilist_user",
            "implementationName": "anilist_user",
            "configContract": "anilist_user",
            "enable": True,
            "qualityProfileId": 10,
            "rootFolderId": 1,
            "monitorMode": "all",
            "settings": {"username": "vinland"},
            "lastSync": "2026-01-04T00:00:00+00:00",
        }
    ]

    exclusions = client.get(
        "/api/v1/importlistexclusion", headers=headers
    ).json()
    assert exclusions == [
        {
            "id": 90,
            "source": "anilist",
            "externalId": "12345",
            "title": "Excluded Manga",
            "titleNormalized": "excluded manga",
            "reason": "Not wanted",
            "addedAt": "2026-01-05T00:00:00+00:00",
        }
    ]

    quality_definitions = client.get(
        "/api/v1/qualitydefinition", headers=headers
    ).json()
    assert quality_definitions == [
        {
            "quality": "cbz",
            "title": "Comic Book Zip",
            "minSize": 1.0,
            "maxSize": 500.0,
            "order": 1,
        }
    ]

    tags = client.get("/api/v1/tag", headers=headers).json()
    assert tags == [
        {
            "label": "dark",
            "seriesCount": 1,
            "indexerCount": 0,
            "delayProfileCount": 0,
            "releaseProfileCount": 0,
            "downloadClientCount": 0,
        },
        {
            "label": "favorite",
            "seriesCount": 1,
            "indexerCount": 1,
            "delayProfileCount": 1,
            "releaseProfileCount": 1,
            "downloadClientCount": 1,
        },
    ]


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


def test_api_v1_series_lookup_contract(env, monkeypatch):
    import routers.api_v1 as api_v1

    async def fake_search(query):
        assert query == "Vinland"
        return [
            {
                "title": "Different Saga",
                "source": "mangaupdates",
                "anilist_id": None,
                "mu_id": "mu-2",
                "mal_id": None,
                "cover_url": "",
                "status": "RELEASING",
                "volumes": 1,
                "chapters": None,
                "pub_year": 2024,
                "description": "Loose match",
            },
            {
                "title": "Vinland",
                "source": "anilist",
                "anilist_id": 123,
                "mu_id": None,
                "mal_id": 456,
                "cover_url": "https://example.invalid/cover.jpg",
                "status": "FINISHED",
                "volumes": 13,
                "chapters": 212,
                "pub_year": 2005,
                "description": "Exact match",
            },
        ], "anilist"

    monkeypatch.setattr(api_v1, "search_series", fake_search)

    resp = _client().get(
        "/api/v1/series/lookup",
        params={"term": " Vinland "},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [row["title"] for row in body] == ["Vinland", "Different Saga"]
    assert body[0] == {
        "title": "Vinland",
        "source": "anilist",
        "confidence": 100,
        "anilistId": 123,
        "mangaUpdatesId": None,
        "malId": 456,
        "coverUrl": "https://example.invalid/cover.jpg",
        "status": "FINISHED",
        "volumes": 13,
        "chapters": 212,
        "year": 2005,
        "description": "Exact match",
    }
    assert body[0]["confidence"] >= body[1]["confidence"]


def test_api_v1_series_lookup_rejects_blank_term(env, monkeypatch):
    import routers.api_v1 as api_v1

    async def should_not_search(_query):
        raise AssertionError("blank lookup should not call metadata search")

    monkeypatch.setattr(api_v1, "search_series", should_not_search)

    resp = _client().get(
        "/api/v1/series/lookup",
        params={"term": "  "},
        headers={"X-Api-Key": _api_key(env)},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "term is required"


def test_api_v1_series_lookup_requires_api_key(env):
    resp = _client().get("/api/v1/series/lookup", params={"term": "Vinland"})
    assert resp.status_code == 401


def test_api_v1_queue_history_and_wanted_contract(env):
    client = _client()
    headers = {"X-Api-Key": _api_key(env)}

    queue_resp = client.get("/api/v1/queue", headers=headers)
    assert queue_resp.status_code == 200, queue_resp.text
    assert queue_resp.headers["X-Total-Count"] == "3"
    queue = queue_resp.json()
    statuses = {row["id"]: row["status"] for row in queue}
    assert statuses["volume-103"] == "grabbed"
    assert statuses["import-201"] == "pending"
    assert statuses["pending-301"] == "pending"
    volume_row = next(row for row in queue if row["id"] == "volume-103")
    assert volume_row["queueType"] == "grabbed"
    assert volume_row["volumeLabel"] == "Vol 3"
    assert volume_row["downloadId"] == "abc123"

    import_only = client.get(
        "/api/v1/queue",
        params={"queueType": "import"},
        headers=headers,
    )
    assert [row["id"] for row in import_only.json()] == ["import-201"]
    assert import_only.headers["X-Total-Count"] == "1"

    pending_delay = client.get(
        "/api/v1/queue",
        params={"trackedDownloadStatus": "delay"},
        headers=headers,
    )
    assert [row["id"] for row in pending_delay.json()] == ["pending-301"]

    series_queue = client.get(
        "/api/v1/queue",
        params={"seriesId": 5, "page": 2, "pageSize": 1},
        headers=headers,
    )
    assert series_queue.status_code == 200, series_queue.text
    assert series_queue.headers["X-Total-Count"] == "3"
    assert series_queue.headers["X-Page"] == "2"
    assert series_queue.headers["X-Page-Size"] == "1"
    assert len(series_queue.json()) == 1

    bad_type = client.get(
        "/api/v1/queue",
        params={"queueType": "torrent"},
        headers=headers,
    )
    assert bad_type.status_code == 400
    assert bad_type.json()["error"] == "queueType must be grabbed, import, or pending"

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

    wanted_filtered = client.get(
        "/api/v1/wanted",
        params={"seriesId": 5, "term": "Vinland", "page": 1, "pageSize": 1},
        headers=headers,
    )
    assert wanted_filtered.status_code == 200, wanted_filtered.text
    assert [row["id"] for row in wanted_filtered.json()] == [102]
    assert wanted_filtered.headers["X-Total-Count"] == "1"
    assert wanted_filtered.headers["X-Page"] == "1"
    assert wanted_filtered.headers["X-Page-Size"] == "1"

    wanted_empty = client.get(
        "/api/v1/wanted",
        params={"seriesId": 999},
        headers=headers,
    )
    assert wanted_empty.status_code == 200, wanted_empty.text
    assert wanted_empty.json() == []
    assert wanted_empty.headers["X-Total-Count"] == "0"


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

    blocklist_filtered = client.get(
        "/api/v1/blocklist",
        params={
            "seriesId": 5,
            "protocol": "Torrent",
            "indexer": "nyaa",
            "term": "Bad",
            "page": 1,
            "pageSize": 1,
        },
        headers=headers,
    )
    assert blocklist_filtered.status_code == 200, blocklist_filtered.text
    assert [row["id"] for row in blocklist_filtered.json()] == [601]
    assert blocklist_filtered.headers["X-Total-Count"] == "1"
    assert blocklist_filtered.headers["X-Page-Size"] == "1"

    blocklist_empty = client.get(
        "/api/v1/blocklist",
        params={"term": "trusted"},
        headers=headers,
    )
    assert blocklist_empty.status_code == 200, blocklist_empty.text
    assert blocklist_empty.json() == []
    assert blocklist_empty.headers["X-Total-Count"] == "0"

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

    cutoff_filtered = client.get(
        "/api/v1/wanted/cutoff",
        params={"seriesId": 5, "term": "Vinland", "page": 1, "pageSize": 1},
        headers=headers,
    )
    assert cutoff_filtered.status_code == 200, cutoff_filtered.text
    assert [row["id"] for row in cutoff_filtered.json()] == [104]
    assert cutoff_filtered.headers["X-Total-Count"] == "1"
    assert cutoff_filtered.headers["X-Page-Size"] == "1"

    cutoff_empty = client.get(
        "/api/v1/wanted/cutoff",
        params={"seriesId": 999},
        headers=headers,
    )
    assert cutoff_empty.status_code == 200, cutoff_empty.text
    assert cutoff_empty.json() == []
    assert cutoff_empty.headers["X-Total-Count"] == "0"
