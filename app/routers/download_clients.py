"""Download Clients — multiple DB-managed download clients (Sonarr parity)."""
import json
import time
import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from routers._templates import templates

from shared import get_db, from_json

# Circuit breaker: track consecutive failures per client id
# Structure: {client_id: {'failures': int, 'open_until': float}}
_circuit: dict[int, dict] = {}
_CB_THRESHOLD = 3      # open after this many consecutive failures
_CB_TIMEOUT   = 300    # stay open for 5 minutes


def _cb_is_open(client_id: int) -> bool:
    """Return True if circuit is open (client should be skipped)."""
    state = _circuit.get(client_id)
    if not state:
        return False
    if state['failures'] >= _CB_THRESHOLD:
        if time.time() < state.get('open_until', 0):
            return True
        # Timeout elapsed — half-open: allow one attempt
        state['failures'] = _CB_THRESHOLD - 1  # reset to one below threshold
    return False


def _cb_record_success(client_id: int):
    _circuit.pop(client_id, None)


def _cb_record_failure(client_id: int):
    state = _circuit.setdefault(client_id, {'failures': 0, 'open_until': 0})
    state['failures'] += 1
    if state['failures'] >= _CB_THRESHOLD:
        state['open_until'] = time.time() + _CB_TIMEOUT


def client_base_url(c: dict) -> str:
    """Return the full base URL for a download client, merging host + port.

    If the host field already embeds a port (e.g. http://host:8080/path) the
    stored ``port`` column is ignored so we never produce a double-port URL.
    Strips any trailing slash.
    """
    from urllib.parse import urlparse, urlunparse
    host = (c.get('host') or '').rstrip('/')
    port = c.get('port')
    if host and port:
        parsed = urlparse(host)
        if not parsed.port:
            # Only the hostname is in the URL — append the port
            netloc = f"{parsed.hostname}:{port}"
            parsed = parsed._replace(netloc=netloc)
            host = urlunparse(parsed).rstrip('/')
    return host

router = APIRouter()

CLIENT_TYPES = ["qbittorrent", "sabnzbd", "nzbget", "deluge", "transmission", "rtorrent", "blackhole", "suwayomi"]


def _all_clients(db):
    clients = db.execute("SELECT * FROM download_clients ORDER BY priority, id").fetchall()
    result = []
    for c in clients:
        tags = db.execute(
            "SELECT tag FROM download_client_tags WHERE client_id=?", (c['id'],)
        ).fetchall()
        result.append({**dict(c), 'tags': [t['tag'] for t in tags]})
    return result


# ── List ──────────────────────────────────────────────────────────────────────
@router.get("/download-clients", response_class=HTMLResponse)
async def download_clients_page(request: Request):
    with get_db() as db:
        clients = _all_clients(db)
        mappings = db.execute(
            "SELECT * FROM remote_path_mappings ORDER BY id"
        ).fetchall()
        mappings = [dict(m) for m in mappings]
        def _opt(key):
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return (row['value'] if row else '0') == '1'
        options = {
            'completed_download_handling':   _opt('completed_download_handling'),
            'failed_download_handling':      _opt('failed_download_handling'),
            'redownload_failed_interactive': _opt('redownload_failed_interactive'),
        }
    return templates.TemplateResponse(request, "download_clients.html", {
        "clients":      clients,
        "client_types": CLIENT_TYPES,
        "mappings":     mappings,
        "options":      options,
        "saved":        request.query_params.get("saved") == "1",
    })


# ── Create ────────────────────────────────────────────────────────────────────
@router.post("/download-clients")
async def create_download_client(
    name: str = Form(...),
    type: str = Form(...),
    host: str = Form(""),
    port: int = Form(0),
    use_ssl: int = Form(0),
    url_base: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    category: str = Form("manga"),
    post_import_category: str = Form(""),
    recent_priority: str = Form("last"),
    older_priority: str = Form("last"),
    initial_state: str = Form("normal"),
    sequential_order: int = Form(0),
    first_last_first: int = Form(0),
    content_layout: str = Form("original"),
    priority: int = Form(1),
    enabled: int = Form(1),
    remove_completed: int = Form(0),
    remove_failed: int = Form(0),
    tags: str = Form(""),
    download_path: str = Form(""),
    merge_chapters: int = Form(0),
):
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO download_clients(name,type,host,port,use_ssl,url_base,username,password,"
            " category,post_import_category,recent_priority,older_priority,initial_state,"
            " sequential_order,first_last_first,content_layout,priority,enabled,remove_completed,remove_failed,"
            " download_path,merge_chapters)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name.strip(), type, host.strip(), port or None, use_ssl, url_base.strip() or None,
             username.strip() or None, password or None, category.strip() or 'manga',
             post_import_category.strip() or None, recent_priority, older_priority,
             initial_state, sequential_order, first_last_first, content_layout,
             priority, enabled, remove_completed, remove_failed,
             download_path.strip() or None, merge_chapters)
        )
        cid = cur.lastrowid
        for tag in [t.strip() for t in tags.split(',') if t.strip()]:
            db.execute("INSERT OR IGNORE INTO download_client_tags(client_id,tag) VALUES(?,?)", (cid, tag))
    return RedirectResponse("/download-clients", status_code=303)


# ── Remote Path Mappings ──────────────────────────────────────────────────────
# NOTE: These literal-path routes must appear BEFORE /download-clients/{client_id}
# so FastAPI does not treat "remote-path-mappings" or "options" as an int client_id.

@router.post("/download-clients/remote-path-mappings")
async def create_remote_path_mapping(
    host: str = Form(""),
    remote_path: str = Form(...),
    local_path: str = Form(...),
):
    with get_db() as db:
        db.execute(
            "INSERT INTO remote_path_mappings(host, remote_path, local_path) VALUES(?,?,?)",
            (host.strip(), remote_path.strip(), local_path.strip())
        )
    return RedirectResponse("/download-clients", status_code=303)


@router.post("/download-clients/remote-path-mappings/{mapping_id}/delete")
async def delete_remote_path_mapping(mapping_id: int):
    with get_db() as db:
        db.execute("DELETE FROM remote_path_mappings WHERE id=?", (mapping_id,))
    return RedirectResponse("/download-clients", status_code=303)


# ── Download Client Options ───────────────────────────────────────────────────
@router.post("/download-clients/options")
async def save_download_client_options(
    completed_download_handling: str = Form("0"),
    failed_download_handling: str = Form("0"),
    redownload_failed_interactive: str = Form("0"),
):
    with get_db() as db:
        for k, v in {
            'completed_download_handling':    '1' if completed_download_handling == '1' else '0',
            'failed_download_handling':       '1' if failed_download_handling == '1' else '0',
            'redownload_failed_interactive':  '1' if redownload_failed_interactive == '1' else '0',
        }.items():
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, v))
    return RedirectResponse("/download-clients?saved=1", status_code=303)


# ── Edit ──────────────────────────────────────────────────────────────────────
@router.post("/download-clients/{client_id}")
async def edit_download_client(
    client_id: int,
    name: str = Form(...),
    type: str = Form(...),
    host: str = Form(""),
    port: int = Form(0),
    use_ssl: int = Form(0),
    url_base: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    category: str = Form("manga"),
    post_import_category: str = Form(""),
    recent_priority: str = Form("last"),
    older_priority: str = Form("last"),
    initial_state: str = Form("normal"),
    sequential_order: int = Form(0),
    first_last_first: int = Form(0),
    content_layout: str = Form("original"),
    priority: int = Form(1),
    enabled: int = Form(1),
    remove_completed: int = Form(0),
    remove_failed: int = Form(0),
    tags: str = Form(""),
    keep_password: int = Form(0),
    download_path: str = Form(""),
    merge_chapters: int = Form(0),
):
    _shared = (name.strip(), type, host.strip(), port or None, use_ssl, url_base.strip() or None,
               username.strip() or None, category.strip() or 'manga',
               post_import_category.strip() or None, recent_priority, older_priority,
               initial_state, sequential_order, first_last_first, content_layout,
               priority, enabled, remove_completed, remove_failed,
               download_path.strip() or None, merge_chapters, client_id)
    with get_db() as db:
        if keep_password:
            db.execute(
                "UPDATE download_clients SET name=?,type=?,host=?,port=?,use_ssl=?,url_base=?,"
                " username=?,category=?,post_import_category=?,recent_priority=?,older_priority=?,"
                " initial_state=?,sequential_order=?,first_last_first=?,content_layout=?,"
                " priority=?,enabled=?,remove_completed=?,remove_failed=?,"
                " download_path=?,merge_chapters=? WHERE id=?",
                _shared
            )
        else:
            db.execute(
                "UPDATE download_clients SET name=?,type=?,host=?,port=?,use_ssl=?,url_base=?,"
                " username=?,password=?,category=?,post_import_category=?,recent_priority=?,"
                " older_priority=?,initial_state=?,sequential_order=?,first_last_first=?,"
                " content_layout=?,priority=?,enabled=?,remove_completed=?,remove_failed=?,"
                " download_path=?,merge_chapters=? WHERE id=?",
                (name.strip(), type, host.strip(), port or None, use_ssl, url_base.strip() or None,
                 username.strip() or None, password or None, category.strip() or 'manga',
                 post_import_category.strip() or None, recent_priority, older_priority,
                 initial_state, sequential_order, first_last_first, content_layout,
                 priority, enabled, remove_completed, remove_failed,
                 download_path.strip() or None, merge_chapters, client_id)
            )
        db.execute("DELETE FROM download_client_tags WHERE client_id=?", (client_id,))
        for tag in [t.strip() for t in tags.split(',') if t.strip()]:
            db.execute("INSERT OR IGNORE INTO download_client_tags(client_id,tag) VALUES(?,?)", (client_id, tag))
    return RedirectResponse("/download-clients", status_code=303)


# ── Delete ────────────────────────────────────────────────────────────────────
@router.post("/download-clients/{client_id}/delete")
async def delete_download_client(client_id: int):
    with get_db() as db:
        db.execute("DELETE FROM download_clients WHERE id=?", (client_id,))
    return RedirectResponse("/download-clients", status_code=303)


# ── Manual circuit breaker reset ──────────────────────────────────────────────
@router.post("/api/download-clients/{client_id}/reset-circuit")
async def reset_circuit_breaker(client_id: int):
    """Force-close the circuit breaker for a client. Useful when a transient
    error tripped it and automated recovery is slow (5-minute timeout)."""
    _circuit.pop(client_id, None)
    return JSONResponse({"ok": True, "message": "Circuit breaker reset"})


@router.post("/api/download-clients/reset-all-circuits")
async def reset_all_circuit_breakers():
    """Clear every circuit breaker state — used at startup and for diagnostics."""
    _circuit.clear()
    return JSONResponse({"ok": True, "message": "All circuit breakers reset"})


# ── Test ──────────────────────────────────────────────────────────────────────
@router.post("/api/download-clients/{client_id}/test")
async def test_download_client(client_id: int):
    with get_db() as db:
        c = db.execute("SELECT * FROM download_clients WHERE id=?", (client_id,)).fetchone()
    if not c:
        return JSONResponse({"ok": False, "message": "Client not found"})

    ok, msg = await _test_client(dict(c))
    return JSONResponse({"ok": ok, "message": msg})


async def _test_client(c: dict) -> tuple[bool, str]:
    t = c['type']
    host = client_base_url(c)
    if not host:
        return False, "No host configured"
    try:
        if t == 'qbittorrent':
            async with httpx.AsyncClient(timeout=10) as cli:
                r = await cli.post(f"{host}/api/v2/auth/login",
                                   data={'username': c['username'] or '', 'password': c['password'] or ''})
            if 'Ok' in r.text:
                return True, "Connected to qBittorrent"
            body = r.text.strip()[:120]
            if 'Unauthorized' in body or r.status_code == 403:
                return False, f"IP banned by qBittorrent (too many failed logins). Restart qBittorrent or wait ~1 hour to clear the ban. [{r.status_code}]"
            if 'Fails' in body:
                return False, f"Wrong username or password [{r.status_code}]"
            return False, f"HTTP {r.status_code}: {body}"

        elif t == 'sabnzbd':
            url_base = (c['url_base'] or '').strip('/')
            api_url = f"{host}/{url_base}/api" if url_base else f"{host}/api"
            async with httpx.AsyncClient(timeout=10) as cli:
                r = await cli.get(api_url, params={'mode': 'version', 'apikey': c['password'] or '', 'output': 'json'})
            if r.status_code == 200:
                v = r.json().get('version', '?')
                return True, f"SABnzbd {v}"
            return False, f"HTTP {r.status_code}"

        elif t == 'deluge':
            async with httpx.AsyncClient(timeout=10) as cli:
                r = await cli.post(f"{host}/json", json={
                    'method': 'auth.login', 'params': [c['password'] or ''], 'id': 1
                })
            data = r.json()
            if data.get('result'):
                return True, "Connected to Deluge"
            return False, str(data.get('error', 'Auth failed'))

        elif t == 'transmission':
            async with httpx.AsyncClient(timeout=10) as cli:
                r = await cli.get(f"{host}/transmission/rpc",
                                  auth=(c['username'] or '', c['password'] or ''))
            if r.status_code in (200, 409):
                return True, "Connected to Transmission"
            return False, f"HTTP {r.status_code}"

        elif t == 'nzbget':
            user = c.get('username') or ''
            pw   = c.get('password') or ''
            port = c.get('port') or 6789
            api_url = f"http://{user}:{pw}@{host}:{port}/jsonrpc"
            async with httpx.AsyncClient(timeout=10) as cli:
                r = await cli.post(api_url, json={"method": "version", "params": []})
            data = r.json()
            version = data.get('result', '')
            if version:
                return True, f"NZBGet {version}"
            return False, data.get('error', {}).get('message', 'No version returned')

        elif t == 'blackhole':
            import os
            path = host  # host field used as folder path for blackhole
            if os.path.isdir(path):
                return True, f"Blackhole folder exists: {path}"
            return False, f"Folder not found: {path}"

        elif t == 'suwayomi':
            from routers import suwayomi_ as _swy
            return await _swy.test_connection(c)

        else:
            return False, f"Unsupported client type: {t}"
    except Exception as e:
        return False, str(e)


# ── Path mapping helper ───────────────────────────────────────────────────────
def apply_remote_path_mapping(db, path: str, host: str = '') -> str:
    """Translate a download client path to a local Mangarr path using remote_path_mappings."""
    rows = db.execute(
        "SELECT remote_path, local_path FROM remote_path_mappings WHERE host=? OR host=''",
        (host,)
    ).fetchall()
    for row in rows:
        remote = row['remote_path'].rstrip('/')
        if path.startswith(remote):
            local = row['local_path'].rstrip('/')
            return local + path[len(remote):]
    return path


# ── Helper: get best download client for a protocol ──────────────────────────
def get_client_for_protocol(db, protocol: str, series_tags: list[str] | None = None) -> dict | None:
    """
    Return the best enabled download client for the given protocol.
    Prefers clients with matching tags; falls back to untagged clients.
    Priority lower number = higher priority.
    """
    proto_map = {'torrent': ['qbittorrent', 'deluge', 'transmission', 'rtorrent', 'blackhole'],
                 'nzb':     ['sabnzbd', 'nzbget']}
    valid_types = proto_map.get(protocol, [])
    if not valid_types:
        return None

    ph = ','.join('?' * len(valid_types))
    clients = db.execute(
        f"SELECT * FROM download_clients WHERE enabled=1 AND type IN ({ph})"
        " ORDER BY priority, id",
        valid_types
    ).fetchall()

    if not clients:
        return None

    def _norm(c) -> dict:
        d = dict(c)
        d['host'] = client_base_url(d)
        return d

    series_tags = set(series_tags or [])
    # Try tagged match first
    if series_tags:
        for c in clients:
            client_tags = {r['tag'] for r in db.execute(
                "SELECT tag FROM download_client_tags WHERE client_id=?", (c['id'],)
            ).fetchall()}
            if client_tags & series_tags:
                return _norm(c)

    # Fall back to first client with no tags, then any client
    for c in clients:
        client_tags = {r['tag'] for r in db.execute(
            "SELECT tag FROM download_client_tags WHERE client_id=?", (c['id'],)
        ).fetchall()}
        if not client_tags:
            return _norm(c)

    return _norm(clients[0])
