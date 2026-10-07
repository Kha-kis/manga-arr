"""Guard descriptor closure must not release SQLite's POSIX writer locks."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
@pytest.mark.parametrize(
    "boundary", ["normal-exit", "contender-close", "hardlink-sidecar"]
)
def test_closing_guard_preserves_another_sqlite_connections_writer_lock(
    tmp_path: Path,
    journal_mode: str,
    boundary: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from file_mutation_lock import (
        FileMutationBusy,
        FileMutationLockError,
        file_mutation_guard,
    )

    path = tmp_path / "local.db"
    owner = sqlite3.connect(path, timeout=0.1)
    try:
        owner.execute(f"PRAGMA journal_mode={journal_mode}")
        owner.execute("CREATE TABLE proof(value)")
        owner.execute("INSERT INTO proof VALUES(0)")
        owner.commit()
        owner.execute("BEGIN IMMEDIATE")
        owner.execute("UPDATE proof SET value=1")
        code = """import sqlite3,sys
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
"""

        def contender() -> str:
            result = subprocess.run(
                [sys.executable, "-c", code, str(path)],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            return result.stdout.strip()

        assert contender() == "locked", "control: writer exclusion was not held"
        if boundary == "hardlink-sidecar":
            path.chmod(0o600)
            lock_path = tmp_path / ".local.db.file-mutation.lock"
            os.link(path, lock_path)
            real_open = os.open

            def refuse_alias_open(
                open_path: Any, flags: int, *args: Any, **kwargs: Any
            ) -> int:
                assert os.fspath(open_path) != str(lock_path), (
                    "SQLite alias opened before rejection"
                )
                return real_open(open_path, flags, *args, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(os, "open", refuse_alias_open)
                with pytest.raises(FileMutationLockError):
                    with file_mutation_guard(str(path)):
                        pytest.fail("sidecar alias of SQLite inode accepted")
        else:
            with file_mutation_guard(str(path)) as guard:
                guard.verify()
                if boundary == "contender-close":
                    with pytest.raises(FileMutationBusy):
                        with file_mutation_guard(str(path)):
                            pytest.fail("nested contender acquired")
                    assert contender() == "locked", (
                        "busy contender close released SQLite writer exclusion"
                    )
        assert contender() == "locked", "guard close released SQLite writer exclusion"
        owner.commit()
        assert contender() == "ACQUIRED", "SQLite did not release after its own commit"
    finally:
        owner.rollback()
        owner.close()
