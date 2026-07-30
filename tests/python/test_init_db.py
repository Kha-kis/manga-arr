"""Regression test for init_db on a fresh /config.

History: init_db() previously called add_col('chapters', 'quality', 'TEXT')
*before* CREATE TABLE chapters. On a fresh DB the ALTER TABLE failed,
get_db rolled back the surrounding transaction, and no schema was created
at all — leaving the app unusable on first boot.

This test runs init_db against a brand-new, empty database file and
asserts that all expected tables (and a couple of late-added columns)
are present afterwards.
"""
import os
import sqlite3
import tempfile
import threading
from collections.abc import Callable

import pytest


# Tables that init_db is responsible for creating. Not exhaustive — just
# enough to catch a transaction rollback that swallows the whole schema.
EXPECTED_TABLES = {
    "settings",
    "auth_admin",
    "auth_sessions",
    "series",
    "volumes",
    "seen",
    "events",
    "root_folders",
    "blocklist",
    "history",
    "import_queue",
    "import_queue_files",
    "import_publications",
    "import_publication_files",
    "import_publication_notifications",
    "import_pack_cleanup_reservations",
    "import_pack_cleanup_tombstones",
    "volume_file_deletions",
    "series_aliases",
    "pending_releases",
    "chapters",
    "quality_profiles",
    "custom_formats",
    "release_profiles",
    "delay_profiles",
    "download_clients",
    "indexers",
    "notification_connections",
    "import_lists",
    "import_list_exclusions",
    "language_profiles",
    "quality_definitions",
    "remote_path_mappings",
    "mangadex_chapters",
    "suwayomi_downloads",
    "suwayomi_sources",
}

_V4_SAFETY_COLUMNS = (
    "source_sha256",
    "source_claim_path",
    "final_expected_absent",
    "prepared_final_dev",
    "prepared_final_inode",
    "prepared_final_size",
    "prepared_final_mtime_ns",
    "prepared_final_sha256",
    "final_claim_path",
)
_DOWNLOAD_CLIENT_OWNERSHIP_COLUMNS = {
    "seen": "download_client_id",
    "volumes": "download_client_id",
    "chapters": "download_client_id",
    "history": "download_client_id",
    "import_queue": "download_client_id",
    "import_publications": "queue_download_client_id",
}


@pytest.fixture
def fresh_db(monkeypatch):
    """Point main.DB_PATH at an empty tmp file and yield it."""
    import main

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.unlink(tmp.name)  # init_db will create it from scratch

    monkeypatch.setattr(main, "DB_PATH", tmp.name)
    # shared.get_db reads its own DB_PATH constant; patch there too so the
    # context manager points at the temp file.
    import shared
    monkeypatch.setattr(shared, "DB_PATH", tmp.name)

    try:
        yield tmp.name
    finally:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)
        # Also clean up the WAL/SHM sidecars that sqlite may have written.
        for ext in ("-wal", "-shm"):
            p = tmp.name + ext
            if os.path.exists(p):
                os.unlink(p)


def _database_state(db_path: str) -> tuple[tuple[str, ...], dict[str, object]]:
    """Return logical schema/data and persistent pragma state.

    SQLite may create or remove WAL, SHM, or rollback-journal sidecars while
    opening and rolling back connections. Those files are engine bookkeeping;
    this snapshot intentionally asserts the logical database state instead.
    """
    pragma_names = (
        "application_id",
        "auto_vacuum",
        "encoding",
        "freelist_count",
        "journal_mode",
        "page_size",
        "schema_version",
        "user_version",
    )
    with sqlite3.connect(db_path) as conn:
        dump = tuple(conn.iterdump())
        pragmas = {
            name: conn.execute(f"PRAGMA {name}").fetchone()[0]
            for name in pragma_names
        }
    return dump, pragmas


def _assert_download_protocol_check(conn: sqlite3.Connection) -> None:
    """Assert the queue accepts only the same protocol domain as fresh DDL."""
    conn.execute("SAVEPOINT protocol_check_probe")
    try:
        for protocol in (None, "torrent", "nzb"):
            conn.execute(
                "INSERT INTO import_queue(download_id,download_protocol)"
                " VALUES('protocol-check-probe',?)",
                (protocol,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO import_queue(download_id,download_protocol)"
                " VALUES('protocol-check-probe','usenet')"
            )
    finally:
        conn.execute("ROLLBACK TO protocol_check_probe")
        conn.execute("RELEASE protocol_check_probe")


def _assert_query_uses_index(
    conn: sqlite3.Connection,
    *,
    sql: str,
    params: tuple[object, ...],
    index_name: str,
) -> None:
    plan = conn.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    assert any(index_name in row[3] for row in plan), plan


def _create_historical_v2_database(db_path: str) -> None:
    """Create the actual pre-publication-journal schema needed by v2→v5."""
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT,
                series_id INTEGER,
                message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                series_id INTEGER,
                series_title TEXT,
                volume_label TEXT,
                source_title TEXT,
                indexer TEXT,
                protocol TEXT,
                client TEXT,
                download_id TEXT,
                size_bytes INTEGER,
                release_group TEXT,
                data TEXT,
                torrent_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE import_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id INTEGER,
                download_id TEXT,
                torrent_name TEXT,
                torrent_url TEXT,
                volume_num REAL,
                src_dir TEXT,
                status TEXT DEFAULT 'pending',
                failed_at TIMESTAMP,
                lease_owner TEXT,
                lease_expires_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE notification_connections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                on_download INTEGER DEFAULT 1
            );
            PRAGMA user_version = 2;
            """
        )


def _create_historical_v3_database(
    db_path: str,
    *,
    publication_state: str | None = None,
) -> None:
    """Create the unreleased v3 journal before v4 safety fields/outboxes."""
    _create_historical_v2_database(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            ALTER TABLE import_queue ADD COLUMN download_client_id INTEGER;
            CREATE TABLE import_publications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                queue_id INTEGER NOT NULL UNIQUE,
                state TEXT NOT NULL,
                owner_token TEXT NOT NULL,
                operation_owner TEXT,
                operation_expires_at TEXT,
                series_id INTEGER NOT NULL,
                dst_dir TEXT NOT NULL,
                import_mode TEXT NOT NULL,
                staging_dir TEXT NOT NULL,
                queue_snapshot_json TEXT NOT NULL,
                series_snapshot_json TEXT,
                series_tags_json TEXT NOT NULL,
                queue_status TEXT NOT NULL,
                queue_download_id TEXT,
                queue_torrent_name TEXT,
                queue_torrent_url TEXT,
                queue_volume_num REAL,
                queue_src_dir TEXT,
                queue_failed_at TEXT,
                queue_lease_owner TEXT,
                queue_lease_expires_at TEXT,
                queue_created_at TEXT,
                result_ok INTEGER,
                result_imported_count INTEGER,
                result_queue_status TEXT,
                diagnostic TEXT NOT NULL DEFAULT '',
                notification_state TEXT NOT NULL DEFAULT 'none',
                notification_title TEXT,
                notification_label TEXT,
                notification_cover_url TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                prepared_at TEXT,
                publishing_at TEXT,
                published_at TEXT,
                db_committed_at TEXT,
                cleaning_at TEXT,
                finalized_at TEXT,
                deleted_at TEXT
            );
            CREATE TABLE import_publication_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                publication_id INTEGER NOT NULL
                    REFERENCES import_publications(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                file_id INTEGER NOT NULL,
                src_path TEXT NOT NULL,
                filename TEXT NOT NULL,
                dst_path TEXT NOT NULL,
                import_kind TEXT NOT NULL,
                file_type TEXT NOT NULL,
                proposed_vol REAL,
                proposed_chap REAL,
                chap_range_end REAL,
                vol_range_start REAL,
                vol_range_end REAL,
                pack_type TEXT,
                is_special INTEGER NOT NULL,
                special_title TEXT,
                has_volume_range INTEGER NOT NULL,
                is_legacy_chapter_stub INTEGER NOT NULL,
                is_legacy_chapter_recheck INTEGER NOT NULL,
                plan_status TEXT NOT NULL,
                plan_failure_reason TEXT NOT NULL DEFAULT '',
                stage_ok INTEGER,
                stage_error TEXT NOT NULL DEFAULT '',
                stage_path TEXT,
                final_path TEXT,
                source_dev INTEGER,
                source_inode INTEGER,
                source_size INTEGER,
                source_mtime_ns INTEGER,
                staged_dev INTEGER,
                staged_inode INTEGER,
                staged_size INTEGER,
                staged_mtime_ns INTEGER,
                staged_sha256 TEXT,
                stage_state TEXT NOT NULL DEFAULT 'pending',
                publish_state TEXT NOT NULL DEFAULT 'pending',
                cleanup_state TEXT NOT NULL DEFAULT 'pending',
                diagnostic TEXT NOT NULL DEFAULT '',
                staged_at TEXT,
                published_at TEXT,
                cleaned_at TEXT,
                UNIQUE(publication_id, ordinal),
                UNIQUE(publication_id, file_id)
            );
            CREATE INDEX idx_import_publications_state
                ON import_publications(state, id);
            CREATE INDEX idx_import_publication_files_publication
                ON import_publication_files(publication_id, ordinal);
            CREATE TABLE import_pack_cleanup_reservations (
                normalized_download_id TEXT PRIMARY KEY,
                download_id TEXT NOT NULL,
                purpose TEXT NOT NULL,
                owner_token TEXT NOT NULL,
                queue_id INTEGER,
                publication_id INTEGER,
                pack_path TEXT NOT NULL,
                tombstone_path TEXT,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE import_pack_cleanup_tombstones (
                tombstone_path TEXT PRIMARY KEY,
                normalized_download_id TEXT NOT NULL,
                download_id TEXT NOT NULL,
                queue_id INTEGER NOT NULL,
                publication_id INTEGER,
                pack_path TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX idx_import_pack_reservations_expiry
                ON import_pack_cleanup_reservations(
                    expires_at, normalized_download_id
                );
            CREATE INDEX idx_import_pack_tombstones_publication
                ON import_pack_cleanup_tombstones(publication_id, created_at);
            PRAGMA user_version = 3;
            """
        )
    if publication_state is not None:
        _seed_v3_publication(db_path, publication_state)


def _create_historical_v4_database(db_path: str) -> None:
    """Create v4 after durable safety/outbox migration but before ownership."""
    _create_historical_v3_database(db_path)
    with sqlite3.connect(db_path) as conn:
        for column in _V4_SAFETY_COLUMNS:
            typedef = "INTEGER" if column not in {
                "source_sha256",
                "source_claim_path",
                "prepared_final_sha256",
                "final_claim_path",
            } else "TEXT"
            conn.execute(
                f"ALTER TABLE import_publication_files"
                f" ADD COLUMN {column} {typedef}"
            )
        conn.executescript(
            """
            ALTER TABLE import_publications
                ADD COLUMN pack_cleanup_state
                TEXT NOT NULL DEFAULT 'retained';
            ALTER TABLE import_publications
                ADD COLUMN pack_cleanup_completed_at TEXT;
            ALTER TABLE import_publications
                ADD COLUMN queue_download_client_id INTEGER;
            CREATE TABLE import_publication_notifications (
                publication_id INTEGER PRIMARY KEY,
                state TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                label TEXT NOT NULL,
                cover_url TEXT NOT NULL DEFAULT '',
                operation_owner TEXT,
                operation_expires_at TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                dispatched_at TEXT
            );
            CREATE TABLE import_publication_notification_deliveries (
                publication_id INTEGER NOT NULL,
                connection_id INTEGER NOT NULL,
                connection_name TEXT NOT NULL,
                connection_type TEXT NOT NULL,
                state TEXT NOT NULL,
                completion_reason TEXT,
                operation_owner TEXT,
                operation_expires_at TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT,
                PRIMARY KEY(publication_id, connection_id)
            );
            CREATE TABLE import_publication_success_effects (
                publication_id INTEGER NOT NULL,
                effect_type TEXT NOT NULL,
                state TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                operation_owner TEXT,
                operation_expires_at TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT,
                PRIMARY KEY(publication_id, effect_type)
            );
            CREATE TABLE volume_file_deletions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                volume_id INTEGER NOT NULL,
                series_id INTEGER NOT NULL,
                state TEXT NOT NULL,
                target_path TEXT NOT NULL,
                parent_path TEXT NOT NULL,
                claim_path TEXT NOT NULL,
                target_present INTEGER NOT NULL,
                target_dev INTEGER,
                target_inode INTEGER,
                target_size INTEGER,
                target_mtime_ns INTEGER,
                target_sha256 TEXT,
                series_title TEXT NOT NULL,
                volume_num REAL,
                source_title TEXT NOT NULL DEFAULT '',
                original_import_path TEXT NOT NULL DEFAULT '',
                diagnostic TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT
            );
            PRAGMA user_version = 4;
            """
        )


def _seed_v3_publication(db_path: str, state: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO import_publications(
                queue_id, state, owner_token, series_id, dst_dir,
                import_mode, staging_dir, queue_snapshot_json,
                series_snapshot_json, series_tags_json, queue_status
            ) VALUES(1, ?, 'v3-owner', 1, '/library/Series', 'copy',
                     '/library/Series/.mangarr-publication-1',
                     '{"id":1}', NULL, '[]', 'importing')
            """,
            (state,),
        )


def _interleave_future_schema_writer(
    monkeypatch,
    db_path: str,
    write_future_state: Callable[[sqlite3.Connection], None],
) -> tuple[Callable[[], None], list[bool]]:
    """Start a future-schema writer immediately after version validation."""
    import schema

    original_validate = schema._validate_schema_version
    attempted = threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []
    lock_states: list[bool] = []
    thread: threading.Thread | None = None

    def writer() -> None:
        try:
            with sqlite3.connect(db_path, timeout=10) as conn:
                conn.execute("PRAGMA busy_timeout=10000")
                conn.execute("PRAGMA ignore_check_constraints=ON")
                attempted.set()
                conn.execute("BEGIN IMMEDIATE")
                write_future_state(conn)
                conn.execute("PRAGMA user_version = 999")
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    def validate_with_interleave(db: sqlite3.Connection) -> int:
        nonlocal thread
        version = original_validate(db)
        if thread is None:
            lock_states.append(db.in_transaction)
            thread = threading.Thread(target=writer, daemon=True)
            thread.start()
            assert attempted.wait(2), "future-schema writer did not start"
            if not db.in_transaction:
                assert finished.wait(5), "unlocked future-schema writer did not finish"
        return version

    def finish() -> None:
        assert thread is not None, "schema validation hook was not reached"
        thread.join(15)
        assert not thread.is_alive(), "future-schema writer remained blocked"
        assert not errors, errors

    monkeypatch.setattr(schema, "_validate_schema_version", validate_with_interleave)
    return finish, lock_states


def test_init_db_succeeds_on_fresh_database(fresh_db):
    """init_db must run cleanly against an empty file and create the schema."""
    import main
    main.init_db()  # must not raise

    conn = sqlite3.connect(fresh_db)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        actual = {r[0] for r in rows}
    finally:
        conn.close()

    missing = EXPECTED_TABLES - actual
    assert not missing, f"init_db left these tables uncreated: {sorted(missing)}"


def test_init_db_creates_chapters_quality_and_imported_at(fresh_db):
    """The two add_col calls that triggered the original bug must run.
    On a fresh DB, chapters.quality and chapters.imported_at are added by
    add_col — not by the CREATE TABLE — so they're a sensitive indicator
    that the post-CREATE add_col block executed."""
    import main
    main.init_db()

    conn = sqlite3.connect(fresh_db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(chapters)").fetchall()}
    finally:
        conn.close()

    assert "quality" in cols, f"chapters.quality missing; got {sorted(cols)}"
    assert "imported_at" in cols, f"chapters.imported_at missing; got {sorted(cols)}"


def test_init_db_creates_nullable_download_client_ownership_lineage(fresh_db):
    """Fresh rows can preserve exact ownership without forcing legacy guesses."""
    import main

    main.init_db()
    with sqlite3.connect(fresh_db) as conn:
        columns = {
            table: {
                row[1]: row
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for table in _DOWNLOAD_CLIENT_OWNERSHIP_COLUMNS
        }

    for table, column in _DOWNLOAD_CLIENT_OWNERSHIP_COLUMNS.items():
        assert column in columns[table]
        assert columns[table][column][3] == 0
        assert columns[table][column][4] is None


def test_fresh_v5_pack_journal_is_owner_and_protocol_qualified(fresh_db):
    import main

    main.init_db()
    with sqlite3.connect(fresh_db) as conn:
        reservation_columns = {
            row[1]: row
            for row in conn.execute(
                "PRAGMA table_info(import_pack_cleanup_reservations)"
            ).fetchall()
        }
        tombstone_columns = {
            row[1]: row
            for row in conn.execute(
                "PRAGMA table_info(import_pack_cleanup_tombstones)"
            ).fetchall()
        }
        queue_columns = {
            row[1]: row
            for row in conn.execute("PRAGMA table_info(import_queue)").fetchall()
        }

    identity_columns = {
        "download_identity_key",
        "download_client_id",
        "protocol",
        "normalized_download_id",
    }
    assert identity_columns <= reservation_columns.keys()
    assert identity_columns <= tombstone_columns.keys()
    assert reservation_columns["download_identity_key"][3] == 1
    assert reservation_columns["download_identity_key"][5] == 1
    assert queue_columns["download_protocol"][3] == 0
    assert queue_columns["download_protocol"][4] is None


def test_fresh_import_queue_rejects_invalid_download_protocol(fresh_db: str) -> None:
    import main

    main.init_db()
    with sqlite3.connect(fresh_db) as conn:
        _assert_download_protocol_check(conn)
        _assert_query_uses_index(
            conn,
            sql=(
                "SELECT * FROM import_queue"
                " WHERE download_id=? COLLATE NOCASE"
            ),
            params=("abcdef",),
            index_name="idx_import_queue_dlid_nocase",
        )


def test_v4_history_ownership_migration_rebuilds_queue_with_protocol_check(
    fresh_db: str,
):
    """A v4 database preserves rows while enforcing the fresh queue contract."""
    import main

    _create_historical_v4_database(fresh_db)
    with sqlite3.connect(fresh_db) as conn:
        conn.execute(
            "INSERT INTO history(event_type, download_id)"
            " VALUES('grabbed', 'legacy-download')"
        )
        conn.execute(
            "INSERT INTO import_queue(download_id,torrent_name)"
            " VALUES('legacy-queue','Legacy Queue')"
        )
        assert "download_client_id" not in {
            row[1]
            for row in conn.execute("PRAGMA table_info(history)").fetchall()
        }
        assert "download_protocol" not in {
            row[1]
            for row in conn.execute("PRAGMA table_info(import_queue)").fetchall()
        }

    main._migrate_schema_constraints()

    with sqlite3.connect(fresh_db) as conn:
        columns = {
            row[1]: row
            for row in conn.execute("PRAGMA table_info(history)").fetchall()
        }
        row = conn.execute(
            "SELECT download_id, download_client_id FROM history"
            " WHERE download_id='legacy-download'"
        ).fetchone()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        reservation_columns = {
            column[1]
            for column in conn.execute(
                "PRAGMA table_info(import_pack_cleanup_reservations)"
            ).fetchall()
        }
        queue_row = conn.execute(
            "SELECT torrent_name,download_protocol FROM import_queue"
            " WHERE download_id='legacy-queue'"
        ).fetchone()
        _assert_download_protocol_check(conn)
        _assert_query_uses_index(
            conn,
            sql=(
                "SELECT * FROM import_queue"
                " WHERE download_id=? COLLATE NOCASE"
            ),
            params=("LEGACY-QUEUE",),
            index_name="idx_import_queue_dlid_nocase",
        )
    assert columns["download_client_id"][3] == 0
    assert columns["download_client_id"][4] is None
    assert row == ("legacy-download", None)
    assert "download_identity_key" in reservation_columns
    assert "download_client_id" in reservation_columns
    assert "protocol" in reservation_columns
    assert queue_row == ("Legacy Queue", None)
    assert version == 5


def test_v4_invalid_download_protocol_blocks_migration_and_rolls_back(
    fresh_db: str,
) -> None:
    """Unrecognized legacy values fail closed without partial schema changes."""
    import main

    _create_historical_v4_database(fresh_db)
    with sqlite3.connect(fresh_db) as conn:
        conn.execute(
            "ALTER TABLE import_queue ADD COLUMN download_protocol TEXT"
        )
        conn.execute(
            "INSERT INTO import_queue(download_id,download_protocol)"
            " VALUES('invalid-protocol','usenet')"
        )
    before = _database_state(fresh_db)

    with pytest.raises(
        RuntimeError,
        match=r"download_protocol CHECK.*1 invalid value",
    ):
        main._migrate_schema_constraints()

    assert _database_state(fresh_db) == before


def test_existing_history_only_v5_gets_qualified_pack_journals(
    fresh_db: str,
) -> None:
    import main

    _create_historical_v4_database(fresh_db)
    with sqlite3.connect(fresh_db) as conn:
        conn.execute("ALTER TABLE history ADD COLUMN download_client_id INTEGER")
        conn.execute(
            "INSERT INTO import_queue(download_id,torrent_name)"
            " VALUES('v5-legacy-queue','Keep V5 Queue')"
        )
        conn.execute("PRAGMA user_version = 5")

    main._migrate_schema_constraints()

    with sqlite3.connect(fresh_db) as conn:
        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(import_pack_cleanup_reservations)"
            ).fetchall()
        }
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        queue_row = conn.execute(
            "SELECT torrent_name,download_protocol FROM import_queue"
            " WHERE download_id='v5-legacy-queue'"
        ).fetchone()
        _assert_download_protocol_check(conn)
    assert {
        "download_identity_key",
        "download_client_id",
        "protocol",
    } <= columns
    assert queue_row == ("Keep V5 Queue", None)
    assert version == 5


@pytest.mark.parametrize("journal", ("reservation", "tombstone"))
def test_v4_pack_journal_migration_fails_closed_when_work_exists(
    fresh_db: str,
    journal: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-write v5 failure rolls back schema, data, and version exactly."""
    import main
    import schema

    _create_historical_v4_database(fresh_db)
    with sqlite3.connect(fresh_db) as conn:
        if journal == "reservation":
            conn.execute(
                """
                INSERT INTO import_pack_cleanup_reservations(
                    normalized_download_id, download_id, purpose, owner_token,
                    pack_path, expires_at
                ) VALUES(
                    'shared-id', 'SHARED-ID', 'queueing', 'old-owner',
                    '/config/import-packs/queue-SHARED-ID',
                    datetime('now', '+5 minutes')
                )
                """
            )
        else:
            conn.execute(
                """
                INSERT INTO import_pack_cleanup_tombstones(
                    tombstone_path, normalized_download_id, download_id,
                    queue_id, pack_path
                ) VALUES(
                    '/config/import-packs/queue-SHARED-ID.cleanup-old',
                    'shared-id', 'SHARED-ID', 1,
                    '/config/import-packs/queue-SHARED-ID'
                )
                """
            )
        conn.execute(
            "INSERT INTO history(event_type, download_id)"
            " VALUES('grabbed', 'rollback-sentinel')"
        )

    before = _database_state(fresh_db)
    traced_statements: list[str] = []
    original_migration = schema._migrate_history_download_ownership

    def _traced_migration(db: sqlite3.Connection) -> None:
        db.set_trace_callback(traced_statements.append)
        original_migration(db)

    monkeypatch.setattr(
        schema,
        "_migrate_history_download_ownership",
        _traced_migration,
    )

    with pytest.raises(RuntimeError, match="ownership cannot be guessed safely"):
        main._migrate_schema_constraints()

    assert any(
        "ALTER TABLE history ADD COLUMN download_client_id" in statement
        for statement in traced_statements
    )
    assert any(
        "ALTER TABLE import_queue ADD COLUMN download_protocol" in statement
        for statement in traced_statements
    )
    assert any(
        "CREATE TABLE import_queue_new" in statement
        for statement in traced_statements
    )
    assert _database_state(fresh_db) == before
    with sqlite3.connect(fresh_db) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        row_count = conn.execute(
            "SELECT COUNT(*) FROM import_pack_cleanup_"
            + ("reservations" if journal == "reservation" else "tombstones")
        ).fetchone()[0]
        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(import_pack_cleanup_reservations)"
            ).fetchall()
        }
    assert version == 4
    assert row_count == 1
    assert "download_identity_key" not in columns


def test_init_db_creates_import_lease_columns_and_partial_expiry_index(fresh_db):
    """Fresh installs carry nullable lease metadata and the expiry hot index."""
    import main

    main.init_db()
    with sqlite3.connect(fresh_db) as conn:
        columns = {
            row[1]: row
            for row in conn.execute("PRAGMA table_info(import_queue)").fetchall()
        }
        index_sql = conn.execute(
            "SELECT sql FROM sqlite_master"
            " WHERE type='index' AND name='idx_import_queue_importing_expiry'"
        ).fetchone()[0]
        volume_download_index = conn.execute(
            "SELECT 1 FROM sqlite_master"
            " WHERE type='index' AND name='idx_volumes_download_id'"
        ).fetchone()

    assert columns["lease_owner"][3] == 0
    assert columns["lease_owner"][4] is None
    assert columns["lease_expires_at"][3] == 0
    assert columns["lease_expires_at"][4] is None
    assert "ON import_queue(lease_expires_at)" in index_sql
    assert "WHERE status='importing'" in index_sql
    assert volume_download_index == (1,)


def test_init_db_migrates_true_historical_v2_database(fresh_db: str) -> None:
    """The real v2 shape preserves rows while gaining v3-v5 durability."""
    import main

    _create_historical_v2_database(fresh_db)
    with sqlite3.connect(fresh_db) as conn:
        conn.execute(
            "INSERT INTO import_queue(download_id, torrent_name, status)"
            " VALUES('legacy-row', 'Keep Me', 'pending')"
        )
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert conn.execute(
            "SELECT 1 FROM sqlite_master"
            " WHERE type='table' AND name='import_publications'"
        ).fetchone() is None
        assert "download_client_id" not in {
            row[1]
            for row in conn.execute("PRAGMA table_info(import_queue)").fetchall()
        }

    main.init_db()

    with sqlite3.connect(fresh_db) as conn:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(import_queue)").fetchall()
        }
        row = conn.execute(
            "SELECT torrent_name, lease_owner, lease_expires_at"
            " FROM import_queue WHERE download_id='legacy-row'"
        ).fetchone()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        _assert_download_protocol_check(conn)
    assert {"lease_owner", "lease_expires_at", "download_client_id"} <= columns
    assert row == ("Keep Me", None, None)
    assert version == 5


def test_init_db_creates_durable_publication_precondition_columns(fresh_db):
    """Fresh journals can persist complete source and destination barriers."""
    import main

    main.init_db()
    with sqlite3.connect(fresh_db) as conn:
        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(import_publication_files)"
            ).fetchall()
        }

    assert {
        "source_sha256",
        "source_claim_path",
        "final_expected_absent",
        "prepared_final_dev",
        "prepared_final_inode",
        "prepared_final_size",
        "prepared_final_mtime_ns",
        "prepared_final_sha256",
        "final_claim_path",
    } <= columns


def test_init_db_creates_replayable_volume_deletion_journal(fresh_db):
    """Fresh v4 databases carry the active import fence and identity fields."""
    import main

    main.init_db()
    with sqlite3.connect(fresh_db) as conn:
        columns = {
            row[1]: row
            for row in conn.execute(
                "PRAGMA table_info(volume_file_deletions)"
            ).fetchall()
        }
        indexes = {
            row[1]
            for row in conn.execute(
                "PRAGMA index_list(volume_file_deletions)"
            ).fetchall()
        }
        version = conn.execute("PRAGMA user_version").fetchone()[0]

    assert {
        "volume_id",
        "series_id",
        "state",
        "target_path",
        "parent_path",
        "claim_path",
        "target_present",
        "target_dev",
        "target_inode",
        "target_size",
        "target_mtime_ns",
        "target_sha256",
        "diagnostic",
        "completed_at",
    } <= columns.keys()
    assert "idx_volume_file_deletions_active_volume" in indexes
    assert "idx_volume_file_deletions_active_series" in indexes
    assert version == 5


@pytest.mark.parametrize("terminal_state", (None, "finalized", "deleted"))
def test_v3_terminal_or_empty_database_migrates_publication_safety_and_outbox(
    fresh_db: str,
    terminal_state: str | None,
) -> None:
    """The v4 migration accepts only terminal or absent v3 journals."""
    import main

    _create_historical_v3_database(
        fresh_db,
        publication_state=terminal_state,
    )

    main._migrate_schema_constraints()

    with sqlite3.connect(fresh_db) as conn:
        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(import_publication_files)"
            ).fetchall()
        }
        outbox = conn.execute(
            "SELECT 1 FROM sqlite_master"
            " WHERE type='table' AND name='import_publication_notifications'"
        ).fetchone()
        deletion_journal = conn.execute(
            "SELECT 1 FROM sqlite_master"
            " WHERE type='table' AND name='volume_file_deletions'"
        ).fetchone()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        publication_columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(import_publications)"
            ).fetchall()
        }
    assert set(_V4_SAFETY_COLUMNS) <= columns
    assert "queue_download_client_id" in publication_columns
    assert outbox == (1,)
    assert deletion_journal == (1,)
    assert version == 5


@pytest.mark.parametrize(
    "active_state",
    ("staging", "prepared", "publishing", "published", "db_committed", "cleaning"),
)
def test_v3_active_publication_blocks_v4_migration_and_rolls_back(
    fresh_db: str,
    active_state: str,
) -> None:
    """Unsafe v3 journals fail startup without changing schema or version."""
    import main

    _create_historical_v3_database(
        fresh_db,
        publication_state=active_state,
    )
    before = _database_state(fresh_db)

    with pytest.raises(
        RuntimeError,
        match=rf"active v3 import publication journals exist:.*{active_state}",
    ):
        main._migrate_schema_constraints()

    assert _database_state(fresh_db) == before
    with sqlite3.connect(fresh_db) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(import_publication_files)"
            ).fetchall()
        }
        outbox = conn.execute(
            "SELECT 1 FROM sqlite_master"
            " WHERE type='table' AND name='import_publication_notifications'"
        ).fetchone()
        deletion_journal = conn.execute(
            "SELECT 1 FROM sqlite_master"
            " WHERE type='table' AND name='volume_file_deletions'"
        ).fetchone()
        publication_columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(import_publications)"
            ).fetchall()
        }
    assert version == 3
    assert set(_V4_SAFETY_COLUMNS).isdisjoint(columns)
    assert "queue_download_client_id" not in publication_columns
    assert outbox is None
    assert deletion_journal is None


def test_init_db_is_idempotent(fresh_db):
    """Existing populated DBs must continue to start cleanly: running
    init_db a second time on an already-initialised DB is a no-op."""
    import main
    main.init_db()
    # Insert a sentinel row so we can verify init_db doesn't drop or wipe
    # data on the second pass.
    with sqlite3.connect(fresh_db) as c:
        c.execute("INSERT INTO settings(key,value) VALUES('sentinel','keep-me')")
        c.commit()

    main.init_db()  # second pass — must not raise, must not lose data

    with sqlite3.connect(fresh_db) as c:
        row = c.execute("SELECT value FROM settings WHERE key='sentinel'").fetchone()
    assert row is not None and row[0] == "keep-me"


def test_init_db_rejects_future_schema_without_mutating_database(fresh_db: str):
    """A newer database must fail closed without changing logical DB state."""
    import main

    with sqlite3.connect(fresh_db) as conn:
        conn.execute(
            "CREATE TABLE future_data(id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO future_data(value) VALUES('keep-me')")
        conn.execute("PRAGMA application_id = 4242")
        conn.execute("PRAGMA user_version = 999")

    before = _database_state(fresh_db)

    with pytest.raises(RuntimeError, match="newer than this Mangarr version"):
        main.init_db()

    assert _database_state(fresh_db) == before


@pytest.mark.parametrize("journal_mode", ("delete", "wal"))
def test_init_db_serializes_future_schema_writer(
    fresh_db: str,
    monkeypatch,
    journal_mode: str,
):
    """Initialization must not write after another connection claims the DB."""
    import main

    main.init_db()
    with sqlite3.connect(fresh_db) as conn:
        actual_mode = conn.execute(
            f"PRAGMA journal_mode={journal_mode}"
        ).fetchone()[0]
        assert actual_mode == journal_mode
        conn.execute(
            "INSERT INTO series(title, search_pattern) VALUES('Race', 'Race')"
        )
        series_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO volumes(series_id, volume_num, status)"
            " VALUES(?, 1, 'wanted')",
            (series_id,),
        )
        volume_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def write_future_state(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE volumes SET status='downloaded', import_path=NULL,"
            " quality=NULL, imported_at=NULL WHERE id=?",
            (volume_id,),
        )

    finish_writer, lock_states = _interleave_future_schema_writer(
        monkeypatch,
        fresh_db,
        write_future_state,
    )
    try:
        main.init_db()
    finally:
        finish_writer()

    with sqlite3.connect(fresh_db) as conn:
        state = conn.execute(
            "SELECT status FROM volumes WHERE id=?", (volume_id,)
        ).fetchone()[0]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert lock_states == [True]
    assert state == "downloaded"
    assert version == 999


@pytest.mark.parametrize("journal_mode", ("delete", "wal"))
def test_direct_migration_serializes_future_schema_writer(
    fresh_db: str,
    monkeypatch,
    journal_mode: str,
):
    """Direct migration must hold its writer lock through version updates."""
    import main

    main.init_db()
    with sqlite3.connect(fresh_db) as conn:
        actual_mode = conn.execute(
            f"PRAGMA journal_mode={journal_mode}"
        ).fetchone()[0]
        assert actual_mode == journal_mode
        conn.execute(
            "INSERT INTO series(title, search_pattern) VALUES('Race', 'Race')"
        )
        series_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO volumes(series_id, volume_num, status)"
            " VALUES(?, 1, 'wanted')",
            (series_id,),
        )
        volume_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("PRAGMA user_version = 1")

    def write_future_state(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE volumes SET status='future-owned' WHERE id=?",
            (volume_id,),
        )

    finish_writer, lock_states = _interleave_future_schema_writer(
        monkeypatch,
        fresh_db,
        write_future_state,
    )
    try:
        main._migrate_schema_constraints()
    finally:
        finish_writer()

    with sqlite3.connect(fresh_db) as conn:
        state = conn.execute(
            "SELECT status FROM volumes WHERE id=?", (volume_id,)
        ).fetchone()[0]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert lock_states == [True]
    assert state == "future-owned"
    assert version == 999
