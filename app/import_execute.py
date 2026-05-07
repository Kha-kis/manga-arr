"""Import execution: orchestrate three-phase pipeline (plan → stage → commit)."""
import asyncio
import os

from shared import get_cfg, get_db
from events import log_event, add_history, broadcast_queue_event
from import_staging import _ImportStaging, _stage_files, _StageOutcome
from parsing import extract_chapter_num
from files import quality_from_filename, build_filename, build_volume_label
from cover_images import extract_cbz_cover, download_cover
from notifications import trigger_komga_scan, make_complete_embed, notify_discord
from volumes import _cascade_chapters, _check_volume_completion
from clients import qbit_remove, sab_remove
from comicinfo import _try_inject_comicinfo, read_comic_info
from helpers import _resolve_series_dest_root
from files import sanitize_filename, safe_join_under
from parsing import (
    extract_volume_num, extract_volume_range, extract_chapter_range,
    detect_pack_type, is_special_release, normalize
)
from datetime import datetime


# Semaphore for bounding concurrent imports
_IMPORT_SEM: asyncio.Semaphore | None = None


def _get_import_sem() -> asyncio.Semaphore:
    """Lazily construct semaphore with current config value."""
    global _IMPORT_SEM
    if _IMPORT_SEM is None:
        limit = int(get_cfg('max_concurrent_imports', '2') or '2')
        _IMPORT_SEM = asyncio.Semaphore(limit)
    return _IMPORT_SEM


def initialize_import_semaphore() -> None:
    """Called from lifespan() to warm-start the semaphore."""
    _get_import_sem()


def claim_import_queue_row(db, queue_id: int,
                            allowed_statuses: tuple[str, ...] = ('pending', 'partial')
                            ) -> bool:
    """Atomically transition the queue row into 'importing' state.

    Returns True iff this caller won the race.
    """
    placeholders = ','.join('?' * len(allowed_statuses))
    cur = db.execute(
        f"UPDATE import_queue SET status='importing'"
        f" WHERE id=? AND status IN ({placeholders})",
        [queue_id, *allowed_statuses],
    )
    return cur.rowcount > 0


async def _guarded_execute_import(
    queue_id: int,
    volume_overrides: dict | None = None,
    skip_ids: set | None = None,
    chapter_overrides: dict | None = None,
) -> bool:
    """Claim the queue row, then run _execute_import under the semaphore."""
    with get_db() as _claim_db:
        if not claim_import_queue_row(_claim_db, queue_id):
            print(f"[Import] queue {queue_id}: claim lost")
            return False
    async with _get_import_sem():
        try:
            return await _execute_import(queue_id, volume_overrides, skip_ids, chapter_overrides)
        except asyncio.CancelledError:
            log_event('info', f"[Import] _guarded_execute_import cancelled for queue {queue_id}")
            with get_db() as _fail_db:
                _fail_db.execute(
                    "UPDATE import_queue SET status='failed', failed_at=datetime('now') WHERE id=?",
                    (queue_id,)
                )
                _fail_db.execute(
                    "UPDATE import_queue_files SET status='cancelled' WHERE queue_id=?",
                    (queue_id,)
                )
            raise


async def _execute_import(
    queue_id: int,
    volume_overrides: dict | None = None,
    skip_ids: set | None = None,
    chapter_overrides: dict | None = None,
) -> bool:
    """Wrapper around _execute_import_impl with auto-pack staging cleanup."""
    pack_cleanup_id: str | None = None
    with get_db() as _db_init:
        _qrow = _db_init.execute(
            "SELECT download_id FROM import_queue WHERE id=?", (queue_id,)
        ).fetchone()
        if _qrow and _qrow['download_id']:
            pack_cleanup_id = _qrow['download_id']
    try:
        try:
            return await _execute_import_impl(
                queue_id, volume_overrides, skip_ids, chapter_overrides
            )
        except asyncio.CancelledError:
            log_event('info', f"[Import] _execute_import cancelled for queue {queue_id}")
            raise
    finally:
        if pack_cleanup_id:
            from import_staging import _cleanup_pack_staging_dir
            _cleanup_pack_staging_dir(pack_cleanup_id)


async def _execute_import_impl(
    queue_id: int,
    volume_overrides: dict | None = None,
    skip_ids: set | None = None,
    chapter_overrides: dict | None = None,
) -> bool:
    """Three-phase pipeline: Plan → Stage → Commit."""
    if volume_overrides is None:
        volume_overrides = {}
    if chapter_overrides is None:
        chapter_overrides = {}
    if skip_ids is None:
        skip_ids = set()

    import_mode = get_cfg('import_mode', 'hardlink')

    # ── Phase 1 — short DB tx for planning ──────────────────────────────
    with get_db() as _db1:
        plan = _plan_import(
            _db1, queue_id,
            volume_overrides, chapter_overrides, skip_ids,
            import_mode,
        )
    if plan is None:
        return False

    queue = plan.queue

    # ── Phase 2 — filesystem (no DB held) ───────────────────────────────
    staging = _ImportStaging(plan.dst_dir, queue['id'], import_mode)
    outcomes = await _stage_files(plan, staging)
    outcomes_by_id = {o.file_id: o for o in outcomes}

    has_pre_failed = any(fp.plan_status == 'pre_failed' for fp in plan.files)
    has_stage_fail = any(
        fp.plan_status == 'ready' and not outcomes_by_id[fp.file_id].ok
        for fp in plan.files
    )
    would_be_imported = sum(
        1 for fp in plan.files
        if fp.plan_status == 'ready' and outcomes_by_id[fp.file_id].ok
    )

    fs_committed = False
    commit_failure_reason = ''
    if (has_pre_failed or has_stage_fail) and would_be_imported > 0:
        await asyncio.to_thread(staging.rollback)
    elif would_be_imported > 0:
        try:
            await asyncio.to_thread(staging.commit_all)
            fs_committed = True
        except Exception as e:
            await asyncio.to_thread(staging.rollback)
            commit_failure_reason = str(e)
    else:
        await asyncio.to_thread(staging.rollback)

    # ── Phase 3 — short DB tx for replay ────────────────────────────────
    with get_db() as _db3:
        ok, imported_count, new_status = _commit_import(
            _db3, plan, outcomes,
            fs_committed=fs_committed,
            commit_failure_reason=commit_failure_reason,
        )

    # ── Post-import work ────────────────────────────────────────────────
    if ok:
        with get_db() as _cdb:
            _series_id = queue['series_id']
            _cover_url = _cdb.execute(
                "SELECT cover_url FROM series WHERE id=?", (_series_id,)
            ).fetchone()
        _local_cover = f"/config/covers/{_series_id}.jpg"
        if not os.path.exists(_local_cover):
            with get_db() as _cdb2:
                _first_cbz = _cdb2.execute(
                    "SELECT dst_path FROM import_queue_files"
                    " WHERE queue_id=? AND status='imported' AND dst_path LIKE '%.cbz'",
                    (queue_id,)
                ).fetchone()
            if _first_cbz and _first_cbz['dst_path']:
                extract_cbz_cover(_series_id, _first_cbz['dst_path'])
            elif _cover_url and _cover_url['cover_url']:
                asyncio.create_task(download_cover(_series_id, _cover_url['cover_url']))
        await trigger_komga_scan()
        if get_cfg('remove_completed', 'false').lower() == 'true' and queue['download_id']:
            with get_db() as db2:
                proto = db2.execute(
                    "SELECT protocol FROM volumes WHERE download_id=? LIMIT 1",
                    (queue['download_id'],)
                ).fetchone()
            protocol = (proto['protocol'] if proto else '') or 'torrent'
            if protocol == 'torrent':
                await qbit_remove(queue['download_id'])
            else:
                await sab_remove(queue['download_id'])
    asyncio.create_task(broadcast_queue_event('import_complete', {'queue_id': queue_id}))
    return ok


def _plan_import(
    db,
    queue_id: int,
    volume_overrides: dict,
    chapter_overrides: dict,
    skip_ids: set,
    import_mode: str,
):
    """Phase 1: read queue/series/files and build _ImportPlan."""
    queue_row = db.execute(
        "SELECT * FROM import_queue WHERE id=?", (queue_id,)
    ).fetchone()
    if not queue_row or queue_row['status'] not in ('pending', 'partial', 'importing'):
        return None
    queue = dict(queue_row)

    files = db.execute(
        "SELECT * FROM import_queue_files WHERE queue_id=? AND status IN ('pending', 'needs_review')",
        (queue_id,)
    ).fetchall()

    if not files:
        if queue['status'] == 'importing':
            db.execute("UPDATE import_queue SET status='pending' WHERE id=?", (queue_id,))
        return None

    s_row = db.execute(
        "SELECT * FROM series WHERE id=?", (queue['series_id'],)
    ).fetchone()
    s = dict(s_row) if s_row else None
    series_tags = [r['tag'] for r in db.execute(
        "SELECT tag FROM series_tags WHERE series_id=?", (queue['series_id'],)
    ).fetchall()]
    rf = db.execute(
        "SELECT path FROM root_folders WHERE id=?", (s['root_folder_id'],)
    ).fetchone() if s and s['root_folder_id'] else None
    dest_root = _resolve_series_dest_root(
        db, s['root_folder_id'] if s else None, rf,
    )
    safe_dir = sanitize_filename(s['title'] or 'Unknown') if s else 'Unknown'
    dst_dir  = os.path.join(dest_root, safe_dir)

    try:
        os.makedirs(dst_dir, exist_ok=True)
    except Exception as e:
        log_event('error', f"Import: cannot create {dst_dir}: {e}", queue['series_id'], db=db)
        db.execute("UPDATE import_queue SET status='failed' WHERE id=?", (queue_id,))
        db.execute(
            "UPDATE volumes SET status='wanted', grabbed_at=NULL, download_id=NULL,"
            " source_url=NULL, torrent_name=NULL, indexer=NULL, protocol=NULL,"
            " client=NULL, release_group=NULL, import_path=NULL"
            " WHERE download_id=? AND status='grabbed'",
            (queue['download_id'],)
        )
        return None

    now_ts = None
    plans = []

    for f in files:
        if f['id'] in skip_ids:
            db.execute("UPDATE import_queue_files SET status='skipped' WHERE id=?", (f['id'],))
            plans.append(_FilePlan(
                file_id=f['id'], src_path=f['src_path'], filename=f['filename'],
                dst_path='', file_type='', proposed_vol=None, proposed_chap=None,
                chap_range_end=None, vol_range_start=None, vol_range_end=None,
                pack_type=None, is_special=0, has_volume_range=False,
                is_legacy_chapter_stub=False, is_legacy_chapter_recheck=False,
                plan_status='skip', plan_failure_reason='',
            ))
            continue

        new_vol  = volume_overrides.get(f['id'])
        new_chap = chapter_overrides.get(f['id'])
        if new_vol is not None:
            db.execute("UPDATE import_queue_files SET proposed_volume=? WHERE id=?", (new_vol, f['id']))
        if new_chap is not None:
            db.execute(
                "UPDATE import_queue_files SET proposed_chapter=?, file_type='chapter' WHERE id=?", 
                (new_chap, f['id'])
            )

        # Stage-2 explicit fields. Back-compat: keys() guard lets pre-migration rows still import.
        _keys = f.keys()
        proposed_vol  = new_vol  if new_vol  is not None else (f['proposed_volume'] if 'proposed_volume' in _keys else None)
        proposed_chap = new_chap if new_chap is not None else (f['proposed_chapter'] if 'proposed_chapter' in _keys else None)
        file_type = (
            'chapter' if new_chap is not None
            else (f['file_type'] if 'file_type' in _keys else 'volume')
        )
        # Stage-2 explicit fields. Back-compat: keys() guard lets pre-migration rows still import.
        _keys = f.keys()
        row_vol_rs    = f['proposed_volume_range_start'] if 'proposed_volume_range_start' in _keys else None
        row_vol_re    = f['proposed_volume_range_end']   if 'proposed_volume_range_end'   in _keys else None
        row_chap_re   = f['proposed_chapter_range_end']  if 'proposed_chapter_range_end'  in _keys else None
        row_pack_type = f['proposed_pack_type']          if 'proposed_pack_type'          in _keys else None
        row_is_special = int(f['proposed_is_special'] or 0) if 'proposed_is_special' in _keys and f['proposed_is_special'] else 0

        is_legacy_chapter_recheck = False
        if (file_type == 'volume' and proposed_vol is None and proposed_chap is None
                and f['id'] not in volume_overrides):
            recheck_chap = extract_chapter_num(os.path.basename(f['src_path']))
            if recheck_chap is not None:
                proposed_chap = recheck_chap
                file_type = 'chapter'
                is_legacy_chapter_recheck = True
                db.execute(
                    "UPDATE import_queue_files SET proposed_chapter=?, file_type='chapter' WHERE id=?",
                    (recheck_chap, f['id'])
                )

        has_vol_range = row_vol_rs is not None and row_vol_re is not None

        plan_status = 'ready'
        plan_failure_reason = ''
        is_legacy_chapter_stub = False

        if (file_type == 'volume' and proposed_vol is None and not has_vol_range
                and f['id'] not in volume_overrides):
            stub = None
            if queue['download_id']:
                stub = db.execute(
                    "SELECT id FROM volumes WHERE series_id=? AND download_id=?"
                    " AND status='grabbed' AND pack_type='chapter'",
                    (queue['series_id'], queue['download_id'])
                ).fetchone()
            if stub:
                is_legacy_chapter_stub = True
            else:
                db.execute("UPDATE import_queue_files SET status='needs_review' WHERE id=?", (f['id'],))
                plan_status = 'needs_review'

        dst_path = ''
        if plan_status == 'ready':
            try:
                dst_path = safe_join_under(dst_dir, f['filename'])
            except ValueError as _e:
                plan_status = 'pre_failed'
                plan_failure_reason = f"unsafe destination ({f['filename']}): {_e}"
            if plan_status == 'ready' and not os.path.isfile(f['src_path']):
                plan_status = 'pre_failed'
                plan_failure_reason = f"source file missing: {f['src_path']}"

        plans.append(_FilePlan(
            file_id=f['id'], src_path=f['src_path'], filename=f['filename'],
            dst_path=dst_path, file_type=file_type, proposed_vol=proposed_vol,
            proposed_chap=proposed_chap, chap_range_end=row_chap_re,
            vol_range_start=row_vol_rs, vol_range_end=row_vol_re,
            pack_type=row_pack_type, is_special=row_is_special,
            has_volume_range=has_vol_range, is_legacy_chapter_stub=is_legacy_chapter_stub,
            is_legacy_chapter_recheck=is_legacy_chapter_recheck,
            plan_status=plan_status, plan_failure_reason=plan_failure_reason,
        ))

    now_ts = None
    if plans:
        now_ts = None

    return _ImportPlan(
        queue=queue,
        series=s,
        series_tags=series_tags,
        dst_dir=dst_dir,
        import_mode=import_mode,
        now_ts=now_ts,
        files=plans,
        series_id=queue['series_id'],
    )


def _commit_import(
    db,
    plan,
    outcomes: list,
    fs_committed: bool,
    commit_failure_reason: str,
) -> tuple[bool, int, str]:
    """Phase 3: short DB transaction replaying all writes."""
    queue       = plan.queue
    series_id   = plan.series_id
    dst_dir     = plan.dst_dir
    queue_id    = queue['id']

    outcomes_by_id = {o.file_id: o for o in outcomes}
    imported_count = 0
    imported_vols: set = set()
    chapter_vols_touched: set = set()

    has_pre_failed = any(fp.plan_status == 'pre_failed' for fp in plan.files)
    has_stage_fail = any(
        fp.plan_status == 'ready' and not outcomes_by_id[fp.file_id].ok
        for fp in plan.files
    )
    any_error = has_pre_failed or has_stage_fail
    would_be_imported = sum(
        1 for fp in plan.files
        if fp.plan_status == 'ready' and outcomes_by_id[fp.file_id].ok
    )

    if fs_committed:
        for fp in plan.files:
            if fp.plan_status in ('skip', 'needs_review'):
                continue
            if fp.plan_status == 'pre_failed':
                db.execute("UPDATE import_queue_files SET status='failed' WHERE id=?", (fp.file_id,))
                log_event('error', f"Import: {fp.plan_failure_reason}", series_id, db=db)
                any_error = True
                continue

            outcome = outcomes_by_id[fp.file_id]
            if not outcome.ok:
                err_label = (
                    f"Import chapter error ({fp.filename}): {outcome.error}"
                    if fp.file_type == 'chapter'
                    else f"Import file error ({fp.filename}): {outcome.error}"
                )
                db.execute("UPDATE import_queue_files SET status='failed' WHERE id=?", (fp.file_id,))
                log_event('error', err_label, series_id, db=db)
                any_error = True
                continue

            dst = outcome.final_dst
            _process_import_file(db, fp, dst, plan, queue, series_id, imported_vols,
                                chapter_vols_touched, outcomes_by_id, has_pre_failed, has_stage_fail)
            imported_count += 1

        fs_committed = True
        commit_failure_reason = ''
    else:
        if would_be_imported > 0:
            if commit_failure_reason:
                any_error = True
                log_event('error', f"Import commit failure: {commit_failure_reason}", series_id, db=db)
            elif has_pre_failed or has_stage_fail:
                first_fail = None
                for fp in plan.files:
                    if fp.plan_status == 'ready':
                        outcome = outcomes_by_id[fp.file_id]
                        if not outcome.ok:
                            err_label = (
                                f"Import chapter error ({fp.filename}): {outcome.error}"
                                if fp.file_type == 'chapter'
                                else f"Import file error ({fp.filename}): {outcome.error}"
                            )
                            first_fail = (fp.file_id, err_label)
                            break
                if first_fail:
                    db.execute("UPDATE import_queue_files SET status='failed' WHERE id=?", (first_fail[0],))
                    log_event('error', f"Import rolled back: {first_fail[1]}", series_id, db=db)
        else:
            for fp in plan.files:
                if fp.plan_status == 'pre_failed':
                    db.execute("UPDATE import_queue_files SET status='failed' WHERE id=?", (fp.file_id,))
                    log_event('error', f"Import: {fp.plan_failure_reason}", series_id, db=db)
                elif fp.plan_status == 'ready':
                    outcome = outcomes_by_id[fp.file_id]
                    if not outcome.ok:
                        err_label = (
                            f"Import chapter error ({fp.filename}): {outcome.error}"
                            if fp.file_type == 'chapter'
                            else f"Import file error ({fp.filename}): {outcome.error}"
                        )
                        db.execute("UPDATE import_queue_files SET status='failed' WHERE id=?", (fp.file_id,))
                        log_event('error', err_label, series_id, db=db)
            imported_count = 0

    # Cascade chapter completion
    from volumes import _cascade_chapters
    for vol_id in chapter_vols_touched:
        total_chaps = db.execute(
            "SELECT COUNT(*) FROM chapters WHERE volume_id=? AND monitored=1", (vol_id,)
        ).fetchone()[0]
        done_chaps = db.execute(
            "SELECT COUNT(*) FROM chapters WHERE volume_id=? AND monitored=1 AND status='downloaded'",
            (vol_id,)
        ).fetchone()[0]
        if total_chaps > 0 and done_chaps >= total_chaps:
            db.execute(
                "UPDATE volumes SET status='downloaded' WHERE id=? AND status!='downloaded'",
                (vol_id,)
            )

    # Final queue status
    has_needs_review = db.execute(
        "SELECT 1 FROM import_queue_files WHERE queue_id=? AND status='needs_review'",
        (queue_id,)
    ).fetchone()

    if imported_count == 0 and any_error:
        new_status = 'failed'
    elif has_needs_review:
        new_status = 'partial'
    elif any_error:
        new_status = 'partial'
    else:
        new_status = 'imported'

    db.execute("UPDATE import_queue SET status=? WHERE id=?", (new_status, queue_id))
    if new_status == 'failed' and queue['download_id']:
        db.execute(
            "UPDATE volumes SET status='wanted', grabbed_at=NULL, download_id=NULL,"
            " source_url=NULL, torrent_name=NULL, indexer=NULL, protocol=NULL,"
            " client=NULL, release_group=NULL, import_path=NULL"
            " WHERE download_id=? AND status='grabbed'",
            (queue['download_id'],)
        )
    if new_status == 'imported':
        db.execute("DELETE FROM import_queue_files WHERE queue_id=?", (queue_id,))
        db.execute("DELETE FROM import_queue WHERE id=?", (queue_id,))

    s_info = db.execute("SELECT title FROM series WHERE id=?", (series_id,)).fetchone()
    s_title = s_info['title'] if s_info else ''
    vol_label = build_volume_label(queue['volume_num'], None, None)

    if imported_count > 0:
        _mark_downloaded(db, series_id, queue['volume_num'], queue['torrent_url'])
        db.execute(
            "UPDATE volumes SET import_path=? WHERE series_id=? AND download_id=?"
            " AND volume_num IS NULL",
            (dst_dir, series_id, queue['download_id'])
        )
        log_event('import', f"Imported {imported_count} file(s): {queue['torrent_name']}", series_id, db=db)
        add_history(db, 'imported', series_id, s_title, vol_label,
                    source_title=queue['torrent_name'] or '',
                    download_id=queue['download_id'] or '',
                    data={'dst_dir': dst_dir, 'count': imported_count})
    else:
        log_event('error', f"Import failed: {queue['torrent_name']}", series_id, db=db)
        add_history(db, 'import_failed', series_id, s_title, vol_label,
                    source_title=queue['torrent_name'] or '',
                    download_id=queue['download_id'] or '')

    return (not any_error, imported_count, new_status)


def _process_import_file(db, fp, dst, plan, queue, series_id, imported_vols,
                         chapter_vols_touched, outcomes_by_id, has_pre_failed, has_stage_fail):
    """Process a single file during Phase 3 import."""
    if fp.file_type == 'chapter' and fp.proposed_chap is not None:
        _process_chapter_import(db, fp, dst, plan, queue, series_id, imported_vols, chapter_vols_touched)
    else:
        _process_volume_import(db, fp, dst, plan, queue, series_id, imported_vols)


def _process_chapter_import(db, fp, dst, plan, queue, series_id, imported_vols, chapter_vols_touched):
    """Process chapter import during Phase 3."""
    db.execute("UPDATE import_queue_files SET status='imported', dst_path=? WHERE id=?", (dst, fp.file_id))

    vol_id = None
    if fp.proposed_vol is not None:
        vol_row = db.execute(
            "SELECT id FROM volumes WHERE series_id=? AND volume_num=?",
            (series_id, fp.proposed_vol)
        ).fetchone()
        if vol_row:
            vol_id = vol_row['id']
        else:
            vol_id = db.execute(
                "INSERT INTO volumes(series_id, volume_num, status)"
                " VALUES(?,?,'wanted')", (series_id, fp.proposed_vol)
            ).lastrowid

    _pv_meta = {}
    if vol_id is not None:
        _pv_row = db.execute(
            "SELECT indexer, protocol, client, release_group, size_bytes, torrent_name FROM volumes WHERE id=?",
            (vol_id,)
        ).fetchone()
        if _pv_row:
            _pv_meta = dict(_pv_row)
    _ch_quality = quality_from_filename(dst)
    _ch_torrent_name = _pv_meta.get('torrent_name') or queue['torrent_name']

    chap_row = db.execute(
        "SELECT id FROM chapters WHERE series_id=? AND chapter_num=?",
        (series_id, fp.proposed_chap)
    ).fetchone()
    if chap_row:
        db.execute(
            "UPDATE chapters SET status='downloaded', import_path=?, quality=COALESCE(quality,?),"
            " torrent_name=COALESCE(torrent_name,?), indexer=COALESCE(indexer,?),"
            " protocol=COALESCE(protocol,?), client=COALESCE(client,?),"
            " release_group=COALESCE(release_group,?), size_bytes=COALESCE(NULLIF(size_bytes,0),?),"
            " volume_id=COALESCE(volume_id,?), download_id=COALESCE(download_id,?),"
            " chapter_range_end=COALESCE(?, chapter_range_end)"
            " WHERE id=?",
            (dst, _ch_quality, _ch_torrent_name, _pv_meta.get('indexer'), _pv_meta.get('protocol'),
             _pv_meta.get('client'), _pv_meta.get('release_group'), _pv_meta.get('size_bytes'),
             vol_id, queue['download_id'], fp.chap_range_end, chap_row['id'])
        )
    else:
        db.execute(
            "INSERT INTO chapters(series_id, volume_id, chapter_num, status, import_path,"
            " download_id, torrent_name, indexer, protocol, client, release_group, size_bytes,"
            " quality, chapter_range_end)"
            " VALUES(?,?,?,'downloaded',?,?,?,?,?,?,?,?,?,?)",
            (series_id, vol_id, fp.proposed_chap, dst, queue['download_id'], _ch_torrent_name,
             _pv_meta.get('indexer'), _pv_meta.get('protocol'), _pv_meta.get('client'),
             _pv_meta.get('release_group'), _pv_meta.get('size_bytes'), _ch_quality, fp.chap_range_end)
        )

    if fp.proposed_vol is not None:
        imported_vols.add(fp.proposed_vol)
    if vol_id is not None:
        chapter_vols_touched.add(vol_id)


def _process_volume_import(db, fp, dst, plan, queue, series_id, imported_vols):
    """Process volume import during Phase 3."""
    db.execute("UPDATE import_queue_files SET status='imported', dst_path=? WHERE id=?", (dst, fp.file_id))
    
    if fp.proposed_vol is not None:
        imported_vols.add(fp.proposed_vol)
    elif fp.is_legacy_chapter_stub:
        _stub = db.execute(
            "SELECT id FROM volumes WHERE series_id=? AND download_id=?"
            " AND status='grabbed' AND pack_type='chapter'",
            (series_id, queue['download_id'])
        ).fetchone()
        if _stub:
            db.execute(
                "UPDATE volumes SET status='downloaded', import_path=? WHERE id=?",
                (dst, _stub['id'])
            )

    if fp.has_volume_range and fp.proposed_vol is None:
        seen_row = db.execute(
            "SELECT torrent_name, indexer, protocol, client, release_group, size_bytes"
            " FROM seen WHERE (download_id=? AND download_id IS NOT NULL)"
            " OR torrent_url=? LIMIT 1",
            (queue['download_id'], queue['torrent_url'])
        ).fetchone()
        meta = dict(seen_row) if seen_row else {}
        file_quality = quality_from_filename(fp.filename)
        _rpt = fp.pack_type if fp.pack_type in ('volume', 'volume_range', 'complete') else 'volume'
        db.execute(
            "INSERT INTO volumes(series_id, volume_num, status, source_url, torrent_name,"
            " import_path, download_id, indexer, protocol, client, release_group, size_bytes,"
            " quality, imported_at, vol_range_start, vol_range_end, pack_type, is_special)"
            " VALUES(?,NULL,'downloaded',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (series_id, queue['torrent_url'], meta.get('torrent_name'), dst, queue['download_id'],
             meta.get('indexer'), meta.get('protocol'), meta.get('client'), meta.get('release_group'),
             meta.get('size_bytes'), file_quality, datetime.utcnow().isoformat(),
             fp.vol_range_start, fp.vol_range_end, _rpt, 0)
        )
        for _v in range(int(fp.vol_range_start), int(fp.vol_range_end) + 1):
            imported_vols.add(float(_v))
        return

    if fp.proposed_vol is not None:
        seen_row = db.execute(
            "SELECT torrent_name, indexer, protocol, client, release_group, size_bytes"
            " FROM seen WHERE (download_id=? AND download_id IS NOT NULL)"
            " OR torrent_url=? LIMIT 1",
            (queue['download_id'], queue['torrent_url'])
        ).fetchone()
        meta = dict(seen_row) if seen_row else {}

        vol_row = db.execute(
            "SELECT id FROM volumes WHERE series_id=? AND volume_num=?",
            (series_id, fp.proposed_vol)
        ).fetchone()
        file_quality = quality_from_filename(fp.filename)
        if vol_row:
            db.execute(
                "UPDATE volumes SET status='downloaded', import_path=?, torrent_name=?,"
                " indexer=?, protocol=?, client=?, release_group=?, size_bytes=?, quality=?,"
                " download_id=COALESCE(download_id,?) WHERE id=?",
                (dst, meta.get('torrent_name'), meta.get('indexer'), meta.get('protocol'),
                 meta.get('client'), meta.get('release_group'), meta.get('size_bytes'),
                 file_quality, queue['download_id'], vol_row['id'])
            )
            _check_volume_completion(db, series_id, vol_row['id'])
            _cascade_chapters(db, series_id, [vol_row['id']], 'downloaded', import_path=dst,
                            download_id=queue['download_id'], quality=file_quality,
                            torrent_name=meta.get('torrent_name'), indexer=meta.get('indexer'),
                            protocol=meta.get('protocol'), client=meta.get('client'),
                            release_group=meta.get('release_group'), size_bytes=meta.get('size_bytes'))
        else:
            db.execute(
                "INSERT INTO volumes(series_id, volume_num, status, source_url, torrent_name,"
                " import_path, download_id, indexer, protocol, client, release_group, size_bytes,"
                " quality, imported_at, pack_type, is_special)"
                " VALUES(?,?,'downloaded',?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (series_id, fp.proposed_vol, queue['torrent_url'], meta.get('torrent_name'), dst,
                 queue['download_id'], meta.get('indexer'), meta.get('protocol'), meta.get('client'),
                 meta.get('release_group'), meta.get('size_bytes'), file_quality,
                 datetime.utcnow().isoformat(),
                 fp.pack_type if fp.pack_type in ('volume', 'complete') else None, 0)
            )
            vol_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            _cascade_chapters(db, series_id, [vol_id], 'downloaded', import_path=dst,
                            download_id=queue['download_id'], quality=file_quality,
                            torrent_name=meta.get('torrent_name'), indexer=meta.get('indexer'),
                            protocol=meta.get('protocol'), client=meta.get('client'),
                            release_group=meta.get('release_group'), size_bytes=meta.get('size_bytes'))


# ── Data classes for planning ─────────────────────────────────────────────────

class _FilePlan:
    """Frozen per-file decision data computed in Phase 1."""
    def __init__(self, file_id: int, src_path: str, filename: str, dst_path: str,
                 file_type: str, proposed_vol: float | None, proposed_chap: float | None,
                 chap_range_end: float | None, vol_range_start: float | None,
                 vol_range_end: float | None, pack_type: str | None, is_special: int,
                 has_volume_range: bool, is_legacy_chapter_stub: bool,
                 is_legacy_chapter_recheck: bool, plan_status: str, plan_failure_reason: str):
        self.file_id = file_id
        self.src_path = src_path
        self.filename = filename
        self.dst_path = dst_path
        self.file_type = file_type
        self.proposed_vol = proposed_vol
        self.proposed_chap = proposed_chap
        self.chap_range_end = chap_range_end
        self.vol_range_start = vol_range_start
        self.vol_range_end = vol_range_end
        self.pack_type = pack_type
        self.is_special = is_special
        self.has_volume_range = has_volume_range
        self.is_legacy_chapter_stub = is_legacy_chapter_stub
        self.is_legacy_chapter_recheck = is_legacy_chapter_recheck
        self.plan_status = plan_status
        self.plan_failure_reason = plan_failure_reason


class _ImportPlan:
    """Phase 1 output: queue/series snapshot plus per-file plans."""
    def __init__(self, queue: dict, series: dict | None, series_tags: list[str],
                 dst_dir: str, import_mode: str, now_ts, files: list[_FilePlan],
                 series_id: int):
        self.queue = queue
        self.series = series
        self.series_tags = series_tags
        self.dst_dir = dst_dir
        self.import_mode = import_mode
        self.now_ts = now_ts
        self.files = files
        self.series_id = series_id


def _mark_downloaded(db, series_id, volume_num, torrent_url) -> bool:
    """Mark volume(s) as downloaded. Returns True if any rows updated."""
    if volume_num is not None:
        cur = db.execute(
            "UPDATE volumes SET status='downloaded' WHERE series_id=? AND volume_num=? AND status='grabbed'",
            (series_id, volume_num)
        )
        if cur.rowcount > 0:
            log_event('download_complete', f"Vol {volume_num:g} download complete", series_id, db=db)
            s = db.execute("SELECT title, cover_url FROM series WHERE id=?", (series_id,)).fetchone()
            if s:
                asyncio.create_task(notify_discord(
                    '',
                    embed=make_complete_embed(s['title'], f"Vol {volume_num:g}", s['cover_url'] or ''),
                    event='on_download'
                ))
            vol_row = db.execute(
                "SELECT id FROM volumes WHERE series_id=? AND volume_num=?",
                (series_id, volume_num)
            ).fetchone()
            if vol_row:
                _cascade_chapters(db, series_id, [vol_row['id']], 'downloaded')
            return True
    else:
        pack = db.execute(
            "SELECT * FROM volumes WHERE series_id=? AND source_url=? AND volume_num IS NULL",
            (series_id, torrent_url)
        ).fetchone()
        if not pack:
            return False

        pt = pack['pack_type']
        seen_meta = db.execute(
            "SELECT torrent_name, indexer, protocol, client, release_group, size_bytes"
            " FROM seen WHERE (download_id=? AND download_id IS NOT NULL)"
            " OR torrent_url=? LIMIT 1",
            (pack['download_id'], torrent_url)
        ).fetchone()
        m = dict(seen_meta) if seen_meta else {}

        if pt == 'complete':
            cur = db.execute(
                "UPDATE volumes SET status='downloaded', torrent_name=?, indexer=?, protocol=?,"
                " client=?, release_group=?, size_bytes=?"
                " WHERE series_id=? AND volume_num IS NOT NULL AND status != 'downloaded'",
                (m.get('torrent_name'), m.get('indexer'), m.get('protocol'),
                 m.get('client'), m.get('release_group'), m.get('size_bytes'), series_id)
            )
        elif pt == 'volume' and pack.get('vol_range_start') and pack.get('vol_range_end'):
            cur = db.execute(
                "UPDATE volumes SET status='downloaded', torrent_name=?, indexer=?, protocol=?,"
                " client=?, release_group=?, size_bytes=?"
                " WHERE series_id=? AND volume_num IS NOT NULL AND status != 'downloaded'"
                " AND volume_num >= ? AND volume_num <= ?",
                (m.get('torrent_name'), m.get('indexer'), m.get('protocol'),
                 m.get('client'), m.get('release_group'), m.get('size_bytes'),
                 series_id, pack['vol_range_start'], pack['vol_range_end'])
            )
        else:
            return False

        if cur.rowcount > 0:
            label = 'Complete Series' if pt == 'complete' else f"Vol {int(pack['vol_range_start'])}–{int(pack['vol_range_end'])}"
            log_event('download_complete', f"{label} pack download complete", series_id, db=db)
            s = db.execute("SELECT title, cover_url FROM series WHERE id=?", (series_id,)).fetchone()
            if s:
                asyncio.create_task(notify_discord(
                    '',
                    embed=make_complete_embed(s['title'], label, s['cover_url'] or ''),
                    event='on_download'
                ))
            if pt == 'complete':
                _cascade_chapters(db, series_id, None, 'downloaded')
            elif pt == 'volume':
                rng_ids = [
                    r['id'] for r in db.execute(
                        "SELECT id FROM volumes WHERE series_id=? AND volume_num IS NOT NULL"
                        " AND volume_num >= ? AND volume_num <= ?",
                        (series_id, pack['vol_range_start'], pack['vol_range_end'])
                    ).fetchall()
                ]
                _cascade_chapters(db, series_id, rng_ids, 'downloaded')
            return True
    return False


def _mark_downloaded(db, series_id, volume_num, torrent_url) -> bool:
    """Mark volume(s) as downloaded. Returns True if any rows updated."""
    if volume_num is not None:
        # Single volume stub
        cur = db.execute(
            "UPDATE volumes SET status='downloaded' WHERE series_id=? AND volume_num=? AND status='grabbed'",
            (series_id, volume_num)
        )
        if cur.rowcount > 0:
            log_event('download_complete', f"Vol {volume_num:g} download complete", series_id, db=db)
            s = db.execute("SELECT title, cover_url FROM series WHERE id=?", (series_id,)).fetchone()
            if s:
                asyncio.create_task(notify_discord(
                    '',
                    embed=make_complete_embed(s['title'], f"Vol {volume_num:g}", s['cover_url'] or ''),
                    event='on_download'
                ))
            vol_row = db.execute(
                "SELECT id FROM volumes WHERE series_id=? AND volume_num=?",
                (series_id, volume_num)
            ).fetchone()
            if vol_row:
                _cascade_chapters(db, series_id, [vol_row['id']], 'downloaded')
            return True
    else:
        pack = db.execute(
            "SELECT * FROM volumes WHERE series_id=? AND source_url=? AND volume_num IS NULL",
            (series_id, torrent_url)
        ).fetchone()
        if not pack:
            return False

        pt = pack['pack_type']
        seen_meta = db.execute(
            "SELECT torrent_name, indexer, protocol, client, release_group, size_bytes"
            " FROM seen WHERE (download_id=? AND download_id IS NOT NULL)"
            " OR torrent_url=? LIMIT 1",
            (pack['download_id'], torrent_url)
        ).fetchone()
        m = dict(seen_meta) if seen_meta else {}

        if pt == 'complete':
            cur = db.execute(
                "UPDATE volumes SET status='downloaded', torrent_name=?, indexer=?, protocol=?,"
                " client=?, release_group=?, size_bytes=?"
                " WHERE series_id=? AND volume_num IS NOT NULL AND status != 'downloaded'",
                (m.get('torrent_name'), m.get('indexer'), m.get('protocol'),
                 m.get('client'), m.get('release_group'), m.get('size_bytes'), series_id)
            )
        elif pt == 'volume' and pack.get('vol_range_start') and pack.get('vol_range_end'):
            cur = db.execute(
                "UPDATE volumes SET status='downloaded', torrent_name=?, indexer=?, protocol=?,"
                " client=?, release_group=?, size_bytes=?"
                " WHERE series_id=? AND volume_num IS NOT NULL AND status != 'downloaded'"
                " AND volume_num >= ? AND volume_num <= ?",
                (m.get('torrent_name'), m.get('indexer'), m.get('protocol'),
                 m.get('client'), m.get('release_group'), m.get('size_bytes'),
                 series_id, pack['vol_range_start'], pack['vol_range_end'])
            )
        else:
            return False

        if cur.rowcount > 0:
            label = 'Complete Series' if pt == 'complete' else f"Vol {int(pack['vol_range_start'])}–{int(pack['vol_range_end'])}"
            log_event('download_complete', f"{label} pack download complete", series_id, db=db)
            s = db.execute("SELECT title, cover_url FROM series WHERE id=?", (series_id,)).fetchone()
            if s:
                asyncio.create_task(notify_discord(
                    '',
                    embed=make_complete_embed(s['title'], label, s['cover_url'] or ''),
                    event='on_download'
                ))
            if pt == 'complete':
                _cascade_chapters(db, series_id, None, 'downloaded')
            elif pt == 'volume':
                rng_ids = [
                    r['id'] for r in db.execute(
                        "SELECT id FROM volumes WHERE series_id=? AND volume_num IS NOT NULL"
                        " AND volume_num >= ? AND volume_num <= ?",
                        (series_id, pack['vol_range_start'], pack['vol_range_end'])
                    ).fetchall()
                ]
                _cascade_chapters(db, series_id, rng_ids, 'downloaded')
            return True
    return False


async def _process_auto_import(queue_id: int):
    """Auto-import a queue item where all files mapped cleanly."""
    try:
        await _guarded_execute_import(queue_id)
    except asyncio.CancelledError:
        log_event('info', f"Auto-import cancelled for queue {queue_id}")
        raise
    except Exception as e:
        import traceback
        log_event('error', f"Auto-import failed for queue {queue_id}: {e}")
        print(f"[AutoImport] {e}\n{traceback.format_exc()}")
        try:
            with get_db() as _db_err:
                _db_err.execute(
                    "UPDATE import_queue SET status='failed'"
                    " WHERE id=? AND status IN ('pending','partial','importing')", (queue_id,)
                )
        except Exception as _db_e:
            print(f"[AutoImport] failed to mark queue {queue_id} as failed: {_db_e}")
