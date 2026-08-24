"""PR B: every series-creation path resolves a root_folder_id or fails
with a clear error. Pre-PR, these paths left root_folder_id NULL and
the library-destination code relied on a save_path fallback; post-PR,
no series row can be created without a folder."""
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


@pytest.fixture
def env():
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-rfreq-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()

    try:
        yield db.name
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


# ── resolver helper ──────────────────────────────────────────────────────────

def test_resolver_prefers_explicit_id_when_valid(env):
    import main
    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
        c.execute("INSERT INTO root_folders(id, path, label, is_default) VALUES(7, '/a', 'A', 0)")
        c.execute("INSERT INTO root_folders(id, path, label, is_default) VALUES(8, '/b', 'B', 1)")
    with main.get_db() as db:
        assert main.resolve_root_folder_id(db, preferred_id=7) == 7
        assert main.resolve_root_folder_id(db, preferred_id=8) == 8


def test_resolver_ignores_invalid_preferred_id(env):
    """Preferred ID that doesn't exist → fall through to default."""
    import main
    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
        c.execute("INSERT INTO root_folders(id, path, label, is_default) VALUES(8, '/b', 'B', 1)")
    with main.get_db() as db:
        assert main.resolve_root_folder_id(db, preferred_id=999) == 8


def test_resolver_picks_default_when_no_preferred(env):
    import main
    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
        c.execute("INSERT INTO root_folders(id, path, label, is_default) VALUES(3, '/a', 'A', 0)")
        c.execute("INSERT INTO root_folders(id, path, label, is_default) VALUES(4, '/b', 'B', 1)")
    with main.get_db() as db:
        assert main.resolve_root_folder_id(db) == 4


def test_resolver_falls_back_to_lowest_id_when_no_default(env):
    import main
    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
        c.execute("INSERT INTO root_folders(id, path, label, is_default) VALUES(10, '/a', 'A', 0)")
        c.execute("INSERT INTO root_folders(id, path, label, is_default) VALUES(11, '/b', 'B', 0)")
    with main.get_db() as db:
        assert main.resolve_root_folder_id(db) == 10


def test_resolver_returns_none_when_no_folders(env):
    import main
    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
    with main.get_db() as db:
        assert main.resolve_root_folder_id(db) is None


# ── import_lists path ────────────────────────────────────────────────────────

def test_import_list_skips_when_no_root_folders(env):
    """_sync_list should stop and log an error instead of creating
    series with root_folder_id NULL."""
    import asyncio
    import main
    from routers import import_lists as _il
    from unittest.mock import patch

    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")

    async def _fake_list(*a, **kw):
        return [{
            'anilist_id': 42, 'title': 'ShouldNotLand',
            'search_pattern': 'ShouldNotLand', 'cover_url': '',
            'status': 'RELEASING', 'total_volumes': 3,
        }]

    with patch.object(_il, '_fetch_list', _fake_list):
        asyncio.run(_il._sync_list({
            'id': 1, 'name': 'TestList', 'type': 'anilist_user',
            'settings': '{}', 'monitor_mode': 'all',
            'quality_profile_id': None, 'root_folder_id': None,
        }))

    with sqlite3.connect(env) as c:
        rows = c.execute(
            "SELECT id FROM series WHERE title='ShouldNotLand'"
        ).fetchall()
        evs = c.execute(
            "SELECT message FROM events WHERE event_type='error'"
            " ORDER BY id DESC LIMIT 3"
        ).fetchall()

    assert rows == [], f"series was created with no root folder: {rows}"
    assert any('no root folders' in (e[0] or '').lower() for e in evs), evs


@pytest.mark.parametrize(
    ("list_type", "anilist_id", "expected_source"),
    [
        ("anilist_user", 51, "anilist"),
        ("mal_user", None, "myanimelist"),
        ("custom_rss", None, "custom_rss"),
    ],
)
def test_import_list_succeeds_when_root_folder_exists(
    env, list_type, anilist_id, expected_source
):
    """Regression guard: the normal path still adds series."""
    import asyncio
    import main
    from routers import import_lists as _il
    from unittest.mock import patch

    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
        c.execute(
            "INSERT INTO root_folders(id, path, label, is_default)"
            " VALUES(1, '/data/media/manga', 'Manga', 1)"
        )

    async def _fake_list(*a, **kw):
        return [{
            'anilist_id': anilist_id, 'title': 'HappyPath',
            'search_pattern': 'HappyPath', 'cover_url': '',
            'status': 'RELEASING', 'total_volumes': 5,
        }]

    with patch.object(_il, '_fetch_list', _fake_list):
        asyncio.run(_il._sync_list({
            'id': 1, 'name': 'TestList', 'type': list_type,
            'settings': '{}', 'monitor_mode': 'all',
            'quality_profile_id': None, 'root_folder_id': None,
        }))

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT id, root_folder_id FROM series WHERE title='HappyPath'"
        ).fetchone()
        assert row is not None
        title_selection = c.execute(
            "SELECT value_json,selected_source,locked FROM series_metadata_fields"
            " WHERE series_id=? AND field_name='title'",
            (row["id"],),
        ).fetchone()
        title_candidate = c.execute(
            "SELECT value_json FROM series_metadata_candidates"
            " WHERE series_id=? AND field_name='title' AND source=?",
            (row["id"], expected_source),
        ).fetchone()
    assert row["root_folder_id"] == 1
    assert dict(title_selection) == {
        "value_json": '"HappyPath"',
        "selected_source": expected_source,
        "locked": 0,
    }
    assert title_candidate["value_json"] == '"HappyPath"'


# ── library search → add path (series_.py:1002) ─────────────────────────────

def test_add_series_returns_400_when_no_folders(env):
    """The UI form handler must refuse to create a series when there's
    no library destination, returning a clear 400."""
    import main
    from fastapi.testclient import TestClient

    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
    main.ensure_api_key()

    client = TestClient(main.app)
    tok = "csrf-addseries-" + "a" * 30
    r = client.post(
        '/series/add',
        data={
            'csrf_token':    tok,
            'title':         'NoFolderSeries',
            'search_pattern': 'NoFolderSeries',
            'anilist_id':    '',
            'edition_type':  'standard',
            'monitored':     '1',
            'search_now':    '0',
        },
        cookies={'csrftoken': tok},
        headers={'X-CSRFToken': tok},
        follow_redirects=False,
    )
    assert r.status_code == 400, r.text
    assert 'root folder' in r.json().get('error', '').lower()

    # No series row was created
    with sqlite3.connect(env) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM series WHERE title='NoFolderSeries'"
        ).fetchone()[0]
    assert n == 0


@pytest.mark.parametrize(
    ("anilist_id", "mu_id", "expected_source"),
    [
        (12345, "", "anilist"),
        (0, "mu-12345", "mangaupdates"),
    ],
)
def test_browser_provider_add_initializes_title_provenance(
    env, monkeypatch, anilist_id, mu_id, expected_source
):
    import main
    from fastapi.testclient import TestClient

    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
        c.execute(
            "INSERT INTO root_folders(id, path, label, is_default)"
            " VALUES(1, '/data/media/manga', 'Manga', 1)"
        )

    def _close_background_task(coro, *, name):
        del name
        coro.close()

    monkeypatch.setattr(main, "create_background_task", _close_background_task)
    token = "csrf-provider-add-" + "a" * 30
    response = TestClient(main.app).post(
        "/series/add",
        data={
            "csrf_token": token,
            "title": "Provider Title",
            "search_pattern": "Provider Title",
            "anilist_id": str(anilist_id),
            "mu_id": mu_id,
            "edition_type": "standard",
            "monitored": "1",
            "search_now": "0",
        },
        cookies={"csrftoken": token},
        headers={"X-CSRFToken": token},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT id FROM series WHERE title='Provider Title'"
        ).fetchone()
        assert row is not None
        selection = c.execute(
            "SELECT value_json,selected_source,locked FROM series_metadata_fields"
            " WHERE series_id=? AND field_name='title'",
            (row["id"],),
        ).fetchone()
        candidate = c.execute(
            "SELECT value_json FROM series_metadata_candidates"
            " WHERE series_id=? AND field_name='title' AND source=?",
            (row["id"], expected_source),
        ).fetchone()
    assert dict(selection) == {
        "value_json": '"Provider Title"',
        "selected_source": expected_source,
        "locked": 0,
    }
    assert candidate["value_json"] == '"Provider Title"'


def test_browser_manual_title_stays_owned_across_metadata_refresh(env, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock, patch

    import main
    import metadata_service
    from fastapi.testclient import TestClient
    from metadata_provenance import get_metadata_field_states

    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
        c.execute(
            "INSERT INTO root_folders(id, path, label, is_default)"
            " VALUES(1, '/data/media/manga', 'Manga', 1)"
        )

    def _close_background_task(coro, *, name):
        del name
        coro.close()

    monkeypatch.setattr(main, "create_background_task", _close_background_task)
    token = "csrf-manual-add-" + "a" * 30
    response = TestClient(main.app).post(
        "/series/add",
        data={
            "csrf_token": token,
            "title": "Operator Title",
            "search_pattern": "Operator Search",
            "anilist_id": "0",
            "mu_id": "",
            "edition_type": "standard",
            "monitored": "1",
            "search_now": "0",
        },
        cookies={"csrftoken": token},
        headers={"X-CSRFToken": token},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    with sqlite3.connect(env) as c:
        series_id = c.execute(
            "SELECT id FROM series WHERE title='Operator Title'"
        ).fetchone()[0]

    record = {
        "anilist_id": 98765,
        "mal_id": None,
        "title": "Provider Replacement",
        "romaji_title": "Provider Replacement",
        "aliases": [],
        "genres": [],
        "cover_url": "",
        "status": "FINISHED",
        "volumes": None,
        "chapters": None,
        "pub_year": 2024,
        "description": "",
    }
    with (
        patch.object(
            metadata_service,
            "_resolve_anilist_record",
            AsyncMock(return_value=record),
        ),
        patch.object(
            metadata_service, "fetch_mu_metadata", AsyncMock(return_value=None)
        ),
        patch.object(
            metadata_service, "refresh_mangadex_map", AsyncMock(return_value=True)
        ),
        patch.object(
            metadata_service,
            "refresh_series_cover",
            AsyncMock(return_value=(True, None)),
        ),
    ):
        result = asyncio.run(
            metadata_service.refresh_series_metadata(
                series_id,
                force=True,
                include_manifest=False,
                reason="title_provenance_test",
            )
        )
    assert result["ok"] is True, result

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT title FROM series WHERE id=?", (series_id,)).fetchone()
        anilist_candidate = c.execute(
            "SELECT value_json FROM series_metadata_candidates"
            " WHERE series_id=? AND field_name='title' AND source='anilist'",
            (series_id,),
        ).fetchone()
    state = next(
        field
        for field in get_metadata_field_states(series_id)
        if field["field_name"] == "title"
    )
    assert row["title"] == "Operator Title"
    assert state["selected_source"] == "manual"
    assert state["locked"] is True
    assert state["pending"] is False
    assert anilist_candidate["value_json"] == '"Provider Replacement"'
