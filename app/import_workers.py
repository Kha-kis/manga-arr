"""Process-local ownership for automatically executed import queue rows."""

from __future__ import annotations

import asyncio
import logging
from functools import partial


log = logging.getLogger(__name__)

_IMPORT_WORKERS: dict[int, asyncio.Task[None]] = {}
_accepting_import_workers = False


async def _run_import_worker(queue_id: int) -> None:
    # Lazy to keep this lifecycle-only module out of the import pipeline cycle.
    from import_execute import _process_auto_import

    await _process_auto_import(queue_id)


def _on_import_worker_done(
    queue_id: int,
    task: asyncio.Task[None],
) -> None:
    """Retrieve one worker result and remove only the task that owns its key."""
    if _IMPORT_WORKERS.get(queue_id) is task:
        del _IMPORT_WORKERS[queue_id]

    if task.cancelled():
        return

    exception = task.exception()
    if exception is not None:
        log.error(
            "import worker for queue %d exited with exception: %r",
            queue_id,
            exception,
            exc_info=exception,
        )


def start_import_worker_scheduling() -> None:
    """Open admission for a new application lifespan."""
    global _accepting_import_workers

    if _IMPORT_WORKERS:
        raise RuntimeError("cannot start import scheduling with workers still owned")
    _accepting_import_workers = True


def stop_import_worker_scheduling() -> None:
    """Close admission synchronously before producer shutdown begins."""
    global _accepting_import_workers

    _accepting_import_workers = False


def schedule_import_worker(queue_id: int) -> asyncio.Task[None] | None:
    """Schedule one auto-import by queue ID, deduplicated while it is live.

    A queue ID is accepted instead of a coroutine so a closed admission gate
    cannot strand an unawaited coroutine during shutdown.
    """
    if not _accepting_import_workers:
        return None

    current = _IMPORT_WORKERS.get(queue_id)
    if current is not None and not current.done():
        return current

    task = asyncio.create_task(
        _run_import_worker(queue_id),
        name=f"import_worker:{queue_id}",
    )
    _IMPORT_WORKERS[queue_id] = task
    task.add_done_callback(partial(_on_import_worker_done, queue_id))
    return task


async def cancel_import_workers() -> None:
    """Cancel and await all workers after their producers have stopped."""
    workers = list(_IMPORT_WORKERS.values())
    for worker in workers:
        _ = worker.cancel()
    if workers:
        _ = await asyncio.gather(*workers, return_exceptions=True)
