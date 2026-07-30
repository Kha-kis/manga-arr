"""Import download: mark volumes as downloaded + post-commit notification intent."""

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from download_identity import (
    DownloadProtocol,
    coerce_download_client_id,
    resolve_download_protocol,
)
from events import log_event
from notifications import notify_discord, make_complete_embed
from volumes import _cascade_chapters

log = logging.getLogger(__name__)


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


def _notification_intent(db, series_id: int, label: str):
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
    db,
    series_id,
    volume_num,
    torrent_url,
    *,
    download_id: str | None = None,
    download_client_id: int | None = None,
    protocol: DownloadProtocol | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> DownloadNotificationIntent | None:
    """Mark volume(s) downloaded and return an external post-commit intent."""
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

        if pt == "complete":
            cur = db.execute(
                "UPDATE volumes SET status='downloaded', torrent_name=?, indexer=?, protocol=?,"
                " client=?, download_client_id=?, release_group=?, size_bytes=?"
                " WHERE series_id=? AND volume_num IS NOT NULL AND status != 'downloaded'",
                (
                    m.get("torrent_name"),
                    m.get("indexer"),
                    m.get("protocol"),
                    m.get("client"),
                    owner_id,
                    m.get("release_group"),
                    m.get("size_bytes"),
                    series_id,
                ),
            )
        elif pt == "volume" and pack["vol_range_start"] and pack["vol_range_end"]:
            cur = db.execute(
                "UPDATE volumes SET status='downloaded', torrent_name=?, indexer=?, protocol=?,"
                " client=?, download_client_id=?, release_group=?, size_bytes=?"
                " WHERE series_id=? AND volume_num IS NOT NULL AND status != 'downloaded'"
                " AND volume_num >= ? AND volume_num <= ?",
                (
                    m.get("torrent_name"),
                    m.get("indexer"),
                    m.get("protocol"),
                    m.get("client"),
                    owner_id,
                    m.get("release_group"),
                    m.get("size_bytes"),
                    series_id,
                    pack["vol_range_start"],
                    pack["vol_range_end"],
                ),
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
            if pt == "complete":
                _cascade_chapters(db, series_id, None, "downloaded")
                db.execute(
                    "UPDATE chapters SET download_client_id=? WHERE series_id=?",
                    (owner_id, series_id),
                )
            elif pt == "volume":
                rng_ids = [
                    r["id"]
                    for r in db.execute(
                        "SELECT id FROM volumes WHERE series_id=? AND volume_num IS NOT NULL"
                        " AND volume_num >= ? AND volume_num <= ?",
                        (series_id, pack["vol_range_start"], pack["vol_range_end"]),
                    ).fetchall()
                ]
                _cascade_chapters(db, series_id, rng_ids, "downloaded")
                if rng_ids:
                    placeholders = ",".join("?" for _ in rng_ids)
                    db.execute(
                        "UPDATE chapters SET download_client_id=?"
                        f" WHERE series_id=? AND volume_id IN ({placeholders})",
                        (owner_id, series_id, *rng_ids),
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
