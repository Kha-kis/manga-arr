"""Operator-triggered reconciliation of chapter→volume links, plus a
read-only metadata-readiness report that explains why a given series
isn't ready for reconciliation (and what supported path will fix it).

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


# ── Metadata readiness report ────────────────────────────────────────────────
# Answers: "is this series in a state where reconcile_series_chapter_map
# will produce useful output, and if not, exactly what needs fixing?"
# Strict read-only — never mutates. Callers decide the next step
# (usually the series editor's total_volumes field, which invokes the
# supported create_volume_stubs path).

_BLOCKER_NEEDS_TOTAL_VOLUMES = 'needs_total_volumes'
_BLOCKER_NEEDS_CHAPTER_VOL_MAP = 'needs_chapter_vol_map'
_BLOCKER_MISSING_MAINLINE_STUBS = 'missing_mainline_stubs'
_BLOCKER_UNLINKED_CHAPTERS = 'unlinked_chapters'
_BLOCKER_SPECIAL_BLOCKS_MAINLINE = 'special_blocks_mainline'


def metadata_readiness_report(series_id: int) -> dict:
    """Inspect a series and classify its metadata readiness.

    Returns a dict with the fields below. Never mutates. The
    ``ready`` flag is True only when ``blockers`` is empty and the
    reconciler has enough information to do useful work.

    Blocker codes:
      - needs_total_volumes       total_volumes is NULL/0 — caller
                                  must fill this via the series editor
                                  (which triggers create_volume_stubs)
                                  or via the MangaDex/AniList refresh
                                  background task.
      - needs_chapter_vol_map     chapter_vol_map is absent — run a
                                  series-metadata refresh before any
                                  reconciliation.
      - missing_mainline_stubs    one or more mainline volume stubs
                                  (volume_num IN 1..total_volumes)
                                  don't exist as rows. These are
                                  created by the standard series-
                                  editor save path (see
                                  routers/series_.py:1257).
      - unlinked_chapters         chapters exist with volume_id IS NULL;
                                  after stubs are in place,
                                  populate_chapters will link them.
      - special_blocks_mainline   a special row shares a volume_num
                                  with the mainline series, which will
                                  cause create_volume_stubs to skip
                                  that mainline vol. This is a known
                                  edge case (see audit notes) — rare
                                  enough to flag rather than auto-fix.
    """
    from shared import get_db
    report: dict = {
        'series_id':               series_id,
        'title':                   None,
        'total_volumes':           None,
        'total_chapters':          None,
        'chapter_vol_map_size':    0,
        'existing_vol_nums':       [],
        'expected_vol_nums':       [],
        'missing_mainline_stubs':  [],
        'downloaded_with_num':     0,
        'wanted_pack_rows':        0,
        'special_count':           0,
        'unlinked_chapters':       0,
        'blockers':                [],
        'ready':                   False,
        'recommended_next_step':   '',
    }

    with get_db() as db:
        s = db.execute(
            "SELECT id, title, total_volumes, total_chapters, chapter_vol_map"
            "  FROM series WHERE id=?", (series_id,)
        ).fetchone()
        if not s:
            report['recommended_next_step'] = f"series_id {series_id} not found"
            return report

        report['title']          = s['title']
        report['total_volumes']  = s['total_volumes']
        report['total_chapters'] = s['total_chapters']

        import json
        cvm: dict = {}
        if s['chapter_vol_map']:
            try:
                loaded = json.loads(s['chapter_vol_map'])
                if isinstance(loaded, dict):
                    cvm = loaded
            except (ValueError, TypeError):
                pass
        report['chapter_vol_map_size'] = len(cvm)

        vols = list(db.execute(
            "SELECT volume_num, status, COALESCE(is_special, 0) AS is_special,"
            " pack_type, vol_range_start, vol_range_end"
            " FROM volumes WHERE series_id=?",
            (series_id,)
        ).fetchall())
        existing_mainline: set[float] = set()
        for v in vols:
            if v['is_special']:
                report['special_count'] += 1
                continue
            if v['volume_num'] is None:
                report['wanted_pack_rows'] += 1
                continue
            existing_mainline.add(float(v['volume_num']))
            if v['status'] == 'downloaded':
                report['downloaded_with_num'] += 1
        report['existing_vol_nums'] = sorted(existing_mainline)

        # Track specials' volume_num separately so we can flag the
        # special_blocks_mainline case where a special would prevent
        # create_volume_stubs from creating the mainline row.
        special_vol_nums = {
            float(v['volume_num']) for v in vols
            if v['is_special'] and v['volume_num'] is not None
        }

        if s['total_volumes'] and s['total_volumes'] > 0:
            expected = [float(i) for i in range(1, int(s['total_volumes']) + 1)]
            report['expected_vol_nums'] = expected
            missing = [v for v in expected if v not in existing_mainline]
            report['missing_mainline_stubs'] = missing
            blocked_by_special = [v for v in missing if v in special_vol_nums]
            if blocked_by_special:
                report['blockers'].append(_BLOCKER_SPECIAL_BLOCKS_MAINLINE)
            if missing:
                report['blockers'].append(_BLOCKER_MISSING_MAINLINE_STUBS)
        else:
            report['blockers'].append(_BLOCKER_NEEDS_TOTAL_VOLUMES)

        if not cvm:
            report['blockers'].append(_BLOCKER_NEEDS_CHAPTER_VOL_MAP)

        report['unlinked_chapters'] = db.execute(
            "SELECT COUNT(*) FROM chapters"
            " WHERE series_id=? AND volume_id IS NULL",
            (series_id,)
        ).fetchone()[0]
        if report['unlinked_chapters'] > 0:
            report['blockers'].append(_BLOCKER_UNLINKED_CHAPTERS)

    report['ready'] = not report['blockers']
    report['recommended_next_step'] = _recommend_next_step(report)
    return report


def _recommend_next_step(r: dict) -> str:
    """One actionable sentence describing what the operator should do
    next. Ordered so the earliest blocker wins — fixing a later blocker
    before an earlier one often doesn't stick."""
    blockers = r['blockers']
    if _BLOCKER_NEEDS_TOTAL_VOLUMES in blockers:
        return (
            "Open the series editor and set 'Total Volumes' (or run a "
            "MangaDex/AniList metadata refresh). This triggers "
            "create_volume_stubs and populates missing mainline stubs."
        )
    if _BLOCKER_NEEDS_CHAPTER_VOL_MAP in blockers:
        return (
            "Run 'Refresh MangaDex map' for this series — reconciliation "
            "needs an explicit chapter→volume map to work."
        )
    if _BLOCKER_SPECIAL_BLOCKS_MAINLINE in blockers:
        vols = [f"vol {int(v)}" for v in r['missing_mainline_stubs']
                if v in {float(x) for x in r['existing_vol_nums']}]
        return (
            "A special/side-story row shares a volume_num with missing "
            "mainline stubs; resolve in the series editor before "
            "reconciling."
        )
    if _BLOCKER_MISSING_MAINLINE_STUBS in blockers:
        n = len(r['missing_mainline_stubs'])
        return (
            f"{n} mainline volume stub(s) missing — open the series "
            "editor and save (total_volumes triggers stub creation)."
        )
    if _BLOCKER_UNLINKED_CHAPTERS in blockers:
        return (
            f"{r['unlinked_chapters']} chapter row(s) have volume_id=NULL. "
            "Running a series refresh calls populate_chapters which "
            "links them via chapter_vol_map."
        )
    return "ready — reconcile_series_chapter_map will produce useful output"
