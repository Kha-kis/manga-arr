"""Import commit: Phase 3 DB transaction replaying all writes."""

import logging
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

from download_identity import (
    DownloadProtocol,
    coerce_download_client_id,
    normalize_download_protocol,
    resolve_download_protocol,
)
from events import log_event, add_history
from files import quality_from_filename, build_volume_label
from volumes import _cascade_chapters, _check_volume_completion
from import_download import DownloadNotificationIntent, _mark_downloaded
from import_lease import (
    ImportQueueStatus,
    has_import_sibling_that_may_use_download,
    refresh_import_queue_lease,
    transition_import_queue_row,
)

if TYPE_CHECKING:
    from import_plan import _ImportPlan
    from import_staging import _StageOutcome

log = logging.getLogger(__name__)


def _queue_download_protocol(
    db: sqlite3.Connection,
    queue: Mapping[str, Any],
    series_id: int,
) -> DownloadProtocol | None:
    persisted_protocol = normalize_download_protocol(
        queue.get("download_protocol")
    )
    if persisted_protocol is not None:
        return persisted_protocol
    return resolve_download_protocol(
        db,
        download_client_id=coerce_download_client_id(
            queue.get("download_client_id")
        ),
        series_id=series_id,
        download_id=str(queue.get("download_id") or ""),
        source_url=str(queue.get("torrent_url") or ""),
        allow_client_configuration=False,
    )


def _queue_metadata(
    db: sqlite3.Connection,
    queue: Mapping[str, Any],
    series_id: int,
) -> dict[str, Any]:
    """Load metadata only from the queue acquisition's exact identity."""
    owner_id = coerce_download_client_id(queue.get("download_client_id"))
    download_id = str(queue.get("download_id") or "")
    torrent_url = str(queue.get("torrent_url") or "")
    protocol = _queue_download_protocol(db, queue, series_id)
    identity_params = (
        series_id,
        owner_id,
        torrent_url,
        torrent_url,
        protocol,
        download_id,
        protocol,
        download_id,
    )
    row = db.execute(
        """
        SELECT torrent_name, indexer, protocol, client, release_group, size_bytes
        FROM (
            SELECT torrent_name, indexer, protocol, client, release_group,
                   size_bytes, 1 AS source_rank
            FROM seen
            WHERE series_id=? AND download_client_id IS ?
              AND (
                  (? != '' AND torrent_url=?)
                  OR (
                      download_id IS NOT NULL
                      AND (
                          (?='torrent' AND lower(download_id)=lower(?))
                          OR (COALESCE(?,'')!='torrent' AND download_id=?)
                      )
                  )
              )
            UNION ALL
            SELECT torrent_name, indexer, protocol, client, release_group,
                   size_bytes, 2 AS source_rank
            FROM volumes
            WHERE series_id=? AND download_client_id IS ?
              AND (
                  (? != '' AND source_url=?)
                  OR (
                      download_id IS NOT NULL
                      AND (
                          (?='torrent' AND lower(download_id)=lower(?))
                          OR (COALESCE(?,'')!='torrent' AND download_id=?)
                      )
                  )
              )
            UNION ALL
            SELECT torrent_name, indexer, protocol, client, release_group,
                   size_bytes, 3 AS source_rank
            FROM chapters
            WHERE series_id=? AND download_client_id IS ?
              AND (
                  (? != '' AND torrent_url=?)
                  OR (
                      download_id IS NOT NULL
                      AND (
                          (?='torrent' AND lower(download_id)=lower(?))
                          OR (COALESCE(?,'')!='torrent' AND download_id=?)
                      )
                  )
              )
        )
        ORDER BY source_rank
        LIMIT 1
        """,
        (*identity_params, *identity_params, *identity_params),
    ).fetchone()
    metadata = dict(row) if row is not None else {}
    if metadata.get("protocol") is None:
        metadata["protocol"] = protocol
    return metadata


def _commit_import(
    db: sqlite3.Connection,
    plan: "_ImportPlan",
    outcomes: list["_StageOutcome"],
    fs_committed: bool,
    commit_failure_reason: str,
    *,
    lease_owner: str,
    lease_seconds: float,
    publication_id: int | None = None,
    post_commit_intents: list[DownloadNotificationIntent] | None = None,
) -> tuple[bool, int, str]:
    """Phase 3: short DB transaction replaying all writes."""
    queue = plan.queue
    series_id = plan.series_id
    dst_dir = plan.dst_dir
    queue_id = queue["id"]

    # This owner-CAS is deliberately the transaction's first mutation. Once a
    # journal is published it replaces the expiring queue lease as authority.
    if publication_id is not None:
        from import_publication import claim_publication_phase3

        if not claim_publication_phase3(db, publication_id, lease_owner):
            state = db.execute(
                "SELECT state FROM import_publications WHERE id=?",
                (publication_id,),
            ).fetchone()
            log.warning(
                "Import publication %s Phase 3 claim lost in state %s",
                publication_id,
                state["state"] if state is not None else "missing",
            )
            return False, 0, "journal_claim_lost"
    elif not refresh_import_queue_lease(
        db,
        queue_id,
        lease_owner,
        lease_seconds=lease_seconds,
    ):
        return False, 0, "lease_lost"

    outcomes_by_id = {o.file_id: o for o in outcomes}
    imported_count = 0
    imported_vols: set[float] = set()
    chapter_vols_touched: set[int] = set()

    has_pre_failed = any(fp.plan_status == "pre_failed" for fp in plan.files)
    has_stage_fail = any(
        fp.plan_status == "ready" and not outcomes_by_id[fp.file_id].ok
        for fp in plan.files
    )
    any_error = has_pre_failed or has_stage_fail
    would_be_imported = sum(
        1
        for fp in plan.files
        if fp.plan_status == "ready" and outcomes_by_id[fp.file_id].ok
    )

    if fs_committed:
        for fp in plan.files:
            if fp.plan_status in ("skip", "needs_review"):
                continue
            if fp.plan_status == "pre_failed":
                db.execute(
                    "UPDATE import_queue_files SET status='failed' WHERE id=?",
                    (fp.file_id,),
                )
                log_event(
                    "error", f"Import: {fp.plan_failure_reason}", series_id, db=db
                )
                any_error = True
                continue

            outcome = outcomes_by_id[fp.file_id]
            if not outcome.ok:
                err_label = (
                    f"Import chapter error ({fp.filename}): {outcome.error}"
                    if fp.file_type == "chapter"
                    else f"Import file error ({fp.filename}): {outcome.error}"
                )
                db.execute(
                    "UPDATE import_queue_files SET status='failed' WHERE id=?",
                    (fp.file_id,),
                )
                log_event("error", err_label, series_id, db=db)
                any_error = True
                continue

            dst = outcome.final_dst
            _process_import_file(
                db,
                fp,
                dst,
                plan,
                queue,
                series_id,
                imported_vols,
                chapter_vols_touched,
                outcomes_by_id,
                has_pre_failed,
                has_stage_fail,
            )
            imported_count += 1

        fs_committed = True
        commit_failure_reason = ""
    else:
        if would_be_imported > 0:
            if commit_failure_reason:
                any_error = True
                log_event(
                    "error",
                    f"Import commit failure: {commit_failure_reason}",
                    series_id,
                    db=db,
                )
            elif has_pre_failed or has_stage_fail:
                first_fail = None
                for fp in plan.files:
                    if fp.plan_status == "ready":
                        outcome = outcomes_by_id[fp.file_id]
                        if not outcome.ok:
                            err_label = (
                                f"Import chapter error ({fp.filename}): {outcome.error}"
                                if fp.file_type == "chapter"
                                else f"Import file error ({fp.filename}): {outcome.error}"
                            )
                            first_fail = (fp.file_id, err_label)
                            break
                if first_fail:
                    db.execute(
                        "UPDATE import_queue_files SET status='failed' WHERE id=?",
                        (first_fail[0],),
                    )
                    log_event(
                        "error",
                        f"Import rolled back: {first_fail[1]}",
                        series_id,
                        db=db,
                    )
        else:
            for fp in plan.files:
                if fp.plan_status == "pre_failed":
                    db.execute(
                        "UPDATE import_queue_files SET status='failed' WHERE id=?",
                        (fp.file_id,),
                    )
                    log_event(
                        "error", f"Import: {fp.plan_failure_reason}", series_id, db=db
                    )
                elif fp.plan_status == "ready":
                    outcome = outcomes_by_id[fp.file_id]
                    if not outcome.ok:
                        err_label = (
                            f"Import chapter error ({fp.filename}): {outcome.error}"
                            if fp.file_type == "chapter"
                            else f"Import file error ({fp.filename}): {outcome.error}"
                        )
                        db.execute(
                            "UPDATE import_queue_files SET status='failed' WHERE id=?",
                            (fp.file_id,),
                        )
                        log_event("error", err_label, series_id, db=db)
            imported_count = 0

    for vol_id in chapter_vols_touched:
        total_chaps = db.execute(
            "SELECT COUNT(*) FROM chapters WHERE volume_id=? AND monitored=1",
            (vol_id,),
        ).fetchone()[0]
        done_chaps = db.execute(
            "SELECT COUNT(*) FROM chapters WHERE volume_id=? AND monitored=1 AND status='downloaded'",
            (vol_id,),
        ).fetchone()[0]
        if total_chaps > 0 and done_chaps >= total_chaps:
            db.execute(
                "UPDATE volumes SET status='downloaded' WHERE id=? AND status!='downloaded'",
                (vol_id,),
            )

    has_needs_review = db.execute(
        "SELECT 1 FROM import_queue_files WHERE queue_id=? AND status='needs_review'",
        (queue_id,),
    ).fetchone()

    if imported_count == 0 and any_error:
        new_status: ImportQueueStatus = "failed"
    elif has_needs_review:
        new_status = "partial"
    elif any_error:
        new_status = "partial"
    else:
        new_status = "imported"

    if publication_id is not None:
        from import_publication import mark_publication_db_committed

        transitioned = db.execute(
            """
            UPDATE import_queue
            SET status=?, lease_owner=NULL, lease_expires_at=NULL,
                failed_at=CASE
                    WHEN ?='failed' THEN datetime('now')
                    ELSE failed_at
                END
            WHERE id=? AND status='importing'
            """,
            (new_status, new_status, queue_id),
        ).rowcount
        if transitioned != 1:
            raise RuntimeError("journal-authorized queue transition failed")
    elif not transition_import_queue_row(
        db,
        queue_id,
        lease_owner,
        new_status,
    ):
        raise RuntimeError("import lease lost during Phase 3 transaction")
    reset_is_shared = has_import_sibling_that_may_use_download(
        db,
        queue_id=queue_id,
        download_id=queue["download_id"],
        download_client_id=queue.get("download_client_id"),
        series_id=series_id,
        protocol=_queue_download_protocol(db, queue, series_id),
    )
    queue_owner_id = coerce_download_client_id(queue.get("download_client_id"))
    queue_protocol = _queue_download_protocol(db, queue, series_id)
    if new_status == "failed" and queue["download_id"] and not reset_is_shared:
        db.execute(
            "UPDATE volumes SET status='wanted', grabbed_at=NULL, download_id=NULL,"
            " source_url=NULL, torrent_name=NULL, indexer=NULL, protocol=NULL,"
            " client=NULL, download_client_id=NULL, release_group=NULL,"
            " import_path=NULL"
            " WHERE series_id=? AND download_client_id IS ?"
            " AND download_id IS NOT NULL"
            " AND ("
            "   (?='torrent' AND lower(download_id)=lower(?))"
            "   OR (COALESCE(?,'')!='torrent' AND download_id=?)"
            " )"
            " AND status='grabbed'",
            (
                series_id,
                queue_owner_id,
                queue_protocol,
                queue["download_id"],
                queue_protocol,
                queue["download_id"],
            ),
        )
    if new_status == "imported":
        if queue["download_id"]:
            db.execute(
                "DELETE FROM volumes"
                " WHERE series_id=? AND download_client_id IS ?"
                " AND download_id IS NOT NULL"
                " AND ("
                "   (?='torrent' AND lower(download_id)=lower(?))"
                "   OR (COALESCE(?,'')!='torrent' AND download_id=?)"
                " )"
                " AND volume_num IS NULL"
                " AND status='grabbed' AND COALESCE(pack_type,'')!='chapter'",
                (
                    series_id,
                    queue_owner_id,
                    queue_protocol,
                    queue["download_id"],
                    queue_protocol,
                    queue["download_id"],
                ),
            )
        if publication_id is None:
            db.execute("DELETE FROM import_queue_files WHERE queue_id=?", (queue_id,))
            db.execute("DELETE FROM import_queue WHERE id=?", (queue_id,))

    s_info = db.execute("SELECT title FROM series WHERE id=?", (series_id,)).fetchone()
    s_title = s_info["title"] if s_info else ""
    vol_label = build_volume_label(queue["volume_num"], None, None)

    notification_intent: DownloadNotificationIntent | None = None
    if imported_count > 0:
        queue_metadata = _queue_metadata(db, queue, series_id)
        notification_intent = _mark_downloaded(
            db,
            series_id,
            queue["volume_num"],
            queue["torrent_url"],
            download_id=queue["download_id"],
            download_client_id=queue_owner_id,
            protocol=queue_protocol,
            metadata=queue_metadata,
        )
        if notification_intent is not None and post_commit_intents is not None:
            post_commit_intents.append(notification_intent)
        db.execute(
            "UPDATE volumes SET import_path=?"
            " WHERE series_id=? AND download_client_id IS ?"
            " AND download_id IS NOT NULL"
            " AND ("
            "   (?='torrent' AND lower(download_id)=lower(?))"
            "   OR (COALESCE(?,'')!='torrent' AND download_id=?)"
            " )"
            " AND volume_num IS NULL AND COALESCE(is_special,0)=0",
            (
                dst_dir,
                series_id,
                queue_owner_id,
                queue_protocol,
                queue["download_id"],
                queue_protocol,
                queue["download_id"],
            ),
        )
        log_event(
            "import",
            f"Imported {imported_count} file(s): {queue['torrent_name']}",
            series_id,
            db=db,
        )
        add_history(
            db,
            "imported",
            series_id,
            s_title,
            vol_label,
            source_title=queue["torrent_name"] or "",
            protocol=queue_protocol or "",
            download_id=queue["download_id"] or "",
            download_client_id=queue_owner_id,
            torrent_url=queue["torrent_url"] or "",
            data={
                "dst_dir": dst_dir,
                "count": imported_count,
                "import_kinds": sorted(
                    {
                        fp.import_kind
                        for fp in plan.files
                        if fp.plan_status == "ready"
                    }
                ),
            },
        )
    elif new_status == "imported" and not any_error:
        skipped_count = sum(fp.plan_status == "skip" for fp in plan.files)
        log_event(
            "import",
            f"Skipped {skipped_count} file(s); import already satisfied: "
            f"{queue['torrent_name']}",
            series_id,
            db=db,
        )
        add_history(
            db,
            "import_skipped",
            series_id,
            s_title,
            vol_label,
            source_title=queue["torrent_name"] or "",
            protocol=queue_protocol or "",
            download_id=queue["download_id"] or "",
            download_client_id=queue_owner_id,
            torrent_url=queue["torrent_url"] or "",
            data={
                "count": 0,
                "skipped_count": skipped_count,
                "reason": "all_files_skipped",
            },
        )
    elif any_error:
        log_event(
            "error",
            f"Import failed: {queue['torrent_name']}",
            series_id,
            db=db,
        )
        add_history(
            db,
            "import_failed",
            series_id,
            s_title,
            vol_label,
            source_title=queue["torrent_name"] or "",
            protocol=queue_protocol or "",
            download_id=queue["download_id"] or "",
            download_client_id=queue_owner_id,
            torrent_url=queue["torrent_url"] or "",
        )

    if publication_id is not None:
        notification = (
            (
                notification_intent.title,
                notification_intent.label,
                notification_intent.cover_url,
            )
            if notification_intent is not None
            else None
        )
        mark_publication_db_committed(
            db,
            publication_id,
            lease_owner,
            result_ok=not any_error,
            imported_count=imported_count,
            queue_status=new_status,
            notification=notification,
        )

    return (not any_error, imported_count, new_status)


def _process_import_file(
    db,
    fp,
    dst,
    plan,
    queue,
    series_id,
    imported_vols,
    chapter_vols_touched,
    outcomes_by_id,
    has_pre_failed,
    has_stage_fail,
):
    """Process a single file during Phase 3 import."""
    if fp.import_kind == "special":
        _process_special_import(db, fp, dst, queue, series_id)
    elif fp.import_kind in ("chapter", "chapter_range") and fp.proposed_chap is not None:
        _process_chapter_import(
            db,
            fp,
            dst,
            plan,
            queue,
            series_id,
            imported_vols,
            chapter_vols_touched,
        )
    else:
        _process_volume_import(db, fp, dst, plan, queue, series_id, imported_vols)


def _process_special_import(db, fp, dst, queue, series_id):
    """Persist a standalone special without touching numbered library rows."""
    imported_at = datetime.utcnow().isoformat()
    meta = _queue_metadata(db, queue, series_id)
    owner_id = coerce_download_client_id(queue.get("download_client_id"))
    title = (fp.special_title or "Special").strip() or "Special"
    quality = quality_from_filename(fp.filename)

    existing = db.execute(
        "SELECT id FROM volumes WHERE series_id=? AND COALESCE(is_special,0)=1"
        " AND import_path=?",
        (series_id, dst),
    ).fetchone()
    if existing:
        db.execute(
            "UPDATE volumes SET title=?, status='downloaded', source_url=?,"
            " torrent_name=?, download_id=?, indexer=?, protocol=?, client=?,"
            " download_client_id=?, release_group=?, size_bytes=?, quality=?,"
            " imported_at=COALESCE(imported_at,?),"
            " pack_type='special', is_special=1, edition_type='special' WHERE id=?",
            (
                title,
                queue["torrent_url"],
                meta.get("torrent_name") or queue["torrent_name"],
                queue["download_id"],
                meta.get("indexer"),
                meta.get("protocol"),
                meta.get("client"),
                owner_id,
                meta.get("release_group"),
                meta.get("size_bytes"),
                quality,
                imported_at,
                existing["id"],
            ),
        )
    else:
        db.execute(
            "INSERT INTO volumes(series_id, volume_num, title, status, source_url,"
            " torrent_name, import_path, download_id, indexer, protocol, client,"
            " download_client_id, release_group, size_bytes, quality, imported_at,"
            " pack_type, is_special, edition_type)"
            " VALUES(?,NULL,?,'downloaded',?,?,?,?,?,?,?,?,?,?,?,?,?,1,'special')",
            (
                series_id,
                title,
                queue["torrent_url"],
                meta.get("torrent_name") or queue["torrent_name"],
                dst,
                queue["download_id"],
                meta.get("indexer"),
                meta.get("protocol"),
                meta.get("client"),
                owner_id,
                meta.get("release_group"),
                meta.get("size_bytes"),
                quality,
                imported_at,
                "special",
            ),
        )

    db.execute(
        "UPDATE import_queue_files SET status='imported', dst_path=? WHERE id=?",
        (dst, fp.file_id),
    )


def _process_chapter_import(
    db,
    fp,
    dst,
    plan,
    queue,
    series_id,
    imported_vols,
    chapter_vols_touched,
):
    """Process chapter import during Phase 3."""
    db.execute(
        "UPDATE import_queue_files SET status='imported', dst_path=? WHERE id=?",
        (dst, fp.file_id),
    )

    vol_id = None
    if fp.proposed_vol is not None:
        vol_row = db.execute(
            "SELECT id FROM volumes WHERE series_id=? AND volume_num=?",
            (series_id, fp.proposed_vol),
        ).fetchone()
        if vol_row:
            vol_id = vol_row["id"]
        else:
            vol_id = db.execute(
                "INSERT INTO volumes(series_id, volume_num, status)"
                " VALUES(?,?,'wanted')",
                (series_id, fp.proposed_vol),
            ).lastrowid

    metadata = _queue_metadata(db, queue, series_id)
    _ch_quality = quality_from_filename(dst)
    _ch_torrent_name = metadata.get("torrent_name") or queue["torrent_name"]
    download_client_id = coerce_download_client_id(
        queue.get("download_client_id")
    )
    imported_at = datetime.utcnow().isoformat()

    chap_row = db.execute(
        "SELECT id FROM chapters WHERE series_id=? AND chapter_num=?",
        (series_id, fp.proposed_chap),
    ).fetchone()
    if chap_row:
        db.execute(
            "UPDATE chapters SET status='downloaded', import_path=?, quality=COALESCE(quality,?),"
            " torrent_name=COALESCE(?,torrent_name), indexer=COALESCE(?,indexer),"
            " protocol=COALESCE(?,protocol), client=COALESCE(?,client),"
            " download_client_id=?,"
            " release_group=COALESCE(?,release_group),"
            " size_bytes=COALESCE(NULLIF(?,0),size_bytes),"
            " volume_id=COALESCE(volume_id,?),"
            " download_id=COALESCE(NULLIF(download_id,''),?),"
            " imported_at=COALESCE(imported_at,?),"
            " chapter_range_end=COALESCE(?, chapter_range_end)"
            " WHERE id=?",
            (
                dst,
                _ch_quality,
                _ch_torrent_name,
                metadata.get("indexer"),
                metadata.get("protocol"),
                metadata.get("client"),
                download_client_id,
                metadata.get("release_group"),
                metadata.get("size_bytes"),
                vol_id,
                queue["download_id"],
                imported_at,
                fp.chap_range_end,
                chap_row["id"],
            ),
        )
    else:
        db.execute(
            "INSERT INTO chapters(series_id, volume_id, chapter_num, status, import_path,"
            " download_id, torrent_name, indexer, protocol, client, download_client_id,"
            " release_group, size_bytes, quality, imported_at, chapter_range_end)"
            " VALUES(?,?,?,'downloaded',?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                series_id,
                vol_id,
                fp.proposed_chap,
                dst,
                queue["download_id"],
                _ch_torrent_name,
                metadata.get("indexer"),
                metadata.get("protocol"),
                metadata.get("client"),
                download_client_id,
                metadata.get("release_group"),
                metadata.get("size_bytes"),
                _ch_quality,
                imported_at,
                fp.chap_range_end,
            ),
        )

    if fp.proposed_vol is not None:
        imported_vols.add(fp.proposed_vol)
    if vol_id is not None:
        chapter_vols_touched.add(vol_id)


def _process_volume_import(db, fp, dst, plan, queue, series_id, imported_vols):
    """Process volume import during Phase 3."""
    imported_at = datetime.utcnow().isoformat()
    owner_id = coerce_download_client_id(queue.get("download_client_id"))
    queue_protocol = _queue_download_protocol(db, queue, series_id)
    db.execute(
        "UPDATE import_queue_files SET status='imported', dst_path=? WHERE id=?",
        (dst, fp.file_id),
    )

    if fp.proposed_vol is not None:
        imported_vols.add(fp.proposed_vol)
    elif fp.is_legacy_chapter_stub:
        _stub = db.execute(
            "SELECT id FROM volumes WHERE series_id=?"
            " AND download_id IS NOT NULL AND download_client_id IS ?"
            " AND ("
            "   (?='torrent' AND lower(download_id)=lower(?))"
            "   OR (COALESCE(?,'')!='torrent' AND download_id=?)"
            " )"
            " AND status='grabbed' AND pack_type='chapter'",
            (
                series_id,
                owner_id,
                queue_protocol,
                queue["download_id"],
                queue_protocol,
                queue["download_id"],
            ),
        ).fetchone()
        if _stub:
            db.execute(
                "UPDATE volumes SET status='downloaded', import_path=?,"
                " imported_at=COALESCE(imported_at,?), download_client_id=?"
                " WHERE id=?",
                (dst, imported_at, owner_id, _stub["id"]),
            )

    if fp.has_volume_range and fp.proposed_vol is None:
        meta = _queue_metadata(db, queue, series_id)
        file_quality = quality_from_filename(fp.filename)
        _rpt = (
            fp.pack_type
            if fp.pack_type in ("volume", "volume_range", "complete")
            else "volume"
        )
        db.execute(
            "INSERT INTO volumes(series_id, volume_num, status, source_url, torrent_name,"
            " import_path, download_id, indexer, protocol, client, download_client_id,"
            " release_group, size_bytes, quality, imported_at, vol_range_start,"
            " vol_range_end, pack_type, is_special)"
            " VALUES(?,NULL,'downloaded',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                series_id,
                queue["torrent_url"],
                meta.get("torrent_name"),
                dst,
                queue["download_id"],
                meta.get("indexer"),
                meta.get("protocol"),
                meta.get("client"),
                owner_id,
                meta.get("release_group"),
                meta.get("size_bytes"),
                file_quality,
                imported_at,
                fp.vol_range_start,
                fp.vol_range_end,
                _rpt,
                0,
            ),
        )
        for _v in range(int(fp.vol_range_start), int(fp.vol_range_end) + 1):
            imported_vols.add(float(_v))
        return

    if fp.proposed_vol is not None:
        meta = _queue_metadata(db, queue, series_id)

        vol_row = db.execute(
            "SELECT id FROM volumes WHERE series_id=? AND volume_num=?",
            (series_id, fp.proposed_vol),
        ).fetchone()
        file_quality = quality_from_filename(fp.filename)
        if vol_row:
            db.execute(
                "UPDATE volumes SET status='downloaded', import_path=?, torrent_name=?,"
                " indexer=?, protocol=?, client=?, release_group=?, size_bytes=?, quality=?,"
                " imported_at=COALESCE(imported_at,?),"
                " download_id=COALESCE(download_id,?),"
                " download_client_id=? WHERE id=?",
                (
                    dst,
                    meta.get("torrent_name"),
                    meta.get("indexer"),
                    meta.get("protocol"),
                    meta.get("client"),
                    meta.get("release_group"),
                    meta.get("size_bytes"),
                    file_quality,
                    imported_at,
                    queue["download_id"],
                    owner_id,
                    vol_row["id"],
                ),
            )
            _check_volume_completion(db, series_id, vol_row["id"])
            _cascade_chapters(
                db,
                series_id,
                [vol_row["id"]],
                "downloaded",
                import_path=dst,
                download_id=queue["download_id"],
                quality=file_quality,
                torrent_name=meta.get("torrent_name"),
                indexer=meta.get("indexer"),
                protocol=meta.get("protocol"),
                client=meta.get("client"),
                release_group=meta.get("release_group"),
                size_bytes=meta.get("size_bytes"),
                imported_at=imported_at,
            )
            db.execute(
                "UPDATE chapters SET download_client_id=?"
                " WHERE series_id=? AND volume_id=?",
                (
                    owner_id,
                    series_id,
                    vol_row["id"],
                ),
            )
        else:
            db.execute(
                "INSERT INTO volumes(series_id, volume_num, status, source_url, torrent_name,"
                " import_path, download_id, indexer, protocol, client, download_client_id,"
                " release_group, size_bytes, quality, imported_at, pack_type, is_special)"
                " VALUES(?,?,'downloaded',?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    series_id,
                    fp.proposed_vol,
                    queue["torrent_url"],
                    meta.get("torrent_name"),
                    dst,
                    queue["download_id"],
                    meta.get("indexer"),
                    meta.get("protocol"),
                    meta.get("client"),
                    owner_id,
                    meta.get("release_group"),
                    meta.get("size_bytes"),
                    file_quality,
                    imported_at,
                    fp.pack_type if fp.pack_type in ("volume", "complete") else None,
                    0,
                ),
            )
            vol_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            _cascade_chapters(
                db,
                series_id,
                [vol_id],
                "downloaded",
                import_path=dst,
                download_id=queue["download_id"],
                quality=file_quality,
                torrent_name=meta.get("torrent_name"),
                indexer=meta.get("indexer"),
                protocol=meta.get("protocol"),
                client=meta.get("client"),
                release_group=meta.get("release_group"),
                size_bytes=meta.get("size_bytes"),
                imported_at=imported_at,
            )
            db.execute(
                "UPDATE chapters SET download_client_id=?"
                " WHERE series_id=? AND volume_id=?",
                (
                    owner_id,
                    series_id,
                    vol_id,
                ),
            )
