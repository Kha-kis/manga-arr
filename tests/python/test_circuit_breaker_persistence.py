"""PR 4a: circuit breaker state for download clients is persisted to
client_breaker_state instead of living only in a module-level dict.
A tripped breaker now survives app restart, so a known-bad client
can't silently retry the moment the container comes back up."""
import os
import sqlite3
import sys
import tempfile
import asyncio
from unittest.mock import patch

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


@pytest.fixture
def env():
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-cb-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()

    # Seed a download_clients row so FK constraint passes
    with sqlite3.connect(db.name) as c:
        c.execute(
            "INSERT INTO download_clients(id, name, type, enabled)"
            " VALUES(1, 'qBit', 'qbittorrent', 1)"
        )
        c.execute(
            "INSERT INTO download_clients(id, name, type, enabled)"
            " VALUES(2, 'SAB', 'sabnzbd', 1)"
        )

    try:
        yield db.name
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


def _breaker_row(db_path: str, client_id: int) -> dict | None:
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        r = c.execute(
            "SELECT failures, open_until FROM client_breaker_state WHERE client_id=?",
            (client_id,)
        ).fetchone()
        return dict(r) if r else None


def test_failures_increment_and_persist(env):
    from routers.download_clients import _cb_record_failure
    assert _breaker_row(env, 1) is None
    _cb_record_failure(1)
    r = _breaker_row(env, 1)
    assert r is not None and r['failures'] == 1
    _cb_record_failure(1)
    r = _breaker_row(env, 1)
    assert r['failures'] == 2


def test_third_failure_opens_breaker(env):
    from routers.download_clients import _cb_record_failure, _cb_is_open
    for _ in range(3):
        _cb_record_failure(1)
    assert _cb_is_open(1) is True
    r = _breaker_row(env, 1)
    assert r['failures'] >= 3
    assert r['open_until'] > 0


def test_success_clears_the_breaker(env):
    from routers.download_clients import _cb_record_failure, _cb_record_success
    _cb_record_failure(1)
    _cb_record_failure(1)
    assert _breaker_row(env, 1)['failures'] == 2
    _cb_record_success(1)
    assert _breaker_row(env, 1) is None


def test_breaker_state_survives_fresh_module_import(env):
    """Simulates an app restart: trip the breaker, drop the cached
    module reference, re-import, and verify the state is still there."""
    from routers.download_clients import _cb_record_failure, _CB_THRESHOLD
    for _ in range(_CB_THRESHOLD):
        _cb_record_failure(1)

    # Simulate restart by re-importing the module
    import importlib
    from routers import download_clients as dc
    importlib.reload(dc)

    assert dc._cb_is_open(1) is True, (
        "breaker state lost across reload — persistence failed"
    )


def test_multiple_clients_tracked_independently(env):
    from routers.download_clients import _cb_record_failure, _cb_is_open
    _cb_record_failure(1)
    _cb_record_failure(1)
    _cb_record_failure(1)
    assert _cb_is_open(1) is True
    assert _cb_is_open(2) is False


def test_half_open_after_timeout_does_not_immediately_reopen(env):
    """When the timeout elapses, the breaker transitions to half-open
    (failures = threshold - 1) instead of staying open. Verified by
    backdating open_until and checking _cb_is_open returns False."""
    import time
    from routers.download_clients import _cb_record_failure, _cb_is_open, _CB_THRESHOLD
    for _ in range(_CB_THRESHOLD):
        _cb_record_failure(1)
    # Force the open_until into the past
    with sqlite3.connect(env) as c:
        c.execute(
            "UPDATE client_breaker_state SET open_until=? WHERE client_id=1",
            (time.time() - 1,)
        )
    assert _cb_is_open(1) is False
    # And next failure still opens (threshold-1 + 1 = threshold)
    _cb_record_failure(1)
    assert _cb_is_open(1) is True


def test_open_circuit_event_opts_into_log_dedup(env):
    import clients
    from routers.download_clients import _cb_record_failure, _CB_THRESHOLD

    for _ in range(_CB_THRESHOLD):
        _cb_record_failure(2)

    with patch("clients.log_event") as log_event:
        result = asyncio.run(clients.grab_url("http://indexer/release.nzb", "nzb"))

    assert result == (False, "SAB", None, False)
    log_event.assert_called_once_with(
        "error",
        "[grab_url] Circuit open for client SAB — skipping grab",
        dedup=True,
    )
