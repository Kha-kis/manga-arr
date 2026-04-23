"""Small shared helpers: root-folder resolution, formatters, Jinja filters.

Fourteenth module extracted from main.py. Pulls together a handful of
small utility functions that were living near the bottom of main.py:

Root-folder helpers (series → library destination):
  - get_root_folders          — list rows, default first
  - resolve_root_folder_id    — pick the id a new series should carry
  - _resolve_series_dest_root — resolve a series row's destination
                                path, with a graceful fallback when
                                the referenced folder was deleted
  - get_series_stats          — volume-status counts for a series

Display / template helpers:
  - format_bytes              — byte count → '1.4 GB' etc.
  - format_protocol           — 'torrent' → 'Torrent', etc.
  - format_client             — 'qbittorrent' → 'qBittorrent', etc.
  - _from_json                — safe json.loads (returns {} on error)
  - _ch_label_filter          — Jinja filter: render chapter number
                                honouring chapter_range_end
  - _get_api_key_global       — Jinja global: current api_key (or '')

`log_event` is imported lazily inside _resolve_series_dest_root to
avoid an import cycle.
"""
from __future__ import annotations

import json

from shared import get_cfg


def get_root_folders(db) -> list:
    return db.execute(
        "SELECT * FROM root_folders ORDER BY is_default DESC, label, path"
    ).fetchall()


def resolve_root_folder_id(db, preferred_id: int | None = None) -> int | None:
    """Pick the root_folder_id a newly-created series should carry.

    Order of preference:
      1. ``preferred_id`` if it refers to an existing row.
      2. The folder flagged ``is_default=1``.
      3. The lowest-id folder (safety net if no default is flagged).

    Returns None only when no root folders exist at all — callers are
    expected to check and surface a clear error to the operator instead
    of silently leaving root_folder_id NULL. Requiring a folder at
    creation time matches the Sonarr/Radarr model and removes the
    save_path fallback that used to paper over this case.
    """
    if preferred_id:
        ok = db.execute(
            "SELECT 1 FROM root_folders WHERE id=?", (preferred_id,)
        ).fetchone()
        if ok:
            return preferred_id
    row = db.execute(
        "SELECT id FROM root_folders ORDER BY is_default DESC, id LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def _resolve_series_dest_root(db, series_rf_id: int | None, rf_row) -> str:
    """Return the library destination root path for a series.

    Assumes PR B's guarantee that every series has a root_folder_id at
    creation time. Handles the edge case where an operator deletes a
    root folder that still has series pointing at it — in that case we
    fall back to any remaining folder and log a warning so the operator
    can re-assign.

    If no root folders exist at all the caller has a bigger problem
    than this function can solve — raises RuntimeError with a clear
    message rather than silently landing imports in a half-configured
    path.
    """
    # Happy path: series has a folder and the row exists.
    if rf_row:
        return rf_row['path']
    # Edge: series references a deleted folder, or was never assigned.
    # Fall back to any available folder and log.
    fallback = resolve_root_folder_id(db)
    if fallback is not None:
        fb_row = db.execute(
            "SELECT path FROM root_folders WHERE id=?", (fallback,)
        ).fetchone()
        from main import log_event  # noqa: WPS433 (lazy to avoid cycle)
        log_event(
            'warning',
            f"series root_folder_id={series_rf_id!r} did not resolve; "
            f"falling back to root_folder_id={fallback} ({fb_row['path']!r}). "
            f"Re-assign the series to an existing folder in the editor.",
            db=db,
        )
        return fb_row['path']
    # Terminal: no folders at all.
    raise RuntimeError(
        "No root folders configured. Add one in Settings before "
        "attempting to import or place files."
    )


def get_series_stats(db, series_id: int) -> dict:
    """Stats are based only on volume stubs (not pack entries)."""
    rows = db.execute(
        "SELECT status FROM volumes WHERE series_id=? AND volume_num IS NOT NULL",
        (series_id,)
    ).fetchall()
    total      = len(rows)
    wanted     = sum(1 for r in rows if r['status'] == 'wanted')
    grabbed    = sum(1 for r in rows if r['status'] == 'grabbed')
    downloaded = sum(1 for r in rows if r['status'] == 'downloaded')
    return {
        'total': total, 'wanted': wanted,
        'grabbed': grabbed, 'downloaded': downloaded,
        'have': grabbed + downloaded,
    }


def format_bytes(n) -> str:
    if not n:
        return ''
    n = int(n)
    for unit in ['B', 'KB', 'MB', 'GB']:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def format_protocol(p: str) -> str:
    if not p:
        return ''
    return {'torrent': 'Torrent', 'nzb': 'NZB', 'ddl': 'DDL'}.get(p, p)


def format_client(c: str) -> str:
    if not c:
        return ''
    return {'qbittorrent': 'qBittorrent', 'sabnzbd': 'SABnzbd', 'suwayomi': 'Suwayomi'}.get(c, c)


def _from_json(s):
    try:
        return json.loads(s) if s else {}
    except Exception:
        return {}


def _ch_label_filter(row) -> str:
    """Jinja filter: render a chapter row's number, honoring chapter_range_end.

    `row` is a dict-like (sqlite3.Row or dict) exposing chapter_num and,
    optionally, chapter_range_end. Returns "1", "1.5", or "1-2".
    """
    if row is None:
        return ""
    try:
        n = row["chapter_num"]
    except (KeyError, IndexError, TypeError):
        return ""
    if n is None:
        return ""
    end = None
    try:
        end = row["chapter_range_end"]
    except (KeyError, IndexError, TypeError):
        end = None
    n_disp = int(n) if n == int(n) else n
    if end is not None and end > n:
        e_disp = int(end) if end == int(end) else end
        return f"{n_disp}-{e_disp}"
    return f"{n_disp}"


def _get_api_key_global() -> str:
    try:
        return get_cfg('api_key', '')
    except Exception:
        return ''
