"""PR 2a: per-indexer backoff. Indexers that return 429/403/5xx are
skipped on subsequent RSS / search cycles until the deadline elapses.
Prior behaviour retried at full speed on every cycle, which risked
IP bans on shared Prowlarr instances.
"""
import os
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


@pytest.fixture
def env():
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-bo-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()

    # Seed indexer
    with sqlite3.connect(db.name) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled)"
            " VALUES(1, 'TestIdx', 'torznab', 'http://indexer.test', 'k', 1)"
        )

    try:
        yield db.name
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


def test_parse_retry_after_accepts_seconds_and_http_date():
    from routers.indexers import _parse_retry_after
    assert _parse_retry_after("120") == 120.0
    # HTTP date — should return non-negative delta-seconds from 'now'
    # Can't test exact values without time freezing, but can verify shape
    import datetime as dt
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=60))
    hdr = future.strftime('%a, %d %b %Y %H:%M:%S GMT')
    v = _parse_retry_after(hdr)
    assert v is not None and 50 <= v <= 70


def test_parse_retry_after_returns_none_for_garbage():
    from routers.indexers import _parse_retry_after
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("") is None
    assert _parse_retry_after("not-a-number") is None


def test_should_backoff_on_429():
    from routers.indexers import _should_backoff_on_response

    class _R:
        def __init__(self, s): self.status_code = s

    should, reason = _should_backoff_on_response(_R(429))
    assert should is True
    assert '429' in reason


def test_should_backoff_on_503():
    from routers.indexers import _should_backoff_on_response

    class _R:
        def __init__(self, s): self.status_code = s

    assert _should_backoff_on_response(_R(503))[0] is True


def test_should_not_backoff_on_200():
    from routers.indexers import _should_backoff_on_response

    class _R:
        def __init__(self, s): self.status_code = s

    assert _should_backoff_on_response(_R(200))[0] is False


def test_record_failure_persists_deadline(env):
    from routers.indexers import _indexer_record_failure, _indexer_is_backed_off
    deadline = _indexer_record_failure(
        1, status=429, retry_after_header="60", reason="rate limited"
    )
    assert deadline > time.time()
    is_off, d = _indexer_is_backed_off(1)
    assert is_off is True
    assert abs(d - deadline) < 1


def test_record_failure_uses_exponential_backoff_without_retry_after(env):
    from routers.indexers import _indexer_record_failure, _BACKOFF_MIN, _BACKOFF_BASE

    d1 = _indexer_record_failure(1, status=500, retry_after_header=None, reason='x')
    assert d1 - time.time() >= _BACKOFF_MIN * 0.9
    # second failure: _BACKOFF_MIN * BASE^1 = 120s
    d2 = _indexer_record_failure(1, status=500, retry_after_header=None, reason='x')
    assert d2 - time.time() >= _BACKOFF_MIN * _BACKOFF_BASE * 0.9


def test_record_success_clears_state(env):
    from routers.indexers import _indexer_record_failure, _indexer_record_success, _indexer_is_backed_off
    _indexer_record_failure(1, status=429, retry_after_header="60", reason='x')
    assert _indexer_is_backed_off(1)[0] is True
    _indexer_record_success(1)
    assert _indexer_is_backed_off(1)[0] is False


def test_backoff_past_deadline_allows_retry(env):
    from routers.indexers import _indexer_record_failure, _indexer_is_backed_off
    _indexer_record_failure(1, status=429, retry_after_header="60", reason='x')
    # Force deadline into the past
    with sqlite3.connect(env) as c:
        c.execute(
            "UPDATE indexer_backoff SET retry_after=? WHERE indexer_id=1",
            (time.time() - 10,)
        )
    assert _indexer_is_backed_off(1)[0] is False
