"""Backup health measures filesystem age, not filename order or ZIP validity."""

import asyncio
import os
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backups import DATABASE_ENTRY, create_backup_archive, validate_backup_archive
from routers import health_ as health


NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc).timestamp()
CURRENT_NAME = "mangarr_backup_20261009T120000Z.zip"
EMPTY_MESSAGE = "No backups created yet \u2014 consider enabling automatic backups"


@pytest.fixture
def backup_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "backups"
    folder.mkdir()
    with sqlite3.connect(tmp_path / DATABASE_ENTRY) as db:
        db.execute("CREATE TABLE fixture(value TEXT)")
        db.execute("INSERT INTO fixture VALUES('synthetic')")
    monkeypatch.delenv("MANGARR_SECRET_KEY", raising=False)
    monkeypatch.setattr(health.time, "time", lambda: NOW)
    monkeypatch.setattr(
        health,
        "get_cfg",
        lambda key, default="": str(folder) if key == "backup_folder" else default,
    )
    monkeypatch.setattr(
        health,
        "_health_db_snapshot",
        lambda: {
            "indexers_enabled": 1,
            "download_clients_enabled": 1,
            "quality_profiles": 1,
            "root_folders": [],
            "orphan_series_rf": 0,
            "wanted_volumes": 0,
            "last_grab": None,
            "last_rss_poll": None,
            "qbit_client": None,
            "sab_client": None,
            "stale_series": [],
            "stale_grabs": [],
            "stuck_imports": [],
            "recent_errors": [],
            "last_backlog": None,
            "stats": {},
        },
    )
    return folder


def _write_backup(
    folder: Path,
    filename: str,
    age_days: float,
    *,
    legacy: bool = False,
) -> Path:
    target = folder / filename
    db_path = folder.parent / DATABASE_ENTRY
    if legacy:
        with zipfile.ZipFile(target, "w") as archive:
            archive.write(db_path, DATABASE_ENTRY)
    else:
        _, archive_path = create_backup_archive(
            db_path=str(db_path),
            backup_dir=str(folder),
            config_dir=str(folder.parent),
            now=datetime.fromtimestamp(NOW, timezone.utc),
        )
        Path(archive_path).rename(target)
    mtime = NOW - age_days * 86400
    os.utime(target, (mtime, mtime))
    return target


def _backup_check() -> dict[str, object]:
    payload = asyncio.run(health.build_health_payload())
    return next(check for check in payload["checks"] if check["name"] == "Backups")


def _expected_check(ok: bool, message: str) -> dict[str, object]:
    return {
        "name": "Backups",
        "ok": ok,
        "message": message,
        "severity": "warning",
        "fix_url": "/system/backup",
    }


@pytest.mark.parametrize(
    "legacy_name",
    [
        "mangarr_backup_20990101_000000.zip",
        "legacy.zip",
    ],
)
def test_fresh_supported_backup_wins_regardless_of_filename_order(
    backup_folder: Path,
    legacy_name: str,
) -> None:
    fresh = _write_backup(backup_folder, CURRENT_NAME, 1)
    legacy = _write_backup(backup_folder, legacy_name, 86, legacy=True)
    assert validate_backup_archive(str(fresh))["databaseValid"] is True
    assert validate_backup_archive(str(legacy))["format"] == "legacy"
    assert _backup_check() == _expected_check(True, "Last backup 1.0 days ago")


def test_all_stale_backups_report_newest_mtime(backup_folder: Path) -> None:
    _write_backup(backup_folder, CURRENT_NAME, 12)
    _write_backup(backup_folder, "mangarr_backup_20990101_000000.zip", 86, legacy=True)
    assert _backup_check() == _expected_check(False, "Last backup was 12 days ago")


@pytest.mark.parametrize(
    "age_days, ok, message",
    [
        (7, True, "Last backup 7.0 days ago"),
        (7 + 1 / 86400, False, "Last backup was 7 days ago"),
    ],
)
def test_backup_age_keeps_fixed_seven_day_threshold(
    backup_folder: Path,
    age_days: float,
    ok: bool,
    message: str,
) -> None:
    _write_backup(backup_folder, CURRENT_NAME, age_days)
    assert _backup_check() == _expected_check(ok, message)


@pytest.mark.parametrize("folder_state", ["missing", "empty", "irrelevant"])
def test_no_backup_messages_stay_compatible(
    backup_folder: Path,
    folder_state: str,
) -> None:
    if folder_state == "missing":
        backup_folder.rmdir()
    elif folder_state == "irrelevant":
        (backup_folder / "notes.txt").write_text("synthetic", encoding="ascii")
        (backup_folder / "unrelated.ZIP").write_bytes(b"not an eligible backup")
        (backup_folder / ".mangarr-backup-staging").mkdir()
    message = "No backups yet" if folder_state == "missing" else EMPTY_MESSAGE
    assert _backup_check() == _expected_check(True, message)


def test_fresh_non_backup_entries_do_not_hide_stale_backup(backup_folder: Path) -> None:
    _write_backup(backup_folder, CURRENT_NAME, 12)
    for name in ("notes.txt", "unrelated.ZIP", ".mangarr-backup-in-progress.tmp"):
        entry = backup_folder / name
        entry.write_bytes(b"synthetic non-backup")
        os.utime(entry, (NOW, NOW))
    directory = backup_folder / "new-directory"
    directory.mkdir()
    os.utime(directory, (NOW, NOW))
    assert _backup_check() == _expected_check(False, "Last backup was 12 days ago")


def test_backup_removed_after_listing_reports_failed_check(
    backup_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup = _write_backup(backup_folder, CURRENT_NAME, 1)
    original = health.os.listdir

    def list_then_remove(path: str) -> list[str]:
        names = original(path)
        if path == str(backup_folder):
            backup.unlink()
        return names

    monkeypatch.setattr(health.os, "listdir", list_then_remove)
    check = _backup_check()
    assert check["ok"] is False
    assert check["severity"] == "warning"
    assert check["fix_url"] == "/system/backup"
    assert "No such file or directory" in str(check["message"])


@pytest.mark.parametrize("other_backup", [False, True])
def test_backup_stat_error_is_not_silently_healthy(
    backup_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
    other_backup: bool,
) -> None:
    unreadable = _write_backup(backup_folder, "legacy.zip", 86, legacy=True)
    if other_backup:
        _write_backup(backup_folder, CURRENT_NAME, 1)
    original = health.os.path.getmtime

    def fail_stat(path: str) -> float:
        if path == str(unreadable):
            raise PermissionError("synthetic backup stat failure")
        return original(path)

    monkeypatch.setattr(health.os.path, "getmtime", fail_stat)
    assert _backup_check() == _expected_check(False, "synthetic backup stat failure")
