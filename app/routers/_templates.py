"""Shared Jinja2 templates instance with all custom filters for routers."""
import json
from datetime import datetime, timezone
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="/app/templates")

def _from_json(s, default=None):
    if not s:
        return default if default is not None else {}
    try:
        return json.loads(s)
    except Exception:
        return default if default is not None else {}

def _fmt_bytes(n):
    if not n:
        return '0 B'
    n = int(n)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != 'B' else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"

def _fmt_protocol(p: str) -> str:
    if not p:
        return ''
    return 'Torrent' if p == 'torrent' else 'NZB'

def _fmt_client(c: str) -> str:
    if not c:
        return ''
    return {'qbittorrent': 'qBittorrent', 'sabnzbd': 'SABnzbd'}.get(c, c)

def _vol_display(vol_num) -> str:
    from shared import vol_num_to_display
    return vol_num_to_display(vol_num) or '?'

def _quality_rank(q: str | None) -> int:
    from shared import quality_rank
    return quality_rank(q)

def _format_date(value, fmt: str | None = None) -> str:
    """Format a date string as relative ('2h ago') or absolute ('2026-04-10 14:30').
    fmt overrides the setting; if None, reads ui_date_format from config."""
    if not value:
        return '—'
    if fmt is None:
        try:
            from shared import get_cfg
            fmt = get_cfg('ui_date_format', 'relative')
        except Exception:
            fmt = 'relative'
    try:
        s = str(value)
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if fmt == 'absolute':
            return dt.strftime('%Y-%m-%d %H:%M')
        # relative
        now = datetime.now(timezone.utc)
        secs = int((now - dt).total_seconds())
        if secs < 0:
            return dt.strftime('%Y-%m-%d')
        if secs < 60:
            return 'just now'
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        if secs < 604800:
            return f"{secs // 86400}d ago"
        return dt.strftime('%Y-%m-%d')
    except Exception:
        return str(value)[:16]

def _get_instance_name() -> str:
    try:
        from shared import get_cfg
        return get_cfg('instance_name', '') or 'Mangarr'
    except Exception:
        return 'Mangarr'

templates.env.filters['from_json']       = _from_json
templates.env.filters['format_bytes']    = _fmt_bytes
templates.env.filters['format_protocol'] = _fmt_protocol
templates.env.filters['format_client']   = _fmt_client
templates.env.filters['vol_display']     = _vol_display
templates.env.filters['quality_rank']    = _quality_rank
templates.env.filters['format_date']     = _format_date

def _get_api_key() -> str:
    try:
        from shared import get_cfg
        return get_cfg('api_key', '')
    except Exception:
        return ''

templates.env.globals['get_api_key']       = _get_api_key
templates.env.globals['get_instance_name'] = _get_instance_name
