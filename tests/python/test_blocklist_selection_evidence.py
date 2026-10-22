"""Retained selections through real HTTP/authentication/SQLite boundaries."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
from types import MappingProxyType
import zipfile
from typing import Any, Literal

import pytest

import auth
import release_selections as selections
import shared
from test_blocklist_source_flow import Flow, flow  # noqa: F401
from test_browser_auth import _setup

SEARCH = "/api/series/1/volumes/1/search"
GRAB = "/api/series/1/volumes/1/grab-release"


@pytest.fixture
def selection(
    flow: Flow, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> Iterator[Flow]:
    monkeypatch.setattr(auth, "_CONFIG_DIR", str(tmp_path))
    auth.set_test_auth_bypass(False)
    auth.LOGIN_THROTTLE.reset()
    _setup(flow.client)
    flow.create()
    try:
        yield flow
    finally:
        auth.LOGIN_THROTTLE.reset()
        auth.set_test_auth_bypass(True)


def browser_headers(state: Flow) -> dict[str, str]:
    return {"X-CSRFToken": str(state.client.cookies.get("csrftoken"))}


def search(state: Flow, *, api: bool = False) -> dict[str, Any]:
    response = state.client.get(SEARCH, headers=state.headers if api else {})
    assert response.status_code == 200, response.text
    return response.json()["results"][0]


def rows() -> list[dict[str, Any]]:
    with shared.get_db() as db:
        return [dict(r) for r in db.execute("SELECT * FROM release_selection_evidence")]


@pytest.mark.parametrize(
    "receipt,status",
    [(None, 400), ("", 400), (1, 400), ([], 400), ("bad", 400), ("A" * 64, 410)],
)
def test_present_invalid_receipt_never_submits(
    selection: Flow, receipt: Any, status: int
) -> None:
    response = selection.client.post(
        GRAB,
        json={
            "url": "https://download.test/manual",
            "title": "Test Series Vol 1",
            "protocol": "torrent",
            "seeders": 10,
            "selection_receipt": receipt,
        },
        headers=browser_headers(selection),
    )
    assert response.status_code == status
    assert selection.submitted == []


def test_retained_selection_rejects_changed_body(selection: Flow) -> None:
    item = search(selection)
    # Before implementation the current route silently ignores this handle.
    item.setdefault("selection_receipt", "A" * 64)
    item["url"] = "https://download.test/changed"
    response = selection.client.post(
        GRAB, json=item, headers=browser_headers(selection)
    )
    assert response.status_code == 409
    assert selection.submitted == []


def test_concurrent_handle_consumption_submits_once(selection: Flow) -> None:
    item = search(selection)
    item.setdefault("selection_receipt", "A" * 64)

    def submit(_: int) -> int:
        return selection.client.post(
            GRAB, json=item, headers=browser_headers(selection)
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(submit, range(2)))
    assert sorted(statuses) == [200, 410]
    assert len(selection.submitted) == 1
    assert rows() == []


def test_absent_legacy_receipt_does_not_promote_caller_source(selection: Flow) -> None:
    item = search(selection)
    item.pop("selection_receipt", None)
    item.update(
        _source_context={"source_scope": "bs1:d:" + "a" * 64},
        release_source_scope="bs1:d:" + "a" * 64,
    )
    response = selection.client.post(
        GRAB, json=item, headers=browser_headers(selection)
    )
    assert response.status_code == 200 and response.json()["ok"] is True
    assert len(selection.submitted) == 1
    with shared.get_db() as db:
        row = db.execute("SELECT release_source_scope FROM seen").fetchone()
        assert row is not None and row[0] is None


def test_real_browser_roundtrip_records_observation(selection: Flow) -> None:
    item = search(selection)
    receipt = item.get("selection_receipt")
    assert isinstance(receipt, str) and len(receipt) == 64
    stored = rows()
    assert len(stored) == 1
    assert stored[0]["handle_digest"] == hashlib.sha256(receipt.encode()).hexdigest()
    assert receipt not in str(stored)
    response = selection.client.post(
        GRAB, json=item, headers=browser_headers(selection)
    )
    assert response.status_code == 200 and response.json()["ok"] is True
    assert len(selection.submitted) == 1
    with shared.get_db() as db:
        scope = db.execute("SELECT release_source_scope FROM seen").fetchone()[0]
        assert (
            scope
            == db.execute("SELECT release_source_scope FROM indexers").fetchone()[0]
        )
    assert len(selection.requests) == 2
    assert rows() == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("url", "https://other.test/a"),
        ("guid", "changed"),
        ("title", "changed"),
        ("protocol", "nzb"),
        ("indexer", "changed"),
        ("size_bytes", 123),
        ("seeders", 123),
        ("preferred_client_id", 23),
        ("size_bytes", True),
        ("seeders", "10"),
        ("guid", None),
    ],
)
def test_echo_mismatch_preserves_valid_handle(
    selection: Flow, field: str, value: Any
) -> None:
    item = search(selection)
    changed = {**item, field: value}
    response = selection.client.post(
        GRAB, json=changed, headers=browser_headers(selection)
    )
    assert response.status_code == 409
    assert selection.submitted == [] and len(rows()) == 1
    response = selection.client.post(
        GRAB, json=item, headers=browser_headers(selection)
    )
    assert response.status_code == 200 and response.json()["ok"]


@pytest.mark.parametrize(
    "guid,expected",
    [(" GUID ", "GUID"), (" \t ", ""), (123, ""), ("G" * 4097, "G" * 4097)],
)
def test_canonical_guid_roundtrip(
    selection: Flow, monkeypatch: pytest.MonkeyPatch, guid: Any, expected: str
) -> None:
    # Preserve real acquisition context while simulating the raw parser scalar.
    import main

    original = main._search_all

    async def parsed(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        found = await original(*args, **kwargs)
        for item in found:
            item["guid"] = guid
        return found

    monkeypatch.setattr(main, "_search_all", parsed)
    item = search(selection)
    assert item["guid"] == expected
    response = selection.client.post(
        GRAB, json=item, headers=browser_headers(selection)
    )
    assert response.status_code == 200 and response.json()["ok"]
    with shared.get_db() as db:
        history = json.loads(
            db.execute(
                "SELECT data FROM history WHERE event_type='grabbed'"
            ).fetchone()[0]
        )
    assert ("release_identity" in history) is (expected == "GUID")


@pytest.mark.parametrize("kind", ["expired", "future", "unknown", "replay"])
def test_gone_handles(selection: Flow, kind: str) -> None:
    item = search(selection)
    if kind in ("expired", "future"):
        with shared.get_db() as db:
            delta = -901 if kind == "expired" else 10
            db.execute(
                "UPDATE release_selection_evidence SET issued_at=issued_at+?,expires_at=expires_at+?",
                (delta, delta),
            )
    elif kind == "unknown":
        item["selection_receipt"] = "B" * 64
    else:
        assert selection.client.post(
            GRAB, json=item, headers=browser_headers(selection)
        ).json()["ok"]
    before = len(selection.submitted)
    response = selection.client.post(
        GRAB, json=item, headers=browser_headers(selection)
    )
    assert response.status_code == 410 and len(selection.submitted) == before


@pytest.mark.parametrize(
    "kind",
    [
        "purpose",
        "version",
        "boolean_version",
        "duplicate",
        "context",
        "unknown_field",
        "nonobject",
        "size_bool",
        "size_string",
        "preferred_bool",
        "guid_noncanonical",
        "protocol",
        "context_bool",
        "nested_duplicate",
    ],
)
def test_corrupt_snapshot_fails_closed(selection: Flow, kind: str) -> None:
    item = search(selection)
    raw = rows()[0]["snapshot_json"]
    value = json.loads(raw)
    if kind == "purpose":
        value["purpose"] = "credential"
    elif kind == "version":
        value["version"] = 2
    elif kind == "boolean_version":
        value["version"] = True
    elif kind == "context":
        value["context"]["row_scope"] = "bad"
    elif kind == "unknown_field":
        value["unknown"] = "private-sentinel"
    elif kind == "nonobject":
        value = []
    elif kind == "size_bool":
        value["release"]["size_bytes"] = True
    elif kind == "size_string":
        value["release"]["size_bytes"] = "1024"
    elif kind == "preferred_bool":
        value["release"]["preferred_client_id"] = True
    elif kind == "guid_noncanonical":
        value["release"]["guid"] = " GUID "
    elif kind == "protocol":
        value["release"]["protocol"] = "usenet"
    elif kind == "context_bool":
        value["context"]["indexer_id"] = True
    corrupt = '{"version":1,' + raw[1:] if kind == "duplicate" else json.dumps(value)
    if kind == "nested_duplicate":
        corrupt = corrupt.replace(
            '"release": {', '"release": {"url":"https://evil.test",'
        )
    with shared.get_db() as db:
        db.execute("UPDATE release_selection_evidence SET snapshot_json=?", (corrupt,))
    response = selection.client.post(
        GRAB, json=item, headers=browser_headers(selection)
    )
    assert response.status_code == 503
    assert selection.submitted == [] and "private-sentinel" not in response.text


def test_duplicate_post_keys_refused(selection: Flow) -> None:
    item = search(selection)
    raw = '{"url":"https://evil.test",' + json.dumps(item)[1:]
    response = selection.client.post(
        GRAB,
        content=raw,
        headers={**browser_headers(selection), "Content-Type": "application/json"},
    )
    assert response.status_code == 400 and selection.submitted == []
    assert len(rows()) == 1


@pytest.mark.parametrize("change", ["source", "volume", "session", "api_mode"])
def test_authority_target_and_source_mismatch(selection: Flow, change: str) -> None:
    import main
    from fastapi.testclient import TestClient
    from test_browser_auth import _login

    item = search(selection)
    client = selection.client
    headers = browser_headers(selection)
    if change == "source":
        selection.edit(1, baseUrl="http://192.168.50.11")
    elif change == "volume":
        with shared.get_db() as db:
            db.execute(
                "INSERT INTO series(id,title,search_pattern) VALUES(2,'Other','Other')"
            )
            db.execute("UPDATE volumes SET series_id=2 WHERE id=1")
    elif change == "session":
        client = TestClient(main.app)
        assert _login(client).status_code == 303
        headers = {"X-CSRFToken": str(client.cookies.get("csrftoken"))}
    else:
        headers = selection.headers
    try:
        response = client.post(GRAB, json=item, headers=headers)
        assert response.status_code == 409 and selection.submitted == []
        assert len(rows()) == 1
    finally:
        if client is not selection.client:
            client.close()


def test_api_mode_rotation_and_browser_refusal(selection: Flow) -> None:
    item = search(selection, api=True)
    response = selection.client.post(
        GRAB, json=item, headers=browser_headers(selection)
    )
    assert response.status_code == 409 and len(rows()) == 1
    with shared.get_db() as db:
        db.execute(
            "UPDATE settings SET value='rotated-fixture-key' WHERE key='api_key'"
        )
    shared.CONFIG["api_key"] = "rotated-fixture-key"
    response = selection.client.post(
        GRAB, json=item, headers={"X-Api-Key": "rotated-fixture-key"}
    )
    assert response.status_code == 200 and response.json()["ok"]


@pytest.mark.parametrize("action", ["logout", "others", "password", "expiry", "reset"])
def test_real_session_revocation_cascades(selection: Flow, action: str) -> None:
    search(selection)
    token = str(selection.client.cookies.get(auth.AUTH_COOKIE_NAME))
    if action == "logout":
        response = selection.client.post(
            "/logout",
            data={"csrf_token": selection.client.cookies.get("csrftoken")},
            follow_redirects=False,
        )
        assert response.status_code == 303
    elif action == "others":
        auth.delete_other_sessions(auth.create_session())
    elif action == "password":
        auth.update_admin_password(auth.hash_password("different valid password"))
    elif action == "reset":
        auth.reset_admin_for_recovery()
    else:
        with shared.get_db() as db:
            db.execute(
                "UPDATE auth_sessions SET expires_at='2000-01-01T00:00:00+00:00'"
            )
        assert auth.validate_session(token) is None
    assert rows() == []


def test_csrf_and_unauthorized_fail_before_consume(selection: Flow) -> None:
    import main
    from fastapi.testclient import TestClient

    item = search(selection)
    assert selection.client.post(GRAB, json=item).status_code == 403
    client = TestClient(main.app)
    try:
        assert client.post(GRAB, json=item).status_code == 401
    finally:
        client.close()
    assert len(rows()) == 1 and selection.submitted == []


def test_test_auth_bypass_is_not_capability(selection: Flow) -> None:
    selection.client.cookies.delete(auth.AUTH_COOKIE_NAME)
    auth.set_test_auth_bypass(True)
    response = selection.client.get(SEARCH)
    assert response.status_code == 409
    assert rows() == []


def test_downstream_rejection_still_consumes(selection: Flow) -> None:
    from clients import GrabResult

    item = search(selection)
    selection.result = GrabResult(False, "qbittorrent", None, True, None)
    response = selection.client.post(
        GRAB, json=item, headers=browser_headers(selection)
    )
    assert response.status_code == 200 and not response.json()["ok"]
    assert rows() == [] and len(selection.submitted) == 1
    assert (
        selection.client.post(
            GRAB, json=item, headers=browser_headers(selection)
        ).status_code
        == 410
    )


def test_store_unavailable_no_fallback(selection: Flow) -> None:
    item = search(selection)
    with shared.get_db() as db:
        db.execute("DROP TABLE release_selection_evidence")
    response = selection.client.post(
        GRAB, json=item, headers=browser_headers(selection)
    )
    assert response.status_code == 503 and selection.submitted == []
    assert selection.client.get(SEARCH).status_code == 503


def test_encryption_oracle_cannot_mint_selection(selection: Flow) -> None:
    import security

    forged = security.encrypt_secret(
        json.dumps(
            {"purpose": "blocklist-release-selection", "url": "https://evil.test"}
        )
    )
    response = selection.client.post(
        GRAB,
        json={"url": "https://evil.test", "selection_receipt": forged},
        headers=browser_headers(selection),
    )
    assert response.status_code == 400 and selection.submitted == []


def test_startup_invalidates_all_worker_handles_before_serving(
    selection: Flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    import main

    first = search(selection)
    second = search(selection, api=True)
    with shared.get_db() as db:
        epoch = db.execute("SELECT release_source_scope FROM indexers").fetchone()[0]

    class StopStartup(Exception):
        pass

    def stop() -> None:
        raise StopStartup

    monkeypatch.setattr(auth, "remove_legacy_setup_token", stop)

    async def start() -> None:
        async with main.lifespan(main.app):
            pytest.fail("must stop before any background or external work")

    with pytest.raises(StopStartup):
        asyncio.run(start())
    assert rows() == []
    for item, headers in (
        (first, browser_headers(selection)),
        (second, selection.headers),
    ):
        assert (
            selection.client.post(GRAB, json=item, headers=headers).status_code == 410
        )
    fresh = search(selection)
    with pytest.raises(StopStartup):
        asyncio.run(start())
    assert rows() == []
    assert (
        selection.client.post(
            GRAB, json=fresh, headers=browser_headers(selection)
        ).status_code
        == 410
    )
    with shared.get_db() as db:
        assert (
            db.execute("SELECT release_source_scope FROM indexers").fetchone()[0]
            == epoch
        )


def test_staged_restore_cannot_resurrect_consumed_handle(
    selection: Flow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import backups
    from release_identity import ReleaseIdentity, add_block

    item = search(selection)
    with shared.get_db() as db:
        epoch = db.execute("SELECT release_source_scope FROM indexers").fetchone()[0]
        add_block(
            db,
            series_id=1,
            url="https://blocked.test/a",
            title="blocked",
            reason="fixture",
            indexer=None,
            protocol="torrent",
            size_bytes=1,
            identities=[ReleaseIdentity(1, epoch, "torrent", "blocked-guid")],
        )
    _, archive = backups.create_backup_archive(
        db_path=shared.DB_PATH,
        backup_dir=str(tmp_path / "archives"),
        config_dir=str(tmp_path / "keys"),
    )
    assert selection.client.post(
        GRAB, json=item, headers=browser_headers(selection)
    ).json()["ok"]
    target = tmp_path / "restored"
    original_replace = backups.os.replace

    def publish(source: str, destination: str) -> None:
        if Path(source).name == backups.DATABASE_ENTRY:
            with closing(sqlite3.connect(source)) as db:
                assert (
                    db.execute(
                        "SELECT count(*) FROM release_selection_evidence"
                    ).fetchone()[0]
                    == 0
                )
        original_replace(source, destination)

    monkeypatch.setattr(backups.os, "replace", publish)
    result = backups.restore_backup_archive(archive, config_dir=str(target))
    monkeypatch.setattr(shared, "DB_PATH", result["databasePath"])
    assert rows() == []
    assert (
        selection.client.post(
            GRAB, json=item, headers=browser_headers(selection)
        ).status_code
        == 410
    )
    with shared.get_db() as db:
        assert (
            db.execute("SELECT release_source_scope FROM indexers").fetchone()[0]
            == epoch
        )
        assert db.execute(
            "SELECT source_scope,release_guid FROM blocklist_release_identities"
        ).fetchone()[:] == (epoch, "blocked-guid")


@pytest.mark.parametrize(
    "version,missing,accepted",
    [(6, True, True), (7, True, False), (8, True, False), (8, False, False)],
)
def test_restore_legacy_missing_and_future_schema(
    tmp_path: Path, version: int, missing: bool, accepted: bool
) -> None:
    import backups

    path = tmp_path / "fixture.db"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE legacy_state(value TEXT)")
        db.execute(f"PRAGMA user_version={version}")
        if not missing:
            db.execute("CREATE TABLE release_selection_evidence(handle_digest TEXT)")
            db.execute("INSERT INTO release_selection_evidence VALUES('old')")
    archive = tmp_path / "fixture.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(path, backups.DATABASE_ENTRY)
    target = tmp_path / "target"
    target.mkdir()
    sentinel = target / backups.DATABASE_ENTRY
    sentinel.write_bytes(b"previous database")
    if accepted:
        backups.restore_backup_archive(str(archive), config_dir=str(target))
        with closing(sqlite3.connect(sentinel)) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == version
    else:
        with pytest.raises(backups.BackupArchiveError):
            backups.restore_backup_archive(str(archive), config_dir=str(target))
        assert sentinel.read_bytes() == b"previous database"


@pytest.mark.parametrize("global_cap", [False, True])
def test_capacity_prunes_expired_then_oldest_digest(
    selection: Flow, global_cap: bool
) -> None:
    search(selection)
    template = rows()[0]
    now = template["issued_at"]
    count = 2048 if global_cap else 256
    with shared.get_db() as db:
        db.execute("DELETE FROM release_selection_evidence")
    sessions: list[str] = []
    if global_cap:
        sessions = [auth.create_session() for _ in range(8)]
    digests = [hashlib.sha256(s.encode()).hexdigest() for s in sessions]
    with shared.get_db() as db:
        db.executemany(
            "INSERT INTO release_selection_evidence VALUES(?,?,?,?,?,?,?,?)",
            [
                (
                    f"{i:064x}",
                    "browser",
                    digests[i // 256] if global_cap else template["session_digest"],
                    now,
                    now + 900,
                    1,
                    1,
                    template["snapshot_json"],
                )
                for i in range(count)
            ],
        )
        db.execute(
            "INSERT INTO release_selection_evidence VALUES(?,?,?,?,?,?,?,?)",
            (
                "f" * 64,
                "api",
                None,
                now - 1000,
                now - 100,
                1,
                1,
                template["snapshot_json"],
            ),
        )
    result = search(selection)
    stored = rows()
    assert len(stored) == count
    handles = {r["handle_digest"] for r in stored}
    assert "0" * 64 not in handles and "f" * 64 not in handles
    assert f"{1:064x}" in handles
    assert hashlib.sha256(result["selection_receipt"].encode()).hexdigest() in handles


def test_highest_ranked_bounded_search(
    selection: Flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    import main

    original = main._search_all
    original_evaluate = main.evaluate_release

    async def many(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        item = (await original(*args, **kwargs))[0]
        return [
            {**item, "url": f"https://download.test/{i}", "_score": i}
            for i in range(260)
        ]

    def evaluate(item: dict[str, Any], *args: Any) -> dict[str, Any]:
        return {**original_evaluate(item, *args), "score": item["_score"]}

    monkeypatch.setattr(main, "_search_all", many)
    monkeypatch.setattr(main, "evaluate_release", evaluate)
    response = selection.client.get(SEARCH)
    assert (
        response.status_code == 200 and response.headers["cache-control"] == "no-store"
    )
    data = response.json()
    assert len(data["results"]) == 256 and data["has_more"] is True
    assert [r["score"] for r in data["results"]] == list(range(259, 3, -1))
    assert len(rows()) == 256 and len(selection.requests) == 2
    assert all(len(r["selection_receipt"]) == 64 for r in data["results"])


@pytest.mark.parametrize(
    "field,value,selectable",
    [
        ("url", "https://download.test/" + "x" * (8192 - 22), True),
        ("url", "https://download.test/" + "x" * 8192, False),
        ("title", "Test Series Vol 1 " + "x" * (2048 - 18), True),
        ("title", "Test Series Vol 1 " + "x" * 2048, False),
        ("title", "Test Series Vol 1 " + "\u00e9" * 1024, False),
        ("indexer", "x" * 16384, False),
        ("size_bytes", True, False),
    ],
    ids=[
        "url-limit",
        "url-over",
        "title-limit",
        "title-over",
        "title-utf8-over",
        "json-over",
        "bool-size",
    ],
)
def test_payload_bounds_never_truncate(
    selection: Flow,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: Any,
    selectable: bool,
) -> None:
    import main

    original = main._search_all

    async def parsed(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        found = await original(*args, **kwargs)
        for item in found:
            item[field] = value
        return found

    monkeypatch.setattr(main, "_search_all", parsed)
    item = search(selection)
    assert item[field] == value
    assert ("selection_receipt" in item) is selectable
    assert bool(rows()) is selectable
    if selectable:
        assert selection.client.post(
            GRAB, json=item, headers=browser_headers(selection)
        ).json()["ok"]


def test_store_write_failure_is_generic_and_atomic(selection: Flow) -> None:
    item = search(selection)
    with shared.get_db() as db:
        db.execute(
            "CREATE TRIGGER deny_consume BEFORE DELETE ON release_selection_evidence BEGIN SELECT RAISE(ABORT, 'private-sentinel'); END"
        )
    response = selection.client.post(
        GRAB, json=item, headers=browser_headers(selection)
    )
    assert response.status_code == 503 and selection.submitted == []
    assert "private-sentinel" not in response.text and len(rows()) == 1


def test_cli_import_graph_stays_offline_and_pure(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "app")}
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import cli,sys; assert 'release_selections' in sys.modules; assert 'release_identity' in sys.modules; assert not any(n in sys.modules for n in ('release_sources','routers','schema','main'))",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("extra", [0, 1])
def test_exact_snapshot_json_byte_limit(
    selection: Flow, monkeypatch: pytest.MonkeyPatch, extra: int
) -> None:
    import main

    search(selection)
    raw = rows()[0]["snapshot_json"]
    previous = json.loads(raw)["release"]["indexer"]
    size = 16384 - len(raw.encode()) + len(previous.encode()) + extra
    with shared.get_db() as db:
        db.execute("DELETE FROM release_selection_evidence")
    original = main._search_all

    async def parsed(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        found = await original(*args, **kwargs)
        for item in found:
            item["indexer"] = "i" * size
        return found

    monkeypatch.setattr(main, "_search_all", parsed)
    item = search(selection)
    assert ("selection_receipt" in item) is (extra == 0)
    if extra == 0:
        assert len(rows()[0]["snapshot_json"].encode()) == 16384
    else:
        assert rows() == []


@pytest.mark.parametrize("protocol", ["unknown", "usenet"])
def test_incomplete_protocol_is_not_issued_known_identity(
    selection: Flow, protocol: str
) -> None:
    selection.raw_protocol = protocol
    item = search(selection)
    assert item["selectable"] is False and "selection_receipt" not in item
    assert rows() == [] and len(selection.requests) == 2


def test_presentation_only_echo_changes_are_ignored(selection: Flow) -> None:
    item = search(selection)
    item.update(
        score=-100,
        status="rejected",
        seen=True,
        blocked=True,
        _source_context={"row_scope": "fake"},
    )
    response = selection.client.post(
        GRAB, json=item, headers=browser_headers(selection)
    )
    assert response.status_code == 200 and response.json()["ok"]
    assert len(selection.submitted) == 1 and len(selection.requests) == 2


def test_direct_recovery_reset_cannot_reauthorize_old_handle(
    selection: Flow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import main
    from fastapi.testclient import TestClient

    item = search(selection)
    # This established offline entry point uses its own connection without FKs.
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    with (
        closing(sqlite3.connect(shared.DB_PATH)) as source,
        closing(sqlite3.connect(recovery / "manga_arr.db")) as target,
    ):
        source.backup(target)
    monkeypatch.setattr(shared, "DB_PATH", str(recovery / "manga_arr.db"))
    monkeypatch.setattr(main, "DB_PATH", shared.DB_PATH)
    auth.reset_admin_for_recovery(str(recovery))
    assert (
        selection.client.post(
            GRAB, json=item, headers=browser_headers(selection)
        ).status_code
        == 401
    )
    fresh = TestClient(main.app)
    try:
        _setup(fresh)
        response = fresh.post(
            GRAB,
            json=item,
            headers={"X-CSRFToken": str(fresh.cookies.get("csrftoken"))},
        )
        assert response.status_code == 409 and selection.submitted == []
    finally:
        fresh.close()


@pytest.mark.parametrize("mutation", ["type", "purpose", "tag", "disabled", "deleted"])
def test_retained_source_eligibility_is_live(selection: Flow, mutation: str) -> None:
    item = search(selection)
    if mutation == "type":
        selection.edit(1, implementation="newznab")
    elif mutation == "disabled":
        selection.edit(1, enable=False)
    else:
        with shared.get_db() as db:
            if mutation == "purpose":
                db.execute("UPDATE indexers SET use_interactive_search=0 WHERE id=1")
            elif mutation == "tag":
                db.execute("INSERT INTO indexer_tags(indexer_id,tag) VALUES(1,'other')")
            else:
                db.execute("DELETE FROM indexers WHERE id=1")
    response = selection.client.post(
        GRAB, json=item, headers=browser_headers(selection)
    )
    assert response.status_code == 409 and selection.submitted == []
    assert len(rows()) == 1 and len(selection.requests) == 2


@pytest.mark.parametrize("mutation", ["source", "blocklist"])
def test_final_gate_after_consumption_is_still_mandatory(
    selection: Flow, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    import main

    item = search(selection)
    original = main.grab_item

    async def race(observed: dict[str, Any], series_id: int, **kwargs: Any) -> bool:
        assert rows() == []
        if mutation == "source":
            selection.edit(1, baseUrl="http://192.168.50.11")
        else:
            with shared.get_db() as db:
                db.execute(
                    "INSERT INTO blocklist(series_id,torrent_url,torrent_name,reason) VALUES(1,?,'blocked','fixture')",
                    (item["url"],),
                )
        return await original(observed, series_id, **kwargs)

    monkeypatch.setattr(main, "grab_item", race)
    response = selection.client.post(
        GRAB, json=item, headers=browser_headers(selection)
    )
    assert response.status_code == 200 and not response.json()["ok"]
    assert selection.submitted == [] and rows() == []
    assert len(selection.requests) == 2


def test_consumption_prunes_expired_rows_even_on_unknown_receipt(
    selection: Flow,
) -> None:
    item = search(selection)
    with shared.get_db() as db:
        db.execute(
            "UPDATE release_selection_evidence SET issued_at=issued_at-901,expires_at=expires_at-901"
        )
    item["selection_receipt"] = "C" * 64
    assert (
        selection.client.post(
            GRAB, json=item, headers=browser_headers(selection)
        ).status_code
        == 410
    )
    assert rows() == [] and selection.submitted == []


@pytest.mark.parametrize("missing", ["url", "guid", "preferred_client_id"])
def test_complete_echo_including_preferred_client(
    selection: Flow, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    import main

    original = main._search_all

    async def preferred(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        found = await original(*args, **kwargs)
        for item in found:
            item["preferred_client_id"] = 23
        return found

    monkeypatch.setattr(main, "_search_all", preferred)
    item = search(selection)
    assert item["preferred_client_id"] == 23
    changed = {k: v for k, v in item.items() if k != missing}
    response = selection.client.post(
        GRAB, json=changed, headers=browser_headers(selection)
    )
    assert (
        response.status_code == 409 and len(rows()) == 1 and selection.submitted == []
    )
    # Configure an actual client matching that preference before handoff.
    with shared.get_db() as db:
        db.execute(
            "INSERT INTO download_clients(id,name,type,host,enabled) VALUES(23,'fixture','qbittorrent','http://download-client.test',1)"
        )
    response = selection.client.post(
        GRAB, json=item, headers=browser_headers(selection)
    )
    assert response.status_code == 200 and response.json()["ok"]


def test_staged_restore_failure_preserves_live_files(
    selection: Flow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import backups

    search(selection)
    _, archive = backups.create_backup_archive(
        db_path=shared.DB_PATH,
        backup_dir=str(tmp_path / "archives"),
        config_dir=str(tmp_path / "keys"),
    )
    target = tmp_path / "rollback-target"
    target.mkdir()
    database = target / backups.DATABASE_ENTRY
    key = target / backups.SECRET_KEY_ENTRY
    database.write_bytes(b"current database")
    key.write_bytes(b"current key")

    def fail(db: sqlite3.Connection, *, allow_legacy: bool = False) -> None:
        db.execute("DELETE FROM release_selection_evidence")
        raise sqlite3.DatabaseError("private-sentinel")

    monkeypatch.setattr(backups, "clear_selection_evidence", fail)
    with pytest.raises(backups.BackupArchiveError, match="failed validation"):
        backups.restore_backup_archive(archive, config_dir=str(target))
    assert (
        database.read_bytes() == b"current database"
        and key.read_bytes() == b"current key"
    )
    assert not list(target.glob("*.pre-restore-*"))


def helper_contract_state(
    state: Flow,
) -> tuple[
    dict[str, Any], selections.SelectionSnapshot, selections.SelectionIssuer, int
]:
    item = search(state, api=True)
    retained = rows()[0]
    return (
        item,
        selections.decode_snapshot(retained["snapshot_json"]),
        selections.SelectionIssuer("api", None),
        retained["issued_at"],
    )


@pytest.mark.parametrize("operation", ["issue", "consume"])
def test_helper_contract_preserves_reserved_caller_transaction(
    selection: Flow, operation: str
) -> None:
    item, snapshot, issuer, now = helper_contract_state(selection)
    with closing(sqlite3.connect(shared.DB_PATH)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO settings(key,value) VALUES('selection-pending','pending')"
        )
        failure = None
        try:
            try:
                if operation == "issue":
                    selections.issue_selections(
                        db, issuer=issuer, snapshots=[snapshot], now=now
                    )
                else:
                    selections.consume_selection(
                        db,
                        issuer=issuer,
                        handle=item["selection_receipt"],
                        series_id=1,
                        volume_id=1,
                        posted=item,
                        now=now,
                    )
            except selections.SelectionError as exc:
                failure = str(exc)
            assert db.in_transaction, f"{operation} destroyed the caller transaction"
            assert (
                db.execute(
                    "SELECT value FROM settings WHERE key='selection-pending'"
                ).fetchone()[0]
                == "pending"
            )
            assert failure is None, (
                f"{operation} rejected a valid reserved caller transaction: {failure}"
            )
        finally:
            db.rollback()
    assert len(rows()) == 1 and selection.submitted == []


@pytest.mark.parametrize("operation", ["issue", "consume"])
def test_helper_contract_success_is_undoable_by_caller_rollback(
    selection: Flow, operation: str
) -> None:
    item, snapshot, issuer, now = helper_contract_state(selection)
    before = len(rows())
    with closing(sqlite3.connect(shared.DB_PATH)) as db:
        db.row_factory = sqlite3.Row
        try:
            if operation == "issue":
                selections.issue_selections(
                    db, issuer=issuer, snapshots=[snapshot], now=now
                )
            else:
                selections.consume_selection(
                    db,
                    issuer=issuer,
                    handle=item["selection_receipt"],
                    series_id=1,
                    volume_id=1,
                    posted=item,
                    now=now,
                )
        finally:
            db.rollback()
    assert len(rows()) == before, f"{operation} committed beyond the caller's rollback"
    assert selection.submitted == []


def test_approved_mapping_snapshot_roundtrip_and_immutable_copy(
    selection: Flow,
) -> None:
    item, observed, issuer, now = helper_contract_state(selection)
    snapshot = None
    try:
        snapshot = selections.SelectionSnapshot(1, 1, observed.context, item)
    except TypeError:
        pass
    assert snapshot is not None, (
        "approved SelectionSnapshot(..., itemMapping) must construct a real observation"
    )
    behavior = snapshot.behavior()
    item["title"] = "caller-mutated"
    assert snapshot.behavior() == behavior
    assert isinstance(snapshot.item, MappingProxyType)
    returned = snapshot.behavior()
    returned["title"] = "mutated"
    assert snapshot.behavior() == behavior
    with pytest.raises(AttributeError):
        setattr(snapshot, "series_id", 2)
    restored = selections.decode_snapshot(selections.encode_snapshot(snapshot))
    assert restored.behavior() == behavior and restored.context == snapshot.context
    with shared.get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        handles = selections.issue_selections(
            db, issuer=issuer, snapshots=[snapshot], now=now + 37
        )
        retained = db.execute(
            "SELECT issued_at,expires_at FROM release_selection_evidence WHERE handle_digest=?",
            (hashlib.sha256(handles[0].encode()).hexdigest(),),
        ).fetchone()
        assert tuple(retained) == (now + 37, now + 937)
        consumed = selections.consume_selection(
            db,
            handle=handles[0],
            issuer=issuer,
            series_id=1,
            volume_id=1,
            posted=behavior,
            now=now + 38,
        )
        assert consumed.behavior() == behavior
        assert db.in_transaction
    assert len(rows()) == 1 and selection.submitted == []


@pytest.mark.parametrize("operation", ["issue", "consume"])
def test_approved_explicit_now_and_posted_helper_contract_on_real_db(
    selection: Flow, operation: str
) -> None:
    item, snapshot, issuer, now = helper_contract_state(selection)
    failure = None
    with closing(sqlite3.connect(shared.DB_PATH)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        try:
            try:
                if operation == "issue":
                    handles = selections.issue_selections(
                        db, issuer=issuer, snapshots=[snapshot], now=now + 37
                    )
                    row = db.execute(
                        "SELECT issued_at FROM release_selection_evidence WHERE handle_digest=?",
                        (hashlib.sha256(handles[0].encode()).hexdigest(),),
                    ).fetchone()
                    assert row[0] == now + 37
                else:
                    consumed = selections.consume_selection(
                        db,
                        handle=item["selection_receipt"],
                        issuer=issuer,
                        series_id=1,
                        volume_id=1,
                        posted=item,
                        now=now + 1,
                    )
                    assert consumed.behavior() == snapshot.behavior()
                    assert (
                        db.execute(
                            "SELECT count(*) FROM release_selection_evidence"
                        ).fetchone()[0]
                        == 0
                    )
            except TypeError as exc:
                failure = str(exc)
            assert failure is None, (
                f"approved {operation} contract rejected before real DB operation: {failure}"
            )
            assert db.in_transaction
        finally:
            db.rollback()
    assert len(rows()) == 1 and selection.submitted == []


@pytest.mark.parametrize(
    "status,reason",
    [(400, "invalid"), (410, "gone"), (409, "mismatch"), (503, "unavailable")],
)
def test_approved_error_status_reason_and_real_db_refusal(
    selection: Flow,
    status: int,
    reason: Literal["invalid", "gone", "mismatch", "unavailable"],
) -> None:
    item, _, issuer, now = helper_contract_state(selection)
    error = None
    try:
        error = selections.SelectionError(status, reason)
    except TypeError:
        pass
    assert error is not None, (
        "approved SelectionError(status, reason) must be constructible"
    )
    assert error.status == status and error.reason == reason
    posted = dict(item)
    handle: object = item["selection_receipt"]
    if reason == "invalid":
        handle = None
    elif reason == "gone":
        handle = "Z" * 64
    elif reason == "mismatch":
        posted["url"] = "https://private-sentinel.test/changed"
    else:
        with shared.get_db() as db:
            db.execute("UPDATE release_selection_evidence SET snapshot_json='{}'")
    with shared.get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(selections.SelectionError) as caught:
            selections.consume_selection(
                db,
                handle=handle,
                issuer=issuer,
                series_id=1,
                volume_id=1,
                posted=posted,
                now=now,
            )
        assert (caught.value.status, caught.value.reason) == (status, reason)
        assert "private-sentinel" not in str(caught.value)
        assert str(handle) not in str(caught.value)
        assert db.in_transaction
    assert len(rows()) == 1 and selection.submitted == []


def expired_contract_row(now: int) -> str:
    retained = rows()[0]
    digest = "0" * 64
    with shared.get_db() as db:
        db.execute(
            "INSERT INTO release_selection_evidence VALUES(?,?,?,?,?,?,?,?)",
            (
                digest,
                retained["issuer_kind"],
                retained["session_digest"],
                now - 901,
                now - 1,
                1,
                1,
                retained["snapshot_json"],
            ),
        )
    return digest


def test_caller_can_commit_refusal_pruning_without_losing_pending_writes(
    selection: Flow,
) -> None:
    item, _, issuer, now = helper_contract_state(selection)
    expired_contract_row(now)
    with shared.get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO settings(key,value) VALUES('selection-pending','pending')"
        )
        with pytest.raises(selections.SelectionError) as caught:
            selections.consume_selection(
                db,
                handle=item["selection_receipt"],
                issuer=issuer,
                series_id=1,
                volume_id=1,
                posted={**item, "guid": "changed"},
                now=now,
            )
        assert caught.value.reason == "mismatch" and db.in_transaction
        assert (
            db.execute(
                "SELECT value FROM settings WHERE key='selection-pending'"
            ).fetchone()[0]
            == "pending"
        )
        assert (
            db.execute("SELECT count(*) FROM release_selection_evidence").fetchone()[0]
            == 1
        )
    assert len(rows()) == 1
    with shared.get_db() as db:
        assert (
            db.execute(
                "SELECT value FROM settings WHERE key='selection-pending'"
            ).fetchone()[0]
            == "pending"
        )


@pytest.mark.parametrize("operation", ["issue", "consume"])
def test_unavailable_helper_does_not_rollback_or_commit_caller_state(
    selection: Flow, operation: str
) -> None:
    item, snapshot, issuer, now = helper_contract_state(selection)
    expired_contract_row(now)
    digest = hashlib.sha256(item["selection_receipt"].encode()).hexdigest()
    with shared.get_db() as db:
        if operation == "issue":
            db.execute(
                "CREATE TRIGGER contract_failure BEFORE INSERT ON release_selection_evidence BEGIN SELECT RAISE(ABORT,'private-sentinel'); END"
            )
        else:
            db.execute(
                f"CREATE TRIGGER contract_failure BEFORE DELETE ON release_selection_evidence WHEN OLD.handle_digest='{digest}' BEGIN SELECT RAISE(ABORT,'private-sentinel'); END"
            )
    with closing(sqlite3.connect(shared.DB_PATH)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO settings(key,value) VALUES('selection-pending','pending')"
        )
        try:
            with pytest.raises(selections.SelectionError) as caught:
                if operation == "issue":
                    selections.issue_selections(
                        db, issuer=issuer, snapshots=[snapshot], now=now
                    )
                else:
                    selections.consume_selection(
                        db,
                        handle=item["selection_receipt"],
                        issuer=issuer,
                        series_id=1,
                        volume_id=1,
                        posted=item,
                        now=now,
                    )
            assert caught.value.reason == "unavailable"
            assert "private-sentinel" not in str(caught.value)
            assert db.in_transaction
            assert (
                db.execute(
                    "SELECT value FROM settings WHERE key='selection-pending'"
                ).fetchone()[0]
                == "pending"
            )
            assert (
                db.execute(
                    "SELECT count(*) FROM release_selection_evidence"
                ).fetchone()[0]
                == 1
            )
        finally:
            db.rollback()
    assert len(rows()) == 2
    with shared.get_db() as db:
        assert (
            db.execute(
                "SELECT 1 FROM settings WHERE key='selection-pending'"
            ).fetchone()
            is None
        )


def test_http_refusal_commits_pruning_but_not_valid_handle_consumption(
    selection: Flow,
) -> None:
    item = search(selection)
    expired_contract_row(rows()[0]["issued_at"])
    response = selection.client.post(
        GRAB, json={**item, "guid": "changed"}, headers=browser_headers(selection)
    )
    assert response.status_code == 409 and len(rows()) == 1
    assert selection.client.post(
        GRAB, json=item, headers=browser_headers(selection)
    ).json()["ok"]
    assert rows() == [] and len(selection.submitted) == 1


def test_http_callers_reserve_and_complete_before_downloader_await(
    selection: Flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    from routers import series_

    statements: list[str] = []
    original = shared.get_db

    @contextmanager
    def traced() -> Iterator[sqlite3.Connection]:
        with original() as db:
            db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(series_, "get_db", traced)
    item = search(selection)
    assert selection.client.post(
        GRAB, json=item, headers=browser_headers(selection)
    ).json()["ok"]
    boundaries = [
        s.upper()
        for s in statements
        if s.upper().startswith(("BEGIN", "COMMIT", "ROLLBACK"))
    ]
    assert boundaries == ["BEGIN IMMEDIATE", "COMMIT", "BEGIN IMMEDIATE", "COMMIT"]
    assert len(selection.submitted) == 1 and len(selection.requests) == 2


@pytest.mark.parametrize(
    "status,reason",
    [
        (400, "private-sentinel"),
        (True, "invalid"),
        (503, "invalid"),
        (400, None),
        (400, []),
    ],
)
def test_error_contract_rejects_invalid_status_reason_without_secret_echo(
    status: Any, reason: Any
) -> None:
    with pytest.raises(ValueError) as caught:
        selections.SelectionError(status, reason)
    assert "private-sentinel" not in str(caught.value)


@pytest.mark.parametrize("operation", ["issue", "consume"])
def test_required_now_rejects_call_without_mutating_caller_transaction(
    selection: Flow, operation: str
) -> None:
    item, snapshot, issuer, _ = helper_contract_state(selection)
    function: Any = (
        selections.issue_selections
        if operation == "issue"
        else selections.consume_selection
    )
    arguments = (
        {"issuer": issuer, "snapshots": [snapshot]}
        if operation == "issue"
        else {
            "issuer": issuer,
            "handle": item["selection_receipt"],
            "series_id": 1,
            "volume_id": 1,
            "posted": item,
        }
    )
    with closing(sqlite3.connect(shared.DB_PATH)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO settings(key,value) VALUES('selection-pending','pending')"
        )
        try:
            with pytest.raises(TypeError):
                function(db, **arguments)
            assert db.in_transaction
            assert (
                db.execute(
                    "SELECT value FROM settings WHERE key='selection-pending'"
                ).fetchone()[0]
                == "pending"
            )
        finally:
            db.rollback()
    assert len(rows()) == 1


@pytest.mark.parametrize("delta,expected", [(-1, "gone"), (899, None), (900, "gone")])
def test_explicit_now_controls_exact_expiry_and_clock_rollback(
    selection: Flow, delta: int, expected: str | None
) -> None:
    item, snapshot, issuer, now = helper_contract_state(selection)
    with closing(sqlite3.connect(shared.DB_PATH)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        try:
            if expected is None:
                assert (
                    selections.consume_selection(
                        db,
                        handle=item["selection_receipt"],
                        issuer=issuer,
                        series_id=1,
                        volume_id=1,
                        posted=item,
                        now=now + delta,
                    ).behavior()
                    == snapshot.behavior()
                )
            else:
                with pytest.raises(selections.SelectionError) as caught:
                    selections.consume_selection(
                        db,
                        handle=item["selection_receipt"],
                        issuer=issuer,
                        series_id=1,
                        volume_id=1,
                        posted=item,
                        now=now + delta,
                    )
                assert (caught.value.status, caught.value.reason) == (410, expected)
            assert db.in_transaction
        finally:
            db.rollback()
    assert len(rows()) == 1


def test_browser_issuer_uses_injected_now_not_hidden_auth_clock(
    selection: Flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    search(selection)
    retained = rows()[0]
    snapshot = selections.decode_snapshot(retained["snapshot_json"])
    issuer = selections.SelectionIssuer("browser", retained["session_digest"])
    with shared.get_db() as db:
        expiry = auth._parse_timestamp(
            db.execute(
                "SELECT expires_at FROM auth_sessions WHERE token_hash=?",
                (issuer.session_digest,),
            ).fetchone()[0]
        )

    def hidden_clock() -> Any:
        pytest.fail("selection helpers must use the supplied clock")

    monkeypatch.setattr(auth, "_now", hidden_clock)
    with closing(sqlite3.connect(shared.DB_PATH)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(selections.SelectionError) as caught:
                selections.issue_selections(
                    db,
                    issuer=issuer,
                    snapshots=[snapshot],
                    now=int(expiry.timestamp()) + 1,
                )
            assert (caught.value.status, caught.value.reason) == (409, "mismatch")
            assert db.in_transaction
        finally:
            db.rollback()
    assert len(rows()) == 1


@pytest.mark.parametrize("now", [True, -1, 1.5])
def test_invalid_injected_time_is_not_coerced_or_written(
    selection: Flow, now: Any
) -> None:
    _, snapshot, issuer, _ = helper_contract_state(selection)
    with shared.get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="invalid selection time"):
            selections.issue_selections(
                db, issuer=issuer, snapshots=[snapshot], now=now
            )
        assert db.in_transaction
    assert len(rows()) == 1


def test_http_unavailable_rolls_back_partial_expiry_pruning(selection: Flow) -> None:
    item = search(selection)
    expired_contract_row(rows()[0]["issued_at"])
    digest = hashlib.sha256(item["selection_receipt"].encode()).hexdigest()
    with shared.get_db() as db:
        db.execute(
            f"CREATE TRIGGER contract_failure BEFORE DELETE ON release_selection_evidence WHEN OLD.handle_digest='{digest}' BEGIN SELECT RAISE(ABORT,'private-sentinel'); END"
        )
    response = selection.client.post(
        GRAB, json=item, headers=browser_headers(selection)
    )
    assert response.status_code == 503 and "private-sentinel" not in response.text
    assert len(rows()) == 2 and selection.submitted == []
