"""Indexers — DB-managed indexer configuration (Sonarr parity)."""
import httpx
import json
import time
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from routers._templates import templates

from shared import get_db, from_json, get_cfg, get_secret_health_summary
from security import (
    validate_outbound_url, UnsafeURLError,
    decrypt_secret_safe, encrypt_if_cipher_available,
)

router = APIRouter()


# ── Per-indexer backoff ──────────────────────────────────────────────────────
# When an indexer rate-limits (429), forbids (403), or returns 5xx, retrying
# at full speed on the next RSS/search cycle risks IP bans, especially on
# shared Prowlarr instances. indexer_backoff persists a per-indexer retry
# deadline so both RSS polls and manual searches honour the backoff.
#
# Honoured inputs:
#   - Retry-After header (HTTP-date or delta-seconds) — treated verbatim
#   - No Retry-After → exponential backoff: 60s, 120s, 240s, capped at 1h
#
# Success clears the row.

_BACKOFF_MIN   = 60      # seconds
_BACKOFF_MAX   = 3600    # 1 hour cap
_BACKOFF_BASE  = 2       # exponential base


def _parse_retry_after(raw: str | None) -> float | None:
    """Parse a Retry-After header value into 'seconds from now'.
    Returns None for unparseable values so the caller can fall back."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        import datetime as _dt
        dt = parsedate_to_datetime(raw)
        if dt is None:
            return None
        delta = (dt - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
        return max(0.0, delta)
    except Exception:
        return None


def _indexer_is_backed_off(indexer_id: int) -> tuple[bool, float]:
    """Return (is_backed_off, retry_after_epoch)."""
    with get_db() as db:
        row = db.execute(
            "SELECT retry_after FROM indexer_backoff WHERE indexer_id=?",
            (indexer_id,)
        ).fetchone()
    if not row:
        return False, 0.0
    deadline = float(row['retry_after'] or 0)
    return (time.time() < deadline), deadline


def _indexer_record_failure(indexer_id: int, *, status: int | None,
                            retry_after_header: str | None,
                            reason: str) -> float:
    """Record a failure, compute + persist the next retry deadline, return it."""
    hdr_seconds = _parse_retry_after(retry_after_header)
    with get_db() as db:
        row = db.execute(
            "SELECT consecutive_failures FROM indexer_backoff WHERE indexer_id=?",
            (indexer_id,)
        ).fetchone()
        failures = (row['consecutive_failures'] + 1) if row else 1
        if hdr_seconds is not None:
            delay = min(max(hdr_seconds, 1.0), _BACKOFF_MAX)
        else:
            delay = min(_BACKOFF_MIN * (_BACKOFF_BASE ** (failures - 1)), _BACKOFF_MAX)
        deadline = time.time() + delay
        db.execute(
            "INSERT INTO indexer_backoff"
            "(indexer_id, retry_after, consecutive_failures, last_status, last_reason, updated_at)"
            " VALUES(?, ?, ?, ?, ?, CURRENT_TIMESTAMP)"
            " ON CONFLICT(indexer_id) DO UPDATE SET"
            "   retry_after=excluded.retry_after,"
            "   consecutive_failures=excluded.consecutive_failures,"
            "   last_status=excluded.last_status,"
            "   last_reason=excluded.last_reason,"
            "   updated_at=CURRENT_TIMESTAMP",
            (indexer_id, deadline, failures, status, reason[:200])
        )
    return deadline


def _indexer_record_success(indexer_id: int) -> None:
    with get_db() as db:
        db.execute(
            "DELETE FROM indexer_backoff WHERE indexer_id=?", (indexer_id,)
        )


def _should_backoff_on_response(r: httpx.Response) -> tuple[bool, str]:
    """Return (should_backoff, reason)."""
    if r.status_code == 429:
        return True, 'rate limited (429)'
    if r.status_code == 403:
        return True, 'forbidden (403)'
    if 500 <= r.status_code < 600:
        return True, f'server error ({r.status_code})'
    return False, ''


def _row_decrypted(row) -> dict:
    """Row → dict with indexers.api_key decrypted (or '' if undecryptable).

    Plaintext values pass through. enc:v1: values are decrypted. Wrong-key
    / corrupt values log a WARNING naming the indexer and become empty
    string (the downstream integration sees "no key" and fails cleanly).
    """
    d = dict(row)
    d['api_key'] = decrypt_secret_safe(
        d.get('api_key'),
        field_name='indexers.api_key',
        context=d.get('name') or '?',
    )
    return d

INDEXER_TYPES = ["prowlarr", "torznab", "newznab"]

MANGA_CATEGORIES = [
    (7000, "Books/General"),
    (7010, "Books/Mags"),
    (7020, "Books/EBook"),
    (7030, "Books/Comics"),
    (7040, "Books/Technical"),
    (7050, "Books/Other"),
    (7060, "Books/Foreign"),
]


def _all_indexers(db):
    return db.execute("SELECT * FROM indexers ORDER BY priority, id").fetchall()


def _friendly_indexer_error(exc: Exception) -> str:
    msg = (str(exc) or type(exc).__name__).strip()
    low = msg.lower()
    if "name or service not known" in low or "could not resolve" in low or "nodename nor servname provided" in low:
        return "Could not resolve the host. Check the hostname."
    if "all connection attempts failed" in low:
        return "Connection failed. Check the host, port, and URL."
    if "connection refused" in low:
        return "Connection refused. Check that the indexer is running and reachable."
    if "timed out" in low or "timeout" in low:
        return "Connection timed out. Check reachability and TLS settings."
    return msg


# ── List ──────────────────────────────────────────────────────────────────────
@router.get("/indexers", response_class=HTMLResponse)
async def indexers_page(request: Request, saved: str = ""):
    with get_db() as db:
        indexers = _all_indexers(db)
        clients  = db.execute(
            "SELECT id, name FROM download_clients WHERE enabled=1 ORDER BY priority, id"
        ).fetchall()
        secret_health = get_secret_health_summary(db)
    return templates.TemplateResponse(request, "indexers.html", {
        "indexers":         indexers,
        "indexer_types":    INDEXER_TYPES,
        "manga_categories": MANGA_CATEGORIES,
        "clients":          [dict(c) for c in clients],
        "saved":            saved,
        "secret_health":    secret_health,
        "cfg":              {
            "rss_interval":      get_cfg("rss_interval",      "900"),
            "indexer_max_size":  get_cfg("indexer_max_size",  "0"),
            "indexer_min_age":   get_cfg("indexer_min_age",   "0"),
            "backlog_search_days": get_cfg("backlog_search_days", "30"),
        },
    })


# ── Options ───────────────────────────────────────────────────────────────────
@router.post("/indexers/options")
async def save_indexer_options(
    rss_interval:        str = Form("900"),
    indexer_max_size:    str = Form("0"),
    indexer_min_age:     str = Form("0"),
    backlog_search_days: str = Form("30"),
):
    with get_db() as db:
        for k, v in {
            'rss_interval':        rss_interval        if rss_interval.isdigit()        else '900',
            'indexer_max_size':    indexer_max_size    if indexer_max_size.isdigit()    else '0',
            'indexer_min_age':     indexer_min_age     if indexer_min_age.isdigit()     else '0',
            'backlog_search_days': backlog_search_days if backlog_search_days.isdigit() else '30',
        }.items():
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, v))
    return RedirectResponse("/indexers?saved=1", status_code=303)


# ── Create ────────────────────────────────────────────────────────────────────
@router.post("/indexers")
async def create_indexer(
    name: str = Form(...),
    type: str = Form("prowlarr"),
    url: str = Form(""),
    api_key: str = Form(""),
    priority: int = Form(25),
    enabled: int = Form(1),
    categories: str = Form("7000,7010,7020"),
    settings: str = Form("{}"),
    client_id: str = Form(""),
    min_seeders: int = Form(0),
    seed_ratio: float = Form(0.0),
):
    cats = json.dumps([int(c.strip()) for c in categories.split(',') if c.strip().isdigit()])
    cid  = int(client_id) if client_id.strip().isdigit() else None
    stored_key = encrypt_if_cipher_available(api_key.strip()) if api_key.strip() else None
    with get_db() as db:
        db.execute(
            "INSERT INTO indexers(name,type,url,api_key,priority,enabled,categories,settings,"
            " client_id,min_seeders,seed_ratio)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (name.strip(), type, url.strip() or None, stored_key,
             priority, enabled, cats, settings or '{}', cid, min_seeders, seed_ratio)
        )
    return RedirectResponse("/indexers", status_code=303)


# ── Edit ──────────────────────────────────────────────────────────────────────
@router.post("/indexers/{indexer_id}")
async def edit_indexer(
    indexer_id: int,
    name: str = Form(...),
    type: str = Form("prowlarr"),
    url: str = Form(""),
    api_key: str = Form(""),
    priority: int = Form(25),
    enabled: int = Form(1),
    categories: str = Form("7000,7010,7020"),
    settings: str = Form("{}"),
    keep_api_key: int = Form(0),
    client_id: str = Form(""),
    min_seeders: int = Form(0),
    seed_ratio: float = Form(0.0),
):
    cats = json.dumps([int(c.strip()) for c in categories.split(',') if c.strip().isdigit()])
    cid  = int(client_id) if client_id.strip().isdigit() else None
    with get_db() as db:
        if keep_api_key:
            db.execute(
                "UPDATE indexers SET name=?,type=?,url=?,priority=?,enabled=?,categories=?,settings=?,"
                " client_id=?,min_seeders=?,seed_ratio=? WHERE id=?",
                (name.strip(), type, url.strip() or None, priority, enabled, cats, settings or '{}',
                 cid, min_seeders, seed_ratio, indexer_id)
            )
        else:
            stored_key = encrypt_if_cipher_available(api_key.strip()) if api_key.strip() else None
            db.execute(
                "UPDATE indexers SET name=?,type=?,url=?,api_key=?,priority=?,enabled=?,"
                " categories=?,settings=?,client_id=?,min_seeders=?,seed_ratio=? WHERE id=?",
                (name.strip(), type, url.strip() or None, stored_key,
                 priority, enabled, cats, settings or '{}', cid, min_seeders, seed_ratio, indexer_id)
            )
    return RedirectResponse("/indexers", status_code=303)


# ── Delete ────────────────────────────────────────────────────────────────────
@router.post("/indexers/{indexer_id}/delete")
async def delete_indexer(indexer_id: int):
    with get_db() as db:
        db.execute("DELETE FROM indexers WHERE id=?", (indexer_id,))
    return RedirectResponse("/indexers", status_code=303)


# ── Prowlarr sub-indexer visibility ───────────────────────────────────────────
async def _list_prowlarr_subs_for_ui(url: str, key: str, cats: list) -> list[dict]:
    """Return the FULL Prowlarr sub-indexer list with status flags for UI display.

    Differs from `_get_prowlarr_indexers` (which filters out disabled/non-manga
    sub-indexers before returning) — this helper preserves the entire list and
    annotates each entry with `enable`, `manga_compatible`, and `will_be_polled`
    so the UI can show why each sub-indexer is or isn't being used.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as cli:
            r = await cli.get(f"{url}/api/v1/indexer", headers={"X-Api-Key": key})
        if r.status_code != 200:
            return []
        manga_cats = set(cats)
        result = []
        for idx in r.json():
            idx_cats_int = {
                int(c.get('id', 0))
                for c in idx.get('capabilities', {}).get('categories', [])
            }
            # Empty caps = unknown caps → benefit of the doubt (mirrors
            # _get_prowlarr_indexers behavior).
            manga_compatible = bool(idx_cats_int & manga_cats) if idx_cats_int else True
            enabled = idx.get('enable', True)
            result.append({
                'id': idx['id'],
                'name': idx.get('name', str(idx['id'])),
                'enable': enabled,
                'protocol': idx.get('protocol', 'torrent'),
                'categories': sorted(idx_cats_int),
                'manga_compatible': manga_compatible,
                'will_be_polled': enabled and manga_compatible,
            })
        # Sort: polled first (so the active set is at top), then alpha
        result.sort(key=lambda r: (not r['will_be_polled'], r['name'].lower()))
        return result
    except Exception as e:
        print(f"[Prowlarr] sub-indexer list failed for UI: {e}")
        return []


@router.get("/api/indexers/{indexer_id}/prowlarr-subs", response_class=HTMLResponse)
async def prowlarr_sub_indexers(request: Request, indexer_id: int):
    """Return the Prowlarr sub-indexer list as a rendered partial, for HTMX
    progressive disclosure on the indexers page.

    UX gap this closes: Mangarr stores ONE indexer row per Prowlarr instance,
    so the indexers page only shows 'Prowlarr' as a single entry. Operators
    debugging 'is X being polled?' had to bounce to Prowlarr's own UI to check.
    This endpoint surfaces the live sub-indexer state inline."""
    with get_db() as db:
        idx = db.execute("SELECT * FROM indexers WHERE id=?", (indexer_id,)).fetchone()
    if not idx:
        return templates.TemplateResponse(
            request, "partials/prowlarr_subs.html",
            {"error": "Indexer not found.", "subs": [], "indexer": None},
            status_code=404,
        )
    if idx['type'] != 'prowlarr':
        return templates.TemplateResponse(
            request, "partials/prowlarr_subs.html",
            {
                "error": f"Sub-indexer listing is only available for Prowlarr-type indexers (this one is `{idx['type']}`).",
                "subs": [], "indexer": dict(idx),
            },
        )

    decrypted = _row_decrypted(idx)
    cats = from_json(decrypted.get('categories'), [7000, 7010, 7020])
    url = (decrypted.get('url') or '').rstrip('/')
    key = decrypted.get('api_key') or ''
    if not url:
        return templates.TemplateResponse(
            request, "partials/prowlarr_subs.html",
            {"error": "No URL configured for this Prowlarr indexer.",
             "subs": [], "indexer": dict(idx)},
        )

    subs = await _list_prowlarr_subs_for_ui(url, key, cats)
    return templates.TemplateResponse(
        request, "partials/prowlarr_subs.html",
        {"subs": subs, "indexer": dict(idx), "manga_cats": cats, "error": None},
    )


# ── Test ──────────────────────────────────────────────────────────────────────
@router.post("/api/indexers/{indexer_id}/test")
async def test_indexer(indexer_id: int):
    with get_db() as db:
        idx = db.execute("SELECT * FROM indexers WHERE id=?", (indexer_id,)).fetchone()
    if not idx:
        return JSONResponse({"ok": False, "message": "Indexer not found"})
    ok, msg = await _test_indexer(_row_decrypted(idx))
    return JSONResponse({"ok": ok, "message": msg})


@router.post("/api/indexers/test-form")
async def test_indexer_form(
    name: str = Form("Unsaved indexer"),
    type: str = Form("prowlarr"),
    url: str = Form(""),
    api_key: str = Form(""),
    categories: str = Form("7000,7010,7020"),
    settings: str = Form("{}"),
    client_id: str = Form(""),
    min_seeders: int = Form(0),
    seed_ratio: float = Form(0.0),
):
    idx = {
        "name": name.strip() or "Unsaved indexer",
        "type": type,
        "url": url.strip() or None,
        "api_key": api_key.strip(),
        "categories": categories,
        "settings": settings or "{}",
        "client_id": int(client_id) if client_id.strip().isdigit() else None,
        "min_seeders": min_seeders,
        "seed_ratio": seed_ratio,
    }
    ok, msg = await _test_indexer(idx)
    return JSONResponse({"ok": ok, "message": msg})


async def _test_indexer(idx: dict) -> tuple[bool, str]:
    t   = idx['type']
    url = (idx['url'] or '').rstrip('/')
    key = idx['api_key'] or ''
    if not url:
        return False, "No URL configured"
    try:
        # Indexers commonly live on a LAN (docker network or local subnet).
        validate_outbound_url(url, allow_private=True)
    except UnsafeURLError as e:
        return False, f"URL rejected: {e}"
    try:
        if t == 'prowlarr':
            async with httpx.AsyncClient(timeout=10) as cli:
                r = await cli.get(f"{url}/api/v1/system/status",
                                  headers={"X-Api-Key": key})
            if r.status_code == 200:
                return True, f"Prowlarr {r.json().get('version', '?')}"
            return False, f"HTTP {r.status_code}"

        elif t in ('torznab', 'newznab'):
            params = {'t': 'caps', 'apikey': key}
            async with httpx.AsyncClient(timeout=10) as cli:
                r = await cli.get(f"{url}/api", params=params)
            if r.status_code == 200:
                return True, f"{t.title()} endpoint reachable"
            return False, f"HTTP {r.status_code}"

        return False, f"Unsupported indexer type: {t}"
    except Exception as e:
        return False, _friendly_indexer_error(e)


# ── Fetch RSS from all enabled indexers ──────────────────────────────────────
async def fetch_all_rss(db) -> list[dict]:
    """
    Fetch RSS from all enabled indexers and return deduplicated list of items.
    Uses the indexer-specific fetch logic.
    Results are sorted by indexer priority (lower number = higher priority).
    Per-indexer min_seeders, global indexer_max_size and indexer_min_age filters applied.
    """
    indexers = db.execute("SELECT * FROM indexers WHERE enabled=1 ORDER BY priority").fetchall()
    if not indexers:
        return []

    max_size_mb  = int(get_cfg('indexer_max_size', '0'))
    min_age_min  = int(get_cfg('indexer_min_age',  '0'))
    max_size_bytes = max_size_mb * 1024 * 1024 if max_size_mb > 0 else 0

    import asyncio
    idx_list = [_row_decrypted(idx) for idx in indexers]
    results  = await asyncio.gather(*[_fetch_rss_for_indexer(idx) for idx in idx_list])

    seen: set[str] = set()
    all_items: list[dict] = []
    # iterate in priority order (already ordered by query)
    for idx, batch in zip(idx_list, results):
        min_seeders       = idx.get('min_seeders') or 0
        preferred_client  = idx.get('client_id')
        for item in batch:
            if item['url'] in seen:
                continue
            # Per-indexer seeders filter (torrent only)
            if item.get('protocol') == 'torrent' and min_seeders > 0:
                if (item.get('seeders') or 0) < min_seeders:
                    continue
            # Global max size
            if max_size_bytes > 0 and (item.get('size_bytes') or 0) > max_size_bytes:
                continue
            # Global min age (item must carry 'age_minutes' if available; skip check if absent)
            if min_age_min > 0 and 'age_minutes' in item:
                if (item['age_minutes'] or 0) < min_age_min:
                    continue
            seen.add(item['url'])
            if preferred_client:
                item['preferred_client_id'] = preferred_client
            all_items.append(item)
    return all_items


async def _fetch_rss_for_indexer(idx: dict) -> list[dict]:
    """Fetch RSS for a single indexer."""
    t = idx['type']
    cats = from_json(idx.get('categories'), [7000, 7010, 7020])
    url  = (idx['url'] or '').rstrip('/')
    key  = idx['api_key'] or ''
    name = idx['name']
    idx_id = idx['id']

    if not url:
        return []

    # Skip if backoff deadline hasn't passed
    is_off, deadline = _indexer_is_backed_off(idx_id)
    if is_off:
        wait_s = int(deadline - time.time())
        print(f"[Indexer:{name}] skipping RSS poll — backoff active ({wait_s}s left)")
        return []

    try:
        # LAN indexers permitted; loopback/link-local/etc. still blocked.
        validate_outbound_url(url, allow_private=True)
    except UnsafeURLError as e:
        print(f"[Indexer:{name}] URL rejected: {e}")
        return []

    try:
        if t == 'prowlarr':
            # Use Prowlarr per-indexer RSS
            sub_indexers = await _get_prowlarr_indexers(url, key, cats)
            import asyncio as _asyncio
            batches = await _asyncio.gather(*[
                _fetch_prowlarr_rss(url, key, iid, iname, proto, cats)
                for iid, iname, proto in sub_indexers
            ])
            items = []
            for b in batches:
                items.extend(b)
            # Consider 'success' when we got any meaningful response — the
            # sub-indexer calls handle their own failures, but an overall
            # no-error path clears the parent's backoff counter.
            _indexer_record_success(idx_id)
            return items

        elif t in ('torznab', 'newznab'):
            cat_str = ','.join(str(c) for c in cats)
            async with httpx.AsyncClient(timeout=20) as cli:
                r = await cli.get(f"{url}/api",
                                  params={'t': 'search', 'cat': cat_str, 'apikey': key, 'q': ''})
            should_off, reason = _should_backoff_on_response(r)
            if should_off:
                dl = _indexer_record_failure(
                    idx_id,
                    status=r.status_code,
                    retry_after_header=r.headers.get('Retry-After'),
                    reason=reason,
                )
                print(f"[Indexer:{name}] backoff set — {reason}; next retry at "
                      f"{int(dl)} ({int(dl - time.time())}s from now)")
                return []
            _indexer_record_success(idx_id)
            return _parse_torznab_rss(r.text, name, 'torrent' if t == 'torznab' else 'nzb')

    except Exception as e:
        print(f"[Indexer:{name}] RSS error: {e}")
        # Unknown failure — increment counter but with shorter backoff
        _indexer_record_failure(
            idx_id, status=None, retry_after_header=None,
            reason=f'{type(e).__name__}: {str(e)[:120]}'
        )
    return []


async def _get_prowlarr_indexers(url: str, key: str, cats: list) -> list[tuple]:
    """Get list of (id, name, protocol) from Prowlarr."""
    try:
        async with httpx.AsyncClient(timeout=10) as cli:
            r = await cli.get(f"{url}/api/v1/indexer", headers={"X-Api-Key": key})
        indexers = r.json() if r.status_code == 200 else []
        result = []
        for idx in indexers:
            if not idx.get('enable', True):
                continue
            idx_cats = {int(c.get('id', 0)) for c in idx.get('capabilities', {}).get('categories', [])}
            if idx_cats and not (idx_cats & set(cats)):
                continue
            proto = 'torrent' if idx.get('protocol', 'torrent').lower() == 'torrent' else 'nzb'
            result.append((idx['id'], idx.get('name', str(idx['id'])), proto))
        return result
    except Exception as e:
        print(f"[Prowlarr] Failed to list indexers: {e}")
        return []


async def _fetch_prowlarr_rss(url, key, indexer_id, name, protocol, cats) -> list[dict]:
    cat_str = ','.join(str(c) for c in cats)
    try:
        async with httpx.AsyncClient(timeout=30) as cli:
            r = await cli.get(
                f"{url}/api/v1/indexer/{indexer_id}/newznab",
                headers={"X-Api-Key": key},
                params={'t': 'search', 'cat': cat_str, 'q': ''}
            )
        return _parse_torznab_rss(r.text, name, protocol)
    except Exception as e:
        print(f"[Prowlarr:{name}] RSS error: {e}")
        return []



def _parse_torznab_rss(xml_text: str, indexer: str, default_protocol: str = 'torrent') -> list[dict]:
    from defusedxml.ElementTree import fromstring as _safe_fromstring
    items = []
    ns = {'torznab': 'http://torznab.com/api/2015/feed',
          'newznab': 'http://www.newznab.com/DTD/2010/feeds/attributes/'}
    try:
        root = _safe_fromstring(xml_text)
    except Exception:
        return items

    def _attr(item, name):
        for ns_url in ns.values():
            el = item.find(f'{{{ns_url}}}attr[@name="{name}"]')
            if el is not None:
                return el.get('value', '')
        return ''

    for item in root.findall('.//item'):
        title = item.findtext('title', '').strip()
        link  = item.findtext('link',  '').strip()
        enclosure = item.find('enclosure')
        if not link and enclosure is not None:
            link = enclosure.get('url', '')
        dl_url = _attr(item, 'downloadUrl') or _attr(item, 'magnetUrl') or link
        if not dl_url:
            continue
        proto_raw = _attr(item, 'downloadProtocol') or default_protocol
        protocol = 'nzb' if proto_raw.lower() == 'usenet' else 'torrent'
        size_raw = _attr(item, 'size') or (enclosure.get('length', '0') if enclosure is not None else '0')
        try:
            size_bytes = int(size_raw)
        except Exception:
            size_bytes = 0
        items.append({
            'title':      title,
            'url':        dl_url,
            'size_bytes': size_bytes,
            'seeders':    int(_attr(item, 'seeders') or 0),
            'indexer':    indexer,
            'protocol':   protocol,
        })
    return items



# ── Search across all enabled indexers ───────────────────────────────────────
async def search_all_indexers(db, query: str) -> list[dict]:
    """
    Search across all enabled indexers and return deduplicated results.
    Results are sorted by indexer priority (lower number = higher priority).
    Per-indexer min_seeders and global indexer_max_size filters applied.
    """
    indexers = db.execute("SELECT * FROM indexers WHERE enabled=1 ORDER BY priority").fetchall()
    if not indexers:
        return []

    max_size_mb    = int(get_cfg('indexer_max_size', '0'))
    max_size_bytes = max_size_mb * 1024 * 1024 if max_size_mb > 0 else 0

    import asyncio
    idx_list = [_row_decrypted(idx) for idx in indexers]
    results  = await asyncio.gather(*[_search_indexer(idx, query) for idx in idx_list])

    seen: set[str] = set()
    all_items: list[dict] = []
    for idx, batch in zip(idx_list, results):
        min_seeders      = idx.get('min_seeders') or 0
        preferred_client = idx.get('client_id')
        for item in batch:
            if item['url'] in seen:
                continue
            if item.get('protocol') == 'torrent' and min_seeders > 0:
                if (item.get('seeders') or 0) < min_seeders:
                    continue
            if max_size_bytes > 0 and (item.get('size_bytes') or 0) > max_size_bytes:
                continue
            seen.add(item['url'])
            if preferred_client:
                item['preferred_client_id'] = preferred_client
            all_items.append(item)
    return all_items


async def _search_indexer(idx: dict, query: str) -> list[dict]:
    t    = idx['type']
    url  = (idx['url'] or '').rstrip('/')
    key  = idx['api_key'] or ''
    cats = from_json(idx.get('categories'), [7000, 7010, 7020])
    name = idx['name']
    idx_id = idx['id']
    cat_str = ','.join(str(c) for c in cats)

    is_off, deadline = _indexer_is_backed_off(idx_id)
    if is_off:
        wait_s = int(deadline - time.time())
        print(f"[Indexer:{name}] skipping search — backoff active ({wait_s}s left)")
        return []

    try:
        if t == 'prowlarr':
            async with httpx.AsyncClient(timeout=30) as cli:
                r = await cli.get(
                    f"{url}/api/v1/search",
                    headers={"X-Api-Key": key},
                    params={'query': query, 'categories': cats, 'type': 'search'}
                )
            should_off, reason = _should_backoff_on_response(r)
            if should_off:
                _indexer_record_failure(
                    idx_id, status=r.status_code,
                    retry_after_header=r.headers.get('Retry-After'),
                    reason=reason,
                )
                return []
            if r.status_code == 200:
                _indexer_record_success(idx_id)
                return _parse_prowlarr_response(r.json(), name)

        elif t in ('torznab', 'newznab'):
            proto = 'torrent' if t == 'torznab' else 'nzb'
            async with httpx.AsyncClient(timeout=20) as cli:
                r = await cli.get(f"{url}/api",
                                  params={'t': 'search', 'q': query, 'cat': cat_str, 'apikey': key})
            should_off, reason = _should_backoff_on_response(r)
            if should_off:
                _indexer_record_failure(
                    idx_id, status=r.status_code,
                    retry_after_header=r.headers.get('Retry-After'),
                    reason=reason,
                )
                return []
            _indexer_record_success(idx_id)
            return _parse_torznab_rss(r.text, name, proto)

    except Exception as e:
        print(f"[Indexer:{name}] search error: {e}")
        _indexer_record_failure(
            idx_id, status=None, retry_after_header=None,
            reason=f'{type(e).__name__}: {str(e)[:120]}'
        )
    return []


def _parse_prowlarr_response(data: list, indexer_name: str = '') -> list[dict]:
    results = []
    for item in data:
        raw_proto = (item.get('protocol') or 'torrent').lower()
        protocol  = 'nzb' if raw_proto == 'usenet' else 'torrent'
        dl_url    = item.get('downloadUrl') or item.get('magnetUrl', '')
        if not dl_url:
            continue
        results.append({
            'title':      item.get('title', ''),
            'url':        dl_url,
            'size_bytes': item.get('size', 0),
            'seeders':    item.get('seeders', 0),
            'indexer':    item.get('indexer') or indexer_name,
            'protocol':   protocol,
        })
    return results
