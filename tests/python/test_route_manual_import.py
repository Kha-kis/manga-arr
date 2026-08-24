"""HTTP-level integration tests for the manual-import endpoints.

The audit flagged these as "the most failure-prone path for new users":
the underlying file-staging primitives are well-tested in
test_import_atomicity.py, but the route entry points themselves
(/api/manual-import/scan, /api/manual-import/import) had no integration
test. A 500 on first-time setup or a path-traversal regression would
only surface in production.

Auto-import (POST /api/manual-import/auto-import) uses the same real
filesystem and database boundaries while replacing only provider search,
background refresh scheduling, and external notification.
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401


@pytest.fixture
def env(tmp_path):
    """Fresh DB + library root + a 'completed' download dir holding cbz files."""
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-mi-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()
    main.ensure_api_key()

    library_root = tmp_path / "library"
    library_root.mkdir()
    completed = tmp_path / "completed"
    completed.mkdir()

    with sqlite3.connect(db.name) as c:
        c.execute("DELETE FROM root_folders")
        c.execute("INSERT INTO root_folders(id, path) VALUES(1, ?)", (str(library_root),))
        c.execute(
            "INSERT INTO series(id, title, search_pattern, edition_type, enabled,"
            " monitored, monitor_mode, root_folder_id, total_volumes)"
            " VALUES(7, 'TestSeries', 'TestSeries', 'standard', 1, 1, 'all', 1, 5)"
        )
        # One pre-existing wanted volume that the import should mark downloaded
        c.execute(
            "INSERT INTO volumes(id, series_id, volume_num, status, monitored)"
            " VALUES(701, 7, 1.0, 'wanted', 1)"
        )

    try:
        yield {
            'db_path': str(db.name),
            'library_root': str(library_root),
            'completed': str(completed),
        }
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


def _client():
    import main
    from fastapi.testclient import TestClient
    return TestClient(main.app)


def _csrf(tag: str = "test"):
    tok = f"csrf-{tag}-" + "x" * 30
    return {
        'cookies': {'csrftoken': tok},
        'headers': {'X-CSRFToken': tok},
    }


def _make_cbz(path: str, payload: bytes = b"PK\x03\x04stub-cbz"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(payload)


# ───────────────────── /api/manual-import/scan ─────────────────────


def test_scan_finds_manga_files_in_directory(env):
    """Happy path: scan returns one entry per cbz/cbr/epub/pdf in the
    target dir, with parsed volume number and proposed series match."""
    client = _client()
    csrf = _csrf("mi-scan")

    # Drop two manga files + one non-manga noise file in the completed dir
    _make_cbz(os.path.join(env['completed'], "TestSeries v01.cbz"))
    _make_cbz(os.path.join(env['completed'], "TestSeries v02.cbz"))
    with open(os.path.join(env['completed'], "readme.txt"), "w") as f:
        f.write("not a manga file")

    r = client.post(
        "/api/manual-import/scan",
        json={'path': env['completed']},
        **csrf,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert 'files' in body
    files = body['files']
    fnames = sorted(f['filename'] for f in files)
    assert fnames == ['TestSeries v01.cbz', 'TestSeries v02.cbz'], (
        f"only manga extensions should be scanned, got {fnames!r}"
    )
    # Parser must populate proposed_volume from the filename
    by_name = {f['filename']: f for f in files}
    assert by_name['TestSeries v01.cbz']['proposed_volume'] == 1.0
    assert by_name['TestSeries v02.cbz']['proposed_volume'] == 2.0
    # Series matching must find our seeded series for both files
    for entry in files:
        assert entry.get('matched_series'), (
            f"matched_series missing for {entry['filename']!r} — series-name "
            f"matcher must find 'TestSeries' from these filenames"
        )
        assert entry['matched_series']['title'] == 'TestSeries'


def test_scan_rejects_blocked_path(env):
    """Path-traversal/system guard: scanning under /etc, /proc, /sys etc.
    must return 403. CLAUDE.md-class invariant: the manual-import flow
    must never read outside the user's downloads."""
    client = _client()
    csrf = _csrf("mi-blocked")

    r = client.post(
        "/api/manual-import/scan",
        json={'path': '/etc'},
        **csrf,
    )
    assert r.status_code == 403, (
        f"scanning /etc must be rejected with 403, got {r.status_code}: {r.text}"
    )


def test_scan_returns_empty_files_for_nonexistent_path(env):
    """Non-existent path is a graceful no-op (200 with empty list, plus
    error message) — not a 500. UI shows 'no files' rather than crashing."""
    client = _client()
    csrf = _csrf("mi-noexist")

    r = client.post(
        "/api/manual-import/scan",
        json={'path': '/tmp/this-path-does-not-exist-' + str(os.getpid())},
        **csrf,
    )
    assert r.status_code == 200, r.text
    assert r.json().get('files') == []


def test_scan_proposes_series_name_when_no_match(env):
    """For files that don't match any tracked series, the suggested_title
    field must be populated so the UI can offer a 'create series' action."""
    client = _client()
    csrf = _csrf("mi-suggest")

    _make_cbz(os.path.join(env['completed'], "Brand New Series v01.cbz"))

    r = client.post(
        "/api/manual-import/scan",
        json={'path': env['completed']},
        **csrf,
    )
    assert r.status_code == 200
    files = r.json()['files']
    assert len(files) == 1
    entry = files[0]
    assert entry['matched_series'] is None, "should not match TestSeries"
    assert entry['suggested_title'], (
        "suggested_title must be populated when no series match — "
        "UI uses this for the 'add new series' offer"
    )
    assert 'Brand New Series' in entry['suggested_title']


# ───────────────────── /api/manual-import/import ─────────────────────


def test_import_files_volume_into_library_and_marks_downloaded(env):
    """The headline manual-import path: user picks a file + confirms
    series + volume. The endpoint hardlinks/copies the file to the library,
    UPDATEs the existing wanted volume to 'downloaded', and logs history.

    Catches the silent-correctness bug: returns 200 with ok=True per
    entry but the volume row never updates → file is on disk but Mangarr
    thinks the volume is still wanted, so it'll keep grabbing it."""
    client = _client()
    csrf = _csrf("mi-import")

    src = os.path.join(env['completed'], "TestSeries v01.cbz")
    _make_cbz(src)

    r = client.post(
        "/api/manual-import/import",
        json={'entries': [{
            'path': src,
            'series_id': 7,
            'volume_num': 1,
        }]},
        **csrf,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['imported'] == 1, f"one file imported, got {body!r}"
    assert body['total'] == 1
    assert body['results'][0]['ok'] is True
    dst = body['results'][0]['dst']
    assert os.path.isfile(dst), f"file should exist at {dst!r}"
    assert dst.startswith(env['library_root']), (
        f"dst {dst!r} must live under library root {env['library_root']!r}"
    )

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        v = c.execute(
            "SELECT status, import_path, imported_at FROM volumes WHERE id=701"
        ).fetchone()
        assert v['status'] == 'downloaded', (
            f"existing wanted volume must transition to 'downloaded', "
            f"got {v['status']!r}"
        )
        assert v['import_path'] == dst
        assert v['imported_at'] is not None

        # History entry must be logged
        ev = c.execute(
            "SELECT event_type, source_title FROM history"
            " WHERE event_type='imported' AND series_id=7"
        ).fetchone()
        assert ev is not None, "imported history event must be logged"
        assert ev['source_title'] == "TestSeries v01.cbz"


def test_import_uses_pinned_series_folder_name(env):
    """Manual import must honor the per-series folder leaf used by adopted
    existing-library folders whose metadata title differs from the directory."""
    client = _client()
    csrf = _csrf("mi-folder-name")

    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "UPDATE series SET title='Official Metadata Title',"
            " search_pattern='Official Metadata Title', folder_name='Existing Folder'"
            " WHERE id=7"
        )

    src = os.path.join(env['completed'], "Official Metadata Title v01.cbz")
    _make_cbz(src)

    r = client.post(
        "/api/manual-import/import",
        json={'entries': [{
            'path': src,
            'series_id': 7,
            'volume_num': 1,
        }]},
        **csrf,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['imported'] == 1, body
    dst = body['results'][0]['dst']
    assert os.path.isfile(dst)
    assert dst.startswith(os.path.join(env['library_root'], "Existing Folder"))
    assert not os.path.exists(
        os.path.join(env['library_root'], "Official Metadata Title")
    )

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        v = c.execute(
            "SELECT status, import_path FROM volumes WHERE id=701"
        ).fetchone()
    assert v['status'] == 'downloaded'
    assert v['import_path'] == dst


def test_import_rejects_blocked_source_path(env):
    """Importing a file from /etc/* (or other blocked prefix) must be
    rejected per-entry — not crash the whole batch."""
    client = _client()
    csrf = _csrf("mi-blocked-src")

    r = client.post(
        "/api/manual-import/import",
        json={'entries': [{
            'path': '/etc/passwd',  # blocked prefix
            'series_id': 7,
            'volume_num': 1,
        }]},
        **csrf,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['imported'] == 0
    assert body['results'][0]['ok'] is False
    assert 'not allowed' in body['results'][0]['message'].lower()


def test_import_reports_per_entry_status_for_missing_file(env):
    """A missing source file produces ok=False for that entry but doesn't
    abort the batch — other entries still process."""
    client = _client()
    csrf = _csrf("mi-mixed")

    real_src = os.path.join(env['completed'], "TestSeries v01.cbz")
    _make_cbz(real_src)
    fake_src = os.path.join(env['completed'], "missing-file-v02.cbz")
    # deliberately don't create fake_src

    r = client.post(
        "/api/manual-import/import",
        json={'entries': [
            {'path': fake_src, 'series_id': 7, 'volume_num': 2},
            {'path': real_src, 'series_id': 7, 'volume_num': 1},
        ]},
        **csrf,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['imported'] == 1, (
        f"missing file fails its own entry but real file imports; got {body!r}"
    )
    assert body['total'] == 2

    statuses = [(r['ok'], r.get('message', '')) for r in body['results']]
    # The missing-file entry must report ok=False with a clear message
    fail_msgs = [m for ok, m in statuses if not ok]
    assert any('not found' in m.lower() for m in fail_msgs), (
        f"missing file should produce 'not found' message, got {fail_msgs!r}"
    )


def test_import_unknown_series_id_returns_per_entry_error(env):
    """Submitting a series_id that doesn't exist must surface a clear
    per-entry error rather than 500ing the whole batch."""
    client = _client()
    csrf = _csrf("mi-unknown")

    src = os.path.join(env['completed'], "TestSeries v01.cbz")
    _make_cbz(src)

    r = client.post(
        "/api/manual-import/import",
        json={'entries': [{
            'path': src,
            'series_id': 99999,  # does not exist
            'volume_num': 1,
        }]},
        **csrf,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['imported'] == 0
    assert body['results'][0]['ok'] is False
    assert 'series not found' in body['results'][0]['message'].lower()


# ───────────────── /api/manual-import/auto-import ─────────────────


def _metadata_result(
    title: str,
    *,
    anilist_id: int | None,
    mal_id: int | None = None,
    mu_id: str | None = None,
    romaji_title: str = "",
    source: str = "anilist",
    volumes: int | None = 3,
) -> dict[str, Any]:
    return {
        "anilist_id": anilist_id,
        "mal_id": mal_id,
        "mu_id": mu_id,
        "title": title,
        "romaji_title": romaji_title,
        "aliases": [],
        "genres": [],
        "cover_url": "https://cdn.example.test/cover.jpg",
        "status": "FINISHED",
        "format": "MANGA",
        "volumes": volumes,
        "chapters": 12,
        "pub_year": 2020,
        "description": "Provider description",
        "source": source,
    }


def _run_auto_import(
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    detected_title: str,
    results: list[dict[str, Any]],
    provider: str,
) -> tuple[httpx.Response, str, list[str], AsyncMock]:
    import main

    source_path = os.path.join(env["completed"], f"{detected_title} v01.cbz")
    _make_cbz(source_path)

    async def _search_series(query: str) -> tuple[list[dict[str, Any]], str]:
        assert query == detected_title
        return results, provider

    scheduled_tasks: list[str] = []

    def _record_background_task(coro: Any, *, name: str) -> None:
        scheduled_tasks.append(name)
        coro.close()

    komga_scan = AsyncMock()
    monkeypatch.setattr(main, "search_series", _search_series)
    monkeypatch.setattr(main, "create_background_task", _record_background_task)
    monkeypatch.setattr(main, "trigger_komga_scan", komga_scan)

    async def _post() -> httpx.Response:
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(
                "/api/manual-import/auto-import",
                json={"path": env["completed"], "remove_source": False},
                headers={"X-Api-Key": str(main.CONFIG["api_key"])},
            )

    response = asyncio.run(_post())
    return response, source_path, scheduled_tasks, komga_scan


def _assert_auto_import_left_group_unmatched(
    env: dict[str, str],
    response: httpx.Response,
    source_path: str,
    scheduled_tasks: list[str],
    komga_scan: AsyncMock,
) -> None:
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["imported"] == 0
    assert body["new_series"] == []
    assert body["results"] == [
        {
            "path": source_path,
            "ok": False,
            "message": f"No series match for: {os.path.basename(source_path)}",
        }
    ]
    assert os.path.isfile(source_path)
    assert os.listdir(env["library_root"]) == []
    assert scheduled_tasks == []
    assert komga_scan.await_count == 0

    with sqlite3.connect(env["db_path"]) as db:
        assert db.execute(
            "SELECT id,anilist_id,mal_id,mu_id FROM series WHERE id<>7"
        ).fetchall() == []
        assert db.execute(
            "SELECT series_id FROM series_metadata_fields WHERE series_id<>7"
        ).fetchall() == []
        assert db.execute(
            "SELECT series_id FROM series_metadata_candidates WHERE series_id<>7"
        ).fetchall() == []
        assert db.execute(
            "SELECT series_id FROM volumes WHERE series_id<>7"
        ).fetchall() == []


def test_auto_import_equal_title_anilist_ambiguity_does_not_select_first_result(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    title = "Shared AniList Title"
    response, source_path, scheduled_tasks, komga_scan = _run_auto_import(
        env,
        monkeypatch,
        detected_title=title,
        results=[
            _metadata_result(title, anilist_id=4101, mal_id=5101),
            _metadata_result(title, anilist_id=4102, mal_id=5102),
        ],
        provider="anilist",
    )

    _assert_auto_import_left_group_unmatched(
        env, response, source_path, scheduled_tasks, komga_scan
    )


def test_auto_import_rejects_multiple_distinct_accepted_anilist_scores(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    title = "Alpha Beta Gamma"
    response, source_path, scheduled_tasks, komga_scan = _run_auto_import(
        env,
        monkeypatch,
        detected_title=title,
        results=[
            _metadata_result(title, anilist_id=4201),
            _metadata_result(f"{title} Deluxe", anilist_id=4202),
        ],
        provider="anilist",
    )

    _assert_auto_import_left_group_unmatched(
        env, response, source_path, scheduled_tasks, komga_scan
    )


def test_auto_import_creates_series_for_one_unique_acceptable_anilist_result(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    title = "Clearly Unique Title"
    response, source_path, scheduled_tasks, komga_scan = _run_auto_import(
        env,
        monkeypatch,
        detected_title=title,
        results=[
            _metadata_result(title, anilist_id=4301, mal_id=5301),
            _metadata_result("Completely Different Work", anilist_id=4302),
        ],
        provider="anilist",
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["imported"] == 1
    assert body["new_series"] == [{"id": body["new_series"][0]["id"], "title": title}]
    assert body["results"][0]["ok"] is True
    assert os.path.isfile(body["results"][0]["dst"])
    assert os.path.isfile(source_path)
    assert scheduled_tasks == [f"manual_import:{body['new_series'][0]['id']}:metadata"]
    assert komga_scan.await_count == 1

    series_id = body["new_series"][0]["id"]
    with sqlite3.connect(env["db_path"]) as db:
        db.row_factory = sqlite3.Row
        series = db.execute(
            "SELECT title,anilist_id,mal_id,mu_id FROM series WHERE id=?",
            (series_id,),
        ).fetchone()
        title_field = db.execute(
            "SELECT value_json,selected_source,locked FROM series_metadata_fields"
            " WHERE series_id=? AND field_name='title'",
            (series_id,),
        ).fetchone()
        title_candidate = db.execute(
            "SELECT value_json FROM series_metadata_candidates"
            " WHERE series_id=? AND field_name='title' AND source='anilist'",
            (series_id,),
        ).fetchone()
        volumes = db.execute(
            "SELECT volume_num,status FROM volumes WHERE series_id=? ORDER BY volume_num",
            (series_id,),
        ).fetchall()
    assert dict(series) == {
        "title": title,
        "anilist_id": 4301,
        "mal_id": 5301,
        "mu_id": None,
    }
    assert dict(title_field) == {
        "value_json": f'"{title}"',
        "selected_source": "anilist",
        "locked": 0,
    }
    assert title_candidate["value_json"] == f'"{title}"'
    assert [tuple(row) for row in volumes] == [
        (1.0, "downloaded"),
        (2.0, "wanted"),
        (3.0, "wanted"),
    ]


def test_auto_import_deduplicates_accepted_rows_with_same_anilist_id(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    title = "Duplicate Alpha Beta"
    response, _source_path, scheduled_tasks, _komga_scan = _run_auto_import(
        env,
        monkeypatch,
        detected_title=title,
        results=[
            _metadata_result(title, anilist_id=4401),
            _metadata_result(f"{title} Deluxe", anilist_id=4401),
        ],
        provider="anilist",
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["imported"] == 1
    assert len(body["new_series"]) == 1
    assert len(scheduled_tasks) == 1
    with sqlite3.connect(env["db_path"]) as db:
        rows = db.execute(
            "SELECT id,title,anilist_id FROM series WHERE anilist_id=4401"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][1:] == (title, 4401)


def test_auto_import_creates_series_for_one_unique_mangaupdates_result(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    title = "Unique MangaUpdates Work"
    response, _source_path, scheduled_tasks, _komga_scan = _run_auto_import(
        env,
        monkeypatch,
        detected_title=title,
        results=[
            _metadata_result(
                title,
                anilist_id=None,
                mu_id="4451",
                source="mangaupdates",
            )
        ],
        provider="mangaupdates",
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["imported"] == 1
    assert len(body["new_series"]) == 1
    series_id = body["new_series"][0]["id"]
    assert scheduled_tasks == [f"manual_import:{series_id}:metadata"]

    with sqlite3.connect(env["db_path"]) as db:
        db.row_factory = sqlite3.Row
        series = db.execute(
            "SELECT mu_id FROM series WHERE id=?", (series_id,)
        ).fetchone()
        title_field = db.execute(
            "SELECT selected_source,locked FROM series_metadata_fields"
            " WHERE series_id=? AND field_name='title'",
            (series_id,),
        ).fetchone()
        imported = db.execute(
            "SELECT status FROM volumes WHERE series_id=? AND volume_num=1",
            (series_id,),
        ).fetchone()
    assert dict(title_field) == {"selected_source": "mangaupdates", "locked": 0}
    assert imported["status"] == "downloaded"
    assert series["mu_id"] == "4451"


def test_auto_import_reuses_existing_series_by_exact_anilist_identity(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with sqlite3.connect(env["db_path"]) as db:
        db.execute(
            "INSERT INTO series(id,title,search_pattern,anilist_id,edition_type,root_folder_id)"
            " VALUES(8,'Stored Canonical Title','Stored Canonical Title',4501,'standard',1)"
        )

    detected_title = "Provider Discovery Alias"
    response, _source_path, _scheduled_tasks, _komga_scan = _run_auto_import(
        env,
        monkeypatch,
        detected_title=detected_title,
        results=[_metadata_result(detected_title, anilist_id=4501)],
        provider="anilist",
    )

    assert response.status_code == 200, response.text
    assert response.json()["imported"] == 1
    with sqlite3.connect(env["db_path"]) as db:
        identity_rows = db.execute(
            "SELECT id FROM series WHERE anilist_id=4501"
        ).fetchall()
        imported = db.execute(
            "SELECT series_id,status FROM volumes WHERE series_id=8"
        ).fetchall()
    assert identity_rows == [(8,)]
    assert imported == [(8, "downloaded")]


def test_auto_import_does_not_reuse_nonstandard_exact_anilist_identity(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with sqlite3.connect(env["db_path"]) as db:
        db.execute(
            "INSERT INTO series(id,title,search_pattern,anilist_id,edition_type,root_folder_id)"
            " VALUES(8,'Stored Omnibus','Stored Omnibus',4551,'omnibus',1)"
        )

    detected_title = "Standard Provider Title"
    response, _source_path, scheduled_tasks, _komga_scan = _run_auto_import(
        env,
        monkeypatch,
        detected_title=detected_title,
        results=[_metadata_result(detected_title, anilist_id=4551)],
        provider="anilist",
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["imported"] == 1
    new_series_id = body["new_series"][0]["id"]
    assert new_series_id != 8
    assert scheduled_tasks == [f"manual_import:{new_series_id}:metadata"]
    with sqlite3.connect(env["db_path"]) as db:
        identities = db.execute(
            "SELECT id,edition_type FROM series WHERE anilist_id=4551 ORDER BY id"
        ).fetchall()
        omnibus_volumes = db.execute(
            "SELECT COUNT(*) FROM volumes WHERE series_id=8"
        ).fetchone()[0]
        imported = db.execute(
            "SELECT series_id,status FROM volumes"
            " WHERE series_id=? AND volume_num=1",
            (new_series_id,),
        ).fetchall()
    assert identities == [(8, "omnibus"), (new_series_id, "standard")]
    assert omnibus_volumes == 0
    assert imported == [(new_series_id, "downloaded")]


def test_auto_import_does_not_reuse_same_title_with_different_anilist_identity(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical_title = "Shared Canonical Work"
    with sqlite3.connect(env["db_path"]) as db:
        db.execute(
            "INSERT INTO series(id,title,search_pattern,anilist_id,edition_type,root_folder_id)"
            " VALUES(8,?,?,4601,'standard',1)",
            (canonical_title, canonical_title),
        )

    detected_title = "Localized Discovery Name"
    response, source_path, scheduled_tasks, komga_scan = _run_auto_import(
        env,
        monkeypatch,
        detected_title=detected_title,
        results=[
            _metadata_result(
                canonical_title,
                anilist_id=4602,
                romaji_title=detected_title,
            )
        ],
        provider="anilist",
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["imported"] == 0
    assert body["new_series"] == []
    assert body["results"] == [
        {
            "path": source_path,
            "ok": False,
            "message": f"No series match for: {os.path.basename(source_path)}",
        }
    ]
    assert os.path.isfile(source_path)
    assert os.listdir(env["library_root"]) == []
    assert scheduled_tasks == []
    assert komga_scan.await_count == 0
    with sqlite3.connect(env["db_path"]) as db:
        identities = db.execute(
            "SELECT id,anilist_id FROM series WHERE title=? ORDER BY id",
            (canonical_title,),
        ).fetchall()
        extra_series = db.execute(
            "SELECT id FROM series WHERE id NOT IN (7,8)"
        ).fetchall()
        side_effects = db.execute(
            "SELECT series_id FROM volumes WHERE series_id<>7"
        ).fetchall()
        provenance = db.execute(
            "SELECT series_id FROM series_metadata_fields WHERE series_id<>7"
        ).fetchall()
    assert identities == [(8, 4601)]
    assert extra_series == []
    assert side_effects == []
    assert provenance == []


def test_auto_import_rejects_ambiguous_mangaupdates_fallback(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    title = "Shared MangaUpdates Title"
    response, source_path, scheduled_tasks, komga_scan = _run_auto_import(
        env,
        monkeypatch,
        detected_title=title,
        results=[
            _metadata_result(
                title,
                anilist_id=None,
                mu_id="4701",
                source="mangaupdates",
            ),
            _metadata_result(
                title,
                anilist_id=None,
                mu_id="4702",
                source="mangaupdates",
            ),
        ],
        provider="mangaupdates",
    )

    _assert_auto_import_left_group_unmatched(
        env, response, source_path, scheduled_tasks, komga_scan
    )
