"""SQLite-backed ownership leases for import queue execution."""

from __future__ import annotations

import math
import sqlite3
from typing import Literal, cast

IMPORT_LEASE_SECONDS = 5 * 60
IMPORT_LEASE_REFRESH_SECONDS = 60

ImportQueueStatus = Literal[
    "pending",
    "partial",
    "importing",
    "imported",
    "failed",
    "skipped",
]
RetryableImportQueueStatus = Literal["pending", "partial"]
_RecoveryCandidate = tuple[
    int,
    str | None,
    RetryableImportQueueStatus,
]

_RETRYABLE_IMPORT_STATUSES = frozenset(("pending", "partial"))
_IMPORT_STATUSES = frozenset(
    ("pending", "partial", "importing", "imported", "failed", "skipped")
)


def _lease_modifier(lease_seconds: float) -> str:
    """Return a validated SQLite datetime modifier for a lease duration."""
    if not math.isfinite(lease_seconds) or lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    return f"+{lease_seconds:.6f} seconds"


def claim_import_queue_row(
    db: sqlite3.Connection,
    queue_id: int,
    lease_owner: str,
    allowed_statuses: tuple[RetryableImportQueueStatus, ...] = (
        "pending",
        "partial",
    ),
    *,
    lease_seconds: float = IMPORT_LEASE_SECONDS,
) -> bool:
    """Atomically claim one retryable queue row for ``lease_owner``."""
    if not lease_owner:
        raise ValueError("lease_owner must be non-empty")
    if not allowed_statuses:
        raise ValueError("allowed_statuses must be non-empty")
    if not set(allowed_statuses) <= _RETRYABLE_IMPORT_STATUSES:
        raise ValueError("allowed_statuses may contain only pending/partial")
    placeholders = ",".join("?" for _ in allowed_statuses)
    cur = db.execute(
        f"""
        UPDATE import_queue
        SET status='importing', lease_owner=?,
            lease_expires_at=datetime('now', ?), failed_at=NULL
        WHERE id=? AND status IN ({placeholders})
          AND lease_owner IS NULL
        """,
        (
            lease_owner,
            _lease_modifier(lease_seconds),
            queue_id,
            *allowed_statuses,
        ),
    )
    return cur.rowcount == 1


def refresh_import_queue_lease(
    db: sqlite3.Connection,
    queue_id: int,
    lease_owner: str,
    *,
    lease_seconds: float = IMPORT_LEASE_SECONDS,
) -> bool:
    """Renew a live lease, returning false after expiry or ownership loss."""
    if not lease_owner:
        return False
    cur = db.execute(
        """
        UPDATE import_queue
        SET lease_expires_at=datetime('now', ?)
        WHERE id=? AND status='importing' AND lease_owner=?
          AND lease_expires_at > datetime('now')
        """,
        (_lease_modifier(lease_seconds), queue_id, lease_owner),
    )
    return cur.rowcount == 1


def owns_import_queue_lease(
    db: sqlite3.Connection,
    queue_id: int,
    lease_owner: str,
) -> bool:
    """Return whether ``lease_owner`` still has a live importing lease."""
    if not lease_owner:
        return False
    row = cast(
        object | None,
        db.execute(
            """
            SELECT 1 FROM import_queue
            WHERE id=? AND status='importing' AND lease_owner=?
              AND lease_expires_at > datetime('now')
            """,
            (queue_id, lease_owner),
        ).fetchone(),
    )
    return row is not None


def transition_import_queue_row(
    db: sqlite3.Connection,
    queue_id: int,
    lease_owner: str,
    new_status: ImportQueueStatus,
) -> bool:
    """Owner-CAS a live importing row to a non-importing state."""
    if new_status not in _IMPORT_STATUSES:
        raise ValueError(f"invalid import queue status: {new_status!r}")
    if new_status == "importing":
        raise ValueError("use refresh_import_queue_lease for importing rows")
    if not lease_owner:
        return False
    cur = db.execute(
        """
        UPDATE import_queue
        SET status=?, lease_owner=NULL, lease_expires_at=NULL,
            failed_at=CASE
                WHEN ?='failed' THEN datetime('now')
                ELSE failed_at
            END
        WHERE id=? AND status='importing' AND lease_owner=?
          AND lease_expires_at > datetime('now')
        """,
        (new_status, new_status, queue_id, lease_owner),
    )
    return cur.rowcount == 1


def retryable_import_queue_status(
    db: sqlite3.Connection,
    queue_id: int,
) -> RetryableImportQueueStatus:
    """Choose the safe retry state without changing child decisions."""
    needs_review = cast(
        object | None,
        db.execute(
            """
            SELECT 1 FROM import_queue_files
            WHERE queue_id=? AND status='needs_review'
            LIMIT 1
            """,
            (queue_id,),
        ).fetchone(),
    )
    return "partial" if needs_review else "pending"


def release_import_queue_lease(
    db: sqlite3.Connection,
    queue_id: int,
    lease_owner: str,
) -> bool:
    """Release a live owned row to pending/partial, preserving child rows."""
    return transition_import_queue_row(
        db,
        queue_id,
        lease_owner,
        retryable_import_queue_status(db, queue_id),
    )


def has_import_sibling_that_may_use_download(
    db: sqlite3.Connection,
    *,
    queue_id: int,
    download_id: str | None,
    series_id: int | None = None,
) -> bool:
    """Return whether another queue row may still use this download.

    Retryable siblings are included so cleanup/reset cannot win immediately
    before their claim. Importing rows block even with an expired or NULL
    lease, preserving legacy and expired-but-unrecovered workers.
    """
    if not download_id:
        return False
    series_filter = "" if series_id is None else " AND series_id=?"
    params: tuple[object, ...] = (
        (queue_id, download_id)
        if series_id is None
        else (queue_id, download_id, series_id)
    )
    row = cast(
        object | None,
        db.execute(
            """
            SELECT 1
            FROM import_queue
            WHERE id != ?
              AND download_id IS NOT NULL
              AND download_id=?
            """
            + series_filter
            + """
              AND (
                  status IN ('pending','partial','importing')
                  OR lease_owner IS NOT NULL
              )
            LIMIT 1
            """,
            params,
        ).fetchone(),
    )
    return row is not None


def recover_expired_import_leases(
    db: sqlite3.Connection,
    *,
    max_rows: int = 500,
) -> int:
    """Recover expired and legacy unleased importing rows using the DB clock."""
    if max_rows <= 0:
        return 0
    candidates = cast(
        list[_RecoveryCandidate],
        db.execute(
            """
            SELECT iq.id, iq.lease_owner,
                   CASE WHEN EXISTS (
                       SELECT 1 FROM import_queue_files f
                       WHERE f.queue_id=iq.id AND f.status='needs_review'
                   ) THEN 'partial' ELSE 'pending' END AS retry_status
            FROM import_queue iq
            WHERE iq.status='importing'
              AND (
                  iq.lease_owner IS NULL
                  OR iq.lease_expires_at IS NULL
                  OR iq.lease_expires_at <= datetime('now')
              )
            ORDER BY iq.lease_expires_at, iq.id
            LIMIT ?
            """,
            (max_rows,),
        ).fetchall(),
    )
    recovered = 0
    for queue_id, lease_owner, retry_status in candidates:
        cur = db.execute(
            """
            UPDATE import_queue
            SET status=?, lease_owner=NULL, lease_expires_at=NULL
            WHERE id=? AND status='importing' AND lease_owner IS ?
              AND (
                  lease_owner IS NULL
                  OR lease_expires_at IS NULL
                  OR lease_expires_at <= datetime('now')
              )
            """,
            (retry_status, queue_id, lease_owner),
        )
        recovered += int(cur.rowcount == 1)
    return recovered


def fail_stale_pending_import_queue_row(
    db: sqlite3.Connection,
    queue_id: int,
    observed_status: RetryableImportQueueStatus,
) -> bool:
    """CAS one observed unleased pending/partial row to failed."""
    if observed_status not in _RETRYABLE_IMPORT_STATUSES:
        return False
    cur = db.execute(
        """
        UPDATE import_queue
        SET status='failed', failed_at=datetime('now'),
            lease_owner=NULL, lease_expires_at=NULL
        WHERE id=? AND status=? AND lease_owner IS NULL
        """,
        (queue_id, observed_status),
    )
    return cur.rowcount == 1
