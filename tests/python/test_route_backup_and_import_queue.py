"""HTTP-level integration tests for backup/restore and import queue actions.

These were the last gaps in the production-readiness coverage matrix:
  - Backup is disaster-recovery code. If silently broken, you find out
    at the worst possible moment (during a real recovery). Solo daily
    use never surfaces it.
  - Import queue actions (skip, dismiss, retry) are buttons in the
    manual-import review UI. The audit specifically called this "the
    most failure-prone path for new users."
"""
import io
import os
import sqlite3
import sys
import tempfile
import zipfile

import pytest

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401


@pytest.fixture
def env(tmp_path):
    """Fresh DB + seeded series + import_queue with one pending and one
    failed entry. The pending one has linked grabbed volumes for the
    dismiss-clears-state test."""
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-bkp-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()
    main.ensure_api_key()

    library_root = tmp_path / "library"
    library_root.mkdir()
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    # Override BACKUP_DIR so tests don't try to write to /config — conftest's
    # /config redirect only patches os.makedirs/isdir, not open(), so the
    # save-copy step fails without this. Also override the DB_PATH the
    # backup-zip code uses (it imported a stale module-load value of
    # shared.DB_PATH).
    import routers.system as _sys
    orig_backup_dir = _sys.BACKUP_DIR
    orig_sys_db = _sys.DB_PATH
    _sys.BACKUP_DIR = str(backup_dir)
    _sys.DB_PATH = db.name

    with sqlite3.connect(db.name) as c:
        c.execute("DELETE FROM root_folders")
        c.execute("INSERT INTO root_folders(id, path) VALUES(1, ?)", (str(library_root),))
        c.execute(
            "INSERT INTO series(id, title, search_pattern, edition_type, enabled,"
            " monitored, monitor_mode, root_folder_id)"
            " VALUES(1, 'IQSeries', 'IQSeries', 'standard', 1, 1, 'all', 1)"
        )
        c.execute(
            "INSERT INTO volumes(id, series_id, volume_num, status, monitored,"
            " download_id, source_url, torrent_name, indexer, protocol, client)"
            " VALUES(11, 1, 1.0, 'grabbed', 1, 'dl-pending', 'http://stub/p.torrent',"
            " 'IQSeries v01', 'Indexer', 'torrent', 'Qbit')"
        )
        c.execute(
            "INSERT INTO seen(torrent_url, series_id, volume_num, indexer, protocol, download_id)"
            " VALUES('http://stub/p.torrent', 1, 1.0, 'Indexer', 'torrent', 'dl-pending')"
        )
        c.execute(
            "INSERT INTO import_queue(id, series_id, download_id, torrent_name,"
            " torrent_url, status)"
            " VALUES(200, 1, 'dl-pending', 'IQSeries v01',"
            "        'http://stub/p.torrent', 'pending'),"
            "       (201, 1, 'dl-failed', 'IQSeries v02 [bad]',"
            "        'http://stub/f.torrent', 'failed'),"
            "       (202, 1, 'dl-skipped', 'IQSeries v03 [skipped]',"
            "        'http://stub/s.torrent', 'skipped')"
        )
        c.execute(
            "INSERT INTO import_queue_files(queue_id, src_path, status)"
            " VALUES(200, '/dl/p/v01.cbz', 'pending'),"
            "       (201, '/dl/f/v02.cbz', 'failed'),"
            "       (201, '/dl/f/v02-extra.cbz', 'needs_review'),"
            "       (202, '/dl/s/v03.cbz', 'skipped')"
        )

    try:
        yield {
            'db_path': db.name,
            'library_root': str(library_root),
            'backup_dir': str(backup_dir),
        }
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        _sys.BACKUP_DIR = orig_backup_dir
        _sys.DB_PATH = orig_sys_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


def _client():
    import main
    from fastapi.testclient import TestClient
    return TestClient(main.app)


def _csrf(tag: str = "test"):
    tok = f"csrf-{tag}-" + "x" * 30
    return {
        'cookies': {'csrftoken': tok},
        'headers': {'X-CSRFToken': tok},
    }


# ───────────────────── backup create ─────────────────────


def test_backup_page_renders_restore_readiness_and_validate_controls(env):
    import routers.system as _sys

    target = os.path.join(_sys.BACKUP_DIR, "mangarr_backup_20260101_000000.zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manga_arr.db", b"not-sqlite")

    r = _client().get("/system/backup")
    assert r.status_code == 200, r.text
    assert "Restore Readiness" in r.text
    assert "/api/system/backup/${encodeURIComponent(filename)}/validate" in r.text
    assert "Validate backup mangarr_backup_20260101_000000.zip" in r.text
    assert "/config/.mangarr-secret-key" in r.text
    assert "MANGARR_SECRET_KEY" in r.text


def test_backup_create_returns_valid_zip_with_db(env):
    """POST /api/system/backup/create must:
      1. Return a streaming zip response with the right filename pattern
      2. The zip must contain manga_arr.db
      3. The DB inside must be a valid sqlite file (queryable for series row)
    Silent-failure mode: returns a zip that's empty or unreadable; user
    'has backups' but they're useless when restoration is needed."""
    client = _client()
    csrf = _csrf("bkp-create")

    r = client.post("/api/system/backup/create", **csrf)
    assert r.status_code == 200, r.text
    assert r.headers.get("content-type", "").startswith("application/zip")

    cd = r.headers.get("content-disposition", "")
    assert "mangarr_backup_" in cd and ".zip" in cd, (
        f"filename header malformed: {cd!r}"
    )

    # Zip must be valid and contain the DB
    zip_buf = io.BytesIO(r.content)
    with zipfile.ZipFile(zip_buf, "r") as zf:
        names = zf.namelist()
        assert "manga_arr.db" in names, (
            f"backup must contain manga_arr.db, got {names!r}"
        )
        db_bytes = zf.read("manga_arr.db")

    # The extracted DB must be queryable and contain our seed row
    extracted = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    extracted.write(db_bytes)
    extracted.close()
    try:
        with sqlite3.connect(extracted.name) as c:
            row = c.execute(
                "SELECT title FROM series WHERE id=1"
            ).fetchone()
        assert row is not None, "extracted DB must be readable and contain seed row"
        assert row[0] == 'IQSeries'
    finally:
        os.unlink(extracted.name)


def test_backup_create_writes_copy_to_backup_dir(env):
    """The backup is also saved on disk so the user has a server-side
    copy (not just the streamed download). After one create, the backup
    directory should contain at least one .zip file."""
    import routers.system as _sys

    client = _client()
    csrf = _csrf("bkp-disk")

    # Snapshot the dir before
    before = set()
    if os.path.isdir(_sys.BACKUP_DIR):
        before = {f for f in os.listdir(_sys.BACKUP_DIR) if f.endswith(".zip")}

    r = client.post("/api/system/backup/create", **csrf)
    assert r.status_code == 200

    after = set()
    if os.path.isdir(_sys.BACKUP_DIR):
        after = {f for f in os.listdir(_sys.BACKUP_DIR) if f.endswith(".zip")}

    new = after - before
    assert len(new) == 1, (
        f"create must save exactly one new backup .zip to disk, got new files: {new!r}"
    )


# ───────────────────── backup validate ─────────────────────


def test_backup_validate_accepts_created_backup(env):
    import routers.system as _sys

    client = _client()
    csrf = _csrf("bkp-validate-created")
    create = client.post("/api/system/backup/create", **csrf)
    assert create.status_code == 200, create.text

    backups = [f for f in os.listdir(_sys.BACKUP_DIR) if f.endswith(".zip")]
    assert len(backups) == 1

    r = client.post(f"/api/system/backup/{backups[0]}/validate", **csrf)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["filename"] == backups[0]
    assert body["containsDatabase"] is True
    assert body["databaseValid"] is True
    assert "manga_arr.db" in body["entries"]


def test_backup_validate_rejects_non_zip_filename(env):
    r = _client().post(
        "/api/system/backup/passwords.txt/validate",
        **_csrf("bkp-validate-bad"),
    )
    assert r.status_code == 400
    assert r.json()["message"] == "Invalid filename"


def test_backup_validate_rejects_missing_backup(env):
    r = _client().post(
        "/api/system/backup/mangarr_backup_20990101_000000.zip/validate",
        **_csrf("bkp-validate-missing"),
    )
    assert r.status_code == 404
    assert r.json()["message"] == "Backup not found"


def test_backup_validate_rejects_malformed_zip(env):
    import routers.system as _sys

    target = os.path.join(_sys.BACKUP_DIR, "mangarr_backup_20260101_000000.zip")
    with open(target, "wb") as f:
        f.write(b"not a zip")

    r = _client().post(
        "/api/system/backup/mangarr_backup_20260101_000000.zip/validate",
        **_csrf("bkp-validate-malformed"),
    )
    assert r.status_code == 400
    assert r.json()["message"] == "Invalid ZIP file"


def test_backup_validate_rejects_zip_without_database(env):
    import routers.system as _sys

    target = os.path.join(_sys.BACKUP_DIR, "mangarr_backup_20260101_000000.zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("notes.txt", "missing db")

    r = _client().post(
        "/api/system/backup/mangarr_backup_20260101_000000.zip/validate",
        **_csrf("bkp-validate-no-db"),
    )
    assert r.status_code == 422
    body = r.json()
    assert body["containsDatabase"] is False
    assert body["databaseValid"] is False


# ───────────────────── backup delete ─────────────────────


def test_backup_delete_removes_file(env):
    """POST /api/system/backup/{filename}/delete must remove the file
    from disk."""
    import routers.system as _sys

    # Place a fake backup
    os.makedirs(_sys.BACKUP_DIR, exist_ok=True)
    target = os.path.join(_sys.BACKUP_DIR, "mangarr_backup_20260101_000000.zip")
    with open(target, "wb") as f:
        f.write(b"PK\x03\x04stub")

    assert os.path.exists(target)

    client = _client()
    csrf = _csrf("bkp-del")
    r = client.post(
        "/api/system/backup/mangarr_backup_20260101_000000.zip/delete",
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text
    assert not os.path.exists(target), "backup file must be deleted"


def test_backup_delete_rejects_non_zip_filename(env):
    """The delete handler refuses non-.zip filenames as a path-traversal /
    accidental-delete guard. POST returning 400 means the safety check
    is firing."""
    client = _client()
    csrf = _csrf("bkp-bad")

    r = client.post(
        "/api/system/backup/passwords.txt/delete",
        **csrf, follow_redirects=False,
    )
    assert r.status_code == 400, (
        f"non-.zip filename must be rejected (path-traversal guard), "
        f"got {r.status_code}: {r.text}"
    )


def test_backup_delete_missing_file_does_not_500(env):
    """Deleting a non-existent backup is silently OSError-swallowed and
    redirects cleanly. Catches the case where the user clicks delete
    twice in a row before the page refreshes."""
    client = _client()
    csrf = _csrf("bkp-missing")

    r = client.post(
        "/api/system/backup/never_existed.zip/delete",
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), (
        f"missing-file delete should redirect, got {r.status_code}: {r.text}"
    )


# ───────────────────── import queue: skip ─────────────────────


def test_import_skip_marks_pending_as_skipped(env):
    """POST /import/{id}/skip flips status='pending' → 'skipped' on the
    queue row AND every linked file row."""
    client = _client()
    csrf = _csrf("iq-skip")

    r = client.post("/import/200/skip", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        q = c.execute("SELECT status FROM import_queue WHERE id=200").fetchone()
        files_status = [
            r['status'] for r in c.execute(
                "SELECT status FROM import_queue_files WHERE queue_id=200"
            ).fetchall()
        ]
    assert q['status'] == 'skipped'
    assert all(s == 'skipped' for s in files_status), (
        f"all queue files must be marked skipped, got {files_status!r}"
    )


def test_import_skip_no_op_on_failed(env):
    """Skip is guarded to status IN ('pending','partial') — calling on
    a 'failed' row leaves it as failed. (Use clear-old to remove failed
    rows; skip is for cancelling pending work.)"""
    client = _client()
    csrf = _csrf("iq-skip-fail")

    r = client.post("/import/201/skip", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        q = c.execute("SELECT status FROM import_queue WHERE id=201").fetchone()
        file_status = c.execute(
            "SELECT status FROM import_queue_files WHERE queue_id=201"
        ).fetchone()[0]
    assert q[0] == 'failed', (
        "skip must NOT touch a 'failed' status row — 201 should still be failed"
    )
    assert file_status == 'failed'


# ───────────────────── import queue: dismiss ─────────────────────


def test_import_dismiss_resets_grabbed_volumes_and_clears_seen(env):
    """Dismiss is the strongest action: removes the queue rows AND
    resets any grabbed volumes back to 'wanted' AND clears the seen
    row so the URL can be re-grabbed.

    Silent-failure mode: queue row gone but the volume stays in
    'grabbed' status forever (zombie state) — user can't trigger a
    new search because the system thinks it's already grabbed."""
    client = _client()
    csrf = _csrf("iq-dismiss")

    r = client.post("/import/200/dismiss", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row

        # Queue row gone (and its files)
        q_gone = c.execute("SELECT 1 FROM import_queue WHERE id=200").fetchone()
        assert q_gone is None, "import_queue row must be deleted"
        f_count = c.execute(
            "SELECT COUNT(*) FROM import_queue_files WHERE queue_id=200"
        ).fetchone()[0]
        assert f_count == 0, "import_queue_files must cascade-delete"

        # Volume reset to wanted with cleared metadata
        v = c.execute(
            "SELECT status, download_id, source_url, indexer FROM volumes WHERE id=11"
        ).fetchone()
        assert v['status'] == 'wanted', f"volume must reset to wanted, got {v['status']!r}"
        assert v['download_id'] is None
        assert v['source_url'] is None
        assert v['indexer'] is None

        # Seen row cleared so the URL can be re-grabbed
        seen = c.execute(
            "SELECT 1 FROM seen WHERE torrent_url='http://stub/p.torrent'"
        ).fetchone()
        assert seen is None, (
            "seen row must be cleared on dismiss — otherwise the URL is "
            "permanently blocklisted from re-grab"
        )


# ───────────────────── import queue: retry ─────────────────────


def test_import_retry_resets_failed_to_pending(env):
    """POST /import/{id}/retry on a 'failed' row resets status='pending'
    AND resets failed/needs_review files back to 'pending'.

    Mocks _process_auto_import to a no-op — the handler dispatches it via
    asyncio.create_task and the test races against the async worker
    moving status from 'pending' → 'importing' → 'imported'."""
    from unittest.mock import patch
    import main

    async def _noop(*a, **kw):
        return None

    client = _client()
    csrf = _csrf("iq-retry")

    with patch.object(main, '_process_auto_import', _noop):
        r = client.post("/import/201/retry", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        q = c.execute("SELECT status FROM import_queue WHERE id=201").fetchone()
        # Both child files should be back to 'pending' (one was 'failed', one
        # 'needs_review'; both reset by the retry handler)
        files = sorted(
            r['status'] for r in c.execute(
                "SELECT status FROM import_queue_files WHERE queue_id=201"
            ).fetchall()
        )
    assert q['status'] == 'pending'
    assert files == ['pending', 'pending'], (
        f"both queue files should reset to pending, got {files!r}"
    )


# ───────────────────── import queue: clear-old ─────────────────────


def test_import_clear_old_deletes_failed_and_skipped(env):
    """clear-old purges every 'failed' and 'skipped' import_queue row
    plus their child file rows. Pending/partial rows must survive."""
    client = _client()
    csrf = _csrf("iq-clear")

    r = client.post("/import/clear-old", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("SELECT id, status FROM import_queue").fetchall()
    statuses = {r['id']: r['status'] for r in rows}

    assert 200 in statuses and statuses[200] == 'pending', (
        "pending row must survive clear-old"
    )
    assert 201 not in statuses, "failed row must be deleted"
    assert 202 not in statuses, "skipped row must be deleted"

    # Cascade — files for deleted queue rows must also be gone
    with sqlite3.connect(env['db_path']) as c:
        f_orphans = c.execute(
            "SELECT COUNT(*) FROM import_queue_files WHERE queue_id IN (201, 202)"
        ).fetchone()[0]
    assert f_orphans == 0, "child file rows must cascade-delete with the queue rows"
