"""Volume and chapter stub helpers.

Tenth module extracted from main.py. These four DB helpers are the
core mechanics for keeping the volumes / chapters / chapter_vol_map
(cvm) tables consistent after metadata refreshes and grabs:

  - create_volume_stubs     — insert missing vol rows up to
                              total_volumes, link cvm chapters to
                              their vol stubs, honour monitor_mode
  - populate_chapters       — seed chapter rows from cvm (with
                              coverage guard against re-creating
                              `wanted` rows for already-imported ranges)
  - _check_volume_completion — promote a volume to `downloaded` once
                               every monitored chapter is downloaded
  - _cascade_chapters       — bulk status + metadata cascade from a
                              volume row to its chapters

All four take a live `db` connection and perform writes only within
the caller's transaction. Pure SQL — no file I/O, no HTTP.
"""
from __future__ import annotations

import json


def create_volume_stubs(db, series_id: int, total_volumes: int):
    existing = {
        r['volume_num']
        for r in db.execute(
            "SELECT volume_num FROM volumes WHERE series_id=? AND volume_num IS NOT NULL",
            (series_id,)
        ).fetchall()
    }
    # Respect monitor_mode: 'existing' means only monitor volumes already in the library;
    # new stubs from a refresh should be unmonitored until the user explicitly enables them.
    s_row = db.execute("SELECT monitor_mode FROM series WHERE id=?", (series_id,)).fetchone()
    mode  = (s_row['monitor_mode'] if s_row else None) or 'all'
    # 'all' and 'missing' → monitor new stubs; 'existing' and 'none' → don't
    new_monitored = 1 if mode in ('all', 'missing') else 0
    for v in range(1, total_volumes + 1):
        if float(v) not in existing:
            db.execute(
                "INSERT INTO volumes(series_id, volume_num, status, monitored) VALUES(?,?,?,?)",
                (series_id, float(v), 'wanted', new_monitored)
            )
    # Link any unlinked chapter stubs to newly-created volume stubs
    s_row = db.execute(
        "SELECT chapter_vol_map FROM series WHERE id=?", (series_id,)
    ).fetchone()
    if s_row and s_row['chapter_vol_map']:
        try:
            ch_map = json.loads(s_row['chapter_vol_map'])
        except Exception:
            ch_map = {}
        vol_id_map = {
            r['volume_num']: r['id']
            for r in db.execute(
                "SELECT id, volume_num FROM volumes WHERE series_id=? AND volume_num IS NOT NULL",
                (series_id,)
            ).fetchall()
        }
        for ch_str, vol_num in ch_map.items():
            vol_id = vol_id_map.get(float(vol_num)) if vol_num is not None else None
            if not vol_id:
                continue
            try:
                ch_num = float(ch_str)
            except (ValueError, TypeError):
                continue
            db.execute(
                "UPDATE chapters SET volume_id=? WHERE series_id=? AND chapter_num=? AND volume_id IS NULL",
                (vol_id, series_id, ch_num)
            )


def populate_chapters(db, series_id: int) -> int:
    """Seed chapter stub rows from the series' chapter_vol_map JSON
    (MangaDex data). Idempotent — uses INSERT OR IGNORE so re-running
    is safe. Links each chapter to its volume stub via volume_id. Also
    updates volume_id on existing unlinked chapters. Returns count of
    newly created rows."""
    row = db.execute(
        "SELECT chapter_vol_map FROM series WHERE id=?", (series_id,)
    ).fetchone()
    if not row or not row['chapter_vol_map']:
        return 0
    try:
        ch_map: dict = json.loads(row['chapter_vol_map'])
    except Exception:
        return 0
    if not ch_map:
        return 0

    vol_id_map: dict[float, int] = {
        r['volume_num']: r['id']
        for r in db.execute(
            "SELECT id, volume_num FROM volumes WHERE series_id=? AND volume_num IS NOT NULL",
            (series_id,)
        ).fetchall()
    }

    created = 0
    for ch_str, vol_num in ch_map.items():
        try:
            ch_num = float(ch_str)
        except (ValueError, TypeError):
            continue
        vol_id = vol_id_map.get(float(vol_num)) if vol_num is not None else None

        # Exact-match unlinked-row linking takes precedence over the
        # coverage guard. If a chapter row with this exact chapter_num
        # already exists but isn't linked to a volume, we always want
        # to link it (that's what the cvm refresh is for). Without this
        # short-circuit the coverage guard below would treat any such
        # row as "covered" and skip both INSERT and UPDATE — leaving
        # legitimately downloaded chapters stranded with volume_id=NULL
        # after a cvm refresh.
        if vol_id:
            existing_unlinked = db.execute(
                "SELECT id FROM chapters"
                " WHERE series_id=? AND chapter_num=? AND volume_id IS NULL"
                " LIMIT 1",
                (series_id, ch_num)
            ).fetchone()
            if existing_unlinked:
                db.execute(
                    "UPDATE chapters SET volume_id=? WHERE id=?",
                    (vol_id, existing_unlinked['id'])
                )
                continue

        # Coverage guard: skip if this chapter is already covered by an
        # existing non-special range row (chapter_num <= ch_num <=
        # chapter_range_end). Without this guard, re-syncing chapter
        # metadata would re-create `wanted` placeholders for chapters
        # that a c001-002 pack already imported.
        #
        # Specials (parent volume is_special=1) must NOT suppress mainline
        # chapter creation — a Gaiden c001-002 grab shouldn't prevent the
        # mainline chapter 1 stub from existing.
        covered = db.execute(
            "SELECT 1 FROM chapters c"
            "  LEFT JOIN volumes v ON v.id = c.volume_id"
            " WHERE c.series_id=?"
            "   AND c.chapter_num <= ?"
            "   AND ? <= COALESCE(c.chapter_range_end, c.chapter_num)"
            "   AND COALESCE(v.is_special, 0) = 0"
            " LIMIT 1",
            (series_id, ch_num, ch_num)
        ).fetchone()
        if covered:
            continue
        cur = db.execute(
            "INSERT OR IGNORE INTO chapters(series_id, volume_id, chapter_num, status, monitored)"
            " VALUES(?,?,?,'wanted',1)",
            (series_id, vol_id, ch_num)
        )
        if cur.rowcount:
            created += 1
    return created


def _check_volume_completion(db, series_id: int, volume_id: int) -> bool:
    """If all monitored chapters in a volume are downloaded, mark the volume
    downloaded. Returns True if the volume was promoted to downloaded."""
    row = db.execute(
        "SELECT COUNT(*) as total,"
        " SUM(CASE WHEN status='downloaded' THEN 1 ELSE 0 END) as done"
        " FROM chapters WHERE series_id=? AND volume_id=? AND monitored=1",
        (series_id, volume_id)
    ).fetchone()
    if row and row['total'] > 0 and row['total'] == row['done']:
        db.execute(
            "UPDATE volumes SET status='downloaded',"
            " imported_at=COALESCE(imported_at, datetime('now'))"
            " WHERE id=? AND status != 'downloaded'",
            (volume_id,)
        )
        return True
    return False


def _cascade_chapters(db, series_id: int,
                      volume_ids: list[int] | None,
                      status: str,
                      **kwargs) -> int:
    """Cascade a status change to chapters belonging to the given volume IDs.
    volume_ids=None cascades to ALL chapters for the series.
    kwargs: optional column=value pairs (grabbed_at, torrent_name,
    torrent_url, indexer, protocol, client, download_id, release_group,
    size_bytes). Only updates monitored=1 chapters. Returns count of
    updated rows."""
    # NOTE: chapters table uses 'torrent_url' (volumes uses 'source_url').
    # Callers should pass torrent_url; source_url alias is intentionally NOT allowed.
    allowed_cols = {
        'grabbed_at', 'torrent_name', 'torrent_url', 'indexer',
        'protocol', 'client', 'download_id', 'release_group', 'size_bytes',
        'import_path', 'quality', 'imported_at',
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
