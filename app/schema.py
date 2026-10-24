"""Database schema initialisation and migrations.

Seventh module extracted from main.py. Contains everything that
creates, alters, or reshapes the SQLite schema at startup:

  - init_db                      — CREATE TABLE / add_col / index
                                   / trigger / seed-default pass
  - _bootstrap_root_folders      — legacy save_path → root_folders
                                   migration
  - _migrate_schema_constraints  — add FK constraints via rebuild
                                   pattern, gated by PRAGMA user_version
  - _SCHEMA_VERSION_FILE_IDENTITY — current migration version

Callers in main.py see `init_db()`, `_bootstrap_root_folders()`, and
`_migrate_schema_constraints()` via a re-export.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast

from events import log_event
from parsing import extract_volume_num
from shared import (
    get_db,
    get_cfg,
    validate_sql_identifier,
    validate_sql_typedef,
)

_SCHEMA_VERSION_FK_CONSTRAINTS = 1
_SCHEMA_VERSION_STATUS_CONSTRAINTS = 2
_SCHEMA_VERSION_IMPORT_PUBLICATIONS = 3
_SCHEMA_VERSION_DURABLE_PUBLICATION_SAFETY = 4
_SCHEMA_VERSION_HISTORY_DOWNLOAD_OWNERSHIP = 5
_SCHEMA_VERSION_FILE_IDENTITY = 6
_SCHEMA_VERSION_BLOCKLIST_SOURCES = 7
_SCHEMA_VERSION_FILE_IDENTITY_PROVISIONING = 8
_SUPPORTED_SCHEMA_VERSION = _SCHEMA_VERSION_FILE_IDENTITY_PROVISIONING


@contextmanager
def _schema_write_transaction() -> Iterator[sqlite3.Connection]:
    """Hold one SQLite writer lock across schema validation and every write."""
    with get_db() as db:
        # Rebuild migrations must drop referenced tables, so foreign-key
        # enforcement has to be disabled before the transaction begins.
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.execute("PRAGMA foreign_keys=ON")


def _execute_script_in_transaction(db: sqlite3.Connection, script: str) -> None:
    """Execute a script without ``executescript()`` releasing the writer lock.

    Python's sqlite3 ``executescript()`` implicitly commits a pending
    transaction before running its script. Execute one complete statement at
    a time so ``BEGIN IMMEDIATE`` remains active for the entire schema pass.
    """
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ""
    if statement.strip():
        raise RuntimeError("incomplete SQL statement in schema initialization")


def _validate_schema_version(db: sqlite3.Connection) -> int:
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


def init_db() -> None:
    with _schema_write_transaction() as db:
        _validate_schema_version(db)
        _execute_script_in_transaction(db, """
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth_admin (
                id            INTEGER PRIMARY KEY CHECK(id = 1),
                username      TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth_sessions (
                token_hash   TEXT PRIMARY KEY,
                admin_id     INTEGER NOT NULL REFERENCES auth_admin(id) ON DELETE CASCADE,
                created_at   TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                expires_at   TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS series (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                title           TEXT NOT NULL,
                search_pattern  TEXT NOT NULL,
                anilist_id      INTEGER,
                cover_url       TEXT,
                status          TEXT,
                description     TEXT,
                total_volumes   INTEGER,
                total_chapters  INTEGER,
                added_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                enabled         INTEGER DEFAULT 1,
                monitored       INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS volumes (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id    INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                volume_num   REAL,
                chapter_num  REAL,
                title        TEXT,
                status       TEXT DEFAULT 'wanted' CHECK(status IN ('wanted','grabbed','downloaded')),
                grabbed_at   TIMESTAMP,
                size_bytes   INTEGER,
                source_url   TEXT,
                torrent_name TEXT,
                indexer      TEXT,
                protocol     TEXT,
                client       TEXT,
                download_client_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS seen (
                torrent_url  TEXT PRIMARY KEY,
                torrent_name TEXT,
                series_id    INTEGER REFERENCES series(id) ON DELETE CASCADE,
                volume_num   REAL,
                grabbed_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                indexer      TEXT,
                protocol     TEXT,
                client       TEXT,
                download_client_id INTEGER,
                respect_grab_claims INTEGER CHECK(respect_grab_claims IN (0,1))
            );
            CREATE TABLE IF NOT EXISTS events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT,
                series_id  INTEGER REFERENCES series(id) ON DELETE CASCADE,
                message    TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS root_folders (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                path       TEXT NOT NULL UNIQUE,
                label      TEXT,
                is_default INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS blocklist (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id    INTEGER REFERENCES series(id) ON DELETE CASCADE,
                torrent_url  TEXT UNIQUE,
                torrent_name TEXT,
                reason       TEXT,
                indexer      TEXT,
                protocol     TEXT,
                size_bytes   INTEGER,
                added_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS history (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type    TEXT NOT NULL,
                series_id     INTEGER,
                series_title  TEXT,
                volume_label  TEXT,
                source_title  TEXT,
                indexer       TEXT,
                protocol      TEXT,
                client        TEXT,
                download_id   TEXT,
                download_client_id INTEGER,
                size_bytes    INTEGER,
                release_group TEXT,
                data          TEXT,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS import_queue (
                 id           INTEGER PRIMARY KEY AUTOINCREMENT,
                 series_id    INTEGER REFERENCES series(id),
                 download_id  TEXT,
                 download_client_id INTEGER,
                 download_protocol TEXT
                                      CHECK(download_protocol IN (
                                          'torrent','nzb'
                                      ) OR download_protocol IS NULL),
                 respect_grab_claims INTEGER CHECK(respect_grab_claims IN (0,1)),
                 torrent_name TEXT,
                 torrent_url  TEXT,
                 volume_num   REAL,
                 src_dir      TEXT,
                 status       TEXT DEFAULT 'pending',
                 failed_at    TIMESTAMP,
                 lease_owner  TEXT,
                 lease_expires_at TIMESTAMP,
                 created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
             );
            CREATE TABLE IF NOT EXISTS import_queue_files (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                queue_id         INTEGER REFERENCES import_queue(id),
                filename         TEXT,
                src_path         TEXT,
                dst_path         TEXT,
                proposed_volume  REAL,
                proposed_import_kind TEXT,
                proposed_special_title TEXT,
                status           TEXT DEFAULT 'pending'
            );
            CREATE TABLE IF NOT EXISTS import_publications (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                queue_id              INTEGER NOT NULL UNIQUE,
                state                 TEXT NOT NULL
                                      CHECK(state IN (
                                          'staging','prepared','publishing',
                                          'published','db_committed','cleaning',
                                          'finalized','deleted'
                                      )),
                owner_token           TEXT NOT NULL,
                operation_owner       TEXT,
                operation_expires_at  TEXT,
                series_id             INTEGER NOT NULL,
                dst_dir               TEXT NOT NULL,
                import_mode           TEXT NOT NULL
                                      CHECK(import_mode IN ('copy','move','hardlink')),
                staging_dir           TEXT NOT NULL,
                queue_snapshot_json   TEXT NOT NULL,
                series_snapshot_json  TEXT,
                series_tags_json      TEXT NOT NULL,
                queue_status          TEXT NOT NULL,
                queue_download_id     TEXT,
                queue_download_client_id INTEGER,
                queue_torrent_name    TEXT,
                queue_torrent_url     TEXT,
                queue_volume_num      REAL,
                queue_src_dir         TEXT,
                queue_failed_at       TEXT,
                queue_lease_owner     TEXT,
                queue_lease_expires_at TEXT,
                queue_created_at      TEXT,
                result_ok             INTEGER CHECK(result_ok IN (0,1)),
                result_imported_count INTEGER,
                result_queue_status   TEXT,
                diagnostic            TEXT NOT NULL DEFAULT '',
                notification_state    TEXT NOT NULL DEFAULT 'none'
                                      CHECK(notification_state IN (
                                          'none','pending','dispatched','suppressed'
                                      )),
                notification_title    TEXT,
                notification_label    TEXT,
                notification_cover_url TEXT,
                pack_cleanup_state    TEXT NOT NULL DEFAULT 'retained'
                                      CHECK(pack_cleanup_state IN (
                                          'pending','complete','retained'
                                      )),
                pack_cleanup_completed_at TEXT,
                created_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                prepared_at           TEXT,
                publishing_at         TEXT,
                published_at          TEXT,
                db_committed_at       TEXT,
                cleaning_at           TEXT,
                finalized_at          TEXT,
                deleted_at            TEXT
            );
            CREATE TABLE IF NOT EXISTS import_publication_files (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                publication_id           INTEGER NOT NULL
                                             REFERENCES import_publications(id)
                                             ON DELETE CASCADE,
                ordinal                  INTEGER NOT NULL,
                file_id                  INTEGER NOT NULL,
                src_path                 TEXT NOT NULL,
                filename                 TEXT NOT NULL,
                dst_path                 TEXT NOT NULL,
                import_kind              TEXT NOT NULL,
                file_type                TEXT NOT NULL,
                proposed_vol             REAL,
                proposed_chap            REAL,
                chap_range_end           REAL,
                vol_range_start          REAL,
                vol_range_end            REAL,
                pack_type                TEXT,
                is_special               INTEGER NOT NULL CHECK(is_special IN (0,1)),
                special_title            TEXT,
                has_volume_range         INTEGER NOT NULL
                                             CHECK(has_volume_range IN (0,1)),
                is_legacy_chapter_stub   INTEGER NOT NULL
                                             CHECK(is_legacy_chapter_stub IN (0,1)),
                is_legacy_chapter_recheck INTEGER NOT NULL
                                             CHECK(is_legacy_chapter_recheck IN (0,1)),
                plan_status              TEXT NOT NULL
                                             CHECK(plan_status IN (
                                                 'ready','skip','needs_review',
                                                 'pre_failed'
                                             )),
                plan_failure_reason      TEXT NOT NULL DEFAULT '',
                stage_ok                 INTEGER CHECK(stage_ok IN (0,1)),
                stage_error              TEXT NOT NULL DEFAULT '',
                stage_path               TEXT,
                final_path               TEXT,
                source_dev               INTEGER,
                source_inode             INTEGER,
                source_size              INTEGER,
                source_mtime_ns          INTEGER,
                source_sha256            TEXT,
                source_claim_path        TEXT,
                staged_dev               INTEGER,
                staged_inode             INTEGER,
                staged_size              INTEGER,
                staged_mtime_ns          INTEGER,
                staged_sha256            TEXT,
                final_expected_absent    INTEGER
                                                 CHECK(final_expected_absent IN (0,1)),
                prepared_final_dev       INTEGER,
                prepared_final_inode     INTEGER,
                prepared_final_size      INTEGER,
                prepared_final_mtime_ns  INTEGER,
                prepared_final_sha256    TEXT,
                final_claim_path         TEXT,
                stage_state              TEXT NOT NULL DEFAULT 'pending'
                                             CHECK(stage_state IN (
                                                 'pending','staged','skipped','failed'
                                             )),
                publish_state            TEXT NOT NULL DEFAULT 'pending'
                                             CHECK(publish_state IN (
                                                 'pending','published',
                                                 'not_applicable','blocked'
                                             )),
                cleanup_state            TEXT NOT NULL DEFAULT 'pending'
                                             CHECK(cleanup_state IN (
                                                 'pending','deleted','missing',
                                                 'replaced','not_applicable','blocked'
                                             )),
                diagnostic               TEXT NOT NULL DEFAULT '',
                staged_at                TEXT,
                published_at             TEXT,
                cleaned_at               TEXT,
                UNIQUE(publication_id, ordinal),
                UNIQUE(publication_id, file_id)
            );
            CREATE TABLE IF NOT EXISTS import_publication_notifications (
                publication_id       INTEGER PRIMARY KEY,
                state                TEXT NOT NULL
                                         CHECK(state IN (
                                             'pending','dispatching','dispatched'
                                         )),
                idempotency_key      TEXT NOT NULL UNIQUE,
                title                TEXT NOT NULL,
                label                TEXT NOT NULL,
                cover_url            TEXT NOT NULL DEFAULT '',
                operation_owner      TEXT,
                operation_expires_at TEXT,
                attempt_count        INTEGER NOT NULL DEFAULT 0,
                next_attempt_at      TEXT,
                last_error           TEXT NOT NULL DEFAULT '',
                created_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                dispatched_at        TEXT
            );
            CREATE TABLE IF NOT EXISTS import_publication_notification_deliveries (
                publication_id       INTEGER NOT NULL,
                connection_id        INTEGER NOT NULL,
                connection_name      TEXT NOT NULL,
                connection_type      TEXT NOT NULL,
                state                TEXT NOT NULL
                                         CHECK(state IN (
                                             'pending','dispatching','completed'
                                         )),
                completion_reason    TEXT
                                         CHECK(
                                             completion_reason IS NULL
                                             OR completion_reason IN (
                                                 'delivered',
                                                 'connection_deleted',
                                                 'connection_disabled'
                                             )
                                         ),
                operation_owner      TEXT,
                operation_expires_at TEXT,
                attempt_count        INTEGER NOT NULL DEFAULT 0,
                next_attempt_at      TEXT,
                last_error           TEXT NOT NULL DEFAULT '',
                created_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at         TEXT,
                PRIMARY KEY(publication_id, connection_id)
            );
            CREATE TABLE IF NOT EXISTS import_publication_success_effects (
                publication_id       INTEGER NOT NULL,
                effect_type          TEXT NOT NULL
                                         CHECK(effect_type IN (
                                             'cover','komga_scan',
                                             'remove_completed'
                                         )),
                state                TEXT NOT NULL
                                         CHECK(state IN (
                                             'pending','dispatching','completed'
                                         )),
                idempotency_key      TEXT NOT NULL UNIQUE,
                payload_json         TEXT NOT NULL,
                operation_owner      TEXT,
                operation_expires_at TEXT,
                attempt_count        INTEGER NOT NULL DEFAULT 0,
                next_attempt_at      TEXT,
                last_error           TEXT NOT NULL DEFAULT '',
                created_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at         TEXT,
                PRIMARY KEY(publication_id, effect_type)
            );
            CREATE TABLE IF NOT EXISTS import_pack_cleanup_reservations (
                download_identity_key  TEXT NOT NULL PRIMARY KEY,
                download_client_id     INTEGER
                                           CHECK(download_client_id IS NULL
                                                 OR download_client_id > 0),
                protocol               TEXT
                                           CHECK(protocol IN ('torrent','nzb')
                                                 OR protocol IS NULL),
                normalized_download_id TEXT NOT NULL
                                           CHECK(normalized_download_id != ''),
                download_id            TEXT NOT NULL,
                purpose                TEXT NOT NULL
                                           CHECK(purpose IN ('queueing','cleanup')),
                owner_token            TEXT NOT NULL,
                queue_id               INTEGER,
                publication_id         INTEGER,
                pack_path              TEXT NOT NULL,
                tombstone_path         TEXT,
                directory_ownership_json TEXT,
                expires_at             TEXT NOT NULL,
                created_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS import_pack_cleanup_tombstones (
                tombstone_path         TEXT PRIMARY KEY,
                download_identity_key  TEXT NOT NULL,
                download_client_id     INTEGER
                                           CHECK(download_client_id IS NULL
                                                 OR download_client_id > 0),
                protocol               TEXT
                                           CHECK(protocol IN ('torrent','nzb')
                                                 OR protocol IS NULL),
                normalized_download_id TEXT NOT NULL,
                download_id            TEXT NOT NULL,
                queue_id               INTEGER NOT NULL,
                publication_id         INTEGER,
                pack_path              TEXT NOT NULL,
                carrier_json           TEXT,
                created_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS volume_file_deletions (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                volume_id            INTEGER NOT NULL,
                series_id            INTEGER NOT NULL,
                state                TEXT NOT NULL
                                          CHECK(state IN ('active','completed')),
                target_path          TEXT NOT NULL,
                parent_path          TEXT NOT NULL,
                claim_path           TEXT NOT NULL,
                target_present       INTEGER NOT NULL
                                             CHECK(target_present IN (0,1)),
                target_dev           INTEGER,
                target_inode         INTEGER,
                target_size          INTEGER,
                target_mtime_ns      INTEGER,
                target_sha256        TEXT,
                series_title         TEXT NOT NULL,
                volume_num           REAL,
                source_title         TEXT NOT NULL DEFAULT '',
                original_import_path TEXT NOT NULL DEFAULT '',
                diagnostic           TEXT NOT NULL DEFAULT '',
                created_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at         TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_volume_file_deletions_active_volume
                ON volume_file_deletions(volume_id) WHERE state='active';
            CREATE INDEX IF NOT EXISTS idx_volume_file_deletions_active_series
                ON volume_file_deletions(series_id, id) WHERE state='active';
            CREATE TABLE IF NOT EXISTS series_aliases (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id INTEGER NOT NULL REFERENCES series(id),
                alias     TEXT    NOT NULL,
                UNIQUE(series_id, alias)
            );
            CREATE TABLE IF NOT EXISTS series_metadata_sources (
                series_id       INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                source          TEXT    NOT NULL,
                status          TEXT    NOT NULL DEFAULT 'pending'
                                        CHECK(status IN ('pending','refreshing','healthy','degraded','failed')),
                last_attempt_at TEXT,
                last_success_at TEXT,
                next_retry_at   TEXT,
                failure_count   INTEGER NOT NULL DEFAULT 0,
                error           TEXT,
                details         TEXT,
                PRIMARY KEY(series_id, source)
            );
            CREATE TABLE IF NOT EXISTS series_metadata_fields (
                series_id       INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                field_name      TEXT    NOT NULL,
                value_json      TEXT,
                selected_source TEXT    NOT NULL DEFAULT 'legacy',
                locked          INTEGER NOT NULL DEFAULT 0 CHECK(locked IN (0,1)),
                selected_at     TEXT    NOT NULL,
                PRIMARY KEY(series_id, field_name)
            );
            CREATE TABLE IF NOT EXISTS series_metadata_candidates (
                series_id  INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                field_name TEXT    NOT NULL,
                source     TEXT    NOT NULL,
                value_json TEXT,
                confidence REAL,
                fetched_at TEXT    NOT NULL,
                PRIMARY KEY(series_id, field_name, source)
            );
            CREATE TABLE IF NOT EXISTS pending_releases (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id  INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                url        TEXT    NOT NULL,
                title      TEXT,
                indexer    TEXT,
                protocol   TEXT,
                size_bytes INTEGER DEFAULT 0,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(series_id, url)
            );
            CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires
                ON auth_sessions(expires_at);
        """)

        # ── Migrations: add columns to existing tables ────────────────────────
        # `table`, `col`, and `typedef` are all interpolated directly into
        # SQL — `?` placeholders can't bind identifiers or type declarations.
        # Validators below enforce a strict shape so a future refactor
        # can't silently introduce injection through this helper, even if
        # a caller started feeding in non-hardcoded values.
        def add_col(table, col, typedef):
            validate_sql_identifier(table, kind="table")
            validate_sql_identifier(col, kind="column")
            validate_sql_typedef(typedef)
            cols = {r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}
            if col not in cols:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")

        add_col("import_publication_files", "source_sha256", "TEXT")
        add_col("import_publication_files", "source_claim_path", "TEXT")
        add_col(
            "import_publication_files",
            "final_expected_absent",
            "INTEGER",
        )
        add_col("import_publication_files", "prepared_final_dev", "INTEGER")
        add_col("import_publication_files", "prepared_final_inode", "INTEGER")
        add_col("import_publication_files", "prepared_final_size", "INTEGER")
        add_col("import_publication_files", "prepared_final_mtime_ns", "INTEGER")
        add_col("import_publication_files", "prepared_final_sha256", "TEXT")
        add_col("import_publication_files", "final_claim_path", "TEXT")
        add_col(
            "import_publications",
            "pack_cleanup_state",
            "TEXT DEFAULT 'retained'",
        )
        add_col(
            "import_publications",
            "pack_cleanup_completed_at",
            "TEXT",
        )
        add_col(
            "import_publications",
            "queue_download_client_id",
            "INTEGER",
        )
        db.execute(
            """
            UPDATE import_publications
            SET pack_cleanup_state='pending'
            WHERE state IN ('finalized','deleted')
              AND result_queue_status IN ('imported','failed','skipped')
              AND queue_download_id IS NOT NULL
              AND trim(queue_download_id)!=''
              AND pack_cleanup_state='retained'
              AND pack_cleanup_completed_at IS NULL
            """
        )
        db.execute(
            """
            UPDATE import_publications
            SET pack_cleanup_state='retained'
            WHERE state IN ('finalized','deleted')
              AND result_queue_status NOT IN ('imported','failed','skipped')
              AND pack_cleanup_state='pending'
            """
        )

        add_col('series',  'total_volumes',   'INTEGER')
        add_col('series',  'total_chapters',  'INTEGER')
        add_col('series',  'monitored',       'INTEGER DEFAULT 1')
        add_col('series',  'pub_year',        'INTEGER')
        add_col('seen',    'volume_num',      'REAL')
        add_col('seen',    'indexer',         'TEXT')
        add_col('seen',    'protocol',        'TEXT')
        add_col('seen',    'client',          'TEXT')
        add_col('seen',    'download_client_id', 'INTEGER')
        add_col('volumes', 'indexer',         'TEXT')
        add_col('volumes', 'protocol',        'TEXT')
        add_col('volumes', 'client',          'TEXT')
        add_col('volumes', 'download_client_id', 'INTEGER')

        # ── Backfill defaults for old seen records ────────────────────────────
        db.execute("""
            UPDATE seen SET indexer='Nyaa', protocol='torrent', client='qbittorrent'
            WHERE indexer IS NULL
        """)

        # ── Backfill volume_num from torrent_name for old seen records ────────
        null_vol = db.execute(
            "SELECT rowid, torrent_name FROM seen WHERE volume_num IS NULL AND torrent_name IS NOT NULL"
        ).fetchall()
        for row in null_vol:
            vn = extract_volume_num(row['torrent_name'])
            if vn is not None:
                db.execute("UPDATE seen SET volume_num=? WHERE rowid=?", (vn, row['rowid']))

        # ── Backfill volumes table from seen where client/indexer missing ─────
        db.execute("""
            UPDATE volumes SET indexer='Nyaa', protocol='torrent', client='qbittorrent'
            WHERE indexer IS NULL AND status != 'wanted'
        """)
        add_col('seen',    'download_id',     'TEXT')
        add_col('seen',    'release_group',  'TEXT')
        add_col('seen',    'size_bytes',     'INTEGER')
        # release_guid: stable per-release identifier from the indexer
        # (Prowlarr/torznab `<guid>`). Used as a second dedup key alongside
        # torrent_url so two URLs that point at the same content (mirrors,
        # redirects, cross-posts) are caught by the second grab attempt.
        add_col('seen',    'release_guid',   'TEXT')
        add_col('volumes', 'download_id',    'TEXT')
        add_col('series',  'root_folder_id', 'INTEGER')
        add_col('series',  'folder_name',    'TEXT')  # optional per-series library folder leaf
        add_col('volumes', 'vol_range_start', 'REAL')
        add_col('volumes', 'vol_range_end',   'REAL')
        add_col('volumes', 'pack_type',       'TEXT')  # 'volume', 'chapter', 'complete'
        add_col('series',  'mangadex_id',     'TEXT')
        add_col('series',  'chapter_vol_map', 'TEXT')  # JSON {chapter_str: vol_int}
        add_col('series',  'mal_id',          'INTEGER')
        add_col('series',  'mu_id',           'TEXT')   # MangaUpdates numeric ID (base36 slug decoded)
        add_col('volumes', 'import_path',     'TEXT')   # final library path after import
        add_col('volumes', 'release_group',   'TEXT')
        add_col('series',  'tags',            'TEXT')   # JSON array of tag strings
        add_col('blocklist', 'indexer',       'TEXT')
        add_col('blocklist', 'protocol',      'TEXT')
        add_col('blocklist', 'size_bytes',    'INTEGER')
        add_col('volumes', 'monitored',       'INTEGER DEFAULT 1')
        add_col('series',  'monitor_mode',    'TEXT DEFAULT "all"')  # all|future|missing|existing|none
        add_col('series',  'language',        'TEXT')   # preferred language filter
        add_col('series',  'quality_cutoff',  'TEXT')   # quality level to stop upgrading at
        add_col('volumes', 'quality',         'TEXT')                   # cbz|cbr|epub|mobi|pdf
        add_col('import_queue_files', 'proposed_chapter', 'REAL')
        add_col('import_queue_files', 'file_type',        'TEXT DEFAULT "volume"')
        # Stage 2 of the mapping audit: explicit range / pack-type /
        # special-release fields the review UI can now set. All nullable
        # so existing queue rows keep importing through the fallback
        # paths in _execute_import. See tests/python/test_import_mapping.py
        # for the contract these columns carry.
        add_col('import_queue_files', 'proposed_volume_range_start',  'REAL')
        add_col('import_queue_files', 'proposed_volume_range_end',    'REAL')
        add_col('import_queue_files', 'proposed_chapter_range_end',   'REAL')
        add_col('import_queue_files', 'proposed_pack_type',           'TEXT')
        add_col('import_queue_files', 'proposed_is_special',          'INTEGER DEFAULT 0')
        add_col('import_queue_files', 'proposed_import_kind',         'TEXT')
        add_col('import_queue_files', 'proposed_special_title',       'TEXT')
        db.execute("""
            UPDATE import_queue_files
               SET proposed_import_kind = CASE
                   WHEN COALESCE(proposed_is_special, 0) = 1
                        OR proposed_pack_type = 'special' THEN 'special'
                   WHEN status = 'skipped' THEN 'skip'
                   WHEN proposed_chapter_range_end IS NOT NULL
                        OR proposed_pack_type = 'chapter_range' THEN 'chapter_range'
                   WHEN proposed_volume_range_end IS NOT NULL
                        OR proposed_pack_type = 'volume_range' THEN 'volume_range'
                   WHEN file_type = 'chapter'
                        OR proposed_pack_type = 'chapter' THEN 'chapter'
                   ELSE 'volume'
               END
             WHERE proposed_import_kind IS NULL
                OR proposed_import_kind NOT IN (
                    'volume', 'volume_range', 'chapter',
                    'chapter_range', 'special', 'skip'
                )
        """)
        db.execute("""
            UPDATE import_queue_files
               SET proposed_is_special = 1
             WHERE proposed_import_kind = 'special'
        """)
        add_col('import_queue',            'failed_at',                'TIMESTAMP')
        add_col('import_queue',            'lease_owner',              'TEXT')
        add_col('import_queue',            'lease_expires_at',         'TIMESTAMP')
        add_col('import_queue',            'download_client_id',       'INTEGER')
        add_col('import_queue',            'download_protocol',        'TEXT')
        # Side-story / oneshot persistence on the final volumes row.
        # Stage 2 only stores this flag; Stage 3 adds the coverage
        # exclusion that makes it load-bearing. Non-null default so
        # existing WHERE is_special=0 filters work without a NULL check.
        add_col('volumes',            'is_special',                   'INTEGER DEFAULT 0')
        add_col('history',            'torrent_url',      'TEXT')
        add_col('history',            'download_client_id', 'INTEGER')
        add_col('volumes',            'imported_at',      'TEXT')
        add_col('volumes',            'edition_type',     'TEXT')   # standard|deluxe|omnibus|special|collector|digital
        add_col('volumes',            'language',         'TEXT')   # en|ja|fr|etc — detected from release title
        add_col('series',             'vol_count_source', 'TEXT DEFAULT "anilist"')  # anilist|mangaupdates|wikipedia|google_books|local|manual

        # ── chapters table ────────────────────────────────────────────────────
        _execute_script_in_transaction(db, """
            CREATE TABLE IF NOT EXISTS chapters (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id     INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                volume_id     INTEGER REFERENCES volumes(id) ON DELETE SET NULL,
                chapter_num   REAL    NOT NULL,
                title         TEXT,
                status        TEXT    DEFAULT 'wanted' CHECK(status IN ('wanted','grabbed','downloaded')),
                monitored     INTEGER DEFAULT 1,
                grabbed_at    TIMESTAMP,
                torrent_name  TEXT,
                torrent_url   TEXT,
                indexer       TEXT,
                protocol      TEXT,
                client        TEXT,
                download_client_id INTEGER,
                size_bytes    INTEGER DEFAULT 0,
                import_path   TEXT,
                download_id   TEXT,
                release_group TEXT,
                UNIQUE(series_id, chapter_num)
            );
        """)
        # chapters add_col calls MUST come after the CREATE TABLE above —
        # add_col runs ALTER TABLE which fails on a fresh DB if the table
        # doesn't exist yet, and the get_db transaction would then roll
        # back every CREATE TABLE that ran earlier in init_db.
        add_col('chapters',           'quality',          'TEXT')
        add_col('chapters',           'imported_at',      'TEXT')
        add_col('chapters',           'pages',            'INTEGER')
        add_col('chapters',           'metadata_source',  'TEXT')
        add_col('chapters',           'metadata_updated_at', 'TEXT')
        add_col('chapters',           'download_client_id', 'INTEGER')
        # Multi-chapter file support (e.g. c001-002 packs in one CBZ).
        # When set, this row covers chapter_num..chapter_range_end inclusive
        # via a single import_path. NULL preserves the original one-row-per-
        # chapter behaviour. Mirrors the volumes.vol_range_start/end pattern.
        add_col('chapters',           'chapter_range_end','REAL')

        # Evidence that a managed library chapter file is a copy Mangarr itself
        # created and verified from one identified Suwayomi chapter. Recorded
        # only; nothing reads it to clean up files (issue #422). Additive, so
        # no schema-version bump: older binaries simply ignore the table.
        _execute_script_in_transaction(db, """
            CREATE TABLE IF NOT EXISTS chapter_file_provenance (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                chapter_id          INTEGER NOT NULL
                                            REFERENCES chapters(id) ON DELETE CASCADE,
                series_id           INTEGER NOT NULL
                                            REFERENCES series(id) ON DELETE CASCADE,
                library_path        TEXT    NOT NULL UNIQUE,
                file_dev            TEXT    NOT NULL,
                file_inode          TEXT    NOT NULL,
                size_bytes          INTEGER NOT NULL CHECK(size_bytes > 0),
                mtime_ns            INTEGER NOT NULL,
                sha256              TEXT    NOT NULL CHECK(length(sha256) = 64),
                suwayomi_client_id  INTEGER NOT NULL,
                suwayomi_server     TEXT    NOT NULL,
                suwayomi_manga_id   INTEGER NOT NULL,
                suwayomi_source_id  TEXT    NOT NULL,
                suwayomi_chapter_id INTEGER NOT NULL,
                suwayomi_job_id     INTEGER,
                recorded_at         TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_chapter_file_provenance_chapter
                ON chapter_file_provenance(chapter_id);
        """)

        # Evidence of which identified Suwayomi chapters a merged volume this
        # import created holds, and the page range each produced. Recorded
        # only; nothing reads it to clean up files (issue #422). Additive.
        _execute_script_in_transaction(db, """
            CREATE TABLE IF NOT EXISTS volume_assembly (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                volume_id           INTEGER NOT NULL
                                            REFERENCES volumes(id) ON DELETE CASCADE,
                series_id           INTEGER NOT NULL
                                            REFERENCES series(id) ON DELETE CASCADE,
                library_path        TEXT    NOT NULL UNIQUE,
                file_dev            TEXT    NOT NULL,
                file_inode          TEXT    NOT NULL,
                size_bytes          INTEGER NOT NULL CHECK(size_bytes > 0),
                mtime_ns            INTEGER NOT NULL,
                sha256              TEXT    NOT NULL CHECK(length(sha256) = 64),
                suwayomi_client_id  INTEGER NOT NULL,
                suwayomi_server     TEXT    NOT NULL,
                suwayomi_manga_id   INTEGER NOT NULL,
                suwayomi_source_id  TEXT    NOT NULL,
                suwayomi_job_id     INTEGER,
                recorded_at         TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_volume_assembly_volume
                ON volume_assembly(volume_id);
            CREATE TABLE IF NOT EXISTS volume_assembly_chapters (
                assembly_id         INTEGER NOT NULL
                                            REFERENCES volume_assembly(id) ON DELETE CASCADE,
                position            INTEGER NOT NULL CHECK(position >= 0),
                suwayomi_chapter_id INTEGER NOT NULL,
                source_sha256       TEXT    NOT NULL CHECK(length(source_sha256) = 64),
                source_size_bytes   INTEGER NOT NULL CHECK(source_size_bytes > 0),
                first_page          INTEGER NOT NULL CHECK(first_page >= 1),
                last_page           INTEGER NOT NULL CHECK(last_page >= first_page),
                pages_sha256        TEXT    NOT NULL CHECK(length(pages_sha256) = 64),
                PRIMARY KEY(assembly_id, position),
                UNIQUE(assembly_id, suwayomi_chapter_id)
            );
        """)

        # Opt-in quarantine of loose chapters a merged volume provably holds
        # (issue #422). One journaled row per moved file. No foreign keys:
        # rows outlive purged series so quarantined files stay accounted for.
        _execute_script_in_transaction(db, """
            CREATE TABLE IF NOT EXISTS chapter_quarantine (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_token     TEXT    NOT NULL UNIQUE
                                            CHECK(length(operation_token) = 32),
                series_id           INTEGER NOT NULL,
                volume_id           INTEGER NOT NULL,
                chapter_id          INTEGER NOT NULL,
                chapter_num         REAL,
                assembly_id         INTEGER NOT NULL,
                suwayomi_chapter_id INTEGER NOT NULL,
                state               TEXT    NOT NULL CHECK(state IN (
                                        'reserved','captured','committed','quarantined',
                                        'rolled_back','restoring','restored')),
                original_path       TEXT    NOT NULL,
                volume_path         TEXT    NOT NULL,
                carrier_path        TEXT,
                quarantine_path     TEXT,
                file_inode          TEXT    NOT NULL,
                size_bytes          INTEGER NOT NULL CHECK(size_bytes > 0),
                mtime_ns            INTEGER NOT NULL,
                sha256              TEXT    NOT NULL CHECK(length(sha256) = 64),
                chapter_guard_json  TEXT    NOT NULL,
                diagnostic          TEXT    NOT NULL DEFAULT '',
                created_at          TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at          TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at        TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_chapter_quarantine_active
                ON chapter_quarantine(series_id, id)
                WHERE state IN ('reserved','captured','committed','restoring');
            CREATE UNIQUE INDEX IF NOT EXISTS idx_chapter_quarantine_active_chapter
                ON chapter_quarantine(chapter_id)
                WHERE state IN ('reserved','captured','committed','restoring');
            CREATE INDEX IF NOT EXISTS idx_chapter_quarantine_series
                ON chapter_quarantine(series_id, state);
        """)

        # Backfill download_id from seen → volumes for existing grabbed items
        db.execute("""
            UPDATE volumes SET download_id = (
                SELECT s.download_id FROM seen s
                WHERE s.series_id = volumes.series_id
                AND s.volume_num = volumes.volume_num
                AND s.download_id IS NOT NULL
                LIMIT 1
            )
            WHERE status != 'wanted' AND download_id IS NULL
        """)

        # ── Sonarr-parity tables ──────────────────────────────────────────────
        _execute_script_in_transaction(db, """
            -- Quality Profiles
            CREATE TABLE IF NOT EXISTS quality_profiles (
                id                            INTEGER PRIMARY KEY AUTOINCREMENT,
                name                          TEXT NOT NULL UNIQUE,
                qualities                     TEXT NOT NULL DEFAULT '["cbz","epub","cbr","pdf"]',
                cutoff                        TEXT,
                upgrades_allowed              INTEGER DEFAULT 1,
                minimum_custom_format_score   INTEGER DEFAULT 0,
                is_default                    INTEGER DEFAULT 0
            );

            -- Custom Formats
            CREATE TABLE IF NOT EXISTS custom_formats (
                id                                  INTEGER PRIMARY KEY AUTOINCREMENT,
                name                                TEXT NOT NULL UNIQUE,
                specifications                      TEXT NOT NULL DEFAULT '[]',
                include_custom_format_when_renaming INTEGER DEFAULT 0
            );

            -- Link custom formats → quality profiles with per-profile scores
            CREATE TABLE IF NOT EXISTS quality_profile_custom_formats (
                profile_id  INTEGER NOT NULL REFERENCES quality_profiles(id) ON DELETE CASCADE,
                format_id   INTEGER NOT NULL REFERENCES custom_formats(id) ON DELETE CASCADE,
                score       INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (profile_id, format_id)
            );

            -- Release Profiles
            CREATE TABLE IF NOT EXISTS release_profiles (
                id                                  INTEGER PRIMARY KEY AUTOINCREMENT,
                name                                TEXT NOT NULL,
                enabled                             INTEGER DEFAULT 1,
                required                            TEXT,
                ignored                             TEXT,
                preferred                           TEXT DEFAULT '[]',
                include_preferred_when_renaming     INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS release_profile_tags (
                profile_id INTEGER NOT NULL REFERENCES release_profiles(id) ON DELETE CASCADE,
                tag        TEXT NOT NULL,
                PRIMARY KEY (profile_id, tag)
            );

            -- Delay Profiles
            CREATE TABLE IF NOT EXISTS delay_profiles (
                id                         INTEGER PRIMARY KEY AUTOINCREMENT,
                name                       TEXT NOT NULL DEFAULT 'Default',
                order_num                  INTEGER NOT NULL DEFAULT 0,
                enable_usenet              INTEGER DEFAULT 1,
                enable_torrent             INTEGER DEFAULT 1,
                usenet_delay               INTEGER DEFAULT 0,
                torrent_delay              INTEGER DEFAULT 0,
                bypass_if_highest_quality  INTEGER DEFAULT 0,
                is_default                 INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS delay_profile_tags (
                profile_id INTEGER NOT NULL REFERENCES delay_profiles(id) ON DELETE CASCADE,
                tag        TEXT NOT NULL,
                PRIMARY KEY (profile_id, tag)
            );

            -- Download Clients
            CREATE TABLE IF NOT EXISTS download_clients (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                name             TEXT NOT NULL,
                type             TEXT NOT NULL,
                host             TEXT,
                port             INTEGER,
                use_ssl          INTEGER DEFAULT 0,
                url_base         TEXT,
                username         TEXT,
                password         TEXT,
                category         TEXT DEFAULT 'manga',
                priority         INTEGER DEFAULT 1,
                enabled          INTEGER DEFAULT 1,
                remove_completed INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS download_client_tags (
                client_id INTEGER NOT NULL REFERENCES download_clients(id) ON DELETE CASCADE,
                tag       TEXT NOT NULL,
                PRIMARY KEY (client_id, tag)
            );
            -- Persisted circuit-breaker state for download clients.
            -- Pre-fix the breaker was an in-memory dict that reset on every
            -- app restart, so a client that was tripped 2 minutes ago would
            -- appear freshly-healthy after a container restart and immediately
            -- hammer again. Now it survives restarts.
            CREATE TABLE IF NOT EXISTS client_breaker_state (
                client_id    INTEGER PRIMARY KEY REFERENCES download_clients(id) ON DELETE CASCADE,
                failures     INTEGER NOT NULL DEFAULT 0,
                open_until   REAL NOT NULL DEFAULT 0,
                updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            -- Per-indexer backoff to avoid hammering upstreams that have
            -- already rate-limited or rejected us. The next RSS/search cycle
            -- skips any indexer whose retry_after is in the future.
            CREATE TABLE IF NOT EXISTS indexer_backoff (
                indexer_id           INTEGER PRIMARY KEY REFERENCES indexers(id) ON DELETE CASCADE,
                retry_after          REAL NOT NULL DEFAULT 0,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                last_status          INTEGER,
                last_reason          TEXT,
                updated_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Indexers
            CREATE TABLE IF NOT EXISTS indexers (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                type       TEXT NOT NULL DEFAULT 'prowlarr',
                url        TEXT,
                api_key    TEXT,
                priority   INTEGER DEFAULT 25,
                enabled    INTEGER DEFAULT 1,
                categories TEXT DEFAULT '[7000,7010,7020]',
                settings   TEXT DEFAULT '{}'
            );

            -- Notification Connections
            CREATE TABLE IF NOT EXISTS notification_connections (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                name               TEXT NOT NULL,
                type               TEXT NOT NULL,
                enabled            INTEGER DEFAULT 1,
                settings           TEXT NOT NULL DEFAULT '{}',
                on_grab            INTEGER DEFAULT 1,
                on_download        INTEGER DEFAULT 1,
                on_upgrade         INTEGER DEFAULT 1,
                on_series_add      INTEGER DEFAULT 1,
                on_health_issue    INTEGER DEFAULT 1,
                on_health_restored INTEGER DEFAULT 0
            );

            -- Import Lists
            CREATE TABLE IF NOT EXISTS import_lists (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                name               TEXT NOT NULL,
                type               TEXT NOT NULL,
                enabled            INTEGER DEFAULT 1,
                quality_profile_id INTEGER REFERENCES quality_profiles(id),
                root_folder_id     INTEGER REFERENCES root_folders(id),
                monitor_mode       TEXT DEFAULT 'all',
                settings           TEXT NOT NULL DEFAULT '{}',
                last_sync          TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS import_list_exclusions (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                source           TEXT NOT NULL,
                external_id      TEXT,
                title            TEXT NOT NULL DEFAULT '',
                title_normalized TEXT NOT NULL DEFAULT '',
                reason           TEXT,
                added_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_import_list_excl_external
            ON import_list_exclusions(source, external_id)
            WHERE external_id IS NOT NULL AND external_id != '';
            CREATE UNIQUE INDEX IF NOT EXISTS idx_import_list_excl_title
            ON import_list_exclusions(source, title_normalized)
            WHERE title_normalized != '';

            -- Series Tags (normalized, separate from JSON column)
            CREATE TABLE IF NOT EXISTS series_tags (
                series_id INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                tag       TEXT NOT NULL,
                PRIMARY KEY (series_id, tag)
            );

            -- Indexer Tags (PR #120) — Sonarr-style per-indexer tag filter.
            -- Rule: an indexer with ZERO tags applies to all series. An
            -- indexer with one or more tags applies only to series whose
            -- own tag set intersects this indexer's tag set. The shared
            -- vocabulary (TEXT tag values) is the same as series_tags so
            -- intersection is a plain SQL JOIN.
            CREATE TABLE IF NOT EXISTS indexer_tags (
                indexer_id INTEGER NOT NULL REFERENCES indexers(id) ON DELETE CASCADE,
                tag        TEXT NOT NULL,
                PRIMARY KEY (indexer_id, tag)
            );

            -- Auto-tagging rules
            CREATE TABLE IF NOT EXISTS auto_tag_rules (
                id                        INTEGER PRIMARY KEY AUTOINCREMENT,
                name                      TEXT NOT NULL,
                remove_tags_automatically INTEGER DEFAULT 0,
                specifications            TEXT NOT NULL DEFAULT '[]',
                tags                      TEXT NOT NULL DEFAULT '[]'
            );

            -- Quality Definitions (min/max file size per quality type)
            CREATE TABLE IF NOT EXISTS quality_definitions (
                quality    TEXT PRIMARY KEY,
                title      TEXT NOT NULL,
                min_size   REAL DEFAULT 0,
                max_size   REAL DEFAULT 0,
                order_num  INTEGER DEFAULT 0
            );

            -- Remote Path Mappings (client path → Mangarr path, for Docker setups)
            CREATE TABLE IF NOT EXISTS remote_path_mappings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                host        TEXT NOT NULL DEFAULT '',
                remote_path TEXT NOT NULL,
                local_path  TEXT NOT NULL
            );

            -- Language Profiles
            CREATE TABLE IF NOT EXISTS language_profiles (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                name      TEXT NOT NULL UNIQUE,
                languages TEXT NOT NULL DEFAULT '["any"]',
                allow_any INTEGER DEFAULT 0
            );

            -- Series chapter map overrides (manual corrections over MangaDex data)
            CREATE TABLE IF NOT EXISTS series_chapter_overrides (
                series_id  INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                chapter    TEXT NOT NULL,
                volume_num REAL,
                PRIMARY KEY (series_id, chapter)
            );
        """)

        add_col('series_tags', 'source', 'TEXT')

        # ── Series quality_profile_id column ─────────────────────────────────
        add_col('series', 'quality_profile_id',    'INTEGER REFERENCES quality_profiles(id)')
        add_col('series', 'language_profile_id',  'INTEGER REFERENCES language_profiles(id)')
        add_col('series', 'preferred_groups',      'TEXT DEFAULT "[]"')
        add_col('series', 'blocked_groups',        'TEXT DEFAULT "[]"')
        add_col('series', 'omnibus_preference',    'TEXT DEFAULT "prefer_individual"')
        # Update strategy (Suwayomi-inspired): always | once | throttled
        add_col('series', 'update_strategy',       "TEXT DEFAULT 'always'")
        add_col('series', 'last_metadata_refresh', 'TEXT')   # ISO datetime of last AniList refresh
        add_col('series', 'metadata_status',        "TEXT DEFAULT 'pending'")
        add_col('series', 'metadata_last_attempt',  'TEXT')
        add_col('series', 'metadata_error',         'TEXT')
        add_col('series', 'chapter_map_source',     'TEXT')
        add_col('series', 'chapter_map_updated_at', 'TEXT')
        add_col('series', 'cover_cached_url',       'TEXT')
        add_col('series', 'cover_updated_at',       'TEXT')
        add_col('series', 'chapter_count_source',   "TEXT DEFAULT 'anilist'")
        add_col('series_aliases', 'source',         'TEXT')
        # No refresh task survives a process restart. Recover any attempt that
        # was persisted while the previous process was shutting down.
        db.execute(
            "UPDATE series SET metadata_status=CASE"
            " WHEN last_metadata_refresh IS NULL THEN 'pending' ELSE 'degraded' END,"
            " metadata_error='previous metadata refresh was interrupted'"
            " WHERE metadata_status='refreshing'"
        )
        db.execute(
            "UPDATE series_metadata_sources SET status=CASE"
            " WHEN last_success_at IS NULL THEN 'pending' ELSE 'degraded' END,"
            " error='previous provider refresh was interrupted'"
            " WHERE status='refreshing'"
        )
        from metadata_provenance import backfill_metadata_provenance

        backfill_metadata_provenance(db)
        # Required scanlator: if set, only grab releases matching this group (strict mode)
        add_col('series', 'required_scanlator',    'TEXT')
        # Source type preference: any | official_only | fan_only
        add_col('series', 'source_type',           "TEXT DEFAULT 'any'")
        # Edition type: standard | official_color | colored | omnibus | deluxe | digital | raw | special | collector | remaster
        add_col('series', 'edition_type',          "TEXT DEFAULT 'standard'")
        # Unique index: (anilist_id, edition_type) allows same series in multiple editions
        db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_series_anilist_edition
            ON series(anilist_id, edition_type) WHERE anilist_id IS NOT NULL
        """)
        add_col('indexers', 'client_id',         'INTEGER REFERENCES download_clients(id)')
        add_col('indexers', 'min_seeders',        'INTEGER DEFAULT 0')
        add_col('indexers', 'seed_ratio',         'REAL DEFAULT 0')
        # Prowlarr sub-indexer attribution (PR #117). When the user uses
        # the "Sync indexers from Prowlarr" flow, each imported sub-indexer
        # becomes its own top-level torznab row. These columns track which
        # Prowlarr instance the row came from + the sub-indexer id within
        # Prowlarr, so a re-sync can dedup ((parent_prowlarr_id, prowlarr_indexer_id)
        # is the natural key) and the UI can group them visually.
        add_col('indexers', 'parent_prowlarr_id',  'INTEGER')
        add_col('indexers', 'prowlarr_indexer_id', 'INTEGER')
        add_col('indexers', 'release_source_scope', 'TEXT')
        add_col('seen', 'release_source_scope', 'TEXT')
        add_col('pending_releases', 'release_source_scope', 'TEXT')
        add_col('pending_releases', 'release_guid', 'TEXT')
        add_col('pending_releases', 'release_context', 'TEXT')
        # Per-purpose indexer toggles (PR #119) — Sonarr/Radarr-style
        # independent control over which flows use this indexer:
        #   use_rss                — included in the RSS poll (`fetch_all_rss`)
        #   use_auto_search        — included in the background grab loop
        #                            (grab_existing, search_complete_pack)
        #   use_interactive_search — included in user-initiated search
        #                            (series-page "find releases", per-volume
        #                             grab button)
        # Default ON so existing indexers keep all-modes behavior; user can
        # narrow per-row in the edit modal. Backward-compat: fetch helpers
        # interpret NULL/missing as 1.
        add_col('indexers', 'use_rss',                'INTEGER DEFAULT 1')
        add_col('indexers', 'use_auto_search',        'INTEGER DEFAULT 1')
        add_col('indexers', 'use_interactive_search', 'INTEGER DEFAULT 1')
        # Per-indexer release-size limits (PR #123) — most-asked-for Sonarr
        # feature that Sonarr doesn't actually have. Some trackers (private
        # premium) only have huge complete-series packs; others only have
        # tiny single-chapter releases. Per-indexer floors/ceilings let the
        # user keep both kinds of trackers active without polluting results.
        # Both stored as megabytes; 0 (or NULL) = no limit on that side.
        # Layered on top of the global indexer_max_size setting — the
        # tighter of the two applies.
        add_col('indexers', 'min_size_mb', 'INTEGER DEFAULT 0')
        add_col('indexers', 'max_size_mb', 'INTEGER DEFAULT 0')
        # Quality-profile upgrade controls (PR #124) — Sonarr v4 / Radarr v5
        # equivalents. Without these, the upgrade engine cannot express
        # "stop CF-driven upgrades at score X" or "ignore tiny score
        # improvements" — leading to either no CF-based upgrades at all,
        # or download loops where the same release re-grabs forever for a
        # +1 score gain.
        #
        #   cutoff_format_score      — once existing release's CF score is
        #                              >= this, no more CF-driven upgrades.
        #                              Default 10000 (effectively unbounded —
        #                              matches TRaSH-Guides recommendation;
        #                              users opt INTO ceilings by lowering).
        #   min_upgrade_format_score — minimum delta (new_score - old_score)
        #                              required to trigger a CF upgrade.
        #                              Default 10. Sonarr's universal answer
        #                              to "I'm in a download loop." Default
        #                              non-zero ships loop-prevention without
        #                              user intervention.
        add_col('quality_profiles', 'cutoff_format_score',      'INTEGER DEFAULT 10000')
        add_col('quality_profiles', 'min_upgrade_format_score', 'INTEGER DEFAULT 10')
        add_col('download_clients', 'post_import_category', 'TEXT DEFAULT ""')
        add_col('download_clients', 'recent_priority',      'TEXT DEFAULT "last"')
        add_col('download_clients', 'older_priority',       'TEXT DEFAULT "last"')
        add_col('download_clients', 'initial_state',        'TEXT DEFAULT "normal"')
        add_col('download_clients', 'sequential_order',     'INTEGER DEFAULT 0')
        add_col('download_clients', 'first_last_first',     'INTEGER DEFAULT 0')
        add_col('download_clients', 'content_layout',       'TEXT DEFAULT "original"')
        add_col('download_clients', 'remove_failed',        'INTEGER DEFAULT 0')

        # ── Seed default delay profile if none exists ─────────────────────────
        if not db.execute("SELECT id FROM delay_profiles LIMIT 1").fetchone():
            db.execute(
                "INSERT INTO delay_profiles(name,order_num,enable_usenet,enable_torrent,"
                " usenet_delay,torrent_delay,is_default) VALUES('Default',0,1,1,0,0,1)"
            )

        # ── Seed default quality definitions if none exist ────────────────────
        if not db.execute("SELECT quality FROM quality_definitions LIMIT 1").fetchone():
            db.executemany(
                "INSERT OR IGNORE INTO quality_definitions(quality,title,min_size,max_size,order_num)"
                " VALUES(?,?,?,?,?)",
                [
                    ('cbz',     'CBZ (Comic Book Archive)', 5.0,   600.0, 1),
                    ('cbr',     'CBR (Comic Book RAR)',     5.0,   600.0, 2),
                    ('epub',    'EPUB',                     0.5,   100.0, 3),
                    ('pdf',     'PDF',                      5.0,   800.0, 4),
                    ('zip',     'ZIP Archive',              5.0,   600.0, 5),
                    ('unknown', 'Unknown',                  0.0,     0.0, 6),
                ]
            )

        # ── Seed default language profile if none exists ──────────────────────
        if not db.execute("SELECT id FROM language_profiles LIMIT 1").fetchone():
            db.executemany(
                "INSERT INTO language_profiles(name,languages,allow_any) VALUES(?,?,?)",
                [
                    ('Any Language',   '["any"]',           1),
                    ('English Only',   '["en"]',            0),
                    ('English + Japanese Raw', '["en","ja"]', 0),
                ]
            )

        # ── Seed CF library + profile presets on a fresh install ─────────────
        # Skip the legacy "Any Quality" seed if we're going to seed the CF
        # library + 4 profile presets (PR #127). Presence of *any* CF means
        # this is an existing install — don't clobber user data.
        _seed_presets = (
            not db.execute("SELECT id FROM custom_formats LIMIT 1").fetchone()
            and not db.execute("SELECT id FROM quality_profiles LIMIT 1").fetchone()
        )
        if _seed_presets:
            from cf_presets import BUILTIN_CUSTOM_FORMATS, PROFILE_PRESETS
            import json as _json
            cf_id_by_name: dict[str, int] = {}
            for cf in BUILTIN_CUSTOM_FORMATS:
                cur = db.execute(
                    "INSERT INTO custom_formats(name, specifications) VALUES(?, ?)",
                    (cf['name'], _json.dumps(cf['specs']))
                )
                cf_id_by_name[cf['name']] = cur.lastrowid
            for preset in PROFILE_PRESETS:
                cur = db.execute(
                    "INSERT INTO quality_profiles(name, qualities, cutoff,"
                    " upgrades_allowed, minimum_custom_format_score,"
                    " cutoff_format_score, min_upgrade_format_score, is_default)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        preset['name'], preset['qualities'], preset['cutoff'],
                        preset['upgrades_allowed'],
                        preset['minimum_custom_format_score'],
                        preset['cutoff_format_score'],
                        preset['min_upgrade_format_score'],
                        1 if preset['is_default'] else 0,
                    )
                )
                profile_id = cur.lastrowid
                for cf_name, score in preset['scores'].items():
                    cf_id = cf_id_by_name.get(cf_name)
                    if cf_id is None or score == 0:
                        continue
                    db.execute(
                        "INSERT INTO quality_profile_custom_formats"
                        "(profile_id, format_id, score) VALUES(?, ?, ?)",
                        (profile_id, cf_id, score)
                    )
        elif not db.execute("SELECT id FROM quality_profiles LIMIT 1").fetchone():
            # Existing-install path: a profile got deleted and the CF library
            # is intact — keep prior behavior so we don't ship a UI with no
            # profiles to choose from.
            db.execute(
                "INSERT INTO quality_profiles(name,qualities,cutoff,upgrades_allowed,"
                " minimum_custom_format_score,is_default) VALUES(?,?,?,?,?,?)",
                ('Any Quality', '["cbz","epub","cbr","pdf","zip","unknown"]', 'cbz', 1, 0, 1)
            )

        # ── Auto-generate API key if none stored ──────────────────────────────
        # Mirrors ensure_api_key() but runs inside the existing init_db
        # transaction so a fresh install commits the seed atomically with
        # the rest of the schema. ensure_api_key() runs again at lifespan
        # startup (defense in depth: catches rows nulled by a bad import,
        # a manual edit, or a partial migration after the app is up).
        _key_row = db.execute("SELECT value FROM settings WHERE key='api_key'").fetchone()
        if not _key_row or not (_key_row['value'] or '').strip():
            import secrets as _secrets
            db.execute(
                "INSERT OR REPLACE INTO settings(key,value) VALUES('api_key',?)",
                (_secrets.token_hex(32),)
            )

        # ── Performance indexes ───────────────────────────────────────────────
        for _idx_stmt in [
            "CREATE INDEX IF NOT EXISTS idx_volumes_series        ON volumes(series_id)",
            "CREATE INDEX IF NOT EXISTS idx_volumes_series_status ON volumes(series_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_volumes_series_volnum ON volumes(series_id, volume_num)",
            "CREATE INDEX IF NOT EXISTS idx_volumes_download_id   ON volumes(download_id)",
            "CREATE INDEX IF NOT EXISTS idx_volumes_download_id_nocase"
            " ON volumes(download_id COLLATE NOCASE)",
            "CREATE INDEX IF NOT EXISTS idx_seen_series           ON seen(series_id)",
            "CREATE INDEX IF NOT EXISTS idx_seen_dlid             ON seen(download_id)",
            "CREATE INDEX IF NOT EXISTS idx_seen_dlid_nocase"
            " ON seen(download_id COLLATE NOCASE)",
            "CREATE INDEX IF NOT EXISTS idx_seen_guid             ON seen(release_guid) WHERE release_guid IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_chapters_series       ON chapters(series_id)",
            "CREATE INDEX IF NOT EXISTS idx_chapters_volid        ON chapters(volume_id)",
            "CREATE INDEX IF NOT EXISTS idx_chapters_dlid_nocase"
            " ON chapters(download_id COLLATE NOCASE)",
            "CREATE INDEX IF NOT EXISTS idx_import_queue_dlid     ON import_queue(download_id)",
            "CREATE INDEX IF NOT EXISTS idx_import_queue_dlid_nocase"
            " ON import_queue(download_id COLLATE NOCASE)",
            "CREATE INDEX IF NOT EXISTS idx_import_queue_status   ON import_queue(status)",
            "CREATE INDEX IF NOT EXISTS idx_import_queue_importing_expiry"
            " ON import_queue(lease_expires_at) WHERE status='importing'",
            "CREATE INDEX IF NOT EXISTS idx_import_publications_state"
            " ON import_publications(state, id)",
            "CREATE INDEX IF NOT EXISTS idx_import_publication_files_publication"
            " ON import_publication_files(publication_id, ordinal)",
            "CREATE INDEX IF NOT EXISTS idx_import_publication_notifications_due"
            " ON import_publication_notifications(state, next_attempt_at,"
            " operation_expires_at, publication_id)",
            "CREATE INDEX IF NOT EXISTS idx_import_publication_notification_deliveries_due"
            " ON import_publication_notification_deliveries(state,"
            " next_attempt_at, operation_expires_at, publication_id,"
            " connection_id)",
            "CREATE INDEX IF NOT EXISTS idx_import_publication_success_effects_due"
            " ON import_publication_success_effects(state, next_attempt_at,"
            " operation_expires_at, publication_id, effect_type)",
            "CREATE INDEX IF NOT EXISTS idx_import_pack_reservations_expiry"
            " ON import_pack_cleanup_reservations(expires_at,"
            " normalized_download_id)",
            "CREATE INDEX IF NOT EXISTS idx_import_pack_tombstones_publication"
            " ON import_pack_cleanup_tombstones(publication_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_volume_file_deletions_active_series"
            " ON volume_file_deletions(series_id, id) WHERE state='active'",
            "CREATE INDEX IF NOT EXISTS idx_events_series         ON events(series_id)",
            "CREATE INDEX IF NOT EXISTS idx_history_series        ON history(series_id)",
            "CREATE INDEX IF NOT EXISTS idx_remote_path_host      ON remote_path_mappings(host)",
            "CREATE INDEX IF NOT EXISTS idx_pending_rel_series    ON pending_releases(series_id)",
            "CREATE INDEX IF NOT EXISTS idx_blocklist_series       ON blocklist(series_id)",
            "CREATE INDEX IF NOT EXISTS idx_import_queue_series    ON import_queue(series_id)",
            "CREATE INDEX IF NOT EXISTS idx_events_series_time     ON events(series_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_metadata_sources_retry ON series_metadata_sources(status, next_retry_at)",
            "CREATE INDEX IF NOT EXISTS idx_metadata_fields_series ON series_metadata_fields(series_id)",
            "CREATE INDEX IF NOT EXISTS idx_metadata_candidates_series ON series_metadata_candidates(series_id, field_name)",
        ]:
            db.execute(_idx_stmt)

        # Cascade volume→chapter status: when a volume is marked downloaded,
        # automatically mark all chapters in that volume as downloaded too.
        # (Volumes are seasons; chapters are episodes — season pack = all episodes owned.)
        db.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_vol_downloaded_cascade
            AFTER UPDATE OF status ON volumes
            WHEN NEW.status = 'downloaded' AND OLD.status != 'downloaded'
            BEGIN
                UPDATE chapters
                SET status = 'downloaded'
                WHERE volume_id = NEW.id
                  AND status != 'downloaded';
            END
        """)

        # ── DDL / Suwayomi tables ─────────────────────────────────────────────
        _execute_script_in_transaction(db, """
            -- MangaDex chapter manifest: per-chapter UUID → num/vol mapping, metadata only
            CREATE TABLE IF NOT EXISTS mangadex_chapters (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id           INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                mangadex_chapter_id TEXT    NOT NULL UNIQUE,
                chapter_num         REAL,
                volume_num          REAL,
                title               TEXT,
                pages               INTEGER,
                scanlation_group    TEXT,
                language            TEXT DEFAULT 'en',
                is_external         INTEGER DEFAULT 0,
                publish_at          TEXT,
                synced_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_mdx_ch_series ON mangadex_chapters(series_id);
            CREATE INDEX IF NOT EXISTS idx_mdx_ch_vol    ON mangadex_chapters(series_id, volume_num);
            CREATE INDEX IF NOT EXISTS idx_mdx_ch_lang   ON mangadex_chapters(series_id, language);

            -- In-flight Suwayomi download jobs
            CREATE TABLE IF NOT EXISTS suwayomi_downloads (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id         INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                volume_num        REAL,
                suwayomi_manga_id INTEGER NOT NULL,
                chapter_ids       TEXT    NOT NULL,
                status            TEXT    DEFAULT 'queued',
                progress          INTEGER DEFAULT 0,
                total             INTEGER DEFAULT 0,
                error             TEXT,
                created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_swy_dl_series ON suwayomi_downloads(series_id);
            CREATE INDEX IF NOT EXISTS idx_swy_dl_status ON suwayomi_downloads(status);

            -- Per-series Suwayomi source linkages (multi-source support)
            CREATE TABLE IF NOT EXISTS suwayomi_sources (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id         INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                source_id         TEXT    NOT NULL,
                source_name       TEXT,
                source_lang       TEXT    DEFAULT 'en',
                suwayomi_manga_id INTEGER,
                source_manga_url  TEXT,
                priority          INTEGER DEFAULT 0,
                source_type       TEXT    DEFAULT 'aggregator',
                linked_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(series_id, source_id)
            );
            CREATE INDEX IF NOT EXISTS idx_swy_src_series ON suwayomi_sources(series_id);
        """)

        # ── DDL / Suwayomi column migrations ─────────────────────────────────
        add_col('series',              'suwayomi_id',          'INTEGER')
        add_col('series',              'ddl_language',         'TEXT')
        add_col('series',              'suwayomi_chapter_map', 'TEXT')
        add_col('download_clients',    'source_id',            'TEXT')
        add_col('download_clients',    'download_path',        'TEXT')
        add_col('download_clients',    'merge_chapters',       'INTEGER DEFAULT 1')
        add_col('suwayomi_downloads',  'chapter_num',          'REAL')
        add_col('series',              'suwayomi_source_id',   'TEXT')

        # ── Recycle bin (PR-1) ───────────────────────────────────────────────
        # Soft-delete state: deleted_at NOT NULL means the series is in the
        # recycle bin. Every multi-row series query MUST filter on
        # deleted_at IS NULL — pinned by tests/python/test_recycle_bin.py
        # and CLAUDE.md hard-invariant entry. Partial index covers the hot
        # recycle-bin page query (rare scan, small partial set).
        add_col('series', 'deleted_at',      'TIMESTAMP')
        add_col('series', 'deletion_reason', 'TEXT')
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_series_deleted_at"
            " ON series(deleted_at) WHERE deleted_at IS NOT NULL"
        )

        # ── Backfill suwayomi_sources from existing mangadex_id linkages ──────
        # A cached manga ID belongs to the persisted source, not necessarily MangaDex.
        db.execute("""
            INSERT OR IGNORE INTO suwayomi_sources
                (series_id, source_id, source_name, source_lang, suwayomi_manga_id, source_type)
            SELECT s.id, 'mangadex', 'MangaDex', COALESCE(s.ddl_language, 'en'),
                   s.suwayomi_id, 'aggregator'
            FROM series s
            WHERE s.mangadex_id IS NOT NULL AND s.suwayomi_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM suwayomi_sources src WHERE src.series_id = s.id
              )
        """)

        # ── Seed DDL settings defaults ────────────────────────────────────────
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('ddl_language','en')")
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('ddl_grab_mode','fallback')")
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('suwayomi_check_interval','21600')")
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('blocklist_ttl_days','90')")

        # ── Backfill quality from import_path for pre-quality-column volumes ──────
        # Volumes imported before the quality column existed have quality=NULL.
        # Infer from the file extension so upgrade badges don't fire incorrectly.
        db.execute("""
            UPDATE volumes
            SET quality = CASE
                WHEN LOWER(SUBSTR(import_path, -4)) = '.cbz' THEN 'cbz'
                WHEN LOWER(SUBSTR(import_path, -4)) = '.zip' THEN 'zip'
                WHEN LOWER(SUBSTR(import_path, -4)) = '.cbr' THEN 'cbr'
                WHEN LOWER(SUBSTR(import_path, -4)) = '.rar' THEN 'rar'
                WHEN LOWER(SUBSTR(import_path, -5)) = '.epub' THEN 'epub'
                WHEN LOWER(SUBSTR(import_path, -5)) = '.mobi' THEN 'mobi'
                WHEN LOWER(SUBSTR(import_path, -4)) = '.pdf'  THEN 'pdf'
            END
            WHERE status = 'downloaded'
              AND quality IS NULL
              AND import_path IS NOT NULL
        """)

        # Chapter metadata backfill: chapters linked to a downloaded volume should
        # inherit the volume's grab/import metadata. Before this migration ~3400
        # downloaded chapters had NULL quality/indexer/import_path because the
        # import code paths weren't stamping them. COALESCE ensures we only fill
        # fields that are currently missing. Independent chapter acquisitions
        # must not inherit observations from an unrelated parent download.
        # Current quarantine ownership deliberately clears file observations;
        # neither legacy backfill below may replace them with volume metadata.
        db.execute("""
            UPDATE chapters
            SET quality        = COALESCE(quality,        (SELECT v.quality        FROM volumes v WHERE v.id = chapters.volume_id)),
                import_path    = COALESCE(import_path,    (SELECT v.import_path    FROM volumes v WHERE v.id = chapters.volume_id)),
                indexer        = COALESCE(indexer,        (SELECT v.indexer        FROM volumes v WHERE v.id = chapters.volume_id)),
                protocol       = COALESCE(protocol,       (SELECT v.protocol       FROM volumes v WHERE v.id = chapters.volume_id)),
                client         = COALESCE(client,         (SELECT v.client         FROM volumes v WHERE v.id = chapters.volume_id)),
                release_group  = COALESCE(release_group,  (SELECT v.release_group  FROM volumes v WHERE v.id = chapters.volume_id)),
                torrent_name   = COALESCE(torrent_name,   (SELECT v.torrent_name   FROM volumes v WHERE v.id = chapters.volume_id)),
                download_id    = COALESCE(download_id,    (SELECT v.download_id    FROM volumes v WHERE v.id = chapters.volume_id)),
                imported_at    = COALESCE(imported_at,    (SELECT v.imported_at    FROM volumes v WHERE v.id = chapters.volume_id)),
                grabbed_at     = COALESCE(grabbed_at,     (SELECT v.grabbed_at     FROM volumes v WHERE v.id = chapters.volume_id)),
                size_bytes     = CASE
                                   WHEN size_bytes IS NULL OR size_bytes = 0
                                     THEN COALESCE((SELECT v.size_bytes FROM volumes v WHERE v.id = chapters.volume_id), 0)
                                   ELSE size_bytes
                                 END
            WHERE status IN ('downloaded','grabbed')
              AND volume_id IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM volumes v WHERE v.id = chapters.volume_id
                    AND (chapters.download_client_id IS NULL
                         OR chapters.download_client_id IS v.download_client_id)
                    AND (chapters.protocol IS NULL OR v.protocol IS NULL
                         OR chapters.protocol = v.protocol)
                    AND (COALESCE(chapters.download_id,'') = ''
                         OR (COALESCE(chapters.protocol,v.protocol) = 'torrent'
                             AND lower(chapters.download_id) = lower(v.download_id))
                         OR (COALESCE(chapters.protocol,v.protocol,'') != 'torrent'
                             AND chapters.download_id = v.download_id))
                    AND (COALESCE(chapters.torrent_url,'') = ''
                         OR chapters.torrent_url = v.source_url)
                    AND (COALESCE(chapters.import_path,'') = ''
                         OR chapters.import_path = v.import_path)
              )
              AND NOT EXISTS (
                  SELECT 1 FROM chapter_quarantine q
                  WHERE q.chapter_id = chapters.id
                    AND q.series_id = chapters.series_id
                    AND q.volume_id = chapters.volume_id
                    AND q.volume_path = chapters.import_path
                    AND q.state IN ('committed','quarantined','restoring')
              )
        """)

        # Uncollected chapter (volume_id IS NULL) quality backfill — derive from
        # filename extension if the chapter has an import_path. These are loose
        # chapter files that never got linked to a volume stub.
        db.execute("""
            UPDATE chapters
            SET quality = CASE
                WHEN LOWER(SUBSTR(import_path, -4)) = '.cbz' THEN 'cbz'
                WHEN LOWER(SUBSTR(import_path, -4)) = '.zip' THEN 'zip'
                WHEN LOWER(SUBSTR(import_path, -4)) = '.cbr' THEN 'cbr'
                WHEN LOWER(SUBSTR(import_path, -4)) = '.rar' THEN 'rar'
                WHEN LOWER(SUBSTR(import_path, -5)) = '.epub' THEN 'epub'
                WHEN LOWER(SUBSTR(import_path, -5)) = '.mobi' THEN 'mobi'
                WHEN LOWER(SUBSTR(import_path, -4)) = '.pdf'  THEN 'pdf'
            END
            WHERE status = 'downloaded'
              AND quality IS NULL
              AND import_path IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM chapter_quarantine q
                  WHERE q.chapter_id = chapters.id
                    AND q.series_id = chapters.series_id
                    AND q.volume_id = chapters.volume_id
                    AND q.volume_path = chapters.import_path
                    AND q.state IN ('committed','quarantined','restoring')
              )
        """)

        # Orphan "downloaded" volume stubs (no file, no quality, no imported_at)
        # are left over from pre-0.x migrations. Reset so the app retries them.
        db.execute("""
            UPDATE volumes
            SET status = 'wanted'
            WHERE status = 'downloaded'
              AND import_path IS NULL
              AND quality IS NULL
              AND imported_at IS NULL
        """)

        # Orphan chapters: volume_id points to a deleted volume. Clear the FK so
        # they reappear as "uncollected" instead of pointing at nothing. Until
        # now, shrinking total_volumes deleted rows without cascading to chapters.
        db.execute("""
            UPDATE chapters
            SET volume_id = NULL
            WHERE volume_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM volumes v WHERE v.id = chapters.volume_id)
        """)

        # Keep schema rebuilds and root-folder migration in this same writer
        # transaction so no newer binary can claim the DB between phases.
        _migrate_schema_constraints_locked(db)
        _backfill_import_notification_deliveries(db)
        _bootstrap_root_folders_locked(db)


# ── Root-folder bootstrap ─────────────────────────────────────────────────────
# Mangarr's historical model allowed a global save_path to stand in for
# root folders. The *arr convention is that root folders are the only
# library-destination mechanism and every series carries a root_folder_id.
# This helper migrates legacy data to that shape on boot:
#   1. If no root folders exist and save_path is non-empty, create a
#      root folder from save_path (labeled "Manga", default=1).
#   2. For any series.root_folder_id IS NULL, assign it to the default
#      root folder (or the first one if no default is flagged).
# Both steps log a schema_migration event so operators can see what
# happened. Once every series has a root_folder_id, the save_path
# fallback paths in the rest of the code can be removed (PR C).

def _bootstrap_root_folders() -> None:
    """Run the one-shot migration from save_path → root folders.

    Idempotent: after the first successful boot, both queries return
    0 rows and this helper is a no-op.
    """
    with _schema_write_transaction() as db:
        _validate_schema_version(db)
        _bootstrap_root_folders_locked(db)


def _bootstrap_root_folders_locked(db: sqlite3.Connection) -> None:
    """Bootstrap root folders inside an existing schema writer transaction."""
    # Step 1: create a root folder from save_path if no folders exist.
    count = db.execute("SELECT COUNT(*) FROM root_folders").fetchone()[0]
    if count == 0:
        sp = (get_cfg('save_path', '') or '').strip()
        if sp:
            db.execute(
                "INSERT INTO root_folders(path, label, is_default)"
                " VALUES(?, 'Manga', 1)",
                (sp,)
            )
            log_event(
                'schema_migration',
                f"bootstrapped root folder from legacy save_path: {sp!r}",
                db=db,
            )
    # Step 2: assign orphan series (root_folder_id IS NULL) to the
    # default root folder. If no default is flagged, pick the
    # lowest-id folder as the fallback.
    default = db.execute(
        "SELECT id FROM root_folders ORDER BY is_default DESC, id LIMIT 1"
    ).fetchone()
    if default is not None:
        cur = db.execute(
            "UPDATE series SET root_folder_id=? WHERE root_folder_id IS NULL",
            (default[0],)
        )
        assigned = cur.rowcount
        if assigned > 0:
            log_event(
                'schema_migration',
                f"assigned {assigned} orphan series to root_folder_id={default[0]}",
                db=db,
            )


# ── Schema constraint migrations ──────────────────────────────────────────────
# SQLite can't ALTER TABLE to add FK or CHECK constraints. The standard
# workaround is the "rebuild" pattern: create a new table with the target
# shape, copy rows in, drop the old table, rename the new into place.
# We use PRAGMA user_version as a migration flag so migrations run exactly
# once per DB. Orphan rows (series_id pointing to a deleted series) are
# dropped during the copy and logged so operators can see what went.


_OWNED_STATUS_CHECK = "status IN ('wanted','grabbed','downloaded')"

_IMPORT_QUEUE_CONSTRAINED_DDL = """
    CREATE TABLE import_queue_new (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        series_id            INTEGER REFERENCES series(id),
        download_id          TEXT,
        download_client_id   INTEGER,
        download_protocol    TEXT
                               CHECK(download_protocol IN ('torrent','nzb')
                                     OR download_protocol IS NULL),
        respect_grab_claims   INTEGER CHECK(respect_grab_claims IN (0,1)),
        torrent_name         TEXT,
        torrent_url          TEXT,
        volume_num           REAL,
        src_dir              TEXT,
        status               TEXT DEFAULT 'pending',
        failed_at            TIMESTAMP,
        lease_owner          TEXT,
        lease_expires_at     TIMESTAMP,
        created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
"""


def _restore_volume_chapter_artifacts(db: sqlite3.Connection) -> None:
    """Recreate hot-path indexes/triggers dropped by SQLite table rebuilds."""
    for stmt in [
        "CREATE INDEX IF NOT EXISTS idx_seen_series ON seen(series_id)",
        "CREATE INDEX IF NOT EXISTS idx_seen_dlid ON seen(download_id)",
        "CREATE INDEX IF NOT EXISTS idx_seen_dlid_nocase"
        " ON seen(download_id COLLATE NOCASE)",
        "CREATE INDEX IF NOT EXISTS idx_seen_guid"
        " ON seen(release_guid) WHERE release_guid IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_volumes_series        ON volumes(series_id)",
        "CREATE INDEX IF NOT EXISTS idx_volumes_series_status ON volumes(series_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_volumes_series_volnum ON volumes(series_id, volume_num)",
        "CREATE INDEX IF NOT EXISTS idx_volumes_download_id   ON volumes(download_id)",
        "CREATE INDEX IF NOT EXISTS idx_volumes_download_id_nocase"
        " ON volumes(download_id COLLATE NOCASE)",
        "CREATE INDEX IF NOT EXISTS idx_chapters_series       ON chapters(series_id)",
        "CREATE INDEX IF NOT EXISTS idx_chapters_volid        ON chapters(volume_id)",
        "CREATE INDEX IF NOT EXISTS idx_chapters_dlid_nocase"
        " ON chapters(download_id COLLATE NOCASE)",
    ]:
        db.execute(stmt)
    db.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_vol_downloaded_cascade
        AFTER UPDATE OF status ON volumes
        WHEN NEW.status = 'downloaded' AND OLD.status != 'downloaded'
        BEGIN
            UPDATE chapters
            SET status = 'downloaded'
            WHERE volume_id = NEW.id
              AND status != 'downloaded';
        END
    """)


def _restore_import_queue_artifacts(db: sqlite3.Connection) -> None:
    """Recreate import-queue indexes dropped by its constraint rebuild."""
    for stmt in [
        "CREATE INDEX IF NOT EXISTS idx_import_queue_dlid"
        " ON import_queue(download_id)",
        "CREATE INDEX IF NOT EXISTS idx_import_queue_dlid_nocase"
        " ON import_queue(download_id COLLATE NOCASE)",
        "CREATE INDEX IF NOT EXISTS idx_import_queue_status"
        " ON import_queue(status)",
        "CREATE INDEX IF NOT EXISTS idx_import_queue_importing_expiry"
        " ON import_queue(lease_expires_at) WHERE status='importing'",
    ]:
        db.execute(stmt)


def _import_queue_protocol_is_constrained(db: sqlite3.Connection) -> bool:
    """Return whether the table has the current download-protocol CHECK."""
    row = db.execute(
        "SELECT sql FROM sqlite_master"
        " WHERE type='table' AND name='import_queue'"
    ).fetchone()
    normalized_sql = "".join(str(row[0] if row else "").lower().split())
    expected = (
        "check(download_protocolin('torrent','nzb')"
        "ordownload_protocolisnull)"
    )
    return expected in normalized_sql


def _enforce_import_queue_protocol_constraint(
    db: sqlite3.Connection,
) -> bool:
    """Rebuild a legacy queue so invalid protocols fail at the DB boundary."""
    if _import_queue_protocol_is_constrained(db):
        return False

    columns = {
        str(row[1])
        for row in db.execute("PRAGMA table_info(import_queue)").fetchall()
    }
    if "download_protocol" in columns:
        invalid = cast(
            int,
            db.execute(
                "SELECT COUNT(*) FROM import_queue"
                " WHERE download_protocol IS NOT NULL"
                " AND download_protocol NOT IN ('torrent','nzb')"
            ).fetchone()[0],
        )
        if invalid:
            raise RuntimeError(
                "cannot enforce import_queue.download_protocol CHECK while "
                f"{invalid} invalid value(s) exist; expected torrent, nzb, or NULL"
            )

    _rebuild_table_copying_known_columns(
        db,
        name="import_queue",
        ddl=_IMPORT_QUEUE_CONSTRAINED_DDL,
    )
    _restore_import_queue_artifacts(db)
    return True


def _migrate_schema_constraints() -> None:
    """Add FK/CHECK constraints that SQLite requires table rebuilds for.

    Idempotent via PRAGMA user_version. Fresh installs already create the
    desired shape, then still flow through these rebuilds once so the
    version stamp is consistent.
    """
    with _schema_write_transaction() as db:
        _migrate_schema_constraints_locked(db)


def _migrate_schema_constraints_locked(db: sqlite3.Connection) -> None:
    """Run pending constraint migrations inside a writer transaction."""
    version = _validate_schema_version(db)
    if version >= _SCHEMA_VERSION_HISTORY_DOWNLOAD_OWNERSHIP:
        # Schema v5 existed briefly with only history ownership. Keep the v5
        # migration idempotent so those unreleased databases also receive the
        # ownership-qualified pack journal shape.
        _migrate_history_download_ownership(db)
        _ensure_acquisition_policy_columns(db)
        _ensure_private_file_claim_schema(db)
        _ensure_rescan_file_operations_schema(db)
        _ensure_pack_ownership_schema(db)
        _ensure_file_identity_schema(db)
        _ensure_blocklist_source_schema(db)
        _migrate_file_identity_provisioning(db)
        return

    if version < _SCHEMA_VERSION_FK_CONSTRAINTS:
        _migrate_series_fk_constraints(db)
        version = _SCHEMA_VERSION_FK_CONSTRAINTS

    if version < _SCHEMA_VERSION_STATUS_CONSTRAINTS:
        _migrate_owned_status_constraints(db)
        version = _SCHEMA_VERSION_STATUS_CONSTRAINTS

    if version < _SCHEMA_VERSION_IMPORT_PUBLICATIONS:
        _migrate_import_publication_journal(db)
        version = _SCHEMA_VERSION_IMPORT_PUBLICATIONS

    if version < _SCHEMA_VERSION_DURABLE_PUBLICATION_SAFETY:
        _migrate_durable_publication_safety(db)
        version = _SCHEMA_VERSION_DURABLE_PUBLICATION_SAFETY

    if version < _SCHEMA_VERSION_HISTORY_DOWNLOAD_OWNERSHIP:
        _migrate_history_download_ownership(db)
    _ensure_acquisition_policy_columns(db)
    _ensure_private_file_claim_schema(db)
    _ensure_rescan_file_operations_schema(db)
    _ensure_pack_ownership_schema(db)
    _ensure_file_identity_schema(db)
    _ensure_blocklist_source_schema(db)
    _migrate_file_identity_provisioning(db)


def _ensure_blocklist_source_schema(db: sqlite3.Connection) -> None:
    """Create durable evidence after rebuilds; initialize configurations only."""
    from release_sources import bind_indexer_scope, facade_matches

    # The standalone constraint entry point also supports older additive shapes.
    for table, column in (
        ("indexers", "release_source_scope"),
        ("seen", "release_source_scope"),
        ("pending_releases", "release_source_scope"),
        ("pending_releases", "release_guid"),
        ("pending_releases", "release_context"),
    ):
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if columns and column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
    _execute_script_in_transaction(
        db,
        """
        CREATE TABLE IF NOT EXISTS blocklist_release_identities (
            blocklist_id INTEGER NOT NULL REFERENCES blocklist(id) ON DELETE CASCADE,
            series_id INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE
                CHECK(typeof(series_id)='integer' AND series_id>0),
            source_scope TEXT COLLATE BINARY NOT NULL CHECK(
                substr(source_scope,7,64) NOT GLOB '*[^0-9a-f]*'
                AND ((length(source_scope)=70 AND substr(source_scope,1,6)='bs1:d:')
                  OR (length(source_scope) BETWEEN 72 AND 90
                      AND substr(source_scope,1,6)='bs1:p:'
                      AND substr(source_scope,71,1)=':'
                      AND CAST(substr(source_scope,72) AS INTEGER)>0
                      AND CAST(CAST(substr(source_scope,72) AS INTEGER) AS TEXT)=substr(source_scope,72)))
            ),
            protocol TEXT NOT NULL CHECK(protocol IN ('torrent','nzb')),
            release_guid TEXT COLLATE BINARY NOT NULL
                CHECK(length(CAST(release_guid AS BLOB)) BETWEEN 1 AND 4096),
            UNIQUE(blocklist_id,series_id,source_scope,protocol,release_guid)
        );
        CREATE INDEX IF NOT EXISTS idx_blocklist_release_identity
            ON blocklist_release_identities(series_id,source_scope,protocol,release_guid);
        CREATE TABLE IF NOT EXISTS release_selection_evidence (
            handle_digest TEXT PRIMARY KEY NOT NULL
                CHECK(length(handle_digest)=64 AND handle_digest NOT GLOB '*[^0-9a-f]*'),
            issuer_kind TEXT NOT NULL CHECK(issuer_kind IN ('browser','api')),
            session_digest TEXT REFERENCES auth_sessions(token_hash) ON DELETE CASCADE,
            issued_at INTEGER NOT NULL CHECK(typeof(issued_at)='integer' AND issued_at>=0),
            expires_at INTEGER NOT NULL CHECK(typeof(expires_at)='integer' AND expires_at=issued_at+900),
            series_id INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE
                CHECK(typeof(series_id)='integer' AND series_id>0),
            volume_id INTEGER NOT NULL REFERENCES volumes(id) ON DELETE CASCADE
                CHECK(typeof(volume_id)='integer' AND volume_id>0),
            snapshot_json TEXT NOT NULL CHECK(length(CAST(snapshot_json AS BLOB)) BETWEEN 1 AND 16384),
            CHECK((issuer_kind='api' AND session_digest IS NULL)
               OR (issuer_kind='browser' AND session_digest IS NOT NULL))
        );
        CREATE INDEX IF NOT EXISTS idx_release_selection_expiry
            ON release_selection_evidence(expires_at);
        CREATE INDEX IF NOT EXISTS idx_release_selection_issuer
            ON release_selection_evidence(issuer_kind,session_digest,issued_at,handle_digest);
        CREATE INDEX IF NOT EXISTS idx_release_selection_oldest
            ON release_selection_evidence(issued_at,handle_digest);
    """,
    )
    has_indexers = db.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='indexers'"
    ).fetchone()
    rows = (
        [dict(row) for row in db.execute("SELECT * FROM indexers ORDER BY id")]
        if has_indexers
        else []
    )
    # Bind parents first so verified facades can capture their root, not a FK.
    for row in rows:
        if row["type"] == "prowlarr" and row["release_source_scope"] is None:
            bind_indexer_scope(db, indexer_id=row["id"], previous=None)
    parents = (
        {
            row["id"]: dict(row)
            for row in db.execute("SELECT * FROM indexers WHERE type='prowlarr'")
        }
        if has_indexers
        else {}
    )
    for row in rows:
        if row["type"] == "prowlarr" or row["release_source_scope"] is not None:
            continue
        parent = parents.get(row.get("parent_prowlarr_id"))
        sub = row.get("prowlarr_indexer_id")
        verified = (
            parent if parent is not None and facade_matches(row, parent, sub) else None
        )
        bind_indexer_scope(
            db,
            indexer_id=row["id"],
            previous=None,
            verified_parent=verified,
            sub_id=sub if verified else None,
        )
    if _validate_schema_version(db) < _SCHEMA_VERSION_BLOCKLIST_SOURCES:
        db.execute(f"PRAGMA user_version={_SCHEMA_VERSION_BLOCKLIST_SOURCES}")


def _file_identity_event_guards(capability: str | None = None) -> str:
    """Share v1/common guards while preserving genuine7's exact generated SQL."""
    standard = (
        "(SELECT count(*) FROM json_each(NEW.payload_json))=3"
        " AND NOT EXISTS(SELECT 1 FROM json_each(NEW.payload_json)"
        " WHERE key NOT IN ('version','snapshot_sha256','manifest_sha256'))"
        " AND json_extract(NEW.payload_json,'$.snapshot_sha256') IS s.snapshot_sha256"
        " AND json_extract(NEW.payload_json,'$.manifest_sha256') IS s.manifest_sha256"
    )
    recovery = (
        "(SELECT count(*) FROM json_each(NEW.payload_json))=4"
        " AND NOT EXISTS(SELECT 1 FROM json_each(NEW.payload_json) WHERE key NOT IN"
        " ('version','before_snapshot_json','before_snapshot_sha256','plan_sha256'))"
        " AND json_extract(NEW.payload_json,'$.before_snapshot_json') IS s.snapshot_json"
        " AND json_extract(NEW.payload_json,'$.before_snapshot_sha256') IS s.snapshot_sha256"
        " AND json_type(NEW.payload_json,'$.plan_sha256')='text'"
        " AND length(json_extract(NEW.payload_json,'$.plan_sha256'))=64"
        " AND json_extract(NEW.payload_json,'$.plan_sha256') NOT GLOB '*[^0-9a-f]*'"
    )
    payload = f"""json_extract(NEW.payload_json,'$.version')=1
                AND CASE WHEN NEW.kind IN ('prepared','activated') THEN ({standard})
                    ELSE ({recovery}) END"""
    declaration = (
        "CREATE TRIGGER IF NOT EXISTS file_identity_events_validate\n"
        "        BEFORE INSERT ON file_identity_events BEGIN"
    )
    end = "END"
    if capability is not None:
        payload = f"(({payload})\n                    OR ({capability}))"
        declaration = "CREATE TRIGGER file_identity_events_validate BEFORE INSERT ON file_identity_events BEGIN"
        end = "END;"
    return f"""
        {declaration}
            SELECT RAISE(ABORT,'invalid identity owner key') WHERE NOT COALESCE(
                json_type(NEW.owner_key_json)='object'
                AND (SELECT count(*) FROM json_each(NEW.owner_key_json))=2
                AND json_type(NEW.owner_key_json,'$.version')='integer'
                AND json_extract(NEW.owner_key_json,'$.version')=1
                AND json_type(NEW.owner_key_json,'$.key')='array'
                AND json_array_length(NEW.owner_key_json,'$.key')=1
                AND CASE WHEN NEW.owner_domain IN ('rescan','publication','deletion','queue_source')
                    THEN json_type(NEW.owner_key_json,'$.key[0]')='integer'
                         AND json_extract(NEW.owner_key_json,'$.key[0]')>0
                    ELSE json_type(NEW.owner_key_json,'$.key[0]')='text'
                         AND length(json_extract(NEW.owner_key_json,'$.key[0]'))>0 END,0);
            SELECT RAISE(ABORT,'stale identity predecessor') WHERE
                NEW.predecessor_event_id IS NOT (SELECT id FROM file_identity_events
                    WHERE owner_domain=NEW.owner_domain AND owner_key_json=NEW.owner_key_json
                    ORDER BY revision DESC LIMIT 1)
                OR NEW.revision != COALESCE((SELECT revision+1 FROM file_identity_events
                    WHERE owner_domain=NEW.owner_domain AND owner_key_json=NEW.owner_key_json
                    ORDER BY revision DESC LIMIT 1),1);
            SELECT RAISE(ABORT,'invalid identity event set/payload') WHERE NOT EXISTS(
                SELECT 1 FROM file_identity_sets s WHERE s.set_id=NEW.set_id
                AND s.owner_domain=NEW.owner_domain AND s.owner_key_json=NEW.owner_key_json
                AND json_type(NEW.payload_json)='object'
                AND json_type(NEW.payload_json,'$.version')='integer'
                AND {payload}
            );
            SELECT RAISE(ABORT,'invalid identity preparation predecessor')
                WHERE NEW.kind='prepared' AND NOT EXISTS(
                    SELECT 1 FROM file_identity_sets s WHERE s.set_id=NEW.set_id
                    AND s.prior_set_id IS (SELECT set_id FROM file_identity_events
                        WHERE id=NEW.predecessor_event_id));
            SELECT RAISE(ABORT,'invalid identity activation predecessor')
                WHERE NEW.kind='activated' AND NOT EXISTS(
                    SELECT 1 FROM file_identity_events p WHERE p.id=NEW.predecessor_event_id
                    AND p.kind='prepared' AND p.set_id=NEW.set_id);
        {end}
    """


def _ensure_file_identity_schema(db: sqlite3.Connection) -> None:
    """Add immutable evidence and one-way, exact-preimage supersession fences.

    JSON hashing/semantic policy belongs to the SQL-only store. These guards
    also protect legacy writers from arbitrary recovery IDs and replacement.
    Old binaries must be stopped before upgrading: the v6 startup refusal
    cannot protect an old process already running against this database.
    """
    from file_identity_store import _PROJECTIONS

    def hex_check(column: str, length: int) -> str:
        return (
            f"typeof({column})='text' AND length({column})={length}"
            f" AND {column} NOT GLOB '*[^0-9a-f]*'"
        )

    domains = (
        "'namespace','rescan','publication','deletion','pack_reservation',"
        "'pack_tombstone','queue_source'"
    )
    _execute_script_in_transaction(db, f"""
        CREATE TABLE IF NOT EXISTS file_identity_authorities (
            authority_id TEXT PRIMARY KEY NOT NULL CHECK({hex_check('authority_id', 32)}),
            incarnation_token TEXT NOT NULL CHECK({hex_check('incarnation_token', 32)}),
            profile_json TEXT NOT NULL CHECK(json_valid(profile_json)),
            declaration_json TEXT NOT NULL CHECK(json_valid(declaration_json)),
            qualification_sha256 TEXT NOT NULL CHECK({hex_check('qualification_sha256', 64)})
        );
        CREATE TABLE IF NOT EXISTS file_identity_epochs (
            epoch_id TEXT PRIMARY KEY NOT NULL CHECK({hex_check('epoch_id', 32)}),
            authority_id TEXT NOT NULL REFERENCES file_identity_authorities(authority_id),
            origin_path TEXT NOT NULL,
            allocation_path TEXT NOT NULL,
            root_path TEXT NOT NULL,
            birth_json TEXT NOT NULL CHECK(json_valid(birth_json))
        );
        CREATE TABLE IF NOT EXISTS file_identity_sets (
            set_id TEXT PRIMARY KEY NOT NULL CHECK({hex_check('set_id', 32)}),
            owner_domain TEXT NOT NULL CHECK(owner_domain IN ({domains})),
            owner_key_json TEXT NOT NULL CHECK(json_valid(owner_key_json)),
            prior_set_id TEXT REFERENCES file_identity_sets(set_id),
            snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json)),
            snapshot_sha256 TEXT NOT NULL CHECK({hex_check('snapshot_sha256', 64)}),
            origin TEXT NOT NULL CHECK(origin IN ('birth','exact_legacy','recovery')),
            epoch_id TEXT NOT NULL REFERENCES file_identity_epochs(epoch_id),
            manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)),
            manifest_sha256 TEXT NOT NULL CHECK({hex_check('manifest_sha256', 64)})
        );
        CREATE INDEX IF NOT EXISTS file_identity_sets_owner
            ON file_identity_sets(owner_domain,owner_key_json);
        CREATE TABLE IF NOT EXISTS file_identity_events (
            id INTEGER PRIMARY KEY,
            owner_domain TEXT NOT NULL CHECK(owner_domain IN ({domains})),
            owner_key_json TEXT NOT NULL CHECK(json_valid(owner_key_json)),
            revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>0),
            predecessor_event_id INTEGER REFERENCES file_identity_events(id),
            set_id TEXT NOT NULL REFERENCES file_identity_sets(set_id),
            kind TEXT NOT NULL CHECK(kind IN ('prepared','activated','recovery_intent',
                'recovery_step','superseded','retained','retired')),
            payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
            UNIQUE(owner_domain,owner_key_json,revision)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS file_identity_events_prepared
            ON file_identity_events(set_id) WHERE kind='prepared';
        CREATE UNIQUE INDEX IF NOT EXISTS file_identity_events_activated
            ON file_identity_events(set_id) WHERE kind='activated';
        CREATE UNIQUE INDEX IF NOT EXISTS file_identity_events_owner_scalar
            ON file_identity_events(owner_domain,
                json_extract(owner_key_json,'$.key[0]'),revision);
    """)
    for suffix, pk in (
        ("authorities", "authority_id"), ("epochs", "epoch_id"),
        ("sets", "set_id"), ("events", "id"),
    ):
        table = f"file_identity_{suffix}"
        for action in ("UPDATE", "DELETE"):
            db.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()}"
                f" BEFORE {action} ON {table} BEGIN"
                " SELECT RAISE(ABORT,'immutable file identity row'); END"
            )
        duplicate = f"EXISTS(SELECT 1 FROM {table} WHERE {pk}=NEW.{pk})"
        if suffix == "events":
            duplicate += (
                " OR EXISTS(SELECT 1 FROM file_identity_events WHERE"
                " owner_domain=NEW.owner_domain AND owner_key_json=NEW.owner_key_json"
                " AND revision=NEW.revision)"
                " OR EXISTS(SELECT 1 FROM file_identity_events WHERE"
                " owner_domain=NEW.owner_domain AND revision=NEW.revision"
                " AND json_extract(owner_key_json,'$.key[0]')"
                " IS json_extract(NEW.owner_key_json,'$.key[0]'))"
                " OR (NEW.kind IN ('prepared','activated') AND EXISTS(SELECT 1"
                " FROM file_identity_events WHERE set_id=NEW.set_id AND kind=NEW.kind))"
            )
        db.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_no_replace BEFORE INSERT ON {table}"
            f" WHEN {duplicate} BEGIN SELECT RAISE(ABORT,'immutable identity replacement'); END"
        )

    _ = db.execute(_file_identity_event_guards())

    for table, domain in (("rescan_file_operations", "rescan"), ("volume_file_deletions", "deletion")):
        columns = {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}
        if "recovery_event_id" not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN recovery_event_id INTEGER REFERENCES file_identity_events(id)")
            columns.add("recovery_event_id")
        unchanged = " AND ".join(f"NEW.{column} IS OLD.{column}" for column in sorted(columns - {"recovery_event_id"}))
        projected = sorted(_PROJECTIONS[table])
        replacement_key = "id=NEW.id"
        if table == "rescan_file_operations":
            replacement_key += " OR operation_token=NEW.operation_token"
        matches = " AND ".join(
            f"json_type(s.snapshot_json,'$.root.row.{column}') IS"
            f" CASE typeof(OLD.{column}) WHEN 'null' THEN 'null'"
            " WHEN 'integer' THEN 'integer' WHEN 'real' THEN 'real' ELSE 'text' END"
            f" AND json_extract(s.snapshot_json,'$.root.row.{column}') IS OLD.{column}"
            for column in projected
        )
        db.execute(f"""
            CREATE TRIGGER IF NOT EXISTS file_identity_{table}_insert
            BEFORE INSERT ON {table} BEGIN
                SELECT RAISE(ABORT,'recovery enrollment requires journal update')
                WHERE NEW.recovery_event_id IS NOT NULL OR EXISTS(
                    SELECT 1 FROM {table} WHERE ({replacement_key})
                    AND recovery_event_id IS NOT NULL);
            END
        """)
        db.execute(f"""
            CREATE TRIGGER IF NOT EXISTS file_identity_{table}_update
            BEFORE UPDATE ON {table} BEGIN
                SELECT RAISE(ABORT,'superseded journal is immutable')
                    WHERE OLD.recovery_event_id IS NOT NULL;
                SELECT RAISE(ABORT,'recovery preimage changed during update')
                    WHERE NEW.recovery_event_id IS NOT NULL AND NOT ({unchanged});
                SELECT RAISE(ABORT,'invalid recovery supersession preimage')
                WHERE NEW.recovery_event_id IS NOT NULL AND NOT EXISTS(
                    SELECT 1 FROM file_identity_events e JOIN file_identity_sets s USING(set_id)
                    WHERE e.id=NEW.recovery_event_id AND e.kind='superseded'
                    AND e.owner_domain='{domain}' AND s.owner_domain=e.owner_domain
                    AND e.owner_key_json=json_object('key',json_array(OLD.id),'version',1)
                    AND s.owner_key_json=e.owner_key_json
                    AND e.id=(SELECT id FROM file_identity_events WHERE
                        owner_domain=e.owner_domain AND owner_key_json=e.owner_key_json
                        ORDER BY revision DESC LIMIT 1)
                    AND json_extract(e.payload_json,'$.before_snapshot_json') IS s.snapshot_json
                    AND json_extract(e.payload_json,'$.before_snapshot_sha256') IS s.snapshot_sha256
                    AND json_type(s.snapshot_json,'$.version')='integer'
                    AND json_extract(s.snapshot_json,'$.version')=1
                    AND json_extract(s.snapshot_json,'$.owner.domain') IS '{domain}'
                    AND json_array_length(s.snapshot_json,'$.owner.key')=1
                    AND json_type(s.snapshot_json,'$.owner.key[0]')='integer'
                    AND json_extract(s.snapshot_json,'$.owner.key[0]') IS OLD.id
                    AND json_extract(s.snapshot_json,'$.root.table') IS '{table}'
                    AND json_array_length(s.snapshot_json,'$.root.key')=1
                    AND json_type(s.snapshot_json,'$.root.key[0]')='integer'
                    AND json_extract(s.snapshot_json,'$.root.key[0]') IS OLD.id
                    AND (SELECT count(*) FROM json_each(s.snapshot_json,'$.root.row'))={len(projected)}
                    AND {matches}
                );
            END
        """)
        db.execute(f"""
            CREATE TRIGGER IF NOT EXISTS file_identity_{table}_delete
            BEFORE DELETE ON {table} WHEN OLD.recovery_event_id IS NOT NULL BEGIN
                SELECT RAISE(ABORT,'superseded journal is immutable');
            END
        """)

    indexes = (
        ("rescan_file_operations_active_volume", "rescan_file_operations", "volume_id", True,
         "state IN ('prepared','published','db_committed','rollback')"),
        ("rescan_file_operations_active_series", "rescan_file_operations", "series_id,id", False,
         "state IN ('prepared','published','db_committed','rollback')"),
        ("rescan_file_operations_active_keyset", "rescan_file_operations", "id", False,
         "state IN ('prepared','published','db_committed','rollback')"),
        ("idx_volume_file_deletions_active_volume", "volume_file_deletions", "volume_id", True, "state='active'"),
        ("idx_volume_file_deletions_active_series", "volume_file_deletions", "series_id,id", False, "state='active'"),
    )
    for name, table, indexed, unique, predicate in indexes:
        existing = db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()
        if existing is not None and "recovery_event_id IS NULL" not in existing[0]:
            db.execute(f"DROP INDEX {name}")
        db.execute(f"CREATE {'UNIQUE ' if unique else ''}INDEX IF NOT EXISTS {name} ON {table}({indexed}) WHERE {predicate} AND recovery_event_id IS NULL")
    if _validate_schema_version(db) < _SCHEMA_VERSION_FILE_IDENTITY:
        db.execute(f"PRAGMA user_version={_SCHEMA_VERSION_FILE_IDENTITY}")


def _migrate_file_identity_provisioning(db: sqlite3.Connection) -> None:
    """Persist intent1/capability2 without changing legacy evidence or authority.

    Called only inside the schema writer transaction. SQL checks structure and
    immutable bindings; the typed readers independently recompute all digests.
    """
    if _validate_schema_version(db) >= _SCHEMA_VERSION_FILE_IDENTITY_PROVISIONING:
        return

    def extract(document: str, name: str) -> str:
        return f"json_extract({document},'$.{name}')"

    def typed(document: str, name: str, kind: str) -> str:
        return f"json_type({document},'$.{name}')='{kind}'"

    def hex_value(value: str, length: int) -> str:
        return (
            f"typeof({value})='text' AND length({value})={length}"
            f" AND {value} NOT GLOB '*[^0-9a-f]*'"
        )

    def keys(document: str, names: str) -> str:
        fields = names.split()
        allowed = ",".join(f"'{name}'" for name in fields)
        return (
            f"json_type({document})='object'"
            f" AND (SELECT count(*) FROM json_each({document}))={len(fields)}"
            f" AND (SELECT count(DISTINCT key) FROM json_each({document}))={len(fields)}"
            f" AND NOT EXISTS(SELECT 1 FROM json_each({document}) WHERE key NOT IN ({allowed}))"
        )

    def text(document: str, name: str) -> str:
        return f"{typed(document, name, 'text')} AND length(trim({extract(document, name)}))>0"

    def version(document: str, number: int) -> str:
        return f"{typed(document, 'version', 'integer')} AND {extract(document, 'version')}={number}"

    p = "NEW.payload_json"
    scope, profile = extract(p, "scope"), extract(p, "profile")
    client, server = (
        extract(profile, "client_contract"),
        extract(profile, "server_contract"),
    )
    mount = extract(client, "mount")
    plan = extract(p, "plan_json")
    epochs = extract(plan, "epoch_bindings_json")
    declaration = extract(plan, "declaration_json")
    checks = [
        keys(
            p,
            "version kind plan_json plan_sha256 inventory_sha256 epoch_id authority_id "
            "authority_incarnation scope profile origin_path allocation_path root_path namespace_owner allocation_token",
        ),
        version(p, 1),
        f"{extract(p, 'kind')} IS 'epoch-provision-intent'",
        *[
            hex_value(extract(p, name), 32)
            for name in (
                "epoch_id",
                "authority_id",
                "authority_incarnation",
                "allocation_token",
            )
        ],
        *[
            hex_value(extract(p, name), 64)
            for name in ("plan_sha256", "inventory_sha256")
        ],
        *[
            text(p, name)
            for name in ("plan_json", "origin_path", "allocation_path", "root_path")
        ],
        f"{extract(p, 'allocation_token')} IS NEW.intent_id",
        f"{extract(p, 'authority_id')} IS NEW.authority_id",
        keys(extract(p, "namespace_owner"), "domain key"),
        f"{extract(p, 'namespace_owner.domain')} IS 'namespace'",
        typed(p, "namespace_owner.key", "array"),
        f"json_array_length({p},'$.namespace_owner.key')=1",
        f"{extract(p, 'namespace_owner.key[0]')} IS {extract(p, 'allocation_path')}",
        typed(p, "scope", "object"),
        keys(scope, "version scope_path scope_token"),
        version(scope, 1),
        text(scope, "scope_path"),
        hex_value(extract(scope, "scope_token"), 32),
        typed(p, "profile", "object"),
        keys(
            profile,
            "version profile_id revision qualification_sha256 client_contract server_contract",
        ),
        version(profile, 1),
        text(profile, "profile_id"),
        typed(profile, "revision", "integer"),
        f"{extract(profile, 'revision')}>0",
        hex_value(extract(profile, "qualification_sha256"), 64),
        typed(profile, "client_contract", "object"),
        keys(client, "platforms mount"),
        typed(client, "platforms", "array"),
        f"json_array_length({client},'$.platforms')>0",
        typed(client, "mount", "object"),
        keys(mount, "filesystem nfs_version transport security hard"),
        *[
            text(mount, name)
            for name in ("filesystem", "nfs_version", "transport", "security")
        ],
        f"json_type({mount},'$.hard') IN ('true','false')",
        typed(profile, "server_contract", "object"),
        keys(
            server,
            "vendor version filesystem exporter handle_contract allowed_transitions recovery_transitions",
        ),
        *[
            text(server, name)
            for name in (
                "vendor",
                "version",
                "filesystem",
                "exporter",
                "handle_contract",
            )
        ],
    ]
    platform = "platform.value"
    platform_checks = [
        "platform.type='object'",
        keys(platform, "arch kernel_release libc_version encoder_revision"),
        *[
            text(platform, name)
            for name in ("arch", "kernel_release", "libc_version", "encoder_revision")
        ],
    ]
    checks.append(
        f"NOT EXISTS(SELECT 1 FROM json_each({client},'$.platforms') platform"
        f" WHERE NOT COALESCE(({' AND '.join(platform_checks)}),0)"
        f" OR EXISTS(SELECT 1 FROM json_each({client},'$.platforms') earlier"
        " WHERE earlier.key<platform.key AND earlier.value>=platform.value))"
    )
    for name in ("allowed_transitions", "recovery_transitions"):
        checks.extend(
            [
                typed(server, name, "array"),
                f"NOT EXISTS(SELECT 1 FROM json_each({server},'$.{name}') item"
                " WHERE item.type!='text' OR length(trim(item.value))=0"
                f" OR EXISTS(SELECT 1 FROM json_each({server},'$.{name}') earlier"
                " WHERE earlier.key<item.key AND earlier.value>=item.value))",
            ]
        )
    origin, allocation, root = (
        extract(p, n) for n in ("origin_path", "allocation_path", "root_path")
    )
    scope_path = extract(scope, "scope_path")
    checks.extend(
        [
            f"({origin}={scope_path} OR substr({origin},1,length(rtrim({scope_path},'/'))+1)=rtrim({scope_path},'/')||'/')",
            f"{allocation} IS rtrim({origin},'/')||'/.mangarr-epoch-'||{extract(p, 'epoch_id')}",
            f"{root} IS {allocation}||'/.mangarr-claims'",
            "EXISTS(SELECT 1 FROM file_identity_authorities a WHERE a.authority_id=NEW.authority_id"
            f" AND a.incarnation_token IS {extract(p, 'authority_incarnation')}"
            f" AND a.declaration_json IS {scope} AND a.profile_json IS {profile}"
            f" AND a.qualification_sha256 IS {extract(profile, 'qualification_sha256')})",
            f"json_valid({plan})",
            f"json_type({plan})='object'",
            version(plan, 2),
            f"{extract(plan, 'sha256')} IS {extract(p, 'plan_sha256')}",
            f"{extract(plan, 'inventory.sha256')} IS {extract(p, 'inventory_sha256')}",
            typed(plan, "inventory", "object"),
            f"json_valid({epochs})",
            f"json_type({epochs})='object'",
            version(epochs, 2),
            typed(epochs, "provision", "array"),
            typed(epochs, "existing", "array"),
            typed(epochs, "allocation_capabilities", "array"),
            f"(SELECT count(*) FROM json_each({epochs},'$.provision') entry"
            f" WHERE json_extract(entry.value,'$.epoch_id') IS {extract(p, 'epoch_id')})=1",
            f"EXISTS(SELECT 1 FROM json_each({epochs},'$.provision') entry WHERE"
            f" json_extract(entry.value,'$.epoch_id') IS {extract(p, 'epoch_id')}"
            f" AND json_extract(entry.value,'$.authority_id') IS NEW.authority_id"
            f" AND json_extract(entry.value,'$.origin_parent') IS {origin}"
            f" AND json_extract(entry.value,'$.allocation_parent') IS {allocation}"
            f" AND json_extract(entry.value,'$.root_path') IS {root}"
            " AND json_extract(entry.value,'$.handling') IS 'retain-old-disable-new-allocation')",
            f"NOT EXISTS(SELECT 1 FROM json_each({epochs},'$.existing') entry"
            f" WHERE json_extract(entry.value,'$.epoch_id') IS {extract(p, 'epoch_id')})",
            f"NOT EXISTS(SELECT 1 FROM json_each({epochs},'$.allocation_capabilities') entry"
            f" WHERE json_extract(entry.value,'$.epoch_id') IS {extract(p, 'epoch_id')})",
            f"json_valid({declaration})",
            f"json_type({declaration})='object'",
            version(declaration, 2),
            typed(declaration, "authorities", "array"),
            f"(SELECT count(*) FROM json_each({declaration},'$.authorities') a"
            " WHERE json_extract(a.value,'$.authority_id') IS NEW.authority_id)=1",
            f"EXISTS(SELECT 1 FROM json_each({declaration},'$.authorities') a"
            " WHERE json_extract(a.value,'$.authority_id') IS NEW.authority_id"
            f" AND json_extract(a.value,'$.incarnation_token') IS {extract(p, 'authority_incarnation')}"
            f" AND json_extract(a.value,'$.scope') IS {scope}"
            f" AND json_extract(a.value,'$.profile') IS {profile})",
        ]
    )
    birth_tuple = " AND ".join(
        f"json_extract(NEW.birth_json,'$.controlled_birth.{name}') IS json_extract(i.payload_json,'$.{name}')"
        for name in (
            "epoch_id",
            "authority_id",
            "authority_incarnation",
            "origin_path",
            "allocation_path",
            "root_path",
            "namespace_owner",
            "plan_sha256",
        )
    )
    _execute_script_in_transaction(
        db,
        f"""
        CREATE TABLE file_identity_provision_intents (
            intent_id TEXT PRIMARY KEY NOT NULL CHECK({hex_value("intent_id", 32)}),
            authority_id TEXT NOT NULL REFERENCES file_identity_authorities(authority_id)
                CHECK({hex_value("authority_id", 32)}),
            payload_json TEXT NOT NULL CHECK(typeof(payload_json)='text' AND json_valid(payload_json)),
            payload_sha256 TEXT NOT NULL CHECK({hex_value("payload_sha256", 64)})
        );
        CREATE UNIQUE INDEX file_identity_provision_epoch ON file_identity_provision_intents(json_extract(payload_json,'$.epoch_id'));
        CREATE UNIQUE INDEX file_identity_provision_allocation ON file_identity_provision_intents(json_extract(payload_json,'$.allocation_path'));
        CREATE UNIQUE INDEX file_identity_provision_root ON file_identity_provision_intents(json_extract(payload_json,'$.root_path'));
        CREATE TRIGGER file_identity_provision_intents_validate BEFORE INSERT ON file_identity_provision_intents BEGIN
            SELECT RAISE(ABORT,'invalid provisioning intent') WHERE NOT COALESCE(({" AND ".join(checks)}),0);
            SELECT RAISE(ABORT,'reserved provisioning tuple') WHERE EXISTS(
                SELECT 1 FROM file_identity_epochs e WHERE e.epoch_id IS {extract(p, "epoch_id")}
                OR e.allocation_path IN ({allocation},{root}) OR e.root_path IN ({allocation},{root}));
        END;
        CREATE TRIGGER file_identity_provision_intents_no_update BEFORE UPDATE ON file_identity_provision_intents BEGIN
            SELECT RAISE(ABORT,'immutable file identity row');
        END;
        CREATE TRIGGER file_identity_provision_intents_no_delete BEFORE DELETE ON file_identity_provision_intents BEGIN
            SELECT RAISE(ABORT,'immutable file identity row');
        END;
        CREATE TRIGGER file_identity_provision_intents_no_replace BEFORE INSERT ON file_identity_provision_intents
            WHEN EXISTS(SELECT 1 FROM file_identity_provision_intents WHERE intent_id=NEW.intent_id
                OR json_extract(payload_json,'$.epoch_id') IS {extract(p, "epoch_id")}
                OR json_extract(payload_json,'$.allocation_path') IS {allocation}
                OR json_extract(payload_json,'$.root_path') IS {root}) BEGIN
            SELECT RAISE(ABORT,'immutable identity replacement');
        END;
        CREATE TRIGGER file_identity_provision_epoch_validate BEFORE INSERT ON file_identity_epochs BEGIN
            SELECT RAISE(ABORT,'foreign provisioning reservation') WHERE EXISTS(
                SELECT 1 FROM file_identity_provision_intents i WHERE
                (json_extract(i.payload_json,'$.epoch_id') IS NEW.epoch_id
                    OR json_extract(i.payload_json,'$.allocation_path') IN (NEW.allocation_path,NEW.root_path)
                    OR json_extract(i.payload_json,'$.root_path') IN (NEW.allocation_path,NEW.root_path))
                AND NOT COALESCE((json_type(NEW.birth_json,'$.version')='integer'
                    AND json_extract(NEW.birth_json,'$.version')=2
                    AND json_type(NEW.birth_json,'$.controlled_birth.version')='integer'
                    AND json_extract(NEW.birth_json,'$.controlled_birth.version')=2
                    AND {birth_tuple}
                    AND json_extract(NEW.birth_json,'$.controlled_birth.intent_id') IS i.intent_id
                    AND json_extract(NEW.birth_json,'$.controlled_birth.allocation_token') IS i.intent_id
                    AND json_extract(i.payload_json,'$.epoch_id') IS NEW.epoch_id
                    AND i.authority_id IS NEW.authority_id
                    AND json_extract(i.payload_json,'$.origin_path') IS NEW.origin_path
                    AND json_extract(i.payload_json,'$.allocation_path') IS NEW.allocation_path
                    AND json_extract(i.payload_json,'$.root_path') IS NEW.root_path),0));
            SELECT RAISE(ABORT,'missing provisioning reservation') WHERE
                json_extract(NEW.birth_json,'$.version')=2 AND NOT EXISTS(
                    SELECT 1 FROM file_identity_provision_intents WHERE
                    intent_id IS json_extract(NEW.birth_json,'$.controlled_birth.intent_id')
                    AND json_extract(payload_json,'$.epoch_id') IS NEW.epoch_id);
        END;
    """,
    )

    capability = [
        "NEW.kind='recovery_step'",
        "NEW.owner_domain='namespace'",
        version(p, 2),
        keys(
            p,
            "version kind before_snapshot_json before_snapshot_sha256 plan_sha256 intent_id activation_event_id "
            "epoch_id authority_id authority_incarnation origin_path allocation_path root_path birth_sha256 state "
            "prior_capability_event_id consumer_contract_sha256",
        ),
        f"{extract(p, 'kind')} IS 'allocation-capability'",
        *[
            hex_value(extract(p, n), 32)
            for n in ("intent_id", "epoch_id", "authority_id", "authority_incarnation")
        ],
        *[
            hex_value(extract(p, n), 64)
            for n in (
                "before_snapshot_sha256",
                "plan_sha256",
                "birth_sha256",
                "consumer_contract_sha256",
            )
        ],
        *[
            text(p, n)
            for n in (
                "origin_path",
                "allocation_path",
                "root_path",
                "before_snapshot_json",
            )
        ],
        typed(p, "activation_event_id", "integer"),
        f"{extract(p, 'activation_event_id')}>0",
        f"{extract(p, 'state')} IN ('grant','revoke')",
        f"({typed(p, 'prior_capability_event_id', 'null')} OR ({typed(p, 'prior_capability_event_id', 'integer')} AND {extract(p, 'prior_capability_event_id')}>0))",
        f"{extract(p, 'before_snapshot_json')} IS s.snapshot_json",
        f"{extract(p, 'before_snapshot_sha256')} IS s.snapshot_sha256",
    ]
    birth = "e.birth_json"
    cb = extract(birth, "controlled_birth")
    manifest, snapshot = "s.manifest_json", "s.snapshot_json"
    tuple_checks = [
        "s.epoch_id=e.epoch_id",
        "s.origin='birth'",
        "e.authority_id=i.authority_id",
        f"e.epoch_id IS {extract(p, 'epoch_id')}",
        f"e.authority_id IS {extract(p, 'authority_id')}",
        f"i.intent_id IS {extract(p, 'intent_id')}",
        f"a.incarnation_token IS {extract(p, 'authority_incarnation')}",
        "a.authority_id=e.authority_id",
        version(birth, 2),
        version(cb, 2),
        f"{extract(birth, 'layout')} IS 'dedicated_parent'",
        f"{extract(birth, 'enrollment')} IS 'birth'",
        typed(birth, "legacy_namespace", "null"),
        version(snapshot, 2),
        f"{extract(snapshot, 'owner.domain')} IS 'namespace'",
        f"{extract(snapshot, 'owner.key')} IS json_extract(NEW.owner_key_json,'$.key')",
        f"{extract(snapshot, 'root.table')} IS 'file_claim_namespaces'",
        f"{extract(snapshot, 'root.key')} IS json_extract(NEW.owner_key_json,'$.key')",
        f"{extract(snapshot, 'root.row.parent_path')} IS e.allocation_path",
        version(manifest, 2),
        f"{extract(manifest, 'phase')} IS 'epoch-provisioned'",
        f"{extract(manifest, 'snapshot_sha256')} IS s.snapshot_sha256",
        keys(extract(manifest, "intent"), "kind snapshot_sha256 intent_id plan_sha256"),
        f"{extract(manifest, 'intent.kind')} IS 'epoch-provision'",
        f"{extract(manifest, 'intent.intent_id')} IS i.intent_id",
        f"{extract(manifest, 'intent.plan_sha256')} IS json_extract(i.payload_json,'$.plan_sha256')",
        f"{extract(manifest, 'intent.snapshot_sha256')} IS s.snapshot_sha256",
        f"{extract(cb, 'intent_id')} IS i.intent_id",
        f"{extract(cb, 'allocation_token')} IS i.intent_id",
        f"{extract(cb, 'namespace_owner')} IS json_extract(i.payload_json,'$.namespace_owner')",
        f"{extract(cb, 'plan_sha256')} IS json_extract(i.payload_json,'$.plan_sha256')",
        "json_extract(i.payload_json,'$.scope') IS a.declaration_json",
        "json_extract(i.payload_json,'$.profile') IS a.profile_json",
        "json_extract(i.payload_json,'$.profile.qualification_sha256') IS a.qualification_sha256",
        "json_extract(NEW.owner_key_json,'$.key[0]') IS e.allocation_path",
    ]
    for name in (
        "epoch_id",
        "authority_id",
        "authority_incarnation",
        "origin_path",
        "allocation_path",
        "root_path",
    ):
        tuple_checks.extend(
            [
                f"{extract(p, name)} IS json_extract(i.payload_json,'$.{name}')",
                f"{extract(cb, name)} IS {extract(p, name)}",
            ]
        )
        if name.endswith("_path"):
            tuple_checks.append(f"e.{name} IS {extract(p, name)}")
    tuple_checks.extend(
        [
            typed(snapshot, "related", "array"),
            f"json_array_length({snapshot},'$.related')=2",
        ]
    )
    for table, alias, primary, columns in (
        (
            "file_identity_epochs",
            "e",
            "epoch_id",
            "epoch_id authority_id origin_path allocation_path root_path birth_json",
        ),
        (
            "file_identity_provision_intents",
            "i",
            "intent_id",
            "intent_id authority_id payload_json payload_sha256",
        ),
    ):
        record_checks = [
            "json_extract(record.value,'$.table') IS '" + table + "'",
            "json_type(record.value,'$.key')='array'",
            "json_array_length(record.value,'$.key')=1",
            f"json_extract(record.value,'$.key[0]') IS {alias}.{primary}",
            keys("json_extract(record.value,'$.row')", columns),
            *[
                f"json_extract(record.value,'$.row.{column}') IS {alias}.{column}"
                for column in columns.split()
            ],
        ]
        tuple_checks.append(
            f"EXISTS(SELECT 1 FROM json_each({snapshot},'$.related') record WHERE "
            + " AND ".join(record_checks)
            + ")"
        )
    capability.append(
        "EXISTS(SELECT 1 FROM file_identity_epochs e JOIN file_identity_provision_intents i"
        " ON i.intent_id=json_extract(e.birth_json,'$.controlled_birth.intent_id')"
        " JOIN file_identity_authorities a ON a.authority_id=e.authority_id WHERE "
        + " AND ".join(tuple_checks)
        + ")"
    )
    capability.append(
        "EXISTS(SELECT 1 FROM file_identity_events activation JOIN file_identity_events prepared"
        " ON prepared.id=activation.predecessor_event_id WHERE"
        f" activation.id IS {extract(p, 'activation_event_id')}"
        " AND activation.kind='activated' AND activation.set_id=NEW.set_id"
        " AND activation.owner_domain=NEW.owner_domain AND activation.owner_key_json=NEW.owner_key_json"
        " AND prepared.kind='prepared' AND prepared.set_id=NEW.set_id"
        " AND prepared.owner_domain=NEW.owner_domain AND prepared.owner_key_json=NEW.owner_key_json)"
    )
    unchanged = " AND ".join(
        f"json_extract(prior.payload_json,'$.{name}') IS {extract(p, name)}"
        for name in (
            "intent_id",
            "activation_event_id",
            "epoch_id",
            "authority_id",
            "authority_incarnation",
            "origin_path",
            "allocation_path",
            "root_path",
            "birth_sha256",
            "consumer_contract_sha256",
            "before_snapshot_json",
            "before_snapshot_sha256",
        )
    )
    capability.append(f"""(
        ({extract(p, "state")}='grant'
            AND {typed(p, "prior_capability_event_id", "null")}
            AND {extract(p, "activation_event_id")} IS NEW.predecessor_event_id
            AND {extract(p, "plan_sha256")} IS (SELECT json_extract(payload_json,'$.plan_sha256')
                FROM file_identity_provision_intents WHERE intent_id IS {extract(p, "intent_id")})
            AND NOT EXISTS(SELECT 1 FROM file_identity_events prior
                WHERE json_extract(prior.payload_json,'$.version')=2
                AND json_extract(prior.payload_json,'$.kind')='allocation-capability'
                AND json_extract(prior.payload_json,'$.epoch_id') IS {extract(p, "epoch_id")}))
        OR ({extract(p, "state")}='revoke' AND EXISTS(
            SELECT 1 FROM file_identity_events prior WHERE prior.id IS {extract(p, "prior_capability_event_id")}
            AND prior.kind='recovery_step' AND prior.set_id=NEW.set_id
            AND prior.owner_domain=NEW.owner_domain AND prior.owner_key_json=NEW.owner_key_json
            AND json_extract(prior.payload_json,'$.version')=2
            AND json_extract(prior.payload_json,'$.kind')='allocation-capability'
            AND json_extract(prior.payload_json,'$.state')='grant' AND {unchanged}
            AND NOT EXISTS(SELECT 1 FROM file_identity_events later WHERE later.id!=prior.id
                AND json_extract(later.payload_json,'$.version')=2
                AND json_extract(later.payload_json,'$.kind')='allocation-capability'
                AND json_extract(later.payload_json,'$.epoch_id') IS {extract(p, "epoch_id")})))
    )""")
    _ = db.execute("DROP TRIGGER file_identity_events_validate")
    _execute_script_in_transaction(
        db,
        _file_identity_event_guards(" AND ".join(capability)),
    )
    _ = db.execute(f"PRAGMA user_version={_SCHEMA_VERSION_FILE_IDENTITY_PROVISIONING}")


def _ensure_rescan_file_operations_schema(db: sqlite3.Connection) -> None:
    """Keep unresolved file authority independent of series/volume lifetimes."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS rescan_file_operations (
            id INTEGER PRIMARY KEY,
            version INTEGER NOT NULL DEFAULT 1 CHECK(version=1),
            operation_token TEXT NOT NULL UNIQUE,
            series_id INTEGER NOT NULL,
            volume_id INTEGER NOT NULL,
            source_path TEXT NOT NULL,
            destination_path TEXT NOT NULL,
            expected_volume_json TEXT NOT NULL,
            expected_context_json TEXT NOT NULL,
            fingerprints_json TEXT NOT NULL,
            carriers_json TEXT NOT NULL,
            publication_receipt_json TEXT,
            state TEXT NOT NULL CHECK(state IN
                ('prepared','published','db_committed','rollback','rolled_back','completed')),
            diagnostic TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS rescan_file_operations_active_volume
        ON rescan_file_operations(volume_id)
        WHERE state IN ('prepared','published','db_committed','rollback')
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS rescan_file_operations_active_series
        ON rescan_file_operations(series_id,id)
        WHERE state IN ('prepared','published','db_committed','rollback')
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS rescan_file_operations_active_keyset
        ON rescan_file_operations(id)
        WHERE state IN ('prepared','published','db_committed','rollback')
    """)


def _ensure_acquisition_policy_columns(db: sqlite3.Connection) -> None:
    """Keep disposable history separate from durable import authority."""
    for table in ("seen", "import_queue"):
        columns = {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}
        if columns and "respect_grab_claims" not in columns:
            db.execute(
                f"ALTER TABLE {table} ADD COLUMN respect_grab_claims INTEGER"
                " CHECK(respect_grab_claims IN (0,1))"
            )


def _ensure_pack_ownership_schema(db: sqlite3.Connection) -> None:
    """Extend pack journals without guessing ownership for existing artifacts."""
    for table, column in (
        ("import_pack_cleanup_reservations", "directory_ownership_json"),
        ("import_pack_cleanup_tombstones", "carrier_json"),
    ):
        columns = {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")


def _ensure_private_file_claim_schema(db: sqlite3.Connection) -> None:
    """Add nullable versioned FILE proofs without changing legacy journal rows."""
    for table, column in (
        ("import_publication_files", "final_claim_carrier_json"),
        ("import_publication_files", "source_claim_carrier_json"),
        ("volume_file_deletions", "claim_carrier_json"),
    ):
        columns = {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
    db.execute(
        "CREATE TABLE IF NOT EXISTS file_claim_namespaces ("
        "parent_path TEXT PRIMARY KEY, ownership_json TEXT)"
    )


def _migrate_history_download_ownership(db: sqlite3.Connection) -> None:
    """Complete the schema-v5 downloader ownership and pack namespace contract."""
    starting_version = cast(int, db.execute("PRAGMA user_version").fetchone()[0])
    changed = False
    history_columns = {
        str(row[1])
        for row in db.execute("PRAGMA table_info(history)").fetchall()
    }
    if "download_client_id" not in history_columns:
        db.execute("ALTER TABLE history ADD COLUMN download_client_id INTEGER")
        changed = True

    queue_columns = {
        str(row[1])
        for row in db.execute("PRAGMA table_info(import_queue)").fetchall()
    }
    if "download_protocol" not in queue_columns:
        db.execute("ALTER TABLE import_queue ADD COLUMN download_protocol TEXT")
        changed = True
    if _enforce_import_queue_protocol_constraint(db):
        changed = True

    reservation_columns = {
        str(row[1])
        for row in db.execute(
            "PRAGMA table_info(import_pack_cleanup_reservations)"
        ).fetchall()
    }
    tombstone_columns = {
        str(row[1])
        for row in db.execute(
            "PRAGMA table_info(import_pack_cleanup_tombstones)"
        ).fetchall()
    }
    reservation_v5_columns = {
        "download_identity_key",
        "download_client_id",
        "protocol",
        "normalized_download_id",
    }
    tombstone_v5_columns = reservation_v5_columns | {"tombstone_path"}
    pack_tables_are_v5 = (
        reservation_v5_columns <= reservation_columns
        and tombstone_v5_columns <= tombstone_columns
    )
    retained_publications = 0
    if not pack_tables_are_v5:
        reservation_count = cast(
            int,
            db.execute(
                "SELECT COUNT(*) FROM import_pack_cleanup_reservations"
            ).fetchone()[0],
        )
        tombstone_count = cast(
            int,
            db.execute(
                "SELECT COUNT(*) FROM import_pack_cleanup_tombstones"
            ).fetchone()[0],
        )
        if reservation_count or tombstone_count:
            raise RuntimeError(
                "cannot migrate database schema from the unreleased v4/v5 "
                "pack journal shape while import-pack cleanup work exists: "
                f"{reservation_count} reservation(s), "
                f"{tombstone_count} tombstone(s). Finish recovery with the "
                "matching build before starting this version; downloader "
                "ownership cannot be guessed safely"
            )

        retained_publications = db.execute(
            """
            UPDATE import_publications
            SET pack_cleanup_state='retained', updated_at=CURRENT_TIMESTAMP
            WHERE pack_cleanup_state='pending'
            """
        ).rowcount
        _execute_script_in_transaction(db, """
            DROP TABLE import_pack_cleanup_tombstones;
            DROP TABLE import_pack_cleanup_reservations;
            CREATE TABLE import_pack_cleanup_reservations (
                download_identity_key  TEXT NOT NULL PRIMARY KEY,
                download_client_id     INTEGER
                                           CHECK(download_client_id IS NULL
                                                 OR download_client_id > 0),
                protocol               TEXT
                                           CHECK(protocol IN ('torrent','nzb')
                                                 OR protocol IS NULL),
                normalized_download_id TEXT NOT NULL
                                           CHECK(normalized_download_id != ''),
                download_id            TEXT NOT NULL,
                purpose                TEXT NOT NULL
                                           CHECK(purpose IN ('queueing','cleanup')),
                owner_token            TEXT NOT NULL,
                queue_id               INTEGER,
                publication_id         INTEGER,
                pack_path              TEXT NOT NULL,
                tombstone_path         TEXT,
                expires_at             TEXT NOT NULL,
                created_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE import_pack_cleanup_tombstones (
                tombstone_path         TEXT PRIMARY KEY,
                download_identity_key  TEXT NOT NULL,
                download_client_id     INTEGER
                                           CHECK(download_client_id IS NULL
                                                 OR download_client_id > 0),
                protocol               TEXT
                                           CHECK(protocol IN ('torrent','nzb')
                                                 OR protocol IS NULL),
                normalized_download_id TEXT NOT NULL,
                download_id            TEXT NOT NULL,
                queue_id               INTEGER NOT NULL,
                publication_id         INTEGER,
                pack_path              TEXT NOT NULL,
                created_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX idx_import_pack_reservations_expiry
                ON import_pack_cleanup_reservations(
                    expires_at, normalized_download_id
                );
            CREATE INDEX idx_import_pack_tombstones_publication
                ON import_pack_cleanup_tombstones(publication_id, created_at);
        """)
        changed = True

    if starting_version < _SCHEMA_VERSION_HISTORY_DOWNLOAD_OWNERSHIP:
        db.execute(
            f"PRAGMA user_version = {_SCHEMA_VERSION_HISTORY_DOWNLOAD_OWNERSHIP}"
        )
    if changed or starting_version < _SCHEMA_VERSION_HISTORY_DOWNLOAD_OWNERSHIP:
        retained_note = (
            f"; retained {retained_publications} legacy pack cleanup "
            "publication(s)"
            if retained_publications
            else ""
        )
        log_event(
            "schema_migration",
            "nullable history/queue download ownership and qualified pack "
            f"namespaces added{retained_note} "
            f"(schema version -> {_SCHEMA_VERSION_HISTORY_DOWNLOAD_OWNERSHIP})",
            db=db,
        )


def _backfill_import_notification_deliveries(db: sqlite3.Connection) -> None:
    """Convert legacy publication-level pending work to connection snapshots.

    This also covers databases initialized by an earlier schema-v4 build:
    publication rows that already have any child delivery rows are left alone,
    so newly enabled providers are never added to an accepted notification.
    """
    db.execute(
        """
        INSERT OR IGNORE INTO import_publication_notification_deliveries(
            publication_id, connection_id, connection_name, connection_type,
            state
        )
        SELECT n.publication_id, c.id, c.name, c.type, 'pending'
        FROM import_publication_notifications AS n
        JOIN notification_connections AS c
          ON c.enabled=1 AND c.on_download=1
        WHERE n.state IN ('pending','dispatching')
          AND NOT EXISTS (
              SELECT 1
              FROM import_publication_notification_deliveries AS existing
              WHERE existing.publication_id=n.publication_id
          )
        """
    )
    db.execute(
        """
        UPDATE import_publication_notifications
        SET state='pending', operation_owner=NULL, operation_expires_at=NULL,
            next_attempt_at=NULL, updated_at=CURRENT_TIMESTAMP
        WHERE state='dispatching'
          AND EXISTS (
              SELECT 1
              FROM import_publication_notification_deliveries AS delivery
              WHERE delivery.publication_id=
                    import_publication_notifications.publication_id
          )
        """
    )
    db.execute(
        """
        UPDATE import_publication_notifications
        SET state='dispatched', operation_owner=NULL,
            operation_expires_at=NULL, next_attempt_at=NULL, last_error='',
            dispatched_at=COALESCE(dispatched_at, CURRENT_TIMESTAMP),
            updated_at=CURRENT_TIMESTAMP
        WHERE state!='dispatched'
          AND NOT EXISTS (
              SELECT 1
              FROM import_publication_notification_deliveries AS delivery
              WHERE delivery.publication_id=
                    import_publication_notifications.publication_id
          )
        """
    )
    db.execute(
        """
        UPDATE import_publications
        SET notification_state='dispatched', updated_at=CURRENT_TIMESTAMP
        WHERE notification_state='pending'
          AND EXISTS (
              SELECT 1
              FROM import_publication_notifications AS notification
              WHERE notification.publication_id=import_publications.id
                AND notification.state='dispatched'
          )
        """
    )


def _migrate_import_publication_journal(db: sqlite3.Connection) -> None:
    """Create the durable import publication journal for schema version 3."""
    _execute_script_in_transaction(db, """
        CREATE TABLE IF NOT EXISTS import_publications (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            queue_id              INTEGER NOT NULL UNIQUE,
            state                 TEXT NOT NULL
                                  CHECK(state IN (
                                      'staging','prepared','publishing',
                                      'published','db_committed','cleaning',
                                      'finalized','deleted'
                                  )),
            owner_token           TEXT NOT NULL,
            operation_owner       TEXT,
            operation_expires_at  TEXT,
            series_id             INTEGER NOT NULL,
            dst_dir               TEXT NOT NULL,
            import_mode           TEXT NOT NULL
                                  CHECK(import_mode IN ('copy','move','hardlink')),
            staging_dir           TEXT NOT NULL,
            queue_snapshot_json   TEXT NOT NULL,
            series_snapshot_json  TEXT,
            series_tags_json      TEXT NOT NULL,
            queue_status          TEXT NOT NULL,
            queue_download_id     TEXT,
            queue_torrent_name    TEXT,
            queue_torrent_url     TEXT,
            queue_volume_num      REAL,
            queue_src_dir         TEXT,
            queue_failed_at       TEXT,
            queue_lease_owner     TEXT,
            queue_lease_expires_at TEXT,
            queue_created_at      TEXT,
            result_ok             INTEGER CHECK(result_ok IN (0,1)),
            result_imported_count INTEGER,
            result_queue_status   TEXT,
            diagnostic            TEXT NOT NULL DEFAULT '',
            notification_state    TEXT NOT NULL DEFAULT 'none'
                                  CHECK(notification_state IN (
                                      'none','pending','dispatched','suppressed'
                                  )),
            notification_title    TEXT,
            notification_label    TEXT,
            notification_cover_url TEXT,
            pack_cleanup_state    TEXT NOT NULL DEFAULT 'retained'
                                  CHECK(pack_cleanup_state IN (
                                      'pending','complete','retained'
                                  )),
            pack_cleanup_completed_at TEXT,
            created_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            prepared_at           TEXT,
            publishing_at         TEXT,
            published_at          TEXT,
            db_committed_at       TEXT,
            cleaning_at           TEXT,
            finalized_at          TEXT,
            deleted_at            TEXT
        );
        CREATE TABLE IF NOT EXISTS import_publication_files (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            publication_id           INTEGER NOT NULL
                                         REFERENCES import_publications(id)
                                         ON DELETE CASCADE,
            ordinal                  INTEGER NOT NULL,
            file_id                  INTEGER NOT NULL,
            src_path                 TEXT NOT NULL,
            filename                 TEXT NOT NULL,
            dst_path                 TEXT NOT NULL,
            import_kind              TEXT NOT NULL,
            file_type                TEXT NOT NULL,
            proposed_vol             REAL,
            proposed_chap            REAL,
            chap_range_end           REAL,
            vol_range_start          REAL,
            vol_range_end            REAL,
            pack_type                TEXT,
            is_special               INTEGER NOT NULL CHECK(is_special IN (0,1)),
            special_title            TEXT,
            has_volume_range         INTEGER NOT NULL
                                         CHECK(has_volume_range IN (0,1)),
            is_legacy_chapter_stub   INTEGER NOT NULL
                                         CHECK(is_legacy_chapter_stub IN (0,1)),
            is_legacy_chapter_recheck INTEGER NOT NULL
                                         CHECK(is_legacy_chapter_recheck IN (0,1)),
            plan_status              TEXT NOT NULL
                                         CHECK(plan_status IN (
                                             'ready','skip','needs_review',
                                             'pre_failed'
                                         )),
            plan_failure_reason      TEXT NOT NULL DEFAULT '',
            stage_ok                 INTEGER CHECK(stage_ok IN (0,1)),
            stage_error              TEXT NOT NULL DEFAULT '',
            stage_path               TEXT,
            final_path               TEXT,
            source_dev               INTEGER,
            source_inode             INTEGER,
            source_size              INTEGER,
            source_mtime_ns          INTEGER,
            source_sha256            TEXT,
            source_claim_path        TEXT,
            staged_dev               INTEGER,
            staged_inode             INTEGER,
            staged_size              INTEGER,
            staged_mtime_ns          INTEGER,
            staged_sha256            TEXT,
            final_expected_absent    INTEGER
                                             CHECK(final_expected_absent IN (0,1)),
            prepared_final_dev       INTEGER,
            prepared_final_inode     INTEGER,
            prepared_final_size      INTEGER,
            prepared_final_mtime_ns  INTEGER,
            prepared_final_sha256    TEXT,
            final_claim_path         TEXT,
            stage_state              TEXT NOT NULL DEFAULT 'pending'
                                         CHECK(stage_state IN (
                                             'pending','staged','skipped','failed'
                                         )),
            publish_state            TEXT NOT NULL DEFAULT 'pending'
                                         CHECK(publish_state IN (
                                             'pending','published',
                                             'not_applicable','blocked'
                                         )),
            cleanup_state            TEXT NOT NULL DEFAULT 'pending'
                                         CHECK(cleanup_state IN (
                                             'pending','deleted','missing',
                                             'replaced','not_applicable','blocked'
                                         )),
            diagnostic               TEXT NOT NULL DEFAULT '',
            staged_at                TEXT,
            published_at             TEXT,
            cleaned_at               TEXT,
            UNIQUE(publication_id, ordinal),
            UNIQUE(publication_id, file_id)
        );
        CREATE INDEX IF NOT EXISTS idx_import_publications_state
            ON import_publications(state, id);
        CREATE INDEX IF NOT EXISTS idx_import_publication_files_publication
            ON import_publication_files(publication_id, ordinal);
        CREATE TABLE IF NOT EXISTS import_pack_cleanup_reservations (
            normalized_download_id TEXT PRIMARY KEY,
            download_id            TEXT NOT NULL,
            purpose                TEXT NOT NULL
                                       CHECK(purpose IN ('queueing','cleanup')),
            owner_token            TEXT NOT NULL,
            queue_id               INTEGER,
            publication_id         INTEGER,
            pack_path              TEXT NOT NULL,
            tombstone_path         TEXT,
            expires_at             TEXT NOT NULL,
            created_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS import_pack_cleanup_tombstones (
            tombstone_path         TEXT PRIMARY KEY,
            normalized_download_id TEXT NOT NULL,
            download_id            TEXT NOT NULL,
            queue_id               INTEGER NOT NULL,
            publication_id         INTEGER,
            pack_path              TEXT NOT NULL,
            created_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_import_pack_reservations_expiry
            ON import_pack_cleanup_reservations(
                expires_at, normalized_download_id
            );
        CREATE INDEX IF NOT EXISTS idx_import_pack_tombstones_publication
            ON import_pack_cleanup_tombstones(publication_id, created_at);
    """)
    db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION_IMPORT_PUBLICATIONS}")
    log_event(
        "schema_migration",
        "durable import publication journal added "
        f"(schema version -> {_SCHEMA_VERSION_IMPORT_PUBLICATIONS})",
        db=db,
    )


def _migrate_durable_publication_safety(db: sqlite3.Connection) -> None:
    """Add fail-closed destination/source preconditions for schema version 4."""
    active_journals = db.execute(
        """
        SELECT state, COUNT(*) AS journal_count
        FROM import_publications
        WHERE state IN (
            'staging','prepared','publishing',
            'published','db_committed','cleaning'
        )
        GROUP BY state
        ORDER BY state
        """
    ).fetchall()
    if active_journals:
        state_summary = ", ".join(
            f"{row['state']} ({row['journal_count']})"
            for row in active_journals
        )
        raise RuntimeError(
            "cannot migrate database schema from version 3 to 4 while active "
            f"v3 import publication journals exist: {state_summary}. "
            "Schema version 3 was an unreleased intermediate and its active "
            "journals cannot be safely backfilled; finish or remove them with "
            "the matching v3 build before starting this version"
        )

    columns = {
        str(row[1])
        for row in db.execute(
            "PRAGMA table_info(import_publication_files)"
        ).fetchall()
    }
    additions = (
        ("source_sha256", "TEXT"),
        ("source_claim_path", "TEXT"),
        ("final_expected_absent", "INTEGER"),
        ("prepared_final_dev", "INTEGER"),
        ("prepared_final_inode", "INTEGER"),
        ("prepared_final_size", "INTEGER"),
        ("prepared_final_mtime_ns", "INTEGER"),
        ("prepared_final_sha256", "TEXT"),
        ("final_claim_path", "TEXT"),
    )
    for column, typedef in additions:
        if column not in columns:
            db.execute(
                f"ALTER TABLE import_publication_files ADD COLUMN {column} {typedef}"
            )
    publication_columns = {
        str(row[1])
        for row in db.execute(
            "PRAGMA table_info(import_publications)"
        ).fetchall()
    }
    publication_additions = (
        ("pack_cleanup_state", "TEXT NOT NULL DEFAULT 'retained'"),
        ("pack_cleanup_completed_at", "TEXT"),
        ("queue_download_client_id", "INTEGER"),
    )
    for column, typedef in publication_additions:
        if column not in publication_columns:
            db.execute(
                f"ALTER TABLE import_publications ADD COLUMN {column} {typedef}"
            )
    _execute_script_in_transaction(db, """
        CREATE TABLE IF NOT EXISTS import_publication_notifications (
            publication_id       INTEGER PRIMARY KEY,
            state                TEXT NOT NULL
                                     CHECK(state IN (
                                         'pending','dispatching','dispatched'
                                     )),
            idempotency_key      TEXT NOT NULL UNIQUE,
            title                TEXT NOT NULL,
            label                TEXT NOT NULL,
            cover_url            TEXT NOT NULL DEFAULT '',
            operation_owner      TEXT,
            operation_expires_at TEXT,
            attempt_count        INTEGER NOT NULL DEFAULT 0,
            next_attempt_at      TEXT,
            last_error           TEXT NOT NULL DEFAULT '',
            created_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            dispatched_at        TEXT
        );
        CREATE TABLE IF NOT EXISTS import_publication_notification_deliveries (
            publication_id       INTEGER NOT NULL,
            connection_id        INTEGER NOT NULL,
            connection_name      TEXT NOT NULL,
            connection_type      TEXT NOT NULL,
            state                TEXT NOT NULL
                                     CHECK(state IN (
                                         'pending','dispatching','completed'
                                     )),
            completion_reason    TEXT
                                     CHECK(
                                         completion_reason IS NULL
                                         OR completion_reason IN (
                                             'delivered',
                                             'connection_deleted',
                                             'connection_disabled'
                                         )
                                     ),
            operation_owner      TEXT,
            operation_expires_at TEXT,
            attempt_count        INTEGER NOT NULL DEFAULT 0,
            next_attempt_at      TEXT,
            last_error           TEXT NOT NULL DEFAULT '',
            created_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at         TEXT,
            PRIMARY KEY(publication_id, connection_id)
        );
        CREATE INDEX IF NOT EXISTS idx_import_publication_notification_deliveries_due
            ON import_publication_notification_deliveries(
                state, next_attempt_at, operation_expires_at,
                publication_id, connection_id
            );
        CREATE INDEX IF NOT EXISTS idx_import_publication_notifications_due
            ON import_publication_notifications(
                state, next_attempt_at, operation_expires_at, publication_id
            );
        CREATE TABLE IF NOT EXISTS import_publication_success_effects (
            publication_id       INTEGER NOT NULL,
            effect_type          TEXT NOT NULL
                                     CHECK(effect_type IN (
                                         'cover','komga_scan',
                                         'remove_completed'
                                     )),
            state                TEXT NOT NULL
                                     CHECK(state IN (
                                         'pending','dispatching','completed'
                                     )),
            idempotency_key      TEXT NOT NULL UNIQUE,
            payload_json         TEXT NOT NULL,
            operation_owner      TEXT,
            operation_expires_at TEXT,
            attempt_count        INTEGER NOT NULL DEFAULT 0,
            next_attempt_at      TEXT,
            last_error           TEXT NOT NULL DEFAULT '',
            created_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at         TEXT,
            PRIMARY KEY(publication_id, effect_type)
        );
        CREATE INDEX IF NOT EXISTS idx_import_publication_success_effects_due
            ON import_publication_success_effects(
                state, next_attempt_at, operation_expires_at,
                publication_id, effect_type
            );
        CREATE TABLE IF NOT EXISTS import_pack_cleanup_reservations (
            normalized_download_id TEXT PRIMARY KEY,
            download_id            TEXT NOT NULL,
            purpose                TEXT NOT NULL
                                       CHECK(purpose IN ('queueing','cleanup')),
            owner_token            TEXT NOT NULL,
            queue_id               INTEGER,
            publication_id         INTEGER,
            pack_path              TEXT NOT NULL,
            tombstone_path         TEXT,
            expires_at             TEXT NOT NULL,
            created_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS import_pack_cleanup_tombstones (
            tombstone_path         TEXT PRIMARY KEY,
            normalized_download_id TEXT NOT NULL,
            download_id            TEXT NOT NULL,
            queue_id               INTEGER NOT NULL,
            publication_id         INTEGER,
            pack_path              TEXT NOT NULL,
            created_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_import_pack_reservations_expiry
            ON import_pack_cleanup_reservations(
                expires_at, normalized_download_id
            );
        CREATE INDEX IF NOT EXISTS idx_import_pack_tombstones_publication
            ON import_pack_cleanup_tombstones(publication_id, created_at);
        CREATE TABLE IF NOT EXISTS volume_file_deletions (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            volume_id            INTEGER NOT NULL,
            series_id            INTEGER NOT NULL,
            state                TEXT NOT NULL
                                      CHECK(state IN ('active','completed')),
            target_path          TEXT NOT NULL,
            parent_path          TEXT NOT NULL,
            claim_path           TEXT NOT NULL,
            target_present       INTEGER NOT NULL
                                         CHECK(target_present IN (0,1)),
            target_dev           INTEGER,
            target_inode         INTEGER,
            target_size          INTEGER,
            target_mtime_ns      INTEGER,
            target_sha256        TEXT,
            series_title         TEXT NOT NULL,
            volume_num           REAL,
            source_title         TEXT NOT NULL DEFAULT '',
            original_import_path TEXT NOT NULL DEFAULT '',
            diagnostic           TEXT NOT NULL DEFAULT '',
            created_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at         TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_volume_file_deletions_active_volume
            ON volume_file_deletions(volume_id) WHERE state='active';
        CREATE INDEX IF NOT EXISTS idx_volume_file_deletions_active_series
            ON volume_file_deletions(series_id, id) WHERE state='active';
    """)
    db.execute(
        """
        INSERT OR IGNORE INTO import_publication_notifications(
            publication_id, state, idempotency_key, title, label, cover_url
        )
        SELECT id,
               CASE
                   WHEN notification_state='dispatched' THEN 'dispatched'
                   ELSE 'pending'
               END,
               'mangarr-import-publication:' || id,
               notification_title,
               notification_label,
               COALESCE(notification_cover_url, '')
        FROM import_publications
        WHERE notification_state IN ('pending','dispatched')
          AND notification_title IS NOT NULL
          AND notification_label IS NOT NULL
        """
    )
    _backfill_import_notification_deliveries(db)
    db.execute(
        f"PRAGMA user_version = {_SCHEMA_VERSION_DURABLE_PUBLICATION_SAFETY}"
    )
    log_event(
        "schema_migration",
        "durable publication destination/source preconditions added "
        f"(schema version -> {_SCHEMA_VERSION_DURABLE_PUBLICATION_SAFETY})",
        db=db,
    )


def _migrate_series_fk_constraints(db) -> None:
    """Add FK constraints on events / blocklist / seen / pending_releases."""

    tables = [
        ('events', """
            CREATE TABLE events_new (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT,
                series_id  INTEGER REFERENCES series(id) ON DELETE CASCADE,
                message    TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """),
        ('blocklist', """
            CREATE TABLE blocklist_new (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id    INTEGER REFERENCES series(id) ON DELETE CASCADE,
                torrent_url  TEXT UNIQUE,
                torrent_name TEXT,
                reason       TEXT,
                indexer      TEXT,
                protocol     TEXT,
                size_bytes   INTEGER,
                added_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """),
        ('seen', """
            CREATE TABLE seen_new (
                torrent_url   TEXT PRIMARY KEY,
                torrent_name  TEXT,
                series_id     INTEGER REFERENCES series(id) ON DELETE CASCADE,
                volume_num    REAL,
                grabbed_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                indexer       TEXT,
                protocol      TEXT,
                client        TEXT,
                download_client_id INTEGER,
                download_id   TEXT,
                release_group TEXT,
                size_bytes    INTEGER,
                release_guid  TEXT,
                release_source_scope TEXT,
                respect_grab_claims INTEGER CHECK(respect_grab_claims IN (0,1))
            )
        """),
        ('pending_releases', """
            CREATE TABLE pending_releases_new (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id  INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                url        TEXT    NOT NULL,
                title      TEXT,
                indexer    TEXT,
                protocol   TEXT,
                size_bytes INTEGER DEFAULT 0,
                release_source_scope TEXT,
                release_guid TEXT,
                release_context TEXT,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(series_id, url)
            )
        """),
    ]

    for name, ddl in tables:
        orphan_count = db.execute(
            f"SELECT COUNT(*) FROM {name}"
            f" WHERE series_id IS NOT NULL"
            f"   AND series_id NOT IN (SELECT id FROM series)"
        ).fetchone()[0]
        if orphan_count:
            log_event(
                'schema_migration',
                f'{name}: {orphan_count} orphan row(s) with stale '
                f'series_id will be dropped during FK migration',
                db=db,
            )
        _rebuild_table_copying_known_columns(
            db,
            name=name,
            ddl=ddl,
            where="series_id IS NULL OR series_id IN (SELECT id FROM series)",
        )

    _restore_volume_chapter_artifacts(db)
    db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION_FK_CONSTRAINTS}")
    log_event(
        'schema_migration',
        f'FK constraints added to events, blocklist, seen, '
        f'pending_releases (schema version -> {_SCHEMA_VERSION_FK_CONSTRAINTS})',
        db=db,
    )


def _rebuild_table_copying_known_columns(
    db,
    *,
    name: str,
    ddl: str,
    where: str | None = None,
    transforms: dict[str, str] | None = None,
) -> None:
    """Rebuild ``name`` into ``name_new`` with drift checks.

    ``transforms`` maps an existing column name to a SQL expression used
    in the SELECT list, while preserving the original INSERT column list.
    """
    old_cols = [str(r[1]) for r in db.execute(
        f"PRAGMA table_info({name})"
    ).fetchall()]

    db.execute(ddl)
    new_cols = [str(r[1]) for r in db.execute(
        f"PRAGMA table_info({name}_new)"
    ).fetchall()]

    missing = [c for c in old_cols if c not in new_cols]
    if missing:
        db.execute(f"DROP TABLE {name}_new")
        raise RuntimeError(
            f"schema migration drift: {name} has columns "
            f"{missing} that the new DDL does not include. "
            f"Update _migrate_schema_constraints() to include "
            f"these columns in the {name}_new DDL before re-running."
        )

    transforms = transforms or {}
    col_list = ', '.join(old_cols)
    select_list = ', '.join(transforms.get(c, c) for c in old_cols)
    where_sql = f" WHERE {where}" if where else ""
    db.execute(
        f"INSERT INTO {name}_new ({col_list})"
        f" SELECT {select_list} FROM {name}"
        f"{where_sql}"
    )
    db.execute(f"DROP TABLE {name}")
    db.execute(f"ALTER TABLE {name}_new RENAME TO {name}")


def _migrate_owned_status_constraints(db) -> None:
    """Add status CHECK constraints and cascade FK shape to owned items."""
    tables = [
        ('volumes', """
            CREATE TABLE volumes_new (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id       INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                volume_num      REAL,
                chapter_num     REAL,
                title           TEXT,
                status          TEXT DEFAULT 'wanted' CHECK(status IN ('wanted','grabbed','downloaded')),
                grabbed_at      TIMESTAMP,
                size_bytes      INTEGER,
                source_url      TEXT,
                torrent_name    TEXT,
                indexer         TEXT,
                protocol        TEXT,
                client          TEXT,
                download_client_id INTEGER,
                download_id     TEXT,
                vol_range_start REAL,
                vol_range_end   REAL,
                pack_type       TEXT,
                import_path     TEXT,
                release_group   TEXT,
                monitored       INTEGER DEFAULT 1,
                quality         TEXT,
                is_special      INTEGER DEFAULT 0,
                imported_at     TEXT,
                edition_type    TEXT,
                language        TEXT
            )
        """),
        ('chapters', """
            CREATE TABLE chapters_new (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id         INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                volume_id         INTEGER REFERENCES volumes(id) ON DELETE SET NULL,
                chapter_num       REAL    NOT NULL,
                title             TEXT,
                status            TEXT    DEFAULT 'wanted' CHECK(status IN ('wanted','grabbed','downloaded')),
                monitored         INTEGER DEFAULT 1,
                grabbed_at        TIMESTAMP,
                torrent_name      TEXT,
                torrent_url       TEXT,
                indexer           TEXT,
                protocol          TEXT,
                client            TEXT,
                download_client_id INTEGER,
                size_bytes        INTEGER DEFAULT 0,
                import_path       TEXT,
                download_id       TEXT,
                release_group     TEXT,
                quality           TEXT,
                imported_at       TEXT,
                chapter_range_end REAL,
                pages             INTEGER,
                metadata_source   TEXT,
                metadata_updated_at TEXT,
                UNIQUE(series_id, chapter_num)
            )
        """),
    ]

    status_expr = (
        "CASE WHEN status IN ('wanted','grabbed','downloaded')"
        " THEN status ELSE 'wanted' END"
    )
    for name, ddl in tables:
        invalid = db.execute(
            f"SELECT COUNT(*) FROM {name}"
            f" WHERE status IS NULL OR status NOT IN "
            f"('wanted','grabbed','downloaded')"
        ).fetchone()[0]
        if invalid:
            log_event(
                'schema_migration',
                f'{name}: normalized {invalid} invalid status value(s) '
                f"to 'wanted' during CHECK migration",
                db=db,
            )
        _rebuild_table_copying_known_columns(
            db,
            name=name,
            ddl=ddl,
            where="series_id IN (SELECT id FROM series)",
            transforms={'status': status_expr},
        )

    _restore_volume_chapter_artifacts(db)
    db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION_STATUS_CONSTRAINTS}")
    log_event(
        'schema_migration',
        f'CHECK constraints added to volumes/chapters status '
        f'(schema version -> {_SCHEMA_VERSION_STATUS_CONSTRAINTS})',
        db=db,
    )
