"""PR 3: cleanup_stuck_state reconciles three patterns the app used
to accumulate indefinitely: grabbed-but-no-download_id volumes,
pending_releases for deleted/unmonitored series, and import_queue
rows stuck in pending/partial for >30 days. Prior behaviour only
ran a subset of this at startup, so a long-running container drifted."""

import os
import shutil
import sqlite3
import sys
import tempfile
import threading

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


@pytest.fixture
def _process_globals_restored():
    import main, shared, security

    main_config = main.CONFIG
    main_values = dict(main.CONFIG)
    shared_config = shared.CONFIG
    shared_values = dict(shared.CONFIG)
    cipher = security._SECRET_CIPHER
    yield
    assert main.CONFIG is main_config
    assert main.CONFIG == main_values
    assert shared.CONFIG is shared_config
    assert shared.CONFIG == shared_values
    assert security._SECRET_CIPHER is cipher


@pytest.fixture
def env(_process_globals_restored):
    import main, shared, security

    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close()
    os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-stuck-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    orig_main_config = main.CONFIG
    orig_main_values = dict(main.CONFIG)
    orig_shared_config = shared.CONFIG
    orig_shared_values = dict(shared.CONFIG)
    orig_cipher = security._SECRET_CIPHER
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
        main.CONFIG = orig_main_config
        main.CONFIG.clear()
        main.CONFIG.update(orig_main_values)
        shared.CONFIG = orig_shared_config
        shared.CONFIG.clear()
        shared.CONFIG.update(orig_shared_values)
        security._SECRET_CIPHER = orig_cipher
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)
        shutil.rmtree(key_dir)


def _seed_series(db_path, sid, monitored=1):
    with sqlite3.connect(db_path) as c:
        c.execute(
            "INSERT INTO series(id, title, search_pattern, enabled, monitored,"
            " monitor_mode) VALUES(?, 'S', 'S', 1, ?, 'all')",
            (sid, monitored),
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
    assert stats["volumes_reset"] == 1
    with sqlite3.connect(env) as c:
        r = c.execute("SELECT status, grabbed_at, download_id FROM volumes").fetchone()
    assert r[0] == "wanted"
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
    assert stats["volumes_reset"] == 0
    with sqlite3.connect(env) as c:
        status = c.execute("SELECT status FROM volumes").fetchone()[0]
    assert status == "grabbed"


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
    assert stats["volumes_reset"] == 0


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
    assert stats["volumes_reset"] == 0


def test_deletes_pending_releases_for_deleted_series(env):
    from main import cleanup_stuck_state

    # series id 99 never existed
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO pending_releases(series_id, url, title) "
            " VALUES(99, 'https://example/r1', 'Orphan Title')"
        )
    stats = cleanup_stuck_state()
    assert stats["pending_deleted"] == 1
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
    assert stats["pending_deleted"] == 1


def test_preserves_pending_releases_for_active_monitored_series(env):
    from main import cleanup_stuck_state

    _seed_series(env, 7, monitored=1)
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO pending_releases(series_id, url, title) "
            " VALUES(7, 'https://example/r1', 'Legit Title')"
        )
    stats = cleanup_stuck_state()
    assert stats["pending_deleted"] == 0
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
    assert stats["queue_failed"] == 1
    with sqlite3.connect(env) as c:
        status = c.execute("SELECT status FROM import_queue").fetchone()[0]
    assert status == "failed"


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
    assert stats["queue_failed"] == 0


# ───────────────────── Phase 4: stuck 'importing' rows ─────────────────────


def test_recovers_expired_and_legacy_importing_leases(env):
    """Lease expiry, never queue age, determines importing recovery."""
    from main import cleanup_stuck_state

    _seed_series(env, 7)
    with sqlite3.connect(env) as c:
        # Legacy importing row with no lease metadata is always recoverable.
        c.execute(
            "INSERT INTO import_queue(series_id, download_id, torrent_name,"
            " status, created_at) VALUES(7, 'dl-old', 'OldImporting',"
            " 'importing', datetime('now', '-10 hours'))"
        )
        # Queue age is irrelevant while a live owner lease exists.
        c.execute(
            "INSERT INTO import_queue(series_id, download_id, torrent_name,"
            " status, lease_owner, lease_expires_at, created_at)"
            " VALUES(7, 'dl-live', 'LiveImporting', 'importing', 'owner-live',"
            " datetime('now', '+5 minutes'), datetime('now', '-40 days'))"
        )

    stats = cleanup_stuck_state()
    assert stats["importing_reset"] == 1
    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        rows = {
            r["torrent_name"]: (r["status"], r["lease_owner"], r["lease_expires_at"])
            for r in c.execute(
                "SELECT torrent_name, status, lease_owner, lease_expires_at"
                " FROM import_queue"
            ).fetchall()
        }
    assert rows["OldImporting"] == ("pending", None, None)
    assert rows["LiveImporting"][0:2] == ("importing", "owner-live")


def test_expired_lease_recovers_to_partial_and_preserves_needs_review(env):
    """Recovery preserves child decisions and derives partial from review state."""
    from main import cleanup_stuck_state

    _seed_series(env, 7)
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO import_queue(id, series_id, download_id, torrent_name,"
            " status, lease_owner, lease_expires_at, created_at)"
            " VALUES(99, 7, 'dl-needs-review', 'NeedsReview', 'importing',"
            " 'dead-owner', datetime('now', '-1 second'), datetime('now'))"
        )
        c.execute(
            "INSERT INTO import_queue_files(queue_id, filename, src_path,"
            " dst_path, status) VALUES(99, 'foo.cbz', '/src/foo.cbz',"
            " '/dst/foo.cbz', 'needs_review')"
        )

    stats = cleanup_stuck_state()
    assert stats["importing_reset"] == 1
    with sqlite3.connect(env) as c:
        queue = c.execute(
            "SELECT status, lease_owner, lease_expires_at"
            " FROM import_queue WHERE id=99"
        ).fetchone()
        child = c.execute(
            "SELECT status FROM import_queue_files WHERE queue_id=99"
        ).fetchone()
    assert queue == ("partial", None, None)
    assert child == ("needs_review",)


def test_importing_recovery_ignores_created_at_and_legacy_threshold(env):
    """The retained importing_stale_hours argument cannot weaken lease rules."""
    from main import cleanup_stuck_state

    _seed_series(env, 7)
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO import_queue(series_id, download_id, torrent_name,"
            " status, lease_owner, lease_expires_at, created_at)"
            " VALUES(7, 'dl-recent-expired', 'RecentExpired', 'importing',"
            " 'dead-owner', datetime('now', '-1 second'), datetime('now'))"
        )
        c.execute(
            "INSERT INTO import_queue(series_id, download_id, torrent_name,"
            " status, lease_owner, lease_expires_at, created_at)"
            " VALUES(7, 'dl-old-live', 'OldLive', 'importing', 'live-owner',"
            " datetime('now', '+5 minutes'), datetime('now', '-100 days'))"
        )

    stats = cleanup_stuck_state(importing_stale_hours=1)
    assert stats["importing_reset"] == 1
    with sqlite3.connect(env) as c:
        rows = dict(
            c.execute(
                "SELECT torrent_name, status FROM import_queue"
                " WHERE torrent_name IN ('RecentExpired','OldLive')"
            ).fetchall()
        )
    assert rows == {
        "RecentExpired": "pending",
        "OldLive": "importing",
    }


def test_stale_pending_cleanup_only_resets_volume_after_parent_cas(env, monkeypatch):
    """A lost parent CAS must not reset the claimed worker's volume."""
    import tasks
    from main import cleanup_stuck_state

    _seed_series(env, 7)
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, download_id)"
            " VALUES(7, 1, 'grabbed', 'dl-race')"
        )
        c.execute(
            "INSERT INTO import_queue(series_id, download_id, torrent_name,"
            " status, created_at) VALUES(7, 'dl-race', 'CleanupRace',"
            " 'pending', datetime('now', '-40 days'))"
        )

    monkeypatch.setattr(
        tasks,
        "fail_stale_pending_import_queue_row",
        lambda *args, **kwargs: False,
    )
    stats = cleanup_stuck_state()
    assert stats["queue_failed"] == 0
    with sqlite3.connect(env) as c:
        queue_status = c.execute(
            "SELECT status FROM import_queue WHERE download_id='dl-race'"
        ).fetchone()[0]
        volume_state = c.execute(
            "SELECT status, download_id FROM volumes WHERE series_id=7"
        ).fetchone()
    assert queue_status == "pending"
    assert volume_state == ("grabbed", "dl-race")


def test_stale_parent_failure_preserves_live_same_download_sibling(env):
    """A parent CAS does not grant ownership of a sibling's shared download."""
    from main import cleanup_stuck_state

    _seed_series(env, 7)
    with sqlite3.connect(env) as db:
        db.execute(
            "INSERT INTO volumes(series_id, volume_num, status, download_id)"
            " VALUES(7, 1, 'grabbed', 'shared-stale')"
        )
        db.execute(
            "INSERT INTO import_queue(series_id, download_id, torrent_name,"
            " status, created_at) VALUES(7, 'shared-stale', 'Stale A',"
            " 'pending', datetime('now', '-40 days'))"
        )
        db.execute(
            "INSERT INTO import_queue(series_id, download_id, torrent_name,"
            " status, lease_owner, lease_expires_at)"
            " VALUES(7, 'shared-stale', 'Live B', 'importing', 'owner-b',"
            " datetime('now', '+5 minutes'))"
        )

    stats = cleanup_stuck_state(
        events_retention_days=0,
        orphan_pack_cleanup=False,
    )
    assert stats["queue_failed"] == 1
    with sqlite3.connect(env) as db:
        rows = dict(
            db.execute(
                "SELECT torrent_name, status FROM import_queue"
                " WHERE download_id='shared-stale'"
            ).fetchall()
        )
        volume = db.execute(
            "SELECT status, download_id FROM volumes WHERE series_id=7"
        ).fetchone()
    assert rows == {"Stale A": "failed", "Live B": "importing"}
    assert volume == ("grabbed", "shared-stale")


def test_stale_reset_is_series_scoped_for_shared_download_hash(env):
    import tasks

    _seed_series(env, 7)
    _seed_series(env, 8)
    with sqlite3.connect(env) as db:
        db.executemany(
            "INSERT INTO volumes(series_id, volume_num, status, download_id)"
            " VALUES(?, 1, 'grabbed', 'cross-series')",
            [(7,), (8,)],
        )
        queue_id = db.execute(
            "INSERT INTO import_queue(series_id, download_id, torrent_name,"
            " status) VALUES(7, 'cross-series', 'Series 7', 'pending')"
        ).lastrowid
        assert queue_id is not None
        assert tasks._fail_stale_queue_and_reset_volume(
            db,
            queue_id=int(queue_id),
            observed_status="pending",
            download_id="cross-series",
            series_id=7,
        )

    with sqlite3.connect(env) as db:
        states = dict(
            db.execute(
                "SELECT series_id, status FROM volumes ORDER BY series_id"
            ).fetchall()
        )
    assert states == {7: "wanted", 8: "grabbed"}


def test_concurrent_claim_and_stale_reset_preserve_shared_download(env):
    """Separate WAL writers cannot reset a sibling immediately before claim."""
    import tasks
    from import_lease import claim_import_queue_row

    _seed_series(env, 7)
    with sqlite3.connect(env) as db:
        db.execute(
            "INSERT INTO volumes(series_id, volume_num, status, download_id)"
            " VALUES(7, 1, 'grabbed', 'shared-concurrent')"
        )
        stale_id = db.execute(
            "INSERT INTO import_queue(series_id, download_id, torrent_name,"
            " status) VALUES(7, 'shared-concurrent', 'Stale A', 'pending')"
        ).lastrowid
        sibling_id = db.execute(
            "INSERT INTO import_queue(series_id, download_id, torrent_name,"
            " status) VALUES(7, 'shared-concurrent', 'Sibling B', 'pending')"
        ).lastrowid
    assert stale_id is not None
    assert sibling_id is not None

    barrier = threading.Barrier(2)
    results: dict[str, bool] = {}
    errors: list[BaseException] = []
    result_lock = threading.Lock()

    def _record(name: str, operation) -> None:
        try:
            with sqlite3.connect(env, timeout=5) as db:
                db.execute("PRAGMA busy_timeout=5000")
                barrier.wait()
                value = operation(db)
            with result_lock:
                results[name] = value
        except BaseException as exc:
            with result_lock:
                errors.append(exc)

    cleanup = threading.Thread(
        target=_record,
        args=(
            "cleanup",
            lambda db: tasks._fail_stale_queue_and_reset_volume(
                db,
                queue_id=int(stale_id),
                observed_status="pending",
                download_id="shared-concurrent",
                series_id=7,
            ),
        ),
    )
    claim = threading.Thread(
        target=_record,
        args=(
            "claim",
            lambda db: claim_import_queue_row(
                db,
                int(sibling_id),
                "owner-b",
            ),
        ),
    )
    cleanup.start()
    claim.start()
    cleanup.join(timeout=10)
    claim.join(timeout=10)

    assert not cleanup.is_alive()
    assert not claim.is_alive()
    assert errors == []
    assert results == {"cleanup": True, "claim": True}
    with sqlite3.connect(env) as db:
        queue_rows = dict(
            db.execute(
                "SELECT torrent_name, status FROM import_queue"
                " WHERE download_id='shared-concurrent'"
            ).fetchall()
        )
        volume = db.execute(
            "SELECT status, download_id FROM volumes WHERE series_id=7"
        ).fetchone()
    assert queue_rows == {"Stale A": "failed", "Sibling B": "importing"}
    assert volume == ("grabbed", "shared-concurrent")


def test_stats_dict_includes_importing_reset_key(env):
    """Schema check: the stats dict must include the new key so
    downstream consumers (logs, tests, dashboards) don't KeyError."""
    from main import cleanup_stuck_state

    _seed_series(env, 7)
    stats = cleanup_stuck_state()
    assert "importing_reset" in stats
    assert stats["importing_reset"] == 0  # nothing to recover


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
    assert stats["events_pruned"] == 1, (
        f"expected 1 event pruned; got {stats['events_pruned']}"
    )
    with sqlite3.connect(env) as c:
        rows = [
            r[0]
            for r in c.execute(
                "SELECT message FROM events WHERE message IN ('old', 'recent')"
            ).fetchall()
        ]
    assert rows == ["recent"]  # 'old' is gone


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
    assert stats["events_pruned"] == 0
    # Tighter 5d retention: prunes it
    stats = cleanup_stuck_state(events_retention_days=5)
    assert stats["events_pruned"] == 1


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
    assert stats["events_pruned"] == 0
    with sqlite3.connect(env) as c:
        n = c.execute("SELECT COUNT(*) FROM events WHERE message='forever'").fetchone()[
            0
        ]
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
            [(f"msg-{i}",) for i in range(12000)],
        )
    stats = cleanup_stuck_state()
    assert stats["events_pruned"] == 12000


# ───────────────────── log_event dedup rate-limit ─────────────────────


def test_log_event_dedup_rate_limits_repeated_messages(env):
    """log_event(..., dedup=True) only writes one row per (type, series_id,
    message[:80]) tuple per TTL. Without this, a stable repeating
    failure (content_path missing) spams the events table forever."""
    from events import log_event, _LOG_DEDUP_LAST

    _LOG_DEDUP_LAST.clear()
    _seed_series(env, 7)
    for _ in range(20):
        log_event("error", "Import queue: content_path not found: /a/b", 7, dedup=True)
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
        log_event("info", f"distinct message {i}", 7)
    with sqlite3.connect(env) as c:
        n = c.execute("SELECT COUNT(*) FROM events WHERE event_type='info'").fetchone()[
            0
        ]
    assert n == 5


# ───────────────────── Phase 4b: orphan pack cleanup ─────────────────────


def test_deletes_orphan_pack_rows(env):
    """Phase 4b: pack rows (vol_num NULL) in 'wanted' state with no
    source URL or download_id are dead state and get deleted.
    Production observed 1,201 such rows accumulating over weeks
    because import_pipeline.py:691 was UPDATEing them to 'wanted'
    instead of DELETEing them."""
    from main import cleanup_stuck_state

    _seed_series(env, 7)
    with sqlite3.connect(env) as c:
        # Orphan pack: no source info, no purpose
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, pack_type,"
            " size_bytes, monitored)"
            " VALUES(7, NULL, 'wanted', 'volume', 21000000000, 1)"
        )
        # Functional pack: has source — should be PRESERVED
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, pack_type,"
            " source_url, torrent_name, monitored)"
            " VALUES(7, NULL, 'wanted', 'volume', 'http://x/y', 'realpack', 1)"
        )
        # Individual volume: should be PRESERVED regardless
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, monitored)"
            " VALUES(7, 1.0, 'wanted', 1)"
        )
        # Pack in grabbed state: PRESERVED (still in flight)
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, pack_type,"
            " source_url, monitored)"
            " VALUES(7, NULL, 'grabbed', 'volume', 'http://x/active', 1)"
        )

    stats = cleanup_stuck_state()
    assert stats["orphan_packs_deleted"] == 1, (
        f"expected exactly 1 orphan pack deleted; got {stats['orphan_packs_deleted']}"
    )
    with sqlite3.connect(env) as c:
        n = c.execute("SELECT COUNT(*) FROM volumes WHERE series_id=7").fetchone()[0]
    assert n == 3, f"expected 3 rows preserved; got {n}"


def test_orphan_pack_cleanup_can_be_disabled(env):
    """orphan_pack_cleanup=False disables the phase entirely. Useful
    if a future bug-class makes mass deletion risky and we want to
    pause it without rolling back the whole cleanup loop."""
    from main import cleanup_stuck_state

    _seed_series(env, 7)
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, pack_type, monitored)"
            " VALUES(7, NULL, 'wanted', 'volume', 1)"
        )
    stats = cleanup_stuck_state(orphan_pack_cleanup=False)
    assert stats["orphan_packs_deleted"] == 0
    with sqlite3.connect(env) as c:
        n = c.execute("SELECT COUNT(*) FROM volumes WHERE series_id=7").fetchone()[0]
    assert n == 1


def test_no_manga_files_found_event_is_deduped(env):
    """Production observation: 'No manga files found in <path> — skipping'
    fired 207K times for one ghost torrent path before the dedup landed.
    The import_pipeline call site at line 389 must pass dedup=True so the
    same (path, torrent_name) tuple within 1h only logs once."""
    import inspect, import_queue

    src = inspect.getsource(import_queue)
    # Find the line and verify dedup=True is present
    idx = src.find("No manga files found in")
    assert idx >= 0, "emitter not found in source"
    # Look at the next ~150 chars after the message string for `dedup=True`
    snippet = src[idx : idx + 300]
    assert "dedup=True" in snippet, (
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
        events = [
            r[0]
            for r in c.execute(
                "SELECT message FROM events WHERE event_type='stuck_cleanup'"
            ).fetchall()
        ]
    assert any("reset" in e and "no-download_id" in e for e in events), events
    assert any("deleted" in e and "pending_release" in e for e in events), events


def test_stats_are_zero_when_nothing_stuck(env):
    from main import cleanup_stuck_state

    stats = cleanup_stuck_state()
    assert stats == {
        "volumes_reset": 0,
        "pending_deleted": 0,
        "queue_failed": 0,
        "importing_reset": 0,
        "events_pruned": 0,
        "orphan_packs_deleted": 0,
    }
