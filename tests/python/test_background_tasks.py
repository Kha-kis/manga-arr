"""Tests for H1: background task lifecycle.

Covers:
- create_background_task() stores the task in _BACKGROUND_TASKS and
  removes it when the task completes normally
- Uncaught exceptions are logged (not silently swallowed) and the
  reference is still cleaned up
- _cancel_background_tasks() cancels every outstanding task and awaits
  their graceful exit
- An exception inside a loop iteration doesn't terminate the loop —
  the existing inner try/except keeps it ticking
- Lifespan startup populates _BACKGROUND_TASKS with the expected loops
"""
import asyncio
import logging


def _run(coro):
    """Run a coroutine in a fresh event loop and restore a default loop
    afterwards (our SSRF tests use the deprecated get_event_loop pattern)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


# ───────────────────── helper lifecycle ─────────────────────

def test_create_background_task_stores_reference():
    import main

    async def _inner():
        async def _short():
            await asyncio.sleep(0)
            return 42
        t = main.create_background_task(_short(), name="test-short")
        assert t in main._BACKGROUND_TASKS
        assert t.get_name() == "test-short"
        await t
        # After completion the done-callback must clear the reference.
        assert t not in main._BACKGROUND_TASKS
    _run(_inner())


def test_task_exception_logs_and_clears_reference(caplog):
    """A task that raises must: (a) still be removed from the set,
    (b) produce a warning/error-level log line naming the task."""
    import main

    async def _inner():
        async def _bad():
            await asyncio.sleep(0)
            raise RuntimeError("boom")
        t = main.create_background_task(_bad(), name="test-bad")
        with caplog.at_level(logging.ERROR, logger="main"):
            try:
                await t
            except RuntimeError:
                pass  # the await re-raises; we still expect the callback to fire
            # Give the done-callback a tick to run.
            await asyncio.sleep(0)
        assert t not in main._BACKGROUND_TASKS
        # Log line names the task and carries the exception.
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "test-bad" in joined, f"task name missing from log: {joined!r}"
        assert "boom" in joined or "RuntimeError" in joined
    _run(_inner())


def test_cancellation_is_silent(caplog):
    """Shutdown cancels every task; that's normal and must NOT log as an
    uncaught exception."""
    import main

    async def _inner():
        async def _forever():
            await asyncio.sleep(1000)
        t = main.create_background_task(_forever(), name="test-forever")
        with caplog.at_level(logging.ERROR, logger="main"):
            await main._cancel_background_tasks()
            await asyncio.sleep(0)
        assert t not in main._BACKGROUND_TASKS
        # No error-level line for the cancel.
        for rec in caplog.records:
            assert "test-forever" not in rec.getMessage(), \
                f"cancellation should not log as error: {rec.getMessage()!r}"
    _run(_inner())


def test_cancel_background_tasks_cancels_all_outstanding():
    """Fire 5 long-running tasks, call _cancel_background_tasks, assert
    they all end up cancelled and the tracking set is empty."""
    import main

    async def _inner():
        tasks = []
        for i in range(5):
            async def _forever():
                await asyncio.sleep(1000)
            tasks.append(main.create_background_task(_forever(), name=f"test-f-{i}"))
        assert len(main._BACKGROUND_TASKS) >= 5
        await main._cancel_background_tasks()
        await asyncio.sleep(0)
        for t in tasks:
            assert t.cancelled() or t.done()
        # All our tasks cleared from the set.
        assert not any(t in main._BACKGROUND_TASKS for t in tasks)
    _run(_inner())


# ───────────────────── loop resilience ─────────────────────

def test_loop_body_exception_does_not_kill_the_loop():
    """Simulates the standard loop pattern: while True + inner try/except.
    An exception on one iteration must not cause the whole loop to exit;
    subsequent iterations must still run."""
    import main

    async def _inner():
        ticks = {"n": 0, "exceptions": 0}

        async def _loop():
            # Mirrors the shape of rss_loop/status_loop/etc.
            while True:
                try:
                    ticks["n"] += 1
                    if ticks["n"] == 1:
                        raise ValueError("simulated failure")
                    if ticks["n"] >= 3:
                        return   # test-only exit after 3 iterations
                except Exception:
                    ticks["exceptions"] += 1
                await asyncio.sleep(0.001)

        t = main.create_background_task(_loop(), name="test-loop")
        await asyncio.wait_for(t, timeout=2.0)

        assert ticks["n"] == 3, f"loop stopped at tick {ticks['n']}"
        assert ticks["exceptions"] == 1
        assert t not in main._BACKGROUND_TASKS
    _run(_inner())


# ───────────────────── lifespan-side smoke check ─────────────────────

def test_expected_loop_names_are_in_source():
    """Guard: every long-running loop the lifespan is supposed to register
    must appear as a create_background_task(...) call in main.py. Catches
    regressions where a loop gets added without registration."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[2] / "app" / "main.py").read_text()
    expected_names = {
        "rss_loop",
        "status_loop",
        "refresh_ongoing_loop",
        "metadata_retry_loop",
        "backfill_metadata_loop",
        "backlog_search_loop",
        "suwayomi_monitor_loop",
        "rescan_loop",
        "import_list_loop",
        "backup_loop",
    }
    for name in expected_names:
        needle = f'name="{name}"'
        assert needle in src, f"expected lifespan to register {needle}"


def test_lifespan_shutdown_calls_cancel_helper():
    """Guard: the old individual .cancel() calls are gone; lifespan now
    goes through _cancel_background_tasks so every loop (not just three)
    gets torn down."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[2] / "app" / "main.py").read_text()
    assert "_cancel_background_tasks()" in src
    # Old, partial cancellation pattern is gone.
    assert "_rss_task.cancel()" not in src
    assert "_status_task.cancel()" not in src
    assert "_refresh_task.cancel()" not in src


def test_router_handlers_do_not_spawn_untracked_asyncio_tasks():
    """HTTP-triggered jobs should use the tracked background-task helper."""
    import pathlib

    routers_dir = pathlib.Path(__file__).resolve().parents[2] / "app" / "routers"
    offenders = []
    for path in routers_dir.glob("*.py"):
        if "asyncio.create_task" in path.read_text():
            offenders.append(path.name)

    assert offenders == [], (
        "route modules must use create_background_task(), not raw "
        f"asyncio.create_task(): {offenders}"
    )
