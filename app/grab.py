"""Grab layer: send releases to download clients and record the state.

This module re-exports symbols from the split modules:
  - grab_dedup.py: deduplication and rate limiting
  - grab_core.py: core grab_item logic
  - grab_backlog.py: backlog search
  - grab_rss.py: RSS polling

For the full grab implementation, see the split modules.
"""
from __future__ import annotations

# Re-export from split modules
from .grab_dedup import (
    _GRABBING_URLS,
    _log_grab_rejection,
    _prune_rejection_log,
    _rejection_log_last,
    _REJECTION_LOG_TTL_S,
    _REJECTION_LOG_PRUNE_EVERY,
)
from .grab_core import grab_item, _collect_and_score, _search_all
from .grab_backlog import grab_existing, _grab_existing_inner, _select_covering_packs, search_complete_pack, matches, is_complete_pack, extract_volume_range
from .grab_rss import poll_rss

# Re-export for backward compatibility with tests that monkeypatch grab.grab_url
from clients import grab_url

# Re-export for backward compatibility with tests
from events import log_event
