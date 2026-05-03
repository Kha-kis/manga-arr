"""Notification Connections — multi-service notification system (Sonarr parity)."""
import json
import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from routers._templates import templates

from shared import get_db, from_json, get_cfg, get_secret_health_summary
from security import (
    validate_outbound_url, UnsafeURLError,
    encrypt_if_cipher_available, decrypt_secret_safe, decrypt_secret,
    SecretDecryptionError, SecretCipherUnavailable,
)

router = APIRouter()


NOTIFICATION_FIELD_SCHEMAS: dict[str, list[dict]] = {
    "discord": [
        {"name": "webhook_url", "label": "Webhook URL", "input_type": "url", "required": True, "secret": True,
         "placeholder": "https://discord.com/api/webhooks/...", "help": "Discord webhook endpoint."},
    ],
    "telegram": [
        {"name": "bot_token", "label": "Bot Token", "input_type": "password", "required": True, "secret": True,
         "placeholder": "123456:ABCDEF...", "help": "Telegram bot token from BotFather."},
        {"name": "chat_id", "label": "Chat ID", "input_type": "text", "required": True, "secret": False,
         "placeholder": "123456789", "help": "Target chat or channel ID."},
    ],
    "slack": [
        {"name": "webhook_url", "label": "Webhook URL", "input_type": "url", "required": True, "secret": True,
         "placeholder": "https://hooks.slack.com/services/...", "help": "Slack incoming webhook endpoint."},
    ],
    "ntfy": [
        {"name": "server", "label": "Server URL", "input_type": "url", "required": False, "secret": False,
         "placeholder": "https://ntfy.sh", "help": "Defaults to https://ntfy.sh.", "default": "https://ntfy.sh"},
        {"name": "topic", "label": "Topic", "input_type": "text", "required": True, "secret": False,
         "placeholder": "mangarr", "help": "Topic to publish notifications to."},
        {"name": "token", "label": "Access Token", "input_type": "password", "required": False, "secret": True,
         "placeholder": "optional", "help": "Optional bearer token for protected topics."},
    ],
    "gotify": [
        {"name": "server", "label": "Server URL", "input_type": "url", "required": True, "secret": False,
         "placeholder": "http://gotify:80", "help": "Base URL of the Gotify server."},
        {"name": "app_token", "label": "App Token", "input_type": "password", "required": True, "secret": True,
         "placeholder": "application token", "help": "Token from the Gotify application."},
    ],
    "pushover": [
        {"name": "user_key", "label": "User Key", "input_type": "password", "required": True, "secret": True,
         "placeholder": "user key", "help": "Pushover user or group key."},
        {"name": "api_token", "label": "API Token", "input_type": "password", "required": True, "secret": True,
         "placeholder": "application token", "help": "Pushover application token."},
    ],
    "email": [
        {"name": "host", "label": "SMTP Host", "input_type": "text", "required": False, "secret": False,
         "placeholder": "localhost", "help": "SMTP server host.", "default": "localhost"},
        {"name": "port", "label": "Port", "input_type": "number", "required": False, "secret": False,
         "placeholder": "25", "help": "SMTP port.", "default": 25},
        {"name": "username", "label": "Username", "input_type": "text", "required": False, "secret": False,
         "placeholder": "optional", "help": "Optional SMTP username."},
        {"name": "password", "label": "Password", "input_type": "password", "required": False, "secret": True,
         "placeholder": "optional", "help": "Optional SMTP password."},
        {"name": "from", "label": "From Address", "input_type": "email", "required": False, "secret": False,
         "placeholder": "mangarr@localhost", "help": "Envelope sender used for notifications.", "default": "mangarr@localhost"},
        {"name": "to", "label": "To Address", "input_type": "email", "required": True, "secret": False,
         "placeholder": "user@example.com", "help": "Recipient address."},
    ],
    "webhook": [
        {"name": "url", "label": "Webhook URL", "input_type": "url", "required": True, "secret": True,
         "placeholder": "https://example.com/hook", "help": "HTTP endpoint to call."},
        {"name": "method", "label": "Method", "input_type": "select", "required": False, "secret": False,
         "default": "POST", "options": [{"value": "POST", "label": "POST"}, {"value": "GET", "label": "GET"}],
         "help": "Request method for the outbound webhook."},
    ],
}

ADVANCED_ONLY_CONNECTION_TYPES = frozenset({"apprise", "pushbullet"})


def _secret_keys_for(ctype: str) -> tuple[str, ...]:
    """Return the tuple of JSON keys whose values are encrypted at rest
    for the given notification connection type."""
    from config import NOTIFICATION_SECRET_KEYS_BY_TYPE
    return NOTIFICATION_SECRET_KEYS_BY_TYPE.get(ctype or "", ())


def _encrypt_secret_fields(ctype: str, settings: dict) -> dict:
    """Return a new dict with this type's secret fields encrypted.

    Idempotent: already enc:v1: values pass through unchanged. Empty /
    None / non-str values pass through. Non-secret keys are preserved.
    Safe when the cipher isn't loaded (plaintext fall-through — the
    next migration boot picks it up).
    """
    out = dict(settings)
    for k in _secret_keys_for(ctype):
        v = out.get(k)
        if v and isinstance(v, str):
            out[k] = encrypt_if_cipher_available(v)
    return out


def _decrypt_secret_fields(ctype: str, name: str, settings: dict) -> dict:
    """Return a new dict with this type's secret fields decrypted via
    decrypt_secret_safe. An undecryptable value becomes '' with a
    WARNING naming the field + connection; the downstream sender sees
    "no credential" and fails that one send cleanly — fanout over
    other connections is unaffected.
    """
    out = dict(settings)
    for k in _secret_keys_for(ctype):
        v = out.get(k)
        if v and isinstance(v, str):
            out[k] = decrypt_secret_safe(
                v,
                field_name=f"notification_connections.settings.{k}",
                context=f"{ctype}/{name}",
            )
    return out

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


def _friendly_connection_error(exc: Exception) -> str:
    msg = (str(exc) or type(exc).__name__).strip()
    low = msg.lower()
    if "name or service not known" in low or "could not resolve" in low or "nodename nor servname provided" in low:
        return "Could not resolve the host. Check the hostname."
    if "all connection attempts failed" in low:
        return "Connection failed. Check the host, port, and scheme."
    if "connection refused" in low:
        return "Connection refused. Check that the service is running and reachable."
    if "timed out" in low or "timeout" in low:
        return "Connection timed out. Check reachability and TLS settings."
    return msg


def _serialize_settings_for_edit(ctype: str, name: str, settings_blob) -> str:
    settings = from_json(settings_blob, {})
    if not isinstance(settings, dict):
        settings = {}
    out = dict(settings)
    for key in _secret_keys_for(ctype):
        value = out.get(key)
        if value and isinstance(value, str):
            try:
                out[key] = decrypt_secret(value)
            except (SecretDecryptionError, SecretCipherUnavailable):
                out[key] = ""
    settings = out
    return json.dumps(settings, indent=2, sort_keys=True)


def _settings_dict_for_edit(ctype: str, name: str, settings_blob) -> dict:
    raw = _serialize_settings_for_edit(ctype, name, settings_blob)
    parsed = from_json(raw, {})
    return parsed if isinstance(parsed, dict) else {}


def _notification_settings_defaults(ctype: str) -> dict:
    out = {}
    for field in NOTIFICATION_FIELD_SCHEMAS.get(ctype, ()):
        if "default" in field:
            out[field["name"]] = field["default"]
    return out


def _coerce_notification_field(field: dict, raw_value):
    if raw_value is None:
        return None
    value = str(raw_value).strip()
    if value == "":
        return field.get("default") if "default" in field else None
    input_type = field.get("input_type")
    if input_type == "number":
        try:
            return int(value)
        except ValueError:
            return None
    if input_type == "select":
        return value.upper()
    return value


def _notification_settings_from_form(form, ctype: str, *, base_settings: dict | None = None,
                                     original_type: str | None = None) -> tuple[dict | None, str | None]:
    mode = (form.get("settings_mode") or "").strip().lower()
    raw_settings = str(form.get("settings") or "").strip()
    schema = NOTIFICATION_FIELD_SCHEMAS.get(ctype, ())
    has_structured_fields = any(field["name"] in form for field in schema)
    if mode == "advanced" or ctype in ADVANCED_ONLY_CONNECTION_TYPES:
        try:
            parsed = json.loads(raw_settings or "{}")
        except Exception:
            return None, "Settings JSON is invalid"
        if not isinstance(parsed, dict):
            return None, "Settings JSON must be an object"
        return parsed, None

    if raw_settings and not has_structured_fields:
        try:
            parsed = json.loads(raw_settings)
        except Exception:
            return None, "Settings JSON is invalid"
        if not isinstance(parsed, dict):
            return None, "Settings JSON must be an object"
        return parsed, None

    settings = dict(base_settings or {})
    if original_type and original_type != ctype:
        settings = {}
    for field in schema:
        value = _coerce_notification_field(field, form.get(field["name"]))
        if value is None:
            settings.pop(field["name"], None)
        else:
            settings[field["name"]] = value
    return settings, None


# ── List ──────────────────────────────────────────────────────────────────────
@router.get("/notifications", response_class=HTMLResponse)
async def notifications_page(request: Request):
    with get_db() as db:
        connections = []
        for row in _all_connections(db):
            conn = dict(row)
            conn["settings_display"] = _serialize_settings_for_edit(
                conn.get("type") or "",
                conn.get("name") or "?",
                conn.get("settings"),
            )
            conn["settings_data"] = _settings_dict_for_edit(
                conn.get("type") or "",
                conn.get("name") or "?",
                conn.get("settings"),
            )
            connections.append(conn)
        secret_health = get_secret_health_summary(db)
    return templates.TemplateResponse(request, "notification_connections.html", {
        "connections":      connections,
        "connection_types": CONNECTION_TYPES,
        "event_flags":      EVENT_FLAGS,
        "secret_health":    secret_health,
        "notification_field_schemas": NOTIFICATION_FIELD_SCHEMAS,
        "advanced_only_connection_types": ADVANCED_ONLY_CONNECTION_TYPES,
        "notification_defaults_by_type": {
            t: _notification_settings_defaults(t) for t in CONNECTION_TYPES
        },
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
    settings_dict, err = _notification_settings_from_form(form, type)
    if err:
        return JSONResponse({"ok": False, "message": err}, status_code=400)
    settings_dict = _encrypt_secret_fields(type, settings_dict or {})
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
async def edit_notification_connection(request: Request, conn_id: int):
    """Edit a notification connection. Partial-POST safe: only columns
    whose form key is present in the request body are written. The
    settings JSON is special-cased — when only some setting keys are
    submitted (e.g. tweaking just the webhook URL), they're merged
    onto the existing stored settings rather than replacing wholesale.
    """
    from routers._form_helpers import submitted_subset, bool_int
    form = await request.form()

    with get_db() as db:
        current = db.execute(
            "SELECT type, settings FROM notification_connections WHERE id=?",
            (conn_id,)
        ).fetchone()
        if not current:
            return RedirectResponse("/notifications", status_code=303)

        # Resolve the type for settings validation. If the form sends
        # `type`, use it (the user is changing connection type — settings
        # validation runs against the new type's schema). Otherwise fall
        # back to the stored type so we can re-encrypt secrets correctly.
        eff_type = (form.get('type') or current['type']) or ''
        eff_type = str(eff_type).strip()

        plain_fields = {
            'name':    ('name',    lambda v: str(v or '').strip()),
            'type':    ('type',    lambda v: str(v or '').strip()),
            'enabled': ('enabled', bool_int),
        }
        updates, params = submitted_subset(form, plain_fields)

        # Settings: only re-derive if the form carries `settings` OR any
        # type-specific setting field. The helper returns the merged
        # dict on top of the base. If neither type nor any setting is in
        # the form, leave the column alone.
        any_settings_field = any(
            k.startswith('settings_') or k == 'settings' for k in form.keys()
        )
        if any_settings_field or 'type' in form:
            current_settings = from_json(current['settings'], {})
            if not isinstance(current_settings, dict):
                current_settings = {}
            settings_dict, err = _notification_settings_from_form(
                form,
                eff_type,
                base_settings=current_settings,
                original_type=current['type'],
            )
            if err:
                return JSONResponse({"ok": False, "message": err}, status_code=400)
            settings_dict = _encrypt_secret_fields(eff_type, settings_dict or {})
            updates.append('settings=?')
            params.append(json.dumps(settings_dict))

        # Event flags: per-flag conditional. With the HTML page's
        # hidden-input-first idiom, every flag is always submitted, so
        # the full-form path still hits every column. Scripted partial
        # POSTs that send only one flag won't clobber the others.
        for flag, _label in EVENT_FLAGS:
            if flag in form:
                updates.append(f"{flag}=?")
                params.append(bool_int(form[flag]))

        if updates:
            params.append(conn_id)
            db.execute(
                f"UPDATE notification_connections SET {', '.join(updates)} WHERE id=?",
                params
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


@router.post("/api/notifications/test-form")
async def test_notification_connection_form(
    request: Request,
    name: str = Form("Unsaved notification"),
    type: str = Form(...),
    settings: str = Form("{}"),
):
    form = await request.form()
    settings_dict, err = _notification_settings_from_form(form, type)
    if err:
        return JSONResponse({"ok": False, "message": err})
    ok, msg = await send_connection(
        {"name": name.strip() or "Unsaved notification", "type": type, "settings": json.dumps(settings_dict or {})},
        "Test notification from Mangarr",
        event="test",
    )
    return JSONResponse({"ok": ok, "message": msg})


# ── Core send function ────────────────────────────────────────────────────────
async def send_connection(conn: dict, message: str,
                          event: str = "", embed: dict | None = None) -> tuple[bool, str]:
    """Send a notification via a single connection. Returns (ok, message)."""
    t        = conn['type']
    settings = from_json(conn.get('settings'), {})
    if isinstance(settings, dict):
        settings = _decrypt_secret_fields(t, conn.get('name') or '?', settings)

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
        return False, _friendly_connection_error(e)


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
