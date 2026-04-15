import asyncio
import difflib
import hmac
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import time
import xml.etree.ElementTree as ET  # for build (serialize-only); parse uses defusedxml
from defusedxml.ElementTree import parse as _safe_xml_parse, fromstring as _safe_xml_fromstring
from defusedxml.ElementTree import ParseError as _SafeXMLParseError
from defusedxml.common import DefusedXmlException as _DefusedXmlException
import zipfile
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from urllib.parse import quote_plus

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

# ── Shared helpers ───────────────────────────────────────────────────────────
from shared import is_htmx, is_boosted

# ── Sonarr-parity routers ─────────────────────────────────────────────────────
import shared as _shared
from routers import (
    quality_profiles          as _qp_router,
    quality_definitions       as _qd_router,
    release_profiles          as _rp_router,
    delay_profiles            as _dp_router,
    download_clients          as _dc_router,
    indexers                  as _idx_router,
    custom_formats            as _cf_router,
    notification_connections  as _nc_router,
    import_lists              as _il_router,
    series_editor             as _se_router,
    language_profiles         as _lp_router,
    system                    as _sys_router,
    # Phase 7 extractions
    blocklist_                as _bl_router,
    history_                  as _hist_router,
    settings_                 as _stg_router,
    queue_                    as _queue_router,
    library_                  as _lib_router,
    import_                   as _import_router,
    health_                   as _health_router,
    series_                   as _series_router,
    # DDL / Suwayomi
    mangadex_                 as _mdx_router,
    suwayomi_                 as _swy_router,
)

# ── Database path ─────────────────────────────────────────────────────────────
DB_PATH = "/config/manga_arr.db"

# ── Config management ─────────────────────────────────────────────────────────
# In-memory config, populated from DB (overriding env vars) at startup
CONFIG: dict = {}

ENV_DEFAULTS = {
    'save_path':           ('MANGA_SAVE_PATH',  '/manga'),
    'import_mode':         (None,               'hardlink'), # hardlink | move | copy
    'category':            ('MANGA_CATEGORY',   'manga'),
    'rss_interval':        ('RSS_INTERVAL',     '900'),
    'komga_url':           ('KOMGA_URL',        ''),
    'komga_user':          ('KOMGA_USER',       ''),
    'komga_pass':          ('KOMGA_PASS',       ''),
    'komga_library_id':    ('KOMGA_LIBRARY_ID', ''),
    'komga_scan_enabled':  (None,               'false'),
    # Release filtering (global fallback when no Release Profile matches)
    'ignored_words':       (None,               'raw,korean,chinese,manhwa,manhua,webtoon'),
    'preferred_words':     (None,               ''),
    'required_words':      (None,               ''),
    'preferred_groups':    (None,               ''),
    'blocked_groups':      (None,               ''),
    # Post-import client management
    'remove_completed':    (None,               'false'),
    # Scheduling
    'refresh_interval':    (None,               '86400'),  # 24h metadata refresh
    # Delay profiles global fallback
    'grab_delay_minutes':  (None,               '0'),
    # File naming format
    'file_format':         (None,               ''),       # e.g. {Series Title} v{Volume:02d}
    'chapter_format':      (None,               ''),       # e.g. {Series Title} c{Chapter:04d}
    'folder_format':       (None,               ''),       # series folder override
    # Quality cutoff (global default; per-series override in series.quality_cutoff)
    'quality_cutoff':      (None,               ''),       # pdf|epub|cbr|cbz
    # API key (auto-generated if blank)
    'api_key':             (None,               ''),
    # DDL / Suwayomi
    'ddl_language':             (None,    'en'),
    'ddl_grab_mode':            (None,    'fallback'),
    'suwayomi_check_interval':  (None,    '21600'),
    # Blocklist TTL — 0 means never auto-expire
    'blocklist_ttl_days':       (None,    '90'),
}

def load_config():
    global CONFIG
    cfg = {}
    for key, (env_var, default) in ENV_DEFAULTS.items():
        cfg[key] = os.getenv(env_var, default) if env_var else default
    try:
        with get_db() as db:
            for row in db.execute("SELECT key, value FROM settings").fetchall():
                cfg[row['key']] = row['value']  # load ALL settings keys, not just ENV_DEFAULTS
    except Exception:
        pass
    CONFIG = cfg
    # Sync to shared module so routers can call shared.get_cfg()
    _shared.CONFIG.clear()
    _shared.CONFIG.update(cfg)
    import logging as _logging
    _ll = cfg.get('log_level', 'INFO').upper()
    _logging.getLogger().setLevel(getattr(_logging, _ll, _logging.INFO))

def get_cfg(key: str, default: str = '') -> str:
    return CONFIG.get(key, default)


def ensure_api_key() -> str:
    """Guarantee a non-empty api_key exists in settings; generate + persist
    if missing or blank. Returns the (possibly newly-generated) key.

    Called from init_db (fresh-install seeding) and from lifespan startup
    (defense in depth: catches DB rows that were nulled by a bad import,
    a manual edit, or a partial migration). Also re-syncs CONFIG so the
    middleware sees the new value without a separate load_config call.
    """
    import secrets as _secrets
    import logging as _logging
    log = _logging.getLogger(__name__)
    with get_db() as db:
        row = db.execute("SELECT value FROM settings WHERE key='api_key'").fetchone()
        existing = (row['value'] if row else '') or ''
        if existing.strip():
            return existing
        new_key = _secrets.token_hex(32)
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('api_key',?)",
                   (new_key,))
    # Sync caches so the very next request sees the new key.
    CONFIG['api_key'] = new_key
    _shared.CONFIG['api_key'] = new_key
    log.warning("Generated a new API key (settings.api_key was missing/blank); "
                "view it at Settings → General")
    return new_key

# ── Database ──────────────────────────────────────────────────────────────────
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()

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
                series_id    INTEGER,
                volume_num   REAL,
                grabbed_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                indexer      TEXT,
                protocol     TEXT,
                client       TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT,
                series_id  INTEGER,
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
                series_id    INTEGER,
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
                series_id  INTEGER NOT NULL,
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
        def add_col(table, col, typedef):
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

# ── Event logging ─────────────────────────────────────────────────────────────
def log_event(event_type: str, message: str, series_id: int | None = None):
    try:
        with get_db() as db:
            db.execute(
                "INSERT INTO events(event_type, series_id, message) VALUES(?,?,?)",
                (event_type, series_id, message)
            )
    except Exception:
        pass

# ── Volume stub creation ───────────────────────────────────────────────────────
def create_volume_stubs(db, series_id: int, total_volumes: int):
    existing = {
        r['volume_num']
        for r in db.execute(
            "SELECT volume_num FROM volumes WHERE series_id=? AND volume_num IS NOT NULL",
            (series_id,)
        ).fetchall()
    }
    # Respect monitor_mode: 'existing' means only monitor volumes already in the library;
    # new stubs from a refresh should be unmonitored until the user explicitly enables them.
    s_row = db.execute("SELECT monitor_mode FROM series WHERE id=?", (series_id,)).fetchone()
    mode  = (s_row['monitor_mode'] if s_row else None) or 'all'
    # 'all' and 'missing' → monitor new stubs; 'existing' and 'none' → don't
    new_monitored = 1 if mode in ('all', 'missing') else 0
    for v in range(1, total_volumes + 1):
        if float(v) not in existing:
            db.execute(
                "INSERT INTO volumes(series_id, volume_num, status, monitored) VALUES(?,?,?,?)",
                (series_id, float(v), 'wanted', new_monitored)
            )
    # Link any unlinked chapter stubs to newly-created volume stubs
    s_row = db.execute(
        "SELECT chapter_vol_map FROM series WHERE id=?", (series_id,)
    ).fetchone()
    if s_row and s_row['chapter_vol_map']:
        try:
            ch_map = json.loads(s_row['chapter_vol_map'])
        except Exception:
            ch_map = {}
        vol_id_map = {
            r['volume_num']: r['id']
            for r in db.execute(
                "SELECT id, volume_num FROM volumes WHERE series_id=? AND volume_num IS NOT NULL",
                (series_id,)
            ).fetchall()
        }
        for ch_str, vol_num in ch_map.items():
            vol_id = vol_id_map.get(float(vol_num)) if vol_num is not None else None
            if not vol_id:
                continue
            try:
                ch_num = float(ch_str)
            except (ValueError, TypeError):
                continue
            db.execute(
                "UPDATE chapters SET volume_id=? WHERE series_id=? AND chapter_num=? AND volume_id IS NULL",
                (vol_id, series_id, ch_num)
            )

def populate_chapters(db, series_id: int) -> int:
    """
    Seed chapter stub rows from the series' chapter_vol_map JSON (MangaDex data).
    Idempotent — uses INSERT OR IGNORE so re-running is safe.
    Links each chapter to its volume stub via volume_id.
    Also updates volume_id on existing unlinked chapters.
    Returns count of newly created rows.
    """
    row = db.execute(
        "SELECT chapter_vol_map FROM series WHERE id=?", (series_id,)
    ).fetchone()
    if not row or not row['chapter_vol_map']:
        return 0
    try:
        ch_map: dict = json.loads(row['chapter_vol_map'])
    except Exception:
        return 0
    if not ch_map:
        return 0

    vol_id_map: dict[float, int] = {
        r['volume_num']: r['id']
        for r in db.execute(
            "SELECT id, volume_num FROM volumes WHERE series_id=? AND volume_num IS NOT NULL",
            (series_id,)
        ).fetchall()
    }

    created = 0
    for ch_str, vol_num in ch_map.items():
        try:
            ch_num = float(ch_str)
        except (ValueError, TypeError):
            continue
        vol_id = vol_id_map.get(float(vol_num)) if vol_num is not None else None
        cur = db.execute(
            "INSERT OR IGNORE INTO chapters(series_id, volume_id, chapter_num, status, monitored)"
            " VALUES(?,?,?,'wanted',1)",
            (series_id, vol_id, ch_num)
        )
        if cur.rowcount:
            created += 1
        elif vol_id:
            # Update volume_id on existing unlinked row
            db.execute(
                "UPDATE chapters SET volume_id=? WHERE series_id=? AND chapter_num=? AND volume_id IS NULL",
                (vol_id, series_id, ch_num)
            )
    return created


def _check_volume_completion(db, series_id: int, volume_id: int) -> bool:
    """If all monitored chapters in a volume are downloaded, mark the volume downloaded.
    Returns True if the volume was promoted to downloaded."""
    row = db.execute(
        "SELECT COUNT(*) as total,"
        " SUM(CASE WHEN status='downloaded' THEN 1 ELSE 0 END) as done"
        " FROM chapters WHERE series_id=? AND volume_id=? AND monitored=1",
        (series_id, volume_id)
    ).fetchone()
    if row and row['total'] > 0 and row['total'] == row['done']:
        db.execute(
            "UPDATE volumes SET status='downloaded',"
            " imported_at=COALESCE(imported_at, datetime('now'))"
            " WHERE id=? AND status != 'downloaded'",
            (volume_id,)
        )
        return True
    return False


def _cascade_chapters(db, series_id: int,
                       volume_ids: list[int] | None,
                       status: str,
                       **kwargs) -> int:
    """
    Cascade a status change to chapters belonging to the given volume IDs.
    volume_ids=None cascades to ALL chapters for the series.
    kwargs: optional column=value pairs (grabbed_at, torrent_name, torrent_url,
            indexer, protocol, client, download_id, release_group, size_bytes).
    Only updates monitored=1 chapters. Returns count of updated rows.
    """
    # NOTE: chapters table uses 'torrent_url' (volumes uses 'source_url').
    # Callers should pass torrent_url; source_url alias is intentionally NOT allowed.
    allowed_cols = {
        'grabbed_at', 'torrent_name', 'torrent_url', 'indexer',
        'protocol', 'client', 'download_id', 'release_group', 'size_bytes',
        'import_path', 'quality', 'imported_at',
    }
    extra_cols = [c for c in kwargs if c in allowed_cols]
    extra_vals = [kwargs[c] for c in extra_cols]
    set_parts  = ['status=?'] + [f'{c}=?' for c in extra_cols]
    set_clause = ', '.join(set_parts)
    base_vals  = [status] + extra_vals

    if volume_ids is None:
        cur = db.execute(
            f"UPDATE chapters SET {set_clause} WHERE series_id=? AND monitored=1",
            base_vals + [series_id]
        )
    else:
        if not volume_ids:
            return 0
        ph = ','.join('?' * len(volume_ids))
        cur = db.execute(
            f"UPDATE chapters SET {set_clause}"
            f" WHERE series_id=? AND volume_id IN ({ph}) AND monitored=1",
            base_vals + [series_id] + list(volume_ids)
        )
    return cur.rowcount


# ── Matching ──────────────────────────────────────────────────────────────────
def normalize(text: str) -> str:
    text = re.sub(r'\[.*?\]|\(.*?\)', ' ', text)
    text = re.sub(r'[^\w\s]', ' ', text.lower())
    text = text.replace('_', ' ')   # treat underscores as word separators
    return re.sub(r'\s+', ' ', text).strip()

# ── Language rejection ────────────────────────────────────────────────────────
_LANG_REJECT_RE = re.compile(
    r'\b(?:french|francais|fran[çc]ais|vostfr|español|espanol|spanish|'
    r'italian[eo]?|german|deutsch|portuguese|portugu[eê]s|russian|'
    r'polish|dutch|indonesian|malay|vietnamese|thai|arabic|turkish|'
    r'japanese|unlocalized)\b'
    r'|\[(?:fr|es|de|it|pt|ru|pl|nl|id|ms|vi|th|ar|tr|jp|jpn|raw)\]'
    r'|\((?:jp|jpn|raw)\)'        # (Raw), (JPN), (JP) in parens
    r'|(?<!\w)vf(?!\w)',          # VF as standalone token (French)
    re.IGNORECASE,
)

def is_foreign_language(title: str) -> bool:
    """Return True if the release title contains non-English language markers."""
    return bool(_LANG_REJECT_RE.search(title))

# ── Fuzzy title matching ──────────────────────────────────────────────────────
FUZZY_MATCH_THRESHOLD = 0.75  # minimum SequenceMatcher ratio

def _extract_series_portion(torrent_title: str) -> str:
    """
    Strip volume/chapter numbers and trailing metadata from a torrent title
    to isolate the series-name portion for fuzzy comparison.
    e.g. "[Group] One Piece v01 [Digital]" → "one piece"
    """
    t = normalize(torrent_title)
    # Remove volume/chapter markers and everything after them
    t = re.sub(
        r'\s*(?:v|vol\.?|volume|ch\.?|chapter|#)\s*\d.*$', '', t,
        flags=re.IGNORECASE
    )
    return t.strip()

def matches(pattern: str, torrent_title: str,
            threshold: float = FUZZY_MATCH_THRESHOLD,
            pub_year: int | None = None) -> bool:
    """
    Fuzzy title match using difflib SequenceMatcher ratio with word-boundary
    guard for short patterns (prevents "Vagabond" matching "Vagabonde").
    If pub_year is provided and the torrent title contains a (YYYY) year token,
    reject if the years differ by more than 1 (±1 year tolerance).
    """
    if not pattern or not torrent_title:
        return False

    norm_pattern = normalize(pattern)
    series_portion = _extract_series_portion(torrent_title)

    if not norm_pattern or not series_portion:
        return False

    ratio = difflib.SequenceMatcher(None, norm_pattern, series_portion).ratio()

    pattern_words = norm_pattern.split()
    torrent_words = set(series_portion.split())

    # Word-boundary guard: all pattern words must appear verbatim as whole words
    # in the torrent title word set. Prevents "Vagabond" matching "Vagabonde".
    if not all(w in torrent_words for w in pattern_words if len(w) > 2):
        return False

    if ratio < threshold:
        return False

    # Year tolerance: if the torrent title has an explicit (YYYY) token and the
    # series has a known pub_year, reject if they differ by more than 1 year.
    if pub_year:
        yr_m = re.search(r'\b((?:19|20)\d{2})\b', torrent_title)
        if yr_m:
            torrent_year = int(yr_m.group(1))
            if abs(torrent_year - pub_year) > 1:
                return False

    return True

# ── Volume number suffix parsing ──────────────────────────────────────────────
_LETTER_SUFFIX_MAP = {'a': 0.01, 'b': 0.02, 'c': 0.03, 'd': 0.04}
_FRAC_SUFFIX_MAP   = {'½': 0.5, '¼': 0.25, '¾': 0.75}

# Roman numerals — supports I through MMMCMXCIX (3999)
_ROMAN_VALUES = {'I': 1, 'V': 5, 'X': 10, 'L': 50,
                 'C': 100, 'D': 500, 'M': 1000}

def _roman_to_int(s: str) -> int | None:
    """Convert a Roman numeral string to int. Returns None if not valid or > 30."""
    s = s.upper().strip()
    if not s or not all(c in _ROMAN_VALUES for c in s):
        return None
    total, prev = 0, 0
    for c in reversed(s):
        val = _ROMAN_VALUES[c]
        total = total - val if val < prev else total + val
        prev = val
    # Sanity check: manga/comic volumes rarely exceed 30 in Roman numerals
    return total if 0 < total <= 30 else None

def _parse_vol_suffix(raw: str) -> float | None:
    """
    Convert raw volume token to float, handling letter/fraction suffixes.
      '1'  -> 1.0   '3a' -> 3.01   '3b' -> 3.02
      '3½' -> 3.5   '3¼' -> 3.25   '3¾' -> 3.75
    Returns None on parse failure.
    """
    if not raw:
        return None
    raw = raw.strip()
    try:
        return float(raw)
    except ValueError:
        pass
    # Trailing letter suffix  e.g. "3a"
    m = re.match(r'^(\d+(?:\.\d+)?)([a-d])$', raw, re.IGNORECASE)
    if m:
        return float(m.group(1)) + _LETTER_SUFFIX_MAP.get(m.group(2).lower(), 0)
    # Unicode fraction suffix  e.g. "3½"
    for frac, offset in _FRAC_SUFFIX_MAP.items():
        if raw.endswith(frac):
            base_part = raw[:-len(frac)]
            try:
                return (float(base_part) if base_part else 0.0) + offset
            except ValueError:
                pass
    return None

def vol_num_to_display(vol_num) -> str:
    """Format a float volume number for human display.
    None->''  3.0->3  3.01->3a  3.02->3b  3.5->3½  3.25->3¼  3.75->3¾  3.14->3.14
    """
    if vol_num is None:
        return ''
    _INT_TO_LETTER = {1: 'a', 2: 'b', 3: 'c', 4: 'd'}
    _INT_TO_FRAC   = {50: '½', 25: '¼', 75: '¾'}
    try:
        base = int(vol_num)
        frac = round((float(vol_num) - base) * 100)
    except (TypeError, ValueError):
        return str(vol_num)
    if frac == 0:
        return str(base)
    if frac in _INT_TO_LETTER:
        return f"{base}{_INT_TO_LETTER[frac]}"
    if frac in _INT_TO_FRAC:
        return f"{base}{_INT_TO_FRAC[frac]}"
    return f"{float(vol_num):g}"

def extract_volume_num(title: str) -> float | None:
    """
    Extract a single volume number from a release title.
    Uses position-based precedence: numbers that appear entirely before any
    explicit volume marker are treated as part of the series title and ignored
    (prevents "20th Century Boys vol.1" extracting 20).

    Supports:
    - Standard: vol/volume/v prefix with digits
    - Letter/fraction suffixes: vol.3a → 3.01, vol.3½ → 3.5
    - Roman numerals: Volume III → 3  (up to XXX)
    - Japanese:  1巻 → 1,  第3巻 → 3
    - Korean:    1권 → 1
    - Negative lookahead: ignores numbers followed by MB/GB/KB/p (file sizes/resolutions)
    """
    if not title:
        return None

    # ── Asian language markers (highest priority, unambiguous) ────────────────
    # Japanese: 第1巻, 1巻
    m = re.search(r'(?:第\s*)?(\d{1,3})\s*巻', title)
    if m:
        val = _parse_vol_suffix(m.group(1))
        if val is not None:
            return val
    # Korean: 1권
    m = re.search(r'(\d{1,3})\s*권', title)
    if m:
        val = _parse_vol_suffix(m.group(1))
        if val is not None:
            return val

    # ── Underscore-separated volume: Volume_0001, Vol_001 (e.g. colored scan packs) ──
    # \b doesn't work here because _ is a \w char, so use a negative char-class lookbehind
    m = re.search(r'(?<![A-Za-z])v(?:ol(?:ume)?)?_(\d{1,4})(?!\d)', title, re.IGNORECASE)
    if m:
        val = _parse_vol_suffix(m.group(1))
        if val is not None:
            return val

    # ── Roman numeral volumes: "Volume III", "Vol. X" ─────────────────────────
    m = re.search(r'\bv(?:ol(?:ume)?)?\.?\s+([IVXLCDM]{1,6})\b', title, re.IGNORECASE)
    if m:
        val = _roman_to_int(m.group(1))
        if val is not None:
            return float(val)

    # ── Standard numeric vol markers ──────────────────────────────────────────
    # Find leftmost explicit volume marker so numbers left of it are ignored
    marker_match = re.search(
        r'\b(?:vol(?:ume)?\.?|v(?=\s*\d))\s*\d|#\s*\d',
        title, re.IGNORECASE
    )
    marker_pos = marker_match.start() if marker_match else len(title)

    # Negative lookahead: (?![gGmMkK][bB]|p\b) prevents matching 720 in "720p" or 300 in "300MB"
    _num = r'(\d{1,3}(?:\.\d+)?[a-d½¼¾]?)(?!\d)(?![gGmMkK][bBiI]|[pP]\b)'
    patterns = [
        rf'\bv(?:ol(?:ume)?)?\.?\s*{_num}',
        rf'\bvolume\s+{_num}',
        rf'\b#{_num}',
    ]
    for pat in patterns:
        for m in re.finditer(pat, title, re.IGNORECASE):
            if m.start() < marker_pos and m.end() <= marker_pos:
                continue
            val = _parse_vol_suffix(m.group(1))
            if val is not None:
                return val
    return None

def extract_volume_range(title: str) -> tuple[float, float] | None:
    """Extract a volume range from a pack title. Returns (start, end) or None.
    Supports letter/fraction suffixes: v1a-v5b, v3½-v7."""
    if not title:
        return None
    _sfx = r'[a-d½¼¾]?'
    patterns = [
        # v01-v10, vol1-vol10, vol.1-vol.10, volume 1-10
        rf'\bv(?:ol(?:ume)?)?\.?\s*(\d{{1,4}}(?:\.\d+)?{_sfx})\s*[-–—~]\s*(?:v(?:ol(?:ume)?)?\.?\s*)?(\d{{1,4}}(?:\.\d+)?{_sfx})\b',
        # [001-038], [01-10]
        rf'\[(\d{{1,4}}(?:\.\d+)?{_sfx})\s*[-–—~]\s*(\d{{1,4}}(?:\.\d+)?{_sfx})\]',
        # c001-c100, ch1-ch50, chapter 1-50
        rf'\bc(?:h(?:apter)?)?\.?\s*(\d{{1,4}}(?:\.\d+)?{_sfx})\s*[-–—~]\s*(?:c(?:h(?:apter)?)?\.?\s*)?(\d{{1,4}}(?:\.\d+)?{_sfx})\b',
        # "1-38" only when preceded by space/start and followed by space/end
        r'(?:^|[\s(])\b(\d{1,3})\s*[-–—]\s*(\d{1,3})\b(?=[\s),\[]|$)',
    ]
    for pat in patterns:
        m = re.search(pat, title, re.IGNORECASE)
        if m:
            start = _parse_vol_suffix(m.group(1))
            end   = _parse_vol_suffix(m.group(2))
            if start is not None and end is not None:
                if start < end and (end - start) < 600:  # sanity: reasonable range
                    return (start, end)
    return None

def extract_chapter_num(title: str) -> float | None:
    """
    Extract a single chapter number from a release name.
    Returns None for ranges, non-chapter titles, or when no number is found.

    Handles:
    - ch/chapter/episode/ep prefix
    - Decimal chapters: ch.10.5, c100.1
    - Fraction/letter suffixes: ch.3½, ch.3a
    - Japanese: 第1話 (episode 1)
    - Negative lookahead: ignores 720/1080 in resolutions and file sizes (300MB)
    """
    if not title:
        return None
    # Reject range patterns (ch1-5, c001-100, etc.)
    if re.search(r'\b(?:ch(?:apter)?|ep(?:isode)?|c)[\s.]?\d+\s*[-–—~]\s*\d', title, re.IGNORECASE):
        return None

    # ── Japanese episode marker: 第3話 ────────────────────────────────────────
    m = re.search(r'第\s*(\d{1,4}(?:\.\d+)?)\s*話', title)
    if m:
        val = _parse_vol_suffix(m.group(1))
        if val is not None:
            return val

    # Capture group for numbers with optional decimal, fraction, or letter suffix
    # Negative lookahead: not followed by MB/GB/KB (file size) or p (resolution)
    _num = r'(\d{1,4}(?:\.\d+)?[a-d½¼¾]?)(?!\d)(?![gGmMkK][bBiI]|[pP]\b)'

    patterns = [
        # chapter / ch / chap with or without dot/space separator
        rf'\bch(?:a(?:p(?:ter)?)?)?\.?\s*{_num}',
        # episode / ep prefix (some manga scanners use this)
        rf'\bep(?:isode)?\.?\s*{_num}',
        # bare 'c' followed by at least 2 digits (avoids catching 'c' in words)
        rf'\bc(\d{{2,4}}(?:\.\d+)?[a-d½¼¾]?)(?!\d)(?![gGmMkK][bBiI]|[pP]\b)',
        # hash-number when no vol marker present (handled below)
    ]
    for pat in patterns:
        m = re.search(pat, title, re.IGNORECASE)
        if m:
            val = _parse_vol_suffix(m.group(1))
            if val is not None:
                return val

    # ── Bare-number fallback ──────────────────────────────────────────────────
    # Only fires when no explicit vol/chapter prefix is present.
    # Excludes: resolutions (720p, 1080p, 4K), file sizes (300MB, 2GB),
    #           years (2020-2024), Asian volume markers (第N巻, N권), and
    #           very large numbers unlikely to be chapters.
    has_vol   = (bool(re.search(r'\bv(?:ol(?:ume)?)?[\s.\-]?\d', title, re.IGNORECASE))
                 or bool(re.search(r'(?:第\s*)?\d+\s*[巻券]|\d+\s*권', title)))  # JP/KR volume
    has_chap  = bool(re.search(r'\bch(?:a(?:p(?:ter)?)?)?[\s.]?\d', title, re.IGNORECASE))
    if not has_vol and not has_chap:
        # Reject resolution patterns before bare-number match
        clean = re.sub(
            r'\b(?:720|1080|2160|480|360|4k|8k)\s*p\b'        # video res
            r'|\b\d+\s*(?:MB|GB|KB|MiB|GiB|KiB)\b'            # file size
            r'|\b(?:19|20)\d{2}\b',                             # years
            '', title, flags=re.IGNORECASE
        )
        m = re.search(r'(?<![.\d])(\d{1,4}(?:\.\d+)?)(?!\d)(?![gGmMkK][bBiI]|[pP]\b)', clean)
        if m:
            val = _parse_vol_suffix(m.group(1))
            if val is not None and val <= 9999:
                return val
    return None


def is_complete_pack(title: str, total_volumes: int | None = None) -> bool:
    """Returns True if the title indicates a complete/full series pack.
    Pass total_volumes to also detect range packs that span the whole series."""
    markers = [
        'complete series', 'complete collection', 'complete pack', 'full series',
        'entire series', 'all volumes', 'complete set', 'omnibus complete',
        'complete manga', 'whole series',
    ]
    t = title.lower()
    if any(m in t for m in markers):
        return True
    # Detect multi-year spans like (2012-2021) — indicates a full run archive
    m = re.search(r'\((\d{4})\s*[-–]\s*(\d{4})\)', title)
    if m:
        try:
            if int(m.group(2)) - int(m.group(1)) >= 3:
                return True
        except ValueError:
            pass
    # If total_volumes is known, check if the range covers ≥90% of the series from vol 1
    if total_volumes and total_volumes > 0:
        rng = extract_volume_range(title)
        if rng and rng[0] <= 1 and rng[1] >= total_volumes * 0.9:
            return True
    return False

def detect_pack_type(title: str, vol_range: tuple | None,
                     total_volumes: int | None = None) -> str:
    """
    Returns 'complete', 'chapter', or 'volume' for a pack release.
    Uses torrent name cues and series volume count as context.
    """
    if is_complete_pack(title):
        return 'complete'
    t = title.lower()
    # Explicit chapter markers in the name always = chapter pack
    if re.search(r'\bch(?:apter)?s?[\s.]', t) or re.search(r'\bc\d{2,}', t):
        return 'chapter'
    if not vol_range:
        # No range detected — try to classify using a single extracted number
        # Look for a bare number that exceeds the series volume count (likely a chapter number)
        single_m = re.search(r'(?:^|[\s\[({])(\d{2,4})(?:[\s\])}]|$)', title)
        if single_m and total_volumes and total_volumes > 0:
            num = float(single_m.group(1))
            if num > total_volumes * 1.5:
                return 'chapter'
        # High bare numbers (>60) with no volume prefix are almost always chapters
        if single_m:
            num = float(single_m.group(1))
            if num > 60 and not re.search(r'\bv(?:ol)?[\s.]', t):
                return 'chapter'
        return 'volume'
    start, end = vol_range
    # If numbers far exceed the series volume count, treat as chapters
    if total_volumes and total_volumes > 0:
        if start > total_volumes * 1.5 or end > total_volumes * 2:
            return 'chapter'
    # Very high numbers (>60) with no volume prefix almost always chapters
    if end > 60 and not re.search(r'\bv(?:ol)?[\s.]', t):
        return 'chapter'
    return 'volume'

def score_release(title: str, series_id: int | None = None,
                  release_group: str = '', indexer: str = '', language: str = '',
                  volume_num: float | None = None, pub_year: int | None = None) -> int:
    """
    Score a release for grab priority.
    Returns -999 if the release should be ignored entirely.
    Higher score = higher priority.

    Uses release profiles when available (Sonarr-parity); falls back to global settings.
    Optional release_group/indexer/language are passed to custom format scoring.
    """
    t = title.lower()

    # Language rejection — skip non-English unless the series has a language profile
    # (language profiles handle per-series filtering; global reject only applies without one)
    _series_lang_profile_id: int | None = None
    if series_id is not None:
        try:
            with get_db() as _ldb:
                _lp_row = _ldb.execute(
                    "SELECT language_profile_id FROM series WHERE id=?", (series_id,)
                ).fetchone()
                if _lp_row and _lp_row['language_profile_id']:
                    _series_lang_profile_id = _lp_row['language_profile_id']
                else:
                    # Fall back to default language profile from settings
                    _def_row = _ldb.execute(
                        "SELECT value FROM settings WHERE key='default_language_profile_id'"
                    ).fetchone()
                    if _def_row:
                        try:
                            _series_lang_profile_id = int(_def_row['value'])
                        except (TypeError, ValueError):
                            pass
        except Exception as e:
            print(f"[score_release] language profile lookup failed: {e}")

    if _series_lang_profile_id is None and is_foreign_language(title):
        return -999

    # ── Release profiles (Sonarr-parity) ─────────────────────────────────────
    profile_score = None
    if series_id is not None:
        try:
            from routers.release_profiles import score_from_release_profiles
            with get_db() as _rp_db:
                _rp_tags = [r['tag'] for r in _rp_db.execute(
                    "SELECT tag FROM series_tags WHERE series_id=?", (series_id,)
                ).fetchall()]
                profile_score = score_from_release_profiles(title, _rp_tags, _rp_db)
        except Exception as e:
            print(f"[score_release] release profile scoring failed: {e}")

    if profile_score is not None:
        if profile_score <= -1000:
            return -999
        score = profile_score
    else:
        # ── Fall back to global settings ──────────────────────────────────────
        # Ignored words — skip entirely
        ignored = [w.strip().lower() for w in get_cfg('ignored_words', '').split(',') if w.strip()]
        for word in ignored:
            if re.search(r'\b' + re.escape(word) + r'\b', t, re.IGNORECASE):
                return -999

        # Required words — must match at least one
        required = [w.strip().lower() for w in get_cfg('required_words', '').split(',') if w.strip()]
        if required and not any(re.search(r'\b' + re.escape(w) + r'\b', t, re.IGNORECASE) for w in required):
            return -998

        # User preferred words — add score per match
        preferred = [w.strip().lower() for w in get_cfg('preferred_words', '').split(',') if w.strip()]
        score = 0
        for word in preferred:
            if word in t:
                score += 10

    # Blocked release groups — global + per-series
    blocked_groups = [g.strip().lower() for g in get_cfg('blocked_groups', '').split(',') if g.strip()]
    if series_id is not None:
        try:
            with get_db() as _bgdb:
                _s_bg = _bgdb.execute("SELECT blocked_groups FROM series WHERE id=?", (series_id,)).fetchone()
                if _s_bg and _s_bg['blocked_groups']:
                    blocked_groups += [g.strip().lower() for g in json.loads(_s_bg['blocked_groups']) if g.strip()]
        except Exception as e:
            print(f"[score_release] blocked groups lookup failed: {e}")
    for grp in blocked_groups:
        if grp in t:
            return -999

    # ── Source type filter ────────────────────────────────────────────────────
    # 'official_only' → only licensed publishers (Viz, Kodansha, Seven Seas…)
    # 'fan_only'      → only fan scanlations (no known publisher in title)
    # 'any' (default) → no filter
    if series_id is not None:
        try:
            with get_db() as _stdb:
                _st_row = _stdb.execute(
                    "SELECT source_type FROM series WHERE id=?", (series_id,)
                ).fetchone()
                if _st_row:
                    _st = (_st_row['source_type'] or 'any')
                    if _st == 'official_only' and not is_official_release(title):
                        return -999
                    elif _st == 'fan_only' and is_official_release(title):
                        return -999
        except Exception as e:
            print(f"[score_release] source type check failed: {e}")

    # ── Required source (strict name match) ───────────────────────────────────
    # If set, only releases whose title contains this exact string are grabbed.
    # Works for both publisher names ("Viz Media") and fan groups ("1r0n").
    if series_id is not None:
        try:
            with get_db() as _rsdb:
                _rs_row = _rsdb.execute(
                    "SELECT required_scanlator FROM series WHERE id=?", (series_id,)
                ).fetchone()
                if _rs_row and _rs_row['required_scanlator']:
                    _req_sc = _rs_row['required_scanlator'].strip().lower()
                    if _req_sc and _req_sc not in t:
                        return -999
        except Exception as e:
            print(f"[score_release] required source check failed: {e}")

    # ── Language profile check ────────────────────────────────────────────────
    if _series_lang_profile_id is not None:
        try:
            from routers.language_profiles import check_language_profile
            with get_db() as _lpdb:
                _lp_allowed, _lp_reason = check_language_profile(_lpdb, _series_lang_profile_id, title)
            if not _lp_allowed:
                return -9999
        except Exception as e:
            print(f"[score_release] language profile check failed: {e}")

    # Preferred release groups — global + per-series boost
    pref_groups = [g.strip().lower() for g in get_cfg('preferred_groups', '').split(',') if g.strip()]
    if series_id is not None:
        try:
            with get_db() as _pgdb:
                _s_pg = _pgdb.execute("SELECT preferred_groups FROM series WHERE id=?", (series_id,)).fetchone()
                if _s_pg and _s_pg['preferred_groups']:
                    pref_groups += [g.strip().lower() for g in json.loads(_s_pg['preferred_groups']) if g.strip()]
        except Exception as e:
            print(f"[score_release] preferred groups lookup failed: {e}")
    for grp in pref_groups:
        if grp in t:
            score += 15

    # ── Filter out non-manga content ──────────────────────────────────────
    # Video quality/codec markers — definitely not manga
    if re.search(r'\b(1080p|720p|480p|2160p|4k uhd|bluray|blu-ray|bdrip|webrip|web-dl|'
                 r'hdtv|x264|x265|h264|h265|hevc|avc|xvid|divx|remux|'
                 r'\.mkv|\.mp4|\.avi)\b', t):
        return -999
    # Game platform/release markers
    if re.search(r'\b(ps4|ps5|xbox|nintendo switch|pc game|repack|fitgirl|skidrow|'
                 r'codex|plaza|cpy|empress)\b', t) or re.search(r'\biso\b', t):
        return -999
    # Audio releases
    if re.search(r'\b(flac|mp3|aac|320kbps|lossless|discography)\b', t):
        return -999

    # ── Built-in quality preferences for manga ────────────────────────────
    if is_official_release(title):
        score += 15
    elif is_quality_fan_release(title):
        score += 10
    # ── Omnibus / multi-volume pack preference ────────────────────────────────
    _is_omnibus = (extract_volume_range(title) is not None or is_complete_pack(title) or
                   any(w in t for w in ('omnibus', '3-in-1', '2-in-1', 'box set',
                                        'collected edition', 'deluxe edition')))
    _is_complete = is_complete_pack(title)
    _omnibus_pref = 'prefer_individual'
    if series_id is not None:
        try:
            with get_db() as _opdb:
                _op_row = _opdb.execute(
                    "SELECT omnibus_preference FROM series WHERE id=?", (series_id,)
                ).fetchone()
                if _op_row and _op_row['omnibus_preference']:
                    _omnibus_pref = _op_row['omnibus_preference']
        except Exception as e:
            print(f"[score_release] omnibus preference lookup failed: {e}")

    if _omnibus_pref == 'only_individual':
        # Reject any omnibus/multi-volume release
        if _is_omnibus:
            return -999
    elif _omnibus_pref == 'only_omnibus':
        # Reject single-volume releases (prefer packs only)
        if not _is_omnibus:
            return -999
        score += 25  # strongly prefer
        if _is_complete:
            score += 15
    elif _omnibus_pref == 'prefer_omnibus':
        if _is_omnibus:
            score += 20
            if _is_complete:
                score += 10
        else:
            score -= 10  # penalise singles
    else:  # prefer_individual (default)
        if _is_omnibus:
            score += 8
        if _is_complete:
            score += 12

    # ── Volume number match bonus (Kapowarr-inspired) ─────────────────────────
    # If we know which volume we're searching for, reward releases that match exactly.
    # This helps when multiple volumes appear in the same RSS feed entry.
    if volume_num is not None:
        _rel_vol = extract_volume_num(title)
        if _rel_vol is not None:
            if abs(_rel_vol - volume_num) < 0.01:
                score += 3   # exact volume match
            elif abs(_rel_vol - volume_num) <= 1.0:
                score += 1   # adjacent volume (off-by-one tolerance)
        _rel_rng = extract_volume_range(title)
        if _rel_rng is not None:
            rng_width = _rel_rng[1] - _rel_rng[0] + 1
            if _rel_rng[0] <= volume_num <= _rel_rng[1]:
                # Range covers the desired volume; smaller range = better match
                score += max(0, 3 - int(rng_width / 5))

    # ── Year match bonus ──────────────────────────────────────────────────────
    # Releases that include the publication year matching the series are more
    # likely to be correct scans of that edition.
    if pub_year and pub_year > 1900:
        _year_m = re.search(r'\b(20\d{2}|19\d{2})\b', title)
        if _year_m:
            _rel_year = int(_year_m.group(1))
            if _rel_year == pub_year:
                score += 1   # exact year match
            elif abs(_rel_year - pub_year) <= 1:
                pass         # close year — neutral (neither bonus nor penalty)

    # ── Custom Format scoring ─────────────────────────────────────────────────
    try:
        from routers.custom_formats import score_custom_formats
        with get_db() as _cfdb:
            cf_score = score_custom_formats(_cfdb, series_id, title,
                                            release_group=release_group,
                                            indexer=indexer, language=language)
        score += cf_score
    except Exception as e:
        print(f"[score_release] custom format scoring failed: {e}")

    return score


def evaluate_release(item: dict, series_id: int, db) -> dict:
    """
    Run all scoring and filtering checks on a single release item and return a
    structured evaluation result suitable for display in the interactive search UI.

    Returns:
        {
            "score": int,
            "status": "would_grab" | "low_score" | "rejected",
            "rejections": ["..."],
            "custom_format_matches": [{"name": "...", "score": N}],
            "quality": "cbz" | "epub" | ...,
            "size_mb": float,
        }
    """
    title      = item.get('title', '')
    size_bytes = item.get('size_bytes') or item.get('size') or 0
    size_mb    = round(size_bytes / (1024 * 1024), 1) if size_bytes else 0.0
    quality    = detect_quality_from_title(title)
    rejections: list[str] = []

    # ── Language rejection ────────────────────────────────────────────────────
    if is_foreign_language(title):
        rejections.append("Release appears to be a foreign-language scan")
        return {
            "score": -999,
            "status": "rejected",
            "rejections": rejections,
            "custom_format_matches": [],
            "quality": quality,
            "size_mb": size_mb,
        }

    # ── Blocked release groups (global setting) ───────────────────────────────
    t_lower = title.lower()
    blocked_groups = [g.strip().lower() for g in get_cfg('blocked_groups', '').split(',') if g.strip()]
    for grp in blocked_groups:
        if grp in t_lower:
            rejections.append(f"Release group '{grp}' is blocked")

    # ── Release profiles ──────────────────────────────────────────────────────
    try:
        series_tags = [r['tag'] for r in db.execute(
            "SELECT tag FROM series_tags WHERE series_id=?", (series_id,)
        ).fetchall()]
        from routers.release_profiles import get_applicable_profiles
        profiles = get_applicable_profiles(db, series_tags)
        for p in profiles:
            if p['required'] and not any(_term_match(t, t_lower) for t in p['required']):
                required_terms = [_term_display(t) for t in p['required']]
                rejections.append(f"Required terms not found: {', '.join(required_terms)}")
            for ig in p['ignored']:
                if _term_match(ig, t_lower):
                    rejections.append(f"Ignored word found: '{_term_display(ig)}'")
        # Global required/ignored words (only when no profiles apply)
        if not profiles:
            ignored_words = [w.strip().lower() for w in get_cfg('ignored_words', '').split(',') if w.strip()]
            for word in ignored_words:
                if word in t_lower:
                    rejections.append(f"Ignored word found: '{word}'")
            required_words = [w.strip().lower() for w in get_cfg('required_words', '').split(',') if w.strip()]
            if required_words and not any(w in t_lower for w in required_words):
                rejections.append(f"Required words not found: {', '.join(required_words)}")
    except Exception:
        pass

    # ── Quality size bounds ───────────────────────────────────────────────────
    try:
        qdef_row = db.execute(
            "SELECT * FROM quality_definitions WHERE quality=?", (quality,)
        ).fetchone()
        if qdef_row and size_mb:
            min_size = qdef_row['min_size'] or 0
            max_size = qdef_row['max_size'] or 0
            if min_size > 0 and size_mb < min_size:
                rejections.append(
                    f"Size {size_mb:.1f} MB is below {quality.upper()} minimum ({min_size} MB)"
                )
            if max_size > 0 and size_mb > max_size:
                rejections.append(
                    f"Size {size_mb:.1f} MB exceeds {quality.upper()} maximum ({max_size} MB)"
                )
    except Exception:
        pass

    # ── Custom format matches ─────────────────────────────────────────────────
    cf_matches: list[dict] = []
    try:
        from routers.custom_formats import evaluate_custom_format
        from shared import from_json as _cfj
        profile_row = db.execute(
            "SELECT qp.id, qp.minimum_custom_format_score FROM quality_profiles qp"
            " JOIN series s ON s.quality_profile_id=qp.id WHERE s.id=?",
            (series_id,)
        ).fetchone()
        if not profile_row:
            profile_row = db.execute(
                "SELECT id, minimum_custom_format_score FROM quality_profiles WHERE is_default=1 LIMIT 1"
            ).fetchone()
        profile_id   = profile_row['id'] if profile_row else None
        min_cf_score = int(profile_row['minimum_custom_format_score'] or 0) if profile_row else 0

        if profile_id:
            format_rows = db.execute(
                "SELECT cf.name, cf.specifications, qpcf.score"
                " FROM quality_profile_custom_formats qpcf"
                " JOIN custom_formats cf ON cf.id=qpcf.format_id"
                " WHERE qpcf.profile_id=?",
                (profile_id,)
            ).fetchall()
            total_cf = 0
            for row in format_rows:
                specs = _cfj(row['specifications'], [])
                if evaluate_custom_format(specs, title, size_bytes, 0):
                    cf_matches.append({"name": row['name'], "score": row['score']})
                    total_cf += row['score']
            if min_cf_score > 0 and total_cf < min_cf_score:
                rejections.append(
                    f"Custom format score {total_cf} is below profile minimum ({min_cf_score})"
                )
    except Exception:
        pass

    # ── Compute final score via score_release ─────────────────────────────────
    sc = score_release(title, series_id)

    if rejections:
        status = "rejected"
    elif sc < 0:
        status = "low_score"
    else:
        status = "would_grab"

    return {
        "score": sc,
        "status": status,
        "rejections": rejections,
        "custom_format_matches": cf_matches,
        "quality": quality,
        "size_mb": size_mb,
    }


def _term_display(term) -> str:
    """Return human-readable display for a profile term (string or dict)."""
    if isinstance(term, dict):
        return term.get('term', '')
    return str(term)


def _term_match(term, title_lower: str) -> bool:
    """Match a profile term (string or dict with is_regex) against a lowercased title."""
    if isinstance(term, dict):
        t = (term.get('term') or '').lower()
        if term.get('is_regex'):
            try:
                return bool(re.search(t, title_lower, re.IGNORECASE))
            except re.error:
                pass  # fall through to substring
        return t in title_lower
    return str(term).lower() in title_lower


def parse_size_bytes(size_str: str) -> int:
    if not size_str:
        return 0
    m = re.match(r'([\d.]+)\s*(K|M|G|T)?i?B', size_str, re.IGNORECASE)
    if not m:
        return 0
    val  = float(m.group(1))
    unit = (m.group(2) or '').upper()
    return int(val * {'K': 1024, 'M': 1024**2, 'G': 1024**3, 'T': 1024**4}.get(unit, 1))

# ── Download clients ──────────────────────────────────────────────────────────
def extract_magnet_hash(magnet: str) -> str | None:
    m = re.search(r'xt=urn:btih:([0-9a-fA-F]{40}|[0-9a-zA-Z]{32})', magnet, re.IGNORECASE)
    return m.group(1).lower() if m else None


async def qbit_grab(torrent_url: str, client: dict | None = None,
                    save_path: str | None = None,
                    torrent_name: str | None = None) -> tuple[bool, str | None, bool]:
    """Add to qBittorrent. Returns (success, torrent_hash_or_None, client_healthy).

    ``client_healthy`` is True when auth + add succeeded, even if the hash
    couldn't be matched afterwards. Used by the circuit breaker so we don't
    trip it on routine matching failures (qBit was reachable the whole time).
    """
    _cfg    = client or {}
    host    = (_cfg.get('host') or '').rstrip('/')
    user    = _cfg.get('username') or ''
    pw      = _cfg.get('password') or ''
    cat     = _cfg.get('category') or get_cfg('category')
    _state  = _cfg.get('initial_state') or 'normal'
    _seq    = bool(_cfg.get('sequential_order'))
    _flf    = bool(_cfg.get('first_last_first'))
    _layout = _cfg.get('content_layout') or 'original'
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{host}/api/v2/auth/login",
                data={'username': user, 'password': pw}
            )
            if 'Ok' not in r.text:
                # Auth fail = real client-health problem → trip CB
                return False, None, False

            # For non-magnet URLs, pre-fetch the .torrent file from within the container
            # (where Prowlarr/indexer URLs are reachable) and upload the raw bytes to qBit.
            # This avoids qBit trying to fetch Docker-internal hostnames from its VPN namespace.
            add_files = None
            add_data  = {'category': cat}
            if save_path:
                add_data['savepath'] = save_path
            # Apply qBit-specific torrent options from client settings
            if _state == 'paused':
                add_data['paused'] = 'true'
            if _seq:
                add_data['sequentialDownload'] = 'true'
            if _flf:
                add_data['firstLastPiecePrio'] = 'true'
            _layout_map = {'subfolder': 'Subfolder', 'none': 'NoSubfolder'}
            if _layout in _layout_map:
                add_data['contentLayout'] = _layout_map[_layout]

            if torrent_url.startswith('magnet:'):
                add_data['urls'] = torrent_url
            else:
                try:
                    tf = await client.get(torrent_url, follow_redirects=True, timeout=15)
                    if tf.status_code == 200 and tf.content:
                        add_files = {'torrents': ('upload.torrent', tf.content, 'application/x-bittorrent')}
                    else:
                        add_data['urls'] = torrent_url  # fallback
                except Exception:
                    add_data['urls'] = torrent_url  # fallback

            if add_files:
                r2 = await client.post(f"{host}/api/v2/torrents/add", data=add_data, files=add_files)
            else:
                r2 = await client.post(f"{host}/api/v2/torrents/add", data=add_data)

            if r2.status_code != 200:
                # HTTP error from qBit add → client-health problem → trip CB
                return False, None, False
            add_failed = r2.text.strip() == 'Fails.'

            # Extract hash from magnet link directly
            dl_id = extract_magnet_hash(torrent_url) if torrent_url.startswith('magnet:') else None

            # For non-magnet URLs, query qBit's torrents and match by name.
            # Also used when add returned "Fails." to detect duplicates already in qBit.
            if not dl_id:
                norm_name = normalize(torrent_name) if torrent_name else ''
                add_time  = time.time()

                # Two-pass lookup: first quick pass (category-filtered, recent),
                # then a broader pass with no category filter if needed.
                for attempt, (sleep_s, use_cat, limit) in enumerate([
                    (1.5, True,  10),   # pass 1: fast, category-scoped
                    (2.0, False, 30),   # pass 2: slower, all categories
                ]):
                    await asyncio.sleep(sleep_s)
                    params: dict = {'filter': 'all'}
                    if use_cat or add_failed:
                        params['category'] = cat
                    if not add_failed:
                        params.update({'sort': 'added_on', 'reverse': 'true', 'limit': limit})
                    r3 = await client.get(f"{host}/api/v2/torrents/info", params=params)
                    if r3.status_code == 200:
                        for t in r3.json():
                            t_norm = normalize(t.get('name', ''))
                            if norm_name and (norm_name == t_norm or
                                              norm_name in t_norm or t_norm in norm_name):
                                dl_id = t.get('hash', '').lower() or None
                                break
                        # Fallback: pick the most recently added torrent if just added
                        if not dl_id and not norm_name and not add_failed and r3.json():
                            newest = r3.json()[0]
                            if time.time() - newest.get('added_on', 0) < add_time + sleep_s + 1:
                                dl_id = newest.get('hash', '').lower() or None
                    if dl_id:
                        break

            # If add returned "Fails." and we can't find the torrent it's a real failure.
            # If we added successfully but can't confirm the hash, also treat as failure
            # to avoid recording a false 'grabbed' event with no tracking — but the
            # client is still HEALTHY (we got all the way to the add + query), so
            # client_healthy=True to prevent tripping the circuit breaker.
            if not dl_id:
                print(f"[qBit] grab added but hash not found for: {torrent_name!r}")
                return False, None, True

            # Force-start cannot be set during add — apply it now that we have the hash
            if _state == 'forced' and dl_id:
                try:
                    await client.post(f"{host}/api/v2/torrents/setForceStart",
                                      data={'hashes': dl_id, 'value': 'true'})
                except Exception:
                    pass

            return True, dl_id, True
    except Exception as e:
        # Connection/timeout error → real client-health problem → trip CB
        print(f"[qBit] grab error: {e}")
        return False, None, False

async def qbit_remove(download_id: str, delete_files: bool = False) -> bool:
    """Remove a torrent from qBittorrent by hash. Returns True on success."""
    if not download_id:
        return False
    from routers.download_clients import get_client_for_protocol
    with get_db() as _rdb:
        _c = get_client_for_protocol(_rdb, 'torrent')
    if not _c:
        return False
    host = (_c.get('host') or '').rstrip('/')
    user = _c.get('username') or ''
    pw   = _c.get('password') or ''
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{host}/api/v2/auth/login", data={'username': user, 'password': pw})
            if 'Ok' not in r.text:
                return False
            r2 = await client.post(
                f"{host}/api/v2/torrents/delete",
                data={'hashes': download_id, 'deleteFiles': 'true' if delete_files else 'false'}
            )
            return r2.status_code == 200
    except Exception as e:
        print(f"[qBit] remove error: {e}")
        return False


async def sab_remove(nzo_id: str) -> bool:
    """Remove a completed job from SABnzbd. Returns True on success."""
    if not nzo_id:
        return False
    from routers.download_clients import get_client_for_protocol
    with get_db() as _rdb:
        _c = get_client_for_protocol(_rdb, 'nzb')
    if not _c:
        return False
    host   = (_c.get('host') or '').rstrip('/')
    apikey = _c.get('password') or ''
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{host}/api", params={
                'mode': 'history', 'action': 'delete', 'del_files': '0',
                'value': nzo_id, 'apikey': apikey, 'output': 'json'
            })
            return r.status_code == 200
    except Exception as e:
        print(f"[SAB] remove error: {e}")
        return False


async def sab_grab(nzb_url: str, client: dict | None = None,
                   save_path: str | None = None) -> tuple[bool, str | None, bool]:
    """Add to SABnzbd. Returns (success, nzo_id_or_None, client_healthy)."""
    host   = ((client or {}).get('host') or '').rstrip('/')
    apikey = (client or {}).get('password') or ''
    cat    = (client or {}).get('category') or get_cfg('category')
    if not apikey:
        return False, None, False
    try:
        async with httpx.AsyncClient(timeout=20) as cli:
            r = await cli.post(
                f"{host}/api",
                params={'mode': 'addurl', 'name': nzb_url, 'cat': cat,
                        'apikey': apikey, 'output': 'json'}
            )
            data = r.json()
            if data.get('status') is True:
                nzo_ids = data.get('nzo_ids', [])
                # If SAB accepted but didn't return an id, still healthy
                return (True, nzo_ids[0], True) if nzo_ids else (False, None, True)
            # Business-level fail (SAB running but rejected the add) — still healthy
            return False, None, True
    except Exception as e:
        # Connection error — real health issue
        print(f"[SAB] grab error: {e}")
        return False, None, False

def sanitize_filename(name: str) -> str:
    """Convert a series title to a safe directory name."""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    safe = safe.strip('. ')
    return safe or 'Unknown'


def safe_join_under(dst_dir: str, filename: str) -> str:
    """Join filename under dst_dir, rejecting unsafe input.

    Raises ValueError if filename:
      - is empty,
      - contains a path separator (/ or \\),
      - is absolute,
      - has any '..' path component,
      - sanitizes to the placeholder 'Unknown' (i.e. nothing usable left).

    Defense-in-depth: also verifies the resolved candidate lives under
    realpath(dst_dir), catching symlink escapes inside dst_dir.
    """
    if not filename:
        raise ValueError("empty filename")
    if '/' in filename or '\\' in filename:
        raise ValueError(f"path separator in filename: {filename!r}")
    if os.path.isabs(filename):
        raise ValueError(f"absolute path rejected: {filename!r}")
    # Reject '..' as a whole component (covers '..', '../x', 'x/..', etc.).
    # Path-separator check above already rules out the embedded forms, but
    # this also rejects a bare '..' filename.
    parts = filename.replace('\\', '/').split('/')
    if any(p == '..' for p in parts):
        raise ValueError(f"path traversal component in filename: {filename!r}")

    safe_name = sanitize_filename(filename)
    if safe_name == 'Unknown':
        # sanitize_filename returns 'Unknown' when the input has nothing usable
        # (empty, all dots/spaces, or only forbidden chars). Refuse rather than
        # silently coining a generic name.
        raise ValueError(f"unusable filename after sanitize: {filename!r}")

    candidate = os.path.join(dst_dir, safe_name)
    base_real = os.path.realpath(dst_dir)
    cand_real = os.path.realpath(candidate)
    if cand_real != base_real and not cand_real.startswith(base_real + os.sep):
        raise ValueError(f"resolved path escapes dst_dir: {filename!r}")
    return candidate


def parse_release_group(title: str) -> str:
    """Extract release group from a manga release title. Returns empty string if not found.

    Tries three strategies in order:
    1. First bracketed token that looks like a group name (allows spaces, e.g. [Viz Media])
    2. Last bracketed token at end of title (many releases put group last)
    3. Any bracketed token with 2-30 chars

    Skips tokens that are clearly metadata: file extensions, quality tags,
    hash strings (8+ hex chars), resolution markers.
    """
    _skip = re.compile(
        r'^(?:CBZ|CBR|EPUB|PDF|ZIP|MOBI|DIGITAL|SCAN|HQ|LQ|WEB|RAW|'
        r'\d{3,4}P|[0-9A-F]{8,}|V\d{1,2}|FIXED|REPACK|PROPER)$',
        re.IGNORECASE
    )
    brackets = re.findall(r'[\[\(]([^\[\]()]{2,30})[\]\)]', title)
    candidates = [b.strip() for b in brackets if not _skip.match(b.strip())]
    if candidates:
        return candidates[0]   # first non-metadata bracket = most likely group
    return ''


def parse_revision(title: str) -> dict:
    """
    Detect REPACK / PROPER / version fix markers in a manga release title.
    Returns:
        {
            'is_repack':  bool,   # title signals a revision/fix of an existing release
            'is_proper':  bool,   # title contains PROPER specifically
            'version':    int,    # version number (1 = original, 2+ = revision)
        }

    Manga-specific rules (differs from Sonarr's video approach):

    1. REPACK / PROPER keywords: unambiguous — rare in manga but straightforward.

    2. Bracketed version tag [v2] / (v2): the standard manga scene convention
       for "this is version 2 of this release". The brackets make it unambiguous
       regardless of what volume number appears elsewhere in the title.

    3. Bare v2 / v3 (no brackets): ONLY treated as a version marker when a
       separate volume indicator already exists in the title (vol., volume, #,
       Japanese/Korean kanji). Without that context, a bare 'v02' is just
       "volume 2" and must NOT be flagged as a repack.

    4. FIXED keyword: common in manga (`(Fixed)`) — treated as a repack.
    """
    t = title.upper()

    is_proper = bool(re.search(r'\bPROPER\b', t))
    is_repack = bool(re.search(r'\bREPACK\b', t)) or is_proper
    is_repack = is_repack or bool(re.search(r'\bFIXED\b', t))
    version   = 1

    # ── Bracketed version tag: [v2], (v2), [v3] etc. ─────────────────────────
    # This is the definitive manga scene convention; unambiguous regardless of
    # how the volume number is expressed elsewhere in the title.
    bm = re.search(r'[\[\(]V(\d{1,2})[\]\)]', t)
    if bm:
        v = int(bm.group(1))
        if v > 1:
            version   = v
            is_repack = True

    # ── Bare v2/v3 — only safe when another volume indicator exists ───────────
    # Prevents "Series Name v02.cbz" being flagged as a repack of volume 1.
    # A bare v-token is only a version marker when there's ALREADY a separate
    # vol./volume/#/kanji indicator, OR when multiple v-tokens appear and the
    # second clearly differs from the first (e.g., "v01 v2").
    if not is_repack:
        # Does the title have any NON-v-prefixed volume indicator?
        has_other_vol = bool(re.search(
            r'\bVOL(?:UME)?\.?\s*\d|\b#\s*\d|\d\s*巻|\d\s*권', t
        ))
        v_tokens = list(re.finditer(r'\bV(\d{1,2})\b(?!\d)', t))

        if has_other_vol and v_tokens:
            # The v-token can't be the volume number — it must be a version tag
            for tok in v_tokens:
                v = int(tok.group(1))
                if v > 1:
                    version   = v
                    is_repack = True
                    break
        elif len(v_tokens) >= 2:
            # Two separate v-tokens: first = volume, subsequent = version
            # e.g. "Manga v01 v2" → volume 1, version 2
            for tok in v_tokens[1:]:
                v = int(tok.group(1))
                if v > 1:
                    version   = v
                    is_repack = True
                    break

    return {'is_repack': is_repack, 'is_proper': is_proper, 'version': version}


def detect_quality_from_title(title: str) -> str:
    """Return the quality key for a release based on its file extension in the title.
    Checks for .cbz, .cbr, .epub, .pdf, .zip (case-insensitive).
    Returns 'unknown' when no recognisable extension is found."""
    t = title.lower()
    for ext, quality in (
        ('.cbz',  'cbz'),
        ('.cbr',  'cbr'),
        ('.epub', 'epub'),
        ('.pdf',  'pdf'),
        ('.zip',  'zip'),
    ):
        if ext in t:
            return quality
    return 'unknown'


def build_volume_label(vol_num, vol_range, pack_type) -> str:
    """Build a human-readable label like 'Vol 5', 'Vol 1–5', 'Complete Series', 'Pack'."""
    if vol_num is not None:
        return f"Vol {vol_num_to_display(vol_num)}"
    if pack_type == 'complete':
        return "Complete Series"
    if pack_type == 'chapter':
        return "Chapter"
    if vol_range:
        return f"Vol {vol_num_to_display(vol_range[0])}–{vol_num_to_display(vol_range[1])}"
    return "Pack"


def add_history(db, event_type: str, series_id: int | None, series_title: str,
                volume_label: str, source_title: str = '',
                indexer: str = '', protocol: str = '', client: str = '',
                download_id: str = '', size_bytes: int = 0,
                release_group: str = '', data: dict | None = None,
                torrent_url: str = ''):
    """Insert a history record."""
    db.execute(
        "INSERT INTO history(event_type, series_id, series_title, volume_label,"
        " source_title, indexer, protocol, client, download_id, size_bytes, release_group, data, torrent_url)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_type, series_id, series_title, volume_label, source_title,
         indexer, protocol, client, download_id, size_bytes or 0, release_group,
         json.dumps(data) if data else None, torrent_url or None)
    )


def _apply_format_tokens(fmt: str, series_title: str,
                          volume_num: float | None = None,
                          chapter_num: float | None = None,
                          pub_year: int | None = None) -> str:
    """Apply all supported template tokens to a format string."""
    safe_title  = sanitize_filename(series_title)
    dot_title   = safe_title.replace(' ', '.')
    year_str    = str(pub_year) if pub_year else ''

    name = fmt
    name = name.replace('{Series Title}', safe_title)
    name = name.replace('{Series.Title}', dot_title)
    name = name.replace('{Year}',         year_str)

    if volume_num is not None:
        name = re.sub(r'\{Volume:(\d+)d\}',
                      lambda m: vol_num_to_display(volume_num).zfill(int(m.group(1))), name)
        name = name.replace('{Volume}', vol_num_to_display(volume_num))

    if chapter_num is not None:
        ch_int = int(chapter_num) if chapter_num == int(chapter_num) else chapter_num
        name = re.sub(r'\{Chapter:(\d+)d\}',
                      lambda m: str(ch_int).zfill(int(m.group(1))), name)
        name = name.replace('{Chapter}', str(ch_int))

    return name.strip()


def build_filename(series_title: str, volume_num: float | None,
                   original_filename: str,
                   pub_year: int | None = None,
                   chapter_num: float | None = None) -> str:
    """
    Apply the configured file_format (or chapter_format for chapter files) template.
    Falls back to original_filename when no template is set.

    Supported tokens:
      {Series Title}    — sanitized series title with spaces
      {Series.Title}    — sanitized series title with dots
      {Year}            — publication year (e.g. 1998)
      {Volume}          — volume number via vol_num_to_display (e.g. "1", "1½")
      {Volume:02d}      — zero-padded volume number (e.g. "01")
      {Chapter}         — chapter number (e.g. "1")
      {Chapter:04d}     — zero-padded chapter number (e.g. "0001")
    """
    ext = os.path.splitext(original_filename)[1]

    # Chapter files use chapter_format if set, else file_format, else keep original
    if chapter_num is not None:
        fmt = get_cfg('chapter_format', '').strip() or get_cfg('file_format', '').strip()
    else:
        fmt = get_cfg('file_format', '').strip()

    if not fmt:
        # Untrusted: original_filename can come from a torrent/NZB and may
        # contain path separators or '..'. Strip to a safe basename.
        return sanitize_filename(os.path.basename(original_filename))

    try:
        name = _apply_format_tokens(fmt, series_title, volume_num, chapter_num, pub_year)
        return name + ext
    except Exception:
        return sanitize_filename(os.path.basename(original_filename))


def read_comic_info(cbz_path: str) -> dict:
    """
    Open a .cbz/.zip file and parse ComicInfo.xml if present.
    Returns dict with keys: series (str|None), number (float|None), volume (float|None).
    Returns all-None dict on any error or if ComicInfo.xml is absent.
    """
    result: dict = {'series': None, 'number': None, 'volume': None}
    try:
        with zipfile.ZipFile(cbz_path, 'r') as zf:
            ci_name = next(
                (n for n in zf.namelist() if n.lower().endswith('comicinfo.xml')),
                None
            )
            if not ci_name:
                return result
            with zf.open(ci_name) as f:
                root = _safe_xml_parse(f).getroot()

        def _text(tag: str) -> str | None:
            el = root.find(tag)
            return el.text.strip() if el is not None and el.text else None

        result['series'] = _text('Series')
        for field, key in (('Volume', 'volume'), ('Number', 'number')):
            raw = _text(field)
            if raw:
                val = _parse_vol_suffix(raw)
                if val is not None:
                    result[key] = val
    except (zipfile.BadZipFile, ET.ParseError, _SafeXMLParseError,
            _DefusedXmlException, KeyError, OSError, StopIteration):
        pass
    return result


def build_comicinfo_xml(series: dict, volume_num: float | None = None,
                         chapter_num: float | None = None,
                         tags: list[str] | None = None) -> str:
    """
    Build a ComicInfo.xml v2.1 string (Anansi Project spec) for a volume or chapter file.
    Compatible with both Kavita and Komga.

    series dict keys used: title, description, status, pub_year, total_volumes,
                           total_chapters, language, anilist_id
    """
    def esc(v: str) -> str:
        return (v or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')

    title       = esc(series.get('title') or '')
    description = esc(series.get('description') or '')
    pub_year    = series.get('pub_year') or ''
    total_vols  = series.get('total_volumes') or -1
    language    = series.get('language') or 'en'
    status      = (series.get('status') or '').upper()
    tag_str     = esc(','.join(tags or []))

    # Map AniList status to ComicInfo Count hint
    is_complete = status in ('FINISHED', 'CANCELLED')
    count_val   = str(total_vols) if (is_complete and total_vols and total_vols > 0) else '-1'

    # Volume or chapter context
    if volume_num is not None:
        vol_tag = f'  <Volume>{int(volume_num)}</Volume>\n'
        num_tag = ''
    elif chapter_num is not None:
        ch_int  = int(chapter_num) if chapter_num == int(chapter_num) else chapter_num
        vol_tag = '  <Volume>0</Volume>\n'
        num_tag = f'  <Number>{ch_int}</Number>\n'
    else:
        vol_tag = ''
        num_tag = ''

    manga_val = 'YesAndRightToLeft'   # manga reads right-to-left

    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<ComicInfo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
        '           xmlns:xsd="http://www.w3.org/2001/XMLSchema">',
        f'  <Series>{title}</Series>',
    ]
    if vol_tag:  lines.append(vol_tag.rstrip())
    if num_tag:  lines.append(num_tag.rstrip())
    if count_val != '-1': lines.append(f'  <Count>{count_val}</Count>')
    if description:       lines.append(f'  <Summary>{description}</Summary>')
    if pub_year:          lines.append(f'  <Year>{pub_year}</Year>')
    if language:          lines.append(f'  <LanguageISO>{language}</LanguageISO>')
    if tag_str:           lines.append(f'  <Tags>{tag_str}</Tags>')
    lines += [
        f'  <Manga>{manga_val}</Manga>',
        '</ComicInfo>',
    ]
    return '\n'.join(lines)


def inject_comicinfo(cbz_path: str, xml_content: str) -> bool:
    """
    Inject or replace ComicInfo.xml at the root of a CBZ (ZIP) file.
    Returns True on success, False if file is not a valid ZIP or on error.
    Uses magic bytes (not extension) to detect file type — handles files
    with wrong extensions. CBR/RAR, EPUB, and PDF are skipped (return False).
    Any existing ComicInfo.xml is stripped and replaced (our DB metadata is authoritative).
    """
    # Use magic bytes first; fall back to extension only if file not readable
    file_type = detect_file_type_magic(cbz_path)
    if file_type is None:
        # Unreadable or non-existent file — fall back to extension check
        ext = os.path.splitext(cbz_path)[1].lower()
        if ext not in ('.cbz', '.zip'):
            return False
    elif file_type != 'cbz':
        return False   # CBR, EPUB, PDF — not injectable
    try:
        # Read existing archive contents (excluding any old ComicInfo.xml)
        with zipfile.ZipFile(cbz_path, 'r') as zf:
            entries = [
                (name, zf.read(name))
                for name in zf.namelist()
                if not name.lower().endswith('comicinfo.xml')
            ]
        # Rewrite archive with new ComicInfo.xml at root
        with zipfile.ZipFile(cbz_path, 'w', zipfile.ZIP_STORED) as zf:
            zf.writestr('ComicInfo.xml', xml_content.encode('utf-8'))
            for name, data in entries:
                zf.writestr(name, data)
        return True
    except (zipfile.BadZipFile, OSError, Exception) as e:
        print(f"[ComicInfo] Failed to inject into {cbz_path}: {e}")
        return False


def _try_inject_comicinfo(dst_path: str, series_row, volume_num=None,
                           chapter_num=None, tags: list[str] | None = None) -> None:
    """Best-effort ComicInfo.xml injection — uses magic bytes, non-fatal on error."""
    if not dst_path or not os.path.isfile(dst_path):
        return
    # Fast-path: skip obvious non-injectables by extension before opening the file
    ext = os.path.splitext(dst_path)[1].lower()
    if ext in ('.epub', '.pdf', '.mobi', '.azw3'):
        return
    try:
        xml = build_comicinfo_xml(dict(series_row), volume_num=volume_num,
                                   chapter_num=chapter_num, tags=tags or [])
        inject_comicinfo(dst_path, xml)
    except Exception as e:
        print(f"[ComicInfo] Inject failed for {dst_path}: {e}")


def _series_library_dir(db, series_id: int) -> str | None:
    """Return the library directory path for a series, or None if not configured."""
    s = db.execute(
        "SELECT title, root_folder_id, pub_year FROM series WHERE id=?", (series_id,)
    ).fetchone()
    if not s:
        return None
    rf = db.execute(
        "SELECT path FROM root_folders WHERE id=?", (s['root_folder_id'],)
    ).fetchone() if s['root_folder_id'] else None
    dest_root = rf['path'] if rf else get_cfg('save_path', '/manga')
    title = s['title'] or 'Unknown'
    fmt = get_cfg('folder_format', '').strip()
    if fmt:
        safe_dir = _apply_format_tokens(fmt, title, pub_year=s['pub_year'])
        safe_dir = sanitize_filename(safe_dir)
    else:
        safe_dir = sanitize_filename(title)
    return os.path.join(dest_root, safe_dir)


def rescan_series_folder(db, series_id: int) -> dict:
    """
    Walk the series' library directory and reconcile volume and pack statuses:
    - File found on disk but volume is wanted/grabbed     → mark downloaded
    - Volume is downloaded but no matching file found     → reset to wanted
    - Volume is grabbed but download no longer active     → reset to wanted
    - Pack (volume_num IS NULL) confirmed on disk         → mark downloaded + cascade stubs
    - Pack is grabbed but no files and download is gone   → reset to wanted
    - File on disk with no stub at all                   → create stub and mark downloaded
    Returns {'found': N, 'recovered': N, 'missing': N, 'lost': N, 'created': N}
    """
    series_dir = _series_library_dir(db, series_id)
    _s_row = db.execute("SELECT title FROM series WHERE id=?", (series_id,)).fetchone()
    _s_title = _s_row['title'] if _s_row else ''

    # ── Scan library directory ────────────────────────────────────────────────
    on_disk: set[float] = set()   # numbered volumes confirmed in library
    any_lib_files = False          # any manga file at all in series library dir
    if series_dir and os.path.isdir(series_dir):
        for root, dirs, files in os.walk(series_dir):
            for fname in files:
                if os.path.splitext(fname)[1].lower() not in MANGA_EXTENSIONS:
                    continue
                any_lib_files = True
                vol = extract_volume_num(fname)
                if vol is not None:
                    on_disk.add(vol)

    recovered = missing = lost = created = 0

    # ── Numbered volume stubs ─────────────────────────────────────────────────
    stubs = db.execute(
        "SELECT id, volume_num, status, download_id, torrent_name, client FROM volumes "
        "WHERE series_id=? AND volume_num IS NOT NULL",
        (series_id,)
    ).fetchall()
    stubbed_vols: set[float] = {stub['volume_num'] for stub in stubs}

    for stub in stubs:
        vol      = stub['volume_num']
        has_file = vol in on_disk

        if stub['status'] == 'downloaded' and not has_file:
            # Suwayomi files live in Suwayomi's own directory, not the managed library
            if stub['client'] == 'suwayomi':
                continue
            # File was deleted from library
            _vol_label = f"Vol {vol_num_to_display(vol)}"
            add_history(db, 'file_deleted', series_id, _s_title, _vol_label,
                        source_title=stub['torrent_name'] or '')
            db.execute(
                "UPDATE volumes SET status='wanted', import_path=NULL, download_id=NULL,"
                " torrent_name=NULL, indexer=NULL, protocol=NULL, client=NULL,"
                " grabbed_at=NULL, imported_at=NULL, source_url=NULL, release_group=NULL "
                "WHERE id=?", (stub['id'],)
            )
            _cascade_chapters(db, series_id, [stub['id']], 'wanted',
                              grabbed_at=None, torrent_name=None, torrent_url=None,
                              indexer=None, protocol=None, client=None,
                              download_id=None, release_group=None)
            missing += 1
        elif stub['status'] in ('wanted', 'grabbed') and has_file:
            # File exists on disk but status not updated — recover it
            matched_path = None
            if series_dir and os.path.isdir(series_dir):
                for _root, _dirs, _files in os.walk(series_dir):
                    for _fname in _files:
                        if os.path.splitext(_fname)[1].lower() in MANGA_EXTENSIONS:
                            _v = extract_volume_num(_fname)
                            if _v is not None and abs(_v - vol) < 0.01:
                                matched_path = os.path.join(_root, _fname)
                                break
                    if matched_path:
                        break
            # Opportunistically convert CBR to CBZ on recovery (best-effort)
            if matched_path:
                matched_path = _maybe_convert_to_cbz(matched_path)
            file_size = os.path.getsize(matched_path) if matched_path else 0
            file_qual = quality_from_filename(matched_path or '') if matched_path else None
            db.execute(
                "UPDATE volumes SET status='downloaded', import_path=?,"
                " size_bytes=COALESCE(NULLIF(size_bytes,0),?),"
                " quality=COALESCE(quality,?),"
                " imported_at=COALESCE(imported_at,?) WHERE id=?",
                (matched_path, file_size, file_qual,
                 datetime.utcnow().isoformat(), stub['id'])
            )
            # Inject ComicInfo.xml into newly-recovered files (best-effort)
            if matched_path:
                _s_full = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
                _tags   = [r['tag'] for r in db.execute(
                    "SELECT tag FROM series_tags WHERE series_id=?", (series_id,)
                ).fetchall()]
                if _s_full:
                    _try_inject_comicinfo(matched_path, _s_full, volume_num=vol, tags=_tags)
            recovered += 1
        # NOTE: "grabbed but not in client" is intentionally NOT handled here.
        # check_download_status() manages that by querying qBit/SABnzbd directly.

    # ── Backfill quality for downloaded volumes where it was never set ─────────
    # Handles volumes imported before the quality column existed, or where the
    # import pipeline set quality=NULL (e.g. manual file drops, pre-migration data).
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
        WHERE series_id = ?
          AND status = 'downloaded'
          AND quality IS NULL
          AND import_path IS NOT NULL
    """, (series_id,))

    # ── Pack entries (volume_num IS NULL) ─────────────────────────────────────
    packs = db.execute(
        "SELECT id, pack_type, vol_range_start, vol_range_end, status, download_id, import_path "
        "FROM volumes WHERE series_id=? AND volume_num IS NULL",
        (series_id,)
    ).fetchall()

    for pack in packs:
        dl_id = (pack['download_id'] or '').lower()
        pt    = pack['pack_type'] or ''

        if pack['status'] == 'downloaded':
            # Verify the imported file still exists; if not, remove the pack stub entirely
            if pack['import_path'] and not os.path.exists(pack['import_path']):
                db.execute("DELETE FROM volumes WHERE id=?", (pack['id'],))
                missing += 1
            continue

        if pack['status'] != 'grabbed':
            continue

        # Determine if this pack's content is confirmed on disk
        confirmed = False
        if pt == 'complete':
            # Complete series pack — any library file means it landed
            confirmed = any_lib_files
        elif pt == 'volume' and pack['vol_range_start'] is not None and pack['vol_range_end'] is not None:
            # Range pack — at least one covered volume file must be present
            confirmed = any(
                pack['vol_range_start'] <= v <= pack['vol_range_end'] for v in on_disk
            )
        # chapter-type packs (spin-offs) rely solely on import_path; skip disk inference

        if confirmed:
            db.execute("UPDATE volumes SET status='downloaded' WHERE id=?", (pack['id'],))
            # Cascade status to the covered numbered stubs
            if pt == 'complete':
                db.execute(
                    "UPDATE volumes SET status='downloaded'"
                    " WHERE series_id=? AND volume_num IS NOT NULL AND status != 'downloaded'",
                    (series_id,)
                )
                _cascade_chapters(db, series_id, None, 'downloaded')
            elif pt == 'volume':
                db.execute(
                    "UPDATE volumes SET status='downloaded'"
                    " WHERE series_id=? AND volume_num IS NOT NULL AND status != 'downloaded'"
                    " AND volume_num >= ? AND volume_num <= ?",
                    (series_id, pack['vol_range_start'], pack['vol_range_end'])
                )
                rng_ids = [r['id'] for r in db.execute(
                    "SELECT id FROM volumes WHERE series_id=? AND volume_num IS NOT NULL"
                    " AND volume_num >= ? AND volume_num <= ?",
                    (series_id, pack['vol_range_start'], pack['vol_range_end'])
                ).fetchall()]
                if rng_ids:
                    _cascade_chapters(db, series_id, rng_ids, 'downloaded')
            recovered += 1
        # NOTE: pack orphan cleanup is handled by check_download_status() via qBit query.

    # ── Create stubs for files on disk that have no stub yet ─────────────────
    # This is the primary mechanism for non-standard editions (omnibus, deluxe, etc.)
    # where AniList's volume count is unreliable and stubs are not auto-created on add.
    unmatched = on_disk - stubbed_vols
    if unmatched:
        _s_meta = db.execute(
            "SELECT monitor_mode, edition_type FROM series WHERE id=?", (series_id,)
        ).fetchone()
        _monitored = 1 if (_s_meta and (_s_meta['monitor_mode'] or 'all') in ('all', 'missing')) else 0
        for vol_num in sorted(unmatched):
            matched_path = None
            if series_dir and os.path.isdir(series_dir):
                for _root, _dirs, _files in os.walk(series_dir):
                    for _fname in _files:
                        if os.path.splitext(_fname)[1].lower() in MANGA_EXTENSIONS:
                            _v = extract_volume_num(_fname)
                            if _v is not None and abs(_v - vol_num) < 0.01:
                                matched_path = os.path.join(_root, _fname)
                                break
                    if matched_path:
                        break
            if matched_path:
                matched_path = _maybe_convert_to_cbz(matched_path)
            file_size = os.path.getsize(matched_path) if matched_path else 0
            file_qual = quality_from_filename(matched_path or '') if matched_path else None
            db.execute(
                "INSERT INTO volumes(series_id, volume_num, status, import_path,"
                " size_bytes, quality, imported_at, monitored)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (series_id, vol_num, 'downloaded', matched_path,
                 file_size, file_qual, datetime.utcnow().isoformat(), _monitored)
            )
            created += 1
            if matched_path:
                _s_full = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
                _tags   = [r['tag'] for r in db.execute(
                    "SELECT tag FROM series_tags WHERE series_id=?", (series_id,)
                ).fetchall()]
                if _s_full:
                    _try_inject_comicinfo(matched_path, _s_full, volume_num=vol_num, tags=_tags)
        # Update total_volumes if newly created stubs push the max higher
        if created:
            max_row = db.execute(
                "SELECT MAX(volume_num) as m FROM volumes"
                " WHERE series_id=? AND volume_num IS NOT NULL", (series_id,)
            ).fetchone()
            if max_row and max_row['m'] is not None:
                new_max = int(max_row['m'])
                tv_row = db.execute(
                    "SELECT total_volumes FROM series WHERE id=?", (series_id,)
                ).fetchone()
                current_tv = tv_row['total_volumes'] if tv_row else None
                if current_tv is None or new_max > current_tv:
                    db.execute(
                        "UPDATE series SET total_volumes=? WHERE id=?", (new_max, series_id)
                    )

    return {'found': len(on_disk), 'recovered': recovered, 'missing': missing, 'lost': lost, 'created': created}

async def grab_url(url: str, protocol: str = '', save_path: str | None = None,
                   torrent_name: str | None = None,
                   series_id: int | None = None) -> tuple[bool, str, str | None]:
    """Route to best available download client. Returns (success, client_name, download_id)."""
    use_torrent = protocol == 'torrent' or url.endswith('.torrent') or url.startswith('magnet:')
    detected_protocol = 'torrent' if use_torrent else 'nzb'

    from routers.download_clients import (
        get_client_for_protocol, _cb_is_open, _cb_record_success, _cb_record_failure
    )
    series_tags: list[str] = []
    if series_id:
        with get_db() as _tdb:
            series_tags = [r['tag'] for r in _tdb.execute(
                "SELECT tag FROM series_tags WHERE series_id=?", (series_id,)
            ).fetchall()]
    with get_db() as _tdb:
        client = get_client_for_protocol(_tdb, detected_protocol, series_tags)

    if not client:
        print(f"[grab_url] No download client configured for {detected_protocol}")
        return False, 'none', None

    client_id = client.get('id', 0) or 0
    if _cb_is_open(client_id):
        print(f"[grab_url] Circuit open for client {client['name']} — skipping grab")
        return False, client['name'], None

    ctype = client['type']
    # Each grab function returns (ok, dl_id, client_healthy). client_healthy
    # distinguishes "reachable + auth worked but matching failed" from "real
    # connection or auth failure". CB only trips on the latter.
    if ctype == 'qbittorrent':
        ok, dl_id, healthy = await qbit_grab(url, client=client, save_path=save_path, torrent_name=torrent_name)
    elif ctype == 'sabnzbd':
        ok, dl_id, healthy = await sab_grab(url, client=client, save_path=save_path)
    elif ctype == 'blackhole':
        ok, dl_id, healthy = await blackhole_grab(url, client=client, torrent_name=torrent_name)
    elif ctype == 'nzbget':
        ok, dl_id, healthy = await nzbget_grab(url, client=client)
    else:
        print(f"[grab_url] Client type '{ctype}' not yet implemented")
        return False, client['name'], None

    if healthy:
        # Client reachable + auth OK → reset CB regardless of whether the
        # individual grab succeeded (business-logic failures don't indicate
        # an unhealthy client).
        _cb_record_success(client_id)
    else:
        _cb_record_failure(client_id)
    return ok, (client.get('type') or client['name']).lower(), dl_id


async def nzbget_grab(nzb_url: str, client: dict | None = None) -> tuple[bool, str | None, bool]:
    """Add to NZBGet via JSON-RPC. Returns (success, nzb_id_or_None, client_healthy)."""
    host = ((client or {}).get('host') or '').rstrip('/')
    user = (client or {}).get('username') or ''
    pw   = (client or {}).get('password') or ''
    cat  = (client or {}).get('category') or get_cfg('category')
    port = (client or {}).get('port') or 6789
    api_url = f"http://{user}:{pw}@{host}:{port}/jsonrpc"
    try:
        async with httpx.AsyncClient(timeout=20) as cli:
            r = await cli.post(api_url, json={
                "method": "append",
                "params": [nzb_url, cat, 0, False, "", 0, "SCORE"]
            })
            data = r.json()
            nzb_id = data.get('result')
            if nzb_id and nzb_id > 0:
                return True, str(nzb_id), True
            # NZBGet reachable but rejected the add — still healthy
            return False, None, True
    except Exception as e:
        print(f"[NZBGet] grab error: {e}")
        return False, None, False


async def blackhole_grab(url: str, client: dict,
                         torrent_name: str | None = None) -> tuple[bool, str | None, bool]:
    """Download a .torrent file and drop it in the blackhole folder.
    Returns (success, dl_id, client_healthy)."""
    import os
    folder = (client.get('host') or '').strip()
    if not folder or not os.path.isdir(folder):
        print(f"[Blackhole] Folder not found: {folder!r}")
        return False, None, False  # misconfig = unhealthy
    fname = (torrent_name or 'download') + '.torrent'
    fname = re.sub(r'[<>:"/\\|?*]', '_', fname)
    dest  = os.path.join(folder, fname)
    try:
        if url.startswith('magnet:'):
            dest = dest.replace('.torrent', '.magnet')
            with open(dest, 'w') as f:
                f.write(url)
        else:
            async with httpx.AsyncClient(timeout=20) as cli:
                r = await cli.get(url, follow_redirects=True)
                if r.status_code != 200:
                    # Tracker URL failed — client itself (folder) is fine
                    return False, None, True
            with open(dest, 'wb') as f:
                f.write(r.content)
        return True, os.path.basename(dest), True
    except Exception as e:
        print(f"[Blackhole] grab error: {e}")
        return False, None, False

async def notify_discord(message: str, embed: dict | None = None,
                         event: str = 'on_grab'):
    """Send notifications via all enabled notification connections."""
    from routers.notification_connections import fire_notifications
    await fire_notifications(event, message, embed=embed)

def make_grab_embed(series_title: str, vol_label: str, indexer: str,
                    protocol: str, client_name: str, cover_url: str = '') -> dict:
    return {
        'title': f'⬇ Grabbed — {series_title}',
        'description': f'**{vol_label}**  ·  {indexer} [{protocol}] → {client_name}',
        'color': 0xffd060,
        'thumbnail': {'url': cover_url} if cover_url else {},
    }

def make_complete_embed(series_title: str, vol_label: str, cover_url: str = '') -> dict:
    return {
        'title': f'✅ Downloaded — {series_title}',
        'description': f'**{vol_label}** download complete',
        'color': 0x5dde94,
        'thumbnail': {'url': cover_url} if cover_url else {},
    }

async def trigger_komga_scan():
    """Optionally trigger a Komga library scan after downloads complete."""
    if get_cfg('komga_scan_enabled', 'false').lower() != 'true':
        return
    url = get_cfg('komga_url')
    lib = get_cfg('komga_library_id')
    if not url or not lib:
        return
    user = get_cfg('komga_user')
    pw   = get_cfg('komga_pass')
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{url}/api/v1/libraries/{lib}/scan",
                auth=(user, pw) if user else None
            )
        log_event('komga_scan', f"Triggered Komga library scan → HTTP {r.status_code}")
    except Exception as e:
        log_event('error', f"Komga scan failed: {e}")

MANGA_EXTENSIONS = {'.cbz', '.cbr', '.zip', '.rar', '.pdf', '.epub', '.mobi'}

# Edition types where AniList's total_volumes reflects the *standard* edition count,
# not the special edition count. Stub auto-creation is suppressed for these; stubs
# are instead created by rescan once real files are present.
_NON_STANDARD_STUB_EDITIONS = {'omnibus', 'deluxe', 'special', 'collector', 'remaster'}

# URLs currently in-flight to a download client.  asyncio is single-threaded so
# plain set ops are safe between awaits.  Prevents duplicate grabs when RSS poll
# and backlog search both pass the `seen` check before either INSERT completes.
_GRABBING_URLS: set[str] = set()

# Search keywords used when querying Google Books for edition-specific volume counts.
# Listed from most-specific to least-specific so we try the best match first.
_EDITION_SEARCH_KEYWORDS: dict[str, list[str]] = {
    'omnibus':   ['omnibus', '2-in-1', '3-in-1', 'two-in-one', 'three-in-one', 'two in one'],
    'deluxe':    ['deluxe edition', 'deluxe'],
    'collector': ["collector's edition", 'collector'],
    'special':   ['special edition'],
    'remaster':  ['remaster', 'remastered'],
}

# ── Quality ranking ───────────────────────────────────────────────────────────
QUALITY_RANK: dict[str, int] = {
    'cbz':  5,
    'zip':  5,   # zip = cbz functionally
    'cbr':  4,
    'rar':  4,   # rar = cbr functionally
    'epub': 3,
    'mobi': 2,
    'pdf':  1,
}

def quality_from_filename(filename: str) -> str | None:
    """
    Return the quality tier string for a file.
    For files on disk, uses magic bytes (more reliable than extension).
    Falls back to extension-based detection for filenames without a path.
    """
    if filename and os.path.isfile(filename):
        magic_type = detect_file_type_magic(filename)
        if magic_type and magic_type in QUALITY_RANK:
            return magic_type
    ext = os.path.splitext(filename)[1].lstrip('.').lower()
    return ext if ext in QUALITY_RANK else None

def quality_rank(q: str | None) -> int:
    """Return numeric rank for a quality string. None/unknown = 0."""
    return QUALITY_RANK.get((q or '').lower(), 0)


_EDITION_PATTERNS = [
    # Official color (Viz Full Color, publisher color releases — highest tier)
    (r'\bofficial[\s-]?colou?r\b',                      'official_color'),
    (r'\bfull[\s-]?colou?r\b',                          'official_color'),
    (r'\bviz[\s-]?(?:full[\s-]?)?colou?r\b',           'official_color'),
    (r'\bdigital\s+colou?red?\b',                       'official_color'),
    # Fan / scan colorizations (lower tier than official)
    (r'\bcolou?red?\s+edition\b',                       'colored'),
    (r'\bin\s+colou?r\b',                               'colored'),
    (r'\bcolou?red\b',                                   'colored'),   # bare: [ColoredManga], "Colored"
    (r'\bcoloredmanga\b',                                'colored'),
    # Deluxe / hardcover
    (r'\bdeluxe\b',                                     'deluxe'),
    (r'\bhardcover\b|\bhc\b(?!\w)',                     'deluxe'),
    (r'\banniversary\s+edition\b',                      'deluxe'),
    # Omnibus / collected editions
    (r'\bomnibus\b',                                    'omnibus'),
    (r'\bvizbig\b',                                     'omnibus'),
    (r'\bgrand\s+edition\b',                            'omnibus'),
    (r'\bperfect\s+edition\b',                          'omnibus'),
    (r'\bcollected\s+edition\b',                        'omnibus'),
    (r'\bcomplete\s+collection\b',                      'omnibus'),
    (r'\b3-in-1\b|\bthree-in-one\b',                   'omnibus'),
    (r'\b2-in-1\b|\btwo-in-one\b',                     'omnibus'),
    # Special / limited / collector
    (r'\bcollector(?:\'?s)?\b',                        'collector'),
    (r'\bspecial\b(?:\s+edition)?',                    'special'),
    (r'\blimited\b(?:\s+edition)?',                    'special'),
    (r'\bcanonical\s+edition\b',                       'special'),
    # Remaster
    (r'\bremaster(?:ed)?\b',                            'remaster'),
    (r'\bhd\s+edition\b',                               'remaster'),
]

_LANGUAGE_PATTERNS = [
    (r'\b(?:english|eng)\b',              'en'),
    (r'\b(?:japanese?|jpn?)\b',           'ja'),
    (r'\b(?:french|fran[çc]ais|fre?)\b', 'fr'),
    (r'\b(?:german|deutsch|ger)\b',       'de'),
    (r'\b(?:spanish?|espa[ñn]ol|spa)\b', 'es'),
    (r'\b(?:italian[oe]?|ita)\b',        'it'),
    (r'\b(?:portuguese?|portugu[eê]s|por)\b', 'pt'),
    (r'\b(?:korean?|kor)\b',             'ko'),
    (r'\b(?:chinese?|mandarin|chi|chs|cht)\b', 'zh'),
    (r'\b(?:russian?|rus)\b',             'ru'),
    (r'\b(?:arabic|ara)\b',               'ar'),
    (r'\b(?:polish?|pol)\b',              'pl'),
    (r'\b(?:dutch|nederlanden?|dut)\b',  'nl'),
    (r'\b(?:thai|tha)\b',                'th'),
    (r'\b(?:vietnamese?|vie)\b',          'vi'),
    (r'\b(?:indonesian?|ind)\b',          'id'),
]

def detect_edition_type(title: str) -> str | None:
    """Detect edition type from a release title (Deluxe, Omnibus, Digital, etc.)."""
    tl = title.lower()
    for pattern, edition in _EDITION_PATTERNS:
        if re.search(pattern, tl):
            return edition
    return None

def detect_language(title: str) -> str | None:
    """Detect language code from a release title."""
    tl = title.lower()
    for pattern, lang in _LANGUAGE_PATTERNS:
        if re.search(pattern, tl):
            return lang
    return None


# ── Source type detection: official publishers vs fan scanlations ─────────────
#
# Official = licensed English-language publishers. These produce authoritative,
# clean digital editions. Pattern-matched against release titles (case-insensitive).
#
# Each entry is a regex pattern. Word-boundary anchors avoid short strings like
# "viz" matching inside series titles (e.g. "Devize").
_OFFICIAL_PUBLISHER_PATTERNS: list[str] = [
    r'\bviz\s*(?:media|digital|big)?\b',   # Viz, Viz Media, Viz Digital, VIZBIG
    r'\bkodansha\b',
    r'\bseven\s+seas\b',
    r'\byen\s+press\b',
    r'\bdark\s+horse\b',
    r'\bsquare\s+enix\b',
    r'\bj[-\s]?novel\s*(?:club)?\b',
    r'\bvertical\s+(?:comics?|inc\.?)\b',
    r'\btokyopop\b',
    r'\bshogakukan\b',
    r'\bshueisha\b',
    r'\bmanga\s*plus\b',                    # Shueisha's official platform
    r'\bone\s+peace\s+books\b',
    r'\bghost\s+ship\b',                    # Seven Seas adult imprint
    r'\bairship\b',                         # Seven Seas YA imprint
    r'\blezhin\b',                          # Korean webtoon official
    r'\bwebtoons?\s+(?:official|originals?|canvas)\b',
    r'\btapas\s+media\b',
    r'\bcrunchyroll\s+manga\b',
    r'\bazuki\s+(?:digital|comics?|manga)\b',  # anchored to avoid false positives
]
_OFFICIAL_RE = re.compile('|'.join(_OFFICIAL_PUBLISHER_PATTERNS), re.IGNORECASE)

# Known quality fan scanlation groups. Used for score bonus only (not for 'official' detection).
_FAN_GROUP_PATTERNS: list[str] = [
    r'\blucaz\b', r'\b1r0n\b', r'\bdanke\b', r'\bstick\b', r'\bjcafe\b',
    r'\bathena\b', r'\bdbs\b', r'\bcxc\b', r'\bhabanero\b', r'\btnt[-\s]empire\b',
    r'\bkc\b',    # Kodansha (fan re-releases)
    r'\bclover\b', r'\bempire\b', r'\blostnere?varine\b',
]
_FAN_GROUP_RE = re.compile('|'.join(_FAN_GROUP_PATTERNS), re.IGNORECASE)


def is_official_release(title: str) -> bool:
    """Return True if the release title contains a known licensed publisher name."""
    return bool(_OFFICIAL_RE.search(title))


def is_quality_fan_release(title: str) -> bool:
    """Return True if the title matches a known quality fan scanlation group."""
    return bool(_FAN_GROUP_RE.search(title))


def classify_source_type(title: str) -> str:
    """
    Classify a release title as 'official' or 'fan'.
    Used to display source type in the UI and for source_type filtering.
    """
    return 'official' if is_official_release(title) else 'fan'


# ── File type detection via magic bytes ───────────────────────────────────────
_MAGIC_ZIP  = b'PK\x03\x04'       # ZIP / CBZ / EPUB
_MAGIC_RAR4 = b'Rar!\x1a\x07\x00' # RAR v4
_MAGIC_RAR5 = b'Rar!\x1a\x07\x01' # RAR v5
_MAGIC_PDF  = b'%PDF'

def detect_file_type_magic(path: str) -> str | None:
    """
    Detect the actual file type by reading magic bytes (not trusting the extension).
    Returns: 'cbz', 'cbr', 'epub', 'pdf', or None for unknown/unreadable.
    """
    try:
        with open(path, 'rb') as f:
            header = f.read(8)
    except OSError:
        return None
    if header[:4] == _MAGIC_ZIP:
        # ZIP could be CBZ or EPUB — check for EPUB-specific mimetype entry
        try:
            with zipfile.ZipFile(path, 'r') as zf:
                if 'mimetype' in zf.namelist():
                    mt = zf.read('mimetype').decode('ascii', errors='ignore').strip()
                    if 'epub' in mt:
                        return 'epub'
        except Exception:
            pass
        return 'cbz'
    if header[:8] in (_MAGIC_RAR4, _MAGIC_RAR5):
        return 'cbr'
    if header[:7] in (_MAGIC_RAR4[:7], _MAGIC_RAR5[:7]):
        return 'cbr'   # partial header match
    if header[:4] == _MAGIC_PDF:
        return 'pdf'
    return None


# ── CBR → CBZ conversion ──────────────────────────────────────────────────────
def convert_cbr_to_cbz(cbr_path: str) -> str | None:
    """
    Convert a CBR (RAR) archive to a CBZ (ZIP) file.
    Creates a new .cbz file alongside the original .cbr.
    Returns the path to the new CBZ on success, None on failure.
    The original CBR is NOT removed — call site decides what to do with it.
    """
    try:
        import rarfile as _rarfile
    except ImportError:
        print("[CBR→CBZ] rarfile not available; cannot convert CBR")
        return None

    cbz_path = os.path.splitext(cbr_path)[0] + '.cbz'
    try:
        with _rarfile.RarFile(cbr_path, 'r') as rf:
            entries = [
                (name, rf.read(name))
                for name in rf.namelist()
                if not rf.getinfo(name).is_dir()
                and not name.lower().endswith('comicinfo.xml')
            ]
        if not entries:
            return None
        with zipfile.ZipFile(cbz_path, 'w', zipfile.ZIP_STORED) as zf:
            for name, data in entries:
                zf.writestr(name, data)
        return cbz_path
    except Exception as e:
        print(f"[CBR→CBZ] Failed to convert {cbr_path}: {e}")
        if os.path.exists(cbz_path):
            try:
                os.remove(cbz_path)
            except OSError:
                pass
        return None


def _maybe_convert_to_cbz(path: str) -> str:
    """
    If path is a CBR file (detected by magic bytes), convert it to CBZ,
    remove the original CBR, and return the new .cbz path.
    For all other types (CBZ, EPUB, PDF) returns path unchanged.
    Non-fatal — returns original path on any failure.
    """
    if not path or not os.path.isfile(path):
        return path
    file_type = detect_file_type_magic(path)
    if file_type != 'cbr':
        return path
    cbz_path = convert_cbr_to_cbz(path)
    if cbz_path and os.path.isfile(cbz_path):
        if os.path.abspath(cbz_path) != os.path.abspath(path):
            # Different paths: safe to remove the original CBR
            try:
                os.remove(path)
                print(f"[CBR→CBZ] Converted and removed original: {os.path.basename(path)}")
            except OSError as e:
                print(f"[CBR→CBZ] Converted but could not remove original: {e}")
        else:
            # Same path (CBR had .cbz extension): already overwritten in-place, nothing to remove
            print(f"[CBR→CBZ] Converted in-place (was CBR with .cbz extension): {os.path.basename(path)}")
        return cbz_path
    return path   # conversion failed — keep original


def _queue_import(db, series_id: int, download_id: str, torrent_name: str,
                  torrent_url: str, volume_num: float | None,
                  content_path: str) -> tuple[int | None, bool]:
    """
    Scan completed download files at content_path (from download client) and create
    an import_queue entry. Returns (queue_id, needs_review).
    needs_review=False means all files mapped cleanly → can auto-import.
    needs_review=True means at least one file is ambiguous → requires user review.
    """
    if not content_path:
        log_event('error', f"Import queue: no content_path for {torrent_name}", series_id)
        return None, False

    s = db.execute(
        "SELECT title, root_folder_id, chapter_vol_map FROM series WHERE id=?", (series_id,)
    ).fetchone()
    if not s:
        return None, False

    # Check early: if this download is already fully imported, skip silently and
    # clean up any stale partial queue entry so it stops showing in the UI.
    already_done = db.execute(
        "SELECT 1 FROM volumes WHERE series_id=? AND download_id=? AND status='downloaded' LIMIT 1",
        (series_id, download_id)
    ).fetchone()
    if already_done:
        db.execute(
            "UPDATE import_queue SET status='imported' WHERE series_id=? AND download_id=?"
            " AND status IN ('partial','failed')",
            (series_id, download_id)
        )
        return None, False

    rf = db.execute(
        "SELECT path FROM root_folders WHERE id=?", (s['root_folder_id'],)
    ).fetchone() if s['root_folder_id'] else None
    cvm: dict = json.loads(s['chapter_vol_map']) if s['chapter_vol_map'] else {}

    # Determine scan scope: single-file torrent vs directory torrent.
    # If content_path is a file (single-file torrent saved directly to a shared dir),
    # we must NOT fall back to scanning the parent directory — that would pick up every
    # file in the library. Instead, scope the import to just that one file.
    if os.path.isdir(content_path):
        src_dir    = content_path
        scan_paths = None  # walk the directory below
    elif os.path.isfile(content_path):
        src_dir    = os.path.dirname(content_path)  # for display / storage only
        scan_paths = [content_path]                  # only this specific file
    else:
        log_event('error', f"Import queue: content_path not found: {content_path}", series_id)
        return None, False

    dest_root = rf['path'] if rf else get_cfg('save_path', '/manga')
    safe_dir  = sanitize_filename(s['title'] or 'Unknown')
    dst_dir   = os.path.join(dest_root, safe_dir)

    # Detect chapter-mode grab — chapter stubs have no volume_num and pack_type='chapter'
    _chap_stub = db.execute(
        "SELECT id FROM volumes WHERE series_id=? AND download_id=?"
        " AND status='grabbed' AND pack_type='chapter'",
        (series_id, download_id)
    ).fetchone()
    _is_chapter_grab = _chap_stub is not None

    # Don't re-queue a download that already has a queue entry in any state.
    # failed/skipped = terminal until user explicitly retries (retry endpoint resets to pending).
    existing = db.execute(
        "SELECT id, status FROM import_queue WHERE series_id=? AND download_id=? LIMIT 1",
        (series_id, download_id)
    ).fetchone()
    if existing:
        if existing['status'] == 'pending':
            # Check if any file actually needs review; if not, auto-import is safe
            has_review = db.execute(
                "SELECT 1 FROM import_queue_files WHERE queue_id=? AND status='needs_review'",
                (existing['id'],)
            ).fetchone()
            return existing['id'], bool(has_review)
        return None, False  # imported/partial/failed/skipped — don't re-queue

    cur = db.execute(
        "INSERT INTO import_queue(series_id, download_id, torrent_name, torrent_url, volume_num, src_dir, status)"
        " VALUES(?,?,?,?,?,?,'pending')",
        (series_id, download_id, torrent_name, torrent_url, volume_num, src_dir)
    )
    queue_id = cur.lastrowid

    # Build the list of files to consider
    if scan_paths is None:
        scan_paths = []
        for root, dirs, files in os.walk(src_dir):
            dirs.sort()
            for fname in sorted(files):
                scan_paths.append(os.path.join(root, fname))

    mapped = unmapped = 0
    for src_path in scan_paths:
        fname = os.path.basename(src_path)
        if os.path.splitext(fname)[1].lower() not in MANGA_EXTENSIONS:
            continue

        # Skip foreign-language files
        if is_foreign_language(fname):
            log_event('import', f"Skipped foreign-language file: {fname}", series_id)
            continue

        proposed_vol  = extract_volume_num(fname)
        proposed_chap = extract_chapter_num(fname)

        # ComicInfo.xml overrides filename-based detection for cbz/zip/cbr
        ext_lower = os.path.splitext(fname)[1].lower()
        if ext_lower in ('.cbz', '.zip'):
            ci = read_comic_info(src_path)
            if ci.get('volume') is not None:
                ci_vol = ci['volume']
                if ci_vol != proposed_vol:
                    log_event('import',
                        f"ComicInfo.xml: vol {proposed_vol} → {ci_vol} for {fname}",
                        series_id)
                    proposed_vol  = ci_vol
                    proposed_chap = None   # <Volume> tag wins — treat as volume file
            elif ci.get('number') is not None and proposed_chap is None:
                # <Number> without <Volume> typically means a chapter number
                proposed_chap = ci['number']
        elif ext_lower == '.cbr':
            try:
                import rarfile
                with rarfile.RarFile(src_path) as rf:
                    ci_name = next(
                        (n for n in rf.namelist() if n.lower().endswith('comicinfo.xml')),
                        None
                    )
                    if ci_name:
                        root = _safe_xml_fromstring(rf.read(ci_name))
                        def _cbr_text(tag: str):
                            el = root.find(tag)
                            return el.text.strip() if el is not None and el.text else None
                        _raw_vol = _cbr_text('Volume')
                        _raw_num = _cbr_text('Number')
                        if _raw_vol:
                            ci_vol = _parse_vol_suffix(_raw_vol)
                            if ci_vol is not None:
                                if ci_vol != proposed_vol:
                                    log_event('import',
                                        f"ComicInfo.xml (CBR): vol {proposed_vol} → {ci_vol} for {fname}",
                                        series_id)
                                proposed_vol  = ci_vol
                                proposed_chap = None
                        elif _raw_num and proposed_chap is None:
                            ci_num = _parse_vol_suffix(_raw_num)
                            if ci_num is not None:
                                proposed_chap = ci_num
            except ImportError:
                pass  # rarfile not installed
            except Exception:
                pass

        # Classify: a chapter file has a chapter num but no volume num
        if proposed_chap is not None and proposed_vol is None:
            file_type = 'chapter'
            # Resolve parent volume from chapter→volume map if available
            chap_key = str(int(proposed_chap)) if proposed_chap == int(proposed_chap) else str(proposed_chap)
            if chap_key in cvm:
                proposed_vol = float(cvm[chap_key])
        else:
            file_type = 'volume'
            proposed_chap = None  # discard spurious chapter detection for volume files

        # If filename has no volume number but we know it from the grab, use it (volume files only)
        if proposed_vol is None and volume_num is not None and file_type == 'volume':
            proposed_vol = volume_num

        dst_fname = build_filename(s['title'], proposed_vol, fname)
        dst_path  = os.path.join(dst_dir, dst_fname)

        if proposed_vol is None and proposed_chap is None and not _is_chapter_grab:
            unmapped += 1
        else:
            mapped += 1
        db.execute(
            "INSERT INTO import_queue_files"
            "(queue_id, filename, src_path, dst_path, proposed_volume, proposed_chapter, file_type, status)"
            " VALUES(?,?,?,?,?,?,?,'pending')",
            (queue_id, dst_fname, src_path, dst_path, proposed_vol, proposed_chap, file_type)
        )

    # No usable files found — remove the empty queue entry so the volume doesn't
    # get stuck in 'grabbed' state waiting for an import that can never complete.
    if mapped == 0 and unmapped == 0:
        db.execute("DELETE FROM import_queue WHERE id=?", (queue_id,))
        log_event('import', f"No manga files found in {src_dir} — skipping: {torrent_name}", series_id)
        return None, False

    # needs_review if ANY file is unmapped — user must confirm before the whole batch imports
    needs_review = unmapped > 0
    if unmapped > 0:
        log_event('import', f"Queued for review ({unmapped} unmapped file(s)): {torrent_name}", series_id)
    return queue_id, needs_review

async def check_download_status():
    """Poll download clients for completed downloads and queue them for import review."""
    # Clean up stale imported/failed entries older than 7 days
    with get_db() as _cdb:
        _cdb.execute(
            "DELETE FROM import_queue_files WHERE queue_id IN ("
            "  SELECT id FROM import_queue WHERE status IN ('imported','skipped')"
            "  AND created_at < datetime('now', '-7 days'))"
        )
        _cdb.execute(
            "DELETE FROM import_queue WHERE status IN ('imported','skipped')"
            " AND created_at < datetime('now', '-7 days')"
        )

    # Auto-prune expired blocklist entries
    _bl_ttl = max(0, int(get_cfg('blocklist_ttl_days', '90') or '90'))
    if _bl_ttl > 0:
        with get_db() as _bldb:
            _bl_deleted = _bldb.execute(
                "DELETE FROM blocklist WHERE added_at < datetime('now', ? || ' days')",
                (f'-{_bl_ttl}',)
            ).rowcount
            if _bl_deleted > 0:
                log_event('info', f"Auto-pruned {_bl_deleted} expired blocklist entr{'ies' if _bl_deleted != 1 else 'y'} (TTL: {_bl_ttl}d)")

    # Auto-reset grabbed volumes that are stuck (no activity for >2 days, not in import queue)
    with get_db() as _stuckdb:
        _stuck_count = _stuckdb.execute(
            "UPDATE volumes SET status='wanted', grabbed_at=NULL, download_id=NULL,"
            " source_url=NULL, torrent_name=NULL, indexer=NULL, protocol=NULL,"
            " client=NULL, release_group=NULL, import_path=NULL"
            " WHERE status='grabbed'"
            "   AND grabbed_at < datetime('now', '-2 days')"
            "   AND NOT EXISTS ("
            "     SELECT 1 FROM import_queue iq WHERE iq.download_id = volumes.download_id"
            "     AND iq.status IN ('pending','partial')"
            "   )"
        ).rowcount
        if _stuck_count > 0:
            log_event('info', f"Auto-reset {_stuck_count} stuck grabbed volume(s) back to wanted")

    # Auto-retry import_queue entries stuck in pending/partial > 2 hours
    with get_db() as _iq_db:
        stuck_pending = _iq_db.execute(
            "SELECT id FROM import_queue"
            " WHERE status IN ('pending','partial')"
            " AND created_at < datetime('now', '-2 hours')"
            " AND NOT EXISTS ("
            "   SELECT 1 FROM import_queue_files f"
            "   WHERE f.queue_id=import_queue.id AND f.status='needs_review'"
            " )"
        ).fetchall()
        stuck_ids = [r['id'] for r in stuck_pending]
    if stuck_ids:
        for _sid in stuck_ids:
            asyncio.create_task(_process_auto_import(_sid))
    from routers.download_clients import get_client_for_protocol, apply_remote_path_mapping
    with get_db() as _cdb:
        _qc = get_client_for_protocol(_cdb, 'torrent')
    host = ((_qc or {}).get('host') or '').rstrip('/')
    user = ((_qc or {}).get('username') or '')
    pw   = ((_qc or {}).get('password') or '')
    cat  = ((_qc or {}).get('category') or get_cfg('category'))
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{host}/api/v2/auth/login", data={'username': user, 'password': pw})
            if 'Ok' in r.text:
                # Fetch ALL torrents in our category so we can also detect removed ones
                r2 = await client.get(f"{host}/api/v2/torrents/info", params={'category': cat})
                if r2.status_code == 200:
                    all_torrents    = r2.json()
                    all_hashes      = {t['hash'].lower() for t in all_torrents}
                    # Completed = 100% progress (covers seeding, checkingUP, etc.)
                    completed       = [t for t in all_torrents if t.get('progress', 0) >= 1.0]
                    torrent_by_hash = {t['hash'].lower(): t for t in completed}
                    completed_names = {normalize(t['name']): t for t in completed}
                    log_event('info', f"qBit check: {len(completed)}/{len(all_torrents)} completed")

                    def _process_qbit_completed():
                        """Run in thread to avoid blocking the event loop."""
                        # Phase 1: quick read to get seen entries (short lock)
                        with get_db() as db:
                            rows = db.execute(
                                "SELECT torrent_url, torrent_name, series_id, volume_num, download_id "
                                "FROM seen WHERE client='qbittorrent' AND protocol='torrent'"
                            ).fetchall()

                        # Phase 2: match against completed torrents (no DB lock)
                        matched = []
                        for row in rows:
                            dl_id     = (row['download_id'] or '').lower()
                            name_norm = normalize(row['torrent_name'] or '')
                            torrent   = torrent_by_hash.get(dl_id) or completed_names.get(name_norm)
                            if torrent:
                                matched.append((row, torrent))

                        # Phase 3: queue imports one at a time (short lock per item)
                        _new_imports = []
                        for row, torrent in matched:
                            dl_id = (row['download_id'] or '').lower()
                            content_path = torrent.get('content_path') or torrent.get('save_path', '')
                            with get_db() as db:
                                content_path = apply_remote_path_mapping(db, content_path, host)
                                q_id, needs_review = _queue_import(
                                    db, row['series_id'], dl_id,
                                    row['torrent_name'] or '',
                                    row['torrent_url'] or '',
                                    row['volume_num'],
                                    content_path)
                            if q_id and not needs_review:
                                _new_imports.append(q_id)
                        return _new_imports

                    _new_imports = await asyncio.to_thread(_process_qbit_completed)
                    for _imp_id in _new_imports:
                        asyncio.create_task(_process_auto_import(_imp_id))

                    with get_db() as db:
                        # ── No-hash orphans: grabbed with no download_id → reset immediately ──
                        db.execute(
                            "UPDATE volumes SET status='wanted', grabbed_at=NULL, source_url=NULL,"
                            " download_id=NULL, torrent_name=NULL, indexer=NULL, protocol=NULL,"
                            " client=NULL, release_group=NULL"
                            " WHERE status='grabbed' AND download_id IS NULL AND volume_num IS NOT NULL"
                        )
                        db.execute(
                            "DELETE FROM volumes WHERE status='grabbed'"
                            " AND download_id IS NULL AND volume_num IS NULL"
                        )
                        # ── Orphan cleanup: grabbed volumes whose torrent is gone from client ──
                        orphaned = db.execute(
                            "SELECT DISTINCT v.download_id, v.series_id,"
                            " COALESCE(sv.torrent_name, v.torrent_name) as torrent_name "
                            "FROM volumes v "
                            "LEFT JOIN seen sv ON sv.download_id = v.download_id "
                            "WHERE v.status='grabbed' "
                            "  AND v.client='qbittorrent' "
                            "  AND v.download_id IS NOT NULL "
                            "  AND v.download_id NOT IN ("
                            "    SELECT download_id FROM import_queue"
                            "    WHERE status='pending' AND download_id IS NOT NULL)"
                        ).fetchall()
                        for gs in orphaned:
                            if (gs['download_id'] or '').lower() in all_hashes:
                                continue  # still present in client
                            h = gs['download_id']
                            # Collect numbered vol IDs before deleting, for chapter cascade
                            orphan_vol_ids = [
                                r[0] for r in db.execute(
                                    "SELECT id FROM volumes WHERE series_id=? AND download_id=?"
                                    " AND status='grabbed' AND volume_num IS NOT NULL",
                                    (gs['series_id'], h)
                                ).fetchall()
                            ]
                            # Pack stubs (volume_num IS NULL) are deleted; numbered stubs reset
                            db.execute(
                                "DELETE FROM volumes WHERE series_id=? AND download_id=?"
                                " AND status='grabbed' AND volume_num IS NULL",
                                (gs['series_id'], h)
                            )
                            db.execute(
                                "UPDATE volumes SET status='wanted', download_id=NULL,"
                                " torrent_name=NULL, indexer=NULL, protocol=NULL, client=NULL,"
                                " grabbed_at=NULL, source_url=NULL, release_group=NULL "
                                "WHERE series_id=? AND download_id=? AND status='grabbed'",
                                (gs['series_id'], h)
                            )
                            if orphan_vol_ids:
                                _cascade_chapters(db, gs['series_id'], orphan_vol_ids, 'wanted',
                                                  grabbed_at=None, torrent_name=None, torrent_url=None,
                                                  indexer=None, protocol=None, client=None,
                                                  download_id=None, release_group=None)
                            db.execute(
                                "UPDATE import_queue SET status='skipped' "
                                "WHERE download_id=? AND status='pending'", (h,)
                            )
                            db.execute(
                                "UPDATE import_queue_files SET status='skipped' "
                                "WHERE queue_id IN "
                                "(SELECT id FROM import_queue WHERE download_id=?)", (h,)
                            )
                            db.execute("DELETE FROM seen WHERE download_id=?", (h,))
                            log_event('warning',
                                f"Grab lost (removed from client): {gs['torrent_name']}",
                                gs['series_id'])
                            _sr = db.execute(
                                "SELECT title FROM series WHERE id=?", (gs['series_id'],)
                            ).fetchone()
                            add_history(db, 'grab_failed', gs['series_id'],
                                        _sr['title'] if _sr else '',
                                        '',
                                        source_title=gs['torrent_name'] or '',
                                        download_id=h,
                                        data={'reason': 'removed_from_client'})

                        # ── Failed download handling ──────────────────────────────────────────────
                        if get_cfg('failed_download_handling', '0') == '1':
                            all_torrent_by_hash = {t['hash'].lower(): t for t in all_torrents}
                            error_states = {'error', 'missingFiles', 'stalledDL'}
                            with get_db() as db:
                                seen_rows = db.execute(
                                    "SELECT download_id, series_id, torrent_name, torrent_url"
                                    " FROM seen WHERE client='qbittorrent' AND protocol='torrent'"
                                ).fetchall()
                            for row in seen_rows:
                                h_fail = (row['download_id'] or '').lower()
                                if not h_fail:
                                    continue
                                torrent_fail = all_torrent_by_hash.get(h_fail)
                                if torrent_fail and torrent_fail.get('state', '') in error_states:
                                    with get_db() as db:
                                        db.execute(
                                            "INSERT OR IGNORE INTO blocklist(series_id, torrent_url, torrent_name, reason)"
                                            " VALUES(?,?,?,?)",
                                            (row['series_id'], row['torrent_url'] or '', row['torrent_name'] or '',
                                             f"Download failed: {torrent_fail.get('state', 'error')}")
                                        )
                                        db.execute(
                                            "UPDATE volumes SET status='wanted', download_id=NULL, grabbed_at=NULL,"
                                            " source_url=NULL, torrent_name=NULL "
                                            "WHERE download_id=? AND status='grabbed'", (h_fail,)
                                        )
                                        db.execute("DELETE FROM seen WHERE download_id=?", (h_fail,))
                                    if (_qc or {}).get('remove_failed'):
                                        await qbit_remove(h_fail, delete_files=True)
                                    log_event('grab_failed',
                                              f"Auto-blacklisted failed download: {row['torrent_name']}",
                                              row['series_id'])
                                    # Trigger re-search unless "interactive search" mode is on
                                    if get_cfg('redownload_failed_interactive', '0') != '1':
                                        with get_db() as _rsdb:
                                            _rs = _rsdb.execute(
                                                "SELECT title, search_pattern FROM series WHERE id=?",
                                                (row['series_id'],)
                                            ).fetchone()
                                        if _rs:
                                            asyncio.create_task(grab_existing(
                                                row['series_id'], _rs['title'], _rs['search_pattern'] or ''
                                            ))
    except Exception as e:
        log_event('error', f"qBit status check failed: {e}")
        print(f"[Status/qBit] {e}")

    # ── SABnzbd ───────────────────────────────────────────────────────────────
    with get_db() as _cdb:
        _sc = get_client_for_protocol(_cdb, 'nzb')
    sab_host   = ((_sc or {}).get('host') or '').rstrip('/')
    sab_apikey = ((_sc or {}).get('password') or '')
    if sab_apikey:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # Fetch both active queue and history — matches Sonarr's GetItems() behavior.
                # An item is only truly gone when absent from BOTH endpoints.
                r_hist = await client.get(f"{sab_host}/api",
                                          params={'mode': 'history', 'limit': 100,
                                                  'apikey': sab_apikey, 'output': 'json'})
                r_queue = await client.get(f"{sab_host}/api",
                                           params={'mode': 'queue', 'limit': 100,
                                                   'apikey': sab_apikey, 'output': 'json'})

                sab_history_slots = []
                sab_queue_slots   = []
                if r_hist.status_code == 200:
                    sab_history_slots = r_hist.json().get('history', {}).get('slots', [])
                if r_queue.status_code == 200:
                    sab_queue_slots = r_queue.json().get('queue', {}).get('slots', [])

                # All nzo_ids currently visible in SABnzbd (queue + history)
                all_sab_nzo_ids: set[str] = (
                    {s['nzo_id'] for s in sab_history_slots if s.get('nzo_id')}
                    | {s['nzo_id'] for s in sab_queue_slots if s.get('nzo_id')}
                )

                # Completed jobs we can import (in history, status=Completed)
                sab_by_nzo = {
                    s['nzo_id']: s for s in sab_history_slots
                    if s.get('status') == 'Completed' and s.get('nzo_id')
                }

                with get_db() as db:
                    rows = db.execute(
                        "SELECT torrent_url, torrent_name, series_id, volume_num, download_id "
                        "FROM seen WHERE client='sabnzbd'"
                    ).fetchall()
                    for row in rows:
                        if not row['download_id']:
                            continue
                        slot = sab_by_nzo.get(row['download_id'])
                        if not slot:
                            continue
                        # SABnzbd puts completed files in 'storage'
                        content_path = slot.get('storage', '')
                        content_path = apply_remote_path_mapping(db, content_path, sab_host)
                        q_id, needs_review = _queue_import(
                            db, row['series_id'], row['download_id'],
                            row['torrent_name'] or '',
                            row['torrent_url'] or '',
                            row['volume_num'],
                            content_path)
                        if q_id and not needs_review:
                            asyncio.create_task(_process_auto_import(q_id))

                    # ── SABnzbd orphan cleanup ─────────────────────────────────────
                    # Volumes grabbed via SAB but whose job has disappeared from both
                    # SAB queue and history (deleted, expired, or failed permanently).
                    sab_orphaned = db.execute(
                        "SELECT DISTINCT v.download_id, v.series_id,"
                        " COALESCE(sv.torrent_name, v.torrent_name) as torrent_name "
                        "FROM volumes v "
                        "LEFT JOIN seen sv ON sv.download_id = v.download_id "
                        "WHERE v.status='grabbed' "
                        "  AND v.client='sabnzbd' "
                        "  AND v.download_id IS NOT NULL "
                        "  AND v.download_id NOT IN ("
                        "    SELECT download_id FROM import_queue"
                        "    WHERE status='pending' AND download_id IS NOT NULL)"
                    ).fetchall()
                    for gs in sab_orphaned:
                        if gs['download_id'] in all_sab_nzo_ids:
                            continue  # still present in SABnzbd
                        h_id = gs['download_id']
                        orphan_vol_ids = [
                            r[0] for r in db.execute(
                                "SELECT id FROM volumes WHERE series_id=? AND download_id=?"
                                " AND status='grabbed' AND volume_num IS NOT NULL",
                                (gs['series_id'], h_id)
                            ).fetchall()
                        ]
                        db.execute(
                            "DELETE FROM volumes WHERE series_id=? AND download_id=?"
                            " AND status='grabbed' AND volume_num IS NULL",
                            (gs['series_id'], h_id)
                        )
                        db.execute(
                            "UPDATE volumes SET status='wanted', download_id=NULL,"
                            " torrent_name=NULL, indexer=NULL, protocol=NULL, client=NULL,"
                            " grabbed_at=NULL, source_url=NULL, release_group=NULL "
                            "WHERE series_id=? AND download_id=? AND status='grabbed'",
                            (gs['series_id'], h_id)
                        )
                        if orphan_vol_ids:
                            _cascade_chapters(db, gs['series_id'], orphan_vol_ids, 'wanted',
                                              grabbed_at=None, torrent_name=None, torrent_url=None,
                                              indexer=None, protocol=None, client=None,
                                              download_id=None, release_group=None)
                        db.execute(
                            "UPDATE import_queue SET status='skipped' "
                            "WHERE download_id=? AND status='pending'", (h_id,)
                        )
                        db.execute(
                            "UPDATE import_queue_files SET status='skipped' "
                            "WHERE queue_id IN "
                            "(SELECT id FROM import_queue WHERE download_id=?)", (h_id,)
                        )
                        db.execute("DELETE FROM seen WHERE download_id=?", (h_id,))
                        log_event('warning',
                            f"SAB grab lost (removed from client): {gs['torrent_name']}",
                            gs['series_id'])
                        _sr = db.execute(
                            "SELECT title FROM series WHERE id=?", (gs['series_id'],)
                        ).fetchone()
                        add_history(db, 'grab_failed', gs['series_id'],
                                    _sr['title'] if _sr else '',
                                    '',
                                    source_title=gs['torrent_name'] or '',
                                    download_id=h_id,
                                    data={'reason': 'removed_from_client'})
        except Exception as e:
            log_event('error', f"SABnzbd status check failed: {e}")
            print(f"[Status/SAB] {e}")

    # ── Suwayomi ─────────────────────────────────────────────────────────────
    try:
        await _swy_router.check_suwayomi_jobs()
    except Exception as e:
        log_event('error', f"Suwayomi status check failed: {e}")
        print(f"[Status/Suwayomi] {e}")

def _mark_downloaded(db, series_id, volume_num, torrent_url) -> bool:
    """Mark volume(s) as downloaded. Returns True if any rows were updated."""
    if volume_num is not None:
        # Single volume stub
        cur = db.execute(
            "UPDATE volumes SET status='downloaded' WHERE series_id=? AND volume_num=? AND status='grabbed'",
            (series_id, volume_num)
        )
        if cur.rowcount > 0:
            log_event('download_complete', f"Vol {volume_num:g} download complete", series_id)
            s = db.execute("SELECT title, cover_url FROM series WHERE id=?", (series_id,)).fetchone()
            if s:
                asyncio.create_task(notify_discord(
                    '',
                    embed=make_complete_embed(s['title'], f"Vol {volume_num:g}", s['cover_url'] or ''),
                    event='on_download'
                ))
            # Cascade chapters to downloaded
            vol_row = db.execute(
                "SELECT id FROM volumes WHERE series_id=? AND volume_num=?",
                (series_id, volume_num)
            ).fetchone()
            if vol_row:
                _cascade_chapters(db, series_id, [vol_row['id']], 'downloaded')
            return True
    else:
        # Pack entry — find the pack row and cascade to covered volume stubs
        pack = db.execute(
            "SELECT * FROM volumes WHERE series_id=? AND source_url=? AND volume_num IS NULL",
            (series_id, torrent_url)
        ).fetchone()
        if not pack:
            return False

        pt = pack['pack_type']
        # Pull source metadata from seen to stamp onto covered stubs
        seen_meta = db.execute(
            "SELECT torrent_name, indexer, protocol, client, release_group, size_bytes"
            " FROM seen WHERE (download_id=? AND download_id IS NOT NULL)"
            " OR torrent_url=? LIMIT 1",
            (pack['download_id'], torrent_url)
        ).fetchone()
        m = dict(seen_meta) if seen_meta else {}

        if pt == 'complete':
            cur = db.execute(
                "UPDATE volumes SET status='downloaded',"
                " torrent_name=?, indexer=?, protocol=?, client=?, release_group=?, size_bytes=?"
                " WHERE series_id=? AND volume_num IS NOT NULL AND status != 'downloaded'",
                (m.get('torrent_name'), m.get('indexer'), m.get('protocol'),
                 m.get('client'), m.get('release_group'), m.get('size_bytes'), series_id)
            )
        elif pt == 'volume' and pack['vol_range_start'] is not None and pack['vol_range_end'] is not None:
            cur = db.execute(
                "UPDATE volumes SET status='downloaded',"
                " torrent_name=?, indexer=?, protocol=?, client=?, release_group=?, size_bytes=?"
                " WHERE series_id=? AND volume_num IS NOT NULL AND status != 'downloaded'"
                " AND volume_num >= ? AND volume_num <= ?",
                (m.get('torrent_name'), m.get('indexer'), m.get('protocol'),
                 m.get('client'), m.get('release_group'), m.get('size_bytes'),
                 series_id, pack['vol_range_start'], pack['vol_range_end'])
            )
        else:
            return False

        if cur.rowcount > 0:
            label = 'Complete Series' if pt == 'complete' else f"Vol {int(pack['vol_range_start'])}–{int(pack['vol_range_end'])}"
            log_event('download_complete', f"{label} pack download complete", series_id)
            s = db.execute("SELECT title, cover_url FROM series WHERE id=?", (series_id,)).fetchone()
            if s:
                asyncio.create_task(notify_discord(
                    '',
                    embed=make_complete_embed(s['title'], label, s['cover_url'] or ''),
                    event='on_download'
                ))
            # Cascade chapters
            if pt == 'complete':
                _cascade_chapters(db, series_id, None, 'downloaded')
            elif pt == 'volume':
                rng_ids = [
                    r['id'] for r in db.execute(
                        "SELECT id FROM volumes WHERE series_id=? AND volume_num IS NOT NULL"
                        " AND volume_num >= ? AND volume_num <= ?",
                        (series_id, pack['vol_range_start'], pack['vol_range_end'])
                    ).fetchall()
                ]
                _cascade_chapters(db, series_id, rng_ids, 'downloaded')
            return True
    return False

async def _execute_import(
    queue_id: int,
    volume_overrides: dict | None = None,
    skip_ids: set | None = None,
    chapter_overrides: dict | None = None,
) -> bool:
    """
    Shared import executor: copy/hardlink/move files for a pending queue item,
    then mark the corresponding volumes/chapters as downloaded.
    volume_overrides:  {file_id: new_volume_num} — user corrections
    chapter_overrides: {file_id: new_chapter_num} — user corrections for chapter files
    skip_ids:          set of file_ids to skip
    Returns True on full success.
    """
    if volume_overrides is None:
        volume_overrides = {}
    if chapter_overrides is None:
        chapter_overrides = {}
    if skip_ids is None:
        skip_ids = set()

    import_mode = get_cfg('import_mode', 'hardlink')
    any_error   = False

    with get_db() as db:
        queue = db.execute("SELECT * FROM import_queue WHERE id=?", (queue_id,)).fetchone()
        if not queue or queue['status'] not in ('pending', 'partial'):
            return False

        # For partial entries, process both pending and needs_review files;
        # for fresh pending entries, process all pending files.
        files = db.execute(
            "SELECT * FROM import_queue_files WHERE queue_id=? AND status IN ('pending', 'needs_review')",
            (queue_id,)
        ).fetchall()

        s = db.execute(
            "SELECT * FROM series WHERE id=?", (queue['series_id'],)
        ).fetchone()
        _series_tags = [r['tag'] for r in db.execute(
            "SELECT tag FROM series_tags WHERE series_id=?", (queue['series_id'],)
        ).fetchall()]
        rf = db.execute(
            "SELECT path FROM root_folders WHERE id=?", (s['root_folder_id'],)
        ).fetchone() if s and s['root_folder_id'] else None
        dest_root = rf['path'] if rf else get_cfg('save_path', '/manga')
        safe_dir  = sanitize_filename(s['title'] or 'Unknown') if s else 'Unknown'
        dst_dir   = os.path.join(dest_root, safe_dir)

        try:
            os.makedirs(dst_dir, exist_ok=True)
        except Exception as e:
            log_event('error', f"Import: cannot create {dst_dir}: {e}", queue['series_id'])
            db.execute("UPDATE import_queue SET status='failed' WHERE id=?", (queue_id,))
            # Reset grabbed volumes back to wanted when import conclusively fails
            if queue['download_id']:
                db.execute(
                    "UPDATE volumes SET status='wanted', grabbed_at=NULL, download_id=NULL,"
                    " source_url=NULL, torrent_name=NULL, indexer=NULL, protocol=NULL,"
                    " client=NULL, release_group=NULL, import_path=NULL"
                    " WHERE download_id=? AND status='grabbed'",
                    (queue['download_id'],)
                )
            return False

        now_ts = datetime.utcnow().isoformat()
        imported_count = 0
        imported_vols: set[float] = set()
        # Track volumes that gained new chapter imports so we can cascade completion
        chapter_vols_touched: set[int] = set()

        for f in files:
            if f['id'] in skip_ids:
                db.execute(
                    "UPDATE import_queue_files SET status='skipped' WHERE id=?", (f['id'],)
                )
                continue

            new_vol  = volume_overrides.get(f['id'])
            new_chap = chapter_overrides.get(f['id'])
            if new_vol is not None:
                db.execute(
                    "UPDATE import_queue_files SET proposed_volume=? WHERE id=?",
                    (new_vol, f['id'])
                )
            if new_chap is not None:
                db.execute(
                    "UPDATE import_queue_files SET proposed_chapter=?, file_type='chapter' WHERE id=?",
                    (new_chap, f['id'])
                )

            proposed_vol  = new_vol  if new_vol  is not None else f['proposed_volume']
            proposed_chap = new_chap if new_chap is not None else (
                f['proposed_chapter'] if 'proposed_chapter' in f.keys() else None
            )
            file_type = (
                'chapter' if new_chap is not None
                else (f['file_type'] if 'file_type' in f.keys() else 'volume')
            )

            # ── Chapter file: has a chapter number ────────────────────────────
            if file_type == 'chapter' and proposed_chap is not None:
                src = f['src_path']
                try:
                    dst = safe_join_under(dst_dir, f['filename'])
                except ValueError as _e:
                    db.execute(
                        "UPDATE import_queue_files SET status='failed' WHERE id=?", (f['id'],)
                    )
                    log_event('error', f"Import: unsafe destination ({f['filename']}): {_e}", queue['series_id'])
                    any_error = True
                    continue

                if not os.path.isfile(src):
                    db.execute(
                        "UPDATE import_queue_files SET status='failed' WHERE id=?", (f['id'],)
                    )
                    log_event('error', f"Import: source file missing: {src}", queue['series_id'])
                    any_error = True
                    continue

                try:
                    if import_mode == 'hardlink':
                        if os.path.exists(dst):
                            os.remove(dst)
                        os.link(src, dst)
                    elif import_mode == 'move':
                        shutil.move(src, dst)
                    else:
                        shutil.copy2(src, dst)

                    dst = _maybe_convert_to_cbz(dst)   # CBR→CBZ if needed
                    if s:
                        _try_inject_comicinfo(dst, s, chapter_num=proposed_chap, tags=_series_tags)

                    db.execute(
                        "UPDATE import_queue_files SET status='imported', dst_path=? WHERE id=?",
                        (dst, f['id'])
                    )
                    imported_count += 1

                    # Resolve or create the parent volume record
                    vol_id = None
                    if proposed_vol is not None:
                        vol_row = db.execute(
                            "SELECT id FROM volumes WHERE series_id=? AND volume_num=?",
                            (queue['series_id'], proposed_vol)
                        ).fetchone()
                        if vol_row:
                            vol_id = vol_row['id']
                        else:
                            cur2 = db.execute(
                                "INSERT INTO volumes(series_id, volume_num, status)"
                                " VALUES(?,?,'wanted')",
                                (queue['series_id'], proposed_vol)
                            )
                            vol_id = cur2.lastrowid

                    # Pull parent-volume metadata (if linked) to stamp onto chapter
                    # rows — keeps chapters in sync with the grab that produced them.
                    _pv_meta = {}
                    if vol_id is not None:
                        _pv_row = db.execute(
                            "SELECT indexer, protocol, client, release_group, size_bytes,"
                            " torrent_name FROM volumes WHERE id=?",
                            (vol_id,)
                        ).fetchone()
                        if _pv_row:
                            _pv_meta = dict(_pv_row)
                    _ch_quality = quality_from_filename(dst)
                    _ch_torrent_name = _pv_meta.get('torrent_name') or queue['torrent_name']

                    # Upsert the chapter record with full metadata
                    chap_row = db.execute(
                        "SELECT id FROM chapters WHERE series_id=? AND chapter_num=?",
                        (queue['series_id'], proposed_chap)
                    ).fetchone()
                    if chap_row:
                        db.execute(
                            "UPDATE chapters SET status='downloaded', import_path=?,"
                            " quality=COALESCE(quality,?), imported_at=COALESCE(imported_at,?),"
                            " torrent_name=COALESCE(torrent_name,?),"
                            " indexer=COALESCE(indexer,?), protocol=COALESCE(protocol,?),"
                            " client=COALESCE(client,?), release_group=COALESCE(release_group,?),"
                            " size_bytes=COALESCE(NULLIF(size_bytes,0),?),"
                            " volume_id=COALESCE(volume_id,?), download_id=COALESCE(download_id,?)"
                            " WHERE id=?",
                            (dst, _ch_quality, now_ts, _ch_torrent_name,
                             _pv_meta.get('indexer'), _pv_meta.get('protocol'),
                             _pv_meta.get('client'), _pv_meta.get('release_group'),
                             _pv_meta.get('size_bytes'),
                             vol_id, queue['download_id'], chap_row['id'])
                        )
                    else:
                        db.execute(
                            "INSERT INTO chapters(series_id, volume_id, chapter_num, status,"
                            " import_path, download_id, torrent_name, indexer, protocol, client,"
                            " release_group, size_bytes, quality, imported_at)"
                            " VALUES(?,?,?,'downloaded',?,?,?,?,?,?,?,?,?,?)",
                            (queue['series_id'], vol_id, proposed_chap, dst,
                             queue['download_id'], _ch_torrent_name,
                             _pv_meta.get('indexer'), _pv_meta.get('protocol'),
                             _pv_meta.get('client'), _pv_meta.get('release_group'),
                             _pv_meta.get('size_bytes'), _ch_quality, now_ts)
                        )

                    if vol_id is not None:
                        chapter_vols_touched.add(vol_id)
                    if proposed_vol is not None:
                        imported_vols.add(proposed_vol)

                except Exception as e:
                    db.execute(
                        "UPDATE import_queue_files SET status='failed' WHERE id=?", (f['id'],)
                    )
                    log_event('error', f"Import chapter error ({f['filename']}): {e}", queue['series_id'])
                    any_error = True
                continue  # chapter file handled — skip volume logic below

            # ── Volume file: needs a volume number ────────────────────────────

            # Fallback: re-run chapter detection on the filename. Handles queue
            # entries that were created by older code before chapter detection
            # was added (file_type='volume', proposed_chapter=NULL).
            if proposed_vol is None and proposed_chap is None and f['id'] not in volume_overrides:
                recheck_chap = extract_chapter_num(os.path.basename(f['src_path']))
                if recheck_chap is not None:
                    proposed_chap = recheck_chap
                    file_type = 'chapter'
                    db.execute(
                        "UPDATE import_queue_files SET proposed_chapter=?, file_type='chapter' WHERE id=?",
                        (recheck_chap, f['id'])
                    )
                    # Re-enter chapter handling path
                    src = f['src_path']
                    try:
                        dst = safe_join_under(dst_dir, f['filename'])
                    except ValueError as _e:
                        db.execute("UPDATE import_queue_files SET status='failed' WHERE id=?", (f['id'],))
                        log_event('error', f"Import: unsafe destination ({f['filename']}): {_e}", queue['series_id'])
                        any_error = True
                        continue
                    if not os.path.isfile(src):
                        db.execute("UPDATE import_queue_files SET status='failed' WHERE id=?", (f['id'],))
                        log_event('error', f"Import: source file missing: {src}", queue['series_id'])
                        any_error = True
                        continue
                    try:
                        if import_mode == 'hardlink':
                            if os.path.exists(dst): os.remove(dst)
                            os.link(src, dst)
                        elif import_mode == 'move':
                            shutil.move(src, dst)
                        else:
                            shutil.copy2(src, dst)
                        dst = _maybe_convert_to_cbz(dst)   # CBR→CBZ if needed
                        if s:
                            _try_inject_comicinfo(dst, s, chapter_num=recheck_chap, tags=_series_tags)
                        db.execute("UPDATE import_queue_files SET status='imported', dst_path=? WHERE id=?", (dst, f['id']))
                        imported_count += 1
                        _ch_quality2 = quality_from_filename(dst)
                        chap_row = db.execute(
                            "SELECT id, volume_id FROM chapters WHERE series_id=? AND chapter_num=?",
                            (queue['series_id'], recheck_chap)
                        ).fetchone()
                        # Pull parent-volume metadata if the chapter is linked
                        _pv_meta2 = {}
                        _pv_vol_id = chap_row['volume_id'] if chap_row else None
                        if _pv_vol_id is not None:
                            _pv_row2 = db.execute(
                                "SELECT indexer, protocol, client, release_group, size_bytes,"
                                " torrent_name FROM volumes WHERE id=?",
                                (_pv_vol_id,)
                            ).fetchone()
                            if _pv_row2:
                                _pv_meta2 = dict(_pv_row2)
                        if chap_row:
                            db.execute(
                                "UPDATE chapters SET status='downloaded', import_path=?,"
                                " quality=COALESCE(quality,?), imported_at=COALESCE(imported_at,?),"
                                " torrent_name=COALESCE(torrent_name,?),"
                                " indexer=COALESCE(indexer,?), protocol=COALESCE(protocol,?),"
                                " client=COALESCE(client,?), release_group=COALESCE(release_group,?),"
                                " size_bytes=COALESCE(NULLIF(size_bytes,0),?),"
                                " download_id=COALESCE(download_id,?)"
                                " WHERE id=?",
                                (dst, _ch_quality2, now_ts,
                                 _pv_meta2.get('torrent_name') or queue['torrent_name'],
                                 _pv_meta2.get('indexer'), _pv_meta2.get('protocol'),
                                 _pv_meta2.get('client'), _pv_meta2.get('release_group'),
                                 _pv_meta2.get('size_bytes'),
                                 queue['download_id'], chap_row['id'])
                            )
                        else:
                            db.execute(
                                "INSERT INTO chapters(series_id, chapter_num, status,"
                                " import_path, download_id, torrent_name, quality, imported_at)"
                                " VALUES(?,?,'downloaded',?,?,?,?,?)",
                                (queue['series_id'], recheck_chap, dst,
                                 queue['download_id'], queue['torrent_name'],
                                 _ch_quality2, now_ts)
                            )
                        if proposed_vol is not None:
                            imported_vols.add(proposed_vol)
                    except Exception as e:
                        db.execute("UPDATE import_queue_files SET status='failed' WHERE id=?", (f['id'],))
                        log_event('error', f"Import chapter error ({f['filename']}): {e}", queue['series_id'])
                        any_error = True
                    continue

            # For legacy chapter-mode grabs the file has no volume number — allow through.
            _ch_stub = None
            if proposed_vol is None and f['id'] not in volume_overrides:
                if queue['download_id']:
                    _ch_stub = db.execute(
                        "SELECT id FROM volumes WHERE series_id=? AND download_id=?"
                        " AND status='grabbed' AND pack_type='chapter'",
                        (queue['series_id'], queue['download_id'])
                    ).fetchone()
                if not _ch_stub:
                    db.execute(
                        "UPDATE import_queue_files SET status='needs_review' WHERE id=?", (f['id'],)
                    )
                    continue

            src = f['src_path']
            try:
                dst = safe_join_under(dst_dir, f['filename'])
            except ValueError as _e:
                db.execute("UPDATE import_queue_files SET status='failed' WHERE id=?", (f['id'],))
                log_event('error', f"Import: unsafe destination ({f['filename']}): {_e}", queue['series_id'])
                any_error = True
                continue

            if not os.path.isfile(src):
                db.execute(
                    "UPDATE import_queue_files SET status='failed' WHERE id=?", (f['id'],)
                )
                log_event('error', f"Import: source file missing: {src}", queue['series_id'])
                any_error = True
                continue

            try:
                if import_mode == 'hardlink':
                    if os.path.exists(dst):
                        os.remove(dst)
                    os.link(src, dst)
                elif import_mode == 'move':
                    shutil.move(src, dst)
                else:
                    shutil.copy2(src, dst)
                dst = _maybe_convert_to_cbz(dst)   # CBR→CBZ if needed
                if s:
                    _try_inject_comicinfo(dst, s, volume_num=proposed_vol, tags=_series_tags)
                db.execute(
                    "UPDATE import_queue_files SET status='imported', dst_path=? WHERE id=?",
                    (dst, f['id'])
                )
                imported_count += 1
                if proposed_vol is not None:
                    imported_vols.add(proposed_vol)
                elif _ch_stub:
                    # Legacy chapter-mode grab — mark the stub downloaded
                    db.execute(
                        "UPDATE volumes SET status='downloaded', import_path=?,"
                        " quality=COALESCE(quality,?), imported_at=? WHERE id=?",
                        (dst, quality_from_filename(dst), now_ts, _ch_stub['id'])
                    )

                # Stamp full source metadata on the volume stub now that the file is confirmed
                if proposed_vol is not None:
                    seen_row = db.execute(
                        "SELECT torrent_name, indexer, protocol, client, release_group, size_bytes"
                        " FROM seen WHERE (download_id=? AND download_id IS NOT NULL)"
                        " OR torrent_url=? LIMIT 1",
                        (queue['download_id'], queue['torrent_url'])
                    ).fetchone()
                    meta = dict(seen_row) if seen_row else {}

                    vol_row = db.execute(
                        "SELECT id FROM volumes WHERE series_id=? AND volume_num=?",
                        (queue['series_id'], proposed_vol)
                    ).fetchone()
                    file_quality = quality_from_filename(f['filename'])
                    if vol_row:
                        db.execute(
                            "UPDATE volumes SET status='downloaded', import_path=?,"
                            " torrent_name=?, indexer=?, protocol=?, client=?,"
                            " release_group=?, size_bytes=?, quality=?, imported_at=?,"
                            " download_id=COALESCE(download_id,?) WHERE id=?",
                            (dst,
                             meta.get('torrent_name'), meta.get('indexer'),
                             meta.get('protocol'), meta.get('client'),
                             meta.get('release_group'), meta.get('size_bytes'),
                             file_quality, now_ts, queue['download_id'], vol_row['id'])
                        )
                        _check_volume_completion(db, queue['series_id'], vol_row['id'])
                        # Whole-volume file satisfies all chapters in this volume.
                        # Cascade FULL metadata from the volume we just imported so
                        # chapter rows don't end up with NULL fields.
                        _cascade_chapters(db, queue['series_id'], [vol_row['id']],
                                          'downloaded', import_path=dst,
                                          download_id=queue['download_id'],
                                          quality=file_quality, imported_at=now_ts,
                                          torrent_name=meta.get('torrent_name'),
                                          indexer=meta.get('indexer'),
                                          protocol=meta.get('protocol'),
                                          client=meta.get('client'),
                                          release_group=meta.get('release_group'),
                                          size_bytes=meta.get('size_bytes'))
                    else:
                        # Stub doesn't exist yet — create it with full metadata
                        cur_ins = db.execute(
                            "INSERT INTO volumes(series_id, volume_num, status, source_url,"
                            " torrent_name, import_path, download_id, indexer, protocol,"
                            " client, release_group, size_bytes, quality, imported_at)"
                            " VALUES(?,?,'downloaded',?,?,?,?,?,?,?,?,?,?,?)",
                            (queue['series_id'], proposed_vol,
                             queue['torrent_url'], meta.get('torrent_name'),
                             dst, queue['download_id'],
                             meta.get('indexer'), meta.get('protocol'),
                             meta.get('client'), meta.get('release_group'),
                             meta.get('size_bytes'), file_quality, now_ts)
                        )
                        _cascade_chapters(db, queue['series_id'], [cur_ins.lastrowid],
                                          'downloaded', import_path=dst,
                                          download_id=queue['download_id'],
                                          quality=file_quality, imported_at=now_ts,
                                          torrent_name=meta.get('torrent_name'),
                                          indexer=meta.get('indexer'),
                                          protocol=meta.get('protocol'),
                                          client=meta.get('client'),
                                          release_group=meta.get('release_group'),
                                          size_bytes=meta.get('size_bytes'))

            except Exception as e:
                db.execute(
                    "UPDATE import_queue_files SET status='failed' WHERE id=?", (f['id'],)
                )
                log_event('error', f"Import file error ({f['filename']}): {e}", queue['series_id'])
                any_error = True

        # ── After all files: cascade chapter completion to volumes ────────────
        for vol_id in chapter_vols_touched:
            total_chaps = db.execute(
                "SELECT COUNT(*) FROM chapters WHERE volume_id=? AND monitored=1",
                (vol_id,)
            ).fetchone()[0]
            done_chaps = db.execute(
                "SELECT COUNT(*) FROM chapters WHERE volume_id=? AND monitored=1 AND status='downloaded'",
                (vol_id,)
            ).fetchone()[0]
            if total_chaps > 0 and done_chaps >= total_chaps:
                db.execute(
                    "UPDATE volumes SET status='downloaded', imported_at=COALESCE(imported_at,?)"
                    " WHERE id=? AND status!='downloaded'",
                    (now_ts, vol_id)
                )

        # Determine final queue status
        has_needs_review = db.execute(
            "SELECT 1 FROM import_queue_files WHERE queue_id=? AND status='needs_review'",
            (queue_id,)
        ).fetchone()

        if imported_count == 0 and any_error:
            new_status = 'failed'
        elif has_needs_review:
            new_status = 'partial'   # some files imported, some need review
        elif any_error:
            new_status = 'partial'
        else:
            new_status = 'imported'

        db.execute("UPDATE import_queue SET status=? WHERE id=?", (new_status, queue_id))
        # Reset grabbed volumes back to wanted when import conclusively fails
        if new_status == 'failed' and queue['download_id']:
            db.execute(
                "UPDATE volumes SET status='wanted', grabbed_at=NULL, download_id=NULL,"
                " source_url=NULL, torrent_name=NULL, indexer=NULL, protocol=NULL,"
                " client=NULL, release_group=NULL, import_path=NULL"
                " WHERE download_id=? AND status='grabbed'",
                (queue['download_id'],)
            )
        # Clean up fully imported records (keep failed/partial for user review)
        if new_status == 'imported':
            db.execute("DELETE FROM import_queue_files WHERE queue_id=?", (queue_id,))
            db.execute("DELETE FROM import_queue WHERE id=?", (queue_id,))

        s_info = db.execute(
            "SELECT title FROM series WHERE id=?", (queue['series_id'],)
        ).fetchone()
        s_title = s_info['title'] if s_info else ''
        vol_label = build_volume_label(queue['volume_num'], None, None)

        if imported_count > 0:
            # Mark the pack/volume entry downloaded and cascade to any remaining stubs
            _mark_downloaded(db, queue['series_id'], queue['volume_num'], queue['torrent_url'])
            # If the user reassigned volume numbers in the review form, the originally
            # grabbed stub (queue['volume_num']) may not have been imported. Reset it
            # so it shows as wanted rather than incorrectly downloaded.
            if (queue['volume_num'] is not None
                    and imported_vols
                    and queue['volume_num'] not in imported_vols):
                db.execute(
                    "UPDATE volumes SET status='wanted', download_id=NULL, grabbed_at=NULL,"
                    " source_url=NULL, torrent_name=NULL, indexer=NULL, protocol=NULL,"
                    " client=NULL, release_group=NULL "
                    "WHERE series_id=? AND volume_num=? AND status='grabbed'",
                    (queue['series_id'], queue['volume_num'])
                )
            # Set import_path on the pack entry itself (directory level)
            db.execute(
                "UPDATE volumes SET import_path=? WHERE series_id=? AND download_id=?"
                " AND volume_num IS NULL",
                (dst_dir, queue['series_id'], queue['download_id'])
            )
            log_event('import', f"Imported {imported_count} file(s): {queue['torrent_name']}", queue['series_id'])
            add_history(db, 'imported', queue['series_id'], s_title, vol_label,
                        source_title=queue['torrent_name'] or '',
                        download_id=queue['download_id'] or '',
                        data={'dst_dir': dst_dir, 'count': imported_count})
        else:
            log_event('error', f"Import failed: {queue['torrent_name']}", queue['series_id'])
            add_history(db, 'import_failed', queue['series_id'], s_title, vol_label,
                        source_title=queue['torrent_name'] or '',
                        download_id=queue['download_id'] or '')

    if not any_error:
        # Extract CBZ cover for any newly imported CBZ files
        with get_db() as _cdb:
            _series_id_for_cover = queue['series_id']
            _cover_url_for_series = _cdb.execute(
                "SELECT cover_url FROM series WHERE id=?", (_series_id_for_cover,)
            ).fetchone()
        _local_cover = f"/config/covers/{_series_id_for_cover}.jpg"
        if not os.path.exists(_local_cover):
            # Try to get cover from a CBZ we just imported
            with get_db() as _cdb2:
                _first_cbz = _cdb2.execute(
                    "SELECT dst_path FROM import_queue_files"
                    " WHERE queue_id=? AND status='imported' AND dst_path LIKE '%.cbz'",
                    (queue_id,)
                ).fetchone()
            if _first_cbz and _first_cbz['dst_path']:
                extract_cbz_cover(_series_id_for_cover, _first_cbz['dst_path'])
            elif _cover_url_for_series and _cover_url_for_series['cover_url']:
                asyncio.create_task(download_cover(_series_id_for_cover, _cover_url_for_series['cover_url']))
        await trigger_komga_scan()
        # Remove from download client after successful import (like Sonarr's "Remove Completed")
        if get_cfg('remove_completed', 'false').lower() == 'true' and queue['download_id']:
            with get_db() as db2:
                proto = db2.execute(
                    "SELECT protocol FROM volumes WHERE download_id=? LIMIT 1",
                    (queue['download_id'],)
                ).fetchone()
            protocol = (proto['protocol'] if proto else '') or 'torrent'
            if protocol == 'torrent':
                await qbit_remove(queue['download_id'])
            else:
                await sab_remove(queue['download_id'])
    asyncio.create_task(broadcast_queue_event('import_complete', {'queue_id': queue_id}))
    return not any_error


async def _process_auto_import(queue_id: int):
    """Auto-import a queue item where all files mapped cleanly (no review needed).
    On unhandled exception, mark the queue as 'failed' so it doesn't stick in
    pending/partial forever and get retried on every startup."""
    try:
        await _execute_import(queue_id)
    except Exception as e:
        import traceback
        log_event('error', f"Auto-import failed for queue {queue_id}: {e}")
        print(f"[AutoImport] {e}\n{traceback.format_exc()}")
        try:
            with get_db() as _db_err:
                _db_err.execute(
                    "UPDATE import_queue SET status='failed' WHERE id=? AND status IN ('pending','partial')",
                    (queue_id,)
                )
        except Exception as _db_e:
            print(f"[AutoImport] failed to mark queue {queue_id} as failed: {_db_e}")


# ── Source helpers ────────────────────────────────────────────────────────────
def mu_slug_to_id(slug: str) -> str:
    """Convert MangaUpdates URL slug (base36) to numeric ID string."""
    try:
        return str(int(slug, 36))
    except (ValueError, TypeError):
        return slug

def mu_id_to_slug(numeric_id) -> str:
    """Convert MangaUpdates numeric ID to URL slug (base36)."""
    try:
        digits = '0123456789abcdefghijklmnopqrstuvwxyz'
        n = int(numeric_id)
        result = ''
        while n:
            result = digits[n % 36] + result
            n //= 36
        return result or '0'
    except (ValueError, TypeError):
        return str(numeric_id)

def _norm_status(s: str) -> str:
    """Normalize status strings from various sources to AniList-style enum."""
    if not s:
        return ''
    sl = s.lower()
    if 'complete' in sl or 'finished' in sl:
        return 'FINISHED'
    if 'ongoing' in sl or 'releasing' in sl or 'publishing' in sl:
        return 'RELEASING'
    if 'hiatus' in sl:
        return 'HIATUS'
    if 'cancelled' in sl or 'canceled' in sl:
        return 'CANCELLED'
    return s.upper()

# ── AniList ───────────────────────────────────────────────────────────────────
ANILIST_QUERY = """
query ($search: String) {
  Page(perPage: 12) {
    media(search: $search, type: MANGA, sort: SEARCH_MATCH) {
      id
      idMal
      title { romaji english }
      coverImage { large }
      status
      format
      description(asHtml: false)
      volumes
      chapters
      startDate { year }
    }
  }
}
"""

ANILIST_ALIASES_QUERY = """
query ($id: Int) {
  Media(id: $id, type: MANGA) {
    title { romaji english }
    synonyms
    genres
  }
}
"""

async def fetch_anilist_aliases(series_id: int, anilist_id: int, main_title: str):
    """Fetch romaji title + synonyms from AniList and store as series aliases."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                'https://graphql.anilist.co',
                json={'query': ANILIST_ALIASES_QUERY, 'variables': {'id': anilist_id}},
                headers={'Content-Type': 'application/json'}
            )
        data = r.json().get('data', {}).get('Media', {})
    except Exception as e:
        print(f"[AniList] alias fetch error: {e}")
        return

    candidates = []
    title_block = data.get('title', {})
    # Always include the romaji title — critical for Nyaa which uses Japanese romanizations
    if title_block.get('romaji'):
        candidates.append(title_block['romaji'])
    candidates.extend(data.get('synonyms') or [])

    def _is_useful(alias: str) -> bool:
        if not alias or len(alias) < 4:
            return False
        if normalize(alias) == normalize(main_title):
            return False
        # Require at least 40% Latin alphabet characters — filters Arabic, Thai, Cyrillic, CJK, etc.
        latin = len(re.findall(r'[a-zA-Z]', alias))
        if latin < max(1, len(alias.replace(' ', '')) * 0.4):
            return False
        return True

    genres = data.get('genres') or []
    with get_db() as db:
        for alias in candidates:
            if _is_useful(alias):
                db.execute(
                    "INSERT OR IGNORE INTO series_aliases(series_id, alias) VALUES(?,?)",
                    (series_id, alias.strip())
                )
        for genre in genres[:8]:
            g = genre.strip().lower()
            if g:
                db.execute(
                    "INSERT OR IGNORE INTO series_tags(series_id, tag) VALUES(?,?)",
                    (series_id, g)
                )
    print(f"[AniList] aliases populated for series {series_id}: {[a for a in candidates if _is_useful(a)]}")
    if genres:
        print(f"[AniList] genres tagged for series {series_id}: {genres[:8]}")

async def anilist_search(query: str) -> list[dict]:
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(
                    'https://graphql.anilist.co',
                    json={'query': ANILIST_QUERY, 'variables': {'search': query}},
                    headers={'Content-Type': 'application/json'}
                )
            if r.status_code == 429:
                retry_after = int(r.headers.get('Retry-After', '60'))
                print(f"[AniList] Rate limited — waiting {retry_after}s (attempt {attempt+1}/3)")
                await asyncio.sleep(min(retry_after, 120))
                continue
            data = r.json()
            break
        except Exception as e:
            print(f"[AniList] error (attempt {attempt+1}/3): {e}")
            if attempt < 2:
                await asyncio.sleep(5 * (attempt + 1))
            else:
                return []
    else:
        return []
    results = []
    for m in data.get('data', {}).get('Page', {}).get('media', []):
        title = m['title'].get('english') or m['title'].get('romaji', '')
        desc  = re.sub(r'<[^>]+>', '', (m.get('description') or ''))[:300].strip()
        results.append({
            'anilist_id':  m['id'],
            'mal_id':      m.get('idMal'),
            'mu_id':       None,
            'title':       title,
            'cover_url':   m['coverImage']['large'],
            'status':      m.get('status', ''),
            'format':      m.get('format', ''),
            'volumes':     m.get('volumes'),
            'chapters':    m.get('chapters'),
            'pub_year':    (m.get('startDate') or {}).get('year'),
            'description': desc,
            'source':      'anilist',
        })
    return results

# ── MangaUpdates ──────────────────────────────────────────────────────────────
async def mu_search(query: str) -> list[dict]:
    """Search MangaUpdates — used as fallback when AniList has no results."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                'https://api.mangaupdates.com/v1/series/search',
                json={'search': query, 'per_page': 12},
                headers={'Content-Type': 'application/json'},
            )
        data = r.json()
    except Exception as e:
        print(f"[MangaUpdates] search error: {e}")
        return []
    results = []
    for item in data.get('results', []):
        rec = item.get('record', {})
        mu_num = str(rec.get('series_id', ''))
        # Parse total volumes from status string e.g. "34 Volumes (Complete)"
        status_str = rec.get('status') or ''
        vol_match  = re.search(r'(\d+)\s+[Vv]olume', status_str)
        volumes    = int(vol_match.group(1)) if vol_match else None
        latest_ch  = rec.get('latest_chapter')
        # Cover image
        img        = rec.get('image') or {}
        cover      = (img.get('url') or {}).get('original') or ''
        desc       = re.sub(r'<[^>]+>', '', (rec.get('description') or ''))[:300].strip()
        results.append({
            'anilist_id':  None,
            'mal_id':      None,
            'mu_id':       mu_num,
            'title':       rec.get('title', ''),
            'cover_url':   cover,
            'status':      _norm_status(status_str),
            'volumes':     volumes,
            'chapters':    int(latest_ch) if latest_ch else None,
            'description': desc,
            'source':      'mangaupdates',
        })
    return results

async def search_series(query: str) -> tuple[list[dict], str]:
    """
    Search across sources. Returns (results, source_used).
    Handles AniList URLs/IDs directly, then AniList text search, then MangaUpdates fallback.
    """
    q = query.strip()

    # AniList URL: https://anilist.co/manga/123/... → extract numeric ID
    _al_url = re.search(r'anilist\.co/(?:manga|anime)/(\d+)', q)
    if _al_url:
        q = _al_url.group(1)

    # Bare numeric ID → look up AniList by ID directly
    if q.isdigit():
        _id_gql = 'query($id:Int){Media(id:$id,type:MANGA){id idMal title{english romaji} coverImage{large} status volumes chapters startDate{year} description genres}}'
        try:
            async with httpx.AsyncClient(timeout=15) as _id_cli:
                _r = await _id_cli.post(
                    'https://graphql.anilist.co',
                    json={'query': _id_gql, 'variables': {'id': int(q)}},
                    headers={'Content-Type': 'application/json'},
                )
            _m = (_r.json().get('data') or {}).get('Media')
            if _m:
                _title = (_m.get('title') or {}).get('english') or (_m.get('title') or {}).get('romaji', '')
                _desc  = re.sub(r'<[^>]+>', '', (_m.get('description') or ''))[:300].strip()
                return [{
                    'anilist_id':  _m['id'],
                    'mal_id':      _m.get('idMal'),
                    'mu_id':       None,
                    'title':       _title,
                    'cover_url':   (_m.get('coverImage') or {}).get('large', ''),
                    'status':      _m.get('status', ''),
                    'volumes':     _m.get('volumes'),
                    'chapters':    _m.get('chapters'),
                    'pub_year':    ((_m.get('startDate') or {}).get('year')),
                    'description': _desc,
                    'source':      'anilist',
                }], 'anilist'
        except Exception:
            pass  # fall through to text search

    results = await anilist_search(q)
    if results:
        return results, 'anilist'
    results = await mu_search(q)
    return results, 'mangaupdates'

# ── MangaDex chapter→volume mapping ──────────────────────────────────────────
async def fetch_mangadex_id(title: str, anilist_id: int | None,
                            mu_id: str | None = None) -> tuple[str | None, dict]:
    """
    Find MangaDex manga UUID by matching AniList or MangaUpdates ID in external links.
    Returns (mangadex_uuid, links_dict) where links_dict has al/mal/mu/kt keys.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                'https://api.mangadex.org/manga',
                params={
                    'title': title,
                    'limit': 15,
                    'order[relevance]': 'desc',
                    'contentRating[]': ['safe', 'suggestive', 'erotica'],
                }
            )
        data = r.json()
        best_id, best_links = None, {}
        for manga in data.get('data', []):
            links = manga.get('attributes', {}).get('links', {}) or {}
            # Match by AniList ID (most reliable)
            if anilist_id and str(links.get('al', '')) == str(anilist_id):
                return manga['id'], links
            # Match by MangaUpdates slug (convert our numeric id to slug for comparison)
            if mu_id:
                mu_slug = mu_id_to_slug(mu_id)
                if links.get('mu', '') == mu_slug:
                    return manga['id'], links
            if best_id is None:
                best_id, best_links = manga['id'], links
        if best_id:
            return best_id, best_links
    except Exception as e:
        print(f"[MangaDex] ID lookup error: {e}")
    return None, {}

async def fetch_chapter_volume_map(mangadex_id: str) -> dict:
    """
    Fetch chapter→volume mapping from MangaDex aggregate endpoint.
    Returns {chapter_str: vol_int, ...} e.g. {"1": 1, "2": 1, "5": 2, ...}
    No language filter — we only need the volume assignment metadata, not the text.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # No translatedLanguage filter — we want volume metadata regardless of language
            r = await client.get(
                f'https://api.mangadex.org/manga/{mangadex_id}/aggregate'
            )
        data = r.json()
        mapping: dict[str, int] = {}
        volumes = data.get('volumes', {})
        # Guard against malformed response (list instead of dict)
        if not isinstance(volumes, dict):
            return mapping
        for vol_key, vol_data in volumes.items():
            try:
                vol_num = int(float(vol_key))
            except (ValueError, TypeError):
                continue  # skip "none" / uncollected chapters
            chapters = vol_data.get('chapters') if isinstance(vol_data, dict) else {}
            if isinstance(chapters, dict):
                for ch_key in chapters.keys():
                    mapping[ch_key] = vol_num
        return mapping
    except Exception as e:
        print(f"[MangaDex] aggregate error: {e}")
    return {}

async def fetch_kitsu_chapter_map(title: str, anilist_id: int | None,
                                  total_chapters: int | None) -> dict:
    """
    Fetch chapter→volume mapping from Kitsu's chapters API.
    Returns {chapter_str: vol_int, ...} or {} on failure.
    Kitsu is a reliable fallback for DMCA'd MangaDex titles.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Find Kitsu manga ID by title search
            r = await client.get(
                'https://kitsu.io/api/edge/manga',
                params={'filter[text]': title, 'page[limit]': 10},
                headers={'Accept': 'application/vnd.api+json'},
            )
        data = r.json()
        kitsu_id = None
        for item in data.get('data', []):
            attrs = item.get('attributes', {})
            # Match on chapterCount to narrow down (AniList doesn't expose ID via Kitsu directly)
            ch_count = attrs.get('chapterCount') or 0
            vol_count = attrs.get('volumeCount') or 0
            # Prefer exact chapter count match, fall back to first result
            if total_chapters and abs(ch_count - total_chapters) <= 2:
                kitsu_id = item['id']
                break
            if kitsu_id is None:
                kitsu_id = item['id']

        if not kitsu_id:
            return {}

        # Paginate through all chapters
        mapping: dict[str, int] = {}
        offset = 0
        limit  = 20
        async with httpx.AsyncClient(timeout=15) as client:
            while True:
                r = await client.get(
                    'https://kitsu.io/api/edge/chapters',
                    params={
                        'filter[manga_id]': kitsu_id,
                        'page[limit]':      limit,
                        'page[offset]':     offset,
                        'fields[chapters]': 'number,volumeNumber',
                    },
                    headers={'Accept': 'application/vnd.api+json'},
                )
                page = r.json()
                rows = page.get('data', [])
                if not rows:
                    break
                for ch in rows:
                    attrs   = ch.get('attributes', {})
                    ch_num  = attrs.get('number')
                    vol_num = attrs.get('volumeNumber')
                    if ch_num is not None and vol_num is not None:
                        try:
                            mapping[str(int(float(ch_num)))] = int(float(vol_num))
                        except (ValueError, TypeError):
                            pass
                # Check if there are more pages
                next_link = (page.get('links') or {}).get('next')
                if not next_link:
                    break
                offset += limit
                if offset > 2000:  # safety cap
                    break

        return mapping
    except Exception as e:
        print(f"[Kitsu] chapter map error: {e}")
    return {}


def _validate_chapter_map(mapping: dict, total_chapters: int | None, source: str) -> bool:
    """Return False if the map looks too sparse to be useful."""
    if not mapping:
        return False
    if total_chapters and total_chapters > 10:
        coverage = len(mapping) / total_chapters
        if coverage < 0.5:
            print(f"[{source}] map covers only {len(mapping)}/{total_chapters} chapters ({coverage:.0%}) — discarding")
            return False
    if len(set(mapping.values())) < 2:
        print(f"[{source}] all chapters map to same volume — discarding")
        return False
    return True


def get_series_chapter_map(series_id: int) -> dict:
    """Load cached chapter→volume map for a series from DB."""
    with get_db() as db:
        row = db.execute(
            "SELECT chapter_vol_map FROM series WHERE id=?", (series_id,)
        ).fetchone()
    if row and row['chapter_vol_map']:
        try:
            return json.loads(row['chapter_vol_map'])
        except Exception:
            pass
    return {}

def chapters_to_volume_set(ch_start: float, ch_end: float,
                            chapter_map: dict,
                            total_chapters: int | None,
                            total_volumes: int | None) -> set:
    """
    Resolve a chapter range to the set of volume numbers it covers.
    Uses MangaDex mapping when available and sufficiently dense;
    falls back to linear approximation otherwise.
    """
    volumes: set[int] = set()
    if chapter_map:
        for ch_str, vol_num in chapter_map.items():
            try:
                ch = float(ch_str)
                if ch_start <= ch <= ch_end:
                    volumes.add(vol_num)
            except (ValueError, TypeError):
                pass
        # Only trust the map if it found volumes OR the map is dense enough
        # A sparse map (e.g. DMCA'd series with 2 entries) should fall through to approximation
        expected_in_range = ch_end - ch_start + 1
        # Map is trustworthy if it covers ≥30% of the expected chapters in range
        map_coverage = sum(
            1 for ch_str in chapter_map
            if ch_start <= float(ch_str) <= ch_end
            if ch_str.replace('.', '').isdigit()
        )
        if volumes and map_coverage >= max(3, expected_in_range * 0.3):
            return volumes
    # Linear approximation fallback (also used when map is sparse)
    if total_chapters and total_chapters > 0 and total_volumes and total_volumes > 0:
        chs_per_vol = total_chapters / total_volumes
        ch_start_capped = min(ch_start, total_chapters)
        ch_end_capped   = min(ch_end,   total_chapters)
        vol_start = max(1, round(ch_start_capped / chs_per_vol))
        vol_end   = min(total_volumes, round(ch_end_capped / chs_per_vol))
        if vol_start <= vol_end:
            return set(range(vol_start, vol_end + 1))
    return volumes

def _coverage_already_grabbed(series_id: int, pack_type: str,
                               vol_rng: tuple | None,
                               ch_range: tuple | None,
                               ch_map: dict,
                               total_chs: int | None,
                               total_vols: int | None) -> bool:
    """
    Return True if the content this pack would provide is already fully covered
    by existing grabbed packs for this series — skip the download client.
    """
    with get_db() as db:
        # If a complete pack is already grabbed, everything is covered
        has_complete = db.execute(
            "SELECT 1 FROM volumes WHERE series_id=? AND pack_type='complete' AND status='grabbed'",
            (series_id,)
        ).fetchone()
        if has_complete and pack_type != 'complete':
            return True

        # For a new complete pack, only skip if no wanted+monitored stubs remain
        if pack_type == 'complete':
            wanted = db.execute(
                "SELECT 1 FROM volumes WHERE series_id=? AND volume_num IS NOT NULL"
                " AND status='wanted' AND monitored=1",
                (series_id,)
            ).fetchone()
            return wanted is None  # no wanted+monitored stubs → already complete

        # Determine which volumes this pack covers
        if pack_type == 'chapter' and ch_range:
            target_vols = chapters_to_volume_set(ch_range[0], ch_range[1], ch_map, total_chs, total_vols)
        elif pack_type == 'chapter' and not ch_range:
            return False  # can't determine coverage, don't skip
        elif pack_type == 'volume' and vol_rng:
            target_vols = set(range(int(vol_rng[0]), int(vol_rng[1]) + 1))
        else:
            return False

        if not target_vols:
            return False

        # Check if all target volume stubs are already grabbed/downloaded or unmonitored.
        # A range is "covered" (skip grab) when no wanted+monitored volumes remain in it.
        placeholders = ','.join('?' * len(target_vols))
        wanted_in_range = db.execute(
            f"SELECT 1 FROM volumes WHERE series_id=? AND volume_num IS NOT NULL"
            f" AND CAST(volume_num AS INTEGER) IN ({placeholders})"
            f" AND status='wanted' AND monitored=1",
            [series_id, *target_vols]
        ).fetchone()
        return wanted_in_range is None  # no wanted+monitored stubs in range → covered

def _extract_map_from_cbzs(series_dir: str) -> dict:
    """
    Scan downloaded CBZ/CBR files in series_dir for danke-Empire style filenames:
      Title - c{N} (v{N}) - p{N} ...
    Returns {chapter_str: vol_int} mapping.
    """
    mapping: dict[str, int] = {}
    if not series_dir or not os.path.isdir(series_dir):
        return mapping
    pat = re.compile(r'\bc(\d+(?:\.\d+)?)\s*\(v(\d+)\)', re.IGNORECASE)
    for fname in os.listdir(series_dir):
        if not fname.lower().endswith(('.cbz', '.cbr', '.zip')):
            continue
        fpath = os.path.join(series_dir, fname)
        try:
            with zipfile.ZipFile(fpath) as zf:
                for entry in zf.namelist():
                    m = pat.search(entry)
                    if m:
                        ch_key = m.group(1)  # keep as string e.g. "1", "168.1"
                        vol_num = int(m.group(2))
                        mapping[ch_key] = vol_num
        except Exception:
            pass
    return mapping


async def refresh_mangadex_map(series_id: int) -> bool:
    """Look up MangaDex, store chapter→volume map and cross-reference IDs. Returns True if successful."""
    with get_db() as db:
        s = db.execute(
            "SELECT title, anilist_id, mangadex_id, mal_id, mu_id FROM series WHERE id=?",
            (series_id,)
        ).fetchone()
    if not s:
        return False
    mdx_id = s['mangadex_id']
    links  = {}
    if not mdx_id:
        mdx_id, links = await fetch_mangadex_id(s['title'], s['anilist_id'], s['mu_id'])
    elif not s['mal_id'] or not s['mu_id']:
        # Have UUID but missing cross-refs — fetch links from MangaDex by ID
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f'https://api.mangadex.org/manga/{mdx_id}')
            md_data = r.json().get('data', {})
            links = (md_data.get('attributes') or {}).get('links') or {}
        except Exception:
            pass
    if not mdx_id:
        print(f"[MangaDex] Could not find ID for series {series_id}")
        return False
    with get_db() as db:
        meta = db.execute(
            "SELECT title, total_volumes, total_chapters FROM series WHERE id=?",
            (series_id,)
        ).fetchone()
    total_ch = meta['total_chapters'] if meta else None

    mapping = await fetch_chapter_volume_map(mdx_id)
    map_source = 'mangadex'
    if not _validate_chapter_map(mapping, total_ch, 'MangaDex'):
        mapping = {}

    # Fallback when MangaDex has no usable chapter data (DMCA'd / sparse): try Kitsu
    if not mapping and meta:
        kitsu_map = await fetch_kitsu_chapter_map(
            meta['title'], s['anilist_id'], meta['total_chapters']
        )
        if _validate_chapter_map(kitsu_map, total_ch, 'Kitsu'):
            mapping = kitsu_map
            map_source = 'kitsu'

    # Fallback: extract chapter→volume map from downloaded CBZ filenames
    if not mapping:
        with get_db() as db:
            cbz_dir = _series_library_dir(db, series_id)
        cbz_map = _extract_map_from_cbzs(cbz_dir)
        print(f"[CBZ] series {series_id}: dir={cbz_dir}, entries={len(cbz_map)}, total_ch={total_ch}")
        if _validate_chapter_map(cbz_map, total_ch, 'CBZ'):
            mapping = cbz_map
            map_source = 'cbz'

    # Extract cross-reference IDs from MangaDex links
    mal_from_mdx = links.get('mal')
    mu_slug      = links.get('mu')
    mu_from_mdx  = mu_slug_to_id(mu_slug) if mu_slug else None

    with get_db() as db:
        db.execute(
            "UPDATE series SET mangadex_id=?, chapter_vol_map=?,"
            " mal_id=COALESCE(mal_id, ?), mu_id=COALESCE(mu_id, ?) WHERE id=?",
            (mdx_id,
             json.dumps(mapping) if mapping else None,
             int(mal_from_mdx) if mal_from_mdx and str(mal_from_mdx).isdigit() else None,
             mu_from_mdx,
             series_id)
        )
        if mapping:
            ch_created = populate_chapters(db, series_id)
            print(f"[{map_source.upper()}] Stored {len(mapping)} chapter→vol entries for series {series_id}"
                  + (f", created {ch_created} chapter stubs" if ch_created else ""))
        else:
            print(f"[MangaDex] No chapter map for {mdx_id} — cross-refs only (no fallback data)")
    return True

# ── Metadata enrichment helpers ───────────────────────────────────────────────

# Word-to-integer mapping used when parsing Wikipedia natural-language counts.
_WIKI_WORD_NUMS: dict[str, int] = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
    'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14,
    'fifteen': 15, 'sixteen': 16, 'seventeen': 17, 'eighteen': 18,
    'nineteen': 19, 'twenty': 20, 'twenty-one': 21, 'twenty-two': 22,
    'twenty-three': 23, 'twenty-four': 24, 'twenty-five': 25,
    'twenty-six': 26, 'twenty-seven': 27, 'twenty-eight': 28,
    'twenty-nine': 29, 'thirty': 30, 'thirty-one': 31, 'thirty-two': 32,
    'thirty-three': 33, 'thirty-four': 34, 'thirty-five': 35,
    'forty': 40, 'forty-five': 45, 'fifty': 50,
}

async def fetch_wikipedia_volume_count(series_id: int, title: str, edition_type: str) -> int | None:
    """
    Query Wikipedia to find edition-specific volume counts as a fallback when
    Google Books returns insufficient data. Parses wikitext for patterns like
    "X volumes" or "fourteen volumes have been released" near edition keywords.
    Returns the count or None if not found with sufficient confidence.
    No API key required — Wikipedia is free and openly accessible.
    """
    edition_kws = _EDITION_SEARCH_KEYWORDS.get(edition_type, [])
    if not edition_kws:
        return None

    # Try both "{title} (manga)" and bare title; follow redirects automatically
    wikitext: str | None = None
    for search_title in [f"{title} (manga)", title]:
        try:
            async with httpx.AsyncClient(timeout=15) as cli:
                r = await cli.get(
                    'https://en.wikipedia.org/w/api.php',
                    params={
                        'action': 'query',
                        'titles': search_title,
                        'prop': 'revisions',
                        'rvprop': 'content',
                        'rvslots': 'main',
                        'format': 'json',
                        'redirects': '1',
                    },
                    headers={'User-Agent': 'mangarr/1.0 (manga metadata; github.com/khak1s/manga-arr)'},
                )
            r.raise_for_status()
            pages = r.json().get('query', {}).get('pages', {})
            for page in pages.values():
                if page.get('pageid', -1) == -1:
                    continue  # missing page
                revs = page.get('revisions', [])
                if revs:
                    # Newer rvslots format
                    wikitext = revs[0].get('slots', {}).get('main', {}).get('*', '')
                    if not wikitext:
                        # Legacy format
                        wikitext = revs[0].get('*', '')
                break
            if wikitext:
                break
        except Exception as e:
            print(f"[Wikipedia] series {series_id} '{search_title}': {e}")

    if not wikitext:
        print(f"[Wikipedia] series {series_id} '{title}': no article found")
        return None

    # Strip <ref> footnotes — they contain long URLs that inflate character
    # distances between prose keywords and volume counts.
    wikitext = re.sub(r'<ref[^>]*/>', '', wikitext)
    wikitext = re.sub(r'<ref[^>]*>.*?</ref>', '', wikitext, flags=re.DOTALL)

    # Build a pattern that matches digit strings or written-out number words
    # followed by "volume(s)" — e.g. "14 volumes" or "fourteen volumes"
    _word_alts = '|'.join(
        re.escape(w) for w in sorted(_WIKI_WORD_NUMS, key=len, reverse=True)
    )
    _num_pat = rf'(\d+|{_word_alts})\s+volumes?'

    def _parse(s: str) -> int | None:
        if s.isdigit():
            n = int(s)
            return n if 1 <= n <= 200 else None
        return _WIKI_WORD_NUMS.get(s.lower())

    # For each edition keyword, scan a 600-char window around each occurrence
    candidates: list[int] = []
    for kw in edition_kws:
        for m in re.finditer(re.escape(kw), wikitext, re.IGNORECASE):
            start = max(0, m.start() - 500)
            end   = min(len(wikitext), m.end() + 500)
            window = wikitext[start:end]
            for nm in re.finditer(_num_pat, window, re.IGNORECASE):
                count = _parse(nm.group(1))
                if count:
                    candidates.append(count)

    if not candidates:
        print(f"[Wikipedia] series {series_id} '{title}' ({edition_type}): "
              f"no volume counts found near edition keywords")
        return None

    # Sanity filter: non-standard editions always have fewer volumes than the standard
    # edition. Candidates at or above 85% of the standard count are almost certainly
    # the standard count bleeding through from nearby text (e.g. "Deluxe Edition...
    # the series ran for 43 volumes"). Filter those out before taking the max.
    #
    # Only apply this filter when vol_count_source is 'anilist' — meaning total_volumes
    # is the provisional AniList standard count. If it's already been enriched by
    # Google Books or Wikipedia, total_volumes is the edition-specific count and
    # should not be used as the standard-edition upper bound.
    with get_db() as db:
        std_row = db.execute(
            "SELECT total_volumes, vol_count_source FROM series WHERE id=?", (series_id,)
        ).fetchone()
    std_count = 0
    if std_row and (std_row['vol_count_source'] or 'anilist') == 'anilist':
        std_count = std_row['total_volumes'] or 0

    if std_count > 0:
        threshold = std_count * 0.85
        filtered = [c for c in candidates if c < threshold]
        if filtered:
            candidates = filtered
            # else: all candidates were near the standard count — keep original set
            # rather than returning nothing

    best = max(candidates)
    print(f"[Wikipedia] series {series_id} '{title}' ({edition_type}): "
          f"found {best} volumes (all candidates: {sorted(set(candidates))}, "
          f"std_count={std_count})")
    return best


async def fetch_edition_volume_count(series_id: int, title: str, edition_type: str) -> int | None:
    """
    Query Google Books to find the correct total_volumes for a non-standard edition
    (omnibus, deluxe, collector, special, remaster). Returns the max volume number
    found, or None if the result was not confident enough to trust.
    """
    keywords = _EDITION_SEARCH_KEYWORDS.get(edition_type)
    if not keywords:
        return None

    # Idempotency: don't overwrite a better source that's already set
    with get_db() as db:
        src_row = db.execute(
            "SELECT vol_count_source FROM series WHERE id=?", (series_id,)
        ).fetchone()
    current_source = (src_row['vol_count_source'] if src_row else None) or 'anilist'
    if current_source in ('google_books', 'wikipedia', 'manual'):
        print(f"[GoogleBooks] series {series_id}: skipping — source already '{current_source}'")
        return None

    title_words = set(normalize(title).lower().split())
    _gb_key = get_cfg('google_books_api_key', '').strip()

    async def _gb_query(q: str) -> list[dict] | None:
        """Run one Google Books query. Returns items list, or None on quota/error."""
        _p: dict = {'q': q, 'maxResults': 40, 'printType': 'books'}
        if _gb_key:
            _p['key'] = _gb_key
        try:
            async with httpx.AsyncClient(timeout=15) as cli:
                _r = await cli.get(
                    'https://www.googleapis.com/books/v1/volumes',
                    params=_p,
                    headers={'User-Agent': 'mangarr/1.0'},
                )
            if _r.status_code == 429:
                print(f"[GoogleBooks] series {series_id}: daily quota exceeded. "
                      f"Add a Google Books API key in Settings to increase the limit.")
                return None  # signal quota — stop all queries
            _r.raise_for_status()
            return _r.json().get('items', [])
        except Exception as e:
            print(f"[GoogleBooks] series {series_id} query '{q}': {e}")
            return []

    def _extract_vols(items: list[dict]) -> set[int]:
        nums: set[int] = set()
        for item in items:
            vol_title = ((item.get('volumeInfo') or {}).get('title') or '').lower()
            # Filter: all series title words must appear in the book title
            if not all(w in vol_title for w in title_words):
                continue
            # Strip parenthetical content like "(Vol. 22-24)" or "(Includes vols. 1-3)"
            # before extracting numbers — otherwise standard-range suffixes inflate the max.
            clean = re.sub(r'\s*\([^)]*\)', '', vol_title).strip()
            for m in re.finditer(
                r'(?:vol(?:ume)?\.?\s*)(\d+)|(?<!\d)(\d+)(?!\d)', clean, re.IGNORECASE
            ):
                n = int(m.group(1) or m.group(2))
                if 1 <= n <= 999:
                    nums.add(n)
        return nums

    found_volumes: set[int] = set()
    quota_hit = False

    # Strategy 1: exact quoted phrase for each keyword — most precise
    for keyword in keywords:
        items = await _gb_query(f'"{title}" "{keyword}"')
        if items is None:
            quota_hit = True
            break
        found_volumes |= _extract_vols(items)
        if len(found_volumes) >= 2:
            break

    # Strategy 2: unquoted fallback — more permissive, catches cases where Google Books
    # metadata doesn't contain the exact keyword string
    if not quota_hit and len(found_volumes) < 2:
        for keyword in keywords:
            items = await _gb_query(f'{title} {keyword}')
            if items is None:
                quota_hit = True
                break
            found_volumes |= _extract_vols(items)
            if len(found_volumes) >= 2:
                break

    if quota_hit:
        return None

    if len(found_volumes) < 2 or (max(found_volumes) - min(found_volumes)) < 1:
        print(f"[GoogleBooks] series {series_id} '{title}' ({edition_type}): "
              f"insufficient data — found volumes {sorted(found_volumes)}")

        # Fallback 1: Try Wikipedia for edition-specific volume count
        wiki_count = await fetch_wikipedia_volume_count(series_id, title, edition_type)
        if wiki_count:
            with get_db() as db:
                db.execute(
                    "UPDATE series SET total_volumes=?, vol_count_source='wikipedia' WHERE id=?",
                    (wiki_count, series_id)
                )
                create_volume_stubs(db, series_id, wiki_count)
            log_event('metadata',
                      f"[Wikipedia] {edition_type} edition: {wiki_count} volumes "
                      f"(Google Books had insufficient data)",
                      series_id)
            print(f"[Wikipedia] series {series_id} '{title}' ({edition_type}): "
                  f"set total_volumes={wiki_count}")
            return wiki_count

        # Fallback 2: use AniList standard count as provisional stubs so the series
        # isn't left idle with nothing to search for. vol_count_source stays 'anilist'
        # so the warning banner appears on the series page.
        with get_db() as db:
            al_row = db.execute(
                "SELECT total_volumes FROM series WHERE id=?", (series_id,)
            ).fetchone()
            al_count = (al_row['total_volumes'] or 0) if al_row else 0
            if al_count > 0:
                create_volume_stubs(db, series_id, al_count)
        if al_count > 0:
            log_event('warning',
                      f"[GoogleBooks/Wikipedia] Could not find {edition_type} volume count. "
                      f"Using AniList standard count ({al_count}) as provisional fallback — "
                      f"may be inaccurate. Use 'Refresh Edition Metadata' for the correct count.",
                      series_id)
            print(f"[GoogleBooks/Wikipedia] series {series_id} '{title}': "
                  f"provisional fallback — {al_count} stubs from AniList")
        return None

    best_count = max(found_volumes)
    with get_db() as db:
        db.execute(
            "UPDATE series SET total_volumes=?, vol_count_source='google_books' WHERE id=?",
            (best_count, series_id)
        )
        create_volume_stubs(db, series_id, best_count)
    log_event('metadata',
              f"[GoogleBooks] {edition_type} edition: {best_count} volumes "
              f"(keywords tried: {keywords[:len(found_volumes)]})",
              series_id)
    print(f"[GoogleBooks] series {series_id} '{title}' ({edition_type}): "
          f"set total_volumes={best_count}")
    return best_count


async def fetch_mu_metadata(series_id: int, title: str) -> dict | None:
    """
    Cross-reference MangaUpdates to get a more reliable volume count for standard
    editions, and to populate mu_id if missing. Never overwrites google_books or manual
    sources. Returns a summary dict or None if no confident match was found.
    """
    with get_db() as db:
        s_row = db.execute(
            "SELECT mu_id, edition_type, total_volumes, vol_count_source FROM series WHERE id=?",
            (series_id,)
        ).fetchone()
    if not s_row:
        return None

    current_source = (s_row['vol_count_source'] or 'anilist')
    if current_source in ('google_books', 'wikipedia', 'manual'):
        return None  # never downgrade

    # Search MU — reuse existing mu_search() which already parses volume counts
    results = await mu_search(title)
    if not results:
        return None

    stored_words = set(normalize(title).split())
    def _f1(r_title: str) -> float:
        r_words = set(normalize(r_title).split())
        if not r_words or not stored_words:
            return 0.0
        inter = stored_words & r_words
        rec  = len(inter) / len(stored_words)
        prec = len(inter) / len(r_words)
        return 2 * rec * prec / (rec + prec) if (rec + prec) else 0.0

    best = max(results, key=lambda r: _f1(r['title']))
    if _f1(best['title']) < 0.7:
        return None  # not confident enough for silent background enrichment

    matched_mu_id  = best['mu_id']
    mu_vol_count   = best['volumes']  # already parsed from "N Volumes (Complete)" by mu_search()
    edition        = (s_row['edition_type'] or 'standard')
    current_vols   = s_row['total_volumes'] or 0

    updated_vols = False
    with get_db() as db:
        # Always store mu_id if we didn't have one
        if matched_mu_id and not s_row['mu_id']:
            db.execute(
                "UPDATE series SET mu_id=? WHERE id=? AND (mu_id IS NULL OR mu_id='')",
                (matched_mu_id, series_id)
            )
        # Update volume count only for standard editions where MU count is strictly higher
        should_update = (
            edition not in _NON_STANDARD_STUB_EDITIONS
            and mu_vol_count is not None
            and mu_vol_count > current_vols
            and current_source not in ('google_books', 'wikipedia', 'manual')
        )
        if should_update:
            db.execute(
                "UPDATE series SET total_volumes=?, vol_count_source='mangaupdates' WHERE id=?",
                (mu_vol_count, series_id)
            )
            create_volume_stubs(db, series_id, mu_vol_count)
            updated_vols = True
            log_event('metadata',
                      f"[MangaUpdates] updated vol count: {current_vols}→{mu_vol_count}",
                      series_id)
            print(f"[MangaUpdates] series {series_id} '{title}': "
                  f"vol count {current_vols}→{mu_vol_count}")

    return {'mu_id': matched_mu_id, 'volumes': mu_vol_count, 'updated_vols': updated_vols}


# ── Grab logic ────────────────────────────────────────────────────────────────
async def grab_item(item: dict, series_id: int, respect_monitoring: bool = True) -> bool:
    """
    Send item to download client and record. Returns True on success.
    respect_monitoring=False bypasses per-volume and series monitor_mode checks
    (used for manual interactive grabs).
    """
    title    = item['title']
    indexer  = item.get('indexer', 'Unknown')
    protocol = item.get('protocol', 'torrent')

    # Check seen (already grabbed) — must be a live DB query, not a cached set,
    # to guard against concurrent grabs (RSS poll + manual, or overlapping polls).
    with get_db() as db:
        if db.execute("SELECT 1 FROM seen WHERE torrent_url=?", (item['url'],)).fetchone():
            return False

    # In-flight dedup: all code between here and `await grab_url` is synchronous,
    # so a second coroutine that also passed the seen check above will see this
    # entry and bail before it can send a duplicate to the download client.
    if item['url'] in _GRABBING_URLS:
        return False
    _GRABBING_URLS.add(item['url'])

    # Check blocklist
    with get_db() as db:
        if db.execute("SELECT 1 FROM blocklist WHERE torrent_url=?", (item['url'],)).fetchone():
            _GRABBING_URLS.discard(item['url'])
            return False

    if respect_monitoring:
        # Check series monitor mode and edition type in one query
        with get_db() as db:
            s_mode_row = db.execute(
                "SELECT monitor_mode, edition_type FROM series WHERE id=?", (series_id,)
            ).fetchone()
        mode = (s_mode_row['monitor_mode'] if s_mode_row else None) or 'all'
        if mode == 'none':
            return False
        # Edition-type filter: series must only grab releases of its own edition.
        # Standard series skips colored/omnibus/deluxe etc. Non-standard series
        # skips standard (B&W) and other editions.
        _series_edition  = (s_mode_row['edition_type'] if s_mode_row else None) or 'standard'
        _release_edition = detect_edition_type(title) or 'standard'
        if _series_edition != _release_edition:
            return False

    # Minimum seeders check for torrents
    if item.get('protocol') == 'torrent':
        _min_seeds = int(get_cfg('min_seeders', '0') or '0')
        if _min_seeds > 0 and (item.get('seeders') or 0) < _min_seeds:
            return False

    # Parse item type early so we can pass volume_num into score_release
    vol_num = extract_volume_num(title)
    vol_rng = extract_volume_range(title)  # always check for range
    if vol_rng is not None:
        vol_num = None  # multi-volume range takes precedence over single number

    # Fetch pub_year for year-match bonus in scoring
    _pub_year: int | None = None
    try:
        with get_db() as _py_db:
            _py_row = _py_db.execute(
                "SELECT pub_year FROM series WHERE id=?", (series_id,)
            ).fetchone()
            if _py_row and _py_row['pub_year']:
                _pub_year = int(_py_row['pub_year'])
    except Exception:
        pass

    # Per-series scoring: apply language profile, release profiles, CF scoring,
    # and volume/year match bonuses for better disambiguation.
    _series_sc = score_release(title, series_id,
                               release_group=item.get('release_group', ''),
                               indexer=item.get('indexer', ''),
                               volume_num=vol_num,
                               pub_year=_pub_year)
    if _series_sc <= -900:
        return False

    # Per-volume monitoring check (single volume)
    if respect_monitoring and vol_num is not None:
        with get_db() as db:
            vol_mon = db.execute(
                "SELECT monitored FROM volumes WHERE series_id=? AND volume_num=?",
                (series_id, vol_num)
            ).fetchone()
        if vol_mon and vol_mon['monitored'] == 0:
            return False

    # Pack monitoring check: reject the entire pack (RSS sync) if no wanted+monitored
    # volumes are in the range — mirrors Sonarr's MonitoredEpisodeSpecification behavior.
    if respect_monitoring and vol_num is None and vol_rng is not None:
        with get_db() as db:
            has_monitored = db.execute(
                "SELECT 1 FROM volumes WHERE series_id=? AND status='wanted' AND monitored=1"
                " AND volume_num >= ? AND volume_num <= ? LIMIT 1",
                (series_id, vol_rng[0], vol_rng[1])
            ).fetchone()
        if not has_monitored:
            return False

    # Fetch series context (needed for coverage check and pack detection)
    with get_db() as db:
        s_row = db.execute(
            "SELECT title, total_volumes, total_chapters, chapter_vol_map, cover_url,"
            " root_folder_id, update_strategy FROM series WHERE id=?", (series_id,)
        ).fetchone()
        rf_row = db.execute(
            "SELECT rf.path FROM root_folders rf WHERE rf.id=?",
            (s_row['root_folder_id'],)
        ).fetchone() if s_row and s_row['root_folder_id'] else None

    total_vols = s_row['total_volumes'] if s_row else None
    total_chs  = s_row['total_chapters'] if s_row else None
    cover_url  = (s_row['cover_url'] or '') if s_row else ''
    # Let the download client use its own configured directory.
    # We query content_path from the client after completion for importing.
    save_path  = None
    ch_map: dict = {}
    if s_row and s_row['chapter_vol_map']:
        try:
            ch_map = json.loads(s_row['chapter_vol_map'])
        except Exception as e:
            print(f"[grab_item] chapter_vol_map parse failed: {e}")

    pack_type = detect_pack_type(title, vol_rng, total_vols) if vol_num is None else None
    complete  = (pack_type == 'complete')

    # ── Coverage check: skip if content already fully grabbed ─────────────────
    if vol_num is None and pack_type:
        # Determine chapter range for chapter packs
        ch_range = vol_rng if pack_type == 'chapter' else None
        if not ch_range and pack_type == 'chapter':
            m = re.search(r'(?:ch(?:apter)?s?\.?\s*|#\s*)(\d{1,4}(?:\.\d+)?)\b', title, re.IGNORECASE)
            if not m:
                m = re.search(r'(?:^|[\s\[({])(\d{2,4})(?:[\s\])}]|$)', title)
            if m:
                ch = float(m.group(1))
                ch_range = (ch, ch)
        if _coverage_already_grabbed(series_id, pack_type, vol_rng, ch_range, ch_map, total_chs, total_vols):
            print(f"[Grab] Skipping '{title[:60]}' — coverage already satisfied")
            return False
    elif vol_num is not None:
        # Single volume — skip if already grabbed or downloaded, UNLESS this is a quality upgrade
        with get_db() as db:
            existing_vol = db.execute(
                "SELECT status, torrent_name, quality, release_group FROM volumes "
                "WHERE series_id=? AND volume_num=? AND status != 'wanted'",
                (series_id, vol_num)
            ).fetchone()
        if existing_vol:
            if existing_vol['status'] == 'grabbed':
                return False  # already in flight
            # 'once' strategy: never upgrade — grab once and stop
            _strategy = (s_row['update_strategy'] or 'always') if s_row else 'always'
            if _strategy == 'once':
                return False  # already have it; no upgrades for 'once' series
            # ── Repack / Proper handling (Sonarr RepackSpecification) ──────────
            _revision   = parse_revision(title)
            _prop_cfg   = get_cfg('propers_and_repacks', 'prefer_and_upgrade')
            if _revision['is_repack']:
                if _prop_cfg == 'do_not_upgrade':
                    # Never auto-grab repacks of already-downloaded volumes
                    print(f"[Grab] Skipping repack '{title[:60]}' — propers_and_repacks=do_not_upgrade")
                    return False
                elif _prop_cfg == 'prefer_and_upgrade':
                    # Only grab if same release group (cross-group repacks rejected)
                    existing_group = (existing_vol['release_group'] or '').strip().lower()
                    new_group      = parse_release_group(title).lower()
                    if existing_group and new_group and existing_group != new_group:
                        print(f"[Grab] Rejecting cross-group repack '{title[:60]}'"
                              f" — existing group '{existing_group}', repack group '{new_group}'")
                        return False
                # do_not_prefer: fall through — treat repack same as any release
            # Cutoff check first — if current quality already meets cutoff, no upgrades needed
            with get_db() as _cutoff_db:
                _s_cutoff = _cutoff_db.execute(
                    "SELECT quality_cutoff FROM series WHERE id=?", (series_id,)
                ).fetchone()
            cutoff = (_s_cutoff['quality_cutoff'] if _s_cutoff else None) or get_cfg('quality_cutoff', '')
            if cutoff and quality_rank(existing_vol['quality'] or '') >= quality_rank(cutoff):
                return False  # already at or above quality cutoff — no upgrade needed
            # For 'downloaded' volumes: allow if new release is strictly higher quality
            new_q = quality_from_filename(title)   # heuristic from release title extension
            old_q = existing_vol['quality']
            if quality_rank(new_q) > quality_rank(old_q):
                pass  # quality upgrade — allow grab
            else:
                # Same or unknown quality — fall back to score comparison
                new_score = score_release(title)
                old_score = score_release(existing_vol['torrent_name'] or '')
                if new_score <= old_score:
                    return False  # not an upgrade

    # Quality cutoff enforcement on initial grab — reject releases below the configured
    # minimum quality so we don't grab CBR when the series requires CBZ.
    # (Upgrades have their own cutoff check above; this handles the 'wanted' case.)
    if vol_num is not None:
        with get_db() as _q_db:
            _q_cutoff_row = _q_db.execute(
                "SELECT quality_cutoff FROM series WHERE id=?", (series_id,)
            ).fetchone()
        _cutoff = (_q_cutoff_row['quality_cutoff'] if _q_cutoff_row else None) or get_cfg('quality_cutoff', '')
        if _cutoff:
            _new_q = quality_from_filename(title)
            if _new_q and quality_rank(_new_q) < quality_rank(_cutoff):
                print(f"[Grab] Skipping '{title[:60]}' — quality {_new_q} below cutoff {_cutoff}")
                return False

    try:
        ok, client_name, dl_id = await grab_url(item['url'], protocol, save_path=save_path,
                                                 torrent_name=title)
    finally:
        _GRABBING_URLS.discard(item['url'])
    if not ok:
        return False

    now      = datetime.utcnow().isoformat()
    rgroup   = parse_release_group(title)
    size     = item.get('size_bytes', 0)
    edition  = detect_edition_type(title)
    lang     = item.get('language') or detect_language(title)

    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO seen"
            "(torrent_url, torrent_name, series_id, volume_num, grabbed_at,"
            " indexer, protocol, client, download_id, release_group, size_bytes)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (item['url'], title, series_id, vol_num, now,
             indexer, protocol, client_name, dl_id, rgroup, size)
        )

        _ch_cascade_kw = dict(grabbed_at=now, torrent_name=title, torrent_url=item['url'],
                              indexer=indexer, protocol=protocol, client=client_name,
                              download_id=dl_id, release_group=rgroup, size_bytes=size)

        if vol_num is not None:
            # ── Single volume ────────────────────────────────────────────────
            existing = db.execute(
                "SELECT id FROM volumes WHERE series_id=? AND volume_num=?",
                (series_id, vol_num)
            ).fetchone()
            if existing:
                db.execute(
                    "UPDATE volumes SET status='grabbed', grabbed_at=?, source_url=?,"
                    " download_id=?, torrent_name=?, client=?, indexer=?, protocol=?,"
                    " release_group=?, size_bytes=?, edition_type=?, language=? WHERE id=?",
                    (now, item['url'], dl_id, title, client_name,
                     indexer, protocol, rgroup, size, edition, lang, existing['id'])
                )
                _cascade_chapters(db, series_id, [existing['id']], 'grabbed', **_ch_cascade_kw)
            else:
                db.execute(
                    "INSERT INTO volumes(series_id, volume_num, status, grabbed_at,"
                    " source_url, download_id, torrent_name, client,"
                    " indexer, protocol, release_group, size_bytes, edition_type, language)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (series_id, vol_num, 'grabbed', now, item['url'], dl_id, title, client_name,
                     indexer, protocol, rgroup, size, edition, lang)
                )
                new_vol = db.execute(
                    "SELECT id FROM volumes WHERE series_id=? AND volume_num=?",
                    (series_id, vol_num)
                ).fetchone()
                if new_vol:
                    _cascade_chapters(db, series_id, [new_vol['id']], 'grabbed', **_ch_cascade_kw)
        else:
            # ── Pack/range/complete ──────────────────────────────────────────
            # For chapter packs the extracted range is a CHAPTER range, not a volume range
            if pack_type == 'chapter':
                store_rng_start = store_rng_end = None
            else:
                store_rng_start = vol_rng[0] if vol_rng else None
                store_rng_end   = vol_rng[1] if vol_rng else None

            # Record a single pack entry for reference
            db.execute(
                "INSERT OR IGNORE INTO volumes"
                "(series_id, status, grabbed_at, source_url, download_id,"
                " vol_range_start, vol_range_end, pack_type, torrent_name, client,"
                " indexer, protocol, release_group, size_bytes, edition_type, language)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (series_id, 'grabbed', now, item['url'], dl_id,
                 store_rng_start, store_rng_end, pack_type, title, client_name,
                 indexer, protocol, rgroup, size, edition, lang)
            )

            # Determine which volume stubs this pack covers
            covered_vols: set[int] = set()
            if complete:
                # Mark every wanted stub as grabbed (minimal tracking only)
                db.execute(
                    "UPDATE volumes SET status='grabbed', grabbed_at=?, source_url=?,"
                    " download_id=?, torrent_name=?, client=?, indexer=?, protocol=?,"
                    " release_group=?, size_bytes=?, edition_type=?, language=?"
                    " WHERE series_id=? AND status='wanted' AND volume_num IS NOT NULL",
                    (now, item['url'], dl_id, title, client_name,
                     indexer, protocol, rgroup, size, edition, lang, series_id)
                )
                # Cascade to ALL chapters
                _cascade_chapters(db, series_id, None, 'grabbed', **_ch_cascade_kw)
            elif pack_type == 'chapter' and vol_rng:
                # Map chapter range → volume set using MangaDex map or approximation
                covered_vols = chapters_to_volume_set(
                    vol_rng[0], vol_rng[1], ch_map, total_chs, total_vols
                )
                # Also directly update chapters in the chapter range
                db.execute(
                    "UPDATE chapters SET status='grabbed', grabbed_at=?, torrent_name=?,"
                    " torrent_url=?, indexer=?, protocol=?, client=?, download_id=?"
                    " WHERE series_id=? AND chapter_num >= ? AND chapter_num <= ? AND monitored=1",
                    (now, title, item['url'], indexer, protocol, client_name, dl_id,
                     series_id, vol_rng[0], vol_rng[1])
                )
            elif pack_type == 'chapter' and not vol_rng:
                # Single chapter number — extract and map
                single_m = re.search(r'(?:^|[\s\[({])(\d{2,4})(?:[\s\])}]|$)', title)
                if single_m:
                    ch = float(single_m.group(1))
                    covered_vols = chapters_to_volume_set(
                        ch, ch, ch_map, total_chs, total_vols
                    )
                    db.execute(
                        "UPDATE chapters SET status='grabbed', grabbed_at=?, torrent_name=?,"
                        " torrent_url=?, indexer=?, protocol=?, client=?, download_id=?"
                        " WHERE series_id=? AND chapter_num=? AND monitored=1",
                        (now, title, item['url'], indexer, protocol, client_name, dl_id,
                         series_id, ch)
                    )
            elif vol_rng:
                # Volume range pack — update existing stubs then insert any missing ones
                db.execute(
                    "UPDATE volumes SET status='grabbed', grabbed_at=?, source_url=?,"
                    " download_id=?, torrent_name=?, client=?, indexer=?, protocol=?,"
                    " release_group=?, size_bytes=?, edition_type=?, language=?"
                    " WHERE series_id=? AND status='wanted'"
                    " AND volume_num IS NOT NULL"
                    " AND volume_num >= ? AND volume_num <= ?",
                    (now, item['url'], dl_id, title, client_name,
                     indexer, protocol, rgroup, size, edition, lang,
                     series_id, vol_rng[0], vol_rng[1])
                )
                # Insert stubs for volumes in the range that have no stub yet
                existing_in_range = {
                    r['volume_num']
                    for r in db.execute(
                        "SELECT volume_num FROM volumes WHERE series_id=?"
                        " AND volume_num IS NOT NULL"
                        " AND volume_num >= ? AND volume_num <= ?",
                        (series_id, vol_rng[0], vol_rng[1])
                    ).fetchall()
                }
                for vn in range(int(vol_rng[0]), int(vol_rng[1]) + 1):
                    if float(vn) not in existing_in_range:
                        db.execute(
                            "INSERT INTO volumes(series_id, volume_num, status,"
                            " grabbed_at, source_url, download_id, torrent_name, client,"
                            " indexer, protocol, release_group, size_bytes, edition_type, language)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (series_id, float(vn), 'grabbed', now, item['url'], dl_id,
                             title, client_name, indexer, protocol, rgroup, size, edition, lang)
                        )
                rng_vol_ids = [
                    r['id'] for r in db.execute(
                        "SELECT id FROM volumes WHERE series_id=? AND volume_num IS NOT NULL"
                        " AND volume_num >= ? AND volume_num <= ?",
                        (series_id, vol_rng[0], vol_rng[1])
                    ).fetchall()
                ]
                if rng_vol_ids:
                    _cascade_chapters(db, series_id, rng_vol_ids, 'grabbed', **_ch_cascade_kw)

            # Mark the resolved volume stubs for chapter packs
            if covered_vols:
                placeholders = ','.join('?' * len(covered_vols))
                db.execute(
                    f"UPDATE volumes SET status='grabbed', grabbed_at=?, source_url=?,"
                    f" download_id=?, torrent_name=?, client=?, indexer=?, protocol=?,"
                    f" release_group=?, size_bytes=?, edition_type=?, language=?"
                    f" WHERE series_id=? AND status='wanted'"
                    f" AND volume_num IS NOT NULL AND CAST(volume_num AS INTEGER) IN ({placeholders})",
                    [now, item['url'], dl_id, title, client_name,
                     indexer, protocol, rgroup, size, edition, lang, series_id, *covered_vols]
                )
                covered_vol_ids = [
                    r['id'] for r in db.execute(
                        f"SELECT id FROM volumes WHERE series_id=? AND volume_num IS NOT NULL"
                        f" AND CAST(volume_num AS INTEGER) IN ({placeholders})",
                        [series_id, *covered_vols]
                    ).fetchall()
                ]
                if covered_vol_ids:
                    _cascade_chapters(db, series_id, covered_vol_ids, 'grabbed', **_ch_cascade_kw)

    # ── Label for logging/notifications ──────────────────────────────────────
    vol_label = build_volume_label(vol_num, vol_rng, pack_type if vol_num is None else None)
    series_title = (s_row['title'] or '') if s_row else ''

    log_event('grab', f"{vol_label} via {indexer} [{protocol}] → {client_name}", series_id)

    with get_db() as db:
        _grab_score = item.get('_score')
        _grab_data  = {'score': _grab_score} if _grab_score is not None else None
        add_history(db, 'grabbed', series_id, series_title, vol_label,
                    source_title=title, indexer=indexer, protocol=protocol,
                    client=client_name, download_id=dl_id or '',
                    size_bytes=size, release_group=rgroup,
                    data=_grab_data,
                    torrent_url=item.get('url', ''))

    asyncio.create_task(notify_discord(
        '',
        embed=make_grab_embed(series_title, vol_label, indexer, protocol, client_name, cover_url),
        event='on_grab'
    ))
    asyncio.create_task(broadcast_queue_event('grabbed', {
        'series_id': series_id, 'label': vol_label, 'series': series_title
    }))
    return True

def _collect_and_score(items: list[dict], seen_in_results: set[str]) -> list[dict]:
    """Deduplicate and score a list of release items. Filters out ignored releases."""
    # Load quality definitions once per call for size enforcement
    qual_defs: dict[str, dict] = {}
    try:
        with get_db() as _qdb:
            for row in _qdb.execute("SELECT * FROM quality_definitions").fetchall():
                qual_defs[row['quality']] = dict(row)
    except Exception:
        pass

    out = []
    for it in items:
        if not it.get('url') or it['url'] in seen_in_results:
            continue
        sc = score_release(it['title'],
                           release_group=it.get('release_group', ''),
                           indexer=it.get('indexer', ''))
        if sc <= -900:  # ignored or required-word failed
            continue

        # ── Quality size enforcement ──────────────────────────────────────────
        size_bytes = it.get('size_bytes') or it.get('size') or 0
        if size_bytes and qual_defs:
            size_mb = size_bytes / (1024 * 1024)
            quality = detect_quality_from_title(it['title'])
            qdef = qual_defs.get(quality) or qual_defs.get('unknown')
            if qdef:
                qname = qdef.get('quality', quality).upper()
                min_size = qdef.get('min_size') or 0
                max_size = qdef.get('max_size') or 0
                if min_size > 0 and size_mb < min_size:
                    print(f"[QualDef] Skipping '{it['title']}' — {qname} too small: "
                          f"{size_mb:.1f} MB (min: {min_size} MB)")
                    continue
                if max_size > 0 and size_mb > max_size:
                    print(f"[QualDef] Skipping '{it['title']}' — {qname} too large: "
                          f"{size_mb:.1f} MB (max: {max_size} MB)")
                    continue

        it = dict(it)
        it['_score'] = sc
        # Boost score slightly by seeder count so well-seeded releases rank higher
        seeders = it.get('seeders', 0) or 0
        it['_score'] += min(seeders, 20)  # cap contribution at 20 to avoid swamping quality score
        out.append(it)
        seen_in_results.add(it['url'])
    return out

async def _search_all(title: str) -> list[dict]:
    """Search all enabled DB indexers, deduplicate, score, and sort by score desc."""
    from routers.indexers import search_all_indexers as _search_db_indexers
    with get_db() as _sdb:
        raw_items = await _search_db_indexers(_sdb, title)
    seen_in_results: set[str] = set()
    all_items = _collect_and_score(raw_items, seen_in_results)
    all_items.sort(key=lambda x: x.get('_score', 0), reverse=True)
    return all_items

async def grab_existing(series_id: int, title: str, pattern: str) -> int:
    """Search all sources for all releases; grab unseen matches. Respects aliases.
    For FINISHED series with significant missing coverage, tries a complete pack search first."""
    try:
        return await _grab_existing_inner(series_id, title, pattern)
    except Exception as e:
        log_event('error', f"[grab_existing] Unhandled error for '{title}': {e}", series_id)
        print(f"[grab_existing] series {series_id} '{title}': {e}")
        return 0


async def _grab_existing_inner(series_id: int, title: str, pattern: str) -> int:
    # ── Complete-pack-first strategy for finished series ─────────────────────
    with get_db() as db:
        s_row = db.execute(
            "SELECT status, total_volumes FROM series WHERE id=?", (series_id,)
        ).fetchone()
        if s_row and s_row['status'] == 'FINISHED' and s_row['total_volumes']:
            wanted_count = db.execute(
                "SELECT COUNT(*) FROM volumes WHERE series_id=? AND status='wanted'",
                (series_id,)
            ).fetchone()[0]
            total = s_row['total_volumes']
            # If we're missing ≥50% of the series, try a complete pack first
            if wanted_count >= total * 0.5:
                grabbed = await search_complete_pack(series_id, title, total)
                if grabbed > 0:
                    log_event('search',
                              f"Complete pack grabbed for finished series '{title}' — skipping individual search",
                              series_id)
                    return grabbed

    # ── Normal per-volume/release search ─────────────────────────────────────
    all_items = await _search_all(title)

    with get_db() as db:
        seen_urls    = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM seen").fetchall()}
        blocked_urls = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM blocklist").fetchall()}
        alias_rows   = db.execute(
            "SELECT alias FROM series_aliases WHERE series_id=?", (series_id,)
        ).fetchall()

    all_patterns = [pattern] + [a['alias'] for a in alias_rows]

    # Also search aliases that may differ significantly from the main title
    for alias in [a['alias'] for a in alias_rows]:
        extra = await _search_all(alias)
        for it in extra:
            if it['url'] not in {x['url'] for x in all_items}:
                all_items.append(it)

    grabbed = 0
    for item in all_items:
        if item['url'] in seen_urls or item['url'] in blocked_urls:
            continue
        if any(matches(p, item['title']) for p in all_patterns):
            if await grab_item(item, series_id):
                grabbed += 1
    log_event('search', f"Search '{title}': {len(all_items)} candidates, {grabbed} grabbed", series_id)
    return grabbed

def _select_covering_packs(
    items: list[dict],
    missing_vols: set[float],
    total_volumes: int | None,
    all_patterns: list[str],
) -> list[dict]:
    """
    Greedy non-overlapping selection of complete/range packs that maximises
    coverage of missing_vols.  Sorted largest-coverage-first, then by seeders.
    Returns ordered list of packs to grab (non-overlapping by volume range).
    """
    candidates = []
    for item in items:
        if not any(matches(p, item['title']) for p in all_patterns):
            continue
        item_complete = is_complete_pack(item['title'], total_volumes)
        rng = extract_volume_range(item['title'])
        if item_complete:
            covered = set(missing_vols)  # treat as covering everything
        elif rng:
            covered = {v for v in missing_vols if rng[0] <= v <= rng[1]}
        else:
            continue  # single volume — handled in gap-fill phase
        if not covered:
            continue
        candidates.append((len(covered), rng, item, covered))

    # Sort by coverage desc, then seeders desc
    candidates.sort(key=lambda x: (x[0], x[2].get('seeders', 0)), reverse=True)

    selected: list[dict] = []
    claimed:  set[float] = set()
    for _coverage, _rng, item, covered in candidates:
        newly = covered - claimed
        if not newly:
            continue
        selected.append(item)
        claimed |= newly
        if claimed >= missing_vols:
            break
    return selected


async def search_complete_pack(series_id: int, title: str,
                               total_volumes: int | None) -> int:
    """
    Search all sources specifically for complete series packs.
    Searches the main title AND all aliases (critical for series whose Nyaa/indexer
    releases use the romaji/Japanese title instead of the English title).
    Only grabs items identified as complete or near-complete packs.
    Returns number of items grabbed.
    """
    with get_db() as db:
        seen_urls    = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM seen").fetchall()}
        blocked_urls = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM blocklist").fetchall()}
        alias_rows   = db.execute(
            "SELECT alias FROM series_aliases WHERE series_id=?", (series_id,)
        ).fetchall()
    aliases = [a['alias'] for a in alias_rows]
    all_patterns = [title] + aliases

    # Only use aliases that are useful for searching:
    # Latin-script, not flagged as a foreign-language title, and meaningfully different
    def _useful_search_term(term: str) -> bool:
        if not term or len(term) < 3:
            return False
        if is_foreign_language(term):
            return False
        latin = len(re.findall(r'[a-zA-Z]', term))
        return latin >= max(1, len(term.replace(' ', '')) * 0.5)

    # Deduplicated list of search terms: main title first, then useful aliases.
    # Sort useful aliases by how DIFFERENT they are from the main title — the most
    # lexically dissimilar aliases (e.g. romaji "Shingeki no Kyojin") come first
    # because they're most likely to surface results the main-title search missed.
    norm_title = normalize(title)
    useful_aliases = [
        a for a in aliases
        if _useful_search_term(a) and normalize(a) != norm_title
    ]
    useful_aliases.sort(
        key=lambda a: difflib.SequenceMatcher(None, norm_title, normalize(a)).ratio()
    )  # ascending: lowest similarity (most different) first

    search_terms: list[str] = [title] + useful_aliases

    # Cap at 8 terms — run sequentially so we don't flood the indexer
    search_terms = search_terms[:8]

    end_str = f"v01-v{int(total_volumes):02d}" if total_volumes else None

    # Search sequentially per term (base + complete variant) to avoid rate-limiting
    seen_item_urls: set[str] = set()
    all_items: list[dict] = []

    async def _add_results(query: str):
        for item in await _search_all(query):
            if item['url'] not in seen_item_urls:
                seen_item_urls.add(item['url'])
                all_items.append(item)

    for term in search_terms:
        await _add_results(term)
        await _add_results(f"{term} complete")
        if end_str:
            await _add_results(f"{term} {end_str}")

    # Fetch currently wanted volumes for gap analysis
    with get_db() as db:
        missing_vols: set[float] = {
            float(r['volume_num'])
            for r in db.execute(
                "SELECT volume_num FROM volumes WHERE series_id=? AND status='wanted'"
                " AND volume_num IS NOT NULL",
                (series_id,)
            ).fetchall()
        }

    available = [i for i in all_items
                 if i['url'] not in seen_urls and i['url'] not in blocked_urls]

    # Phase 1: greedy non-overlapping pack selection
    packs_to_grab = _select_covering_packs(
        available, missing_vols or set(range(1, (total_volumes or 1) + 1)),
        total_volumes, all_patterns
    )

    grabbed = 0
    for item in packs_to_grab:
        if await grab_item(item, series_id):
            grabbed += 1

    # Phase 2: gap-fill — identify volumes not covered by any selected pack
    claimed_by_packs: set[float] = set()
    for item in packs_to_grab:
        if is_complete_pack(item['title'], total_volumes):
            claimed_by_packs |= missing_vols
        else:
            rng = extract_volume_range(item['title'])
            if rng:
                claimed_by_packs |= {v for v in missing_vols if rng[0] <= v <= rng[1]}
    gaps = missing_vols - claimed_by_packs

    # Cap gap-fill to 10 individual searches to avoid flooding the indexer
    gap_grabbed = 0
    for vol_num in sorted(gaps)[:10]:
        query = f"{title} vol {int(vol_num)}"
        for item in await _search_all(query):
            if item['url'] in seen_urls or item['url'] in blocked_urls:
                continue
            if not any(matches(p, item['title']) for p in all_patterns):
                continue
            item_vol = extract_volume_num(item['title'])
            if item_vol is not None and abs(item_vol - vol_num) < 0.02:
                if await grab_item(item, series_id):
                    gap_grabbed += 1
                break
    grabbed += gap_grabbed

    title_matched = sum(1 for item in all_items
                        if any(matches(p, item['title']) for p in all_patterns))
    n_queries = len(search_terms) * (3 if end_str else 2) + len(gaps)
    print(f"[CompleteSearch] '{title}': {n_queries} queries ({len(search_terms)} terms), "
          f"{len(all_items)} raw candidates, {title_matched} title-matched, "
          f"{len(packs_to_grab)} packs + {gap_grabbed} gaps = {grabbed} grabbed")
    log_event('search',
              f"Complete pack search '{title}': {len(all_items)} candidates "
              f"({title_matched} matched), {grabbed} grabbed",
              series_id)
    return grabbed


async def poll_rss():
    """Poll all enabled DB indexers for new releases."""
    from routers.indexers import fetch_all_rss as _fetch_all_rss_db
    with get_db() as _rdb:
        items = await _fetch_all_rss_db(_rdb)
    source = 'Indexers'
    if not items:
        return

    # Global fallback delay (still used if delay profiles return 0)
    _global_delay = max(0, int(get_cfg('grab_delay_minutes', '0') or '0'))
    now_ts        = datetime.utcnow()

    with get_db() as db:
        series_list = [dict(r) for r in db.execute(
            "SELECT id, title, search_pattern, pub_year, edition_type FROM series WHERE enabled=1 AND monitored=1"
        ).fetchall()]
        seen_urls  = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM seen").fetchall()}
        blocked_urls = {r['torrent_url'] for r in db.execute("SELECT torrent_url FROM blocklist").fetchall()}
        # Build alias lookup: series_id → [alias, ...]
        alias_map: dict[int, list[str]] = {}
        for row in db.execute("SELECT series_id, alias FROM series_aliases").fetchall():
            alias_map.setdefault(row['series_id'], []).append(row['alias'])

    grabbed = 0
    for item in items:
        if not item['url'] or item['url'] in seen_urls or item['url'] in blocked_urls:
            continue
        if is_foreign_language(item['title']):
            continue
        for s in series_list:
            all_patterns = list({s['title'], s['search_pattern']} | set(alias_map.get(s['id'], [])))
            pub_year = s['pub_year']
            if not any(matches(p, item['title'], pub_year=pub_year) for p in all_patterns):
                continue

            # Determine effective delay from delay profiles or global fallback
            try:
                from routers.delay_profiles import get_delay_for_series
                with get_db() as _ddb:
                    delay_minutes = get_delay_for_series(_ddb, s['id'], item.get('protocol', 'torrent'))
                if delay_minutes == 0:
                    delay_minutes = _global_delay
            except Exception:
                delay_minutes = _global_delay

            if delay_minutes < 0:
                # Protocol explicitly disabled by delay profile for this series — skip
                break

            if delay_minutes > 0:
                # Insert or ignore into pending_releases; grab when delay elapses
                with get_db() as db2:
                    existing_pr = db2.execute(
                        "SELECT first_seen FROM pending_releases WHERE series_id=? AND url=?",
                        (s['id'], item['url'])
                    ).fetchone()
                    if not existing_pr:
                        db2.execute(
                            "INSERT OR IGNORE INTO pending_releases"
                            "(series_id, url, title, indexer, protocol, size_bytes)"
                            " VALUES(?,?,?,?,?,?)",
                            (s['id'], item['url'], item['title'],
                             item.get('indexer', ''), item.get('protocol', 'torrent'),
                             item.get('size_bytes', 0))
                        )
                    else:
                        elapsed = (now_ts - datetime.fromisoformat(
                            existing_pr['first_seen'].replace('Z', '')
                        )).total_seconds() / 60
                        if elapsed >= delay_minutes:
                            if await grab_item(item, s['id']):
                                grabbed += 1
                                seen_urls.add(item['url'])
                                with get_db() as db3:
                                    db3.execute(
                                        "DELETE FROM pending_releases WHERE series_id=? AND url=?",
                                        (s['id'], item['url'])
                                    )
            else:
                if await grab_item(item, s['id']):
                    grabbed += 1
                    seen_urls.add(item['url'])
            break

    # Expire stale pending_releases (older than 7 days)
    with get_db() as db:
        db.execute(
            "DELETE FROM pending_releases WHERE first_seen < datetime('now', '-7 days')"
        )

    log_event('rss_poll', f"{source} RSS: {len(items)} items checked, {grabbed} grabbed")

# ── Scheduler ─────────────────────────────────────────────────────────────────
_rss_task:     asyncio.Task | None = None
_status_task:  asyncio.Task | None = None
_refresh_task: asyncio.Task | None = None

async def rss_loop():
    from routers.system import update_task_state
    await asyncio.sleep(5)  # brief startup delay to let lifespan complete
    while True:
        try:
            await poll_rss()
        except Exception as e:
            log_event('error', f"RSS poll error: {e}")
        interval = max(60, int(get_cfg('rss_interval', '900')))
        now = datetime.now(timezone.utc)
        update_task_state('RssSyncAll', last_run=now,
                          next_run=datetime.fromtimestamp(now.timestamp() + interval, tz=timezone.utc))
        await asyncio.sleep(interval)

async def status_loop():
    """Check download completion every 5 minutes."""
    from routers.system import update_task_state
    await asyncio.sleep(60)  # initial delay
    while True:
        try:
            await check_download_status()
        except Exception as e:
            log_event('error', f"Download status check error: {e}")
        now = datetime.now(timezone.utc)
        update_task_state('CheckDownloads', last_run=now,
                          next_run=datetime.fromtimestamp(now.timestamp() + 300, tz=timezone.utc))
        await asyncio.sleep(300)

_THROTTLED_REFRESH_DAYS = 7   # how many days between refreshes for 'throttled' series

async def refresh_ongoing_loop():
    """Daily: check AniList for new volumes on RELEASING series, respecting per-series update_strategy."""
    await asyncio.sleep(300)  # initial delay
    while True:
        try:
            interval = max(3600, int(get_cfg('refresh_interval', '86400')))
            with get_db() as db:
                # Include RELEASING and any series explicitly set to 'always' or 'throttled'
                # ('once' series are auto-skipped below)
                candidates = db.execute(
                    "SELECT * FROM series WHERE UPPER(status) IN ('RELEASING','HIATUS')"
                    " AND anilist_id IS NOT NULL AND monitored=1"
                ).fetchall()
            updated = 0
            now_utc = datetime.utcnow()
            for s in candidates:
                strategy = (s['update_strategy'] or 'always') if 'update_strategy' in s.keys() else 'always'

                # ── Update strategy filter ────────────────────────────────────
                if strategy == 'once':
                    # 'once' = manual-only; skip auto-refresh entirely
                    continue
                elif strategy == 'throttled':
                    last_refresh = s['last_metadata_refresh'] if 'last_metadata_refresh' in s.keys() else None
                    if last_refresh:
                        try:
                            last_dt = datetime.fromisoformat(last_refresh)
                            if (now_utc - last_dt).days < _THROTTLED_REFRESH_DAYS:
                                continue   # too soon
                        except ValueError:
                            pass
                # 'always' → fall through

                results = await anilist_search(s['title'])
                match = next((r for r in results if r['anilist_id'] == s['anilist_id']), None)
                if not match:
                    continue
                new_vols   = match.get('volumes') or 0
                old_vols   = s['total_volumes'] or 0
                new_status = match.get('status', s['status'])
                with get_db() as db:
                    # Always stamp last_metadata_refresh even if no data changed
                    db.execute(
                        "UPDATE series SET last_metadata_refresh=? WHERE id=?",
                        (now_utc.isoformat(), s['id'])
                    )
                    if new_vols > old_vols or new_status != s['status']:
                        db.execute(
                            "UPDATE series SET total_volumes=?, status=?,"
                            " vol_count_source=CASE WHEN COALESCE(vol_count_source,'anilist')"
                            " IN ('google_books','wikipedia','manual') THEN vol_count_source ELSE 'anilist' END"
                            " WHERE id=?",
                            (new_vols or None, new_status, s['id'])
                        )
                        if new_vols > old_vols and (s['edition_type'] or 'standard') not in _NON_STANDARD_STUB_EDITIONS:
                            create_volume_stubs(db, s['id'], new_vols)
                        # Auto-switch to 'once' when a series finishes — no need to keep polling
                        if new_status in ('FINISHED', 'CANCELLED') and strategy == 'always':
                            db.execute(
                                "UPDATE series SET update_strategy='once' WHERE id=?", (s['id'],)
                            )
                        log_event('refresh',
                                  f"Auto-refresh: {old_vols}→{new_vols} vols, status={new_status}",
                                  s['id'])
                        updated += 1
                await asyncio.sleep(1)  # rate-limit AniList requests
            if updated:
                log_event('refresh', f"Auto-refresh complete: {updated} series updated")
        except Exception as e:
            print(f"[Refresh] Error: {e}")
        from routers.system import update_task_state
        now = datetime.now(timezone.utc)
        update_task_state('RefreshMetadata', last_run=now,
                          next_run=datetime.fromtimestamp(now.timestamp() + interval, tz=timezone.utc))
        await asyncio.sleep(interval)

async def _backfill_metadata_loop():
    """
    At startup, backfill MangaDex ID + cross-references (MAL/MU) for series missing them.
    Runs once, with a small delay between each to respect MangaDex rate limits (~5 req/s).
    """
    await asyncio.sleep(10)  # let startup settle first
    with get_db() as db:
        missing = db.execute(
            "SELECT id FROM series WHERE mangadex_id IS NULL OR mal_id IS NULL OR mu_id IS NULL"
            " OR (mangadex_id IS NOT NULL AND chapter_vol_map IS NULL)"
        ).fetchall()
    for row in missing:
        try:
            await refresh_mangadex_map(row['id'])
        except Exception as e:
            print(f"[Startup] metadata backfill error for series {row['id']}: {e}")
        await asyncio.sleep(2)  # ~0.5 req/s — well under MangaDex limit

    # Sync MangaDex chapter manifests for series that have mangadex_id but no chapter rows
    with get_db() as db:
        needs_sync = db.execute(
            "SELECT id FROM series WHERE mangadex_id IS NOT NULL"
            " AND NOT EXISTS (SELECT 1 FROM mangadex_chapters m WHERE m.series_id=series.id)"
        ).fetchall()
    for row in needs_sync:
        try:
            await _mdx_router.sync_mangadex_chapters(row['id'])
        except Exception as e:
            print(f"[Startup] MangaDex chapter sync error for series {row['id']}: {e}")
        await asyncio.sleep(1.5)

async def backlog_search_loop():
    """Daily: actively search for all wanted volumes that RSS may have missed."""
    await asyncio.sleep(600)   # initial delay — let startup settle
    while True:
        try:
            interval = 86400  # 24 hours
            ddl_only  = get_cfg('ddl_grab_mode', 'fallback') == 'only'
            with get_db() as db:
                wanted_series = db.execute(
                    "SELECT DISTINCT s.id, s.title, s.search_pattern, s.mangadex_id FROM series s"
                    " JOIN volumes v ON v.series_id=s.id"
                    " WHERE s.monitored=1 AND v.status='wanted'"
                ).fetchall()
            searched = 0
            if ddl_only:
                from routers.suwayomi_ import _get_series_source
            for s in wanted_series:
                # In DDL-only mode, skip indexer search for series tracked via Suwayomi/MangaDex
                if ddl_only and _get_series_source(s['id'], dict(s)):
                    continue
                try:
                    grabbed = await grab_existing(s['id'], s['title'], s['search_pattern'])
                    if grabbed:
                        searched += grabbed
                except Exception as e:
                    import traceback
                    print(f"[Backlog] Error searching {s['title']}: {e}")
                    print(traceback.format_exc())
                await asyncio.sleep(2)  # rate-limit: ~0.5 series/sec
            if wanted_series:
                log_event('backlog_search', f"Backlog search complete: {len(wanted_series)} series, {searched} grabbed")
        except Exception as e:
            print(f"[Backlog] Error: {e}")
        from routers.system import update_task_state
        now = datetime.now(timezone.utc)
        update_task_state('BacklogSearch', last_run=now,
                          next_run=datetime.fromtimestamp(now.timestamp() + interval, tz=timezone.utc))
        await asyncio.sleep(interval)


async def backlog_search():
    """One-shot backlog search — search all wanted volumes once. Used by task scheduler 'Run Now'."""
    with get_db() as db:
        wanted_series = db.execute(
            "SELECT DISTINCT s.id, s.title, s.search_pattern FROM series s"
            " JOIN volumes v ON v.series_id=s.id"
            " WHERE s.monitored=1 AND v.status='wanted'"
        ).fetchall()
    searched = 0
    for s in wanted_series:
        try:
            grabbed = await grab_existing(s['id'], s['title'], s['search_pattern'])
            if grabbed:
                searched += grabbed
        except Exception as e:
            print(f"[Backlog] Error searching {s['title']}: {e}")
        await asyncio.sleep(2)
    if wanted_series:
        log_event('backlog_search', f"Backlog search: {len(wanted_series)} series, {searched} grabbed")


async def import_list_sync():
    """One-shot import list sync — sync all enabled import lists once. Used by task scheduler 'Run Now'."""
    try:
        from routers.import_lists import _sync_all_lists as _do_sync
        await _do_sync()
        log_event('import_list_sync', "Import list sync completed")
    except Exception as e:
        log_event('error', f"Import list sync failed: {e}")
        print(f"[ImportListSync] {e}")


async def rescan_loop():
    """Periodic library rescan — walks all series folders and reconciles on-disk state."""
    interval_h = int(get_cfg('rescan_interval_hours', '12'))
    # Delay first run so startup tasks finish before hammering disk
    await asyncio.sleep(interval_h * 3600)
    while True:
        try:
            await _rescan_all_impl()
        except Exception as e:
            log_event('error', f"Periodic rescan error: {e}")
        await asyncio.sleep(interval_h * 3600)


async def _import_list_loop():
    """Periodic import list sync — runs every 12 hours."""
    await asyncio.sleep(300)  # 5 min delay after startup
    while True:
        try:
            from routers.import_lists import _sync_all_lists
            await _sync_all_lists()
            log_event('import_list_sync', "Scheduled import list sync completed")
        except Exception as e:
            log_event('error', f"Import list sync error: {e}")
        from routers.system import update_task_state
        now = datetime.now(timezone.utc)
        update_task_state('ImportListSync', last_run=now,
                          next_run=datetime.fromtimestamp(now.timestamp() + 43200, tz=timezone.utc))
        await asyncio.sleep(43200)  # 12 hours


async def _backup_loop():
    """Auto-backup — interval and retention controlled by settings."""
    from routers.system import BACKUP_DIR, update_task_state
    await asyncio.sleep(3600)  # 1h delay after startup
    while True:
        interval_days = max(1, min(30, int(get_cfg('backup_interval_days', '1') or 1)))
        retention     = max(1, min(30, int(get_cfg('backup_retention',     '7') or 7)))
        try:
            os.makedirs(BACKUP_DIR, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fname = f"mangarr_auto_{ts}.zip"
            fpath = os.path.join(BACKUP_DIR, fname)
            with zipfile.ZipFile(fpath, 'w', zipfile.ZIP_DEFLATED) as zf:
                zf.write(DB_PATH, "mangarr.db")
            # Keep only last N auto backups
            auto_backups = sorted(
                [f for f in os.listdir(BACKUP_DIR)
                 if f.startswith('mangarr_auto_') and f.endswith('.zip')],
                reverse=True
            )
            for old in auto_backups[retention:]:
                try:
                    os.remove(os.path.join(BACKUP_DIR, old))
                except Exception:
                    pass
            now = datetime.now(timezone.utc)
            update_task_state('Backup', last_run=now,
                              next_run=datetime.fromtimestamp(now.timestamp() + interval_days * 86400, tz=timezone.utc))
            log_event('backup', f"Auto-backup created: {fname} (retaining last {retention})")
        except Exception as e:
            log_event('error', f"Auto-backup failed: {e}")
        await asyncio.sleep(interval_days * 86400)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _rss_task, _status_task, _refresh_task
    init_db()
    load_config()
    # Defense in depth: if api_key is still blank after init_db + load_config
    # (DB row nulled, partial migration, etc.), generate one now. The
    # middleware fails closed on blank api_key, so the alternative is the
    # whole API returning 401 until an operator notices.
    ensure_api_key()
    backfill_pack_ranges()
    # Create qBit manga category on startup
    try:
        from routers.download_clients import get_client_for_protocol
        with get_db() as _cdb:
            _qc = get_client_for_protocol(_cdb, 'torrent')
        _qhost = ((_qc or {}).get('host') or '').rstrip('/')
        _quser = ((_qc or {}).get('username') or '')
        _qpw   = ((_qc or {}).get('password') or '')
        _qcat  = ((_qc or {}).get('category') or get_cfg('category'))
        if _qhost:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    f"{_qhost}/api/v2/auth/login",
                    data={'username': _quser, 'password': _qpw}
                )
                if 'Ok' in r.text:
                    await client.post(
                        f"{_qhost}/api/v2/torrents/createCategory",
                        data={'category': _qcat, 'savePath': get_cfg('save_path')}
                    )
    except Exception:
        pass
    _rss_task     = asyncio.create_task(rss_loop())
    _status_task  = asyncio.create_task(status_loop())
    _refresh_task = asyncio.create_task(refresh_ongoing_loop())
    asyncio.create_task(_backfill_metadata_loop())
    asyncio.create_task(backlog_search_loop())
    asyncio.create_task(_swy_router.suwayomi_monitor_loop())
    asyncio.create_task(rescan_loop())
    asyncio.create_task(_import_list_loop())
    asyncio.create_task(_backup_loop())
    # Re-process any import_queue entries that were left 'pending' from a previous
    # run (e.g. app restarted mid-import). Only retry entries with no needs_review files.
    with get_db() as _db:
        _stuck = _db.execute(
            "SELECT iq.id FROM import_queue iq"
            " WHERE iq.status='pending'"
            " AND NOT EXISTS ("
            "   SELECT 1 FROM import_queue_files f"
            "   WHERE f.queue_id=iq.id AND f.status='needs_review'"
            ")"
        ).fetchall()
        _stuck_ids = [r[0] for r in _stuck]
        # Reset grabbed volumes with no download_id that somehow persisted through shutdown
        _db.execute(
            "UPDATE volumes SET status='wanted', grabbed_at=NULL, source_url=NULL,"
            " download_id=NULL, torrent_name=NULL, indexer=NULL, protocol=NULL,"
            " client=NULL, release_group=NULL, imported_at=NULL"
            " WHERE status='grabbed' AND download_id IS NULL AND volume_num IS NOT NULL"
            " AND (client IS NULL OR client != 'suwayomi')"
        )
        _db.execute(
            "DELETE FROM volumes WHERE status='grabbed' AND download_id IS NULL AND volume_num IS NULL"
        )
    # Defer stuck-import retries until after startup completes to avoid blocking
    # the event loop (since _execute_import is fully synchronous).
    async def _retry_stuck():
        await asyncio.sleep(5)
        for _qid in _stuck_ids:
            await _process_auto_import(_qid)
    if _stuck_ids:
        asyncio.create_task(_retry_stuck())
    yield
    _rss_task.cancel()
    _status_task.cancel()
    _refresh_task.cancel()

# ── Helpers ───────────────────────────────────────────────────────────────────
def get_root_folders(db) -> list:
    return db.execute("SELECT * FROM root_folders ORDER BY is_default DESC, label, path").fetchall()

def get_series_stats(db, series_id: int) -> dict:
    """Stats are based only on volume stubs (not pack entries)."""
    rows = db.execute(
        "SELECT status FROM volumes WHERE series_id=? AND volume_num IS NOT NULL",
        (series_id,)
    ).fetchall()
    total      = len(rows)
    wanted     = sum(1 for r in rows if r['status'] == 'wanted')
    grabbed    = sum(1 for r in rows if r['status'] == 'grabbed')
    downloaded = sum(1 for r in rows if r['status'] == 'downloaded')
    return {
        'total': total, 'wanted': wanted,
        'grabbed': grabbed, 'downloaded': downloaded,
        'have': grabbed + downloaded,
    }

def format_bytes(n) -> str:
    if not n:
        return ''
    n = int(n)
    for unit in ['B', 'KB', 'MB', 'GB']:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"

def format_protocol(p: str) -> str:
    if not p:
        return ''
    return {'torrent': 'Torrent', 'nzb': 'NZB', 'ddl': 'DDL'}.get(p, p)

def format_client(c: str) -> str:
    if not c:
        return ''
    return {'qbittorrent': 'qBittorrent', 'sabnzbd': 'SABnzbd', 'suwayomi': 'Suwayomi'}.get(c, c)

# ── Cover image helpers ───────────────────────────────────────────────────────
os.makedirs('/config/covers', exist_ok=True)

async def download_cover(series_id: int, cover_url: str):
    """Download cover from URL and save to /config/covers/{series_id}.jpg"""
    if not cover_url:
        return
    dest = f"/config/covers/{series_id}.jpg"
    if os.path.exists(dest):
        return  # already have a cover
    from security import validate_outbound_url, UnsafeURLError
    try:
        validate_outbound_url(cover_url)
    except UnsafeURLError as e:
        print(f"[Cover] URL rejected for series {series_id}: {e}")
        return
    # follow_redirects=False: a public hostname could 30x to a private IP
    # and bypass the validation above. AniList/MangaDex serve covers from
    # direct CDN URLs, so disabling redirects has no impact in practice.
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            r = await client.get(cover_url)
            if r.status_code == 200:
                with open(dest, 'wb') as f:
                    f.write(r.content)
    except Exception as e:
        print(f"[Cover] download error for series {series_id}: {e}")

def extract_cbz_cover(series_id: int, cbz_path: str):
    """Extract first image from CBZ and save as cover if none exists."""
    dest = f"/config/covers/{series_id}.jpg"
    if os.path.exists(dest):
        return
    try:
        with zipfile.ZipFile(cbz_path, 'r') as z:
            images = sorted([f for f in z.namelist()
                           if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))
                           and not f.startswith('__MACOSX')])
            if images:
                with z.open(images[0]) as img_file:
                    with open(dest, 'wb') as out:
                        out.write(img_file.read())
    except Exception as e:
        print(f"[Cover] CBZ extract error: {e}")

# ── App ───────────────────────────────────────────────────────────────────────
app       = FastAPI(lifespan=lifespan)
app.mount("/covers", StaticFiles(directory="/config/covers"), name="covers")
app.mount("/static", StaticFiles(directory="/app/static"),   name="static")
templates = Jinja2Templates(directory="/app/templates")

# ── API Key middleware ─────────────────────────────────────────────────────────
class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # Only protect /api/ routes
        if not path.startswith('/api/'):
            return await call_next(request)
        # Exempt SSE endpoint and health check
        if path in ('/api/queue-events', '/api/health'):
            return await call_next(request)
        # Exempt in-session browser requests on unsafe methods: the CSRF
        # middleware validates the form csrf_token against the cookie for
        # POST/PUT/DELETE/PATCH, so presence of the cookie alone is not
        # sufficient — the CSRF check enforces origin. This lets plain
        # <form action="/api/..."> submissions work from the web UI without
        # manual header injection. GET requests still require X-Api-Key so
        # read endpoints aren't exposed by someone sending a fake cookie.
        if (request.method in ('POST', 'PUT', 'DELETE', 'PATCH')
                and request.cookies.get('csrftoken')):
            return await call_next(request)
        # Check API key. Fail closed: if the configured key is blank/missing
        # (e.g. a bad import nulled the row, or someone cleared the setting
        # at runtime), refuse the request. ensure_api_key() runs at startup
        # to seed a fresh key on a healthy boot — if we still see blank here
        # something is wrong; do not silently expose the API.
        api_key = (get_cfg('api_key', '') or '').strip()
        if not api_key:
            if not getattr(ApiKeyMiddleware, '_warned_no_key', False):
                print("[ERROR] /api/ routes denied — settings.api_key is blank. "
                      "Restart the app to auto-seed, or set one via Settings.")
                ApiKeyMiddleware._warned_no_key = True
            return JSONResponse(
                {"message": "Unauthorized",
                 "description": "API key not configured on the server"},
                status_code=401
            )
        provided = (request.headers.get('X-Api-Key') or
                   request.query_params.get('apikey') or '')
        if provided != api_key:
            return JSONResponse(
                {"message": "Unauthorized", "description": "Invalid or missing API key"},
                status_code=401
            )
        return await call_next(request)

# ── CSRF middleware ────────────────────────────────────────────────────────────
_CSRF_COOKIE  = "csrftoken"
_CSRF_HEADER  = "X-CSRFToken"
_CSRF_FIELD   = "csrf_token"
_CSRF_SKIP_PREFIXES = ("/api/", "/static/", "/covers/")

class CSRFMiddleware:
    """Pure ASGI CSRF middleware.
    When the CSRF token must be read from a form body, we buffer the raw bytes
    and hand a replay-receive callable to the downstream app so the route
    handler can still parse the same body.  BaseHTTPMiddleware drains the
    receive channel and does not replay it, which caused 422 errors.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        from starlette.requests import Request as _Req
        from urllib.parse import parse_qs

        req = _Req(scope, receive)
        token = req.cookies.get(_CSRF_COOKIE) or secrets.token_hex(32)

        # Expose token for templates via request.state
        # Request.state property always returns a proper State() object backed by scope["state"]
        req.state.csrf_token = token

        path = scope.get("path", "")
        is_exempt = any(path.startswith(p) for p in _CSRF_SKIP_PREFIXES)
        method = scope.get("method", "GET")

        # Receive callable that will be forwarded to the app (may be replaced
        # with a replay version if we had to buffer the body for CSRF checking).
        forward_receive = receive

        if method not in ("GET", "HEAD", "OPTIONS", "TRACE") and not is_exempt:
            valid = False

            # 1. Header check — no body read needed
            hdr = ""
            for k, v in scope.get("headers", []):
                if k.lower() == b"x-csrftoken":
                    hdr = v.decode()
                    break
            if hdr and token:
                valid = hmac.compare_digest(token, hdr)

            # 2. Form-field check — must buffer body, then replay for route handler
            if not valid:
                ct = ""
                for k, v in scope.get("headers", []):
                    if k.lower() == b"content-type":
                        ct = v.decode().lower()
                        break

                if "urlencoded" in ct or "multipart" in ct:
                    # Drain the full body from the receive channel
                    chunks = []
                    while True:
                        msg = await receive()
                        body_chunk = msg.get("body", b"")
                        if body_chunk:
                            chunks.append(body_chunk)
                        if not msg.get("more_body", False):
                            break
                    raw_body = b"".join(chunks)

                    # Parse CSRF field without creating a Request (avoids re-draining)
                    try:
                        if "urlencoded" in ct:
                            params = parse_qs(raw_body.decode("latin-1"), keep_blank_values=True)
                            fv = params.get(_CSRF_FIELD, [""])[0]
                        else:
                            # multipart: fall back to a throwaway Request on the cached body
                            _replayed_once = False
                            async def _tmp_receive():
                                nonlocal _replayed_once
                                if not _replayed_once:
                                    _replayed_once = True
                                    return {"type": "http.request", "body": raw_body, "more_body": False}
                                return {"type": "http.disconnect"}
                            _tmp_req = _Req(scope, _tmp_receive)
                            fd = await _tmp_req.form()
                            fv = fd.get(_CSRF_FIELD, "")
                        if fv and token:
                            valid = hmac.compare_digest(token, fv)
                    except Exception:
                        pass

                    # Replace the receive callable so the route handler gets the body back
                    _replayed = False
                    async def _replay_receive():
                        nonlocal _replayed
                        if not _replayed:
                            _replayed = True
                            return {"type": "http.request", "body": raw_body, "more_body": False}
                        return {"type": "http.disconnect"}
                    forward_receive = _replay_receive

            if not valid:
                resp = JSONResponse({"detail": "CSRF token missing or invalid."}, status_code=403)
                await resp(scope, forward_receive, send)
                return

        # Wrap send to set the CSRF cookie on first response if not yet present
        has_cookie = bool(req.cookies.get(_CSRF_COOKIE))

        async def send_with_cookie(message):
            nonlocal has_cookie
            if not has_cookie and message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                cookie_val = f"{_CSRF_COOKIE}={token}; Path=/; SameSite=lax"
                headers.append((b"set-cookie", cookie_val.encode()))
                message = {**message, "headers": headers}
                has_cookie = True
            await send(message)

        await self.app(scope, forward_receive, send_with_cookie)

app.add_middleware(CSRFMiddleware)
app.add_middleware(ApiKeyMiddleware)

def _from_json(s):
    try:
        return json.loads(s) if s else {}
    except Exception:
        return {}

templates.env.filters['format_bytes']    = format_bytes
templates.env.filters['format_protocol'] = format_protocol
templates.env.filters['format_client']   = format_client
templates.env.filters['vol_display']     = vol_num_to_display
templates.env.filters['quality_rank']    = quality_rank
templates.env.filters['from_json']       = _from_json

def _get_api_key_global() -> str:
    try:
        return get_cfg('api_key', '')
    except Exception:
        return ''

templates.env.globals['get_api_key'] = _get_api_key_global

# ── Include Sonarr-parity routers ─────────────────────────────────────────────
app.include_router(_qp_router.router,  tags=["Quality Profiles"])
app.include_router(_qd_router.router,  tags=["Quality Definitions"])
app.include_router(_rp_router.router,  tags=["Release Profiles"])
app.include_router(_dp_router.router,  tags=["Delay Profiles"])
app.include_router(_dc_router.router,  tags=["Download Clients"])
app.include_router(_idx_router.router, tags=["Indexers"])
app.include_router(_cf_router.router,  tags=["Custom Formats"])
app.include_router(_nc_router.router,  tags=["Notifications"])
app.include_router(_il_router.router,  tags=["Import Lists"])
app.include_router(_se_router.router,   tags=["Series Editor"])
app.include_router(_lp_router.router,   tags=["Language Profiles"])
app.include_router(_sys_router.router,  tags=["System"])
app.include_router(_bl_router.router,    tags=["Blocklist"])
app.include_router(_hist_router.router,  tags=["History"])
app.include_router(_stg_router.router,   tags=["Settings"])
app.include_router(_queue_router.router,  tags=["Queue"])
app.include_router(_lib_router.router,   tags=["Library"])
app.include_router(_import_router.router, tags=["Import"])
app.include_router(_health_router.router, tags=["Health"])
app.include_router(_series_router.router, tags=["Series"])
app.include_router(_mdx_router.router,    tags=["MangaDex"])
app.include_router(_swy_router.router,    tags=["Suwayomi"])

# ── Server-Sent Events for real-time queue updates ────────────────────────────
_sse_subscribers: list[asyncio.Queue] = []

async def broadcast_queue_event(event: str, data: dict | None = None):
    """Push a queue update event to all connected SSE clients."""
    payload = json.dumps({'event': event, **(data or {})})
    dead = []
    for q in _sse_subscribers:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        try:
            _sse_subscribers.remove(q)
        except ValueError:
            pass

@app.get("/api/queue-events")
async def queue_events(request: Request):
    """SSE endpoint — queue page subscribes here for real-time updates."""
    from fastapi.responses import StreamingResponse
    q: asyncio.Queue = asyncio.Queue(maxsize=32)
    _sse_subscribers.append(q)
    async def generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=5)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            try:
                _sse_subscribers.remove(q)
            except ValueError:
                pass
    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def backfill_pack_ranges():
    """Retroactively parse ranges/complete from existing pack volumes and mark stubs."""
    with get_db() as db:
        # Process all packs — re-evaluate pack_type even if range was set
        packs = db.execute(
            "SELECT v.id, v.series_id, v.torrent_name, v.vol_range_start, v.vol_range_end, "
            "s.total_volumes, s.total_chapters, s.chapter_vol_map "
            "FROM volumes v LEFT JOIN series s ON s.id=v.series_id "
            "WHERE v.volume_num IS NULL AND v.torrent_name IS NOT NULL"
        ).fetchall()

        total_marked = 0
        now = datetime.utcnow().isoformat()
        for p in packs:
            name       = p['torrent_name']
            total_vols = p['total_volumes']
            total_chs  = p['total_chapters']
            ch_map: dict = {}
            if p['chapter_vol_map']:
                try:
                    ch_map = json.loads(p['chapter_vol_map'])
                except Exception:
                    pass

            vol_rng   = extract_volume_range(name)
            pack_type = detect_pack_type(name, vol_rng, total_vols)
            complete  = (pack_type == 'complete')

            # For chapter packs: clear stored vol_range (those were chapter numbers, not volumes)
            if pack_type == 'chapter':
                rng_start, rng_end = None, None
            elif vol_rng and pack_type == 'volume':
                rng_start, rng_end = vol_rng
            else:
                rng_start, rng_end = None, None

            db.execute(
                "UPDATE volumes SET vol_range_start=?, vol_range_end=?, pack_type=? WHERE id=?",
                (rng_start, rng_end, pack_type, p['id'])
            )

            if complete:
                cur = db.execute(
                    "UPDATE volumes SET status='grabbed', grabbed_at=?, torrent_name=? "
                    "WHERE series_id=? AND status='wanted' AND volume_num IS NOT NULL",
                    (now, name, p['series_id'])
                )
                total_marked += cur.rowcount
            elif pack_type == 'volume' and vol_rng:
                cur = db.execute(
                    "UPDATE volumes SET status='grabbed', grabbed_at=?, torrent_name=? "
                    "WHERE series_id=? AND status='wanted' "
                    "AND volume_num IS NOT NULL "
                    "AND volume_num >= ? AND volume_num <= ?",
                    (now, name, p['series_id'], rng_start, rng_end)
                )
                total_marked += cur.rowcount
            elif pack_type == 'chapter':
                # Map chapter range → volume stubs using MangaDex map or approximation
                if vol_rng:
                    covered = chapters_to_volume_set(vol_rng[0], vol_rng[1], ch_map, total_chs, total_vols)
                else:
                    single_m = re.search(r'(?:^|[\s\[({])(\d{2,4})(?:[\s\])}]|$)', name)
                    covered = set()
                    if single_m:
                        ch = float(single_m.group(1))
                        covered = chapters_to_volume_set(ch, ch, ch_map, total_chs, total_vols)
                if covered:
                    placeholders = ','.join('?' * len(covered))
                    cur = db.execute(
                        f"UPDATE volumes SET status='grabbed', grabbed_at=?, torrent_name=? "
                        f"WHERE series_id=? AND status='wanted' "
                        f"AND volume_num IS NOT NULL "
                        f"AND CAST(volume_num AS INTEGER) IN ({placeholders})",
                        [now, name, p['series_id'], *covered]
                    )
                    total_marked += cur.rowcount
    return total_marked

