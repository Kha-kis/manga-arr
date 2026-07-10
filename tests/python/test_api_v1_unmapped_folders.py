import os
import shutil
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
    assert "adopt-quality-profile" in html
    assert "adopt-language-profile" in html
    assert "/api/v1/rootfolder/${rootId}/unmappedfolders" in html
    assert "/api/v1/rootfolder/${this.activeRootId}/unmappedfolders/adopt" in html


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


def test_unmapped_folder_match_proposals_search_metadata(env, monkeypatch):
    import routers.api_v1 as api_v1

    async def fake_search(query):
        assert query == "Unmapped A"
        return [
            {
                "title": "Unmapped A",
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
        params={"path": target},
        headers={"X-Api-Key": _api_key(env["db_path"])},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rootFolderId"] == 1
    assert body["folder"]["name"] == "Unmapped A"
    assert body["query"] == "Unmapped A"
    assert body["source"] == "anilist"
    assert body["matches"][0]["title"] == "Unmapped A"
    assert body["matches"][0]["confidence"] == 100
    assert body["matches"][0]["anilistId"] == 123
    assert body["matches"][0]["malId"] == 456
    assert body["matches"][1]["title"] == "Different Manga"
    assert body["matches"][1]["mangaUpdatesId"] == "789"
    assert body["matches"][0]["confidence"] >= body["matches"][1]["confidence"]


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


def test_unmapped_folder_adoption_rejects_title_that_maps_elsewhere(env):
    before = _series_count(env["db_path"])
    resp = _client().post(
        "/api/v1/rootfolder/1/unmappedfolders/adopt",
        json={
            "path": os.path.join(env["library_root"], "Unmapped A"),
            "title": "Other Title",
        },
        headers={"X-Api-Key": _api_key(env["db_path"])},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "path does not match title"
    assert _series_count(env["db_path"]) == before


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
