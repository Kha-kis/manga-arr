"""Explicit pytest child entry point; not collected by the parent suite."""

from __future__ import annotations

from contextlib import closing
import fcntl
import os
from pathlib import Path
import sqlite3

import pytest

from test_file_identity_schema import ledger_path as ledger_path
from test_file_identity_observation_sql import (
    _assert_sql_no_application_effects,
    _fd_targets,
    fds,
    namespace as namespace,
)


@pytest.mark.parametrize("journal", ["delete", "wal"])
def test_sql_no_effects_process(
    ledger_path: Path,
    namespace: Path,
    journal: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_root = Path(os.environ["TMPDIR"]).parent
    assert ledger_path.is_relative_to(child_root / "basetemp")
    assert Path(os.environ["MANGARR_CONFIG_DIR"]).is_relative_to(child_root)
    before_owned = fds()
    sentinel_path = ledger_path.parent / "held-sentinel"
    with sentinel_path.open("w+b") as sentinel, sentinel_path.open("r+b") as contender:
        with closing(sqlite3.connect(ledger_path.parent / "rooted-sentinel.db")) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE sentinel(value)")
            db.commit()
            fcntl.flock(sentinel, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = _fd_targets()
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _assert_sql_no_application_effects(
                ledger_path, namespace, journal, monkeypatch
            )
            assert _fd_targets() == before
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert sentinel.closed and contender.closed
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        db.execute("SELECT 1")
    assert fds() == before_owned
