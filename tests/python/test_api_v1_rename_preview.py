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
    key_dir = tempfile.mkdtemp(prefix="mangarr-rename-keys-")
    library_root = tempfile.mkdtemp(prefix="mangarr-rename-library-")
    series_dir = os.path.join(library_root, "Plan Manga")
    os.makedirs(series_dir)

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

    old_v1 = os.path.join(series_dir, "bad-name.cbz")
    old_v2 = os.path.join(series_dir, "bad-v2.cbz")
    target_v2 = os.path.join(series_dir, "Plan Manga v02.cbz")
    missing_v3 = os.path.join(series_dir, "missing-v3.cbz")
    old_ch5 = os.path.join(series_dir, "chapter-old.cbz")
    for path in (old_v1, old_v2, target_v2, old_ch5):
        with open(path, "wb") as f:
            f.write(b"cbz")

    with sqlite3.connect(db.name) as c:
        c.execute(
            "INSERT OR REPLACE INTO settings(key, value)"
            " VALUES('file_format', '{Series Title} v{Volume:02d}')"
        )
        c.execute(
            "INSERT OR REPLACE INTO settings(key, value)"
            " VALUES('chapter_format', '{Series Title} c{Chapter:03d}')"
        )
        c.execute("DELETE FROM volumes")
        c.execute("DELETE FROM chapters")
        c.execute("DELETE FROM series")
        c.execute("DELETE FROM root_folders")
        c.execute(
            "INSERT INTO root_folders(id, path, label, is_default)"
            " VALUES(1, ?, 'Library', 1)",
            (library_root,),
        )
        c.execute(
            "INSERT INTO series"
            "(id, title, search_pattern, root_folder_id, enabled, monitored,"
            " pub_year)"
            " VALUES(7, 'Plan Manga', 'Plan Manga', 1, 1, 1, 2020)"
        )
        c.execute(
            "INSERT INTO volumes(id, series_id, volume_num, status, import_path)"
            " VALUES(101, 7, 1.0, 'downloaded', ?)",
            (old_v1,),
        )
        c.execute(
            "INSERT INTO volumes(id, series_id, volume_num, status, import_path)"
            " VALUES(102, 7, 2.0, 'downloaded', ?)",
            (old_v2,),
        )
        c.execute(
            "INSERT INTO volumes(id, series_id, volume_num, status, import_path)"
            " VALUES(103, 7, 3.0, 'downloaded', ?)",
            (missing_v3,),
        )
        c.execute(
            "INSERT INTO chapters"
            "(id, series_id, volume_id, chapter_num, status, import_path)"
            " VALUES(201, 7, 101, 5.0, 'downloaded', ?)",
            (old_ch5,),
        )

    main.load_config()

    try:
        yield {
            "db_path": db.name,
            "series_dir": series_dir,
            "old_v1": old_v1,
            "old_v2": old_v2,
            "target_v2": target_v2,
            "missing_v3": missing_v3,
            "old_ch5": old_ch5,
        }
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


def _volume_paths(db_path: str) -> dict[int, str]:
    with sqlite3.connect(db_path) as c:
        return {
            row[0]: row[1]
            for row in c.execute("SELECT id, import_path FROM volumes ORDER BY id")
        }


def _chapter_paths(db_path: str) -> dict[int, str]:
    with sqlite3.connect(db_path) as c:
        return {
            row[0]: row[1]
            for row in c.execute("SELECT id, import_path FROM chapters ORDER BY id")
        }


def test_rename_preview_reports_dry_run_plan(env):
    resp = _client().get(
        "/api/v1/rename/series/7/preview",
        headers={"X-Api-Key": _api_key(env["db_path"])},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["seriesId"] == 7
    assert body["seriesTitle"] == "Plan Manga"
    assert body["seriesPath"] == env["series_dir"]
    assert body["fileFormat"] == "{Series Title} v{Volume:02d}"
    assert body["chapterFormat"] == "{Series Title} c{Chapter:03d}"
    assert body["total"] == 4
    assert body["changed"] == 4
    assert body["renameable"] == 2
    assert body["conflicts"] == 2

    by_id = {item["id"]: item for item in body["items"]}
    assert by_id[101]["newName"] == "Plan Manga v01.cbz"
    assert by_id[101]["canRename"] is True
    assert by_id[102]["conflict"] == "target_exists"
    assert by_id[103]["conflict"] == "source_missing"
    assert by_id[201]["type"] == "chapter"
    assert by_id[201]["label"] == "Ch.005"
    assert by_id[201]["newName"] == "Plan Manga c005.cbz"
    assert by_id[201]["canRename"] is True


def test_rename_preview_is_read_only(env):
    before = _volume_paths(env["db_path"])
    resp = _client().get(
        "/api/v1/rename/series/7/preview",
        headers={"X-Api-Key": _api_key(env["db_path"])},
    )
    assert resp.status_code == 200
    after = _volume_paths(env["db_path"])
    assert after == before
    assert os.path.exists(env["old_v1"])
    assert not os.path.exists(os.path.join(env["series_dir"], "Plan Manga v01.cbz"))


def test_rename_preview_404_for_unknown_series(env):
    resp = _client().get(
        "/api/v1/rename/series/999/preview",
        headers={"X-Api-Key": _api_key(env["db_path"])},
    )
    assert resp.status_code == 404


def test_rename_execute_moves_all_renameable_items(env):
    client = _client()
    headers = {"X-Api-Key": _api_key(env["db_path"])}
    new_v1 = os.path.join(env["series_dir"], "Plan Manga v01.cbz")
    new_ch5 = os.path.join(env["series_dir"], "Plan Manga c005.cbz")

    resp = client.post("/api/v1/rename/series/7", json={}, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["seriesId"] == 7
    assert body["requested"] == 4
    assert body["renamed"] == 2
    assert body["skipped"] == 2
    assert body["errors"] == 0
    statuses = {(row["type"], row["id"]): row["status"] for row in body["results"]}
    assert statuses[("volume", 101)] == "renamed"
    assert statuses[("chapter", 201)] == "renamed"
    assert statuses[("volume", 102)] == "skipped"
    assert statuses[("volume", 103)] == "skipped"

    assert not os.path.exists(env["old_v1"])
    assert os.path.exists(new_v1)
    assert os.path.exists(env["old_v2"])
    assert os.path.exists(env["target_v2"])
    assert not os.path.exists(env["old_ch5"])
    assert os.path.exists(new_ch5)

    assert _volume_paths(env["db_path"])[101] == new_v1
    assert _volume_paths(env["db_path"])[102] == env["old_v2"]
    assert _chapter_paths(env["db_path"])[201] == new_ch5
    with sqlite3.connect(env["db_path"]) as c:
        history = c.execute(
            "SELECT event_type, volume_label, source_title, data"
            " FROM history WHERE event_type='file_renamed'"
            " ORDER BY id"
        ).fetchall()
    assert len(history) == 2
    assert history[0][0] == "file_renamed"
    assert history[0][1] == "Vol 1"
    assert history[0][2] == "bad-name.cbz"


def test_rename_execute_can_limit_to_selected_ids(env):
    new_v1 = os.path.join(env["series_dir"], "Plan Manga v01.cbz")
    new_ch5 = os.path.join(env["series_dir"], "Plan Manga c005.cbz")
    resp = _client().post(
        "/api/v1/rename/series/7",
        json={"volumeIds": [101]},
        headers={"X-Api-Key": _api_key(env["db_path"])},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["requested"] == 1
    assert body["renamed"] == 1
    assert body["skipped"] == 0
    assert body["errors"] == 0

    assert os.path.exists(new_v1)
    assert not os.path.exists(env["old_v1"])
    assert os.path.exists(env["old_ch5"])
    assert not os.path.exists(new_ch5)
    assert _volume_paths(env["db_path"])[101] == new_v1
    assert _chapter_paths(env["db_path"])[201] == env["old_ch5"]


def test_rename_execute_reports_selected_conflicts_without_moving(env):
    resp = _client().post(
        "/api/v1/rename/series/7",
        json={"volumeIds": [102, 103]},
        headers={"X-Api-Key": _api_key(env["db_path"])},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["requested"] == 2
    assert body["renamed"] == 0
    assert body["skipped"] == 2
    conflicts = {row["id"]: row["conflict"] for row in body["results"]}
    assert conflicts == {102: "target_exists", 103: "source_missing"}
    assert os.path.exists(env["old_v2"])
    assert _volume_paths(env["db_path"])[102] == env["old_v2"]


def test_rename_execute_validates_body(env):
    resp = _client().post(
        "/api/v1/rename/series/7",
        json={"volumeIds": "101"},
        headers={"X-Api-Key": _api_key(env["db_path"])},
    )
    assert resp.status_code == 400
    assert "volumeIds" in resp.json()["error"]


def test_rename_execute_404_for_unknown_series(env):
    resp = _client().post(
        "/api/v1/rename/series/999",
        json={},
        headers={"X-Api-Key": _api_key(env["db_path"])},
    )
    assert resp.status_code == 404
