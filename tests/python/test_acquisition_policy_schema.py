"""Policy migration is additive on the historical domain tables that exist."""

import sqlite3

import pytest


@pytest.mark.parametrize("table", ["seen", "import_queue"])
def test_policy_migration_skips_absent_domain_without_creating_or_backfilling(
    table: str,
) -> None:
    import schema

    with sqlite3.connect(":memory:") as db:
        db.execute(f"CREATE TABLE {table}(id INTEGER PRIMARY KEY, download_id TEXT)")
        db.execute(f"INSERT INTO {table}(id,download_id) VALUES(1,'legacy-download')")
        schema._ensure_acquisition_policy_columns(db)
        schema._ensure_acquisition_policy_columns(db)
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall() == [(table,)]
        assert db.execute(
            f"SELECT id,download_id,respect_grab_claims FROM {table}"
        ).fetchall() == [(1, "legacy-download", None)]
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(f"UPDATE {table} SET respect_grab_claims=2 WHERE id=1")
        columns = {row[1]: row for row in db.execute(f"PRAGMA table_info({table})")}
        assert columns["respect_grab_claims"][3:5] == (0, None)
