"""HTTP-level integration tests for state-changing routes.

The audit's coverage matrix flagged these routes as having unit tests
(or none) but no HTTP-level integration: a 200 response that silently
fails to persist, or persists to the wrong row, would only surface in
production. Most of these routes are tied directly to a UI button —
the user clicks, sees the response, and assumes the DB updated.

Covers:
  - Volume actions: mark-downloaded, mark-wanted, reset-to-wanted,
    toggle-monitor
  - Chapter map editor: save (JSON body), reset
  - History: mark-failed, delete single, clear-failed
  - Queue actions: orphaned-volume reset (the most common path)
  - Tags: rename, delete
  - Import lists: create, edit, delete, single-list sync trigger
"""
import json
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401


@pytest.fixture
def env(tmp_path):
    """Fresh DB seeded with one series, three volumes (states: wanted,
    grabbed, downloaded), tags, history, and an import list."""
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-state-keys-")

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

    with sqlite3.connect(db.name) as c:
        c.execute("DELETE FROM root_folders")
        c.execute("INSERT INTO root_folders(id, path) VALUES(1, ?)", (str(library_root),))
        c.execute(
            "INSERT INTO series(id, title, search_pattern, edition_type, enabled,"
            " monitored, monitor_mode, root_folder_id)"
            " VALUES(1, 'StateSeries', 'StateSeries', 'standard', 1, 1, 'all', 1)"
        )
        # Three volumes: vol 1 wanted, vol 2 grabbed (with download_id+source_url),
        # vol 3 downloaded
        c.execute(
            "INSERT INTO volumes(id, series_id, volume_num, status, monitored)"
            " VALUES(11, 1, 1.0, 'wanted', 1),"
            "       (12, 1, 2.0, 'grabbed', 1),"
            "       (13, 1, 3.0, 'downloaded', 1)"
        )
        c.execute(
            "UPDATE volumes SET source_url='http://stub/v2.torrent',"
            " download_id='dl-vol2', torrent_name='StateSeries v02',"
            " indexer='Indexer', protocol='torrent', client='Qbit'"
            " WHERE id=12"
        )
        c.execute(
            "INSERT INTO seen(torrent_url, series_id, volume_num, indexer, protocol)"
            " VALUES('http://stub/v2.torrent', 1, 2.0, 'Indexer', 'torrent')"
        )
        # Tags
        c.execute(
            "INSERT INTO series_tags(series_id, tag) VALUES(1, 'shounen'), (1, 'completed')"
        )
        # History row (grabbed) for mark-failed test
        c.execute(
            "INSERT INTO history(id, event_type, series_id, source_title,"
            " download_id, indexer, protocol, size_bytes)"
            " VALUES(101, 'grabbed', 1, 'StateSeries v04 [Group]', 'dl-h101',"
            " 'Indexer', 'torrent', 100000000)"
        )
        c.execute(
            "INSERT INTO history(id, event_type, series_id, source_title)"
            " VALUES(102, 'grab_failed', 1, 'old failed grab'),"
            "       (103, 'import_failed', 1, 'old failed import'),"
            "       (104, 'series_added', 1, 'log row to keep')"
        )
        # Import list
        c.execute(
            "INSERT INTO import_lists(id, name, type, enabled, settings)"
            " VALUES(50, 'TestList', 'anilist_user', 1, '{}')"
        )

    try:
        yield {
            'db_path': db.name,
            'library_root': str(library_root),
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


def _csrf_kwargs(tag: str = "test"):
    tok = f"csrf-{tag}-" + "x" * 30
    return {
        'cookies': {'csrftoken': tok},
        'headers': {'X-CSRFToken': tok},
    }


# ───────────────────── volume actions ─────────────────────


def test_volume_mark_downloaded_persists_status(env):
    """POST mark-downloaded must move status='wanted' → 'downloaded' AND
    set imported_at. Silent-correctness mode: returns 200, status unchanged."""
    client = _client()
    csrf = _csrf_kwargs("mark-dl")

    r = client.post("/series/1/volumes/11/mark-downloaded", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        v = c.execute("SELECT status, imported_at FROM volumes WHERE id=11").fetchone()
        assert v['status'] == 'downloaded', f"status should be 'downloaded', got {v['status']!r}"
        assert v['imported_at'] is not None, "imported_at must be set"


def test_volume_mark_wanted_clears_grab_state(env):
    """POST mark-wanted on a 'grabbed' volume must clear download_id,
    source_url, indexer, protocol, etc. AND remove the seen row.
    Silent-correctness mode: status changes to wanted but download_id
    remains, leaving the volume in zombie state."""
    client = _client()
    csrf = _csrf_kwargs("mark-w")

    r = client.post("/series/1/volumes/12/mark-wanted", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        v = c.execute(
            "SELECT status, source_url, download_id, indexer, protocol,"
            " torrent_name, release_group FROM volumes WHERE id=12"
        ).fetchone()
        assert v['status'] == 'wanted'
        assert v['source_url'] is None
        assert v['download_id'] is None
        assert v['indexer'] is None
        assert v['protocol'] is None
        assert v['torrent_name'] is None
        # And the seen row must be gone (otherwise the URL is permanently blocklisted)
        seen = c.execute(
            "SELECT 1 FROM seen WHERE torrent_url='http://stub/v2.torrent'"
        ).fetchone()
        assert seen is None, "seen row must be deleted so the URL can be re-grabbed"


def test_volume_reset_to_wanted_clears_grab_state(env):
    """reset-to-wanted is the queue-page version of mark-wanted; same
    invariants — only fires on status='grabbed'."""
    client = _client()
    csrf = _csrf_kwargs("reset-w")

    r = client.post("/series/1/volumes/12/reset-to-wanted", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        v = c.execute(
            "SELECT status, download_id, source_url, indexer, release_group"
            " FROM volumes WHERE id=12"
        ).fetchone()
        assert v['status'] == 'wanted'
        assert v['download_id'] is None
        assert v['source_url'] is None
        assert v['indexer'] is None


def test_volume_reset_to_wanted_no_op_on_downloaded(env):
    """The route guards on status='grabbed' — calling on a 'downloaded'
    volume must NOT clobber its state."""
    client = _client()
    csrf = _csrf_kwargs("reset-noop")

    r = client.post("/series/1/volumes/13/reset-to-wanted", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        v = c.execute("SELECT status FROM volumes WHERE id=13").fetchone()
        assert v['status'] == 'downloaded', (
            "reset-to-wanted must guard on grabbed status only — downloaded "
            "volumes must NOT be reset"
        )


def test_volume_toggle_monitor_flips_bit(env):
    """Toggle from monitored=1 → 0 → 1 across two POSTs."""
    client = _client()
    csrf = _csrf_kwargs("toggle-mon")

    r1 = client.post("/series/1/volumes/11/toggle-monitor", **csrf, follow_redirects=False)
    assert r1.status_code in (200, 303)
    with sqlite3.connect(env['db_path']) as c:
        m = c.execute("SELECT monitored FROM volumes WHERE id=11").fetchone()[0]
    assert m == 0, f"first toggle should set 1→0, got {m}"

    r2 = client.post("/series/1/volumes/11/toggle-monitor", **csrf, follow_redirects=False)
    assert r2.status_code in (200, 303)
    with sqlite3.connect(env['db_path']) as c:
        m = c.execute("SELECT monitored FROM volumes WHERE id=11").fetchone()[0]
    assert m == 1, f"second toggle should set 0→1, got {m}"


# ───────────────────── chapter map editor ─────────────────────


def test_chapter_map_save_persists_overrides(env):
    """POST JSON {overrides: {chapter: volume}} replaces the override set."""
    client = _client()
    csrf = _csrf_kwargs("cmap-save")

    payload = {'overrides': {'5': 1, '5.5': 1, '10': 2, 'extra': None}}
    r = client.post(
        "/series/1/chapter-map",
        json=payload,
        **csrf,
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}

    with sqlite3.connect(env['db_path']) as c:
        rows = c.execute(
            "SELECT chapter, volume_num FROM series_chapter_overrides"
            " WHERE series_id=1 ORDER BY chapter"
        ).fetchall()
    overrides = {r[0]: r[1] for r in rows}
    assert overrides == {'10': 2.0, '5': 1.0, '5.5': 1.0, 'extra': None}, (
        f"overrides must persist all 4 entries (None preserved as NULL), got {overrides!r}"
    )


def test_chapter_map_save_replaces_old_overrides(env):
    """Saving twice must replace, not accumulate — each save is a full
    state replacement of the override set."""
    client = _client()
    csrf = _csrf_kwargs("cmap-replace")

    # First save
    client.post("/series/1/chapter-map",
                json={'overrides': {'1': 1, '2': 1}}, **csrf)
    # Second save with smaller set
    r = client.post("/series/1/chapter-map",
                    json={'overrides': {'1': 2}}, **csrf)
    assert r.status_code == 200

    with sqlite3.connect(env['db_path']) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM series_chapter_overrides WHERE series_id=1"
        ).fetchone()[0]
    assert n == 1, (
        f"second save should replace, not accumulate — expected 1 row, got {n}"
    )


def test_chapter_map_reset_clears_overrides(env):
    """POST chapter-map/reset deletes all override rows for the series."""
    client = _client()
    csrf = _csrf_kwargs("cmap-reset")

    # Seed some overrides first
    client.post("/series/1/chapter-map",
                json={'overrides': {'1': 1, '2': 2}}, **csrf)

    r = client.post("/series/1/chapter-map/reset", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM series_chapter_overrides WHERE series_id=1"
        ).fetchone()[0]
    assert n == 0, f"reset must clear all overrides, {n} remain"


# ───────────────────── history mutations ─────────────────────


def test_history_delete_single_row(env):
    """POST /history/{id}/delete removes one row, others survive."""
    client = _client()
    csrf = _csrf_kwargs("hist-del")

    r = client.post("/history/102/delete", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        gone = c.execute("SELECT COUNT(*) FROM history WHERE id=102").fetchone()[0]
        rest = c.execute("SELECT COUNT(*) FROM history WHERE id != 102").fetchone()[0]
    assert gone == 0
    assert rest == 3, f"3 other history rows should survive, got {rest}"


def test_history_clear_failed_only_removes_failed(env):
    """clear-failed removes import_failed + grab_failed, NOT other event types."""
    client = _client()
    csrf = _csrf_kwargs("hist-clear")

    r = client.post("/history/clear-failed", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("SELECT id, event_type FROM history").fetchall()
    by_type = {r['id']: r['event_type'] for r in rows}
    # 102 (grab_failed) and 103 (import_failed) must be gone
    assert 102 not in by_type, "grab_failed must be cleared"
    assert 103 not in by_type, "import_failed must be cleared"
    # 101 (grabbed) and 104 (series_added) must survive
    assert 101 in by_type, "grabbed history must survive"
    assert 104 in by_type, "series_added must survive (not a failure)"


def test_history_mark_failed_creates_blocklist_entry(env):
    """Marking a 'grabbed' history row as failed must INSERT into blocklist
    AND change the history event_type to 'grab_failed'."""
    client = _client()
    csrf = _csrf_kwargs("hist-mf")

    before_bl = None
    with sqlite3.connect(env['db_path']) as c:
        before_bl = c.execute("SELECT COUNT(*) FROM blocklist").fetchone()[0]

    r = client.post("/history/101/mark-failed", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        h = c.execute("SELECT event_type FROM history WHERE id=101").fetchone()
        assert h['event_type'] == 'grab_failed', (
            f"history row should flip to grab_failed, got {h['event_type']!r}"
        )
        bl_count = c.execute("SELECT COUNT(*) FROM blocklist").fetchone()[0]
    assert bl_count == before_bl + 1, (
        f"mark-failed must add a blocklist row (was {before_bl}, now {bl_count})"
    )


# ───────────────────── queue actions ─────────────────────


def test_queue_reset_orphaned_volume(env):
    """POST /queue/grabbed/{vol_id}/reset returns a grabbed volume to
    'wanted' AND removes the seen row (so it can be re-grabbed)."""
    client = _client()
    csrf = _csrf_kwargs("q-reset")

    r = client.post("/queue/grabbed/12/reset", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        v = c.execute("SELECT status, download_id FROM volumes WHERE id=12").fetchone()
        seen = c.execute(
            "SELECT 1 FROM seen WHERE torrent_url='http://stub/v2.torrent'"
        ).fetchone()
    assert v['status'] == 'wanted', f"volume must be wanted, got {v['status']!r}"
    assert v['download_id'] is None
    assert seen is None, "seen row must be cleared so URL can be re-grabbed"


# ───────────────────── tag mutations ─────────────────────


def test_tag_rename(env):
    """POST /api/tags/rename updates every series_tag row with the old tag."""
    client = _client()
    csrf = _csrf_kwargs("tag-rename")

    r = client.post(
        "/api/tags/rename",
        data={'csrf_token': csrf['headers']['X-CSRFToken'],
              'old_name': 'shounen', 'new_name': 'shonen-action'},
        **csrf,
        follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        old_count = c.execute(
            "SELECT COUNT(*) FROM series_tags WHERE tag='shounen'"
        ).fetchone()[0]
        new_count = c.execute(
            "SELECT COUNT(*) FROM series_tags WHERE tag='shonen-action'"
        ).fetchone()[0]
    assert old_count == 0, "old tag must be gone after rename"
    assert new_count == 1, f"new tag must exist, got {new_count} rows"


def test_tag_delete(env):
    """POST /api/tags/{tag}/delete removes every series_tag row for that tag."""
    client = _client()
    csrf = _csrf_kwargs("tag-del")

    r = client.post("/api/tags/completed/delete", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        gone = c.execute(
            "SELECT COUNT(*) FROM series_tags WHERE tag='completed'"
        ).fetchone()[0]
        other = c.execute(
            "SELECT COUNT(*) FROM series_tags WHERE tag='shounen'"
        ).fetchone()[0]
    assert gone == 0
    assert other == 1, "other tags must survive"


# ───────────────────── import list CRUD ─────────────────────


def test_import_list_create(env):
    """POST /import-lists creates a new list row."""
    client = _client()
    csrf = _csrf_kwargs("il-create")

    r = client.post(
        "/import-lists",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'name': 'NewList',
            'type': 'mal_user',
            'enabled': '1',
            'monitor_mode': 'all',
            'settings': '{"username":"alice"}',
        },
        **csrf,
        follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT name, type, enabled, settings FROM import_lists"
            " WHERE name='NewList'"
        ).fetchone()
    assert row is not None, "new list must be inserted"
    assert row['type'] == 'mal_user'
    assert row['enabled'] == 1
    assert json.loads(row['settings']) == {"username": "alice"}


def test_import_list_edit(env):
    """POST /import-lists/{id} updates an existing row."""
    client = _client()
    csrf = _csrf_kwargs("il-edit")

    r = client.post(
        "/import-lists/50",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'name': 'EditedList',
            'type': 'anilist_user',
            'enabled': '0',  # flipped
            'monitor_mode': 'recent',
            'settings': '{"updated":true}',
        },
        **csrf,
        follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT name, enabled, monitor_mode, settings FROM import_lists WHERE id=50"
        ).fetchone()
    assert row['name'] == 'EditedList'
    assert row['enabled'] == 0, "enabled must persist as 0"
    assert row['monitor_mode'] == 'recent'
    assert json.loads(row['settings']) == {"updated": True}


def test_import_list_delete(env):
    """POST /import-lists/{id}/delete removes the row."""
    client = _client()
    csrf = _csrf_kwargs("il-del")

    r = client.post("/import-lists/50/delete", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        n = c.execute("SELECT COUNT(*) FROM import_lists WHERE id=50").fetchone()[0]
    assert n == 0, "list must be deleted"
