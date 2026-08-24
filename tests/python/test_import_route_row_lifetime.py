"""Regression tests for sqlite3.Row lifetime in import routes."""

from __future__ import annotations

import ast
import inspect
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

_TEST_API_KEY = "row-lifetime-api-key"


@dataclass
class _RowLifetime:
    active: bool = True


class _ExpiringRow:
    """Row-shaped test double that rejects every access after context exit."""

    def __init__(
        self,
        lifetime: _RowLifetime,
        columns: tuple[str, ...],
        values: tuple[object, ...],
    ) -> None:
        self._lifetime = lifetime
        self._columns = columns
        self._values = values

    def _assert_active(self) -> None:
        if not self._lifetime.active:
            raise AssertionError("sqlite3.Row escaped its get_db() context")

    def __bool__(self) -> bool:
        self._assert_active()
        return bool(self._values)

    def __getitem__(self, key: str | int) -> object:
        self._assert_active()
        if isinstance(key, int):
            return self._values[key]
        return self._values[self._columns.index(key)]

    def keys(self) -> tuple[str, ...]:
        self._assert_active()
        return self._columns


def _guarded_get_db_factory(
    db_path: str,
) -> Callable[[], AbstractContextManager[sqlite3.Connection]]:
    @contextmanager
    def _guarded_get_db() -> Iterator[sqlite3.Connection]:
        lifetime = _RowLifetime()
        db = sqlite3.connect(db_path)

        def _row_factory(
            cursor: sqlite3.Cursor,
            values: tuple[object, ...],
        ) -> _ExpiringRow:
            columns = tuple(column[0] for column in cursor.description)
            return _ExpiringRow(lifetime, columns, values)

        db.row_factory = _row_factory
        try:
            db.execute("PRAGMA foreign_keys=ON")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            lifetime.active = False
            db.close()

    return _guarded_get_db


@pytest.fixture
def row_lifetime_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict[str, Path]]:
    """Use a real database whose route rows expire at context exit."""
    import main
    import routers.import_ as import_router
    import shared

    old_main_config_object = main.CONFIG
    old_main_config_values = dict(main.CONFIG)
    old_shared_config_object = shared.CONFIG
    old_shared_config_values = dict(shared.CONFIG)
    db_path = tmp_path / "row-lifetime.db"
    library_path = tmp_path / "library"
    library_path.mkdir()

    monkeypatch.setattr(main, "DB_PATH", str(db_path))
    monkeypatch.setattr(shared, "DB_PATH", str(db_path))
    main.init_db()

    with sqlite3.connect(db_path) as db:
        db.execute("DELETE FROM root_folders")
        db.execute(
            "INSERT INTO root_folders(id, path, is_default) VALUES(1, ?, 1)",
            (str(library_path),),
        )
        db.execute(
            "INSERT INTO series(id, title, search_pattern, root_folder_id)"
            " VALUES(1, 'Lifetime Series', 'Lifetime Series', 1)"
        )

    monkeypatch.setattr(
        import_router,
        "get_db",
        _guarded_get_db_factory(str(db_path)),
    )
    monkeypatch.setattr(import_router, "get_cfg", lambda *_args: "copy")
    main.CONFIG["api_key"] = _TEST_API_KEY
    shared.CONFIG["api_key"] = _TEST_API_KEY
    try:
        yield {
            "db_path": db_path,
            "library_path": library_path,
            "scan_path": tmp_path / "scan",
        }
    finally:
        main.CONFIG = old_main_config_object
        main.CONFIG.clear()
        main.CONFIG.update(old_main_config_values)
        shared.CONFIG = old_shared_config_object
        shared.CONFIG.clear()
        shared.CONFIG.update(old_shared_config_values)


def _client() -> TestClient:
    import main

    return TestClient(main.app)


def _csrf(tag: str) -> dict[str, dict[str, str]]:
    token = f"csrf-{tag}-" + "x" * 30
    return {
        "cookies": {"csrftoken": token},
        "headers": {"X-CSRFToken": token},
    }


def _insert_queue(
    db_path: Path,
    *,
    status: str,
    child_status: str | None = None,
) -> tuple[int, int | None]:
    with sqlite3.connect(db_path) as db:
        queue = db.execute(
            "INSERT INTO import_queue("
            "series_id, download_id, torrent_name, src_dir, status"
            ") VALUES(1, 'row-lifetime', 'Lifetime Series v01', '/staging', ?)",
            (status,),
        )
        assert queue.lastrowid is not None
        queue_id = int(queue.lastrowid)
        if child_status is None:
            return queue_id, None
        child = db.execute(
            "INSERT INTO import_queue_files("
            "queue_id, filename, src_path, proposed_volume,"
            " proposed_import_kind, status"
            ") VALUES(?, 'Lifetime Series v01.cbz', '/staging/v01.cbz',"
            " 1, 'volume', ?)",
            (queue_id, child_status),
        )
        assert child.lastrowid is not None
        return queue_id, int(child.lastrowid)


def test_retry_materializes_parent_status_and_review_presence(
    row_lifetime_env: dict[str, Path],
) -> None:
    from routers.import_ import retry_import_queue_entry

    queue_id, _ = _insert_queue(
        row_lifetime_env["db_path"],
        status="partial",
        child_status="needs_review",
    )

    assert retry_import_queue_entry(queue_id) == {
        "ok": False,
        "status": "needs_review",
        "queued": False,
        "retried_files": 0,
    }


def test_retry_reports_closed_scheduler_rejection_as_pending(
    row_lifetime_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main
    from routers.import_ import retry_import_queue_entry

    queue_id, _ = _insert_queue(
        row_lifetime_env["db_path"],
        status="failed",
        child_status="failed",
    )
    monkeypatch.setattr(main, "schedule_import_worker", lambda queue_id: None)

    assert retry_import_queue_entry(queue_id) == {
        "ok": True,
        "status": "pending",
        "queued": False,
        "retried_files": 1,
    }


def test_retry_reports_deduplicated_live_worker_as_queued(
    row_lifetime_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main
    from routers.import_ import retry_import_queue_entry

    queue_id, _ = _insert_queue(
        row_lifetime_env["db_path"],
        status="failed",
        child_status="failed",
    )
    live_worker = object()
    scheduled_ids: list[int] = []

    def _deduplicated_worker(scheduled_queue_id: int) -> object:
        scheduled_ids.append(scheduled_queue_id)
        return live_worker

    monkeypatch.setattr(main, "schedule_import_worker", _deduplicated_worker)

    assert retry_import_queue_entry(queue_id) == {
        "ok": True,
        "status": "queued",
        "queued": True,
        "retried_files": 1,
    }
    assert scheduled_ids == [queue_id]


@pytest.mark.parametrize("htmx", [False, True])
def test_process_route_materializes_result_status_for_response(
    row_lifetime_env: dict[str, Path],
    htmx: bool,
) -> None:
    queue_id, _ = _insert_queue(
        row_lifetime_env["db_path"],
        status="pending",
    )
    csrf = _csrf(f"process-{htmx}")
    if htmx:
        csrf["headers"]["HX-Request"] = "true"

    client = _client()
    client.cookies.update(csrf["cookies"])
    response = client.post(
        f"/import/{queue_id}/process",
        data={},
        headers=csrf["headers"],
        follow_redirects=False,
    )

    if htmx:
        assert response.status_code == 200
        trigger = json.loads(response.headers["HX-Trigger"])
        assert trigger["showToast"] == {
            "msg": "Import queued",
            "type": "info",
        }
        assert response.headers["HX-Refresh"] == "true"
    else:
        assert response.status_code == 303
        assert "Import+queued" in response.headers["location"]


def test_manual_import_page_and_scan_materialize_series_rows(
    row_lifetime_env: dict[str, Path],
) -> None:
    scan_path = row_lifetime_env["scan_path"]
    scan_path.mkdir()
    source = scan_path / "Lifetime Series v01.cbz"
    source.write_bytes(b"test archive")

    page = _client().get("/manual-import")
    scan = _client().post(
        "/api/manual-import/scan",
        json={"path": str(scan_path)},
        headers={"X-Api-Key": _TEST_API_KEY},
    )

    assert page.status_code == 200
    assert "Lifetime Series" in page.text
    assert scan.status_code == 200
    assert scan.json()["files"][0]["matched_series"] == {
        "id": 1,
        "title": "Lifetime Series",
    }


def test_auto_import_materializes_existing_series_rows(
    row_lifetime_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main

    scan_path = row_lifetime_env["scan_path"]
    scan_path.mkdir()
    source = scan_path / "Lifetime Series v01.cbz"
    source.write_bytes(b"test archive")

    async def _noop_scan() -> None:
        return None

    monkeypatch.setattr(main, "trigger_komga_scan", _noop_scan)
    response = _client().post(
        "/api/manual-import/auto-import",
        json={"path": str(scan_path), "remove_source": False},
        headers={"X-Api-Key": _TEST_API_KEY},
    )

    assert response.status_code == 200
    assert response.json()["imported"] == 1
    assert source.exists()


def test_auto_import_materializes_new_series_row(
    row_lifetime_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main

    scan_path = row_lifetime_env["scan_path"]
    scan_path.mkdir()
    source = scan_path / "Brand New Series v01.cbz"
    source.write_bytes(b"test archive")

    async def _search_series(_title: str) -> tuple[list[dict[str, Any]], str]:
        return (
            [
                {
                    "title": "Brand New Series",
                    "anilist_id": 987654,
                    "mal_id": None,
                    "cover_url": "",
                    "status": "FINISHED",
                    "description": "",
                    "volumes": None,
                    "chapters": None,
                }
            ],
            "anilist",
        )

    async def _noop_scan() -> None:
        return None

    def _close_background_task(coro: Any, *, name: str) -> None:
        del name
        coro.close()

    monkeypatch.setattr(main, "search_series", _search_series)
    monkeypatch.setattr(main, "trigger_komga_scan", _noop_scan)
    monkeypatch.setattr(main, "create_background_task", _close_background_task)
    response = _client().post(
        "/api/manual-import/auto-import",
        json={"path": str(scan_path), "remove_source": False},
        headers={"X-Api-Key": _TEST_API_KEY},
    )

    assert response.status_code == 200
    assert response.json()["imported"] == 1
    assert response.json()["new_series"] == [
        {"id": response.json()["new_series"][0]["id"], "title": "Brand New Series"}
    ]
    series_id = response.json()["new_series"][0]["id"]
    with sqlite3.connect(row_lifetime_env["db_path"]) as db:
        db.row_factory = sqlite3.Row
        title_selection = db.execute(
            "SELECT value_json,selected_source,locked FROM series_metadata_fields"
            " WHERE series_id=? AND field_name='title'",
            (series_id,),
        ).fetchone()
        title_candidate = db.execute(
            "SELECT value_json FROM series_metadata_candidates"
            " WHERE series_id=? AND field_name='title' AND source='anilist'",
            (series_id,),
        ).fetchone()
    assert dict(title_selection) == {
        "value_json": '"Brand New Series"',
        "selected_source": "anilist",
        "locked": 0,
    }
    assert title_candidate["value_json"] == '"Brand New Series"'


def _is_get_db_with(node: ast.With) -> bool:
    return any(
        isinstance(item.context_expr, ast.Call)
        and isinstance(item.context_expr.func, ast.Name)
        and item.context_expr.func.id == "get_db"
        for item in node.items
    )


def _is_direct_row_fetch(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Attribute) and node.func.attr in {
        "fetchone",
        "fetchall",
    }:
        return True
    return (
        isinstance(node.func, ast.Name)
        and node.func.id == "list"
        and any(_is_direct_row_fetch(argument) for argument in node.args)
    )


def _raw_row_assignments(node: ast.With) -> set[str]:
    assignments: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, (ast.Assign, ast.AnnAssign)):
            continue
        value = child.value
        if value is None or not _is_direct_row_fetch(value):
            continue
        targets = child.targets if isinstance(child, ast.Assign) else [child.target]
        assignments.update(
            target.id for target in targets if isinstance(target, ast.Name)
        )
    return assignments


def _loads_before_reassignment(
    statements: list[ast.stmt],
    name: str,
) -> bool:
    loads = [
        node.lineno
        for statement in statements
        for node in ast.walk(statement)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id == name
    ]
    stores = [
        node.lineno
        for statement in statements
        for node in ast.walk(statement)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Store)
        and node.id == name
    ]
    return bool(loads) and (not stores or min(loads) < min(stores))


def _escaped_row_names(statements: list[ast.stmt]) -> list[tuple[int, str]]:
    escaped: list[tuple[int, str]] = []
    for index, statement in enumerate(statements):
        if isinstance(statement, ast.With) and _is_get_db_with(statement):
            for name in _raw_row_assignments(statement):
                if _loads_before_reassignment(statements[index + 1 :], name):
                    escaped.append((statement.lineno, name))
        for _field, value in ast.iter_fields(statement):
            if (
                isinstance(value, list)
                and value
                and all(isinstance(item, ast.stmt) for item in value)
            ):
                escaped.extend(_escaped_row_names(value))
    return escaped


def test_import_router_has_no_direct_row_escape_from_get_db() -> None:
    import routers.import_ as import_router

    source_file = inspect.getsourcefile(import_router)
    assert source_file is not None
    tree = ast.parse(Path(source_file).read_text())

    assert _escaped_row_names(tree.body) == []
