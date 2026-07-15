"""Consistent backup creation, validation, and offline restore primitives."""

from __future__ import annotations

import hmac
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet

from version import APP_VERSION


BACKUP_FORMAT = "mangarr-backup"
BACKUP_FORMAT_VERSION = 1
DATABASE_ENTRY = "manga_arr.db"
MANIFEST_ENTRY = "manifest.json"
SECRET_KEY_ENTRY = ".mangarr-secret-key"
SECRET_KEY_ENV = "MANGARR_SECRET_KEY"
_CREATION_LOCK = threading.Lock()


class BackupArchiveError(ValueError):
    """A backup archive is malformed or cannot be restored safely."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 422,
        details: dict | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.details = details or {}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _secret_key_bytes(config_dir: str) -> bytes | None:
    env_key = os.environ.get(SECRET_KEY_ENV)
    if env_key:
        return env_key.strip().encode("ascii")

    key_path = os.path.join(config_dir, SECRET_KEY_ENTRY)
    try:
        return Path(key_path).read_bytes().strip()
    except FileNotFoundError:
        return None


def _snapshot_database(db_path: str, snapshot_path: str) -> int:
    if not os.path.isfile(db_path):
        raise FileNotFoundError(f"Mangarr database not found: {db_path}")

    with sqlite3.connect(db_path, timeout=30) as source:
        with sqlite3.connect(snapshot_path) as destination:
            source.backup(destination)
            quick_check = destination.execute("PRAGMA quick_check").fetchone()
            if not quick_check or quick_check[0] != "ok":
                raise sqlite3.DatabaseError("SQLite snapshot failed PRAGMA quick_check")
            schema_version = int(
                destination.execute("PRAGMA user_version").fetchone()[0]
            )
    return schema_version


def _write_secret_entry(zf: zipfile.ZipFile, key_bytes: bytes, now: datetime) -> None:
    info = zipfile.ZipInfo(
        SECRET_KEY_ENTRY,
        date_time=now.astimezone(timezone.utc).timetuple()[:6],
    )
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100600 << 16
    zf.writestr(info, key_bytes + b"\n")


def create_backup_archive(
    *,
    db_path: str,
    backup_dir: str,
    filename_prefix: str = "mangarr_backup",
    config_dir: str | None = None,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Create an atomic, WAL-safe backup archive and return its name and path."""
    with _CREATION_LOCK:
        return _create_backup_archive(
            db_path=db_path,
            backup_dir=backup_dir,
            filename_prefix=filename_prefix,
            config_dir=config_dir,
            now=now,
        )


def _create_backup_archive(
    *,
    db_path: str,
    backup_dir: str,
    filename_prefix: str,
    config_dir: str | None,
    now: datetime | None,
) -> tuple[str, str]:
    now = now or _utc_now()
    config_dir = config_dir or os.path.dirname(db_path)
    os.makedirs(backup_dir, exist_ok=True)

    base_name = f"{filename_prefix}_{_timestamp(now)}"
    filename = f"{base_name}.zip"
    final_path = os.path.join(backup_dir, filename)
    sequence = 1
    while os.path.exists(final_path):
        filename = f"{base_name}_{sequence:03d}.zip"
        final_path = os.path.join(backup_dir, filename)
        sequence += 1

    key_bytes = _secret_key_bytes(config_dir)
    if key_bytes is not None:
        try:
            Fernet(key_bytes)
        except (TypeError, ValueError) as exc:
            raise BackupArchiveError(
                "The active Mangarr secret key is invalid; backup was not created"
            ) from exc

    with tempfile.TemporaryDirectory(prefix=".mangarr-backup-", dir=backup_dir) as tmp:
        snapshot_path = os.path.join(tmp, DATABASE_ENTRY)
        schema_version = _snapshot_database(db_path, snapshot_path)
        manifest = {
            "format": BACKUP_FORMAT,
            "formatVersion": BACKUP_FORMAT_VERSION,
            "appVersion": APP_VERSION,
            "createdAt": now.astimezone(timezone.utc).isoformat(),
            "database": DATABASE_ENTRY,
            "schemaVersion": schema_version,
            "includesSecretKey": key_bytes is not None,
        }

        fd, temporary_archive = tempfile.mkstemp(
            prefix=".mangarr-backup-", suffix=".tmp", dir=backup_dir
        )
        os.close(fd)
        try:
            with zipfile.ZipFile(
                temporary_archive, "w", compression=zipfile.ZIP_DEFLATED
            ) as zf:
                zf.write(snapshot_path, arcname=DATABASE_ENTRY)
                if key_bytes is not None:
                    _write_secret_entry(zf, key_bytes, now)
                zf.writestr(
                    MANIFEST_ENTRY,
                    json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                )
            os.chmod(temporary_archive, 0o600)
            os.replace(temporary_archive, final_path)
        except Exception:
            try:
                os.unlink(temporary_archive)
            except FileNotFoundError:
                pass
            raise

    return filename, final_path


def _read_manifest(zf: zipfile.ZipFile, names: list[str]) -> dict | None:
    if MANIFEST_ENTRY not in names:
        return None
    try:
        manifest = json.loads(zf.read(MANIFEST_ENTRY))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BackupArchiveError("Backup manifest is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise BackupArchiveError("Backup manifest must be a JSON object")
    if manifest.get("format") != BACKUP_FORMAT:
        raise BackupArchiveError("Backup manifest has an unsupported format")
    if manifest.get("formatVersion") != BACKUP_FORMAT_VERSION:
        raise BackupArchiveError("Backup manifest version is not supported")
    if manifest.get("database") != DATABASE_ENTRY:
        raise BackupArchiveError("Backup manifest names an unsupported database entry")
    return manifest


def _validate_database_entry(zf: zipfile.ZipFile) -> tuple[int, int]:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        temp_path = tmp.name
        with zf.open(DATABASE_ENTRY) as source:
            shutil.copyfileobj(source, tmp)

    try:
        with sqlite3.connect(temp_path) as connection:
            quick_check = connection.execute("PRAGMA quick_check").fetchone()
            if not quick_check or quick_check[0] != "ok":
                raise BackupArchiveError(
                    f"{DATABASE_ENTRY} is not a valid SQLite database",
                    details={"containsDatabase": True, "databaseValid": False},
                )
            schema_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
        return os.path.getsize(temp_path), schema_version
    except sqlite3.DatabaseError as exc:
        raise BackupArchiveError(
            f"{DATABASE_ENTRY} is not a valid SQLite database",
            details={"containsDatabase": True, "databaseValid": False},
        ) from exc
    finally:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass


def validate_backup_archive(archive_path: str) -> dict:
    """Validate a current or legacy Mangarr backup without extracting it."""
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            names = zf.namelist()
            if len(names) != len(set(names)):
                raise BackupArchiveError("Backup contains duplicate entries")
            if DATABASE_ENTRY not in names:
                raise BackupArchiveError(
                    f"Backup does not contain {DATABASE_ENTRY}",
                    details={"containsDatabase": False, "databaseValid": False},
                )
            corrupt_entry = zf.testzip()
            if corrupt_entry:
                raise BackupArchiveError(
                    f"Backup contains a corrupt entry: {corrupt_entry}"
                )

            manifest = _read_manifest(zf, names)
            database_size, schema_version = _validate_database_entry(zf)
            if manifest and manifest.get("schemaVersion") != schema_version:
                raise BackupArchiveError(
                    "Backup manifest schema version does not match the database",
                    details={"containsDatabase": True, "databaseValid": True},
                )
            contains_key = SECRET_KEY_ENTRY in names
            if contains_key:
                try:
                    Fernet(zf.read(SECRET_KEY_ENTRY).strip())
                except (TypeError, ValueError) as exc:
                    raise BackupArchiveError(
                        "Backup contains an invalid Mangarr secret key"
                    ) from exc
            if manifest and bool(manifest.get("includesSecretKey")) != contains_key:
                raise BackupArchiveError(
                    "Backup manifest does not match its secret-key contents"
                )
    except zipfile.BadZipFile as exc:
        raise BackupArchiveError("Invalid ZIP file", status_code=400) from exc
    except OSError as exc:
        raise BackupArchiveError(
            f"Backup validation failed: {type(exc).__name__}", status_code=500
        ) from exc

    return {
        "entries": names,
        "containsDatabase": True,
        "databaseValid": True,
        "databaseSizeBytes": database_size,
        "schemaVersion": schema_version,
        "containsSecretKey": contains_key,
        "selfContained": contains_key,
        "format": manifest.get("format") if manifest else "legacy",
        "formatVersion": manifest.get("formatVersion") if manifest else 0,
        "appVersion": manifest.get("appVersion") if manifest else None,
    }


def restore_backup_archive(
    archive_path: str,
    *,
    config_dir: str,
    now: datetime | None = None,
) -> dict:
    """Restore a validated archive while Mangarr is stopped.

    Legacy database-only archives remain supported. They retain the currently
    installed secret key because older Mangarr releases did not bundle it.
    """
    details = validate_backup_archive(archive_path)
    now = now or _utc_now()
    os.makedirs(config_dir, exist_ok=True)

    db_path = os.path.join(config_dir, DATABASE_ENTRY)
    key_path = os.path.join(config_dir, SECRET_KEY_ENTRY)
    base_suffix = _timestamp(now)
    suffix = base_suffix
    sequence = 1
    while os.path.exists(f"{db_path}.pre-restore-{suffix}") or os.path.exists(
        f"{key_path}.pre-restore-{suffix}"
    ):
        suffix = f"{base_suffix}_{sequence:03d}"
        sequence += 1
    previous_db = f"{db_path}.pre-restore-{suffix}"
    previous_key = f"{key_path}.pre-restore-{suffix}"

    with tempfile.TemporaryDirectory(prefix=".mangarr-restore-", dir=config_dir) as tmp:
        staged_db = os.path.join(tmp, DATABASE_ENTRY)
        staged_key = os.path.join(tmp, SECRET_KEY_ENTRY)
        with zipfile.ZipFile(archive_path, "r") as zf:
            archived_key = None
            if details["containsSecretKey"]:
                archived_key = zf.read(SECRET_KEY_ENTRY).strip()
                configured_key = os.environ.get(SECRET_KEY_ENV)
                if configured_key and not hmac.compare_digest(
                    archived_key, configured_key.strip().encode("ascii")
                ):
                    raise BackupArchiveError(
                        "The backup key differs from MANGARR_SECRET_KEY; "
                        "update or unset that environment value before restore"
                    )
            with zf.open(DATABASE_ENTRY) as source, open(staged_db, "wb") as target:
                shutil.copyfileobj(source, target)
            if details["containsSecretKey"]:
                with open(staged_key, "wb") as target:
                    target.write(archived_key + b"\n")

        try:
            with sqlite3.connect(staged_db) as connection:
                quick_check = connection.execute("PRAGMA quick_check").fetchone()
                if not quick_check or quick_check[0] != "ok":
                    raise BackupArchiveError(
                        f"{DATABASE_ENTRY} failed validation during restore"
                    )
        except sqlite3.DatabaseError as exc:
            raise BackupArchiveError(
                f"{DATABASE_ENTRY} failed validation during restore"
            ) from exc

        os.chmod(staged_db, 0o600)
        if details["containsSecretKey"]:
            os.chmod(staged_key, 0o600)

        moved_db = False
        moved_key = False
        installed_db = False
        installed_key = False
        try:
            if os.path.exists(db_path):
                os.replace(db_path, previous_db)
                moved_db = True
            if details["containsSecretKey"] and os.path.exists(key_path):
                os.replace(key_path, previous_key)
                moved_key = True

            for stale_path in (f"{db_path}-wal", f"{db_path}-shm"):
                try:
                    os.unlink(stale_path)
                except FileNotFoundError:
                    pass

            os.replace(staged_db, db_path)
            installed_db = True
            if details["containsSecretKey"]:
                os.replace(staged_key, key_path)
                installed_key = True
        except Exception:
            if installed_db:
                try:
                    os.unlink(db_path)
                except FileNotFoundError:
                    pass
            if installed_key:
                try:
                    os.unlink(key_path)
                except FileNotFoundError:
                    pass
            if moved_db:
                os.replace(previous_db, db_path)
            if moved_key:
                os.replace(previous_key, key_path)
            raise

    return {
        **details,
        "databasePath": db_path,
        "secretKeyRestored": details["containsSecretKey"],
        "previousDatabasePath": previous_db if moved_db else None,
        "previousSecretKeyPath": previous_key if moved_key else None,
    }
