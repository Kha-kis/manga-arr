"""Download Clients — multiple DB-managed download clients (Sonarr parity)."""

import json
import time
import httpx
from urllib.parse import quote, urlparse, urlunparse
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from routers._templates import templates

from shared import get_db, from_json, get_secret_health_summary
from security import decrypt_secret_safe, encrypt_if_cipher_available


def _row_decrypted(c) -> dict:
    """Row → dict with download_clients.password decrypted (or '' if
    undecryptable). Plaintext values pass through; enc:v1: values are
    decrypted; wrong-key / corrupt values log a WARNING naming the
    client and become '' so the downstream integration fails cleanly.
    """
    d = dict(c)
    d["password"] = decrypt_secret_safe(
        d.get("password"),
        field_name="download_clients.password",
        context=d.get("name") or "?",
    )
    return d


# Circuit breaker: track consecutive failures per client id.
# Persisted in client_breaker_state so a tripped breaker survives app
# restarts — an in-memory dict would reset on boot and let a known-bad
# client retry immediately (masking the failure pattern).
_CB_THRESHOLD = 3  # open after this many consecutive failures
_CB_TIMEOUT = 300  # stay open for 5 minutes


def _cb_load(client_id: int) -> dict | None:
    with get_db() as db:
        row = db.execute(
            "SELECT failures, open_until FROM client_breaker_state WHERE client_id=?",
            (client_id,),
        ).fetchone()
    if not row:
        return None
    return {"failures": int(row["failures"]), "open_until": float(row["open_until"])}


def _cb_write(client_id: int, failures: int, open_until: float) -> None:
    with get_db() as db:
        db.execute(
            "INSERT INTO client_breaker_state(client_id, failures, open_until, updated_at)"
            " VALUES(?, ?, ?, CURRENT_TIMESTAMP)"
            " ON CONFLICT(client_id) DO UPDATE SET"
            "   failures=excluded.failures,"
            "   open_until=excluded.open_until,"
            "   updated_at=CURRENT_TIMESTAMP",
            (client_id, failures, open_until),
        )


def _cb_clear(client_id: int) -> None:
    with get_db() as db:
        db.execute("DELETE FROM client_breaker_state WHERE client_id=?", (client_id,))


def _cb_is_open(client_id: int) -> bool:
    """Return True if circuit is open (client should be skipped)."""
    state = _cb_load(client_id)
    if not state:
        return False
    if state["failures"] >= _CB_THRESHOLD:
        if time.time() < state["open_until"]:
            return True
        # Timeout elapsed — half-open: decrement failures so next attempt
        # counts but doesn't stay open. Persist the half-open state so a
        # concurrent worker sees it too.
        _cb_write(client_id, _CB_THRESHOLD - 1, state["open_until"])
    return False


def _cb_record_success(client_id: int):
    _cb_clear(client_id)


def _cb_record_failure(client_id: int):
    state = _cb_load(client_id) or {"failures": 0, "open_until": 0.0}
    failures = state["failures"] + 1
    open_until = state["open_until"]
    if failures >= _CB_THRESHOLD:
        open_until = time.time() + _CB_TIMEOUT
    _cb_write(client_id, failures, open_until)


def client_base_url(c: dict) -> str:
    """Return the full base URL for a download client, merging host + port.

    If the host field already embeds a port (e.g. http://host:8080/path) the
    stored ``port`` column is ignored so we never produce a double-port URL.
    Strips any trailing slash.
    """
    from urllib.parse import urlparse, urlunparse

    host = (c.get("host") or "").rstrip("/")
    port = c.get("port")
    if host and port:
        parsed = urlparse(host)
        if not parsed.port:
            # Only the hostname is in the URL — append the port
            netloc = f"{parsed.hostname}:{port}"
            parsed = parsed._replace(netloc=netloc)
            host = urlunparse(parsed).rstrip("/")
    return host


def _friendly_client_error(exc: Exception) -> str:
    msg = (str(exc) or type(exc).__name__).strip()
    low = msg.lower()
    if (
        "name or service not known" in low
        or "could not resolve" in low
        or "nodename nor servname provided" in low
    ):
        return "Could not resolve the host. Check the hostname."
    if "all connection attempts failed" in low:
        return "Connection failed. Check the host, port, and scheme."
    if "connection refused" in low:
        return "Connection refused. Check that the service is running and reachable."
    if "timed out" in low or "timeout" in low:
        return "Connection timed out. Check reachability and TLS settings."
    return msg


def _nzbget_rpc_url(c: dict) -> str:
    host = (c.get("host") or "").strip()
    if not host:
        return ""
    scheme_default = "https" if c.get("use_ssl") else "http"
    raw = host if "://" in host else f"{scheme_default}://{host}"
    parsed = urlparse(raw)
    hostname = parsed.hostname or parsed.path
    if not hostname:
        return raw
    port = parsed.port or c.get("port") or 6789
    netloc = hostname
    if port:
        netloc = f"{hostname}:{port}"
    user = c.get("username") or ""
    password = c.get("password") or ""
    if user or password:
        netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{netloc}"
    path = (parsed.path or "").rstrip("/")
    path = f"{path}/jsonrpc" if path else "/jsonrpc"
    return urlunparse((parsed.scheme or scheme_default, netloc, path, "", "", ""))


router = APIRouter()

CLIENT_TYPES = [
    "qbittorrent",
    "sabnzbd",
    "nzbget",
    "deluge",
    "transmission",
    "rtorrent",
    "blackhole",
    "suwayomi",
]


def _all_clients(db):
    clients = db.execute(
        "SELECT * FROM download_clients ORDER BY priority, id"
    ).fetchall()
    result = []
    for c in clients:
        tags = db.execute(
            "SELECT tag FROM download_client_tags WHERE client_id=?", (c["id"],)
        ).fetchall()
        result.append({**_row_decrypted(c), "tags": [t["tag"] for t in tags]})
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
            row = db.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
            return (row["value"] if row else "0") == "1"

        options = {
            "failed_download_handling": _opt("failed_download_handling"),
            "redownload_failed_interactive": _opt("redownload_failed_interactive"),
        }
        secret_health = get_secret_health_summary(db)
    return templates.TemplateResponse(
        request,
        "download_clients.html",
        {
            "clients": clients,
            "client_types": CLIENT_TYPES,
            "mappings": mappings,
            "options": options,
            "saved": request.query_params.get("saved") == "1",
            "secret_health": secret_health,
        },
    )


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
    stored_password = encrypt_if_cipher_available(password) if password else None
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO download_clients(name,type,host,port,use_ssl,url_base,username,password,"
            " category,post_import_category,recent_priority,older_priority,initial_state,"
            " sequential_order,first_last_first,content_layout,priority,enabled,remove_completed,remove_failed,"
            " download_path,merge_chapters)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                name.strip(),
                type,
                host.strip(),
                port or None,
                use_ssl,
                url_base.strip() or None,
                username.strip() or None,
                stored_password,
                category.strip() or "manga",
                post_import_category.strip() or None,
                recent_priority,
                older_priority,
                initial_state,
                sequential_order,
                first_last_first,
                content_layout,
                priority,
                enabled,
                remove_completed,
                remove_failed,
                download_path.strip() or None,
                merge_chapters,
            ),
        )
        cid = cur.lastrowid
        for tag in [t.strip() for t in tags.split(",") if t.strip()]:
            db.execute(
                "INSERT OR IGNORE INTO download_client_tags(client_id,tag) VALUES(?,?)",
                (cid, tag),
            )
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
            (host.strip(), remote_path.strip(), local_path.strip()),
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
    failed_download_handling: str = Form("0"),
    redownload_failed_interactive: str = Form("0"),
):
    with get_db() as db:
        for k, v in {
            "failed_download_handling": "1" if failed_download_handling == "1" else "0",
            "redownload_failed_interactive": "1"
            if redownload_failed_interactive == "1"
            else "0",
        }.items():
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, v))
    return RedirectResponse("/download-clients?saved=1", status_code=303)


# ── Edit ──────────────────────────────────────────────────────────────────────
@router.post("/download-clients/{client_id}")
async def edit_download_client(request: Request, client_id: int):
    """Edit a download client. Partial-POST safe: only columns whose
    form key is present in the request body are written. The password
    field is only written if the form carries it AND it's non-empty —
    this replaces the legacy `keep_password` checkbox marker, since
    "field absent" now naturally means "leave the existing password
    alone". Tags are only rebuilt if the `tags` field is in the form.
    """
    from routers._form_helpers import (
        submitted_subset,
        str_or_none,
        int_or_none,
        int_default_zero,
        bool_int,
    )

    submitted = await request.form()

    plain_fields = {
        "name": ("name", lambda v: str(v or "").strip()),
        "type": ("type", lambda v: str(v or "").strip()),
        "host": ("host", lambda v: str(v or "").strip()),
        "port": ("port", lambda v: int_or_none(v) or None),
        "use_ssl": ("use_ssl", bool_int),
        "url_base": ("url_base", str_or_none),
        "username": ("username", str_or_none),
        "category": ("category", lambda v: str(v or "").strip() or "manga"),
        "post_import_category": ("post_import_category", str_or_none),
        "recent_priority": (
            "recent_priority",
            lambda v: str(v or "").strip() or "last",
        ),
        "older_priority": ("older_priority", lambda v: str(v or "").strip() or "last"),
        "initial_state": ("initial_state", lambda v: str(v or "").strip() or "normal"),
        "sequential_order": ("sequential_order", bool_int),
        "first_last_first": ("first_last_first", bool_int),
        "content_layout": (
            "content_layout",
            lambda v: str(v or "").strip() or "original",
        ),
        "priority": ("priority", int_default_zero),
        "enabled": ("enabled", bool_int),
        "remove_completed": ("remove_completed", bool_int),
        "remove_failed": ("remove_failed", bool_int),
        "download_path": ("download_path", str_or_none),
        "merge_chapters": ("merge_chapters", bool_int),
    }

    with get_db() as db:
        updates, params = submitted_subset(submitted, plain_fields)

        # password: only update if the form carries it AND it's non-empty.
        # The legacy `keep_password=1` checkbox marker is no longer needed
        # — the HTML page submits password only when the user has typed
        # a new one.
        if "password" in submitted:
            pw_raw = str(submitted["password"] or "")
            if pw_raw:
                updates.append("password=?")
                params.append(encrypt_if_cipher_available(pw_raw))

        if updates:
            params.append(client_id)
            db.execute(
                f"UPDATE download_clients SET {', '.join(updates)} WHERE id=?", params
            )

        # Tag set is only rebuilt if the form carries `tags`.
        if "tags" in submitted:
            tag_list = [
                t.strip() for t in str(submitted["tags"] or "").split(",") if t.strip()
            ]
            db.execute(
                "DELETE FROM download_client_tags WHERE client_id=?", (client_id,)
            )
            for tag in tag_list:
                db.execute(
                    "INSERT OR IGNORE INTO download_client_tags(client_id,tag) VALUES(?,?)",
                    (client_id, tag),
                )
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
    _cb_clear(client_id)
    return JSONResponse({"ok": True, "message": "Circuit breaker reset"})


@router.post("/api/download-clients/reset-all-circuits")
async def reset_all_circuit_breakers():
    """Clear every circuit breaker state — used at startup and for diagnostics."""
    with get_db() as db:
        db.execute("DELETE FROM client_breaker_state")
    return JSONResponse({"ok": True, "message": "All circuit breakers reset"})


# ── Test ──────────────────────────────────────────────────────────────────────
@router.post("/api/download-clients/{client_id}/test")
async def test_download_client(client_id: int):
    with get_db() as db:
        c = db.execute(
            "SELECT * FROM download_clients WHERE id=?", (client_id,)
        ).fetchone()
    if not c:
        return JSONResponse({"ok": False, "message": "Client not found"})

    ok, msg = await _test_client(_row_decrypted(c))
    if ok:
        _cb_clear(client_id)
    return JSONResponse({"ok": ok, "message": msg})


@router.post("/api/download-clients/test-form")
async def test_download_client_form(
    name: str = Form("Unsaved client"),
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
    download_path: str = Form(""),
    merge_chapters: int = Form(0),
):
    client = {
        "name": name.strip() or "Unsaved client",
        "type": type,
        "host": host.strip(),
        "port": port or None,
        "use_ssl": use_ssl,
        "url_base": url_base.strip() or None,
        "username": username.strip() or None,
        "password": password,
        "category": category.strip() or "manga",
        "post_import_category": post_import_category.strip() or None,
        "recent_priority": recent_priority,
        "older_priority": older_priority,
        "initial_state": initial_state,
        "sequential_order": sequential_order,
        "first_last_first": first_last_first,
        "content_layout": content_layout,
        "priority": priority,
        "enabled": enabled,
        "remove_completed": remove_completed,
        "remove_failed": remove_failed,
        "download_path": download_path.strip() or None,
        "merge_chapters": merge_chapters,
    }
    ok, msg = await _test_client(client)
    return JSONResponse({"ok": ok, "message": msg})


async def _test_client(c: dict) -> tuple[bool, str]:
    t = c["type"]
    host = client_base_url(c)
    if not host:
        return False, "No host configured"
    try:
        if t == "qbittorrent":
            async with httpx.AsyncClient(timeout=10) as cli:
                r = await cli.post(
                    f"{host}/api/v2/auth/login",
                    data={
                        "username": c["username"] or "",
                        "password": c["password"] or "",
                    },
                )
            if "Ok" in r.text:
                return True, "Connected to qBittorrent"
            body = r.text.strip()[:120]
            if "Unauthorized" in body or r.status_code == 403:
                return (
                    False,
                    f"IP banned by qBittorrent (too many failed logins). Restart qBittorrent or wait ~1 hour to clear the ban. [{r.status_code}]",
                )
            if "Fails" in body:
                return False, f"Wrong username or password [{r.status_code}]"
            return False, f"HTTP {r.status_code}: {body}"

        elif t == "sabnzbd":
            if not (c.get("password") or "").strip():
                return False, "API key is required for SABnzbd"
            url_base = (c["url_base"] or "").strip("/")
            api_url = f"{host}/{url_base}/api" if url_base else f"{host}/api"
            async with httpx.AsyncClient(timeout=10) as cli:
                r = await cli.get(
                    api_url,
                    params={
                        "mode": "queue",
                        "start": 0,
                        "limit": 0,
                        "apikey": c["password"] or "",
                        "output": "json",
                    },
                )
            if r.status_code == 200:
                data = r.json()
                queue = data.get("queue") if isinstance(data, dict) else None
                if isinstance(queue, dict):
                    return True, f"SABnzbd {queue.get('version', '?')}"
                detail = str(data.get("error") or "invalid API response")[:120]
                return False, f"SABnzbd API error: {detail}"
            return False, f"HTTP {r.status_code}"

        elif t == "deluge":
            async with httpx.AsyncClient(timeout=10) as cli:
                r = await cli.post(
                    f"{host}/json",
                    json={
                        "method": "auth.login",
                        "params": [c["password"] or ""],
                        "id": 1,
                    },
                )
            data = r.json()
            if data.get("result"):
                return True, "Connected to Deluge"
            return False, str(data.get("error", "Auth failed"))

        elif t == "transmission":
            async with httpx.AsyncClient(timeout=10) as cli:
                r = await cli.get(
                    f"{host}/transmission/rpc",
                    auth=(c["username"] or "", c["password"] or ""),
                )
            if r.status_code in (200, 409):
                return True, "Connected to Transmission"
            return False, f"HTTP {r.status_code}"

        elif t == "nzbget":
            api_url = _nzbget_rpc_url(c)
            async with httpx.AsyncClient(timeout=10) as cli:
                r = await cli.post(api_url, json={"method": "version", "params": []})
            data = r.json()
            version = data.get("result", "")
            if version:
                return True, f"NZBGet {version}"
            return False, data.get("error", {}).get("message", "No version returned")

        elif t == "blackhole":
            import os

            path = host  # host field used as folder path for blackhole
            if os.path.isdir(path):
                return True, f"Blackhole folder exists: {path}"
            return False, f"Folder not found: {path}"

        elif t == "suwayomi":
            from routers import suwayomi_ as _swy

            ok, msg = await _swy.test_connection(c)
            if ok:
                try:
                    data = await _swy._gql(
                        c,
                        """
                        { extensions(filter: {isInstalled: {eq: true}}) { nodes { pkgName } } }
                    """,
                    )
                    installed = (data.get("extensions") or {}).get("nodes") or []
                    if not installed:
                        msg += (
                            " | No extensions installed — visit Settings → Extensions"
                        )
                except Exception:
                    pass
            return ok, msg

        else:
            return False, f"Unsupported client type: {t}"
    except Exception as e:
        return False, _friendly_client_error(e)


# ── Path mapping helper ───────────────────────────────────────────────────────
def apply_remote_path_mapping(db, path: str, host: str = "") -> str:
    """Translate a download client path to a local Mangarr path using remote_path_mappings."""
    rows = db.execute(
        "SELECT remote_path, local_path FROM remote_path_mappings WHERE host=? OR host=''",
        (host,),
    ).fetchall()
    for row in rows:
        remote = row["remote_path"].rstrip("/")
        if path.startswith(remote):
            local = row["local_path"].rstrip("/")
            return local + path[len(remote) :]
    return path


# ── Helper: get best download client for a protocol ──────────────────────────
def get_client_for_protocol(
    db, protocol: str, series_tags: list[str] | None = None
) -> dict | None:
    """
    Return the best enabled download client for the given protocol.
    Prefers clients with matching tags; falls back to untagged clients.
    Priority lower number = higher priority.
    """
    proto_map = {
        "torrent": ["qbittorrent", "deluge", "transmission", "rtorrent", "blackhole"],
        "nzb": ["sabnzbd", "nzbget"],
    }
    valid_types = proto_map.get(protocol, [])
    if not valid_types:
        return None

    ph = ",".join("?" * len(valid_types))
    clients = db.execute(
        f"SELECT * FROM download_clients WHERE enabled=1 AND type IN ({ph})"
        " ORDER BY priority, id",
        valid_types,
    ).fetchall()

    if not clients:
        return None

    def _norm(c) -> dict:
        d = _row_decrypted(c)
        d["host"] = client_base_url(d)
        return d

    tag_set = set(series_tags or [])
    # Try tagged match first
    if tag_set:
        for c in clients:
            client_tags = {
                r["tag"]
                for r in db.execute(
                    "SELECT tag FROM download_client_tags WHERE client_id=?", (c["id"],)
                ).fetchall()
            }
            if client_tags & tag_set:
                return _norm(c)

    # Fall back to first client with no tags, then any client
    for c in clients:
        client_tags = {
            r["tag"]
            for r in db.execute(
                "SELECT tag FROM download_client_tags WHERE client_id=?", (c["id"],)
            ).fetchall()
        }
        if not client_tags:
            return _norm(c)

    return _norm(clients[0])
