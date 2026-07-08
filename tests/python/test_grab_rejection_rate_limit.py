"""Regression tests for the grab-rejection log rate-limit.

Production observation: 829 'rejected_release' events in 24h, all from
the same handful of releases hitting edition-mismatch on every RSS
poll. One Hunter x Hunter colored release alone produced 173 identical
events. The rejection logic is correct (release IS being filtered) but
the LOGGING fires every poll — drowning the events table in noise and
making it harder to spot rare rejections that operators actually care
about.

The fix: in-memory rate-limit on (series_id, title, reason) tuples
with 1h TTL. Same rejection within that window silently skips the
log_event call. The actual rejection decision (return False from
grab_item) is unchanged — only the noise is suppressed.
"""
import sys

import pytest

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401


@pytest.fixture
def reset_rejection_cache():
    """Clear the module-level rate-limit cache between tests so they
    don't leak state into each other."""
    import grab_dedup
    grab_dedup._rejection_log_last.clear()
    yield
    grab_dedup._rejection_log_last.clear()


def test_first_call_logs(reset_rejection_cache, monkeypatch):
    import grab_dedup
    import events
    calls = []
    monkeypatch.setattr(events, 'log_event',
                        lambda *a, **kw: calls.append((a, kw)))
    grab_dedup._log_grab_rejection(1, 'Foo Vol 1', 'edition mismatch')
    assert len(calls) == 1, "first rejection must log"


def test_repeat_same_key_within_ttl_is_silent(reset_rejection_cache, monkeypatch):
    import grab_dedup
    import events
    calls = []
    monkeypatch.setattr(events, 'log_event',
                        lambda *a, **kw: calls.append(a))
    for _ in range(20):
        grab_dedup._log_grab_rejection(1, 'Foo Vol 1', 'edition mismatch')
    assert len(calls) == 1, (
        f"expected only the first call to log; got {len(calls)} log_event calls"
    )


def test_different_series_id_logs_independently(reset_rejection_cache, monkeypatch):
    import grab_dedup
    import events
    calls = []
    monkeypatch.setattr(events, 'log_event',
                        lambda *a, **kw: calls.append(a))
    # Same title/reason but different series — separate cache entries
    grab_dedup._log_grab_rejection(1, 'Foo Vol 1', 'edition mismatch')
    grab_dedup._log_grab_rejection(2, 'Foo Vol 1', 'edition mismatch')
    grab_dedup._log_grab_rejection(3, 'Foo Vol 1', 'edition mismatch')
    assert len(calls) == 3


def test_different_titles_log_independently(reset_rejection_cache, monkeypatch):
    import grab_dedup
    import events
    calls = []
    monkeypatch.setattr(events, 'log_event',
                        lambda *a, **kw: calls.append(a))
    grab_dedup._log_grab_rejection(1, 'Foo Vol 1', 'edition mismatch')
    grab_dedup._log_grab_rejection(1, 'Foo Vol 2', 'edition mismatch')
    grab_dedup._log_grab_rejection(1, 'Foo Vol 3', 'edition mismatch')
    assert len(calls) == 3


def test_different_reasons_log_independently(reset_rejection_cache, monkeypatch):
    """A release rejected for different reasons (e.g. edition then
    quality) should log each — they're distinct diagnostic signals."""
    import grab_dedup
    import events
    calls = []
    monkeypatch.setattr(events, 'log_event',
                        lambda *a, **kw: calls.append(a))
    grab_dedup._log_grab_rejection(1, 'Foo Vol 1', 'edition mismatch (a, b)')
    grab_dedup._log_grab_rejection(1, 'Foo Vol 1', 'quality below cutoff')
    grab_dedup._log_grab_rejection(1, 'Foo Vol 1', 'blocked group')
    assert len(calls) == 3


def test_log_again_after_ttl_expires(reset_rejection_cache, monkeypatch):
    """After the TTL passes, the same key logs once more. This means
    long-running operators still see periodic confirmation that the
    filter is firing for unchanged releases (sanity, not silenced
    forever)."""
    import grab_dedup
    import events
    calls = []
    monkeypatch.setattr(events, 'log_event',
                        lambda *a, **kw: calls.append(a))
    # Force the TTL to be tiny for this test
    monkeypatch.setattr(grab_dedup, '_REJECTION_LOG_TTL_S', 0.05)
    grab_dedup._log_grab_rejection(1, 'Foo Vol 1', 'edition mismatch')
    grab_dedup._log_grab_rejection(1, 'Foo Vol 1', 'edition mismatch')  # silenced
    import time
    time.sleep(0.1)
    grab_dedup._log_grab_rejection(1, 'Foo Vol 1', 'edition mismatch')  # logs again
    assert len(calls) == 2, (
        f"expected 2 log_events (first + after-TTL); got {len(calls)}"
    )


def test_log_event_failure_does_not_break_grab(reset_rejection_cache, monkeypatch):
    """The except: pass guard. log_event raising must not propagate."""
    import grab_dedup
    import events
    def _boom(*a, **kw): raise RuntimeError("DB write failed")
    monkeypatch.setattr(events, 'log_event', _boom)
    # Must not raise
    grab_dedup._log_grab_rejection(1, 'Foo Vol 1', 'edition mismatch')


def test_cache_does_not_grow_unboundedly_across_ttl_cycles(reset_rejection_cache, monkeypatch):
    """Sanity: across multiple TTL cycles, the cache stays bounded.
    This is the property that matters in production — a long-running
    process polling thousands of releases per cycle for months on end
    must not accumulate cache entries indefinitely."""
    import grab_dedup
    import events
    import time
    monkeypatch.setattr(events, 'log_event', lambda *a, **kw: None)
    monkeypatch.setattr(grab_dedup, '_REJECTION_LOG_TTL_S', 0.05)

    # Three TTL cycles, ~500 unique entries per cycle.
    for cycle in range(3):
        for i in range(500):
            grab_dedup._log_grab_rejection(i + cycle * 1000, f'Title-{cycle}-{i}', 'reason')
        # Sleep past the TTL so next cycle's entries start fresh
        time.sleep(0.06)

    # One more call to trigger the prune branch (>1000)
    grab_dedup._log_grab_rejection(99999, 'final', 'reason')

    # Across 3 cycles we wrote 1500 entries; if pruning never ran we'd
    # have 1500+1=1501. The prune cap is 1000 entries; after the final
    # call's prune, only the most-recent-cycle entries plus the fresh
    # 'final' should remain. Allow generous slack: <= 1000 means the
    # cap is doing its job.
    assert len(grab_dedup._rejection_log_last) <= 1000, (
        f"cache grew past prune cap; size = {len(grab_dedup._rejection_log_last)}"
    )
