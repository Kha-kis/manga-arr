import json
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from typing import Any
import zipfile

from cryptography.fernet import Fernet
import pytest

import cli
from backups import DATABASE_ENTRY, SECRET_KEY_ENTRY, create_backup_archive
from release_identity import ReleaseIdentity, SourceContext, add_block
from release_selections import SelectionIssuer, SelectionSnapshot, issue_selections


_INSTALLED_CLI_PROBE = """
import base64
import hashlib
import importlib
import importlib.metadata
import json
from pathlib import Path
import sys

executable, prefix, distribution_name, entry_name, entry_value, *names = sys.argv[1:]
assert Path(sys.prefix).resolve() == Path(prefix).resolve(), "CLI Python environment mismatch"
try:
    dist = importlib.metadata.distribution(distribution_name)
except importlib.metadata.PackageNotFoundError:
    raise AssertionError("CLI launcher is not registered") from None
entries = [e for e in dist.entry_points
           if e.group == 'console_scripts' and e.name == entry_name and e.value == entry_value]
assert len(entries) == 1, "CLI launcher is not registered"
records = {str(Path(dist.locate_file(p)).resolve()): p for p in dist.files or ()}
launcher = Path(executable).resolve()
assert launcher.name == entries[0].name and str(launcher) in records, "CLI launcher is not registered in distribution RECORD"
record = records[str(launcher)]
assert record.hash is not None, "CLI launcher has no RECORD hash"
digest = base64.urlsafe_b64encode(hashlib.new(record.hash.mode, launcher.read_bytes()).digest()).decode().rstrip('=')
assert digest == record.hash.value, "CLI launcher does not match distribution RECORD"
modules = {n: str(Path(importlib.import_module(n).__file__).resolve()) for n in names}
assert set(modules.values()) <= records.keys(), "CLI modules are not owned by installed distribution"
assert not {'main', 'schema', 'release_sources', 'routers'} & sys.modules.keys()
print(json.dumps({'modules': modules, 'prefix': sys.prefix, 'executable': sys.executable,
                  'launcher': str(launcher)}))
"""


@pytest.mark.parametrize("kind", ["cross-venv", "unregistered-launcher"])
def test_installed_cli_guard_rejects_wrong_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    checkout = Path(__file__).resolve().parents[2]
    assert not tmp_path.resolve().is_relative_to(checkout)
    run = subprocess.run
    interpreter = Path(sys.executable)
    if kind == "cross-venv":
        venv = tmp_path / "negative-venv"
        run(
            [sys.executable, "-B", "-m", "venv", "--without-pip", str(venv)],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        interpreter = venv / "bin" / "python"
        assert interpreter.resolve() == Path(sys.executable).resolve()
        environment = run(
            [str(interpreter), "-B", "-c", "import sys; print(sys.prefix)"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert Path(environment.stdout.strip()).resolve() != Path(sys.prefix).resolve()
    executable = tmp_path / "mangarr"
    executable.write_text(f"#!{interpreter}\nraise SystemExit(0)\n")
    executable.chmod(0o700)
    monkeypatch.setenv("MANGARR_INSTALLED_CLI", str(executable))

    def observe_probe(
        args: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        assert args[0] == str(interpreter), (
            "probe used pytest's interpreter instead of selected shebang"
        )
        assert str(executable) in args[4:], (
            "probe omitted the selected launcher identity"
        )
        return run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", observe_probe)
    reason = (
        "CLI Python environment mismatch"
        if kind == "cross-venv"
        else "CLI launcher is not registered"
    )
    with pytest.raises(AssertionError, match=reason):
        _operator_command(tmp_path)


def test_installed_cli_probe_rejects_unregistered_record_path(tmp_path: Path) -> None:
    from importlib.metadata import distribution

    assert not tmp_path.resolve().is_relative_to(Path(__file__).resolve().parents[2])
    executable = tmp_path / "pytest"
    executable.write_text(f"#!{sys.executable}\nraise SystemExit(0)\n")
    executable.chmod(0o700)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    # Existing pytest metadata exercises the real RECORD check, not a fake Mangarr install.
    entry = next(
        e
        for e in distribution("pytest").entry_points
        if e.group == "console_scripts" and e.name == "pytest"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            _INSTALLED_CLI_PROBE,
            str(executable),
            sys.prefix,
            "pytest",
            "pytest",
            entry.value,
            "pytest",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert "CLI launcher is not registered in distribution RECORD" in result.stderr


def _operator_command(workdir: Path) -> tuple[list[str], dict[str, str]]:
    checkout = Path(__file__).resolve().parents[2]
    assert not workdir.resolve().is_relative_to(checkout)
    env = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "MANGARR_SECRET_KEY"):
        env.pop(name, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if "MANGARR_INSTALLED_CLI" not in env:
        return [sys.executable, "-B", str(checkout / "app" / "cli.py")], env

    # An explicitly selected installed lane must never fall back to source.
    executable = Path(env["MANGARR_INSTALLED_CLI"])
    assert executable.is_absolute() and executable.is_file()
    assert os.access(executable, os.X_OK)
    assert not executable.resolve().is_relative_to(checkout)
    shebang = executable.read_text().splitlines()[0]
    assert shebang.startswith("#!")
    interpreter = Path(shebang[2:])
    assert interpreter.is_absolute() and interpreter.is_file()
    assert os.access(interpreter, os.X_OK)
    probe = subprocess.run(
        [
            str(interpreter),
            "-B",
            "-c",
            _INSTALLED_CLI_PROBE,
            str(executable),
            sys.prefix,
            "mangarr",
            "mangarr",
            "cli:main",
            "cli",
            "backups",
            "release_selections",
            "release_identity",
        ],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    origins = json.loads(probe.stdout)["modules"]
    assert set(origins) == {"cli", "backups", "release_selections", "release_identity"}
    assert all(not Path(p).is_relative_to(checkout) for p in origins.values())
    return [str(executable)], env


def _durable_evidence(
    db: sqlite3.Connection,
) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    return (
        db.execute(
            "SELECT id,release_source_scope FROM indexers ORDER BY id"
        ).fetchall(),
        db.execute(
            "SELECT * FROM blocklist_release_identities "
            "ORDER BY blocklist_id,series_id,source_scope,protocol,release_guid"
        ).fetchall(),
    )


@pytest.mark.parametrize("schema_version", [7, 6], ids=["current", "legacy"])
def test_operator_restore_clears_selection_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema_version: int
) -> None:
    source = tmp_path / "fixture"
    source.mkdir()
    source_db = source / DATABASE_ENTRY
    key = Fernet.generate_key()
    (source / SECRET_KEY_ENTRY).write_bytes(key)
    archive = tmp_path / "fixture.zip"
    now = int(time.time())
    if schema_version == 7:
        import schema
        import shared

        monkeypatch.setattr(shared, "DB_PATH", str(source_db))
        schema.init_db()
        direct, parent = "bs1:d:" + "a" * 64, "bs1:p:" + "b" * 64
        with closing(sqlite3.connect(source_db)) as db, db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO series(id,title,search_pattern) VALUES(1,'one','one')"
            )
            db.execute("INSERT INTO volumes(id,series_id,volume_num) VALUES(1,1,1)")
            db.executemany(
                "INSERT INTO indexers(id,name,type,release_source_scope) VALUES(?,?,?,?)",
                [
                    (1, "direct", "torznab", direct),
                    (2, "parent", "prowlarr", parent),
                    (3, "facade", "torznab", parent + ":17"),
                ],
            )
            add_block(
                db,
                series_id=1,
                url="https://blocked.test/original",
                title="one",
                reason="fixture",
                indexer="direct",
                protocol="torrent",
                size_bytes=1,
                identities=[
                    ReleaseIdentity(1, direct, "torrent", "Guid"),
                    ReleaseIdentity(1, parent + ":17", "nzb", "OtherGuid"),
                ],
            )
            context = SourceContext(
                1, direct, "torznab", None, "torrent", "interactive", 1
            )
            db.row_factory = sqlite3.Row
            handles = issue_selections(
                db,
                issuer=SelectionIssuer("api", None),
                now=now,
                snapshots=[
                    SelectionSnapshot(
                        1,
                        1,
                        context,
                        {
                            "url": f"https://download.test/{i}",
                            "guid": f"Guid-{i}",
                            "protocol": "torrent",
                            "title": "one",
                            "indexer": "direct",
                            "size_bytes": 1,
                            "seeders": 1,
                        },
                    )
                    for i in range(2)
                ],
            )
            assert len(handles) == 2
            db.row_factory = None
            durable = _durable_evidence(db)
            assert len(durable[0]) == 3 and len(durable[1]) == 2
        _, archive_path = create_backup_archive(
            db_path=str(source_db),
            backup_dir=str(source / "backups"),
            config_dir=str(source),
        )
        archive = Path(archive_path)
        snapshot = tmp_path / "archived.db"
        with zipfile.ZipFile(archive) as zf:
            snapshot.write_bytes(zf.read(DATABASE_ENTRY))
        with closing(sqlite3.connect(snapshot)) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 7
            assert (
                db.execute(
                    "SELECT count(*) FROM release_selection_evidence WHERE issued_at=? AND expires_at>?",
                    (now, now),
                ).fetchone()[0]
                == 2
            )
    else:
        with closing(sqlite3.connect(source_db)) as db, db:
            db.execute("CREATE TABLE legacy_state(value TEXT NOT NULL)")
            db.execute("INSERT INTO legacy_state VALUES('legacy')")
            db.execute("PRAGMA user_version=6")
        with zipfile.ZipFile(archive, "w") as zf:
            zf.write(source_db, DATABASE_ENTRY)

    target = tmp_path / "restore"
    target.mkdir()
    target_db = target / DATABASE_ENTRY
    with closing(sqlite3.connect(target_db)) as db, db:
        db.execute("CREATE TABLE previous_state(value TEXT)")
        db.execute("INSERT INTO previous_state VALUES('keep until confirmed')")
    previous_db = target_db.read_bytes()
    previous_key = Fernet.generate_key()
    (target / SECRET_KEY_ENTRY).write_bytes(previous_key)
    command, env = _operator_command(tmp_path)
    command += ["--config-dir", str(target), "backup", "restore", str(archive)]
    refused = subprocess.run(
        command,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert refused.returncode == 2
    assert "requires --yes" in refused.stderr
    assert target_db.read_bytes() == previous_db
    assert (target / SECRET_KEY_ENTRY).read_bytes() == previous_key
    assert not list(target.glob("*.pre-restore-*"))
    restored = subprocess.run(
        [*command, "--yes"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert restored.returncode == 0, restored.stderr
    assert "Backup restored" in restored.stdout
    with closing(sqlite3.connect(target_db)) as db:
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA user_version").fetchone()[0] == schema_version
        if schema_version == 7:
            assert (
                db.execute(
                    "SELECT count(*) FROM release_selection_evidence"
                ).fetchone()[0]
                == 0
            )
            assert _durable_evidence(db) == durable
        else:
            assert db.execute("SELECT value FROM legacy_state").fetchone() == (
                "legacy",
            )
            assert (
                db.execute(
                    "SELECT name FROM sqlite_schema WHERE name='release_selection_evidence'"
                ).fetchall()
                == []
            )
    expected_key = key if schema_version == 7 else previous_key
    assert (target / SECRET_KEY_ENTRY).read_bytes().strip() == expected_key
    rollback = list(target.glob(f"{DATABASE_ENTRY}.pre-restore-*"))
    assert len(rollback) == 1 and rollback[0].read_bytes() == previous_db


def test_admin_reset_uses_config_directory_and_revokes_sessions(tmp_path, capsys):
    db_path = tmp_path / "manga_arr.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE auth_admin(id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE auth_sessions(token_hash TEXT)")
        connection.execute("INSERT INTO auth_admin(id) VALUES(1)")
        connection.execute("INSERT INTO auth_sessions(token_hash) VALUES('session')")
    legacy_token = tmp_path / ".mangarr-setup-token"
    legacy_token.write_text("obsolete")

    result = cli.main(["--config-dir", str(tmp_path), "admin", "reset", "--yes"])

    assert result == 0
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM auth_admin").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0] == 0
        )
    assert not legacy_token.exists()
    assert "browser sessions revoked" in capsys.readouterr().out
