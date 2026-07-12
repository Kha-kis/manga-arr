"""Import download: mark volumes as downloaded + auto-import."""

import asyncio
import logging

from shared import get_db
from events import log_event
from notifications import notify_discord, make_complete_embed
from volumes import _cascade_chapters

log = logging.getLogger(__name__)


def _mark_downloaded(db, series_id, volume_num, torrent_url) -> bool:
    """Mark volume(s) as downloaded. Returns True if any rows updated."""
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
            s = db.execute(
                "SELECT title, cover_url FROM series WHERE id=?", (series_id,)
            ).fetchone()
            if s:
                asyncio.create_task(
                    notify_discord(
                        "",
                        embed=make_complete_embed(
                            s["title"], f"Vol {volume_num:g}", s["cover_url"] or ""
                        ),
                        event="on_download",
                    )
                )
            vol_row = db.execute(
                "SELECT id FROM volumes WHERE series_id=? AND volume_num=?",
                (series_id, volume_num),
            ).fetchone()
            if vol_row:
                _cascade_chapters(db, series_id, [vol_row["id"]], "downloaded")
            return True
    else:
        pack = db.execute(
            "SELECT * FROM volumes WHERE series_id=? AND source_url=? AND volume_num IS NULL",
            (series_id, torrent_url),
        ).fetchone()
        if not pack:
            return False

        pt = pack["pack_type"]
        seen_meta = db.execute(
            "SELECT torrent_name, indexer, protocol, client, release_group, size_bytes"
            " FROM seen WHERE (download_id=? AND download_id IS NOT NULL)"
            " OR torrent_url=? LIMIT 1",
            (pack["download_id"], torrent_url),
        ).fetchone()
        m = dict(seen_meta) if seen_meta else {}

        if pt == "complete":
            cur = db.execute(
                "UPDATE volumes SET status='downloaded', torrent_name=?, indexer=?, protocol=?,"
                " client=?, release_group=?, size_bytes=?"
                " WHERE series_id=? AND volume_num IS NOT NULL AND status != 'downloaded'",
                (
                    m.get("torrent_name"),
                    m.get("indexer"),
                    m.get("protocol"),
                    m.get("client"),
                    m.get("release_group"),
                    m.get("size_bytes"),
                    series_id,
                ),
            )
        elif pt == "volume" and pack["vol_range_start"] and pack["vol_range_end"]:
            cur = db.execute(
                "UPDATE volumes SET status='downloaded', torrent_name=?, indexer=?, protocol=?,"
                " client=?, release_group=?, size_bytes=?"
                " WHERE series_id=? AND volume_num IS NOT NULL AND status != 'downloaded'"
                " AND volume_num >= ? AND volume_num <= ?",
                (
                    m.get("torrent_name"),
                    m.get("indexer"),
                    m.get("protocol"),
                    m.get("client"),
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
                " client=?, release_group=?, size_bytes=?"
                " WHERE id=? AND status != 'downloaded'",
                (
                    m.get("torrent_name"),
                    m.get("indexer"),
                    m.get("protocol"),
                    m.get("client"),
                    m.get("release_group"),
                    m.get("size_bytes"),
                    pack["id"],
                ),
            )
        else:
            return False

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
            s = db.execute(
                "SELECT title, cover_url FROM series WHERE id=?", (series_id,)
            ).fetchone()
            if s:
                asyncio.create_task(
                    notify_discord(
                        "",
                        embed=make_complete_embed(
                            s["title"], label, s["cover_url"] or ""
                        ),
                        event="on_download",
                    )
                )
            if pt == "complete":
                _cascade_chapters(db, series_id, None, "downloaded")
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
            return True
    return False


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
        try:
            with get_db() as _db_err:
                _db_err.execute(
                    "UPDATE import_queue SET status='failed'"
                    " WHERE id=? AND status IN ('pending','partial','importing')",
                    (queue_id,),
                )
        except Exception as _db_e:
            log.error(
                "[AutoImport] failed to mark queue %s as failed: %s",
                queue_id,
                _db_e,
            )
