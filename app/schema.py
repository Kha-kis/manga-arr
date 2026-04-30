"""Database schema initialisation and migrations.

Seventh module extracted from main.py. Contains everything that
creates, alters, or reshapes the SQLite schema at startup:

  - init_db                      — CREATE TABLE / add_col / index
                                   / trigger / seed-default pass
  - _bootstrap_root_folders      — legacy save_path → root_folders
                                   migration
  - _migrate_schema_constraints  — add FK constraints via rebuild
                                   pattern, gated by PRAGMA user_version
  - _SCHEMA_VERSION_FK_CONSTRAINTS — current migration version

Pure move — no behaviour changes. Callers in main.py still see
`init_db()`, `_bootstrap_root_folders()`, and `_migrate_schema_constraints()`
via a re-export.
"""
from __future__ import annotations

from events import log_event
from parsing import extract_volume_num
from shared import (
    get_db,
    get_cfg,
    validate_sql_identifier,
    validate_sql_typedef,
)


def init_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
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
                series_id    INTEGER NOT NULL REFERENCES series(id),
                volume_num   REAL,
                chapter_num  REAL,
                title        TEXT,
                status       TEXT DEFAULT 'wanted',
                grabbed_at   TIMESTAMP,
                size_bytes   INTEGER,
                source_url   TEXT,
                torrent_name TEXT,
                indexer      TEXT,
                protocol     TEXT,
                client       TEXT
            );
            CREATE TABLE IF NOT EXISTS seen (
                torrent_url  TEXT PRIMARY KEY,
                torrent_name TEXT,
                series_id    INTEGER REFERENCES series(id) ON DELETE CASCADE,
                volume_num   REAL,
                grabbed_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                indexer      TEXT,
                protocol     TEXT,
                client       TEXT
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
                size_bytes    INTEGER,
                release_group TEXT,
                data          TEXT,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS import_queue (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id    INTEGER REFERENCES series(id),
                download_id  TEXT,
                torrent_name TEXT,
                torrent_url  TEXT,
                volume_num   REAL,
                src_dir      TEXT,
                status       TEXT DEFAULT 'pending',
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS import_queue_files (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                queue_id         INTEGER REFERENCES import_queue(id),
                filename         TEXT,
                src_path         TEXT,
                dst_path         TEXT,
                proposed_volume  REAL,
                status           TEXT DEFAULT 'pending'
            );
            CREATE TABLE IF NOT EXISTS series_aliases (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id INTEGER NOT NULL REFERENCES series(id),
                alias     TEXT    NOT NULL,
                UNIQUE(series_id, alias)
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

        add_col('series',  'total_volumes',   'INTEGER')
        add_col('series',  'total_chapters',  'INTEGER')
        add_col('series',  'monitored',       'INTEGER DEFAULT 1')
        add_col('series',  'pub_year',        'INTEGER')
        add_col('seen',    'volume_num',      'REAL')
        add_col('seen',    'indexer',         'TEXT')
        add_col('seen',    'protocol',        'TEXT')
        add_col('seen',    'client',          'TEXT')
        add_col('volumes', 'indexer',         'TEXT')
        add_col('volumes', 'protocol',        'TEXT')
        add_col('volumes', 'client',          'TEXT')

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
        # Side-story / oneshot persistence on the final volumes row.
        # Stage 2 only stores this flag; Stage 3 adds the coverage
        # exclusion that makes it load-bearing. Non-null default so
        # existing WHERE is_special=0 filters work without a NULL check.
        add_col('volumes',            'is_special',                   'INTEGER DEFAULT 0')
        add_col('history',            'torrent_url',      'TEXT')
        add_col('volumes',            'imported_at',      'TEXT')
        add_col('volumes',            'edition_type',     'TEXT')   # standard|deluxe|omnibus|special|collector|digital
        add_col('volumes',            'language',         'TEXT')   # en|ja|fr|etc — detected from release title
        add_col('series',             'vol_count_source', 'TEXT DEFAULT "anilist"')  # anilist|mangaupdates|wikipedia|google_books|manual

        # ── chapters table ────────────────────────────────────────────────────
        db.executescript("""
            CREATE TABLE IF NOT EXISTS chapters (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id     INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                volume_id     INTEGER REFERENCES volumes(id) ON DELETE SET NULL,
                chapter_num   REAL    NOT NULL,
                title         TEXT,
                status        TEXT    DEFAULT 'wanted',
                monitored     INTEGER DEFAULT 1,
                grabbed_at    TIMESTAMP,
                torrent_name  TEXT,
                torrent_url   TEXT,
                indexer       TEXT,
                protocol      TEXT,
                client        TEXT,
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
        # Multi-chapter file support (e.g. c001-002 packs in one CBZ).
        # When set, this row covers chapter_num..chapter_range_end inclusive
        # via a single import_path. NULL preserves the original one-row-per-
        # chapter behaviour. Mirrors the volumes.vol_range_start/end pattern.
        add_col('chapters',           'chapter_range_end','REAL')

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
        db.executescript("""
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

            -- Series Tags (normalized, separate from JSON column)
            CREATE TABLE IF NOT EXISTS series_tags (
                series_id INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                tag       TEXT NOT NULL,
                PRIMARY KEY (series_id, tag)
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

        # ── Series quality_profile_id column ─────────────────────────────────
        add_col('series', 'quality_profile_id',    'INTEGER REFERENCES quality_profiles(id)')
        add_col('series', 'language_profile_id',  'INTEGER REFERENCES language_profiles(id)')
        add_col('series', 'preferred_groups',      'TEXT DEFAULT "[]"')
        add_col('series', 'blocked_groups',        'TEXT DEFAULT "[]"')
        add_col('series', 'omnibus_preference',    'TEXT DEFAULT "prefer_individual"')
        # Update strategy (Suwayomi-inspired): always | once | throttled
        add_col('series', 'update_strategy',       "TEXT DEFAULT 'always'")
        add_col('series', 'last_metadata_refresh', 'TEXT')   # ISO datetime of last AniList refresh
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

        # ── Seed default quality profile if none exists ───────────────────────
        if not db.execute("SELECT id FROM quality_profiles LIMIT 1").fetchone():
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
            "CREATE INDEX IF NOT EXISTS idx_seen_series           ON seen(series_id)",
            "CREATE INDEX IF NOT EXISTS idx_seen_dlid             ON seen(download_id)",
            "CREATE INDEX IF NOT EXISTS idx_seen_guid             ON seen(release_guid) WHERE release_guid IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_chapters_series       ON chapters(series_id)",
            "CREATE INDEX IF NOT EXISTS idx_chapters_volid        ON chapters(volume_id)",
            "CREATE INDEX IF NOT EXISTS idx_import_queue_dlid     ON import_queue(download_id)",
            "CREATE INDEX IF NOT EXISTS idx_import_queue_status   ON import_queue(status)",
            "CREATE INDEX IF NOT EXISTS idx_events_series         ON events(series_id)",
            "CREATE INDEX IF NOT EXISTS idx_history_series        ON history(series_id)",
            "CREATE INDEX IF NOT EXISTS idx_remote_path_host      ON remote_path_mappings(host)",
            "CREATE INDEX IF NOT EXISTS idx_pending_rel_series    ON pending_releases(series_id)",
            "CREATE INDEX IF NOT EXISTS idx_blocklist_series       ON blocklist(series_id)",
            "CREATE INDEX IF NOT EXISTS idx_import_queue_series    ON import_queue(series_id)",
            "CREATE INDEX IF NOT EXISTS idx_events_series_time     ON events(series_id, created_at)",
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
        db.executescript("""
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

        # ── Backfill suwayomi_sources from existing mangadex_id linkages ──────
        db.execute("""
            INSERT OR IGNORE INTO suwayomi_sources
                (series_id, source_id, source_name, source_lang, suwayomi_manga_id, source_type)
            SELECT s.id, 'mangadex', 'MangaDex', COALESCE(s.ddl_language, 'en'),
                   s.suwayomi_id, 'aggregator'
            FROM series s
            WHERE s.mangadex_id IS NOT NULL AND s.suwayomi_id IS NOT NULL
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
        # fields that are currently missing — never overwrite real data.
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

    # Schema constraint migrations (FK / CHECK). Run *after* init_db's
    # main create-and-fill pass so the existing data is stable.
    _migrate_schema_constraints()
    # Library-destination model: root folders are the single mechanism,
    # matching the Sonarr/Radarr convention. Bootstrap a root folder
    # from the legacy save_path if none exists; auto-assign any series
    # missing a root_folder_id. Runs every boot but is a no-op once
    # both invariants hold, so it's safe to call idempotently.
    _bootstrap_root_folders()


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
    with get_db() as db:
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
# We use PRAGMA user_version as a migration flag so the migration runs
# exactly once per DB. Orphan rows (series_id pointing to a deleted series)
# are dropped during the copy and logged so operators can see what went.

_SCHEMA_VERSION_FK_CONSTRAINTS = 1


def _migrate_schema_constraints() -> None:
    """Add FK constraints on events / blocklist / seen / pending_releases.

    Pre-migration: series_id was declared INTEGER with no REFERENCES
    clause, so deleting a series silently orphaned rows in these tables.
    Post-migration: the same column is INTEGER REFERENCES series(id) ON
    DELETE CASCADE, enforced by SQLite when foreign_keys=ON.

    Idempotent via PRAGMA user_version. No-op on fresh installs (their
    CREATE TABLE already uses the new shape).
    """
    with get_db() as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version >= _SCHEMA_VERSION_FK_CONSTRAINTS:
            return

        # Each entry: (table_name, new_schema_ddl, copy_columns)
        # copy_columns are the columns to copy verbatim. We rely on the
        # new schema having the same column list so INSERT..SELECT works
        # without explicit column naming, but explicit naming is safer
        # against future add_col calls that extend the old table.
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
                    download_id   TEXT,
                    release_group TEXT,
                    size_bytes    INTEGER,
                    release_guid  TEXT
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
                    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(series_id, url)
                )
            """),
        ]

        # foreign_keys must be OFF during the rename; SQLite otherwise
        # validates refs against the half-built new table. Restore after.
        db.execute("PRAGMA foreign_keys=OFF")
        try:
            for name, ddl in tables:
                # Count orphans so we can log a useful summary. An orphan
                # is a row whose series_id doesn't resolve in series.
                # NOT NULL columns (pending_releases.series_id) get
                # orphans dropped silently; nullable columns drop them
                # too — either way, the old value is bad.
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

                old_cols = [r[1] for r in db.execute(
                    f"PRAGMA table_info({name})"
                ).fetchall()]

                db.execute(ddl)
                new_cols = [r[1] for r in db.execute(
                    f"PRAGMA table_info({name}_new)"
                ).fetchall()]

                # Drift guard: the hardcoded CREATE TABLE _new DDL must
                # carry every column that the old table has. If a future
                # add_col targets one of these four tables without being
                # reflected in the DDL above, the old table will have
                # columns the new one doesn't — INSERT..SELECT below
                # would fail mid-migration and leave the DB half-migrated
                # (old dropped, new not yet renamed into place). Abort
                # cleanly instead so an operator sees a clear error.
                missing = [c for c in old_cols if c not in new_cols]
                if missing:
                    # Clean up the half-built _new table so a future run
                    # with a corrected DDL can try again.
                    db.execute(f"DROP TABLE {name}_new")
                    raise RuntimeError(
                        f"schema migration drift: {name} has columns "
                        f"{missing} that the new DDL does not include. "
                        f"Update _migrate_schema_constraints() to include "
                        f"these columns in the {name}_new DDL before re-running."
                    )

                col_list = ', '.join(old_cols)
                db.execute(
                    f"INSERT INTO {name}_new ({col_list})"
                    f" SELECT {col_list} FROM {name}"
                    f" WHERE series_id IS NULL"
                    f"    OR series_id IN (SELECT id FROM series)"
                )
                db.execute(f"DROP TABLE {name}")
                db.execute(f"ALTER TABLE {name}_new RENAME TO {name}")
        finally:
            db.execute("PRAGMA foreign_keys=ON")

        db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION_FK_CONSTRAINTS}")
        log_event(
            'schema_migration',
            f'FK constraints added to events, blocklist, seen, '
            f'pending_releases (schema version → {_SCHEMA_VERSION_FK_CONSTRAINTS})',
            db=db,
        )
