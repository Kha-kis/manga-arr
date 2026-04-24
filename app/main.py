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

# ── Metadata enrichment helpers moved to metadata_enrichment.py ─────────────
# DB-coupled helpers that enrich series data from MangaDex / Kitsu / Wikipedia /
# Google Books / MangaUpdates. Also holds _NON_STANDARD_STUB_EDITIONS and
# _EDITION_SEARCH_KEYWORDS constants. Re-exported so grab / import-pipeline /
# backfill / editor call sites keep working.
from metadata_enrichment import (  # noqa: F401
    _NON_STANDARD_STUB_EDITIONS, _EDITION_SEARCH_KEYWORDS,
    get_series_chapter_map, chapters_to_volume_set,
    _coverage_already_grabbed, _extract_map_from_cbzs,
    refresh_mangadex_map,
    fetch_wikipedia_volume_count, fetch_edition_volume_count,
    fetch_mu_metadata,
)

# ── Grab layer moved to grab.py ──────────────────────────────────────────────
# _log_grab_rejection, grab_item, _collect_and_score, _search_all,
# grab_existing, _grab_existing_inner, _select_covering_packs,
# search_complete_pack, poll_rss, and the _GRABBING_URLS in-flight set all
# live in grab.py now. Re-exported so routers and background loops keep
# working unchanged. Cross-module deps (log_event, add_history,
# broadcast_queue_event) are imported lazily inside grab.py to break cycles.
from grab import (  # noqa: F401
    _GRABBING_URLS, _log_grab_rejection,
    grab_item, _collect_and_score, _search_all,
    grab_existing, _grab_existing_inner,
    _select_covering_packs, search_complete_pack,
    poll_rss,
)


# ── Import pipeline moved to import_pipeline.py ──────────────────────────────
# _queue_import, check_download_status + _check_download_status_impl,
# _mark_downloaded, _ImportStaging (two-phase staging dir + commit_all /
# rollback), _IMPORT_SEM, claim_import_queue_row, _guarded_execute_import,
# _execute_import, _process_auto_import, and the _CHECK_DOWNLOAD_STATUS_LOCK
# single-flight guard all live in import_pipeline.py now. Re-exported so
# routers, background loops, and the lifespan stuck-retry all keep working
# unchanged. Cross-module deps (log_event, add_history, broadcast_queue_event,
# router modules) are imported lazily inside function bodies to break cycles.
from import_pipeline import (  # noqa: F401
    _queue_import,
    _CHECK_DOWNLOAD_STATUS_LOCK, check_download_status,
    _check_download_status_impl,
    _mark_downloaded,
    _ImportStaging, _IMPORT_SEM,
    claim_import_queue_row, _guarded_execute_import,
    _execute_import, _process_auto_import,
)

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



# ── Background loops + scheduler moved to tasks.py ────────────────────────────
# Every long-running asyncio loop (rss, status, refresh, backfill, backlog,
# stuck-cleanup, rescan, import-list, backup), the one-shot "Run Now" entry
# points (backlog_search, import_list_sync), the task-lifecycle harness
# (_BACKGROUND_TASKS, create_background_task, _cancel_background_tasks),
# the MangaDex backoff state (_MDX_BACKOFF_UNTIL, _mdx_backoff_active,
# _mdx_set_backoff, _maybe_backoff_from_exception, _parse_retry_after_seconds),
# and the three-phase reconciliation helper (cleanup_stuck_state) live in
# tasks.py now. Cross-module deps (log_event, check_download_status,
# DB_PATH, router modules) are imported lazily inside the loops to break
# cycles — same pattern as prior extractions.
from tasks import (  # noqa: F401
    rss_loop, status_loop, refresh_ongoing_loop,
    _backfill_metadata_loop, _stuck_state_cleanup_loop,
    backlog_search_loop, backlog_search,
    import_list_sync, rescan_loop,
    _import_list_loop, _backup_loop,
    _MDX_BACKOFF_UNTIL, _mdx_backoff_active, _mdx_set_backoff,
    _maybe_backoff_from_exception, _parse_retry_after_seconds,
    cleanup_stuck_state,
    _BACKGROUND_TASKS, create_background_task, _cancel_background_tasks,
)

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
# ── Helpers moved to helpers.py ─────────────────────────────────────────────
# Root-folder resolution + display formatters + Jinja filters all live
# in helpers.py. Re-exported so routers / templates keep working.
from helpers import (  # noqa: F401
    get_root_folders,
    _resolve_series_dest_root,
    resolve_root_folder_id,
    get_series_stats,
    format_bytes, format_protocol, format_client,
    _from_json, _ch_label_filter, _get_api_key_global,
)

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

templates.env.filters['format_bytes']    = format_bytes
templates.env.filters['format_protocol'] = format_protocol
templates.env.filters['format_client']   = format_client
templates.env.filters['vol_display']     = vol_num_to_display
templates.env.filters['quality_rank']    = quality_rank
templates.env.filters['from_json']       = _from_json
templates.env.filters['ch_label']        = _ch_label_filter

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

