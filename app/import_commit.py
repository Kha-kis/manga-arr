"""Import commit: Phase 3 DB transaction replaying all writes."""

import logging
from datetime import datetime

from events import log_event, add_history
from files import quality_from_filename, build_volume_label
from volumes import _cascade_chapters, _check_volume_completion
from import_download import _mark_downloaded

log = logging.getLogger(__name__)


def _commit_import(
    db,
    plan,
    outcomes: list,
    fs_committed: bool,
    commit_failure_reason: str,
) -> tuple[bool, int, str]:
    """Phase 3: short DB transaction replaying all writes."""
    queue = plan.queue
    series_id = plan.series_id
    dst_dir = plan.dst_dir
    queue_id = queue["id"]

    outcomes_by_id = {o.file_id: o for o in outcomes}
    imported_count = 0
    imported_vols: set = set()
    chapter_vols_touched: set = set()

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
        new_status = "failed"
    elif has_needs_review:
        new_status = "partial"
    elif any_error:
        new_status = "partial"
    else:
        new_status = "imported"

    db.execute("UPDATE import_queue SET status=? WHERE id=?", (new_status, queue_id))
    if new_status == "failed" and queue["download_id"]:
        db.execute(
            "UPDATE volumes SET status='wanted', grabbed_at=NULL, download_id=NULL,"
            " source_url=NULL, torrent_name=NULL, indexer=NULL, protocol=NULL,"
            " client=NULL, release_group=NULL, import_path=NULL"
            " WHERE download_id=? AND status='grabbed'",
            (queue["download_id"],),
        )
    if new_status == "imported":
        if queue["download_id"]:
            db.execute(
                "DELETE FROM volumes"
                " WHERE series_id=? AND download_id=? AND volume_num IS NULL"
                " AND status='grabbed' AND COALESCE(pack_type,'')!='chapter'",
                (series_id, queue["download_id"]),
            )
        db.execute("DELETE FROM import_queue_files WHERE queue_id=?", (queue_id,))
        db.execute("DELETE FROM import_queue WHERE id=?", (queue_id,))

    s_info = db.execute("SELECT title FROM series WHERE id=?", (series_id,)).fetchone()
    s_title = s_info["title"] if s_info else ""
    vol_label = build_volume_label(queue["volume_num"], None, None)

    if imported_count > 0:
        _mark_downloaded(db, series_id, queue["volume_num"], queue["torrent_url"])
        db.execute(
            "UPDATE volumes SET import_path=? WHERE series_id=? AND download_id=?"
            " AND volume_num IS NULL",
            (dst_dir, series_id, queue["download_id"]),
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
            download_id=queue["download_id"] or "",
            data={"dst_dir": dst_dir, "count": imported_count},
        )
    else:
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
            download_id=queue["download_id"] or "",
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
    if fp.file_type == "chapter" and fp.proposed_chap is not None:
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

    _pv_meta = {}
    if vol_id is not None:
        _pv_row = db.execute(
            "SELECT indexer, protocol, client, release_group, size_bytes, torrent_name"
            " FROM volumes WHERE id=?",
            (vol_id,),
        ).fetchone()
        if _pv_row:
            _pv_meta = dict(_pv_row)
    _ch_quality = quality_from_filename(dst)
    _ch_torrent_name = _pv_meta.get("torrent_name") or queue["torrent_name"]
    imported_at = datetime.utcnow().isoformat()

    chap_row = db.execute(
        "SELECT id FROM chapters WHERE series_id=? AND chapter_num=?",
        (series_id, fp.proposed_chap),
    ).fetchone()
    if chap_row:
        db.execute(
            "UPDATE chapters SET status='downloaded', import_path=?, quality=COALESCE(quality,?),"
            " torrent_name=COALESCE(torrent_name,?), indexer=COALESCE(indexer,?),"
            " protocol=COALESCE(protocol,?), client=COALESCE(client,?),"
            " release_group=COALESCE(release_group,?), size_bytes=COALESCE(NULLIF(size_bytes,0),?),"
            " volume_id=COALESCE(volume_id,?), download_id=COALESCE(download_id,?),"
            " imported_at=COALESCE(imported_at,?),"
            " chapter_range_end=COALESCE(?, chapter_range_end)"
            " WHERE id=?",
            (
                dst,
                _ch_quality,
                _ch_torrent_name,
                _pv_meta.get("indexer"),
                _pv_meta.get("protocol"),
                _pv_meta.get("client"),
                _pv_meta.get("release_group"),
                _pv_meta.get("size_bytes"),
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
            " download_id, torrent_name, indexer, protocol, client, release_group, size_bytes,"
            " quality, imported_at, chapter_range_end)"
            " VALUES(?,?,?,'downloaded',?,?,?,?,?,?,?,?,?,?,?)",
            (
                series_id,
                vol_id,
                fp.proposed_chap,
                dst,
                queue["download_id"],
                _ch_torrent_name,
                _pv_meta.get("indexer"),
                _pv_meta.get("protocol"),
                _pv_meta.get("client"),
                _pv_meta.get("release_group"),
                _pv_meta.get("size_bytes"),
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
    db.execute(
        "UPDATE import_queue_files SET status='imported', dst_path=? WHERE id=?",
        (dst, fp.file_id),
    )

    if fp.proposed_vol is not None:
        imported_vols.add(fp.proposed_vol)
    elif fp.is_legacy_chapter_stub:
        _stub = db.execute(
            "SELECT id FROM volumes WHERE series_id=? AND download_id=?"
            " AND status='grabbed' AND pack_type='chapter'",
            (series_id, queue["download_id"]),
        ).fetchone()
        if _stub:
            db.execute(
                "UPDATE volumes SET status='downloaded', import_path=?,"
                " imported_at=COALESCE(imported_at,?) WHERE id=?",
                (dst, imported_at, _stub["id"]),
            )

    if fp.has_volume_range and fp.proposed_vol is None:
        seen_row = db.execute(
            "SELECT torrent_name, indexer, protocol, client, release_group, size_bytes"
            " FROM seen WHERE (download_id=? AND download_id IS NOT NULL)"
            " OR torrent_url=? LIMIT 1",
            (queue["download_id"], queue["torrent_url"]),
        ).fetchone()
        meta = dict(seen_row) if seen_row else {}
        file_quality = quality_from_filename(fp.filename)
        _rpt = (
            fp.pack_type
            if fp.pack_type in ("volume", "volume_range", "complete")
            else "volume"
        )
        db.execute(
            "INSERT INTO volumes(series_id, volume_num, status, source_url, torrent_name,"
            " import_path, download_id, indexer, protocol, client, release_group, size_bytes,"
            " quality, imported_at, vol_range_start, vol_range_end, pack_type, is_special)"
            " VALUES(?,NULL,'downloaded',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                series_id,
                queue["torrent_url"],
                meta.get("torrent_name"),
                dst,
                queue["download_id"],
                meta.get("indexer"),
                meta.get("protocol"),
                meta.get("client"),
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
        seen_row = db.execute(
            "SELECT torrent_name, indexer, protocol, client, release_group, size_bytes"
            " FROM seen WHERE (download_id=? AND download_id IS NOT NULL)"
            " OR torrent_url=? LIMIT 1",
            (queue["download_id"], queue["torrent_url"]),
        ).fetchone()
        meta = dict(seen_row) if seen_row else {}

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
                " download_id=COALESCE(download_id,?) WHERE id=?",
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
        else:
            db.execute(
                "INSERT INTO volumes(series_id, volume_num, status, source_url, torrent_name,"
                " import_path, download_id, indexer, protocol, client, release_group, size_bytes,"
                " quality, imported_at, pack_type, is_special)"
                " VALUES(?,?,'downloaded',?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
