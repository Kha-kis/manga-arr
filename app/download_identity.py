"""Shared ownership-aware identity rules for downloader-local IDs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Literal

DownloadProtocol = Literal["torrent", "nzb"]


def coerce_download_client_id(value: object) -> int | None:
    """Return a persisted positive client ID, rejecting bools and bad values."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def normalize_download_protocol(value: object) -> DownloadProtocol | None:
    """Normalize the protocol names persisted by current and legacy releases."""
    normalized = str(value or "").strip().lower()
    if normalized == "torrent":
        return "torrent"
    if normalized in {"nzb", "usenet"}:
        return "nzb"
    return None


def protocol_for_client_type(value: object) -> DownloadProtocol | None:
    """Return the supported download protocol for a configured client type."""
    normalized = str(value or "").strip().lower()
    if normalized == "qbittorrent":
        return "torrent"
    if normalized == "sabnzbd":
        return "nzb"
    return None


def normalize_download_id(
    download_id: object,
    protocol: DownloadProtocol | None,
) -> str:
    """Normalize an ID according to the downloader's actual ID semantics."""
    raw = str(download_id or "")
    if protocol == "nzb":
        return raw
    # Unknown legacy protocols use the more conservative qBittorrent
    # comparison. Known SAB identities remain byte-for-byte exact.
    return raw.strip().lower()


@dataclass(frozen=True, slots=True)
class DownloadIdentity:
    """One downloader-local ID bound to its persisted owner and protocol."""

    download_client_id: int | None
    protocol: DownloadProtocol | None
    download_id: str


def download_identity_key(identity: DownloadIdentity) -> str:
    """Return an unambiguous durable key for one ownership-aware identity."""
    normalized_id = normalize_download_id(
        identity.download_id,
        identity.protocol,
    )
    if not normalized_id:
        return ""
    owner_id = coerce_download_client_id(identity.download_client_id)
    owner_key: int | str = owner_id if owner_id is not None else "legacy"
    protocol_key = identity.protocol or "unknown"
    return json.dumps(
        (owner_key, protocol_key, normalized_id),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def download_identity_path_token(identity: DownloadIdentity) -> str:
    """Return a bounded filesystem-safe token for one durable identity key."""
    key = download_identity_key(identity)
    if not key:
        return ""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def download_identities_match(
    left: DownloadIdentity,
    right: DownloadIdentity,
) -> bool:
    """Return whether two rows may represent the same downloader item.

    Concrete, different owners are independent. A legacy NULL owner is a
    conservative wildcard because assigning it to a configured server would
    be a guess. qBittorrent hashes are case-insensitive; SAB IDs are exact.
    """
    if not left.download_id or not right.download_id:
        return False
    if (
        left.download_client_id is not None
        and right.download_client_id is not None
        and left.download_client_id != right.download_client_id
    ):
        return False
    if (
        left.protocol is not None
        and right.protocol is not None
        and left.protocol != right.protocol
    ):
        return False
    protocol = left.protocol or right.protocol
    return normalize_download_id(
        left.download_id,
        protocol,
    ) == normalize_download_id(right.download_id, protocol)


def resolve_download_protocol(
    db: sqlite3.Connection,
    *,
    download_client_id: int | None,
    series_id: int | None = None,
    download_id: str = "",
    source_url: str = "",
    allow_client_configuration: bool = True,
) -> DownloadProtocol | None:
    """Resolve protocol from exact owner configuration or exact row evidence."""
    if allow_client_configuration and download_client_id is not None:
        configured = db.execute(
            "SELECT type FROM download_clients WHERE id=?",
            (download_client_id,),
        ).fetchone()
        if configured is not None:
            protocol = protocol_for_client_type(configured[0])
            if protocol is not None:
                return protocol

    if series_id is None:
        return None

    evidence_queries = (
        (
            "SELECT protocol, download_id, series_id, download_client_id"
            " FROM seen WHERE download_id=? COLLATE NOCASE",
            "SELECT protocol FROM seen"
            " WHERE series_id=? AND download_client_id IS ? AND torrent_url=?",
        ),
        (
            "SELECT protocol, download_id, series_id, download_client_id"
            " FROM volumes WHERE download_id=? COLLATE NOCASE",
            "SELECT protocol FROM volumes"
            " WHERE series_id=? AND download_client_id IS ? AND source_url=?",
        ),
        (
            "SELECT protocol, download_id, series_id, download_client_id"
            " FROM chapters WHERE download_id=? COLLATE NOCASE",
            "SELECT protocol FROM chapters"
            " WHERE series_id=? AND download_client_id IS ? AND torrent_url=?",
        ),
    )
    protocols: set[DownloadProtocol] = set()
    if download_id:
        for download_id_query, _ in evidence_queries:
            rows = db.execute(download_id_query, (download_id,)).fetchall()
            for row in rows:
                candidate_protocol = normalize_download_protocol(row[0])
                if (
                    candidate_protocol is None
                    or row[2] != series_id
                    or row[3] != download_client_id
                    or normalize_download_id(row[1], candidate_protocol)
                    != normalize_download_id(download_id, candidate_protocol)
                ):
                    continue
                protocols.add(candidate_protocol)

    if source_url:
        for _, source_url_query in evidence_queries:
            rows = db.execute(
                source_url_query,
                (series_id, download_client_id, source_url),
            ).fetchall()
            protocols.update(
                candidate_protocol
                for row in rows
                if (candidate_protocol := normalize_download_protocol(row[0]))
                is not None
            )
    return next(iter(protocols)) if len(protocols) == 1 else None
