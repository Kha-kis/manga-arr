"""Queue page — grabbed items, download client status, pending releases."""

import asyncio
import json
import sqlite3
from collections import defaultdict as _dd
from typing import Any, Literal, NamedTuple

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

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
    from routers.download_clients import get_client_for_protocol as _gcp

    with get_db() as _db:
        qc = _gcp(_db, "torrent")
        sc = _gcp(_db, "nzb")

    def _one(snap, configured: bool) -> dict[str, object]:
        if not configured:
            return {"label": "disabled", "age_seconds": None, "error": None}
        label = _sc.DOWNLOAD_STATUS_CACHE.freshness_label(snap)
        if snap is None or snap.last_success_at is None:
            age = None
        else:
            from datetime import datetime, timezone

            age = int(
                (datetime.now(timezone.utc) - snap.last_success_at).total_seconds()
            )
        return {
            "label": label,
            "age_seconds": age,
            "error": snap.error if snap else None,
        }

    return {
        "qbit": _one(_sc.DOWNLOAD_STATUS_CACHE.snapshot_qbit(), bool(qc)),
        "sab": _one(_sc.DOWNLOAD_STATUS_CACHE.snapshot_sab(), bool(sc)),
    }


router = APIRouter()


class _DownloadIdentity(NamedTuple):
    persisted_id: str
    external_id: str
    client_kind: Literal["qbit", "sab"]


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
    """Classify a persisted identity without changing SAB's case-sensitive ID."""
    identifier = download_id.lower() if isinstance(download_id, str) else ""
    client_name = client.lower() if isinstance(client, str) else ""
    protocol_name = protocol.lower() if isinstance(protocol, str) else ""
    if client_name in ("sab", "sabnzbd"):
        return "sab"
    if client_name in ("qbit", "qbittorrent"):
        return "qbit"
    if protocol_name in ("nzb", "usenet"):
        return "sab"
    if protocol_name == "torrent":
        return "qbit"
    return "sab" if identifier.startswith("sabnzbd_nzo_") else "qbit"


def _identity_key(
    download_id: str,
    client: object = None,
    protocol: object = None,
) -> tuple[Literal["qbit", "sab"], str]:
    """Return the client-qualified key used for queue grouping and rendering."""
    client_kind = _download_client_kind(download_id, client, protocol)
    identity_id = download_id if client_kind == "sab" else download_id.lower()
    return client_kind, identity_id


def _resolve_manual_download_identity(
    db: sqlite3.Connection,
    requested_id: str,
) -> tuple[
    _DownloadIdentity | None,
    Literal["ok", "ambiguous", "not_found"],
]:
    """Resolve one exact client-qualified identity before acquiring a writer lock.

    Exact persisted IDs take precedence. A case-insensitive compatibility lookup
    is accepted only when all matching evidence describes one distinct identity.
    """
    rows = db.execute(
        """
        SELECT download_id, client, protocol, 'domain' AS source_kind
        FROM volumes
        WHERE download_id IS NOT NULL
          AND lower(download_id)=lower(?)
        UNION ALL
        SELECT download_id, client, protocol, 'domain' AS source_kind
        FROM seen
        WHERE download_id IS NOT NULL
          AND lower(download_id)=lower(?)
        UNION ALL
        SELECT download_id, NULL AS client, NULL AS protocol,
               'queue' AS source_kind
        FROM import_queue
        WHERE download_id IS NOT NULL
          AND lower(download_id)=lower(?)
        """,
        (requested_id, requested_id, requested_id),
    ).fetchall()
    if not rows:
        return None, "not_found"

    explicit_kinds: dict[str, set[Literal["qbit", "sab"]]] = {}
    queue_ids: set[str] = set()
    for row in rows:
        persisted_id = row["download_id"]
        if row["source_kind"] == "queue":
            queue_ids.add(persisted_id)
            continue
        explicit_kinds.setdefault(persisted_id, set()).add(
            _download_client_kind(
                persisted_id,
                row["client"],
                row["protocol"],
            )
        )

    persisted_ids = set(explicit_kinds) | queue_ids
    identities: dict[str, _DownloadIdentity] = {}
    ambiguous_ids: set[str] = set()
    for persisted_id in persisted_ids:
        kinds = explicit_kinds.get(persisted_id)
        if not kinds:
            kinds = {_download_client_kind(persisted_id)}
        if len(kinds) != 1:
            ambiguous_ids.add(persisted_id)
            continue
        client_kind: Literal["qbit", "sab"] = (
            "sab" if "sab" in kinds else "qbit"
        )
        identities[persisted_id] = _DownloadIdentity(
            persisted_id,
            persisted_id if client_kind == "sab" else persisted_id.lower(),
            client_kind,
        )

    if requested_id in ambiguous_ids:
        return None, "ambiguous"
    route_key_matches = [
        identity
        for identity in identities.values()
        if identity.external_id == requested_id
    ]
    if len({identity.client_kind for identity in route_key_matches}) > 1:
        return None, "ambiguous"
    exact = identities.get(requested_id)
    if exact is not None:
        return exact, "ok"

    if ambiguous_ids or len(identities) != 1:
        return None, "ambiguous"
    identity = next(iter(identities.values()))
    return identity, "ok"


def _row_matches_identity(
    row: sqlite3.Row,
    identity: _DownloadIdentity,
) -> bool:
    return (
        row["download_id"] == identity.persisted_id
        and _download_client_kind(
            row["download_id"],
            row["client"],
            row["protocol"],
        )
        == identity.client_kind
    )


def _grabbed_volumes_for_identity(
    db: sqlite3.Connection,
    identity: _DownloadIdentity,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in db.execute(
            "SELECT * FROM volumes"
            " WHERE download_id=? AND status='grabbed'",
            (identity.persisted_id,),
        ).fetchall()
        if _row_matches_identity(row, identity)
    ]


def _seen_rows_for_identity(
    db: sqlite3.Connection,
    identity: _DownloadIdentity,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in db.execute(
            "SELECT * FROM seen WHERE download_id=?",
            (identity.persisted_id,),
        ).fetchall()
        if _row_matches_identity(row, identity)
    ]


def _delete_seen_rows(
    db: sqlite3.Connection,
    rows: list[dict[str, Any]],
) -> None:
    db.executemany(
        "DELETE FROM seen WHERE torrent_url=?",
        ((row["torrent_url"],) for row in rows),
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
    return f"Download is no longer tracked; refresh the queue before {action}"


def _reserve_manual_download_cleanup(
    db: sqlite3.Connection,
    download_id: str,
) -> _ManualDownloadCleanup:
    """Resolve and skip unowned review rows for one exact client identity.

    Identity resolution is read-only and happens before the parent UPDATE. The
    UPDATE remains the transaction's first mutation, making a worker claim and
    manual cleanup mutually exclusive across separate SQLite connections.
    """
    db.execute("BEGIN IMMEDIATE")
    identity, resolution_status = _resolve_manual_download_identity(
        db,
        download_id,
    )
    if identity is None:
        return _ManualDownloadCleanup(
            False,
            resolution_status,
            None,
            (),
        )

    transitioned = [
        int(row["id"])
        for row in db.execute(
            """
            UPDATE import_queue
            SET status='skipped'
            WHERE download_id IS NOT NULL
              AND download_id=?
              AND status IN ('pending','partial')
              AND lease_owner IS NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM import_queue active
                  WHERE active.download_id IS NOT NULL
                    AND active.download_id=?
                    AND (
                        active.status='importing'
                        OR active.lease_owner IS NOT NULL
                    )
              )
            RETURNING id
            """,
            (identity.persisted_id, identity.persisted_id),
        ).fetchall()
    ]
    active = db.execute(
        "SELECT 1 FROM import_queue"
        " WHERE download_id IS NOT NULL AND download_id=?"
        " AND (status='importing' OR lease_owner IS NOT NULL)"
        " LIMIT 1",
        (identity.persisted_id,),
    ).fetchone()
    if active is not None:
        return _ManualDownloadCleanup(
            False,
            "in_progress",
            identity,
            (),
        )
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
    _qbit_snap = _sc.DOWNLOAD_STATUS_CACHE.snapshot_qbit()
    _sab_snap = _sc.DOWNLOAD_STATUS_CACHE.snapshot_sab()
    all_client_items: dict[
        tuple[Literal["qbit", "sab"], str],
        dict[str, Any],
    ] = {}
    if _qbit_snap:
        for download_id, item in _qbit_snap.items.items():
            all_client_items[("qbit", str(download_id).lower())] = item
    if _sab_snap:
        for download_id, item in _sab_snap.items.items():
            all_client_items[("sab", str(download_id))] = item

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
            tuple[Literal["qbit", "sab"], str],
            dict[str, Any],
        ] = {}
        for _sm in db.execute(
            "SELECT download_id, client, protocol, indexer, size_bytes"
            " FROM seen WHERE download_id IS NOT NULL"
        ).fetchall():
            persisted_id = _sm["download_id"] or ""
            identity_key = _identity_key(
                persisted_id,
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
            tuple[Literal["qbit", "sab"], str],
            dict[str, Any],
        ] = {}
        for q in pending_raw:
            persisted_id = q["download_id"] or ""
            identity_evidence = db.execute(
                "SELECT client, protocol FROM volumes WHERE download_id=?"
                " UNION ALL"
                " SELECT client, protocol FROM seen WHERE download_id=?",
                (persisted_id, persisted_id),
            ).fetchall()
            evidence_kinds = {
                _download_client_kind(
                    persisted_id,
                    evidence["client"],
                    evidence["protocol"],
                )
                for evidence in identity_evidence
            }
            identity_kind = (
                next(iter(evidence_kinds))
                if len(evidence_kinds) == 1
                else _download_client_kind(persisted_id)
            )
            identity_key = _identity_key(
                persisted_id,
                identity_kind,
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
                "download_id": identity_key[1],
                "needs_review": needs_review,
                "files": files,
            }

        grabbed_raw = db.execute(
            "SELECT v.id, v.series_id, v.volume_num, v.pack_type,"
            " v.vol_range_start, v.vol_range_end, v.grabbed_at,"
            " v.download_id, v.torrent_name, v.client as grabbed_client,"
            " v.protocol as grabbed_protocol,"
            " s.title as series_title "
            "FROM volumes v JOIN series s ON s.id=v.series_id "
            "WHERE v.status='grabbed' "
            "ORDER BY v.grabbed_at DESC"
        ).fetchall()

        by_dlid: dict[
            tuple[Literal["qbit", "sab"], str],
            list[sqlite3.Row],
        ] = _dd(list)
        for v in grabbed_raw:
            persisted_id = v["download_id"] or ""
            by_dlid[
                _identity_key(
                    persisted_id,
                    v["grabbed_client"],
                    v["grabbed_protocol"],
                )
            ].append(v)

        queue_rows: list[dict[str, Any]] = []
        seen_dlids: set[tuple[Literal["qbit", "sab"], str]] = set()

        for identity_key, vols in by_dlid.items():
            seen_dlids.add(identity_key)
            v0 = vols[0]
            sm = seen_meta.get(identity_key, {})
            external_download_id = identity_key[1]

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
                live = all_client_items.get(identity_key, {})
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
            elif identity_key in all_client_items:
                live = all_client_items[identity_key]
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
            live = all_client_items.get(identity_key, {})
            sm = seen_meta.get(identity_key, {})
            stage = "review" if pq["needs_review"] else "importing"
            external_download_id = identity_key[1]
            is_sab = identity_key[0] == "sab"
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
            "SELECT status, source_url, download_id, series_id, client, protocol"
            " FROM volumes WHERE id=?",
            (vol_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "status": "not_found"}
        if row["status"] != "grabbed":
            return {"ok": False, "status": "not_grabbed"}

        if row["download_id"]:
            cleanup = _reserve_manual_download_cleanup(
                db,
                row["download_id"],
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
            " client=NULL, release_group=NULL WHERE id=? AND status='grabbed'",
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
            download_id=None,
            release_group=None,
        )
    return {"ok": True, "status": "reset"}


def dismiss_pending_release(pending_id: int) -> dict[str, object]:
    """Remove one pending delayed release from the queue."""
    with get_db() as db:
        cur = db.execute("DELETE FROM pending_releases WHERE id=?", (pending_id,))
        if cur.rowcount < 1:
            return {"ok": False, "status": "not_found"}
    return {"ok": True, "status": "dismissed"}


@router.post("/queue/grabbed/{dl_hash}/reset-all")
async def reset_orphaned_by_hash(request: Request, dl_hash: str):
    """Reset all grabbed volumes sharing a download_id back to wanted (for 'missing' queue items)."""
    cleanup_allowed = False
    cleanup_status = "not_found"
    with get_db() as db:
        cleanup = _reserve_manual_download_cleanup(db, dl_hash)
        cleanup_allowed = cleanup.allowed
        cleanup_status = cleanup.status
        if cleanup_allowed and cleanup.identity is not None:
            volumes = _grabbed_volumes_for_identity(db, cleanup.identity)
            _delete_seen_rows(
                db,
                _seen_rows_for_identity(db, cleanup.identity),
            )
            db.executemany(
                "UPDATE volumes SET status='wanted', download_id=NULL,"
                " grabbed_at=NULL, source_url=NULL, torrent_name=NULL,"
                " indexer=NULL, protocol=NULL, client=NULL, release_group=NULL"
                " WHERE id=? AND status='grabbed'",
                (
                    (volume["id"],)
                    for volume in volumes
                    if volume["volume_num"] is not None
                ),
            )
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
async def reset_download_by_hash(request: Request, dl_hash: str):
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
                    " release_group=NULL"
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
                        download_id=None,
                        release_group=None,
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


@router.post("/queue/torrent/{torrent_hash}/remove")
async def remove_from_queue(
    request: Request,
    torrent_hash: str,
    remove_from_client: str = Form("1"),
    delete_files: str = Form("0"),
    blocklist: str = Form("0"),
    change_category: str = Form(""),
):
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
    with get_db() as db:
        cleanup = _reserve_manual_download_cleanup(db, torrent_hash)
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
                " indexer=NULL, protocol=NULL, client=NULL, release_group=NULL"
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
                    download_id=None,
                    release_group=None,
                )
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
    cat_new = change_category.strip()
    if cat_new and remove_from_client != "1" and identity.client_kind == "qbit":
        from routers.download_clients import get_client_for_protocol as _gcp_cc

        with get_db() as _cc_db:
            _cc_c = _gcp_cc(_cc_db, "torrent")
        if _cc_c:
            _cc_host = (_cc_c.get("host") or "").rstrip("/")
            _cc_user = _cc_c.get("username") or ""
            _cc_pw = _cc_c.get("password") or ""
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
            await _m.sab_remove(identity.external_id)
        else:
            await _m.qbit_remove(
                identity.external_id,
                delete_files=delete_files == "1",
            )

    return await _queue_partial_response(request)


@router.post("/queue/torrent/{torrent_hash}/block-remove")
async def block_and_remove(
    request: Request, torrent_hash: str, delete_files: str = Form("1")
):
    """Blacklist the release, remove from client, reset volume to wanted, trigger re-search."""
    import main as _m

    cleanup_allowed = False
    cleanup_status = "not_found"
    identity: _DownloadIdentity | None = None
    seen_row: dict[str, Any] | None = None
    with get_db() as db:
        cleanup = _reserve_manual_download_cleanup(db, torrent_hash)
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
            db.executemany(
                "UPDATE volumes SET status='wanted', download_id=NULL,"
                " grabbed_at=NULL, source_url=NULL, torrent_name=NULL,"
                " indexer=NULL, protocol=NULL, client=NULL, release_group=NULL"
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
        await _m.sab_remove(identity.external_id)
    else:
        await _m.qbit_remove(
            identity.external_id,
            delete_files=delete_files == "1",
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


@router.post("/queue/torrent/{torrent_hash}/set-category")
async def set_torrent_category(
    request: Request, torrent_hash: str, category: str = Form(...)
):
    """Change the qBittorrent category for an active torrent.

    Useful to move a torrent from a pre-import category to the import category,
    or to correct a mis-categorised grab.
    """
    from routers.download_clients import get_client_for_protocol as _gcp_sc

    with get_db() as _sc_db:
        _sc_c = _gcp_sc(_sc_db, "torrent")
    if _sc_c:
        host = (_sc_c.get("host") or "").rstrip("/")
        user = _sc_c.get("username") or ""
        pw = _sc_c.get("password") or ""
        cat = category.strip()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    f"{host}/api/v2/auth/login", data={"username": user, "password": pw}
                )
                if "Ok" in r.text:
                    # Ensure the category exists in qBittorrent first
                    await client.post(
                        f"{host}/api/v2/torrents/createCategory",
                        data={"category": cat, "savePath": ""},
                    )
                    # Set the category on the torrent
                    await client.post(
                        f"{host}/api/v2/torrents/setCategory",
                        data={"hashes": torrent_hash, "category": cat},
                    )
        except Exception:
            pass
    return await _queue_partial_response(request)


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
