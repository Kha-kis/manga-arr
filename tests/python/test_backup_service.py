import errno
import json
import os
import sqlite3
import zipfile
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
import pytest

from backups import (
    BackupArchiveError,
    DATABASE_ENTRY,
    MANIFEST_ENTRY,
    SECRET_KEY_ENTRY,
    _snapshot_database,
    create_backup_archive,
    restore_backup_archive,
    validate_backup_archive,
)


def _create_database(path, value="current"):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE state(value TEXT NOT NULL)")
    connection.execute("INSERT INTO state(value) VALUES(?)", (value,))
    # This state-only fixture represents a pre-selection legacy database.
    connection.execute("PRAGMA user_version=6")
    connection.commit()
    return connection


def _read_value(path):
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT value FROM state").fetchone()[0]


@contextmanager
def _track_snapshot_connections(
    monkeypatch: pytest.MonkeyPatch, *, failure: str | None = None
) -> Generator[list[sqlite3.Connection], None, None]:
    connections: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    class SnapshotConnection(sqlite3.Connection):
        def backup(
            self,
            target: sqlite3.Connection,
            *,
            pages: int = -1,
            progress: Callable[[int, int, int], object] | None = None,
            name: str = "main",
            sleep: float = 0.250,
        ) -> None:
            if failure == "backup":
                raise sqlite3.DatabaseError("injected backup failure")
            super().backup(
                target, pages=pages, progress=progress, name=name, sleep=sleep
            )

        def execute(
            self,
            sql: str,
            parameters: Any = (),
            /,
        ) -> sqlite3.Cursor:
            if sql == "PRAGMA quick_check" and failure == "quick-check":
                return super().execute("SELECT 'injected quick_check failure'")
            if sql == "PRAGMA user_version" and failure == "schema-read":
                raise sqlite3.DatabaseError("injected schema read failure")
            return super().execute(sql, parameters)

    def connect(database: str, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        if failure == "destination-open" and len(connections) == 1:
            raise sqlite3.OperationalError("injected destination open failure")
        connection = real_connect(database, *args, factory=SnapshotConnection, **kwargs)
        connections.append(connection)
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", connect)
        try:
            yield connections
        finally:
            # Retain references for observation, but never leak handles on RED.
            for connection in connections:
                connection.close()


def _assert_connections_closed(connections: list[sqlite3.Connection]) -> None:
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")


@pytest.mark.parametrize(
    "failure", [None, "destination-open", "backup", "quick-check", "schema-read"]
)
def test_snapshot_closes_connections_on_success_and_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    db_path = tmp_path / "source.db"
    writer = _create_database(db_path)
    try:
        with _track_snapshot_connections(monkeypatch, failure=failure) as connections:
            if failure is None:
                assert (
                    _snapshot_database(str(db_path), str(tmp_path / "snapshot.db")) == 6
                )
            else:
                message = {
                    "destination-open": "injected destination open failure",
                    "backup": "injected backup failure",
                    "quick-check": "SQLite snapshot failed PRAGMA quick_check",
                    "schema-read": "injected schema read failure",
                }[failure]
                with pytest.raises(sqlite3.DatabaseError, match=message):
                    _snapshot_database(str(db_path), str(tmp_path / "snapshot.db"))
            assert len(connections) == (1 if failure == "destination-open" else 2)
            _assert_connections_closed(connections)
    finally:
        writer.close()


@pytest.mark.parametrize("boundary", ["zip-read", "cleanup"])
def test_archive_snapshot_connections_closed_at_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    db_path = tmp_path / DATABASE_ENTRY
    writer = _create_database(db_path)
    real_write = zipfile.ZipFile.write
    real_rmdir = os.rmdir
    observed: list[str] = []

    def write_snapshot(
        archive: zipfile.ZipFile,
        filename: str,
        arcname: str | None = None,
        compress_type: int | None = None,
        compresslevel: int | None = None,
    ) -> None:
        if arcname == DATABASE_ENTRY and boundary == "zip-read":
            assert len(connections) == 2
            _assert_connections_closed(connections)
            observed.append("zip-read")
        real_write(archive, filename, arcname, compress_type, compresslevel)

    def remove_snapshot(path: str, *, dir_fd: int | None = None) -> None:
        if Path(path).name.startswith(".mangarr-backup-") and boundary == "cleanup":
            assert len(connections) == 2
            _assert_connections_closed(connections)
            observed.append("cleanup")
        real_rmdir(path, dir_fd=dir_fd)

    try:
        with _track_snapshot_connections(monkeypatch) as connections:
            with monkeypatch.context() as patch:
                patch.setattr(zipfile.ZipFile, "write", write_snapshot)
                patch.setattr(os, "rmdir", remove_snapshot)
                filename, archive_path = create_backup_archive(
                    db_path=str(db_path), backup_dir=str(tmp_path / "backups")
                )
            assert observed == [boundary]
            assert Path(archive_path).name == filename
            assert not list((tmp_path / "backups").glob(".mangarr-backup-*"))
        with zipfile.ZipFile(archive_path) as archive:
            assert archive.testzip() is None
            extracted_db = tmp_path / "extracted.db"
            extracted_db.write_bytes(archive.read(DATABASE_ENTRY))
        connection = sqlite3.connect(extracted_db)
        try:
            assert connection.execute("SELECT value FROM state").fetchone() == (
                "current",
            )
        finally:
            connection.close()
    finally:
        writer.close()


def test_archive_cleanup_obstruction_still_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / DATABASE_ENTRY
    writer = _create_database(db_path)
    backup_dir = tmp_path / "backups"
    real_rmdir = os.rmdir
    obstruction = OSError(errno.ENOTEMPTY, "injected snapshot cleanup obstruction")

    def obstruct_snapshot(path: str, *, dir_fd: int | None = None) -> None:
        if Path(path).name.startswith(".mangarr-backup-"):
            raise obstruction
        real_rmdir(path, dir_fd=dir_fd)

    try:
        with _track_snapshot_connections(monkeypatch) as connections:
            with monkeypatch.context() as patch:
                patch.setattr(os, "rmdir", obstruct_snapshot)
                with pytest.raises(OSError) as raised:
                    create_backup_archive(
                        db_path=str(db_path), backup_dir=str(backup_dir)
                    )
            assert raised.value is obstruction
            assert raised.value.errno == errno.ENOTEMPTY
            assert len(list(backup_dir.glob("*.zip"))) == 1
            assert len(list(backup_dir.glob(".mangarr-backup-*"))) == 1
            assert len(connections) == 2
            _assert_connections_closed(connections)
    finally:
        writer.close()


def test_create_backup_is_wal_safe_self_contained_and_versioned(tmp_path):
    config_dir = tmp_path / "config"
    backup_dir = config_dir / "backups"
    config_dir.mkdir()
    db_path = config_dir / DATABASE_ENTRY
    writer = _create_database(db_path)
    key = Fernet.generate_key()
    (config_dir / SECRET_KEY_ENTRY).write_bytes(key)

    try:
        filename, archive_path = create_backup_archive(
            db_path=str(db_path),
            backup_dir=str(backup_dir),
            config_dir=str(config_dir),
            now=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
        )
    finally:
        writer.close()

    assert filename == "mangarr_backup_20260715T120000Z.zip"
    assert os.stat(archive_path).st_mode & 0o777 == 0o600
    details = validate_backup_archive(archive_path)
    assert details["selfContained"] is True
    assert details["schemaVersion"] == 6
    assert details["formatVersion"] == 1

    extracted_db = tmp_path / "snapshot.db"
    with zipfile.ZipFile(archive_path) as archive:
        assert set(archive.namelist()) == {
            DATABASE_ENTRY,
            SECRET_KEY_ENTRY,
            MANIFEST_ENTRY,
        }
        extracted_db.write_bytes(archive.read(DATABASE_ENTRY))
        assert archive.read(SECRET_KEY_ENTRY).strip() == key
        manifest = json.loads(archive.read(MANIFEST_ENTRY))
        assert manifest["includesSecretKey"] is True
        assert manifest["schemaVersion"] == 6
    assert _read_value(extracted_db) == "current"


def test_concurrent_backup_requests_get_distinct_archives(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    db_path = config_dir / DATABASE_ENTRY
    writer = _create_database(db_path)
    writer.close()
    fixed_now = datetime(2026, 7, 15, 12, 1, tzinfo=timezone.utc)

    def create_one():
        return create_backup_archive(
            db_path=str(db_path),
            backup_dir=str(config_dir / "backups"),
            config_dir=str(config_dir),
            now=fixed_now,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: create_one(), range(2)))

    names = sorted(filename for filename, _path in results)
    assert names == [
        "mangarr_backup_20260715T120100Z.zip",
        "mangarr_backup_20260715T120100Z_001.zip",
    ]
    for _filename, archive_path in results:
        assert validate_backup_archive(archive_path)["databaseValid"] is True


def test_restore_replaces_database_and_key_but_keeps_rollback_files(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    source_db = source / DATABASE_ENTRY
    source_writer = _create_database(source_db, "restored")
    restored_key = Fernet.generate_key()
    (source / SECRET_KEY_ENTRY).write_bytes(restored_key)
    _, archive_path = create_backup_archive(
        db_path=str(source_db),
        backup_dir=str(source / "backups"),
        config_dir=str(source),
    )
    source_writer.close()

    target = tmp_path / "target"
    target.mkdir()
    current_db = target / DATABASE_ENTRY
    current_writer = _create_database(current_db, "previous")
    current_writer.close()
    previous_key = Fernet.generate_key()
    (target / SECRET_KEY_ENTRY).write_bytes(previous_key)
    collision = target / f"{DATABASE_ENTRY}.pre-restore-20260715T120500Z"
    collision.write_text("preserve me")

    result = restore_backup_archive(
        archive_path,
        config_dir=str(target),
        now=datetime(2026, 7, 15, 12, 5, tzinfo=timezone.utc),
    )

    assert _read_value(current_db) == "restored"
    assert (target / SECRET_KEY_ENTRY).read_bytes().strip() == restored_key
    assert collision.read_text() == "preserve me"
    assert result["previousDatabasePath"].endswith("20260715T120500Z_001")
    assert _read_value(result["previousDatabasePath"]) == "previous"
    assert open(result["previousSecretKeyPath"], "rb").read().strip() == previous_key
    assert result["secretKeyRestored"] is True


def test_legacy_database_only_backup_validates_and_retains_current_key(tmp_path):
    source_db = tmp_path / "legacy.db"
    writer = _create_database(source_db, "legacy")
    writer.close()
    archive_path = tmp_path / "legacy.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(source_db, DATABASE_ENTRY)

    details = validate_backup_archive(str(archive_path))
    assert details["format"] == "legacy"
    assert details["selfContained"] is False

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    current_db = config_dir / DATABASE_ENTRY
    current_writer = _create_database(current_db, "current")
    current_writer.close()
    current_key = Fernet.generate_key()
    (config_dir / SECRET_KEY_ENTRY).write_bytes(current_key)

    result = restore_backup_archive(str(archive_path), config_dir=str(config_dir))

    assert _read_value(current_db) == "legacy"
    assert (config_dir / SECRET_KEY_ENTRY).read_bytes() == current_key
    assert result["secretKeyRestored"] is False


def test_scheduled_backup_uses_the_shared_archive_service():
    tasks_source = (
        Path(__file__).resolve().parents[2] / "app" / "tasks.py"
    ).read_text()
    assert "await asyncio.to_thread(" in tasks_source
    assert '"mangarr_auto",' in tasks_source
    assert 'zf.write(DB_PATH, "mangarr.db")' not in tasks_source


def test_invalid_database_reports_that_the_entry_was_present(tmp_path):
    archive_path = tmp_path / "invalid-db.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(DATABASE_ENTRY, b"not sqlite")

    with pytest.raises(BackupArchiveError) as raised:
        validate_backup_archive(str(archive_path))

    assert raised.value.details == {
        "containsDatabase": True,
        "databaseValid": False,
    }


def test_manifest_schema_version_must_match_database(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    db_path = config_dir / DATABASE_ENTRY
    writer = _create_database(db_path)
    writer.close()
    _, original_path = create_backup_archive(
        db_path=str(db_path),
        backup_dir=str(config_dir / "backups"),
        config_dir=str(config_dir),
    )

    altered_path = tmp_path / "altered.zip"
    with (
        zipfile.ZipFile(original_path) as source,
        zipfile.ZipFile(altered_path, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for name in source.namelist():
            payload = source.read(name)
            if name == MANIFEST_ENTRY:
                manifest = json.loads(payload)
                manifest["schemaVersion"] = 999
                payload = json.dumps(manifest).encode()
            target.writestr(name, payload)

    with pytest.raises(BackupArchiveError, match="schema version"):
        validate_backup_archive(str(altered_path))
