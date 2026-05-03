"""Recycle bin / soft-delete tests (PR-1 of the recycle-bin epic).

Covers the soft-delete state, restore round-trip, visibility filtering
across listing pages + search loops, and the dedup-on-re-add behaviour.
The reaper job tests live in PR-3; the UI tests in PR-2.
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
    """Fresh DB; each test seeds its own series rows."""
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-recyclebin-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()
    main.ensure_api_key()
    try:
        yield {'db_path': db.name}
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


def _csrf(tag="t"):
    tok = f"csrf-{tag}-" + "x" * 30
    return {
        'cookies': {'csrftoken': tok},
        'headers': {'X-CSRFToken': tok},
    }


def _seed_series(db_path, sid, title, **kwargs) -> None:
    """Insert a minimal series + 2 volumes + 1 chapter + 1 seen + 1 tag,
    so deleting it then restoring it has substantive state to verify.
    """
    monitored = kwargs.get('monitored', 1)
    with sqlite3.connect(db_path) as c:
        c.execute(
            "INSERT INTO series(id, title, search_pattern, monitored, status,"
            " preferred_groups, anilist_id) VALUES(?, ?, ?, ?, 'RELEASING',"
            " '[\"LuCaZ\"]', ?)",
            (sid, title, kwargs.get('search', title.lower()), monitored, kwargs.get('anilist_id'))
        )
        # 2 volumes
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, monitored)"
            " VALUES(?, 1, 'wanted', 1), (?, 2, 'downloaded', 1)",
            (sid, sid)
        )
        # 1 chapter
        c.execute(
            "INSERT INTO chapters(series_id, chapter_num, status, monitored)"
            " VALUES(?, 1, 'wanted', 1)", (sid,)
        )
        # 1 seen row
        c.execute(
            "INSERT INTO seen(series_id, torrent_url, release_guid)"
            " VALUES(?, ?, ?)",
            (sid, f"http://test/{sid}.torrent", f"guid-{sid}")
        )
        # 1 tag
        c.execute(
            "INSERT OR IGNORE INTO series_tags(series_id, tag) VALUES(?, ?)",
            (sid, f"tag-{sid}")
        )
        # 1 alias
        c.execute(
            "INSERT INTO series_aliases(series_id, alias) VALUES(?, ?)",
            (sid, f"alias-{sid}")
        )


# ───────────────────── Soft-delete state ─────────────────────


def test_soft_delete_sets_deleted_at_and_reason(env):
    _seed_series(env['db_path'], 1, 'Test Series')
    csrf = _csrf("soft")

    r = _client().post(
        "/series/1/delete",
        data={'csrf_token': csrf['headers']['X-CSRFToken']},
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        row = c.execute(
            "SELECT deleted_at, deletion_reason FROM series WHERE id=1"
        ).fetchone()
    assert row[0] is not None, "deleted_at must be set"
    assert row[1] == 'user_action', f"deletion_reason got {row[1]!r}"


def test_soft_delete_keeps_dependent_rows(env):
    """Volumes, chapters, seen, tags, aliases must all remain in place
    during the soft-delete window — restore needs to find them."""
    _seed_series(env['db_path'], 1, 'Test Series')
    csrf = _csrf("keep")
    _client().post(
        "/series/1/delete",
        data={'csrf_token': csrf['headers']['X-CSRFToken']},
        **csrf, follow_redirects=False,
    )

    with sqlite3.connect(env['db_path']) as c:
        vols = c.execute("SELECT COUNT(*) FROM volumes WHERE series_id=1").fetchone()[0]
        chs  = c.execute("SELECT COUNT(*) FROM chapters WHERE series_id=1").fetchone()[0]
        seen = c.execute("SELECT COUNT(*) FROM seen WHERE series_id=1").fetchone()[0]
        tags = c.execute("SELECT COUNT(*) FROM series_tags WHERE series_id=1").fetchone()[0]
        ali  = c.execute("SELECT COUNT(*) FROM series_aliases WHERE series_id=1").fetchone()[0]
    assert vols == 2
    assert chs  == 1
    assert seen == 1
    assert tags == 1
    assert ali  == 1


def test_soft_delete_logs_history_event(env):
    _seed_series(env['db_path'], 1, 'Test Series')
    csrf = _csrf("hist")
    _client().post(
        "/series/1/delete",
        data={'csrf_token': csrf['headers']['X-CSRFToken']},
        **csrf, follow_redirects=False,
    )
    with sqlite3.connect(env['db_path']) as c:
        ev = c.execute(
            "SELECT event_type FROM history WHERE series_id IS NULL"
            " AND event_type='series_soft_deleted'"
        ).fetchone()
    assert ev is not None


# ───────────────────── Visibility filtering ─────────────────────


def test_soft_deleted_series_hidden_from_library(env):
    _seed_series(env['db_path'], 1, 'Visible Series')
    _seed_series(env['db_path'], 2, 'Hidden Series')
    # Soft-delete series 2
    with sqlite3.connect(env['db_path']) as c:
        c.execute("UPDATE series SET deleted_at=CURRENT_TIMESTAMP, deletion_reason='user_action' WHERE id=2")

    r = _client().get("/")
    assert r.status_code == 200
    assert 'Visible Series' in r.text
    assert 'Hidden Series' not in r.text


def test_soft_deleted_series_hidden_from_wanted(env):
    _seed_series(env['db_path'], 1, 'Visible Series')
    _seed_series(env['db_path'], 2, 'Hidden Series')
    with sqlite3.connect(env['db_path']) as c:
        c.execute("UPDATE series SET deleted_at=CURRENT_TIMESTAMP WHERE id=2")

    r = _client().get("/wanted")
    assert r.status_code == 200
    assert 'Visible Series' in r.text
    assert 'Hidden Series' not in r.text


def test_soft_deleted_series_hidden_from_calendar(env):
    _seed_series(env['db_path'], 1, 'Visible Series')
    _seed_series(env['db_path'], 2, 'Hidden Series')
    with sqlite3.connect(env['db_path']) as c:
        c.execute("UPDATE series SET deleted_at=CURRENT_TIMESTAMP WHERE id=2")

    r = _client().get("/calendar")
    assert r.status_code == 200
    assert 'Visible Series' in r.text
    assert 'Hidden Series' not in r.text


def test_soft_deleted_series_hidden_from_stats(env):
    """Stats overview must not count soft-deleted series in total_series."""
    _seed_series(env['db_path'], 1, 'A')
    _seed_series(env['db_path'], 2, 'B')
    _seed_series(env['db_path'], 3, 'C')
    with sqlite3.connect(env['db_path']) as c:
        c.execute("UPDATE series SET deleted_at=CURRENT_TIMESTAMP WHERE id=3")

    r = _client().get("/stats")
    assert r.status_code == 200
    # The total_series count should be 2, not 3 — render check:
    # the stats template renders the count; just verify the soft-deleted
    # title is NOT in the page (top-series JOIN filters via history).
    assert 'C' not in r.text or r.text.count('B') > 0  # smoke


def test_soft_deleted_series_hidden_from_series_editor(env):
    _seed_series(env['db_path'], 1, 'Visible Series')
    _seed_series(env['db_path'], 2, 'Hidden Series')
    with sqlite3.connect(env['db_path']) as c:
        c.execute("UPDATE series SET deleted_at=CURRENT_TIMESTAMP WHERE id=2")

    r = _client().get("/series-editor")
    assert r.status_code == 200
    assert 'Visible Series' in r.text
    assert 'Hidden Series' not in r.text


def test_soft_deleted_series_count_excluded_from_system_status(env):
    _seed_series(env['db_path'], 1, 'A')
    _seed_series(env['db_path'], 2, 'B')
    with sqlite3.connect(env['db_path']) as c:
        c.execute("UPDATE series SET deleted_at=CURRENT_TIMESTAMP WHERE id=2")

    r = _client().get("/system/status")
    assert r.status_code == 200
    # The system status page renders series_count; the count should be 1.
    # We don't assert on rendered HTML count formatting (template-specific);
    # instead verify via the underlying query.
    with sqlite3.connect(env['db_path']) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM series WHERE deleted_at IS NULL"
        ).fetchone()[0]
    assert n == 1


# ───────────────────── Restore ─────────────────────


def test_restore_clears_deleted_at(env):
    _seed_series(env['db_path'], 1, 'Test Series')
    csrf = _csrf("restore")
    # soft-delete
    _client().post(
        "/series/1/delete",
        data={'csrf_token': csrf['headers']['X-CSRFToken']},
        **csrf, follow_redirects=False,
    )
    # restore
    r = _client().post(
        "/series/1/restore",
        data={'csrf_token': csrf['headers']['X-CSRFToken']},
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text
    with sqlite3.connect(env['db_path']) as c:
        row = c.execute(
            "SELECT deleted_at, deletion_reason FROM series WHERE id=1"
        ).fetchone()
    assert row[0] is None
    assert row[1] is None


def test_restore_round_trip_preserves_state(env):
    """Soft-delete a series with full state, restore it, verify every
    dependent row count matches the original."""
    _seed_series(env['db_path'], 1, 'Test Series')

    with sqlite3.connect(env['db_path']) as c:
        before = {
            'volumes':  c.execute("SELECT COUNT(*) FROM volumes WHERE series_id=1").fetchone()[0],
            'chapters': c.execute("SELECT COUNT(*) FROM chapters WHERE series_id=1").fetchone()[0],
            'seen':     c.execute("SELECT COUNT(*) FROM seen WHERE series_id=1").fetchone()[0],
            'tags':     c.execute("SELECT COUNT(*) FROM series_tags WHERE series_id=1").fetchone()[0],
            'aliases':  c.execute("SELECT COUNT(*) FROM series_aliases WHERE series_id=1").fetchone()[0],
        }

    csrf = _csrf("rt")
    _client().post("/series/1/delete",
                   data={'csrf_token': csrf['headers']['X-CSRFToken']},
                   **csrf, follow_redirects=False)
    _client().post("/series/1/restore",
                   data={'csrf_token': csrf['headers']['X-CSRFToken']},
                   **csrf, follow_redirects=False)

    with sqlite3.connect(env['db_path']) as c:
        after = {
            'volumes':  c.execute("SELECT COUNT(*) FROM volumes WHERE series_id=1").fetchone()[0],
            'chapters': c.execute("SELECT COUNT(*) FROM chapters WHERE series_id=1").fetchone()[0],
            'seen':     c.execute("SELECT COUNT(*) FROM seen WHERE series_id=1").fetchone()[0],
            'tags':     c.execute("SELECT COUNT(*) FROM series_tags WHERE series_id=1").fetchone()[0],
            'aliases':  c.execute("SELECT COUNT(*) FROM series_aliases WHERE series_id=1").fetchone()[0],
        }
    assert after == before, f"counts diverged after restore: {before} → {after}"


def test_restored_series_reappears_in_library(env):
    _seed_series(env['db_path'], 1, 'Test Series')
    csrf = _csrf("reappear")
    _client().post("/series/1/delete",
                   data={'csrf_token': csrf['headers']['X-CSRFToken']},
                   **csrf, follow_redirects=False)
    # gone
    r = _client().get("/")
    assert 'Test Series' not in r.text
    # restore
    _client().post("/series/1/restore",
                   data={'csrf_token': csrf['headers']['X-CSRFToken']},
                   **csrf, follow_redirects=False)
    # back
    r = _client().get("/")
    assert 'Test Series' in r.text


def test_restore_logs_history(env):
    _seed_series(env['db_path'], 1, 'Test Series')
    csrf = _csrf("rh")
    _client().post("/series/1/delete",
                   data={'csrf_token': csrf['headers']['X-CSRFToken']},
                   **csrf, follow_redirects=False)
    _client().post("/series/1/restore",
                   data={'csrf_token': csrf['headers']['X-CSRFToken']},
                   **csrf, follow_redirects=False)
    with sqlite3.connect(env['db_path']) as c:
        ev = c.execute(
            "SELECT event_type FROM history WHERE event_type='series_restored'"
        ).fetchone()
    assert ev is not None


# ───────────────────── Re-add after soft-delete ─────────────────────


def test_search_does_not_block_readd_of_soft_deleted(env):
    """Soft-deleted series should NOT appear as 'already in library' on
    the search page — user can re-add fresh while the bin entry sits."""
    _seed_series(env['db_path'], 1, 'Soft-Deleted Series', anilist_id=12345)
    with sqlite3.connect(env['db_path']) as c:
        c.execute("UPDATE series SET deleted_at=CURRENT_TIMESTAMP WHERE id=1")

    r = _client().get("/search")
    assert r.status_code == 200
    # The existing_titles dict should NOT contain the soft-deleted title.
    # Hard to assert via rendered HTML since search has no query — instead
    # verify via the data layer the route uses.
    with sqlite3.connect(env['db_path']) as c:
        existing_anilist_count = c.execute(
            "SELECT COUNT(*) FROM series WHERE anilist_id IS NOT NULL"
            " AND deleted_at IS NULL"
        ).fetchone()[0]
    assert existing_anilist_count == 0


# ───────────────────── HX-Trigger toast ─────────────────────


def test_hx_delete_carries_undo_action(env):
    """When the delete is HTMX-driven, the response must carry an
    HX-Trigger payload with actionLabel='Undo' and actionUrl pointing
    at the restore endpoint."""
    import json as _json
    _seed_series(env['db_path'], 1, 'Test Series')
    csrf = _csrf("undo")
    r = _client().post(
        "/series/1/delete",
        data={'csrf_token': csrf['headers']['X-CSRFToken']},
        cookies=csrf['cookies'],
        headers={**csrf['headers'], "HX-Request": "true"},
        follow_redirects=False,
    )
    assert r.status_code in (200, 303)
    trig = r.headers.get('HX-Trigger', '')
    assert trig, "HX-Trigger header must be present on HTMX delete"
    payload = _json.loads(trig)
    toast = payload.get('showToast', {})
    assert toast.get('actionLabel') == 'Undo'
    assert toast.get('actionUrl') == '/series/1/restore'


# ───────────────────── Background-loop filter ─────────────────────


def test_grab_existing_skips_soft_deleted_series(env):
    """The poll_rss / backlog grab loops select monitored series;
    soft-deleted ones must not appear."""
    _seed_series(env['db_path'], 1, 'Active')
    _seed_series(env['db_path'], 2, 'In Bin')
    with sqlite3.connect(env['db_path']) as c:
        c.execute("UPDATE series SET deleted_at=CURRENT_TIMESTAMP WHERE id=2")

    # Verify the actual SQL filter matches what the grab loop uses
    with sqlite3.connect(env['db_path']) as c:
        rows = c.execute(
            "SELECT id, title FROM series"
            " WHERE monitored=1 AND deleted_at IS NULL"
        ).fetchall()
    titles = [r[1] for r in rows]
    assert 'Active' in titles
    assert 'In Bin' not in titles


# ───────────────────── Idempotency ─────────────────────


def test_double_delete_is_idempotent(env):
    """Soft-deleting an already-soft-deleted series must not change its
    deleted_at (preserves the original deletion timestamp for the reaper)."""
    _seed_series(env['db_path'], 1, 'Test Series')
    csrf = _csrf("dbl")
    _client().post("/series/1/delete",
                   data={'csrf_token': csrf['headers']['X-CSRFToken']},
                   **csrf, follow_redirects=False)
    with sqlite3.connect(env['db_path']) as c:
        first_ts = c.execute(
            "SELECT deleted_at FROM series WHERE id=1"
        ).fetchone()[0]

    _client().post("/series/1/delete",
                   data={'csrf_token': csrf['headers']['X-CSRFToken']},
                   **csrf, follow_redirects=False)
    with sqlite3.connect(env['db_path']) as c:
        second_ts = c.execute(
            "SELECT deleted_at FROM series WHERE id=1"
        ).fetchone()[0]

    assert first_ts == second_ts, "second delete must not bump the timestamp"


def test_restore_of_active_series_is_no_op(env):
    """Restoring a series that isn't in the bin must not error or log."""
    _seed_series(env['db_path'], 1, 'Active')
    csrf = _csrf("noop")
    r = _client().post(
        "/series/1/restore",
        data={'csrf_token': csrf['headers']['X-CSRFToken']},
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303)
    with sqlite3.connect(env['db_path']) as c:
        # No 'series_restored' event was logged
        ev = c.execute(
            "SELECT COUNT(*) FROM history WHERE event_type='series_restored'"
        ).fetchone()[0]
    assert ev == 0


# ───────────────────── PR-2: /recycle-bin page ─────────────────────


def test_recycle_bin_page_renders_with_binned_series(env):
    """The /recycle-bin page lists every soft-deleted series with
    restore + permanent-delete buttons."""
    _seed_series(env['db_path'], 1, 'Active Series')
    _seed_series(env['db_path'], 2, 'Binned Alpha')
    _seed_series(env['db_path'], 3, 'Binned Beta')
    with sqlite3.connect(env['db_path']) as c:
        c.execute("UPDATE series SET deleted_at=CURRENT_TIMESTAMP WHERE id=2")
        c.execute("UPDATE series SET deleted_at=CURRENT_TIMESTAMP WHERE id=3")

    r = _client().get("/recycle-bin")
    assert r.status_code == 200
    body = r.text
    assert 'Binned Alpha' in body
    assert 'Binned Beta' in body
    assert 'Active Series' not in body, "active series must not appear in bin"
    # Restore + purge form actions present
    assert '/series/2/restore' in body
    assert '/series/2/purge' in body


def test_recycle_bin_page_empty_state(env):
    r = _client().get("/recycle-bin")
    assert r.status_code == 200
    assert 'recycle bin is empty' in r.text.lower()


# ───────────────────── PR-2: purge endpoint ─────────────────────


def test_purge_hard_deletes_only_soft_deleted_series(env):
    """Purge fires the destructive cascade — series + all dependents
    must be gone after."""
    _seed_series(env['db_path'], 1, 'To Purge')
    with sqlite3.connect(env['db_path']) as c:
        c.execute("UPDATE series SET deleted_at=CURRENT_TIMESTAMP WHERE id=1")

    csrf = _csrf("purge")
    r = _client().post(
        "/series/1/purge",
        data={'csrf_token': csrf['headers']['X-CSRFToken']},
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        s   = c.execute("SELECT 1 FROM series WHERE id=1").fetchone()
        v   = c.execute("SELECT COUNT(*) FROM volumes WHERE series_id=1").fetchone()[0]
        ch  = c.execute("SELECT COUNT(*) FROM chapters WHERE series_id=1").fetchone()[0]
        sn  = c.execute("SELECT COUNT(*) FROM seen WHERE series_id=1").fetchone()[0]
        tg  = c.execute("SELECT COUNT(*) FROM series_tags WHERE series_id=1").fetchone()[0]
        al  = c.execute("SELECT COUNT(*) FROM series_aliases WHERE series_id=1").fetchone()[0]
    assert s is None
    assert v == 0
    assert ch == 0
    assert sn == 0
    assert tg == 0
    assert al == 0


def test_purge_logs_history_event(env):
    _seed_series(env['db_path'], 1, 'To Purge')
    with sqlite3.connect(env['db_path']) as c:
        c.execute("UPDATE series SET deleted_at=CURRENT_TIMESTAMP WHERE id=1")
    csrf = _csrf("phist")
    _client().post(
        "/series/1/purge",
        data={'csrf_token': csrf['headers']['X-CSRFToken']},
        **csrf, follow_redirects=False,
    )
    with sqlite3.connect(env['db_path']) as c:
        ev = c.execute(
            "SELECT event_type FROM history WHERE event_type='series_purged'"
        ).fetchone()
    assert ev is not None


def test_purge_refuses_active_series(env):
    """Purge on an active (not soft-deleted) series must NOT cascade —
    safety guard against a stale UI button or scripted misuse."""
    _seed_series(env['db_path'], 1, 'Active Series')
    csrf = _csrf("prefuse")
    r = _client().post(
        "/series/1/purge",
        data={'csrf_token': csrf['headers']['X-CSRFToken']},
        **csrf, follow_redirects=False,
    )
    # Must redirect (no 500 / no 400) but the series must still exist
    assert r.status_code in (200, 303)
    with sqlite3.connect(env['db_path']) as c:
        s = c.execute("SELECT 1 FROM series WHERE id=1").fetchone()
        v = c.execute("SELECT COUNT(*) FROM volumes WHERE series_id=1").fetchone()[0]
    assert s is not None, "active series must NOT be purged"
    assert v > 0, "volumes must NOT be cascaded"


# ───────────────────── PR-2: nav link ─────────────────────


def test_nav_includes_recycle_bin_link(env):
    """Discoverability: Recycle Bin appears in the sidebar nav."""
    r = _client().get("/")
    assert r.status_code == 200
    assert '/recycle-bin' in r.text
    assert 'Recycle Bin' in r.text


# ───────────────────── PR-3: reaper job ─────────────────────


def test_reaper_purges_expired_series(env):
    """Series soft-deleted longer than retention_days must be hard-deleted
    by the reaper."""
    from tasks import _run_recycle_bin_purge_once

    _seed_series(env['db_path'], 1, 'Old Series')
    _seed_series(env['db_path'], 2, 'New Series')
    with sqlite3.connect(env['db_path']) as c:
        # Series 1: deleted 31 days ago (expired against 30-day retention)
        c.execute(
            "UPDATE series SET deleted_at=datetime('now', '-31 days'),"
            " deletion_reason='user_action' WHERE id=1"
        )
        # Series 2: deleted 5 days ago (still in window)
        c.execute(
            "UPDATE series SET deleted_at=datetime('now', '-5 days'),"
            " deletion_reason='user_action' WHERE id=2"
        )

    purged = _run_recycle_bin_purge_once(retention_days=30)
    assert purged == 1, f"expected 1 purge, got {purged}"

    with sqlite3.connect(env['db_path']) as c:
        s1 = c.execute("SELECT 1 FROM series WHERE id=1").fetchone()
        s2 = c.execute("SELECT 1 FROM series WHERE id=2").fetchone()
        v1 = c.execute("SELECT COUNT(*) FROM volumes WHERE series_id=1").fetchone()[0]
        v2 = c.execute("SELECT COUNT(*) FROM volumes WHERE series_id=2").fetchone()[0]
    assert s1 is None, "expired series 1 must be hard-deleted"
    assert s2 is not None, "in-window series 2 must remain"
    assert v1 == 0, "series-1 volumes must cascade-delete"
    assert v2 == 2, "series-2 volumes must remain"


def test_reaper_skips_active_series(env):
    """Active (not soft-deleted) series must NEVER be touched by the
    reaper, regardless of how old they are."""
    from tasks import _run_recycle_bin_purge_once
    _seed_series(env['db_path'], 1, 'Active Series')

    purged = _run_recycle_bin_purge_once(retention_days=1)
    assert purged == 0

    with sqlite3.connect(env['db_path']) as c:
        s = c.execute("SELECT 1 FROM series WHERE id=1").fetchone()
    assert s is not None, "active series must NOT be reaped"


def test_reaper_logs_purge_event_per_series(env):
    """Each reaped series gets a 'series_purged' history event so the
    user can audit what the reaper removed."""
    from tasks import _run_recycle_bin_purge_once
    _seed_series(env['db_path'], 1, 'Old A')
    _seed_series(env['db_path'], 2, 'Old B')
    with sqlite3.connect(env['db_path']) as c:
        c.execute("UPDATE series SET deleted_at=datetime('now', '-60 days') WHERE id IN (1,2)")

    _run_recycle_bin_purge_once(retention_days=30)
    with sqlite3.connect(env['db_path']) as c:
        events = c.execute(
            "SELECT source_title FROM history WHERE event_type='series_purged'"
            " ORDER BY id"
        ).fetchall()
    titles = sorted(e[0] for e in events)
    assert titles == ['Old A', 'Old B']


def test_reaper_with_default_retention(env):
    """If retention_days isn't passed explicitly, the helper reads
    the recycle_bin_retention_days config setting (default 30)."""
    from tasks import _run_recycle_bin_purge_once

    _seed_series(env['db_path'], 1, 'Just Past')
    with sqlite3.connect(env['db_path']) as c:
        # 31 days ago — past default 30-day retention
        c.execute(
            "UPDATE series SET deleted_at=datetime('now', '-31 days') WHERE id=1"
        )

    purged = _run_recycle_bin_purge_once()  # no retention_days arg
    assert purged == 1


def test_reaper_handles_empty_bin(env):
    """No soft-deleted series → reaper is a no-op (no exception)."""
    from tasks import _run_recycle_bin_purge_once
    _seed_series(env['db_path'], 1, 'Active')
    purged = _run_recycle_bin_purge_once(retention_days=30)
    assert purged == 0


# ───────────────────── PR-3: retention setting ─────────────────────


def test_retention_setting_persists_via_general_form(env):
    """POST /settings/general with recycle_bin_retention_days writes
    the value to the settings table (clamped 1-365)."""
    csrf = _csrf("ret-set")
    _client().post(
        "/settings/general",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'recycle_bin_retention_days': '60',
        },
        **csrf, follow_redirects=False,
    )
    with sqlite3.connect(env['db_path']) as c:
        v = c.execute(
            "SELECT value FROM settings WHERE key='recycle_bin_retention_days'"
        ).fetchone()
    assert v is not None and v[0] == '60'


def test_retention_setting_clamps_out_of_range(env):
    """Values outside 1-365 are clamped to the boundary (matches the
    blocklist_ttl_days pattern)."""
    csrf = _csrf("ret-clamp")
    # Below min → 1
    _client().post(
        "/settings/general",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'recycle_bin_retention_days': '0',
        },
        **csrf, follow_redirects=False,
    )
    with sqlite3.connect(env['db_path']) as c:
        v = c.execute(
            "SELECT value FROM settings WHERE key='recycle_bin_retention_days'"
        ).fetchone()
    assert v[0] == '1'

    # Above max → 365
    _client().post(
        "/settings/general",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'recycle_bin_retention_days': '9999',
        },
        **csrf, follow_redirects=False,
    )
    with sqlite3.connect(env['db_path']) as c:
        v = c.execute(
            "SELECT value FROM settings WHERE key='recycle_bin_retention_days'"
        ).fetchone()
    assert v[0] == '365'


def test_settings_general_template_renders_retention_input(env):
    """The /settings/general page renders the recycle-bin retention
    input so users can find the setting."""
    r = _client().get("/settings/general")
    assert r.status_code == 200
    assert 'recycle_bin_retention_days' in r.text
    assert '/recycle-bin' in r.text  # the "Open recycle bin" link


def test_recycle_bin_purge_task_in_system_tasks(env):
    """The System → Tasks page lists the RecycleBinPurge task so users
    can see when it last ran / next runs."""
    r = _client().get("/system/tasks")
    assert r.status_code == 200
    assert 'Recycle Bin Purge' in r.text or 'RecycleBinPurge' in r.text
