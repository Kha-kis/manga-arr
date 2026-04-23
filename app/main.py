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
# Use shared.get_db as the single source of truth for connection config
# (busy_timeout, synchronous=NORMAL, cache, mmap). Previously main.py had
# its own thinner get_db() missing those PRAGMAs, which left every
# background-loop write running with synchronous=FULL + default cache.
# Under contention that produced 15–60s event-loop stalls visible to
# unrelated HTTP requests (issue #31).
from shared import is_htmx, is_boosted, get_db as _shared_get_db

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

# ── Config schema + encryption migrations moved to config.py ────────────────
# ENV_DEFAULTS, SETTINGS_SECRET_KEYS, SETTINGS_VALIDATORS, _validate_setting_value,
# TABLE_SECRET_COLUMNS, NOTIFICATION_SECRET_KEYS_BY_TYPE, and the three
# migrate_encrypt_* helpers all live in config.py now. Re-exported so
# load_config / ensure_api_key / lifespan keep working unchanged.
from config import (  # noqa: F401
    ENV_DEFAULTS,
    SETTINGS_SECRET_KEYS,
    SETTINGS_VALIDATORS,
    _validate_setting_value,
    TABLE_SECRET_COLUMNS,
    NOTIFICATION_SECRET_KEYS_BY_TYPE,
    migrate_encrypt_settings_secrets,
    migrate_encrypt_table_column_secrets,
    migrate_encrypt_notification_connection_secrets,
)

def load_config():
    global CONFIG
    cfg = {}
    for key, (env_var, default) in ENV_DEFAULTS.items():
        cfg[key] = os.getenv(env_var, default) if env_var else default
    try:
        with get_db() as db:
            for row in db.execute("SELECT key, value FROM settings").fetchall():
                k = row['key']
                v = row['value']
                # For keys with semantic constraints, validate; fall back
                # to the ENV_DEFAULTS default on failure.
                if k in SETTINGS_VALIDATORS:
                    default_for_key = ENV_DEFAULTS.get(k, (None, ''))[1]
                    v = _validate_setting_value(k, v, default_for_key)
                cfg[k] = v  # load ALL settings keys, not just ENV_DEFAULTS
    except Exception as e:
        # Swallowing is intentional — fresh-install path before init_db has
        # created the settings table, and worker restarts that race with
        # schema migration. But silent silence makes real DB corruption
        # invisible (user sees default config with no clue why). Log at
        # WARNING so it's visible without spamming hot paths.
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "load_config: could not read settings from DB, using env/defaults: %r", e,
        )
    # Decrypt secret keys in-place. Plaintext values pass through; only
    # enc:v1: values are decrypted. If decryption fails (wrong key,
    # corruption), the secret becomes empty in CONFIG and a WARNING is
    # logged naming the field — caller-side code (api-key middleware,
    # Komga test, etc.) sees "no credential" and degrades gracefully
    # instead of crashing the whole app.
    import logging as _logging
    _log = _logging.getLogger(__name__)
    from security import decrypt_secret, SecretDecryptionError, SecretCipherUnavailable
    for k in SETTINGS_SECRET_KEYS:
        v = cfg.get(k)
        if not v:
            continue
        try:
            cfg[k] = decrypt_secret(v)
        except SecretDecryptionError:
            _log.warning(
                "load_config: settings.%s could not be decrypted — "
                "treating as unavailable; re-enter via Settings to fix", k,
            )
            cfg[k] = ""
        except SecretCipherUnavailable:
            # Cipher not initialised yet OR cryptography missing. Leave the
            # value as-is — it's either plaintext (back-compat) or
            # enc:v1:... that will be readable once cipher loads. For
            # enc:v1: values where cipher truly can't be loaded, the
            # api-key middleware will fail closed (H2 behaviour).
            pass
    CONFIG = cfg
    # Sync to shared module so routers can call shared.get_cfg()
    _shared.CONFIG.clear()
    _shared.CONFIG.update(cfg)
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
    # Lazy import: this module-level helper runs before the security
    # module is fully imported during init paths in some test harnesses.
    from security import encrypt_if_cipher_available, decrypt_secret
    with get_db() as db:
        row = db.execute("SELECT value FROM settings WHERE key='api_key'").fetchone()
        raw = (row['value'] if row else '') or ''
        # `raw` may be plaintext (legacy / pre-migration) or enc:v1: (post-PR-#2).
        # We need a plaintext probe to know whether the row already has a
        # usable key — an encrypted-but-decryptable row is "set" and should
        # NOT trigger regeneration.
        try:
            existing_plain = decrypt_secret(raw) if raw else ''
        except Exception:
            # Encrypted but undecryptable (wrong key). Don't overwrite —
            # operator may want to recover it. Return empty so the
            # middleware fails closed; operator sees the api-key as
            # unavailable in the UI and can re-enter.
            log.warning("ensure_api_key: existing api_key could not be decrypted; "
                        "leaving in place. Re-enter via Settings if needed.")
            return ''
        if existing_plain.strip():
            return existing_plain
        # No key yet — generate, encrypt if possible, persist.
        new_key = _secrets.token_hex(32)
        stored_value = encrypt_if_cipher_available(new_key)
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('api_key',?)",
                   (stored_value,))
    # CONFIG holds plaintext (decrypt happens in load_config; here we
    # already have the plaintext in `new_key`).
    CONFIG['api_key'] = new_key
    _shared.CONFIG['api_key'] = new_key
    log.warning("Generated a new API key (settings.api_key was missing/blank); "
                "view it at Settings → General")
    return new_key

# ── Database ──────────────────────────────────────────────────────────────────
# get_db delegates to shared.get_db so every writer — routes *and* the
# background loops that live in this module — uses the same PRAGMA set
# (busy_timeout=5000, synchronous=NORMAL, WAL, 8MB cache, 64MB mmap).
# See the import block above for context.
get_db = _shared_get_db

# ── Schema moved to schema.py ────────────────────────────────────────────────
# init_db (CREATE TABLE / add_col / index / seed-default pass),
# _bootstrap_root_folders (legacy save_path → root_folders migration),
# and _migrate_schema_constraints (FK-constraint rebuild via PRAGMA
# user_version) all live in schema.py now. Re-exported so routers /
# tests / lifespan keep working unchanged.
from schema import (  # noqa: F401
    init_db,
    _bootstrap_root_folders,
    _migrate_schema_constraints,
    _SCHEMA_VERSION_FK_CONSTRAINTS,
)

# ── Event logging ─────────────────────────────────────────────────────────────
def log_event(event_type: str, message: str, series_id: int | None = None,
              *, db=None):
    """Insert a row into the events table.

    If `db` is provided, the INSERT is executed on that existing connection
    — use this when calling from inside an already-open write transaction
    (e.g. `_execute_import`, `_queue_import`) to avoid opening a second
    connection that would serialize behind the outer writer and burn the
    15-second SQLITE_BUSY timeout.

    If `db` is None, opens a fresh connection as before. Normal callers
    (loops, HTTP handlers, one-shot background tasks) should not pass db.

    Swallows exceptions either way — event logging is best-effort and must
    not break the caller.
    """
    try:
        if db is not None:
            db.execute(
                "INSERT INTO events(event_type, series_id, message) VALUES(?,?,?)",
                (event_type, series_id, message),
            )
        else:
            with get_db() as _db:
                _db.execute(
                    "INSERT INTO events(event_type, series_id, message) VALUES(?,?,?)",
                    (event_type, series_id, message),
                )
    except Exception:
        pass

# ── Volume / chapter stub helpers moved to volumes.py ───────────────────────
# create_volume_stubs, populate_chapters, _check_volume_completion,
# _cascade_chapters. Re-exported so routers / import pipeline / rescan
# call sites keep working unchanged.
from volumes import (  # noqa: F401
    create_volume_stubs, populate_chapters,
    _check_volume_completion, _cascade_chapters,
)
# ── Parsing helpers moved to parsing.py ──────────────────────────────────────
# Title parsing, volume/chapter extraction, matching, and pack detection
# live in parsing.py. Re-exported here so existing call sites keep
# working unchanged (routers and internal helpers call these by bare
# name). score_release / evaluate_release stay in main.py because
# they're DB-coupled; they'll migrate with the grab layer later.
from parsing import (  # noqa: F401
    normalize, is_foreign_language, matches, FUZZY_MATCH_THRESHOLD,
    _roman_to_int, _parse_vol_suffix, vol_num_to_display,
    extract_volume_num, extract_volume_range,
    extract_chapter_range, extract_chapter_num,
    is_complete_pack, detect_pack_type, is_special_release,
)


# ── Release scoring + evaluation moved to evaluation.py ─────────────────────
# score_release (grab-priority scoring with every filter layer),
# evaluate_release (structured evaluator for the search UI),
# _term_display, _term_match (profile term helpers),
# parse_size_bytes. Re-exported so grab / routers keep working.
from evaluation import (  # noqa: F401
    score_release, evaluate_release,
    _term_display, _term_match,
    parse_size_bytes,
)

# ── Download clients moved to clients.py ─────────────────────────────────────
# qBit / SAB / NZBget / blackhole adapters + grab_url dispatcher live in
# clients.py. Re-exported here so existing call sites keep working
# unchanged (routers and internal helpers call these by bare name).
from clients import (  # noqa: F401
    extract_magnet_hash,
    qbit_grab, qbit_remove,
    sab_grab, sab_remove,
    nzbget_grab, blackhole_grab,
    grab_url,
)


# ── Filename / release-metadata / file-type helpers moved to files.py ────────
# Pure functions for path safety, release title parsing, filename templating,
# quality tiering, edition/language detection, and CBR→CBZ conversion. Re-exported
# here so all existing call sites keep working unchanged.
from files import (  # noqa: F401
    MANGA_EXTENSIONS,
    sanitize_filename, safe_join_under,
    parse_release_group, parse_revision, detect_quality_from_title,
    build_volume_label, _format_chapter_num, build_chapter_label,
    _apply_format_tokens, build_filename,
    QUALITY_RANK, quality_from_filename, quality_rank,
    _EDITION_PATTERNS, _LANGUAGE_PATTERNS,
    detect_edition_type, detect_language,
    _OFFICIAL_PUBLISHER_PATTERNS, _OFFICIAL_RE,
    _FAN_GROUP_PATTERNS, _FAN_GROUP_RE,
    is_official_release, is_quality_fan_release, classify_source_type,
    _MAGIC_ZIP, _MAGIC_RAR4, _MAGIC_RAR5, _MAGIC_PDF,
    detect_file_type_magic,
    convert_cbr_to_cbz, _maybe_convert_to_cbz,
)


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


# ── ComicInfo.xml helpers moved to comicinfo.py ─────────────────────────────
# read_comic_info, build_comicinfo_xml, inject_comicinfo (magic-byte safe),
# and _try_inject_comicinfo (best-effort wrapper used by import pipeline).
# Re-exported so import-pipeline and rescan call sites keep working.
from comicinfo import (  # noqa: F401
    read_comic_info, build_comicinfo_xml,
    inject_comicinfo, _try_inject_comicinfo,
)

# ── Library-dir rescan moved to rescan.py ───────────────────────────────────
# _series_library_dir (path + folder_format resolution) and
# rescan_series_folder (disk ↔ DB reconciliation). Re-exported so
# routers / import pipeline / mangadex chapter-map fallback keep working.
from rescan import _series_library_dir, rescan_series_folder  # noqa: F401

# ── Notification dispatch + embeds moved to notifications.py ────────────────
# notify_discord (fan-out to all enabled connections), make_grab_embed,
# make_complete_embed, trigger_komga_scan. Re-exported so grab / import
# pipeline / rescan call sites keep working unchanged.
from notifications import (  # noqa: F401
    notify_discord, make_grab_embed, make_complete_embed, trigger_komga_scan,
)

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
        log_event('error', f"Import queue: no content_path for {torrent_name}", series_id, db=db)
        return None, False

    s = db.execute(
        "SELECT title, root_folder_id, chapter_vol_map, total_volumes FROM series WHERE id=?", (series_id,)
    ).fetchone()
    if not s:
        return None, False
    _total_vols = s['total_volumes'] if 'total_volumes' in s.keys() else None

    # ── Release-level parser signals (Stage 2) ───────────────────────────────
    # Computed once per release so each file-level decision can reference
    # them without re-parsing torrent_name every iteration. These feed
    # the new import_queue_files columns (proposed_pack_type,
    # proposed_is_special) so the review UI and _execute_import can see
    # the shape the parser inferred.
    _rel_vol_range  = extract_volume_range(torrent_name or '')
    _rel_chap_range = extract_chapter_range(torrent_name or '')
    _rel_is_special = is_special_release(torrent_name or '')
    _rel_pack_type  = detect_pack_type(torrent_name or '', _rel_vol_range, _total_vols)

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
        log_event('error', f"Import queue: content_path not found: {content_path}", series_id, db=db)
        return None, False

    dest_root = _resolve_series_dest_root(db, s['root_folder_id'], rf)
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
            log_event('import', f"Skipped foreign-language file: {fname}", series_id, db=db)
            continue

        proposed_vol        = extract_volume_num(fname)
        proposed_chap       = extract_chapter_num(fname)
        # Per-file range detection. Stage 1 made these mutually exclusive
        # with the single-value parsers, so vol_range ∧ vol_num and
        # chap_range ∧ chap_num can't both be set for the same file.
        file_vol_range      = extract_volume_range(fname)
        file_chap_range     = extract_chapter_range(fname)
        proposed_vol_rs: float | None = None
        proposed_vol_re: float | None = None
        proposed_chap_re: float | None = None
        if file_vol_range is not None:
            proposed_vol_rs, proposed_vol_re = file_vol_range
            proposed_vol  = None  # range owns this file
        if file_chap_range is not None:
            proposed_chap, proposed_chap_re = file_chap_range
        # Special / side-story marker: release-level override wins, but a
        # per-file marker (e.g. an extras subfolder) also flips the flag.
        proposed_is_special = int(_rel_is_special or is_special_release(fname))

        # ComicInfo.xml overrides filename-based detection for cbz/zip/cbr
        ext_lower = os.path.splitext(fname)[1].lower()
        if ext_lower in ('.cbz', '.zip'):
            ci = read_comic_info(src_path)
            if ci.get('volume') is not None:
                ci_vol = ci['volume']
                if ci_vol != proposed_vol:
                    log_event('import',
                        f"ComicInfo.xml: vol {proposed_vol} → {ci_vol} for {fname}",
                        series_id, db=db)
                    proposed_vol     = ci_vol
                    proposed_chap    = None   # <Volume> tag wins — treat as volume file
                    proposed_vol_rs  = None   # and clear any filename range detection
                    proposed_vol_re  = None
                    proposed_chap_re = None
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
                                        series_id, db=db)
                                proposed_vol     = ci_vol
                                proposed_chap    = None
                                proposed_vol_rs  = None
                                proposed_vol_re  = None
                                proposed_chap_re = None
                        elif _raw_num and proposed_chap is None:
                            ci_num = _parse_vol_suffix(_raw_num)
                            if ci_num is not None:
                                proposed_chap = ci_num
            except ImportError:
                pass  # rarfile not installed
            except Exception:
                pass

        # Classify: chapter file has a chapter num or chapter range,
        # volume file has a volume num or volume range. Ranges are the
        # stronger signal — they can only be inferred from an explicit
        # v#-v# / c#-c# pattern.
        has_chap_signal = proposed_chap is not None or proposed_chap_re is not None
        has_vol_signal  = proposed_vol  is not None or proposed_vol_re  is not None

        if has_chap_signal and not has_vol_signal:
            file_type = 'chapter'
            # Resolve parent volume from chapter→volume map if available.
            # For chapter ranges, key off the start chapter.
            _key_src = proposed_chap if proposed_chap is not None else proposed_chap_re
            if _key_src is not None:
                chap_key = str(int(_key_src)) if _key_src == int(_key_src) else str(_key_src)
                if chap_key in cvm:
                    proposed_vol = float(cvm[chap_key])
        else:
            file_type = 'volume'
            # Discard spurious chapter detection for volume files.
            proposed_chap    = None
            proposed_chap_re = None

        # If filename has no volume number but we know it from the grab, use it (volume files only)
        if (proposed_vol is None and proposed_vol_rs is None
                and volume_num is not None and file_type == 'volume'):
            proposed_vol = volume_num

        dst_fname = build_filename(s['title'], proposed_vol, fname)
        dst_path  = os.path.join(dst_dir, dst_fname)

        # Per-file pack type. 'complete' at the release level always wins
        # (a file in a complete pack is part of that pack; the operator
        # can override in review if they disagree). Otherwise refine to
        # 'chapter_range' / 'volume_range' when this file carries range
        # info, else fall back to the release-level verdict. None means
        # "no explicit verdict, let _execute_import use legacy behaviour".
        if _rel_pack_type == 'complete':
            proposed_pack_type: str | None = 'complete'
        elif proposed_chap_re is not None:
            proposed_pack_type = 'chapter_range'
        elif proposed_vol_re is not None:
            proposed_pack_type = 'volume_range'
        elif _rel_pack_type in ('chapter', 'volume'):
            proposed_pack_type = _rel_pack_type
        else:
            proposed_pack_type = None

        if proposed_vol is None and proposed_chap is None and proposed_vol_rs is None \
                and proposed_chap_re is None and not _is_chapter_grab:
            unmapped += 1
        else:
            mapped += 1
        db.execute(
            "INSERT INTO import_queue_files"
            "(queue_id, filename, src_path, dst_path, proposed_volume, proposed_chapter,"
            " proposed_volume_range_start, proposed_volume_range_end,"
            " proposed_chapter_range_end, proposed_pack_type, proposed_is_special,"
            " file_type, status)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'pending')",
            (queue_id, dst_fname, src_path, dst_path,
             proposed_vol, proposed_chap,
             proposed_vol_rs, proposed_vol_re,
             proposed_chap_re, proposed_pack_type, proposed_is_special,
             file_type)
        )

    # No usable files found — remove the empty queue entry so the volume doesn't
    # get stuck in 'grabbed' state waiting for an import that can never complete.
    if mapped == 0 and unmapped == 0:
        db.execute("DELETE FROM import_queue WHERE id=?", (queue_id,))
        log_event('import', f"No manga files found in {src_dir} — skipping: {torrent_name}", series_id, db=db)
        return None, False

    # needs_review if ANY file is unmapped — user must confirm before the whole batch imports
    needs_review = unmapped > 0
    if unmapped > 0:
        log_event('import', f"Queued for review ({unmapped} unmapped file(s)): {torrent_name}", series_id, db=db)
    return queue_id, needs_review

# Single-flight guard for check_download_status. Evidence from issue #31
# follow-up A: the function's body takes 7-38s per run and was being
# spawned concurrently (up to 4× at once) from:
#   - status_loop (every 5 min)
#   - /api/check-downloads button
#   - /api/backfill-packs / system endpoints
# Overlapping runs amplify event-loop blocking and DB write contention.
# When one run is in flight, additional invocations are no-ops — the
# in-flight run will pick up whatever new state the caller cared about.
_CHECK_DOWNLOAD_STATUS_LOCK = asyncio.Lock()


async def check_download_status():
    """Poll download clients for completed downloads and queue them for import review.

    Skips if another invocation is still running (single-flight). Callers
    that need guaranteed execution should await a completed call instead
    of firing-and-forgetting via asyncio.create_task.
    """
    from shared import timed_block as _tb
    if _CHECK_DOWNLOAD_STATUS_LOCK.locked():
        # Another worker is already scanning; its results will reflect the
        # same queue state this caller would have seen.
        return
    async with _CHECK_DOWNLOAD_STATUS_LOCK:
        with _tb("check_download_status"):
            return await _check_download_status_impl()


async def _check_download_status_impl():
    """Inner body (wrapped for timing instrumentation — issue #31 follow-up A)."""
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
                log_event('info', f"Auto-pruned {_bl_deleted} expired blocklist entr{'ies' if _bl_deleted != 1 else 'y'} (TTL: {_bl_ttl}d)", db=_bldb)

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
            log_event('info', f"Auto-reset {_stuck_count} stuck grabbed volume(s) back to wanted", db=_stuckdb)

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

                    # qBit orphan cleanup — originally ran synchronously on the
                    # event loop inside `with get_db()`, iterating orphaned
                    # rows with ~6 writes each plus add_history. For N orphans
                    # that's a multi-second sync block; the event-loop lag
                    # monitor showed 5s CRITICAL blocks attributed to this
                    # exact section. Moved into asyncio.to_thread — same
                    # semantics, off the event loop.
                    _all_hashes_snapshot = all_hashes
                    def _qbit_orphan_cleanup_sync():
                        # Split across multiple transactions so a large orphan
                        # list doesn't hold the SQLite write lock for the whole
                        # duration. Prior behaviour wrapped everything in a
                        # single `with get_db()` block; with N orphans × ~10
                        # writes per iteration, the write lock could be held
                        # for many seconds, starving every other writer in the
                        # app (user HTTP handlers, other background loops) and
                        # producing OperationalError('database is locked').
                        #
                        # Phase A: bulk no-hash orphan resets (one transaction,
                        #   cheap — just two statements that scan a small set).
                        # Phase B: list orphan download_ids (short read-only
                        #   transaction). Orphan count cached for phase C.
                        # Phase C: per-orphan cleanup, each in its own
                        #   transaction. Commits in between so other writers
                        #   can slot in.
                        # ── Phase A: no-hash orphans (quick) ──
                        with get_db() as db:
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
                        # ── Phase B: enumerate hash-orphans (read-only) ──
                        with get_db() as db:
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
                            # Materialise out of the transaction so the per-
                            # orphan loop below doesn't still hold this conn.
                            orphaned = [dict(r) for r in orphaned]

                        # ── Phase C: per-orphan cleanup (one tx per orphan) ──
                        for gs in orphaned:
                            if (gs['download_id'] or '').lower() in all_hashes:
                                continue  # still present in client
                            h = gs['download_id']
                            with get_db() as db:
                                orphan_vol_ids = [
                                    r[0] for r in db.execute(
                                        "SELECT id FROM volumes WHERE series_id=? AND download_id=?"
                                        " AND status='grabbed' AND volume_num IS NOT NULL",
                                        (gs['series_id'], h)
                                    ).fetchall()
                                ]
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
                                    gs['series_id'], db=db)
                                _sr = db.execute(
                                    "SELECT title FROM series WHERE id=?", (gs['series_id'],)
                                ).fetchone()
                                add_history(db, 'grab_failed', gs['series_id'],
                                            _sr['title'] if _sr else '',
                                            '',
                                            source_title=gs['torrent_name'] or '',
                                            download_id=h,
                                            data={'reason': 'removed_from_client'})

                    # Close the orphan-cleanup helper. End of _qbit_orphan_cleanup_sync.
                    await asyncio.to_thread(_qbit_orphan_cleanup_sync)

                    # ── Failed download handling ──────────────────────────────────────────────
                    # Separate from the orphan cleanup because it has an
                    # awaited `qbit_remove` call — can't live in the
                    # to_thread helper. DB writes that don't need the
                    # await are threaded; the HTTP call is awaited; then
                    # a final sync helper logs and optionally re-grabs.
                    if get_cfg('failed_download_handling', '0') == '1':
                        all_torrent_by_hash = {t['hash'].lower(): t for t in all_torrents}
                        error_states = {'error', 'missingFiles', 'stalledDL'}
                        with get_db() as _fdb:
                            seen_rows = _fdb.execute(
                                "SELECT download_id, series_id, torrent_name, torrent_url"
                                " FROM seen WHERE client='qbittorrent' AND protocol='torrent'"
                            ).fetchall()
                        for row in seen_rows:
                            h_fail = (row['download_id'] or '').lower()
                            if not h_fail:
                                continue
                            torrent_fail = all_torrent_by_hash.get(h_fail)
                            if torrent_fail and torrent_fail.get('state', '') in error_states:
                                def _mark_failed_sync(r=row, tf=torrent_fail, h=h_fail):
                                    with get_db() as db:
                                        db.execute(
                                            "INSERT OR IGNORE INTO blocklist(series_id, torrent_url, torrent_name, reason)"
                                            " VALUES(?,?,?,?)",
                                            (r['series_id'], r['torrent_url'] or '', r['torrent_name'] or '',
                                             f"Download failed: {tf.get('state', 'error')}")
                                        )
                                        db.execute(
                                            "UPDATE volumes SET status='wanted', download_id=NULL, grabbed_at=NULL,"
                                            " source_url=NULL, torrent_name=NULL "
                                            "WHERE download_id=? AND status='grabbed'", (h,)
                                        )
                                        db.execute("DELETE FROM seen WHERE download_id=?", (h,))
                                await asyncio.to_thread(_mark_failed_sync)
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

                # SAB processing — moved off the event loop via asyncio.to_thread.
                # Previously this entire block (seen-row match + orphan cleanup)
                # ran synchronously on the event loop inside one long write
                # transaction, stalling HTTP requests for seconds (issue #31).
                _sab_new_queue_ids: list[int] = []
                def _sab_process_sync():
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
                            _sab_new_queue_ids.append(q_id)

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

                await asyncio.to_thread(_sab_process_sync)
                # Spawn post-processing for any newly queued SAB imports (async).
                for _sqid in _sab_new_queue_ids:
                    asyncio.create_task(_process_auto_import(_sqid))
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
            log_event('download_complete', f"Vol {volume_num:g} download complete", series_id, db=db)
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
            log_event('download_complete', f"{label} pack download complete", series_id, db=db)
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

# ── Import staging (two-phase commit for multi-file imports) ─────────────────
# Protects against partial failure mid-batch: a queue with 5 files where
# file 3 fails used to leave files 1+2 at the final destination with files
# 4+5 still at source. Now every file op lands in a hidden staging dir
# under dst_dir first; only after ALL files stage successfully does the
# helper rename them into place. Staging + DB are committed/rolled back
# together via a SQLite SAVEPOINT.
#
# True atomicity caveats (documented — not claimed):
# - `os.replace` is atomic ONLY within the same filesystem. Staging lives
#   under dst_dir so this holds for the rename phase.
# - For import_mode='move', the source file's ultimate deletion is
#   deferred until the commit phase. If the batch rolls back, source is
#   untouched. If the commit phase itself fails after some renames have
#   happened (extremely rare — rename within a dir is near-atomic), we
#   log and let the next import retry; partial rename is the only
#   window we can't fully roll back.
# - CBR→CBZ and ComicInfo.xml injection happen on the staging file so a
#   crash mid-transform leaves the staging dir to be cleaned up on
#   rollback; the live library tree never sees the partial file.
import tempfile as _tempfile


class _ImportStaging:
    """Per-import-batch staging directory + two-phase commit.

    Usage:
        staging = _ImportStaging(dst_dir, queue_id, import_mode)
        try:
            for f in files:
                stage_path = staging.stage(src, final_path)
                # ... transforms operate on stage_path ...
                # If a transform renamed the in-staging file:
                final_path = staging.rename(stage_path, new_stage_path)
            staging.commit_all()
        except Exception:
            staging.rollback()
            raise
    """

    def __init__(self, dst_dir: str, queue_id: int, import_mode: str):
        self.dst_dir = dst_dir
        self.import_mode = import_mode
        # Dot-prefixed so the library scanner / file browser hide it.
        self.staging_dir = _tempfile.mkdtemp(
            prefix=f".mangarr-staging-{queue_id}-",
            dir=dst_dir,
        )
        # Each entry: {'stage_path', 'final_path', 'src_path'}
        self._staged: list[dict] = []

    def stage(self, src: str, final_path: str) -> str:
        """Place `src` at a staging path using a per-mode strategy that
        always preserves the source during staging. Returns the staging
        path. Raises OSError on filesystem failure.
        """
        fname = os.path.basename(final_path)
        stage_path = os.path.join(self.staging_dir, fname)
        if self.import_mode == 'hardlink':
            # Hardlink to staging; original source and staging share the
            # same inode. Unlinking staging later is safe — source keeps
            # its own directory entry. At commit, staging is renamed
            # into dst_dir (still the same inode).
            os.link(src, stage_path)
        else:
            # Both 'copy' and 'move' go through copy2 in the staging
            # phase so that a batch rollback leaves `src` intact. For
            # 'move', src is deleted in commit_all() after the rename
            # succeeds. This means move-mode on the same filesystem now
            # pays a bytes-copy cost that a bare shutil.move would avoid;
            # the tradeoff is that batch atomicity is preserved.
            shutil.copy2(src, stage_path)
        self._staged.append({
            'stage_path': stage_path,
            'final_path': final_path,
            'src_path': src,
        })
        return stage_path

    def rename(self, old_stage_path: str, new_stage_path: str) -> str:
        """Tell the helper that an in-staging transform (e.g. CBR→CBZ)
        renamed the staged file. Updates tracking so commit_all uses
        the post-transform basename as the final path. Returns the new
        final_path."""
        for rec in self._staged:
            if rec['stage_path'] == old_stage_path:
                rec['stage_path'] = new_stage_path
                new_basename = os.path.basename(new_stage_path)
                rec['final_path'] = os.path.join(
                    os.path.dirname(rec['final_path']), new_basename,
                )
                return rec['final_path']
        # Not found — the transform produced a path we didn't stage.
        # Don't silently accept; the caller should investigate.
        raise ValueError(f"rename on unknown stage path: {old_stage_path!r}")

    def commit_all(self) -> None:
        """Move every staged file to its final destination, then delete
        source files for move-mode entries. Called only when every file
        staged successfully. After this, the staging dir is removed.
        """
        for rec in self._staged:
            # os.replace is atomic when src and dst are on the same
            # filesystem; staging lives under dst_dir so this holds.
            os.replace(rec['stage_path'], rec['final_path'])
        # Source-file removal happens AFTER all renames succeed, so a
        # mid-rename failure (rare) still leaves sources intact for the
        # files we haven't renamed yet.
        if self.import_mode == 'move':
            for rec in self._staged:
                try:
                    os.unlink(rec['src_path'])
                except FileNotFoundError:
                    pass  # already gone
                except OSError as e:
                    # The file is at its final path; source is just
                    # leftover. Log but don't fail the import.
                    print(f"[Import] could not remove source {rec['src_path']}: {e}")
        self._cleanup()

    def rollback(self) -> None:
        """Remove every staged file; source files are untouched."""
        self._cleanup()

    def _cleanup(self) -> None:
        try:
            shutil.rmtree(self.staging_dir)
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"[Import] failed to clean staging dir {self.staging_dir}: {e}")


# ── Import concurrency guard ──────────────────────────────────────────────────
# Bound the number of imports that can run in parallel so a backlog of pending
# queue rows doesn't spawn one task per row and hammer SQLite + disk I/O.
# Two feels right for a typical self-hosted install (single spinning drive or
# a Docker volume); raise it if you have fast SSD + many cores.
_IMPORT_SEM = asyncio.Semaphore(2)


def claim_import_queue_row(db, queue_id: int,
                            allowed_statuses: tuple[str, ...] = ('pending', 'partial')
                            ) -> bool:
    """Atomically transition the queue row into 'importing' state.

    Returns True iff this caller won the race — i.e. the row was in one of
    allowed_statuses and is now 'importing'. Returns False if another worker
    already claimed it, or if the row has moved to a terminal state
    (imported/failed/skipped). The caller is expected to bail out cleanly
    when False is returned.

    Safe to call from multiple concurrent coroutines / threads because
    SQLite serialises writes and the UPDATE's rowcount is authoritative.
    """
    placeholders = ','.join('?' * len(allowed_statuses))
    cur = db.execute(
        f"UPDATE import_queue SET status='importing'"
        f" WHERE id=? AND status IN ({placeholders})",
        [queue_id, *allowed_statuses],
    )
    return cur.rowcount > 0


async def _guarded_execute_import(
    queue_id: int,
    volume_overrides: dict | None = None,
    skip_ids: set | None = None,
    chapter_overrides: dict | None = None,
) -> bool:
    """Claim the queue row, then run _execute_import under the semaphore.

    Wrapper used by every entry point that starts an import (auto-import
    loop, qbit-complete discovery, stuck-retry, manual retry endpoint,
    and the manual-submit form). Ensures:
      1. only one worker ever runs per queue_id (atomic UPDATE claim)
      2. at most _IMPORT_SEM._value imports run concurrently

    Returns the underlying _execute_import result on success, or False
    if the claim was lost (meaning another worker is already processing
    this row, or the row has moved to a terminal state).
    """
    with get_db() as _claim_db:
        if not claim_import_queue_row(_claim_db, queue_id):
            print(f"[Import] queue {queue_id}: claim lost "
                  f"(another worker owns it, or row is in a terminal state)")
            return False
    async with _IMPORT_SEM:
        return await _execute_import(queue_id, volume_overrides, skip_ids, chapter_overrides)


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
        # 'importing' accepted because _guarded_execute_import's claim
        # flips the row from pending → importing atomically before we
        # get here. Rejecting 'importing' silently no-oped every form
        # POST that routed through the guarded wrapper.
        if not queue or queue['status'] not in ('pending', 'partial', 'importing'):
            return False

        # For partial entries, process both pending and needs_review files;
        # for fresh pending entries, process all pending files.
        files = db.execute(
            "SELECT * FROM import_queue_files WHERE queue_id=? AND status IN ('pending', 'needs_review')",
            (queue_id,)
        ).fetchall()

        # Empty queue → pure no-op. Previously this path marked the queue
        # as 'imported' and deleted it, which is wrong (nothing was
        # imported) and broke the safety contract that an active queue row
        # protects its grabbed volumes from the stuck-grabbed sweeper. If
        # the row was flipped to 'importing' by our claim, flip it back
        # so the next poll can find it again.
        if not files:
            if queue['status'] == 'importing':
                db.execute(
                    "UPDATE import_queue SET status='pending' WHERE id=?",
                    (queue_id,),
                )
            return False

        s = db.execute(
            "SELECT * FROM series WHERE id=?", (queue['series_id'],)
        ).fetchone()
        _series_tags = [r['tag'] for r in db.execute(
            "SELECT tag FROM series_tags WHERE series_id=?", (queue['series_id'],)
        ).fetchall()]
        rf = db.execute(
            "SELECT path FROM root_folders WHERE id=?", (s['root_folder_id'],)
        ).fetchone() if s and s['root_folder_id'] else None
        dest_root = _resolve_series_dest_root(db, s['root_folder_id'], rf)
        safe_dir  = sanitize_filename(s['title'] or 'Unknown') if s else 'Unknown'
        dst_dir   = os.path.join(dest_root, safe_dir)

        try:
            os.makedirs(dst_dir, exist_ok=True)
        except Exception as e:
            log_event('error', f"Import: cannot create {dst_dir}: {e}", queue['series_id'], db=db)
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

        # Two-phase commit for the whole batch. Every file op goes into
        # staging/; commit_all() renames them into place only if every
        # file staged successfully. The SQLite SAVEPOINT mirrors the
        # filesystem staging so DB writes (queue_files, volumes, chapters)
        # commit together with the renames — or roll back together.
        staging = _ImportStaging(dst_dir, queue['id'], import_mode)
        db.execute("SAVEPOINT import_batch")
        # First file that failed mid-batch, so we can still mark it 'failed'
        # in the DB AFTER the savepoint rollback reverts other writes.
        _batch_failed_file_id: int | None = None
        _batch_failed_reason: str = ""

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
            # Stage 2 — explicit range / pack-type / special fields.
            # Back-compat: the keys() guard lets rows written before the
            # migration still import through the legacy code paths.
            _keys = f.keys()
            row_vol_rs     = f['proposed_volume_range_start'] if 'proposed_volume_range_start' in _keys else None
            row_vol_re     = f['proposed_volume_range_end']   if 'proposed_volume_range_end'   in _keys else None
            row_chap_re    = f['proposed_chapter_range_end']  if 'proposed_chapter_range_end'  in _keys else None
            row_pack_type  = f['proposed_pack_type']          if 'proposed_pack_type'          in _keys else None
            row_is_special = int(f['proposed_is_special']) if 'proposed_is_special' in _keys and f['proposed_is_special'] else 0

            # ── Chapter file: has a chapter number ────────────────────────────
            if file_type == 'chapter' and proposed_chap is not None:
                src = f['src_path']

                # Chapter-range end (covers `c001-002` imports as one row).
                # Stage 2: the review UI now carries an explicit
                # proposed_chapter_range_end column — trust it first.
                # The filename auto-detect survives as a fallback so older
                # queue rows written before the migration still work.
                _ch_range_end: float | None = None
                if row_chap_re is not None:
                    _ch_range_end = row_chap_re
                else:
                    _detected_range = extract_chapter_range(os.path.basename(src))
                    if _detected_range is not None:
                        _r_start, _r_end = _detected_range
                        # Only honour the detected range if it agrees with
                        # the proposed start (don't silently rewrite an
                        # operator's explicit single-chapter assignment).
                        if abs(_r_start - proposed_chap) < 1e-6:
                            _ch_range_end = _r_end

                try:
                    dst = safe_join_under(dst_dir, f['filename'])
                except ValueError as _e:
                    db.execute(
                        "UPDATE import_queue_files SET status='failed' WHERE id=?", (f['id'],)
                    )
                    log_event('error', f"Import: unsafe destination ({f['filename']}): {_e}", queue['series_id'], db=db)
                    any_error = True
                    continue

                if not os.path.isfile(src):
                    db.execute(
                        "UPDATE import_queue_files SET status='failed' WHERE id=?", (f['id'],)
                    )
                    log_event('error', f"Import: source file missing: {src}", queue['series_id'], db=db)
                    any_error = True
                    continue

                try:
                    # Stage the file on a worker thread so a large CBZ
                    # copy can't freeze the event loop (py-spy dump
                    # during the v0.1.5 HxH session showed uvicorn's
                    # MainThread stuck inside shutil.copy2 here, which
                    # blocked every concurrent page render). Same
                    # treatment for the CBR→CBZ conversion and
                    # ComicInfo injection, both of which read/write
                    # zip archives. staging.rename is a dict update
                    # and safe to keep sync.
                    stage_path = await asyncio.to_thread(staging.stage, src, dst)
                    stage_after = await asyncio.to_thread(_maybe_convert_to_cbz, stage_path)
                    if stage_after != stage_path:
                        dst = staging.rename(stage_path, stage_after)
                        stage_path = stage_after
                    if s:
                        await asyncio.to_thread(
                            _try_inject_comicinfo,
                            stage_path, s,
                            chapter_num=proposed_chap, tags=_series_tags,
                        )

                    db.execute(
                        "UPDATE import_queue_files SET status='imported', dst_path=? WHERE id=?",
                        (dst, f['id'])
                    )
                    imported_count += 1

                    # Resolve or create the parent volume record.
                    # Specials and mainline share volume numbers (Gaiden
                    # "vol 3" is not mainline vol 3), so route by the
                    # is_special flag — a special chapter gets its own
                    # parent row that the Stage 3 coverage queries
                    # recognise as non-mainline.
                    vol_id = None
                    if proposed_vol is not None:
                        if row_is_special:
                            vol_row = db.execute(
                                "SELECT id FROM volumes WHERE series_id=? AND volume_num=?"
                                " AND COALESCE(is_special, 0) = 1",
                                (queue['series_id'], proposed_vol)
                            ).fetchone()
                            if vol_row:
                                vol_id = vol_row['id']
                            else:
                                cur2 = db.execute(
                                    "INSERT INTO volumes(series_id, volume_num, status, is_special)"
                                    " VALUES(?,?,'wanted',1)",
                                    (queue['series_id'], proposed_vol)
                                )
                                vol_id = cur2.lastrowid
                        else:
                            vol_row = db.execute(
                                "SELECT id FROM volumes WHERE series_id=? AND volume_num=?"
                                " AND COALESCE(is_special, 0) = 0",
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

                    # Upsert the chapter record with full metadata. When
                    # importing a chapter pack (c001-002), set chapter_range_end
                    # so a single row covers the whole span.
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
                            " volume_id=COALESCE(volume_id,?), download_id=COALESCE(download_id,?),"
                            " chapter_range_end=COALESCE(?, chapter_range_end)"
                            " WHERE id=?",
                            (dst, _ch_quality, now_ts, _ch_torrent_name,
                             _pv_meta.get('indexer'), _pv_meta.get('protocol'),
                             _pv_meta.get('client'), _pv_meta.get('release_group'),
                             _pv_meta.get('size_bytes'),
                             vol_id, queue['download_id'], _ch_range_end, chap_row['id'])
                        )
                    else:
                        db.execute(
                            "INSERT INTO chapters(series_id, volume_id, chapter_num, status,"
                            " import_path, download_id, torrent_name, indexer, protocol, client,"
                            " release_group, size_bytes, quality, imported_at, chapter_range_end)"
                            " VALUES(?,?,?,'downloaded',?,?,?,?,?,?,?,?,?,?,?)",
                            (queue['series_id'], vol_id, proposed_chap, dst,
                             queue['download_id'], _ch_torrent_name,
                             _pv_meta.get('indexer'), _pv_meta.get('protocol'),
                             _pv_meta.get('client'), _pv_meta.get('release_group'),
                             _pv_meta.get('size_bytes'), _ch_quality, now_ts,
                             _ch_range_end)
                        )

                    # If this row covers a chapter range, sweep up any
                    # pre-existing placeholder rows for the inner chapters
                    # (status='wanted', no import_path) — they're now covered
                    # by this single file. Rows with their own import_path
                    # are left alone (different physical files).
                    if _ch_range_end is not None:
                        db.execute(
                            "DELETE FROM chapters WHERE series_id=?"
                            "   AND chapter_num > ? AND chapter_num <= ?"
                            "   AND status = 'wanted'"
                            "   AND import_path IS NULL",
                            (queue['series_id'], proposed_chap, _ch_range_end)
                        )

                    if vol_id is not None:
                        chapter_vols_touched.add(vol_id)
                    if proposed_vol is not None:
                        imported_vols.add(proposed_vol)

                except Exception as e:
                    db.execute(
                        "UPDATE import_queue_files SET status='failed' WHERE id=?", (f['id'],)
                    )
                    log_event('error', f"Import chapter error ({f['filename']}): {e}", queue['series_id'], db=db)
                    any_error = True
                    if _batch_failed_file_id is None:
                        _batch_failed_file_id = f['id']
                        _batch_failed_reason = f"Import chapter error ({f['filename']}): {e}"
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
                        log_event('error', f"Import: unsafe destination ({f['filename']}): {_e}", queue['series_id'], db=db)
                        any_error = True
                        continue
                    if not os.path.isfile(src):
                        db.execute("UPDATE import_queue_files SET status='failed' WHERE id=?", (f['id'],))
                        log_event('error', f"Import: source file missing: {src}", queue['series_id'], db=db)
                        any_error = True
                        continue
                    try:
                        stage_path = await asyncio.to_thread(staging.stage, src, dst)
                        stage_after = await asyncio.to_thread(_maybe_convert_to_cbz, stage_path)
                        if stage_after != stage_path:
                            dst = staging.rename(stage_path, stage_after)
                            stage_path = stage_after
                        if s:
                            await asyncio.to_thread(
                                _try_inject_comicinfo,
                                stage_path, s,
                                chapter_num=recheck_chap, tags=_series_tags,
                            )
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
                        log_event('error', f"Import chapter error ({f['filename']}): {e}", queue['series_id'], db=db)
                        any_error = True
                        if _batch_failed_file_id is None:
                            _batch_failed_file_id = f['id']
                            _batch_failed_reason = f"Import chapter error ({f['filename']}): {e}"
                    continue

            # For legacy chapter-mode grabs the file has no volume number — allow through.
            # Stage 2: a volume-range file (e.g. one CBZ covering v1-v3) may
            # also have proposed_vol=None but row_vol_rs/re set. That's a
            # volume import with a range, not a chapter stub — don't treat
            # it as needs_review. The range-aware write below handles it.
            _ch_stub = None
            _has_vol_range = row_vol_rs is not None and row_vol_re is not None
            if proposed_vol is None and not _has_vol_range and f['id'] not in volume_overrides:
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
                log_event('error', f"Import: unsafe destination ({f['filename']}): {_e}", queue['series_id'], db=db)
                any_error = True
                continue

            if not os.path.isfile(src):
                db.execute(
                    "UPDATE import_queue_files SET status='failed' WHERE id=?", (f['id'],)
                )
                log_event('error', f"Import: source file missing: {src}", queue['series_id'], db=db)
                any_error = True
                continue

            try:
                stage_path = await asyncio.to_thread(staging.stage, src, dst)
                stage_after = await asyncio.to_thread(_maybe_convert_to_cbz, stage_path)
                if stage_after != stage_path:
                    dst = staging.rename(stage_path, stage_after)
                    stage_path = stage_after
                if s:
                    await asyncio.to_thread(
                        _try_inject_comicinfo,
                        stage_path, s,
                        volume_num=proposed_vol, tags=_series_tags,
                    )
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

                # ── Volume-range file (Stage 2) ─────────────────────────
                # One physical file covering v1-v3 style: write a single
                # volumes row with vol_range_start/end + pack_type, then
                # skip the single-volume flow below.
                if _has_vol_range and proposed_vol is None:
                    seen_row = db.execute(
                        "SELECT torrent_name, indexer, protocol, client, release_group, size_bytes"
                        " FROM seen WHERE (download_id=? AND download_id IS NOT NULL)"
                        " OR torrent_url=? LIMIT 1",
                        (queue['download_id'], queue['torrent_url'])
                    ).fetchone()
                    meta = dict(seen_row) if seen_row else {}
                    file_quality = quality_from_filename(f['filename'])
                    _rpt = row_pack_type if row_pack_type in ('volume', 'volume_range', 'complete') \
                           else 'volume'
                    db.execute(
                        "INSERT INTO volumes(series_id, volume_num, status, source_url,"
                        " torrent_name, import_path, download_id, indexer, protocol,"
                        " client, release_group, size_bytes, quality, imported_at,"
                        " vol_range_start, vol_range_end, pack_type, is_special)"
                        " VALUES(?,NULL,'downloaded',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (queue['series_id'],
                         queue['torrent_url'], meta.get('torrent_name'),
                         dst, queue['download_id'],
                         meta.get('indexer'), meta.get('protocol'),
                         meta.get('client'), meta.get('release_group'),
                         meta.get('size_bytes'), file_quality, now_ts,
                         row_vol_rs, row_vol_re, _rpt, row_is_special)
                    )
                    # Volume range satisfies all interior volumes — skip
                    # the single-volume cascade; chapter tables for those
                    # inner volumes will be updated by future grabs.
                    for _v in range(int(row_vol_rs), int(row_vol_re) + 1):
                        imported_vols.add(float(_v))
                    continue  # next queue file

                # Stamp full source metadata on the volume stub now that the file is confirmed
                if proposed_vol is not None:
                    seen_row = db.execute(
                        "SELECT torrent_name, indexer, protocol, client, release_group, size_bytes"
                        " FROM seen WHERE (download_id=? AND download_id IS NOT NULL)"
                        " OR torrent_url=? LIMIT 1",
                        (queue['download_id'], queue['torrent_url'])
                    ).fetchone()
                    meta = dict(seen_row) if seen_row else {}

                    # Match/create the volumes row on the same is_special
                    # track as the import itself — a special single-volume
                    # grab must not flip a mainline row to is_special=1.
                    if row_is_special:
                        vol_row = db.execute(
                            "SELECT id FROM volumes WHERE series_id=? AND volume_num=?"
                            " AND COALESCE(is_special, 0) = 1",
                            (queue['series_id'], proposed_vol)
                        ).fetchone()
                    else:
                        vol_row = db.execute(
                            "SELECT id FROM volumes WHERE series_id=? AND volume_num=?"
                            " AND COALESCE(is_special, 0) = 0",
                            (queue['series_id'], proposed_vol)
                        ).fetchone()
                    file_quality = quality_from_filename(f['filename'])
                    if vol_row:
                        db.execute(
                            "UPDATE volumes SET status='downloaded', import_path=?,"
                            " torrent_name=?, indexer=?, protocol=?, client=?,"
                            " release_group=?, size_bytes=?, quality=?, imported_at=?,"
                            " download_id=COALESCE(download_id,?),"
                            " is_special=COALESCE(NULLIF(?,0), is_special),"
                            " pack_type=COALESCE(?, pack_type) WHERE id=?",
                            (dst,
                             meta.get('torrent_name'), meta.get('indexer'),
                             meta.get('protocol'), meta.get('client'),
                             meta.get('release_group'), meta.get('size_bytes'),
                             file_quality, now_ts, queue['download_id'],
                             row_is_special,
                             row_pack_type if row_pack_type in ('volume', 'complete') else None,
                             vol_row['id'])
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
                        _rpt_new = row_pack_type if row_pack_type in ('volume', 'complete') else None
                        cur_ins = db.execute(
                            "INSERT INTO volumes(series_id, volume_num, status, source_url,"
                            " torrent_name, import_path, download_id, indexer, protocol,"
                            " client, release_group, size_bytes, quality, imported_at,"
                            " pack_type, is_special)"
                            " VALUES(?,?,'downloaded',?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (queue['series_id'], proposed_vol,
                             queue['torrent_url'], meta.get('torrent_name'),
                             dst, queue['download_id'],
                             meta.get('indexer'), meta.get('protocol'),
                             meta.get('client'), meta.get('release_group'),
                             meta.get('size_bytes'), file_quality, now_ts,
                             _rpt_new, row_is_special)
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
                log_event('error', f"Import file error ({f['filename']}): {e}", queue['series_id'], db=db)
                any_error = True
                if _batch_failed_file_id is None:
                    _batch_failed_file_id = f['id']
                    _batch_failed_reason = f"Import file error ({f['filename']}): {e}"

        # ── Two-phase commit decision ─────────────────────────────────────────
        # If ANY file failed mid-batch AND at least one other file would have
        # imported, roll back the whole batch so the library doesn't end up
        # half-full with an incomplete release. Pure-failure batches (0
        # imports) keep their 'failed' per-file markers so the user sees
        # which file was bad.
        if any_error and imported_count > 0:
            # Filesystem rollback: drop every staged file, sources intact.
            # Off the event loop — shutil.rmtree on a partially-staged
            # batch can touch dozens of files.
            await asyncio.to_thread(staging.rollback)
            # DB rollback: revert every per-file UPDATE done in the loop.
            db.execute("ROLLBACK TO SAVEPOINT import_batch")
            # Re-apply the bits we want to keep: the queue status and the
            # identity of the one file that actually broke.
            if _batch_failed_file_id is not None:
                db.execute(
                    "UPDATE import_queue_files SET status='failed' WHERE id=?",
                    (_batch_failed_file_id,),
                )
            db.execute("RELEASE SAVEPOINT import_batch")
            log_event(
                'error',
                f"Import rolled back (batch atomicity): {_batch_failed_reason}",
                queue['series_id'],
                db=db,
            )
            # Force the post-loop "Determine final queue status" logic to see
            # a fully-failed batch.
            imported_count = 0
            chapter_vols_touched.clear()
            imported_vols.clear()
        elif imported_count > 0:
            # All-or-nothing succeeded: commit the staged files into place.
            # commit_all does N os.replace calls (+ optional os.unlink for
            # move-mode sources) — fast per call but N matters for big
            # batches, and it's still disk I/O.
            try:
                await asyncio.to_thread(staging.commit_all)
                db.execute("RELEASE SAVEPOINT import_batch")
            except Exception as e:
                # Commit-phase failure is extremely rare (rename within one
                # filesystem). Fall back to rolling the batch back.
                await asyncio.to_thread(staging.rollback)
                db.execute("ROLLBACK TO SAVEPOINT import_batch")
                db.execute("RELEASE SAVEPOINT import_batch")
                log_event(
                    'error',
                    f"Import commit phase failed; rolled back: {e}",
                    queue['series_id'],
                    db=db,
                )
                imported_count = 0
        else:
            # No files succeeded; nothing to commit or roll back.
            await asyncio.to_thread(staging.rollback)
            db.execute("RELEASE SAVEPOINT import_batch")

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
            log_event('import', f"Imported {imported_count} file(s): {queue['torrent_name']}", queue['series_id'], db=db)
            add_history(db, 'imported', queue['series_id'], s_title, vol_label,
                        source_title=queue['torrent_name'] or '',
                        download_id=queue['download_id'] or '',
                        data={'dst_dir': dst_dir, 'count': imported_count})
        else:
            log_event('error', f"Import failed: {queue['torrent_name']}", queue['series_id'], db=db)
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

    Routes through _guarded_execute_import so:
      - an atomic claim prevents two workers from processing the same row
      - the bounded _IMPORT_SEM caps concurrent imports

    On unhandled exception, mark the queue as 'failed' so it doesn't stick
    forever and get retried on every startup. 'importing' is included in
    the WHERE so a row whose claim we won but which raised mid-import is
    still moved to the terminal 'failed' state."""
    try:
        await _guarded_execute_import(queue_id)
    except Exception as e:
        import traceback
        log_event('error', f"Auto-import failed for queue {queue_id}: {e}")
        print(f"[AutoImport] {e}\n{traceback.format_exc()}")
        try:
            with get_db() as _db_err:
                _db_err.execute(
                    "UPDATE import_queue SET status='failed'"
                    " WHERE id=? AND status IN ('pending','partial','importing')",
                    (queue_id,)
                )
        except Exception as _db_e:
            print(f"[AutoImport] failed to mark queue {queue_id} as failed: {_db_e}")


# ── Metadata source adapters moved to metadata.py ────────────────────────────
# AniList / MangaUpdates / MangaDex / Kitsu adapters: pure HTTP+JSON wrappers
# plus helpers for MU slug conversion, status normalisation, and chapter-map
# validation. Re-exported so all existing call sites keep working unchanged.
from metadata import (  # noqa: F401
    mu_slug_to_id, mu_id_to_slug, _norm_status,
    ANILIST_QUERY, ANILIST_ALIASES_QUERY,
    fetch_anilist_aliases, anilist_search,
    mu_search, search_series,
    fetch_mangadex_id, fetch_chapter_volume_map, fetch_kitsu_chapter_map,
    _trim_cvm_to_vol_range, _validate_chapter_map,
    _WIKI_WORD_NUMS,
)



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
    """Return True if the content this pack would provide is already fully
    covered by existing non-special grabbed/downloaded rows.

    Stage 3 rules:
      - Only non-special rows (is_special = 0) can satisfy mainline coverage.
        A Gaiden / oneshot / side-story grab does NOT cover mainline slots.
      - Volume matching is float-precise: volume 3 does not cover 3.5 or 3a.
      - Existing volume-range rows count as coverage — a row with
        vol_range_start=1, vol_range_end=5 and status in (grabbed, downloaded)
        satisfies targets 1..5 even if interior stubs are still 'wanted'.
      - "Grabbed" and "downloaded" both count as covering; the pre-Stage-3
        logic only inspected 'wanted' stubs, which missed ranges that hadn't
        cascaded into interior stubs.
    """
    with get_db() as db:
        # A non-special complete pack supersedes any narrower new pack.
        has_complete = db.execute(
            "SELECT 1 FROM volumes WHERE series_id=? AND pack_type='complete'"
            " AND status IN ('grabbed','downloaded')"
            " AND COALESCE(is_special, 0) = 0",
            (series_id,)
        ).fetchone()
        if has_complete and pack_type != 'complete':
            return True

        # For a new complete pack, only skip if no mainline wanted+monitored
        # stubs remain (specials don't block a mainline complete grab).
        if pack_type == 'complete':
            wanted = db.execute(
                "SELECT 1 FROM volumes WHERE series_id=? AND volume_num IS NOT NULL"
                " AND status='wanted' AND monitored=1"
                " AND COALESCE(is_special, 0) = 0",
                (series_id,)
            ).fetchone()
            return wanted is None

        # Determine target volumes. Keep them as floats so fractional
        # parser outputs round-trip cleanly (no int cast collapse).
        # For volume ranges, include the explicit endpoints plus any
        # integer volumes in between — (3.5, 3.5) → {3.5}, not {3}.
        target_vols: set[float] = set()
        if pack_type == 'chapter' and ch_range:
            target_vols = {float(v) for v in chapters_to_volume_set(
                ch_range[0], ch_range[1], ch_map, total_chs, total_vols)}
        elif pack_type == 'chapter' and not ch_range:
            return False  # unknown coverage → don't skip
        elif pack_type == 'volume' and vol_rng:
            start_f, end_f = float(vol_rng[0]), float(vol_rng[1])
            target_vols = {start_f, end_f}
            lo = int(start_f) + 1
            hi = int(end_f)
            for iv in range(lo, hi + 1):
                target_vols.add(float(iv))
        else:
            return False

        if not target_vols:
            return False

        # Each target must be satisfied by SOME non-special row, either a
        # precise volume_num match OR a range row covering it. Use one
        # parameterised SELECT per target — the loop is cheap and keeps
        # the SQL trivially readable.
        satisfy_sql = (
            "SELECT 1 FROM volumes WHERE series_id=?"
            "  AND status IN ('grabbed','downloaded')"
            "  AND COALESCE(is_special, 0) = 0"
            "  AND ("
            "    volume_num = ?"
            "    OR (vol_range_start IS NOT NULL AND vol_range_end IS NOT NULL"
            "        AND vol_range_start <= ? AND vol_range_end >= ?)"
            "  )"
            "  LIMIT 1"
        )
        for v in target_vols:
            row = db.execute(satisfy_sql, (series_id, v, v, v)).fetchone()
            if row is None:
                return False
        return True

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
    total_ch  = meta['total_chapters'] if meta else None
    total_vol = meta['total_volumes'] if meta else None

    mapping = await fetch_chapter_volume_map(mdx_id)
    mapping = _trim_cvm_to_vol_range(mapping, total_vol, 'MangaDex')
    map_source = 'mangadex'
    if not _validate_chapter_map(mapping, total_ch, 'MangaDex'):
        mapping = {}

    # Fallback when MangaDex has no usable chapter data (DMCA'd / sparse): try Kitsu
    if not mapping and meta:
        kitsu_map = await fetch_kitsu_chapter_map(
            meta['title'], s['anilist_id'], meta['total_chapters']
        )
        kitsu_map = _trim_cvm_to_vol_range(kitsu_map, total_vol, 'Kitsu')
        if _validate_chapter_map(kitsu_map, total_ch, 'Kitsu'):
            mapping = kitsu_map
            map_source = 'kitsu'

    # Fallback: extract chapter→volume map from downloaded CBZ filenames
    if not mapping:
        with get_db() as db:
            cbz_dir = _series_library_dir(db, series_id)
        cbz_map = _extract_map_from_cbzs(cbz_dir)
        cbz_map = _trim_cvm_to_vol_range(cbz_map, total_vol, 'CBZ')
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
def _log_grab_rejection(series_id: int, title: str, reason: str) -> None:
    """Surface a grab rejection as a `rejected_release` event.

    Called from `grab_item` on rejection paths that represent real
    filtering decisions (blocklist, edition mismatch, cross-group
    repack, quality cutoff). Normal-flow deduplication (seen, in-flight
    dedup) is NOT logged here — those aren't rejections, just guards,
    and logging them would flood the events table on every RSS poll.

    Also skipped: high-frequency informational rejections that repeat
    every poll for a stable reason (coverage-already-satisfied,
    score-too-low, not-an-upgrade). Those would drown the debugging
    signal from the rare rejections operators actually care about.
    """
    try:
        log_event('rejected_release',
                  f'{reason}: {title[:120]}',
                  series_id)
    except Exception:
        pass  # logging must never break grab


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
            _log_grab_rejection(series_id, title, 'blocklisted')
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
            _log_grab_rejection(
                series_id, title,
                f'edition mismatch (series={_series_edition}, release={_release_edition})'
            )
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

    # Pack monitoring check: reject the entire pack (RSS sync) if no
    # MAINLINE wanted+monitored volumes are in the range — mirrors Sonarr's
    # MonitoredEpisodeSpecification. Specials don't count for a mainline
    # pack grab (a side-story marked monitored shouldn't make us grab an
    # unrelated mainline pack).
    if respect_monitoring and vol_num is None and vol_rng is not None:
        with get_db() as db:
            has_monitored = db.execute(
                "SELECT 1 FROM volumes WHERE series_id=? AND status='wanted' AND monitored=1"
                " AND volume_num >= ? AND volume_num <= ?"
                " AND COALESCE(is_special, 0) = 0 LIMIT 1",
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
                    _log_grab_rejection(series_id, title,
                                        'repack skipped: propers_and_repacks=do_not_upgrade')
                    return False
                elif _prop_cfg == 'prefer_and_upgrade':
                    # Only grab if same release group (cross-group repacks rejected)
                    existing_group = (existing_vol['release_group'] or '').strip().lower()
                    new_group      = parse_release_group(title).lower()
                    if existing_group and new_group and existing_group != new_group:
                        _log_grab_rejection(
                            series_id, title,
                            f'cross-group repack rejected '
                            f'(existing={existing_group!r}, repack={new_group!r})'
                        )
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
                _log_grab_rejection(
                    series_id, title,
                    f'quality {_new_q} below cutoff {_cutoff}'
                )
                return False

    # Outer timeout on the grab operation as a whole. grab_url has its own
    # per-HTTP-request timeout (30s for qBittorrent, similar for SABnzbd),
    # but a slow client + retry logic can accumulate well past that. Without
    # this wrapper an indexer/client combination that hangs will pin the URL
    # in _GRABBING_URLS until the httpx timeout chains expire, blocking any
    # retry for minutes.
    try:
        try:
            ok, client_name, dl_id = await asyncio.wait_for(
                grab_url(item['url'], protocol, save_path=save_path,
                         torrent_name=title),
                timeout=45,
            )
        except asyncio.TimeoutError:
            log_event(
                'grab_timeout',
                f'grab_url exceeded 45s for {title[:120]}',
                series_id,
            )
            return False
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

            # Mark the resolved volume stubs for chapter packs.
            # Float-precise match (no CAST collapse): a chapter pack that
            # maps to volume 3 must not also flip volume 3.5 to grabbed.
            # Skip specials so a mainline grab doesn't touch side-story rows.
            if covered_vols:
                placeholders = ','.join('?' * len(covered_vols))
                _float_vols = [float(v) for v in covered_vols]
                db.execute(
                    f"UPDATE volumes SET status='grabbed', grabbed_at=?, source_url=?,"
                    f" download_id=?, torrent_name=?, client=?, indexer=?, protocol=?,"
                    f" release_group=?, size_bytes=?, edition_type=?, language=?"
                    f" WHERE series_id=? AND status='wanted'"
                    f" AND volume_num IS NOT NULL AND volume_num IN ({placeholders})"
                    f" AND COALESCE(is_special, 0) = 0",
                    [now, item['url'], dl_id, title, client_name,
                     indexer, protocol, rgroup, size, edition, lang,
                     series_id, *_float_vols]
                )
                covered_vol_ids = [
                    r['id'] for r in db.execute(
                        f"SELECT id FROM volumes WHERE series_id=? AND volume_num IS NOT NULL"
                        f" AND volume_num IN ({placeholders})"
                        f" AND COALESCE(is_special, 0) = 0",
                        [series_id, *_float_vols]
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
# Individual task handles are no longer module globals; every background loop
# is tracked via _BACKGROUND_TASKS (see create_background_task above).

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

_MDX_BACKOFF_UNTIL: float = 0.0


def _mdx_backoff_active() -> bool:
    import time as _t
    return _t.time() < _MDX_BACKOFF_UNTIL


def _mdx_set_backoff(seconds: float, reason: str) -> None:
    """Extend the MangaDex backoff deadline. Persisted only in-process —
    a restart resets it, which is fine because the backfill loop re-runs
    from scratch at startup anyway and will re-hit any ongoing rate limit
    immediately."""
    global _MDX_BACKOFF_UNTIL
    import time as _t
    deadline = _t.time() + max(seconds, 1.0)
    if deadline > _MDX_BACKOFF_UNTIL:
        _MDX_BACKOFF_UNTIL = deadline
        print(f"[Backfill] MangaDex backoff set: {int(seconds)}s ({reason})")


async def _backfill_metadata_loop():
    """
    At startup, backfill MangaDex ID + cross-references (MAL/MU) for series missing them.
    Runs once, with a small delay between each to respect MangaDex rate limits (~5 req/s).
    When upstream signals rate-limiting (via an httpx.HTTPStatusError from a 429),
    respect the Retry-After value and defer remaining work until the deadline
    elapses so we don't burn through IP-ban thresholds.
    """
    await asyncio.sleep(10)  # let startup settle first
    with get_db() as db:
        missing = db.execute(
            "SELECT id FROM series WHERE mangadex_id IS NULL OR mal_id IS NULL OR mu_id IS NULL"
            " OR (mangadex_id IS NOT NULL AND chapter_vol_map IS NULL)"
        ).fetchall()
    for row in missing:
        # If we've been rate-limited recently, hold off until the deadline
        while _mdx_backoff_active():
            import time as _t
            wait = max(1.0, _MDX_BACKOFF_UNTIL - _t.time())
            await asyncio.sleep(min(wait, 30))
        try:
            await refresh_mangadex_map(row['id'])
        except Exception as e:
            print(f"[Startup] metadata backfill error for series {row['id']}: {e}")
            _maybe_backoff_from_exception(e)
        await asyncio.sleep(2)  # ~0.5 req/s — well under MangaDex limit

    # Sync MangaDex chapter manifests for series that have mangadex_id but no chapter rows
    with get_db() as db:
        needs_sync = db.execute(
            "SELECT id FROM series WHERE mangadex_id IS NOT NULL"
            " AND NOT EXISTS (SELECT 1 FROM mangadex_chapters m WHERE m.series_id=series.id)"
        ).fetchall()
    for row in needs_sync:
        while _mdx_backoff_active():
            import time as _t
            wait = max(1.0, _MDX_BACKOFF_UNTIL - _t.time())
            await asyncio.sleep(min(wait, 30))
        try:
            await _mdx_router.sync_mangadex_chapters(row['id'])
        except Exception as e:
            print(f"[Startup] MangaDex chapter sync error for series {row['id']}: {e}")
            _maybe_backoff_from_exception(e)
        await asyncio.sleep(1.5)


def _maybe_backoff_from_exception(exc: Exception) -> None:
    """If an httpx exception carries a 429 response with Retry-After, honour
    it. Otherwise this is a no-op — the caller already handled the error."""
    resp = getattr(exc, 'response', None)
    if resp is None:
        return
    try:
        status = getattr(resp, 'status_code', None)
        if status == 429:
            ra = resp.headers.get('Retry-After') if hasattr(resp, 'headers') else None
            seconds = _parse_retry_after_seconds(ra) if ra else 60.0
            _mdx_set_backoff(seconds or 60.0, f'Retry-After={ra!r}')
    except Exception:
        pass


def _parse_retry_after_seconds(raw: str | None) -> float | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        import datetime as _dt
        dt = parsedate_to_datetime(raw)
        if dt is None:
            return None
        return max(0.0, (dt - _dt.datetime.now(_dt.timezone.utc)).total_seconds())
    except Exception:
        return None

def cleanup_stuck_state(*, grabbed_stale_hours: int = 6,
                        queue_stale_days: int = 30,
                        max_rows_per_sweep: int = 500) -> dict:
    """Reconcile the three stuck-state patterns the app can otherwise
    accumulate indefinitely:

      1. Volumes in status='grabbed' whose grabbed_at is older than
         ``grabbed_stale_hours`` and whose download_id is NULL. These
         got stranded when a client crash lost the download_id before
         the volume row was fully updated. Reset to 'wanted' so the
         series can pick them back up.

      2. pending_releases whose series has been deleted or unmonitored
         since the release was queued. No legitimate grab will fire
         for these, but the auto-prune only removes rows >7 days old
         — leaving a long tail of junk in the queue UI.

      3. import_queue rows stuck in status='pending' or 'partial' for
         more than ``queue_stale_days`` days. Mark them failed so the
         next periodic reconcile can return the associated volumes
         to 'wanted'.

    Every destructive action is logged via `log_event` so operators
    can see what moved. The ``max_rows_per_sweep`` cap exists as a
    safety valve against a bad filter matching the whole table — if
    we ever hit it, the next sweep picks up the rest.

    Returns a dict of counts for visibility in tests and logs.
    """
    # One transaction per phase — not one big transaction for all three.
    # Each phase might process hundreds of rows; keeping each phase its
    # own transaction lets other writers slot in between. The stats dict
    # is accumulated across phases at function scope.
    stats = {
        'volumes_reset':   0,
        'pending_deleted': 0,
        'queue_failed':    0,
    }

    # ── Phase 1: stale grabbed volumes ──
    with get_db() as db:
        # (1) Stale grabbed volumes with no download_id
        stale = db.execute(
            "SELECT v.id, v.series_id, v.volume_num, s.title"
            "  FROM volumes v LEFT JOIN series s ON s.id=v.series_id"
            " WHERE v.status='grabbed' AND v.download_id IS NULL"
            "   AND v.grabbed_at IS NOT NULL"
            "   AND v.grabbed_at < datetime('now', ?)"
            "   AND (v.client IS NULL OR v.client != 'suwayomi')"
            " LIMIT ?",
            (f'-{int(grabbed_stale_hours)} hours', max_rows_per_sweep)
        ).fetchall()
        for row in stale:
            db.execute(
                "UPDATE volumes SET status='wanted', grabbed_at=NULL,"
                " source_url=NULL, download_id=NULL, torrent_name=NULL,"
                " indexer=NULL, protocol=NULL, client=NULL, release_group=NULL,"
                " imported_at=NULL WHERE id=?",
                (row['id'],)
            )
            stats['volumes_reset'] += 1
        if stale:
            log_event(
                'stuck_cleanup',
                f'reset {len(stale)} stale grabbed-with-no-download_id volume(s) '
                f'(older than {grabbed_stale_hours}h)',
                db=db,
            )

    # ── Phase 2: pending_releases orphans ──
    with get_db() as db:
        orphans = db.execute(
            "SELECT pr.id, pr.series_id, pr.title, s.monitored"
            "  FROM pending_releases pr"
            "  LEFT JOIN series s ON s.id=pr.series_id"
            " WHERE s.id IS NULL OR s.monitored=0"
            " LIMIT ?",
            (max_rows_per_sweep,)
        ).fetchall()
        if orphans:
            db.execute(
                "DELETE FROM pending_releases WHERE id IN ("
                + ','.join('?' * len(orphans)) + ")",
                tuple(o['id'] for o in orphans)
            )
            stats['pending_deleted'] = len(orphans)
            log_event(
                'stuck_cleanup',
                f'deleted {len(orphans)} pending_release(s) for deleted or '
                f'unmonitored series',
                db=db,
            )

    # ── Phase 3: import_queue stuck in pending/partial ──
    with get_db() as db:
        stale_queue = db.execute(
            "SELECT id, series_id, torrent_name"
            "  FROM import_queue"
            " WHERE status IN ('pending', 'partial')"
            "   AND created_at < datetime('now', ?)"
            " LIMIT ?",
            (f'-{int(queue_stale_days)} days', max_rows_per_sweep)
        ).fetchall()
        for row in stale_queue:
            db.execute(
                "UPDATE import_queue SET status='failed' WHERE id=?",
                (row['id'],)
            )
            # Return any grabbed volumes associated via download_id back to wanted
            db.execute(
                "UPDATE volumes SET status='wanted', grabbed_at=NULL,"
                " download_id=NULL, torrent_name=NULL, indexer=NULL,"
                " protocol=NULL, client=NULL, release_group=NULL"
                " WHERE download_id IN ("
                "   SELECT download_id FROM import_queue WHERE id=?"
                " ) AND status='grabbed'",
                (row['id'],)
            )
            stats['queue_failed'] += 1
        if stale_queue:
            log_event(
                'stuck_cleanup',
                f'failed {len(stale_queue)} import_queue row(s) stuck in '
                f'pending/partial for >{queue_stale_days} days',
                db=db,
            )

    return stats


async def _stuck_state_cleanup_loop():
    """Run cleanup_stuck_state hourly. Kept separate from backlog_search_loop
    so a failure in one doesn't hide the other."""
    await asyncio.sleep(300)   # let startup settle and the boot-time one-shot finish
    while True:
        try:
            stats = cleanup_stuck_state()
            if any(stats.values()):
                print(f"[stuck-cleanup] {stats}")
        except Exception as e:
            print(f"[stuck-cleanup] error: {e}")
        await asyncio.sleep(3600)   # 1 hour


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


# ── Background task lifecycle ────────────────────────────────────────────────
# All long-running asyncio loops (rss, status, refresh, backfill, backlog,
# suwayomi, rescan, import-list, backup, stuck-retry) are registered here so
# lifespan shutdown can cancel them, and so an unexpected exit from one
# surfaces in the log instead of silently dying.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def create_background_task(coro, name: str) -> asyncio.Task:
    """Start a long-running background task and track its lifecycle.

    - Names the task (visible in `asyncio.all_tasks()`).
    - Stores a strong reference so Python's GC doesn't collect it mid-run
      (raw asyncio.create_task() emits a "Task was destroyed but it is
      pending" warning if the return value isn't held).
    - Removes the reference when the task finishes.
    - Logs (warning-level) if the task exited via an uncaught exception.
      Clean cancellation on shutdown is silent.
    """
    import logging as _logging
    log = _logging.getLogger(__name__)

    task = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _BACKGROUND_TASKS.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            log.error("background task %r exited with exception: %r",
                      t.get_name(), exc, exc_info=exc)

    task.add_done_callback(_on_done)
    return task


async def _cancel_background_tasks() -> None:
    """Cancel every registered background task and await graceful exit.

    Called from lifespan shutdown. Uses return_exceptions so one slow task
    doesn't starve the others; each task's final state is logged by its
    own done-callback.
    """
    tasks = list(_BACKGROUND_TASKS)
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Apply WAL journal mode once — persistent setting, so every later
    # connection inherits it without re-running the (write-locked) PRAGMA.
    from shared import ensure_wal_journal_mode as _ensure_wal
    _ensure_wal()
    # Initialise the secret-encryption cipher (H4 PR #1) BEFORE load_config
    # so load_config can transparently decrypt enc:v1: values for the
    # SETTINGS_SECRET_KEYS allowlist on the way into CONFIG.
    try:
        from security import load_or_create_secret_cipher, SecretCipherUnavailable
        load_or_create_secret_cipher("/config")
    except SecretCipherUnavailable as _e:
        # Cipher unavailable. The app can still boot — load_config falls
        # back to plaintext-only and any enc:v1: values stay opaque.
        # api-key middleware will fail closed on a blank api_key (H2),
        # which is the right outcome if the operator's MANGARR_SECRET_KEY
        # is missing/wrong: refuse to expose the API rather than 200 it
        # without auth.
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "secret cipher unavailable at startup: %s — encryption-at-rest disabled", _e,
        )
    load_config()
    # Defense in depth: if api_key is still blank after init_db + load_config
    # (DB row nulled, partial migration, etc.), generate one now. The
    # middleware fails closed on blank api_key, so the alternative is the
    # whole API returning 401 until an operator notices.
    ensure_api_key()
    # H4 PR #2: encrypt any plaintext settings secrets at rest. Idempotent;
    # no-op when cipher unavailable. Runs after ensure_api_key so the
    # auto-seeded api_key (which is written plaintext by ensure_api_key)
    # gets encrypted on the same boot it was created.
    migrate_encrypt_settings_secrets()
    # H4 PR #3: encrypt indexers.api_key and download_clients.password
    # at rest. Idempotent; no-op when cipher unavailable. Read paths
    # decrypt per-call via security.decrypt_secret_safe() so the
    # caller-visible plaintext is unchanged.
    migrate_encrypt_table_column_secrets()
    # H4 PR #4: encrypt per-provider secret fields inside the JSON blob
    # stored in notification_connections.settings. Per-row atomicity so
    # one malformed JSON row doesn't block siblings.
    migrate_encrypt_notification_connection_secrets()
    # Re-run load_config so CONFIG reflects the just-encrypted-and-decrypted
    # values. Without this, the in-memory CONFIG still holds whatever the
    # first load_config produced; for the api_key auto-seed path that's
    # already correct (ensure_api_key writes both DB and CONFIG), but
    # other flows that read settings post-migration get the plaintext
    # round-tripped through Fernet — same value, just paranoid consistency.
    load_config()
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
            # Torrent download path is separate from the library path so the
            # standard *arr convention holds: qBit writes to e.g.
            # /data/torrents/manga while the library lives at /data/media/manga.
            # When torrent_save_path is empty we fall back to save_path — the
            # old single-directory behaviour, preserved for existing installs.
            _qbit_save = (get_cfg('torrent_save_path', '') or '').strip() \
                         or get_cfg('save_path')
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    f"{_qhost}/api/v2/auth/login",
                    data={'username': _quser, 'password': _qpw}
                )
                if 'Ok' in r.text:
                    await client.post(
                        f"{_qhost}/api/v2/torrents/createCategory",
                        data={'category': _qcat, 'savePath': _qbit_save}
                    )
    except Exception as e:
        # Best-effort at startup: failure here (qBit offline, bad creds,
        # wrong host) doesn't block the app, but it used to swallow
        # silently — users then wondered why their category never
        # appeared. Log at INFO so it's visible in normal operation
        # without being noisy when qBit genuinely isn't configured.
        # Do NOT include _qpw or _quser in the log message.
        import logging as _logging
        _logging.getLogger(__name__).info(
            "startup: qBit category bootstrap skipped (%r)", e,
        )
    # All long-running background loops are registered with the tracker so
    # they can be cancelled on shutdown and their unexpected exits logged.
    # Previously seven of the nine were fire-and-forget tasks without a
    # stored reference — see create_background_task() docstring.
    # Event-loop lag watchdog — no-op unless MANGARR_DEBUG_TIMING=1.
    # Helps diagnose issue #31 follow-up A stalls during investigation.
    from shared import event_loop_lag_monitor as _event_loop_lag
    create_background_task(_event_loop_lag(),                  name="event_loop_lag_monitor")

    create_background_task(rss_loop(),                         name="rss_loop")
    create_background_task(status_loop(),                      name="status_loop")
    create_background_task(refresh_ongoing_loop(),             name="refresh_ongoing_loop")
    create_background_task(_backfill_metadata_loop(),          name="backfill_metadata_loop")
    create_background_task(backlog_search_loop(),              name="backlog_search_loop")
    create_background_task(_stuck_state_cleanup_loop(),        name="stuck_state_cleanup_loop")
    create_background_task(_swy_router.suwayomi_monitor_loop(), name="suwayomi_monitor_loop")
    create_background_task(rescan_loop(),                      name="rescan_loop")
    create_background_task(_import_list_loop(),                name="import_list_loop")
    create_background_task(_backup_loop(),                     name="backup_loop")
    # Poll qBit/SAB in the background so /queue renders from cached
    # snapshots instead of making live HTTP calls on every pageview.
    from status_cache import download_status_refresh_loop as _dl_status_loop
    create_background_task(_dl_status_loop(),                  name="download_status_refresh_loop")
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
        create_background_task(_retry_stuck(), name="retry_stuck_imports")
    yield
    # Cancel every registered background task and wait for graceful exit.
    # Done-callbacks log unexpected exceptions; cancellations are silent.
    await _cancel_background_tasks()

# ── Helpers ───────────────────────────────────────────────────────────────────
def get_root_folders(db) -> list:
    return db.execute("SELECT * FROM root_folders ORDER BY is_default DESC, label, path").fetchall()


def _resolve_series_dest_root(db, series_rf_id: int | None, rf_row) -> str:
    """Return the library destination root path for a series.

    Assumes PR B's guarantee that every series has a root_folder_id at
    creation time. Handles the edge case where an operator deletes a
    root folder that still has series pointing at it — in that case we
    fall back to any remaining folder and log a warning so the operator
    can re-assign.

    If no root folders exist at all the caller has a bigger problem
    than this function can solve — raises RuntimeError with a clear
    message rather than silently landing imports in a half-configured
    path.
    """
    # Happy path: series has a folder and the row exists.
    if rf_row:
        return rf_row['path']
    # Edge: series references a deleted folder, or was never assigned.
    # Fall back to any available folder and log.
    fallback = resolve_root_folder_id(db)
    if fallback is not None:
        fb_row = db.execute(
            "SELECT path FROM root_folders WHERE id=?", (fallback,)
        ).fetchone()
        log_event(
            'warning',
            f"series root_folder_id={series_rf_id!r} did not resolve; "
            f"falling back to root_folder_id={fallback} ({fb_row['path']!r}). "
            f"Re-assign the series to an existing folder in the editor.",
            db=db,
        )
        return fb_row['path']
    # Terminal: no folders at all.
    raise RuntimeError(
        "No root folders configured. Add one in Settings before "
        "attempting to import or place files."
    )


def resolve_root_folder_id(db, preferred_id: int | None = None) -> int | None:
    """Pick the root_folder_id a newly-created series should carry.

    Order of preference:
      1. ``preferred_id`` if it refers to an existing row.
      2. The folder flagged ``is_default=1``.
      3. The lowest-id folder (safety net if no default is flagged).

    Returns None only when no root folders exist at all — callers are
    expected to check and surface a clear error to the operator instead
    of silently leaving root_folder_id NULL. Requiring a folder at
    creation time matches the Sonarr/Radarr model and removes the
    save_path fallback that used to paper over this case.
    """
    if preferred_id:
        ok = db.execute(
            "SELECT 1 FROM root_folders WHERE id=?", (preferred_id,)
        ).fetchone()
        if ok:
            return preferred_id
    row = db.execute(
        "SELECT id FROM root_folders ORDER BY is_default DESC, id LIMIT 1"
    ).fetchone()
    return row[0] if row else None

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
# Cover helpers live in cover_images.py. Re-exported here so existing
# call sites (`main.download_cover`, `_m.download_cover` from routers)
# keep working unchanged during the incremental main.py split.
from cover_images import download_cover, extract_cbz_cover  # noqa: F401

# ── App ───────────────────────────────────────────────────────────────────────
app       = FastAPI(lifespan=lifespan)
app.mount("/covers", StaticFiles(directory="/config/covers"), name="covers")
app.mount("/static", StaticFiles(directory="/app/static"),   name="static")
templates = Jinja2Templates(directory="/app/templates")

# ── Middleware moved to middleware.py ────────────────────────────────────────
# ApiKeyMiddleware + CSRFMiddleware live in middleware.py. Re-exported
# here so `app.add_middleware(...)` below can reference them by bare
# name (FastAPI looks them up in this module's globals). The _CSRF_*
# constants are re-exported for any external callers (none known, but
# keeping the surface identical through the split).
from middleware import (  # noqa: F401
    ApiKeyMiddleware, CSRFMiddleware, _should_secure_cookie,
    _CSRF_COOKIE, _CSRF_HEADER, _CSRF_FIELD, _CSRF_SKIP_PREFIXES,
)


# Temporary timing instrumentation — env-gated. Logs total request duration
# to stderr for every request when MANGARR_DEBUG_TIMING=1. Used to diagnose
# issue #31 page-navigation stalls. Safe to keep in the codebase; off by
# default imposes ~zero overhead (one env var check per request).
if os.environ.get("MANGARR_DEBUG_TIMING") == "1":
    import time as _time_mod

    class _TimingMiddleware:
        def __init__(self, app):
            self.app = app
        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return
            t0 = _time_mod.perf_counter()
            status_holder = {"code": 0}
            async def _send(message):
                if message["type"] == "http.response.start":
                    status_holder["code"] = message.get("status", 0)
                await send(message)
            try:
                await self.app(scope, receive, _send)
            finally:
                dt_ms = (_time_mod.perf_counter() - t0) * 1000
                path = scope.get("path", "")
                print(f"[TIMING] {dt_ms:>8.1f}ms  {status_holder['code']}  {path}",
                      flush=True)
    app.add_middleware(_TimingMiddleware)

app.add_middleware(CSRFMiddleware)
app.add_middleware(ApiKeyMiddleware)

def _from_json(s):
    try:
        return json.loads(s) if s else {}
    except Exception:
        return {}

def _ch_label_filter(row) -> str:
    """Jinja filter: render a chapter row's number, honoring chapter_range_end.

    `row` is a dict-like (sqlite3.Row or dict) exposing chapter_num and,
    optionally, chapter_range_end. Returns "1", "1.5", or "1-2".
    """
    if row is None:
        return ""
    try:
        n = row["chapter_num"]
    except (KeyError, IndexError, TypeError):
        return ""
    if n is None:
        return ""
    end = None
    try:
        end = row["chapter_range_end"]
    except (KeyError, IndexError, TypeError):
        end = None
    n_disp = int(n) if n == int(n) else n
    if end is not None and end > n:
        e_disp = int(end) if end == int(end) else end
        return f"{n_disp}-{e_disp}"
    return f"{n_disp}"


templates.env.filters['format_bytes']    = format_bytes
templates.env.filters['format_protocol'] = format_protocol
templates.env.filters['format_client']   = format_client
templates.env.filters['vol_display']     = vol_num_to_display
templates.env.filters['quality_rank']    = quality_rank
templates.env.filters['from_json']       = _from_json
templates.env.filters['ch_label']        = _ch_label_filter

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
                    _float_covered = [float(v) for v in covered]
                    cur = db.execute(
                        f"UPDATE volumes SET status='grabbed', grabbed_at=?, torrent_name=? "
                        f"WHERE series_id=? AND status='wanted' "
                        f"AND volume_num IS NOT NULL "
                        f"AND volume_num IN ({placeholders}) "
                        f"AND COALESCE(is_special, 0) = 0",
                        [now, name, p['series_id'], *_float_covered]
                    )
                    total_marked += cur.rowcount
    return total_marked

