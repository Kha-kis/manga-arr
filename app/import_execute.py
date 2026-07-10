"""Import execution: orchestrate three-phase pipeline (plan → stage → commit)."""

import asyncio
import os

from shared import get_cfg, get_db
from events import log_event, broadcast_queue_event
from import_staging import _ImportStaging, _stage_files
from import_plan import _plan_import as _split_plan_import, _FilePlan, _ImportPlan
from import_commit import _commit_import as _split_commit_import
from import_download import _mark_downloaded
from cover_images import extract_cbz_cover, download_cover
from notifications import trigger_komga_scan
from clients import qbit_remove, sab_remove


# Semaphore for bounding concurrent imports
_IMPORT_SEM: asyncio.Semaphore | None = None


def _get_import_sem() -> asyncio.Semaphore:
    """Lazily construct semaphore with current config value."""
    global _IMPORT_SEM
    if _IMPORT_SEM is None:
        limit = int(get_cfg("max_concurrent_imports", "2") or "2")
        _IMPORT_SEM = asyncio.Semaphore(limit)
    return _IMPORT_SEM


def initialize_import_semaphore() -> None:
    """Called from lifespan() to warm-start the semaphore."""
    _get_import_sem()


def claim_import_queue_row(
    db, queue_id: int, allowed_statuses: tuple[str, ...] = ("pending", "partial")
) -> bool:
    """Atomically transition the queue row into 'importing' state.

    Returns True iff this caller won the race.
    """
    placeholders = ",".join("?" * len(allowed_statuses))
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
            log_event("info", f"[Import] queue {queue_id}: claim lost", db=_claim_db)
            return False
    async with _get_import_sem():
        try:
            return await _execute_import(
                queue_id, volume_overrides, skip_ids, chapter_overrides
            )
        except asyncio.CancelledError:
            log_event(
                "info",
                f"[Import] _guarded_execute_import cancelled for queue {queue_id}",
            )
            with get_db() as _fail_db:
                _fail_db.execute(
                    "UPDATE import_queue SET status='failed', failed_at=datetime('now') WHERE id=?",
                    (queue_id,),
                )
                _fail_db.execute(
                    "UPDATE import_queue_files SET status='cancelled' WHERE queue_id=?",
                    (queue_id,),
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
        if _qrow and _qrow["download_id"]:
            pack_cleanup_id = _qrow["download_id"]
    try:
        try:
            return await _execute_import_impl(
                queue_id, volume_overrides, skip_ids, chapter_overrides
            )
        except asyncio.CancelledError:
            log_event(
                "info", f"[Import] _execute_import cancelled for queue {queue_id}"
            )
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

    import_mode = get_cfg("import_mode", "hardlink")

    # ── Phase 1 — short DB tx for planning ──────────────────────────────
    with get_db() as _db1:
        plan = _split_plan_import(
            _db1,
            queue_id,
            volume_overrides,
            chapter_overrides,
            skip_ids,
            import_mode,
        )
    if plan is None:
        return False

    queue = plan.queue

    # ── Phase 2 — filesystem (no DB held) ───────────────────────────────
    staging = _ImportStaging(plan.dst_dir, queue["id"], import_mode)
    outcomes = await _stage_files(plan, staging)
    outcomes_by_id = {o.file_id: o for o in outcomes}

    has_pre_failed = any(fp.plan_status == "pre_failed" for fp in plan.files)
    has_stage_fail = any(
        fp.plan_status == "ready" and not outcomes_by_id[fp.file_id].ok
        for fp in plan.files
    )
    would_be_imported = sum(
        1
        for fp in plan.files
        if fp.plan_status == "ready" and outcomes_by_id[fp.file_id].ok
    )

    fs_committed = False
    commit_failure_reason = ""
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
        ok, imported_count, new_status = _split_commit_import(
            _db3,
            plan,
            outcomes,
            fs_committed=fs_committed,
            commit_failure_reason=commit_failure_reason,
        )

    # ── Post-import work ────────────────────────────────────────────────
    if ok:
        with get_db() as _cdb:
            _series_id = queue["series_id"]
            _cover_url = _cdb.execute(
                "SELECT cover_url FROM series WHERE id=?", (_series_id,)
            ).fetchone()
        _local_cover = f"/config/covers/{_series_id}.jpg"
        if not os.path.exists(_local_cover):
            with get_db() as _cdb2:
                _first_cbz = _cdb2.execute(
                    "SELECT dst_path FROM import_queue_files"
                    " WHERE queue_id=? AND status='imported' AND dst_path LIKE '%.cbz'",
                    (queue_id,),
                ).fetchone()
            if _first_cbz and _first_cbz["dst_path"]:
                extract_cbz_cover(_series_id, _first_cbz["dst_path"])
            elif _cover_url and _cover_url["cover_url"]:
                asyncio.create_task(download_cover(_series_id, _cover_url["cover_url"]))
        await trigger_komga_scan()
        if (
            get_cfg("remove_completed", "false").lower() == "true"
            and queue["download_id"]
        ):
            with get_db() as db2:
                proto = db2.execute(
                    "SELECT protocol FROM volumes WHERE download_id=? LIMIT 1",
                    (queue["download_id"],),
                ).fetchone()
            protocol = (proto["protocol"] if proto else "") or "torrent"
            if protocol == "torrent":
                await qbit_remove(queue["download_id"])
            else:
                await sab_remove(queue["download_id"])
    asyncio.create_task(
        broadcast_queue_event("import_complete", {"queue_id": queue_id})
    )
    return ok


async def _process_auto_import(queue_id: int):
    """Auto-import a queue item where all files mapped cleanly."""
    try:
        await _guarded_execute_import(queue_id)
    except asyncio.CancelledError:
        log_event("info", f"Auto-import cancelled for queue {queue_id}")
        raise
    except Exception as e:
        import traceback

        log_event(
            "error",
            f"Auto-import failed for queue {queue_id}: {e}\n{traceback.format_exc()}",
        )
        try:
            with get_db() as _db_err:
                _db_err.execute(
                    "UPDATE import_queue SET status='failed'"
                    " WHERE id=? AND status IN ('pending','partial','importing')",
                    (queue_id,),
                )
        except Exception as _db_e:
            log_event(
                "error",
                f"Auto-import failed to mark queue {queue_id} as failed: {_db_e}",
            )
