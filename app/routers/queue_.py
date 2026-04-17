"""Queue page — grabbed items, download client status, pending releases."""
import asyncio
from collections import defaultdict as _dd

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from routers._templates import templates
from shared import (
    cascade_chapters, get_cfg, get_db, get_root_folders,
    build_volume_label, vol_num_to_display, is_htmx,
)

router = APIRouter()

# Upper bound on how long ONE upstream HTTP call is allowed to wait.
# Healthy LAN clients respond in <200ms; 2.5s is the per-call ceiling.
QUEUE_UPSTREAM_TIMEOUT_SECONDS = 2.5

# Stricter upper bound on how long the queue PAGE render will wait on
# upstream status fetches before rendering without them. The page is
# built primarily from the DB; live qBit/SAB data is an enrichment
# (progress %, speed, ETA) that can be missing without breaking UX.
# Bounding the render path to 0.8s keeps navigation fast even when a
# single upstream takes ~2s to answer. Subsequent page loads will pick
# up live data once the upstream responds within budget.
QUEUE_RENDER_UPSTREAM_BUDGET = 0.8


async def _fetch_qbit_status(qc: dict) -> dict:
    """Poll qBittorrent for current torrent state.

    Returns {hash: info_dict} or {} on any failure (timeout, auth fail,
    non-200, unreachable). Never raises — the queue page must render even
    when qBit is down.
    """
    if not qc:
        return {}
    host = (qc.get('host') or '').rstrip('/')
    user = qc.get('username') or ''
    pw   = qc.get('password') or ''
    cat  = qc.get('category') or get_cfg('category')
    try:
        async with httpx.AsyncClient(timeout=QUEUE_UPSTREAM_TIMEOUT_SECONDS) as client:
            r = await client.post(
                f"{host}/api/v2/auth/login",
                data={'username': user, 'password': pw},
            )
            if 'Ok' not in r.text:
                return {}
            r2 = await client.get(
                f"{host}/api/v2/torrents/info",
                params={'category': cat},
            )
            if r2.status_code != 200:
                return {}
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
    except Exception:
        return {}


async def _fetch_sab_status(sc: dict) -> dict:
    """Poll SABnzbd for current queue state.

    Returns {nzo_id: info_dict} or {} on any failure. Never raises.
    """
    if not sc:
        return {}
    host   = (sc.get('host') or '').rstrip('/')
    apikey = sc.get('password') or ''
    if not apikey:
        return {}
    try:
        async with httpx.AsyncClient(timeout=QUEUE_UPSTREAM_TIMEOUT_SECONDS) as client:
            r = await client.get(
                f"{host}/api",
                params={'mode': 'queue', 'apikey': apikey, 'output': 'json'},
            )
            if r.status_code != 200:
                return {}
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
    except Exception:
        return {}


async def _build_queue_rows() -> tuple[list, list]:
    """Build (queue_rows, disk_info) for the queue page and queue/table partial."""
    import shutil as _shutil
    from routers.download_clients import get_client_for_protocol as _gcp_q

    # ── Download client data ──────────────────────────────────────────────
    # qBit and SAB status are independent — run concurrently so a slow or
    # unreachable client can't cascade into the other one's timeout. Both
    # helpers swallow their own exceptions and return {} on failure.
    #
    # Additionally bound the total wait with asyncio.wait_for: the page
    # renders all queued items from the DB regardless of upstream status,
    # so once the wait exceeds a page-render budget we'd rather show the
    # rows without live progress bars than keep the user waiting. Live
    # status then reappears on the next page load when the upstreams are
    # responsive again. (Issue #31 — "render from DB / last-known status
    # immediately; degrade gracefully when live status is unavailable.")
    with get_db() as _q_dc_db:
        _q_qc = _gcp_q(_q_dc_db, 'torrent')
        _q_sc = _gcp_q(_q_dc_db, 'nzb')

    try:
        torrent_by_hash, sab_by_id = await asyncio.wait_for(
            asyncio.gather(
                _fetch_qbit_status(_q_qc),
                _fetch_sab_status(_q_sc),
            ),
            timeout=QUEUE_RENDER_UPSTREAM_BUDGET,
        )
    except asyncio.TimeoutError:
        # One or both upstreams missed the budget. Render without their
        # live data; the rest of the page still shows every queued item
        # straight from the DB.
        torrent_by_hash, sab_by_id = {}, {}

    all_client_items = {**torrent_by_hash, **sab_by_id}

    def _client_stage(state: str) -> str:
        sl = (state or '').lower()
        if 'stalled' in sl and 'up' not in sl: return 'stalled'
        if 'error' in sl or 'missing' in sl:   return 'error'
        if 'paused' in sl:                      return 'paused'
        if 'queued' in sl or 'checking' in sl:  return 'queued_dl'
        if 'upload' in sl or ('stalled' in sl and 'up' in sl): return 'completed'
        return 'downloading'

    with get_db() as db:
        seen_meta: dict = {}
        for _sm in db.execute(
            "SELECT download_id, protocol, indexer, size_bytes FROM seen WHERE download_id IS NOT NULL"
        ).fetchall():
            did = (_sm['download_id'] or '').lower()
            if did and did not in seen_meta:
                seen_meta[did] = {
                    'protocol':   _sm['protocol'] or '',
                    'indexer':    _sm['indexer'] or '',
                    'size_bytes': _sm['size_bytes'] or 0,
                }

        pending_raw = db.execute(
            "SELECT iq.*, s.title as series_title "
            "FROM import_queue iq JOIN series s ON s.id=iq.series_id "
            "WHERE iq.status IN ('pending','partial') ORDER BY iq.created_at DESC"
        ).fetchall()
        pending_by_dlid: dict = {}
        for q in pending_raw:
            dl_id = (q['download_id'] or '').lower()
            files = db.execute(
                "SELECT * FROM import_queue_files WHERE queue_id=? ORDER BY filename",
                (q['id'],)
            ).fetchall()
            needs_review = (
                q['status'] == 'partial' or
                any(f['status'] in ('needs_review', 'pending') and f['proposed_volume'] is None
                    for f in files)
            )
            pending_by_dlid[dl_id] = {
                'queue_id':     q['id'],
                'series_id':    q['series_id'],
                'series_title': q['series_title'],
                'torrent_name': q['torrent_name'],
                'grabbed_at':   q['created_at'],
                'src_dir':      q['src_dir'],
                'needs_review': needs_review,
                'files':        files,
            }

        grabbed_raw = db.execute(
            "SELECT v.id, v.series_id, v.volume_num, v.pack_type,"
            " v.vol_range_start, v.vol_range_end, v.grabbed_at,"
            " v.download_id, v.torrent_name, v.client as grabbed_client,"
            " s.title as series_title "
            "FROM volumes v JOIN series s ON s.id=v.series_id "
            "WHERE v.status='grabbed' "
            "ORDER BY v.grabbed_at DESC"
        ).fetchall()

        by_dlid: dict = _dd(list)
        for v in grabbed_raw:
            by_dlid[(v['download_id'] or '').lower()].append(v)

        queue_rows = []
        seen_dlids: set = set()

        for dl_id, vols in by_dlid.items():
            seen_dlids.add(dl_id)
            v0  = vols[0]
            sm  = seen_meta.get(dl_id, {})

            if len(vols) == 1:
                vol_label = build_volume_label(
                    v0['volume_num'],
                    (v0['vol_range_start'], v0['vol_range_end']) if v0['vol_range_start'] else None,
                    v0['pack_type'] if v0['volume_num'] is None else None,
                )
            else:
                nums = sorted(v['volume_num'] for v in vols if v['volume_num'] is not None)
                vol_label = (f"Vol {vol_num_to_display(nums[0])}–{vol_num_to_display(nums[-1])}"
                             if nums else "Pack")

            base = {
                'series_id':     v0['series_id'],
                'series_title':  v0['series_title'],
                'vol_label':     vol_label,
                'torrent_name':  v0['torrent_name'] or '',
                'grabbed_at':    v0['grabbed_at'],
                'hash':          dl_id,
                'client':        v0['grabbed_client'] or 'qbittorrent',
                'protocol':      sm.get('protocol', ''),
                'indexer':       sm.get('indexer', ''),
                'size_bytes':    sm.get('size_bytes', 0),
                'queue_id':      None,
                'src_dir':       None,
                'files':         [],
                'pending_id':    None,
                'error_message': '',
            }

            if dl_id in pending_by_dlid:
                pq    = pending_by_dlid[dl_id]
                live  = all_client_items.get(dl_id, {})
                stage = 'review' if pq['needs_review'] else 'importing'
                queue_rows.append({**base,
                    'stage':    stage,
                    'progress': live.get('progress', 100),
                    'dlspeed':  0,
                    'eta':      -1,
                    'queue_id': pq['queue_id'],
                    'src_dir':  pq['src_dir'],
                    'files':    pq['files'],
                })
            elif dl_id in all_client_items:
                live  = all_client_items[dl_id]
                stage = _client_stage(live.get('state', ''))
                queue_rows.append({**base,
                    'stage':         stage,
                    'torrent_name':  v0['torrent_name'] or live.get('name', ''),
                    'progress':      live.get('progress', 0),
                    'dlspeed':       live.get('dlspeed', 0),
                    'eta':           live.get('eta', -1),
                    'client':        v0['grabbed_client'] or live.get('client', 'qbittorrent'),
                    'error_message': live.get('error_message', ''),
                })
            else:
                queue_rows.append({**base,
                    'stage':    'warning',
                    'progress': 0,
                    'dlspeed':  0,
                    'eta':      -1,
                })

        for dl_id, pq in pending_by_dlid.items():
            if dl_id in seen_dlids:
                continue
            live  = all_client_items.get(dl_id, {})
            sm    = seen_meta.get(dl_id, {})
            stage = 'review' if pq['needs_review'] else 'importing'
            queue_rows.append({
                'stage':         stage,
                'series_id':     pq['series_id'],
                'series_title':  pq['series_title'],
                'vol_label':     '',
                'torrent_name':  pq['torrent_name'] or '',
                'grabbed_at':    pq['grabbed_at'],
                'progress':      live.get('progress', 100),
                'dlspeed':       0,
                'eta':           -1,
                'hash':          dl_id,
                'client':        'qbittorrent',
                'queue_id':      pq['queue_id'],
                'src_dir':       pq['src_dir'],
                'files':         pq['files'],
                'pending_id':    None,
                'protocol':      sm.get('protocol', ''),
                'indexer':       sm.get('indexer', ''),
                'size_bytes':    sm.get('size_bytes', 0),
                'error_message': '',
            })

        for pr in db.execute(
            "SELECT pr.id, pr.series_id, pr.url, pr.title, pr.indexer, pr.protocol,"
            " pr.size_bytes, pr.first_seen, s.title as series_title "
            "FROM pending_releases pr LEFT JOIN series s ON s.id=pr.series_id "
            "ORDER BY pr.first_seen DESC"
        ).fetchall():
            queue_rows.append({
                'stage':         'pending',
                'series_id':     pr['series_id'],
                'series_title':  pr['series_title'] or '—',
                'vol_label':     '',
                'torrent_name':  pr['title'],
                'grabbed_at':    pr['first_seen'],
                'progress':      0,
                'dlspeed':       0,
                'eta':           -1,
                'hash':          None,
                'client':        pr['protocol'] or 'torrent',
                'queue_id':      None,
                'src_dir':       None,
                'files':         [],
                'pending_id':    pr['id'],
                'protocol':      pr['protocol'] or '',
                'indexer':       pr['indexer'] or '',
                'size_bytes':    pr['size_bytes'] or 0,
                'error_message': '',
            })

        if any(r['stage'] in ('completed', 'importing') for r in queue_rows):
            try:
                import main as _m
                asyncio.create_task(_m.check_download_status())
            except Exception:
                pass

        queue_rows = [r for r in queue_rows if r['stage'] not in ('completed', 'importing')]

        _stage_pri = {
            'review':      0,
            'error':       1,
            'warning':     2,
            'stalled':     3,
            'downloading': 4,
            'queued_dl':   5,
            'paused':      6,
            'pending':     7,
        }
        queue_rows.sort(key=lambda r: (_stage_pri.get(r['stage'], 5), r['grabbed_at'] or ''))

        disk_info = []
        for rf in get_root_folders(db):
            try:
                usage = _shutil.disk_usage(rf['path'])
                disk_info.append({
                    'path':  rf['path'],
                    'label': rf['label'] or rf['path'],
                    'total': usage.total,
                    'used':  usage.used,
                    'free':  usage.free,
                    'pct':   round(usage.used / usage.total * 100, 1) if usage.total else 0,
                })
            except Exception:
                pass

    # Configured download client category (for the Change Category modal)
    configured_category = ''
    with get_db() as _cat_db:
        from routers.download_clients import get_client_for_protocol as _gcp_cat
        _qb_cat_c = _gcp_cat(_cat_db, 'torrent')
        if _qb_cat_c:
            configured_category = _qb_cat_c.get('category') or get_cfg('category')

    # ── Suwayomi downloads ────────────────────────────────────────────────────
    suwayomi_rows = []
    with get_db() as _swy_db:
        _swy_jobs = _swy_db.execute(
            "SELECT sd.*, s.title as series_title"
            " FROM suwayomi_downloads sd"
            " JOIN series s ON s.id=sd.series_id"
            " WHERE sd.status IN ('queued','error')"
            " ORDER BY sd.created_at DESC"
        ).fetchall()
    for job in _swy_jobs:
        vol_num = job['volume_num']
        ch_num  = job['chapter_num']
        if vol_num is not None:
            vol_label = f"Vol {vol_num:g}"
        elif ch_num is not None:
            cn = int(ch_num) if ch_num == int(ch_num) else ch_num
            vol_label = f"Ch {cn}"
        else:
            vol_label = "—"
        total = job['total'] or 1
        pct   = round(job['progress'] / total * 100, 1)
        suwayomi_rows.append({
            'job_id':       job['id'],
            'series_id':    job['series_id'],
            'series_title': job['series_title'],
            'vol_label':    vol_label,
            'progress':     pct,
            'done':         job['progress'],
            'total':        total,
            'status':       job['status'],
            'created_at':   job['created_at'],
            'error':        job['error'] or '',
        })

    return queue_rows, disk_info, configured_category, suwayomi_rows


async def _queue_partial_response(request: Request):
    """Return queue table partial for HTMX, or redirect to /queue for normal requests."""
    if is_htmx(request):
        queue_rows, _, configured_category, suwayomi_rows = await _build_queue_rows()
        return templates.TemplateResponse(request, "partials/queue_table.html",
                                          {"queue_rows": queue_rows,
                                           "suwayomi_rows": suwayomi_rows,
                                           "configured_category": configured_category})
    return RedirectResponse("/queue", status_code=303)


@router.get("/queue/table", response_class=HTMLResponse)
async def queue_table_partial(request: Request):
    """HTMX partial: queue table + modals, polled every 8 s from the queue page."""
    queue_rows, _, configured_category, suwayomi_rows = await _build_queue_rows()
    return templates.TemplateResponse(request, "partials/queue_table.html", {
        "queue_rows":          queue_rows,
        "suwayomi_rows":       suwayomi_rows,
        "configured_category": configured_category,
    })


@router.get("/queue", response_class=HTMLResponse)
async def queue_page(request: Request):
    queue_rows, disk_info, configured_category, suwayomi_rows = await _build_queue_rows()
    return templates.TemplateResponse(request, "queue.html", {
        "queue_rows":          queue_rows,
        "disk_info":           disk_info,
        "suwayomi_rows":       suwayomi_rows,
        "configured_category": configured_category,
    })


@router.post("/queue/grabbed/{dl_hash}/reset-all")
async def reset_orphaned_by_hash(request: Request, dl_hash: str):
    """Reset all grabbed volumes sharing a download_id back to wanted (for 'missing' queue items)."""
    h = dl_hash.lower()
    with get_db() as db:
        rows = db.execute(
            "SELECT id, source_url, series_id FROM volumes WHERE download_id=? AND status='grabbed'",
            (h,)
        ).fetchall()
        for row in rows:
            if row['source_url']:
                db.execute("DELETE FROM seen WHERE torrent_url=?", (row['source_url'],))
        db.execute(
            "UPDATE volumes SET status='wanted', download_id=NULL, grabbed_at=NULL,"
            " source_url=NULL, torrent_name=NULL, indexer=NULL, protocol=NULL,"
            " client=NULL, release_group=NULL "
            "WHERE download_id=? AND status='grabbed'", (h,)
        )
        db.execute("DELETE FROM volumes WHERE download_id=? AND volume_num IS NULL", (h,))
        db.execute("DELETE FROM seen WHERE download_id=?", (h,))
    return await _queue_partial_response(request)


@router.post("/queue/grabbed/{vol_id}/reset")
async def reset_orphaned_volume(request: Request, vol_id: int):
    """Reset an orphaned grabbed volume back to wanted so it can be re-grabbed."""
    with get_db() as db:
        row = db.execute(
            "SELECT source_url, download_id, series_id FROM volumes WHERE id=? AND status='grabbed'",
            (vol_id,)
        ).fetchone()
        if row:
            if row['source_url']:
                db.execute("DELETE FROM seen WHERE torrent_url=?", (row['source_url'],))
            if row['download_id']:
                others = db.execute(
                    "SELECT COUNT(*) FROM volumes WHERE download_id=? AND status='grabbed' AND id != ?",
                    (row['download_id'], vol_id)
                ).fetchone()[0]
                if others == 0:
                    db.execute("DELETE FROM seen WHERE download_id=?", (row['download_id'],))
        db.execute(
            "UPDATE volumes SET status='wanted', download_id=NULL, grabbed_at=NULL,"
            " source_url=NULL, torrent_name=NULL, indexer=NULL, protocol=NULL,"
            " client=NULL, release_group=NULL WHERE id=? AND status='grabbed'",
            (vol_id,)
        )
        if row:
            cascade_chapters(db, row['series_id'], [vol_id], 'wanted',
                             grabbed_at=None, torrent_name=None, torrent_url=None,
                             indexer=None, protocol=None, client=None,
                             download_id=None, release_group=None)
    return await _queue_partial_response(request)


@router.post("/queue/download/{dl_hash}/reset")
async def reset_download_by_hash(request: Request, dl_hash: str):
    """Reset all grabbed volumes for a download_id back to wanted (for missing/orphaned items)."""
    with get_db() as db:
        grabbed = db.execute(
            "SELECT id, series_id, source_url FROM volumes "
            "WHERE download_id=? AND status='grabbed'",
            (dl_hash,)
        ).fetchall()
        if grabbed:
            seen_urls = set()
            for row in grabbed:
                if row['source_url'] and row['source_url'] not in seen_urls:
                    db.execute("DELETE FROM seen WHERE torrent_url=?", (row['source_url'],))
                    seen_urls.add(row['source_url'])
            db.execute("DELETE FROM seen WHERE download_id=?", (dl_hash,))
            db.execute(
                "UPDATE volumes SET status='wanted', download_id=NULL, grabbed_at=NULL,"
                " source_url=NULL, torrent_name=NULL, indexer=NULL, protocol=NULL,"
                " client=NULL, release_group=NULL WHERE download_id=? AND status='grabbed'",
                (dl_hash,)
            )
            by_series: dict = {}
            for row in grabbed:
                by_series.setdefault(row['series_id'], []).append(row['id'])
            for sid, vol_ids in by_series.items():
                cascade_chapters(db, sid, vol_ids, 'wanted',
                                 grabbed_at=None, torrent_name=None, torrent_url=None,
                                 indexer=None, protocol=None, client=None,
                                 download_id=None, release_group=None)
    return await _queue_partial_response(request)


@router.post("/queue/torrent/{torrent_hash}/remove")
async def remove_from_queue(
    request: Request,
    torrent_hash: str,
    remove_from_client: str = Form("1"),
    delete_files: str = Form("0"),
    blocklist: str = Form("0"),
    change_category: str = Form(""),
):
    """Remove a torrent from the queue.

    Params:
      remove_from_client — "1" to delete from download client, "0" to keep (Mangarr tracking only)
      delete_files       — "1" to also delete downloaded files (only when remove_from_client=1)
      blocklist          — "1" to add to blocklist so the release won't be re-grabbed
      change_category    — optional: change qBit category before untracking (only when keeping in client)
    """
    h = torrent_hash.lower()

    with get_db() as db:
        seen_row = db.execute(
            "SELECT series_id, torrent_name, torrent_url, indexer, protocol, size_bytes"
            " FROM seen WHERE download_id=?", (h,)
        ).fetchone()

        # Blocklist the release if requested
        if blocklist == "1" and seen_row:
            db.execute(
                "INSERT OR IGNORE INTO blocklist"
                "(series_id, torrent_url, torrent_name, reason, indexer, protocol)"
                " VALUES(?,?,?,?,?,?)",
                (seen_row['series_id'],
                 seen_row['torrent_url'] or '',
                 seen_row['torrent_name'] or '',
                 'Manually removed from queue',
                 seen_row['indexer'] or '',
                 seen_row['protocol'] or '')
            )

        # Reset grabbed volumes back to wanted
        grabbed = db.execute(
            "SELECT id, series_id FROM volumes WHERE download_id=? AND status='grabbed'"
            " AND volume_num IS NOT NULL", (h,)
        ).fetchall()
        by_series: dict = {}
        for v in grabbed:
            by_series.setdefault(v['series_id'], []).append(v['id'])
        db.execute(
            "DELETE FROM volumes WHERE download_id=? AND status='grabbed' AND volume_num IS NULL", (h,)
        )
        db.execute(
            "UPDATE volumes SET status='wanted', download_id=NULL, grabbed_at=NULL,"
            " source_url=NULL, torrent_name=NULL, indexer=NULL, protocol=NULL,"
            " client=NULL, release_group=NULL "
            "WHERE download_id=? AND status='grabbed'", (h,)
        )
        for sid, vol_ids in by_series.items():
            cascade_chapters(db, sid, vol_ids, 'wanted',
                             grabbed_at=None, torrent_name=None, torrent_url=None,
                             indexer=None, protocol=None, client=None,
                             download_id=None, release_group=None)
        db.execute(
            "UPDATE import_queue SET status='skipped' "
            "WHERE download_id=? AND status='pending'", (h,)
        )
        db.execute(
            "UPDATE import_queue_files SET status='skipped' "
            "WHERE queue_id IN (SELECT id FROM import_queue WHERE download_id=?)", (h,)
        )
        db.execute("DELETE FROM seen WHERE download_id=?", (h,))
        if seen_row:
            import main as _m
            action = "Removed" if remove_from_client == "1" else "Untracked"
            bl_note = " (blocklisted)" if blocklist == "1" else ""
            _m.log_event('warning',
                f"{action} from queue{bl_note}: {seen_row['torrent_name']}",
                seen_row['series_id'])

    # Optional: change category in download client before untracking
    cat_new = change_category.strip()
    if cat_new and remove_from_client != "1":
        from routers.download_clients import get_client_for_protocol as _gcp_cc
        with get_db() as _cc_db:
            _cc_c = _gcp_cc(_cc_db, 'torrent')
        if _cc_c:
            _cc_host = (_cc_c.get('host') or '').rstrip('/')
            _cc_user = _cc_c.get('username') or ''
            _cc_pw   = _cc_c.get('password') or ''
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    r = await client.post(f"{_cc_host}/api/v2/auth/login",
                                          data={'username': _cc_user, 'password': _cc_pw})
                    if 'Ok' in r.text:
                        await client.post(f"{_cc_host}/api/v2/torrents/createCategory",
                                          data={'category': cat_new, 'savePath': ''})
                        await client.post(f"{_cc_host}/api/v2/torrents/setCategory",
                                          data={'hashes': torrent_hash, 'category': cat_new})
            except Exception:
                pass

    # Remove from download client (optional)
    if remove_from_client == "1":
        from routers.download_clients import get_client_for_protocol as _gcp_rq
        with get_db() as _rq_db:
            _rq_c = _gcp_rq(_rq_db, 'torrent')
        if _rq_c:
            host = (_rq_c.get('host') or '').rstrip('/')
            user = _rq_c.get('username') or ''
            pw   = _rq_c.get('password') or ''
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(f"{host}/api/v2/auth/login",
                                      data={'username': user, 'password': pw})
                    await client.post(f"{host}/api/v2/torrents/delete",
                                      data={'hashes': torrent_hash,
                                            'deleteFiles': delete_files})
            except Exception:
                pass

    return await _queue_partial_response(request)


@router.post("/queue/torrent/{torrent_hash}/block-remove")
async def block_and_remove(request: Request, torrent_hash: str, delete_files: str = Form("1")):
    """Blacklist the release, remove from client, reset volume to wanted, trigger re-search."""
    h = torrent_hash.lower()
    with get_db() as db:
        seen_row = db.execute(
            "SELECT series_id, torrent_name, torrent_url, indexer, protocol, size_bytes"
            " FROM seen WHERE download_id=?", (h,)
        ).fetchone()
        if seen_row:
            db.execute(
                "INSERT OR IGNORE INTO blocklist(series_id, torrent_url, torrent_name, reason, indexer, protocol)"
                " VALUES(?,?,?,?,?,?)",
                (seen_row['series_id'], seen_row['torrent_url'] or '', seen_row['torrent_name'] or '',
                 'Manually blocked from queue', seen_row['indexer'] or '', seen_row['protocol'] or '')
            )
        db.execute(
            "UPDATE volumes SET status='wanted', download_id=NULL, grabbed_at=NULL,"
            " source_url=NULL, torrent_name=NULL, indexer=NULL, protocol=NULL,"
            " client=NULL, release_group=NULL "
            "WHERE download_id=? AND status='grabbed'", (h,)
        )
        db.execute("DELETE FROM volumes WHERE download_id=? AND volume_num IS NULL", (h,))
        db.execute("DELETE FROM seen WHERE download_id=?", (h,))
    import main as _m
    await _m.qbit_remove(h, delete_files=delete_files == "1")
    if seen_row:
        with get_db() as db:
            s = db.execute("SELECT title, search_pattern FROM series WHERE id=?",
                           (seen_row['series_id'],)).fetchone()
        if s:
            asyncio.create_task(_m.grab_existing(seen_row['series_id'], s['title'],
                                                  s['search_pattern']))
    return await _queue_partial_response(request)


@router.post("/queue/torrent/{torrent_hash}/set-category")
async def set_torrent_category(request: Request, torrent_hash: str,
                               category: str = Form(...)):
    """Change the qBittorrent category for an active torrent.

    Useful to move a torrent from a pre-import category to the import category,
    or to correct a mis-categorised grab.
    """
    from routers.download_clients import get_client_for_protocol as _gcp_sc
    with get_db() as _sc_db:
        _sc_c = _gcp_sc(_sc_db, 'torrent')
    if _sc_c:
        host = (_sc_c.get('host') or '').rstrip('/')
        user = _sc_c.get('username') or ''
        pw   = _sc_c.get('password') or ''
        cat  = category.strip()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(f"{host}/api/v2/auth/login",
                                      data={'username': user, 'password': pw})
                if 'Ok' in r.text:
                    # Ensure the category exists in qBittorrent first
                    await client.post(f"{host}/api/v2/torrents/createCategory",
                                      data={'category': cat, 'savePath': ''})
                    # Set the category on the torrent
                    await client.post(f"{host}/api/v2/torrents/setCategory",
                                      data={'hashes': torrent_hash, 'category': cat})
        except Exception:
            pass
    return await _queue_partial_response(request)


@router.post("/queue/pending/{pending_id}/force-grab")
async def force_grab_pending(request: Request, pending_id: int):
    """Immediately grab a pending release, bypassing its delay profile."""
    with get_db() as db:
        row = db.execute(
            "SELECT id, series_id, url, title, indexer, protocol, size_bytes"
            " FROM pending_releases WHERE id=?", (pending_id,)
        ).fetchone()
        if not row:
            return await _queue_partial_response(request)
        item = {
            'url':        row['url'],
            'title':      row['title'],
            'indexer':    row['indexer'] or '',
            'protocol':   row['protocol'] or 'torrent',
            'size_bytes': row['size_bytes'] or 0,
        }
        db.execute("DELETE FROM pending_releases WHERE id=?", (pending_id,))
    import main as _m
    await _m.grab_item(item, row['series_id'])
    return await _queue_partial_response(request)


@router.post("/queue/pending/{pending_id}/dismiss")
async def dismiss_pending(request: Request, pending_id: int):
    """Remove a pending release from the delay queue without grabbing it."""
    with get_db() as db:
        db.execute("DELETE FROM pending_releases WHERE id=?", (pending_id,))
    return await _queue_partial_response(request)
