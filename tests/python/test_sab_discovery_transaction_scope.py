"""Regression coverage for discovery DB transaction and Row lifetimes."""

from __future__ import annotations

import ast
import asyncio
import inspect
import sqlite3
import threading
import zipfile
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest


class _ExpiringRow(Mapping[str, Any]):
    """Act like sqlite3.Row, but reject access after its DB context exits."""

    def __init__(self, row: sqlite3.Row, escaped_accesses: list[str]) -> None:
        self._row = row
        self._escaped_accesses = escaped_accesses
        self._alive = True

    def expire(self) -> None:
        self._alive = False

    def __getitem__(self, key: str) -> Any:
        if not self._alive:
            self._escaped_accesses.append(key)
            raise AssertionError("sqlite row escaped its get_db context")
        return self._row[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._row.keys())

    def __len__(self) -> int:
        return len(self._row.keys())


class _TrackingCursor:
    def __init__(
        self,
        cursor: sqlite3.Cursor,
        rows: list[_ExpiringRow],
        escaped_accesses: list[str],
        query_name: str,
        tracked_queries: list[str],
    ) -> None:
        self._cursor = cursor
        self._rows = rows
        self._escaped_accesses = escaped_accesses
        self._query_name = query_name
        self._tracked_queries = tracked_queries

    def _track(self, row: sqlite3.Row) -> _ExpiringRow:
        tracked = _ExpiringRow(row, self._escaped_accesses)
        self._rows.append(tracked)
        return tracked

    def fetchall(self) -> list[_ExpiringRow]:
        self._tracked_queries.append(self._query_name)
        return [self._track(row) for row in self._cursor.fetchall()]

    def fetchone(self) -> _ExpiringRow | None:
        self._tracked_queries.append(self._query_name)
        row = self._cursor.fetchone()
        return self._track(row) if row is not None else None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class _TrackingConnection:
    def __init__(
        self,
        connection: sqlite3.Connection,
        escaped_accesses: list[str],
        tracked_queries: list[str],
    ) -> None:
        self._connection = connection
        self._escaped_accesses = escaped_accesses
        self._tracked_queries = tracked_queries
        self._rows: list[_ExpiringRow] = []

    def execute(
        self,
        sql: str,
        parameters: tuple[Any, ...] = (),
    ) -> sqlite3.Cursor | _TrackingCursor:
        cursor = self._connection.execute(sql, parameters)
        if "FROM seen WHERE client='sabnzbd'" in sql:
            query_name = "sab_seen"
        elif "FROM seen WHERE client='qbittorrent'" in sql:
            query_name = (
                "qbit_completed_seen" if "volume_num" in sql else "qbit_failed_seen"
            )
        elif "SELECT title, search_pattern FROM series" in sql:
            query_name = "qbit_retry_series"
        else:
            return cursor
        return _TrackingCursor(
            cursor,
            self._rows,
            self._escaped_accesses,
            query_name,
            self._tracked_queries,
        )

    def expire_rows(self) -> None:
        for row in self._rows:
            row.expire()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


@pytest.fixture
def sab_scope_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[str, Path, Path]]:
    import main
    import security
    import shared

    db_path = str(tmp_path / "sab-scope.db")
    old_main_config = dict(main.CONFIG)
    old_shared_config = dict(shared.CONFIG)
    old_cipher = security._SECRET_CIPHER

    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(shared, "DB_PATH", db_path)
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(str(tmp_path / "keys"))
    main.init_db()
    shared.ensure_wal_journal_mode()

    library = tmp_path / "library"
    first_download = tmp_path / "downloads" / "first"
    second_download = tmp_path / "downloads" / "second"
    library.mkdir()
    first_download.mkdir(parents=True)
    second_download.mkdir(parents=True)
    with zipfile.ZipFile(first_download / "Scope Series v01.cbz", "w") as archive:
        archive.writestr("001.jpg", b"first")
    with zipfile.ZipFile(second_download / "Scope Series v02.cbz", "w") as archive:
        archive.writestr("001.jpg", b"second")

    with sqlite3.connect(db_path) as db:
        db.execute("DELETE FROM root_folders")
        db.execute(
            "INSERT INTO root_folders(id, path, is_default) VALUES(1, ?, 1)",
            (str(library),),
        )
        db.execute(
            "INSERT INTO series(id, title, search_pattern, root_folder_id)"
            " VALUES(1, 'Scope Series', 'Scope Series', 1)"
        )
        db.execute(
            "INSERT INTO remote_path_mappings(host, remote_path, local_path)"
            " VALUES('http://sab.invalid', '/remote', ?)",
            (str(tmp_path / "downloads"),),
        )
        db.executemany(
            "INSERT INTO seen("
            "torrent_url, torrent_name, series_id, volume_num, client, protocol,"
            " download_id"
            ") VALUES(?, ?, 1, ?, 'sabnzbd', 'nzb', ?)",
            (
                ("https://example.invalid/first", "Scope Series v01", 1, "sab-1"),
                ("https://example.invalid/second", "Scope Series v02", 2, "sab-2"),
            ),
        )
        db.execute("CREATE TABLE writer_probe(value TEXT NOT NULL)")

    try:
        yield db_path, first_download, second_download
    finally:
        security._SECRET_CIPHER = old_cipher
        main.CONFIG.clear()
        main.CONFIG.update(old_main_config)
        shared.CONFIG.clear()
        shared.CONFIG.update(old_shared_config)


def test_sab_items_do_not_inherit_prior_queue_writer_transaction(
    sab_scope_env: tuple[str, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_discovery
    import import_queue
    import shared

    db_path, first_download, second_download = sab_scope_env
    escaped_accesses: list[str] = []
    tracked_rows: list[_ExpiringRow] = []
    tracked_queries: list[str] = []
    real_get_db = shared.get_db

    @contextmanager
    def tracked_get_db() -> Iterator[_TrackingConnection]:
        with real_get_db() as connection:
            tracked = _TrackingConnection(
                connection,
                escaped_accesses,
                tracked_queries,
            )
            try:
                yield tracked
            finally:
                tracked_rows.extend(tracked._rows)
                tracked.expire_rows()

    second_scan_started = threading.Event()
    release_second_scan = threading.Event()
    real_walk = import_queue.os.walk

    def blocking_walk(
        top: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if Path(top) == second_download and not second_scan_started.is_set():
            second_scan_started.set()
            assert release_second_scan.wait(timeout=5)
        return real_walk(top, *args, **kwargs)

    monkeypatch.setattr(import_discovery, "get_db", tracked_get_db)
    monkeypatch.setattr(import_queue.os, "walk", blocking_walk)

    sab_by_nzo = {
        "sab-1": {"storage": "/remote/first", "status": "Completed"},
        "sab-2": {"storage": "/remote/second", "status": "Completed"},
    }
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            discovery = pool.submit(
                import_discovery._sab_process_sync,
                sab_by_nzo,
                set(sab_by_nzo),
                "http://sab.invalid",
            )
            assert second_scan_started.wait(timeout=3)

            with sqlite3.connect(db_path, timeout=0.1) as writer:
                writer.execute("PRAGMA busy_timeout=100")
                writer.execute("INSERT INTO writer_probe(value) VALUES('succeeded')")
                writer.commit()

            release_second_scan.set()
            queue_ids = discovery.result(timeout=5)
    finally:
        release_second_scan.set()

    with sqlite3.connect(db_path) as db:
        queue_rows = db.execute(
            "SELECT id, download_id, src_dir, status FROM import_queue ORDER BY id"
        ).fetchall()
        file_rows = db.execute(
            "SELECT queue_id, status FROM import_queue_files ORDER BY queue_id"
        ).fetchall()
        writer_rows = db.execute("SELECT value FROM writer_probe").fetchall()

    assert queue_ids == [1, 2]
    assert queue_rows == [
        (1, "sab-1", str(first_download), "pending"),
        (2, "sab-2", str(second_download), "pending"),
    ]
    assert file_rows == [(1, "pending"), (2, "pending")]
    assert writer_rows == [("succeeded",)]
    assert tracked_rows
    assert all(not row._alive for row in tracked_rows)
    assert tracked_queries == ["sab_seen"]
    assert escaped_accesses == []


class _Response:
    def __init__(
        self,
        *,
        text: str = "",
        data: object = None,
        status_code: int = 200,
    ) -> None:
        self.text = text
        self._data = data
        self.status_code = status_code

    def json(self) -> object:
        return self._data


def test_qbit_rows_are_snapshotted_before_threaded_discovery_work(
    sab_scope_env: tuple[str, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import grab
    import import_discovery
    import shared
    from routers import suwayomi_ as suwayomi_router

    db_path, completed_download, _ = sab_scope_env
    with sqlite3.connect(db_path) as db:
        db.execute("DELETE FROM seen")
        db.execute(
            "INSERT INTO download_clients("
            "name, type, host, port, username, password, category, priority, enabled"
            ") VALUES("
            "'qbit', 'qbittorrent', 'http://qbit.invalid', 8080,"
            " 'user', 'password', 'manga', 1, 1"
            ")"
        )
        db.executemany(
            "INSERT INTO seen("
            "torrent_url, torrent_name, series_id, volume_num, client, protocol,"
            " download_id"
            ") VALUES(?, ?, 1, ?, 'qbittorrent', 'torrent', ?)",
            (
                (
                    "https://example.invalid/completed",
                    "Scope Series v01",
                    1,
                    "qbit-completed",
                ),
                (
                    "https://example.invalid/failed",
                    "Scope Series v02",
                    2,
                    "qbit-failed",
                ),
            ),
        )

    torrents = [
        {
            "hash": "QBIT-COMPLETED",
            "name": "Scope Series v01",
            "progress": 1.0,
            "content_path": str(completed_download),
            "state": "uploading",
        },
        {
            "hash": "QBIT-FAILED",
            "name": "Scope Series v02",
            "progress": 0.5,
            "save_path": str(completed_download),
            "state": "error",
        },
    ]

    class _QbitClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def __aenter__(self) -> _QbitClient:
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
            del args, kwargs
            assert url.endswith("/api/v2/auth/login")
            return _Response(text="Ok.")

        async def get(
            self,
            url: str,
            *args: object,
            **kwargs: object,
        ) -> _Response:
            del args, kwargs
            assert url.endswith("/api/v2/torrents/info")
            return _Response(data=torrents)

    escaped_accesses: list[str] = []
    tracked_rows: list[_ExpiringRow] = []
    tracked_queries: list[str] = []
    real_get_db = shared.get_db

    @contextmanager
    def tracked_get_db() -> Iterator[_TrackingConnection]:
        with real_get_db() as connection:
            tracked = _TrackingConnection(
                connection,
                escaped_accesses,
                tracked_queries,
            )
            try:
                yield tracked
            finally:
                tracked_rows.extend(tracked._rows)
                tracked.expire_rows()

    def fake_get_cfg(key: str, default: str = "") -> str:
        return {
            "blocklist_ttl_days": "0",
            "failed_download_handling": "1",
            "redownload_failed_interactive": "0",
        }.get(key, default)

    retry_calls: list[tuple[int, str, str]] = []
    auto_import_ids: list[int] = []

    async def fake_grab_existing(series_id: int, title: str, pattern: str) -> int:
        retry_calls.append((series_id, title, pattern))
        return 0

    def fake_schedule_import(queue_id: int) -> None:
        auto_import_ids.append(queue_id)

    async def no_suwayomi() -> None:
        return None

    monkeypatch.setattr(import_discovery, "get_db", tracked_get_db)
    monkeypatch.setattr(import_discovery, "get_cfg", fake_get_cfg)
    monkeypatch.setattr(import_discovery.httpx, "AsyncClient", _QbitClient)
    monkeypatch.setattr(
        import_discovery,
        "schedule_import_worker",
        fake_schedule_import,
    )
    monkeypatch.setattr(grab, "grab_existing", fake_grab_existing)
    monkeypatch.setattr(suwayomi_router, "check_suwayomi_jobs", no_suwayomi)

    async def drive_discovery() -> None:
        await import_discovery._check_download_status_impl()
        await asyncio.sleep(0)

    asyncio.run(drive_discovery())

    with sqlite3.connect(db_path) as db:
        queue_rows = db.execute(
            "SELECT download_id, status FROM import_queue ORDER BY id"
        ).fetchall()
        blocklist_rows = db.execute(
            "SELECT torrent_url, torrent_name, reason FROM blocklist"
        ).fetchall()
        seen_ids = db.execute(
            "SELECT download_id FROM seen ORDER BY download_id"
        ).fetchall()

    assert queue_rows == [("qbit-completed", "pending")]
    assert blocklist_rows == [
        (
            "https://example.invalid/failed",
            "Scope Series v02",
            "Download failed: error",
        )
    ]
    assert seen_ids == [("qbit-completed",)]
    assert auto_import_ids == [1]
    assert retry_calls == [(1, "Scope Series", "Scope Series")]
    assert tracked_rows
    assert all(not row._alive for row in tracked_rows)
    assert tracked_queries == [
        "qbit_completed_seen",
        "qbit_failed_seen",
        "qbit_retry_series",
    ]
    assert escaped_accesses == []


def test_get_db_fetch_results_are_snapshotted_before_scope_exit() -> None:
    """Reject direct sqlite fetch results used after their get_db context."""
    import import_discovery

    tree = ast.parse(inspect.getsource(import_discovery))
    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node

    def enclosing_scope(node: ast.AST) -> ast.AST:
        current = parent[node]
        while not isinstance(
            current,
            (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            current = parent[current]
        return current

    def contains_fetch(node: ast.AST) -> bool:
        return any(
            isinstance(candidate, ast.Call)
            and isinstance(candidate.func, ast.Attribute)
            and candidate.func.attr in {"fetchall", "fetchone"}
            for candidate in ast.walk(node)
        )

    def is_plain_snapshot(node: ast.AST) -> bool:
        if not isinstance(node, ast.ListComp):
            return False
        return (
            isinstance(node.elt, ast.Call)
            and isinstance(node.elt.func, ast.Name)
            and node.elt.func.id == "dict"
        )

    violations: list[tuple[int, str]] = []
    for context in (
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.With)
        and any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Name)
            and item.context_expr.func.id == "get_db"
            for item in node.items
        )
    ):
        scope = enclosing_scope(context)
        for assignment in (
            node
            for node in ast.walk(context)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
        ):
            value = assignment.value
            if value is None or not contains_fetch(value):
                continue
            targets = (
                assignment.targets
                if isinstance(assignment, ast.Assign)
                else [assignment.target]
            )
            names = {target.id for target in targets if isinstance(target, ast.Name)}
            escapes = any(
                isinstance(candidate, ast.Name)
                and isinstance(candidate.ctx, ast.Load)
                and candidate.id in names
                and candidate.lineno > (context.end_lineno or context.lineno)
                for candidate in ast.walk(scope)
            )
            if escapes and not is_plain_snapshot(value):
                violations.extend((assignment.lineno, name) for name in sorted(names))

    assert violations == []
