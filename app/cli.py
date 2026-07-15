"""Supported operator CLI for Mangarr container maintenance."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

from backups import (
    BackupArchiveError,
    create_backup_archive,
    restore_backup_archive,
    validate_backup_archive,
)
from version import APP_VERSION


def _config_dir(value: str | None) -> str:
    return os.path.abspath(value or os.environ.get("MANGARR_CONFIG_DIR", "/config"))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mangarr",
        description="Mangarr operator maintenance commands",
    )
    parser.add_argument("--version", action="version", version=f"Mangarr {APP_VERSION}")
    parser.add_argument(
        "--config-dir",
        help="persistent configuration directory (default: /config)",
    )
    commands = parser.add_subparsers(dest="group", required=True)

    admin = commands.add_parser("admin", help="administrator recovery")
    admin_commands = admin.add_subparsers(dest="admin_command", required=True)
    reset = admin_commands.add_parser(
        "reset", help="remove the administrator and revoke browser sessions"
    )
    reset.add_argument("--yes", action="store_true", help="confirm the reset")

    backup = commands.add_parser("backup", help="backup and restore maintenance")
    backup_commands = backup.add_subparsers(dest="backup_command", required=True)

    create = backup_commands.add_parser("create", help="create a consistent backup")
    create.add_argument(
        "--output-dir",
        help="archive directory (default: CONFIG_DIR/backups)",
    )

    validate = backup_commands.add_parser("validate", help="validate a backup archive")
    validate.add_argument("archive", help="path to the backup ZIP")
    validate.add_argument("--json", action="store_true", help="emit JSON details")

    restore = backup_commands.add_parser(
        "restore", help="restore a backup while the Mangarr service is stopped"
    )
    restore.add_argument("archive", help="path to the backup ZIP")
    restore.add_argument("--yes", action="store_true", help="confirm the restore")
    return parser


def _reset_admin(
    config_dir: str, confirmed: bool, parser: argparse.ArgumentParser
) -> int:
    if not confirmed:
        parser.error("admin reset requires --yes")

    import auth

    auth.reset_admin_for_recovery(config_dir)
    sys.stdout.write(
        "Administrator reset and browser sessions revoked.\n"
        "Open Mangarr and create the replacement administrator immediately.\n"
    )
    return 0


def _create_backup(config_dir: str, output_dir: str | None) -> int:
    backup_dir = os.path.abspath(output_dir or os.path.join(config_dir, "backups"))
    filename, path = create_backup_archive(
        db_path=os.path.join(config_dir, "manga_arr.db"),
        backup_dir=backup_dir,
        config_dir=config_dir,
    )
    sys.stdout.write(f"Backup created: {filename}\n{path}\n")
    return 0


def _validate_backup(archive: str, emit_json: bool) -> int:
    path = os.path.abspath(os.path.expanduser(archive))
    details = validate_backup_archive(path)
    if emit_json:
        sys.stdout.write(json.dumps(details, indent=2, sort_keys=True) + "\n")
    else:
        kind = "self-contained" if details["selfContained"] else "legacy database-only"
        sys.stdout.write(
            f"Backup valid: {path}\n"
            f"Format: {kind}; schema version: {details['schemaVersion']}\n"
        )
    return 0


def _restore_backup(
    archive: str,
    config_dir: str,
    confirmed: bool,
    parser: argparse.ArgumentParser,
) -> int:
    if not confirmed:
        parser.error("backup restore requires --yes and a stopped Mangarr service")
    path = os.path.abspath(os.path.expanduser(archive))
    result = restore_backup_archive(path, config_dir=config_dir)
    sys.stdout.write(f"Backup restored to {result['databasePath']}.\n")
    if result["secretKeyRestored"]:
        sys.stdout.write("The matching Mangarr secret key was restored.\n")
    else:
        sys.stdout.write(
            "Legacy backup: the existing Mangarr secret key was retained.\n"
        )
    if result["previousDatabasePath"]:
        sys.stdout.write(
            f"Previous database retained at {result['previousDatabasePath']}.\n"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    config_dir = _config_dir(args.config_dir)

    try:
        if args.group == "admin" and args.admin_command == "reset":
            return _reset_admin(config_dir, args.yes, parser)
        if args.group == "backup" and args.backup_command == "create":
            return _create_backup(config_dir, args.output_dir)
        if args.group == "backup" and args.backup_command == "validate":
            return _validate_backup(args.archive, args.json)
        if args.group == "backup" and args.backup_command == "restore":
            return _restore_backup(args.archive, config_dir, args.yes, parser)
    except (
        BackupArchiveError,
        FileNotFoundError,
        OSError,
        sqlite3.DatabaseError,
    ) as exc:
        sys.stderr.write(f"mangarr: {exc}\n")
        return 1

    parser.error("unsupported command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
