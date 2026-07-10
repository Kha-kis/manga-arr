import asyncio
import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


@pytest.fixture
def env(tmp_path):
    import main, shared, security

    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close()
    os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-import-list-exclusions-")

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

    library_root = tmp_path / "library"
    library_root.mkdir()
    with sqlite3.connect(db.name) as c:
        c.execute("DELETE FROM root_folders")
        c.execute(
            "INSERT INTO root_folders(id, path, label, is_default)"
            " VALUES(1, ?, 'Manga', 1)",
            (str(library_root),),
        )
        c.execute(
            "INSERT INTO import_lists(id, name, type, enabled, settings,"
            " monitor_mode, root_folder_id)"
            " VALUES(10, 'AniList', 'anilist_user', 1, '{}', 'all', 1)"
        )

    try:
        yield {"db_path": db.name}
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


def _csrf(tag: str = "test"):
    tok = f"csrf-{tag}-" + "x" * 30
    return {
        "cookies": {"csrftoken": tok},
        "headers": {"X-CSRFToken": tok},
    }


def _form(csrf, **fields):
    return {"csrf_token": csrf["headers"]["X-CSRFToken"], **fields}


def _series_titles(db_path: str) -> list[str]:
    with sqlite3.connect(db_path) as c:
        return [
            row[0]
            for row in c.execute("SELECT title FROM series ORDER BY title").fetchall()
        ]


def test_import_list_exclusion_create_and_delete_routes(env):
    client = _client()
    csrf = _csrf("exclusion-route")

    resp = client.post(
        "/import-lists/exclusions",
        data=_form(
            csrf,
            source="anilist_user",
            external_id="42",
            title="Blocked Manga",
            reason="already owned elsewhere",
        ),
        **csrf,
        follow_redirects=False,
    )
    assert resp.status_code in (200, 303), resp.text

    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT id, source, external_id, title, title_normalized, reason"
            " FROM import_list_exclusions"
        ).fetchone()
    assert row is not None
    assert row["source"] == "anilist_user"
    assert row["external_id"] == "42"
    assert row["title_normalized"] == "blocked manga"
    assert row["reason"] == "already owned elsewhere"

    resp = client.post(
        f"/import-lists/exclusions/{row['id']}/delete",
        **csrf,
        follow_redirects=False,
    )
    assert resp.status_code in (200, 303), resp.text
    with sqlite3.connect(env["db_path"]) as c:
        count = c.execute(
            "SELECT COUNT(*) FROM import_list_exclusions"
        ).fetchone()[0]
    assert count == 0


def test_import_list_exclusions_skip_sync_by_external_id_and_title(env):
    from routers import import_lists as _il

    with sqlite3.connect(env["db_path"]) as c:
        c.execute(
            "INSERT INTO import_list_exclusions(source, external_id, title,"
            " title_normalized)"
            " VALUES('anilist_user', '42', 'Blocked ID', 'blocked id')"
        )
        c.execute(
            "INSERT INTO import_list_exclusions(source, title, title_normalized)"
            " VALUES('anilist_user', 'Blocked By Title', 'blocked by title')"
        )
        c.execute(
            "INSERT INTO import_list_exclusions(source, title, title_normalized)"
            " VALUES('custom_rss', 'Source Specific', 'source specific')"
        )

    async def _fake_list(*a, **kw):
        return [
            {
                "anilist_id": 42,
                "title": "Blocked ID",
                "search_pattern": "Blocked ID",
                "cover_url": "",
                "status": "RELEASING",
                "total_volumes": 1,
            },
            {
                "anilist_id": 43,
                "title": "  Blocked   By Title  ",
                "search_pattern": "Blocked By Title",
                "cover_url": "",
                "status": "RELEASING",
                "total_volumes": 1,
            },
            {
                "anilist_id": 44,
                "title": "Source Specific",
                "search_pattern": "Source Specific",
                "cover_url": "",
                "status": "RELEASING",
                "total_volumes": 1,
            },
            {
                "anilist_id": 45,
                "title": "Allowed Manga",
                "search_pattern": "Allowed Manga",
                "cover_url": "",
                "status": "RELEASING",
                "total_volumes": 1,
            },
        ]

    def _close_background_coro(coro, name):
        close = getattr(coro, "close", None)
        if close:
            close()
        return None

    with patch.object(_il, "_fetch_list", _fake_list), patch(
        "main.create_background_task", _close_background_coro
    ):
        asyncio.run(
            _il._sync_list(
                {
                    "id": 10,
                    "name": "AniList",
                    "type": "anilist_user",
                    "settings": "{}",
                    "monitor_mode": "all",
                    "quality_profile_id": None,
                    "root_folder_id": 1,
                }
            )
        )

    assert _series_titles(env["db_path"]) == ["Allowed Manga", "Source Specific"]
    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        event = c.execute(
            "SELECT message FROM events WHERE event_type='import_list_sync'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert event is not None
    assert "Synced 4 items" in event["message"]
    assert "added 2 new" in event["message"]
    assert "skipped 2 excluded" in event["message"]


def test_import_list_exclusion_rejects_missing_key(env):
    client = _client()
    csrf = _csrf("exclusion-bad")
    resp = client.post(
        "/import-lists/exclusions",
        data=_form(csrf, source="anilist_user", external_id="", title=""),
        **csrf,
        follow_redirects=False,
    )
    assert resp.status_code == 400
