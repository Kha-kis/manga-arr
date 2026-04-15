"""Notification Connections — multi-service notification system (Sonarr parity)."""
import json
import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from routers._templates import templates

from shared import get_db, from_json, get_cfg
from security import validate_outbound_url, UnsafeURLError

router = APIRouter()

CONNECTION_TYPES = [
    "discord", "telegram", "slack", "ntfy", "gotify",
    "pushover", "email", "webhook", "apprise", "pushbullet",
]

EVENT_FLAGS = [
    ("on_grab",            "On Grab"),
    ("on_download",        "On Download"),
    ("on_upgrade",         "On Upgrade"),
    ("on_series_add",      "On Series Add"),
    ("on_health_issue",    "On Health Issue"),
    ("on_health_restored", "On Health Restored"),
]


def _all_connections(db):
    return db.execute("SELECT * FROM notification_connections ORDER BY name").fetchall()


# ── List ──────────────────────────────────────────────────────────────────────
@router.get("/notifications", response_class=HTMLResponse)
async def notifications_page(request: Request):
    with get_db() as db:
        connections = _all_connections(db)
    return templates.TemplateResponse(request, "notification_connections.html", {
        "connections":      connections,
        "connection_types": CONNECTION_TYPES,
        "event_flags":      EVENT_FLAGS,
    })


# ── Create ────────────────────────────────────────────────────────────────────
@router.post("/notifications")
async def create_notification_connection(
    request: Request,
    name: str = Form(...),
    type: str = Form(...),
    enabled: int = Form(1),
    settings: str = Form("{}"),
):
    form = await request.form()
    events = {flag: int(form.get(flag, 0)) for flag, _ in EVENT_FLAGS}
    try:
        settings_dict = json.loads(settings)
    except Exception:
        settings_dict = {}
    with get_db() as db:
        db.execute(
            "INSERT INTO notification_connections(name,type,enabled,settings,"
            " on_grab,on_download,on_upgrade,on_series_add,on_health_issue,on_health_restored)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (name.strip(), type, enabled, json.dumps(settings_dict),
             events.get('on_grab', 1), events.get('on_download', 1),
             events.get('on_upgrade', 1), events.get('on_series_add', 1),
             events.get('on_health_issue', 1), events.get('on_health_restored', 0))
        )
    return RedirectResponse("/notifications", status_code=303)


# ── Edit ──────────────────────────────────────────────────────────────────────
@router.post("/notifications/{conn_id}")
async def edit_notification_connection(
    request: Request,
    conn_id: int,
    name: str = Form(...),
    type: str = Form(...),
    enabled: int = Form(1),
    settings: str = Form("{}"),
):
    form = await request.form()
    events = {flag: int(form.get(flag, 0)) for flag, _ in EVENT_FLAGS}
    try:
        settings_dict = json.loads(settings)
    except Exception:
        settings_dict = {}
    with get_db() as db:
        db.execute(
            "UPDATE notification_connections SET name=?,type=?,enabled=?,settings=?,"
            " on_grab=?,on_download=?,on_upgrade=?,on_series_add=?,on_health_issue=?,"
            " on_health_restored=? WHERE id=?",
            (name.strip(), type, enabled, json.dumps(settings_dict),
             events.get('on_grab', 1), events.get('on_download', 1),
             events.get('on_upgrade', 1), events.get('on_series_add', 1),
             events.get('on_health_issue', 1), events.get('on_health_restored', 0),
             conn_id)
        )
    return RedirectResponse("/notifications", status_code=303)


# ── Delete ────────────────────────────────────────────────────────────────────
@router.post("/notifications/{conn_id}/delete")
async def delete_notification_connection(conn_id: int):
    with get_db() as db:
        db.execute("DELETE FROM notification_connections WHERE id=?", (conn_id,))
    return RedirectResponse("/notifications", status_code=303)


# ── Test ─────────────────────────────────────────────────────────────────────
@router.post("/api/notifications/{conn_id}/test")
async def test_notification_connection(conn_id: int):
    with get_db() as db:
        conn = db.execute("SELECT * FROM notification_connections WHERE id=?", (conn_id,)).fetchone()
    if not conn:
        return JSONResponse({"ok": False, "message": "Connection not found"})
    ok, msg = await send_connection(dict(conn), "Test notification from Mangarr", event="test")
    return JSONResponse({"ok": ok, "message": msg})


# ── Core send function ────────────────────────────────────────────────────────
async def send_connection(conn: dict, message: str,
                          event: str = "", embed: dict | None = None) -> tuple[bool, str]:
    """Send a notification via a single connection. Returns (ok, message)."""
    t        = conn['type']
    settings = from_json(conn.get('settings'), {})

    try:
        if t == 'discord':
            return await _send_discord(settings, message, embed)
        elif t == 'telegram':
            return await _send_telegram(settings, message)
        elif t == 'slack':
            return await _send_slack(settings, message)
        elif t == 'ntfy':
            return await _send_ntfy(settings, message)
        elif t == 'gotify':
            return await _send_gotify(settings, message)
        elif t == 'pushover':
            return await _send_pushover(settings, message)
        elif t == 'webhook':
            return await _send_webhook(settings, message, event, embed)
        elif t == 'email':
            return await _send_email(settings, message)
        elif t == 'apprise':
            return await _send_apprise(settings, message)
        elif t == 'pushbullet':
            return await _send_pushbullet(settings, message)
        else:
            return False, f"Unsupported type: {t}"
    except Exception as e:
        return False, str(e)


async def _send_discord(s: dict, message: str, embed: dict | None) -> tuple[bool, str]:
    webhook = s.get('webhook_url', '')
    if not webhook:
        return False, "No webhook URL"
    try:
        validate_outbound_url(webhook)
    except UnsafeURLError as e:
        return False, f"URL rejected: {e}"
    payload: dict = {}
    if embed:
        payload['embeds'] = [embed]
    else:
        payload['content'] = message
    async with httpx.AsyncClient(timeout=10) as cli:
        r = await cli.post(webhook, json=payload)
    if r.status_code in (200, 204):
        return True, "Sent"
    return False, f"HTTP {r.status_code}: {r.text[:100]}"


async def _send_telegram(s: dict, message: str) -> tuple[bool, str]:
    token   = s.get('bot_token', '')
    chat_id = s.get('chat_id', '')
    if not token or not chat_id:
        return False, "Missing bot_token or chat_id"
    async with httpx.AsyncClient(timeout=10) as cli:
        r = await cli.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={'chat_id': chat_id, 'text': message, 'parse_mode': 'HTML'}
        )
    data = r.json()
    if data.get('ok'):
        return True, "Sent"
    return False, data.get('description', 'Unknown error')


async def _send_slack(s: dict, message: str) -> tuple[bool, str]:
    webhook = s.get('webhook_url', '')
    if not webhook:
        return False, "No webhook URL"
    try:
        validate_outbound_url(webhook)
    except UnsafeURLError as e:
        return False, f"URL rejected: {e}"
    async with httpx.AsyncClient(timeout=10) as cli:
        r = await cli.post(webhook, json={'text': message})
    if r.text == 'ok' or r.status_code == 200:
        return True, "Sent"
    return False, f"HTTP {r.status_code}"


async def _send_ntfy(s: dict, message: str) -> tuple[bool, str]:
    server = (s.get('server', 'https://ntfy.sh')).rstrip('/')
    topic  = s.get('topic', '')
    token  = s.get('token', '')
    if not topic:
        return False, "No topic configured"
    target = f"{server}/{topic}"
    try:
        validate_outbound_url(target)
    except UnsafeURLError as e:
        return False, f"URL rejected: {e}"
    headers = {'Title': 'Mangarr', 'Priority': 'default'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    async with httpx.AsyncClient(timeout=10) as cli:
        r = await cli.post(target, content=message, headers=headers)
    if r.status_code == 200:
        return True, "Sent"
    return False, f"HTTP {r.status_code}"


async def _send_gotify(s: dict, message: str) -> tuple[bool, str]:
    server = (s.get('server', '')).rstrip('/')
    token  = s.get('app_token', '')
    if not server or not token:
        return False, "Missing server or app_token"
    target = f"{server}/message"
    try:
        validate_outbound_url(target)
    except UnsafeURLError as e:
        return False, f"URL rejected: {e}"
    async with httpx.AsyncClient(timeout=10) as cli:
        r = await cli.post(
            target,
            params={'token': token},
            json={'title': 'Mangarr', 'message': message, 'priority': 5}
        )
    if r.status_code == 200:
        return True, "Sent"
    return False, f"HTTP {r.status_code}"


async def _send_pushover(s: dict, message: str) -> tuple[bool, str]:
    user_key = s.get('user_key', '')
    api_token = s.get('api_token', '')
    if not user_key or not api_token:
        return False, "Missing user_key or api_token"
    async with httpx.AsyncClient(timeout=10) as cli:
        r = await cli.post(
            "https://api.pushover.net/1/messages.json",
            data={'token': api_token, 'user': user_key, 'message': message, 'title': 'Mangarr'}
        )
    data = r.json()
    if data.get('status') == 1:
        return True, "Sent"
    return False, str(data.get('errors', 'Unknown error'))


async def _send_webhook(s: dict, message: str, event: str, embed: dict | None) -> tuple[bool, str]:
    url    = s.get('url', '')
    method = s.get('method', 'POST').upper()
    if not url:
        return False, "No URL configured"
    try:
        validate_outbound_url(url)
    except UnsafeURLError as e:
        return False, f"URL rejected: {e}"
    payload = {
        "eventType": event,
        "message": message,
        "embed": embed,
        "instanceName": get_cfg('instance_name', 'Mangarr'),
    }
    async with httpx.AsyncClient(timeout=10) as cli:
        if method == 'GET':
            r = await cli.get(url, params={"eventType": event, "message": message})
        else:
            r = await cli.post(url, json=payload,
                               headers={"Content-Type": "application/json"})
    if 200 <= r.status_code < 300:
        return True, f"HTTP {r.status_code}"
    return False, f"HTTP {r.status_code}: {r.text[:100]}"


async def _send_email(s: dict, message: str) -> tuple[bool, str]:
    """Send via SMTP (using smtplib in a thread to avoid blocking)."""
    import asyncio, smtplib
    from email.mime.text import MIMEText

    host    = s.get('host', 'localhost')
    port    = int(s.get('port', 25))
    user    = s.get('username', '')
    pw      = s.get('password', '')
    to_addr = s.get('to', '')
    from_addr = s.get('from', 'mangarr@localhost')

    if not to_addr:
        return False, "No recipient configured"

    def _send():
        msg = MIMEText(message)
        msg['Subject'] = 'Mangarr Notification'
        msg['From']    = from_addr
        msg['To']      = to_addr
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            if user:
                smtp.login(user, pw)
            smtp.sendmail(from_addr, [to_addr], msg.as_string())

    await asyncio.get_event_loop().run_in_executor(None, _send)
    return True, "Sent"


async def _send_apprise(s: dict, message: str) -> tuple[bool, str]:
    """Send via Apprise API server."""
    url  = (s.get('url', '')).rstrip('/')
    key  = s.get('config_key', '')
    if not url:
        return False, "No Apprise URL"
    payload = {'body': message, 'title': 'Mangarr'}
    api_url = f"{url}/notify/{key}" if key else f"{url}/notify"
    try:
        validate_outbound_url(api_url)
    except UnsafeURLError as e:
        return False, f"URL rejected: {e}"
    async with httpx.AsyncClient(timeout=10) as cli:
        r = await cli.post(api_url, json=payload)
    if r.status_code == 200:
        return True, "Sent"
    return False, f"HTTP {r.status_code}"


async def _send_pushbullet(s: dict, message: str) -> tuple[bool, str]:
    token = s.get('access_token', '')
    if not token:
        return False, "No access_token"
    async with httpx.AsyncClient(timeout=10) as cli:
        r = await cli.post(
            "https://api.pushbullet.com/v2/pushes",
            headers={'Access-Token': token},
            json={'type': 'note', 'title': 'Mangarr', 'body': message}
        )
    if r.status_code == 200:
        return True, "Sent"
    return False, f"HTTP {r.status_code}"


# ── Public API: fire notifications for an event ───────────────────────────────
# `event` is interpolated directly into SQL as a column name — `?`
# placeholders can't bind identifiers. The whitelist below is the single
# defence against a future refactor passing untrusted input into this
# helper. Any event not in the set is a no-op with a warning log.
_VALID_NOTIFICATION_EVENTS = frozenset({
    "on_grab",
    "on_download",
    "on_upgrade",
    "on_series_add",
    "on_health_issue",
    "on_health_restored",
})


async def fire_notifications(event: str, message: str, embed: dict | None = None):
    """
    Send notifications to all enabled connections subscribed to the given event.
    event: 'on_grab' | 'on_download' | 'on_upgrade' | 'on_series_add' |
           'on_health_issue' | 'on_health_restored'

    An unknown or malformed event is a no-op (logs a warning and returns
    without touching SQL). This prevents arbitrary column/identifier
    strings from being interpolated into the SELECT below.
    """
    if event not in _VALID_NOTIFICATION_EVENTS:
        try:
            import main as _m
            _m.log_event(
                'error',
                f"fire_notifications: ignoring unknown event {event!r}",
            )
        except Exception:
            pass
        return
    with get_db() as db:
        connections = db.execute(
            f"SELECT * FROM notification_connections WHERE enabled=1 AND {event}=1"
        ).fetchall()

    import asyncio

    async def _send_and_log(c):
        ok, msg = await send_connection(c, message, event=event, embed=embed)
        if not ok:
            try:
                import main as _m
                _m.log_event('error', f"Notification failed [{c['type']} — {c['name']}]: {msg}")
            except Exception:
                pass

    tasks = [_send_and_log(dict(c)) for c in connections]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
