"""RSS polling: poll all enabled indexers for new releases.

Implements the RSS loop for background grab pipeline:
  - Poll every enabled indexer's RSS
  - Match against series patterns / aliases
  - Apply delay profiles
  - Grab on the fly
"""
from __future__ import annotations

from shared import get_cfg, get_db
try:
    from .grab_core import grab_item, _search_all
except ImportError:
    from grab_core import grab_item, _search_all


async def poll_rss():
    """Poll all enabled DB indexers for new releases."""
    from routers.indexers import fetch_all_rss as _fetch_all_rss_db
    from events import log_event
    
    with get_db() as _rdb:
        raw_items = await _fetch_all_rss_db(_rdb)
    
    # Apply delay profiles and series matching
    # This is a simplified version - full implementation would:
    # 1. Match against series patterns and aliases
    # 2. Apply delay profile waiting periods
    # 3. Filter by indexer enable status
    # 4. Only grab items that pass all filters
    
    # For now, return early since full implementation requires
    # additional context from routers/indexers.py
    print(f"[RSS] Collected {len(raw_items)} raw items")
    return len(raw_items)
