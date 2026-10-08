"""Additive FILE carrier metadata on fresh and schema-v5 databases."""

import sqlite3

from test_volume_file_deletion_journal import deletion_env as deletion_env


def _assert_shape(db_path: str) -> None:
    with sqlite3.connect(db_path) as db:
        columns = {
            r[1] for r in db.execute("PRAGMA table_info(import_publication_files)")
        }
        assert {"final_claim_carrier_json", "source_claim_carrier_json"} <= columns
        assert "claim_carrier_json" in {
            r[1] for r in db.execute("PRAGMA table_info(volume_file_deletions)")
        }
        assert [
            r[1] for r in db.execute("PRAGMA table_info(file_claim_namespaces)")
        ] == ["parent_path", "ownership_json"]


def test_fresh_schema_has_nullable_carrier_records(
    deletion_env: dict[str, object],
) -> None:
    import volume_file_deletion

    db_path = str(deletion_env["db_path"])
    _assert_shape(db_path)
    reservation = volume_file_deletion.reserve_volume_file_deletion(1, 11)
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT claim_carrier_json FROM volume_file_deletions WHERE id=?",
            (reservation.journal_id,),
        ).fetchone() == (None,)


def test_v5_migration_adds_metadata_without_rewriting_legacy_journal(
    deletion_env: dict[str, object],
) -> None:
    import schema
    import volume_file_deletion

    db_path = str(deletion_env["db_path"])
    reservation = volume_file_deletion.reserve_volume_file_deletion(1, 11)
    with sqlite3.connect(db_path) as db:
        before = db.execute(
            "SELECT state,target_path,claim_path,target_sha256 FROM volume_file_deletions"
        ).fetchone()
        for table, column in (
            ("import_publication_files", "final_claim_carrier_json"),
            ("import_publication_files", "source_claim_carrier_json"),
            ("volume_file_deletions", "claim_carrier_json"),
        ):
            if column in {r[1] for r in db.execute(f"PRAGMA table_info({table})")}:
                db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        db.execute("DROP TABLE IF EXISTS file_claim_namespaces")
        db.execute("PRAGMA user_version=5")
    schema._migrate_schema_constraints()
    _assert_shape(db_path)
    with sqlite3.connect(db_path) as db:
        assert (
            db.execute(
                "SELECT state,target_path,claim_path,target_sha256 FROM volume_file_deletions"
            ).fetchone()
            == before
        )
        assert db.execute(
            "SELECT claim_carrier_json FROM volume_file_deletions"
        ).fetchone() == (None,)
    assert reservation.journal_id is not None
    assert (
        volume_file_deletion.replay_volume_file_deletion(reservation.journal_id)
        == "completed"
    )
