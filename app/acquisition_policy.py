"""Durable import authority, with conservative exact-identity legacy evidence."""

from __future__ import annotations

import json
import sqlite3

from download_identity import (
    DownloadIdentity,
    normalize_download_id,
    normalize_download_protocol,
)


def policy_value(value: object) -> int | None:
    return value if type(value) is int and value in (0, 1) else None


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate acquisition history field")
        result[key] = value
    return result


def _same_identity(row: sqlite3.Row, identity: DownloadIdentity) -> bool:
    protocol = normalize_download_protocol(row["protocol"])
    if (
        row["download_client_id"] != identity.download_client_id
        or protocol != identity.protocol
    ):
        return False
    if row["protocol"] and protocol is None:
        return False
    raw_id = str(row["download_id"] or "")
    if not raw_id or not identity.download_id:
        return False
    if protocol is None:
        return raw_id == identity.download_id
    return normalize_download_id(raw_id, protocol) == normalize_download_id(
        identity.download_id, protocol
    )


def acquisition_policy(
    db: sqlite3.Connection,
    *,
    series_id: int,
    source_url: str,
    identity: DownloadIdentity,
) -> int | None:
    """Return durable intent, or unanimous explicit legacy evidence; never guess manual."""
    if not source_url:
        return None
    seen = db.execute(
        "SELECT download_id,download_client_id,protocol,respect_grab_claims"
        " FROM seen WHERE series_id=? AND torrent_url=?",
        (series_id, source_url),
    ).fetchone()
    if seen is not None and _same_identity(seen, identity):
        persisted = policy_value(seen["respect_grab_claims"])
        if persisted is not None:
            return persisted
    evidence: set[int] = set()
    for row in db.execute(
        "SELECT download_id,download_client_id,protocol,data FROM history"
        " WHERE series_id=? AND torrent_url=? AND event_type='grabbed'",
        (series_id, source_url),
    ):
        if not _same_identity(row, identity):
            continue
        try:
            data = json.loads(row["data"] or "{}", object_pairs_hook=_unique_pairs)
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        if data.get("claim_lost") is True:
            evidence.add(1)
        elif type(data.get("respect_monitoring")) is bool:
            evidence.add(int(data["respect_monitoring"]))
        else:
            return None
    return next(iter(evidence)) if len(evidence) == 1 else None
