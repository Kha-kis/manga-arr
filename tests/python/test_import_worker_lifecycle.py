"""Adversarial contracts for process-local auto-import worker ownership."""

from __future__ import annotations

import asyncio
import logging

import pytest


def test_same_queue_id_deduplicates_to_one_live_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_execute
    import import_workers

    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[int] = []

    async def _blocked_import(queue_id: int) -> None:
        calls.append(queue_id)
        started.set()
        await release.wait()

    monkeypatch.setattr(import_execute, "_process_auto_import", _blocked_import)

    async def _exercise() -> None:
        import_workers.start_import_worker_scheduling()
        first = import_workers.schedule_import_worker(41)
        second = import_workers.schedule_import_worker(41)
        assert first is not None
        assert second is first
        await started.wait()
        assert calls == [41]

        release.set()
        await first
        await asyncio.sleep(0)
        assert import_workers._IMPORT_WORKERS == {}
        import_workers.stop_import_worker_scheduling()
        await import_workers.cancel_import_workers()

    asyncio.run(_exercise())


def test_stale_callback_cannot_remove_replacement_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_execute
    import import_workers

    async def _wait_for_cancellation(queue_id: int) -> None:
        del queue_id
        await asyncio.Event().wait()

    monkeypatch.setattr(
        import_execute,
        "_process_auto_import",
        _wait_for_cancellation,
    )

    async def _exercise() -> None:
        old = asyncio.create_task(asyncio.sleep(0))
        await old

        import_workers.start_import_worker_scheduling()
        import_workers._IMPORT_WORKERS[42] = old
        replacement = import_workers.schedule_import_worker(42)
        assert replacement is not None
        assert replacement is not old
        import_workers._on_import_worker_done(42, old)
        assert import_workers._IMPORT_WORKERS[42] is replacement

        import_workers.stop_import_worker_scheduling()
        await import_workers.cancel_import_workers()
        assert import_workers._IMPORT_WORKERS == {}

    asyncio.run(_exercise())


def test_worker_exception_is_retrieved_logged_once_and_cleaned(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import import_execute
    import import_workers

    async def _fail(queue_id: int) -> None:
        raise RuntimeError(f"queue {queue_id} exploded")

    monkeypatch.setattr(import_execute, "_process_auto_import", _fail)

    async def _exercise() -> None:
        import_workers.start_import_worker_scheduling()
        with caplog.at_level(logging.ERROR, logger="import_workers"):
            task = import_workers.schedule_import_worker(43)
            assert task is not None
            while import_workers._IMPORT_WORKERS:
                await asyncio.sleep(0)
        assert task.done()
        import_workers.stop_import_worker_scheduling()
        await import_workers.cancel_import_workers()

    asyncio.run(_exercise())

    records = [
        record
        for record in caplog.records
        if "import worker for queue 43 exited with exception" in record.getMessage()
    ]
    assert len(records) == 1
    assert "queue 43 exploded" in records[0].getMessage()


def test_closed_gate_rejects_queue_id_without_creating_coroutine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_workers

    calls: list[int] = []

    async def _unexpected_worker(queue_id: int) -> None:
        calls.append(queue_id)

    monkeypatch.setattr(import_workers, "_run_import_worker", _unexpected_worker)

    async def _exercise() -> None:
        import_workers.stop_import_worker_scheduling()
        assert import_workers.schedule_import_worker(44) is None
        await asyncio.sleep(0)
        assert calls == []
        assert import_workers._IMPORT_WORKERS == {}

    asyncio.run(_exercise())


def test_shutdown_gate_wins_producer_cancellation_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_execute
    import import_workers
    import tasks

    worker_calls: list[int] = []
    rejected: list[asyncio.Task[None] | None] = []

    async def _unexpected_import(queue_id: int) -> None:
        worker_calls.append(queue_id)

    async def _producer() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            rejected.append(import_workers.schedule_import_worker(45))
            raise

    monkeypatch.setattr(import_execute, "_process_auto_import", _unexpected_import)

    async def _exercise() -> None:
        import_workers.start_import_worker_scheduling()
        producer = tasks.create_background_task(
            _producer(),
            name="adversarial-import-producer",
        )
        await asyncio.sleep(0)

        import_workers.stop_import_worker_scheduling()
        await tasks._cancel_background_tasks()
        await import_workers.cancel_import_workers()

        assert producer.cancelled()
        assert rejected == [None]
        assert worker_calls == []
        assert import_workers._IMPORT_WORKERS == {}

    asyncio.run(_exercise())


def test_worker_cancellation_is_awaited_and_registry_drained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_execute
    import import_workers

    started = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def _cancellation_safe_import(queue_id: int) -> None:
        del queue_id
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0)
            cleanup_finished.set()
            raise

    monkeypatch.setattr(
        import_execute,
        "_process_auto_import",
        _cancellation_safe_import,
    )

    async def _exercise() -> None:
        import_workers.start_import_worker_scheduling()
        task = import_workers.schedule_import_worker(46)
        assert task is not None
        await started.wait()

        import_workers.stop_import_worker_scheduling()
        await import_workers.cancel_import_workers()

        assert task.cancelled()
        assert cleanup_finished.is_set()
        assert import_workers._IMPORT_WORKERS == {}

    asyncio.run(_exercise())


def test_clean_shutdown_allows_fresh_scheduling_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_execute
    import import_workers

    calls: list[int] = []

    async def _record(queue_id: int) -> None:
        calls.append(queue_id)

    monkeypatch.setattr(import_execute, "_process_auto_import", _record)

    async def _run_lifecycle(queue_id: int) -> None:
        import_workers.start_import_worker_scheduling()
        task = import_workers.schedule_import_worker(queue_id)
        assert task is not None
        await task
        await asyncio.sleep(0)
        import_workers.stop_import_worker_scheduling()
        await import_workers.cancel_import_workers()
        assert import_workers._IMPORT_WORKERS == {}

    asyncio.run(_run_lifecycle(47))
    asyncio.run(_run_lifecycle(48))
    assert calls == [47, 48]
