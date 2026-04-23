"""PR 2b: the startup metadata backfill loop honours MangaDex
Retry-After signals. A 429 response halts further per-series
refresh work until the deadline elapses, preventing the backfill
from racing toward an IP ban on large new libraries."""
import sys
import time

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


def test_parse_retry_after_seconds_numeric():
    from main import _parse_retry_after_seconds
    assert _parse_retry_after_seconds("30") == 30.0


def test_parse_retry_after_seconds_http_date():
    import datetime as dt
    from main import _parse_retry_after_seconds
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=90))
    v = _parse_retry_after_seconds(future.strftime('%a, %d %b %Y %H:%M:%S GMT'))
    assert v is not None and 80 <= v <= 100


def test_parse_retry_after_seconds_none_on_garbage():
    from main import _parse_retry_after_seconds
    assert _parse_retry_after_seconds(None) is None
    assert _parse_retry_after_seconds("") is None
    assert _parse_retry_after_seconds("xyz") is None


def test_set_backoff_extends_deadline_forward_only():
    import tasks
    # Reset
    tasks._MDX_BACKOFF_UNTIL = 0.0
    tasks._mdx_set_backoff(30, "test")
    v1 = tasks._MDX_BACKOFF_UNTIL
    # A shorter backoff must NOT reduce the existing deadline
    tasks._mdx_set_backoff(5, "test")
    assert tasks._MDX_BACKOFF_UNTIL == v1
    # A longer one extends
    tasks._mdx_set_backoff(60, "test")
    assert tasks._MDX_BACKOFF_UNTIL > v1


def test_maybe_backoff_from_exception_honours_429():
    import tasks
    tasks._MDX_BACKOFF_UNTIL = 0.0

    class _Resp:
        status_code = 429
        headers = {'Retry-After': '45'}

    class _Exc(Exception):
        def __init__(self):
            super().__init__("mock")
            self.response = _Resp()

    tasks._maybe_backoff_from_exception(_Exc())
    assert tasks._mdx_backoff_active() is True
    assert tasks._MDX_BACKOFF_UNTIL - time.time() >= 40


def test_maybe_backoff_from_exception_ignores_other_statuses():
    import tasks
    tasks._MDX_BACKOFF_UNTIL = 0.0

    class _Resp:
        status_code = 500
        headers = {'Retry-After': '60'}

    class _Exc(Exception):
        def __init__(self):
            super().__init__("mock")
            self.response = _Resp()

    tasks._maybe_backoff_from_exception(_Exc())
    # Non-429 errors shouldn't set global backoff
    assert tasks._mdx_backoff_active() is False


def test_maybe_backoff_from_exception_handles_missing_response_cleanly():
    import tasks
    tasks._MDX_BACKOFF_UNTIL = 0.0
    tasks._maybe_backoff_from_exception(RuntimeError("no response attr"))
    assert tasks._mdx_backoff_active() is False
