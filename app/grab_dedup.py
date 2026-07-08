"""Deduplication & rate limiting helpers for grab pipeline.

Separate from grab_core to isolate the in-memory state (URL tracking,
rejection cache) from the full grab logic. This keeps grab_core pure
and makes it easier to test grab_item's side-effect-free decision
sections.
"""
from __future__ import annotations

import time as _time

import events
from shared import get_cfg


# URLs currently in-flight to a download client. asyncio is single-threaded so
# plain set ops are safe between awaits. Prevents duplicate grabs when RSS poll
# and backlog search both pass the `seen` check before either INSERT completes.
_GRABBING_URLS: set[str] = set()


# In-memory rate-limit cache for grab-rejection events. See module docstring
# and grab_core.py:_log_grab_rejection for details.
_REJECTION_LOG_TTL_S = 3600  # 1 hour
_rejection_log_last: dict[tuple[int, str, str], float] = {}
_REJECTION_LOG_PRUNE_EVERY = 10


def _prune_rejection_log() -> None:
    """Remove entries older than _REJECTION_LOG_TTL_S."""
    now = _time.monotonic()
    cutoff = now - _REJECTION_LOG_TTL_S
    for k, t in list(_rejection_log_last.items()):
        if t < cutoff:
            del _rejection_log_last[k]


def _log_grab_rejection(series_id: int, title: str, reason: str) -> None:
    """Surface a grab rejection as a `rejected_release` event with rate limiting.

    Called from grab_core on rejection paths that represent real filtering
    decisions (blocklist, edition mismatch, cross-group repack, quality cutoff).
    Normal-flow deduplication (seen, in-flight dedup) is NOT logged here —
    those aren't rejections, just guards.

    Rate-limited: the same (series_id, title, reason) tuple is only logged
    once per _REJECTION_LOG_TTL_S (default 1h).
    """
    key = (series_id, title[:120], reason[:80])
    now = _time.monotonic()
    last = _rejection_log_last.get(key)
    if last is not None and (now - last) < _REJECTION_LOG_TTL_S:
        return
    _rejection_log_last[key] = now
    if len(_rejection_log_last) > _REJECTION_LOG_PRUNE_EVERY * 2:
        _prune_rejection_log()
    try:
        events.log_event('rejected_release', f'{reason}: {title[:120]}', series_id)
    except Exception:
        pass
