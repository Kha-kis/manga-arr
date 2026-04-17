"""Health check page and operational API endpoints."""
import asyncio
import os
import shutil
import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from routers._templates import templates
from shared import get_cfg, get_db

router = APIRouter()


# Severity mapping for health checks.
# critical = core feature will not work at all (no grabs, no imports, no tracking)
# high     = major feature degraded or specific subsystem broken
# warning  = non-critical degradation (backup staleness, optional integration)
CHECK_SEVERITY = {
    'Root Folders':        'critical',
    'Series Root Folders': 'critical',
    'Quality Profiles':    'critical',
    'Indexers':            'critical',
    'Download Clients':    'critical',
    'API Key':             'high',
    'RSS Health':          'high',
    'Recent Grabs':        'warning',
    'qBittorrent':         'high',
    'SABnzbd':             'high',
    'Komga':               'warning',
    'Backups':             'warning',
}

# Fix-page URLs per check — moved here from the template so new checks only need one edit.
CHECK_FIX_URL = {
    'Root Folders':        '/settings',
    'Series Root Folders': '/settings',
    'Indexers':            '/indexers',
    'Recent Grabs':        '/indexers',
    'Download Clients':    '/download-clients',
    'qBittorrent':         '/download-clients',
    'SABnzbd':             '/download-clients',
    'Quality Profiles':    '/quality-profiles',
    'API Key':             '/settings/general',
    'Backups':             '/system/backup',
    'Komga':               '/settings#metadata',
    'RSS Health':          '/system/tasks',
}


# Per-severity HTTP timeouts. These bound how long the /health endpoint
# will wait on any single upstream probe; a healthy LAN download client
# typically responds in <200ms. Timeouts used to be 6s uniformly, which
# made p50 /health latency ~6s whenever any configured upstream was
# unreachable (connect timeout pinned to the full 6s). Short timeouts
# keep the page responsive while still reporting the slow provider as
# degraded in its own check row (not as whole-endpoint failure).
#
# Issue #31 follow-up B — /health upstream probe latency.
HEALTH_TIMEOUT_HIGH    = 2.5   # qBit, SAB — informational; a real slow/dead upstream shows as degraded
HEALTH_TIMEOUT_WARNING = 2.0   # Komga and other optional integrations


def _health_db_snapshot() -> dict:
    """Fetch every DB fact /health needs in a SINGLE connection.

    Previously /health opened 10+ separate get_db() connections (one per
    check, plus a second aggregate block for the stale-series /
    stuck-imports tables at the bottom of the page). Under background-
    writer contention each was exposed to the 5s busy_timeout and the
    waits cascaded into ~30s page stalls. Consolidating into ONE open
    connection caps the exposure to a single lock wait.
    """
    from routers.download_clients import get_client_for_protocol as _gcp_h
    with get_db() as db:
        snap = {
            # ── Fast COUNT / config facts (used by check functions) ──
            'indexers_enabled': db.execute(
                "SELECT COUNT(*) AS n FROM indexers WHERE enabled=1"
            ).fetchone()['n'],
            'download_clients_enabled': db.execute(
                "SELECT COUNT(*) AS n FROM download_clients WHERE enabled=1"
            ).fetchone()['n'],
            'quality_profiles': db.execute(
                "SELECT COUNT(*) AS n FROM quality_profiles"
            ).fetchone()['n'],
            'root_folders': [
                {'path': r['path'], 'label': r['label']}
                for r in db.execute("SELECT path, label FROM root_folders").fetchall()
            ],
            'orphan_series_rf': db.execute("""
                SELECT COUNT(*) AS n FROM series s
                WHERE s.root_folder_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM root_folders rf WHERE rf.id = s.root_folder_id
                  )
            """).fetchone()['n'],
            'wanted_volumes': db.execute(
                "SELECT COUNT(*) AS n FROM volumes WHERE status='wanted' AND monitored=1"
            ).fetchone()['n'],
            'last_grab': db.execute(
                "SELECT created_at FROM events WHERE event_type='grab'"
                " ORDER BY created_at DESC LIMIT 1"
            ).fetchone(),
            'last_rss_poll': db.execute(
                "SELECT created_at FROM events WHERE event_type='rss_poll'"
                " ORDER BY created_at DESC LIMIT 1"
            ).fetchone(),
            'qbit_client': _gcp_h(db, 'torrent'),
            'sab_client':  _gcp_h(db, 'nzb'),

            # ── Aggregate rows shown at the bottom of the page ──
            # Previously these lived in a second `with get_db()` block
            # after asyncio.gather returned. That second open was a
            # second exposure to the busy_timeout.
            'stale_series': db.execute("""
                SELECT s.id, s.title, COUNT(v.id) as wanted_count,
                       MAX(s.added_at) as added_at
                FROM series s
                JOIN volumes v ON v.series_id = s.id AND v.status='wanted'
                WHERE s.monitored=1
                  AND NOT EXISTS (
                    SELECT 1 FROM volumes v2
                    WHERE v2.series_id = s.id AND v2.status IN ('grabbed','downloaded')
                  )
                GROUP BY s.id
            """).fetchall(),
            'stale_grabs': db.execute("""
                SELECT v.id, v.series_id, v.volume_num, v.pack_type, v.grabbed_at,
                       v.torrent_name, s.title as series_title
                FROM volumes v JOIN series s ON s.id=v.series_id
                WHERE v.status='grabbed'
                  AND v.grabbed_at < datetime('now', '-2 days')
                ORDER BY v.grabbed_at ASC
                LIMIT 20
            """).fetchall(),
            'stuck_imports': db.execute("""
                SELECT iq.id, iq.series_id, iq.torrent_name, iq.status, iq.created_at,
                       s.title as series_title
                FROM import_queue iq JOIN series s ON s.id=iq.series_id
                WHERE iq.status IN ('pending', 'partial')
                  AND iq.created_at < datetime('now', '-1 hour')
                ORDER BY iq.created_at ASC
                LIMIT 10
            """).fetchall(),
            'recent_errors': db.execute(
                "SELECT * FROM events WHERE event_type='error'"
                " ORDER BY created_at DESC LIMIT 10"
            ).fetchall(),
            'last_backlog': db.execute(
                "SELECT created_at FROM events WHERE event_type='backlog_search'"
                " ORDER BY created_at DESC LIMIT 1"
            ).fetchone(),
            'stats': db.execute("""
                SELECT
                    COUNT(DISTINCT series_id) as series,
                    SUM(CASE WHEN status='wanted'     THEN 1 ELSE 0 END) as wanted,
                    SUM(CASE WHEN status='grabbed'    THEN 1 ELSE 0 END) as grabbed,
                    SUM(CASE WHEN status='downloaded' THEN 1 ELSE 0 END) as downloaded
                FROM volumes
            """).fetchone(),
        }
    return snap


@router.get("/health", response_class=HTMLResponse)
async def health_page(request: Request):
    checks = []

    # ── One-shot DB snapshot used by every check below. ──────────────────
    # This is the "smallest safe fix" half of the change: we used to open
    # ~10 separate get_db() connections (one per check). Each call was
    # exposed to the 5s busy_timeout during background-writer contention,
    # and the waits serialised, cascading into ~30s page stalls. With a
    # single snapshot we take that ceiling to exactly one wait.
    snap = _health_db_snapshot()

    async def check(name: str, coro):
        try:
            result = await coro
            checks.append({
                'name': name,
                'ok': result[0],
                'message': result[1],
                'severity': CHECK_SEVERITY.get(name, 'warning'),
                'fix_url': CHECK_FIX_URL.get(name, ''),
            })
        except Exception as e:
            checks.append({
                'name': name,
                'ok': False,
                'message': str(e),
                'severity': CHECK_SEVERITY.get(name, 'warning'),
                'fix_url': CHECK_FIX_URL.get(name, ''),
            })

    async def _qbit():
        c = snap['qbit_client']
        if not c:
            return True, 'Not configured'
        host = (c.get('host') or '').rstrip('/')
        async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT_HIGH) as client:
            r = await client.post(f"{host}/api/v2/auth/login",
                data={'username': c.get('username') or '', 'password': c.get('password') or ''})
            if 'Ok' not in r.text:
                return False, 'Auth failed'
            r2 = await client.get(f"{host}/api/v2/app/version")
            return True, f"qBittorrent {r2.text.strip()}"

    async def _sab():
        c = snap['sab_client']
        if not c:
            return True, 'Not configured'
        h = (c.get('host') or '').rstrip('/')
        k = c.get('password') or ''
        if not k:
            return True, 'Not configured'
        async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT_HIGH) as client:
            r = await client.get(f"{h}/api",
                                 params={'mode': 'version', 'apikey': k, 'output': 'json'})
            d = r.json()
            return ('version' in d), d.get('version', 'Bad response')

    async def _indexers():
        n = snap['indexers_enabled']
        if n == 0:
            return False, "No indexers configured — nothing will be grabbed"
        return True, f'{n} indexer(s) enabled'

    async def _komga():
        u = get_cfg('komga_url')
        if not u:
            return True, 'Not configured'
        # Komga is a warning-severity optional integration — its slowness
        # must not translate into slow page navigation. 2s is long enough
        # for a healthy response and short enough to surface a degraded
        # provider quickly.
        async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT_WARNING) as client:
            r = await client.get(f"{u}/api/v1/libraries",
                auth=(get_cfg('komga_user'), get_cfg('komga_pass')) if get_cfg('komga_user') else None)
            return (r.status_code == 200,
                    f'{len(r.json())} libraries' if r.status_code == 200 else f'HTTP {r.status_code}')

    async def _root_folders():
        folders = snap['root_folders']
        issues = []
        for f in folders:
            p = f['path']
            if not os.path.exists(p):
                issues.append(f"Root folder missing: {p}")
                continue
            if not os.access(p, os.W_OK):
                issues.append(f"Root folder not writable: {p}")
                continue
            usage = shutil.disk_usage(p)
            free_gb = usage.free / 1_073_741_824
            if free_gb < 10:
                issues.append(f"Low disk space on {p}: {free_gb:.1f} GB free")
        if not folders:
            return True, "No root folders configured"
        if issues:
            return False, "; ".join(issues)
        return True, f"{len(folders)} root folder(s) OK"

    async def _backup_age():
        backup_folder = get_cfg('backup_folder', '/config/backups/')
        if not os.path.exists(backup_folder):
            return True, "No backups yet"
        backups = sorted(
            [f for f in os.listdir(backup_folder) if f.endswith('.zip')], reverse=True
        )
        if not backups:
            return True, "No backups created yet — consider enabling automatic backups"
        latest = os.path.getmtime(os.path.join(backup_folder, backups[0]))
        age_days = (time.time() - latest) / 86400
        if age_days > 7:
            return False, f"Last backup was {age_days:.0f} days ago"
        return True, f"Last backup {age_days:.1f} days ago"

    async def _quality_profiles():
        n = snap['quality_profiles']
        if n == 0:
            return False, "No quality profiles — create one in Settings → Quality Profiles"
        return True, f'{n} quality profile(s) configured'

    async def _api_key():
        key = get_cfg('api_key', '')
        if not key:
            return False, "API key not set — set one in Settings → General"
        return True, 'Configured'

    async def _download_clients():
        n = snap['download_clients_enabled']
        if n == 0:
            return False, "No download clients enabled — add one in Settings → Download Clients"
        return True, f'{n} client(s) enabled'

    async def _series_root_folders():
        orphans = snap['orphan_series_rf']
        if orphans:
            return False, f"{orphans} series reference a deleted root folder — reassign in Series Editor"
        return True, 'All series root folders valid'

    async def _recent_grabs():
        wanted = snap['wanted_volumes']
        if wanted == 0:
            return True, 'No wanted volumes — nothing to grab'
        last_grab = snap['last_grab']
        if not last_grab:
            return False, 'No grabs ever recorded — check indexers and release profiles'
        from datetime import datetime, timezone
        age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(
            last_grab['created_at'].replace('Z', '') + '+00:00'
        )).days
        if age_days > 7:
            return False, f'No grabs in {age_days} days — check indexers, RSS interval, and release profiles'
        return True, f'Last grab {age_days}d ago'

    async def _rss_health():
        last_poll = snap['last_rss_poll']
        if not last_poll:
            return True, 'No RSS poll yet (first run)'
        from datetime import datetime, timezone
        try:
            age_hours = (datetime.now(timezone.utc) - datetime.fromisoformat(
                last_poll['created_at'].replace('Z', '') + '+00:00'
            )).total_seconds() / 3600
        except Exception:
            return True, 'Unable to parse last poll time'
        if age_hours > 2:
            return False, f'RSS not polled in {age_hours:.1f}h — rss_loop may be stuck'
        return True, f'RSS polled {age_hours:.1f}h ago'

    await asyncio.gather(
        check('qBittorrent',         _qbit()),
        check('SABnzbd',             _sab()),
        check('Indexers',            _indexers()),
        check('Download Clients',    _download_clients()),
        check('Komga',               _komga()),
        check('Root Folders',        _root_folders()),
        check('Series Root Folders', _series_root_folders()),
        check('Quality Profiles',    _quality_profiles()),
        check('API Key',             _api_key()),
        check('Backups',             _backup_age()),
        check('Recent Grabs',        _recent_grabs()),
        check('RSS Health',          _rss_health()),
    )

    # Sort: failures first, then by severity (critical → high → warning), then name.
    _sev_order = {'critical': 0, 'high': 1, 'warning': 2}
    checks.sort(key=lambda c: (
        0 if not c['ok'] else 1,
        _sev_order.get(c['severity'], 3),
        c['name'],
    ))

    # Aggregate rows for the bottom of the page come from the same
    # snapshot — second get_db() block removed as part of issue #31
    # follow-up B.
    last_rss_row = snap['last_rss_poll']
    last_backlog_row = snap['last_backlog']
    return templates.TemplateResponse(request, "health.html", {
        "checks":        checks,
        "stale_series":  snap['stale_series'],
        "stale_grabs":   snap['stale_grabs'],
        "stuck_imports": snap['stuck_imports'],
        "recent_errors": snap['recent_errors'],
        "last_rss":      last_rss_row['created_at'] if last_rss_row else None,
        "last_backlog":  last_backlog_row['created_at'] if last_backlog_row else None,
        "stats":         snap['stats'],
    })


@router.post("/api/backfill-packs")
async def trigger_backfill():
    """Retroactively parse ranges from existing pack names."""
    import main as _m
    marked = _m.backfill_pack_ranges()
    _m.log_event('refresh', f"Pack backfill: {marked} volume stubs updated")
    return JSONResponse({"ok": True, "message": f"{marked} volume stubs updated"})


@router.post("/api/check-downloads")
async def trigger_status_check():
    """Manually trigger a download status check."""
    import main as _m
    asyncio.create_task(_m.check_download_status())
    return JSONResponse({"ok": True, "message": "Status check queued"})


@router.post("/api/backlog-search")
async def trigger_backlog_search():
    """Manually trigger a backlog search for all wanted volumes."""
    import main as _m

    async def _run():
        with get_db() as db:
            wanted_series = db.execute(
                "SELECT DISTINCT s.id, s.title, s.search_pattern FROM series s"
                " JOIN volumes v ON v.series_id=s.id"
                " WHERE s.monitored=1 AND v.status='wanted'"
            ).fetchall()
        grabbed = 0
        for s in wanted_series:
            try:
                grabbed += await _m.grab_existing(s['id'], s['title'], s['search_pattern'])
            except Exception as e:
                import traceback
                print(f"[Backlog Manual] Error searching {s['title']}: {e}")
                print(traceback.format_exc())
            await asyncio.sleep(1)
        _m.log_event('backlog_search',
                     f"Manual backlog search: {len(wanted_series)} series, {grabbed} grabbed")

    asyncio.create_task(_run())
    return JSONResponse({"ok": True, "message": "Backlog search started in background"})


@router.get("/api/naming-preview")
async def naming_preview(fmt: str = ""):
    """Return a rendered example of a file naming format string."""
    import main as _m
    example_series = "One Piece"
    example_volume = 42.0
    example_file   = "release.cbz"
    if fmt:
        old = _m.CONFIG.get('file_format', '')
        _m.CONFIG['file_format'] = fmt
        result = _m.build_filename(example_series, example_volume, example_file)
        _m.CONFIG['file_format'] = old
    else:
        result = _m.build_filename(example_series, example_volume, example_file)
    return JSONResponse({"preview": result, "format": fmt or get_cfg('file_format', '')})
