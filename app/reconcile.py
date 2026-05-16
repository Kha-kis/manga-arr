"""Operator-facing reconciliation: read-only report + dry-run repair planner.

Two modes (both read-only by default):

  python3 reconcile.py report [db_path]
      Human-readable rundown of every state-machine residue verify_e2e
      would flag, with affected ids, ages, and severity.

  python3 reconcile.py plan [db_path]
      Proposed repair actions, one per finding. Every action is dry-run
      only (would_mutate=True doesn't mean it will mutate — it means
      `apply` *would* mutate that row). No --apply flag is exposed in
      this pass; operators run the plan output through review first.

Library API:
  - report(db_path)  → list[Finding] (proxies verify_e2e.diagnose)
  - plan(db_path)    → list[RepairAction]

Design rules:
  - Never opens the DB read-write. Connections use mode=ro URI.
  - Never deletes files.
  - Never marks data healthy without evidence (e.g. ghost chapter is
    always requires_manual_review=True; we don't have the source file
    to verify it's recoverable).
  - Risk levels are per-action, not per-finding.

Background: pass-3 diagnostics surfaced 5 stuck-grabbed volumes, 43 ghost
chapters, and 13 import-queue rows in failed/importing/partial. The app
already auto-repairs *some* of these inside check_download_status, but
that path doesn't cover ghost chapters or stuck-importing rows whose
download_id is gone. This module gives the operator a structured plan
they can act on manually.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import dataclass, field, asdict
from typing import Literal

# Make 'verify_e2e' importable from siblings under /app/ without a package.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import verify_e2e  # noqa: E402

DEFAULT_DB_PATH = verify_e2e.DEFAULT_DB_PATH

RiskLevel = Literal["low", "medium", "high"]


@dataclass
class RepairAction:
    """One proposed action. Always dry-run.

    Fields:
      action:                 short verb describing what would happen
      target:                 (table, id) tuple identifying the affected row
      reason:                 human-readable justification
      risk:                   low / medium / high
      would_mutate:           dict of {column: new_value} the apply step
                              *would* set. Empty means "review only".
      requires_manual_review: True when the action is not safe to apply
                              automatically even with apply=True. Operator
                              must inspect the affected row first.
      severity:               propagated from the underlying Finding
    """

    action: str
    target: tuple
    reason: str
    risk: RiskLevel
    would_mutate: dict = field(default_factory=dict)
    requires_manual_review: bool = False
    severity: str = "warning"

    def as_dict(self) -> dict:
        d = asdict(self)
        d["target"] = list(self.target)
        return d


# ─────────────────────── thresholds (mirror app constants) ───────────────────

STUCK_GRABBED_DAYS = 2  # volume.status='grabbed' older than this is stuck
STUCK_IMPORTING_HOURS = 6  # import_queue.status='importing' older than this is stuck
PARTIAL_AGE_HOURS = 6  # import_queue.status='partial' older than this is stuck
FAILED_RETRY_HOURS = 1  # failed import_queue rows older than this can be retried


# ─────────────────────── read-only DB helpers ────────────────────────────────


def _ro_connect(db_path: str) -> sqlite3.Connection:
    """Read-only connection. Any attempt to mutate raises."""
    if not os.path.isfile(db_path):
        raise FileNotFoundError(f"DB not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ─────────────────────── report (proxies diagnose) ───────────────────────────


def report(db_path: str = DEFAULT_DB_PATH) -> list:
    """Return verify_e2e findings. Pure proxy so callers have one entry point."""
    return verify_e2e.diagnose(db_path)


# ─────────────────────── individual planners ────────────────────────────────


def _plan_stuck_grabbed(db) -> list[RepairAction]:
    """Volumes stuck in 'grabbed' state past the threshold.

    The app's check_download_status already auto-resets these IF the volume
    is not referenced by an in-flight import_queue row. We mirror that
    safety check here: when the volume IS in the import queue, the action
    requires manual review (the import may still complete).
    """
    rows = db.execute(
        f"""
        SELECT v.id, v.series_id, v.volume_num, v.grabbed_at, v.download_id,
               (SELECT COUNT(*) FROM import_queue iq
                  WHERE iq.download_id = v.download_id
                    AND iq.status IN ('pending','partial','importing')
               ) AS in_queue
          FROM volumes v
         WHERE v.status = 'grabbed'
           AND v.grabbed_at < datetime('now', ?)
         ORDER BY v.grabbed_at
        """,
        (f"-{STUCK_GRABBED_DAYS} days",),
    ).fetchall()
    actions: list[RepairAction] = []
    for r in rows:
        if r["in_queue"]:
            actions.append(
                RepairAction(
                    action="leave_pending_import",
                    target=("volumes", r["id"]),
                    reason=(
                        f"vol {r['volume_num']} of series {r['series_id']} grabbed at "
                        f"{r['grabbed_at']} but its download_id is still in import_queue"
                    ),
                    risk="low",
                    would_mutate={},
                    requires_manual_review=True,
                    severity="warning",
                )
            )
        else:
            # Same fields the app's auto-recovery clears.
            actions.append(
                RepairAction(
                    action="reset_to_wanted",
                    target=("volumes", r["id"]),
                    reason=(
                        f"vol {r['volume_num']} of series {r['series_id']} stuck in "
                        f"'grabbed' since {r['grabbed_at']}; not referenced by any "
                        "active import_queue row"
                    ),
                    risk="low",
                    would_mutate={
                        "status": "wanted",
                        "grabbed_at": None,
                        "download_id": None,
                        "source_url": None,
                        "torrent_name": None,
                        "indexer": None,
                        "protocol": None,
                        "client": None,
                        "release_group": None,
                        "import_path": None,
                    },
                    requires_manual_review=False,
                    severity="warning",
                )
            )
    return actions


def _plan_stale_importing(db) -> list[RepairAction]:
    """import_queue rows stuck in 'importing' beyond the threshold.

    'importing' means a worker claimed it; if the worker died, the row
    sits forever. Safe action: revert to 'failed' so the next status_loop
    can retry. We require manual review when the row has any
    needs_review files — those carry user decisions we shouldn't drop.
    """
    rows = db.execute(
        f"""
        SELECT iq.id, iq.torrent_name, iq.created_at,
               (SELECT COUNT(*) FROM import_queue_files f
                  WHERE f.queue_id = iq.id AND f.status='needs_review'
               ) AS needs_review_files
          FROM import_queue iq
         WHERE iq.status = 'importing'
           AND iq.created_at < datetime('now', ?)
         ORDER BY iq.created_at
        """,
        (f"-{STUCK_IMPORTING_HOURS} hours",),
    ).fetchall()
    actions: list[RepairAction] = []
    for r in rows:
        manual = bool(r["needs_review_files"])
        actions.append(
            RepairAction(
                action="revert_to_failed_for_retry",
                target=("import_queue", r["id"]),
                reason=(
                    f"import_queue row '{r['torrent_name']}' stuck in 'importing' "
                    f"since {r['created_at']}"
                    + (
                        f" with {r['needs_review_files']} needs_review files"
                        if manual
                        else ""
                    )
                ),
                risk="medium" if manual else "low",
                would_mutate={"status": "failed"},
                requires_manual_review=manual,
                severity="warning",
            )
        )
    return actions


def _plan_stale_partial(db) -> list[RepairAction]:
    """'partial' import_queue rows are mid-batch failures. Safe action is
    the same as stale-importing — revert to 'failed' so the auto-retry
    path picks them up. Always requires manual review because partial
    means some files may already be in the library."""
    rows = db.execute(
        f"""
        SELECT id, torrent_name, created_at FROM import_queue
         WHERE status = 'partial'
           AND created_at < datetime('now', ?)
         ORDER BY created_at
        """,
        (f"-{PARTIAL_AGE_HOURS} hours",),
    ).fetchall()
    return [
        RepairAction(
            action="revert_to_failed_for_retry",
            target=("import_queue", r["id"]),
            reason=(
                f"import_queue row '{r['torrent_name']}' stuck in 'partial' "
                f"since {r['created_at']}; some files may already be imported"
            ),
            risk="high",
            would_mutate={"status": "failed"},
            requires_manual_review=True,
            severity="warning",
        )
        for r in rows
    ]


def _plan_failed_retry(db) -> list[RepairAction]:
    """Failed import_queue rows are candidates for re-grab. We do NOT auto-
    retry — file may have been moved/blocklisted/etc. Always manual."""
    rows = db.execute(
        f"""
        SELECT id, torrent_name, created_at FROM import_queue
         WHERE status = 'failed'
           AND created_at < datetime('now', ?)
         ORDER BY created_at
        """,
        (f"-{FAILED_RETRY_HOURS} hours",),
    ).fetchall()
    return [
        RepairAction(
            action="review_failed_import",
            target=("import_queue", r["id"]),
            reason=(
                f"failed import '{r['torrent_name']}' from {r['created_at']} — "
                "operator should decide between retry, blocklist, or delete"
            ),
            risk="medium",
            would_mutate={},
            requires_manual_review=True,
            severity="warning",
        )
        for r in rows
    ]


def _plan_ghost_chapters(db) -> list[RepairAction]:
    """Chapters with status='downloaded' but no quality + no import_path.

    The DB says the chapter is downloaded; the disk record disagrees. We
    have no automatic way to know whether the file actually exists in the
    library or was lost — always manual review. Suggested action is to
    revert to 'wanted' so it re-downloads, but we don't apply it.
    """
    rows = db.execute(
        """
        SELECT c.id, c.series_id, c.volume_id, c.chapter_num
          FROM chapters c
         WHERE c.status = 'downloaded'
           AND c.quality IS NULL
           AND c.import_path IS NULL
         ORDER BY c.series_id, c.chapter_num
        """
    ).fetchall()
    return [
        RepairAction(
            action="revert_ghost_chapter_to_wanted",
            target=("chapters", r["id"]),
            reason=(
                f"chapter {r['chapter_num']} of series {r['series_id']} marked "
                "downloaded but has neither quality nor import_path; file may be "
                "missing"
            ),
            risk="medium",
            would_mutate={"status": "wanted"},
            requires_manual_review=True,
            severity="warning",
        )
        for r in rows
    ]


def _plan_orphans(db) -> list[RepairAction]:
    """Orphan rows (FK violation) are critical and never auto-repaired.

    The right answer depends on which side is correct: keep the orphan
    (and re-link its parent) or delete it (data loss). Always manual.
    """
    actions: list[RepairAction] = []
    for r in db.execute(
        """
        SELECT id, series_id FROM volumes
         WHERE NOT EXISTS (SELECT 1 FROM series s WHERE s.id = volumes.series_id)
        """
    ).fetchall():
        actions.append(
            RepairAction(
                action="manual_review_orphan_volume",
                target=("volumes", r["id"]),
                reason=(
                    f"volume {r['id']} references missing series {r['series_id']}; "
                    "either re-create the series or delete the volume"
                ),
                risk="high",
                would_mutate={},
                requires_manual_review=True,
                severity="critical",
            )
        )
    for r in db.execute(
        """
        SELECT id, volume_id FROM chapters
         WHERE volume_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM volumes v WHERE v.id = chapters.volume_id)
        """
    ).fetchall():
        actions.append(
            RepairAction(
                action="manual_review_orphan_chapter",
                target=("chapters", r["id"]),
                reason=(
                    f"chapter {r['id']} references missing volume {r['volume_id']}"
                ),
                risk="high",
                would_mutate={},
                requires_manual_review=True,
                severity="critical",
            )
        )
    return actions


_ALL_PLANNERS = [
    _plan_stuck_grabbed,
    _plan_stale_importing,
    _plan_stale_partial,
    _plan_failed_retry,
    _plan_ghost_chapters,
    _plan_orphans,
]


# ─────────────────────── public planner ──────────────────────────────────────


def plan(db_path: str = DEFAULT_DB_PATH) -> list[RepairAction]:
    """Return the dry-run repair plan for ``db_path``.

    Read-only. Iterates every planner and concatenates their proposals.
    Never opens a writable connection.
    """
    with _ro_connect(db_path) as db:
        actions: list[RepairAction] = []
        for planner in _ALL_PLANNERS:
            actions.extend(planner(db))
    return actions


# ─────────────────────── CLI ─────────────────────────────────────────────────


def _print_report(findings: list) -> None:
    """Wraps verify_e2e._print_legacy so `reconcile.py report` is one call."""
    verify_e2e._print_legacy(findings)


def _print_plan(actions: list[RepairAction]) -> None:
    if not actions:
        print("No reconciliation actions proposed. DB is in a clean state.")
        return
    by_risk: dict[str, list[RepairAction]] = {"low": [], "medium": [], "high": []}
    for a in actions:
        by_risk[a.risk].append(a)
    print("=" * 60)
    print(f"DRY-RUN REPAIR PLAN ({len(actions)} actions)")
    print("=" * 60)
    for risk in ("high", "medium", "low"):
        bucket = by_risk[risk]
        if not bucket:
            continue
        print(f"\n── {risk.upper()} RISK ({len(bucket)}) " + "─" * (40 - len(risk)))
        for a in bucket:
            tbl, tid = a.target
            review = " [MANUAL REVIEW]" if a.requires_manual_review else ""
            print(f"  {a.action} on {tbl}#{tid}{review}")
            print(f"      reason: {a.reason}")
            if a.would_mutate:
                cols = ", ".join(f"{k}={v!r}" for k, v in a.would_mutate.items())
                print(f"      would set: {cols}")
            else:
                print(f"      would set: (review only — no mutation proposed)")
    print()
    print(
        f"Summary: {len(by_risk['high'])} high, {len(by_risk['medium'])} medium, "
        f"{len(by_risk['low'])} low risk"
    )
    print("This was a DRY RUN. No rows were modified.")


_USAGE = """\
usage: reconcile.py {report|plan} [db_path]

  report  Human-readable rundown of state-machine residue (= verify_e2e.diagnose).
  plan    Dry-run repair plan: proposed actions per finding, never mutates.

DB path defaults to /config/manga_arr.db. The connection is opened read-only.
This script never modifies data.
"""


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(_USAGE, file=sys.stderr)
        return 0 if len(argv) >= 2 and argv[1] in ("-h", "--help") else 2
    cmd = argv[1]
    db_path = argv[2] if len(argv) > 2 else DEFAULT_DB_PATH
    if cmd == "report":
        _print_report(report(db_path))
        return 0
    if cmd == "plan":
        _print_plan(plan(db_path))
        return 0
    print(_USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
