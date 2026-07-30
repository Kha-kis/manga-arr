"""Queue page — grabbed items, download client status, pending releases."""

import asyncio
import json
import sqlite3
from collections import defaultdict as _dd
from typing import Any, Literal, NamedTuple

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from clients import load_bound_download_client
from download_identity import (
    DownloadIdentity,
    DownloadProtocol,
    coerce_download_client_id,
    download_identities_match,
    normalize_download_id,
    normalize_download_protocol,
    protocol_for_client_type,
    resolve_download_protocol,
)
from routers._templates import templates
from shared import (
    cascade_chapters,
    get_cfg,
    get_db,
    get_root_folders,
    build_volume_label,
    vol_num_to_display,
    is_htmx,
    with_flash,
)

# Imported as a module (not `from status_cache import DOWNLOAD_STATUS_CACHE`)
# so tests can swap the module singleton — a name-level import would snapshot
# the old reference.
import status_cache as _sc


def _queue_status_context() -> dict[str, dict[str, object]]:
    """Build the freshness badge context shown in the queue header.

    Returned shape (one sub-dict per client):
      {
        'qbit': {'label': 'live'|'stale'|'unavailable'|'warming_up'|'disabled',
                 'age_seconds': int | None,
                 'error': str | None},
        'sab':  {...},
      }

    'disabled' means the download client isn't configured — we surface
    that separately from 'warming_up' so operators can tell the cache
    isn't broken, there's simply nothing to poll.
    """
    with get_db() as _db:
        configured: dict[DownloadProtocol, list[int]] = {
            "torrent": [],
            "nzb": [],
        }
        for row in _db.execute(
            "SELECT id,type FROM download_clients"
            " WHERE enabled=1 AND type IN ('qbittorrent','sabnzbd')"
        ).fetchall():
            protocol = protocol_for_client_type(row["type"])
            if protocol is not None:
                configured[protocol].append(int(row["id"]))

    def _one(
        protocol: DownloadProtocol,
        legacy_snapshot: _sc.DownloadClientSnapshot | None,
    ) -> dict[str, object]:
        owner_ids = configured[protocol]
        if not owner_ids:
            return {"label": "disabled", "age_seconds": None, "error": None}
        concrete = _sc.DOWNLOAD_STATUS_CACHE.snapshots_for_protocol(protocol)
        snapshots = [concrete.get(owner_id) for owner_id in owner_ids]
        if not concrete and len(owner_ids) == 1 and legacy_snapshot is not None:
            snapshots = [legacy_snapshot]
        labels = [
            _sc.DOWNLOAD_STATUS_CACHE.freshness_label(snapshot)
            for snapshot in snapshots
        ]
        priority = {
            "live": 0,
            "stale": 1,
            "warming_up": 2,
            "unavailable": 3,
        }
        label = max(labels, key=priority.__getitem__)
        from datetime import datetime, timezone

        ages = [
            int(
                (datetime.now(timezone.utc) - snapshot.last_success_at).total_seconds()
            )
            for snapshot in snapshots
            if snapshot is not None and snapshot.last_success_at is not None
        ]
        errors = [
            snapshot.error
            for snapshot in snapshots
            if snapshot is not None and snapshot.error
        ]
        return {
            "label": label,
            "age_seconds": max(ages) if ages else None,
            "error": "; ".join(errors)[:240] or None,
        }

    return {
        "qbit": _one(
            "torrent",
            _sc.DOWNLOAD_STATUS_CACHE.snapshot_qbit(),
        ),
        "sab": _one(
            "nzb",
            _sc.DOWNLOAD_STATUS_CACHE.snapshot_sab(),
        ),
    }


router = APIRouter()


class _DownloadIdentity(NamedTuple):
    download_client_id: int | None
    protocol: DownloadProtocol | None
    persisted_id: str
    external_id: str
    client_kind: Literal["qbit", "sab"]

    @property
    def shared(self) -> DownloadIdentity:
        return DownloadIdentity(
            self.download_client_id,
            self.protocol,
            self.persisted_id,
        )


class _ManualDownloadCleanup(NamedTuple):
    allowed: bool
    status: Literal["ok", "in_progress", "ambiguous", "not_found"]
    identity: _DownloadIdentity | None
    transitioned_parent_ids: tuple[int, ...]


def _download_client_kind(
    download_id: object,
    client: object = None,
    protocol: object = None,
) -> Literal["qbit", "sab"]:
    """Classify display-only legacy evidence without rewriting SAB IDs."""
    resolved = normalize_download_protocol(protocol) or protocol_for_client_type(client)
    if resolved == "nzb":
        return "sab"
    if resolved == "torrent":
        return "qbit"
    identifier = str(download_id or "").lower()
    return "sab" if identifier.startswith("sabnzbd_nzo_") else "qbit"


def _identity_key(
    download_id: str,
    download_client_id: object = None,
    client: object = None,
    protocol: object = None,
) -> tuple[int | None, Literal["qbit", "sab"], str]:
    """Return the client-qualified key used for queue grouping and rendering."""
    client_kind = _download_client_kind(download_id, client, protocol)
    identity_id = download_id if client_kind == "sab" else download_id.lower()
    return coerce_download_client_id(download_client_id), client_kind, identity_id


def _row_value(row: sqlite3.Row | dict[str, Any], key: str) -> Any:
    return row[key] if key in row.keys() else None


def _identity_from_row(
    db: sqlite3.Connection,
    row: sqlite3.Row | dict[str, Any],
    *,
    require_current_client_match: bool = False,
) -> _DownloadIdentity | None:
    persisted_id = str(_row_value(row, "download_id") or "")
    if not persisted_id:
        return None
    owner_id = coerce_download_client_id(_row_value(row, "download_client_id"))
    queue_protocol = normalize_download_protocol(
        _row_value(row, "download_protocol")
    )
    row_protocol = normalize_download_protocol(_row_value(row, "protocol"))
    client_protocol = protocol_for_client_type(_row_value(row, "client"))
    persisted_protocols: set[DownloadProtocol] = {
        protocol
        for protocol in (queue_protocol, row_protocol, client_protocol)
        if protocol is not None
    }
    if len(persisted_protocols) > 1:
        return None
    protocol: DownloadProtocol | None = next(iter(persisted_protocols), None)
    configured_protocol: DownloadProtocol | None = None
    if owner_id is not None and require_current_client_match:
        configured = db.execute(
            "SELECT type FROM download_clients WHERE id=?",
            (owner_id,),
        ).fetchone()
        if configured is None:
            return None
        configured_protocol = protocol_for_client_type(configured["type"])
        if configured_protocol is None:
            return None
        if protocol is not None and configured_protocol != protocol:
            return None
    if protocol is None:
        protocol = resolve_download_protocol(
            db,
            download_client_id=owner_id,
            series_id=_row_value(row, "series_id"),
            download_id=persisted_id,
            source_url=str(
                _row_value(row, "source_url")
                or _row_value(row, "torrent_url")
                or ""
            ),
            allow_client_configuration=require_current_client_match,
        )
    if (
        require_current_client_match
        and configured_protocol is not None
        and protocol is not None
        and configured_protocol != protocol
    ):
        return None
    if protocol is None:
        return _DownloadIdentity(owner_id, None, persisted_id, persisted_id, "qbit")
    client_kind: Literal["qbit", "sab"] = "qbit" if protocol == "torrent" else "sab"
    return _DownloadIdentity(
        owner_id,
        protocol,
        persisted_id,
        normalize_download_id(persisted_id, protocol),
        client_kind,
    )


def _download_identity_evidence(
    db: sqlite3.Connection,
    requested_id: str,
) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT download_id, download_client_id, client, protocol,
               NULL AS download_protocol,
               series_id, source_url, NULL AS torrent_url
        FROM volumes
        WHERE download_id IS NOT NULL AND lower(download_id)=lower(?)
        UNION ALL
        SELECT download_id, download_client_id, client, protocol,
               NULL AS download_protocol,
               series_id, NULL AS source_url, torrent_url
        FROM seen
        WHERE download_id IS NOT NULL AND lower(download_id)=lower(?)
        UNION ALL
        SELECT download_id, download_client_id, client, protocol,
               NULL AS download_protocol,
               series_id, NULL AS source_url, torrent_url
        FROM chapters
        WHERE download_id IS NOT NULL AND lower(download_id)=lower(?)
        UNION ALL
        SELECT download_id, download_client_id, NULL AS client, NULL AS protocol,
               download_protocol,
               series_id, NULL AS source_url, torrent_url
        FROM import_queue
        WHERE download_id IS NOT NULL AND lower(download_id)=lower(?)
        """,
        (requested_id, requested_id, requested_id, requested_id),
    ).fetchall()


def _resolve_manual_download_identity(
    db: sqlite3.Connection,
    requested_id: str,
    *,
    download_client_id: int | None = None,
) -> tuple[
    _DownloadIdentity | None,
    Literal["ok", "ambiguous", "not_found"],
]:
    """Resolve one concrete owner plus its protocol-aware local identifier."""
    rows = _download_identity_evidence(db, requested_id)
    if not rows:
        return None, "not_found"

    requested_owner = coerce_download_client_id(download_client_id)
    if download_client_id is not None and requested_owner is None:
        return None, "not_found"
    candidates: dict[tuple[int, DownloadProtocol, str], _DownloadIdentity] = {}
    legacy_match = False
    conflicting_evidence = False
    for row in rows:
        owner_id = coerce_download_client_id(row["download_client_id"])
        if requested_owner is not None and owner_id != requested_owner:
            continue
        identity = _identity_from_row(
            db,
            row,
            require_current_client_match=True,
        )
        if identity is None:
            conflicting_evidence = True
            continue
        if identity.protocol is None:
            if identity.persisted_id.lower() == requested_id.lower():
                conflicting_evidence = True
            continue
        probe = DownloadIdentity(
            identity.download_client_id,
            identity.protocol,
            requested_id,
        )
        if not download_identities_match(identity.shared, probe):
            continue
        if owner_id is None:
            legacy_match = True
            continue
        key = (
            owner_id,
            identity.protocol,
            normalize_download_id(identity.persisted_id, identity.protocol),
        )
        candidates.setdefault(key, identity)

    if conflicting_evidence or legacy_match or len(candidates) > 1:
        return None, "ambiguous"
    if not candidates:
        return None, "not_found"
    identity = next(iter(candidates.values()))
    return identity, "ok"


def _row_matches_identity(
    db: sqlite3.Connection,
    row: sqlite3.Row,
    identity: _DownloadIdentity,
) -> bool:
    row_identity = _identity_from_row(db, row)
    return bool(
        row_identity is not None
        and row_identity.download_client_id == identity.download_client_id
        and download_identities_match(row_identity.shared, identity.shared)
    )


def _row_may_match_identity(
    db: sqlite3.Connection,
    row: sqlite3.Row,
    identity: _DownloadIdentity,
) -> bool:
    row_identity = _identity_from_row(db, row)
    return bool(
        row_identity is not None
        and download_identities_match(row_identity.shared, identity.shared)
    )


def _grabbed_volumes_for_identity(
    db: sqlite3.Connection,
    identity: _DownloadIdentity,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in db.execute(
            "SELECT * FROM volumes"
            " WHERE lower(download_id)=lower(?) AND status='grabbed'",
            (identity.persisted_id,),
        ).fetchall()
        if _row_matches_identity(db, row, identity)
    ]


def _seen_rows_for_identity(
    db: sqlite3.Connection,
    identity: _DownloadIdentity,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in db.execute(
            "SELECT * FROM seen WHERE lower(download_id)=lower(?)",
            (identity.persisted_id,),
        ).fetchall()
        if _row_matches_identity(db, row, identity)
    ]


def _delete_seen_rows(
    db: sqlite3.Connection,
    rows: list[dict[str, Any]],
) -> None:
    db.executemany(
        "DELETE FROM seen WHERE torrent_url=?",
        ((row["torrent_url"],) for row in rows),
    )


def _clear_chapter_download_owners(
    db: sqlite3.Connection,
    series_id: int,
    volume_ids: list[int],
) -> None:
    """Clear ownership on the same monitored chapter rows cascade updates."""
    if not volume_ids:
        return
    placeholders = ",".join("?" for _ in volume_ids)
    db.execute(
        "UPDATE chapters SET download_client_id=NULL"
        f" WHERE series_id=? AND volume_id IN ({placeholders})"
        " AND monitored=1",
        [series_id, *volume_ids],
    )


def _manual_cleanup_failure_message(
    status: str,
    action: str,
) -> str:
    if status == "in_progress":
        return f"Import is in progress; wait for it to finish before {action}"
    if status == "ambiguous":
        return (
            "Download identity is ambiguous across clients or SAB jobs; "
            f"refresh the queue before {action}"
        )
    if status == "client_unavailable":
        return (
            "The owning download client is unavailable or no longer matches; "
            f"refresh its configuration before {action}"
        )
    return f"Download is no longer tracked; refresh the queue before {action}"


def _load_exact_download_client(
    db: sqlite3.Connection,
    identity: _DownloadIdentity,
) -> dict[str, Any] | None:
    """Load only the persisted owner, with no protocol routing or fallback."""
    owner_id = coerce_download_client_id(identity.download_client_id)
    if owner_id is None or identity.protocol is None:
        return None
    row = db.execute(
        "SELECT id,name,type FROM download_clients WHERE id=?",
        (owner_id,),
    ).fetchone()
    expected_type = "qbittorrent" if identity.protocol == "torrent" else "sabnzbd"
    if row is None or str(row["type"]) != expected_type:
        return None
    bound = load_bound_download_client(
        owner_id,
        expected_type=expected_type,
        expected_name=str(row["name"]),
    )
    return bound.client


def _reserve_manual_download_cleanup(
    db: sqlite3.Connection,
    download_id: str,
    *,
    download_client_id: int | None = None,
    identity: _DownloadIdentity | None = None,
) -> _ManualDownloadCleanup:
    """Resolve and skip unowned review rows for one exact client identity.

    Identity resolution is read-only and happens before the parent UPDATE. The
    UPDATE remains the transaction's first mutation, making a worker claim and
    manual cleanup mutually exclusive across separate SQLite connections.
    """
    db.execute("BEGIN IMMEDIATE")
    resolution_status: Literal["ok", "ambiguous", "not_found"] = "ok"
    if identity is None:
        identity, resolution_status = _resolve_manual_download_identity(
            db,
            download_id,
            download_client_id=download_client_id,
        )
    if identity is None:
        return _ManualDownloadCleanup(
            False,
            resolution_status,
            None,
            (),
        )

    queue_rows = db.execute(
        "SELECT * FROM import_queue WHERE download_id IS NOT NULL"
        " AND lower(download_id)=lower(?)",
        (identity.persisted_id,),
    ).fetchall()
    active_ids = {
        int(row["id"])
        for row in queue_rows
        if (
            row["status"] == "importing"
            or row["lease_owner"] is not None
            or db.execute(
                "SELECT 1 FROM import_publications WHERE queue_id=?"
                " AND state IN ('staging','prepared','publishing','published',"
                " 'db_committed','cleaning') LIMIT 1",
                (row["id"],),
            ).fetchone()
            is not None
        )
        and _row_may_match_identity(db, row, identity)
    }
    if active_ids:
        return _ManualDownloadCleanup(
            False,
            "in_progress",
            identity,
            (),
        )
    transitioned: list[int] = []
    for row in queue_rows:
        if not _row_matches_identity(db, row, identity):
            continue
        if row["status"] not in ("pending", "partial") or row["lease_owner"] is not None:
            continue
        active_publication = db.execute(
            "SELECT 1 FROM import_publications WHERE queue_id=?"
            " AND state IN ('staging','prepared','publishing','published',"
            " 'db_committed','cleaning') LIMIT 1",
            (row["id"],),
        ).fetchone()
        if active_publication is not None:
            continue
        updated = db.execute(
            "UPDATE import_queue SET status='skipped'"
            " WHERE id=? AND status IN ('pending','partial')"
            " AND lease_owner IS NULL",
            (row["id"],),
        )
        if updated.rowcount == 1:
            transitioned.append(int(row["id"]))
    if transitioned:
        db.executemany(
            "UPDATE import_queue_files SET status='skipped' WHERE queue_id=?",
            ((queue_id,) for queue_id in transitioned),
        )

    return _ManualDownloadCleanup(
        True,
        "ok",
        identity,
        tuple(transitioned),
    )


async def _build_queue_rows() -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    str,
    list[dict[str, Any]],
]:
    """Build (queue_rows, disk_info) for the queue page and queue/table partial."""
    import shutil as _shutil

    # ── Download client data ──────────────────────────────────────────────
    # Read from the in-memory cache populated by download_status_refresh_loop
    # every 20s. Rendering no longer makes live qBit/SAB HTTP calls — a slow
    # or dead upstream can't stall the page. See app/status_cache.py.
    all_client_items: dict[
        tuple[int, DownloadProtocol, str],
        dict[str, Any],
    ] = _sc.DOWNLOAD_STATUS_CACHE.qualified_items()
    with get_db() as _client_db:
        configured_owners: dict[DownloadProtocol, list[int]] = {
            "torrent": [],
            "nzb": [],
        }
        for configured in _client_db.execute(
            "SELECT id,type FROM download_clients"
            " WHERE enabled=1 AND type IN ('qbittorrent','sabnzbd')"
        ).fetchall():
            configured_protocol = protocol_for_client_type(configured["type"])
            if configured_protocol is not None:
                configured_owners[configured_protocol].append(int(configured["id"]))

    # Compatibility for tests and warm upgrades that populated the historical
    # single-protocol attributes. Never fan one legacy snapshot out to multiple
    # concrete owners.
    legacy_snapshots: dict[
        DownloadProtocol,
        _sc.DownloadClientSnapshot | None,
    ] = {
        "torrent": _sc.DOWNLOAD_STATUS_CACHE.snapshot_qbit(),
        "nzb": _sc.DOWNLOAD_STATUS_CACHE.snapshot_sab(),
    }
    for protocol, legacy_snapshot in legacy_snapshots.items():
        if (
            not _sc.DOWNLOAD_STATUS_CACHE.snapshots_for_protocol(protocol)
            and len(configured_owners[protocol]) == 1
            and legacy_snapshot is not None
        ):
            owner_id = configured_owners[protocol][0]
            for download_id, item in legacy_snapshot.items.items():
                normalized_id = normalize_download_id(download_id, protocol)
                if normalized_id:
                    all_client_items[(owner_id, protocol, normalized_id)] = item

    def _live_item(
        identity_key: tuple[
            int | None,
            Literal["qbit", "sab"],
            str,
        ],
    ) -> dict[str, Any]:
        owner_id, client_kind, normalized_id = identity_key
        protocol: DownloadProtocol = (
            "torrent" if client_kind == "qbit" else "nzb"
        )
        if owner_id is not None:
            return all_client_items.get(
                (owner_id, protocol, normalized_id),
                {},
            )
        matches = [
            item
            for (candidate_owner, candidate_protocol, candidate_id), item
            in all_client_items.items()
            if candidate_owner > 0
            and candidate_protocol == protocol
            and candidate_id == normalized_id
        ]
        return matches[0] if len(matches) == 1 else {}

    def _client_stage(state: str) -> str:
        sl = (state or "").lower()
        if "stalled" in sl and "up" not in sl:
            return "stalled"
        if "error" in sl or "missing" in sl:
            return "error"
        if "paused" in sl:
            return "paused"
        if "queued" in sl or "checking" in sl:
            return "queued_dl"
        if "upload" in sl or ("stalled" in sl and "up" in sl):
            return "completed"
        return "downloading"

    with get_db() as db:
        seen_meta: dict[
            tuple[int | None, Literal["qbit", "sab"], str],
            dict[str, Any],
        ] = {}
        for _sm in db.execute(
            "SELECT download_id, download_client_id, client, protocol,"
            " indexer, size_bytes"
            " FROM seen WHERE download_id IS NOT NULL"
        ).fetchall():
            persisted_id = _sm["download_id"] or ""
            identity_key = _identity_key(
                persisted_id,
                _sm["download_client_id"],
                _sm["client"],
                _sm["protocol"],
            )
            if persisted_id and identity_key not in seen_meta:
                seen_meta[identity_key] = {
                    "download_id": _sm["download_id"],
                    "client": _sm["client"] or "",
                    "protocol": _sm["protocol"] or "",
                    "indexer": _sm["indexer"] or "",
                    "size_bytes": _sm["size_bytes"] or 0,
                }

        pending_raw = db.execute(
            "SELECT iq.*, s.title as series_title "
            "FROM import_queue iq JOIN series s ON s.id=iq.series_id "
            "WHERE iq.status IN ('pending','partial') ORDER BY iq.created_at DESC"
        ).fetchall()
        pending_by_dlid: dict[
            tuple[int | None, Literal["qbit", "sab"], str],
            dict[str, Any],
        ] = {}
        for q in pending_raw:
            persisted_id = q["download_id"] or ""
            pending_identity = _identity_from_row(db, q)
            identity_kind = (
                pending_identity.client_kind
                if pending_identity is not None
                else _download_client_kind(persisted_id)
            )
            identity_key = _identity_key(
                persisted_id,
                q["download_client_id"],
                protocol="nzb" if identity_kind == "sab" else "torrent",
            )
            files = [
                dict(file_row)
                for file_row in db.execute(
                    "SELECT * FROM import_queue_files WHERE queue_id=?"
                    " AND status IN ('pending','needs_review','failed')"
                    " ORDER BY filename",
                    (q["id"],),
                ).fetchall()
            ]
            needs_review = q["status"] == "partial" or any(
                f["status"] == "needs_review"
                or (
                    f["status"] == "pending"
                    and f["proposed_volume"] is None
                    and f["proposed_chapter"] is None
                )
                for f in files
            )
            pending_by_dlid[identity_key] = {
                "queue_id": q["id"],
                "series_id": q["series_id"],
                "series_title": q["series_title"],
                "torrent_name": q["torrent_name"],
                "grabbed_at": q["created_at"],
                "src_dir": q["src_dir"],
                "download_id": identity_key[2],
                "download_client_id": q["download_client_id"],
                "needs_review": needs_review,
                "files": files,
            }

        grabbed_raw = db.execute(
            "SELECT v.id, v.series_id, v.volume_num, v.pack_type,"
            " v.vol_range_start, v.vol_range_end, v.grabbed_at,"
            " v.download_id, v.torrent_name, v.client as grabbed_client,"
            " v.protocol as grabbed_protocol, v.download_client_id,"
            " s.title as series_title "
            "FROM volumes v JOIN series s ON s.id=v.series_id "
            "WHERE v.status='grabbed' "
            "ORDER BY v.grabbed_at DESC"
        ).fetchall()

        by_dlid: dict[
            tuple[int | None, Literal["qbit", "sab"], str],
            list[sqlite3.Row],
        ] = _dd(list)
        for v in grabbed_raw:
            persisted_id = v["download_id"] or ""
            by_dlid[
                _identity_key(
                    persisted_id,
                    v["download_client_id"],
                    v["grabbed_client"],
                    v["grabbed_protocol"],
                )
            ].append(v)

        queue_rows: list[dict[str, Any]] = []
        seen_dlids: set[tuple[int | None, Literal["qbit", "sab"], str]] = set()

        for identity_key, vols in by_dlid.items():
            seen_dlids.add(identity_key)
            v0 = vols[0]
            sm = seen_meta.get(identity_key, {})
            external_download_id = identity_key[2]

            if len(vols) == 1:
                vol_label = build_volume_label(
                    v0["volume_num"],
                    (v0["vol_range_start"], v0["vol_range_end"])
                    if v0["vol_range_start"]
                    else None,
                    v0["pack_type"] if v0["volume_num"] is None else None,
                )
            else:
                nums = sorted(
                    v["volume_num"] for v in vols if v["volume_num"] is not None
                )
                vol_label = (
                    f"Vol {vol_num_to_display(nums[0])}–{vol_num_to_display(nums[-1])}"
                    if nums
                    else "Pack"
                )

            base = {
                "series_id": v0["series_id"],
                "series_title": v0["series_title"],
                "vol_label": vol_label,
                "torrent_name": v0["torrent_name"] or "",
                "grabbed_at": v0["grabbed_at"],
                "hash": external_download_id,
                "download_client_id": identity_key[0],
                "client": v0["grabbed_client"] or "qbittorrent",
                "protocol": sm.get("protocol", ""),
                "indexer": sm.get("indexer", ""),
                "size_bytes": sm.get("size_bytes", 0),
                "queue_id": None,
                "src_dir": None,
                "files": [],
                "pending_id": None,
                "error_message": "",
            }

            if identity_key in pending_by_dlid:
                pq = pending_by_dlid[identity_key]
                live = _live_item(identity_key)
                stage = "review" if pq["needs_review"] else "importing"
                queue_rows.append(
                    {
                        **base,
                        "stage": stage,
                        "progress": live.get("progress", 100),
                        "dlspeed": 0,
                        "eta": -1,
                        "queue_id": pq["queue_id"],
                        "src_dir": pq["src_dir"],
                        "files": pq["files"],
                    }
                )
            elif (live := _live_item(identity_key)):
                stage = _client_stage(live.get("state", ""))
                queue_rows.append(
                    {
                        **base,
                        "stage": stage,
                        "torrent_name": v0["torrent_name"] or live.get("name", ""),
                        "progress": live.get("progress", 0),
                        "dlspeed": live.get("dlspeed", 0),
                        "eta": live.get("eta", -1),
                        "client": v0["grabbed_client"]
                        or live.get("client", "qbittorrent"),
                        "error_message": live.get("error_message", ""),
                    }
                )
            else:
                queue_rows.append(
                    {
                        **base,
                        "stage": "warning",
                        "progress": 0,
                        "dlspeed": 0,
                        "eta": -1,
                    }
                )

        for identity_key, pq in pending_by_dlid.items():
            if identity_key in seen_dlids:
                continue
            live = _live_item(identity_key)
            sm = seen_meta.get(identity_key, {})
            stage = "review" if pq["needs_review"] else "importing"
            external_download_id = identity_key[2]
            is_sab = identity_key[1] == "sab"
            queue_rows.append(
                {
                    "stage": stage,
                    "series_id": pq["series_id"],
                    "series_title": pq["series_title"],
                    "vol_label": "",
                    "torrent_name": pq["torrent_name"] or "",
                    "grabbed_at": pq["grabbed_at"],
                    "progress": live.get("progress", 100),
                    "dlspeed": 0,
                    "eta": -1,
                    "hash": external_download_id,
                    "download_client_id": identity_key[0],
                    "client": "sabnzbd" if is_sab else "qbittorrent",
                    "queue_id": pq["queue_id"],
                    "src_dir": pq["src_dir"],
                    "files": pq["files"],
                    "pending_id": None,
                    "protocol": sm.get("protocol", ""),
                    "indexer": sm.get("indexer", ""),
                    "size_bytes": sm.get("size_bytes", 0),
                    "error_message": "",
                }
            )

        for pr in db.execute(
            "SELECT pr.id, pr.series_id, pr.url, pr.title, pr.indexer, pr.protocol,"
            " pr.size_bytes, pr.first_seen, s.title as series_title "
            "FROM pending_releases pr LEFT JOIN series s ON s.id=pr.series_id "
            "ORDER BY pr.first_seen DESC"
        ).fetchall():
            queue_rows.append(
                {
                    "stage": "pending",
                    "series_id": pr["series_id"],
                    "series_title": pr["series_title"] or "—",
                    "vol_label": "",
                    "torrent_name": pr["title"],
                    "grabbed_at": pr["first_seen"],
                    "progress": 0,
                    "dlspeed": 0,
                    "eta": -1,
                    "hash": None,
                    "download_client_id": None,
                    "client": pr["protocol"] or "torrent",
                    "queue_id": None,
                    "src_dir": None,
                    "files": [],
                    "pending_id": pr["id"],
                    "protocol": pr["protocol"] or "",
                    "indexer": pr["indexer"] or "",
                    "size_bytes": pr["size_bytes"] or 0,
                    "error_message": "",
                }
            )

        should_check_download_status = any(
            r["stage"] in ("completed", "importing") for r in queue_rows
        )

        queue_rows = [
            r for r in queue_rows if r["stage"] not in ("completed", "importing")
        ]

        _stage_pri = {
            "review": 0,
            "error": 1,
            "warning": 2,
            "stalled": 3,
            "downloading": 4,
            "queued_dl": 5,
            "paused": 6,
            "pending": 7,
        }
        queue_rows.sort(
            key=lambda r: (_stage_pri.get(r["stage"], 5), r["grabbed_at"] or "")
        )
        root_folders = [dict(row) for row in get_root_folders(db)]

    if should_check_download_status:
        try:
            import main as _m

            _m.create_background_task(
                _m.check_download_status(),
                name="queue:check_download_status",
            )
        except Exception:
            pass

    disk_info = []
    for rf in root_folders:
        try:
            usage = _shutil.disk_usage(rf["path"])
            disk_info.append(
                {
                    "path": rf["path"],
                    "label": rf["label"] or rf["path"],
                    "total": usage.total,
                    "used": usage.used,
                    "free": usage.free,
                    "pct": round(usage.used / usage.total * 100, 1)
                    if usage.total
                    else 0,
                }
            )
        except Exception:
            pass

    # Configured download client category (for the Change Category modal)
    configured_category = ""
    with get_db() as _cat_db:
        from routers.download_clients import get_client_for_protocol as _gcp_cat

        _qb_cat_c = _gcp_cat(_cat_db, "torrent")
        if _qb_cat_c:
            configured_category = _qb_cat_c.get("category") or get_cfg("category")

    # ── Suwayomi downloads ────────────────────────────────────────────────────
    suwayomi_rows = []
    with get_db() as _swy_db:
        _swy_jobs = [
            dict(row)
            for row in _swy_db.execute(
                "SELECT sd.*, s.title as series_title"
                " FROM suwayomi_downloads sd"
                " JOIN series s ON s.id=sd.series_id"
                " WHERE sd.status IN ('queued','error')"
                " ORDER BY sd.created_at DESC"
            ).fetchall()
        ]
    for job in _swy_jobs:
        vol_num = job["volume_num"]
        ch_num = job["chapter_num"]
        if vol_num is not None:
            vol_label = f"Vol {vol_num:g}"
        elif ch_num is not None:
            cn = int(ch_num) if ch_num == int(ch_num) else ch_num
            vol_label = f"Ch {cn}"
        else:
            vol_label = "—"
        total = job["total"] or 1
        pct = round(job["progress"] / total * 100, 1)
        suwayomi_rows.append(
            {
                "job_id": job["id"],
                "series_id": job["series_id"],
                "series_title": job["series_title"],
                "vol_label": vol_label,
                "progress": pct,
                "done": job["progress"],
                "total": total,
                "status": job["status"],
                "created_at": job["created_at"],
                "error": job["error"] or "",
            }
        )

    return queue_rows, disk_info, configured_category, suwayomi_rows


async def _queue_partial_response(
    request: Request,
    *,
    message: str | None = None,
    toast_type: str = "warning",
) -> Response:
    """Return queue table partial for HTMX, or redirect to /queue for normal requests."""
    if is_htmx(request):
        queue_rows, _, configured_category, suwayomi_rows = await _build_queue_rows()
        headers = None
        if message:
            headers = {
                "HX-Trigger": json.dumps(
                    {"showToast": {"msg": message, "type": toast_type}}
                )
            }
        return templates.TemplateResponse(
            request,
            "partials/queue_table.html",
            {
                "queue_rows": queue_rows,
                "suwayomi_rows": suwayomi_rows,
                "configured_category": configured_category,
                "queue_status": _queue_status_context(),
            },
            headers=headers,
        )
    redirect_url = with_flash("/queue", message, toast_type) if message else "/queue"
    return RedirectResponse(redirect_url, status_code=303)


@router.get("/queue/table", response_class=HTMLResponse)
async def queue_table_partial(request: Request):
    """HTMX partial: queue table + modals, polled every 8 s from the queue page."""
    queue_rows, _, configured_category, suwayomi_rows = await _build_queue_rows()
    return templates.TemplateResponse(
        request,
        "partials/queue_table.html",
        {
            "queue_rows": queue_rows,
            "suwayomi_rows": suwayomi_rows,
            "configured_category": configured_category,
            "queue_status": _queue_status_context(),
        },
    )


@router.get("/queue", response_class=HTMLResponse)
async def queue_page(request: Request):
    (
        queue_rows,
        disk_info,
        configured_category,
        suwayomi_rows,
    ) = await _build_queue_rows()
    return templates.TemplateResponse(
        request,
        "queue.html",
        {
            "queue_rows": queue_rows,
            "disk_info": disk_info,
            "suwayomi_rows": suwayomi_rows,
            "configured_category": configured_category,
            "queue_status": _queue_status_context(),
        },
    )


@router.post("/api/queue/refresh")
async def queue_refresh(request: Request):
    """Kick off a download-client status refresh and return immediately.

    We intentionally don't await the refresh — a live upstream poll can
    take several seconds, and the manual-refresh button shouldn't block
    the UI. The refresh task writes to DOWNLOAD_STATUS_CACHE; the next
    /queue/table poll (every 8s) picks up the new data.

    Returns HTTP 202. If HTMX, additionally returns the freshness-only
    fragment so the badge updates without waiting for the next poll.
    """
    import main as _m

    _m.create_background_task(
        _sc.DOWNLOAD_STATUS_CACHE.refresh(),
        name="queue:status_cache_refresh",
    )
    if is_htmx(request):
        return templates.TemplateResponse(
            request,
            "partials/queue_status_badge.html",
            {"queue_status": _queue_status_context()},
            status_code=202,
        )
    return JSONResponse(
        {"ok": True, "status": _queue_status_context()}, status_code=202
    )


def reset_grabbed_volume(vol_id: int) -> dict[str, object]:
    """Reset one grabbed volume to wanted and clear its seen dedup rows."""
    cleanup_identity: _DownloadIdentity | None = None
    with get_db() as db:
        row = db.execute(
            "SELECT status, source_url, download_id, download_client_id,"
            " series_id, client, protocol"
            " FROM volumes WHERE id=?",
            (vol_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "status": "not_found"}
        if row["status"] != "grabbed":
            return {"ok": False, "status": "not_grabbed"}

        if row["download_id"]:
            volume_identity = _identity_from_row(db, row)
            if volume_identity is None:
                return {"ok": False, "status": "ambiguous"}
            cleanup = _reserve_manual_download_cleanup(
                db,
                row["download_id"],
                identity=volume_identity,
            )
            if not cleanup.allowed:
                return {"ok": False, "status": cleanup.status}
            cleanup_identity = cleanup.identity

        if row["source_url"]:
            db.execute("DELETE FROM seen WHERE torrent_url=?", (row["source_url"],))
        if row["download_id"] and cleanup_identity is not None:
            others = [
                volume
                for volume in _grabbed_volumes_for_identity(db, cleanup_identity)
                if volume["id"] != vol_id
            ]
            if not others:
                _delete_seen_rows(
                    db,
                    _seen_rows_for_identity(db, cleanup_identity),
                )
        db.execute(
            "UPDATE volumes SET status='wanted', download_id=NULL, grabbed_at=NULL,"
            " source_url=NULL, torrent_name=NULL, indexer=NULL, protocol=NULL,"
            " client=NULL, download_client_id=NULL, release_group=NULL"
            " WHERE id=? AND status='grabbed'",
            (vol_id,),
        )
        cascade_chapters(
            db,
            row["series_id"],
            [vol_id],
            "wanted",
            grabbed_at=None,
            torrent_name=None,
            torrent_url=None,
            indexer=None,
            protocol=None,
            client=None,
            download_client_id=None,
            download_id=None,
            release_group=None,
        )
        _clear_chapter_download_owners(db, row["series_id"], [vol_id])
    return {"ok": True, "status": "reset"}


def dismiss_pending_release(pending_id: int) -> dict[str, object]:
    """Remove one pending delayed release from the queue."""
    with get_db() as db:
        cur = db.execute("DELETE FROM pending_releases WHERE id=?", (pending_id,))
        if cur.rowcount < 1:
            return {"ok": False, "status": "not_found"}
    return {"ok": True, "status": "dismissed"}


async def _reset_orphaned_by_hash(
    request: Request,
    dl_hash: str,
    download_client_id: int | None,
) -> Response:
    """Reset all grabbed volumes sharing a download_id back to wanted (for 'missing' queue items)."""
    cleanup_allowed = False
    cleanup_status = "not_found"
    with get_db() as db:
        cleanup = _reserve_manual_download_cleanup(
            db,
            dl_hash,
            download_client_id=download_client_id,
        )
        cleanup_allowed = cleanup.allowed
        cleanup_status = cleanup.status
        if cleanup_allowed and cleanup.identity is not None:
            volumes = _grabbed_volumes_for_identity(db, cleanup.identity)
            by_series: dict[int, list[int]] = {}
            for volume in volumes:
                if volume["volume_num"] is not None:
                    by_series.setdefault(volume["series_id"], []).append(volume["id"])
            _delete_seen_rows(
                db,
                _seen_rows_for_identity(db, cleanup.identity),
            )
            db.executemany(
                "UPDATE volumes SET status='wanted', download_id=NULL,"
                " grabbed_at=NULL, source_url=NULL, torrent_name=NULL,"
                " indexer=NULL, protocol=NULL, client=NULL,"
                " download_client_id=NULL, release_group=NULL"
                " WHERE id=? AND status='grabbed'",
                (
                    (volume["id"],)
                    for volume in volumes
                    if volume["volume_num"] is not None
                ),
            )
            for series_id, volume_ids in by_series.items():
                cascade_chapters(
                    db,
                    series_id,
                    volume_ids,
                    "wanted",
                    grabbed_at=None,
                    torrent_name=None,
                    torrent_url=None,
                    indexer=None,
                    protocol=None,
                    client=None,
                    download_client_id=None,
                    download_id=None,
                    release_group=None,
                )
                _clear_chapter_download_owners(db, series_id, volume_ids)
            db.executemany(
                "DELETE FROM volumes WHERE id=? AND volume_num IS NULL",
                (
                    (volume["id"],)
                    for volume in volumes
                    if volume["volume_num"] is None
                ),
            )
    if not cleanup_allowed:
        return await _queue_partial_response(
            request,
            message=_manual_cleanup_failure_message(
                cleanup_status,
                "resetting this download",
            ),
        )
    return await _queue_partial_response(request)


@router.post(
    "/api/queue/download-clients/{download_client_id}/downloads/{dl_hash}/reset"
)
@router.post("/queue/download/client/{download_client_id}/{dl_hash}/reset")
async def reset_owned_download(
    request: Request,
    download_client_id: int,
    dl_hash: str,
) -> Response:
    """Reset only one concrete download-client owner's matching rows."""
    return await _reset_orphaned_by_hash(request, dl_hash, download_client_id)


@router.post("/queue/grabbed/{dl_hash}/reset-all")
async def reset_orphaned_by_hash(request: Request, dl_hash: str) -> Response:
    """Legacy ID-only reset, accepted only for one unambiguous concrete owner."""
    return await _reset_orphaned_by_hash(request, dl_hash, None)


@router.post("/queue/grabbed/{vol_id}/reset")
async def reset_orphaned_volume(request: Request, vol_id: int):
    """Reset an orphaned grabbed volume back to wanted so it can be re-grabbed."""
    result = reset_grabbed_volume(vol_id)
    if result["status"] not in ("reset", "not_found", "not_grabbed"):
        return await _queue_partial_response(
            request,
            message=_manual_cleanup_failure_message(
                str(result["status"]),
                "resetting this volume",
            ),
        )
    return await _queue_partial_response(request)


@router.post("/queue/download/{dl_hash}/reset")
async def reset_download_by_hash(request: Request, dl_hash: str) -> Response:
    """Reset all grabbed volumes for a download_id back to wanted (for missing/orphaned items)."""
    cleanup_allowed = False
    cleanup_status = "not_found"
    with get_db() as db:
        cleanup = _reserve_manual_download_cleanup(db, dl_hash)
        cleanup_allowed = cleanup.allowed
        cleanup_status = cleanup.status
        if cleanup_allowed and cleanup.identity is not None:
            grabbed = _grabbed_volumes_for_identity(db, cleanup.identity)
            if grabbed:
                _delete_seen_rows(
                    db,
                    _seen_rows_for_identity(db, cleanup.identity),
                )
                db.executemany(
                    "UPDATE volumes SET status='wanted', download_id=NULL,"
                    " grabbed_at=NULL, source_url=NULL, torrent_name=NULL,"
                    " indexer=NULL, protocol=NULL, client=NULL,"
                    " download_client_id=NULL, release_group=NULL"
                    " WHERE id=? AND status='grabbed'",
                    ((row["id"],) for row in grabbed),
                )
                by_series: dict[int, list[int]] = {}
                for row in grabbed:
                    by_series.setdefault(row["series_id"], []).append(row["id"])
                for sid, vol_ids in by_series.items():
                    cascade_chapters(
                        db,
                        sid,
                        vol_ids,
                        "wanted",
                        grabbed_at=None,
                        torrent_name=None,
                        torrent_url=None,
                        indexer=None,
                        protocol=None,
                        client=None,
                        download_client_id=None,
                        download_id=None,
                        release_group=None,
                    )
                    _clear_chapter_download_owners(db, sid, vol_ids)
    if not cleanup_allowed:
        return await _queue_partial_response(
            request,
            message=_manual_cleanup_failure_message(
                cleanup_status,
                "resetting this download",
            ),
        )
    return await _queue_partial_response(request)


@router.post(
    "/api/queue/download-clients/{download_client_id}/downloads/{torrent_hash}/remove"
)
@router.post("/queue/download/client/{download_client_id}/{torrent_hash}/remove")
async def remove_owned_from_queue(
    request: Request,
    download_client_id: int,
    torrent_hash: str,
    remove_from_client: str = Form("1"),
    delete_files: str = Form("0"),
    blocklist: str = Form("0"),
    change_category: str = Form(""),
) -> Response:
    """Remove one owner-qualified download without protocol/client fallback."""
    return await _remove_from_queue(
        request,
        torrent_hash,
        download_client_id=download_client_id,
        remove_from_client=remove_from_client,
        delete_files=delete_files,
        blocklist=blocklist,
        change_category=change_category,
    )


async def _remove_from_queue(
    request: Request,
    torrent_hash: str,
    *,
    download_client_id: int | None,
    remove_from_client: str,
    delete_files: str,
    blocklist: str,
    change_category: str,
) -> Response:
    """Remove a torrent from the queue.

    Params:
      remove_from_client — "1" to delete from download client, "0" to keep (Mangarr tracking only)
      delete_files       — "1" to also delete downloaded files (only when remove_from_client=1)
      blocklist          — "1" to add to blocklist so the release won't be re-grabbed
      change_category    — optional: change qBit category before untracking (only when keeping in client)
    """
    import main as _m

    cleanup_allowed = False
    cleanup_status = "not_found"
    identity: _DownloadIdentity | None = None
    seen_row: dict[str, Any] | None = None
    exact_client: dict[str, Any] | None = None
    cat_new = change_category.strip()
    if remove_from_client == "1" or cat_new:
        with get_db() as lookup_db:
            lookup_identity, lookup_status = _resolve_manual_download_identity(
                lookup_db,
                torrent_hash,
                download_client_id=download_client_id,
            )
            if lookup_identity is not None:
                exact_client = _load_exact_download_client(
                    lookup_db,
                    lookup_identity,
                )
        if lookup_identity is None or exact_client is None:
            failure_status = (
                lookup_status if lookup_identity is None else "client_unavailable"
            )
            return await _queue_partial_response(
                request,
                message=_manual_cleanup_failure_message(
                    failure_status,
                    "removing or untracking this download",
                ),
            )
    with get_db() as db:
        cleanup = _reserve_manual_download_cleanup(
            db,
            torrent_hash,
            download_client_id=download_client_id,
        )
        cleanup_allowed = cleanup.allowed
        cleanup_status = cleanup.status
        identity = cleanup.identity
        if cleanup_allowed and identity is not None:
            seen_rows = _seen_rows_for_identity(db, identity)
            seen_row = seen_rows[0] if seen_rows else None

            if blocklist == "1" and seen_row:
                db.execute(
                    "INSERT OR IGNORE INTO blocklist"
                    "(series_id, torrent_url, torrent_name, reason,"
                    " indexer, protocol) VALUES(?,?,?,?,?,?)",
                    (
                        seen_row["series_id"],
                        seen_row["torrent_url"] or "",
                        seen_row["torrent_name"] or "",
                        "Manually removed from queue",
                        seen_row["indexer"] or "",
                        seen_row["protocol"] or "",
                    ),
                )

            grabbed = _grabbed_volumes_for_identity(db, identity)
            by_series: dict[int, list[int]] = {}
            for volume in grabbed:
                if volume["volume_num"] is not None:
                    by_series.setdefault(volume["series_id"], []).append(volume["id"])
            db.executemany(
                "DELETE FROM volumes WHERE id=? AND status='grabbed'"
                " AND volume_num IS NULL",
                (
                    (volume["id"],)
                    for volume in grabbed
                    if volume["volume_num"] is None
                ),
            )
            db.executemany(
                "UPDATE volumes SET status='wanted', download_id=NULL,"
                " grabbed_at=NULL, source_url=NULL, torrent_name=NULL,"
                " indexer=NULL, protocol=NULL, client=NULL,"
                " download_client_id=NULL, release_group=NULL"
                " WHERE id=? AND status='grabbed'",
                (
                    (volume["id"],)
                    for volume in grabbed
                    if volume["volume_num"] is not None
                ),
            )
            for series_id, volume_ids in by_series.items():
                cascade_chapters(
                    db,
                    series_id,
                    volume_ids,
                    "wanted",
                    grabbed_at=None,
                    torrent_name=None,
                    torrent_url=None,
                    indexer=None,
                    protocol=None,
                    client=None,
                    download_client_id=None,
                    download_id=None,
                    release_group=None,
                )
                _clear_chapter_download_owners(db, series_id, volume_ids)
            _delete_seen_rows(db, seen_rows)
            if seen_row:
                action = "Removed" if remove_from_client == "1" else "Untracked"
                bl_note = " (blocklisted)" if blocklist == "1" else ""
                _m.log_event(
                    "warning",
                    f"{action} from queue{bl_note}: {seen_row['torrent_name']}",
                    seen_row["series_id"],
                    db=db,
                )

    if not cleanup_allowed:
        return await _queue_partial_response(
            request,
            message=_manual_cleanup_failure_message(
                cleanup_status,
                "removing or untracking this download",
            ),
        )
    if identity is None:
        return await _queue_partial_response(
            request,
            message="Download identity could not be resolved; refresh the queue",
        )

    # Optional: change category in download client before untracking
    if cat_new and remove_from_client != "1" and identity.client_kind == "qbit":
        if exact_client:
            _cc_host = (exact_client.get("host") or "").rstrip("/")
            _cc_user = exact_client.get("username") or ""
            _cc_pw = exact_client.get("password") or ""
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    r = await client.post(
                        f"{_cc_host}/api/v2/auth/login",
                        data={"username": _cc_user, "password": _cc_pw},
                    )
                    if "Ok" in r.text:
                        await client.post(
                            f"{_cc_host}/api/v2/torrents/createCategory",
                            data={"category": cat_new, "savePath": ""},
                        )
                        await client.post(
                            f"{_cc_host}/api/v2/torrents/setCategory",
                            data={"hashes": identity.external_id, "category": cat_new},
                        )
            except Exception:
                pass

    # Remove from download client (optional)
    if remove_from_client == "1":
        if identity.client_kind == "sab":
            await _m.sab_remove(identity.external_id, client=exact_client)
        else:
            await _m.qbit_remove(
                identity.external_id,
                delete_files=delete_files == "1",
                client=exact_client,
            )

    return await _queue_partial_response(request)


@router.post("/queue/torrent/{torrent_hash}/remove")
async def remove_from_queue(
    request: Request,
    torrent_hash: str,
    remove_from_client: str = Form("1"),
    delete_files: str = Form("0"),
    blocklist: str = Form("0"),
    change_category: str = Form(""),
) -> Response:
    """Legacy ID-only removal, accepted only for one unambiguous owner."""
    return await _remove_from_queue(
        request,
        torrent_hash,
        download_client_id=None,
        remove_from_client=remove_from_client,
        delete_files=delete_files,
        blocklist=blocklist,
        change_category=change_category,
    )


@router.post(
    "/api/queue/download-clients/{download_client_id}/downloads/{torrent_hash}/block-remove"
)
@router.post("/queue/download/client/{download_client_id}/{torrent_hash}/block-remove")
async def block_and_remove_owned(
    request: Request,
    download_client_id: int,
    torrent_hash: str,
    delete_files: str = Form("1"),
) -> Response:
    """Block and remove one owner-qualified download."""
    return await _block_and_remove(
        request,
        torrent_hash,
        download_client_id=download_client_id,
        delete_files=delete_files,
    )


async def _block_and_remove(
    request: Request,
    torrent_hash: str,
    *,
    download_client_id: int | None,
    delete_files: str,
) -> Response:
    """Blacklist the release, remove from client, reset volume to wanted, trigger re-search."""
    import main as _m

    cleanup_allowed = False
    cleanup_status = "not_found"
    identity: _DownloadIdentity | None = None
    seen_row: dict[str, Any] | None = None
    with get_db() as lookup_db:
        lookup_identity, lookup_status = _resolve_manual_download_identity(
            lookup_db,
            torrent_hash,
            download_client_id=download_client_id,
        )
        exact_client = (
            _load_exact_download_client(lookup_db, lookup_identity)
            if lookup_identity is not None
            else None
        )
    if lookup_identity is None or exact_client is None:
        failure_status = (
            lookup_status if lookup_identity is None else "client_unavailable"
        )
        return await _queue_partial_response(
            request,
            message=_manual_cleanup_failure_message(
                failure_status,
                "blocking or removing this download",
            ),
        )
    with get_db() as db:
        cleanup = _reserve_manual_download_cleanup(
            db,
            torrent_hash,
            download_client_id=download_client_id,
        )
        cleanup_allowed = cleanup.allowed
        cleanup_status = cleanup.status
        identity = cleanup.identity
        if cleanup_allowed and identity is not None:
            seen_rows = _seen_rows_for_identity(db, identity)
            seen_row = seen_rows[0] if seen_rows else None
            if seen_row:
                db.execute(
                    "INSERT OR IGNORE INTO blocklist"
                    "(series_id, torrent_url, torrent_name, reason,"
                    " indexer, protocol) VALUES(?,?,?,?,?,?)",
                    (
                        seen_row["series_id"],
                        seen_row["torrent_url"] or "",
                        seen_row["torrent_name"] or "",
                        "Manually blocked from queue",
                        seen_row["indexer"] or "",
                        seen_row["protocol"] or "",
                    ),
                )
            grabbed = _grabbed_volumes_for_identity(db, identity)
            by_series: dict[int, list[int]] = {}
            for volume in grabbed:
                if volume["volume_num"] is not None:
                    by_series.setdefault(volume["series_id"], []).append(volume["id"])
            db.executemany(
                "UPDATE volumes SET status='wanted', download_id=NULL,"
                " grabbed_at=NULL, source_url=NULL, torrent_name=NULL,"
                " indexer=NULL, protocol=NULL, client=NULL,"
                " download_client_id=NULL, release_group=NULL"
                " WHERE id=? AND status='grabbed'",
                (
                    (volume["id"],)
                    for volume in grabbed
                    if volume["volume_num"] is not None
                ),
            )
            db.executemany(
                "DELETE FROM volumes WHERE id=? AND volume_num IS NULL",
                (
                    (volume["id"],)
                    for volume in grabbed
                    if volume["volume_num"] is None
                ),
            )
            for series_id, volume_ids in by_series.items():
                cascade_chapters(
                    db,
                    series_id,
                    volume_ids,
                    "wanted",
                    grabbed_at=None,
                    torrent_name=None,
                    torrent_url=None,
                    indexer=None,
                    protocol=None,
                    client=None,
                    download_client_id=None,
                    download_id=None,
                    release_group=None,
                )
                _clear_chapter_download_owners(db, series_id, volume_ids)
            _delete_seen_rows(db, seen_rows)

    if not cleanup_allowed:
        return await _queue_partial_response(
            request,
            message=_manual_cleanup_failure_message(
                cleanup_status,
                "blocking or removing this download",
            ),
        )
    if identity is None:
        return await _queue_partial_response(
            request,
            message="Download identity could not be resolved; refresh the queue",
        )

    if identity.client_kind == "sab":
        await _m.sab_remove(identity.external_id, client=exact_client)
    else:
        await _m.qbit_remove(
            identity.external_id,
            delete_files=delete_files == "1",
            client=exact_client,
        )
    if seen_row:
        with get_db() as db:
            series_row = db.execute(
                "SELECT title, search_pattern FROM series WHERE id=?",
                (seen_row["series_id"],),
            ).fetchone()
            s = dict(series_row) if series_row is not None else None
        if s:
            _m.create_background_task(
                _m.grab_existing(
                    seen_row["series_id"], s["title"], s["search_pattern"]
                ),
                name=f"queue:grab_again:{seen_row['series_id']}",
            )
    return await _queue_partial_response(request)


@router.post("/queue/torrent/{torrent_hash}/block-remove")
async def block_and_remove(
    request: Request,
    torrent_hash: str,
    delete_files: str = Form("1"),
) -> Response:
    """Legacy ID-only block/remove, accepted only for one concrete owner."""
    return await _block_and_remove(
        request,
        torrent_hash,
        download_client_id=None,
        delete_files=delete_files,
    )


@router.post(
    "/api/queue/download-clients/{download_client_id}/downloads/{torrent_hash}/set-category"
)
@router.post("/queue/download/client/{download_client_id}/{torrent_hash}/set-category")
async def set_owned_torrent_category(
    request: Request,
    download_client_id: int,
    torrent_hash: str,
    category: str = Form(...),
) -> Response:
    """Change category through the exact qBit owner rendered for the item."""
    return await _set_torrent_category(
        request,
        torrent_hash,
        download_client_id=download_client_id,
        category=category,
    )


async def _set_torrent_category(
    request: Request,
    torrent_hash: str,
    *,
    download_client_id: int | None,
    category: str,
) -> Response:
    """Change one resolved qBit item without protocol/default fallback."""
    with get_db() as db:
        identity, resolution_status = _resolve_manual_download_identity(
            db,
            torrent_hash,
            download_client_id=download_client_id,
        )
        exact_client = (
            _load_exact_download_client(db, identity)
            if identity is not None and identity.protocol == "torrent"
            else None
        )
    if identity is None:
        return await _queue_partial_response(
            request,
            message=_manual_cleanup_failure_message(
                resolution_status,
                "changing its category",
            ),
        )
    if identity.protocol != "torrent":
        return await _queue_partial_response(
            request,
            message="Only qBittorrent downloads have a category",
        )
    if exact_client is None:
        return await _queue_partial_response(
            request,
            message=_manual_cleanup_failure_message(
                "client_unavailable",
                "changing its category",
            ),
        )

    host = (exact_client.get("host") or "").rstrip("/")
    user = exact_client.get("username") or ""
    password = exact_client.get("password") or ""
    normalized_category = category.strip()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{host}/api/v2/auth/login",
                data={"username": user, "password": password},
            )
            if "Ok" in response.text:
                await client.post(
                    f"{host}/api/v2/torrents/createCategory",
                    data={"category": normalized_category, "savePath": ""},
                )
                await client.post(
                    f"{host}/api/v2/torrents/setCategory",
                    data={
                        "hashes": identity.external_id,
                        "category": normalized_category,
                    },
                )
    except Exception:
        pass
    return await _queue_partial_response(request)


@router.post("/queue/torrent/{torrent_hash}/set-category")
async def set_torrent_category(
    request: Request,
    torrent_hash: str,
    category: str = Form(...),
) -> Response:
    """Legacy ID-only category change, accepted only for one exact owner."""
    return await _set_torrent_category(
        request,
        torrent_hash,
        download_client_id=None,
        category=category,
    )


@router.post("/queue/pending/{pending_id}/force-grab")
async def force_grab_pending(request: Request, pending_id: int):
    """Immediately grab a pending release, bypassing its delay profile."""
    row = None
    item = None
    with get_db() as db:
        pending_row = db.execute(
            "SELECT id, series_id, url, title, indexer, protocol, size_bytes"
            " FROM pending_releases WHERE id=?",
            (pending_id,),
        ).fetchone()
        row = dict(pending_row) if pending_row is not None else None
        if row:
            item = {
                "url": row["url"],
                "title": row["title"],
                "indexer": row["indexer"] or "",
                "protocol": row["protocol"] or "torrent",
                "size_bytes": row["size_bytes"] or 0,
            }
            db.execute("DELETE FROM pending_releases WHERE id=?", (pending_id,))
    if row is None or item is None:
        return await _queue_partial_response(request)
    import main as _m

    await _m.grab_item(item, row["series_id"])
    return await _queue_partial_response(request)


@router.post("/queue/pending/{pending_id}/dismiss")
async def dismiss_pending(request: Request, pending_id: int):
    """Remove a pending release from the delay queue without grabbing it."""
    dismiss_pending_release(pending_id)
    return await _queue_partial_response(request)
