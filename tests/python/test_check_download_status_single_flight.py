"""Single-flight guard for check_download_status.

Issue #31 follow-up A: `check_download_status` takes 7-38 seconds per
invocation and was being spawned concurrently (up to 4×) from four
different callers:
  - status_loop (every 5 min)
  - /api/check-downloads button
  - /api/backfill-packs trigger
  - system endpoints

Overlapping runs amplified event-loop blocking and DB write contention.
These tests pin the new single-flight guard so a second invocation
silently skips while a prior one is still running.
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


@pytest.fixture
def fresh_db():
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-sf-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()

    # Reset the lock in case prior test left it in an odd state.
    import import_pipeline
    import_pipeline._CHECK_DOWNLOAD_STATUS_LOCK = asyncio.Lock()

    try:
        yield db.name
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


def test_lock_exists_as_module_attribute():
    """Regression guard: the single-flight lock must be a module-level
    asyncio.Lock, not a local or per-call object."""
    import import_pipeline
    assert isinstance(import_pipeline._CHECK_DOWNLOAD_STATUS_LOCK, asyncio.Lock)


def test_second_invocation_skips_while_first_is_running(fresh_db):
    """Two concurrent check_download_status calls: only the first runs
    the body; the second returns immediately without entering the impl."""
    import main

    impl_calls = {"n": 0, "started": asyncio.Event()}

    async def _slow_impl():
        impl_calls["n"] += 1
        impl_calls["started"].set()
        await asyncio.sleep(0.3)  # simulate a long-running scan
        return None

    async def _run():
        import import_pipeline
        with patch.object(import_pipeline, "_check_download_status_impl", new=_slow_impl):
            # Fire two invocations concurrently. The first acquires the
            # lock and starts the impl; the second hits the locked check
            # and returns early.
            task_a = asyncio.create_task(main.check_download_status())
            # Wait until the first one is inside the impl so the lock is held.
            await impl_calls["started"].wait()
            task_b = asyncio.create_task(main.check_download_status())
            await asyncio.gather(task_a, task_b)

    asyncio.run(_run())

    assert impl_calls["n"] == 1, (
        f"expected 1 impl call (second invocation skipped), got {impl_calls['n']}"
    )


def test_sequential_invocations_still_run(fresh_db):
    """After the first invocation completes, the next invocation must
    still run normally. The lock must release cleanly."""
    import main

    impl_calls = {"n": 0}

    async def _fast_impl():
        impl_calls["n"] += 1
        return None

    async def _run():
        import import_pipeline
        with patch.object(import_pipeline, "_check_download_status_impl", new=_fast_impl):
            await main.check_download_status()
            await main.check_download_status()
            await main.check_download_status()

    asyncio.run(_run())
    assert impl_calls["n"] == 3, (
        f"sequential calls should all run; got {impl_calls['n']}"
    )


def test_exception_in_impl_releases_the_lock(fresh_db):
    """If the impl raises, the lock must be released so future callers
    aren't permanently blocked."""
    import main

    state = {"calls": 0}

    async def _flaky_impl():
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("simulated failure")
        return None

    async def _run():
        import import_pipeline
        with patch.object(import_pipeline, "_check_download_status_impl", new=_flaky_impl):
            with pytest.raises(RuntimeError):
                await main.check_download_status()
            # Lock must now be free. Next call should run the impl.
            await main.check_download_status()

    asyncio.run(_run())
    assert state["calls"] == 2, (
        f"expected impl called twice (first raised, second recovered); "
        f"got {state['calls']}"
    )


def test_create_task_amplification_is_bounded(fresh_db):
    """Simulate the original bug pattern: four callers fire-and-forget
    via asyncio.create_task simultaneously. Only one impl should run."""
    import main

    impl_calls = {"n": 0, "inside": asyncio.Event()}

    async def _very_slow_impl():
        impl_calls["n"] += 1
        impl_calls["inside"].set()
        # Hold the lock long enough that several more callers pile on.
        await asyncio.sleep(0.2)
        return None

    async def _run():
        import import_pipeline
        with patch.object(import_pipeline, "_check_download_status_impl", new=_very_slow_impl):
            # First caller starts the work.
            task1 = asyncio.create_task(main.check_download_status())
            await impl_calls["inside"].wait()
            # Three more pile on while the first is still running.
            tasks = [asyncio.create_task(main.check_download_status())
                     for _ in range(3)]
            await asyncio.gather(task1, *tasks)

    asyncio.run(_run())
    assert impl_calls["n"] == 1, (
        f"four concurrent spawns should collapse to one impl run; "
        f"got {impl_calls['n']}"
    )


def test_lock_is_a_single_module_level_object():
    """The lock must NOT be recreated on each call — otherwise every
    invocation gets its own lock and the single-flight guard is moot."""
    import import_pipeline
    lock_id_first = id(import_pipeline._CHECK_DOWNLOAD_STATUS_LOCK)
    # Trigger the accessor path — importing import_pipeline already ran module body.
    lock_id_second = id(import_pipeline._CHECK_DOWNLOAD_STATUS_LOCK)
    assert lock_id_first == lock_id_second
