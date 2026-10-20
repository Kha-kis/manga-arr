"""Schema-v6 ledger, compatibility gate, and SQL supersession fences."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(encoded: str) -> str:
    return hashlib.sha256(encoded.encode()).hexdigest()


@pytest.fixture
def ledger_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import schema
    import shared

    path = tmp_path / "identity.db"
    monkeypatch.setattr(shared, "DB_PATH", str(path))
    schema.init_db()
    return path


@pytest.fixture
def ledger_db(ledger_path: Path) -> Iterator[sqlite3.Connection]:
    with sqlite3.connect(ledger_path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        yield db


def insert_rescan(db: sqlite3.Connection, row_id: int = 1) -> None:
    legacy = '{ "legacy" : [1, 2] }\n'
    db.execute(
        "INSERT INTO rescan_file_operations(id,operation_token,series_id,volume_id,"
        "source_path,destination_path,expected_volume_json,expected_context_json,"
        "fingerprints_json,carriers_json,state) VALUES(?,?,7,?, '/library/source',"
        "'/library/destination',?,?,?,?,'prepared')",
        (row_id, str(row_id), 10 + row_id, legacy, legacy, legacy, legacy),
    )


def test_fresh_schema_v6_and_no_automatic_enrollment(
    ledger_db: sqlite3.Connection,
) -> None:
    assert ledger_db.execute("PRAGMA user_version").fetchone()[0] == 6
    for table in ("authorities", "epochs", "sets", "events"):
        assert (
            ledger_db.execute(f"SELECT * FROM file_identity_{table}").fetchall() == []
        )
    for table in ("rescan_file_operations", "volume_file_deletions"):
        fks = ledger_db.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        assert [(r[2], r[3], r[4], r[5], r[6]) for r in fks] == [
            (
                "file_identity_events",
                "recovery_event_id",
                "id",
                "NO ACTION",
                "NO ACTION",
            )
        ]


def test_v5_upgrade_and_idempotent_v6_preserve_legacy_bytes(ledger_path: Path) -> None:
    import schema

    with sqlite3.connect(ledger_path) as db:
        insert_rescan(db)
        db.execute(
            "INSERT INTO file_claim_namespaces VALUES('/library',?)",
            ('{ "old":true }\n',),
        )
        before_legacy_json = db.execute(
            "SELECT expected_volume_json,expected_context_json,fingerprints_json,carriers_json"
            " FROM rescan_file_operations"
        ).fetchall()
        # Recreate the exact pre-v6 additive boundary without rewriting old rows.
        for table in ("rescan_file_operations", "volume_file_deletions"):
            triggers = db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
                (table,),
            ).fetchall()
            for (name,) in triggers:
                if name.startswith("file_identity_"):
                    db.execute(f'DROP TRIGGER "{name}"')
            for (name,) in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?"
                " AND sql LIKE '%recovery_event_id%'",
                (table,),
            ).fetchall():
                db.execute(f'DROP INDEX "{name}"')
            db.execute(f"ALTER TABLE {table} DROP COLUMN recovery_event_id")
        for table in ("events", "sets", "epochs", "authorities"):
            db.execute(f"DROP TABLE file_identity_{table}")
        db.execute("PRAGMA user_version=5")
    schema._migrate_schema_constraints()
    schema.init_db()
    schema._migrate_schema_constraints()
    with sqlite3.connect(ledger_path) as db:
        after_legacy_json = db.execute(
            "SELECT expected_volume_json,expected_context_json,fingerprints_json,carriers_json"
            " FROM rescan_file_operations"
        ).fetchall()
        assert after_legacy_json == before_legacy_json
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6
        assert db.execute(
            "SELECT recovery_event_id FROM rescan_file_operations"
        ).fetchall() == [(None,)]
        assert db.execute(
            "SELECT ownership_json FROM file_claim_namespaces"
        ).fetchone() == ('{ "old":true }\n',)
        assert db.execute("SELECT * FROM file_identity_events").fetchall() == []
        # The idempotent old helper must not downgrade a v6 stamp.
        schema._migrate_history_download_ownership(db)
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6


# Frozen verbatim from schema.py at a4010b42b400bbbfffdb76936f0fed8e7f2a4ed8.
_V5_VALIDATOR = '''def _validate_schema_version(db: sqlite3.Connection) -> int:
    """Return the DB schema version, rejecting versions this binary cannot read."""
    version = cast(int, db.execute("PRAGMA user_version").fetchone()[0])
    if version > _SUPPORTED_SCHEMA_VERSION:
        message = (
            f"database schema version {version} is newer than this Mangarr version"
        )
        message += (
            f" supports ({_SUPPORTED_SCHEMA_VERSION}); refusing to modify it"
        )
        raise RuntimeError(message)
    return version
'''


def test_v5_binary_rejects_v6_before_any_write(ledger_path: Path) -> None:
    with sqlite3.connect(ledger_path) as db:
        insert_rescan(db)
    with sqlite3.connect(ledger_path) as db:
        before = tuple(db.iterdump())
    program = (
        "import sqlite3, sys\nfrom typing import cast\n_SUPPORTED_SCHEMA_VERSION=5\n"
        + _V5_VALIDATOR
        + "\ndb=sqlite3.connect(sys.argv[1])\nstatements=[]\ndb.set_trace_callback(statements.append)\n"
        "db.execute('BEGIN IMMEDIATE')\ntry:\n _validate_schema_version(db)\n"
        " db.execute(\"INSERT INTO settings VALUES('old-writer','unsafe')\")\n"
        "except RuntimeError as exc:\n db.rollback()\n"
        " assert 'refusing to modify' in str(exc)\n"
        " assert statements == ['BEGIN IMMEDIATE','PRAGMA user_version','ROLLBACK'], statements\n"
        "else:\n raise AssertionError('v5 writer accepted v6')\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", program, str(ledger_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(ledger_path) as db:
        assert tuple(db.iterdump()) == before


@pytest.mark.parametrize("table", ["rescan_file_operations", "volume_file_deletions"])
def test_journal_insert_cannot_bypass_reservation_with_recovery_id(
    ledger_db: sqlite3.Connection, table: str
) -> None:
    if table == "rescan_file_operations":
        insert_rescan(ledger_db)
    else:
        ledger_db.execute(
            "INSERT INTO volume_file_deletions(volume_id,series_id,state,target_path,"
            "parent_path,claim_path,target_present,series_title)"
            " VALUES(11,7,'active','/library/a','/library','/library/claim',1,'test')"
        )
    columns = [r[1] for r in ledger_db.execute(f"PRAGMA table_info({table})")]
    values = list(ledger_db.execute(f"SELECT * FROM {table}").fetchone())
    values[columns.index("id")] = 2
    if "operation_token" in columns:
        values[columns.index("operation_token")] = "second"
    values[columns.index("recovery_event_id")] = 999
    with pytest.raises(sqlite3.IntegrityError):
        ledger_db.execute(
            f"INSERT INTO {table} VALUES({','.join('?' for _ in values)})", values
        )
