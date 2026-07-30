"""Import execution: orchestrate three-phase pipeline (plan → stage → commit)."""

import asyncio
import logging
import os
import secrets
import shutil
from collections.abc import Callable
from typing import TypeVar

from shared import get_cfg, get_db
from events import log_event, broadcast_queue_event
from import_staging import _ImportStaging, _stage_files
from import_lease import (
    IMPORT_LEASE_REFRESH_SECONDS,
    IMPORT_LEASE_SECONDS,
    claim_import_queue_row,
    has_import_sibling_that_may_use_download,
    refresh_import_queue_lease,
    release_import_queue_lease,
    transition_import_queue_row,
)
from import_plan import (
    _FilePlan,
    _ImportPlan,
    _ImportPlanLeaseLost,
    _plan_import as _split_plan_import,
)
from import_commit import _commit_import as _split_commit_import
from import_download import _mark_downloaded
from cover_images import extract_cbz_cover, download_cover
from notifications import trigger_komga_scan
from clients import qbit_remove, sab_remove

log = logging.getLogger(__name__)
_T = TypeVar("_T")
_HEARTBEAT_RETRY_SECONDS = 1.0


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


_MIB = 1024 * 1024


def _fmt_bytes(num: int) -> str:
    if num >= 1024 * 1024 * 1024:
        return f"{num / (1024 * 1024 * 1024):.1f} GiB"
    return f"{num / _MIB:.1f} MiB"


def _minimum_free_space_bytes() -> int:
    raw = str(get_cfg("minimum_free_space_mb", "0") or "0").strip()
    try:
        mb = int(float(raw))
    except (TypeError, ValueError):
        mb = 0
    return max(0, mb) * _MIB


def _planned_import_bytes(plan: _ImportPlan, import_mode: str) -> int:
    if import_mode == "hardlink":
        return 0
    total = 0
    for fp in plan.files:
        if fp.plan_status != "ready":
            continue
        try:
            total += max(0, os.path.getsize(fp.src_path))
        except OSError:
            pass
    return total


def _check_minimum_free_space(plan: _ImportPlan, import_mode: str) -> tuple[bool, str]:
    reserve_bytes = _minimum_free_space_bytes()
    if reserve_bytes <= 0:
        return True, ""
    if not any(fp.plan_status == "ready" for fp in plan.files):
        return True, ""

    planned_bytes = _planned_import_bytes(plan, import_mode)
    required = reserve_bytes + planned_bytes
    try:
        free = shutil.disk_usage(plan.dst_dir).free
    except OSError as exc:
        return False, f"Import blocked: cannot check free space for {plan.dst_dir}: {exc}"
    if free >= required:
        return True, ""
    return (
        False,
        "Import blocked: insufficient free space in "
        f"{plan.dst_dir} ({_fmt_bytes(free)} free, "
        f"{_fmt_bytes(required)} required: {_fmt_bytes(reserve_bytes)} reserve"
        f" + {_fmt_bytes(planned_bytes)} planned import)",
    )


def _mark_plan_failed(plan: _ImportPlan, reason: str) -> None:
    for fp in plan.files:
        if fp.plan_status == "ready":
            fp.plan_status = "pre_failed"
            fp.plan_failure_reason = reason


def _prepare_plan_filesystem(plan: _ImportPlan) -> str:
    """Perform Phase 1 filesystem checks without holding its DB transaction."""
    try:
        os.makedirs(plan.dst_dir, exist_ok=True)
    except OSError as exc:
        reason = f"Import: cannot create {plan.dst_dir}: {exc}"
        _mark_plan_failed(plan, reason)
        return reason

    for fp in plan.files:
        if fp.plan_status == "ready" and not os.path.isfile(fp.src_path):
            fp.plan_status = "pre_failed"
            fp.plan_failure_reason = f"source file missing: {fp.src_path}"
    return ""


async def _wait_task_uninterruptibly(task: asyncio.Task[_T]) -> bool:
    """Wait for ``task`` to settle while deferring caller cancellation.

    Returns whether one or more cancellations were consumed. Shielding keeps
    the worker alive; ``uncancel`` lets repeated cancellations be observed and
    deferred instead of cancelling a later await of the same worker.
    """
    current = asyncio.current_task()
    cancellation_requested = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                break
            cancellation_requested = True
            if current is not None:
                current.uncancel()
        except Exception:
            # The settled exception is retrieved exactly once by task.result()
            # in the caller after the worker can no longer mutate anything.
            break
    return cancellation_requested


async def _run_blocking_uninterruptibly(
    func: Callable[[], _T],
) -> tuple[_T, bool]:
    task = asyncio.create_task(asyncio.to_thread(func))
    cancellation_requested = await _wait_task_uninterruptibly(task)
    return task.result(), cancellation_requested


async def _lease_heartbeat(
    queue_id: int,
    lease_owner: str,
    stop: asyncio.Event,
    ownership_lost: asyncio.Event,
) -> None:
    """Renew one import lease until execution finishes or ownership is lost."""
    refresh_interval = max(0.01, float(IMPORT_LEASE_REFRESH_SECONDS))
    retry_interval = max(0.01, min(refresh_interval, _HEARTBEAT_RETRY_SECONDS))
    lease_deadline = asyncio.get_running_loop().time() + IMPORT_LEASE_SECONDS
    next_delay = refresh_interval
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=next_delay)
            return
        except TimeoutError:
            pass
        try:
            refreshed = await asyncio.to_thread(
                _refresh_owned_import,
                queue_id,
                lease_owner,
            )
        except Exception as exc:
            remaining = lease_deadline - asyncio.get_running_loop().time()
            log.warning(
                "Import queue %s lease heartbeat failed; retrying "
                "(%.3fs estimated remaining): %s",
                queue_id,
                max(0.0, remaining),
                exc,
            )
            next_delay = min(retry_interval, max(0.01, remaining))
            continue
        if not refreshed:
            ownership_lost.set()
            return
        lease_deadline = asyncio.get_running_loop().time() + IMPORT_LEASE_SECONDS
        next_delay = refresh_interval


async def _stop_lease_heartbeat(
    heartbeat: asyncio.Task[None],
    stop: asyncio.Event,
) -> bool:
    stop.set()
    cancellation_requested = await _wait_task_uninterruptibly(heartbeat)
    try:
        heartbeat.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        log_event("error", f"[Import] lease heartbeat shutdown failed: {exc}")
    return cancellation_requested


def _refresh_owned_import(queue_id: int, lease_owner: str) -> bool:
    with get_db() as db:
        return refresh_import_queue_lease(
            db,
            queue_id,
            lease_owner,
            lease_seconds=IMPORT_LEASE_SECONDS,
        )


def _cleanup_pack_staging_if_safe(
    queue_id: int,
    download_id: str,
    lease_owner: str,
) -> bool:
    """Detach shared pack staging only while a successor cannot own it.

    The pack source path predates leases and is keyed only by download ID.
    The writer lock covers the ownership proof and atomic rename, preventing a
    successor claim between them. Recursive deletion happens after commit so
    slow storage cannot hold the SQLite writer lock.
    """
    from import_pipeline import PACK_STAGING_ROOT

    pack_dir = os.path.join(PACK_STAGING_ROOT, f"queue-{download_id}")
    tombstone = f"{pack_dir}.cleanup-{secrets.token_urlsafe(18)}"
    detached = False
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT status, lease_owner,"
            " lease_expires_at > datetime('now') AS lease_live"
            " FROM import_queue WHERE id=?",
            (queue_id,),
        ).fetchone()
        if row is not None:
            exact_owner = (
                row["status"] == "importing"
                and row["lease_owner"] == lease_owner
                and bool(row["lease_live"])
            )
            settled_unleased = (
                row["status"] != "importing" and row["lease_owner"] is None
            )
            if not exact_owner and not settled_unleased:
                return False
        if has_import_sibling_that_may_use_download(
            db,
            queue_id=queue_id,
            download_id=download_id,
        ):
            return False
        try:
            os.replace(pack_dir, tombstone)
        except FileNotFoundError:
            return True
        except OSError as exc:
            log.warning(
                "Import queue %s could not detach pack staging %s: %s",
                queue_id,
                pack_dir,
                exc,
            )
            return False
        detached = True

    if detached:
        try:
            shutil.rmtree(tombstone)
        except OSError as exc:
            log.warning(
                "Import queue %s could not remove detached pack staging %s: %s",
                queue_id,
                tombstone,
                exc,
            )
            return False
    return True


async def _guarded_execute_import(
    queue_id: int,
    volume_overrides: dict[int, float] | None = None,
    skip_ids: set[int] | None = None,
    chapter_overrides: dict[int, float] | None = None,
) -> bool:
    """Acquire capacity, claim with an owner token, and execute with heartbeat."""
    async with _get_import_sem():
        lease_owner = secrets.token_urlsafe(32)
        try:
            with get_db() as claim_db:
                if not claim_import_queue_row(
                    claim_db,
                    queue_id,
                    lease_owner,
                    lease_seconds=IMPORT_LEASE_SECONDS,
                ):
                    log_event(
                        "info",
                        f"[Import] queue {queue_id}: claim lost",
                        db=claim_db,
                    )
                    return False
        except Exception as exc:
            log_event(
                "error",
                f"[Import] queue {queue_id}: claim failed: {exc}",
            )
            return False

        heartbeat_stop = asyncio.Event()
        ownership_lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            _lease_heartbeat(
                queue_id,
                lease_owner,
                heartbeat_stop,
                ownership_lost,
            ),
            name=f"import-lease-heartbeat-{queue_id}",
        )
        result = False
        execution_error: Exception | None = None
        cancellation_requested = False
        try:
            result = await _execute_import(
                queue_id,
                volume_overrides,
                skip_ids,
                chapter_overrides,
                lease_owner=lease_owner,
                ownership_lost=ownership_lost,
            )
        except asyncio.CancelledError:
            cancellation_requested = True
        except Exception as exc:
            execution_error = exc
        finally:
            cancellation_requested |= await _stop_lease_heartbeat(
                heartbeat,
                heartbeat_stop,
            )

        if cancellation_requested:
            log_event(
                "info",
                f"[Import] _guarded_execute_import cancelled for queue {queue_id}",
            )
            try:
                with get_db() as release_db:
                    release_import_queue_lease(
                        release_db,
                        queue_id,
                        lease_owner,
                    )
            except Exception as exc:
                log_event(
                    "error",
                    f"[Import] queue {queue_id}: cancellation release failed: {exc}",
                )
            raise asyncio.CancelledError

        if execution_error is not None:
            log_event(
                "error",
                f"[Import] queue {queue_id}: execution failed: {execution_error}",
            )
            try:
                with get_db() as fail_db:
                    transition_import_queue_row(
                        fail_db,
                        queue_id,
                        lease_owner,
                        "failed",
                    )
            except Exception as fail_exc:
                log_event(
                    "error",
                    f"[Import] queue {queue_id}: failure transition failed: "
                    f"{fail_exc}",
                )
            return False
        return result


async def _execute_import(
    queue_id: int,
    volume_overrides: dict[int, float] | None = None,
    skip_ids: set[int] | None = None,
    chapter_overrides: dict[int, float] | None = None,
    *,
    lease_owner: str | None = None,
    ownership_lost: asyncio.Event | None = None,
) -> bool:
    """Wrapper around _execute_import_impl with auto-pack staging cleanup."""
    if lease_owner is None:
        return await _guarded_execute_import(
            queue_id,
            volume_overrides,
            skip_ids,
            chapter_overrides,
        )

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
                queue_id,
                volume_overrides,
                skip_ids,
                chapter_overrides,
                lease_owner=lease_owner,
                ownership_lost=ownership_lost,
            )
        except asyncio.CancelledError:
            log_event(
                "info", f"[Import] _execute_import cancelled for queue {queue_id}"
            )
            raise
    finally:
        if pack_cleanup_id:
            try:
                _, cleanup_cancelled = await _run_blocking_uninterruptibly(
                    lambda: _cleanup_pack_staging_if_safe(
                        queue_id,
                        pack_cleanup_id,
                        lease_owner,
                    )
                )
                if cleanup_cancelled:
                    raise asyncio.CancelledError
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "Import queue %s pack staging cleanup failed: %s",
                    queue_id,
                    exc,
                )


async def _execute_import_impl(
    queue_id: int,
    volume_overrides: dict[int, float] | None = None,
    skip_ids: set[int] | None = None,
    chapter_overrides: dict[int, float] | None = None,
    *,
    lease_owner: str | None = None,
    ownership_lost: asyncio.Event | None = None,
) -> bool:
    """Three-phase pipeline: Plan → Stage → Commit."""
    if lease_owner is None:
        return await _guarded_execute_import(
            queue_id,
            volume_overrides,
            skip_ids,
            chapter_overrides,
        )
    if ownership_lost is None:
        ownership_lost = asyncio.Event()

    if volume_overrides is None:
        volume_overrides = {}
    if chapter_overrides is None:
        chapter_overrides = {}
    if skip_ids is None:
        skip_ids = set()

    import_mode = get_cfg("import_mode", "hardlink")

    # ── Phase 1 — short DB tx for planning ──────────────────────────────
    try:
        with get_db() as _db1:
            if not refresh_import_queue_lease(
                _db1,
                queue_id,
                lease_owner,
                lease_seconds=IMPORT_LEASE_SECONDS,
            ):
                ownership_lost.set()
                return False
            plan = _split_plan_import(
                _db1,
                queue_id,
                lease_owner,
                volume_overrides,
                chapter_overrides,
                skip_ids,
                import_mode,
                lease_seconds=IMPORT_LEASE_SECONDS,
            )
    except _ImportPlanLeaseLost:
        ownership_lost.set()
        return False
    if plan is None:
        return False

    queue = plan.queue
    filesystem_reason, prepare_cancelled = await _run_blocking_uninterruptibly(
        lambda: _prepare_plan_filesystem(plan)
    )
    if prepare_cancelled:
        raise asyncio.CancelledError

    space_ok, space_reason = _check_minimum_free_space(plan, import_mode)
    if filesystem_reason or not space_ok:
        failure_reason = filesystem_reason or space_reason
        if not filesystem_reason:
            _mark_plan_failed(plan, failure_reason)
        with get_db() as _db_space:
            _split_commit_import(
                _db_space,
                plan,
                [],
                fs_committed=False,
                commit_failure_reason=failure_reason,
                lease_owner=lease_owner,
                lease_seconds=IMPORT_LEASE_SECONDS,
            )
        return False

    # ── Phase 2 — filesystem (no DB held) ───────────────────────────────
    staging = _ImportStaging(plan.dst_dir, queue["id"], import_mode)
    stage_task = asyncio.create_task(_stage_files(plan, staging))
    stage_cancelled = await _wait_task_uninterruptibly(stage_task)
    try:
        outcomes = stage_task.result()
    except Exception:
        _, rollback_cancelled = await _run_blocking_uninterruptibly(staging.rollback)
        if stage_cancelled or rollback_cancelled:
            raise asyncio.CancelledError
        raise
    if stage_cancelled:
        _, rollback_cancelled = await _run_blocking_uninterruptibly(staging.rollback)
        if rollback_cancelled:
            stage_cancelled = True
        raise asyncio.CancelledError
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
    cancelled_after_publication = False
    if (has_pre_failed or has_stage_fail) and would_be_imported > 0:
        _, rollback_cancelled = await _run_blocking_uninterruptibly(staging.rollback)
        if rollback_cancelled:
            raise asyncio.CancelledError
    elif would_be_imported > 0:
        if ownership_lost.is_set():
            _, rollback_cancelled = await _run_blocking_uninterruptibly(
                staging.rollback
            )
            if rollback_cancelled:
                raise asyncio.CancelledError
            return False
        refresh_task = asyncio.create_task(
            asyncio.to_thread(
                _refresh_owned_import,
                queue_id,
                lease_owner,
            )
        )
        refresh_cancelled = await _wait_task_uninterruptibly(refresh_task)
        if refresh_cancelled:
            await _run_blocking_uninterruptibly(staging.rollback)
            raise asyncio.CancelledError
        try:
            refresh_owned = refresh_task.result()
        except Exception:
            _, rollback_cancelled = await _run_blocking_uninterruptibly(
                staging.rollback
            )
            if rollback_cancelled:
                raise asyncio.CancelledError
            raise
        if not refresh_owned:
            ownership_lost.set()
            _, rollback_cancelled = await _run_blocking_uninterruptibly(
                staging.rollback
            )
            if rollback_cancelled:
                raise asyncio.CancelledError
            return False
        commit_task = asyncio.create_task(asyncio.to_thread(staging.commit_all))
        cancelled_after_publication = await _wait_task_uninterruptibly(commit_task)
        try:
            commit_task.result()
            fs_committed = True
        except Exception as e:
            _, rollback_cancelled = await _run_blocking_uninterruptibly(
                staging.rollback
            )
            cancelled_after_publication |= rollback_cancelled
            commit_failure_reason = str(e)
    else:
        _, rollback_cancelled = await _run_blocking_uninterruptibly(staging.rollback)
        if rollback_cancelled:
            raise asyncio.CancelledError

    # ── Phase 3 — short DB tx for replay ────────────────────────────────
    with get_db() as _db3:
        ok, imported_count, new_status = _split_commit_import(
            _db3,
            plan,
            outcomes,
            fs_committed=fs_committed,
            commit_failure_reason=commit_failure_reason,
            lease_owner=lease_owner,
            lease_seconds=IMPORT_LEASE_SECONDS,
        )

    if cancelled_after_publication:
        raise asyncio.CancelledError

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
