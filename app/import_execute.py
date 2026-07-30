"""Import execution: orchestrate three-phase pipeline (plan → stage → commit)."""

import asyncio
import logging
import os
import secrets
import shutil
from collections.abc import Callable
from typing import TypeVar

from download_identity import (
    DownloadProtocol,
    coerce_download_client_id,
    normalize_download_protocol,
)
from shared import get_cfg, get_db
from events import log_event, broadcast_queue_event
from import_staging import _ImportStaging, _stage_files
from import_lease import (
    IMPORT_LEASE_REFRESH_SECONDS,
    IMPORT_LEASE_SECONDS,
    claim_import_queue_row,
    refresh_import_queue_lease,
    release_import_queue_lease,
    transition_import_queue_row,
)
from import_pack_cleanup import cleanup_terminal_pack_staging
from import_plan import (
    _FilePlan,
    _ImportPlan,
    _ImportPlanLeaseLost,
    _plan_import as _split_plan_import,
)
from import_commit import _commit_import as _split_commit_import
from import_download import _mark_downloaded
from import_publication import (
    abort_staging_publication,
    commit_prepared_barrier,
    complete_publication,
    create_publication,
    ensure_durable_directory,
    initialize_publication_filesystem,
    load_publication,
    prepare_staged_artifacts,
)

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
        ensure_durable_directory(plan.dst_dir)
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
    """Compatibility wrapper for terminal-only durable pack cleanup."""
    _ = lease_owner
    with get_db() as db:
        queue = db.execute(
            "SELECT download_client_id, download_protocol"
            " FROM import_queue WHERE id=?",
            (queue_id,),
        ).fetchone()
    return cleanup_terminal_pack_staging(
        queue_id,
        download_id,
        download_client_id=(
            coerce_download_client_id(queue["download_client_id"])
            if queue is not None
            else None
        ),
        protocol=(
            normalize_download_protocol(queue["download_protocol"])
            if queue is not None
            else None
        ),
    )


async def _guarded_execute_import(
    queue_id: int,
    volume_overrides: dict[int, float] | None = None,
    skip_ids: set[int] | None = None,
    chapter_overrides: dict[int, float] | None = None,
) -> bool:
    """Acquire capacity, claim with an owner token, and execute with heartbeat."""
    async with _get_import_sem():
        lease_owner = secrets.token_urlsafe(32)
        pack_cleanup_identity: tuple[
            str,
            int | None,
            DownloadProtocol | None,
        ] | None = None
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
                identity = claim_db.execute(
                    "SELECT download_id, download_client_id, download_protocol"
                    " FROM import_queue WHERE id=?",
                    (queue_id,),
                ).fetchone()
                if identity is not None and identity["download_id"]:
                    pack_cleanup_identity = (
                        str(identity["download_id"]),
                        coerce_download_client_id(
                            identity["download_client_id"]
                        ),
                        normalize_download_protocol(
                            identity["download_protocol"]
                        ),
                    )
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
                    transitioned = transition_import_queue_row(
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
                transitioned = False
            if transitioned and pack_cleanup_identity:
                download_id, download_client_id, protocol = (
                    pack_cleanup_identity
                )
                try:
                    _, cleanup_cancelled = await _run_blocking_uninterruptibly(
                        lambda: cleanup_terminal_pack_staging(
                            queue_id,
                            download_id,
                            download_client_id=download_client_id,
                            protocol=protocol,
                        )
                    )
                    if cleanup_cancelled:
                        raise asyncio.CancelledError
                except asyncio.CancelledError:
                    raise
                except Exception as cleanup_exc:
                    log.warning(
                        "Import queue %s pack staging cleanup failed: %s",
                        queue_id,
                        cleanup_exc,
                    )
            return False
        if pack_cleanup_identity:
            download_id, download_client_id, protocol = pack_cleanup_identity
            try:
                with get_db() as cleanup_db:
                    publication_row = cleanup_db.execute(
                        "SELECT id FROM import_publications"
                        " WHERE queue_id=? ORDER BY id DESC LIMIT 1",
                        (queue_id,),
                    ).fetchone()
                cleanup_publication_id = (
                    int(publication_row["id"])
                    if publication_row is not None
                    else None
                )
                _, cleanup_cancelled = await _run_blocking_uninterruptibly(
                    lambda: cleanup_terminal_pack_staging(
                        queue_id,
                        download_id,
                        download_client_id=download_client_id,
                        protocol=protocol,
                        publication_id=cleanup_publication_id,
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
    """Run one already-owned import while preserving deferred cancellation."""
    if lease_owner is None:
        return await _guarded_execute_import(
            queue_id,
            volume_overrides,
            skip_ids,
            chapter_overrides,
        )

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

    outcomes = []
    publication_id: int | None = None
    ready_count = sum(fp.plan_status == "ready" for fp in plan.files)
    if ready_count == 0:
        # All-skipped and wholly invalid plans have no publication boundary.
        with get_db() as _db3:
            ok, imported_count, new_status = _split_commit_import(
                _db3,
                plan,
                [],
                fs_committed=False,
                commit_failure_reason="",
                lease_owner=lease_owner,
                lease_seconds=IMPORT_LEASE_SECONDS,
            )
    else:
        # ── Phase 2 — filesystem + durable prepared barrier ─────────────
        try:
            (
                publication_fs,
                init_cancelled,
            ) = await _run_blocking_uninterruptibly(
                lambda: initialize_publication_filesystem(plan, lease_owner)
            )
        except Exception:
            raise
        if init_cancelled:
            staging_dir, _ = publication_fs
            await _run_blocking_uninterruptibly(
                lambda: shutil.rmtree(staging_dir, ignore_errors=True)
            )
            raise asyncio.CancelledError
        staging_dir, source_fingerprints = publication_fs

        try:
            with get_db() as journal_db:
                publication_id = create_publication(
                    journal_db,
                    plan,
                    lease_owner,
                    staging_dir,
                    source_fingerprints,
                )
        except Exception:
            await _run_blocking_uninterruptibly(
                lambda: shutil.rmtree(staging_dir, ignore_errors=True)
            )
            raise

        staging = _ImportStaging(
            plan.dst_dir,
            queue["id"],
            import_mode,
            staging_dir=staging_dir,
            journal_owned=True,
        )

        async def _abort_reversible_staging() -> bool:
            _, rollback_cancelled = await _run_blocking_uninterruptibly(
                staging.rollback
            )
            abort_staging_publication(
                publication_id,
                release_queue=False,
            )
            return rollback_cancelled

        stage_task = asyncio.create_task(_stage_files(plan, staging))
        stage_cancelled = await _wait_task_uninterruptibly(stage_task)
        try:
            outcomes = stage_task.result()
        except Exception:
            rollback_cancelled = await _abort_reversible_staging()
            if stage_cancelled or rollback_cancelled:
                raise asyncio.CancelledError
            raise
        if stage_cancelled:
            await _abort_reversible_staging()
            raise asyncio.CancelledError

        outcomes_by_id = {outcome.file_id: outcome for outcome in outcomes}
        has_pre_failed = any(
            file_plan.plan_status == "pre_failed" for file_plan in plan.files
        )
        has_stage_fail = any(
            file_plan.plan_status == "ready"
            and not outcomes_by_id[file_plan.file_id].ok
            for file_plan in plan.files
        )
        if has_pre_failed or has_stage_fail:
            rollback_cancelled = await _abort_reversible_staging()
            if rollback_cancelled:
                raise asyncio.CancelledError
            with get_db() as _db3:
                ok, imported_count, new_status = _split_commit_import(
                    _db3,
                    plan,
                    outcomes,
                    fs_committed=False,
                    commit_failure_reason="",
                    lease_owner=lease_owner,
                    lease_seconds=IMPORT_LEASE_SECONDS,
                )
        else:
            if ownership_lost.is_set():
                await _abort_reversible_staging()
                return False
            refresh_owned, refresh_cancelled = await _run_blocking_uninterruptibly(
                lambda: _refresh_owned_import(queue_id, lease_owner)
            )
            if refresh_cancelled:
                await _abort_reversible_staging()
                raise asyncio.CancelledError
            if not refresh_owned:
                ownership_lost.set()
                await _abort_reversible_staging()
                return False

            try:
                artifacts, artifact_cancelled = await _run_blocking_uninterruptibly(
                    lambda: prepare_staged_artifacts(
                        plan,
                        staging_dir,
                        outcomes,
                    )
                )
                if artifact_cancelled:
                    await _abort_reversible_staging()
                    raise asyncio.CancelledError
                commit_prepared_barrier(
                    publication_id,
                    outcomes,
                    artifacts,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                await _abort_reversible_staging()
                raise

            publication_task = asyncio.create_task(
                complete_publication(publication_id, lease_owner)
            )
            cancelled_after_publication = await _wait_task_uninterruptibly(
                publication_task
            )
            publication_task.result()
            with get_db() as publication_db:
                publication = load_publication(
                    publication_db,
                    publication_id=publication_id,
                )
            if publication is None:
                return False
            ok = bool(publication.result_ok) and publication.state in (
                "finalized",
                "deleted",
            )
            imported_count = publication.result_imported_count or 0
            new_status = publication.result_queue_status or "importing"
            if cancelled_after_publication:
                raise asyncio.CancelledError

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
