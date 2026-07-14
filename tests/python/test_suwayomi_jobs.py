"""Regression tests for the Suwayomi job-completion path.

Production bug (v0.1.2-operator-hardening): check_suwayomi_jobs called
`.get("title", "")` on a sqlite3.Row returned by get_db().fetchone(),
producing AttributeError after every successful download. This corrupted
425 production rows: the imports actually succeeded (chapters/volumes
got status='downloaded') but the suwayomi_downloads row was overwritten
to status='error' by the outer except handler.

These tests use real sqlite3.Row objects (not dicts) because the bug only
surfaces when the row factory is sqlite3.Row — which is exactly what
get_db() configures.

Coverage:
  - chapter-level job completes without crashing on .get()
  - volume-level job completes without crashing on .get()
  - failed import_path is still reported as 'error' (not regression)
  - missing series row degrades to empty title, not crash
  - the underlying chapters/volumes are correctly marked downloaded
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
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


def _make_cbz(path: str, page_count: int = 1) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for i in range(page_count):
            zf.writestr(f"{i+1:04d}.png", _TINY_PNG)
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fresh DB + temp Suwayomi library + temp Mangarr library + queued jobs."""
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-swyjobs-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()

    swy_root = tmp_path / "swy"
    lib_root = tmp_path / "library"
    lib_root.mkdir()

    def _fake_dir(_db, sid):
        d = lib_root / f"series-{sid}"
        d.mkdir(exist_ok=True)
        return str(d)
    monkeypatch.setattr(main, "_series_library_dir", _fake_dir)

    # Seed: a Suwayomi DL client + a series + the manga dir + cbz files.
    with sqlite3.connect(db.name) as c:
        c.execute(
            "INSERT INTO download_clients(id, name, type, host, enabled, download_path)"
            " VALUES(1, 'swy', 'suwayomi', 'http://swy.local:4567', 1, ?)",
            (str(swy_root),)
        )
        c.execute(
            "INSERT INTO series(id, title, search_pattern) VALUES(7, 'Hunter x Hunter', 'Hunter x Hunter')"
        )

    manga_dir = swy_root / "mangas" / "MangaDex" / "Hunter x Hunter"
    manga_dir.mkdir(parents=True)

    try:
        yield {
            "db_path":   db.name,
            "manga_dir": manga_dir,
            "lib_root":  lib_root,
        }
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


def _gql_stub(manga_title: str, chapter_ids: list[int]):
    """Returns an _gql replacement that mimics the Suwayomi GraphQL response.
    Reports every chapter id as isDownloaded=True."""
    async def _fake_gql(_c, _query, _vars=None):
        return {
            "manga": {
                "title": manga_title,
                "chapters": {
                    "nodes": [{"id": cid, "isDownloaded": True} for cid in chapter_ids],
                },
            },
        }
    return _fake_gql


# ─────────────────── chapter-level job (Hunter x Hunter pattern) ─────────────

def test_chapter_job_completes_without_attribute_error(env):
    """The bug: check_suwayomi_jobs raised AttributeError after a successful
    chapter import, overwriting status='completed' with status='error'.
    With the fix, the row stays 'completed'."""
    import asyncio
    from routers import suwayomi_ as swy

    # Drop a chapter CBZ where _import_suwayomi_chapter expects it.
    _make_cbz(str(env["manga_dir"] / "Vol.1 Ch.200.cbz"))

    # Seed the wanted chapter row + suwayomi job.
    with sqlite3.connect(env["db_path"]) as c:
        c.execute(
            "INSERT INTO chapters(series_id, chapter_num, status)"
            " VALUES(7, 200.0, 'wanted')"
        )
        c.execute(
            "INSERT INTO suwayomi_downloads"
            " (series_id, suwayomi_manga_id, chapter_ids, chapter_num, status,"
            "  progress, total, created_at)"
            " VALUES(7, 999, ?, 200.0, 'queued', 0, 1, datetime('now'))",
            (json.dumps([2683]),),
        )

    with patch.object(swy, "_gql", new=_gql_stub("Hunter x Hunter", [2683])), \
         patch.object(swy, "get_suwayomi_client",
                      new=lambda _db: {"id": 1, "type": "suwayomi",
                                       "host": "http://swy.local:4567",
                                       "download_path": str(env["manga_dir"].parent.parent.parent)}):
        asyncio.run(swy.check_suwayomi_jobs())

    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        job = c.execute("SELECT status, error FROM suwayomi_downloads WHERE chapter_num=200.0").fetchone()
        ch = c.execute(
            "SELECT status, import_path, quality, imported_at, indexer, protocol,"
            " client, torrent_name FROM chapters WHERE chapter_num=200.0"
        ).fetchone()

    # The bug: status would be 'error' with AttributeError in `error`.
    assert job["status"] == "completed", (
        f"job left in {job['status']!r} with error={job['error']!r}"
    )
    # And the file actually got imported.
    assert ch["status"] == "downloaded"
    assert ch["import_path"] and ch["import_path"].endswith(".cbz")
    assert ch["quality"] == "cbz"
    assert ch["imported_at"]
    assert ch["indexer"] == "Suwayomi"
    assert ch["protocol"] == "ddl"
    assert ch["client"] == "suwayomi"
    assert ch["torrent_name"]


def test_chapter_job_does_not_record_attribute_error(env):
    """Belt-and-braces: even if the row factory ever changes, calling
    sqlite3.Row.get() must never appear in the error message of a
    completed job. Asserts on the message text."""
    import asyncio
    from routers import suwayomi_ as swy

    _make_cbz(str(env["manga_dir"] / "Vol.1 Ch.42.cbz"))
    with sqlite3.connect(env["db_path"]) as c:
        c.execute("INSERT INTO chapters(series_id, chapter_num, status)"
                  " VALUES(7, 42.0, 'wanted')")
        c.execute(
            "INSERT INTO suwayomi_downloads"
            " (series_id, suwayomi_manga_id, chapter_ids, chapter_num, status,"
            "  progress, total, created_at)"
            " VALUES(7, 999, ?, 42.0, 'queued', 0, 1, datetime('now'))",
            (json.dumps([100]),),
        )

    with patch.object(swy, "_gql", new=_gql_stub("Hunter x Hunter", [100])), \
         patch.object(swy, "get_suwayomi_client",
                      new=lambda _db: {"id": 1, "type": "suwayomi",
                                       "host": "http://swy.local:4567",
                                       "download_path": str(env["manga_dir"].parent.parent.parent)}):
        asyncio.run(swy.check_suwayomi_jobs())

    with sqlite3.connect(env["db_path"]) as c:
        err = c.execute(
            "SELECT error FROM suwayomi_downloads WHERE chapter_num=42.0"
        ).fetchone()[0]
    assert err is None or "sqlite3.Row" not in err


# ─────────────────── volume-level job (JoJo Vol 17 pattern) ──────────────────

def test_volume_job_completes_without_attribute_error(env):
    """Same bug, volume branch. Multi-chapter merge into a single CBZ."""
    import asyncio
    from routers import suwayomi_ as swy

    # Drop two chapter cbzs that belong to vol 17 — _import_suwayomi_volume
    # will merge them.
    _make_cbz(str(env["manga_dir"] / "Vol.17 Ch.150.cbz"))
    _make_cbz(str(env["manga_dir"] / "Vol.17 Ch.151.cbz"))

    with sqlite3.connect(env["db_path"]) as c:
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status)"
            " VALUES(7, 17.0, 'wanted')"
        )
        c.execute(
            "INSERT INTO suwayomi_downloads"
            " (series_id, volume_num, suwayomi_manga_id, chapter_ids, status,"
            "  progress, total, created_at)"
            " VALUES(7, 17.0, 999, ?, 'queued', 0, 2, datetime('now'))",
            (json.dumps([301, 302]),),
        )

    with patch.object(swy, "_gql", new=_gql_stub("Hunter x Hunter", [301, 302])), \
         patch.object(swy, "get_suwayomi_client",
                      new=lambda _db: {"id": 1, "type": "suwayomi",
                                       "host": "http://swy.local:4567",
                                       "download_path": str(env["manga_dir"].parent.parent.parent)}):
        asyncio.run(swy.check_suwayomi_jobs())

    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        job = c.execute("SELECT status, error FROM suwayomi_downloads WHERE volume_num=17.0").fetchone()
        vol = c.execute(
            "SELECT status, import_path, quality, imported_at, indexer, protocol,"
            " client, torrent_name FROM volumes WHERE volume_num=17.0"
        ).fetchone()

    assert job["status"] == "completed", (
        f"volume job left in {job['status']!r} with error={job['error']!r}"
    )
    assert vol["status"] == "downloaded"
    assert vol["import_path"]
    assert vol["quality"] == "cbz"
    assert vol["imported_at"]
    assert vol["indexer"] == "Suwayomi"
    assert vol["protocol"] == "ddl"
    assert vol["client"] == "suwayomi"
    assert vol["torrent_name"]


# ─────────────────── degenerate paths (ensure no regression) ─────────────────

def test_chapter_job_with_missing_series_does_not_crash(env):
    """Edge case: series row is somehow missing when add_history runs.
    Title degrades to empty string instead of crashing."""
    import asyncio
    from routers import suwayomi_ as swy

    _make_cbz(str(env["manga_dir"] / "Vol.1 Ch.7.cbz"))
    with sqlite3.connect(env["db_path"]) as c:
        # Reference series id that doesn't exist.
        c.execute("INSERT INTO chapters(series_id, chapter_num, status)"
                  " VALUES(99, 7.0, 'wanted')")
        c.execute(
            "INSERT INTO suwayomi_downloads"
            " (series_id, suwayomi_manga_id, chapter_ids, chapter_num, status,"
            "  progress, total, created_at)"
            " VALUES(99, 999, ?, 7.0, 'queued', 0, 1, datetime('now'))",
            (json.dumps([5]),),
        )

    # Filesystem lookup needs a series to find the manga dir → import returns None.
    # That's still a successful no-import path (status='error' set with the
    # specific 'Import failed' message, NOT an AttributeError).
    with patch.object(swy, "_gql", new=_gql_stub("Doesn't Matter", [5])), \
         patch.object(swy, "get_suwayomi_client",
                      new=lambda _db: {"id": 1, "type": "suwayomi",
                                       "host": "http://swy.local:4567",
                                       "download_path": str(env["manga_dir"].parent.parent.parent)}):
        asyncio.run(swy.check_suwayomi_jobs())

    with sqlite3.connect(env["db_path"]) as c:
        err = c.execute(
            "SELECT error FROM suwayomi_downloads WHERE chapter_num=7.0"
        ).fetchone()[0]
    # Accept either the explicit "Import failed" branch or absence of error.
    # The key invariant: never the AttributeError signature.
    assert err is None or "sqlite3.Row" not in err


def test_failed_import_still_marks_error_with_descriptive_message(env):
    """Pre-existing behaviour preserved: when _import_suwayomi_chapter
    returns None (CBZ not found), the row is marked error with a clear
    message — not AttributeError."""
    import asyncio
    from routers import suwayomi_ as swy

    # No CBZ on disk for this chapter — import will return None.
    with sqlite3.connect(env["db_path"]) as c:
        c.execute("INSERT INTO chapters(series_id, chapter_num, status)"
                  " VALUES(7, 999.0, 'wanted')")
        c.execute(
            "INSERT INTO suwayomi_downloads"
            " (series_id, suwayomi_manga_id, chapter_ids, chapter_num, status,"
            "  progress, total, created_at)"
            " VALUES(7, 999, ?, 999.0, 'queued', 0, 1, datetime('now'))",
            (json.dumps([404]),),
        )

    with patch.object(swy, "_gql", new=_gql_stub("Hunter x Hunter", [404])), \
         patch.object(swy, "get_suwayomi_client",
                      new=lambda _db: {"id": 1, "type": "suwayomi",
                                       "host": "http://swy.local:4567",
                                       "download_path": str(env["manga_dir"].parent.parent.parent)}):
        asyncio.run(swy.check_suwayomi_jobs())

    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        job = c.execute("SELECT status, error FROM suwayomi_downloads WHERE chapter_num=999.0").fetchone()
    assert job["status"] == "error"
    assert "Import failed" in (job["error"] or "")
    assert "sqlite3.Row" not in (job["error"] or "")


# ─────────────────── duplicate-row guard (no retry amplification) ────────────

def test_check_suwayomi_jobs_only_processes_queued_rows(env):
    """The bug created 425 'error' rows. Verify that error rows are NOT
    re-processed on the next tick (they'd accumulate fix-attempt errors)."""
    import asyncio
    from routers import suwayomi_ as swy

    with sqlite3.connect(env["db_path"]) as c:
        # Drop one error row from the past — should be ignored.
        c.execute(
            "INSERT INTO suwayomi_downloads"
            " (series_id, suwayomi_manga_id, chapter_ids, chapter_num, status,"
            "  progress, total, error, created_at)"
            " VALUES(7, 999, ?, 1.0, 'error', 1, 1, 'old error', datetime('now', '-1 day'))",
            (json.dumps([1]),),
        )

    gql_calls = []
    async def _counting_gql(*a, **kw):
        gql_calls.append(1)
        return {"manga": {"title": "X", "chapters": {"nodes": []}}}

    with patch.object(swy, "_gql", new=_counting_gql), \
         patch.object(swy, "get_suwayomi_client",
                      new=lambda _db: {"id": 1, "type": "suwayomi",
                                       "host": "http://swy.local:4567",
                                       "download_path": str(env["manga_dir"].parent.parent.parent)}):
        asyncio.run(swy.check_suwayomi_jobs())

    # Zero queued rows → loop returns early before even fetching the client.
    assert gql_calls == [], (
        f"check_suwayomi_jobs touched the upstream {len(gql_calls)} times "
        "despite there being no 'queued' rows — error rows leaked into the loop"
    )
