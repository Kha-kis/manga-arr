"""Regression coverage for manual/discovery live-import lease bypasses."""

from __future__ import annotations

import asyncio
import ast
import inspect
import json
import os
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterator, TypedDict

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401


class CsrfArgs(TypedDict):
    cookies: dict[str, str]
    headers: dict[str, str]


@pytest.fixture
def lease_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[str]:
    """Create a real SQLite database and restore every touched singleton."""
    import import_execute
    import main
    import security
    import shared

    db_path = str(tmp_path / "lease-bypass.db")
    old_main_config_object = main.CONFIG
    old_main_config_values = dict(main.CONFIG)
    old_shared_config_object = shared.CONFIG
    old_shared_config_values = dict(shared.CONFIG)
    old_cipher = security._SECRET_CIPHER
    old_sem = import_execute._IMPORT_SEM

    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(shared, "DB_PATH", db_path)
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(str(tmp_path / "keys"))
    import_execute._IMPORT_SEM = None
    main.init_db()
    main.load_config()
    main.ensure_api_key()

    library = tmp_path / "library"
    library.mkdir()
    with sqlite3.connect(db_path) as db:
        db.execute("DELETE FROM root_folders")
        db.execute(
            "INSERT INTO root_folders(id, path, is_default) VALUES(1, ?, 1)",
            (str(library),),
        )
        db.execute(
            "INSERT INTO series(id, title, search_pattern, root_folder_id)"
            " VALUES(1, 'Lease Series', 'Lease Series', 1)"
        )
        db.executemany(
            "INSERT INTO download_clients("
            "id,name,type,host,username,password,enabled,priority,category"
            ") VALUES(?,?,?,?,?,?,0,?,'manga')",
            (
                (
                    101,
                    "qBit primary",
                    "qbittorrent",
                    "http://qbit-primary.invalid",
                    "qbit-primary",
                    "qbit-primary-secret",
                    1,
                ),
                (
                    102,
                    "qBit secondary",
                    "qbittorrent",
                    "http://qbit-secondary.invalid",
                    "qbit-secondary",
                    "qbit-secondary-secret",
                    2,
                ),
                (
                    201,
                    "SAB primary",
                    "sabnzbd",
                    "http://sab-primary.invalid",
                    "",
                    "sab-primary-secret",
                    1,
                ),
                (
                    202,
                    "SAB secondary",
                    "sabnzbd",
                    "http://sab-secondary.invalid",
                    "",
                    "sab-secondary-secret",
                    2,
                ),
            ),
        )
        db.execute(
            "UPDATE download_clients SET enabled=1 WHERE id IN (101, 201)"
        )

    try:
        yield db_path
    finally:
        import_execute._IMPORT_SEM = old_sem
        security._SECRET_CIPHER = old_cipher
        main.CONFIG = old_main_config_object
        main.CONFIG.clear()
        main.CONFIG.update(old_main_config_values)
        shared.CONFIG = old_shared_config_object
        shared.CONFIG.clear()
        shared.CONFIG.update(old_shared_config_values)
        assert main.CONFIG is old_main_config_object
        assert main.CONFIG == old_main_config_values
        assert shared.CONFIG is old_shared_config_object
        assert shared.CONFIG == old_shared_config_values
        assert security._SECRET_CIPHER is old_cipher
        assert import_execute._IMPORT_SEM is old_sem


def _csrf(tag: str) -> CsrfArgs:
    token = f"csrf-{tag}-" + "x" * 30
    return {
        "cookies": {"csrftoken": token},
        "headers": {"X-CSRFToken": token},
    }


def _client() -> TestClient:
    import main

    return TestClient(main.app)


def _queue(
    db_path: str,
    *,
    download_id: str,
    status: str,
    child_status: str,
    download_client_id: int | None = None,
    owner: str | None = None,
    expired: bool = False,
    src_path: str = "/staging/keep.cbz",
) -> tuple[int, int]:
    with sqlite3.connect(db_path) as db:
        if owner is None:
            cur = db.execute(
                "INSERT INTO import_queue("
                "series_id, download_id, download_client_id, torrent_name,"
                " src_dir, status"
                ") VALUES(1, ?, ?, ?, '/staging', ?)",
                (download_id, download_client_id, download_id, status),
            )
        else:
            modifier = "-5 minutes" if expired else "+5 minutes"
            cur = db.execute(
                "INSERT INTO import_queue("
                "series_id, download_id, download_client_id, torrent_name,"
                " src_dir, status, lease_owner, lease_expires_at"
                ") VALUES(1, ?, ?, ?, '/staging', ?, ?, datetime('now', ?))",
                (
                    download_id,
                    download_client_id,
                    download_id,
                    status,
                    owner,
                    modifier,
                ),
            )
        queue_lastrowid = cur.lastrowid
        assert queue_lastrowid is not None
        queue_id = int(queue_lastrowid)
        child = db.execute(
            "INSERT INTO import_queue_files("
            "queue_id, filename, src_path, proposed_volume,"
            " proposed_import_kind, status"
            ") VALUES(?, 'keep.cbz', ?, 1, 'volume', ?)",
            (queue_id, src_path, child_status),
        )
        child_lastrowid = child.lastrowid
        assert child_lastrowid is not None
        return queue_id, int(child_lastrowid)


def _grabbed_domain(
    db_path: str,
    *,
    download_id: str,
    volume_num: float,
    client: str = "qbittorrent",
    protocol: str = "torrent",
    download_client_id: int | None = None,
    source_key: str | None = None,
) -> tuple[int, int, str]:
    source_url = f"https://source.invalid/{client}/{download_id}"
    if source_key is not None:
        source_url = f"{source_url}?owner={source_key}"
    with sqlite3.connect(db_path) as db:
        volume = db.execute(
            "INSERT INTO volumes("
            "series_id, volume_num, status, monitored, grabbed_at, download_id,"
            " download_client_id, source_url, torrent_name, client, protocol"
            ") VALUES(1, ?, 'grabbed', 1, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?)",
            (
                volume_num,
                download_id,
                download_client_id,
                source_url,
                download_id,
                client,
                protocol,
            ),
        )
        volume_lastrowid = volume.lastrowid
        assert volume_lastrowid is not None
        volume_id = int(volume_lastrowid)
        chapter = db.execute(
            "INSERT INTO chapters("
            "series_id, volume_id, chapter_num, status, monitored, download_id,"
            " download_client_id, torrent_url, client, protocol"
            ") VALUES(1, ?, ?, 'grabbed', 1, ?, ?, ?, ?, ?)",
            (
                volume_id,
                volume_num,
                download_id,
                download_client_id,
                source_url,
                client,
                protocol,
            ),
        )
        db.execute(
            "INSERT INTO seen("
            "torrent_url, torrent_name, series_id, volume_num, client, protocol,"
            " download_id, download_client_id"
            ") VALUES(?, ?, 1, ?, ?, ?, ?, ?)",
            (
                source_url,
                download_id,
                volume_num,
                client,
                protocol,
                download_id,
                download_client_id,
            ),
        )
        chapter_lastrowid = chapter.lastrowid
        assert chapter_lastrowid is not None
        return volume_id, int(chapter_lastrowid), source_url


def _claim_before_action(
    db_path: str,
    queue_id: int,
    action,
):
    """Hold the writer lock so the action races a claim on another connection."""
    from import_lease import claim_import_queue_row

    started = threading.Event()

    def _run_action():
        started.set()
        return action()

    owner_db = sqlite3.connect(db_path, timeout=30)
    owner_db.row_factory = sqlite3.Row
    owner_db.execute("PRAGMA busy_timeout=30000")
    owner_db.execute("BEGIN IMMEDIATE")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_run_action)
            assert started.wait(timeout=2)
            time.sleep(0.02)
            assert claim_import_queue_row(owner_db, queue_id, "worker-won")
            owner_db.commit()
            return future.result(timeout=5)
    finally:
        if owner_db.in_transaction:
            owner_db.rollback()
        owner_db.close()


def test_dismiss_skip_retry_and_process_lose_cleanly_to_concurrent_claim(
    lease_env: str,
) -> None:
    """Every manual path checks its parent CAS before changing child decisions."""
    from routers.import_ import (
        dismiss_import_queue_entry,
        retry_import_queue_entry,
        skip_import_queue_entry,
    )

    cases = (
        ("dismiss", "pending", "pending", dismiss_import_queue_entry),
        ("skip", "pending", "pending", skip_import_queue_entry),
        ("retry", "partial", "failed", retry_import_queue_entry),
    )
    for index, (name, status, child_status, action) in enumerate(cases, start=1):
        download_id = f"claim-race-{name}"
        queue_id, child_id = _queue(
            lease_env,
            download_id=download_id,
            status=status,
            child_status=child_status,
        )
        _grabbed_domain(
            lease_env,
            download_id=download_id,
            volume_num=float(index),
        )
        result = _claim_before_action(
            lease_env,
            queue_id,
            lambda action=action, queue_id=queue_id: action(queue_id),
        )
        assert result == {"ok": False, "status": "in_progress"}
        with sqlite3.connect(lease_env) as db:
            assert db.execute(
                "SELECT status, lease_owner FROM import_queue WHERE id=?",
                (queue_id,),
            ).fetchone() == ("importing", "worker-won")
            assert db.execute(
                "SELECT status FROM import_queue_files WHERE id=?",
                (child_id,),
            ).fetchone() == (child_status,)
            assert db.execute(
                "SELECT status FROM volumes WHERE download_id=?",
                (download_id,),
            ).fetchone() == ("grabbed",)
            assert db.execute(
                "SELECT COUNT(*) FROM seen WHERE download_id=?",
                (download_id,),
            ).fetchone()[0] == 1

    process_id, process_child = _queue(
        lease_env,
        download_id="claim-race-process",
        status="pending",
        child_status="needs_review",
    )

    def _post_process():
        return _client().post(
            f"/import/{process_id}/process",
            data={
                f"kind_{process_child}": "volume",
                f"vol_{process_child}": "9",
            },
            **_csrf("process-claim"),
            follow_redirects=False,
        )

    response = _claim_before_action(lease_env, process_id, _post_process)
    assert response.status_code == 303
    assert "in+progress" in response.headers["location"]
    with sqlite3.connect(lease_env) as db:
        assert db.execute(
            "SELECT status, proposed_volume FROM import_queue_files WHERE id=?",
            (process_child,),
        ).fetchone() == ("needs_review", 1.0)


def test_double_submit_cas_has_one_mutating_winner(lease_env: str) -> None:
    """Separate connections cannot both skip/dismiss/retry the same work."""
    from routers.import_ import (
        dismiss_import_queue_entry,
        retry_import_queue_entry,
        skip_import_queue_entry,
    )

    def _race(call):
        barrier = threading.Barrier(2)

        def _one():
            barrier.wait(timeout=2)
            return call()

        with ThreadPoolExecutor(max_workers=2) as pool:
            return [future.result(timeout=5) for future in (pool.submit(_one), pool.submit(_one))]

    skip_id, _ = _queue(
        lease_env,
        download_id="double-skip",
        status="pending",
        child_status="pending",
    )
    skip_results = _race(lambda: skip_import_queue_entry(skip_id))
    assert sum(result["ok"] for result in skip_results) == 1

    dismiss_id, _ = _queue(
        lease_env,
        download_id="double-dismiss",
        status="pending",
        child_status="pending",
    )
    dismiss_results = _race(lambda: dismiss_import_queue_entry(dismiss_id))
    assert sum(result["ok"] for result in dismiss_results) == 1

    retry_id, retry_child = _queue(
        lease_env,
        download_id="double-retry",
        status="partial",
        child_status="failed",
    )
    with sqlite3.connect(lease_env) as db:
        db.execute(
            "INSERT INTO import_queue_files(queue_id, filename, status)"
            " VALUES(?, 'review.cbz', 'needs_review')",
            (retry_id,),
        )
    retry_results = _race(lambda: retry_import_queue_entry(retry_id))
    assert sorted(result.get("retried_files", 0) for result in retry_results) == [0, 1]
    assert sum(result["ok"] for result in retry_results) == 1
    with sqlite3.connect(lease_env) as db:
        assert db.execute(
            "SELECT status FROM import_queue_files WHERE id=?",
            (retry_child,),
        ).fetchone() == ("pending",)


def test_importing_process_responses_preserve_htmx_and_plain_contracts(
    lease_env: str,
    tmp_path: Path,
) -> None:
    """An expired importing lease is still protected and gets an actionable UI."""
    staged = tmp_path / "staged.cbz"
    staged.write_bytes(b"keep")
    queue_id, child_id = _queue(
        lease_env,
        download_id="expired-process",
        status="importing",
        child_status="needs_review",
        owner="expired-owner",
        expired=True,
        src_path=str(staged),
    )
    _grabbed_domain(
        lease_env,
        download_id="expired-process",
        volume_num=20,
    )
    payload = {
        f"kind_{child_id}": "volume",
        f"vol_{child_id}": "99",
    }

    htmx_headers = dict(_csrf("process-htmx")["headers"])
    htmx_headers["HX-Request"] = "true"
    htmx = _client().post(
        f"/import/{queue_id}/process",
        data=payload,
        headers=htmx_headers,
        cookies=_csrf("process-htmx")["cookies"],
        follow_redirects=False,
    )
    assert htmx.status_code == 200
    assert htmx.headers["HX-Refresh"] == "true"
    trigger = json.loads(htmx.headers["HX-Trigger"])
    assert "in progress" in trigger["showToast"]["msg"].lower()

    plain = _client().post(
        f"/import/{queue_id}/process",
        data=payload,
        **_csrf("process-plain"),
        follow_redirects=False,
    )
    assert plain.status_code == 303
    assert plain.headers["location"].startswith("/queue?")
    assert "in+progress" in plain.headers["location"]

    with sqlite3.connect(lease_env) as db:
        assert db.execute(
            "SELECT status, lease_owner FROM import_queue WHERE id=?",
            (queue_id,),
        ).fetchone() == ("importing", "expired-owner")
        assert db.execute(
            "SELECT status, proposed_volume FROM import_queue_files WHERE id=?",
            (child_id,),
        ).fetchone() == ("needs_review", 1.0)
        assert db.execute(
            "SELECT status FROM volumes WHERE download_id='expired-process'"
        ).fetchone() == ("grabbed",)
        assert db.execute(
            "SELECT COUNT(*) FROM seen WHERE download_id='expired-process'"
        ).fetchone()[0] == 1
    assert staged.read_bytes() == b"keep"


@pytest.mark.parametrize("action", ("skip", "dismiss", "retry"))
def test_importing_manual_action_responses_preserve_htmx_and_plain_contracts(
    lease_env: str,
    action: str,
) -> None:
    """All import-review buttons surface the same protected-owner outcome."""
    queue_id, child_id = _queue(
        lease_env,
        download_id=f"active-{action}",
        status="importing",
        child_status="pending",
        owner=f"{action}-owner",
    )

    csrf = _csrf(f"{action}-htmx")
    htmx_headers = dict(csrf["headers"])
    htmx_headers["HX-Request"] = "true"
    htmx = _client().post(
        f"/import/{queue_id}/{action}",
        headers=htmx_headers,
        cookies=csrf["cookies"],
        follow_redirects=False,
    )
    assert htmx.status_code == 200
    assert htmx.headers["HX-Refresh"] == "true"
    assert "in progress" in json.loads(htmx.headers["HX-Trigger"])[
        "showToast"
    ]["msg"].lower()

    plain = _client().post(
        f"/import/{queue_id}/{action}",
        **_csrf(f"{action}-plain"),
        follow_redirects=False,
    )
    assert plain.status_code == 303
    assert plain.headers["location"].startswith("/import?")
    assert "in+progress" in plain.headers["location"]

    with sqlite3.connect(lease_env) as db:
        assert db.execute(
            "SELECT status, lease_owner FROM import_queue WHERE id=?",
            (queue_id,),
        ).fetchone() == ("importing", f"{action}-owner")
        assert db.execute(
            "SELECT status FROM import_queue_files WHERE id=?",
            (child_id,),
        ).fetchone() == ("pending",)


def test_queue_untrack_blocks_active_shared_download_before_any_mutation(
    lease_env: str,
) -> None:
    """One importing sibling protects all rows and domain state for the hash."""
    active_id, active_child = _queue(
        lease_env,
        download_id="shared-download",
        status="importing",
        child_status="pending",
        download_client_id=101,
        owner="live-owner",
    )
    pending_id, pending_child = _queue(
        lease_env,
        download_id="SHARED-DOWNLOAD",
        status="pending",
        child_status="needs_review",
        download_client_id=101,
    )
    volume_id, chapter_id, source_url = _grabbed_domain(
        lease_env,
        download_id="shared-download",
        volume_num=30,
        download_client_id=101,
    )

    response = _client().post(
        "/queue/torrent/shared-download/remove",
        data={
            "remove_from_client": "0",
            "delete_files": "0",
            "blocklist": "1",
        },
        **_csrf("queue-active"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "in+progress" in response.headers["location"]

    with sqlite3.connect(lease_env) as db:
        assert db.execute(
            "SELECT id, status, lease_owner FROM import_queue"
            " WHERE id IN (?, ?) ORDER BY id",
            (active_id, pending_id),
        ).fetchall() == [
            (active_id, "importing", "live-owner"),
            (pending_id, "pending", None),
        ]
        assert db.execute(
            "SELECT id, status FROM import_queue_files"
            " WHERE id IN (?, ?) ORDER BY id",
            (active_child, pending_child),
        ).fetchall() == [
            (active_child, "pending"),
            (pending_child, "needs_review"),
        ]
        assert db.execute(
            "SELECT status, download_id FROM volumes WHERE id=?",
            (volume_id,),
        ).fetchone() == ("grabbed", "shared-download")
        assert db.execute(
            "SELECT status, download_id FROM chapters WHERE id=?",
            (chapter_id,),
        ).fetchone() == ("grabbed", "shared-download")
        assert db.execute(
            "SELECT COUNT(*) FROM seen WHERE torrent_url=?",
            (source_url,),
        ).fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM blocklist").fetchone()[0] == 0


def test_queue_untrack_scopes_children_to_transitioned_nonactive_parents(
    lease_env: str,
) -> None:
    """A same-hash terminal sibling does not have its child decisions rewritten."""
    pending_id, pending_child = _queue(
        lease_env,
        download_id="nonactive-shared",
        status="pending",
        child_status="pending",
        download_client_id=101,
    )
    failed_id, failed_child = _queue(
        lease_env,
        download_id="NONACTIVE-SHARED",
        status="failed",
        child_status="failed",
        download_client_id=101,
    )
    _grabbed_domain(
        lease_env,
        download_id="nonactive-shared",
        volume_num=31,
        download_client_id=101,
    )

    response = _client().post(
        "/queue/torrent/nonactive-shared/remove",
        data={"remove_from_client": "0"},
        **_csrf("queue-nonactive"),
        follow_redirects=False,
    )
    assert response.status_code == 303

    with sqlite3.connect(lease_env) as db:
        assert db.execute(
            "SELECT status FROM import_queue WHERE id=?",
            (pending_id,),
        ).fetchone() == ("skipped",)
        assert db.execute(
            "SELECT status FROM import_queue_files WHERE id=?",
            (pending_child,),
        ).fetchone() == ("skipped",)
        assert db.execute(
            "SELECT status FROM import_queue WHERE id=?",
            (failed_id,),
        ).fetchone() == ("failed",)
        assert db.execute(
            "SELECT status FROM import_queue_files WHERE id=?",
            (failed_child,),
        ).fetchone() == ("failed",)


@pytest.mark.parametrize(
    ("action", "expects_blocklist", "expects_client_remove"),
    (
        ("reset_all", False, False),
        ("reset_download", False, False),
        ("reset_volume", False, False),
        ("remove", True, True),
        ("block_remove", True, True),
    ),
)
def test_mixed_case_sab_identifier_is_coherent_for_manual_queue_actions(
    lease_env: str,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    expects_blocklist: bool,
    expects_client_remove: bool,
) -> None:
    """SAB actions preserve exact case and use the persisted owning client."""
    import main

    sab_id = "SABnzbd_nzo_kyt1f0"
    queue_id, child_id = _queue(
        lease_env,
        download_id=sab_id,
        status="pending",
        child_status="pending",
        download_client_id=201,
    )
    volume_id, _, source_url = _grabbed_domain(
        lease_env,
        download_id=sab_id,
        volume_num=40,
        client="sabnzbd",
        protocol="nzb",
        download_client_id=201,
    )

    client_calls: list[tuple[str, str, str]] = []

    async def fake_sab_remove(
        download_id: str,
        *,
        client: dict[str, object] | None = None,
    ) -> bool:
        assert client is not None
        client_calls.append(("sab", download_id, str(client["host"])))
        return True

    async def fake_qbit_remove(
        download_id: str,
        delete_files: bool = False,
        *,
        client: dict[str, object] | None = None,
    ) -> bool:
        del delete_files, client
        client_calls.append(("qbit", download_id, ""))
        return True

    def discard_background_task(coro: object, *, name: str) -> None:
        del name
        if inspect.iscoroutine(coro):
            coro.close()

    monkeypatch.setattr(main, "sab_remove", fake_sab_remove)
    monkeypatch.setattr(main, "qbit_remove", fake_qbit_remove)
    monkeypatch.setattr(main, "create_background_task", discard_background_task)

    data: dict[str, str] = {}
    if action == "reset_all":
        path = f"/queue/grabbed/{sab_id}/reset-all"
    elif action == "reset_download":
        path = f"/queue/download/client/201/{sab_id}/reset"
    elif action == "reset_volume":
        path = f"/queue/grabbed/{volume_id}/reset"
    elif action == "remove":
        path = f"/queue/download/client/201/{sab_id}/remove"
        data = {
            "remove_from_client": "1",
            "delete_files": "1",
            "blocklist": "1",
        }
    else:
        path = f"/queue/download/client/201/{sab_id}/block-remove"
        data = {"delete_files": "1"}

    response = _client().post(
        path,
        data=data,
        **_csrf(f"sab-{action}"),
        follow_redirects=False,
    )
    assert response.status_code == 303

    with sqlite3.connect(lease_env) as db:
        assert db.execute(
            "SELECT status FROM import_queue WHERE id=?",
            (queue_id,),
        ).fetchone() == ("skipped",)
        assert db.execute(
            "SELECT status FROM import_queue_files WHERE id=?",
            (child_id,),
        ).fetchone() == ("skipped",)
        assert db.execute(
            "SELECT status, download_id, source_url FROM volumes WHERE id=?",
            (volume_id,),
        ).fetchone() == ("wanted", None, None)
        assert db.execute(
            "SELECT COUNT(*) FROM seen WHERE lower(download_id)=lower(?)",
            (sab_id,),
        ).fetchone()[0] == 0
        blocklist_rows = db.execute(
            "SELECT torrent_url, torrent_name, protocol FROM blocklist"
        ).fetchall()

    if expects_blocklist:
        assert blocklist_rows == [(source_url, sab_id, "nzb")]
    else:
        assert blocklist_rows == []
    expected_calls = (
        [("sab", sab_id, "http://sab-primary.invalid")]
        if expects_client_remove
        else []
    )
    assert client_calls == expected_calls


def test_case_distinct_sab_ids_fail_legacy_probe_then_mutate_exact_only(
    lease_env: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SAB case variants remain exact and independently owner-qualified."""
    import main

    sab_ids = ("SABnzbd_nzo_AbC", "SABnzbd_nzo_aBc")
    queue_rows = []
    for offset, sab_id in enumerate(sab_ids):
        download_client_id = 201 + offset
        queue_id, child_id = _queue(
            lease_env,
            download_id=sab_id,
            status="pending",
            child_status="pending",
            download_client_id=download_client_id,
        )
        volume_id, _, source_url = _grabbed_domain(
            lease_env,
            download_id=sab_id,
            volume_num=50 + offset,
            client="sabnzbd",
            protocol="nzb",
            download_client_id=download_client_id,
        )
        queue_rows.append((queue_id, child_id, volume_id, source_url))

    client_calls: list[tuple[str, str]] = []

    async def fake_sab_remove(
        download_id: str,
        *,
        client: dict[str, object] | None = None,
    ) -> bool:
        assert client is not None
        client_calls.append((download_id, str(client["host"])))
        return True

    async def fail_qbit_remove(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        raise AssertionError("qBit must not receive a SAB identity")

    monkeypatch.setattr(main, "sab_remove", fake_sab_remove)
    monkeypatch.setattr(main, "qbit_remove", fail_qbit_remove)

    case_miss = _client().post(
        "/queue/torrent/sabnzbd_nzo_abc/remove",
        data={"remove_from_client": "1"},
        **_csrf("sab-case-ambiguous"),
        follow_redirects=False,
    )
    assert case_miss.status_code == 303
    assert "no+longer+tracked" in case_miss.headers["location"]
    assert client_calls == []

    exact = _client().post(
        f"/queue/download/client/201/{sab_ids[0]}/remove",
        data={"remove_from_client": "1", "blocklist": "1"},
        **_csrf("sab-case-exact"),
        follow_redirects=False,
    )
    assert exact.status_code == 303
    assert client_calls == [(sab_ids[0], "http://sab-primary.invalid")]

    with sqlite3.connect(lease_env) as db:
        first_queue, first_child, first_volume, first_source = queue_rows[0]
        assert db.execute(
            "SELECT status FROM import_queue WHERE id=?",
            (first_queue,),
        ).fetchone() == ("skipped",)
        assert db.execute(
            "SELECT status FROM import_queue_files WHERE id=?",
            (first_child,),
        ).fetchone() == ("skipped",)
        assert db.execute(
            "SELECT status, download_id FROM volumes WHERE id=?",
            (first_volume,),
        ).fetchone() == ("wanted", None)
        assert db.execute(
            "SELECT COUNT(*) FROM seen WHERE torrent_url=?",
            (first_source,),
        ).fetchone()[0] == 0

        second_queue, second_child, second_volume, second_source = queue_rows[1]
        assert db.execute(
            "SELECT status FROM import_queue WHERE id=?",
            (second_queue,),
        ).fetchone() == ("pending",)
        assert db.execute(
            "SELECT status FROM import_queue_files WHERE id=?",
            (second_child,),
        ).fetchone() == ("pending",)
        assert db.execute(
            "SELECT status, download_id FROM volumes WHERE id=?",
            (second_volume,),
        ).fetchone() == ("grabbed", sab_ids[1])
        assert db.execute(
            "SELECT COUNT(*) FROM seen WHERE torrent_url=?",
            (second_source,),
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT torrent_name FROM blocklist"
        ).fetchall() == [(sab_ids[0],)]


def test_qbit_exact_identity_wins_over_casefolded_sab_collision(
    lease_env: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A qBit-shaped SAB ID cannot redirect an exact qBit action to SAB."""
    import main

    qbit_id = "abcdef0123456789abcdef0123456789abcdef01"
    sab_id = "Abcdef0123456789abcdef0123456789abcdef01"
    qbit_queue, qbit_child = _queue(
        lease_env,
        download_id=qbit_id,
        status="pending",
        child_status="pending",
        download_client_id=101,
    )
    sab_queue, sab_child = _queue(
        lease_env,
        download_id=sab_id,
        status="pending",
        child_status="pending",
        download_client_id=201,
    )
    qbit_volume, _, qbit_source = _grabbed_domain(
        lease_env,
        download_id=qbit_id,
        volume_num=60,
        download_client_id=101,
    )
    sab_volume, _, sab_source = _grabbed_domain(
        lease_env,
        download_id=sab_id,
        volume_num=61,
        client="sabnzbd",
        protocol="nzb",
        download_client_id=201,
    )

    client_calls: list[tuple[str, str, str]] = []

    async def fake_qbit_remove(
        download_id: str,
        delete_files: bool = False,
        *,
        client: dict[str, object] | None = None,
    ) -> bool:
        del delete_files
        assert client is not None
        client_calls.append(("qbit", download_id, str(client["host"])))
        return True

    async def fake_sab_remove(
        download_id: str,
        *,
        client: dict[str, object] | None = None,
    ) -> bool:
        assert client is not None
        client_calls.append(("sab", download_id, str(client["host"])))
        return True

    monkeypatch.setattr(main, "qbit_remove", fake_qbit_remove)
    monkeypatch.setattr(main, "sab_remove", fake_sab_remove)

    response = _client().post(
        f"/queue/torrent/{qbit_id}/remove",
        data={"remove_from_client": "1"},
        **_csrf("cross-client-qbit"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client_calls == [
        ("qbit", qbit_id, "http://qbit-primary.invalid")
    ]

    with sqlite3.connect(lease_env) as db:
        assert db.execute(
            "SELECT status FROM import_queue WHERE id=?",
            (qbit_queue,),
        ).fetchone() == ("skipped",)
        assert db.execute(
            "SELECT status FROM import_queue_files WHERE id=?",
            (qbit_child,),
        ).fetchone() == ("skipped",)
        assert db.execute(
            "SELECT status, download_id FROM volumes WHERE id=?",
            (qbit_volume,),
        ).fetchone() == ("wanted", None)
        assert db.execute(
            "SELECT COUNT(*) FROM seen WHERE torrent_url=?",
            (qbit_source,),
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT status FROM import_queue WHERE id=?",
            (sab_queue,),
        ).fetchone() == ("pending",)
        assert db.execute(
            "SELECT status FROM import_queue_files WHERE id=?",
            (sab_child,),
        ).fetchone() == ("pending",)
        assert db.execute(
            "SELECT status, download_id FROM volumes WHERE id=?",
            (sab_volume,),
        ).fetchone() == ("grabbed", sab_id)
        assert db.execute(
            "SELECT COUNT(*) FROM seen WHERE torrent_url=?",
            (sab_source,),
        ).fetchone()[0] == 1


@pytest.mark.parametrize("action", ("reset", "remove", "block_remove"))
def test_qbit_rendered_key_collision_with_exact_sab_is_ambiguous(
    lease_env: str,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    """A rendered qBit key cannot select an exact same-text SAB identity."""
    import main

    qbit_id = "ABCDEF0123456789ABCDEF0123456789ABCDEF01"
    route_key = qbit_id.lower()
    identities = (
        (qbit_id, "qbittorrent", "torrent", 101, 66),
        (route_key, "sabnzbd", "nzb", 201, 67),
    )
    queue_rows: list[tuple[int, int]] = []
    domain_rows: list[tuple[int, int, str, str]] = []
    for download_id, client, protocol, download_client_id, volume_num in identities:
        queue_rows.append(
            _queue(
                lease_env,
                download_id=download_id,
                status="pending",
                child_status="pending",
                download_client_id=download_client_id,
            )
        )
        volume_id, chapter_id, source_url = _grabbed_domain(
            lease_env,
            download_id=download_id,
            volume_num=volume_num,
            client=client,
            protocol=protocol,
            download_client_id=download_client_id,
        )
        domain_rows.append((volume_id, chapter_id, source_url, download_id))

    client_calls: list[tuple[object, ...]] = []

    async def fake_qbit_remove(
        download_id: str,
        delete_files: bool = False,
    ) -> bool:
        client_calls.append(("qbit", download_id, delete_files))
        return True

    async def fake_sab_remove(download_id: str) -> bool:
        client_calls.append(("sab", download_id))
        return True

    monkeypatch.setattr(main, "qbit_remove", fake_qbit_remove)
    monkeypatch.setattr(main, "sab_remove", fake_sab_remove)

    if action == "reset":
        path = f"/queue/download/{route_key}/reset"
        data = {}
    elif action == "remove":
        path = f"/queue/torrent/{route_key}/remove"
        data = {
            "remove_from_client": "1",
            "delete_files": "1",
            "blocklist": "1",
        }
    else:
        path = f"/queue/torrent/{route_key}/block-remove"
        data = {"delete_files": "1"}

    response = _client().post(
        path,
        data=data,
        **_csrf(f"canonical-collision-{action}"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "ambiguous" in response.headers["location"]
    assert client_calls == []

    with sqlite3.connect(lease_env) as db:
        for queue_id, child_id in queue_rows:
            assert db.execute(
                "SELECT status FROM import_queue WHERE id=?",
                (queue_id,),
            ).fetchone() == ("pending",)
            assert db.execute(
                "SELECT status FROM import_queue_files WHERE id=?",
                (child_id,),
            ).fetchone() == ("pending",)
        for volume_id, chapter_id, source_url, download_id in domain_rows:
            assert db.execute(
                "SELECT status, download_id FROM volumes WHERE id=?",
                (volume_id,),
            ).fetchone() == ("grabbed", download_id)
            assert db.execute(
                "SELECT status, download_id FROM chapters WHERE id=?",
                (chapter_id,),
            ).fetchone() == ("grabbed", download_id)
            assert db.execute(
                "SELECT download_id FROM seen WHERE torrent_url=?",
                (source_url,),
            ).fetchone() == (download_id,)
        assert db.execute("SELECT COUNT(*) FROM blocklist").fetchone()[0] == 0


def test_legacy_uppercase_qbit_uses_exact_db_id_and_lowercase_external_id(
    lease_env: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy uppercase storage is exact in SQLite and canonical at qBit."""
    import main
    import routers.queue_ as queue_router
    import status_cache

    persisted_id = "ABCDEF0123456789ABCDEF0123456789ABCDEF01"
    external_id = persisted_id.lower()
    queue_id, child_id = _queue(
        lease_env,
        download_id=persisted_id,
        status="partial",
        child_status="needs_review",
        download_client_id=101,
    )
    volume_id, _, source_url = _grabbed_domain(
        lease_env,
        download_id=persisted_id,
        volume_num=64,
        download_client_id=101,
    )
    untouched_queue, untouched_child = _queue(
        lease_env,
        download_id="unrelated-qbit",
        status="pending",
        child_status="pending",
        download_client_id=101,
    )
    untouched_volume, _, untouched_source = _grabbed_domain(
        lease_env,
        download_id="unrelated-qbit",
        volume_num=65,
        download_client_id=101,
    )
    monkeypatch.setattr(
        queue_router._sc,
        "DOWNLOAD_STATUS_CACHE",
        status_cache.DownloadStatusCache(),
    )
    queue_rows, _, _, _ = asyncio.run(queue_router._build_queue_rows())
    assert any(
        row["queue_id"] == queue_id
        and row["client"] == "qbittorrent"
        and row["hash"] == external_id
        for row in queue_rows
    )

    client_calls: list[tuple[str, str, str]] = []

    async def fake_qbit_remove(
        download_id: str,
        delete_files: bool = False,
        *,
        client: dict[str, object] | None = None,
    ) -> bool:
        del delete_files
        assert client is not None
        client_calls.append(("qbit", download_id, str(client["host"])))
        return True

    async def fail_sab_remove(
        download_id: str,
        *,
        client: dict[str, object] | None = None,
    ) -> bool:
        del client
        raise AssertionError(f"SAB received qBit identity {download_id}")

    monkeypatch.setattr(main, "qbit_remove", fake_qbit_remove)
    monkeypatch.setattr(main, "sab_remove", fail_sab_remove)

    response = _client().post(
        f"/queue/torrent/{external_id}/remove",
        data={"remove_from_client": "1"},
        **_csrf("uppercase-qbit"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client_calls == [
        ("qbit", external_id, "http://qbit-primary.invalid")
    ]

    with sqlite3.connect(lease_env) as db:
        assert db.execute(
            "SELECT status FROM import_queue WHERE id=?",
            (queue_id,),
        ).fetchone() == ("skipped",)
        assert db.execute(
            "SELECT status FROM import_queue_files WHERE id=?",
            (child_id,),
        ).fetchone() == ("skipped",)
        assert db.execute(
            "SELECT status, download_id FROM volumes WHERE id=?",
            (volume_id,),
        ).fetchone() == ("wanted", None)
        assert db.execute(
            "SELECT COUNT(*) FROM seen WHERE torrent_url=?",
            (source_url,),
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT status FROM import_queue WHERE id=?",
            (untouched_queue,),
        ).fetchone() == ("pending",)
        assert db.execute(
            "SELECT status FROM import_queue_files WHERE id=?",
            (untouched_child,),
        ).fetchone() == ("pending",)
        assert db.execute(
            "SELECT status, download_id FROM volumes WHERE id=?",
            (untouched_volume,),
        ).fetchone() == ("grabbed", "unrelated-qbit")
        assert db.execute(
            "SELECT COUNT(*) FROM seen WHERE torrent_url=?",
            (untouched_source,),
        ).fetchone()[0] == 1


def test_same_text_cross_client_identity_fails_without_mutation(
    lease_env: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exact ID backed by both clients is ambiguous and cannot be reserved."""
    import main

    collision_id = "0123456789abcdef0123456789abcdef01234567"
    queues = [
        _queue(
            lease_env,
            download_id=collision_id,
            status="pending",
            child_status="pending",
            download_client_id=download_client_id,
        )
        for download_client_id in (101, 201)
    ]
    domains = (
        _grabbed_domain(
            lease_env,
            download_id=collision_id,
            volume_num=62,
            download_client_id=101,
        ),
        _grabbed_domain(
            lease_env,
            download_id=collision_id,
            volume_num=63,
            client="sabnzbd",
            protocol="nzb",
            download_client_id=201,
        ),
    )

    async def fail_remove(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        raise AssertionError("ambiguous identity must not reach a client")

    monkeypatch.setattr(main, "qbit_remove", fail_remove)
    monkeypatch.setattr(main, "sab_remove", fail_remove)

    response = _client().post(
        f"/queue/torrent/{collision_id}/remove",
        data={"remove_from_client": "1", "blocklist": "1"},
        **_csrf("cross-client-exact"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "ambiguous" in response.headers["location"]

    with sqlite3.connect(lease_env) as db:
        assert [
            db.execute(
                "SELECT status FROM import_queue WHERE id=?",
                (queue_id,),
            ).fetchone()
            for queue_id, _ in queues
        ] == [("pending",), ("pending",)]
        assert [
            db.execute(
                "SELECT status FROM import_queue_files WHERE id=?",
                (child_id,),
            ).fetchone()
            for _, child_id in queues
        ] == [("pending",), ("pending",)]
        assert [
            db.execute(
                "SELECT status, download_id FROM volumes WHERE id=?",
                (volume_id,),
            ).fetchone()
            for volume_id, _, _ in domains
        ] == [
            ("grabbed", collision_id),
            ("grabbed", collision_id),
        ]
        assert db.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM blocklist").fetchone()[0] == 0


@pytest.mark.parametrize(
    (
        "client_ids",
        "client_name",
        "protocol",
        "persisted_ids",
        "route_id",
        "expected_external_id",
        "expected_host",
    ),
    (
        (
            (101, 102),
            "qbittorrent",
            "torrent",
            (
                "ABCDEF0123456789ABCDEF0123456789ABCDEF01",
                "abcdef0123456789abcdef0123456789abcdef01",
            ),
            "abcdef0123456789abcdef0123456789abcdef01",
            "abcdef0123456789abcdef0123456789abcdef01",
            "http://qbit-primary.invalid",
        ),
        (
            (201, 202),
            "sabnzbd",
            "nzb",
            ("NZO-SHARED", "NZO-SHARED"),
            "NZO-SHARED",
            "NZO-SHARED",
            "http://sab-primary.invalid",
        ),
    ),
)
def test_two_client_collision_fails_closed_then_owner_api_mutates_exactly(
    lease_env: str,
    monkeypatch: pytest.MonkeyPatch,
    client_ids: tuple[int, int],
    client_name: str,
    protocol: str,
    persisted_ids: tuple[str, str],
    route_id: str,
    expected_external_id: str,
    expected_host: str,
) -> None:
    """Legacy plain/HTMX calls fail closed; the qualified API selects one owner."""
    import main

    queues: list[tuple[int, int]] = []
    domains: list[tuple[int, int, str]] = []
    for offset, (download_client_id, persisted_id) in enumerate(
        zip(client_ids, persisted_ids, strict=True),
    ):
        queues.append(
            _queue(
                lease_env,
                download_id=persisted_id,
                status="pending",
                child_status="pending",
                download_client_id=download_client_id,
            )
        )
        domains.append(
            _grabbed_domain(
                lease_env,
                download_id=persisted_id,
                volume_num=90 + offset,
                client=client_name,
                protocol=protocol,
                download_client_id=download_client_id,
                source_key=str(download_client_id),
            )
        )

    plain = _client().post(
        f"/queue/torrent/{route_id}/remove",
        data={"remove_from_client": "0"},
        **_csrf(f"{protocol}-collision-plain"),
        follow_redirects=False,
    )
    assert plain.status_code == 303
    assert "ambiguous" in plain.headers["location"]

    htmx_csrf = _csrf(f"{protocol}-collision-htmx")
    htmx_headers = dict(htmx_csrf["headers"])
    htmx_headers["HX-Request"] = "true"
    htmx = _client().post(
        f"/queue/torrent/{route_id}/remove",
        data={"remove_from_client": "0"},
        headers=htmx_headers,
        cookies=htmx_csrf["cookies"],
        follow_redirects=False,
    )
    assert htmx.status_code == 200
    assert "ambiguous" in json.loads(htmx.headers["HX-Trigger"])[
        "showToast"
    ]["msg"].lower()

    client_calls: list[tuple[str, str, str]] = []

    async def fake_qbit_remove(
        download_id: str,
        delete_files: bool = False,
        *,
        client: dict[str, object] | None = None,
    ) -> bool:
        del delete_files
        assert client is not None
        client_calls.append(("qbit", download_id, str(client["host"])))
        return True

    async def fake_sab_remove(
        download_id: str,
        *,
        client: dict[str, object] | None = None,
    ) -> bool:
        assert client is not None
        client_calls.append(("sab", download_id, str(client["host"])))
        return True

    monkeypatch.setattr(main, "qbit_remove", fake_qbit_remove)
    monkeypatch.setattr(main, "sab_remove", fake_sab_remove)
    qualified = _client().post(
        "/api/queue/download-clients/"
        f"{client_ids[0]}/downloads/{route_id}/remove",
        data={"remove_from_client": "1"},
        **_csrf(f"{protocol}-collision-qualified"),
        follow_redirects=False,
    )
    assert qualified.status_code == 303
    expected_kind = "qbit" if protocol == "torrent" else "sab"
    assert client_calls == [
        (expected_kind, expected_external_id, expected_host)
    ]

    with sqlite3.connect(lease_env) as db:
        first_queue, first_child = queues[0]
        first_volume, _, first_source = domains[0]
        assert db.execute(
            "SELECT status FROM import_queue WHERE id=?",
            (first_queue,),
        ).fetchone() == ("skipped",)
        assert db.execute(
            "SELECT status FROM import_queue_files WHERE id=?",
            (first_child,),
        ).fetchone() == ("skipped",)
        assert db.execute(
            "SELECT status,download_id,download_client_id FROM volumes WHERE id=?",
            (first_volume,),
        ).fetchone() == ("wanted", None, None)
        assert db.execute(
            "SELECT COUNT(*) FROM seen WHERE torrent_url=?",
            (first_source,),
        ).fetchone() == (0,)

        second_queue, second_child = queues[1]
        second_volume, _, second_source = domains[1]
        assert db.execute(
            "SELECT status FROM import_queue WHERE id=?",
            (second_queue,),
        ).fetchone() == ("pending",)
        assert db.execute(
            "SELECT status FROM import_queue_files WHERE id=?",
            (second_child,),
        ).fetchone() == ("pending",)
        assert db.execute(
            "SELECT status,download_id,download_client_id FROM volumes WHERE id=?",
            (second_volume,),
        ).fetchone() == ("grabbed", persisted_ids[1], client_ids[1])
        assert db.execute(
            "SELECT COUNT(*) FROM seen WHERE torrent_url=?",
            (second_source,),
        ).fetchone() == (1,)


def test_blocked_htmx_render_releases_writer_transaction(
    lease_env: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked response may render slowly without retaining SQLite's writer lock."""
    import routers.queue_ as queue_router

    download_id = "blocked-render"
    queue_id, child_id = _queue(
        lease_env,
        download_id=download_id,
        status="importing",
        child_status="pending",
        download_client_id=101,
        owner="render-owner",
    )
    render_started = threading.Event()
    release_render = threading.Event()

    async def paused_queue_rows():
        render_started.set()
        assert await asyncio.to_thread(release_render.wait, 5)
        return [], [], "", []

    monkeypatch.setattr(queue_router, "_build_queue_rows", paused_queue_rows)
    csrf = _csrf("blocked-render-htmx")
    headers = dict(csrf["headers"])
    headers["HX-Request"] = "true"

    def post_htmx():
        return _client().post(
            f"/queue/torrent/{download_id}/remove",
            data={"remove_from_client": "0"},
            headers=headers,
            cookies=csrf["cookies"],
            follow_redirects=False,
        )

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            response_future = pool.submit(post_htmx)
            assert render_started.wait(timeout=3)
            with sqlite3.connect(lease_env, timeout=0.2) as writer:
                writer.execute("PRAGMA busy_timeout=200")
                writer.execute("UPDATE series SET title=title WHERE id=1")
                writer.commit()
            release_render.set()
            htmx = response_future.result(timeout=5)
    finally:
        release_render.set()

    assert htmx.status_code == 200
    assert "in progress" in json.loads(htmx.headers["HX-Trigger"])[
        "showToast"
    ]["msg"].lower()

    plain = _client().post(
        f"/queue/torrent/{download_id}/remove",
        data={"remove_from_client": "0"},
        **_csrf("blocked-render-plain"),
        follow_redirects=False,
    )
    assert plain.status_code == 303
    assert "in+progress" in plain.headers["location"]
    with sqlite3.connect(lease_env) as db:
        assert db.execute(
            "SELECT status, lease_owner FROM import_queue WHERE id=?",
            (queue_id,),
        ).fetchone() == ("importing", "render-owner")
        assert db.execute(
            "SELECT status FROM import_queue_files WHERE id=?",
            (child_id,),
        ).fetchone() == ("pending",)


def test_queue_rendering_preserves_client_identity_and_escapes_no_rows(
    lease_env: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rendered data uses qualified keys and contains no connection-bound Rows."""
    import routers.queue_ as queue_router
    import status_cache

    monkeypatch.setattr(
        queue_router._sc,
        "DOWNLOAD_STATUS_CACHE",
        status_cache.DownloadStatusCache(),
    )
    qbit_id = "abcdef0123456789abcdef0123456789abcdef01"
    sab_ids = (
        "Abcdef0123456789abcdef0123456789abcdef01",
        "aBcdef0123456789abcdef0123456789abcdef01",
    )
    for index, (download_id, client, protocol, download_client_id) in enumerate(
        (
            (qbit_id, "qbittorrent", "torrent", 101),
            (sab_ids[0], "sabnzbd", "nzb", 201),
            (sab_ids[1], "sabnzbd", "nzb", 202),
        ),
        start=1,
    ):
        _queue(
            lease_env,
            download_id=download_id,
            status="partial",
            child_status="needs_review",
            download_client_id=download_client_id,
        )
        _grabbed_domain(
            lease_env,
            download_id=download_id,
            volume_num=70 + index,
            client=client,
            protocol=protocol,
            download_client_id=download_client_id,
        )
    with sqlite3.connect(lease_env) as db:
        db.execute(
            "INSERT INTO suwayomi_downloads("
            "series_id, volume_num, suwayomi_manga_id, chapter_ids,"
            " status, progress, total"
            ") VALUES(1, 80, 10, '[]', 'queued', 1, 2)"
        )

    rendered = asyncio.run(queue_router._build_queue_rows())

    def assert_plain(value: object) -> None:
        assert not isinstance(value, sqlite3.Row)
        if isinstance(value, dict):
            for key, item in value.items():
                assert_plain(key)
                assert_plain(item)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                assert_plain(item)

    assert_plain(rendered)
    queue_rows, disk_info, _, suwayomi_rows = rendered
    assert {
        (row["client"], row["hash"])
        for row in queue_rows
        if row["stage"] == "review"
    } == {
        ("qbittorrent", qbit_id),
        ("sabnzbd", sab_ids[0]),
        ("sabnzbd", sab_ids[1]),
    }
    assert all(isinstance(row, dict) for row in queue_rows)
    assert all(isinstance(row, dict) for row in disk_info)
    assert all(
        isinstance(file_row, dict)
        for row in queue_rows
        for file_row in row["files"]
    )
    assert suwayomi_rows and all(isinstance(row, dict) for row in suwayomi_rows)


def test_force_grab_converts_pending_row_before_database_exit(
    lease_env: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The awaited grab receives plain data, not a sqlite.Row from a closed DB."""
    import main

    with sqlite3.connect(lease_env) as db:
        cursor = db.execute(
            "INSERT INTO pending_releases("
            "series_id, url, title, indexer, protocol, size_bytes"
            ") VALUES(1, 'https://release.invalid/item', 'Pending Item',"
            " 'Indexer', 'nzb', 123)"
        )
        pending_id = cursor.lastrowid
    assert pending_id is not None
    captured: list[tuple[dict[str, object], int]] = []

    async def fake_grab_item(item: dict[str, object], series_id: int) -> None:
        assert not isinstance(item, sqlite3.Row)
        captured.append((item, series_id))

    monkeypatch.setattr(main, "grab_item", fake_grab_item)
    response = _client().post(
        f"/queue/pending/{pending_id}/force-grab",
        **_csrf("force-grab-row"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert captured == [
        (
            {
                "url": "https://release.invalid/item",
                "title": "Pending Item",
                "indexer": "Indexer",
                "protocol": "nzb",
                "size_bytes": 123,
            },
            1,
        )
    ]


class _Response:
    def __init__(
        self,
        *,
        status_code: int = 200,
        text: str = "",
        data: object = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self._data = data

    def json(self):
        return self._data


class _EmptyDownloadClients:
    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args: object) -> bool:
        del args
        return False

    async def post(self, url: str, *args: object, **kwargs: object) -> _Response:
        del args, kwargs
        if "/api/v2/auth/login" in url:
            return _Response(text="Ok.")
        return _Response()

    async def get(self, url: str, *args: object, **kwargs: object) -> _Response:
        del args
        params_value = kwargs.get("params")
        params = params_value if isinstance(params_value, dict) else {}
        if "/api/v2/torrents/info" in url:
            return _Response(data=[])
        if params.get("mode") == "history":
            return _Response(data={"history": {"slots": []}})
        if params.get("mode") == "queue":
            return _Response(data={"queue": {"slots": []}})
        return _Response(data={})


def test_qbit_and_sab_orphan_passes_protect_active_rows_and_scope_children(
    lease_env: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both client passes protect review/import work and only touch won parents."""
    import httpx
    from import_discovery import _check_download_status_impl
    from routers import suwayomi_ as suwayomi_router

    with sqlite3.connect(lease_env) as db:
        db.execute(
            "UPDATE download_clients SET enabled=1 WHERE id IN (101, 201)"
        )

    protected: list[tuple[int, int, int, int, str]] = []
    for download_id, status, owner, expired, client, volume_num in (
        ("QBIT-LIVE", "importing", "q-owner", True, "qbittorrent", 40.0),
        ("qbit-partial", "partial", None, False, "qbittorrent", 41.0),
        ("qbit-pending", "pending", None, False, "qbittorrent", 41.5),
        ("sab-live", "importing", "sab-owner", True, "sabnzbd", 42.0),
        ("sab-pending", "pending", None, False, "sabnzbd", 43.0),
        ("sab-partial", "partial", None, False, "sabnzbd", 43.5),
    ):
        download_client_id = 101 if client == "qbittorrent" else 201
        queue_id, child_id = _queue(
            lease_env,
            download_id=download_id,
            status=status,
            child_status="needs_review",
            download_client_id=download_client_id,
            owner=owner,
            expired=expired,
        )
        volume_download_id = (
            download_id.lower() if download_id == "QBIT-LIVE" else download_id
        )
        volume_id, chapter_id, source_url = _grabbed_domain(
            lease_env,
            download_id=volume_download_id,
            volume_num=volume_num,
            client=client,
            protocol="torrent" if client == "qbittorrent" else "nzb",
            download_client_id=download_client_id,
        )
        protected.append(
            (queue_id, child_id, volume_id, chapter_id, source_url)
        )

    transitioned: list[tuple[int, int, int, int, int, str]] = []
    for download_id, client, volume_num in (
        ("qbit-failed", "qbittorrent", 44.0),
        ("sab-failed", "sabnzbd", 45.0),
    ):
        download_client_id = 101 if client == "qbittorrent" else 201
        failed_id, failed_child = _queue(
            lease_env,
            download_id=download_id,
            status="failed",
            child_status="failed",
            download_client_id=download_client_id,
        )
        sibling_id, sibling_child = _queue(
            lease_env,
            download_id=download_id,
            status="imported",
            child_status="needs_review",
            download_client_id=download_client_id,
        )
        volume_id, chapter_id, source_url = _grabbed_domain(
            lease_env,
            download_id=download_id,
            volume_num=volume_num,
            client=client,
            protocol="torrent" if client == "qbittorrent" else "nzb",
            download_client_id=download_client_id,
        )
        transitioned.append(
            (
                failed_id,
                failed_child,
                sibling_id,
                sibling_child,
                volume_id,
                source_url,
            )
        )

    async def _no_suwayomi() -> None:
        return None

    monkeypatch.setattr(httpx, "AsyncClient", _EmptyDownloadClients)
    monkeypatch.setattr(
        suwayomi_router,
        "check_suwayomi_jobs",
        _no_suwayomi,
    )
    asyncio.run(_check_download_status_impl())

    with sqlite3.connect(lease_env) as db:
        for queue_id, child_id, volume_id, chapter_id, source_url in protected:
            assert db.execute(
                "SELECT status FROM import_queue WHERE id=?",
                (queue_id,),
            ).fetchone()[0] in {"pending", "partial", "importing"}
            assert db.execute(
                "SELECT status FROM import_queue_files WHERE id=?",
                (child_id,),
            ).fetchone() == ("needs_review",)
            assert db.execute(
                "SELECT status FROM volumes WHERE id=?",
                (volume_id,),
            ).fetchone() == ("grabbed",)
            assert db.execute(
                "SELECT status FROM chapters WHERE id=?",
                (chapter_id,),
            ).fetchone() == ("grabbed",)
            assert db.execute(
                "SELECT COUNT(*) FROM seen WHERE torrent_url=?",
                (source_url,),
            ).fetchone()[0] == 1

        for (
            failed_id,
            failed_child,
            sibling_id,
            sibling_child,
            volume_id,
            source_url,
        ) in transitioned:
            assert db.execute(
                "SELECT status FROM import_queue WHERE id=?",
                (failed_id,),
            ).fetchone() == ("skipped",)
            assert db.execute(
                "SELECT status FROM import_queue_files WHERE id=?",
                (failed_child,),
            ).fetchone() == ("skipped",)
            assert db.execute(
                "SELECT status FROM import_queue WHERE id=?",
                (sibling_id,),
            ).fetchone() == ("imported",)
            assert db.execute(
                "SELECT status FROM import_queue_files WHERE id=?",
                (sibling_child,),
            ).fetchone() == ("needs_review",)
            assert db.execute(
                "SELECT status FROM volumes WHERE id=?",
                (volume_id,),
            ).fetchone() == ("wanted",)
            assert db.execute(
                "SELECT COUNT(*) FROM seen WHERE torrent_url=?",
                (source_url,),
            ).fetchone()[0] == 0


def test_import_download_fallback_never_overwrites_importing_owner(
    lease_env: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guarded executor is solely responsible for owner-CAS failures."""
    import import_download
    import import_execute

    queue_id, child_id = _queue(
        lease_env,
        download_id="fallback-owner",
        status="importing",
        child_status="pending",
        owner="still-owned",
    )

    async def _raise(_queue_id: int) -> bool:
        raise RuntimeError("guarded failure already handled")

    monkeypatch.setattr(import_execute, "_guarded_execute_import", _raise)
    asyncio.run(import_download._process_auto_import(queue_id))

    with sqlite3.connect(lease_env) as db:
        assert db.execute(
            "SELECT status, lease_owner FROM import_queue WHERE id=?",
            (queue_id,),
        ).fetchone() == ("importing", "still-owned")
        assert db.execute(
            "SELECT status FROM import_queue_files WHERE id=?",
            (child_id,),
        ).fetchone() == ("pending",)


def test_set_category_uses_exact_owner_and_legacy_collision_fails_closed(
    lease_env: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Standalone category changes never route through the default qBit."""
    from routers import queue_ as queue_router

    download_id = "ABCDEF0123456789ABCDEF0123456789ABCDEF01"
    for offset, owner_id in enumerate((101, 102), start=1):
        queue_id, _ = _queue(
            lease_env,
            download_id=download_id.lower(),
            status="pending",
            child_status="pending",
            download_client_id=owner_id,
        )
        _grabbed_domain(
            lease_env,
            download_id=download_id,
            volume_num=110 + offset,
            download_client_id=owner_id,
            source_key=str(owner_id),
        )
        with sqlite3.connect(lease_env) as db:
            db.execute(
                "UPDATE import_queue SET download_protocol='torrent' WHERE id=?",
                (queue_id,),
            )
    with sqlite3.connect(lease_env) as db:
        db.execute("UPDATE download_clients SET enabled=1 WHERE id=102")

    requests: list[tuple[str, dict[str, object]]] = []

    class _Qbit:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: object) -> bool:
            del args
            return False

        async def post(
            self,
            url: str,
            *args: object,
            **kwargs: object,
        ) -> _Response:
            del args
            data = kwargs.get("data")
            requests.append((url, data if isinstance(data, dict) else {}))
            return _Response(text="Ok.")

    monkeypatch.setattr(queue_router.httpx, "AsyncClient", _Qbit)

    legacy = _client().post(
        f"/queue/torrent/{download_id.lower()}/set-category",
        data={"category": "new-category"},
        **_csrf("category-legacy"),
        follow_redirects=False,
    )
    assert legacy.status_code == 303
    assert "ambiguous" in legacy.headers["location"]
    assert requests == []

    qualified = _client().post(
        f"/queue/download/client/101/{download_id}/set-category",
        data={"category": "new-category"},
        **_csrf("category-qualified"),
        follow_redirects=False,
    )
    assert qualified.status_code == 303
    assert [url for url, _ in requests] == [
        "http://qbit-primary.invalid/api/v2/auth/login",
        "http://qbit-primary.invalid/api/v2/torrents/createCategory",
        "http://qbit-primary.invalid/api/v2/torrents/setCategory",
    ]
    assert requests[-1][1] == {
        "hashes": download_id.lower(),
        "category": "new-category",
    }


def test_manual_queue_and_import_actions_reject_client_type_drift(
    lease_env: str,
) -> None:
    """Persisted NZB work cannot be reinterpreted after its owner becomes qBit."""
    queue_id, child_id = _queue(
        lease_env,
        download_id="NZO-Drift",
        status="pending",
        child_status="pending",
        download_client_id=101,
    )
    volume_id, chapter_id, source_url = _grabbed_domain(
        lease_env,
        download_id="NZO-Drift",
        volume_num=120,
        client="sabnzbd",
        protocol="nzb",
        download_client_id=101,
    )
    with sqlite3.connect(lease_env) as db:
        db.execute(
            "UPDATE import_queue SET download_protocol='nzb' WHERE id=?",
            (queue_id,),
        )

    queue_response = _client().post(
        "/queue/download/client/101/NZO-Drift/remove",
        data={"remove_from_client": "0"},
        **_csrf("drift-queue"),
        follow_redirects=False,
    )
    assert queue_response.status_code == 303
    assert "ambiguous" in queue_response.headers["location"]

    import_response = _client().post(
        f"/import/{queue_id}/dismiss",
        **_csrf("drift-import"),
        follow_redirects=False,
    )
    assert import_response.status_code == 303
    assert "persisted" in import_response.headers["location"].lower()

    with sqlite3.connect(lease_env) as db:
        assert db.execute(
            "SELECT status,download_protocol FROM import_queue WHERE id=?",
            (queue_id,),
        ).fetchone() == ("pending", "nzb")
        assert db.execute(
            "SELECT status FROM import_queue_files WHERE id=?",
            (child_id,),
        ).fetchone() == ("pending",)
        assert db.execute(
            "SELECT status,download_id,download_client_id FROM volumes WHERE id=?",
            (volume_id,),
        ).fetchone() == ("grabbed", "NZO-Drift", 101)
        assert db.execute(
            "SELECT status,download_id,download_client_id FROM chapters WHERE id=?",
            (chapter_id,),
        ).fetchone() == ("grabbed", "NZO-Drift", 101)
        assert db.execute(
            "SELECT COUNT(*) FROM seen WHERE torrent_url=?",
            (source_url,),
        ).fetchone() == (1,)


def test_mark_wanted_uses_owner_and_protocol_aware_siblings(
    lease_env: str,
) -> None:
    """qBit case variants group; SAB case variants and other owners do not."""
    qbit_target = _grabbed_domain(
        lease_env,
        download_id="ABCDEF",
        volume_num=130,
        download_client_id=101,
        source_key="target",
    )
    qbit_sibling = _grabbed_domain(
        lease_env,
        download_id="abcdef",
        volume_num=131,
        download_client_id=101,
        source_key="same-owner",
    )
    qbit_collision = _grabbed_domain(
        lease_env,
        download_id="ABCDEF",
        volume_num=132,
        download_client_id=102,
        source_key="other-owner",
    )
    sab_target = _grabbed_domain(
        lease_env,
        download_id="NZO-Case",
        volume_num=133,
        client="sabnzbd",
        protocol="nzb",
        download_client_id=201,
        source_key="sab-target",
    )
    sab_case_variant = _grabbed_domain(
        lease_env,
        download_id="nzo-case",
        volume_num=134,
        client="sabnzbd",
        protocol="nzb",
        download_client_id=201,
        source_key="sab-variant",
    )

    for tag, volume_id in (
        ("qbit-mark-wanted", qbit_target[0]),
        ("sab-mark-wanted", sab_target[0]),
    ):
        response = _client().post(
            f"/series/1/volumes/{volume_id}/mark-wanted",
            **_csrf(tag),
            follow_redirects=False,
        )
        assert response.status_code == 303

    with sqlite3.connect(lease_env) as db:
        for volume_id, chapter_id, source_url in (qbit_target, sab_target):
            assert db.execute(
                "SELECT status,download_id,download_client_id FROM volumes"
                " WHERE id=?",
                (volume_id,),
            ).fetchone() == ("wanted", None, None)
            assert db.execute(
                "SELECT status,download_id,download_client_id FROM chapters"
                " WHERE id=?",
                (chapter_id,),
            ).fetchone() == ("wanted", None, None)
            assert db.execute(
                "SELECT COUNT(*) FROM seen WHERE torrent_url=?",
                (source_url,),
            ).fetchone() == (0,)

        for volume_id, chapter_id, source_url in (
            qbit_sibling,
            qbit_collision,
            sab_case_variant,
        ):
            assert db.execute(
                "SELECT status,download_client_id FROM volumes WHERE id=?",
                (volume_id,),
            ).fetchone()[0] == "grabbed"
            assert db.execute(
                "SELECT status,download_client_id FROM chapters WHERE id=?",
                (chapter_id,),
            ).fetchone()[0] == "grabbed"
            assert db.execute(
                "SELECT COUNT(*) FROM seen WHERE torrent_url=?",
                (source_url,),
            ).fetchone() == (1,)


def test_system_maintenance_isolates_owners_and_protects_active_work(
    lease_env: str,
) -> None:
    """Cleanup/reset honor owner, qBit/SAB ID rules, leases, and publications."""
    from routers.system import _cleanup_stale_seen_rows, _reset_stuck_grabs
    from shared import get_db

    active_queue, _ = _queue(
        lease_env,
        download_id="MAINT-HASH",
        status="importing",
        child_status="pending",
        download_client_id=101,
        owner="import-owner",
    )
    lease_queue, _ = _queue(
        lease_env,
        download_id="NZO-Lease",
        status="failed",
        child_status="failed",
        download_client_id=201,
        owner="lease-owner",
    )
    publication_queue, _ = _queue(
        lease_env,
        download_id="NZO-Publication",
        status="failed",
        child_status="failed",
        download_client_id=201,
    )
    exact_qbit = _grabbed_domain(
        lease_env,
        download_id="maint-hash",
        volume_num=140,
        download_client_id=101,
        source_key="active",
    )
    owner_collision = _grabbed_domain(
        lease_env,
        download_id="MAINT-HASH",
        volume_num=141,
        download_client_id=102,
        source_key="idle-collision",
    )
    leased_sab = _grabbed_domain(
        lease_env,
        download_id="NZO-Lease",
        volume_num=142,
        client="sabnzbd",
        protocol="nzb",
        download_client_id=201,
        source_key="lease",
    )
    published_sab = _grabbed_domain(
        lease_env,
        download_id="NZO-Publication",
        volume_num=143,
        client="sabnzbd",
        protocol="nzb",
        download_client_id=201,
        source_key="publication",
    )
    sab_case_miss = _grabbed_domain(
        lease_env,
        download_id="nzo-publication",
        volume_num=144,
        client="sabnzbd",
        protocol="nzb",
        download_client_id=201,
        source_key="case-miss",
    )

    with sqlite3.connect(lease_env) as db:
        db.executemany(
            "UPDATE import_queue SET download_protocol=? WHERE id=?",
            (
                ("torrent", active_queue),
                ("nzb", lease_queue),
                ("nzb", publication_queue),
            ),
        )
        db.execute(
            """
            INSERT INTO import_publications(
                queue_id,state,owner_token,series_id,dst_dir,import_mode,
                staging_dir,queue_snapshot_json,series_tags_json,queue_status
            ) VALUES(
                ?,'publishing','publication-owner',1,'/library/Lease Series',
                'copy','/library/.publication','{}','[]','failed'
            )
            """,
            (publication_queue,),
        )
        db.execute(
            "UPDATE volumes SET grabbed_at=datetime('now','-3 days')"
            " WHERE id IN (?,?,?,?,?)",
            (
                exact_qbit[0],
                owner_collision[0],
                leased_sab[0],
                published_sab[0],
                sab_case_miss[0],
            ),
        )
        db.executemany(
            "INSERT INTO seen("
            "torrent_url,torrent_name,series_id,grabbed_at,protocol,client,"
            "download_id,download_client_id"
            ") VALUES(?,?,1,datetime('now','-120 days'),?,?,?,?)",
            (
                (
                    "https://source.invalid/orphan/owner-101",
                    "owner 101",
                    "torrent",
                    "qbittorrent",
                    "ORPHAN-ID",
                    101,
                ),
                (
                    "https://source.invalid/orphan/owner-102",
                    "owner 102",
                    "torrent",
                    "qbittorrent",
                    "ORPHAN-ID",
                    102,
                ),
                (
                    "https://source.invalid/orphan/sab-case",
                    "sab case",
                    "nzb",
                    "sabnzbd",
                    "nzo-orphan",
                    201,
                ),
            ),
        )
        orphan_active = db.execute(
            "INSERT INTO import_queue("
            "series_id,download_id,download_client_id,download_protocol,"
            "torrent_name,status,lease_owner"
            ") VALUES(1,'ORPHAN-ID',101,'torrent','owner 101',"
            "'importing','orphan-owner')"
        ).lastrowid
        assert orphan_active is not None
        db.execute(
            "INSERT INTO import_queue("
            "series_id,download_id,download_client_id,download_protocol,"
            "torrent_name,status"
            ") VALUES(1,'NZO-Orphan',201,'nzb','sab exact case','importing')"
        )

    with get_db() as db:
        assert _cleanup_stale_seen_rows(db) == 2
    with sqlite3.connect(lease_env) as db:
        assert db.execute(
            "SELECT torrent_url FROM seen WHERE torrent_url LIKE"
            " 'https://source.invalid/orphan/%' ORDER BY torrent_url"
        ).fetchall() == [
            ("https://source.invalid/orphan/owner-101",),
        ]

    with get_db() as db:
        assert _reset_stuck_grabs(db) == 2
    with sqlite3.connect(lease_env) as db:
        for volume_id in (
            exact_qbit[0],
            leased_sab[0],
            published_sab[0],
        ):
            assert db.execute(
                "SELECT status,download_client_id FROM volumes WHERE id=?",
                (volume_id,),
            ).fetchone()[0] == "grabbed"
        for volume_id in (owner_collision[0], sab_case_miss[0]):
            assert db.execute(
                "SELECT status,download_id,download_client_id FROM volumes"
                " WHERE id=?",
                (volume_id,),
            ).fetchone() == ("wanted", None, None)


def test_async_queue_db_contexts_do_not_render_or_yield() -> None:
    """Async queue routes must close SQLite contexts before slow response work."""
    from routers import queue_ as queue_router

    source = inspect.getsource(queue_router)
    tree = ast.parse(source)
    forbidden_calls = (
        "disk_usage",
        "TemplateResponse",
        "RedirectResponse",
        "JSONResponse",
        "_queue_partial_response",
        "AsyncClient",
        "qbit_remove",
        "sab_remove",
        "grab_item",
    )
    violations: list[str] = []
    for function in (
        node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)
    ):
        for context in (
            node for node in ast.walk(function) if isinstance(node, ast.With)
        ):
            context_text = " ".join(
                ast.get_source_segment(source, item.context_expr) or ""
                for item in context.items
            )
            if "get_db" not in context_text:
                continue
            for node in ast.walk(context):
                if isinstance(node, ast.Await):
                    violations.append(f"{function.name}:await:{node.lineno}")
                elif isinstance(node, ast.Return):
                    violations.append(f"{function.name}:return:{node.lineno}")
                elif isinstance(node, ast.Call):
                    call_text = ast.get_source_segment(source, node.func) or ""
                    if any(name in call_text for name in forbidden_calls):
                        violations.append(
                            f"{function.name}:{call_text}:{node.lineno}"
                        )
    assert violations == []


def test_owned_modules_do_not_call_get_on_sqlite_rows() -> None:
    """Guard against the sqlite3.Row.get regression in cleanup paths."""
    import import_discovery
    import import_download
    from routers import import_ as import_router
    from routers import queue_ as queue_router

    row_names = r"(?:row|q|file_row|gs|seen_row|result_row|reserved|parent)"
    pattern = re.compile(rf"\b{row_names}\.get\(")
    for module in (
        import_router,
        queue_router,
        import_discovery,
        import_download,
    ):
        assert pattern.search(inspect.getsource(module)) is None
