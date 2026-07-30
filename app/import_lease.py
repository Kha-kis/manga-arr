"""SQLite-backed ownership leases for import queue execution."""

from __future__ import annotations

import math
import sqlite3
from typing import Literal, cast

from download_identity import (
    DownloadIdentity,
    DownloadProtocol,
    coerce_download_client_id,
    download_identities_match,
    normalize_download_protocol,
)

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
    identity_row = db.execute(
        "SELECT download_id FROM import_queue WHERE id=?",
        (queue_id,),
    ).fetchone()
    observed_download_id = identity_row[0] if identity_row is not None else None
    placeholders = ",".join("?" for _ in allowed_statuses)
    cur = db.execute(
        f"""
        UPDATE import_queue
        SET status='importing', lease_owner=?,
            lease_expires_at=datetime('now', ?), failed_at=NULL
        WHERE id=? AND status IN ({placeholders})
          AND lease_owner IS NULL
          AND download_id IS ?
          AND NOT EXISTS (
              SELECT 1 FROM import_pack_cleanup_reservations reservation
              WHERE reservation.purpose='cleanup'
                AND reservation.expires_at > CURRENT_TIMESTAMP
                AND (
                    reservation.download_client_id IS NULL
                    OR import_queue.download_client_id IS NULL
                    OR reservation.download_client_id=
                       import_queue.download_client_id
                )
                AND (
                    reservation.protocol IS NULL
                    OR import_queue.download_protocol IS NULL
                    OR reservation.protocol=import_queue.download_protocol
                )
                AND (
                    CASE
                        WHEN COALESCE(
                            reservation.protocol,
                            import_queue.download_protocol
                        )='nzb'
                        THEN reservation.normalized_download_id=
                             import_queue.download_id
                        ELSE reservation.normalized_download_id=
                             lower(trim(import_queue.download_id))
                    END
                )
          )
          AND NOT EXISTS (
              SELECT 1 FROM import_publications publication
              WHERE publication.queue_id=import_queue.id
                AND publication.state IN (
                    'staging','prepared','publishing','published',
                    'db_committed','cleaning'
                )
          )
          AND NOT EXISTS (
              SELECT 1 FROM volume_file_deletions deletion
              WHERE deletion.series_id=import_queue.series_id
                AND deletion.state='active'
          )
        """,
        (
            lease_owner,
            _lease_modifier(lease_seconds),
            queue_id,
            *allowed_statuses,
            observed_download_id,
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
          AND NOT EXISTS (
              SELECT 1 FROM import_publications publication
              WHERE publication.queue_id=import_queue.id
                AND publication.state IN (
                    'staging','prepared','publishing','published',
                    'db_committed','cleaning'
                )
          )
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
    download_client_id: int | None,
    series_id: int | None = None,
    protocol: DownloadProtocol | None = None,
) -> bool:
    """Return whether another queue row may still use this download.

    Retryable siblings are included so cleanup/reset cannot win immediately
    before their claim. Importing rows block even with an expired or NULL
    lease, preserving legacy and expired-but-unrecovered workers. Concrete
    client owners are independent; a legacy NULL owner blocks conservatively.
    """
    if not download_id:
        return False
    owner_id = coerce_download_client_id(download_client_id)
    target_protocol = normalize_download_protocol(protocol)
    if target_protocol is None:
        target_row = db.execute(
            "SELECT download_protocol FROM import_queue WHERE id=?",
            (queue_id,),
        ).fetchone()
        if target_row is not None:
            target_protocol = normalize_download_protocol(target_row[0])

    id_filter = (
        " AND download_id=?"
        if target_protocol == "nzb"
        else " AND download_id=? COLLATE NOCASE"
    )
    rows = db.execute(
        """
            SELECT download_id, download_client_id, download_protocol, series_id
            FROM import_queue
            WHERE id != ?
              AND download_id IS NOT NULL
        """
        + id_filter
        + """
              AND (
                  status IN ('pending','partial','importing')
                  OR lease_owner IS NOT NULL
                  OR EXISTS (
                      SELECT 1 FROM import_publications publication
                      WHERE publication.queue_id=import_queue.id
                        AND publication.state IN (
                            'staging','prepared','publishing','published',
                            'db_committed','cleaning'
                        )
                  )
              )
        """,
        (queue_id, download_id),
    ).fetchall()
    target = DownloadIdentity(owner_id, target_protocol, download_id)
    for row in rows:
        if series_id is not None and row[3] != series_id:
            continue
        sibling_owner = coerce_download_client_id(row[1])
        sibling_protocol = normalize_download_protocol(row[2])
        sibling = DownloadIdentity(
            sibling_owner,
            sibling_protocol,
            str(row[0] or ""),
        )
        if download_identities_match(target, sibling):
            return True
    return False


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
              AND NOT EXISTS (
                  SELECT 1 FROM import_publications publication
                  WHERE publication.queue_id=iq.id
                    AND publication.state IN (
                        'staging','prepared','publishing','published',
                        'db_committed','cleaning'
                    )
              )
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
              AND NOT EXISTS (
                  SELECT 1 FROM import_publications publication
                  WHERE publication.queue_id=import_queue.id
                    AND publication.state IN (
                        'staging','prepared','publishing','published',
                        'db_committed','cleaning'
                    )
              )
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
          AND NOT EXISTS (
              SELECT 1 FROM import_publications publication
              WHERE publication.queue_id=import_queue.id
                AND publication.state IN (
                    'staging','prepared','publishing','published',
                    'db_committed','cleaning'
                )
          )
        """,
        (queue_id, observed_status),
    )
    return cur.rowcount == 1
