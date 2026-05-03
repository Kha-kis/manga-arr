"""HTTP-level integration tests for destructive routes.

Covers irreversible operations the production-readiness audit flagged as
silent-correctness risks: a bug in series/{id}/delete that deletes the
wrong cascade chain, or a blocklist add that returns 200 without
inserting, would not surface in casual daily use because the thing you'd
verify against is the same row that just got deleted (or never written).

Each test posts the real request through the real router → real DB,
verifies the response and the resulting DB state.
"""
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
    """Fresh DB + 2 series + their volumes, blocklist, indexer."""
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-destroy-keys-")

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
        # Two series — destructive ops on series 1 must not touch series 2
        c.execute(
            "INSERT INTO series(id, title, search_pattern, edition_type, enabled,"
            " monitored, monitor_mode, root_folder_id)"
            " VALUES(1, 'AlphaSeries', 'AlphaSeries', 'standard', 1, 1, 'all', 1),"
            "       (2, 'BetaSeries',  'BetaSeries',  'standard', 1, 1, 'all', 1)"
        )
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, monitored)"
            " VALUES(1, 1.0, 'wanted', 1), (1, 2.0, 'wanted', 1),"
            "       (2, 1.0, 'wanted', 1)"
        )
        # Blocklist with 3 rows: 2 for series 1, 1 standalone
        c.execute(
            "INSERT INTO blocklist(series_id, torrent_url, torrent_name, reason)"
            " VALUES(1, 'http://stub/a.torrent', 'AlphaSeries v01 bad release', 'Manual'),"
            "       (1, 'http://stub/b.torrent', 'AlphaSeries v02 bad release', 'Manual'),"
            "       (NULL, 'http://stub/c.torrent', 'unrelated', 'Manual')"
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
    """Build the CSRF cookie + header pair required by middleware."""
    tok = f"csrf-{tag}-" + "x" * 30
    return {
        'cookies': {'csrftoken': tok},
        'headers': {'X-CSRFToken': tok},
    }


# ───────────────────── series delete ─────────────────────


def test_series_delete_soft_deletes_only_target(env):
    """Soft-delete of series_id=1 must mark series 1 only — series 2's
    `deleted_at` must remain NULL.

    NOTE: as of the recycle-bin epic, /series/{id}/delete is a soft-delete
    that sets `deleted_at` + `deletion_reason` and leaves every dependent
    row in place. The hard cascade now lives in `_hard_delete_series`,
    called by the reaper and by the explicit purge endpoint. The
    cross-series isolation property still holds.
    """
    client = _client()
    csrf = _csrf_kwargs("delete-series")

    r = client.post("/series/1/delete", **csrf, follow_redirects=False)
    assert r.status_code in (303, 200), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row

        s1 = c.execute("SELECT title, deleted_at FROM series WHERE id=1").fetchone()
        s2 = c.execute("SELECT title, deleted_at FROM series WHERE id=2").fetchone()
        assert s1 is not None, "series 1 row must still exist (soft-delete)"
        assert s1['deleted_at'] is not None, "series 1 must be marked soft-deleted"
        assert s2 is not None and s2['title'] == 'BetaSeries'
        assert s2['deleted_at'] is None, "series 2 deleted_at must be NULL"

        # During the soft-delete window, dependent rows are preserved for
        # restore. Just verify series-2 wasn't accidentally touched.
        v2_count = c.execute("SELECT COUNT(*) FROM volumes WHERE series_id=2").fetchone()[0]
        assert v2_count == 1, "series-2 volumes must NOT be affected"
        bl_orphan = c.execute("SELECT COUNT(*) FROM blocklist WHERE series_id IS NULL").fetchone()[0]
        assert bl_orphan == 1, "standalone blocklist row must survive"


def test_series_delete_logs_history(env):
    """The soft-delete must add a `series_soft_deleted` history event so
    the user can audit what happened. (Renamed from `series_deleted` —
    the hard `series_purged` event is logged by the reaper / purge.)"""
    client = _client()
    csrf = _csrf_kwargs("delete-history")

    r = client.post("/series/1/delete", **csrf, follow_redirects=False)
    assert r.status_code in (303, 200)

    with sqlite3.connect(env['db_path']) as c:
        ev = c.execute(
            "SELECT event_type, source_title FROM history"
            " WHERE event_type='series_soft_deleted'"
        ).fetchone()
        assert ev is not None, "series_soft_deleted event must be logged"
        assert ev[1] == 'AlphaSeries', f"deleted-title must match, got {ev[1]!r}"


def test_series_delete_unknown_id_returns_redirect_not_500(env):
    """A delete for a non-existent series should silently no-op (the row
    simply isn't there to delete) rather than 500. CSRF must still pass."""
    client = _client()
    csrf = _csrf_kwargs("delete-missing")

    r = client.post("/series/99999/delete", **csrf, follow_redirects=False)
    assert r.status_code in (303, 200), (
        f"missing-series delete should redirect cleanly, got {r.status_code}: {r.text}"
    )

    # Existing series untouched
    with sqlite3.connect(env['db_path']) as c:
        n = c.execute("SELECT COUNT(*) FROM series").fetchone()[0]
        assert n == 2, "no series should have been deleted"


# ───────────────────── blocklist mutations ─────────────────────


def test_blocklist_add_persists_row(env):
    """POST /blocklist/add must INSERT a row. If it returns 200 but
    silently fails to write, releases the user blocked get re-grabbed
    weeks later — exactly the silent-correctness mode the audit warned
    about."""
    client = _client()
    csrf = _csrf_kwargs("bl-add")

    r = client.post(
        "/blocklist/add",
        data={
            'csrf_token':   csrf['headers']['X-CSRFToken'],
            'series_id':    '1',
            'torrent_url':  'http://stub/new-block.torrent',
            'torrent_name': 'AlphaSeries v03 [BadGroup]',
            'reason':       'low quality',
        },
        **csrf,
        follow_redirects=False,
    )
    assert r.status_code in (303, 200), r.text

    with sqlite3.connect(env['db_path']) as c:
        row = c.execute(
            "SELECT series_id, torrent_url, torrent_name, reason FROM blocklist"
            " WHERE torrent_url=?",
            ('http://stub/new-block.torrent',)
        ).fetchone()
        assert row is not None, "blocklist row must be inserted"
        assert row[0] == 1
        assert row[2] == 'AlphaSeries v03 [BadGroup]'
        assert row[3] == 'low quality'


def test_blocklist_add_without_url_does_not_insert(env):
    """Empty torrent_url is a no-op — guards against a UI bug submitting
    a blank form and creating a junk blocklist entry."""
    client = _client()
    csrf = _csrf_kwargs("bl-noop")

    before = None
    with sqlite3.connect(env['db_path']) as c:
        before = c.execute("SELECT COUNT(*) FROM blocklist").fetchone()[0]

    r = client.post(
        "/blocklist/add",
        data={
            'csrf_token':   csrf['headers']['X-CSRFToken'],
            'series_id':    '1',
            'torrent_url':  '',  # empty
            'torrent_name': 'should not insert',
            'reason':       '',
        },
        **csrf,
        follow_redirects=False,
    )
    assert r.status_code in (303, 200)

    with sqlite3.connect(env['db_path']) as c:
        after = c.execute("SELECT COUNT(*) FROM blocklist").fetchone()[0]
    assert after == before, f"empty-URL submit should not insert (was {before}, now {after})"


def test_blocklist_delete_single_row(env):
    """DELETE one specific row by id — neighbors must survive."""
    client = _client()
    csrf = _csrf_kwargs("bl-del")

    with sqlite3.connect(env['db_path']) as c:
        target_id = c.execute(
            "SELECT id FROM blocklist WHERE torrent_url='http://stub/a.torrent'"
        ).fetchone()[0]

    r = client.post(f"/blocklist/{target_id}/delete", **csrf, follow_redirects=False)
    assert r.status_code in (303, 200), r.text

    with sqlite3.connect(env['db_path']) as c:
        gone = c.execute("SELECT COUNT(*) FROM blocklist WHERE id=?", (target_id,)).fetchone()[0]
        rest = c.execute("SELECT COUNT(*) FROM blocklist WHERE id!=?", (target_id,)).fetchone()[0]
    assert gone == 0, "target row must be deleted"
    assert rest == 2, f"other 2 rows must survive, got {rest}"


def test_blocklist_clear_all_empties_table(env):
    """clear-all wipes every row regardless of series_id."""
    client = _client()
    csrf = _csrf_kwargs("bl-clear")

    r = client.post("/blocklist/clear-all", **csrf, follow_redirects=False)
    assert r.status_code in (303, 200), r.text

    with sqlite3.connect(env['db_path']) as c:
        remaining = c.execute("SELECT COUNT(*) FROM blocklist").fetchone()[0]
    assert remaining == 0, (
        f"blocklist must be empty after clear-all, {remaining} row(s) remain"
    )

    # Series intact (clear-all is blocklist-scoped, must not cascade)
    with sqlite3.connect(env['db_path']) as c:
        s_count = c.execute("SELECT COUNT(*) FROM series").fetchone()[0]
    assert s_count == 2, "clear-all must NOT touch series table"


def test_blocklist_delete_unknown_id_does_not_500(env):
    """DELETE for a non-existent blocklist id is a no-op, not a 500."""
    client = _client()
    csrf = _csrf_kwargs("bl-missing")

    r = client.post("/blocklist/99999/delete", **csrf, follow_redirects=False)
    assert r.status_code in (303, 200), (
        f"missing-id delete should redirect cleanly, got {r.status_code}: {r.text}"
    )

    with sqlite3.connect(env['db_path']) as c:
        remaining = c.execute("SELECT COUNT(*) FROM blocklist").fetchone()[0]
    assert remaining == 3, "no blocklist rows should have changed"
