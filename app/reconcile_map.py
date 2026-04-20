"""Operator-triggered reconciliation of chapter→volume links.

Problem:
    When MangaDex (or Kitsu, or the operator via the series editor)
    updates `series.chapter_vol_map`, Mangarr's pre-existing `chapters`
    rows keep the `volume_id` they were assigned at import time. The
    `populate_chapters` path only fills NULL links (see
    `app/main.py:1347`). Users reported systematic wrong-volume
    assignment from this drift.

What this module does:
    - Compares every chapter row for a series against the CURRENT
      `chapter_vol_map`.
    - In `dry_run=True` mode, reports which rows would move, which
      are blocked (and why), and mutates nothing.
    - In `dry_run=False` mode, applies ONLY the rows flagged
      `safe_to_apply=True`. Everything else is intentionally skipped
      to keep the blast radius bounded.

What this module does NOT do:
    - Touch files on disk. `import_path` and `status` are never
      rewritten.
    - Walk the whole DB. Reconciliation is strictly series-scoped.
    - Run automatically. It exists behind an operator-triggered entry
      point; callers are responsible for wiring the UI/CLI button.
    - Migrate Suwayomi job rows. Frozen `suwayomi_downloads.volume_num`
      values only apply to IN-FLIGHT jobs; once the job completes the
      resulting chapter row is what this reconciler fixes.

Reason codes surfaced in the report:
    - `ok_move`                — safe reassignment available.
    - `already_correct`        — row's volume_id already matches map.
    - `no_map_entry`           — chapter number not in the map.
    - `target_volume_missing`  — map target has no mainline volume row.
    - `target_ambiguous`       — multiple mainline rows share the num.
    - `special_parent`         — row is linked to a special volume.
"""
from __future__ import annotations

import json
from typing import Any

from shared import get_db


def _get_chapter_vol_map(db, series_id: int) -> dict[str, Any]:
    """Return the current chapter_vol_map for a series, or {}."""
    row = db.execute(
        "SELECT chapter_vol_map FROM series WHERE id=?", (series_id,)
    ).fetchone()
    if not row or not row['chapter_vol_map']:
        return {}
    try:
        data = json.loads(row['chapter_vol_map'])
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _chapter_key_candidates(chapter_num: float) -> list[str]:
    """Produce the string forms under which a chapter may appear in the
    map. The map is JSON-serialised so keys are always strings — and
    both integer and decimal forms are used by different callers."""
    as_int = int(chapter_num)
    if chapter_num == as_int:
        return [str(as_int), f"{as_int}.0", str(chapter_num)]
    return [str(chapter_num)]


def _lookup_target_vol_num(chapter_num: float, cvm: dict[str, Any]) -> float | None:
    """Resolve chapter → target volume number from the map. Returns
    None when no entry exists. Non-numeric values in the map are
    treated as "no entry" — defensive against manually-edited maps."""
    for k in _chapter_key_candidates(chapter_num):
        if k in cvm:
            try:
                return float(cvm[k])
            except (ValueError, TypeError):
                return None
    return None


def _find_mainline_target(db, series_id: int, target_vol_num: float) -> tuple[list[int], list[int]]:
    """Return (mainline_ids, special_ids) for rows matching
    (series_id, volume_num = target_vol_num). Mainline excludes rows
    flagged is_special=1; the split is needed so the reconciler can
    report 'target_ambiguous' when multiple mainline rows exist and
    ignore specials (they're not mainline candidates)."""
    rows = db.execute(
        "SELECT id, COALESCE(is_special, 0) AS is_special"
        "  FROM volumes WHERE series_id=? AND volume_num=?",
        (series_id, target_vol_num)
    ).fetchall()
    mainline = [r['id'] for r in rows if not r['is_special']]
    special  = [r['id'] for r in rows if r['is_special']]
    return mainline, special


def _chapter_plan_row(db, series_id: int, chapter_row, cvm: dict) -> dict | None:
    """Classify one chapter row. Returns a plan dict or None when the
    row is in the 'already_correct' state we don't need to report.
    Shape of dict is documented in the module docstring."""
    ch_num = chapter_row['chapter_num']
    current_vol_id = chapter_row['volume_id']

    # What volume does the row currently point to?
    current_vol_num = None
    current_is_special = 0
    if current_vol_id is not None:
        row = db.execute(
            "SELECT volume_num, COALESCE(is_special, 0) AS is_special"
            "  FROM volumes WHERE id=?", (current_vol_id,)
        ).fetchone()
        if row:
            current_vol_num = row['volume_num']
            current_is_special = int(row['is_special'])

    # Never silently migrate a chapter attached to a special. The
    # operator has to resolve this by hand (might be intentional).
    if current_is_special:
        proposed_vol_num = _lookup_target_vol_num(ch_num, cvm)
        return {
            'chapter_id':             chapter_row['id'],
            'chapter_num':            ch_num,
            'current_volume_id':      current_vol_id,
            'current_volume_num':     current_vol_num,
            'proposed_volume_id':     None,
            'proposed_volume_num':    proposed_vol_num,
            'safe_to_apply':          False,
            'requires_manual_review': True,
            'reason':                 'special_parent',
        }

    proposed_vol_num = _lookup_target_vol_num(ch_num, cvm)
    if proposed_vol_num is None:
        # Chapter not in the map — nothing to say, no move to propose.
        return {
            'chapter_id':             chapter_row['id'],
            'chapter_num':            ch_num,
            'current_volume_id':      current_vol_id,
            'current_volume_num':     current_vol_num,
            'proposed_volume_id':     None,
            'proposed_volume_num':    None,
            'safe_to_apply':          False,
            'requires_manual_review': False,
            'reason':                 'no_map_entry',
        }

    if current_vol_num is not None and current_vol_num == proposed_vol_num:
        return {
            'chapter_id':             chapter_row['id'],
            'chapter_num':            ch_num,
            'current_volume_id':      current_vol_id,
            'current_volume_num':     current_vol_num,
            'proposed_volume_id':     current_vol_id,
            'proposed_volume_num':    proposed_vol_num,
            'safe_to_apply':          False,
            'requires_manual_review': False,
            'reason':                 'already_correct',
        }

    mainline_ids, _special_ids = _find_mainline_target(db, series_id, proposed_vol_num)
    if not mainline_ids:
        return {
            'chapter_id':             chapter_row['id'],
            'chapter_num':            ch_num,
            'current_volume_id':      current_vol_id,
            'current_volume_num':     current_vol_num,
            'proposed_volume_id':     None,
            'proposed_volume_num':    proposed_vol_num,
            'safe_to_apply':          False,
            'requires_manual_review': True,
            'reason':                 'target_volume_missing',
        }
    if len(mainline_ids) > 1:
        return {
            'chapter_id':             chapter_row['id'],
            'chapter_num':            ch_num,
            'current_volume_id':      current_vol_id,
            'current_volume_num':     current_vol_num,
            'proposed_volume_id':     None,
            'proposed_volume_num':    proposed_vol_num,
            'safe_to_apply':          False,
            'requires_manual_review': True,
            'reason':                 'target_ambiguous',
        }

    return {
        'chapter_id':             chapter_row['id'],
        'chapter_num':            ch_num,
        'current_volume_id':      current_vol_id,
        'current_volume_num':     current_vol_num,
        'proposed_volume_id':     mainline_ids[0],
        'proposed_volume_num':    proposed_vol_num,
        'safe_to_apply':          True,
        'requires_manual_review': False,
        'reason':                 'ok_move',
    }


def reconcile_series_chapter_map(series_id: int, dry_run: bool = True) -> dict:
    """Reconcile chapter rows against the current chapter_vol_map.

    Parameters
    ----------
    series_id:
        Scope is strictly one series per call.
    dry_run:
        True (default) → compute the plan, mutate nothing, return it.
        False → open ONE transaction and apply every row where
        safe_to_apply=True. Skipped rows get a log line so the
        operator can see what still needs attention.

    Returns a dict::

        {
            'series_id': int,
            'rows':      list[dict],   # one per affected chapter row
            'applied':   int,          # count of rows moved (0 in dry-run)
            'skipped':   int,          # count of unsafe rows
            'ok_move':   int,
            'target_volume_missing': int,
            'target_ambiguous':      int,
            'special_parent':        int,
            'no_map_entry':          int,
            'already_correct':       int,
        }
    """
    result: dict = {
        'series_id': series_id,
        'rows':      [],
        'applied':   0,
        'skipped':   0,
        'ok_move':              0,
        'target_volume_missing': 0,
        'target_ambiguous':      0,
        'special_parent':        0,
        'no_map_entry':          0,
        'already_correct':       0,
    }

    with get_db() as db:
        series = db.execute(
            "SELECT id, title FROM series WHERE id=?", (series_id,)
        ).fetchone()
        if not series:
            return result

        cvm = _get_chapter_vol_map(db, series_id)

        chapters = db.execute(
            "SELECT id, chapter_num, volume_id FROM chapters WHERE series_id=?"
            " ORDER BY chapter_num",
            (series_id,)
        ).fetchall()

        for ch in chapters:
            plan = _chapter_plan_row(db, series_id, ch, cvm)
            if plan is None:
                continue
            result['rows'].append(plan)
            result[plan['reason']] = result.get(plan['reason'], 0) + 1

        safe_rows = [r for r in result['rows'] if r['safe_to_apply']]
        result['skipped'] = sum(1 for r in result['rows']
                                if r['reason'] not in ('already_correct', 'ok_move'))

        if dry_run:
            return result

        # ── Apply ──
        # SQLite's get_db wrapper commits on __exit__. We don't open a
        # manual SAVEPOINT: if any UPDATE raises, the context manager's
        # rollback takes the whole batch down together. Each safe row
        # gets an independent history entry so an operator can audit
        # exactly what moved.
        from main import add_history  # local import avoids circular

        for r in safe_rows:
            db.execute(
                "UPDATE chapters SET volume_id=? WHERE id=? AND series_id=?",
                (r['proposed_volume_id'], r['chapter_id'], series_id)
            )
            result['applied'] += 1
            add_history(
                db, 'reconcile_chapter_vol', series_id,
                series['title'] or '', f"Ch {r['chapter_num']:g}",
                source_title=f"chapter {r['chapter_num']:g}",
                data={
                    'chapter_id':         r['chapter_id'],
                    'from_volume_id':     r['current_volume_id'],
                    'from_volume_num':    r['current_volume_num'],
                    'to_volume_id':       r['proposed_volume_id'],
                    'to_volume_num':      r['proposed_volume_num'],
                },
            )
    return result
