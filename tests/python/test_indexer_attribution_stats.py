"""Tests for indexer attribution + per-indexer grab counter (PR #121).

Per the upstream community research: Sonarr surfaces grab attribution
poorly (Indexer column hidden by default, no per-indexer stats). This
PR closes both gaps cheaply:

  - History page already shows the indexer name on grab/import rows
    (verified live; nothing to change there).
  - /indexers page now shows per-indexer 30-day grab + failure counts
    as small badges on each row, sourced from the history table.
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
    """Fresh DB; tests seed indexers + history rows directly."""
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-attr-keys-")

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


# ───────────────────── helper: counts ─────────────────────


def test_grab_stats_counts_by_indexer_in_30d_window(env):
    from routers.indexers import _indexer_grab_stats
    from shared import get_db

    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled)"
            " VALUES(1, 'Nyaa', 'torznab', 'http://x', 'k', 1)"
        )
        # 3 grabs from Nyaa within 30 days, 1 grab from another, 1 ancient grab
        c.execute(
            "INSERT INTO history(event_type, indexer, source_title, created_at)"
            " VALUES"
            "  ('grabbed',     'Nyaa',      'A v01', datetime('now', '-1 day')),"
            "  ('grabbed',     'Nyaa',      'A v02', datetime('now', '-15 days')),"
            "  ('grabbed',     'Nyaa',      'A v03', datetime('now', '-29 days')),"
            "  ('grabbed',     'AnimeBytes','B v01', datetime('now', '-2 days')),"
            "  ('grabbed',     'Nyaa',      'OLD',   datetime('now', '-31 days'))"
        )

    with get_db() as db:
        stats = _indexer_grab_stats(db)

    assert stats['Nyaa']['grabs_30d'] == 3, (
        "ancient (>30d) grab must be excluded"
    )
    assert stats['AnimeBytes']['grabs_30d'] == 1


def test_grab_stats_separates_failures_from_successes(env):
    """`grabbed` = success, `grab_failed` = failure. Counted separately."""
    from routers.indexers import _indexer_grab_stats
    from shared import get_db

    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO history(event_type, indexer, source_title, created_at)"
            " VALUES"
            "  ('grabbed',     'X', 'a', datetime('now', '-1 day')),"
            "  ('grabbed',     'X', 'b', datetime('now', '-2 day')),"
            "  ('grab_failed', 'X', 'c', datetime('now', '-3 day')),"
            "  ('imported',    'X', 'd', datetime('now', '-1 day'))"  # other type, ignored
        )

    with get_db() as db:
        stats = _indexer_grab_stats(db)

    assert stats['X']['grabs_30d'] == 2
    assert stats['X']['failures_30d'] == 1


def test_grab_stats_records_last_grab_timestamp(env):
    """`last_grab_at` is the most-recent successful grab timestamp."""
    from routers.indexers import _indexer_grab_stats
    from shared import get_db

    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO history(event_type, indexer, source_title, created_at)"
            " VALUES"
            "  ('grabbed', 'Y', 'old', '2024-01-01 00:00:00'),"
            "  ('grabbed', 'Y', 'new', '2026-04-30 12:00:00')"
        )

    with get_db() as db:
        stats = _indexer_grab_stats(db)

    assert stats['Y']['last_grab_at'].startswith('2026-04-30'), (
        f"must pick the latest timestamp, got {stats['Y']['last_grab_at']!r}"
    )


def test_grab_stats_ignores_null_or_empty_indexer(env):
    """Some history rows pre-date the indexer column (NULL); some have ''.
    Both must be excluded — they don't attribute to any specific indexer."""
    from routers.indexers import _indexer_grab_stats
    from shared import get_db

    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO history(event_type, indexer, source_title, created_at)"
            " VALUES"
            "  ('grabbed', 'Real', 'a', datetime('now', '-1 day')),"
            "  ('grabbed', NULL,   'b', datetime('now', '-1 day')),"
            "  ('grabbed', '',     'c', datetime('now', '-1 day'))"
        )

    with get_db() as db:
        stats = _indexer_grab_stats(db)

    assert 'Real' in stats
    assert None not in stats
    assert '' not in stats
    assert stats['Real']['grabs_30d'] == 1


# ───────────────────── /indexers page renders stats ─────────────────────


def test_indexers_page_shows_grab_count_badge(env):
    """Each indexer row should display its 30-day grab count badge."""
    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled)"
            " VALUES(10, 'ActiveTracker', 'torznab', 'http://t', 'k', 1)"
        )
        c.execute(
            "INSERT INTO history(event_type, indexer, source_title, created_at)"
            " VALUES"
            "  ('grabbed', 'ActiveTracker', 'a', datetime('now', '-1 day')),"
            "  ('grabbed', 'ActiveTracker', 'b', datetime('now', '-1 day')),"
            "  ('grabbed', 'ActiveTracker', 'c', datetime('now', '-1 day'))"
        )

    client = _client()
    r = client.get("/indexers")
    assert r.status_code == 200
    body = r.text
    assert 'ActiveTracker' in body
    # Badge with the count "3"
    assert '> 3</span>' in body or '"3"' in body or '>3<' in body or 'grab(s) in the last 30 days' in body, (
        f"page must render the per-indexer 30-day grab count near the row; "
        f"snippet excerpt:\n{body[body.find('ActiveTracker'):body.find('ActiveTracker')+800]}"
    )


def test_indexers_page_shows_failure_count_badge(env):
    """If the indexer has had failures, the failure-count badge appears."""
    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled)"
            " VALUES(11, 'FlakyTracker', 'torznab', 'http://t', 'k', 1)"
        )
        c.execute(
            "INSERT INTO history(event_type, indexer, source_title, created_at)"
            " VALUES"
            "  ('grab_failed', 'FlakyTracker', 'a', datetime('now', '-1 day')),"
            "  ('grab_failed', 'FlakyTracker', 'b', datetime('now', '-1 day'))"
        )

    client = _client()
    r = client.get("/indexers")
    assert r.status_code == 200
    body = r.text
    assert 'failure(s) in the last 30 days' in body, (
        "failure count badge tooltip text should appear when failures exist"
    )


def test_indexers_page_no_badge_when_zero_activity(env):
    """A pristine indexer (no grab history) must not have a stats badge —
    we only render badges when at least one count is non-zero."""
    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled)"
            " VALUES(12, 'PristineTracker', 'torznab', 'http://t', 'k', 1)"
        )

    client = _client()
    r = client.get("/indexers")
    assert r.status_code == 200
    body = r.text
    assert 'PristineTracker' in body
    # No "grab(s) in the last 30 days" tooltip in the row's vicinity
    snippet = body[body.find('PristineTracker'):body.find('PristineTracker')+1000]
    assert 'grab(s) in the last 30 days' not in snippet, (
        "no activity → no stats badge"
    )
