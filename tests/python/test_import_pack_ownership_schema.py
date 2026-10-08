"""The pack adapter adds only nullable proofs to existing journals."""

from __future__ import annotations

import sqlite3

import pytest

from test_import_pack_cleanup_durability import _PackEnv, pack_env  # noqa: F401


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("import_pack_cleanup_reservations", "directory_ownership_json"),
        ("import_pack_cleanup_tombstones", "carrier_json"),
    ],
)
def test_pack_proof_columns_are_nullable_text(
    pack_env: _PackEnv, table: str, column: str
) -> None:
    with sqlite3.connect(pack_env["db_path"]) as db:
        columns = {row[1]: row for row in db.execute(f"PRAGMA table_info({table})")}
    assert column in columns
    assert columns[column][2:5] == ("TEXT", 0, None)


def test_pack_upgrade_preserves_legacy_rows_and_is_idempotent(
    pack_env: _PackEnv,
) -> None:
    import import_pack_cleanup
    import main

    with main.get_db() as db:
        owner = import_pack_cleanup.reserve_pack_queue_creation(
            db, "legacy-schema", download_client_id=None, protocol=None
        )
        before = dict(
            db.execute("SELECT * FROM import_pack_cleanup_reservations").fetchone()
        )
        columns = {
            r[1]
            for r in db.execute("PRAGMA table_info(import_pack_cleanup_reservations)")
        }
        assert "directory_ownership_json" in columns
        db.execute(
            "ALTER TABLE import_pack_cleanup_reservations DROP COLUMN directory_ownership_json"
        )
        db.execute(
            "ALTER TABLE import_pack_cleanup_tombstones DROP COLUMN carrier_json"
        )
    assert owner is not None
    main.init_db()
    main.init_db()
    with main.get_db() as db:
        after = dict(
            db.execute("SELECT * FROM import_pack_cleanup_reservations").fetchone()
        )
        assert after == before
        assert after["directory_ownership_json"] is None
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
