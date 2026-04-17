"""In-memory download-client status cache.

Purpose:
    Before this module existed, /queue and /queue/table made a live
    httpx.AsyncClient call to qBit and SAB on every pageview, bounded by
    a 0.8s render budget. A single slow upstream meant the page waited
    hundreds of ms even for warm navigation.

    This cache mirrors the Sonarr/Radarr pattern:
      - One dedicated background loop polls the download clients every
        STATUS_REFRESH_INTERVAL_SECONDS.
      - Results land in `_qbit` / `_sab` in-memory snapshots.
      - The queue route reads those snapshots instantly — no httpx on
        the request path, no wait_for, no budget.

    Freshness is explicit. Every read sees a timestamp + a label
    (live / stale / unavailable / warming_up) so the UI can show
    operators when the data is out of date. See `freshness_label`.

    The cache never mutates the DB and never raises out of refresh() —
    upstream failures are recorded and the loop continues.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

import httpx

from shared import get_cfg

log = logging.getLogger(__name__)

# How often the background loop polls qBit/SAB. Hardcoded for this pass;
# ship as a setting only if operators ask.
STATUS_REFRESH_INTERVAL_SECONDS = 20.0

# Per-call HTTP timeout when polling an upstream. The background loop
# isn't on the request path, so this can be more generous than the old
# render-budget — but still bounded so a dead upstream doesn't keep the
# refresh loop spinning.
STATUS_UPSTREAM_TIMEOUT_SECONDS = 5.0

# Freshness thresholds (seconds since last successful refresh).
FRESHNESS_LIVE_MAX_AGE         = 60      # <60s → "live"
FRESHNESS_STALE_MAX_AGE        = 300     # 60-300s → "stale"
                                         # >300s → "unavailable"

FreshnessLabel = Literal["warming_up", "live", "stale", "unavailable"]


@dataclass
class DownloadClientSnapshot:
    """Immutable-ish state snapshot for one download client.

    items             - dict keyed by hash/nzo_id with per-item state.
                        Empty dict is a valid "no active downloads" result.
    fetched_at        - when the refresh attempt ended (success or failure).
    last_success_at   - most recent successful refresh (None if never).
                        Freshness is computed from this, not fetched_at,
                        so a stream of failures doesn't mask stale data.
    error             - short error string from the last failed attempt,
                        None when last attempt succeeded.
    """
    items:            dict
    fetched_at:       datetime
    last_success_at:  datetime | None = None
    error:            str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _fetch_qbit(qc: dict) -> dict:
    """Query qBittorrent. Returns {hash: info} or raises on any failure.

    Kept separate from _fetch_sab so one client's failure can't poison
    the other's snapshot.
    """
    if not qc:
        return {}
    host = (qc.get('host') or '').rstrip('/')
    user = qc.get('username') or ''
    pw   = qc.get('password') or ''
    cat  = qc.get('category') or get_cfg('category')
    async with httpx.AsyncClient(timeout=STATUS_UPSTREAM_TIMEOUT_SECONDS) as client:
        r = await client.post(
            f"{host}/api/v2/auth/login",
            data={'username': user, 'password': pw},
        )
        if 'Ok' not in r.text:
            raise RuntimeError(f"qBit auth failed (HTTP {r.status_code})")
        r2 = await client.get(
            f"{host}/api/v2/torrents/info",
            params={'category': cat},
        )
        if r2.status_code != 200:
            raise RuntimeError(f"qBit torrents/info HTTP {r2.status_code}")
        out: dict = {}
        for t in r2.json():
            h = t.get('hash', '').lower()
            out[h] = {
                'hash':          h,
                'name':          t.get('name', ''),
                'state':         t.get('state', ''),
                'progress':      round(t.get('progress', 0) * 100, 1),
                'dlspeed':       t.get('dlspeed', 0),
                'eta':           t.get('eta', -1),
                'client':        'qbittorrent',
                'error_message': t.get('stateMessage', ''),
            }
        return out


async def _fetch_sab(sc: dict) -> dict:
    """Query SABnzbd. Returns {nzo_id: info} or raises on any failure."""
    if not sc:
        return {}
    host   = (sc.get('host') or '').rstrip('/')
    apikey = sc.get('password') or ''
    if not apikey:
        return {}
    async with httpx.AsyncClient(timeout=STATUS_UPSTREAM_TIMEOUT_SECONDS) as client:
        r = await client.get(
            f"{host}/api",
            params={'mode': 'queue', 'apikey': apikey, 'output': 'json'},
        )
        if r.status_code != 200:
            raise RuntimeError(f"SAB HTTP {r.status_code}")
        out: dict = {}
        for s in r.json().get('queue', {}).get('slots', []):
            nzo = s.get('nzo_id', '')
            out[nzo] = {
                'hash':     nzo,
                'name':     s.get('filename', ''),
                'state':    s.get('status', '').lower(),
                'progress': float(s.get('percentage', 0)),
                'dlspeed':  0,
                'eta':      s.get('timeleft', ''),
                'client':   'sabnzbd',
            }
        return out


class DownloadStatusCache:
    """Single source of truth for live download-client state shown on /queue."""

    def __init__(self) -> None:
        self._qbit: DownloadClientSnapshot | None = None
        self._sab:  DownloadClientSnapshot | None = None
        # Single-flight: multiple concurrent refresh() callers collapse
        # into a single poll. The second caller returns immediately.
        self._refresh_lock = asyncio.Lock()

    # ── read-side accessors ────────────────────────────────────────────

    def snapshot_qbit(self) -> DownloadClientSnapshot | None:
        return self._qbit

    def snapshot_sab(self) -> DownloadClientSnapshot | None:
        return self._sab

    def freshness_label(
        self, snap: DownloadClientSnapshot | None, now: datetime | None = None
    ) -> FreshnessLabel:
        """Map a snapshot's age to a UI label.

        Returned values:
          "warming_up"  - no snapshot taken yet (cache empty; first refresh pending)
          "live"        - last success < 60s ago
          "stale"       - last success between 60s and 5min ago
          "unavailable" - last success > 5min ago, or never succeeded
        """
        if snap is None:
            return "warming_up"
        if snap.last_success_at is None:
            return "unavailable"
        age = ((now or _now()) - snap.last_success_at).total_seconds()
        if age <= FRESHNESS_LIVE_MAX_AGE:
            return "live"
        if age <= FRESHNESS_STALE_MAX_AGE:
            return "stale"
        return "unavailable"

    # ── refresh path ────────────────────────────────────────────────────

    async def refresh(self) -> bool:
        """Poll qBit and SAB. Updates snapshots in place.

        Returns True if this caller actually performed the poll, False if
        another refresh was in flight and this call was collapsed. Never
        raises — failures are recorded into the relevant snapshot's
        `error` field while `last_success_at` is left alone, so the UI
        keeps showing last-known-good data while surfacing the error.
        """
        if self._refresh_lock.locked():
            return False
        async with self._refresh_lock:
            from routers.download_clients import get_client_for_protocol as _gcp
            from shared import get_db as _get_db

            with _get_db() as db:
                qc = _gcp(db, 'torrent')
                sc = _gcp(db, 'nzb')

            # Run the two polls concurrently. A qBit failure must not
            # stop SAB (and vice-versa) — hence return_exceptions=True.
            qbit_result, sab_result = await asyncio.gather(
                _fetch_qbit(qc) if qc else _noop_dict(),
                _fetch_sab(sc)  if sc else _noop_dict(),
                return_exceptions=True,
            )

            self._qbit = _merge_snapshot(self._qbit, qbit_result)
            self._sab  = _merge_snapshot(self._sab,  sab_result)
            return True


async def _noop_dict() -> dict:
    return {}


def _merge_snapshot(
    prev: DownloadClientSnapshot | None, result
) -> DownloadClientSnapshot:
    """Apply one refresh result to a previous snapshot, preserving
    last-known-good items when the refresh itself failed."""
    now = _now()
    if isinstance(result, Exception):
        # Refresh failed. Keep prev items so UI shows last-known-good;
        # record the error and advance fetched_at so UI can show
        # "last tried X seconds ago".
        return DownloadClientSnapshot(
            items=prev.items if prev else {},
            fetched_at=now,
            last_success_at=prev.last_success_at if prev else None,
            error=f"{type(result).__name__}: {str(result)[:120]}",
        )
    # Success path.
    return DownloadClientSnapshot(
        items=result,
        fetched_at=now,
        last_success_at=now,
        error=None,
    )


# Module-level singleton. Tests can reach in and reset via the public
# refresh() method or by assigning fresh snapshots.
DOWNLOAD_STATUS_CACHE = DownloadStatusCache()


async def download_status_refresh_loop() -> None:
    """Background loop: refresh the cache every STATUS_REFRESH_INTERVAL_SECONDS.

    Registered from main.lifespan. Never raises out — exceptions are
    logged and the loop continues.
    """
    # Brief startup delay so init_db / ensure_wal / config load finish
    # before the first refresh touches the DB.
    await asyncio.sleep(2.0)
    while True:
        try:
            await DOWNLOAD_STATUS_CACHE.refresh()
        except Exception as e:
            log.warning("download_status_refresh_loop iteration failed: %r", e)
        await asyncio.sleep(STATUS_REFRESH_INTERVAL_SECONDS)
