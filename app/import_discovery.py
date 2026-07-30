"""Download client discovery: poll qBittorrent/SABnzbd for completed downloads."""

import asyncio
import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from typing import cast

import httpx

from clients import load_bound_download_client
from download_identity import (
    DownloadIdentity,
    DownloadProtocol,
    coerce_download_client_id,
    download_identities_match,
)
from shared import get_cfg, get_db
from events import add_history, log_event
from routers.download_clients import apply_remote_path_mapping
from import_queue import _queue_import
from import_workers import schedule_import_worker

log = logging.getLogger(__name__)


# Single-flight guard for check_download_status. Evidence from issue #31
# follow-up A: the function's body takes 7-38s per run and was being
# spawned concurrently (up to 4× at once) from:
#   - status_loop (every 5 min)
#   - /api/check-downloads button
#   - /api/backfill-packs / system endpoints
# Overlapping runs amplify event-loop blocking and DB write contention.
# When one run is in flight, additional invocations are no-ops — the
# in-flight run will pick up whatever new state the caller cared about.
_CHECK_DOWNLOAD_STATUS_LOCK = asyncio.Lock()


_SupportedClientType = Literal["qbittorrent", "sabnzbd"]


@dataclass(frozen=True, slots=True)
class _ClientPollPartition:
    client_id: int
    name: str
    client_type: _SupportedClientType
    include_legacy_ownerless: bool


def _download_client_poll_partitions(
    client_type: _SupportedClientType,
) -> list[_ClientPollPartition]:
    """Snapshot exact owners that have completion or orphan work to poll."""
    if client_type == "qbittorrent":
        evidence_sql = """
            SELECT DISTINCT download_client_id
            FROM seen
            WHERE client='qbittorrent' AND protocol='torrent'
              AND download_id IS NOT NULL
            UNION
            SELECT DISTINCT download_client_id
            FROM volumes
            WHERE client='qbittorrent' AND status='grabbed'
              AND download_id IS NOT NULL
        """
    else:
        evidence_sql = """
            SELECT DISTINCT download_client_id
            FROM seen
            WHERE client='sabnzbd' AND protocol='nzb'
              AND download_id IS NOT NULL
            UNION
            SELECT DISTINCT download_client_id
            FROM volumes
            WHERE client='sabnzbd' AND status='grabbed'
              AND download_id IS NOT NULL
        """

    with get_db() as db:
        evidence_rows = db.execute(evidence_sql).fetchall()
        owner_ids = {
            owner_id
            for row in evidence_rows
            if (
                owner_id := coerce_download_client_id(
                    row["download_client_id"]
                )
            )
            is not None
        }
        has_legacy = any(row["download_client_id"] is None for row in evidence_rows)
        configured_rows = [
            dict(row)
            for row in db.execute(
                "SELECT id, name, type, enabled FROM download_clients"
                " WHERE type=? ORDER BY priority, id",
                (client_type,),
            ).fetchall()
        ]
        configured_by_id = {
            int(row["id"]): row
            for row in configured_rows
            if coerce_download_client_id(row["id"]) is not None
        }
        missing_ids = sorted(owner_ids - configured_by_id.keys())
        for missing_id in missing_ids:
            log_event(
                "configuration_error",
                f"Download discovery cannot poll deleted {client_type} owner "
                f"id={missing_id}; ownership was not reassigned",
                db=db,
                dedup=True,
            )

        enabled = [
            row for row in configured_rows if int(row["enabled"] or 0) == 1
        ]
        legacy_owner_id: int | None = None
        if has_legacy:
            possible_owner_ids = set(configured_by_id) | owner_ids
            enabled_owner_id = (
                int(enabled[0]["id"]) if len(enabled) == 1 else None
            )
            if (
                enabled_owner_id is not None
                and possible_owner_ids == {enabled_owner_id}
            ):
                legacy_owner_id = enabled_owner_id
            else:
                log_event(
                    "configuration_error",
                    f"Legacy ownerless {client_type} downloads were left "
                    f"unpolled because {len(possible_owner_ids)} possible "
                    f"owner(s), including {len(enabled)} enabled, make ownership "
                    "ambiguous",
                    db=db,
                    dedup=True,
                )

    poll_ids = set(owner_ids)
    if legacy_owner_id is not None:
        poll_ids.add(legacy_owner_id)
    return [
        _ClientPollPartition(
            client_id=client_id,
            name=str(configured_by_id[client_id]["name"]),
            client_type=client_type,
            include_legacy_ownerless=client_id == legacy_owner_id,
        )
        for client_id in sorted(poll_ids)
        if client_id in configured_by_id
    ]


def _load_poll_client(
    partition: _ClientPollPartition,
) -> dict[str, Any] | None:
    """Load the partition's exact current credentials without route fallback."""
    bound = load_bound_download_client(
        partition.client_id,
        expected_type=partition.client_type,
        expected_name=partition.name,
    )
    if bound.client is not None:
        return bound.client
    log_event(
        "configuration_error",
        f"Download discovery skipped {partition.client_type} owner "
        f"id={partition.client_id}: {bound.reason}",
        dedup=True,
    )
    return None


def _reserve_orphan_cleanup(
    db: sqlite3.Connection,
    download_id: str,
    *,
    download_client_id: int | None,
    protocol: DownloadProtocol,
) -> tuple[bool, list[int]]:
    """Acquire the writer tx and protect active imports for one download.

    Pending, partial, and importing rows are active protection. Failed rows are
    the only nonactive work transitioned by orphan cleanup; child updates are
    limited to the exact parent IDs returned by that transition.
    """
    owner_id = coerce_download_client_id(download_client_id)
    target = DownloadIdentity(owner_id, protocol, download_id)
    candidates = [
        dict(row)
        for row in db.execute(
            """
            SELECT iq.id, iq.series_id, iq.download_id, iq.download_client_id,
                   iq.torrent_url, iq.status, iq.lease_owner,
                   EXISTS (
                       SELECT 1 FROM import_publications publication
                       WHERE publication.queue_id=iq.id
                         AND publication.state IN (
                             'staging','prepared','publishing','published',
                             'db_committed','cleaning'
                         )
                   ) AS has_publication
            FROM import_queue iq
            WHERE iq.download_id IS NOT NULL
              AND iq.download_id=? COLLATE NOCASE
            """,
            (download_id,),
        ).fetchall()
    ]
    matching: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_owner = coerce_download_client_id(
            candidate["download_client_id"]
        )
        if (
            candidate_owner == owner_id
            and download_identities_match(
                target,
                DownloadIdentity(
                    candidate_owner,
                    protocol,
                    str(candidate["download_id"] or ""),
                ),
            )
        ):
            matching.append(candidate)

    # Active work is a safety fence, not a mutation target. Treat unresolved
    # legacy protocol evidence conservatively while still keeping concrete
    # owners independent. The target protocol supplies qBit/SAB ID semantics.
    if any(
        (
            candidate["status"] in ("pending", "partial", "importing")
            or candidate["lease_owner"] is not None
            or bool(candidate["has_publication"])
        )
        and download_identities_match(
            target,
            DownloadIdentity(
                coerce_download_client_id(candidate["download_client_id"]),
                None,
                str(candidate["download_id"] or ""),
            ),
        )
        for candidate in candidates
    ):
        return False, []
    transitioned: list[int] = []
    for candidate in matching:
        if (
            candidate["status"] != "failed"
            or candidate["lease_owner"] is not None
            or coerce_download_client_id(candidate["download_client_id"]) != owner_id
        ):
            continue
        cur = db.execute(
            "UPDATE import_queue SET status='skipped'"
            " WHERE id=? AND status='failed' AND lease_owner IS NULL",
            (candidate["id"],),
        )
        if cur.rowcount == 1:
            transitioned.append(int(candidate["id"]))
    if transitioned:
        db.executemany(
            "UPDATE import_queue_files SET status='skipped' WHERE queue_id=?",
            ((queue_id,) for queue_id in transitioned),
        )
    return True, transitioned


def _sab_process_sync(
    sab_by_nzo: Mapping[str, Mapping[str, object]],
    all_sab_nzo_ids: set[str],
    sab_host: str,
    *,
    download_client_id: int | None = None,
    include_legacy_ownerless: bool = True,
) -> list[int]:
    """Queue completed SAB items without carrying writes into the next scan."""
    with get_db() as db:
        rows = [
            dict(row)
            for row in db.execute(
                "SELECT torrent_url, torrent_name, series_id, volume_num,"
                " download_id, download_client_id "
                "FROM seen WHERE client='sabnzbd' AND protocol='nzb'"
                " AND (download_client_id=?"
                "      OR (?=1 AND download_client_id IS NULL))",
                (download_client_id, int(include_legacy_ownerless)),
            ).fetchall()
        ]

    new_queue_ids: list[int] = []
    for row in rows:
        download_id = row["download_id"]
        if not download_id:
            continue
        slot = sab_by_nzo.get(download_id)
        if not slot:
            continue

        with get_db() as db:
            content_path = apply_remote_path_mapping(
                db,
                cast(str, slot.get("storage", "")),
                sab_host,
            )
        with get_db() as db:
            queue_id, needs_review = _queue_import(
                db,
                row["series_id"],
                download_id,
                row["torrent_name"] or "",
                row["torrent_url"] or "",
                row["volume_num"],
                content_path,
                download_client_id=coerce_download_client_id(
                    row["download_client_id"]
                ),
                protocol="nzb",
            )
        if queue_id and not needs_review:
            new_queue_ids.append(queue_id)

    with get_db() as db:
        sab_orphaned = [
            dict(row)
            for row in db.execute(
                "SELECT DISTINCT v.download_id, v.series_id,"
                " v.download_client_id,"
                " COALESCE(sv.torrent_name, v.torrent_name) as torrent_name "
                "FROM volumes v "
                "LEFT JOIN seen sv ON sv.series_id=v.series_id"
                " AND sv.download_client_id IS v.download_client_id"
                " AND sv.download_id=v.download_id "
                "WHERE v.status='grabbed' "
                "  AND v.client='sabnzbd' "
                "  AND v.download_id IS NOT NULL "
                "  AND (v.download_client_id=?"
                "       OR (?=1 AND v.download_client_id IS NULL))",
                (download_client_id, int(include_legacy_ownerless)),
            ).fetchall()
        ]

    for orphan in sab_orphaned:
        download_id = orphan["download_id"]
        if download_id in all_sab_nzo_ids:
            continue
        with get_db() as db:
            cleanup_allowed, _ = _reserve_orphan_cleanup(
                db,
                download_id,
                download_client_id=coerce_download_client_id(
                    orphan["download_client_id"]
                ),
                protocol="nzb",
            )
            if not cleanup_allowed:
                continue
            orphan_vol_ids = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM volumes WHERE series_id=? AND download_id=?"
                    " AND download_client_id IS ?"
                    " AND status='grabbed' AND volume_num IS NOT NULL",
                    (
                        orphan["series_id"],
                        download_id,
                        orphan["download_client_id"],
                    ),
                ).fetchall()
            ]
            db.execute(
                "DELETE FROM volumes WHERE series_id=? AND download_id=?"
                " AND download_client_id IS ?"
                " AND status='grabbed' AND volume_num IS NULL",
                (
                    orphan["series_id"],
                    download_id,
                    orphan["download_client_id"],
                ),
            )
            db.execute(
                "UPDATE volumes SET status='wanted', download_id=NULL,"
                " torrent_name=NULL, indexer=NULL, protocol=NULL, client=NULL,"
                " download_client_id=NULL, grabbed_at=NULL, source_url=NULL,"
                " release_group=NULL "
                "WHERE series_id=? AND download_id=? AND download_client_id IS ?"
                " AND status='grabbed'",
                (
                    orphan["series_id"],
                    download_id,
                    orphan["download_client_id"],
                ),
            )
            if orphan_vol_ids:
                from volumes import _cascade_chapters

                _cascade_chapters(
                    db,
                    orphan["series_id"],
                    orphan_vol_ids,
                    "wanted",
                    grabbed_at=None,
                    torrent_name=None,
                    torrent_url=None,
                    indexer=None,
                    protocol=None,
                    client=None,
                    download_id=None,
                    download_client_id=None,
                    release_group=None,
                )
            db.execute(
                "DELETE FROM seen WHERE series_id=? AND download_id=?"
                " AND download_client_id IS ?",
                (
                    orphan["series_id"],
                    download_id,
                    orphan["download_client_id"],
                ),
            )
            log_event(
                "warning",
                f"SAB grab lost (removed from client): {orphan['torrent_name']}",
                orphan["series_id"],
                db=db,
            )
            series_row = db.execute(
                "SELECT title FROM series WHERE id=?",
                (orphan["series_id"],),
            ).fetchone()
            add_history(
                db,
                "grab_failed",
                orphan["series_id"],
                series_row["title"] if series_row else "",
                "",
                source_title=orphan["torrent_name"] or "",
                protocol="nzb",
                download_id=download_id,
                download_client_id=coerce_download_client_id(
                    orphan["download_client_id"]
                ),
                data={
                    "reason": "removed_from_client",
                    "download_client_id": orphan["download_client_id"],
                },
            )

    return new_queue_ids


async def _poll_qbit_partition(
    partition: _ClientPollPartition,
    client_config: dict[str, Any],
) -> None:
    """Poll one exact qBittorrent owner and process only its persisted rows."""
    host = str(client_config.get("host") or "").rstrip("/")
    user = str(client_config.get("username") or "")
    password = str(client_config.get("password") or "")
    category = client_config.get("category") or get_cfg("category")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{host}/api/v2/auth/login",
                data={"username": user, "password": password},
            )
            if "Ok" not in response.text:
                raise RuntimeError("authentication rejected")
            torrents_response = await client.get(
                f"{host}/api/v2/torrents/info",
                params={"category": category},
            )
            if torrents_response.status_code != 200:
                raise RuntimeError(
                    f"torrent listing returned HTTP {torrents_response.status_code}"
                )
            all_torrents = torrents_response.json()

        all_hashes = {
            str(torrent.get("hash") or "").lower()
            for torrent in all_torrents
            if torrent.get("hash")
        }
        completed = [
            torrent
            for torrent in all_torrents
            if torrent.get("progress", 0) >= 1.0
        ]
        torrent_by_hash = {
            str(torrent["hash"]).lower(): torrent
            for torrent in completed
            if torrent.get("hash")
        }
        completed_names = {
            normalize(str(torrent.get("name") or "")): torrent
            for torrent in completed
            if torrent.get("name")
        }

        def _process_completed() -> list[int]:
            with get_db() as db:
                rows = [
                    dict(row)
                    for row in db.execute(
                        "SELECT torrent_url, torrent_name, series_id, volume_num,"
                        " download_id, download_client_id "
                        "FROM seen WHERE client='qbittorrent'"
                        " AND protocol='torrent'"
                        " AND (download_client_id=?"
                        "      OR (?=1 AND download_client_id IS NULL))",
                        (
                            partition.client_id,
                            int(partition.include_legacy_ownerless),
                        ),
                    ).fetchall()
                ]

            new_imports: list[int] = []
            for row, torrent, matched_download_id in _deduplicate_qbit_matches(
                rows,
                torrent_by_hash,
                completed_names,
            ):
                content_path = torrent.get("content_path") or torrent.get(
                    "save_path", ""
                )
                with get_db() as db:
                    mapped_path = apply_remote_path_mapping(
                        db,
                        str(content_path or ""),
                        host,
                    )
                    queue_id, needs_review = _queue_import(
                        db,
                        int(row["series_id"]),
                        matched_download_id,
                        str(row["torrent_name"] or ""),
                        str(row["torrent_url"] or ""),
                        cast(float | None, row["volume_num"]),
                        mapped_path,
                        download_client_id=coerce_download_client_id(
                            row["download_client_id"]
                        ),
                        protocol="torrent",
                    )
                if queue_id and not needs_review:
                    new_imports.append(queue_id)
            return new_imports

        for queue_id in await asyncio.to_thread(_process_completed):
            schedule_import_worker(queue_id)

        def _orphan_cleanup() -> None:
            with get_db() as db:
                db.execute(
                    "UPDATE volumes SET status='wanted', grabbed_at=NULL,"
                    " source_url=NULL, download_id=NULL, torrent_name=NULL,"
                    " indexer=NULL, protocol=NULL, client=NULL,"
                    " download_client_id=NULL, release_group=NULL"
                    " WHERE status='grabbed' AND download_id IS NULL"
                    " AND volume_num IS NOT NULL AND client='qbittorrent'"
                    " AND (download_client_id=?"
                    "      OR (?=1 AND download_client_id IS NULL))",
                    (
                        partition.client_id,
                        int(partition.include_legacy_ownerless),
                    ),
                )
                db.execute(
                    "DELETE FROM volumes WHERE status='grabbed'"
                    " AND download_id IS NULL AND volume_num IS NULL"
                    " AND client='qbittorrent'"
                    " AND (download_client_id=?"
                    "      OR (?=1 AND download_client_id IS NULL))",
                    (
                        partition.client_id,
                        int(partition.include_legacy_ownerless),
                    ),
                )
                orphaned = [
                    dict(row)
                    for row in db.execute(
                        "SELECT DISTINCT v.download_id, v.series_id,"
                        " v.download_client_id,"
                        " COALESCE(sv.torrent_name, v.torrent_name)"
                        " AS torrent_name "
                        "FROM volumes v "
                        "LEFT JOIN seen sv ON sv.series_id=v.series_id"
                        " AND sv.download_client_id IS v.download_client_id"
                        " AND sv.download_id=v.download_id COLLATE NOCASE "
                        "WHERE v.status='grabbed'"
                        " AND v.client='qbittorrent'"
                        " AND v.download_id IS NOT NULL"
                        " AND (v.download_client_id=?"
                        "      OR (?=1 AND v.download_client_id IS NULL))",
                        (
                            partition.client_id,
                            int(partition.include_legacy_ownerless),
                        ),
                    ).fetchall()
                ]

            for orphan in orphaned:
                download_id = str(orphan["download_id"] or "")
                if download_id.lower() in all_hashes:
                    continue
                owner_id = coerce_download_client_id(
                    orphan["download_client_id"]
                )
                with get_db() as db:
                    cleanup_allowed, _ = _reserve_orphan_cleanup(
                        db,
                        download_id,
                        download_client_id=owner_id,
                        protocol="torrent",
                    )
                    if not cleanup_allowed:
                        continue
                    orphan_vol_ids = [
                        int(row["id"])
                        for row in db.execute(
                            "SELECT id FROM volumes WHERE series_id=?"
                            " AND download_client_id IS ?"
                            " AND download_id IS NOT NULL"
                            " AND download_id=? COLLATE NOCASE"
                            " AND status='grabbed'"
                            " AND volume_num IS NOT NULL",
                            (
                                orphan["series_id"],
                                owner_id,
                                download_id,
                            ),
                        ).fetchall()
                    ]
                    db.execute(
                        "DELETE FROM volumes WHERE series_id=?"
                        " AND download_client_id IS ?"
                        " AND download_id IS NOT NULL"
                        " AND download_id=? COLLATE NOCASE"
                        " AND status='grabbed' AND volume_num IS NULL",
                        (orphan["series_id"], owner_id, download_id),
                    )
                    db.execute(
                        "UPDATE volumes SET status='wanted', download_id=NULL,"
                        " torrent_name=NULL, indexer=NULL, protocol=NULL,"
                        " client=NULL, download_client_id=NULL, grabbed_at=NULL,"
                        " source_url=NULL, release_group=NULL"
                        " WHERE series_id=? AND download_client_id IS ?"
                        " AND download_id IS NOT NULL"
                        " AND download_id=? COLLATE NOCASE"
                        " AND status='grabbed'",
                        (orphan["series_id"], owner_id, download_id),
                    )
                    if orphan_vol_ids:
                        from volumes import _cascade_chapters

                        _cascade_chapters(
                            db,
                            int(orphan["series_id"]),
                            orphan_vol_ids,
                            "wanted",
                            grabbed_at=None,
                            torrent_name=None,
                            torrent_url=None,
                            indexer=None,
                            protocol=None,
                            client=None,
                            download_id=None,
                            download_client_id=None,
                            release_group=None,
                        )
                    db.execute(
                        "DELETE FROM seen WHERE series_id=?"
                        " AND download_client_id IS ?"
                        " AND download_id IS NOT NULL"
                        " AND download_id=? COLLATE NOCASE",
                        (orphan["series_id"], owner_id, download_id),
                    )
                    log_event(
                        "warning",
                        f"Grab lost (removed from client): "
                        f"{orphan['torrent_name']}",
                        int(orphan["series_id"]),
                        db=db,
                    )
                    series_row = db.execute(
                        "SELECT title FROM series WHERE id=?",
                        (orphan["series_id"],),
                    ).fetchone()
                    add_history(
                        db,
                        "grab_failed",
                        int(orphan["series_id"]),
                        series_row["title"] if series_row else "",
                        "",
                        source_title=str(orphan["torrent_name"] or ""),
                        protocol="torrent",
                        download_id=download_id,
                        download_client_id=owner_id,
                        data={
                            "reason": "removed_from_client",
                            "download_client_id": owner_id,
                        },
                    )

        await asyncio.to_thread(_orphan_cleanup)

        if get_cfg("failed_download_handling", "0") != "1":
            return
        all_torrent_by_hash = {
            str(torrent["hash"]).lower(): torrent
            for torrent in all_torrents
            if torrent.get("hash")
        }
        error_states = {"error", "missingFiles", "stalledDL"}
        with get_db() as db:
            seen_rows = [
                dict(row)
                for row in db.execute(
                    "SELECT download_id, download_client_id, series_id,"
                    " torrent_name, torrent_url"
                    " FROM seen WHERE client='qbittorrent'"
                    " AND protocol='torrent'"
                    " AND (download_client_id=?"
                    "      OR (?=1 AND download_client_id IS NULL))",
                    (
                        partition.client_id,
                        int(partition.include_legacy_ownerless),
                    ),
                ).fetchall()
            ]
        for row in seen_rows:
            failed_hash = str(row["download_id"] or "").lower()
            if not failed_hash:
                continue
            torrent = all_torrent_by_hash.get(failed_hash)
            if torrent is None or torrent.get("state", "") not in error_states:
                continue
            failed_state = str(torrent.get("state") or "error")

            def _mark_failed() -> bool:
                owner_id = coerce_download_client_id(
                    row["download_client_id"]
                )
                with get_db() as db:
                    cleanup_allowed, _ = _reserve_orphan_cleanup(
                        db,
                        failed_hash,
                        download_client_id=owner_id,
                        protocol="torrent",
                    )
                    if not cleanup_allowed:
                        return False
                    db.execute(
                        "INSERT OR IGNORE INTO blocklist("
                        "series_id, torrent_url, torrent_name, reason"
                        ") VALUES(?,?,?,?)",
                        (
                            row["series_id"],
                            row["torrent_url"] or "",
                            row["torrent_name"] or "",
                            f"Download failed: {failed_state}",
                        ),
                    )
                    db.execute(
                        "DELETE FROM volumes WHERE series_id=?"
                        " AND download_client_id IS ?"
                        " AND download_id IS NOT NULL"
                        " AND download_id=? COLLATE NOCASE"
                        " AND status='grabbed' AND volume_num IS NULL",
                        (row["series_id"], owner_id, failed_hash),
                    )
                    db.execute(
                        "UPDATE volumes SET status='wanted', download_id=NULL,"
                        " download_client_id=NULL, grabbed_at=NULL,"
                        " source_url=NULL, torrent_name=NULL"
                        " WHERE series_id=? AND download_client_id IS ?"
                        " AND download_id IS NOT NULL"
                        " AND download_id=? COLLATE NOCASE"
                        " AND status='grabbed' AND volume_num IS NOT NULL",
                        (row["series_id"], owner_id, failed_hash),
                    )
                    db.execute(
                        "DELETE FROM seen WHERE series_id=?"
                        " AND download_client_id IS ?"
                        " AND download_id IS NOT NULL"
                        " AND download_id=? COLLATE NOCASE",
                        (row["series_id"], owner_id, failed_hash),
                    )
                return True

            if not await asyncio.to_thread(_mark_failed):
                continue
            if client_config.get("remove_failed"):
                from clients import qbit_remove

                await qbit_remove(
                    failed_hash,
                    delete_files=True,
                    client=client_config,
                )
            log_event(
                "grab_failed",
                f"Auto-blacklisted failed download: {row['torrent_name']}",
                int(row["series_id"]),
            )
            if get_cfg("redownload_failed_interactive", "0") == "1":
                continue
            from grab import grab_existing

            with get_db() as db:
                series_row = db.execute(
                    "SELECT title, search_pattern FROM series WHERE id=?",
                    (row["series_id"],),
                ).fetchone()
                series = dict(series_row) if series_row else None
            if series:
                asyncio.create_task(
                    grab_existing(
                        int(row["series_id"]),
                        str(series["title"]),
                        str(series["search_pattern"] or ""),
                    )
                )
    except Exception as exc:
        log_event(
            "error",
            f"qBit status check failed for owner id={partition.client_id}: {exc}",
        )


async def _poll_sab_partition(
    partition: _ClientPollPartition,
    client_config: dict[str, Any],
) -> None:
    """Poll one exact SAB owner and process only its persisted rows."""
    sab_host = str(client_config.get("host") or "").rstrip("/")
    sab_apikey = str(client_config.get("password") or "")
    if not sab_apikey:
        log_event(
            "configuration_error",
            f"SABnzbd owner id={partition.client_id} has no usable API key",
            dedup=True,
        )
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            history_response = await client.get(
                f"{sab_host}/api",
                params={
                    "mode": "history",
                    "limit": 100,
                    "apikey": sab_apikey,
                    "output": "json",
                },
            )
            queue_response = await client.get(
                f"{sab_host}/api",
                params={
                    "mode": "queue",
                    "limit": 100,
                    "apikey": sab_apikey,
                    "output": "json",
                },
            )

        history_slots = (
            history_response.json().get("history", {}).get("slots", [])
            if history_response.status_code == 200
            else []
        )
        queue_slots = (
            queue_response.json().get("queue", {}).get("slots", [])
            if queue_response.status_code == 200
            else []
        )
        all_nzo_ids = {
            str(slot["nzo_id"])
            for slot in (*history_slots, *queue_slots)
            if slot.get("nzo_id")
        }
        completed_by_nzo = {
            str(slot["nzo_id"]): slot
            for slot in history_slots
            if slot.get("status") == "Completed" and slot.get("nzo_id")
        }
        queue_ids = await asyncio.to_thread(
            _sab_process_sync,
            completed_by_nzo,
            all_nzo_ids,
            sab_host,
            download_client_id=partition.client_id,
            include_legacy_ownerless=partition.include_legacy_ownerless,
        )
        for queue_id in queue_ids:
            schedule_import_worker(queue_id)
    except Exception as exc:
        log_event(
            "error",
            f"SABnzbd status check failed for owner id={partition.client_id}: "
            f"{exc}",
        )


async def check_download_status():
    """Poll download clients for completed downloads and queue them for import review.

    Skips if another invocation is still running (single-flight). Callers
    that need guaranteed execution should await a completed call instead
    of firing-and-forgetting via asyncio.create_task.
    """
    from shared import timed_block as _tb

    if _CHECK_DOWNLOAD_STATUS_LOCK.locked():
        return
    async with _CHECK_DOWNLOAD_STATUS_LOCK:
        with _tb("check_download_status"):
            return await _check_download_status_impl()


async def _check_download_status_impl():
    """Inner body (wrapped for timing instrumentation)."""
    from routers import suwayomi_ as _swy_router

    # Clean up stale imported/failed entries older than 7 days
    with get_db() as _cdb:
        _cdb.execute(
            "DELETE FROM import_queue_files WHERE queue_id IN ("
            "  SELECT id FROM import_queue WHERE status IN ('imported','skipped')"
            "  AND created_at < datetime('now', '-7 days')"
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM import_publications publication"
            "    WHERE publication.queue_id=import_queue.id"
            "      AND publication.state IN ('staging','prepared','publishing',"
            "          'published','db_committed','cleaning')"
            "  ))"
        )
        _cdb.execute(
            "DELETE FROM import_queue WHERE status IN ('imported','skipped')"
            " AND created_at < datetime('now', '-7 days')"
            " AND NOT EXISTS ("
            "   SELECT 1 FROM import_publications publication"
            "   WHERE publication.queue_id=import_queue.id"
            "     AND publication.state IN ('staging','prepared','publishing',"
            "         'published','db_committed','cleaning')"
            " )"
        )

    # Auto-prune expired blocklist entries
    _bl_ttl = max(0, int(get_cfg("blocklist_ttl_days", "90") or "90"))
    if _bl_ttl > 0:
        with get_db() as _bldb:
            _bl_deleted = _bldb.execute(
                "DELETE FROM blocklist WHERE added_at < datetime('now', ? || ' days')",
                (f"-{_bl_ttl}",),
            ).rowcount
            if _bl_deleted > 0:
                log_event(
                    "info",
                    f"Auto-pruned {_bl_deleted} expired blocklist entr{'ies' if _bl_deleted != 1 else 'y'}",
                    db=_bldb,
                )

    # Auto-reset grabbed volumes that are stuck (no activity for >2 days)
    with get_db() as _stuckdb:
        _stuck_count = _stuckdb.execute(
            "UPDATE volumes SET status='wanted', grabbed_at=NULL, download_id=NULL,"
            " source_url=NULL, torrent_name=NULL, indexer=NULL, protocol=NULL,"
            " client=NULL, download_client_id=NULL, release_group=NULL,"
            " import_path=NULL"
            " WHERE status='grabbed'"
            "   AND grabbed_at < datetime('now', '-2 days')"
            "   AND NOT EXISTS ("
            "     SELECT 1 FROM import_queue iq"
            "     WHERE ("
            "       (lower(COALESCE(volumes.client,'')) IN ('qbittorrent','qbit')"
            "        AND iq.download_id=volumes.download_id COLLATE NOCASE)"
            "       OR (lower(COALESCE(volumes.client,'')) NOT IN ('qbittorrent','qbit')"
            "           AND iq.download_id=volumes.download_id)"
            "     )"
            "     AND (iq.download_client_id=volumes.download_client_id"
            "          OR iq.download_client_id IS NULL"
            "          OR volumes.download_client_id IS NULL)"
            "     AND (iq.status IN ('pending','partial','importing')"
            "          OR iq.lease_owner IS NOT NULL"
            "          OR EXISTS (SELECT 1 FROM import_publications publication"
            "            WHERE publication.queue_id=iq.id"
            "              AND publication.state IN ('staging','prepared','publishing',"
            "                  'published','db_committed','cleaning')))"
            "   )"
        ).rowcount
        if _stuck_count > 0:
            log_event(
                "info",
                f"Auto-reset {_stuck_count} stuck grabbed volume(s) back to wanted",
                db=_stuckdb,
            )

    # Auto-retry import_queue entries stuck in pending/partial > 2 hours
    with get_db() as _iq_db:
        stuck_pending = _iq_db.execute(
            "SELECT id FROM import_queue"
            " WHERE status IN ('pending','partial')"
            " AND created_at < datetime('now', '-2 hours')"
            " AND NOT EXISTS ("
            "   SELECT 1 FROM import_queue_files f"
            "   WHERE f.queue_id=import_queue.id AND f.status='needs_review'"
            " )"
            " AND NOT EXISTS ("
            "   SELECT 1 FROM import_publications publication"
            "   WHERE publication.queue_id=import_queue.id"
            "     AND publication.state IN ('staging','prepared','publishing',"
            "         'published','db_committed','cleaning')"
            " )"
        ).fetchall()
        stuck_ids = [r["id"] for r in stuck_pending]
    if stuck_ids:
        for _sid in stuck_ids:
            schedule_import_worker(_sid)

    # Poll every persisted owner with that exact client configuration.
    for partition in _download_client_poll_partitions("qbittorrent"):
        client_config = _load_poll_client(partition)
        if client_config is not None:
            await _poll_qbit_partition(partition, client_config)

    for partition in _download_client_poll_partitions("sabnzbd"):
        client_config = _load_poll_client(partition)
        if client_config is not None:
            await _poll_sab_partition(partition, client_config)

    # ── Suwayomi ─────────────────────────────────────────────────────────────
    try:
        await _swy_router.check_suwayomi_jobs()
    except Exception as e:
        log_event("error", f"Suwayomi status check failed: {e}")


async def _process_auto_import(queue_id: int):
    """Auto-import a queue item where all files mapped cleanly (no review needed).

    This is re-exported from import_execute but kept here for backwards compatibility
    with import_pipeline.
    """
    from import_execute import _process_auto_import as _pap

    return await _pap(queue_id)


def normalize(text: str) -> str:
    """Normalize text for comparison (lowercase, strip)."""
    return (text or "").lower().strip()


def _deduplicate_qbit_matches(rows, torrent_by_hash, completed_names):
    """Match seen rows once per exact owner, series, and canonical hash."""
    matched = []
    matched_keys = set()
    for row in rows:
        seen_download_id = (row["download_id"] or "").lower()
        name_norm = normalize(row["torrent_name"] or "")
        torrent = torrent_by_hash.get(seen_download_id) or completed_names.get(
            name_norm
        )
        if not torrent:
            continue
        download_id = str(torrent.get("hash") or seen_download_id).lower()
        identity = download_id or normalize(torrent.get("name") or "")
        row_keys = row.keys()
        download_client_id = (
            row["download_client_id"]
            if "download_client_id" in row_keys
            else None
        )
        key = (
            coerce_download_client_id(download_client_id),
            row["series_id"],
            identity,
        )
        if key in matched_keys:
            continue
        matched_keys.add(key)
        matched.append((row, torrent, download_id))
    return matched
