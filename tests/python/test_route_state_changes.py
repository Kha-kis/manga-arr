"""HTTP-level integration tests for state-changing routes.

The audit's coverage matrix flagged these routes as having unit tests
(or none) but no HTTP-level integration: a 200 response that silently
fails to persist, or persists to the wrong row, would only surface in
production. Most of these routes are tied directly to a UI button —
the user clicks, sees the response, and assumes the DB updated.

Covers:
  - Volume actions: mark-downloaded, mark-wanted, reset-to-wanted,
    toggle-monitor
  - Chapter map editor: save (JSON body), reset
  - History: mark-failed, delete single, clear-failed
  - Queue actions: orphaned-volume reset (the most common path)
  - Tags: rename, delete
  - Import lists: create, edit, delete, single-list sync trigger
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401


@pytest.fixture
def env(tmp_path):
    """Fresh DB seeded with one series, three volumes (states: wanted,
    grabbed, downloaded), tags, history, and an import list."""
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-state-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    orig_main_config = main.CONFIG
    orig_main_config_values = dict(main.CONFIG)
    orig_shared_config = shared.CONFIG
    orig_shared_config_values = dict(shared.CONFIG)
    orig_secret_cipher = security._SECRET_CIPHER
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
        c.execute("INSERT INTO root_folders(id, path) VALUES(1, ?)", (str(library_root),))
        c.execute(
            "INSERT INTO series(id, title, search_pattern, edition_type, enabled,"
            " monitored, monitor_mode, root_folder_id)"
            " VALUES(1, 'StateSeries', 'StateSeries', 'standard', 1, 1, 'all', 1)"
        )
        # Three volumes: vol 1 wanted, vol 2 grabbed (with download_id+source_url),
        # vol 3 downloaded
        c.execute(
            "INSERT INTO volumes(id, series_id, volume_num, status, monitored)"
            " VALUES(11, 1, 1.0, 'wanted', 1),"
            "       (12, 1, 2.0, 'grabbed', 1),"
            "       (13, 1, 3.0, 'downloaded', 1)"
        )
        c.execute(
            "UPDATE volumes SET source_url='http://stub/v2.torrent',"
            " download_id='dl-vol2', torrent_name='StateSeries v02',"
            " indexer='Indexer', protocol='torrent', client='Qbit'"
            " WHERE id=12"
        )
        c.execute(
            "INSERT INTO seen(torrent_url, series_id, volume_num, indexer, protocol)"
            " VALUES('http://stub/v2.torrent', 1, 2.0, 'Indexer', 'torrent')"
        )
        # Tags
        c.execute(
            "INSERT INTO series_tags(series_id, tag) VALUES(1, 'shounen'), (1, 'completed')"
        )
        # History row (grabbed) for mark-failed test
        c.execute(
            "INSERT INTO history(id, event_type, series_id, source_title,"
            " download_id, indexer, protocol, size_bytes)"
            " VALUES(101, 'grabbed', 1, 'StateSeries v04 [Group]', 'dl-h101',"
            " 'Indexer', 'torrent', 100000000)"
        )
        c.execute(
            "INSERT INTO history(id, event_type, series_id, source_title)"
            " VALUES(102, 'grab_failed', 1, 'old failed grab'),"
            "       (103, 'import_failed', 1, 'old failed import'),"
            "       (104, 'series_added', 1, 'log row to keep')"
        )
        # Import list
        c.execute(
            "INSERT INTO import_lists(id, name, type, enabled, settings)"
            " VALUES(50, 'TestList', 'anilist_user', 1, '{}')"
        )

    try:
        yield {
            'db_path': db.name,
            'library_root': str(library_root),
        }
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        main.CONFIG = orig_main_config
        main.CONFIG.clear()
        main.CONFIG.update(orig_main_config_values)
        shared.CONFIG = orig_shared_config
        shared.CONFIG.clear()
        shared.CONFIG.update(orig_shared_config_values)
        security._SECRET_CIPHER = orig_secret_cipher
        shutil.rmtree(key_dir)
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


@pytest.fixture
def _config_restoration_probe():
    """Verify the route fixture restores process globals and key directories."""
    import main, security, shared

    original_main_config = main.CONFIG
    original_main_values = dict(main.CONFIG)
    original_shared_config = shared.CONFIG
    original_shared_values = dict(shared.CONFIG)
    original_secret_cipher = security._SECRET_CIPHER
    original_key_dirs = set(
        Path(tempfile.gettempdir()).glob("mangarr-state-keys-*")
    )
    yield
    assert main.CONFIG is original_main_config
    assert main.CONFIG == original_main_values
    assert shared.CONFIG is original_shared_config
    assert shared.CONFIG == original_shared_values
    assert security._SECRET_CIPHER is original_secret_cipher
    assert (
        set(Path(tempfile.gettempdir()).glob("mangarr-state-keys-*"))
        == original_key_dirs
    )


def _client():
    import main
    from fastapi.testclient import TestClient
    return TestClient(main.app)


def _csrf_kwargs(tag: str = "test"):
    tok = f"csrf-{tag}-" + "x" * 30
    return {
        'cookies': {'csrftoken': tok},
        'headers': {'X-CSRFToken': tok},
    }


def _seed_volume_publication(
    env,
    state: str,
    *,
    volume_id: int = 13,
) -> tuple[int, str]:
    file_path = os.path.join(
        env["library_root"],
        f"StateSeries v{volume_id - 10:02d}.cbz",
    )
    with open(file_path, "wb") as stream:
        stream.write(b"published-volume")

    with sqlite3.connect(env["db_path"]) as c:
        c.execute(
            "UPDATE volumes SET import_path=?, torrent_name='Published release'"
            " WHERE id=?",
            (file_path, volume_id),
        )
        volume_num = c.execute(
            "SELECT volume_num FROM volumes WHERE id=?",
            (volume_id,),
        ).fetchone()[0]
        queue_id = 700 + volume_id
        queue_status = (
            "importing"
            if state in {
                "staging",
                "prepared",
                "publishing",
                "published",
                "db_committed",
                "cleaning",
            }
            else "imported"
        )
        c.execute(
            "INSERT INTO import_queue(id, series_id, volume_num, status)"
            " VALUES(?, 1, ?, ?)",
            (queue_id, volume_num, queue_status),
        )
        publication_id = c.execute(
            """
            INSERT INTO import_publications(
                queue_id, state, owner_token, series_id, dst_dir, import_mode,
                staging_dir, queue_snapshot_json, series_tags_json,
                queue_status, queue_volume_num
            ) VALUES(
                ?, ?, 'publication-owner', 1, ?, 'copy', ?,
                '{}', '[]', 'importing', ?
            )
            """,
            (
                queue_id,
                state,
                env["library_root"],
                os.path.join(
                    env["library_root"],
                    f".mangarr-publication-{queue_id}",
                ),
                volume_num,
            ),
        ).lastrowid
        c.execute(
            """
            INSERT INTO import_publication_files(
                publication_id, ordinal, file_id, src_path, filename,
                dst_path, final_path, import_kind, file_type, proposed_vol,
                is_special, has_volume_range, is_legacy_chapter_stub,
                is_legacy_chapter_recheck, plan_status
            ) VALUES(
                ?, 0, ?, '/downloads/source.cbz', 'StateSeries.cbz',
                ?, ?, 'volume', 'volume', ?, 0, 0, 0, 0, 'ready'
            )
            """,
            (
                publication_id,
                queue_id,
                file_path,
                file_path,
                volume_num,
            ),
        )
    return publication_id, file_path


def _volume_delete_domain(db_path: str, volume_id: int = 13):
    with sqlite3.connect(db_path) as c:
        return {
            "volume": c.execute(
                "SELECT status, import_path, download_id, torrent_name"
                " FROM volumes WHERE id=?",
                (volume_id,),
            ).fetchone(),
            "chapters": c.execute(
                "SELECT id, status, import_path, download_id"
                " FROM chapters WHERE volume_id=? ORDER BY id",
                (volume_id,),
            ).fetchall(),
            "history": c.execute(
                "SELECT event_type, series_id, data FROM history ORDER BY id"
            ).fetchall(),
            "events": c.execute(
                "SELECT event_type, series_id, message FROM events ORDER BY id"
            ).fetchall(),
            "publication": c.execute(
                "SELECT state, operation_owner, operation_expires_at"
                " FROM import_publications ORDER BY id"
            ).fetchall(),
        }


# ───────────────────── volume actions ─────────────────────


def test_volume_mark_downloaded_persists_status(env):
    """POST mark-downloaded must move status='wanted' → 'downloaded' AND
    set imported_at. Silent-correctness mode: returns 200, status unchanged."""
    client = _client()
    csrf = _csrf_kwargs("mark-dl")

    r = client.post("/series/1/volumes/11/mark-downloaded", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        v = c.execute("SELECT status, imported_at FROM volumes WHERE id=11").fetchone()
        assert v['status'] == 'downloaded', f"status should be 'downloaded', got {v['status']!r}"
        assert v['imported_at'] is not None, "imported_at must be set"


def test_volume_mark_wanted_clears_grab_state(env):
    """POST mark-wanted on a 'grabbed' volume must clear download_id,
    source_url, indexer, protocol, etc. AND remove the seen row.
    Silent-correctness mode: status changes to wanted but download_id
    remains, leaving the volume in zombie state."""
    client = _client()
    csrf = _csrf_kwargs("mark-w")

    r = client.post("/series/1/volumes/12/mark-wanted", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        v = c.execute(
            "SELECT status, source_url, download_id, indexer, protocol,"
            " torrent_name, release_group FROM volumes WHERE id=12"
        ).fetchone()
        assert v['status'] == 'wanted'
        assert v['source_url'] is None
        assert v['download_id'] is None
        assert v['indexer'] is None
        assert v['protocol'] is None
        assert v['torrent_name'] is None
        # And the seen row must be gone (otherwise the URL is permanently blocklisted)
        seen = c.execute(
            "SELECT 1 FROM seen WHERE torrent_url='http://stub/v2.torrent'"
        ).fetchone()
        assert seen is None, "seen row must be deleted so the URL can be re-grabbed"


def test_volume_reset_to_wanted_clears_grab_state(env):
    """reset-to-wanted is the queue-page version of mark-wanted; same
    invariants — only fires on status='grabbed'."""
    client = _client()
    csrf = _csrf_kwargs("reset-w")

    r = client.post("/series/1/volumes/12/reset-to-wanted", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        v = c.execute(
            "SELECT status, download_id, source_url, indexer, release_group"
            " FROM volumes WHERE id=12"
        ).fetchone()
        assert v['status'] == 'wanted'
        assert v['download_id'] is None
        assert v['source_url'] is None
        assert v['indexer'] is None


def test_volume_reset_to_wanted_no_op_on_downloaded(env):
    """The route guards on status='grabbed' — calling on a 'downloaded'
    volume must NOT clobber its state."""
    client = _client()
    csrf = _csrf_kwargs("reset-noop")

    r = client.post("/series/1/volumes/13/reset-to-wanted", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        v = c.execute("SELECT status FROM volumes WHERE id=13").fetchone()
        assert v['status'] == 'downloaded', (
            "reset-to-wanted must guard on grabbed status only — downloaded "
            "volumes must NOT be reset"
        )


def test_volume_toggle_monitor_flips_bit(env):
    """Toggle from monitored=1 → 0 → 1 across two POSTs."""
    client = _client()
    csrf = _csrf_kwargs("toggle-mon")

    r1 = client.post("/series/1/volumes/11/toggle-monitor", **csrf, follow_redirects=False)
    assert r1.status_code in (200, 303)
    with sqlite3.connect(env['db_path']) as c:
        m = c.execute("SELECT monitored FROM volumes WHERE id=11").fetchone()[0]
    assert m == 0, f"first toggle should set 1→0, got {m}"

    r2 = client.post("/series/1/volumes/11/toggle-monitor", **csrf, follow_redirects=False)
    assert r2.status_code in (200, 303)
    with sqlite3.connect(env['db_path']) as c:
        m = c.execute("SELECT monitored FROM volumes WHERE id=11").fetchone()[0]
    assert m == 1, f"second toggle should set 0→1, got {m}"


def test_state_route_fixture_restores_global_config(
    _config_restoration_probe,
    env,
):
    """This module must not leak its temporary DB config into later suites."""
    assert env["db_path"]


@pytest.mark.parametrize("htmx", [False, True], ids=["plain", "htmx"])
def test_volume_file_delete_active_publication_blocks_without_mutation(env, htmx):
    """An active journal retains both its filesystem and database authority."""
    _, file_path = _seed_volume_publication(env, "staging")
    before = _volume_delete_domain(env["db_path"])
    csrf = _csrf_kwargs(f"delete-active-{htmx}")
    headers = dict(csrf["headers"])
    if htmx:
        headers["HX-Request"] = "true"

    response = _client().post(
        "/series/1/volumes/13/delete-file",
        cookies=csrf["cookies"],
        headers=headers,
        follow_redirects=False,
    )

    if htmx:
        assert response.status_code == 200
        toast = json.loads(response.headers["HX-Trigger"])["showToast"]
        assert toast["type"] == "warning"
        assert "Import is in progress" in toast["msg"]
    else:
        assert response.status_code == 303
        assert "flash_type=warning" in response.headers["location"]
    assert os.path.exists(file_path)
    with open(file_path, "rb") as stream:
        assert stream.read() == b"published-volume"
    assert _volume_delete_domain(env["db_path"]) == before


@pytest.mark.parametrize(
    ("range_start", "range_end", "pack_type"),
    [(2.0, 4.0, "volume_range"), (None, None, "complete")],
    ids=["volume-range", "complete-pack"],
)
def test_volume_file_delete_active_publication_file_coverage_blocks(
    env,
    range_start,
    range_end,
    pack_type,
):
    """File range/pack coverage fences deletion without scalar or path matches."""
    _, file_path = _seed_volume_publication(env, "staging")
    with sqlite3.connect(env["db_path"]) as db:
        db.execute(
            "UPDATE volumes SET download_id='volume-download' WHERE id=13"
        )
        db.execute(
            "UPDATE import_publications"
            " SET queue_volume_num=9.0,"
            " queue_download_id='different-download'"
        )
        db.execute(
            """
            UPDATE import_publication_files
            SET proposed_vol=9.0,
                vol_range_start=?,
                vol_range_end=?,
                pack_type=?,
                dst_path='/unrelated/destination.cbz',
                final_path='/unrelated/final.cbz'
            """,
            (range_start, range_end, pack_type),
        )
    before = _volume_delete_domain(env["db_path"])
    csrf = _csrf_kwargs(f"delete-active-{pack_type}")

    response = _client().post(
        "/series/1/volumes/13/delete-file",
        cookies=csrf["cookies"],
        headers=csrf["headers"],
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "flash_type=warning" in response.headers["location"]
    assert os.path.exists(file_path)
    assert _volume_delete_domain(env["db_path"]) == before
    with sqlite3.connect(env["db_path"]) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM volume_file_deletions"
        ).fetchone() == (0,)


@pytest.mark.parametrize("htmx", [False, True], ids=["plain", "htmx"])
def test_volume_file_delete_pending_warns_and_defers_history(
    env,
    monkeypatch,
    htmx,
):
    """A blocked replay is visible and emits history only after completion."""
    import volume_file_deletion

    file_path = os.path.join(env["library_root"], "StateSeries v03.cbz")
    with open(file_path, "wb") as stream:
        stream.write(b"pending-delete")
    with sqlite3.connect(env["db_path"]) as db:
        db.execute(
            "UPDATE volumes SET import_path=?, torrent_name='Pending release'"
            " WHERE id=13",
            (file_path,),
        )

    real_unlink = volume_file_deletion._unlink_claim

    def fail_unlink(_path):
        raise OSError("simulated unlink failure")

    monkeypatch.setattr(volume_file_deletion, "_unlink_claim", fail_unlink)
    csrf = _csrf_kwargs(f"delete-pending-{htmx}")
    headers = dict(csrf["headers"])
    if htmx:
        headers["HX-Request"] = "true"

    response = _client().post(
        "/series/1/volumes/13/delete-file",
        cookies=csrf["cookies"],
        headers=headers,
        follow_redirects=False,
    )

    if htmx:
        assert response.status_code == 200
        toast = json.loads(response.headers["HX-Trigger"])["showToast"]
        message = toast["msg"]
        assert toast["type"] == "warning"
    else:
        assert response.status_code == 303
        from urllib.parse import parse_qs, urlsplit

        query = parse_qs(urlsplit(response.headers["location"]).query)
        message = query["flash_msg"][0]
        assert query["flash_type"] == ["warning"]
    assert "pending recovery" in message
    assert "simulated unlink failure" in message

    with sqlite3.connect(env["db_path"]) as db:
        journal = db.execute(
            "SELECT id, state, claim_path FROM volume_file_deletions"
        ).fetchone()
        assert journal is not None
        assert journal[1] == "active"
        assert Path(journal[2]).exists()
        assert db.execute(
            "SELECT COUNT(*) FROM history WHERE event_type='file_deleted'"
        ).fetchone()[0] == 0

    monkeypatch.setattr(volume_file_deletion, "_unlink_claim", real_unlink)
    assert volume_file_deletion.replay_volume_file_deletion(journal[0]) == "completed"
    with sqlite3.connect(env["db_path"]) as db:
        assert db.execute(
            "SELECT state FROM volume_file_deletions WHERE id=?",
            (journal[0],),
        ).fetchone() == ("completed",)
        assert db.execute(
            "SELECT COUNT(*) FROM history WHERE event_type='file_deleted'"
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    ("status", "lease_owner"),
    [("importing", None), ("pending", "pre-journal-owner")],
)
def test_volume_file_delete_blocks_prejournal_import_without_mutation(
    env,
    status,
    lease_owner,
):
    """A queue owner is authoritative before its publication row exists."""
    file_path = os.path.join(env["library_root"], "StateSeries v03.cbz")
    with open(file_path, "wb") as stream:
        stream.write(b"pre-journal-import")
    with sqlite3.connect(env["db_path"]) as c:
        c.execute(
            "UPDATE volumes SET import_path=?, torrent_name='Pre-journal release'"
            " WHERE id=13",
            (file_path,),
        )
        c.execute(
            "INSERT INTO import_queue(id, series_id, volume_num, status,"
            " lease_owner) VALUES(713, 1, 3.0, ?, ?)",
            (status, lease_owner),
        )
    before = _volume_delete_domain(env["db_path"])

    response = _client().post(
        "/series/1/volumes/13/delete-file",
        **_csrf_kwargs(f"delete-prejournal-{status}-{lease_owner}"),
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "flash_type=warning" in response.headers["location"]
    assert os.path.exists(file_path)
    assert _volume_delete_domain(env["db_path"]) == before
    with sqlite3.connect(env["db_path"]) as c:
        assert c.execute(
            "SELECT COUNT(*) FROM volume_file_deletions"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("state", ["finalized", "deleted"])
def test_volume_file_delete_terminal_publication_permits_delete(env, state):
    """Terminal journals no longer own the volume or its published path."""
    _, file_path = _seed_volume_publication(env, state)
    csrf = _csrf_kwargs(f"delete-terminal-{state}")

    response = _client().post(
        "/series/1/volumes/13/delete-file",
        **csrf,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert not os.path.exists(file_path)
    with sqlite3.connect(env["db_path"]) as c:
        volume = c.execute(
            "SELECT status, import_path, torrent_name FROM volumes WHERE id=13"
        ).fetchone()
        publication_state = c.execute(
            "SELECT state FROM import_publications"
        ).fetchone()[0]
    assert volume == ("wanted", None, None)
    assert publication_state == state


def test_volume_file_delete_and_publication_claim_have_one_winner(
    env,
    monkeypatch,
):
    """A committed deletion reservation prevents a later queue claim."""
    import volume_file_deletion
    from import_lease import claim_import_queue_row

    file_path = os.path.join(env["library_root"], "StateSeries v03.cbz")
    with open(file_path, "wb") as stream:
        stream.write(b"delete-race")
    with sqlite3.connect(env["db_path"]) as c:
        c.execute(
            "UPDATE volumes SET import_path=?, torrent_name='Race release'"
            " WHERE id=13",
            (file_path,),
        )
        c.execute(
            "INSERT INTO import_queue(id, series_id, volume_num, status)"
            " VALUES(713, 1, 3.0, 'pending')"
        )

    csrf = _csrf_kwargs("delete-claim-race")
    reservation_committed = threading.Event()
    release_filesystem = threading.Event()
    real_rename = volume_file_deletion._rename_noreplace

    def paused_rename(source, destination):
        reservation_committed.set()
        assert release_filesystem.wait(timeout=5)
        real_rename(source, destination)

    monkeypatch.setattr(volume_file_deletion, "_rename_noreplace", paused_rename)

    def claim_import():
        assert reservation_committed.wait(timeout=5)
        with sqlite3.connect(env["db_path"], timeout=5) as c:
            c.execute("BEGIN IMMEDIATE")
            return claim_import_queue_row(c, 713, "race-owner")

    def delete_file():
        return _client().post(
            "/series/1/volumes/13/delete-file",
            **csrf,
            follow_redirects=False,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        delete_future = pool.submit(delete_file)
        assert reservation_committed.wait(timeout=5)
        claim_future = pool.submit(claim_import)
        claim_won = claim_future.result(timeout=5)
        release_filesystem.set()
        response = delete_future.result(timeout=5)

    delete_won = not os.path.exists(file_path)
    assert sum((claim_won, delete_won)) == 1
    assert delete_won
    assert response.status_code == 303
    with sqlite3.connect(env["db_path"]) as c:
        assert c.execute(
            "SELECT COUNT(*) FROM import_publications"
        ).fetchone()[0] == 0
        assert c.execute(
            "SELECT status, import_path FROM volumes WHERE id=13"
        ).fetchone() == ("wanted", None)


def test_volume_file_delete_slow_cleanup_does_not_hold_writer(
    env,
    monkeypatch,
):
    """A concurrent low-timeout writer succeeds while detached cleanup pauses."""
    import volume_file_deletion

    file_path = os.path.join(env["library_root"], "StateSeries v03.cbz")
    with open(file_path, "wb") as stream:
        stream.write(b"slow-delete")
    with sqlite3.connect(env["db_path"]) as c:
        c.execute(
            "UPDATE volumes SET import_path=?, torrent_name='Slow release'"
            " WHERE id=13",
            (file_path,),
        )

    cleanup_started = threading.Event()
    cleanup_release = threading.Event()
    real_unlink = volume_file_deletion._unlink_claim

    def slow_unlink(path):
        cleanup_started.set()
        assert cleanup_release.wait(timeout=5)
        real_unlink(path)

    monkeypatch.setattr(volume_file_deletion, "_unlink_claim", slow_unlink)
    csrf = _csrf_kwargs("slow-volume-delete")

    def delete_file():
        return _client().post(
            "/series/1/volumes/13/delete-file",
            **csrf,
            follow_redirects=False,
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(delete_file)
        assert cleanup_started.wait(timeout=2), "delete never reached detached cleanup"
        try:
            with sqlite3.connect(env["db_path"], timeout=0.05) as writer:
                writer.execute("PRAGMA busy_timeout=50")
                writer.execute(
                    "UPDATE series SET title='Concurrent Writer' WHERE id=1"
                )
        finally:
            cleanup_release.set()
        response = future.result(timeout=5)

    assert response.status_code == 303
    assert not os.path.exists(file_path)
    with sqlite3.connect(env["db_path"]) as c:
        assert c.execute(
            "SELECT title FROM series WHERE id=1"
        ).fetchone()[0] == "Concurrent Writer"
        assert c.execute(
            "SELECT status, import_path FROM volumes WHERE id=13"
        ).fetchone() == ("wanted", None)


def test_set_pack_range_non_htmx_redirect_does_not_500(env):
    """The non-HTMX fallback used to reference an undefined `request`
    after persisting the range, causing a 500 instead of redirecting."""
    client = _client()
    csrf = _csrf_kwargs("set-range")

    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO volumes(id, series_id, volume_num, status, monitored,"
            " torrent_name) VALUES(20, 1, NULL, 'grabbed', 1, 'Pack v01-03')"
        )

    r = client.post(
        "/series/1/volumes/20/set-range",
        data={"vol_range_start": "1", "vol_range_end": "3", "mark_stubs": "1"},
        **csrf,
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        pack = c.execute(
            "SELECT vol_range_start, vol_range_end FROM volumes WHERE id=20"
        ).fetchone()
    assert pack['vol_range_start'] == 1
    assert pack['vol_range_end'] == 3


def test_chapter_grab_non_htmx_redirect_does_not_500(env, monkeypatch):
    """The non-HTMX fallback used `len(chs)` even though this route queues
    exactly one chapter."""
    import routers.series_ as series_router

    async def _noop_grab_chapter_task(*a, **kw):
        return None

    monkeypatch.setattr(series_router, "_grab_chapter_task", _noop_grab_chapter_task)

    client = _client()
    csrf = _csrf_kwargs("chapter-grab")
    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO chapters(id, series_id, chapter_num, status, monitored)"
            " VALUES(30, 1, 1.0, 'wanted', 1)"
        )

    r = client.post("/series/1/chapters/30/grab", **csrf, follow_redirects=False)
    assert r.status_code == 303, r.text
    assert "series/1" in r.headers["location"]


def test_uncollected_toggle_non_htmx_redirect_does_not_500(env):
    """The non-HTMX fallback used an undefined `wanted` variable after
    toggling monitor state for uncollected chapters."""
    client = _client()
    csrf = _csrf_kwargs("uncollected-toggle")
    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO chapters(id, series_id, chapter_num, status, monitored,"
            " volume_id) VALUES(31, 1, 2.0, 'wanted', 1, NULL)"
        )

    r = client.post(
        "/series/1/uncollected/toggle-monitor",
        **csrf,
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text

    with sqlite3.connect(env['db_path']) as c:
        monitored = c.execute(
            "SELECT monitored FROM chapters WHERE id=31"
        ).fetchone()[0]
    assert monitored == 0


# ───────────────────── chapter map editor ─────────────────────


def test_chapter_map_save_persists_overrides(env):
    """POST JSON {overrides: {chapter: volume}} replaces the override set."""
    client = _client()
    csrf = _csrf_kwargs("cmap-save")

    payload = {'overrides': {'5': 1, '5.5': 1, '10': 2, 'extra': None}}
    r = client.post(
        "/series/1/chapter-map",
        json=payload,
        **csrf,
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}

    with sqlite3.connect(env['db_path']) as c:
        rows = c.execute(
            "SELECT chapter, volume_num FROM series_chapter_overrides"
            " WHERE series_id=1 ORDER BY chapter"
        ).fetchall()
    overrides = {r[0]: r[1] for r in rows}
    assert overrides == {'10': 2.0, '5': 1.0, '5.5': 1.0, 'extra': None}, (
        f"overrides must persist all 4 entries (None preserved as NULL), got {overrides!r}"
    )


def test_chapter_map_save_replaces_old_overrides(env):
    """Saving twice must replace, not accumulate — each save is a full
    state replacement of the override set."""
    client = _client()
    csrf = _csrf_kwargs("cmap-replace")

    # First save
    client.post("/series/1/chapter-map",
                json={'overrides': {'1': 1, '2': 1}}, **csrf)
    # Second save with smaller set
    r = client.post("/series/1/chapter-map",
                    json={'overrides': {'1': 2}}, **csrf)
    assert r.status_code == 200

    with sqlite3.connect(env['db_path']) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM series_chapter_overrides WHERE series_id=1"
        ).fetchone()[0]
    assert n == 1, (
        f"second save should replace, not accumulate — expected 1 row, got {n}"
    )


def test_chapter_map_reset_clears_overrides(env):
    """POST chapter-map/reset deletes all override rows for the series."""
    client = _client()
    csrf = _csrf_kwargs("cmap-reset")

    # Seed some overrides first
    client.post("/series/1/chapter-map",
                json={'overrides': {'1': 1, '2': 2}}, **csrf)

    r = client.post("/series/1/chapter-map/reset", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM series_chapter_overrides WHERE series_id=1"
        ).fetchone()[0]
    assert n == 0, f"reset must clear all overrides, {n} remain"


# ───────────────────── history mutations ─────────────────────


def test_history_delete_single_row(env):
    """POST /history/{id}/delete removes one row, others survive."""
    client = _client()
    csrf = _csrf_kwargs("hist-del")

    r = client.post("/history/102/delete", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        gone = c.execute("SELECT COUNT(*) FROM history WHERE id=102").fetchone()[0]
        rest = c.execute("SELECT COUNT(*) FROM history WHERE id != 102").fetchone()[0]
    assert gone == 0
    assert rest == 3, f"3 other history rows should survive, got {rest}"


def test_history_clear_failed_only_removes_failed(env):
    """clear-failed removes import_failed + grab_failed, NOT other event types."""
    client = _client()
    csrf = _csrf_kwargs("hist-clear")

    r = client.post("/history/clear-failed", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("SELECT id, event_type FROM history").fetchall()
    by_type = {r['id']: r['event_type'] for r in rows}
    # 102 (grab_failed) and 103 (import_failed) must be gone
    assert 102 not in by_type, "grab_failed must be cleared"
    assert 103 not in by_type, "import_failed must be cleared"
    # 101 (grabbed) and 104 (series_added) must survive
    assert 101 in by_type, "grabbed history must survive"
    assert 104 in by_type, "series_added must survive (not a failure)"


def test_history_mark_failed_creates_blocklist_entry(env):
    """Marking a 'grabbed' history row as failed must INSERT into blocklist
    AND change the history event_type to 'grab_failed'."""
    client = _client()
    csrf = _csrf_kwargs("hist-mf")

    before_bl = None
    with sqlite3.connect(env['db_path']) as c:
        before_bl = c.execute("SELECT COUNT(*) FROM blocklist").fetchone()[0]

    r = client.post("/history/101/mark-failed", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        h = c.execute("SELECT event_type FROM history WHERE id=101").fetchone()
        assert h['event_type'] == 'grab_failed', (
            f"history row should flip to grab_failed, got {h['event_type']!r}"
        )
        bl_count = c.execute("SELECT COUNT(*) FROM blocklist").fetchone()[0]
    assert bl_count == before_bl + 1, (
        f"mark-failed must add a blocklist row (was {before_bl}, now {bl_count})"
    )


# ───────────────────── queue actions ─────────────────────


def test_queue_reset_orphaned_volume(env):
    """POST /queue/grabbed/{vol_id}/reset returns a grabbed volume to
    'wanted' AND removes the seen row (so it can be re-grabbed)."""
    client = _client()
    csrf = _csrf_kwargs("q-reset")

    r = client.post("/queue/grabbed/12/reset", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        v = c.execute("SELECT status, download_id FROM volumes WHERE id=12").fetchone()
        seen = c.execute(
            "SELECT 1 FROM seen WHERE torrent_url='http://stub/v2.torrent'"
        ).fetchone()
    assert v['status'] == 'wanted', f"volume must be wanted, got {v['status']!r}"
    assert v['download_id'] is None
    assert seen is None, "seen row must be cleared so URL can be re-grabbed"


# ───────────────────── tag mutations ─────────────────────


def test_tag_rename(env):
    """POST /api/tags/rename updates every series_tag row with the old tag."""
    client = _client()
    csrf = _csrf_kwargs("tag-rename")

    r = client.post(
        "/api/tags/rename",
        data={'csrf_token': csrf['headers']['X-CSRFToken'],
              'old_name': 'shounen', 'new_name': 'shonen-action'},
        **csrf,
        follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        old_count = c.execute(
            "SELECT COUNT(*) FROM series_tags WHERE tag='shounen'"
        ).fetchone()[0]
        new_count = c.execute(
            "SELECT COUNT(*) FROM series_tags WHERE tag='shonen-action'"
        ).fetchone()[0]
    assert old_count == 0, "old tag must be gone after rename"
    assert new_count == 1, f"new tag must exist, got {new_count} rows"


def test_tag_delete(env):
    """POST /api/tags/{tag}/delete removes every series_tag row for that tag."""
    client = _client()
    csrf = _csrf_kwargs("tag-del")

    r = client.post("/api/tags/completed/delete", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        gone = c.execute(
            "SELECT COUNT(*) FROM series_tags WHERE tag='completed'"
        ).fetchone()[0]
        other = c.execute(
            "SELECT COUNT(*) FROM series_tags WHERE tag='shounen'"
        ).fetchone()[0]
    assert gone == 0
    assert other == 1, "other tags must survive"


# ───────────────────── import list CRUD ─────────────────────


def test_import_list_create(env):
    """POST /import-lists creates a new list row."""
    client = _client()
    csrf = _csrf_kwargs("il-create")

    r = client.post(
        "/import-lists",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'name': 'NewList',
            'type': 'mal_user',
            'enabled': '1',
            'monitor_mode': 'all',
            'settings': '{"username":"alice"}',
        },
        **csrf,
        follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT name, type, enabled, settings FROM import_lists"
            " WHERE name='NewList'"
        ).fetchone()
    assert row is not None, "new list must be inserted"
    assert row['type'] == 'mal_user'
    assert row['enabled'] == 1
    assert json.loads(row['settings']) == {"username": "alice"}


def test_import_list_edit(env):
    """POST /import-lists/{id} updates an existing row."""
    client = _client()
    csrf = _csrf_kwargs("il-edit")

    r = client.post(
        "/import-lists/50",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'name': 'EditedList',
            'type': 'anilist_user',
            'enabled': '0',  # flipped
            'monitor_mode': 'recent',
            'settings': '{"updated":true}',
        },
        **csrf,
        follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT name, enabled, monitor_mode, settings FROM import_lists WHERE id=50"
        ).fetchone()
    assert row['name'] == 'EditedList'
    assert row['enabled'] == 0, "enabled must persist as 0"
    assert row['monitor_mode'] == 'recent'
    assert json.loads(row['settings']) == {"updated": True}


def test_import_list_delete(env):
    """POST /import-lists/{id}/delete removes the row."""
    client = _client()
    csrf = _csrf_kwargs("il-del")

    r = client.post("/import-lists/50/delete", **csrf, follow_redirects=False)
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        n = c.execute("SELECT COUNT(*) FROM import_lists WHERE id=50").fetchone()[0]
    assert n == 0, "list must be deleted"
