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
    with open(os.path.join(unmapped_a, "one.cbz"), "wb") as f:
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
