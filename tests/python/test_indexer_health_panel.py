"""Tests for the indexer health panel (PR #122).

The `indexer_backoff` table already records failures (status, reason,
consecutive_failures, retry_after) but Mangarr never surfaced any of it
in the UI — users had to read logs or run SQL to know why an indexer
was silently skipped.

This PR exposes that data per row on `/indexers`:
  - "In backoff — next poll in 4m 30s" when retry_after > now
  - "N recent failures (currently retrying)" when there's a streak but
    no active backoff
  - Last failure detail (HTTP status + reason + when)
  - Last successful grab timestamp

Soaks up the Sonarr top-7 pain points around auto-disable surprises and
"why isn't this indexer working?" questions.
"""
import os
import sqlite3
import sys
import tempfile
import time

import pytest

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401


@pytest.fixture
def env(tmp_path):
    """Fresh DB; tests seed indexers + indexer_backoff rows directly."""
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-health-keys-")

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


# ───────────────────── _all_indexers enrichment ─────────────────────


def test_pristine_indexer_has_zero_health_state(env):
    """An indexer with no failures / no backoff_row has all-zero/None
    health fields and `backoff_active` = False."""
    from routers.indexers import _all_indexers
    from shared import get_db

    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled)"
            " VALUES(1, 'Clean', 'torznab', 'http://t', 'k', 1)"
        )

    with get_db() as db:
        rows = _all_indexers(db)
    row = next(r for r in rows if r['id'] == 1)
    assert row['backoff_active'] is False
    assert row['backoff_seconds'] == 0
    assert row['consecutive_failures'] == 0
    assert row['last_status'] is None
    assert row['last_reason'] is None


def test_backoff_active_when_retry_after_is_future(env):
    """retry_after > now → backoff_active=True with seconds remaining."""
    from routers.indexers import _all_indexers
    from shared import get_db

    future_ts = time.time() + 600  # 10 minutes from now
    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled)"
            " VALUES(2, 'Throttled', 'torznab', 'http://t', 'k', 1)"
        )
        c.execute(
            "INSERT INTO indexer_backoff(indexer_id, retry_after,"
            " consecutive_failures, last_status, last_reason)"
            " VALUES(2, ?, 3, 429, 'rate-limited')",
            (future_ts,)
        )

    with get_db() as db:
        rows = _all_indexers(db)
    row = next(r for r in rows if r['id'] == 2)
    assert row['backoff_active'] is True
    assert 590 <= row['backoff_seconds'] <= 600, (
        f"seconds should be ~600 (give or take a sec), got {row['backoff_seconds']}"
    )
    assert row['consecutive_failures'] == 3
    assert row['last_status'] == 429
    assert row['last_reason'] == 'rate-limited'
    assert row['backoff_until'] is not None, "must include ISO timestamp for tooltip"


def test_backoff_inactive_when_retry_after_is_past(env):
    """retry_after in the past → backoff_active=False, but consecutive_failures
    + last_reason are still preserved (so user can see the recent streak even
    though it's not blocking polls right now)."""
    from routers.indexers import _all_indexers
    from shared import get_db

    past_ts = time.time() - 60  # 1 minute ago
    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled)"
            " VALUES(3, 'WasFlaky', 'torznab', 'http://t', 'k', 1)"
        )
        c.execute(
            "INSERT INTO indexer_backoff(indexer_id, retry_after,"
            " consecutive_failures, last_status, last_reason)"
            " VALUES(3, ?, 2, 503, 'service unavailable')",
            (past_ts,)
        )

    with get_db() as db:
        rows = _all_indexers(db)
    row = next(r for r in rows if r['id'] == 3)
    assert row['backoff_active'] is False
    assert row['backoff_seconds'] == 0
    assert row['backoff_until'] is None
    # Detail still surfaced for the UI's "N recent failures (currently retrying)" state
    assert row['consecutive_failures'] == 2
    assert row['last_reason'] == 'service unavailable'


# ───────────────────── /indexers page rendering ─────────────────────


def test_indexers_page_shows_active_backoff_banner(env):
    """When an indexer is in active backoff, the row body must include the
    'In backoff' banner with countdown."""
    future_ts = time.time() + 270  # 4m 30s from now
    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled)"
            " VALUES(10, 'Cooling', 'torznab', 'http://t', 'k', 1)"
        )
        c.execute(
            "INSERT INTO indexer_backoff(indexer_id, retry_after,"
            " consecutive_failures, last_status, last_reason)"
            " VALUES(10, ?, 5, 429, 'too many requests')",
            (future_ts,)
        )

    client = _client()
    r = client.get("/indexers")
    assert r.status_code == 200
    body = r.text
    assert 'In backoff' in body, "active-backoff state must show banner"
    assert 'too many requests' in body, "last-failure reason must be visible"
    assert 'HTTP 429' in body, "last-failure HTTP status must be visible"


def test_indexers_page_shows_failure_streak_warning(env):
    """When backoff has expired but failures recently happened, show a
    softer warning (gold border, not red). User can see something's been
    going wrong without it blocking polls."""
    past_ts = time.time() - 30
    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled)"
            " VALUES(11, 'WarnState', 'torznab', 'http://t', 'k', 1)"
        )
        c.execute(
            "INSERT INTO indexer_backoff(indexer_id, retry_after,"
            " consecutive_failures, last_status, last_reason)"
            " VALUES(11, ?, 4, 500, 'internal server error')",
            (past_ts,)
        )

    client = _client()
    r = client.get("/indexers")
    assert r.status_code == 200
    body = r.text
    assert 'recent failure' in body, (
        "must show the failure-streak warning even when not currently backed off"
    )
    assert 'currently retrying' in body
    assert 'internal server error' in body


def test_indexers_page_no_health_panel_for_clean_indexer(env):
    """No backoff record, no failures → no health panel rendered."""
    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled)"
            " VALUES(12, 'Pristine', 'torznab', 'http://t', 'k', 1)"
        )

    client = _client()
    r = client.get("/indexers")
    assert r.status_code == 200
    body = r.text
    snippet = body[body.find('Pristine'):body.find('Pristine') + 1000]
    assert 'In backoff' not in snippet
    assert 'recent failure' not in snippet


def test_indexers_page_shows_last_grab_timestamp_when_present(env):
    """If the indexer has a last_grab_at, the panel-body should display it."""
    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled)"
            " VALUES(13, 'Grabby', 'torznab', 'http://t', 'k', 1)"
        )
        c.execute(
            "INSERT INTO history(event_type, indexer, source_title, created_at)"
            " VALUES('grabbed', 'Grabby', 'a', '2026-04-30 12:00:00')"
        )

    client = _client()
    r = client.get("/indexers")
    assert r.status_code == 200
    body = r.text
    assert 'Last grab' in body, (
        "panel-body must show 'Last grab:' label when there's a successful grab"
    )
    # The format_date filter renders relative dates by default ('5 mo ago'),
    # so we don't assert on the literal year — just that the label section
    # rendered with non-empty content (no '—' fallback).
    snippet = body[body.find('Last grab'):body.find('Last grab') + 200]
    assert 'text-ember' in snippet, (
        f"the date span should be rendered (text-ember class), got: {snippet!r}"
    )
