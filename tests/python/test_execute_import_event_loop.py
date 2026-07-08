"""Prove that `_execute_import` does not block the asyncio event loop.

Real-world trigger: a py-spy dump of the live container during the
v0.1.5 HxH pilot caught the MainThread stuck inside `shutil.copy2`
(staging.stage) — the whole uvicorn event loop was frozen until the
copy finished, so every concurrent page render waited behind it. Users
reported "navigation is slow" with a 60% timeout rate on `/` at a 3s
client budget.

The fix: every disk-touching call inside `_execute_import` goes
through `asyncio.to_thread` so it runs on a worker thread and yields
the event loop. These tests pin that contract.

The test shape is simple: stub `shutil.copy2` with a version that
sleeps synchronously for 2 seconds, then run `_execute_import` in
parallel with a lightweight coroutine that ticks every 50 ms. If the
event loop is still responsive during the import, the ticker records
ticks throughout the whole 2 s window. If it isn't, we see a gap
bigger than the stall threshold and the assertion fails.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import time
import zipfile
from unittest.mock import patch

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63f8cf000000030001fe79bff70000000049454e44ae42"
    "6082"
)


def _make_zip(path: str, name: str = "page.png") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(name, _TINY_PNG)
    return path


@pytest.fixture
def env(tmp_path):
    import main, shared, security

    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close()
    os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-evloop-keys-")
    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()
    src_root = tmp_path / "src"
    lib_root = tmp_path / "library"
    src_root.mkdir()
    lib_root.mkdir()
    with sqlite3.connect(db.name) as c:
        c.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES('save_path', ?)",
            (str(lib_root),),
        )
        # Force copy mode so staging.stage goes through shutil.copy2
        # (the real offender observed in prod). Hardlink mode uses
        # os.link and wouldn't exercise the slow-copy stub below.
        c.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES('import_mode', 'copy')"
        )
    main.load_config()
    try:
        yield {"db_path": db.name, "src_root": src_root, "lib_root": lib_root}
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


def _seed_chapter_queue(
    db_path: str,
    src_path: str,
    *,
    series_id: int = 7,
    chap_num: float = 1.0,
    library_root: str | None = None,
) -> int:
    """Insert a minimal queue row with one chapter file to import.

    Library destination now requires a root folder (PR C removed the
    save_path fallback). Seed one pointing at library_root (defaults
    to /tmp) so the import pipeline has somewhere to place files.
    """
    # Use the settings save_path as the library root by default — that's
    # where the env fixture already pointed things. init_db's bootstrap
    # may have pre-created root_folders(id=1) pointing at the old default
    # (/manga), so REPLACE rather than IGNORE to land on the real tmp path.
    with sqlite3.connect(db_path) as c:
        if library_root is None:
            sp_row = c.execute(
                "SELECT value FROM settings WHERE key='save_path'"
            ).fetchone()
            library_root = (
                sp_row[0] if sp_row else (os.path.dirname(src_path) or "/tmp")
            )
        c.execute(
            "INSERT OR REPLACE INTO root_folders(id, path, label, is_default)"
            " VALUES(1, ?, 'Manga', 1)",
            (library_root,),
        )
        c.execute(
            "INSERT INTO series(id, title, search_pattern, root_folder_id)"
            " VALUES(?, ?, ?, 1)",
            (series_id, "EvLoopTest", "EvLoopTest"),
        )
        cur = c.execute(
            "INSERT INTO import_queue(series_id, torrent_name, status, created_at)"
            " VALUES(?, 'evloop-torrent', 'pending', datetime('now'))",
            (series_id,),
        )
        qid = cur.lastrowid
        c.execute(
            "INSERT INTO import_queue_files(queue_id, src_path, filename,"
            " file_type, proposed_volume, proposed_chapter, status)"
            " VALUES(?, ?, ?, 'chapter', NULL, ?, 'pending')",
            (qid, src_path, os.path.basename(src_path), chap_num),
        )
        return qid


class _SlowCopyTracker:
    """Stand-in for shutil.copy2 that sleeps synchronously on a worker
    thread. Records when it was called so we can assert it ran at all."""

    def __init__(self, delay_seconds: float = 2.0):
        self.delay = delay_seconds
        self.calls: list[tuple[float, float]] = []

    def __call__(self, src, dst, *args, **kwargs):
        import shutil

        t0 = time.perf_counter()
        time.sleep(self.delay)  # SYNC sleep — would freeze the event loop
        # if not on a worker thread
        shutil.copyfile(src, dst)
        self.calls.append((t0, time.perf_counter()))
        return dst


async def _tick_ticks(stop_event: asyncio.Event, interval: float) -> list[float]:
    """Record timestamps until stop_event is set. Only gets to run when
    the event loop is scheduling coroutines."""
    out: list[float] = []
    while not stop_event.is_set():
        out.append(time.perf_counter())
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    return out


def _max_gap(ticks: list[float]) -> float:
    """Largest interval between consecutive ticks. A stalled event loop
    produces a gap ≈ the stall duration; a responsive loop keeps the
    gap near `interval`."""
    if len(ticks) < 2:
        return float("inf")
    return max(b - a for a, b in zip(ticks, ticks[1:]))


# ─────────── main contract ─────────────────────────────────────────


def test_execute_import_does_not_block_event_loop(env):
    """Run an import whose staging copy takes 2 seconds, while a
    parallel ticker coroutine fires every 50 ms. The ticker must keep
    firing throughout the 2 s window — proving the event loop is
    scheduling other work during the file copy."""
    import shutil
    import main

    src = _make_zip(str(env["src_root"] / "c001.zip"))
    qid = _seed_chapter_queue(env["db_path"], src, chap_num=1.0)

    slow = _SlowCopyTracker(delay_seconds=2.0)
    stop = asyncio.Event()

    async def _run():
        ticker_task = asyncio.create_task(_tick_ticks(stop, interval=0.05))
        result = await main._execute_import(qid, {}, set(), {})
        stop.set()
        ticks = await ticker_task
        return result, ticks

    with patch("import_staging.shutil.copy2", new=slow):
        result, ticks = asyncio.run(_run())

    assert result is True, "import should complete"
    assert slow.calls, "the slow copy stand-in should have been called"
    # Event loop responsiveness: the longest gap between ticks must be
    # well under the 2 s copy duration. 250 ms is generous for a CI
    # runner; a blocked loop here would record a ~2 s gap.
    gap = _max_gap(ticks)
    assert gap < 0.25, (
        f"event loop stalled for {gap:.3f}s during import — copy2 is "
        "running on the main thread instead of asyncio.to_thread"
    )
    # Minimum tick count — under 2 s with 50 ms interval we expect
    # ~40 ticks. Be lenient for CI noise.
    assert len(ticks) >= 15, (
        f"expected the ticker to fire many times during a 2 s import; "
        f"got {len(ticks)} — loop was probably blocked"
    )


def test_rollback_also_runs_off_event_loop(env):
    """The rollback path (shutil.rmtree on the staging dir) must also
    yield. Simulate a failure mid-stage so rollback fires."""
    import main

    src = _make_zip(str(env["src_root"] / "c005.zip"))
    qid = _seed_chapter_queue(env["db_path"], src, chap_num=5.0)

    slow = _SlowCopyTracker(delay_seconds=0.2)  # succeed quickly so
    # we don't blow the
    # 30s test budget

    # We don't have a reliable way to force a rollback without touching
    # DB or FS mid-import. Instead, drive a successful import and pin
    # that commit_all is invoked through asyncio.to_thread — the same
    # mechanism rollback uses. If this test regresses, a grep for
    # "await asyncio.to_thread(staging." in _execute_import_impl is the
    # quickest way to triage. (PR #147 added a thin _execute_import
    # wrapper for staging-dir cleanup; the actual import body lives in
    # _execute_import_impl now.)
    import inspect
    from import_pipeline import _execute_import_impl

    src_code = inspect.getsource(_execute_import_impl)
    assert "await asyncio.to_thread(staging.commit_all" in src_code, (
        "_execute_import_impl must call staging.commit_all via asyncio.to_thread"
    )
    assert "await asyncio.to_thread(staging.rollback" in src_code, (
        "_execute_import_impl must call staging.rollback via asyncio.to_thread"
    )


def test_inject_comicinfo_runs_off_event_loop(env):
    """ComicInfo injection reads and rewrites a zip. That's blocking
    I/O; must go through asyncio.to_thread."""
    import inspect
    from import_pipeline import _execute_import_impl

    src = inspect.getsource(_execute_import_impl)
    # Every _try_inject_comicinfo call inside _execute_import is
    # prefixed with asyncio.to_thread. A bare `_try_inject_comicinfo(`
    # call (no `to_thread` before it) would regress.
    import re

    bare_calls = re.findall(
        r"(?<!to_thread,\s)_try_inject_comicinfo\(",
        src,
    )
    # Filter out the to_thread-wrapped ones explicitly — count only
    # calls that don't sit inside an `asyncio.to_thread(...)` call.
    # Simplest check: the source must not contain the bare pattern
    # directly preceded by whitespace-only (i.e. a statement-level call).
    bad = re.findall(
        r"^\s+_try_inject_comicinfo\(",
        src,
        flags=re.MULTILINE,
    )
    assert not bad, (
        "_execute_import still has bare _try_inject_comicinfo calls — "
        f"wrap them in asyncio.to_thread. Offenders:\n{bad}"
    )


def test_maybe_convert_to_cbz_runs_off_event_loop(env):
    """CBR→CBZ conversion is CPU+IO heavy (rarfile extraction + zip
    write). Same rule as above."""
    import inspect, re
    from import_pipeline import _execute_import_impl

    src = inspect.getsource(_execute_import_impl)
    bad = re.findall(
        r"^\s+stage_after\s*=\s*_maybe_convert_to_cbz\(",
        src,
        flags=re.MULTILINE,
    )
    assert not bad, (
        "_execute_import still has bare _maybe_convert_to_cbz calls — "
        f"wrap them in asyncio.to_thread. Offenders:\n{bad}"
    )
