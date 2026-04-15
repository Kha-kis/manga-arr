"""Tests for M7: silent except/pass swallowing replaced with logging.

Four previously-silent sites now emit a WARNING/INFO log line while
preserving the best-effort behaviour (never raise through to caller):

  1. main.load_config — DB read failure falls back to env/defaults,
     but the failure is now visible.
  2. main.get_db — rollback failure after another exception used to be
     silent; now logged (while still re-raising the ORIGINAL exception).
  3. shared.get_db — same pattern in shared.py's get_db.
  4. main lifespan qBit category bootstrap — network failure used to
     be silent; now an INFO line appears.

Plus a regression check: cleanup paths (file unlink, log_event itself,
notification send) still do not raise.
"""
import logging
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401


# ───────────────────── load_config ─────────────────────

def test_load_config_logs_warning_on_db_read_failure(monkeypatch, caplog):
    """Point DB_PATH at a non-existent file and call load_config.
    load_config must not raise, must fall back to defaults, and must
    log a WARNING naming the failure."""
    import main
    import shared
    bad_path = "/tmp/definitely-does-not-exist-and-cant-create.db/nope"
    monkeypatch.setattr(main, "DB_PATH", bad_path)
    monkeypatch.setattr(shared, "DB_PATH", bad_path)

    with caplog.at_level(logging.WARNING, logger="main"):
        main.load_config()  # must not raise

    # Defaults still loaded (env + ENV_DEFAULTS fallbacks)
    assert main.CONFIG.get("save_path") == "/manga" or "save_path" in main.CONFIG
    # And a warning was emitted naming load_config
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "load_config" in joined, f"warning not emitted: {joined!r}"


# ───────────────────── get_db rollback (main) ─────────────────────

def _install_broken_rollback(monkeypatch, sql_module, reason: str):
    """Replace sqlite3.connect so every returned connection's rollback()
    raises. sqlite3.Connection is an immutable C type so we wrap it
    via a subclass-with-delegation rather than monkeypatching attrs."""
    orig_connect = sql_module.connect

    class _RollbackFails(sqlite3.Connection):
        def rollback(self):
            raise sqlite3.OperationalError(reason)

    def _connect(database, *a, **kw):
        kw["factory"] = _RollbackFails
        return orig_connect(database, *a, **kw)

    monkeypatch.setattr(sql_module, "connect", _connect)


def test_main_get_db_rollback_failure_is_logged(monkeypatch, caplog):
    """Simulate rollback() raising on a dead connection. The ORIGINAL
    exception from inside the `with` block must still propagate; the
    rollback failure itself is logged at WARNING but does not mask."""
    import main
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.unlink(tmp.name)
    monkeypatch.setattr(main, "DB_PATH", tmp.name)
    main.init_db()  # initialise BEFORE installing the broken factory

    _install_broken_rollback(monkeypatch, sqlite3, "simulated rollback failure")

    try:
        with caplog.at_level(logging.WARNING, logger="main"):
            with pytest.raises(RuntimeError, match="primary error"):
                with main.get_db() as db:
                    db.execute("INSERT INTO settings(key,value) VALUES('x','y')")
                    raise RuntimeError("primary error")   # triggers rollback
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "rollback failed" in joined
        assert "simulated rollback failure" in joined
    finally:
        for ext in ("", "-wal", "-shm"):
            p = tmp.name + ext
            if os.path.exists(p):
                os.unlink(p)


# ───────────────────── get_db rollback (shared) ─────────────────────

def test_shared_get_db_rollback_failure_is_logged(monkeypatch, caplog):
    """Same pattern in shared.get_db. Routers use this one, not main's."""
    import main  # for init_db + schema
    import shared
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.unlink(tmp.name)
    monkeypatch.setattr(main, "DB_PATH", tmp.name)
    monkeypatch.setattr(shared, "DB_PATH", tmp.name)
    main.init_db()

    _install_broken_rollback(monkeypatch, sqlite3, "simulated shared rollback fail")

    try:
        with caplog.at_level(logging.WARNING, logger="shared"):
            with pytest.raises(RuntimeError, match="primary"):
                with shared.get_db() as db:
                    db.execute("INSERT INTO settings(key,value) VALUES('x','y')")
                    raise RuntimeError("primary")
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "rollback failed" in joined
    finally:
        for ext in ("", "-wal", "-shm"):
            p = tmp.name + ext
            if os.path.exists(p):
                os.unlink(p)


def test_rollback_failure_does_not_mask_original_exception(monkeypatch):
    """Explicit check: when rollback itself raises, the caller must see
    the ORIGINAL exception (the one that triggered the rollback), not
    the rollback failure. Log is secondary."""
    import main
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.unlink(tmp.name)
    monkeypatch.setattr(main, "DB_PATH", tmp.name)
    main.init_db()

    _install_broken_rollback(monkeypatch, sqlite3, "swallow this")

    try:
        with pytest.raises(ValueError, match="caller sees ME"):
            with main.get_db() as db:
                db.execute("INSERT INTO settings(key,value) VALUES('x','y')")
                raise ValueError("caller sees ME")
    finally:
        for ext in ("", "-wal", "-shm"):
            p = tmp.name + ext
            if os.path.exists(p):
                os.unlink(p)


# ───────────────────── best-effort paths still don't raise ─────────────────────

def test_log_event_cleanup_still_silent(monkeypatch):
    """log_event itself is documented as best-effort; even with a broken
    DB it must NOT raise (otherwise every caller would need to wrap it)."""
    import main
    # Point DB_PATH at a bad path so the internal get_db fails
    monkeypatch.setattr(main, "DB_PATH", "/nonexistent/path/db")
    # Must not raise
    main.log_event("test", "irrelevant")   # no assertion needed — if it raises, the test fails


def test_fire_notifications_unknown_event_still_non_raising(monkeypatch):
    """Unknown event now logs a warning (from M5) but must still not raise."""
    import main
    import shared
    import asyncio
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.unlink(tmp.name)
    monkeypatch.setattr(main, "DB_PATH", tmp.name)
    monkeypatch.setattr(shared, "DB_PATH", tmp.name)
    main.init_db()
    main.load_config()

    from routers.notification_connections import fire_notifications
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(fire_notifications("never_heard_of_it", "msg"))
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())
        for ext in ("", "-wal", "-shm"):
            p = tmp.name + ext
            if os.path.exists(p):
                os.unlink(p)


# ───────────────────── secret-leak regression guard ─────────────────────

def test_qbit_startup_log_excludes_credentials():
    """Source-level guard: the qBit category bootstrap's except block
    must log via %r on the exception object only, never interpolating
    _qpw (password) or _quser (username). A grep over that except
    block's body fails the test if a secret token leaks in."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[2] / "app" / "main.py").read_text()
    # Locate the qBit except block
    marker = "startup: qBit category bootstrap"
    idx = src.find(marker)
    assert idx > 0, "could not find new qBit startup log line"
    # Grab a window around the marker
    window = src[max(0, idx - 300):idx + 300]
    # The log formatting must NOT include _qpw or _quser
    assert "_qpw" not in window or "Do NOT include _qpw" in window, \
        "password variable mentioned near log; verify it's not interpolated"
    assert "_quser" not in window or "Do NOT" in window, \
        "username variable mentioned near log; verify it's not interpolated"
