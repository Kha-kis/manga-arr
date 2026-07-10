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
                 failed_at    TIMESTAMP,
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
        add_col('import_queue',            'failed_at',                'TIMESTAMP')
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
                status        TEXT    DEFAULT 'wanted' CHECK(status IN ('wanted','grabbed','downloaded')),
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
        # Prowlarr sub-indexer attribution (PR #117). When the user uses
        # the "Sync indexers from Prowlarr" flow, each imported sub-indexer
        # becomes its own top-level torznab row. These columns track which
        # Prowlarr instance the row came from + the sub-indexer id within
        # Prowlarr, so a re-sync can dedup ((parent_prowlarr_id, prowlarr_indexer_id)
        # is the natural key) and the UI can group them visually.
        add_col('indexers', 'parent_prowlarr_id',  'INTEGER')
        add_col('indexers', 'prowlarr_indexer_id', 'INTEGER')
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
# We use PRAGMA user_version as a migration flag so migrations run exactly
# once per DB. Orphan rows (series_id pointing to a deleted series) are
# dropped during the copy and logged so operators can see what went.

_SCHEMA_VERSION_FK_CONSTRAINTS = 1
_SCHEMA_VERSION_STATUS_CONSTRAINTS = 2


_OWNED_STATUS_CHECK = "status IN ('wanted','grabbed','downloaded')"


def _restore_volume_chapter_artifacts(db) -> None:
    """Recreate indexes/triggers dropped by SQLite table rebuilds."""
    for stmt in [
        "CREATE INDEX IF NOT EXISTS idx_volumes_series        ON volumes(series_id)",
        "CREATE INDEX IF NOT EXISTS idx_volumes_series_status ON volumes(series_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_volumes_series_volnum ON volumes(series_id, volume_num)",
        "CREATE INDEX IF NOT EXISTS idx_chapters_series       ON chapters(series_id)",
        "CREATE INDEX IF NOT EXISTS idx_chapters_volid        ON chapters(volume_id)",
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


def _migrate_schema_constraints() -> None:
    """Add FK/CHECK constraints that SQLite requires table rebuilds for.

    Idempotent via PRAGMA user_version. Fresh installs already create the
    desired shape, then still flow through these rebuilds once so the
    version stamp is consistent.
    """
    with get_db() as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version >= _SCHEMA_VERSION_STATUS_CONSTRAINTS:
            return

        if version < _SCHEMA_VERSION_FK_CONSTRAINTS:
            _migrate_series_fk_constraints(db)
            version = _SCHEMA_VERSION_FK_CONSTRAINTS

        if version < _SCHEMA_VERSION_STATUS_CONSTRAINTS:
            _migrate_owned_status_constraints(db)


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

    db.execute("PRAGMA foreign_keys=OFF")
    try:
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
    finally:
        db.execute("PRAGMA foreign_keys=ON")

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
    old_cols = [r[1] for r in db.execute(
        f"PRAGMA table_info({name})"
    ).fetchall()]

    db.execute(ddl)
    new_cols = [r[1] for r in db.execute(
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
                size_bytes        INTEGER DEFAULT 0,
                import_path       TEXT,
                download_id       TEXT,
                release_group     TEXT,
                quality           TEXT,
                imported_at       TEXT,
                chapter_range_end REAL,
                UNIQUE(series_id, chapter_num)
            )
        """),
    ]

    status_expr = (
        "CASE WHEN status IN ('wanted','grabbed','downloaded')"
        " THEN status ELSE 'wanted' END"
    )
    db.execute("PRAGMA foreign_keys=OFF")
    try:
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
    finally:
        db.execute("PRAGMA foreign_keys=ON")

    _restore_volume_chapter_artifacts(db)
    db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION_STATUS_CONSTRAINTS}")
    log_event(
        'schema_migration',
        f'CHECK constraints added to volumes/chapters status '
        f'(schema version -> {_SCHEMA_VERSION_STATUS_CONSTRAINTS})',
        db=db,
    )
