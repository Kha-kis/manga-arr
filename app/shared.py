"""
shared.py — Shared database + config primitives.
Imported by both main.py and all router modules to avoid circular imports.
"""
import json
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = "/config/manga_arr.db"

# ── In-memory config (populated at startup by load_config) ────────────────────
CONFIG: dict = {}

def get_cfg(key: str, default: str = '') -> str:
    return CONFIG.get(key, default)

# ── Database ──────────────────────────────────────────────────────────────────
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")   # safe with WAL, much faster
        conn.execute("PRAGMA busy_timeout=5000")     # wait up to 5s on lock instead of failing
        conn.execute("PRAGMA cache_size=-8000")      # 8MB cache (was 2MB)
        conn.execute("PRAGMA mmap_size=67108864")    # 64MB memory-mapped I/O
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


# ── Tiny helpers used in routers ─────────────────────────────────────────────
def fmt_bytes(n: int) -> str:
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != 'B' else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"


def is_htmx(request) -> bool:
    """Return True if the request was made by HTMX (hx-* attribute or hx-request header)."""
    return request.headers.get("HX-Request") == "true"


def is_boosted(request) -> bool:
    """Return True if the request is an HTMX boosted navigation."""
    return request.headers.get("HX-Boosted") == "true"


def from_json(v, default=None):
    """Safe JSON decode."""
    if not v:
        return default
    try:
        return json.loads(v)
    except Exception:
        return default


def cascade_chapters(db, series_id: int, volume_ids, status: str, **kwargs) -> int:
    """Cascade a status change to chapters belonging to the given volume IDs.

    volume_ids=None cascades to ALL chapters for the series.
    kwargs: optional column=value pairs (grabbed_at, torrent_name, torrent_url,
            indexer, protocol, client, download_id, release_group, size_bytes).
    Only updates monitored=1 chapters. Returns count of updated rows.
    """
    allowed_cols = {
        'grabbed_at', 'torrent_name', 'torrent_url', 'indexer',
        'protocol', 'client', 'download_id', 'release_group', 'size_bytes',
        'import_path',
    }
    extra_cols = [c for c in kwargs if c in allowed_cols]
    extra_vals = [kwargs[c] for c in extra_cols]
    set_parts  = ['status=?'] + [f'{c}=?' for c in extra_cols]
    set_clause = ', '.join(set_parts)
    base_vals  = [status] + extra_vals

    if volume_ids is None:
        cur = db.execute(
            f"UPDATE chapters SET {set_clause} WHERE series_id=? AND monitored=1",
            base_vals + [series_id]
        )
    else:
        if not volume_ids:
            return 0
        ph = ','.join('?' * len(volume_ids))
        cur = db.execute(
            f"UPDATE chapters SET {set_clause}"
            f" WHERE series_id=? AND volume_id IN ({ph}) AND monitored=1",
            base_vals + [series_id] + list(volume_ids)
        )
    return cur.rowcount


# ── Volume / quality helpers (shared between main + routers) ──────────────────

QUALITY_RANK: dict[str, int] = {
    'cbz':  5,
    'zip':  5,
    'cbr':  4,
    'rar':  4,
    'epub': 3,
    'mobi': 2,
    'pdf':  1,
}


def quality_rank(q: str | None) -> int:
    """Return numeric rank for a quality string. None/unknown = 0."""
    return QUALITY_RANK.get((q or '').lower(), 0)


def vol_num_to_display(vol_num) -> str:
    """Format a float volume number for human display.
    None->''  3.0->3  3.01->3a  3.02->3b  3.5->3½  3.25->3¼  3.75->3¾  3.14->3.14
    """
    if vol_num is None:
        return ''
    _INT_TO_LETTER = {1: 'a', 2: 'b', 3: 'c', 4: 'd'}
    _INT_TO_FRAC   = {50: '½', 25: '¼', 75: '¾'}
    try:
        base = int(vol_num)
        frac = round((float(vol_num) - base) * 100)
    except (TypeError, ValueError):
        return str(vol_num)
    if frac == 0:
        return str(base)
    if frac in _INT_TO_LETTER:
        return f"{base}{_INT_TO_LETTER[frac]}"
    if frac in _INT_TO_FRAC:
        return f"{base}{_INT_TO_FRAC[frac]}"
    return f"{float(vol_num):g}"


def build_volume_label(vol_num, vol_range, pack_type) -> str:
    """Build a human-readable label like 'Vol 5', 'Vol 1–5', 'Complete Series', 'Pack'."""
    if vol_num is not None:
        return f"Vol {vol_num_to_display(vol_num)}"
    if pack_type == 'complete':
        return "Complete Series"
    if pack_type == 'chapter':
        return "Chapter"
    if vol_range:
        return f"Vol {vol_num_to_display(vol_range[0])}–{vol_num_to_display(vol_range[1])}"
    return "Pack"


def get_root_folders(db) -> list:
    return db.execute(
        "SELECT * FROM root_folders ORDER BY is_default DESC, label, path"
    ).fetchall()
