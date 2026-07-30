"""Durability contracts for committed-import success side effects."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def outbox_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Path]:
    import import_download
    import import_execute
    import main
    import shared

    db_path = tmp_path / "success-effects.db"
    library_root = tmp_path / "library"
    source_root = tmp_path / "downloads"
    library_root.mkdir()
    source_root.mkdir()

    monkeypatch.setattr(main, "DB_PATH", str(db_path))
    monkeypatch.setattr(shared, "DB_PATH", str(db_path))
    main.init_db()
    main.load_config()
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT OR REPLACE INTO root_folders(id,path,label,is_default)"
            " VALUES(1,?,'Test',1)",
            (str(library_root),),
        )

    for config in (main.CONFIG, shared.CONFIG):
        monkeypatch.setitem(config, "save_path", str(library_root))
        monkeypatch.setitem(config, "import_mode", "copy")
        monkeypatch.setitem(config, "remove_completed", "true")
        monkeypatch.setitem(config, "komga_scan_enabled", "true")

    monkeypatch.setattr(import_execute, "_IMPORT_SEM", None)

    async def _noop(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(import_execute, "broadcast_queue_event", _noop)
    monkeypatch.setattr(import_download, "dispatch_download_notification", _noop)
    return {
        "db_path": db_path,
        "library_root": library_root,
        "source_root": source_root,
    }


def _seed_import(
    env: dict[str, Path],
    *,
    protocol: str = "torrent",
    client_type: str = "qbittorrent",
    client_name: str = "Outbox client",
    client_id: int = 915_100,
) -> tuple[int, int]:
    source = env["source_root"] / "Outbox v01.cbz"
    with zipfile.ZipFile(source, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("page.txt", b"page")

    series_id = 915_001
    with sqlite3.connect(env["db_path"]) as db:
        db.execute(
            "INSERT INTO series(id,title,search_pattern,root_folder_id,cover_url)"
            " VALUES(?, 'Outbox Series', 'outbox-series', 1,"
            " 'https://example.invalid/cover.jpg')",
            (series_id,),
        )
        db.execute(
            "INSERT INTO download_clients("
            " id,name,type,host,username,password,enabled,priority"
            ") VALUES(?,?,?,?,?,?,1,1)",
            (
                client_id,
                client_name,
                client_type,
                f"http://{client_type}.old.test:8080",
                "download-user",
                "download-password",
            ),
        )
        db.execute(
            "INSERT INTO seen(torrent_url,torrent_name,series_id,volume_num,"
            " protocol,client,download_id,download_client_id)"
            " VALUES('magnet:outbox','Outbox v01',?,1,?,?,"
            " 'outbox-download',?)",
            (series_id, protocol, client_type, client_id),
        )
        db.execute(
            "INSERT INTO volumes(series_id,volume_num,status,download_id,"
            " protocol,client,download_client_id)"
            " VALUES(?,1,'grabbed','outbox-download',?,?,?)",
            (series_id, protocol, client_type, client_id),
        )
        db.execute(
            "INSERT INTO import_queue(series_id,download_id,download_client_id,torrent_name,"
            " torrent_url,volume_num,src_dir,status)"
            " VALUES(?,'outbox-download',?,'Outbox v01','magnet:outbox',1,?,"
            " 'pending')",
            (series_id, client_id, str(env["source_root"])),
        )
        queue_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
        db.execute(
            "INSERT INTO import_queue_files(queue_id,filename,src_path,"
            " proposed_volume,file_type,proposed_import_kind,status)"
            " VALUES(?,?,?,?,?,'volume','pending')",
            (queue_id, source.name, str(source), 1.0, "volume"),
        )
    return queue_id, series_id


def _prepare_deferred_effects(
    env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    protocol: str = "torrent",
    client_type: str = "qbittorrent",
    client_name: str = "Outbox client",
    client_id: int = 915_100,
) -> int:
    import import_execute
    import import_publication

    queue_id, _ = _seed_import(
        env,
        protocol=protocol,
        client_type=client_type,
        client_name=client_name,
        client_id=client_id,
    )

    async def _defer_effects(publication_id: int) -> bool:
        del publication_id
        return False

    monkeypatch.setattr(
        import_publication,
        "_dispatch_success_effects",
        _defer_effects,
    )
    assert asyncio.run(import_execute._execute_import(queue_id)) is True
    with sqlite3.connect(env["db_path"]) as db:
        return int(db.execute("SELECT id FROM import_publications").fetchone()[0])


def test_committed_effects_replay_while_pack_cleanup_remains_deferred(
    outbox_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash after Phase 3 cannot lose effects or falsify domain success."""
    queue_id, _ = _seed_import(outbox_env)

    import clients
    import cover_images
    import import_execute
    import import_pack_cleanup
    import import_publication
    import main
    import shared

    calls: list[str] = []
    guard_cleanup_publication_ids: list[int | None] = []

    def _cover(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        calls.append("cover")
        return True

    async def _komga(payload: dict[str, object]) -> bool:
        del payload
        calls.append("komga_scan")
        return True

    async def _remove(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        calls.append("remove_completed")
        return True

    async def _defer_effects(publication_id: int) -> bool:
        del publication_id
        return False

    def _defer_guard_cleanup(
        queue_id: int,
        download_id: str,
        *,
        download_client_id: int | None,
        protocol: str | None,
        publication_id: int | None = None,
    ) -> bool:
        del queue_id, download_id, download_client_id, protocol
        guard_cleanup_publication_ids.append(publication_id)
        return False

    real_dispatch = import_publication._dispatch_success_effects
    monkeypatch.setattr(cover_images, "cached_cover_is_valid", lambda _path: False)
    monkeypatch.setattr(cover_images, "extract_cbz_cover", _cover)
    monkeypatch.setattr(
        import_publication,
        "_dispatch_journaled_komga_scan",
        _komga,
    )
    monkeypatch.setattr(clients, "qbit_remove", _remove)
    monkeypatch.setattr(clients, "sab_remove", _remove)
    monkeypatch.setattr(
        import_pack_cleanup,
        "cleanup_terminal_pack_staging",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        import_execute,
        "cleanup_terminal_pack_staging",
        _defer_guard_cleanup,
    )
    monkeypatch.setattr(
        import_publication,
        "_dispatch_success_effects",
        _defer_effects,
    )

    assert asyncio.run(import_execute._execute_import(queue_id)) is True
    assert calls == []
    with sqlite3.connect(outbox_env["db_path"]) as db:
        publication = db.execute(
            "SELECT id, state, result_ok, pack_cleanup_state FROM import_publications"
        ).fetchone()
        assert publication is not None
        assert publication[1:] == ("deleted", 1, "pending")
        assert db.execute(
            "SELECT effect_type, state, attempt_count"
            " FROM import_publication_success_effects ORDER BY effect_type"
        ).fetchall() == [
            ("cover", "pending", 0),
            ("komga_scan", "pending", 0),
            ("remove_completed", "pending", 0),
        ]
    assert guard_cleanup_publication_ids == [publication[0]]

    # The committed configuration snapshot remains authoritative after restart.
    for config in (main.CONFIG, shared.CONFIG):
        config["remove_completed"] = "false"
        config["komga_scan_enabled"] = "false"
    monkeypatch.setattr(
        import_publication,
        "_dispatch_success_effects",
        real_dispatch,
    )

    replayed = asyncio.run(import_publication.replay_import_publications(max_rows=None))
    assert replayed.completed == 1
    assert sorted(calls) == ["cover", "komga_scan", "remove_completed"]
    with sqlite3.connect(outbox_env["db_path"]) as db:
        assert db.execute(
            "SELECT effect_type, state, attempt_count"
            " FROM import_publication_success_effects ORDER BY effect_type"
        ).fetchall() == [
            ("cover", "completed", 1),
            ("komga_scan", "completed", 1),
            ("remove_completed", "completed", 1),
        ]
        assert db.execute(
            "SELECT pack_cleanup_state FROM import_publications"
        ).fetchone() == ("pending",)

    # Pack cleanup can keep replaying without duplicating completed effects.
    asyncio.run(import_publication.replay_import_publications(max_rows=None))
    assert sorted(calls) == ["cover", "komga_scan", "remove_completed"]


def test_effect_retry_backoff_expired_claim_and_idempotency_key(
    outbox_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failures back off; an expired post-effect claim safely replays."""
    queue_id, _ = _seed_import(outbox_env)

    import clients
    import cover_images
    import import_execute
    import import_publication
    import main
    import shared

    for config in (main.CONFIG, shared.CONFIG):
        config["komga_scan_enabled"] = "false"

    cover_calls = 0
    remove_calls = 0

    def _cover(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        nonlocal cover_calls
        cover_calls += 1
        return True

    async def _remove(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        nonlocal remove_calls
        remove_calls += 1
        return remove_calls > 1

    monkeypatch.setattr(cover_images, "cached_cover_is_valid", lambda _path: False)
    monkeypatch.setattr(cover_images, "extract_cbz_cover", _cover)
    monkeypatch.setattr(clients, "qbit_remove", _remove)
    monkeypatch.setattr(clients, "sab_remove", _remove)

    assert asyncio.run(import_execute._execute_import(queue_id)) is True
    assert (cover_calls, remove_calls) == (1, 1)
    with sqlite3.connect(outbox_env["db_path"]) as db:
        publication_id = int(
            db.execute("SELECT id FROM import_publications").fetchone()[0]
        )
        retry = db.execute(
            "SELECT state, attempt_count, next_attempt_at, last_error,"
            " idempotency_key FROM import_publication_success_effects"
            " WHERE effect_type='remove_completed'"
        ).fetchone()
    assert retry is not None
    assert retry[:2] == ("pending", 1)
    assert retry[2] is not None
    assert retry[3] == "unsuccessful_result"
    stable_key = f"mangarr-import-publication:{publication_id}:success:remove_completed"
    assert retry[4] == stable_key

    # A not-yet-due retry is absent from the replay page.
    immediate = asyncio.run(
        import_publication.replay_import_publications(max_rows=None)
    )
    assert immediate.examined == 0
    assert remove_calls == 1

    with sqlite3.connect(outbox_env["db_path"]) as db:
        db.execute(
            "UPDATE import_publication_success_effects"
            " SET next_attempt_at=datetime('now', '-1 second')"
            " WHERE effect_type='remove_completed'"
        )
    retried = asyncio.run(import_publication.replay_import_publications(max_rows=None))
    assert retried.completed == 1
    assert remove_calls == 2

    # Model a crash after remote acceptance but before the completed CAS.
    with sqlite3.connect(outbox_env["db_path"]) as db:
        db.execute(
            """
            UPDATE import_publication_success_effects
            SET state='dispatching', operation_owner='crashed-owner',
                operation_expires_at=datetime('now', '-1 second'),
                completed_at=NULL
            WHERE effect_type='remove_completed'
            """
        )
    recovered = asyncio.run(
        import_publication.replay_import_publications(max_rows=None)
    )
    assert recovered.completed == 1
    assert remove_calls == 3
    with sqlite3.connect(outbox_env["db_path"]) as db:
        assert db.execute(
            "SELECT state, attempt_count, operation_owner,"
            " operation_expires_at, idempotency_key"
            " FROM import_publication_success_effects"
            " WHERE effect_type='remove_completed'"
        ).fetchone() == ("completed", 3, None, None, stable_key)

    final_replay = asyncio.run(
        import_publication.replay_import_publications(max_rows=None)
    )
    assert final_replay.examined == 0
    assert (cover_calls, remove_calls) == (1, 3)


@pytest.mark.parametrize(
    ("config_key", "replacement"),
    [
        ("komga_url", "https://other-komga.test:8443"),
        ("komga_library_id", "other-library"),
        ("komga_user", "other-user"),
    ],
)
def test_komga_target_or_credential_owner_rotation_terminal_skips(
    outbox_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    config_key: str,
    replacement: str,
) -> None:
    """Current credentials are never sent to a changed snapshotted target."""
    import httpx
    import import_publication
    import main
    import shared

    initial = {
        "komga_url": "HTTP://Komga.Example.Test:80/base/",
        "komga_library_id": "library-one",
        "komga_user": "owner-one",
        "komga_pass": "old-komga-password",
    }
    for config in (main.CONFIG, shared.CONFIG):
        config.update(initial)

    publication_id = _prepare_deferred_effects(
        outbox_env,
        monkeypatch,
    )
    with sqlite3.connect(outbox_env["db_path"]) as db:
        payload_json = str(
            db.execute(
                "SELECT payload_json FROM import_publication_success_effects"
                " WHERE effect_type='komga_scan'"
            ).fetchone()[0]
        )
    payload = json.loads(payload_json)
    assert payload["url"] == "http://komga.example.test/base"
    assert payload["library_id"] == "library-one"
    assert len(payload["target_fingerprint"]) == 64
    assert "owner-one" not in payload_json
    assert "old-komga-password" not in payload_json

    for config in (main.CONFIG, shared.CONFIG):
        config[config_key] = replacement

    class _NetworkMustNotStart:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            raise AssertionError("Komga network client started after identity drift")

    monkeypatch.setattr(httpx, "AsyncClient", _NetworkMustNotStart)
    assert asyncio.run(
        import_publication._dispatch_success_effect(
            publication_id,
            "komga_scan",
        )
    )

    with sqlite3.connect(outbox_env["db_path"]) as db:
        assert db.execute(
            "SELECT state, next_attempt_at, last_error"
            " FROM import_publication_success_effects"
            " WHERE effect_type='komga_scan'"
        ).fetchone() == ("completed", None, "")
        event = db.execute(
            "SELECT event_type, message FROM events"
            " WHERE event_type='import_success_effect_skipped'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert event is not None
    assert event[0] == "import_success_effect_skipped"
    assert "Komga" in event[1] or "komga" in event[1]
    assert "old-komga-password" not in event[1]


def test_komga_password_only_rotation_uses_current_password(
    outbox_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A password rotation preserves target identity and remains retryable."""
    import httpx
    import import_publication
    import main
    import shared

    initial = {
        "komga_url": "https://komga.example.test/",
        "komga_library_id": "library-one",
        "komga_user": "owner-one",
        "komga_pass": "old-komga-password",
    }
    for config in (main.CONFIG, shared.CONFIG):
        config.update(initial)
    publication_id = _prepare_deferred_effects(
        outbox_env,
        monkeypatch,
    )
    for config in (main.CONFIG, shared.CONFIG):
        config["komga_pass"] = "rotated-komga-password"

    requests: list[tuple[str, object]] = []

    class _Response:
        is_success = True
        status_code = 202

    class _AsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def __aenter__(self) -> "_AsyncClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def post(self, url: str, *, auth: object = None) -> _Response:
            requests.append((url, auth))
            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)
    monkeypatch.setattr(
        httpx,
        "BasicAuth",
        lambda username, password: (username, password),
    )
    assert asyncio.run(
        import_publication._dispatch_success_effect(
            publication_id,
            "komga_scan",
        )
    )
    assert requests == [
        (
            "https://komga.example.test/api/v1/libraries/library-one/scan",
            ("owner-one", "rotated-komga-password"),
        )
    ]


@pytest.mark.parametrize(
    ("protocol", "client_type"),
    [
        ("torrent", "qbittorrent"),
        ("nzb", "sabnzbd"),
    ],
)
def test_remove_completed_uses_reconfigured_bound_client_credentials(
    outbox_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    client_type: str,
) -> None:
    """Host/password changes on the same logical client ID remain valid."""
    import httpx
    import import_publication

    client_id = 915_200
    client_name = f"Bound {client_type}"
    publication_id = _prepare_deferred_effects(
        outbox_env,
        monkeypatch,
        protocol=protocol,
        client_type=client_type,
        client_name=client_name,
        client_id=client_id,
    )
    with sqlite3.connect(outbox_env["db_path"]) as db:
        payload_json = str(
            db.execute(
                "SELECT payload_json FROM import_publication_success_effects"
                " WHERE effect_type='remove_completed'"
            ).fetchone()[0]
        )
        db.execute(
            "UPDATE download_clients"
            " SET host='https://reconfigured-client.test:9443',"
            " username='rotated-user', password='rotated-password'"
            " WHERE id=?",
            (client_id,),
        )
    payload = json.loads(payload_json)
    assert payload == {
        "client_id": client_id,
        "client_name": client_name,
        "client_type": client_type,
        "download_id": "outbox-download",
        "protocol": protocol,
    }
    assert "rotated-password" not in payload_json

    calls: list[tuple[str, str, dict[str, Any]]] = []

    class _Response:
        text = "Ok."
        status_code = 200

    class _AsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def __aenter__(self) -> "_AsyncClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def post(
            self,
            url: str,
            *,
            data: dict[str, Any] | None = None,
        ) -> _Response:
            calls.append(("post", url, data or {}))
            return _Response()

        async def get(
            self,
            url: str,
            *,
            params: dict[str, Any] | None = None,
        ) -> _Response:
            calls.append(("get", url, params or {}))
            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)
    assert asyncio.run(
        import_publication._dispatch_success_effect(
            publication_id,
            "remove_completed",
        )
    )
    if protocol == "torrent":
        assert calls == [
            (
                "post",
                "https://reconfigured-client.test:9443/api/v2/auth/login",
                {"username": "rotated-user", "password": "rotated-password"},
            ),
            (
                "post",
                "https://reconfigured-client.test:9443/api/v2/torrents/delete",
                {"hashes": "outbox-download", "deleteFiles": "false"},
            ),
        ]
    else:
        assert calls == [
            (
                "get",
                "https://reconfigured-client.test:9443/api",
                {
                    "mode": "history",
                    "action": "delete",
                    "del_files": "0",
                    "value": "outbox-download",
                    "apikey": "rotated-password",
                    "output": "json",
                },
            )
        ]


@pytest.mark.parametrize(
    ("protocol", "client_type"),
    [
        ("torrent", "qbittorrent"),
        ("nzb", "sabnzbd"),
    ],
)
@pytest.mark.parametrize("mutation", ["deleted", "disabled", "replaced"])
def test_remove_completed_terminal_skips_client_identity_drift(
    outbox_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    client_type: str,
    mutation: str,
) -> None:
    """Deleted, disabled, or replaced owners never fall back to another server."""
    import httpx
    import import_publication
    import security

    client_id = 915_300
    client_name = f"Original {client_type}"
    publication_id = _prepare_deferred_effects(
        outbox_env,
        monkeypatch,
        protocol=protocol,
        client_type=client_type,
        client_name=client_name,
        client_id=client_id,
    )
    with sqlite3.connect(outbox_env["db_path"]) as db:
        if mutation == "deleted":
            db.execute("DELETE FROM download_clients WHERE id=?", (client_id,))
        elif mutation == "disabled":
            db.execute(
                "UPDATE download_clients SET enabled=0 WHERE id=?",
                (client_id,),
            )
        else:
            db.execute(
                "UPDATE download_clients"
                " SET name='Replacement owner', password='replacement-secret'"
                " WHERE id=?",
                (client_id,),
            )
        db.execute(
            "INSERT INTO download_clients("
            " id,name,type,host,password,enabled,priority"
            ") VALUES(?,?,?,?,?,1,0)",
            (
                client_id + 1,
                "Fallback client",
                client_type,
                "https://fallback-client.test",
                "fallback-secret",
            ),
        )

    monkeypatch.setattr(
        security,
        "decrypt_secret_safe",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("credentials decrypted before identity validation")
        ),
    )

    class _NetworkMustNotStart:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            raise AssertionError("download-client fallback was contacted")

    monkeypatch.setattr(httpx, "AsyncClient", _NetworkMustNotStart)
    assert asyncio.run(
        import_publication._dispatch_success_effect(
            publication_id,
            "remove_completed",
        )
    )
    with sqlite3.connect(outbox_env["db_path"]) as db:
        assert db.execute(
            "SELECT state, attempt_count, next_attempt_at"
            " FROM import_publication_success_effects"
            " WHERE effect_type='remove_completed'"
        ).fetchone() == ("completed", 1, None)
        event = db.execute(
            "SELECT message FROM events"
            " WHERE event_type='import_success_effect_skipped'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert event is not None
    assert mutation in event[0] or (
        mutation == "replaced" and "identity_mismatch" in event[0]
    )


@pytest.mark.parametrize("sibling_status", ["pending", "partial", "failed"])
def test_remove_completed_requires_every_download_sibling_to_be_safe(
    outbox_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    sibling_status: str,
) -> None:
    """Retryable, review, and failed siblings retain the downloader item."""
    import import_execute
    import import_publication

    queue_id, series_id = _seed_import(outbox_env)
    with sqlite3.connect(outbox_env["db_path"]) as db:
        db.execute(
            "INSERT INTO import_queue(series_id,download_id,download_client_id,torrent_name,"
            " torrent_url,src_dir,status) VALUES(?,?,?,'Sibling','magnet:sibling',"
            " ?,?)",
            (
                series_id,
                "outbox-download",
                915_100,
                str(outbox_env["source_root"]),
                sibling_status,
            ),
        )
        sibling_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
        db.execute(
            "INSERT INTO import_queue_files(queue_id,filename,src_path,status)"
            " VALUES(?,?,?,?)",
            (
                sibling_id,
                "Sibling.cbz",
                str(outbox_env["source_root"] / "Sibling.cbz"),
                "needs_review" if sibling_status == "partial" else sibling_status,
            ),
        )

    async def _defer_effects(publication_id: int) -> bool:
        del publication_id
        return False

    monkeypatch.setattr(
        import_publication,
        "_dispatch_success_effects",
        _defer_effects,
    )
    assert asyncio.run(import_execute._execute_import(queue_id)) is True
    with sqlite3.connect(outbox_env["db_path"]) as db:
        assert db.execute(
            "SELECT effect_type FROM import_publication_success_effects"
            " ORDER BY effect_type"
        ).fetchall() == [("cover",), ("komga_scan",)]
        assert db.execute(
            "SELECT status FROM import_queue WHERE id=?",
            (sibling_id,),
        ).fetchone() == (sibling_status,)


@pytest.mark.parametrize("sibling_status", ["imported", "skipped"])
def test_remove_completed_accepts_safe_terminal_download_siblings(
    outbox_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    sibling_status: str,
) -> None:
    queue_id, series_id = _seed_import(outbox_env)
    with sqlite3.connect(outbox_env["db_path"]) as db:
        db.execute(
            "INSERT INTO import_queue(series_id,download_id,download_client_id,torrent_name,"
            " torrent_url,src_dir,status) VALUES(?,?,?,'Sibling','magnet:sibling',"
            " ?,?)",
            (
                series_id,
                "outbox-download",
                915_100,
                str(outbox_env["source_root"]),
                sibling_status,
            ),
        )

    import import_execute
    import import_publication

    async def _defer_effects(publication_id: int) -> bool:
        del publication_id
        return False

    monkeypatch.setattr(
        import_publication,
        "_dispatch_success_effects",
        _defer_effects,
    )
    assert asyncio.run(import_execute._execute_import(queue_id)) is True
    with sqlite3.connect(outbox_env["db_path"]) as db:
        assert db.execute(
            "SELECT effect_type FROM import_publication_success_effects"
            " ORDER BY effect_type"
        ).fetchall() == [
            ("cover",),
            ("komga_scan",),
            ("remove_completed",),
        ]


def test_remove_completed_ignores_unsafe_sibling_owned_by_another_client(
    outbox_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identical client-local IDs on different servers are unrelated work."""
    queue_id, series_id = _seed_import(outbox_env)
    with sqlite3.connect(outbox_env["db_path"]) as db:
        db.execute(
            "INSERT INTO download_clients("
            " id,name,type,host,password,enabled,priority"
            ") VALUES(915101,'Other qBit','qbittorrent',"
            " 'https://other-qbit.test','secret',1,2)"
        )
        db.execute(
            "INSERT INTO import_queue(series_id,download_id,download_client_id,torrent_name,"
            " torrent_url,src_dir,status) VALUES(?,? ,915101,'Other sibling',"
            " 'magnet:other-sibling',?,'pending')",
            (
                series_id,
                "outbox-download",
                str(outbox_env["source_root"]),
            ),
        )
        sibling_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
        db.execute(
            "INSERT INTO import_queue_files(queue_id,filename,src_path,status)"
            " VALUES(?, 'Other sibling.cbz', ?, 'pending')",
            (
                sibling_id,
                str(outbox_env["source_root"] / "Other sibling.cbz"),
            ),
        )

    import import_execute
    import import_publication

    async def _defer_effects(publication_id: int) -> bool:
        del publication_id
        return False

    monkeypatch.setattr(
        import_publication,
        "_dispatch_success_effects",
        _defer_effects,
    )
    assert asyncio.run(import_execute._execute_import(queue_id)) is True
    with sqlite3.connect(outbox_env["db_path"]) as db:
        assert db.execute(
            "SELECT effect_type FROM import_publication_success_effects"
            " ORDER BY effect_type"
        ).fetchall() == [
            ("cover",),
            ("komga_scan",),
            ("remove_completed",),
        ]
        assert db.execute(
            "SELECT status FROM import_queue WHERE id=?",
            (sibling_id,),
        ).fetchone() == ("pending",)


@pytest.mark.parametrize(
    ("protocol", "client_type"),
    [
        ("torrent", "qbittorrent"),
        ("nzb", "sabnzbd"),
    ],
)
def test_remove_completed_uses_grab_owner_not_current_priority_or_tags(
    outbox_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    client_type: str,
) -> None:
    """A later routing change cannot redirect cleanup to the wrong server."""
    import clients
    import import_execute
    import import_publication

    selected_id = 915_100
    wrong_id = 915_101
    queue_id, series_id = _seed_import(
        outbox_env,
        protocol=protocol,
        client_type=client_type,
        client_name="Grab-time owner",
        client_id=selected_id,
    )
    with sqlite3.connect(outbox_env["db_path"]) as db:
        db.execute(
            "UPDATE download_clients SET priority=100 WHERE id=?",
            (selected_id,),
        )
        db.execute(
            "INSERT INTO download_clients("
            " id,name,type,host,password,enabled,priority"
            ") VALUES(?,?,?,?,?,1,0)",
            (
                wrong_id,
                "Wrong current route",
                client_type,
                "https://wrong-current-route.test",
                "wrong-secret",
            ),
        )
        db.execute(
            "INSERT INTO series_tags(series_id,tag) VALUES(?,?)",
            (series_id, "wrong-route"),
        )
        db.execute(
            "INSERT INTO download_client_tags(client_id,tag) VALUES(?,?)",
            (wrong_id, "wrong-route"),
        )

    async def _defer_effects(publication_id: int) -> bool:
        del publication_id
        return False

    monkeypatch.setattr(
        import_publication,
        "_dispatch_success_effects",
        _defer_effects,
    )
    assert asyncio.run(import_execute._execute_import(queue_id)) is True

    with sqlite3.connect(outbox_env["db_path"]) as db:
        publication_id, payload_json = db.execute(
            "SELECT publication_id,payload_json"
            " FROM import_publication_success_effects"
            " WHERE effect_type='remove_completed'"
        ).fetchone()
    payload = json.loads(payload_json)
    assert payload["client_id"] == selected_id

    bound_ids: list[int] = []

    async def _remove(
        download_id: str,
        *args: object,
        client: dict[str, Any] | None = None,
        **kwargs: object,
    ) -> bool:
        del download_id, args, kwargs
        assert client is not None
        bound_ids.append(int(client["id"]))
        return True

    monkeypatch.setattr(clients, "qbit_remove", _remove)
    monkeypatch.setattr(clients, "sab_remove", _remove)
    assert asyncio.run(
        import_publication._dispatch_success_effect(
            int(publication_id),
            "remove_completed",
        )
    )
    assert bound_ids == [selected_id]


def test_legacy_ownerless_rows_skip_remove_completed_without_guessing(
    outbox_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_execute
    import import_publication

    queue_id, _ = _seed_import(outbox_env)
    with sqlite3.connect(outbox_env["db_path"]) as db:
        db.execute("UPDATE seen SET download_client_id=NULL")
        db.execute("UPDATE volumes SET download_client_id=NULL")
        db.execute(
            "UPDATE import_queue SET download_client_id=NULL WHERE id=?",
            (queue_id,),
        )

    async def _defer_effects(publication_id: int) -> bool:
        del publication_id
        return False

    monkeypatch.setattr(
        import_publication,
        "_dispatch_success_effects",
        _defer_effects,
    )
    assert asyncio.run(import_execute._execute_import(queue_id)) is True
    with sqlite3.connect(outbox_env["db_path"]) as db:
        assert db.execute(
            "SELECT queue_download_client_id FROM import_publications"
        ).fetchone() == (None,)
        assert db.execute(
            "SELECT effect_type FROM import_publication_success_effects"
            " ORDER BY effect_type"
        ).fetchall() == [("cover",), ("komga_scan",)]


def _block_legacy_success_path(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str],
) -> None:
    """Instrument symbols present in builds that still have the legacy bypass."""
    import import_execute

    async def _async_call(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        calls.append("external")
        return True

    def _sync_call(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        calls.append("external")
        return True

    for name, replacement in (
        ("trigger_komga_scan", _async_call),
        ("download_cover", _async_call),
        ("qbit_remove", _async_call),
        ("sab_remove", _async_call),
        ("extract_cbz_cover", _sync_call),
    ):
        if hasattr(import_execute, name):
            monkeypatch.setattr(import_execute, name, replacement)


def test_no_ready_needs_review_runs_no_success_side_effects(
    outbox_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_execute

    queue_id, _ = _seed_import(outbox_env)
    with sqlite3.connect(outbox_env["db_path"]) as db:
        db.execute(
            "UPDATE import_queue_files SET proposed_volume=NULL,"
            " status='needs_review' WHERE queue_id=?",
            (queue_id,),
        )
    calls: list[str] = []
    _block_legacy_success_path(monkeypatch, calls)

    assert asyncio.run(import_execute._execute_import(queue_id)) is True
    assert calls == []
    with sqlite3.connect(outbox_env["db_path"]) as db:
        assert db.execute(
            "SELECT status FROM import_queue WHERE id=?",
            (queue_id,),
        ).fetchone() == ("partial",)
        assert db.execute("SELECT COUNT(*) FROM import_publications").fetchone() == (
            0,
        )
        assert db.execute(
            "SELECT COUNT(*) FROM import_publication_success_effects"
        ).fetchone() == (0,)


def test_no_ready_all_skipped_runs_no_success_side_effects_or_removal(
    outbox_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_execute

    queue_id, _ = _seed_import(outbox_env)
    with sqlite3.connect(outbox_env["db_path"]) as db:
        db.execute(
            "UPDATE volumes SET status='downloaded', quality='cbz'"
            " WHERE series_id=915001 AND volume_num=1"
        )
    calls: list[str] = []
    _block_legacy_success_path(monkeypatch, calls)

    assert asyncio.run(import_execute._execute_import(queue_id)) is True
    assert calls == []
    with sqlite3.connect(outbox_env["db_path"]) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM import_queue WHERE id=?",
            (queue_id,),
        ).fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM import_publications").fetchone() == (
            0,
        )
        assert db.execute(
            "SELECT event_type FROM history WHERE download_id='outbox-download'"
        ).fetchall() == [("import_skipped",)]
