"""Notification dispatch + embed builders + Komga scan trigger.

Eleventh module extracted from main.py. Contains four tiny helpers
that fan out events to external systems:

  - notify_discord       — legacy entry point; now delegates to the
                           notification_connections router's
                           fire_notifications() so every enabled
                           connection (Discord, Slack, telegram, ntfy,
                           gotify, pushover, apprise, email, webhook…)
                           receives each event
  - make_grab_embed      — Discord embed for a grab event
  - make_complete_embed  — Discord embed for a completed download
  - trigger_komga_scan   — POST /api/v1/libraries/{id}/scan when the
                           komga_scan_enabled setting is true

`log_event` is imported lazily inside trigger_komga_scan to avoid a
circular import (main imports notifications; notifications only
touches log_event when the HTTP call returns).
"""
from __future__ import annotations

import httpx

from shared import get_cfg
from events import log_event


async def notify_discord(message: str, embed: dict | None = None,
                         event: str = 'on_grab'):
    """Send notifications via all enabled notification connections."""
    from routers.notification_connections import fire_notifications
    await fire_notifications(event, message, embed=embed)


def make_grab_embed(series_title: str, vol_label: str, indexer: str,
                    protocol: str, client_name: str, cover_url: str = '') -> dict:
    return {
        'title': f'⬇ Grabbed — {series_title}',
        'description': f'**{vol_label}**  ·  {indexer} [{protocol}] → {client_name}',
        'color': 0xffd060,
        'thumbnail': {'url': cover_url} if cover_url else {},
    }


def make_complete_embed(series_title: str, vol_label: str, cover_url: str = '') -> dict:
    return {
        'title': f'✅ Downloaded — {series_title}',
        'description': f'**{vol_label}** download complete',
        'color': 0x5dde94,
        'thumbnail': {'url': cover_url} if cover_url else {},
    }


async def trigger_komga_scan():
    """Optionally trigger a Komga library scan after downloads complete."""
    if get_cfg('komga_scan_enabled', 'false').lower() != 'true':
        return
    url = get_cfg('komga_url')
    lib = get_cfg('komga_library_id')
    if not url or not lib:
        return
    user = get_cfg('komga_user')
    pw   = get_cfg('komga_pass')
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{url}/api/v1/libraries/{lib}/scan",
                auth=(user, pw) if user else None
            )
        log_event('komga_scan', f"Triggered Komga library scan → HTTP {r.status_code}")
    except Exception as e:
        log_event('error', f"Komga scan failed: {e}")
