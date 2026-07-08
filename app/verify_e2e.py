"""End-to-end DB state-machine verifier.

Two modes:
  - Library: import `diagnose(db_path)` for a structured report. Used by
    tests/python/test_state_diagnostics.py and any future tooling.
  - CLI:    `python3 verify_e2e.py [path]` prints the legacy text report
            and exits non-zero if a critical issue was found.

History: this used to be a flat script that crashed on empty DBs because
SUM() returns NULL with no rows. Refactored to a library so the test suite
can run it against fixture DBs without docker, and so the CLI can stay
backward-compatible with operator scripts.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import dataclass, field
from typing import Literal

DEFAULT_DB_PATH = "/config/manga_arr.db"

Severity = Literal["info", "warning", "critical"]


@dataclass
class Finding:
    """One observation. `severity` drives both display and exit code."""

    code: str
    severity: Severity
    message: str
    count: int = 0
    detail: dict = field(default_factory=dict)


# ──────────────────────────── individual checks ──────────────────────────────
# Each function takes an open sqlite3.Connection (row_factory must be Row)
# and returns a list[Finding]. SUM/COUNT NULLs are coalesced to 0 so the
# checks are safe on empty DBs.


def _i(row_val) -> int:
    """SUM-of-CASE returns NULL when no rows match; treat as 0."""
    return int(row_val) if row_val is not None else 0


def check_grabbed_volumes(db) -> list[Finding]:
    r = db.execute("""
      SELECT
        SUM(CASE WHEN grabbed_at   IS NULL THEN 1 ELSE 0 END) AS no_grabbed_at,
        SUM(CASE WHEN torrent_name IS NULL THEN 1 ELSE 0 END) AS no_torrent_name,
        SUM(CASE WHEN indexer      IS NULL THEN 1 ELSE 0 END) AS no_indexer,
        SUM(CASE WHEN protocol     IS NULL THEN 1 ELSE 0 END) AS no_protocol,
        SUM(CASE WHEN source_url   IS NULL THEN 1 ELSE 0 END) AS no_source_url,
        COUNT(*) AS total
      FROM volumes WHERE status='grabbed'
    """).fetchone()
    total = _i(r["total"])
    missing = {
        "grabbed_at": _i(r["no_grabbed_at"]),
        "torrent_name": _i(r["no_torrent_name"]),
        "indexer": _i(r["no_indexer"]),
        "protocol": _i(r["no_protocol"]),
        "source_url": _i(r["no_source_url"]),
    }
    findings = [
        Finding(
            "grabbed_volumes_total", "info", f"grabbed volumes: {total}", total, missing
        )
    ]
    for col, n in missing.items():
        if n:
            findings.append(
                Finding(
                    f"grabbed_volume_missing_{col}",
                    "warning",
                    f"{n} grabbed volumes missing '{col}'",
                    n,
                )
            )
    return findings


def check_downloaded_volumes(db) -> list[Finding]:
    r = db.execute("""
      SELECT
        SUM(CASE WHEN import_path IS NULL THEN 1 ELSE 0 END) AS no_import_path,
        SUM(CASE WHEN quality     IS NULL THEN 1 ELSE 0 END) AS no_quality,
        SUM(CASE WHEN imported_at IS NULL THEN 1 ELSE 0 END) AS no_imported_at,
        COUNT(*) AS total
      FROM volumes WHERE status='downloaded'
    """).fetchone()
    total = _i(r["total"])
    missing = {
        "import_path": _i(r["no_import_path"]),
        "quality": _i(r["no_quality"]),
        "imported_at": _i(r["no_imported_at"]),
    }
    findings = [
        Finding(
            "downloaded_volumes_total",
            "info",
            f"downloaded volumes: {total}",
            total,
            missing,
        )
    ]
    for col, n in missing.items():
        if n:
            findings.append(
                Finding(
                    f"downloaded_volume_missing_{col}",
                    "warning",
                    f"{n} downloaded volumes missing '{col}'",
                    n,
                )
            )
    return findings


def check_grabbed_chapters(db) -> list[Finding]:
    r = db.execute("""
      SELECT
        SUM(CASE WHEN grabbed_at IS NULL THEN 1 ELSE 0 END) AS no_grabbed_at,
        SUM(CASE WHEN indexer    IS NULL THEN 1 ELSE 0 END) AS no_indexer,
        COUNT(*) AS total
      FROM chapters WHERE status='grabbed'
    """).fetchone()
    total = _i(r["total"])
    missing = {
        "grabbed_at": _i(r["no_grabbed_at"]),
        "indexer": _i(r["no_indexer"]),
    }
    findings = [
        Finding(
            "grabbed_chapters_total",
            "info",
            f"grabbed chapters: {total}",
            total,
            missing,
        )
    ]
    for col, n in missing.items():
        if n:
            findings.append(
                Finding(
                    f"grabbed_chapter_missing_{col}",
                    "warning",
                    f"{n} grabbed chapters missing '{col}'",
                    n,
                )
            )
    return findings


def check_downloaded_chapters(db) -> list[Finding]:
    r = db.execute("""
      SELECT
        SUM(CASE WHEN quality     IS NULL THEN 1 ELSE 0 END) AS no_quality,
        SUM(CASE WHEN import_path IS NULL THEN 1 ELSE 0 END) AS no_import_path,
        SUM(CASE WHEN imported_at IS NULL THEN 1 ELSE 0 END) AS no_imported_at,
        SUM(CASE WHEN quality IS NULL AND import_path IS NULL THEN 1 ELSE 0 END) AS ghost,
        COUNT(*) AS total
      FROM chapters WHERE status='downloaded'
    """).fetchone()
    total = _i(r["total"])
    ghost = _i(r["ghost"])
    findings = [
        Finding(
            "downloaded_chapters_total", "info", f"downloaded chapters: {total}", total
        )
    ]
    if ghost:
        # Ghost = marked downloaded but no file path + no quality. Operator
        # action required to either re-grab or mark blocklisted.
        findings.append(
            Finding(
                "ghost_downloaded_chapters",
                "warning",
                f"{ghost} ghost downloaded chapters (no file, no quality)",
                ghost,
            )
        )
    for col, key in (
        ("no_quality", "quality"),
        ("no_import_path", "import_path"),
        ("no_imported_at", "imported_at"),
    ):
        n = _i(r[col])
        if n and n != ghost:  # ghost row already covers the both-missing case
            findings.append(
                Finding(
                    f"downloaded_chapter_missing_{key}",
                    "warning",
                    f"{n} downloaded chapters missing '{key}'",
                    n,
                )
            )
    return findings


def check_import_queue(db) -> list[Finding]:
    rows = db.execute(
        "SELECT status, COUNT(*) AS n FROM import_queue GROUP BY status"
    ).fetchall()
    findings: list[Finding] = []
    by_status = {row["status"]: int(row["n"]) for row in rows}
    findings.append(
        Finding(
            "import_queue_states",
            "info",
            f"import queue states: {by_status or '(empty)'}",
            sum(by_status.values()),
            {"by_status": by_status},
        )
    )
    # Failed and stuck "importing" rows are operator-actionable.
    for status, sev in (
        ("failed", "warning"),
        ("importing", "warning"),
        ("partial", "warning"),
    ):
        n = by_status.get(status, 0)
        if n:
            findings.append(
                Finding(
                    f"import_queue_{status}",
                    sev,  # type: ignore[arg-type]
                    f"{n} import-queue rows in '{status}' state",
                    n,
                )
            )
    return findings


def check_stuck_grabbed(db, threshold_days: int = 2) -> list[Finding]:
    """Volumes stuck in 'grabbed' state past the threshold suggest the
    download client lost the torrent or the import pipeline broke."""
    n = _i(
        db.execute(
            "SELECT COUNT(*) AS n FROM volumes "
            " WHERE status='grabbed' AND grabbed_at < datetime('now', ?)",
            (f"-{threshold_days} days",),
        ).fetchone()["n"]
    )
    findings = [
        Finding(
            "stuck_grabbed_total",
            "info",
            f"stuck grabbed volumes (>{threshold_days}d): {n}",
            n,
            {"threshold_days": threshold_days},
        )
    ]
    if n:
        findings.append(
            Finding(
                "stuck_grabbed_warning",
                "warning",
                f"{n} volumes stuck in 'grabbed' state for >{threshold_days} days; "
                "either downloader lost them or import never picked them up",
                n,
                {"threshold_days": threshold_days},
            )
        )
    return findings


def check_blocklist(db) -> list[Finding]:
    n = _i(db.execute("SELECT COUNT(*) AS n FROM blocklist").fetchone()["n"])
    return [Finding("blocklist_size", "info", f"blocklist entries: {n}", n)]


def check_settings(db) -> list[Finding]:
    findings: list[Finding] = []
    for key in ("blocklist_ttl_days", "api_key"):
        r = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        v = r["value"] if r else None
        if key == "api_key":
            if v and v.strip():
                findings.append(Finding("api_key_present", "info", "api_key: set", 1))
            else:
                # Blank api_key is a *critical* security regression.
                findings.append(
                    Finding(
                        "api_key_blank",
                        "critical",
                        "api_key is blank or missing — /api routes will reject all requests",
                        0,
                    )
                )
        else:
            findings.append(
                Finding(
                    f"setting_{key}",
                    "info",
                    f"{key}: {v if v is not None else '(missing)'}",
                    0,
                )
            )
    return findings


def check_orphans(db) -> list[Finding]:
    """Foreign-key integrity. Both should always be 0 in a healthy DB."""
    findings: list[Finding] = []
    n_ch = _i(
        db.execute(
            "SELECT COUNT(*) AS n FROM chapters c "
            " WHERE c.volume_id IS NOT NULL "
            " AND NOT EXISTS (SELECT 1 FROM volumes v WHERE v.id = c.volume_id)"
        ).fetchone()["n"]
    )
    findings.append(
        Finding(
            "orphan_chapters",
            "critical" if n_ch else "info",
            f"orphan chapters (volume_id → missing volume): {n_ch}",
            n_ch,
        )
    )
    n_vol = _i(
        db.execute(
            "SELECT COUNT(*) AS n FROM volumes v "
            " WHERE NOT EXISTS (SELECT 1 FROM series s WHERE s.id = v.series_id)"
        ).fetchone()["n"]
    )
    findings.append(
        Finding(
            "orphan_volumes",
            "critical" if n_vol else "info",
            f"orphan volumes (series_id → missing series): {n_vol}",
            n_vol,
        )
    )
    return findings


_ALL_CHECKS: list = [
    check_grabbed_volumes,
    check_downloaded_volumes,
    check_grabbed_chapters,
    check_downloaded_chapters,
    check_import_queue,
    check_stuck_grabbed,
    check_blocklist,
    check_settings,
    check_orphans,
]


def diagnose(db_path: str = DEFAULT_DB_PATH) -> list[Finding]:
    """Run all diagnostic checks against the DB at ``db_path``.

    Read-only — opens the connection in URI mode with mode=ro to make
    accidental mutations impossible. Returns the flat list of findings;
    callers can filter by severity for exit codes or alerting.
    """
    if not os.path.isfile(db_path):
        return [
            Finding("db_missing", "critical", f"database not found at {db_path}", 0)
        ]
    uri = f"file:{db_path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        db.row_factory = sqlite3.Row
        findings: list[Finding] = []
        for check in _ALL_CHECKS:
            findings.extend(check(db))
    return findings


# ─────────────────────────── CLI / legacy output ─────────────────────────────


def _print_legacy(findings: list[Finding]) -> None:
    """Preserve the old human-readable output as closely as practical so
    operator scripts that grep this don't break.
    """
    by_code = {f.code: f for f in findings}
    print("=" * 60)
    print("END-TO-END STATE MACHINE VERIFICATION")
    print("=" * 60)

    def _pick(code, default=0):
        f = by_code.get(code)
        return f.count if f else default

    g = by_code["grabbed_volumes_total"]
    print(f"\n[1] GRABBED VOLUMES (n={g.count}):")
    for k in ("grabbed_at", "torrent_name", "indexer", "protocol", "source_url"):
        print(f"    no {k}: {g.detail.get(k, 0)}")

    d = by_code["downloaded_volumes_total"]
    print(f"\n[2] DOWNLOADED VOLUMES (n={d.count}):")
    for k in ("import_path", "quality", "imported_at"):
        print(f"    no {k}: {d.detail.get(k, 0)}")

    gc = by_code["grabbed_chapters_total"]
    print(f"\n[3] GRABBED CHAPTERS (n={gc.count}):")
    for k in ("grabbed_at", "indexer"):
        print(f"    no {k}: {gc.detail.get(k, 0)}")

    dc = by_code["downloaded_chapters_total"]
    ghost = _pick("ghost_downloaded_chapters")
    print(f"\n[4] DOWNLOADED CHAPTERS (n={dc.count}):")
    print(f"    no quality:     {_pick('downloaded_chapter_missing_quality') + ghost}")
    print(
        f"    no import_path: {_pick('downloaded_chapter_missing_import_path') + ghost}"
    )
    print(f"    no imported_at: {_pick('downloaded_chapter_missing_imported_at')}")
    print(f"    ghost (no file, no quality): {ghost}")

    iq = by_code["import_queue_states"]
    print("\n[5] IMPORT QUEUE STATES:")
    by_status = iq.detail.get("by_status", {})
    if not by_status:
        print("    (empty)")
    for status, n in by_status.items():
        print(f"    {status}: {n}")

    print(f"\n[6] STUCK GRABBED (>2 days): {_pick('stuck_grabbed_total')}")
    print(f"\n[7] BLOCKLIST ENTRIES: {_pick('blocklist_size')}")

    print("\n[8] Settings:")
    for f in findings:
        if f.code.startswith("setting_") or f.code in (
            "api_key_present",
            "api_key_blank",
        ):
            # Display verbatim from the message minus prefixes.
            msg = f.message
            if msg.startswith("api_key"):
                # 'api_key: set' or the critical EMPTY message
                if f.severity == "critical":
                    print("    api_key: EMPTY - security risk")
                else:
                    print("    api_key: set")
            else:
                print(f"    {msg}")

    n_ch = _pick("orphan_chapters")
    n_vol = _pick("orphan_volumes")
    print(f"\n[9] ORPHANED CHAPTERS (volume_id points to missing volume): {n_ch}")
    print(f"    ORPHANED VOLUMES (series_id points to missing series): {n_vol}")

    print("\n" + "=" * 60)
    print("DONE")

    crits = [f for f in findings if f.severity == "critical"]
    warns = [f for f in findings if f.severity == "warning"]
    if crits or warns:
        print(f"\nSummary: {len(crits)} critical, {len(warns)} warning")
        for f in crits + warns:
            print(f"  [{f.severity.upper():>8}] {f.code}: {f.message}")


def main(argv: list[str]) -> int:
    db_path = argv[1] if len(argv) > 1 else DEFAULT_DB_PATH
    findings = diagnose(db_path)
    _print_legacy(findings)
    # Exit non-zero only on critical issues. Warnings inform the operator
    # but don't fail the verifier — operators were running this against a
    # DB with known warnings already.
    return 1 if any(f.severity == "critical" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
