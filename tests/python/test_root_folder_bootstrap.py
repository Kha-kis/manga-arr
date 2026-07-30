"""Root-folder bootstrap and health regression coverage.

The schema pass can run before configuration exists on a fresh install, so the
real lifespan must repeat the locked bootstrap after loading environment and DB
settings. The migration remains idempotent and health must classify a missing
root-folder configuration as a critical readiness issue.
"""
import asyncio
import logging
import os
import sqlite3
import sys
from collections.abc import Coroutine, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


class _BlockedAsyncClient:
    """Fail closed if a health or lifespan path attempts external HTTP."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    async def __aenter__(self) -> "_BlockedAsyncClient":
        return self

    async def __aexit__(self, *args: object) -> bool:
        del args
        return False

    async def get(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("external HTTP GET attempted by hermetic test")

    async def post(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("external HTTP POST attempted by hermetic test")


@pytest.fixture
def isolated_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[str]:
    import config
    import import_execute
    import main
    import security
    import shared

    db_path = str(tmp_path / "root-folder.db")
    key_dir = str(tmp_path / "keys")
    original_main_config = main.CONFIG
    original_main_values = dict(main.CONFIG)
    original_shared_config = shared.CONFIG
    original_shared_values = dict(shared.CONFIG)
    original_cipher = security._SECRET_CIPHER
    original_import_semaphore = import_execute._IMPORT_SEM
    original_log_level = logging.getLogger().level

    try:
        monkeypatch.setattr(main, "DB_PATH", db_path)
        monkeypatch.setattr(shared, "DB_PATH", db_path)
        configured_env_names = {
            env_name
            for env_name, _default in config.ENV_DEFAULTS.values()
            if env_name is not None
        }
        configured_env_names.update(
            alias
            for aliases in config.ENV_ALIASES.values()
            for alias in aliases
        )
        for env_name in configured_env_names:
            monkeypatch.delenv(env_name, raising=False)
        monkeypatch.delenv("MANGARR_SECRET_KEY", raising=False)
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            _BlockedAsyncClient,
        )
        main.CONFIG.clear()
        shared.CONFIG.clear()
        security._SECRET_CIPHER = None
        security.load_or_create_secret_cipher(key_dir)
        yield db_path
    finally:
        original_main_config.clear()
        original_main_config.update(original_main_values)
        main.CONFIG = original_main_config
        original_shared_config.clear()
        original_shared_config.update(original_shared_values)
        shared.CONFIG = original_shared_config
        security._SECRET_CIPHER = original_cipher
        import_execute._IMPORT_SEM = original_import_semaphore
        logging.getLogger().setLevel(original_log_level)


@pytest.fixture
def env(isolated_state: str) -> str:
    import main

    main.init_db()
    main.load_config()
    return isolated_state


def _folders(db_path: str) -> list[dict[str, object]]:
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(
            "SELECT id, path, label, is_default FROM root_folders"
        ).fetchall()]


def _disable_lifespan_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    import auth
    import main

    def _discard(coro: Coroutine[object, object, object], *, name: str) -> None:
        del name
        coro.close()

    async def _cancel_none() -> None:
        return None

    def _skip_legacy_setup_token_removal() -> None:
        """Prevent lifespan tests from inspecting any host config directory."""

    monkeypatch.setattr(main, "create_background_task", _discard)
    monkeypatch.setattr(main, "_cancel_background_tasks", _cancel_none)
    monkeypatch.setattr(
        auth,
        "remove_legacy_setup_token",
        _skip_legacy_setup_token_removal,
    )


def _run_lifespan_once() -> None:
    import main

    async def _run() -> None:
        async with main.lifespan(main.app):
            pass

    asyncio.run(_run())


def _root_folder_health_check() -> dict[str, object]:
    from routers.health_ import build_health_payload

    payload = asyncio.run(build_health_payload())
    return next(check for check in payload["checks"] if check["name"] == "Root Folders")


def test_lifespan_bootstraps_public_compose_library_path_once(
    isolated_state: str,
    monkeypatch: pytest.MonkeyPatch,
):
    """The real startup sequence must see env config after fresh schema init."""
    import main

    monkeypatch.setenv("MANGARR_LIBRARY_PATH", "/data/media/manga")
    _disable_lifespan_tasks(monkeypatch)

    _run_lifespan_once()
    _run_lifespan_once()
    main._bootstrap_root_folders()

    assert _folders(isolated_state) == [
        {
            "id": 1,
            "path": "/data/media/manga",
            "label": "Manga",
            "is_default": 1,
        }
    ]
    with sqlite3.connect(isolated_state) as db:
        event_count = db.execute(
            "SELECT COUNT(*) FROM events"
            " WHERE event_type='schema_migration'"
            " AND message LIKE 'bootstrapped root folder%'"
        ).fetchone()[0]
    assert event_count == 1


def test_lifespan_uses_db_save_path_after_config_load(
    isolated_state: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """A persisted legacy setting keeps precedence over the environment."""
    import main

    configured_path = str(tmp_path / "persisted-library")
    main.init_db()
    with sqlite3.connect(isolated_state) as db:
        db.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES('save_path', ?)",
            (configured_path,),
        )
    monkeypatch.setenv(
        "MANGARR_LIBRARY_PATH",
        str(tmp_path / "environment-library"),
    )
    _disable_lifespan_tasks(monkeypatch)

    _run_lifespan_once()

    folders = _folders(isolated_state)
    assert len(folders) == 1
    assert folders[0]["path"] == configured_path
    assert folders[0]["is_default"] == 1


def test_lifespan_preserves_existing_roots_and_default(
    isolated_state: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    import main

    first_path = str(tmp_path / "library-a")
    default_path = str(tmp_path / "library-b")
    main.init_db()
    with sqlite3.connect(isolated_state) as db:
        db.execute(
            "INSERT INTO root_folders(id, path, label, is_default)"
            " VALUES(7, ?, 'Library A', 0)",
            (first_path,),
        )
        db.execute(
            "INSERT INTO root_folders(id, path, label, is_default)"
            " VALUES(9, ?, 'Library B', 1)",
            (default_path,),
        )
    monkeypatch.setenv(
        "MANGARR_LIBRARY_PATH",
        str(tmp_path / "environment-library"),
    )
    _disable_lifespan_tasks(monkeypatch)

    _run_lifespan_once()

    assert _folders(isolated_state) == [
        {"id": 7, "path": first_path, "label": "Library A", "is_default": 0},
        {"id": 9, "path": default_path, "label": "Library B", "is_default": 1},
    ]


def test_no_root_folders_are_critical_in_api_and_ui(env: str):
    import main

    main.ensure_api_key()
    client = TestClient(main.app, follow_redirects=False)
    try:
        api_response = client.get(
            "/api/v1/health",
            headers={"X-Api-Key": main.CONFIG["api_key"]},
        )
        ui_response = client.get("/health")
    finally:
        client.close()

    assert api_response.status_code == 200
    api_payload = api_response.json()
    root_check = next(
        check for check in api_payload["checks"] if check["name"] == "Root Folders"
    )
    assert root_check == {
        "name": "Root Folders",
        "ok": False,
        "message": "No root folders configured — add one in Settings",
        "severity": "critical",
        "fix_url": "/settings",
    }
    assert root_check in api_payload["issues"]
    assert api_payload["ok"] is False

    assert ui_response.status_code == 200
    body = ui_response.text
    root_name_at = body.index('<span class="health-name">Root Folders</span>')
    root_row = body[body.rfind("health-row", 0, root_name_at):root_name_at + 800]
    assert "is-critical" in root_row
    assert "No root folders configured — add one in Settings" in root_row
    assert 'href="/settings"' in root_row
    assert 'aria-label="Go to fix page for Root Folders"' in root_row


def test_missing_root_folder_remains_unhealthy(env: str, tmp_path: Path):
    missing_path = str(tmp_path / "missing-library")
    with sqlite3.connect(env) as db:
        db.execute(
            "INSERT INTO root_folders(path, label, is_default)"
            " VALUES(?, 'Missing', 1)",
            (missing_path,),
        )

    root_check = _root_folder_health_check()

    assert root_check["ok"] is False
    assert root_check["severity"] == "critical"
    assert root_check["message"] == f"Root folder missing: {missing_path}"
    assert root_check["fix_url"] == "/settings"


def test_unwritable_root_folder_remains_unhealthy(
    env: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    import routers.health_ as health_router

    unwritable_path = tmp_path / "unwritable-library"
    unwritable_path.mkdir()
    original_access = health_router.os.access

    def _target_access(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        mode: int,
        *,
        dir_fd: int | None = None,
        effective_ids: bool = False,
        follow_symlinks: bool = True,
    ) -> bool:
        if os.fspath(path) == str(unwritable_path) and mode & os.W_OK:
            return False
        return original_access(
            path,
            mode,
            dir_fd=dir_fd,
            effective_ids=effective_ids,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(health_router.os, "access", _target_access)
    assert health_router.os.access(tmp_path, os.W_OK) == original_access(
        tmp_path,
        os.W_OK,
    )
    with sqlite3.connect(env) as db:
        db.execute(
            "INSERT INTO root_folders(path, label, is_default)"
            " VALUES(?, 'Unwritable', 1)",
            (str(unwritable_path),),
        )

    root_check = _root_folder_health_check()

    assert root_check["ok"] is False
    assert root_check["severity"] == "critical"
    assert root_check["message"] == f"Root folder not writable: {unwritable_path}"
    assert root_check["fix_url"] == "/settings"


def test_bootstrap_creates_folder_from_save_path_when_none_exist(env):
    import main
    # Fresh env may already have folders from init_db bootstrap — reset
    # to the pre-bootstrap state: zero folders, save_path set.
    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
        c.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES('save_path', '/legacy/manga')"
        )
    main.load_config()
    assert _folders(env) == []

    main._bootstrap_root_folders()

    folders = _folders(env)
    assert len(folders) == 1
    assert folders[0]['path'] == '/legacy/manga'
    assert folders[0]['label'] == 'Manga'
    assert folders[0]['is_default'] == 1


def test_bootstrap_skips_folder_creation_when_any_folder_exists(env):
    """If any root folder is already present, don't create one from save_path
    — the operator has clearly configured the app manually."""
    import main
    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
        c.execute(
            "INSERT INTO root_folders(path, label, is_default)"
            " VALUES('/data/media/manga', 'Manga', 1)"
        )
        c.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES('save_path', '/should/not/be/created')"
        )
    main.load_config()

    main._bootstrap_root_folders()

    folders = _folders(env)
    assert len(folders) == 1
    assert folders[0]['path'] == '/data/media/manga'


def test_bootstrap_skips_when_save_path_empty(env):
    """Nothing to bootstrap from. Don't silently create a weird folder."""
    import main
    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
        c.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES('save_path', '')"
        )
    main.load_config()

    main._bootstrap_root_folders()

    assert _folders(env) == []


def test_bootstrap_assigns_orphan_series_to_default_folder(env):
    import main
    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
        c.execute(
            "INSERT INTO root_folders(id, path, label, is_default)"
            " VALUES(5, '/data/media/manga', 'Manga', 1)"
        )
        c.execute(
            "INSERT INTO series(id, title, search_pattern, root_folder_id)"
            " VALUES(10, 'S', 'S', NULL)"
        )
        c.execute(
            "INSERT INTO series(id, title, search_pattern, root_folder_id)"
            " VALUES(11, 'T', 'T', NULL)"
        )

    main._bootstrap_root_folders()

    with sqlite3.connect(env) as c:
        rows = c.execute(
            "SELECT id, root_folder_id FROM series WHERE id IN (10, 11)"
        ).fetchall()
    assert all(r[1] == 5 for r in rows)


def test_bootstrap_assigns_to_lowest_id_when_no_default_flagged(env):
    """Safety: if no folder has is_default=1 (operator flubbed config),
    fall back to the lowest-id folder so series still get a home."""
    import main
    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
        c.execute(
            "INSERT INTO root_folders(id, path, label, is_default)"
            " VALUES(3, '/data/media/a', 'A', 0)"
        )
        c.execute(
            "INSERT INTO root_folders(id, path, label, is_default)"
            " VALUES(4, '/data/media/b', 'B', 0)"
        )
        c.execute(
            "INSERT INTO series(id, title, search_pattern, root_folder_id)"
            " VALUES(10, 'S', 'S', NULL)"
        )
    main._bootstrap_root_folders()

    with sqlite3.connect(env) as c:
        rf = c.execute(
            "SELECT root_folder_id FROM series WHERE id=10"
        ).fetchone()[0]
    assert rf == 3  # lowest id wins when no default is flagged


def test_bootstrap_is_no_op_when_every_series_has_folder(env):
    import main
    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
        c.execute(
            "INSERT INTO root_folders(id, path, label, is_default)"
            " VALUES(9, '/data/media/manga', 'Manga', 1)"
        )
        c.execute(
            "INSERT INTO series(id, title, search_pattern, root_folder_id)"
            " VALUES(10, 'S', 'S', 9)"
        )
    # Count pre-migration state
    with sqlite3.connect(env) as c:
        before_events = c.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='schema_migration'"
        ).fetchone()[0]

    main._bootstrap_root_folders()

    # No new schema_migration events written — no-op
    with sqlite3.connect(env) as c:
        after_events = c.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='schema_migration'"
        ).fetchone()[0]
        # The orphan-assignment UPDATE fires but touches zero rows, so
        # its event is not written (guarded by `if assigned > 0`).
        # Similarly the folder-creation block is skipped when a folder
        # already exists. Net: no new events.
    assert after_events == before_events


def test_bootstrap_logs_events_for_each_action(env):
    import main
    with sqlite3.connect(env) as c:
        c.execute("DELETE FROM root_folders")
        c.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES('save_path', '/legacy/m')"
        )
        c.execute(
            "INSERT INTO series(id, title, search_pattern, root_folder_id)"
            " VALUES(10, 'S', 'S', NULL)"
        )
    main.load_config()

    main._bootstrap_root_folders()

    with sqlite3.connect(env) as c:
        events = [r[0] for r in c.execute(
            "SELECT message FROM events WHERE event_type='schema_migration'"
            " ORDER BY id DESC LIMIT 10"
        ).fetchall()]
    assert any('bootstrapped root folder' in e and '/legacy/m' in e for e in events)
    assert any('assigned 1 orphan series' in e for e in events)
