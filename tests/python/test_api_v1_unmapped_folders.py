import multiprocessing
import os
import shutil
import sqlite3
import sys
import tempfile
import threading

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
    key_dir = tempfile.mkdtemp(prefix="mangarr-unmapped-keys-")
    library_root = tempfile.mkdtemp(prefix="mangarr-unmapped-library-")
    known_dir = os.path.join(library_root, "Known Manga")
    unmapped_a = os.path.join(library_root, "Unmapped A")
    unmapped_b = os.path.join(library_root, "Unmapped B")
    hidden_dir = os.path.join(library_root, ".hidden")
    for path in (known_dir, unmapped_a, unmapped_b, hidden_dir):
        os.makedirs(path)
    with open(os.path.join(unmapped_a, "Unmapped A v01.cbz"), "wb") as f:
        f.write(b"1234")
    with open(os.path.join(unmapped_a, "notes.txt"), "wb") as f:
        f.write(b"note")
    with open(os.path.join(unmapped_b, "two.epub"), "wb") as f:
        f.write(b"12")
    with open(os.path.join(hidden_dir, "hidden.cbz"), "wb") as f:
        f.write(b"hidden")

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

    missing_root = os.path.join(library_root, "does-not-exist")
    with sqlite3.connect(db.name) as c:
        c.execute("DELETE FROM series")
        c.execute("DELETE FROM root_folders")
        c.execute(
            "INSERT INTO root_folders(id, path, label, is_default)"
            " VALUES(1, ?, 'Library', 1)",
            (library_root,),
        )
        c.execute(
            "INSERT INTO root_folders(id, path, label, is_default)"
            " VALUES(2, ?, 'Missing', 0)",
            (missing_root,),
        )
        c.execute(
            "INSERT INTO series"
            "(id, title, search_pattern, root_folder_id, enabled, monitored)"
            " VALUES(7, 'Known Manga', 'Known Manga', 1, 1, 1)"
        )

    try:
        yield {"db_path": db.name, "library_root": library_root}
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        security._SECRET_CIPHER = orig_cipher
        main.CONFIG.clear()
        main.CONFIG.update(orig_main_config)
        shared.CONFIG.clear()
        shared.CONFIG.update(orig_shared_config)
        shutil.rmtree(library_root, ignore_errors=True)
        shutil.rmtree(key_dir, ignore_errors=True)
        for ext in ("", "-wal", "-shm", ".adoption.lock"):
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


def _series_count(db_path: str) -> int:
    with sqlite3.connect(db_path) as c:
        return c.execute("SELECT COUNT(*) FROM series").fetchone()[0]


def _series_row(db_path: str, title: str):
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        return c.execute(
            "SELECT * FROM series WHERE title=?", (title,)
        ).fetchone()


def _volume_rows(db_path: str, series_id: int) -> list[sqlite3.Row]:
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        return c.execute(
            "SELECT * FROM volumes WHERE series_id=? ORDER BY volume_num",
            (series_id,),
        ).fetchall()


def _metadata_result(
    title: str,
    *,
    source: str = "anilist",
    anilist_id: int | None = None,
    manga_updates_id: str | None = None,
    mal_id: int | None = None,
    description: str = "Candidate",
) -> dict[str, object]:
    return {
        "title": title,
        "source": source,
        "anilist_id": anilist_id,
        "mal_id": mal_id,
        "mu_id": manga_updates_id,
        "cover_url": "https://example.invalid/cover.jpg",
        "status": "FINISHED",
        "volumes": 3,
        "chapters": 24,
        "pub_year": 2020,
        "description": description,
    }


def _match_request(
    env,
    monkeypatch: pytest.MonkeyPatch,
    results: list[dict[str, object]],
    *,
    query: str = "Unmapped A",
    source: str = "anilist",
):
    import routers.api_v1 as api_v1

    queries: list[str] = []

    async def fake_search(search_query: str):
        queries.append(search_query)
        return results, source

    monkeypatch.setattr(api_v1, "search_series", fake_search)
    response = _client().get(
        "/api/v1/rootfolder/1/unmappedfolders/matches",
        params={
            "path": os.path.join(env["library_root"], "Unmapped A"),
            "query": query,
        },
        headers={"X-Api-Key": _api_key(env["db_path"])},
    )
    assert response.status_code == 200, response.text
    assert queries == [query]
    return response.json()


def test_unmapped_folder_scan_excludes_known_and_hidden_dirs(env):
    resp = _client().get(
        "/api/v1/rootfolder/1/unmappedfolders",
        headers={"X-Api-Key": _api_key(env["db_path"])},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rootFolderId"] == 1
    assert body["path"] == env["library_root"]
    assert body["exists"] is True
    assert body["knownFolderCount"] == 1
    assert body["unmappedFolderCount"] == 2

    names = [item["name"] for item in body["unmappedFolders"]]
    assert names == ["Unmapped A", "Unmapped B"]
    by_name = {item["name"]: item for item in body["unmappedFolders"]}
    assert by_name["Unmapped A"]["mangaFileCount"] == 1
    assert by_name["Unmapped A"]["totalFileCount"] == 2
    assert by_name["Unmapped A"]["sizeBytes"] == 8
    assert by_name["Unmapped B"]["mangaFileCount"] == 1


def test_unmapped_folder_scan_handles_missing_root_without_mutation(env):
    before = _series_count(env["db_path"])
    resp = _client().get(
        "/api/v1/rootfolder/2/unmappedfolders",
        headers={"X-Api-Key": _api_key(env["db_path"])},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["exists"] is False
    assert body["unmappedFolderCount"] == 0
    assert body["unmappedFolders"] == []
    assert _series_count(env["db_path"]) == before


def test_unmapped_folder_scan_404s_for_unknown_root(env):
    resp = _client().get(
        "/api/v1/rootfolder/999/unmappedfolders",
        headers={"X-Api-Key": _api_key(env["db_path"])},
    )
    assert resp.status_code == 404


def test_settings_page_renders_unmapped_folder_adoption_controls(env):
    resp = _client().get("/settings")
    assert resp.status_code == 200, resp.text
    html = resp.text
    assert 'x-data="unmappedAdoption()"' in html
    assert "Scan unmapped folders" in html
    assert "Existing Library" in html
    assert "Metadata Matches" in html
    assert "adopt-quality-profile" in html
    assert "adopt-language-profile" in html
    assert "unmapped-match-query" in html
    assert "new URLSearchParams" in html
    assert "/api/v1/rootfolder/${rootId}/unmappedfolders" in html
    assert "/unmappedfolders/matches?${params.toString()}" in html
    assert "/api/v1/rootfolder/${this.activeRootId}/unmappedfolders/adopt" in html
    assert "payload.metadataTitle = this.selectedMatch.title" in html
    assert "payload.anilistId = this.selectedMatch.anilistId" in html

    load_matches_start = html.index("async loadMatches(folder)")
    load_matches_end = html.index("chooseMatch(match)", load_matches_start)
    load_matches_html = html[load_matches_start:load_matches_end]
    for field in (
        "matchState",
        "ambiguous",
        "topConfidence",
        "topMatchCount",
        "confidenceGap",
    ):
        assert f"this.{field} = data.{field}" in load_matches_html

    assert (
        "Multiple equally strong metadata matches found. Choose the correct series."
        in html
    )
    assert "matchState === 'unique'" in html
    assert "matchState === 'ambiguous'" in html
    assert "match.isTopMatch" in html
    assert "match.isTopTie" in html
    assert "this.selectedMatch = null" in load_matches_html
    assert "this.selectedMatch = this.matches[0]" not in load_matches_html
    assert "this.selectedMatch = data.matches[0]" not in load_matches_html

    proposal_start = html.index('<template x-for="match in matches"')
    proposal_end = html.index("</template>", proposal_start)
    proposal_html = html[proposal_start:proposal_end]
    assert ':key="match.matchKey"' in proposal_html
    assert "AniList ID" in proposal_html
    assert "match.anilistId" in proposal_html
    assert "MangaUpdates ID" in proposal_html
    assert "match.mangaUpdatesId" in proposal_html
    assert "match.year" in proposal_html
    assert "match.status" in proposal_html
    assert "match.matchBasis" in proposal_html
    assert '@click="chooseMatch(match)"' in proposal_html
    assert "Use" in proposal_html


def test_unmapped_folder_adoption_creates_series_and_rescans_files(env):
    target = os.path.join(env["library_root"], "Unmapped A")
    resp = _client().post(
        "/api/v1/rootfolder/1/unmappedfolders/adopt",
        json={"path": target},
        headers={"X-Api-Key": _api_key(env["db_path"])},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["series"]["title"] == "Unmapped A"
    assert body["series"]["path"] == target
    assert body["series"]["monitorMode"] == "missing"
    assert body["rescan"]["created"] == 1

    row = _series_row(env["db_path"], "Unmapped A")
    assert row is not None
    assert row["root_folder_id"] == 1
    assert row["search_pattern"] == "Unmapped A"
    assert row["monitored"] == 1
    assert row["monitor_mode"] == "missing"
    assert row["folder_name"] == "Unmapped A"
    assert row["quality_profile_id"] is not None
    assert row["language_profile_id"] is not None

    volumes = _volume_rows(env["db_path"], row["id"])
    assert len(volumes) == 1
    assert volumes[0]["volume_num"] == 1.0
    assert volumes[0]["status"] == "downloaded"
    assert volumes[0]["monitored"] == 1
    assert volumes[0]["import_path"].endswith("Unmapped A v01.cbz")

    scan = _client().get(
        "/api/v1/rootfolder/1/unmappedfolders",
        headers={"X-Api-Key": _api_key(env["db_path"])},
    )
    names = [item["name"] for item in scan.json()["unmappedFolders"]]
    assert names == ["Unmapped B"]


def test_unmapped_folder_adoption_can_seed_selected_metadata(env):
    target = os.path.join(env["library_root"], "Unmapped A")
    resp = _client().post(
        "/api/v1/rootfolder/1/unmappedfolders/adopt",
        json={
            "path": target,
            "metadataTitle": "Official Unmapped A",
            "anilistId": 123,
            "malId": 456,
            "mangaUpdatesId": "789",
            "coverUrl": "https://example.invalid/cover.jpg",
            "status": "FINISHED",
            "overview": "Matched metadata",
            "totalVolumes": 3,
            "totalChapters": 24,
            "year": 2020,
            "metadataSource": "anilist",
        },
        headers={"X-Api-Key": _api_key(env["db_path"])},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["series"]["title"] == "Unmapped A"
    assert body["series"]["searchPattern"] == "Official Unmapped A"
    assert body["series"]["folderName"] == "Unmapped A"
    assert body["series"]["anilistId"] == 123
    assert body["series"]["malId"] == 456
    assert body["series"]["mangaUpdatesId"] == "789"
    assert body["series"]["totalVolumes"] == 3
    assert body["series"]["totalChapters"] == 24
    assert body["series"]["year"] == 2020
    assert body["series"]["volumeCountSource"] == "anilist"

    row = _series_row(env["db_path"], "Unmapped A")
    assert row["search_pattern"] == "Official Unmapped A"
    assert row["anilist_id"] == 123
    assert row["mal_id"] == 456
    assert row["mu_id"] == "789"
    assert row["cover_url"] == "https://example.invalid/cover.jpg"
    assert row["status"] == "FINISHED"
    assert row["description"] == "Matched metadata"
    assert row["total_volumes"] == 3
    assert row["total_chapters"] == 24
    assert row["pub_year"] == 2020
    assert row["vol_count_source"] == "anilist"

    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        title_selection = c.execute(
            "SELECT value_json,selected_source,locked FROM series_metadata_fields"
            " WHERE series_id=? AND field_name='title'",
            (row["id"],),
        ).fetchone()
        title_candidate = c.execute(
            "SELECT value_json FROM series_metadata_candidates"
            " WHERE series_id=? AND field_name='title' AND source='local'",
            (row["id"],),
        ).fetchone()
    assert dict(title_selection) == {
        "value_json": '"Unmapped A"',
        "selected_source": "local",
        "locked": 1,
    }
    assert title_candidate["value_json"] == '"Unmapped A"'

    volumes = _volume_rows(env["db_path"], row["id"])
    assert [v["volume_num"] for v in volumes] == [1.0, 2.0, 3.0]
    assert [v["status"] for v in volumes] == ["downloaded", "wanted", "wanted"]


def test_local_title_unlock_relinquishes_recommendation_without_losing_history(
    env, monkeypatch
):
    import main
    import metadata_service
    from metadata_provenance import (
        apply_recommended_candidates,
        get_metadata_field_states,
        record_metadata_candidates,
        set_metadata_field_lock,
    )

    def close_background_task(coro, *, name):
        del name
        coro.close()

    monkeypatch.setattr(main, "create_background_task", close_background_task)
    response = _client().post(
        "/api/v1/rootfolder/1/unmappedfolders/adopt",
        json={
            "path": os.path.join(env["library_root"], "Unmapped A"),
            "metadataTitle": "Official Unmapped A",
            "anilistId": 123,
            "totalVolumes": 3,
            "totalChapters": 24,
            "metadataSource": "anilist",
        },
        headers={"X-Api-Key": _api_key(env["db_path"])},
    )
    assert response.status_code == 200, response.text
    series_id = response.json()["series"]["id"]

    def title_state():
        return next(
            field
            for field in get_metadata_field_states(series_id)
            if field["field_name"] == "title"
        )

    initial = title_state()
    assert initial["value"] == "Unmapped A"
    assert initial["selected_source"] == "local"
    assert initial["locked"] is True
    assert initial["recommended"]["source"] == "local"
    assert initial["recommended"]["is_current"] is True

    metadata_service._apply_anilist_record(
        series_id,
        {
            "anilist_id": 123,
            "mal_id": None,
            "title": "Official Unmapped A",
            "cover_url": None,
            "status": "FINISHED",
            "description": None,
            "pub_year": None,
            "volumes": 3,
            "chapters": 24,
        },
    )
    locked = title_state()
    assert locked["value"] == "Unmapped A"
    assert locked["recommended"]["source"] == "local"
    assert locked["recommended"]["is_current"] is True
    assert locked["pending"] is False
    assert locked["conflict"] is True

    set_metadata_field_lock(series_id, "title", False)
    unlocked = title_state()
    assert unlocked["value"] == "Unmapped A"
    assert unlocked["selected_source"] == "local"
    assert unlocked["locked"] is False
    assert unlocked["recommended"]["source"] == "anilist"
    assert unlocked["recommended"]["value"] == "Official Unmapped A"
    assert unlocked["pending"] is True
    assert unlocked["conflict"] is False
    assert {
        candidate["source"]: candidate["value"]
        for candidate in unlocked["candidates"]
    } == {
        "anilist": "Official Unmapped A",
        "local": "Unmapped A",
    }

    with sqlite3.connect(env["db_path"]) as db:
        assert db.execute(
            "SELECT title FROM series WHERE id=?", (series_id,)
        ).fetchone()[0] == "Unmapped A"

    safe_result = apply_recommended_candidates(series_id)
    assert {
        "field_name": "title",
        "source": "anilist",
        "value": "Official Unmapped A",
    } in safe_result["applied"]
    assert not any(
        skipped["field_name"] == "title" for skipped in safe_result["skipped"]
    )
    selected = title_state()
    assert selected["value"] == "Official Unmapped A"
    assert selected["selected_source"] == "anilist"
    assert selected["locked"] is False
    assert selected["recommended"]["source"] == "anilist"
    assert selected["recommended"]["is_current"] is True
    assert selected["conflict"] is False
    assert {
        candidate["source"]: candidate["value"]
        for candidate in selected["candidates"]
    } == {
        "anilist": "Official Unmapped A",
        "local": "Unmapped A",
    }

    record_metadata_candidates(
        series_id,
        "mangaupdates",
        {"title": "MangaUpdates Unmapped A"},
    )
    provider_conflict = title_state()
    assert provider_conflict["conflict"] is True
    safe_result = apply_recommended_candidates(series_id)
    assert {"field_name": "title", "reason": "conflict"} in safe_result["skipped"]
    with sqlite3.connect(env["db_path"]) as db:
        assert db.execute(
            "SELECT title FROM series WHERE id=?", (series_id,)
        ).fetchone()[0] == "Official Unmapped A"

    set_metadata_field_lock(series_id, "title", True)
    record_metadata_candidates(
        series_id,
        "anilist",
        {"title": "Future AniList Title"},
    )
    relocked = title_state()
    assert relocked["value"] == "Official Unmapped A"
    assert relocked["selected_source"] == "anilist"
    assert relocked["locked"] is True
    assert relocked["pending"] is False
    assert relocked["recommended"]["source"] == "local"
    assert relocked["conflict"] is True
    with sqlite3.connect(env["db_path"]) as db:
        assert db.execute(
            "SELECT title FROM series WHERE id=?", (series_id,)
        ).fetchone()[0] == "Official Unmapped A"


def test_unmapped_folder_match_proposals_search_metadata(env, monkeypatch):
    import routers.api_v1 as api_v1

    queries = []

    async def fake_search(query):
        queries.append(query)
        return [
            {
                "title": "Official Unmapped A",
                "source": "anilist",
                "anilist_id": 123,
                "mal_id": 456,
                "mu_id": None,
                "cover_url": "https://example.invalid/cover.jpg",
                "status": "FINISHED",
                "volumes": 3,
                "chapters": 24,
                "pub_year": 2020,
                "description": "Exact",
            },
            {
                "title": "Different Manga",
                "source": "mangaupdates",
                "anilist_id": None,
                "mal_id": None,
                "mu_id": "789",
                "cover_url": "",
                "status": "RELEASING",
                "volumes": 2,
                "chapters": None,
                "description": "Loose",
            },
        ], "anilist"

    monkeypatch.setattr(api_v1, "search_series", fake_search)

    target = os.path.join(env["library_root"], "Unmapped A")
    resp = _client().get(
        "/api/v1/rootfolder/1/unmappedfolders/matches",
        params={"path": target, "query": "Official Unmapped A"},
        headers={"X-Api-Key": _api_key(env["db_path"])},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rootFolderId"] == 1
    assert body["folder"]["name"] == "Unmapped A"
    assert body["query"] == "Official Unmapped A"
    assert body["source"] == "anilist"
    assert queries == ["Official Unmapped A"]
    assert body["matches"][0]["title"] == "Official Unmapped A"
    assert body["matches"][0]["confidence"] == 100
    assert body["matches"][0]["anilistId"] == 123
    assert body["matches"][0]["malId"] == 456
    assert body["matches"][1]["title"] == "Different Manga"
    assert body["matches"][1]["mangaUpdatesId"] == "789"
    assert body["matches"][0]["confidence"] >= body["matches"][1]["confidence"]


def test_unmapped_folder_match_unique_exact_preserves_payload_and_marks_top(
    env, monkeypatch
):
    body = _match_request(
        env,
        monkeypatch,
        [
            _metadata_result(
                "Unmapped A",
                anilist_id=123,
                mal_id=456,
                description="Exact",
            )
        ],
    )

    assert len(body["matches"]) == 1
    match = body["matches"][0]
    assert {
        key: match[key]
        for key in (
            "title",
            "source",
            "confidence",
            "anilistId",
            "mangaUpdatesId",
            "malId",
            "coverUrl",
            "status",
            "volumes",
            "chapters",
            "year",
            "description",
        )
    } == {
        "title": "Unmapped A",
        "source": "anilist",
        "confidence": 100,
        "anilistId": 123,
        "mangaUpdatesId": None,
        "malId": 456,
        "coverUrl": "https://example.invalid/cover.jpg",
        "status": "FINISHED",
        "volumes": 3,
        "chapters": 24,
        "year": 2020,
        "description": "Exact",
    }
    assert body["matchState"] == "unique"
    assert body["ambiguous"] is False
    assert body["topConfidence"] == 100
    assert body["topMatchCount"] == 1
    assert body["confidenceGap"] is None
    assert match["matchBasis"] == "exact_title"
    assert match["rank"] == 1
    assert match["isTopMatch"] is True
    assert match["isTopTie"] is False


def test_unmapped_folder_match_exact_title_keeps_distinct_anilist_identities(
    env, monkeypatch
):
    body = _match_request(
        env,
        monkeypatch,
        [
            _metadata_result("Unmapped A", anilist_id=101),
            _metadata_result("Unmapped A", anilist_id=202),
        ],
    )

    assert {match["anilistId"] for match in body["matches"]} == {101, 202}
    assert [match["confidence"] for match in body["matches"]] == [100, 100]
    assert body["matchState"] == "ambiguous"
    assert body["ambiguous"] is True
    assert body["topConfidence"] == 100
    assert body["topMatchCount"] == 2
    assert body["confidenceGap"] is None
    assert [match["rank"] for match in body["matches"]] == [1, 1]
    assert all(match["matchBasis"] == "exact_title" for match in body["matches"])
    assert all(match["isTopMatch"] is True for match in body["matches"])
    assert all(match["isTopTie"] is True for match in body["matches"])


def test_unmapped_folder_match_duplicate_anilist_rows_do_not_create_false_tie(
    env, monkeypatch
):
    body = _match_request(
        env,
        monkeypatch,
        [
            _metadata_result("Unmapped A", anilist_id=101, description="First"),
            _metadata_result("Unmapped A", anilist_id=101, description="Duplicate"),
        ],
    )

    assert len(body["matches"]) == 1
    assert body["matches"][0]["anilistId"] == 101
    assert body["matchState"] == "unique"
    assert body["ambiguous"] is False
    assert body["topConfidence"] == 100
    assert body["topMatchCount"] == 1
    assert body["confidenceGap"] is None
    assert body["matches"][0]["rank"] == 1
    assert body["matches"][0]["isTopMatch"] is True
    assert body["matches"][0]["isTopTie"] is False


def test_unmapped_folder_match_exact_title_keeps_distinct_mangaupdates_identities(
    env, monkeypatch
):
    body = _match_request(
        env,
        monkeypatch,
        [
            _metadata_result(
                "Unmapped A",
                source="mangaupdates",
                anilist_id=900,
                manga_updates_id="111",
            ),
            _metadata_result(
                "Unmapped A",
                source="mangaupdates",
                anilist_id=900,
                manga_updates_id="222",
            ),
            _metadata_result(
                "Unmapped A",
                source="mangaupdates",
                anilist_id=901,
                manga_updates_id="111",
                description="Duplicate MU identity",
            ),
        ],
        source="mangaupdates",
    )

    assert len(body["matches"]) == 2
    assert len({match["matchKey"] for match in body["matches"]}) == 2
    assert {match["matchKey"] for match in body["matches"]} == {
        "mangaupdates:111",
        "mangaupdates:222",
    }
    assert {match["mangaUpdatesId"] for match in body["matches"]} == {
        "111",
        "222",
    }
    assert body["matchState"] == "ambiguous"
    assert body["ambiguous"] is True
    assert body["topConfidence"] == 100
    assert body["topMatchCount"] == 2
    assert body["confidenceGap"] is None
    assert [match["rank"] for match in body["matches"]] == [1, 1]
    assert all(match["isTopMatch"] is True for match in body["matches"])
    assert all(match["isTopTie"] is True for match in body["matches"])


def test_unmapped_folder_match_uses_mal_identity_for_ties_and_deduplication(
    env, monkeypatch
):
    body = _match_request(
        env,
        monkeypatch,
        [
            _metadata_result("Unmapped A", source="mal", mal_id=101),
            _metadata_result("Unmapped A", source="mal", mal_id=202),
            _metadata_result(
                "Unmapped A",
                source="mal",
                mal_id=101,
                description="Duplicate MAL identity",
            ),
        ],
        source="mal",
    )

    assert len(body["matches"]) == 2
    assert {match["malId"] for match in body["matches"]} == {101, 202}
    assert {match["matchKey"] for match in body["matches"]} == {
        "myanimelist:101",
        "myanimelist:202",
    }
    assert body["matchState"] == "ambiguous"
    assert body["topMatchCount"] == 2
    assert all(match["isTopTie"] is True for match in body["matches"])


def test_unmapped_folder_match_unique_top_reports_confidence_gap(env, monkeypatch):
    body = _match_request(
        env,
        monkeypatch,
        [
            _metadata_result("Alpha", anilist_id=101),
            _metadata_result("Alpha Deluxe", anilist_id=202),
        ],
        query="Alpha",
    )

    top, runner_up = body["matches"]
    assert (top["confidence"], runner_up["confidence"]) == (100, 86)
    assert body["matchState"] == "unique"
    assert body["ambiguous"] is False
    assert body["topConfidence"] == 100
    assert body["topMatchCount"] == 1
    assert body["confidenceGap"] == 14
    assert (top["rank"], runner_up["rank"]) == (1, 2)
    assert top["matchBasis"] == "exact_title"
    assert runner_up["matchBasis"] == "title_contains"
    assert top["isTopMatch"] is True
    assert top["isTopTie"] is False
    assert runner_up["isTopMatch"] is False
    assert runner_up["isTopTie"] is False


def test_unmapped_folder_match_ambiguous_top_reports_gap_to_lower_alternative(
    env, monkeypatch
):
    body = _match_request(
        env,
        monkeypatch,
        [
            _metadata_result("Alpha", anilist_id=101),
            _metadata_result("Alpha", anilist_id=202),
            _metadata_result("Alpha Deluxe", anilist_id=303),
        ],
        query="Alpha",
    )

    assert [match["confidence"] for match in body["matches"]] == [100, 100, 86]
    assert body["matchState"] == "ambiguous"
    assert body["ambiguous"] is True
    assert body["topConfidence"] == 100
    assert body["topMatchCount"] == 2
    assert body["confidenceGap"] == 14
    assert [match["isTopMatch"] for match in body["matches"]] == [True, True, False]
    assert [match["isTopTie"] for match in body["matches"]] == [True, True, False]


def test_unmapped_folder_match_equal_fuzzy_scores_are_top_tie(env, monkeypatch):
    body = _match_request(
        env,
        monkeypatch,
        [
            _metadata_result("Alphi", anilist_id=101),
            _metadata_result("Alpho", anilist_id=202),
        ],
        query="Alpha",
    )

    assert [match["confidence"] for match in body["matches"]] == [80, 80]
    assert body["matchState"] == "ambiguous"
    assert body["ambiguous"] is True
    assert body["topConfidence"] == 80
    assert body["topMatchCount"] == 2
    assert body["confidenceGap"] is None
    assert [match["rank"] for match in body["matches"]] == [1, 1]
    assert all(match["matchBasis"] == "fuzzy_title" for match in body["matches"])
    assert all(match["isTopMatch"] is True for match in body["matches"])
    assert all(match["isTopTie"] is True for match in body["matches"])


def test_unmapped_folder_match_identityless_rows_are_not_optimistically_collapsed(
    env, monkeypatch
):
    body = _match_request(
        env,
        monkeypatch,
        [
            _metadata_result("Unmapped A", description="First unknown identity"),
            _metadata_result("Unmapped A", description="Second unknown identity"),
        ],
    )

    assert len(body["matches"]) == 2
    assert len({match["matchKey"] for match in body["matches"]}) == 2
    assert {match["description"] for match in body["matches"]} == {
        "First unknown identity",
        "Second unknown identity",
    }
    assert body["matchState"] == "ambiguous"
    assert body["ambiguous"] is True
    assert body["topConfidence"] == 100
    assert body["topMatchCount"] == 2
    assert body["confidenceGap"] is None
    assert all(match["isTopMatch"] is True for match in body["matches"])
    assert all(match["isTopTie"] is True for match in body["matches"])


def test_unmapped_folder_match_exact_tie_is_not_resolved_by_provider_order(
    env, monkeypatch
):
    import routers.api_v1 as api_v1

    candidates = [
        _metadata_result("Unmapped A", anilist_id=202),
        _metadata_result("Unmapped A", anilist_id=101),
    ]
    provider_results = [candidates, list(reversed(candidates))]

    async def fake_search(_query: str):
        return provider_results.pop(0), "anilist"

    monkeypatch.setattr(api_v1, "search_series", fake_search)
    request = {
        "params": {
            "path": os.path.join(env["library_root"], "Unmapped A"),
            "query": "Unmapped A",
        },
        "headers": {"X-Api-Key": _api_key(env["db_path"])},
    }

    first = (
        _client().get("/api/v1/rootfolder/1/unmappedfolders/matches", **request).json()
    )
    second = (
        _client().get("/api/v1/rootfolder/1/unmappedfolders/matches", **request).json()
    )

    # Regression: confidence-only stable sorting previously made provider order
    # choose which equally exact identity appeared first.
    assert [match["anilistId"] for match in first["matches"]] == [
        match["anilistId"] for match in second["matches"]
    ]
    assert first["matchState"] == second["matchState"] == "ambiguous"
    assert first["ambiguous"] is second["ambiguous"] is True
    assert first["topConfidence"] == second["topConfidence"] == 100
    assert first["topMatchCount"] == second["topMatchCount"] == 2
    assert first["confidenceGap"] is second["confidenceGap"] is None
    assert all(match["isTopTie"] is True for match in first["matches"])


def test_unmapped_folder_match_empty_provider_result_has_empty_state(env, monkeypatch):
    body = _match_request(env, monkeypatch, [])

    assert body["rootFolderId"] == 1
    assert body["folder"]["name"] == "Unmapped A"
    assert body["query"] == "Unmapped A"
    assert body["source"] == "anilist"
    assert body["matches"] == []
    assert body["matchState"] == "none"
    assert body["ambiguous"] is False
    assert body["topConfidence"] is None
    assert body["topMatchCount"] == 0
    assert body["confidenceGap"] is None


def test_unmapped_folder_match_endpoint_is_read_only(env, monkeypatch):
    with sqlite3.connect(env["db_path"]) as observer:
        before_version = observer.execute("PRAGMA data_version").fetchone()[0]
        before_series = observer.execute(
            "SELECT id,title,anilist_id,mu_id FROM series ORDER BY id"
        ).fetchall()

        _match_request(
            env,
            monkeypatch,
            [_metadata_result("Unmapped A", anilist_id=101)],
        )

        after_version = observer.execute("PRAGMA data_version").fetchone()[0]
        after_series = observer.execute(
            "SELECT id,title,anilist_id,mu_id FROM series ORDER BY id"
        ).fetchall()

    assert after_version == before_version
    assert after_series == before_series


@pytest.mark.parametrize(
    ("query", "expected_basis"),
    [
        ("123", None),
        ("https://anilist.co/manga/123/unmapped-a", "exact_provider_id"),
    ],
)
def test_unmapped_folder_match_keeps_direct_anilist_lookup_unique(
    env, monkeypatch, query, expected_basis
):
    body = _match_request(
        env,
        monkeypatch,
        [_metadata_result("Canonical AniList Title", anilist_id=123)],
        query=query,
    )

    assert body["query"] == query
    assert len(body["matches"]) == 1
    assert body["matches"][0]["anilistId"] == 123
    assert body["matchState"] == "unique"
    assert body["ambiguous"] is False
    assert body["topMatchCount"] == 1
    assert body["confidenceGap"] is None
    assert body["matches"][0]["isTopMatch"] is True
    assert body["matches"][0]["isTopTie"] is False
    if expected_basis is not None:
        assert body["matches"][0]["matchBasis"] == expected_basis
    # Bare numeric provenance is resolved inside metadata.search_series. This
    # route only receives the resulting row, so the numeric case deliberately
    # makes no exact_provider_id assertion at this layer.


def test_unmapped_folder_ambiguous_match_adoption_persists_explicit_identity(
    env, monkeypatch
):
    import main

    body = _match_request(
        env,
        monkeypatch,
        [
            _metadata_result("Unmapped A", anilist_id=101),
            _metadata_result("Unmapped A", anilist_id=202),
        ],
    )
    selected = next(match for match in body["matches"] if match["anilistId"] == 202)

    def close_background_task(coro, *, name):
        del name
        coro.close()

    monkeypatch.setattr(main, "create_background_task", close_background_task)
    response = _client().post(
        "/api/v1/rootfolder/1/unmappedfolders/adopt",
        json={
            "path": os.path.join(env["library_root"], "Unmapped A"),
            "metadataTitle": selected["title"],
            "metadataSource": selected["source"],
            "anilistId": selected["anilistId"],
        },
        headers={"X-Api-Key": _api_key(env["db_path"])},
    )

    assert response.status_code == 200, response.text
    assert response.json()["series"]["anilistId"] == 202
    row = _series_row(env["db_path"], "Unmapped A")
    assert row["anilist_id"] == 202
    assert row["anilist_id"] != 101


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        ({"anilistId": "202"}, "anilistId must be an integer"),
        ({"monitored": 1}, "monitored must be a boolean"),
        ({"qualityProfileId": "1"}, "qualityProfileId must be an integer"),
    ],
)
def test_unmapped_folder_ambiguity_contract_does_not_relax_adoption_validation(
    env, payload, expected_error
):
    before = _series_count(env["db_path"])
    response = _client().post(
        "/api/v1/rootfolder/1/unmappedfolders/adopt",
        json={
            "path": os.path.join(env["library_root"], "Unmapped A"),
            **payload,
        },
        headers={"X-Api-Key": _api_key(env["db_path"])},
    )

    assert response.status_code == 400
    assert response.json()["error"] == expected_error
    assert _series_count(env["db_path"]) == before


def test_unmapped_folder_match_proposals_reject_non_unmapped_path(env, monkeypatch):
    import routers.api_v1 as api_v1

    async def should_not_search(_query):
        raise AssertionError("metadata search should not run")

    monkeypatch.setattr(api_v1, "search_series", should_not_search)

    resp = _client().get(
        "/api/v1/rootfolder/1/unmappedfolders/matches",
        params={"path": os.path.join(env["library_root"], "Known Manga")},
        headers={"X-Api-Key": _api_key(env["db_path"])},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "path is not an unmapped folder"


def test_unmapped_folder_adoption_rejects_already_mapped_path(env):
    before = _series_count(env["db_path"])
    resp = _client().post(
        "/api/v1/rootfolder/1/unmappedfolders/adopt",
        json={"path": os.path.join(env["library_root"], "Known Manga")},
        headers={"X-Api-Key": _api_key(env["db_path"])},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "path is already mapped"
    assert _series_count(env["db_path"]) == before


def test_unmapped_folder_adoption_rejects_sequential_physical_alias(env):
    import library_scan

    canonical = os.path.join(env["library_root"], "Canonical")
    alias = os.path.join(env["library_root"], "Alias")
    os.mkdir(canonical)
    os.symlink(canonical, alias)
    with open(os.path.join(canonical, "Canonical v01.cbz"), "wb") as archive:
        archive.write(b"archive")

    first = library_scan.adopt_unmapped_folder(1, canonical)
    second = library_scan.adopt_unmapped_folder(1, alias)

    assert first.ok
    assert not second.ok
    assert second.error == "path is already mapped"
    with sqlite3.connect(env["db_path"]) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM series WHERE folder_name IN ('Canonical','Alias')"
            ).fetchone()[0]
            == 1
        )


def test_unmapped_folder_adoption_serializes_concurrent_physical_aliases(env):
    import library_scan

    canonical = os.path.join(env["library_root"], "Canonical")
    alias = os.path.join(env["library_root"], "Alias")
    os.mkdir(canonical)
    os.symlink(canonical, alias)
    with open(os.path.join(canonical, "Canonical v01.cbz"), "wb") as archive:
        archive.write(b"archive")

    barrier = threading.Barrier(2)
    results = []
    errors: list[BaseException] = []

    def adopt(path: str) -> None:
        try:
            barrier.wait(timeout=5)
            results.append(library_scan.adopt_unmapped_folder(1, path))
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=adopt, args=(canonical,)),
        threading.Thread(target=adopt, args=(alias,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert not errors
    assert len(results) == 2
    assert sorted(result.ok for result in results) == [False, True]
    loser = next(result for result in results if not result.ok)
    assert loser.error == "path is already mapped"
    with sqlite3.connect(env["db_path"]) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM series WHERE folder_name IN ('Canonical','Alias')"
            ).fetchone()[0]
            == 1
        )


def test_unmapped_folder_adoption_serializes_cross_process_physical_aliases(env):
    import library_scan

    canonical = os.path.join(env["library_root"], "Canonical")
    alias = os.path.join(env["library_root"], "Alias")
    os.mkdir(canonical)
    os.symlink(canonical, alias)
    with open(os.path.join(canonical, "Canonical v01.cbz"), "wb") as archive:
        archive.write(b"archive")

    context = multiprocessing.get_context("fork")
    start = context.Event()
    results = context.Queue()

    def adopt(path: str) -> None:
        start.wait(timeout=5)
        result = library_scan.adopt_unmapped_folder(1, path)
        results.put((result.ok, result.error))

    processes = [
        context.Process(target=adopt, args=(canonical,)),
        context.Process(target=adopt, args=(alias,)),
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=15)

    assert not any(process.is_alive() for process in processes)
    assert [process.exitcode for process in processes] == [0, 0]
    outcomes = sorted(results.get(timeout=2) for _ in processes)
    assert outcomes == [(False, "path is already mapped"), (True, None)]
    with sqlite3.connect(env["db_path"]) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM series"
                " WHERE folder_name IN ('Canonical','Alias')"
            ).fetchone()[0]
            == 1
        )


def test_unmapped_folder_adoption_rejects_path_outside_root(env):
    outside = tempfile.mkdtemp(prefix="mangarr-unmapped-outside-")
    try:
        before = _series_count(env["db_path"])
        resp = _client().post(
            "/api/v1/rootfolder/1/unmappedfolders/adopt",
            json={"path": outside},
            headers={"X-Api-Key": _api_key(env["db_path"])},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "path is not an unmapped folder"
        assert _series_count(env["db_path"]) == before
    finally:
        shutil.rmtree(outside, ignore_errors=True)


def test_unmapped_folder_adoption_rejects_child_symlink_outside_root(env):
    import library_scan

    outside = tempfile.mkdtemp(prefix="mangarr-unmapped-outside-")
    alias = os.path.join(env["library_root"], "Outside Alias")
    os.symlink(outside, alias)
    before = _series_count(env["db_path"])
    try:
        result = library_scan.adopt_unmapped_folder(1, alias)

        assert not result.ok
        assert result.error == "path is not an unmapped folder"
        assert _series_count(env["db_path"]) == before
    finally:
        shutil.rmtree(outside, ignore_errors=True)


def test_unmapped_folder_adoption_pins_existing_folder_for_custom_title(env):
    resp = _client().post(
        "/api/v1/rootfolder/1/unmappedfolders/adopt",
        json={
            "path": os.path.join(env["library_root"], "Unmapped A"),
            "title": "Other Title",
            "metadataTitle": "Official Other Title",
        },
        headers={"X-Api-Key": _api_key(env["db_path"])},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["series"]["title"] == "Other Title"
    assert body["series"]["searchPattern"] == "Official Other Title"
    assert body["series"]["folderName"] == "Unmapped A"
    assert body["series"]["path"] == os.path.join(env["library_root"], "Unmapped A")

    row = _series_row(env["db_path"], "Other Title")
    assert row is not None
    assert row["folder_name"] == "Unmapped A"

    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        title_selection = c.execute(
            "SELECT value_json,selected_source,locked FROM series_metadata_fields"
            " WHERE series_id=? AND field_name='title'",
            (row["id"],),
        ).fetchone()
        title_candidate = c.execute(
            "SELECT value_json FROM series_metadata_candidates"
            " WHERE series_id=? AND field_name='title' AND source='manual'",
            (row["id"],),
        ).fetchone()
    assert dict(title_selection) == {
        "value_json": '"Other Title"',
        "selected_source": "manual",
        "locked": 1,
    }
    assert title_candidate["value_json"] == '"Other Title"'

    import rescan
    import shared

    with shared.get_db() as db:
        assert rescan._series_library_dir(db, row["id"]) == os.path.join(
            env["library_root"], "Unmapped A"
        )


def test_unmapped_folder_adoption_validates_profile_ids(env):
    before = _series_count(env["db_path"])
    resp = _client().post(
        "/api/v1/rootfolder/1/unmappedfolders/adopt",
        json={
            "path": os.path.join(env["library_root"], "Unmapped A"),
            "qualityProfileId": 999999,
        },
        headers={"X-Api-Key": _api_key(env["db_path"])},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "qualityProfileId not found"
    assert _series_count(env["db_path"]) == before
