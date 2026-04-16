"""
shared.py — Shared database + config primitives.
Imported by both main.py and all router modules to avoid circular imports.
"""
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
        except Exception as _rb:
            # Rollback itself failed — usually means the connection is
            # already dead. Surface the ORIGINAL exception via `raise` below
            # (don't mask it with the rollback error), but log the rollback
            # failure so operators can see connection-level corruption.
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "get_db: rollback failed (connection may be corrupt): %r", _rb,
            )
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


def get_secret_health_summary(db=None) -> dict:
    """Summarize whether encrypted credentials are currently unreadable.

    This is intentionally UI-oriented and side-effect free: it inspects the
    stored encrypted values directly and attempts decryption without emitting
    extra warning logs. Operators need a visible recovery hint when the active
    key no longer matches the DB, not just backend log lines.
    """
    from security import (
        SecretCipherUnavailable,
        SecretDecryptionError,
        decrypt_secret,
        is_encrypted_secret,
    )
    from main import NOTIFICATION_SECRET_KEYS_BY_TYPE, SETTINGS_SECRET_KEYS

    owns_db = db is None
    if owns_db:
        db_cm = get_db()
        db = db_cm.__enter__()

    try:
        affected: list[str] = []
        encrypted_present = False

        def _check(label: str, value):
            nonlocal encrypted_present
            if not is_encrypted_secret(value):
                return
            encrypted_present = True
            try:
                decrypt_secret(value)
            except (SecretDecryptionError, SecretCipherUnavailable):
                affected.append(label)

        placeholders = ",".join("?" * len(SETTINGS_SECRET_KEYS))
        settings_rows = db.execute(
            f"SELECT key, value FROM settings WHERE key IN ({placeholders})",
            tuple(SETTINGS_SECRET_KEYS),
        ).fetchall()
        for row in settings_rows:
            _check(f"Setting: {row['key']}", row["value"])

        for row in db.execute(
            "SELECT name, api_key FROM indexers WHERE api_key IS NOT NULL AND api_key != ''"
        ).fetchall():
            _check(f"Indexer: {row['name']}", row["api_key"])

        for row in db.execute(
            "SELECT name, password FROM download_clients WHERE password IS NOT NULL AND password != ''"
        ).fetchall():
            _check(f"Download Client: {row['name']}", row["password"])

        for row in db.execute(
            "SELECT name, type, settings FROM notification_connections WHERE settings IS NOT NULL AND settings != ''"
        ).fetchall():
            blob = from_json(row["settings"], {})
            if not isinstance(blob, dict):
                continue
            for key in NOTIFICATION_SECRET_KEYS_BY_TYPE.get(row["type"] or "", ()):
                _check(f"Notification: {row['name']} ({key})", blob.get(key))

        return {
            "has_warning": bool(affected),
            "encrypted_present": encrypted_present,
            "affected_count": len(affected),
            "affected_items": affected[:5],
        }
    finally:
        if owns_db:
            db_cm.__exit__(None, None, None)


def with_flash(path: str, msg: str, type: str = "info") -> str:
    """Append a one-shot toast payload to a redirect URL."""
    parts = urlsplit(path)
    qs = dict(parse_qsl(parts.query, keep_blank_values=True))
    qs["flash_msg"] = msg
    qs["flash_type"] = type
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(qs), parts.fragment))


# ── SQL ORDER BY allowlist helper ─────────────────────────────────────────────
# Any endpoint that builds ORDER BY from request params MUST route through
# build_order_by. It guarantees only values the caller hardcoded in `allowed`
# end up in the emitted SQL — no interpolation of raw request strings.

_VALID_ORDER_DIRECTIONS = frozenset({"asc", "desc"})


def build_order_by(sort_key: str, *,
                   allowed: "dict[str, str]",
                   default_key: str,
                   direction: "str | None" = None) -> str:
    """Build a safe ORDER BY fragment from an allowlist.

    Never interpolates request values into SQL. Only values present in
    `allowed` (hardcoded by the caller) can appear in the returned string.

    Args:
      sort_key      — request-supplied sort key. Unknown / missing values
                      fall back to `default_key`.
      allowed       — {public_sort_key: SQL fragment}. Fragments may
                      already include a direction (e.g. "added_at DESC")
                      or be column-only; the `direction` parameter below
                      can append one either way.
      default_key   — fallback sort key; MUST be a key in `allowed`.
      direction     — optional "asc" / "desc" (case-insensitive). When
                      set and valid, appended as " ASC" / " DESC".
                      Callers that bake direction into their allowed
                      fragments should omit this.

    Raises ValueError only on caller misuse (default_key not in allowed).
    Never raises on bad request values — those silently fall back to the
    default, mirroring existing endpoint behavior.
    """
    if default_key not in allowed:
        raise ValueError(f"default_key {default_key!r} not in allowed map")
    column = allowed[sort_key] if sort_key in allowed else allowed[default_key]
    if direction is None:
        return column
    d = (direction or "").strip().lower()
    if d not in _VALID_ORDER_DIRECTIONS:
        return column   # invalid / empty direction: return column alone
    return f"{column} {d.upper()}"


# ── SQL identifier / typedef validators ───────────────────────────────────────
# Used by add_col() and any other helper that must interpolate table or
# column names into SQL. `?` placeholders can't bind identifiers, so
# validation is the only defence when these come from any source other
# than a hardcoded literal. Today's callers are all hardcoded; these
# guards prevent a future refactor from silently introducing injection.

_SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

# Matches the shape of every typedef currently passed to add_col, nothing
# more. Explicitly:
#   BASE_TYPE                                     (INTEGER, REAL, TEXT, …)
#   BASE_TYPE DEFAULT <literal>                   (numeric, string, NULL, CURRENT_TIMESTAMP)
#   BASE_TYPE REFERENCES table[(column)]          (single FK)
#   BASE_TYPE DEFAULT <literal> REFERENCES …      (combination)
# Quoted string defaults must NOT contain ;, ), or quote chars — no escape
# tricks can slip through.
_SQL_TYPEDEF_RE = re.compile(
    r"""
    ^
    (INTEGER|REAL|TEXT|BLOB|NUMERIC|TIMESTAMP)              # base type
    (
        \s+DEFAULT\s+
        (
            -?\d+(\.\d+)?                                    # numeric literal
          | '[^';\\\n\r"]*'                                   # single-quoted string
          | "[^";\\\n\r]*"                                    # double-quoted string
          | NULL
          | CURRENT_TIMESTAMP
        )
    )?
    (
        \s+REFERENCES\s+
        [A-Za-z_][A-Za-z0-9_]{0,63}                          # table name
        (\(\s*[A-Za-z_][A-Za-z0-9_]{0,63}\s*\))?             # optional column
    )?
    $
    """,
    re.VERBOSE | re.IGNORECASE,
)


def validate_sql_identifier(name: str, kind: str = "identifier") -> str:
    """Return `name` if it matches the strict identifier pattern, else raise.

    Identifiers are table/column/index names that SQL placeholders can't
    bind. Must start with a letter or underscore, followed by up to 63
    letters/digits/underscores.
    """
    if not isinstance(name, str) or not _SQL_IDENTIFIER_RE.match(name):
        raise ValueError(f"invalid SQL {kind}: {name!r}")
    return name


def validate_sql_typedef(typedef: str) -> str:
    """Return `typedef` if it matches the conservative typedef pattern,
    else raise. Accepts the shapes used by the existing migrations:
    base type, optional DEFAULT with a literal, optional REFERENCES."""
    if not isinstance(typedef, str) or not _SQL_TYPEDEF_RE.match(typedef.strip()):
        raise ValueError(f"invalid SQL typedef: {typedef!r}")
    return typedef
