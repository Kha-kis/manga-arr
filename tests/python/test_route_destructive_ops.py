"""HTTP-level integration tests for destructive routes.

Covers irreversible operations the production-readiness audit flagged as
silent-correctness risks: a bug in series/{id}/delete that deletes the
wrong cascade chain, or a blocklist add that returns 200 without
inserting, would not surface in casual daily use because the thing you'd
verify against is the same row that just got deleted (or never written).

Each test posts the real request through the real router → real DB,
verifies the response and the resulting DB state.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401


@pytest.fixture
def _process_globals_restored():
    """Assert this module's DB fixture leaves process globals exactly as found."""
    import import_execute
    import main
    import security
    import shared

    state = {
        "main_db": main.DB_PATH,
        "shared_db": shared.DB_PATH,
        "main_config": main.CONFIG,
        "main_config_values": dict(main.CONFIG),
        "shared_config": shared.CONFIG,
        "shared_config_values": dict(shared.CONFIG),
        "cipher": security._SECRET_CIPHER,
        "import_sem": import_execute._IMPORT_SEM,
    }
    yield state
    assert main.DB_PATH == state["main_db"]
    assert shared.DB_PATH == state["shared_db"]
    assert main.CONFIG is state["main_config"]
    assert main.CONFIG == state["main_config_values"]
    assert shared.CONFIG is state["shared_config"]
    assert shared.CONFIG == state["shared_config_values"]
    assert security._SECRET_CIPHER is state["cipher"]
    assert import_execute._IMPORT_SEM is state["import_sem"]


@pytest.fixture
def env(tmp_path, _process_globals_restored):
    """Fresh DB + 2 series + their volumes, blocklist, indexer."""
    import import_execute
    import main
    import security
    import shared

    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-destroy-keys-")

    try:
        main.DB_PATH = db.name
        shared.DB_PATH = db.name
        security._SECRET_CIPHER = None
        security.load_or_create_secret_cipher(key_dir)
        main.init_db()
        main.load_config()
        main.ensure_api_key()

        library_root = tmp_path / "library"
        library_root.mkdir()

        with sqlite3.connect(db.name) as c:
            c.execute("DELETE FROM root_folders")
            c.execute(
                "INSERT INTO root_folders(id, path) VALUES(1, ?)",
                (str(library_root),),
            )
            # Two series — destructive ops on series 1 must not touch series 2
            c.execute(
                "INSERT INTO series(id, title, search_pattern, edition_type, enabled,"
                " monitored, monitor_mode, root_folder_id)"
                " VALUES(1, 'AlphaSeries', 'AlphaSeries', 'standard', 1, 1, 'all', 1),"
                "       (2, 'BetaSeries',  'BetaSeries',  'standard', 1, 1, 'all', 1)"
            )
            c.execute(
                "INSERT INTO volumes(series_id, volume_num, status, monitored)"
                " VALUES(1, 1.0, 'wanted', 1), (1, 2.0, 'wanted', 1),"
                "       (2, 1.0, 'wanted', 1)"
            )
            # Blocklist with 3 rows: 2 for series 1, 1 standalone
            c.execute(
                "INSERT INTO blocklist(series_id, torrent_url, torrent_name, reason)"
                " VALUES(1, 'http://stub/a.torrent',"
                " 'AlphaSeries v01 bad release', 'Manual'),"
                "       (1, 'http://stub/b.torrent',"
                " 'AlphaSeries v02 bad release', 'Manual'),"
                "       (NULL, 'http://stub/c.torrent', 'unrelated', 'Manual')"
            )

        yield {
            'db_path': db.name,
            'library_root': str(library_root),
        }
    finally:
        state = _process_globals_restored
        main.DB_PATH = state["main_db"]
        shared.DB_PATH = state["shared_db"]
        main.CONFIG = state["main_config"]
        main.CONFIG.clear()
        main.CONFIG.update(state["main_config_values"])
        shared.CONFIG = state["shared_config"]
        shared.CONFIG.clear()
        shared.CONFIG.update(state["shared_config_values"])
        security._SECRET_CIPHER = state["cipher"]
        import_execute._IMPORT_SEM = state["import_sem"]
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)
        shutil.rmtree(key_dir)


def _client():
    import main
    from fastapi.testclient import TestClient
    return TestClient(main.app)


def _csrf_kwargs(tag: str = "test"):
    """Build the CSRF cookie + header pair required by middleware."""
    tok = f"csrf-{tag}-" + "x" * 30
    return {
        'cookies': {'csrftoken': tok},
        'headers': {'X-CSRFToken': tok},
    }


def _seed_history_publication(
    db_path: str,
    state: str,
    *,
    publication_series_id: int = 1,
) -> None:
    """Seed one grabbed history domain and its publication journal."""
    with sqlite3.connect(db_path) as c:
        c.execute(
            "INSERT INTO volumes(id, series_id, volume_num, status, monitored,"
            " download_id, source_url, torrent_name, indexer, protocol, client)"
            " VALUES(901, 1, 9.0, 'grabbed', 1, 'HISTORY-DOWNLOAD',"
            " 'http://stub/history.torrent', 'AlphaSeries v09',"
            " 'Indexer', 'torrent', 'Qbit')"
        )
        c.execute(
            "INSERT INTO seen(torrent_url, torrent_name, series_id, volume_num,"
            " indexer, protocol, client, download_id)"
            " VALUES('http://stub/history.torrent', 'AlphaSeries v09', 1, 9.0,"
            " 'Indexer', 'torrent', 'Qbit', 'HISTORY-DOWNLOAD')"
        )
        c.execute(
            "INSERT INTO history(id, event_type, series_id, source_title,"
            " torrent_url, download_id, indexer, protocol, size_bytes)"
            " VALUES(901, 'grabbed', 1, 'AlphaSeries v09',"
            " 'http://stub/history.torrent', 'HISTORY-DOWNLOAD',"
            " 'Indexer', 'torrent', 9001)"
        )
        queue_status = (
            "pending"
            if state in {"staging", "prepared", "publishing", "published",
                         "db_committed", "cleaning"}
            else "imported"
        )
        c.execute(
            "INSERT INTO import_queue(id, series_id, download_id, torrent_name,"
            " torrent_url, status, lease_owner)"
            " VALUES(901, ?, 'history-download', 'AlphaSeries v09',"
            " 'http://stub/history.torrent', ?, NULL)",
            (publication_series_id, queue_status),
        )
        c.execute(
            """
            INSERT INTO import_publications(
                queue_id, state, owner_token, series_id, dst_dir, import_mode,
                staging_dir, queue_snapshot_json, series_tags_json, queue_status
            ) VALUES(
                901, ?, 'history-owner', ?, '/library/AlphaSeries', 'copy',
                '/library/AlphaSeries/.mangarr-publication-901',
                '{}', '[]', 'pending'
            )
            """,
            (state, publication_series_id),
        )


def _history_failure_domain(db_path: str) -> dict[str, object]:
    with sqlite3.connect(db_path) as c:
        return {
            "history": c.execute(
                "SELECT event_type FROM history WHERE id=901"
            ).fetchone(),
            "blocklist": c.execute(
                "SELECT COUNT(*) FROM blocklist"
                " WHERE torrent_url='http://stub/history.torrent'"
            ).fetchone()[0],
            "volume": c.execute(
                "SELECT status, download_id, source_url, indexer"
                " FROM volumes WHERE id=901"
            ).fetchone(),
            "seen": c.execute(
                "SELECT COUNT(*) FROM seen"
                " WHERE torrent_url='http://stub/history.torrent'"
            ).fetchone()[0],
        }


# ───────────────────── history mark-failed ─────────────────────


def test_mark_history_failed_helper_blocks_active_publication_without_mutation(env):
    """A journal remains authoritative after queue status/lease become stale."""
    from routers.history_ import mark_history_failed

    _seed_history_publication(env["db_path"], "published")
    before = _history_failure_domain(env["db_path"])

    result = mark_history_failed(901)

    assert result == {"ok": False, "status": "in_progress"}
    assert _history_failure_domain(env["db_path"]) == before


def test_mark_history_failed_allows_active_publication_for_other_series(env):
    """A matching download identity in another series does not over-block."""
    from routers.history_ import mark_history_failed

    _seed_history_publication(
        env["db_path"],
        "published",
        publication_series_id=2,
    )

    result = mark_history_failed(901)

    assert result == {"ok": True, "status": "marked_failed"}
    assert _history_failure_domain(env["db_path"]) == {
        "history": ("grab_failed",),
        "blocklist": 1,
        "volume": ("wanted", None, None, None),
        "seen": 0,
    }


@pytest.mark.parametrize(
    ("protocol", "target_id", "other_id", "target_owner", "other_owner"),
    (
        ("torrent", "ABCDEF", "abcdef", 101, 102),
        ("nzb", "NZO-Case", "nzo-case", 201, 201),
    ),
)
def test_history_mark_failed_route_resets_only_exact_owned_identity(
    env,
    protocol,
    target_id,
    other_id,
    target_owner,
    other_owner,
):
    with sqlite3.connect(env["db_path"]) as db:
        db.executemany(
            "INSERT INTO volumes("
            "id,series_id,volume_num,status,monitored,download_id,"
            "download_client_id,source_url,protocol"
            ") VALUES(?,1,?,'grabbed',1,?,?,?,?)",
            (
                (910, 10.0, target_id, target_owner, "owned:target", protocol),
                (911, 11.0, other_id, other_owner, "owned:other", protocol),
            ),
        )
        db.executemany(
            "INSERT INTO seen("
            "torrent_url,torrent_name,series_id,volume_num,protocol,"
            "download_id,download_client_id"
            ") VALUES(?,?,1,?,?,?,?)",
            (
                ("owned:target", "Target", 10.0, protocol, target_id, target_owner),
                ("owned:other", "Other", 11.0, protocol, other_id, other_owner),
            ),
        )
        db.execute(
            "INSERT INTO history("
            "id,event_type,series_id,source_title,torrent_url,protocol,"
            "download_id,download_client_id"
            ") VALUES(910,'grabbed',1,'Target','owned:target',?,?,?)",
            (protocol, target_id, target_owner),
        )

    response = _client().post(
        "/history/910/mark-failed",
        follow_redirects=False,
        **_csrf_kwargs(f"owned-history-{protocol}"),
    )

    assert response.status_code == 303
    with sqlite3.connect(env["db_path"]) as db:
        assert db.execute(
            "SELECT status,download_id,download_client_id"
            " FROM volumes WHERE id=910"
        ).fetchone() == ("wanted", None, None)
        assert db.execute(
            "SELECT status,download_id,download_client_id"
            " FROM volumes WHERE id=911"
        ).fetchone() == ("grabbed", other_id, other_owner)
        assert db.execute(
            "SELECT torrent_url FROM seen ORDER BY torrent_url"
        ).fetchall() == [("owned:other",)]


@pytest.mark.parametrize(
    ("status", "lease_owner"),
    [("importing", None), ("pending", "pre-journal-owner")],
)
def test_mark_history_failed_blocks_prejournal_import_without_mutation(
    env,
    status,
    lease_owner,
):
    """History failure cannot reset a release owned before journal creation."""
    from routers.history_ import mark_history_failed

    _seed_history_publication(env["db_path"], "deleted")
    with sqlite3.connect(env["db_path"]) as c:
        c.execute("DELETE FROM import_publications")
        c.execute(
            "UPDATE import_queue SET status=?, lease_owner=? WHERE id=901",
            (status, lease_owner),
        )
    before = _history_failure_domain(env["db_path"])

    result = mark_history_failed(901)

    assert result == {"ok": False, "status": "in_progress"}
    assert _history_failure_domain(env["db_path"]) == before


@pytest.mark.parametrize("state", ["staging", "finalized", "deleted"])
@pytest.mark.parametrize("htmx", [False, True], ids=["plain", "htmx"])
def test_history_mark_failed_route_surfaces_publication_outcome(
    env,
    state,
    htmx,
):
    """The route reports journal blocks and allows both terminal states."""
    _seed_history_publication(env["db_path"], state)
    before = _history_failure_domain(env["db_path"])
    csrf = _csrf_kwargs(f"history-publication-{state}-{htmx}")
    headers = dict(csrf["headers"])
    if htmx:
        headers["HX-Request"] = "true"

    response = _client().post(
        "/history/901/mark-failed",
        cookies=csrf["cookies"],
        headers=headers,
        follow_redirects=False,
    )

    blocked = state == "staging"
    if htmx:
        assert response.status_code == 200
        assert response.headers["HX-Refresh"] == "true"
        toast = json.loads(response.headers["HX-Trigger"])["showToast"]
        assert toast["type"] == ("warning" if blocked else "success")
        assert (
            "Import is in progress"
            if blocked
            else "Marked failed and added to blocklist"
        ) in toast["msg"]
    else:
        assert response.status_code == 303
        assert response.headers["location"].startswith("/history?")
        assert (
            "flash_type=warning"
            if blocked
            else "flash_type=success"
        ) in response.headers["location"]

    after = _history_failure_domain(env["db_path"])
    if blocked:
        assert after == before
    else:
        assert after == {
            "history": ("grab_failed",),
            "blocklist": 1,
            "volume": ("wanted", None, None, None),
            "seen": 0,
        }


# ───────────────────── series delete ─────────────────────


def test_series_delete_soft_deletes_only_target(env):
    """Soft-delete of series_id=1 must mark series 1 only — series 2's
    `deleted_at` must remain NULL.

    NOTE: as of the recycle-bin epic, /series/{id}/delete is a soft-delete
    that sets `deleted_at` + `deletion_reason` and leaves every dependent
    row in place. The hard cascade now lives in `_run_hard_delete_series`,
    called by the reaper and by the explicit purge endpoint. The
    cross-series isolation property still holds.
    """
    client = _client()
    csrf = _csrf_kwargs("delete-series")

    r = client.post("/series/1/delete", **csrf, follow_redirects=False)
    assert r.status_code in (303, 200), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row

        s1 = c.execute("SELECT title, deleted_at FROM series WHERE id=1").fetchone()
        s2 = c.execute("SELECT title, deleted_at FROM series WHERE id=2").fetchone()
        assert s1 is not None, "series 1 row must still exist (soft-delete)"
        assert s1['deleted_at'] is not None, "series 1 must be marked soft-deleted"
        assert s2 is not None and s2['title'] == 'BetaSeries'
        assert s2['deleted_at'] is None, "series 2 deleted_at must be NULL"

        # During the soft-delete window, dependent rows are preserved for
        # restore. Just verify series-2 wasn't accidentally touched.
        v2_count = c.execute("SELECT COUNT(*) FROM volumes WHERE series_id=2").fetchone()[0]
        assert v2_count == 1, "series-2 volumes must NOT be affected"
        bl_orphan = c.execute("SELECT COUNT(*) FROM blocklist WHERE series_id IS NULL").fetchone()[0]
        assert bl_orphan == 1, "standalone blocklist row must survive"


def test_series_delete_logs_history(env):
    """The soft-delete must add a `series_soft_deleted` history event so
    the user can audit what happened. (Renamed from `series_deleted` —
    the hard `series_purged` event is logged by the reaper / purge.)"""
    client = _client()
    csrf = _csrf_kwargs("delete-history")

    r = client.post("/series/1/delete", **csrf, follow_redirects=False)
    assert r.status_code in (303, 200)

    with sqlite3.connect(env['db_path']) as c:
        ev = c.execute(
            "SELECT event_type, source_title FROM history"
            " WHERE event_type='series_soft_deleted'"
        ).fetchone()
        assert ev is not None, "series_soft_deleted event must be logged"
        assert ev[1] == 'AlphaSeries', f"deleted-title must match, got {ev[1]!r}"


def test_series_delete_unknown_id_returns_redirect_not_500(env):
    """A delete for a non-existent series should silently no-op (the row
    simply isn't there to delete) rather than 500. CSRF must still pass."""
    client = _client()
    csrf = _csrf_kwargs("delete-missing")

    r = client.post("/series/99999/delete", **csrf, follow_redirects=False)
    assert r.status_code in (303, 200), (
        f"missing-series delete should redirect cleanly, got {r.status_code}: {r.text}"
    )

    # Existing series untouched
    with sqlite3.connect(env['db_path']) as c:
        n = c.execute("SELECT COUNT(*) FROM series").fetchone()[0]
        assert n == 2, "no series should have been deleted"


# ───────────────────── series hard purge ─────────────────────


@pytest.mark.parametrize("htmx", [False, True], ids=["plain", "htmx"])
def test_series_purge_blocks_exact_live_import_lease_without_mutation(env, htmx):
    """Even an expired lease remains owned until recovery explicitly clears it."""
    with sqlite3.connect(env["db_path"]) as c:
        c.execute("UPDATE series SET deleted_at=CURRENT_TIMESTAMP WHERE id=1")
        queue_id = c.execute(
            "INSERT INTO import_queue("
            "series_id, download_id, torrent_name, status, lease_owner,"
            " lease_expires_at"
            ") VALUES(1, 'live-download', 'AlphaSeries v01', 'importing',"
            " 'live-owner', datetime('now', '-1 hour'))"
        ).lastrowid
        assert queue_id is not None
        c.execute(
            "INSERT INTO import_queue_files(queue_id, filename, status)"
            " VALUES(?, 'AlphaSeries v01.cbz', 'pending')",
            (queue_id,),
        )
        before = {
            "series": c.execute(
                "SELECT title, deleted_at FROM series WHERE id=1"
            ).fetchone(),
            "volumes": c.execute(
                "SELECT COUNT(*) FROM volumes WHERE series_id=1"
            ).fetchone()[0],
            "blocklist": c.execute(
                "SELECT COUNT(*) FROM blocklist WHERE series_id=1"
            ).fetchone()[0],
            "queue": c.execute(
                "SELECT status, lease_owner, lease_expires_at"
                " FROM import_queue WHERE id=?",
                (queue_id,),
            ).fetchone(),
            "queue_files": c.execute(
                "SELECT COUNT(*) FROM import_queue_files WHERE queue_id=?",
                (queue_id,),
            ).fetchone()[0],
            "history": c.execute(
                "SELECT COUNT(*) FROM history WHERE event_type='series_purged'"
            ).fetchone()[0],
        }

    csrf = _csrf_kwargs(f"purge-live-lease-{htmx}")
    headers = dict(csrf["headers"])
    if htmx:
        headers["HX-Request"] = "true"
    response = _client().post(
        "/series/1/purge",
        cookies=csrf["cookies"],
        headers=headers,
        follow_redirects=False,
    )

    if htmx:
        assert response.status_code == 200
        assert response.headers["HX-Redirect"] == "/recycle-bin"
        toast = json.loads(response.headers["HX-Trigger"])["showToast"]
        assert toast["type"] == "warning"
        assert "Import is in progress" in toast["msg"]
    else:
        assert response.status_code == 303
        assert response.headers["location"].startswith("/recycle-bin?")
        assert "flash_type=warning" in response.headers["location"]
        assert "Import+is+in+progress" in response.headers["location"]

    with sqlite3.connect(env["db_path"]) as c:
        after = {
            "series": c.execute(
                "SELECT title, deleted_at FROM series WHERE id=1"
            ).fetchone(),
            "volumes": c.execute(
                "SELECT COUNT(*) FROM volumes WHERE series_id=1"
            ).fetchone()[0],
            "blocklist": c.execute(
                "SELECT COUNT(*) FROM blocklist WHERE series_id=1"
            ).fetchone()[0],
            "queue": c.execute(
                "SELECT status, lease_owner, lease_expires_at"
                " FROM import_queue WHERE id=?",
                (queue_id,),
            ).fetchone(),
            "queue_files": c.execute(
                "SELECT COUNT(*) FROM import_queue_files WHERE queue_id=?",
                (queue_id,),
            ).fetchone()[0],
            "history": c.execute(
                "SELECT COUNT(*) FROM history WHERE event_type='series_purged'"
            ).fetchone()[0],
        }
    assert after == before
    assert after["queue"][:2] == ("importing", "live-owner")


def test_series_purge_allows_unowned_pending_queue_and_cascades_atomically(env):
    """An eligible purge still removes the complete DB domain and its file."""
    volume_file = os.path.join(env["library_root"], "AlphaSeries v01.cbz")
    with open(volume_file, "wb") as stream:
        stream.write(b"purge-me")
    with sqlite3.connect(env["db_path"]) as c:
        c.execute("UPDATE series SET deleted_at=CURRENT_TIMESTAMP WHERE id=1")
        c.execute(
            "UPDATE volumes SET import_path=? WHERE series_id=1 AND volume_num=1",
            (volume_file,),
        )
        queue_id = c.execute(
            "INSERT INTO import_queue(series_id, status, lease_owner)"
            " VALUES(1, 'pending', NULL)"
        ).lastrowid
        assert queue_id is not None
        c.execute(
            "INSERT INTO import_queue_files(queue_id, filename, status)"
            " VALUES(?, 'AlphaSeries v01.cbz', 'pending')",
            (queue_id,),
        )

    csrf = _csrf_kwargs("purge-normal")
    response = _client().post(
        "/series/1/purge",
        cookies=csrf["cookies"],
        headers=csrf["headers"],
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/recycle-bin"
    assert not os.path.exists(volume_file)

    with sqlite3.connect(env["db_path"]) as c:
        assert c.execute("SELECT 1 FROM series WHERE id=1").fetchone() is None
        assert c.execute("SELECT 1 FROM series WHERE id=2").fetchone() is not None
        for table in (
            "volumes",
            "chapters",
            "pending_releases",
            "seen",
            "blocklist",
            "series_aliases",
            "series_tags",
            "import_queue",
        ):
            assert (
                c.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE series_id=1"
                ).fetchone()[0]
                == 0
            )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM import_queue_files WHERE queue_id=?",
                (queue_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM history"
                " WHERE event_type='series_purged' AND source_title='AlphaSeries'"
            ).fetchone()[0]
            == 1
        )


def test_series_purge_removes_only_paths_strictly_inside_configured_root(env, tmp_path):
    """Corrupt import paths cannot make purge delete outside the library root."""
    library_root = env["library_root"]
    inside_file = os.path.join(library_root, "inside.cbz")
    inside_link = os.path.join(library_root, "inside-link.cbz")
    inside_dir = os.path.join(library_root, "inside-dir")
    outside_regular = str(tmp_path / "outside.cbz")
    outside_link = str(tmp_path / "outside-link.cbz")
    outside_target = str(tmp_path / "outside-target.cbz")
    escaped_parent = os.path.join(library_root, "escaped-parent")
    escaped_dir = tmp_path / "escaped-dir"
    escaped_victim = escaped_dir / "escaped-victim.cbz"
    relative_victim = tmp_path / "relative-victim.cbz"

    with open(inside_file, "wb") as stream:
        stream.write(b"inside")
    os.mkdir(inside_dir)
    with open(os.path.join(inside_dir, "nested.cbz"), "wb") as stream:
        stream.write(b"nested")
    with open(outside_regular, "wb") as stream:
        stream.write(b"outside")
    with open(outside_target, "wb") as stream:
        stream.write(b"target")
    os.symlink(outside_target, inside_link)
    os.symlink(inside_file, outside_link)
    escaped_dir.mkdir()
    escaped_victim.write_bytes(b"escaped")
    os.symlink(str(escaped_dir), escaped_parent)
    relative_victim.write_bytes(b"relative")
    relative_path = os.path.relpath(relative_victim, os.getcwd())

    import_paths = (
        library_root,  # The root itself must never be recursively removed.
        inside_file,
        inside_link,  # Unlink without following its outside target.
        inside_dir,
        outside_regular,
        outside_link,
        os.path.join(escaped_parent, escaped_victim.name),
        relative_path,
    )
    with sqlite3.connect(env["db_path"]) as c:
        c.execute("UPDATE series SET deleted_at=CURRENT_TIMESTAMP WHERE id=1")
        c.execute("DELETE FROM volumes WHERE series_id=1")
        c.executemany(
            "INSERT INTO volumes(series_id, volume_num, status, import_path)"
            " VALUES(1, ?, 'downloaded', ?)",
            (
                (float(index), import_path)
                for index, import_path in enumerate(import_paths, start=1)
            ),
        )

    csrf = _csrf_kwargs("purge-path-containment")
    response = _client().post(
        "/series/1/purge",
        cookies=csrf["cookies"],
        headers=csrf["headers"],
        follow_redirects=False,
    )
    assert response.status_code == 303

    assert os.path.isdir(library_root), "configured root itself must survive"
    assert not os.path.exists(inside_file)
    assert not os.path.lexists(inside_link)
    assert not os.path.exists(inside_dir)
    assert os.path.exists(outside_regular)
    assert os.path.islink(outside_link)
    assert os.path.exists(outside_target), "unlinking must not follow the target"
    assert os.path.islink(escaped_parent)
    assert escaped_victim.exists(), "a symlinked parent must not escape the root"
    assert relative_victim.exists(), "relative import paths must be rejected"


@pytest.mark.parametrize("slow_operation", ["library_rmtree", "cover_remove"])
def test_series_purge_disk_cleanup_does_not_hold_writer_lock(
    env, monkeypatch, slow_operation
):
    """A low-timeout writer succeeds while post-commit disk cleanup is paused."""
    import routers.series_ as series_router

    series_dir = os.path.join(env["library_root"], "AlphaSeries")
    os.mkdir(series_dir)
    with open(os.path.join(series_dir, "AlphaSeries v01.cbz"), "wb") as stream:
        stream.write(b"purge-me")
    with sqlite3.connect(env["db_path"]) as c:
        c.execute("UPDATE series SET deleted_at=CURRENT_TIMESTAMP WHERE id=1")
        c.execute(
            "UPDATE volumes SET import_path=? WHERE series_id=1 AND volume_num=1",
            (series_dir,),
        )

    cleanup_started = threading.Event()
    cleanup_release = threading.Event()
    if slow_operation == "library_rmtree":
        real_rmtree = series_router.shutil.rmtree

        def slow_rmtree(path):
            cleanup_started.set()
            assert cleanup_release.wait(timeout=5)
            real_rmtree(path)

        monkeypatch.setattr(series_router.shutil, "rmtree", slow_rmtree)
    else:
        cover_path = "/config/covers/1.jpg"
        real_lexists = series_router.os.path.lexists
        real_remove = series_router.os.remove

        def cover_exists(path):
            return path == cover_path or real_lexists(path)

        def slow_cover_remove(path):
            if path == cover_path:
                cleanup_started.set()
                assert cleanup_release.wait(timeout=5)
                return
            real_remove(path)

        monkeypatch.setattr(series_router.os.path, "lexists", cover_exists)
        monkeypatch.setattr(series_router.os, "remove", slow_cover_remove)

    csrf = _csrf_kwargs(f"slow-{slow_operation}")

    def purge():
        return _client().post(
            "/series/1/purge",
            cookies=csrf["cookies"],
            headers=csrf["headers"],
            follow_redirects=False,
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(purge)
        assert cleanup_started.wait(timeout=2), "purge never reached disk cleanup"
        try:
            with sqlite3.connect(env["db_path"], timeout=0.05) as writer:
                writer.execute("PRAGMA busy_timeout=50")
                writer.execute(
                    "UPDATE series SET title='Concurrent Writer' WHERE id=2"
                )
        finally:
            cleanup_release.set()
        response = future.result(timeout=5)

    assert response.status_code == 303
    with sqlite3.connect(env["db_path"]) as c:
        assert c.execute("SELECT 1 FROM series WHERE id=1").fetchone() is None
        assert c.execute("SELECT title FROM series WHERE id=2").fetchone()[0] == (
            "Concurrent Writer"
        )


# ───────────────────── blocklist mutations ─────────────────────


def test_blocklist_add_persists_row(env):
    """POST /blocklist/add must INSERT a row. If it returns 200 but
    silently fails to write, releases the user blocked get re-grabbed
    weeks later — exactly the silent-correctness mode the audit warned
    about."""
    client = _client()
    csrf = _csrf_kwargs("bl-add")

    r = client.post(
        "/blocklist/add",
        data={
            'csrf_token':   csrf['headers']['X-CSRFToken'],
            'series_id':    '1',
            'torrent_url':  'http://stub/new-block.torrent',
            'torrent_name': 'AlphaSeries v03 [BadGroup]',
            'reason':       'low quality',
        },
        **csrf,
        follow_redirects=False,
    )
    assert r.status_code in (303, 200), r.text

    with sqlite3.connect(env['db_path']) as c:
        row = c.execute(
            "SELECT series_id, torrent_url, torrent_name, reason FROM blocklist"
            " WHERE torrent_url=?",
            ('http://stub/new-block.torrent',)
        ).fetchone()
        assert row is not None, "blocklist row must be inserted"
        assert row[0] == 1
        assert row[2] == 'AlphaSeries v03 [BadGroup]'
        assert row[3] == 'low quality'


def test_blocklist_add_without_url_does_not_insert(env):
    """Empty torrent_url is a no-op — guards against a UI bug submitting
    a blank form and creating a junk blocklist entry."""
    client = _client()
    csrf = _csrf_kwargs("bl-noop")

    before = None
    with sqlite3.connect(env['db_path']) as c:
        before = c.execute("SELECT COUNT(*) FROM blocklist").fetchone()[0]

    r = client.post(
        "/blocklist/add",
        data={
            'csrf_token':   csrf['headers']['X-CSRFToken'],
            'series_id':    '1',
            'torrent_url':  '',  # empty
            'torrent_name': 'should not insert',
            'reason':       '',
        },
        **csrf,
        follow_redirects=False,
    )
    assert r.status_code in (303, 200)

    with sqlite3.connect(env['db_path']) as c:
        after = c.execute("SELECT COUNT(*) FROM blocklist").fetchone()[0]
    assert after == before, f"empty-URL submit should not insert (was {before}, now {after})"


def test_blocklist_delete_single_row(env):
    """DELETE one specific row by id — neighbors must survive."""
    client = _client()
    csrf = _csrf_kwargs("bl-del")

    with sqlite3.connect(env['db_path']) as c:
        target_id = c.execute(
            "SELECT id FROM blocklist WHERE torrent_url='http://stub/a.torrent'"
        ).fetchone()[0]

    r = client.post(f"/blocklist/{target_id}/delete", **csrf, follow_redirects=False)
    assert r.status_code in (303, 200), r.text

    with sqlite3.connect(env['db_path']) as c:
        gone = c.execute("SELECT COUNT(*) FROM blocklist WHERE id=?", (target_id,)).fetchone()[0]
        rest = c.execute("SELECT COUNT(*) FROM blocklist WHERE id!=?", (target_id,)).fetchone()[0]
    assert gone == 0, "target row must be deleted"
    assert rest == 2, f"other 2 rows must survive, got {rest}"


def test_blocklist_clear_all_empties_table(env):
    """clear-all wipes every row regardless of series_id."""
    client = _client()
    csrf = _csrf_kwargs("bl-clear")

    r = client.post("/blocklist/clear-all", **csrf, follow_redirects=False)
    assert r.status_code in (303, 200), r.text

    with sqlite3.connect(env['db_path']) as c:
        remaining = c.execute("SELECT COUNT(*) FROM blocklist").fetchone()[0]
    assert remaining == 0, (
        f"blocklist must be empty after clear-all, {remaining} row(s) remain"
    )

    # Series intact (clear-all is blocklist-scoped, must not cascade)
    with sqlite3.connect(env['db_path']) as c:
        s_count = c.execute("SELECT COUNT(*) FROM series").fetchone()[0]
    assert s_count == 2, "clear-all must NOT touch series table"


def test_blocklist_delete_unknown_id_does_not_500(env):
    """DELETE for a non-existent blocklist id is a no-op, not a 500."""
    client = _client()
    csrf = _csrf_kwargs("bl-missing")

    r = client.post("/blocklist/99999/delete", **csrf, follow_redirects=False)
    assert r.status_code in (303, 200), (
        f"missing-id delete should redirect cleanly, got {r.status_code}: {r.text}"
    )

    with sqlite3.connect(env['db_path']) as c:
        remaining = c.execute("SELECT COUNT(*) FROM blocklist").fetchone()[0]
    assert remaining == 3, "no blocklist rows should have changed"
