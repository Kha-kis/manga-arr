"""Additive rescan journal migration, including the existing v5 fast path."""

import sqlite3

import pytest

from test_rescan_transactions import rescan_env as rescan_env


COLUMNS = [
    "id",
    "version",
    "operation_token",
    "series_id",
    "volume_id",
    "source_path",
    "destination_path",
    "expected_volume_json",
    "expected_context_json",
    "fingerprints_json",
    "carriers_json",
    "publication_receipt_json",
    "state",
    "diagnostic",
    "created_at",
    "updated_at",
]


def _insert(db: sqlite3.Connection, token: str, state: str = "prepared") -> None:
    db.execute(
        "INSERT INTO rescan_file_operations(operation_token,series_id,volume_id,"
        "source_path,destination_path,expected_volume_json,expected_context_json,"
        "fingerprints_json,carriers_json,state) VALUES(?,7,99,'/source','/dest',"
        "'{}','{}','{}','{}',?)",
        (token, state),
    )


def test_fresh_journal_has_exact_columns_and_no_cascading_authority(rescan_env):
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert [
            r[1] for r in db.execute("PRAGMA table_info(rescan_file_operations)")
        ] == COLUMNS
        assert list(db.execute("PRAGMA foreign_key_list(rescan_file_operations)")) == []
        _insert(db, "first")
        db.execute("DELETE FROM series WHERE id=7")
        assert db.execute(
            "SELECT operation_token FROM rescan_file_operations"
        ).fetchone() == ("first",)


@pytest.mark.parametrize("state", ["prepared", "published", "db_committed", "rollback"])
def test_every_nonterminal_state_reserves_volume(rescan_env, state):
    with sqlite3.connect(rescan_env["db_path"]) as db:
        _insert(db, "first", state)
        with pytest.raises(sqlite3.IntegrityError):
            _insert(db, "second")


@pytest.mark.parametrize("state", ["completed", "rolled_back"])
def test_terminal_operations_allow_new_reservation(rescan_env, state):
    with sqlite3.connect(rescan_env["db_path"]) as db:
        _insert(db, "first", state)
        _insert(db, "second")
        assert db.execute("SELECT COUNT(*) FROM rescan_file_operations").fetchone() == (
            2,
        )


def test_v5_upgrade_and_repeat_init_preserve_other_owned_columns_and_records(
    rescan_env,
):
    import schema

    with sqlite3.connect(rescan_env["db_path"]) as db:
        db.execute("DROP TABLE IF EXISTS rescan_file_operations")
        db.execute("ALTER TABLE series ADD COLUMN independent_policy TEXT")
        db.execute("UPDATE series SET independent_policy='retain' WHERE id=7")
        db.execute("INSERT INTO file_claim_namespaces VALUES('/unrecorded',NULL)")
        db.execute("PRAGMA user_version=5")
    schema._migrate_schema_constraints()
    with sqlite3.connect(rescan_env["db_path"]) as db:
        _insert(db, "durable")
        before = db.execute("SELECT * FROM rescan_file_operations").fetchall()
    schema._migrate_schema_constraints()
    schema.init_db()
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert db.execute("SELECT * FROM rescan_file_operations").fetchall() == before
        assert db.execute(
            "SELECT independent_policy FROM series WHERE id=7"
        ).fetchone() == ("retain",)
        assert db.execute("SELECT * FROM file_claim_namespaces").fetchall() == [
            ("/unrecorded", None)
        ]
        assert "claim_carrier_json" in {
            r[1] for r in db.execute("PRAGMA table_info(volume_file_deletions)")
        }
