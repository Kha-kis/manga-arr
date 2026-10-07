"""Real root-owned fsGroup layouts, qualified without touching user config."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0 or shutil.which("setpriv") is None,
    reason="actual root-owned fsGroup controls require root and setpriv",
)

_CHILD = r"""
import json, os, sqlite3, stat, subprocess, sys
from file_mutation_lock import FileMutationBusy, file_mutation_guard

path, layout, mode_text = sys.argv[1:]
config_mode = int(mode_text, 0)
parent = os.path.dirname(path)
before = os.stat(parent)
assert os.geteuid() == 1000
assert (before.st_uid, before.st_gid, stat.S_IMODE(before.st_mode)) == (0, 1000, config_mode)
if layout == 'wrong-group':
    try:
        with file_mutation_guard(path):
            raise AssertionError('wrong-group worker acquired')
    except PermissionError:
        print(json.dumps({'result': 'denied'}))
    else:
        raise AssertionError('wrong-group worker was not refused')
    sys.exit(0)

probe = '''import sqlite3,sys
c=sqlite3.connect(sys.argv[1],timeout=0.05)
try:
    c.execute('BEGIN IMMEDIATE')
except sqlite3.OperationalError as e:
    print('locked' if 'locked' in str(e) else str(e))
else:
    print('ACQUIRED')
    c.rollback()
finally:
    c.close()
'''
def competing_writer():
    result = subprocess.run([sys.executable, '-c', probe, path],
                            capture_output=True, text=True, check=True, timeout=5)
    return result.stdout.strip()

owner = sqlite3.connect(path, timeout=0.1)
try:
    owner.execute('BEGIN IMMEDIATE')
    owner.execute('UPDATE proof SET value=1')
    assert competing_writer() == 'locked'
    with file_mutation_guard(path) as guard:
        guard.verify()
        try:
            with file_mutation_guard(path):
                raise AssertionError('nested contender acquired')
        except FileMutationBusy:
            pass
        assert competing_writer() == 'locked', 'failed contender close lost SQLite lock'
    assert competing_writer() == 'locked', 'normal guard close lost SQLite lock'
    owner.commit()
    assert competing_writer() == 'ACQUIRED'
finally:
    owner.close()

lock = os.path.join(parent, '.local.db.file-mutation.lock')
identity = os.stat(lock)
expected_gid = 1000 if config_mode & stat.S_ISGID or layout == 'primary-group' else 1001
assert (identity.st_uid, identity.st_gid, stat.S_IMODE(identity.st_mode),
        identity.st_nlink, identity.st_size) == (1000, expected_gid, 0o600, 1, 0)
for _ in range(2):
    with file_mutation_guard(path) as guard:
        guard.verify()
        assert os.stat(lock).st_ino == identity.st_ino
after = os.stat(parent)
assert (after.st_dev, after.st_ino, after.st_uid, after.st_gid, after.st_mode) == (
        before.st_dev, before.st_ino, before.st_uid, before.st_gid, before.st_mode)
assert os.listdir(parent).count('.local.db.file-mutation.lock') == 1
print(json.dumps({'result': 'held', 'uid': os.geteuid(), 'gid': os.getegid(),
                  'groups': os.getgroups(), 'parent_uid': after.st_uid,
                  'parent_gid': after.st_gid, 'parent_mode': stat.S_IMODE(after.st_mode)}))
"""


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
@pytest.mark.parametrize(
    "layout", ["primary-group", "supplemental-group", "wrong-group"]
)
@pytest.mark.parametrize("config_mode", [0o770, 0o2770])
def test_root_owned_fsgroup_config_keeps_sqlite_and_guard_exclusion(
    journal_mode: str,
    layout: str,
    config_mode: int,
) -> None:
    app_dir = Path(__file__).resolve().parents[2] / "app"
    with tempfile.TemporaryDirectory(prefix="mangarr-fsgroup-guard-") as temp:
        outer = Path(temp)
        outer.chmod(0o755)
        parent = outer / "config"
        parent.mkdir()
        os.chown(parent, 0, 1000)
        parent.chmod(config_mode)
        db_path = parent / "local.db"
        with sqlite3.connect(db_path) as db:
            db.execute(f"PRAGMA journal_mode={journal_mode}")
            db.execute("CREATE TABLE proof(value)")
            db.execute("INSERT INTO proof VALUES(0)")
        db.close()
        os.chown(db_path, 1000, 1000)
        db_path.chmod(0o660)
        group_args = (
            ["--regid=1000", "--clear-groups"]
            if layout == "primary-group"
            else ["--regid=1001", "--groups=1000"]
            if layout == "supplemental-group"
            else ["--regid=1001", "--clear-groups"]
        )
        result = subprocess.run(
            [
                "setpriv",
                "--reuid=1000",
                *group_args,
                sys.executable,
                "-c",
                _CHILD,
                str(db_path),
                layout,
                oct(config_mode),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            env={
                **os.environ,
                "PYTHONPATH": str(app_dir),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
        assert result.returncode == 0, result.stderr
        proof = json.loads(result.stdout)
        assert proof["result"] == ("denied" if layout == "wrong-group" else "held")
        assert (
            parent.stat().st_uid,
            parent.stat().st_gid,
            parent.stat().st_mode & 0o7777,
        ) == (0, 1000, config_mode)
        if layout == "wrong-group":
            assert not (parent / ".local.db.file-mutation.lock").exists()
        else:
            assert proof["uid"] == 1000
            assert proof["parent_mode"] == config_mode
