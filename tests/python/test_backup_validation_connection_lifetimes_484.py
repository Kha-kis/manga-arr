"""Retained real handles expose validation lifetimes without GC timing or NFS."""

import os
import sqlite3
import zipfile
from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Literal

import pytest
from cryptography.fernet import Fernet

from backups import (
    BackupArchiveError,
    DATABASE_ENTRY,
    SECRET_KEY_ENTRY,
    restore_backup_archive,
    validate_backup_archive,
)


def _create_database(path: Path, value: str) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE state(value TEXT NOT NULL)")
        connection.execute("INSERT INTO state(value) VALUES(?)", (value,))
        # This state-only fixture represents a pre-selection legacy database.
        connection.execute("PRAGMA user_version=6")


@pytest.fixture
def legacy_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("MANGARR_SECRET_KEY", raising=False)
    database = tmp_path / "fixture.db"
    _create_database(database, "restored")
    archive_path = tmp_path / "legacy.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.write(database, DATABASE_ENTRY)
    return archive_path


@contextmanager
def _retain_connections(
    monkeypatch: pytest.MonkeyPatch,
    *,
    failure: Literal["validator-query", "staged-quick-check"] | None = None,
) -> Generator[list[tuple[Path, sqlite3.Connection]], None, None]:
    real_connect = sqlite3.connect
    connections: list[tuple[Path, sqlite3.Connection]] = []

    def connect(database: str) -> sqlite3.Connection:
        class ValidationConnection(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                if failure == "validator-query" and sql == "PRAGMA user_version":
                    raise sqlite3.DatabaseError("injected validator query failure")
                if (
                    failure == "staged-quick-check"
                    and Path(database).parent.name.startswith(".mangarr-restore-")
                    and sql == "PRAGMA quick_check"
                ):
                    return super().execute("SELECT 'injected quick_check failure'")
                return super().execute(sql, parameters)

        connection = real_connect(database, factory=ValidationConnection)
        connections.append((Path(database), connection))
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", connect)
        try:
            yield connections
        finally:
            # Keep handles alive through assertions, but close even on RED.
            for _, connection in connections:
                connection.close()


def _closed(connection: sqlite3.Connection) -> bool:
    try:
        connection.execute("SELECT 1").close()
    except sqlite3.ProgrammingError as exc:
        assert "closed database" in str(exc)
        return True
    return False


def test_public_validation_closes_database_before_unlink(
    legacy_archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_archive = legacy_archive.read_bytes()
    real_unlink = os.unlink
    observed: list[bool] = []

    with _retain_connections(monkeypatch) as connections:

        def unlink(path: str, *, dir_fd: int | None = None) -> None:
            for database, connection in connections:
                if Path(path) == database:
                    observed.append(_closed(connection))
            real_unlink(path, dir_fd=dir_fd)

        with monkeypatch.context() as patch:
            patch.setattr(os, "unlink", unlink)
            details = validate_backup_archive(str(legacy_archive))

        assert details["databaseValid"] is True
        assert details["schemaVersion"] == 6
        assert legacy_archive.read_bytes() == original_archive
        assert len(connections) == 1
        assert not connections[0][0].exists()
        assert observed == [True], "validator SQLite handle still open before unlink"
        assert _closed(connections[0][1]), "validator handle still open at return"


def test_private_restore_closes_staged_database_before_publish(
    legacy_archive: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    database = target / DATABASE_ENTRY
    _create_database(database, "previous")
    original_database = database.read_bytes()
    current_key = Fernet.generate_key()
    (target / SECRET_KEY_ENTRY).write_bytes(current_key)
    real_replace = os.replace
    observed: list[bool] = []

    with _retain_connections(monkeypatch) as connections:

        def replace(source: str, destination: str) -> None:
            for path, connection in connections:
                if Path(source) == path and path.parent.name.startswith(
                    ".mangarr-restore-"
                ):
                    observed.append(_closed(connection))
            real_replace(source, destination)

        with monkeypatch.context() as patch:
            patch.setattr(os, "replace", replace)
            result = restore_backup_archive(str(legacy_archive), config_dir=str(target))

        assert len(connections) == 2
        assert (target / SECRET_KEY_ENTRY).read_bytes() == current_key
        assert Path(result["previousDatabasePath"]).read_bytes() == original_database
        assert result["secretKeyRestored"] is False
        assert not list(target.glob(".mangarr-restore-*"))
        with closing(sqlite3.Connection(database)) as restored:
            assert restored.execute("SELECT value FROM state").fetchall() == [
                ("restored",)
            ]
        assert observed == [True], (
            "staged restore SQLite handle still open before replace"
        )
        assert _closed(connections[1][1]), "staged restore handle still open at return"


@pytest.mark.parametrize("failure", ["validator-query", "invalid-sqlite"])
def test_validation_failure_wraps_error_and_closes_database(
    legacy_archive: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    if failure == "invalid-sqlite":
        with zipfile.ZipFile(legacy_archive, "w") as archive:
            archive.writestr(DATABASE_ENTRY, b"not a SQLite database")
    original_archive = legacy_archive.read_bytes()
    injection = "validator-query" if failure == "validator-query" else None

    with _retain_connections(monkeypatch, failure=injection) as connections:
        with pytest.raises(
            BackupArchiveError, match="not a valid SQLite database"
        ) as raised:
            validate_backup_archive(str(legacy_archive))

        assert raised.value.status_code == 422
        assert raised.value.details == {
            "containsDatabase": True,
            "databaseValid": False,
        }
        assert isinstance(raised.value.__cause__, sqlite3.DatabaseError)
        if failure == "validator-query":
            assert str(raised.value.__cause__) == "injected validator query failure"
        assert legacy_archive.read_bytes() == original_archive
        assert len(connections) == 1
        assert not connections[0][0].exists()
        assert _closed(connections[0][1]), (
            "failed validator handle still open at return"
        )


def test_staged_quick_check_failure_closes_database_and_preserves_originals(
    legacy_archive: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    database = target / DATABASE_ENTRY
    _create_database(database, "previous")
    original_database = database.read_bytes()
    current_key = Fernet.generate_key()
    (target / SECRET_KEY_ENTRY).write_bytes(current_key)

    with _retain_connections(monkeypatch, failure="staged-quick-check") as connections:
        with pytest.raises(
            BackupArchiveError, match="failed validation during restore"
        ) as raised:
            restore_backup_archive(str(legacy_archive), config_dir=str(target))

        assert raised.value.status_code == 422
        assert raised.value.details == {}
        assert raised.value.__cause__ is None
        assert database.read_bytes() == original_database
        assert (target / SECRET_KEY_ENTRY).read_bytes() == current_key
        assert not list(target.glob("*.pre-restore-*"))
        assert not list(target.glob(".mangarr-restore-*"))
        assert len(connections) == 2
        assert _closed(connections[1][1]), (
            "failed staged restore handle still open at return"
        )
        assert _closed(connections[0][1]), (
            "restore prevalidation handle still open at return"
        )
