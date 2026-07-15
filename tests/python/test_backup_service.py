import json
import os
import sqlite3
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet
import pytest

from backups import (
    BackupArchiveError,
    DATABASE_ENTRY,
    MANIFEST_ENTRY,
    SECRET_KEY_ENTRY,
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
    connection.execute("PRAGMA user_version=7")
    connection.commit()
    return connection


def _read_value(path):
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT value FROM state").fetchone()[0]


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
    assert details["schemaVersion"] == 7
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
        assert manifest["schemaVersion"] == 7
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
