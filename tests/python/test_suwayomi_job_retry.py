"""PR 2c: Suwayomi job processing now retries transient failures
up to 3 times before marking the job 'error'. Prior behaviour set
status='error' on the first exception, stranding user-initiated DDL
grabs after a brief network hiccup."""
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
def env():
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-swy-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()

    # Series + chapter + queued suwayomi_download
    with sqlite3.connect(db.name) as c:
        c.execute(
            "INSERT INTO series(id, title, search_pattern, enabled, monitored)"
            " VALUES(88, 'SwyRetry', 'SwyRetry', 1, 1)"
        )
        c.execute(
            "INSERT INTO suwayomi_downloads"
            "(series_id, suwayomi_manga_id, volume_num, chapter_num, chapter_ids,"
            " status, progress) VALUES(88, 101, 1.0, NULL, '[1,2,3]', 'queued', 0)"
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


def _job_status(db_path, job_id=1):
    with sqlite3.connect(db_path) as c:
        r = c.execute(
            "SELECT status, error FROM suwayomi_downloads WHERE id=?", (job_id,)
        ).fetchone()
    return r


def test_transient_failure_then_success_does_not_error(env):
    """First two invocations of _process_suwayomi_job raise; third
    succeeds. The outer retry loop must complete without marking error."""
    from routers import suwayomi_ as s

    attempts = {'n': 0}

    async def _flaky(c, job):
        attempts['n'] += 1
        if attempts['n'] < 3:
            raise RuntimeError(f"transient #{attempts['n']}")
        # success path — mark completed so the loop thinks it's done
        import sqlite3 as _s
        with _s.connect(env) as db:
            db.execute(
                "UPDATE suwayomi_downloads SET status='completed' WHERE id=?",
                (job['id'],)
            )

    async def _no_sleep(*a, **kw):
        return None

    # Bypass get_suwayomi_client — we only need any truthy dict
    with patch.object(s, '_process_suwayomi_job', _flaky), \
         patch.object(s, 'get_suwayomi_client', lambda db: {'base': 'http://x'}), \
         patch('routers.suwayomi_._aio.sleep', _no_sleep):
        asyncio.run(s._check_suwayomi_jobs_impl())

    assert attempts['n'] == 3
    status, err = _job_status(env)
    assert status == 'completed'
    assert err is None


def test_exhausted_retries_mark_error(env):
    """All three attempts fail → status='error' with the last exception."""
    from routers import suwayomi_ as s

    attempts = {'n': 0}

    async def _always_fail(c, job):
        attempts['n'] += 1
        raise RuntimeError(f"boom #{attempts['n']}")

    async def _no_sleep(*a, **kw):
        return None

    with patch.object(s, '_process_suwayomi_job', _always_fail), \
         patch.object(s, 'get_suwayomi_client', lambda db: {'base': 'http://x'}), \
         patch('routers.suwayomi_._aio.sleep', _no_sleep):
        asyncio.run(s._check_suwayomi_jobs_impl())

    assert attempts['n'] == 3, f"expected exactly 3 attempts, got {attempts['n']}"
    status, err = _job_status(env)
    assert status == 'error'
    assert err is not None and 'boom' in err


def test_retry_failed_volume_restores_grabbed_metadata(env):
    """Manual retry should leave a volume in verifier-clean grabbed state."""
    from routers import suwayomi_ as s

    async def _fake_gql(*a, **kw):
        return {"ok": True}

    with sqlite3.connect(env) as c:
        c.execute(
            "UPDATE suwayomi_downloads SET status='error', error='old', progress=1"
            " WHERE id=1"
        )
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status)"
            " VALUES(88, 1.0, 'wanted')"
        )

    with patch.object(s, '_gql', _fake_gql), \
         patch.object(s, 'get_suwayomi_client', lambda db: {'base': 'http://x'}):
        resp = asyncio.run(s.retry_suwayomi_job(1))

    assert resp.status_code == 200
    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        job = c.execute(
            "SELECT status, error, progress FROM suwayomi_downloads WHERE id=1"
        ).fetchone()
        vol = c.execute(
            "SELECT status, grabbed_at, source_url, torrent_name, indexer,"
            " protocol, client FROM volumes WHERE series_id=88 AND volume_num=1.0"
        ).fetchone()

    assert job["status"] == "queued"
    assert job["error"] is None
    assert job["progress"] == 0
    assert vol["status"] == "grabbed"
    assert vol["grabbed_at"]
    assert vol["source_url"] == "suwayomi:101"
    assert vol["torrent_name"] == "Suwayomi DDL: vol 1"
    assert vol["indexer"] == "Suwayomi"
    assert vol["protocol"] == "ddl"
    assert vol["client"] == "suwayomi"


def test_retry_failed_chapter_restores_grabbed_metadata(env):
    """Manual retry should leave a chapter in verifier-clean grabbed state."""
    from routers import suwayomi_ as s

    async def _fake_gql(*a, **kw):
        return {"ok": True}

    with sqlite3.connect(env) as c:
        c.execute(
            "UPDATE suwayomi_downloads"
            " SET volume_num=NULL, chapter_num=5.0, status='error',"
            " error='old', progress=1 WHERE id=1"
        )
        c.execute(
            "INSERT INTO chapters(series_id, chapter_num, status)"
            " VALUES(88, 5.0, 'grabbed')"
        )

    with patch.object(s, '_gql', _fake_gql), \
         patch.object(s, 'get_suwayomi_client', lambda db: {'base': 'http://x'}):
        resp = asyncio.run(s.retry_suwayomi_job(1))

    assert resp.status_code == 200
    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        job = c.execute(
            "SELECT status, error, progress FROM suwayomi_downloads WHERE id=1"
        ).fetchone()
        ch = c.execute(
            "SELECT status, grabbed_at, torrent_url, torrent_name, indexer,"
            " protocol, client FROM chapters WHERE series_id=88 AND chapter_num=5.0"
        ).fetchone()

    assert job["status"] == "queued"
    assert job["error"] is None
    assert job["progress"] == 0
    assert ch["status"] == "grabbed"
    assert ch["grabbed_at"]
    assert ch["torrent_url"] == "suwayomi:101"
    assert ch["torrent_name"] == "Suwayomi DDL: ch 5"
    assert ch["indexer"] == "Suwayomi"
    assert ch["protocol"] == "ddl"
    assert ch["client"] == "suwayomi"
