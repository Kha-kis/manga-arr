"""Import download: mark volumes as downloaded + post-commit notification intent."""

import asyncio
import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from download_identity import (
    DownloadProtocol,
    DownloadIdentity,
    coerce_download_client_id,
    download_identities_match,
    normalize_download_protocol,
    resolve_download_protocol,
)
from events import log_event
from notifications import notify_discord, make_complete_embed
from volumes import _cascade_chapters

log = logging.getLogger(__name__)


def _same_acquisition(
    row: sqlite3.Row | Mapping[str, Any],
    identity: DownloadIdentity,
    source_url: str,
    url_column: Literal["source_url", "torrent_url"],
) -> bool:
    """Require exact owner/URL evidence before accepting a legacy NULL protocol."""
    owner = coerce_download_client_id(row["download_client_id"])
    protocol = normalize_download_protocol(row["protocol"])
    if row["protocol"] and protocol is None:
        return False
    return (
        owner == identity.download_client_id
        and row[url_column] == source_url
        and download_identities_match(
            DownloadIdentity(owner, protocol, str(row["download_id"] or "")),
            identity,
        )
    )


@dataclass(frozen=True, slots=True)
class DownloadNotificationIntent:
    """External notification payload safe to dispatch after DB commit."""

    title: str
    label: str
    cover_url: str


async def dispatch_download_notification(intent: DownloadNotificationIntent) -> None:
    """Dispatch one download notification after its domain transaction commits."""
    await notify_discord(
        "",
        embed=make_complete_embed(intent.title, intent.label, intent.cover_url),
        event="on_download",
    )


def _notification_intent(
    db: sqlite3.Connection, series_id: int, label: str
) -> DownloadNotificationIntent | None:
    row = db.execute(
        "SELECT title, cover_url FROM series WHERE id=?",
        (series_id,),
    ).fetchone()
    if row is None:
        return None
    return DownloadNotificationIntent(
        title=str(row["title"] or ""),
        label=label,
        cover_url=str(row["cover_url"] or ""),
    )


def _mark_downloaded(
    db: sqlite3.Connection,
    series_id: int,
    volume_num: float | None,
    torrent_url: str,
    *,
    download_id: str | None = None,
    download_client_id: int | None = None,
    protocol: DownloadProtocol | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> DownloadNotificationIntent | None:
    """Mark claimed volume(s) downloaded in the caller's transaction.

    Return an external notification intent for dispatch after commit.
    """
    if volume_num is not None:
        cur = db.execute(
            "UPDATE volumes SET status='downloaded' WHERE series_id=? AND volume_num=? AND status='grabbed'",
            (series_id, volume_num),
        )
        if cur.rowcount > 0:
            log_event(
                "download_complete",
                f"Vol {volume_num:g} download complete",
                series_id,
                db=db,
            )
            vol_row = db.execute(
                "SELECT id FROM volumes WHERE series_id=? AND volume_num=?",
                (series_id, volume_num),
            ).fetchone()
            if vol_row:
                _cascade_chapters(db, series_id, [vol_row["id"]], "downloaded")
            return _notification_intent(db, series_id, f"Vol {volume_num:g}")
    else:
        owner_id = coerce_download_client_id(download_client_id)
        resolved_protocol = protocol or resolve_download_protocol(
            db,
            download_client_id=owner_id,
            series_id=series_id,
            download_id=str(download_id or ""),
            source_url=str(torrent_url or ""),
        )
        if download_id:
            pack = db.execute(
                "SELECT * FROM volumes"
                " WHERE series_id=? AND source_url=? AND volume_num IS NULL"
                " AND download_client_id IS ? AND download_id IS NOT NULL"
                " AND ("
                "   (?='torrent' AND lower(download_id)=lower(?))"
                "   OR (COALESCE(?,'')!='torrent' AND download_id=?)"
                " )"
                " ORDER BY id DESC LIMIT 1",
                (
                    series_id,
                    torrent_url,
                    owner_id,
                    resolved_protocol,
                    download_id,
                    resolved_protocol,
                    download_id,
                ),
            ).fetchone()
        else:
            pack = db.execute(
                "SELECT * FROM volumes"
                " WHERE series_id=? AND source_url=? AND volume_num IS NULL"
                " ORDER BY id DESC LIMIT 1",
                (series_id, torrent_url),
            ).fetchone()
        if not pack:
            return None
        pack_identity = DownloadIdentity(
            coerce_download_client_id(pack["download_client_id"]),
            resolved_protocol or normalize_download_protocol(pack["protocol"]),
            str(pack["download_id"] or ""),
        )
        if not _same_acquisition(pack, pack_identity, torrent_url, "source_url"):
            return None

        pt = pack["pack_type"]
        if metadata is not None:
            m = dict(metadata)
        else:
            seen_meta = db.execute(
                "SELECT torrent_name, indexer, protocol, client, release_group,"
                " size_bytes FROM seen"
                " WHERE series_id=? AND download_client_id IS ?"
                " AND ("
                "   (download_id=? AND download_id IS NOT NULL)"
                "   OR torrent_url=?"
                " )"
                " LIMIT 1",
                (
                    series_id,
                    owner_id,
                    pack["download_id"],
                    torrent_url,
                ),
            ).fetchone()
            m = dict(seen_meta) if seen_meta else {}

        if pt == "complete" or (
            pt == "volume" and pack["vol_range_start"] and pack["vol_range_end"]
        ):
            # Grab-time rows are the durable claims. Coverage and current
            # monitoring alone cannot distinguish this pack from another download.
            claim_sql = (
                "SELECT * FROM volumes WHERE series_id=? AND volume_num IS NOT NULL"
                " AND status='grabbed' AND source_url=?"
            )
            claim_args: list[str | int | float | None] = [series_id, torrent_url]
            if pt == "volume":
                claim_sql += " AND volume_num >= ? AND volume_num <= ?"
                claim_args.extend((pack["vol_range_start"], pack["vol_range_end"]))
            claimed_vol_ids: list[int] = [
                r["id"]
                for r in db.execute(claim_sql, claim_args)
                if _same_acquisition(r, pack_identity, torrent_url, "source_url")
            ]
            if not claimed_vol_ids:
                return None
            placeholders = ",".join("?" for _ in claimed_vol_ids)
            chapter_statuses: list[tuple[str, int]] = [
                (
                    "downloaded"
                    if r["status"] == "grabbed"
                    and _same_acquisition(r, pack_identity, torrent_url, "torrent_url")
                    else r["status"],
                    r["id"],
                )
                for r in db.execute(
                    "SELECT * FROM chapters WHERE series_id=?"
                    f" AND volume_id IN ({placeholders})",
                    (series_id, *claimed_vol_ids),
                )
            ]
            cur = db.execute(
                "UPDATE volumes SET status='downloaded', torrent_name=?, indexer=?, protocol=?,"
                " client=?, download_client_id=?, release_group=?, size_bytes=?"
                f" WHERE id IN ({placeholders})",
                (
                    m.get("torrent_name"),
                    m.get("indexer"),
                    m.get("protocol"),
                    m.get("client"),
                    pack["download_client_id"],
                    m.get("release_group"),
                    m.get("size_bytes"),
                    *claimed_vol_ids,
                ),
            )
            # The legacy volume-status trigger marks every child downloaded.
            # Restore unclaimed states atomically without changing manual cascades.
            db.executemany(
                "UPDATE chapters SET status=? WHERE id=?",
                chapter_statuses,
            )
        elif pt == "chapter":
            cur = db.execute(
                "UPDATE volumes SET status='downloaded', torrent_name=?, indexer=?, protocol=?,"
                " client=?, download_client_id=?, release_group=?, size_bytes=?"
                " WHERE id=? AND status != 'downloaded'",
                (
                    m.get("torrent_name"),
                    m.get("indexer"),
                    m.get("protocol"),
                    m.get("client"),
                    owner_id,
                    m.get("release_group"),
                    m.get("size_bytes"),
                    pack["id"],
                ),
            )
        else:
            return None

        if cur.rowcount > 0:
            label = (
                "Complete Series"
                if pt == "complete"
                else (
                    "Chapter Pack"
                    if pt == "chapter"
                    else f"Vol {int(pack['vol_range_start'])}–{int(pack['vol_range_end'])}"
                )
            )
            log_event(
                "download_complete",
                f"{label} pack download complete",
                series_id,
                db=db,
            )
            return _notification_intent(db, series_id, label)
    return None


async def _process_auto_import(queue_id: int):
    """Auto-import a queue item where all files mapped cleanly."""
    from import_execute import _guarded_execute_import

    try:
        await _guarded_execute_import(queue_id)
    except asyncio.CancelledError:
        log_event("info", f"Auto-import cancelled for queue {queue_id}")
        raise
    except Exception as e:
        import traceback

        log_event("error", f"Auto-import failed for queue {queue_id}: {e}")
        log.error("[AutoImport] %s\n%s", e, traceback.format_exc())
