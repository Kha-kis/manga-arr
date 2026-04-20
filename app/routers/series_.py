"""Series library — index, detail, add, edit, volume/chapter actions."""
import asyncio
import json
import math
import os
import re
from collections import defaultdict
from datetime import datetime

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from routers._templates import templates
from shared import (
    build_order_by, cascade_chapters, get_cfg, get_db, get_root_folders,
    quality_rank, vol_num_to_display, with_flash,
)

router = APIRouter()

# Library-index sort allowlist. Any value not in this dict falls back to
# the default ("title"). Fragments include direction where a non-ASC
# default matters (e.g. "added" shows newest first).
_LIBRARY_SORT_ALLOWED = {
    "title":  "title",
    "status": "status, title",
    "added":  "added_at DESC",
}
_LIBRARY_SORT_DEFAULT = "title"


# ── Private helpers ───────────────────────────────────────────────────────────

def _chapter_map_to_ranges(chapter_vol_map_json: str | None) -> str:
    """Convert {ch_str: vol_int} JSON to human-readable 'one range per line' format."""
    if not chapter_vol_map_json:
        return ''
    try:
        cvm = json.loads(chapter_vol_map_json)
    except Exception:
        return ''
    vol_to_chs: dict[int, list[int]] = defaultdict(list)
    for ch_str, vol_num in cvm.items():
        try:
            vol_to_chs[int(vol_num)].append(int(float(ch_str)))
        except (ValueError, TypeError):
            pass
    lines = []
    for vol_num in sorted(vol_to_chs.keys()):
        chs = sorted(vol_to_chs[vol_num])
        if not chs:
            continue
        if len(chs) == 1:
            lines.append(str(chs[0]))
        elif chs[-1] - chs[0] + 1 == len(chs):
            lines.append(f"{chs[0]}-{chs[-1]}")
        else:
            lines.append(', '.join(str(c) for c in chs))
    return '\n'.join(lines)


def _parse_chapter_ranges(text: str) -> dict[str, int] | None:
    """Parse 'one range per line' chapter map into {ch_str: vol_int}."""
    mapping: dict[str, int] = {}
    vol_num = 0
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        vol_num += 1
        for part in line.split(','):
            part = part.strip()
            if not part:
                continue
            if '-' in part:
                halves = part.split('-', 1)
                try:
                    start, end = int(halves[0].strip()), int(halves[1].strip())
                    if start > end or end - start > 500:
                        return None
                    for ch in range(start, end + 1):
                        mapping[str(ch)] = vol_num
                except ValueError:
                    return None
            else:
                try:
                    mapping[str(int(part))] = vol_num
                except ValueError:
                    return None
    return mapping if mapping else None


async def _rescan_all_impl():
    """Core logic for full library rescan — shared by route and periodic loop."""
    import main as _m
    with get_db() as db:
        series_ids = [r['id'] for r in db.execute("SELECT id FROM series").fetchall()]
        total = {'found': 0, 'recovered': 0, 'missing': 0, 'lost': 0, 'created': 0}
        for sid in series_ids:
            r = _m.rescan_series_folder(db, sid)
            total['found']     += r['found']
            total['recovered'] += r['recovered']
            total['missing']   += r['missing']
            total['lost']      += r['lost']
            total['created']   += r.get('created', 0)
    _m.log_event('rescan',
        f"Full library rescan: {total['found']} files, "
        f"{total['recovered']} recovered, {total['missing']} missing, "
        f"{total['lost']} grabs lost, {total['created']} stubs created")


def _build_swy_vol_jobs(db, series_id: int) -> dict:
    """Return {volume_num: {progress, total, status, error}} for active Suwayomi jobs."""
    rows = db.execute(
        "SELECT volume_num, progress, total, status, error"
        " FROM suwayomi_downloads"
        " WHERE series_id=? AND volume_num IS NOT NULL AND status IN ('queued','error')",
        (series_id,),
    ).fetchall()
    return {float(r["volume_num"]): dict(r) for r in rows}


async def _get_volume_row_ctx(series_id: int, volume_id: int) -> dict:
    """Build template context for a single volume row partial (HTMX partial responses)."""
    with get_db() as db:
        s    = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
        v    = db.execute("SELECT * FROM volumes WHERE id=? AND series_id=?",
                          (volume_id, series_id)).fetchone()
        vchs = db.execute(
            "SELECT * FROM chapters WHERE volume_id=? AND series_id=? ORDER BY chapter_num",
            (volume_id, series_id)
        ).fetchall()
        _iq  = db.execute(
            "SELECT download_id, status FROM import_queue WHERE series_id=?"
            " AND status IN ('pending','partial')", (series_id,)
        ).fetchall()
        swy_vol_jobs = _build_swy_vol_jobs(db, series_id)
    pending_dl_ids = {(r['download_id'] or '').lower() for r in _iq
                      if r['download_id'] and r['status'] == 'pending'}
    review_dl_ids  = {(r['download_id'] or '').lower() for r in _iq
                      if r['download_id'] and r['status'] == 'partial'}
    vct = {
        'total':      len(vchs),
        'downloaded': sum(1 for c in vchs if c['status'] == 'downloaded'),
        'grabbed':    sum(1 for c in vchs if c['status'] == 'grabbed'),
        'wanted':     sum(1 for c in vchs if c['status'] == 'wanted' and c['monitored']),
    }
    effective_cutoff = (s['quality_cutoff'] or '').strip() if s else ''
    effective_cutoff = effective_cutoff or get_cfg('quality_cutoff', '')
    return {
        "s": s, "v": v,
        "vchs": list(vchs), "vct": vct,
        "quality_cutoff":  effective_cutoff,
        "cutoff_rank":     quality_rank(effective_cutoff),
        "pending_dl_ids":  pending_dl_ids,
        "review_dl_ids":   review_dl_ids,
        "active_dl_ids":   set(),
        "dl_stages":       {},
        "swy_vol_jobs":    swy_vol_jobs,
    }


async def _grab_volume_task(series_id: int, s, v, query: str):
    import main as _m
    specific = await _m._search_all(query)
    general  = await _m._search_all(s['title']) if query != s['title'] else []
    seen_urls_all = {i['url'] for i in specific}
    all_items = list(specific) + [i for i in general if i['url'] not in seen_urls_all]
    all_items.sort(key=lambda x: x.get('_score', 0), reverse=True)

    with get_db() as db:
        seen_urls    = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM seen").fetchall()}
        blocked_urls = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM blocklist").fetchall()}
        alias_rows   = db.execute(
            "SELECT alias FROM series_aliases WHERE series_id=?", (series_id,)
        ).fetchall()
    all_patterns = list({s['search_pattern'], s['title']} | {a['alias'] for a in alias_rows})
    target_vol = v['volume_num'] if v else None
    for item in all_items:
        if item['url'] in seen_urls or item['url'] in blocked_urls:
            continue
        if any(_m.matches(p, item['title']) for p in all_patterns):
            item_vol = _m.extract_volume_num(item['title'])
            item_rng = _m.extract_volume_range(item['title'])
            if item_rng is not None:
                item_vol = None
            vol_ok = (
                target_vol is None
                or item_vol is None
                or abs(item_vol - target_vol) < 0.01
                or (item_rng and item_rng[0] <= target_vol <= item_rng[1])
                or _m.is_complete_pack(item['title'])
            )
            if vol_ok:
                await _m.grab_item(item, series_id, respect_monitoring=False)
                break


async def _grab_volume_task_sync(series_id: int, s, v, query: str) -> bool:
    """Same as _grab_volume_task but returns True if something was grabbed.
    Used by grab_volume() in 'fallback' mode to decide whether to try DDL."""
    import main as _m
    specific = await _m._search_all(query)
    general  = await _m._search_all(s['title']) if query != s['title'] else []
    seen_urls_all = {i['url'] for i in specific}
    all_items = list(specific) + [i for i in general if i['url'] not in seen_urls_all]
    all_items.sort(key=lambda x: x.get('_score', 0), reverse=True)

    with get_db() as db:
        seen_urls    = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM seen").fetchall()}
        blocked_urls = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM blocklist").fetchall()}
        alias_rows   = db.execute(
            "SELECT alias FROM series_aliases WHERE series_id=?", (series_id,)
        ).fetchall()
    all_patterns = list({s['search_pattern'], s['title']} | {a['alias'] for a in alias_rows})
    target_vol = v['volume_num'] if v else None
    for item in all_items:
        if item['url'] in seen_urls or item['url'] in blocked_urls:
            continue
        if any(_m.matches(p, item['title']) for p in all_patterns):
            item_vol = _m.extract_volume_num(item['title'])
            item_rng = _m.extract_volume_range(item['title'])
            if item_rng is not None:
                item_vol = None
            vol_ok = (
                target_vol is None
                or item_vol is None
                or abs(item_vol - target_vol) < 0.01
                or (item_rng and item_rng[0] <= target_vol <= item_rng[1])
                or _m.is_complete_pack(item['title'])
            )
            if vol_ok:
                if await _m.grab_item(item, series_id, respect_monitoring=False):
                    return True
                break
    return False


async def _grab_chapter_task(sid: int, s: dict, ch: dict):
    import main as _m
    ch_num = ch['chapter_num']
    ch_int = int(ch_num) if ch_num == int(ch_num) else ch_num

    # Try Suwayomi DDL first if series has a source configured and DDL is enabled
    from routers import suwayomi_ as _swy
    if _swy._ddl_enabled() and _swy._get_series_source(sid, s):
        with get_db() as _db:
            _swy_client = _swy.get_suwayomi_client(_db)
        if _swy_client:
            ok = await _swy.suwayomi_chapter_grab(sid, float(ch_num))
            if ok:
                return

    query  = f"{s['search_pattern']} chapter {ch_int}"
    all_items = await _m._search_all(query)
    with get_db() as db:
        seen_urls    = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM seen").fetchall()}
        blocked_urls = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM blocklist").fetchall()}
    for item in all_items:
        if item['url'] in seen_urls or item['url'] in blocked_urls:
            continue
        if not _m.matches(s['search_pattern'], item['title']):
            continue
        item_ch  = _m.extract_chapter_num(item['title'])
        item_rng = _m.extract_volume_range(item['title'])
        ch_ok = (
            (item_ch is not None and abs(item_ch - ch_num) < 0.01)
            or (item_rng and item_rng[0] <= ch_num <= item_rng[1])
            or _m.is_complete_pack(item['title'])
        )
        if ch_ok:
            if await _m.grab_item(item, sid, respect_monitoring=False):
                with get_db() as db:
                    # If chapter is linked to a volume, grab_item's cascade already
                    # populated it with full metadata — verify and top-up from the
                    # sibling volume. If uncollected (volume_id IS NULL), stamp
                    # what we know from the item dict.
                    if ch['volume_id']:
                        _sib = db.execute(
                            "SELECT source_url, torrent_name, indexer, protocol, client,"
                            " download_id, release_group, size_bytes"
                            " FROM volumes WHERE id=?",
                            (ch['volume_id'],)
                        ).fetchone()
                        _sib = dict(_sib) if _sib else {}
                        db.execute(
                            "UPDATE chapters SET status='grabbed', grabbed_at=?,"
                            " torrent_url=COALESCE(torrent_url,?),"
                            " torrent_name=COALESCE(torrent_name,?),"
                            " indexer=COALESCE(indexer,?),"
                            " protocol=COALESCE(protocol,?),"
                            " client=COALESCE(client,?),"
                            " download_id=COALESCE(download_id,?),"
                            " release_group=COALESCE(release_group,?),"
                            " size_bytes=COALESCE(NULLIF(size_bytes,0),?)"
                            " WHERE id=? AND status='wanted'",
                            (datetime.utcnow().isoformat(),
                             _sib.get('source_url') or item['url'],
                             _sib.get('torrent_name') or item['title'],
                             _sib.get('indexer') or item.get('indexer'),
                             _sib.get('protocol') or item.get('protocol'),
                             _sib.get('client'),
                             _sib.get('download_id'),
                             _sib.get('release_group'),
                             _sib.get('size_bytes'),
                             ch['id'])
                        )
                    else:
                        db.execute(
                            "UPDATE chapters SET status='grabbed', grabbed_at=?,"
                            " torrent_url=?, torrent_name=?, indexer=?, protocol=?"
                            " WHERE id=? AND status='wanted'",
                            (datetime.utcnow().isoformat(), item['url'], item['title'],
                             item.get('indexer'), item.get('protocol'), ch['id'])
                        )
            break


# ── Library index ─────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def index(request: Request, q: str = "", sort: str = "title",
                filter_status: str = "", filter_tag: str = "",
                filter_monitored: str = "", filter_missing: str = "",
                view: str = "", page: int = 1):
    if not view:
        view = request.cookies.get("library_view", "grid")

    with get_db() as db:
        # Allowlist-backed ORDER BY — only values in _LIBRARY_SORT_ALLOWED
        # can ever appear in the emitted SQL. `sort` is a request param.
        order = build_order_by(sort,
                               allowed=_LIBRARY_SORT_ALLOWED,
                               default_key=_LIBRARY_SORT_DEFAULT)
        series_rows = db.execute(f"SELECT * FROM series ORDER BY {order}").fetchall()

        _vstats = {
            r['series_id']: dict(r) for r in db.execute(
                "SELECT series_id,"
                " SUM(CASE WHEN status='wanted'     THEN 1 ELSE 0 END) as wanted,"
                " SUM(CASE WHEN status='grabbed'    THEN 1 ELSE 0 END) as grabbed,"
                " SUM(CASE WHEN status='downloaded' THEN 1 ELSE 0 END) as downloaded,"
                " COUNT(*) as total"
                " FROM volumes WHERE volume_num IS NOT NULL GROUP BY series_id"
            ).fetchall()
        }
        series       = []
        total_wanted = total_have = total_tracked = 0
        for s in series_rows:
            _r = _vstats.get(s['id'], {})
            st = {
                'total':      _r.get('total', 0) or 0,
                'wanted':     _r.get('wanted', 0) or 0,
                'grabbed':    _r.get('grabbed', 0) or 0,
                'downloaded': _r.get('downloaded', 0) or 0,
                'have':       (_r.get('grabbed', 0) or 0) + (_r.get('downloaded', 0) or 0),
            }
            total_wanted  += st['wanted']
            total_have    += st['have']
            total_tracked += st['total']
            series.append({'row': s, 'stats': st})

        if sort == "missing":
            series.sort(key=lambda x: x['stats']['wanted'], reverse=True)
        elif sort == "downloaded":
            series.sort(key=lambda x: x['stats']['downloaded'], reverse=True)

        if q:
            q_lower = q.lower()
            series  = [x for x in series if q_lower in x['row']['title'].lower()]
        if filter_status:
            series = [x for x in series
                      if (x['row']['status'] or '').upper() == filter_status.upper()]
        if filter_tag:
            def _has_tag(row):
                try:
                    return filter_tag.lower() in json.loads(row['tags'] or '[]')
                except Exception:
                    return False
            series = [x for x in series if _has_tag(x['row'])]
        if filter_monitored == '1':
            series = [x for x in series if x['row']['monitored']]
        elif filter_monitored == '0':
            series = [x for x in series if not x['row']['monitored']]
        if filter_missing == '1':
            series = [x for x in series if x['stats']['wanted'] > 0]
        elif filter_missing == '0':
            series = [x for x in series if x['stats']['wanted'] == 0 and x['stats']['total'] > 0]

        if sort == "progress":
            series.sort(key=lambda x: -(x['stats']['have'] / max(x['stats']['total'], 1)))
        elif sort == "missing":
            series.sort(key=lambda x: -x['stats']['wanted'])
        elif sort == "downloaded":
            series.sort(key=lambda x: -x['stats']['have'])

        total_count = len(series)
        total_pages = max(1, math.ceil(total_count / 200))
        page = max(1, min(page, total_pages))
        series = series[(page - 1) * 200 : page * 200]

        all_tags_rows = db.execute("SELECT tags FROM series WHERE tags IS NOT NULL").fetchall()
        all_tags: set[str] = set()
        for r in all_tags_rows:
            try:
                all_tags.update(json.loads(r['tags']))
            except Exception:
                pass

        activity = [dict(r) for r in db.execute(
            "SELECT MAX(h.event_type) as event_type, h.series_title, h.series_id, "
            "MAX(h.source_title) as source_title, "
            "MIN(h.volume_label) as first_vol, "
            "MAX(h.volume_label) as last_vol, "
            "COUNT(*) as vol_count, "
            "MAX(h.indexer) as indexer, MAX(h.protocol) as protocol, "
            "MAX(h.created_at) as created_at "
            "FROM history h "
            "WHERE h.event_type IN ('grabbed', 'imported') "
            "AND h.series_id IS NOT NULL "
            "GROUP BY h.series_id, DATE(h.created_at) "
            "ORDER BY MAX(h.created_at) DESC LIMIT 20"
        ).fetchall()]
        profiles      = db.execute("SELECT id, name FROM quality_profiles  ORDER BY name").fetchall()
        lang_profiles = db.execute("SELECT id, name FROM language_profiles ORDER BY name").fetchall()
        root_folders  = db.execute("SELECT id, path FROM root_folders      ORDER BY path").fetchall()

    resp = templates.TemplateResponse(request, "index.html", {
        "series":   series,
        "activity": activity,
        "stats_bar": {
            'series_count': len(series_rows),
            'tracked':      total_tracked,
            'have':         total_have,
            'wanted':       total_wanted,
            'pct':          int(100 * total_have / total_tracked) if total_tracked else 0,
        },
        "q": q, "sort": sort,
        "filter_status":    filter_status,
        "filter_tag":       filter_tag,
        "filter_monitored": filter_monitored,
        "filter_missing":   filter_missing,
        "all_tags":         sorted(all_tags),
        "view":             view,
        "page":             page,
        "total_pages":      total_pages,
        "total_count":      total_count,
        "profiles":         profiles,
        "lang_profiles":    lang_profiles,
        "root_folders":     root_folders,
        "monitor_modes":    ["all", "future", "missing", "existing", "none"],
    })
    resp.set_cookie("library_view", view, max_age=60*60*24*365, samesite="lax")
    return resp


# ── Series detail ─────────────────────────────────────────────────────────────

@router.get("/series/{series_id}", response_class=HTMLResponse)
async def series_detail(request: Request, series_id: int):
    import main as _m
    with get_db() as db:
        s = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
        if not s:
            return HTMLResponse("Not found", status_code=404)
        all_rows = db.execute(
            "SELECT * FROM volumes WHERE series_id=? "
            "ORDER BY COALESCE(volume_num, 9999), COALESCE(chapter_num, 9999), id",
            (series_id,)
        ).fetchall()
        all_chapters = db.execute(
            "SELECT * FROM chapters WHERE series_id=? ORDER BY chapter_num",
            (series_id,)
        ).fetchall()
        stats             = _m.get_series_stats(db, series_id)
        root_folders      = get_root_folders(db)
        quality_profiles  = db.execute("SELECT id, name FROM quality_profiles ORDER BY name").fetchall()
        language_profiles = db.execute("SELECT id, name FROM language_profiles ORDER BY name").fetchall()

    volumes   = [v for v in all_rows if v['volume_num'] is not None]
    raw_packs = [v for v in all_rows
                 if v['volume_num'] is None and v['status'] in ('grabbed', 'downloaded')]

    ch_map: dict = {}
    if s['chapter_vol_map']:
        try:
            ch_map = json.loads(s['chapter_vol_map'])
        except Exception:
            pass
    total_vols = s['total_volumes']
    total_chs  = s['total_chapters']

    def _vol_set_label(vols: set) -> str:
        if not vols:
            return ''
        sv = sorted(vols)
        if len(sv) == 1:
            return f"Vol {sv[0]}"
        runs, start, prev = [], sv[0], sv[0]
        for v in sv[1:]:
            if v == prev + 1:
                prev = v
            else:
                runs.append((start, prev)); start = prev = v
        runs.append((start, prev))
        parts = [f"{a}" if a == b else f"{a}–{b}" for a, b in runs]
        return "Vol " + ", ".join(parts)

    def _enrich_pack(p) -> dict:
        pt   = p['pack_type'] or 'volume'
        name = p['torrent_name'] or ''
        ch_label  = ''
        vol_label = ''
        covers: set = set()

        if pt == 'complete':
            vol_label = 'Complete Series'
            if total_vols:
                covers = set(range(1, total_vols + 1))
        elif pt == 'volume':
            rs, re_ = p['vol_range_start'], p['vol_range_end']
            if rs is not None and re_ is not None:
                vol_label = f"Vol {vol_num_to_display(rs)}–{vol_num_to_display(re_)}"
                covers = set(range(int(rs), int(re_) + 1))
            else:
                vn = _m.extract_volume_num(name)
                vol_label = f"Vol {vol_num_to_display(vn)}" if vn else ''
                if vn:
                    covers = {int(vn)}
        elif pt == 'chapter':
            rng = _m.extract_volume_range(name)
            if rng:
                s_ch, e_ch = rng
                ch_label = f"Ch {int(s_ch)}–{int(e_ch)}" if s_ch != e_ch else f"Ch {int(s_ch)}"
                covers = _m.chapters_to_volume_set(s_ch, e_ch, ch_map, total_chs, total_vols)
            else:
                m = re.search(r'(?:ch(?:apter)?s?\.?\s*|#\s*)(\d{1,4}(?:\.\d+)?)\b', name, re.IGNORECASE)
                if not m:
                    m = re.search(r'(?:^|[\s\[({])(\d{2,4})(?:[\s\])}]|$)', name)
                if m:
                    ch = float(m.group(1))
                    ch_label = f"Ch {int(ch)}" if ch == int(ch) else f"Ch {ch}"
                    covers = _m.chapters_to_volume_set(ch, ch, ch_map, total_chs, total_vols)
                else:
                    ch_label = ''
            vol_label = _vol_set_label(covers) if covers else ''

        return dict(p) | {'ch_label': ch_label, 'vol_label': vol_label, 'covers': sorted(covers)}

    enriched    = [_enrich_pack(p) for p in raw_packs]
    has_complete = any(p['pack_type'] == 'complete' for p in enriched)
    seen_keys: dict[str, int] = {}
    packs: list[dict] = []
    for p in enriched:
        key = f"{p['ch_label']}|{p['vol_label']}|{p['pack_type']}"
        if key in seen_keys:
            packs[seen_keys[key]]['dup_count'] = packs[seen_keys[key]].get('dup_count', 1) + 1
        else:
            p['dup_count'] = 1
            p['superseded'] = has_complete and p['pack_type'] != 'complete'
            seen_keys[key] = len(packs)
            packs.append(p)

    def _pack_sort_key(p):
        if p['pack_type'] == 'complete':
            return (0, 0)
        if p['pack_type'] == 'volume':
            return (1, p['vol_range_start'] or 0)
        m = re.search(r'\d+', p['ch_label'])
        return (2, float(m.group()) if m else 9999)
    packs.sort(key=_pack_sort_key)

    ch_map_count = 0
    if s['chapter_vol_map']:
        try:
            ch_map_count = len(json.loads(s['chapter_vol_map']))
        except Exception:
            pass

    with get_db() as db:
        _iq_rows = db.execute(
            "SELECT download_id, status FROM import_queue WHERE series_id=?"
            " AND status IN ('pending','partial')",
            (s['id'],)
        ).fetchall()
        pending_dl_ids: set[str] = {
            (r['download_id'] or '').lower()
            for r in _iq_rows if r['download_id'] and r['status'] == 'pending'
        }
        review_dl_ids: set[str] = {
            (r['download_id'] or '').lower()
            for r in _iq_rows if r['download_id'] and r['status'] == 'partial'
        }
        aliases = db.execute(
            "SELECT id, alias FROM series_aliases WHERE series_id=? ORDER BY alias",
            (s['id'],)
        ).fetchall()
        series_tags = []
        if s['tags']:
            try:
                series_tags = json.loads(s['tags'])
            except Exception:
                pass
        all_tags_rows = db.execute("SELECT tags FROM series WHERE tags IS NOT NULL").fetchall()

    all_tags: set[str] = set()
    for r in all_tags_rows:
        try:
            all_tags.update(json.loads(r['tags']))
        except Exception:
            pass

    chapters_by_vol: dict = defaultdict(list)
    for ch in all_chapters:
        chapters_by_vol[ch['volume_id']].append(ch)
    unlinked_chapters = list(chapters_by_vol.pop(None, []))

    ch_counts: dict = {}
    for vol_id, chs in chapters_by_vol.items():
        ch_counts[vol_id] = {
            'total':      len(chs),
            'downloaded': sum(1 for c in chs if c['status'] == 'downloaded'),
            'grabbed':    sum(1 for c in chs if c['status'] == 'grabbed'),
            'wanted':     sum(1 for c in chs if c['status'] == 'wanted' and c['monitored']),
        }

    dl_stages: dict[str, str] = {}
    from routers.download_clients import get_client_for_protocol as _gcp
    with get_db() as _qb_db:
        _qb_c = _gcp(_qb_db, 'torrent')
    if _qb_c:
        _qb_host = (_qb_c.get('host') or '').rstrip('/')
        _qb_user = _qb_c.get('username') or ''
        _qb_pw   = _qb_c.get('password') or ''
        _qb_cat  = _qb_c.get('category') or get_cfg('category')
        def _s_stage(state: str) -> str:
            sl = (state or '').lower()
            if 'stalled' in sl and 'up' not in sl: return 'stalled'
            if 'error' in sl or 'missing' in sl:   return 'error'
            if 'paused' in sl:                      return 'paused'
            if 'queued' in sl or 'checking' in sl:  return 'queued_dl'
            if 'upload' in sl or ('stalled' in sl and 'up' in sl): return 'completed'
            return 'downloading'
        try:
            async with httpx.AsyncClient(timeout=5) as _qb:
                _r = await _qb.post(f"{_qb_host}/api/v2/auth/login",
                                    data={'username': _qb_user, 'password': _qb_pw})
                if 'Ok' in _r.text:
                    _r2 = await _qb.get(f"{_qb_host}/api/v2/torrents/info",
                                        params={'category': _qb_cat})
                    if _r2.status_code == 200:
                        for _t in _r2.json():
                            _h = _t.get('hash', '').lower()
                            if _h:
                                dl_stages[_h] = _s_stage(_t.get('state', ''))
        except Exception:
            pass
    active_dl_ids: set[str] = set(dl_stages.keys())

    effective_cutoff = (s['quality_cutoff'] or '').strip() or get_cfg('quality_cutoff', '')
    cutoff_rank_val  = quality_rank(effective_cutoff)

    with get_db() as _swy_db:
        _swy_row = _swy_db.execute(
            "SELECT 1 FROM download_clients WHERE type='suwayomi' AND enabled=1 LIMIT 1"
        ).fetchone()
        swy_vol_jobs = _build_swy_vol_jobs(_swy_db, s['id'])
    suwayomi_enabled = _swy_row is not None

    # Metadata health panel (read-only, reuses the Stage 4 helpers).
    # Computed inline so the panel renders on first paint without an
    # extra round-trip. For typical series this is one small
    # aggregation + one per-chapter EXISTS check; HxH-sized series
    # (400+ chapters) still complete in <20ms.
    from reconcile_map import build_metadata_health
    try:
        metadata_health = build_metadata_health(series_id)
    except Exception as _e:  # defensive — a panel error must not 500 the page
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "build_metadata_health(%s) failed: %r", series_id, _e
        )
        metadata_health = None

    return templates.TemplateResponse(request, "series.html", {
        "s": s, "volumes": volumes, "packs": packs, "stats": stats,
        "root_folders": root_folders, "ch_map_count": ch_map_count,
        "aliases": aliases, "series_tags": series_tags,
        "all_tags": sorted(all_tags),
        "chapters_by_vol":   dict(chapters_by_vol),
        "unlinked_chapters": unlinked_chapters,
        "ch_counts":         ch_counts,
        "pending_dl_ids":    pending_dl_ids,
        "review_dl_ids":     review_dl_ids,
        "active_dl_ids":     active_dl_ids,
        "dl_stages":         dl_stages,
        "quality_cutoff":    effective_cutoff,
        "cutoff_rank":       cutoff_rank_val,
        "chapter_map_text":  _chapter_map_to_ranges(s['chapter_vol_map']),
        "quality_profiles":  quality_profiles,
        "language_profiles": language_profiles,
        "suwayomi_enabled":  suwayomi_enabled,
        "swy_vol_jobs":      swy_vol_jobs,
        "metadata_health":   metadata_health,
    })


@router.get("/api/series/{series_id}/metadata-health", response_class=HTMLResponse)
async def api_series_metadata_health(request: Request, series_id: int):
    """Return the metadata-health payload for a series.

    HTMX callers get the rendered panel partial for in-place refresh;
    plain callers get JSON. Strictly read-only — reuses
    build_metadata_health(), which in turn reuses the Stage 4 helpers.
    """
    from reconcile_map import build_metadata_health
    try:
        payload = build_metadata_health(series_id)
    except Exception as _e:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "build_metadata_health(%s) failed: %r", series_id, _e
        )
        return JSONResponse({"error": "series not found or helper failed"},
                            status_code=404)
    if not payload or payload.get('title') is None:
        return JSONResponse({"error": "series not found"}, status_code=404)
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request, "partials/metadata_health_panel.html",
            {"metadata_health": payload}
        )
    return JSONResponse(payload)


# ── Search & add ──────────────────────────────────────────────────────────────

@router.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = ""):
    import main as _m
    results, source_used = [], ''
    if q.strip():
        results, source_used = await _m.search_series(q)
    with get_db() as db:
        existing_anilist: dict[int, list[str]] = {}
        for r in db.execute(
            "SELECT anilist_id, edition_type FROM series WHERE anilist_id IS NOT NULL"
        ).fetchall():
            existing_anilist.setdefault(r['anilist_id'], []).append(r['edition_type'] or 'standard')
        existing_mu = {
            r['mu_id']
            for r in db.execute("SELECT mu_id FROM series WHERE mu_id IS NOT NULL").fetchall()
        }
        existing_titles = {
            r['title'].lower()
            for r in db.execute("SELECT title FROM series").fetchall()
        }
        root_folders = get_root_folders(db)
    return templates.TemplateResponse(request, "search.html", {
        "search_results":  results,
        "query":           q,
        "source_used":     source_used,
        "existing_anilist": existing_anilist,
        "existing_mu":     existing_mu,
        "existing_titles": existing_titles,
        "root_folders":    root_folders,
    })


@router.post("/series/add")
async def add_series(
    title:          str = Form(...),
    search_pattern: str = Form(...),
    anilist_id:     int = Form(0),
    mal_id:         int = Form(0),
    mu_id:          str = Form(""),
    cover_url:      str = Form(""),
    status:         str = Form(""),
    description:    str = Form(""),
    total_volumes:  int = Form(0),
    total_chapters: int = Form(0),
    root_folder_id: int = Form(0),
    pub_year:       int = Form(0),
    edition_type:   str = Form("standard"),
    monitored:      str = Form("0"),
    search_now:     str = Form("0"),
):
    import main as _m
    _valid_editions = {
        'standard', 'official_color', 'colored', 'omnibus', 'deluxe', 'digital',
        'raw', 'special', 'collector', 'remaster', 'unlocalized'
    }
    if edition_type not in _valid_editions:
        edition_type = 'standard'
    with get_db() as db:
        if anilist_id:
            existing = db.execute(
                "SELECT id FROM series WHERE anilist_id=? AND edition_type=?",
                (anilist_id, edition_type)
            ).fetchone()
        else:
            existing = db.execute(
                "SELECT id FROM series WHERE anilist_id IS NULL AND title=? AND edition_type=?",
                (title, edition_type)
            ).fetchone()
        if not existing and mu_id:
            existing = db.execute(
                "SELECT id FROM series WHERE mu_id=? AND edition_type=?", (mu_id, edition_type)
            ).fetchone()
        if existing:
            return RedirectResponse(f"/series/{existing['id']}", status_code=303)
        rf_id = root_folder_id or None
        if not rf_id:
            default_rf = db.execute(
                "SELECT id FROM root_folders WHERE is_default=1 LIMIT 1"
            ).fetchone()
            if default_rf:
                rf_id = default_rf['id']
        _monitored = monitored == "1"
        _search_now = search_now == "1"
        cur = db.execute(
            "INSERT INTO series(title, search_pattern, anilist_id, mal_id, mu_id, cover_url,"
            " status, description, total_volumes, total_chapters, root_folder_id, pub_year,"
            " edition_type, vol_count_source, monitored)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (title, search_pattern, anilist_id or None, mal_id or None, mu_id or None,
             cover_url, status, description, total_volumes or None, total_chapters or None,
             rf_id, pub_year or None, edition_type, 'anilist', 1 if _monitored else 0)
        )
        series_id = cur.lastrowid
        if total_volumes and total_volumes > 0 and edition_type not in _m._NON_STANDARD_STUB_EDITIONS:
            _m.create_volume_stubs(db, series_id, total_volumes)
        _m.add_history(db, 'series_added', series_id, title, '',
                       source_title=title,
                       data={'total_volumes': total_volumes, 'status': status})
    # Fire all post-add tasks in background — don't block the response
    asyncio.create_task(_m.refresh_mangadex_map(series_id))
    if anilist_id:
        asyncio.create_task(_m.fetch_anilist_aliases(series_id, anilist_id, title))
    if cover_url:
        asyncio.create_task(_m.download_cover(series_id, cover_url))
    asyncio.create_task(_m.fetch_mu_metadata(series_id, title))
    if _search_now:
        asyncio.create_task(_m.grab_existing(series_id, title, search_pattern))
    if edition_type in _m._NON_STANDARD_STUB_EDITIONS:
        asyncio.create_task(_m.fetch_edition_volume_count(series_id, title, edition_type))
    asyncio.create_task(_m.notify_discord('', event='on_series_add', embed={
        'title': f'Added — {title}',
        'description': (f"Status: {status}" if status else "Added to library"),
        'color': 0x4cc9f0,
        'thumbnail': {'url': cover_url} if cover_url else {},
    }))
    return RedirectResponse(with_flash(f"/series/{series_id}", "Search queued for all wanted volumes", "success"), status_code=303)


@router.get("/api/series/{series_id}/cover-refresh")
async def api_cover_refresh(series_id: int):
    import main as _m
    with get_db() as db:
        s = db.execute("SELECT id, cover_url FROM series WHERE id=?", (series_id,)).fetchone()
    if not s:
        return JSONResponse({"error": "Series not found"}, status_code=404)
    dest = f"/config/covers/{series_id}.jpg"
    try:
        if os.path.exists(dest):
            os.remove(dest)
    except Exception:
        pass
    if s['cover_url']:
        await _m.download_cover(series_id, s['cover_url'])
        return JSONResponse({"ok": True, "cover_url": f"/covers/{series_id}.jpg"})
    return JSONResponse({"ok": False, "error": "No cover_url stored for this series"})


@router.post("/series/{series_id}/delete")
async def delete_series(request: Request, series_id: int):
    import main as _m
    with get_db() as db:
        s = db.execute("SELECT title FROM series WHERE id=?", (series_id,)).fetchone()
        title = s['title'] if s else ''
        iq_ids = [r['id'] for r in db.execute(
            "SELECT id FROM import_queue WHERE series_id=?", (series_id,)
        ).fetchall()]
        for iq_id in iq_ids:
            db.execute("DELETE FROM import_queue_files WHERE queue_id=?", (iq_id,))
        db.execute("DELETE FROM import_queue WHERE series_id=?", (series_id,))
        db.execute("DELETE FROM chapters WHERE series_id=?", (series_id,))
        db.execute("DELETE FROM volumes WHERE series_id=?", (series_id,))
        db.execute("DELETE FROM pending_releases WHERE series_id=?", (series_id,))
        db.execute("DELETE FROM seen WHERE series_id=?", (series_id,))
        db.execute("DELETE FROM blocklist WHERE series_id=?", (series_id,))
        db.execute("DELETE FROM series_aliases WHERE series_id=?", (series_id,))
        db.execute("DELETE FROM series_tags WHERE series_id=?", (series_id,))
        _m.add_history(db, 'series_deleted', None, title, '', source_title=title)
        db.execute("DELETE FROM series WHERE id=?", (series_id,))
    cover_path = f"/config/covers/{series_id}.jpg"
    try:
        if os.path.exists(cover_path):
            os.remove(cover_path)
    except OSError:
        pass
    if request.headers.get("HX-Request") == "true":
        from fastapi.responses import Response as _Resp
        return _Resp(headers={"HX-Redirect": "/"})
    return RedirectResponse("/", status_code=303)


@router.post("/series/{series_id}/grab")
async def manual_grab(request: Request, series_id: int):
    import main as _m
    with get_db() as db:
        s = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
    if s:
        asyncio.create_task(_m.grab_existing(series_id, s['title'], s['search_pattern']))
    if request.headers.get("HX-Request") == "true":
        import json
        from fastapi.responses import Response as _Resp
        return _Resp(headers={"HX-Trigger": json.dumps({
            "showToast": {"msg": "Search queued for all wanted volumes", "type": "success"}
        })})
    return RedirectResponse(f"/series/{series_id}", status_code=303)


@router.post("/api/series/{series_id}/search-complete")
async def api_search_complete_pack(series_id: int):
    import main as _m
    with get_db() as db:
        s = db.execute(
            "SELECT title, total_volumes FROM series WHERE id=?", (series_id,)
        ).fetchone()
    if not s:
        return JSONResponse({'error': 'Series not found'}, status_code=404)
    grabbed = await _m.search_complete_pack(series_id, s['title'], s['total_volumes'])
    return JSONResponse({'grabbed': grabbed, 'title': s['title']})


@router.post("/series/{series_id}/volumes/{volume_id}/grab")
async def grab_volume(request: Request, series_id: int, volume_id: int):
    with get_db() as db:
        s = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
        v = db.execute(
            "SELECT * FROM volumes WHERE id=? AND series_id=?", (volume_id, series_id)
        ).fetchone()
        swy_client = None
        if s and v and v['volume_num']:
            from routers.suwayomi_ import get_suwayomi_client, _get_series_source
            swy_client = get_suwayomi_client(db)
            if swy_client and not _get_series_source(series_id, dict(s)):
                swy_client = None  # no source configured for this series

    if s and v:
        ddl_mode = get_cfg('ddl_grab_mode', 'fallback')
        ddl_available = swy_client and v['volume_num'] and ddl_mode != 'off'

        if ddl_mode == 'only' and ddl_available:
            # DDL-only mode: only try Suwayomi
            from routers import suwayomi_ as _swy
            await _swy.suwayomi_grab(series_id, float(v['volume_num']))

        elif ddl_mode == 'prefer' and ddl_available:
            # DDL-preferred: try Suwayomi first, indexers as fallback
            from routers import suwayomi_ as _swy
            ddl_ok = await _swy.suwayomi_grab(series_id, float(v['volume_num']))
            if not ddl_ok:
                vol_q = f"{s['title']} v{vol_num_to_display(v['volume_num'])}" if v['volume_num'] else s['title']
                asyncio.create_task(_grab_volume_task(series_id, s, v, vol_q))

        else:
            # 'fallback' (default) or 'off': try indexers first
            vol_q = f"{s['title']} v{vol_num_to_display(v['volume_num'])}" if v['volume_num'] else s['title']
            grabbed = await _grab_volume_task_sync(series_id, s, v, vol_q)
            # If indexers found nothing and DDL is available, try Suwayomi as fallback
            if not grabbed and ddl_available:
                from routers import suwayomi_ as _swy
                await _swy.suwayomi_grab(series_id, float(v['volume_num']))

    if request.headers.get("HX-Request") == "true":
        ctx = await _get_volume_row_ctx(series_id, volume_id)
        return templates.TemplateResponse(request, "partials/volume_row.html", ctx)
    return RedirectResponse(f"/series/{series_id}", status_code=303)


@router.post("/series/{series_id}/toggle")
async def toggle_monitored(request: Request, series_id: int):
    with get_db() as db:
        cur = db.execute("SELECT monitored FROM series WHERE id=?", (series_id,)).fetchone()
        if cur:
            new_val = 0 if cur['monitored'] else 1
            db.execute("UPDATE series SET monitored=? WHERE id=?", (new_val, series_id))
    if request.headers.get("HX-Request") == "true":
        import json
        from fastapi.responses import Response as _Resp
        state = "monitored" if new_val else "unmonitored"
        return _Resp(headers={"HX-Trigger": json.dumps({
            "showToast": {"msg": f"Series {state}", "type": "success"}
        })})
    return RedirectResponse(f"/series/{series_id}", status_code=303)


@router.post("/series/{series_id}/refresh")
async def refresh_series(request: Request, series_id: int):
    """Refresh metadata from AniList and create any new volume stubs."""
    import main as _m
    with get_db() as db:
        s = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
    if not s:
        return RedirectResponse(f"/series/{series_id}", status_code=303)
    results = await _m.anilist_search(s['title'])
    if results:
        stored_words = set(_m.normalize(s['title']).split())

        def _title_f1(r) -> float:
            r_words = set(_m.normalize(r['title']).split())
            if not r_words or not stored_words:
                return 0.0
            inter     = stored_words & r_words
            recall    = len(inter) / len(stored_words)
            precision = len(inter) / len(r_words)
            return 2 * recall * precision / (recall + precision) if (recall + precision) else 0.0

        with get_db() as db:
            max_stub_row = db.execute(
                "SELECT MAX(volume_num) as m FROM volumes WHERE series_id=? AND volume_num IS NOT NULL",
                (series_id,)
            ).fetchone()
        min_vols   = int(max_stub_row['m']) if max_stub_row and max_stub_row['m'] else 0
        plausible  = [r for r in results if not r.get('volumes') or r.get('volumes', 0) >= min_vols]
        candidates = plausible if plausible else results
        best_by_title = max(candidates, key=lambda r: (_title_f1(r), r.get('volumes') or 0))
        match = None
        if stored_words and _title_f1(best_by_title) >= 0.5:
            match = best_by_title
        elif s['anilist_id']:
            match = next((r for r in candidates if r['anilist_id'] == s['anilist_id']), None)
        if not match:
            match = results[0]
        with get_db() as db:
            existing = db.execute(
                "SELECT total_volumes, total_chapters FROM series WHERE id=?", (series_id,)
            ).fetchone()
            new_total_vols = match['volumes'] or (existing['total_volumes'] if existing else None)
            new_total_chs  = match['chapters'] or (existing['total_chapters'] if existing else None)
            _new_status = match['status']
            db.execute(
                "UPDATE series SET status=?, cover_url=?, total_volumes=?, total_chapters=?,"
                " description=?, anilist_id=?, last_metadata_refresh=?,"
                " vol_count_source=CASE WHEN COALESCE(vol_count_source,'anilist')"
                " IN ('google_books','wikipedia','manual') THEN vol_count_source ELSE 'anilist' END"
                " WHERE id=?",
                (_new_status, match['cover_url'], new_total_vols, new_total_chs,
                 match['description'], match['anilist_id'], datetime.utcnow().isoformat(), series_id)
            )
            if _new_status in ('FINISHED', 'CANCELLED'):
                _cur_strategy = db.execute(
                    "SELECT update_strategy FROM series WHERE id=?", (series_id,)
                ).fetchone()
                if _cur_strategy and (_cur_strategy['update_strategy'] or 'always') == 'always':
                    db.execute(
                        "UPDATE series SET update_strategy='once' WHERE id=?", (series_id,)
                    )
            if match['volumes'] and int(match['volumes']) > 0 \
                    and (s['edition_type'] or 'standard') not in _m._NON_STANDARD_STUB_EDITIONS:
                _m.create_volume_stubs(db, series_id, int(match['volumes']))
                has_complete = db.execute(
                    "SELECT 1 FROM volumes WHERE series_id=? AND pack_type='complete' AND status='grabbed'",
                    (series_id,)
                ).fetchone()
                if has_complete:
                    db.execute(
                        "UPDATE volumes SET status='grabbed' WHERE series_id=? "
                        "AND status='wanted' AND volume_num IS NOT NULL",
                        (series_id,)
                    )
        _m.log_event('refresh', f"Refreshed from AniList: status={match['status']}, "
                     f"{match['volumes'] or '?'} vols", series_id)
        await _m.refresh_mangadex_map(series_id)
        _m.backfill_pack_ranges()
        if match.get('anilist_id'):
            asyncio.create_task(_m.fetch_anilist_aliases(series_id, match['anilist_id'], s['title']))
        asyncio.create_task(_m.fetch_mu_metadata(series_id, s['title']))
    if request.headers.get("HX-Request") == "true":
        import json
        from fastapi.responses import Response as _Resp
        return _Resp(headers={"HX-Trigger": json.dumps({
            "showToast": {"msg": "Refreshed", "type": "success"}
        })})
    return RedirectResponse(f"/series/{series_id}", status_code=303)


@router.post("/library/refresh-all")
async def refresh_all_series(request: Request):
    """Refresh metadata from AniList for all monitored series (background task)."""
    import main as _m

    async def _run():
        with get_db() as db:
            series = db.execute(
                "SELECT id, title, edition_type FROM series WHERE monitored=1 ORDER BY title"
            ).fetchall()
        refreshed = 0
        for s in series:
            try:
                results = await _m.anilist_search(s['title'])
                if results:
                    stored_words = set(_m.normalize(s['title']).split())
                    def _f1(r) -> float:
                        r_words = set(_m.normalize(r['title']).split())
                        if not r_words or not stored_words: return 0.0
                        inter = stored_words & r_words
                        rec = len(inter)/len(stored_words); prec = len(inter)/len(r_words)
                        return 2*rec*prec/(rec+prec) if (rec+prec) else 0.0
                    with get_db() as db2:
                        max_row = db2.execute(
                            "SELECT MAX(volume_num) as m FROM volumes"
                            " WHERE series_id=? AND volume_num IS NOT NULL",
                            (s['id'],)
                        ).fetchone()
                        s_row = db2.execute(
                            "SELECT anilist_id FROM series WHERE id=?", (s['id'],)
                        ).fetchone()
                    min_vols   = int(max_row['m']) if max_row and max_row['m'] else 0
                    plausible  = [r for r in results if not r.get('volumes') or r.get('volumes', 0) >= min_vols]
                    candidates = plausible if plausible else results
                    best = max(candidates, key=lambda r: (_f1(r), r.get('volumes') or 0))
                    match = None
                    if stored_words and _f1(best) >= 0.5:
                        match = best
                    elif s_row and s_row['anilist_id']:
                        match = next((r for r in candidates if r['anilist_id'] == s_row['anilist_id']), None)
                    if not match:
                        match = results[0]
                    with get_db() as db2:
                        db2.execute(
                            "UPDATE series SET status=?, cover_url=?, total_volumes=?, total_chapters=?,"
                            " description=?,"
                            " vol_count_source=CASE WHEN COALESCE(vol_count_source,'anilist')"
                            " IN ('google_books','wikipedia','manual') THEN vol_count_source ELSE 'anilist' END"
                            " WHERE id=?",
                            (match['status'], match['cover_url'], match['volumes'] or None,
                             match['chapters'] or None, match['description'], s['id'])
                        )
                        if match['volumes'] and int(match['volumes']) > 0 \
                                and (s['edition_type'] or 'standard') not in _m._NON_STANDARD_STUB_EDITIONS:
                            _m.create_volume_stubs(db2, s['id'], int(match['volumes']))
                    refreshed += 1
            except Exception as e:
                print(f"[RefreshAll] Error refreshing {s['title']}: {e}")
            await asyncio.sleep(1.5)
        _m.log_event('refresh', f"Refresh all: {refreshed}/{len(series)} series updated")

    asyncio.create_task(_run())
    if request.headers.get("HX-Request") == "true":
        import json
        from fastapi.responses import Response as _Resp
        return _Resp(headers={"HX-Trigger": json.dumps({
            "showToast": {"msg": "Metadata refresh started in background", "type": "success"}
        })})
    return RedirectResponse("/?sort=added", status_code=303)


# ── Chapter map editor ────────────────────────────────────────────────────────

@router.get("/series/{series_id}/chapter-map", response_class=HTMLResponse)
async def chapter_map_editor(request: Request, series_id: int):
    with get_db() as db:
        s = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
        if not s:
            return HTMLResponse("Not found", status_code=404)
        ch_map = json.loads(s['chapter_vol_map'] or '{}') if s else {}
        overrides = {r['chapter']: r['volume_num']
                     for r in db.execute(
                         "SELECT chapter, volume_num FROM series_chapter_overrides WHERE series_id=?",
                         (series_id,)
                     ).fetchall()}
        merged = {**ch_map, **{k: str(v) if v is not None else None for k, v in overrides.items()}}
        total_volumes = s['total_volumes'] if s else 0

        def _ch_sort_key(ch_str: str) -> float:
            try:
                return float(ch_str)
            except (ValueError, TypeError):
                return 9999.0
        ch_map_items = sorted(merged.items(), key=lambda kv: _ch_sort_key(kv[0]))
    return templates.TemplateResponse(request, "chapter_map_editor.html", {
        "request": request, "s": s, "ch_map": merged, "ch_map_items": ch_map_items,
        "overrides": overrides, "total_volumes": total_volumes,
    })


@router.post("/series/{series_id}/chapter-map")
async def save_chapter_map(request: Request, series_id: int):
    body = await request.json()
    with get_db() as db:
        db.execute("DELETE FROM series_chapter_overrides WHERE series_id=?", (series_id,))
        for chapter, volume in body.get('overrides', {}).items():
            db.execute(
                "INSERT INTO series_chapter_overrides(series_id, chapter, volume_num) VALUES(?,?,?)",
                (series_id, chapter,
                 float(volume) if volume not in (None, '', 'null') else None)
            )
    return JSONResponse({"ok": True})


@router.post("/series/{series_id}/chapter-map/reset")
async def reset_chapter_map(series_id: int):
    with get_db() as db:
        db.execute("DELETE FROM series_chapter_overrides WHERE series_id=?", (series_id,))
    return RedirectResponse(f"/series/{series_id}/chapter-map", status_code=303)


# ── Series edit ───────────────────────────────────────────────────────────────

@router.post("/series/{series_id}/edit")
async def edit_series(
    request:                Request,
    series_id:              int,
    title:                  str = Form(""),
    search_pattern:         str = Form(""),
    chapter_map_text:       str = Form(""),
    preferred_groups_input: str = Form(""),
    blocked_groups_input:   str = Form(""),
    omnibus_preference:     str = Form("prefer_individual"),
    quality_profile_id:     int = Form(0),
    language_profile_id:    int = Form(0),
    quality_cutoff:         str = Form(""),
    update_strategy:        str = Form("always"),
    required_scanlator:     str = Form(""),
    source_type:            str = Form("any"),
    edition_type:           str = Form("standard"),
    total_volumes:          int = Form(0),
    ddl_language:           str = Form(""),
):
    import main as _m
    map_updated = False
    preferred_groups = json.dumps(
        [g.strip() for g in preferred_groups_input.split(',') if g.strip()]
    )
    blocked_groups = json.dumps(
        [g.strip() for g in blocked_groups_input.split(',') if g.strip()]
    )
    with get_db() as db:
        updates, params = [], []
        if title:
            updates.append("title=?"); params.append(title)
        if search_pattern:
            updates.append("search_pattern=?"); params.append(search_pattern)
        if chapter_map_text.strip():
            new_map = _parse_chapter_ranges(chapter_map_text)
            if new_map:
                updates.append("chapter_vol_map=?")
                params.append(json.dumps(new_map))
                map_updated = True
        updates.append("preferred_groups=?"); params.append(preferred_groups)
        updates.append("blocked_groups=?");   params.append(blocked_groups)
        valid_prefs = {'prefer_individual', 'prefer_omnibus', 'only_individual', 'only_omnibus'}
        pref = omnibus_preference if omnibus_preference in valid_prefs else 'prefer_individual'
        updates.append("omnibus_preference=?"); params.append(pref)
        updates.append("quality_profile_id=?")
        params.append(int(quality_profile_id) if quality_profile_id else None)
        updates.append("language_profile_id=?")
        params.append(int(language_profile_id) if language_profile_id else None)
        valid_cutoffs = {'', 'cbz', 'cbr', 'epub', 'pdf', 'zip', 'mobi'}
        updates.append("quality_cutoff=?")
        params.append(quality_cutoff if quality_cutoff in valid_cutoffs else '')
        valid_strategies = {'always', 'once', 'throttled'}
        updates.append("update_strategy=?")
        params.append(update_strategy if update_strategy in valid_strategies else 'always')
        updates.append("required_scanlator=?")
        params.append(required_scanlator.strip() or None)
        valid_editions = {
            'standard', 'official_color', 'colored', 'omnibus', 'deluxe', 'digital',
            'raw', 'special', 'collector', 'remaster', 'unlocalized'
        }
        _edition = edition_type if edition_type in valid_editions else 'standard'
        updates.append("edition_type=?"); params.append(_edition)
        _implied_source = {'official_color': 'official_only', 'colored': 'fan_only', 'unlocalized': 'fan_only'}
        valid_source_types = {'any', 'official_only', 'fan_only'}
        _source = _implied_source.get(_edition) or (source_type if source_type in valid_source_types else 'any')
        updates.append("source_type=?"); params.append(_source)
        # DDL language override — store None to clear (fall back to global default)
        _ddl_lang = ddl_language.strip().lower()[:5] if ddl_language.strip() else None
        updates.append("ddl_language=?"); params.append(_ddl_lang)

        _manual_new = _manual_old = None
        if total_volumes and total_volumes > 0:
            tv_row = db.execute(
                "SELECT total_volumes FROM series WHERE id=?", (series_id,)
            ).fetchone()
            _manual_old = (tv_row['total_volumes'] or 0) if tv_row else 0
            _manual_new = total_volumes
            updates.append("total_volumes=?"); params.append(_manual_new)
            updates.append("vol_count_source=?"); params.append('manual')

        if updates:
            params.append(series_id)
            db.execute(f"UPDATE series SET {', '.join(updates)} WHERE id=?", params)

        if _manual_new is not None:
            if _manual_new > (_manual_old or 0):
                _m.create_volume_stubs(db, series_id, _manual_new)
            elif _manual_old and _manual_new < _manual_old:
                # Clear volume_id on any chapters pointing at the volumes we're
                # about to delete — otherwise the chapters become orphans with a
                # dangling FK pointer.
                db.execute(
                    "UPDATE chapters SET volume_id=NULL"
                    " WHERE volume_id IN ("
                    "   SELECT id FROM volumes WHERE series_id=? AND volume_num>? AND status='wanted'"
                    " )",
                    (series_id, float(_manual_new))
                )
                db.execute(
                    "DELETE FROM volumes WHERE series_id=? AND volume_num>? AND status='wanted'",
                    (series_id, float(_manual_new))
                )
                _m.log_event('metadata', f"[Manual] removed wanted stubs > vol {_manual_new}", series_id)

        if map_updated:
            _m.populate_chapters(db, series_id)

    if request.headers.get("HX-Request") == "true":
        from fastapi.responses import Response as _Resp
        return _Resp(headers={"HX-Refresh": "true"})
    return RedirectResponse(f"/series/{series_id}", status_code=303)


# ── Volume actions ────────────────────────────────────────────────────────────

@router.post("/series/{series_id}/volumes/{volume_id}/mark-downloaded")
async def mark_volume_downloaded(request: Request, series_id: int, volume_id: int):
    import main as _m
    with get_db() as db:
        now_ts = datetime.utcnow().isoformat()
        v = db.execute(
            "SELECT volume_num FROM volumes WHERE id=? AND series_id=?",
            (volume_id, series_id)
        ).fetchone()
        s = db.execute("SELECT title FROM series WHERE id=?", (series_id,)).fetchone()
        db.execute(
            "UPDATE volumes SET status='downloaded', imported_at=COALESCE(imported_at,?)"
            " WHERE id=? AND series_id=?",
            (now_ts, volume_id, series_id)
        )
        cascade_chapters(db, series_id, [volume_id], 'downloaded')
        if v and s:
            vol_label = f"Vol {vol_num_to_display(v['volume_num'])}" if v['volume_num'] else '—'
            _m.add_history(db, 'volume_marked_downloaded', series_id, s['title'], vol_label)
    if request.headers.get("HX-Request") == "true":
        ctx = await _get_volume_row_ctx(series_id, volume_id)
        return templates.TemplateResponse(request, "partials/volume_row.html", ctx)
    return RedirectResponse(f"/series/{series_id}", status_code=303)


@router.post("/series/{series_id}/volumes/{volume_id}/mark-wanted")
async def mark_volume_wanted(request: Request, series_id: int, volume_id: int):
    import main as _m
    with get_db() as db:
        row = db.execute(
            "SELECT source_url, download_id, volume_num FROM volumes WHERE id=? AND series_id=?",
            (volume_id, series_id)
        ).fetchone()
        s = db.execute("SELECT title FROM series WHERE id=?", (series_id,)).fetchone()
        if row:
            if row['source_url']:
                db.execute("DELETE FROM seen WHERE torrent_url=?", (row['source_url'],))
            if row['download_id']:
                others = db.execute(
                    "SELECT COUNT(*) FROM volumes WHERE download_id=? AND status='grabbed' AND id != ?",
                    (row['download_id'], volume_id)
                ).fetchone()[0]
                if others == 0:
                    db.execute("DELETE FROM seen WHERE download_id=?", (row['download_id'],))
        db.execute(
            "UPDATE volumes SET status='wanted', grabbed_at=NULL, imported_at=NULL,"
            " import_path=NULL, source_url=NULL, download_id=NULL, torrent_name=NULL,"
            " indexer=NULL, protocol=NULL, client=NULL, release_group=NULL,"
            " size_bytes=NULL, quality=NULL WHERE id=? AND series_id=?",
            (volume_id, series_id)
        )
        cascade_chapters(db, series_id, [volume_id], 'wanted',
                         grabbed_at=None, torrent_name=None, torrent_url=None,
                         indexer=None, protocol=None, client=None,
                         download_id=None, release_group=None)
        if row and s:
            vol_label = f"Vol {vol_num_to_display(row['volume_num'])}" if row['volume_num'] else '—'
            _m.add_history(db, 'volume_marked_wanted', series_id, s['title'], vol_label)
    if request.headers.get("HX-Request") == "true":
        ctx = await _get_volume_row_ctx(series_id, volume_id)
        return templates.TemplateResponse(request, "partials/volume_row.html", ctx)
    return RedirectResponse(f"/series/{series_id}", status_code=303)


@router.post("/series/{series_id}/volumes/{volume_id}/reset-to-wanted")
async def reset_volume_to_wanted(series_id: int, volume_id: int):
    with get_db() as db:
        db.execute(
            "UPDATE volumes SET status='wanted', download_id=NULL, grabbed_at=NULL,"
            " source_url=NULL, torrent_name=NULL, indexer=NULL, protocol=NULL,"
            " client=NULL, release_group=NULL, import_path=NULL"
            " WHERE id=? AND series_id=? AND status='grabbed'",
            (volume_id, series_id)
        )
    return RedirectResponse(f"/series/{series_id}", status_code=303)


@router.post("/series/{series_id}/volumes/{volume_id}/toggle-monitor")
async def toggle_volume_monitor(request: Request, series_id: int, volume_id: int):
    with get_db() as db:
        v = db.execute(
            "SELECT monitored FROM volumes WHERE id=? AND series_id=?", (volume_id, series_id)
        ).fetchone()
        if v:
            db.execute(
                "UPDATE volumes SET monitored=? WHERE id=?",
                (0 if v['monitored'] else 1, volume_id)
            )
    if request.headers.get("HX-Request") == "true":
        ctx = await _get_volume_row_ctx(series_id, volume_id)
        return templates.TemplateResponse(request, "partials/volume_row.html", ctx)
    return RedirectResponse(f"/series/{series_id}", status_code=303)


@router.post("/series/{series_id}/volumes/{volume_id}/delete-file")
async def delete_volume_file(request: Request, series_id: int, volume_id: int):
    import main as _m
    with get_db() as db:
        v = db.execute(
            "SELECT * FROM volumes WHERE id=? AND series_id=?", (volume_id, series_id)
        ).fetchone()
        s = db.execute("SELECT title FROM series WHERE id=?", (series_id,)).fetchone()
        if not v:
            return RedirectResponse(f"/series/{series_id}", status_code=303)

        deleted = False
        if v['import_path']:
            path = v['import_path']
            if os.path.isfile(path):
                try:
                    os.remove(path)
                    deleted = True
                except Exception as e:
                    _m.log_event('error', f"File delete failed: {e}", series_id)
            elif os.path.isdir(path) and v['volume_num']:
                for fname in os.listdir(path):
                    fvol = _m.extract_volume_num(fname)
                    if fvol is not None and abs(fvol - v['volume_num']) < 0.01:
                        try:
                            os.remove(os.path.join(path, fname))
                            deleted = True
                        except Exception as e:
                            _m.log_event('error', f"File delete failed: {e}", series_id)
                        break

        db.execute(
            "UPDATE volumes SET status='wanted', import_path=NULL, download_id=NULL, "
            "grabbed_at=NULL, source_url=NULL, torrent_name=NULL, indexer=NULL, protocol=NULL,"
            " client=NULL, release_group=NULL WHERE id=?", (volume_id,)
        )
        cascade_chapters(db, series_id, [volume_id], 'wanted',
                         grabbed_at=None, torrent_name=None, torrent_url=None,
                         indexer=None, protocol=None, client=None,
                         download_id=None, release_group=None)
        from shared import build_volume_label
        vol_label = build_volume_label(v['volume_num'], None, None)
        _m.add_history(db, 'file_deleted', series_id, s['title'] if s else '',
                       vol_label, source_title=v['torrent_name'] or '',
                       data={'deleted': deleted, 'path': v['import_path']})
        msg = f"Deleted file for {vol_label}" if deleted else f"Reset {vol_label} to wanted (file not found)"
        _m.log_event('delete', msg, series_id)
    if request.headers.get("HX-Request") == "true":
        ctx = await _get_volume_row_ctx(series_id, volume_id)
        return templates.TemplateResponse(request, "partials/volume_row.html", ctx)
    return RedirectResponse(f"/series/{series_id}", status_code=303)


@router.post("/series/{series_id}/volumes/{volume_id}/set-range")
async def set_pack_range(
    series_id:       int,
    volume_id:       int,
    vol_range_start: float = Form(0),
    vol_range_end:   float = Form(0),
    mark_stubs:      str   = Form("1"),
):
    now = datetime.utcnow().isoformat()
    with get_db() as db:
        pack = db.execute(
            "SELECT torrent_name FROM volumes WHERE id=? AND series_id=?",
            (volume_id, series_id)
        ).fetchone()
        if not pack:
            return RedirectResponse(f"/series/{series_id}", status_code=303)
        db.execute(
            "UPDATE volumes SET vol_range_start=?, vol_range_end=? WHERE id=?",
            (vol_range_start or None, vol_range_end or None, volume_id)
        )
        if mark_stubs and vol_range_start and vol_range_end:
            db.execute(
                "UPDATE volumes SET status='grabbed', grabbed_at=?, torrent_name=? "
                "WHERE series_id=? AND status='wanted' "
                "AND volume_num IS NOT NULL "
                "AND volume_num >= ? AND volume_num <= ?",
                (now, pack['torrent_name'], series_id, vol_range_start, vol_range_end)
            )
        elif mark_stubs and not vol_range_start and not vol_range_end:
            db.execute(
                "UPDATE volumes SET status='grabbed', grabbed_at=?, torrent_name=? "
                "WHERE series_id=? AND status='wanted' AND volume_num IS NOT NULL",
                (now, pack['torrent_name'], series_id)
            )
    if request.headers.get("HX-Request") == "true":
        from fastapi.responses import Response as _Resp
        return _Resp(headers={"HX-Refresh": "true"})
    return RedirectResponse(f"/series/{series_id}", status_code=303)


@router.post("/series/{series_id}/set-root-folder")
async def set_series_root_folder(request: Request, series_id: int, root_folder_id: int = Form(0)):
    with get_db() as db:
        db.execute(
            "UPDATE series SET root_folder_id=? WHERE id=?",
            (root_folder_id or None, series_id)
        )
    if request.headers.get("HX-Request") == "true":
        import json
        from fastapi.responses import Response as _Resp
        return _Resp(headers={"HX-Trigger": json.dumps({
            "showToast": {"msg": "Root folder updated", "type": "success"}
        })})
    return RedirectResponse(f"/series/{series_id}", status_code=303)


@router.post("/series/{series_id}/set-monitor-mode")
async def set_monitor_mode(request: Request, series_id: int, mode: str = Form("all")):
    import main as _m
    valid = ('all', 'future', 'missing', 'existing', 'none')
    if mode not in valid:
        mode = 'all'
    with get_db() as db:
        db.execute("UPDATE series SET monitor_mode=? WHERE id=?", (mode, series_id))
        if mode == 'none':
            db.execute("UPDATE volumes SET monitored=0 WHERE series_id=?", (series_id,))
        elif mode == 'all':
            db.execute("UPDATE volumes SET monitored=1 WHERE series_id=?", (series_id,))
        elif mode == 'missing':
            db.execute(
                "UPDATE volumes SET monitored=CASE WHEN status='wanted' THEN 1 ELSE 0 END "
                "WHERE series_id=?", (series_id,)
            )
        elif mode == 'existing':
            db.execute(
                "UPDATE volumes SET monitored=CASE WHEN status='downloaded' THEN 1 ELSE 0 END "
                "WHERE series_id=?", (series_id,)
            )
        elif mode == 'future':
            max_dl = db.execute(
                "SELECT MAX(volume_num) as m FROM volumes "
                "WHERE series_id=? AND status='downloaded' AND volume_num IS NOT NULL",
                (series_id,)
            ).fetchone()
            threshold = (max_dl['m'] or 0) if max_dl else 0
            db.execute(
                "UPDATE volumes SET monitored=CASE WHEN volume_num > ? THEN 1 ELSE 0 END "
                "WHERE series_id=?", (threshold, series_id)
            )
    _m.log_event('monitor', f"Monitor mode set to '{mode}'", series_id)
    if request.headers.get("HX-Request") == "true":
        import json
        from fastapi.responses import Response as _Resp
        labels = {'all': 'All', 'future': 'Future', 'missing': 'Missing', 'existing': 'Existing', 'none': 'None'}
        return _Resp(headers={"HX-Trigger": json.dumps({
            "showToast": {"msg": f"Monitor mode: {labels.get(mode, mode)}", "type": "success"}
        })})
    return RedirectResponse(f"/series/{series_id}", status_code=303)


# ── Interactive volume search ─────────────────────────────────────────────────

@router.get("/api/series/{series_id}/volumes/{volume_id}/search")
async def search_volume_releases(series_id: int, volume_id: int):
    import main as _m
    with get_db() as db:
        s = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
        v = db.execute(
            "SELECT * FROM volumes WHERE id=? AND series_id=?", (volume_id, series_id)
        ).fetchone()
        alias_rows   = db.execute("SELECT alias FROM series_aliases WHERE series_id=?",
                                  (s['id'],)).fetchall() if s else []
        seen_urls    = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM seen").fetchall()}
        blocked_urls = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM blocklist").fetchall()}
    if not s or not v:
        return JSONResponse({"error": "Not found"}, status_code=404)

    vol_num = v['volume_num']
    query   = f"{s['title']} v{int(vol_num)}" if vol_num else s['title']
    all_match_patterns = list({s['search_pattern'], s['title']} | {a['alias'] for a in alias_rows})

    queries = [query]
    if query != s['title']:
        queries.append(s['title'])
    if s['search_pattern'] not in (query, s['title']):
        sp_q = f"{s['search_pattern']} v{int(vol_num)}" if vol_num else s['search_pattern']
        queries.append(sp_q)
    for a in alias_rows[:3]:
        alias_q = f"{a['alias']} v{int(vol_num)}" if vol_num else a['alias']
        queries.append(alias_q)

    seen_q: set[str] = set()
    items: list[dict] = []
    all_results = await asyncio.gather(*[_m._search_all(q) for q in queries])
    for query_results in all_results:
        for item in query_results:
            if item['url'] not in seen_q:
                items.append(item)
                seen_q.add(item['url'])
    items.sort(key=lambda x: x.get('_score', 0), reverse=True)

    results = []
    with get_db() as _eval_db:
        for item in items:
            if not any(_m.matches(p, item['title']) for p in all_match_patterns):
                continue
            ev = _m.evaluate_release(item, series_id, _eval_db)
            results.append({
                'title':                 item['title'],
                'url':                   item['url'],
                'size_bytes':            item.get('size_bytes', 0),
                'size':                  _m.format_bytes(item.get('size_bytes', 0)),
                'seeders':               item.get('seeders', 0),
                'indexer':               item.get('indexer', ''),
                'protocol':              item.get('protocol', ''),
                'score':                 ev['score'],
                'status':                ev['status'],
                'rejections':            ev['rejections'],
                'custom_format_matches': ev['custom_format_matches'],
                'quality':               ev['quality'],
                'size_mb':               ev['size_mb'],
                'seen':                  item['url'] in seen_urls,
                'blocked':               item['url'] in blocked_urls,
            })

    _status_order = {'would_grab': 0, 'low_score': 1, 'rejected': 2}
    results.sort(key=lambda r: (_status_order.get(r['status'], 9), -r['score']))

    # Check Suwayomi DDL availability
    suwayomi_available = False
    from routers.suwayomi_ import get_suwayomi_client, _get_series_source
    if _get_series_source(series_id, dict(s)):
        with get_db() as _swy_db:
            suwayomi_available = bool(get_suwayomi_client(_swy_db))

    return JSONResponse({"results": results, "query": query,
                         "suwayomi_available": suwayomi_available})


@router.post("/api/series/{series_id}/volumes/{volume_id}/grab-ddl")
async def grab_volume_ddl(series_id: int, volume_id: int):
    """Trigger a Suwayomi DDL grab for a volume (called from interactive search modal)."""
    with get_db() as db:
        v = db.execute(
            "SELECT volume_num FROM volumes WHERE id=? AND series_id=?", (volume_id, series_id)
        ).fetchone()
    if not v or not v['volume_num']:
        return JSONResponse({"ok": False, "message": "Volume not found"}, status_code=404)
    from routers import suwayomi_ as _swy
    ok = await _swy.suwayomi_grab(series_id, float(v['volume_num']))
    if ok:
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False, "message": "DDL grab failed — check logs"}, status_code=500)


@router.post("/api/series/{series_id}/volumes/{volume_id}/grab-release")
async def grab_volume_release(series_id: int, volume_id: int, request: Request):
    import main as _m
    data     = await request.json()
    url      = data.get('url', '')
    title    = data.get('title', '')
    indexer  = data.get('indexer', '')
    protocol = data.get('protocol', 'torrent')
    size     = data.get('size_bytes', 0)
    if not url:
        return JSONResponse({"ok": False, "message": "No URL provided"})
    item = {'title': title, 'url': url, 'indexer': indexer,
            'protocol': protocol, 'size_bytes': size}
    ok = await _m.grab_item(item, series_id, respect_monitoring=False)
    return JSONResponse({"ok": ok, "message": "Grabbed" if ok else "Failed or already grabbed"})


# ── Rescan / metadata ─────────────────────────────────────────────────────────

@router.post("/series/{series_id}/rescan")
async def rescan_series(request: Request, series_id: int):
    import main as _m
    with get_db() as db:
        result = _m.rescan_series_folder(db, series_id)
    parts = []
    if result['found']:     parts.append(f"{result['found']} file(s) on disk")
    if result['recovered']: parts.append(f"{result['recovered']} marked downloaded")
    if result['missing']:   parts.append(f"{result['missing']} reset to wanted (files missing)")
    if result['lost']:      parts.append(f"{result['lost']} reset to wanted (grab lost)")
    if result.get('created'): parts.append(f"{result['created']} new stub(s) created from disk")
    msg = "Rescan: " + (", ".join(parts) if parts else "nothing changed")
    _m.log_event('rescan', msg, series_id)
    if request.headers.get("HX-Request") == "true":
        import json
        from fastapi.responses import Response as _Resp
        return _Resp(headers={"HX-Trigger": json.dumps({
            "showToast": {"msg": msg, "type": "success"}
        })})
    return RedirectResponse(f"/series/{series_id}", status_code=303)


@router.post("/api/series/{series_id}/reinject-metadata")
async def reinject_metadata(series_id: int):
    import main as _m
    with get_db() as db:
        s = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
        if not s:
            return JSONResponse({"ok": False, "message": "Series not found"})
        tags = [r['tag'] for r in db.execute(
            "SELECT tag FROM series_tags WHERE series_id=?", (series_id,)
        ).fetchall()]
        vols = db.execute(
            "SELECT volume_num, import_path FROM volumes"
            " WHERE series_id=? AND status='downloaded' AND import_path IS NOT NULL",
            (series_id,)
        ).fetchall()
        chaps = db.execute(
            "SELECT chapter_num, import_path FROM chapters"
            " WHERE series_id=? AND status='downloaded' AND import_path IS NOT NULL",
            (series_id,)
        ).fetchall()

    ok_count = skip_count = fail_count = 0
    for v in vols:
        if not os.path.isfile(v['import_path']):
            skip_count += 1; continue
        xml = _m.build_comicinfo_xml(dict(s), volume_num=v['volume_num'], tags=tags)
        if _m.inject_comicinfo(v['import_path'], xml):
            ok_count += 1
        else:
            fail_count += 1
    for c in chaps:
        if not os.path.isfile(c['import_path']):
            skip_count += 1; continue
        xml = _m.build_comicinfo_xml(dict(s), chapter_num=c['chapter_num'], tags=tags)
        if _m.inject_comicinfo(c['import_path'], xml):
            ok_count += 1
        else:
            fail_count += 1

    _m.log_event('metadata',
                 f"Re-injected ComicInfo.xml: {ok_count} updated, "
                 f"{skip_count} missing, {fail_count} skipped (non-CBZ)",
                 series_id)
    return JSONResponse({
        "ok": True, "updated": ok_count,
        "skipped_missing": skip_count, "skipped_format": fail_count,
    })


@router.post("/library/rescan")
async def rescan_all_series(request: Request):
    asyncio.create_task(_rescan_all_impl())
    if request.headers.get("HX-Request") == "true":
        import json
        from fastapi.responses import Response as _Resp
        return _Resp(headers={"HX-Trigger": json.dumps({
            "showToast": {"msg": "Library rescan started in background", "type": "success"}
        })})
    return RedirectResponse("/health", status_code=303)


@router.post("/series/{series_id}/mark-all-downloaded")
async def mark_all_grabbed_downloaded(request: Request, series_id: int):
    import main as _m
    with get_db() as db:
        cur = db.execute(
            "UPDATE volumes SET status='downloaded' WHERE series_id=? AND status='grabbed'"
            " AND volume_num IS NOT NULL",
            (series_id,)
        )
        marked = cur.rowcount
    _m.log_event('download_complete', "Manually marked all grabbed volumes as downloaded", series_id)
    if request.headers.get("HX-Request") == "true":
        from fastapi.responses import Response as _Resp
        return _Resp(headers={"HX-Refresh": "true"})
    return RedirectResponse(f"/series/{series_id}", status_code=303)


# ── Aliases & tags ────────────────────────────────────────────────────────────

@router.post("/series/{series_id}/aliases/add")
async def add_series_alias(request: Request, series_id: int, alias: str = Form("")):
    alias = alias.strip()
    if alias:
        with get_db() as db:
            db.execute(
                "INSERT OR IGNORE INTO series_aliases(series_id, alias) VALUES(?,?)",
                (series_id, alias)
            )
    if request.headers.get("HX-Request") == "true":
        with get_db() as db:
            aliases = db.execute(
                "SELECT * FROM series_aliases WHERE series_id=? ORDER BY alias", (series_id,)
            ).fetchall()
        return templates.TemplateResponse(request, "partials/alias_list.html",
                                          {"series_id": series_id, "aliases": aliases})
    return RedirectResponse(f"/series/{series_id}", status_code=303)


@router.post("/series/{series_id}/aliases/{alias_id}/delete")
async def delete_series_alias(request: Request, series_id: int, alias_id: int):
    with get_db() as db:
        db.execute(
            "DELETE FROM series_aliases WHERE id=? AND series_id=?", (alias_id, series_id)
        )
    if request.headers.get("HX-Request") == "true":
        with get_db() as db:
            aliases = db.execute(
                "SELECT * FROM series_aliases WHERE series_id=? ORDER BY alias", (series_id,)
            ).fetchall()
        return templates.TemplateResponse(request, "partials/alias_list.html",
                                          {"series_id": series_id, "aliases": aliases})
    return RedirectResponse(f"/series/{series_id}", status_code=303)


@router.post("/series/{series_id}/set-tags")
async def set_series_tags(request: Request, series_id: int, tags: str = Form("")):
    tag_list = [t.strip().lower() for t in tags.split(',') if t.strip()]
    with get_db() as db:
        db.execute(
            "UPDATE series SET tags=? WHERE id=?",
            (json.dumps(tag_list) if tag_list else None, series_id)
        )
    if request.headers.get("HX-Request") == "true":
        from fastapi.responses import Response as _Resp
        return _Resp(headers={"HX-Refresh": "true"})
    return RedirectResponse(f"/series/{series_id}", status_code=303)


# ── MangaDex / edition metadata ───────────────────────────────────────────────

@router.post("/series/{series_id}/refresh-mangadex")
async def refresh_series_mangadex(request: Request, series_id: int):
    import main as _m
    ok = await _m.refresh_mangadex_map(series_id)
    if ok:
        _m.backfill_pack_ranges()
        _m.log_event('refresh', "MangaDex chapter map refreshed and backfill applied", series_id)
    if request.headers.get("HX-Request") == "true":
        import json
        from fastapi.responses import Response as _Resp
        msg = "Chapter map refreshed" if ok else "No MangaDex data found"
        return _Resp(headers={"HX-Trigger": json.dumps({
            "showToast": {"msg": msg, "type": "success" if ok else "error"}
        })})
    return RedirectResponse(f"/series/{series_id}", status_code=303)


@router.post("/series/{series_id}/refresh-edition-metadata")
async def refresh_edition_metadata(series_id: int):
    import main as _m
    with get_db() as db:
        s = db.execute(
            "SELECT title, edition_type, vol_count_source FROM series WHERE id=?",
            (series_id,)
        ).fetchone()
    if not s:
        return JSONResponse({"ok": False, "error": "Series not found"}, status_code=404)
    edition_type = s['edition_type'] or 'standard'
    if edition_type not in _m._NON_STANDARD_STUB_EDITIONS:
        return JSONResponse({
            "ok": False,
            "error": f"Edition '{edition_type}' uses standard volume numbering"
        }, status_code=400)
    count = await _m.fetch_edition_volume_count(series_id, s['title'], edition_type)
    if count is None:
        return JSONResponse({"ok": False, "message": "Could not determine volume count from Google Books"})
    return JSONResponse({"ok": True, "total_volumes": count, "source": "google_books"})


# ── Chapter actions ───────────────────────────────────────────────────────────

@router.post("/series/{sid}/chapters/{cid}/toggle-monitor")
async def toggle_chapter_monitor(request: Request, sid: int, cid: int):
    with get_db() as db:
        ch = db.execute(
            "SELECT monitored, volume_id FROM chapters WHERE id=? AND series_id=?", (cid, sid)
        ).fetchone()
        if ch:
            db.execute("UPDATE chapters SET monitored=? WHERE id=?",
                       (0 if ch['monitored'] else 1, cid))
    if request.headers.get("HX-Request") == "true" and ch and ch['volume_id']:
        ctx = await _get_volume_row_ctx(sid, ch['volume_id'])
        return templates.TemplateResponse(request, "partials/volume_row.html", ctx)
    return RedirectResponse(f"/series/{sid}", status_code=303)


@router.post("/series/{sid}/chapters/{cid}/mark-downloaded")
async def mark_chapter_downloaded(request: Request, sid: int, cid: int):
    import main as _m
    with get_db() as db:
        ch = db.execute("SELECT chapter_num, volume_id FROM chapters WHERE id=?", (cid,)).fetchone()
        s  = db.execute("SELECT title FROM series WHERE id=?", (sid,)).fetchone()
        now_iso = datetime.utcnow().isoformat()
        # If linked to a volume, copy metadata from the sibling. Otherwise just
        # stamp status + imported_at so chapter rows don't stay sparse.
        if ch and ch['volume_id']:
            _sib = db.execute(
                "SELECT import_path, quality, torrent_name, indexer, protocol,"
                " client, release_group, size_bytes, download_id"
                " FROM volumes WHERE id=?",
                (ch['volume_id'],)
            ).fetchone()
            _sib = dict(_sib) if _sib else {}
            db.execute(
                "UPDATE chapters SET status='downloaded',"
                " imported_at=COALESCE(imported_at,?),"
                " import_path=COALESCE(import_path,?),"
                " quality=COALESCE(quality,?),"
                " torrent_name=COALESCE(torrent_name,?),"
                " indexer=COALESCE(indexer,?),"
                " protocol=COALESCE(protocol,?),"
                " client=COALESCE(client,?),"
                " release_group=COALESCE(release_group,?),"
                " size_bytes=COALESCE(NULLIF(size_bytes,0),?),"
                " download_id=COALESCE(download_id,?)"
                " WHERE id=? AND series_id=?",
                (now_iso,
                 _sib.get('import_path'), _sib.get('quality'),
                 _sib.get('torrent_name'), _sib.get('indexer'),
                 _sib.get('protocol'), _sib.get('client'),
                 _sib.get('release_group'), _sib.get('size_bytes'),
                 _sib.get('download_id'),
                 cid, sid)
            )
            _m._check_volume_completion(db, sid, ch['volume_id'])
        else:
            db.execute(
                "UPDATE chapters SET status='downloaded',"
                " imported_at=COALESCE(imported_at,?)"
                " WHERE id=? AND series_id=?",
                (now_iso, cid, sid)
            )
        if ch and s:
            ch_label = f"Ch {ch['chapter_num']}" if ch['chapter_num'] is not None else '—'
            _m.add_history(db, 'chapter_marked_downloaded', sid, s['title'], ch_label)
    if request.headers.get("HX-Request") == "true":
        if ch and ch['volume_id']:
            ctx = await _get_volume_row_ctx(sid, ch['volume_id'])
            return templates.TemplateResponse(request, "partials/volume_row.html", ctx)
        from fastapi.responses import Response as _Resp
        return _Resp(headers={"HX-Refresh": "true"})
    return RedirectResponse(f"/series/{sid}", status_code=303)


@router.post("/series/{sid}/chapters/{cid}/mark-wanted")
async def mark_chapter_wanted(request: Request, sid: int, cid: int):
    import main as _m
    with get_db() as db:
        ch = db.execute("SELECT chapter_num, volume_id FROM chapters WHERE id=?", (cid,)).fetchone()
        s  = db.execute("SELECT title FROM series WHERE id=?", (sid,)).fetchone()
        db.execute(
            "UPDATE chapters SET status='wanted', grabbed_at=NULL WHERE id=? AND series_id=?",
            (cid, sid)
        )
        if ch and s:
            ch_label = f"Ch {ch['chapter_num']}" if ch['chapter_num'] is not None else '—'
            _m.add_history(db, 'chapter_marked_wanted', sid, s['title'], ch_label)
    if request.headers.get("HX-Request") == "true" and ch and ch['volume_id']:
        ctx = await _get_volume_row_ctx(sid, ch['volume_id'])
        return templates.TemplateResponse(request, "partials/volume_row.html", ctx)
    return RedirectResponse(f"/series/{sid}", status_code=303)


@router.post("/series/{sid}/chapters/{cid}/grab")
async def grab_chapter_route(request: Request, sid: int, cid: int):
    with get_db() as db:
        s  = db.execute("SELECT * FROM series WHERE id=?", (sid,)).fetchone()
        ch = db.execute(
            "SELECT * FROM chapters WHERE id=? AND series_id=?", (cid, sid)
        ).fetchone()
    if not s or not ch:
        return RedirectResponse(with_flash(f"/series/{sid}", "No wanted chapters found", "info"), status_code=303)
    asyncio.create_task(_grab_chapter_task(sid, dict(s), dict(ch)))
    if request.headers.get("HX-Request") == "true":
        if ch['volume_id']:
            ctx = await _get_volume_row_ctx(sid, ch['volume_id'])
            return templates.TemplateResponse(request, "partials/volume_row.html", ctx)
        from fastapi.responses import Response as _Resp
        return _Resp(headers={"HX-Refresh": "true"})
    return RedirectResponse(with_flash(f"/series/{sid}", f"Grab queued for {len(chs)} chapters", "success"), status_code=303)


# ── Uncollected chapters ──────────────────────────────────────────────────────

@router.post("/series/{sid}/uncollected/toggle-monitor")
async def uncollected_toggle_monitor(request: Request, sid: int):
    with get_db() as db:
        current = db.execute(
            "SELECT monitored FROM chapters WHERE series_id=? AND volume_id IS NULL LIMIT 1", (sid,)
        ).fetchone()
        new_val = 0 if (current and current['monitored']) else 1
        db.execute(
            "UPDATE chapters SET monitored=? WHERE series_id=? AND volume_id IS NULL",
            (new_val, sid)
        )
    if request.headers.get("HX-Request") == "true":
        from fastapi.responses import Response as _Resp
        return _Resp(headers={"HX-Refresh": "true"})
    return RedirectResponse(with_flash(f"/series/{sid}", f"Search queued for {len(wanted)} volumes", "success"), status_code=303)


@router.post("/series/{sid}/uncollected/mark-downloaded")
async def uncollected_mark_downloaded(request: Request, sid: int):
    with get_db() as db:
        db.execute(
            "UPDATE chapters SET status='downloaded',"
            " imported_at=COALESCE(imported_at,?)"
            " WHERE series_id=? AND volume_id IS NULL",
            (datetime.utcnow().isoformat(), sid)
        )
    if request.headers.get("HX-Request") == "true":
        from fastapi.responses import Response as _Resp
        return _Resp(headers={"HX-Refresh": "true"})
    return RedirectResponse(f"/series/{sid}", status_code=303)


@router.post("/series/{sid}/uncollected/grab-all")
async def uncollected_grab_all(request: Request, sid: int):
    with get_db() as db:
        s   = db.execute("SELECT * FROM series WHERE id=?", (sid,)).fetchone()
        chs = db.execute(
            "SELECT * FROM chapters WHERE series_id=? AND volume_id IS NULL"
            " AND status='wanted' AND monitored=1",
            (sid,)
        ).fetchall()
    if not s or not chs:
        if request.headers.get("HX-Request") == "true":
            import json
            from fastapi.responses import Response as _Resp
            return _Resp(headers={"HX-Trigger": json.dumps({
                "showToast": {"msg": "No wanted chapters found", "type": "info"}
            })})
        return RedirectResponse(f"/series/{sid}", status_code=303)
    for ch in chs:
        asyncio.create_task(_grab_chapter_task(sid, dict(s), dict(ch)))
    if request.headers.get("HX-Request") == "true":
        import json
        from fastapi.responses import Response as _Resp
        return _Resp(headers={"HX-Trigger": json.dumps({
            "showToast": {"msg": f"Grab queued for {len(chs)} chapters", "type": "success"}
        })})
    return RedirectResponse(f"/series/{sid}", status_code=303)


# ── Quality upgrade trigger ───────────────────────────────────────────────────

@router.post("/series/{sid}/volumes/{vol_id}/trigger-upgrade")
async def trigger_volume_upgrade(
    request: Request, sid: int, vol_id: int,
    redirect_to: str = Form("/calendar")
):
    with get_db() as db:
        s = db.execute("SELECT * FROM series WHERE id=?", (sid,)).fetchone()
        v = db.execute("SELECT * FROM volumes WHERE id=? AND series_id=?", (vol_id, sid)).fetchone()
    if not s or not v or not v['volume_num']:
        if request.headers.get("HX-Request") == "true":
            ctx = await _get_volume_row_ctx(sid, vol_id)
            return templates.TemplateResponse(request, "partials/volume_row.html", ctx)
        return RedirectResponse(f"/series/{sid}", status_code=303)
    query = f"{s['search_pattern']} volume {vol_num_to_display(v['volume_num'])}"
    asyncio.create_task(_grab_volume_task(sid, s, v, query))
    if request.headers.get("HX-Request") == "true":
        ctx = await _get_volume_row_ctx(sid, vol_id)
        return templates.TemplateResponse(request, "partials/volume_row.html", ctx)
    safe_redirect = redirect_to if redirect_to.startswith('/') else f"/series/{sid}"
    return RedirectResponse(safe_redirect, status_code=303)


@router.post("/series/{sid}/grab-all-wanted")
async def grab_all_wanted_for_series(request: Request, sid: int):
    with get_db() as db:
        s      = db.execute("SELECT * FROM series WHERE id=?", (sid,)).fetchone()
        wanted = db.execute(
            "SELECT * FROM volumes WHERE series_id=? AND status='wanted' AND volume_num IS NOT NULL",
            (sid,)
        ).fetchall()
    if not s:
        return RedirectResponse("/wanted", status_code=303)
    for v in wanted:
        query = f"{s['search_pattern']} volume {vol_num_to_display(v['volume_num'])}"
        asyncio.create_task(_grab_volume_task(sid, dict(s), dict(v), query))
    if request.headers.get("HX-Request") == "true":
        import json
        from fastapi.responses import Response as _Resp
        return _Resp(headers={"HX-Trigger": json.dumps({
            "showToast": {"msg": f"Search queued for {len(wanted)} volumes", "type": "success"}
        })})
    return RedirectResponse(f"/series/{sid}", status_code=303)
