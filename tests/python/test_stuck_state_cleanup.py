"""PR 3: cleanup_stuck_state reconciles three patterns the app used
to accumulate indefinitely: grabbed-but-no-download_id volumes,
pending_releases for deleted/unmonitored series, and import_queue
rows stuck in pending/partial for >30 days. Prior behaviour only
ran a subset of this at startup, so a long-running container drifted."""
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
    key_dir = tempfile.mkdtemp(prefix="mangarr-stuck-keys-")

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


def _seed_series(db_path, sid, monitored=1):
    with sqlite3.connect(db_path) as c:
        c.execute(
            "INSERT INTO series(id, title, search_pattern, enabled, monitored,"
            " monitor_mode) VALUES(?, 'S', 'S', 1, ?, 'all')",
            (sid, monitored)
        )


def test_resets_stale_grabbed_volume_without_download_id(env):
    from main import cleanup_stuck_state
    _seed_series(env, 7)
    with sqlite3.connect(env) as c:
        # Grabbed 7 hours ago, no download_id
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, monitored,"
            " grabbed_at) VALUES(7, 1.0, 'grabbed', 1,"
            " datetime('now', '-7 hours'))"
        )
    stats = cleanup_stuck_state()
    assert stats['volumes_reset'] == 1
    with sqlite3.connect(env) as c:
        r = c.execute("SELECT status, grabbed_at, download_id FROM volumes").fetchone()
    assert r[0] == 'wanted'
    assert r[1] is None
    assert r[2] is None


def test_recently_grabbed_without_download_id_is_left_alone(env):
    """A grab that just fired might not have had its download_id saved yet."""
    from main import cleanup_stuck_state
    _seed_series(env, 7)
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, monitored,"
            " grabbed_at) VALUES(7, 1.0, 'grabbed', 1,"
            " datetime('now', '-30 minutes'))"
        )
    stats = cleanup_stuck_state()
    assert stats['volumes_reset'] == 0
    with sqlite3.connect(env) as c:
        status = c.execute("SELECT status FROM volumes").fetchone()[0]
    assert status == 'grabbed'


def test_does_not_reset_volume_with_download_id(env):
    """Having a download_id means the grab succeeded — the client
    just hasn't finished yet. Never reset these."""
    from main import cleanup_stuck_state
    _seed_series(env, 7)
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, monitored,"
            " grabbed_at, download_id) VALUES(7, 1.0, 'grabbed', 1,"
            " datetime('now', '-10 hours'), 'abc123')"
        )
    stats = cleanup_stuck_state()
    assert stats['volumes_reset'] == 0


def test_suwayomi_volumes_are_protected(env):
    """Suwayomi/DDL jobs complete asynchronously and can legitimately
    sit in grabbed state for a long time; never reset them."""
    from main import cleanup_stuck_state
    _seed_series(env, 7)
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, monitored,"
            " grabbed_at, client) VALUES(7, 1.0, 'grabbed', 1,"
            " datetime('now', '-12 hours'), 'suwayomi')"
        )
    stats = cleanup_stuck_state()
    assert stats['volumes_reset'] == 0


def test_deletes_pending_releases_for_deleted_series(env):
    from main import cleanup_stuck_state
    # series id 99 never existed
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO pending_releases(series_id, url, title) "
            " VALUES(99, 'https://example/r1', 'Orphan Title')"
        )
    stats = cleanup_stuck_state()
    assert stats['pending_deleted'] == 1
    with sqlite3.connect(env) as c:
        count = c.execute("SELECT COUNT(*) FROM pending_releases").fetchone()[0]
    assert count == 0


def test_deletes_pending_releases_for_unmonitored_series(env):
    from main import cleanup_stuck_state
    _seed_series(env, 7, monitored=0)
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO pending_releases(series_id, url, title) "
            " VALUES(7, 'https://example/r1', 'Unmonitored Title')"
        )
    stats = cleanup_stuck_state()
    assert stats['pending_deleted'] == 1


def test_preserves_pending_releases_for_active_monitored_series(env):
    from main import cleanup_stuck_state
    _seed_series(env, 7, monitored=1)
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO pending_releases(series_id, url, title) "
            " VALUES(7, 'https://example/r1', 'Legit Title')"
        )
    stats = cleanup_stuck_state()
    assert stats['pending_deleted'] == 0
    with sqlite3.connect(env) as c:
        count = c.execute("SELECT COUNT(*) FROM pending_releases").fetchone()[0]
    assert count == 1


def test_fails_import_queue_stuck_in_pending_over_30_days(env):
    from main import cleanup_stuck_state
    _seed_series(env, 7)
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO import_queue(series_id, download_id, torrent_name,"
            " status, created_at) VALUES(7, 'dl-123', 'OldGrab',"
            " 'pending', datetime('now', '-40 days'))"
        )
    stats = cleanup_stuck_state()
    assert stats['queue_failed'] == 1
    with sqlite3.connect(env) as c:
        status = c.execute("SELECT status FROM import_queue").fetchone()[0]
    assert status == 'failed'


def test_recent_pending_import_queue_is_left_alone(env):
    from main import cleanup_stuck_state
    _seed_series(env, 7)
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO import_queue(series_id, download_id, torrent_name,"
            " status, created_at) VALUES(7, 'dl-fresh', 'NewGrab',"
            " 'pending', datetime('now', '-1 day'))"
        )
    stats = cleanup_stuck_state()
    assert stats['queue_failed'] == 0


# ───────────────────── Phase 4: stuck 'importing' rows ─────────────────────


def test_reverts_stuck_importing_queue_after_threshold(env):
    """Phase 4: import_queue rows stuck in 'importing' state past
    threshold get reverted to 'failed'. This was the production bug
    where a worker died mid-import (or hit "database is locked"
    trying to mark itself failed) and left the row claimed forever.
    Auto-import status_loop never retried the row because it only
    looks at 'pending'."""
    from main import cleanup_stuck_state
    _seed_series(env, 7)
    with sqlite3.connect(env) as c:
        # Old stuck importing row — should be recovered
        c.execute(
            "INSERT INTO import_queue(series_id, download_id, torrent_name,"
            " status, created_at) VALUES(7, 'dl-old', 'OldImporting',"
            " 'importing', datetime('now', '-10 hours'))"
        )
        # Recent importing row — should be left alone (worker may still be live)
        c.execute(
            "INSERT INTO import_queue(series_id, download_id, torrent_name,"
            " status, created_at) VALUES(7, 'dl-fresh', 'FreshImporting',"
            " 'importing', datetime('now', '-30 minutes'))"
        )

    stats = cleanup_stuck_state()
    assert stats['importing_reset'] == 1, (
        f"expected 1 stuck 'importing' to be reset; got {stats['importing_reset']}"
    )
    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        rows = {r['torrent_name']: r['status'] for r in c.execute(
            "SELECT torrent_name, status FROM import_queue"
        ).fetchall()}
    assert rows['OldImporting']   == 'failed', "old stuck-importing must be reset to 'failed'"
    assert rows['FreshImporting'] == 'importing', "fresh in-flight import must NOT be touched"


def test_does_not_revert_importing_with_needs_review_files(env):
    """Safety: rows with needs_review files carry user decisions and
    must NOT be auto-recovered — operator must intervene via the
    reconcile UI. Mirrors the planning logic in app/reconcile.py."""
    from main import cleanup_stuck_state
    _seed_series(env, 7)
    with sqlite3.connect(env) as c:
        # Old stuck importing row...
        c.execute(
            "INSERT INTO import_queue(id, series_id, download_id, torrent_name,"
            " status, created_at) VALUES(99, 7, 'dl-needs-review', 'NeedsReview',"
            " 'importing', datetime('now', '-10 hours'))"
        )
        # ...with at least one needs_review file
        c.execute(
            "INSERT INTO import_queue_files(queue_id, filename, src_path,"
            " dst_path, status) VALUES(99, 'foo.cbz', '/src/foo.cbz',"
            " '/dst/foo.cbz', 'needs_review')"
        )

    stats = cleanup_stuck_state()
    assert stats['importing_reset'] == 0, (
        "must NOT auto-recover rows with needs_review files"
    )
    with sqlite3.connect(env) as c:
        status = c.execute(
            "SELECT status FROM import_queue WHERE id=99"
        ).fetchone()[0]
    assert status == 'importing', "row must remain 'importing' for operator review"


def test_importing_threshold_param_overridable(env):
    """The threshold is parameterized (not just hardcoded). Useful for
    tests + future tuning. Default is 6h."""
    from main import cleanup_stuck_state
    _seed_series(env, 7)
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO import_queue(series_id, download_id, torrent_name,"
            " status, created_at) VALUES(7, 'dl-2h', 'TwoHoursOld',"
            " 'importing', datetime('now', '-2 hours'))"
        )
    # With default threshold (6h), 2h-old row is left alone
    stats = cleanup_stuck_state()
    assert stats['importing_reset'] == 0
    # With 1h threshold, same row gets recovered
    stats = cleanup_stuck_state(importing_stale_hours=1)
    assert stats['importing_reset'] == 1


def test_stats_dict_includes_importing_reset_key(env):
    """Schema check: the stats dict must include the new key so
    downstream consumers (logs, tests, dashboards) don't KeyError."""
    from main import cleanup_stuck_state
    _seed_series(env, 7)
    stats = cleanup_stuck_state()
    assert 'importing_reset' in stats
    assert stats['importing_reset'] == 0  # nothing to recover


# ───────────────────── Phase 5: events table retention ─────────────────────


def test_prunes_events_older_than_retention(env):
    """Phase 5: events older than `events_retention_days` are deleted.
    Production hit 5.8M rows / ~1GB; without pruning the events table
    grows indefinitely. Default retention 90 days."""
    from main import cleanup_stuck_state
    _seed_series(env, 7)
    with sqlite3.connect(env) as c:
        # Old event (>90 days) — should be pruned
        c.execute(
            "INSERT INTO events(event_type, message, created_at)"
            " VALUES('error', 'old', datetime('now', '-100 days'))"
        )
        # Recent event (<90 days) — should be kept
        c.execute(
            "INSERT INTO events(event_type, message, created_at)"
            " VALUES('error', 'recent', datetime('now', '-1 day'))"
        )

    stats = cleanup_stuck_state()
    assert stats['events_pruned'] == 1, (
        f"expected 1 event pruned; got {stats['events_pruned']}"
    )
    with sqlite3.connect(env) as c:
        rows = [r[0] for r in c.execute(
            "SELECT message FROM events WHERE message IN ('old', 'recent')"
        ).fetchall()]
    assert rows == ['recent']  # 'old' is gone


def test_events_retention_threshold_overridable(env):
    """The retention is parameterizable for tests + future tuning."""
    from main import cleanup_stuck_state
    _seed_series(env, 7)
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO events(event_type, message, created_at)"
            " VALUES('error', 'one-week-old', datetime('now', '-7 days'))"
        )
    # Default 90d retention: keeps it
    stats = cleanup_stuck_state()
    assert stats['events_pruned'] == 0
    # Tighter 5d retention: prunes it
    stats = cleanup_stuck_state(events_retention_days=5)
    assert stats['events_pruned'] == 1


def test_events_retention_disabled_when_zero(env):
    """events_retention_days=0 disables pruning entirely (never delete).
    Useful for users who want to keep historical events for forensics."""
    from main import cleanup_stuck_state
    _seed_series(env, 7)
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO events(event_type, message, created_at)"
            " VALUES('error', 'forever', datetime('now', '-365 days'))"
        )
    stats = cleanup_stuck_state(events_retention_days=0)
    assert stats['events_pruned'] == 0
    with sqlite3.connect(env) as c:
        n = c.execute("SELECT COUNT(*) FROM events WHERE message='forever'").fetchone()[0]
    assert n == 1, "events_retention_days=0 must keep all events"


def test_events_pruning_is_chunked_for_large_tables(env):
    """Sanity: even with many old events, the prune doesn't lock the
    writer for minutes. We chunk-DELETE 5K rows per transaction."""
    from main import cleanup_stuck_state
    _seed_series(env, 7)
    with sqlite3.connect(env) as c:
        # Insert 12K old events
        c.executemany(
            "INSERT INTO events(event_type, message, created_at)"
            " VALUES('error', ?, datetime('now', '-100 days'))",
            [(f'msg-{i}',) for i in range(12000)]
        )
    stats = cleanup_stuck_state()
    assert stats['events_pruned'] == 12000


# ───────────────────── log_event dedup rate-limit ─────────────────────


def test_log_event_dedup_rate_limits_repeated_messages(env):
    """log_event(..., dedup=True) only writes one row per (type, series_id,
    message[:80]) tuple per TTL. Without this, a stable repeating
    failure (content_path missing) spams the events table forever."""
    from events import log_event, _LOG_DEDUP_LAST
    _LOG_DEDUP_LAST.clear()
    _seed_series(env, 7)
    for _ in range(20):
        log_event('error', 'Import queue: content_path not found: /a/b', 7, dedup=True)
    with sqlite3.connect(env) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM events WHERE message='Import queue: content_path not found: /a/b'"
        ).fetchone()[0]
    assert n == 1, f"expected 1 event with dedup; got {n}"


def test_log_event_without_dedup_unchanged(env):
    """Default dedup=False keeps prior behavior — every call writes a row."""
    from events import log_event
    _seed_series(env, 7)
    for i in range(5):
        log_event('info', f'distinct message {i}', 7)
    with sqlite3.connect(env) as c:
        n = c.execute("SELECT COUNT(*) FROM events WHERE event_type='info'").fetchone()[0]
    assert n == 5


def test_no_manga_files_found_event_is_deduped(env):
    """Production observation: 'No manga files found in <path> — skipping'
    fired 207K times for one ghost torrent path before the dedup landed.
    The import_pipeline call site at line 389 must pass dedup=True so the
    same (path, torrent_name) tuple within 1h only logs once."""
    import inspect, import_pipeline
    src = inspect.getsource(import_pipeline)
    # Find the line and verify dedup=True is present
    idx = src.find("No manga files found in")
    assert idx >= 0, "emitter not found in source"
    # Look at the next ~150 chars after the message string for `dedup=True`
    snippet = src[idx:idx + 300]
    assert 'dedup=True' in snippet, (
        "log_event call for 'No manga files found' must opt into dedup=True. "
        "Without it, a stuck content_path produces hundreds of thousands of "
        "duplicate import events."
    )


def test_logs_events_for_each_category(env):
    from main import cleanup_stuck_state
    _seed_series(env, 7)
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, monitored,"
            " grabbed_at) VALUES(7, 1.0, 'grabbed', 1,"
            " datetime('now', '-10 hours'))"
        )
        c.execute(
            "INSERT INTO pending_releases(series_id, url, title)"
            " VALUES(99, 'https://example/pr', 'Orphan')"
        )
    cleanup_stuck_state()
    with sqlite3.connect(env) as c:
        events = [r[0] for r in c.execute(
            "SELECT message FROM events WHERE event_type='stuck_cleanup'"
        ).fetchall()]
    assert any('reset' in e and 'no-download_id' in e for e in events), events
    assert any('deleted' in e and 'pending_release' in e for e in events), events


def test_stats_are_zero_when_nothing_stuck(env):
    from main import cleanup_stuck_state
    stats = cleanup_stuck_state()
    assert stats == {
        'volumes_reset':   0,
        'pending_deleted': 0,
        'queue_failed':    0,
        'importing_reset': 0,
        'events_pruned':   0,
    }
