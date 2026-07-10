"""Settings schema, validators, and encryption migrations.

Ninth module extracted from main.py. Holds the declarative config
surface and the one-shot migrations that encrypt existing plaintext
secrets on first boot after the encryption-at-rest feature landed.

Contents:
  - ENV_DEFAULTS                       — (env-var, default) for every
                                         settings-table key
  - SETTINGS_SECRET_KEYS               — keys whose values are credentials
  - SETTINGS_VALIDATORS                — type/range/enum rules per key
  - _validate_setting_value            — apply the rules (pure fn)
  - TABLE_SECRET_COLUMNS               — (secret col, label col) per
                                         table that holds credentials
  - NOTIFICATION_SECRET_KEYS_BY_TYPE   — per-provider JSON blob keys
                                         that must be encrypted
  - migrate_encrypt_settings_secrets           — settings table
  - migrate_encrypt_table_column_secrets       — indexers / download_clients
  - migrate_encrypt_notification_connection_secrets — per-row JSON blobs

`load_config`, `get_cfg`, `CONFIG`, and `ensure_api_key` stay in
main.py — they read/write the shared `CONFIG` dict and would need a
deeper refactor to move safely.

Pure move — no behaviour changes.
"""
from __future__ import annotations

import logging

from shared import get_db


# ── ENV defaults ─────────────────────────────────────────────────────────────

ENV_DEFAULTS = {
    'instance_name':       ('MANGARR_INSTANCE_NAME', 'Mangarr'),
    'log_level':           ('MANGARR_LOG_LEVEL',     'INFO'),
    # External URL prefix advertised to API clients and shown in settings.
    # Leave empty when Mangarr is served at the domain root. When set,
    # operators must configure the reverse proxy to strip the prefix before
    # forwarding requests to the container.
    'url_base':            ('MANGARR_URL_BASE',      ''),
    'save_path':           ('MANGA_SAVE_PATH',  '/manga'),
    # torrent_save_path: where qBittorrent writes in-progress downloads.
    # When empty, falls back to save_path (keeps the single-directory
    # default from before this setting was split out). Setting it enables
    # the standard *arr separation: downloads in /data/torrents/manga,
    # library in /data/media/manga. Both must be on the same filesystem
    # if import_mode='hardlink' (the default) — see docs/deployment.md.
    'torrent_save_path':   ('MANGA_TORRENT_PATH', ''),
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
    # Import concurrency
    'max_concurrent_imports':   (None,    '2'),  # Max concurrent imports (2 for spinning disks, 5+ for SSD)
    # Import free-space guard. 0 disables the guard; otherwise imports
    # require this many MiB to remain free after planned staging bytes.
    'minimum_free_space_mb':    (None,    '0'),
}


# ── Encryption-at-rest config ────────────────────────────────────────────────

# Settings-table keys whose values are credentials and must be encrypted
# at rest (H4 PR #2). Values for these keys are decrypted on the way into
# CONFIG by load_config and encrypted on the way to the DB by the
# settings-form handlers and migrate_encrypt_settings_secrets().
#
# komga_user is included as a pair with komga_pass — usernames aren't
# strictly secrets but treating credentials atomically avoids partial
# leakage if the DB dump is exposed.
SETTINGS_SECRET_KEYS = frozenset({
    "api_key",
    "komga_user",
    "komga_pass",
    "google_books_api_key",
})

# Per-table secret columns encrypted at rest (H4 PR #3).
# Structure: {table_name: (secret_column, label_column_for_logs)}
# Label column is used only for WARNING log context when a row fails to
# decrypt — never for routing or business logic. notification_connections
# is intentionally absent; it lands in a later PR.
TABLE_SECRET_COLUMNS = {
    "indexers":         ("api_key",  "name"),
    "download_clients": ("password", "name"),
}

# Per-provider secret keys that live INSIDE the JSON blob stored in
# notification_connections.settings (H4 PR #4). For each row we parse
# the JSON, encrypt the keys listed for that row's type, preserve
# everything else unchanged, and re-serialize. Keys not in this map
# (server/host/port/chat_id/method/etc.) stay plaintext.
#
# URL-shaped credentials (Discord/Slack webhooks, Apprise, generic
# webhook) are included because the URL IS the bearer token for those
# providers — anyone who reads the URL can post to it.
NOTIFICATION_SECRET_KEYS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "discord":    ("webhook_url",),
    "slack":      ("webhook_url",),
    "telegram":   ("bot_token",),
    "ntfy":       ("token",),
    "gotify":     ("app_token",),
    "pushover":   ("user_key", "api_token"),
    "webhook":    ("url",),
    "email":      ("password",),
    "apprise":    ("url", "config_key"),
    "pushbullet": ("access_token",),
}


# ── Settings-value validation ────────────────────────────────────────────────

# Value-type validation for settings table entries. Before this check
# load_config accepted whatever string was in the DB, so a stray
# 'rss_interval=abc' or 'import_mode=hardlinkk' would sit quietly in
# CONFIG until a later int() or enum check raised.
#
# Each entry: key → ('int', min, max) | ('enum', allowed_set) | ('bool',)
# On validation failure, load_config falls back to the key's default
# value from ENV_DEFAULTS and logs a WARNING naming the key. Keys not
# listed here are accepted verbatim — only the ones with semantic
# constraints get enforced.
SETTINGS_VALIDATORS: dict = {
    'rss_interval':            ('int', 30, 86400 * 7),
    'refresh_interval':        ('int', 60, 86400 * 30),
    'grab_delay_minutes':      ('int', 0, 60 * 24 * 30),
    'suwayomi_check_interval': ('int', 60, 86400 * 7),
    'blocklist_ttl_days':      ('int', 0, 365 * 5),
    'import_mode':             ('enum', frozenset({'hardlink', 'move', 'copy'})),
    'log_level':               ('enum', frozenset({'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'})),
    'url_base':                ('url_base',),
    'komga_scan_enabled':      ('bool',),
    'remove_completed':        ('bool',),
    'ddl_grab_mode':           ('enum', frozenset({'fallback', 'only'})),
    'quality_cutoff':          ('enum', frozenset({'', 'pdf', 'epub', 'cbr', 'cbz', 'rar', 'zip', 'mobi'})),
}


def normalize_url_base(value) -> str:
    """Normalize a reverse-proxy URL prefix.

    Empty means "served at /". Non-empty values must be a single path prefix
    such as "/mangarr"; query strings, fragments, schemes, and traversal are
    rejected by returning an empty string.
    """
    raw = str(value or "").strip()
    if not raw or raw == "/":
        return ""
    if "://" in raw or "?" in raw or "#" in raw or "\\" in raw:
        return ""
    if not raw.startswith("/"):
        raw = f"/{raw}"
    raw = raw.rstrip("/")
    parts = [part for part in raw.split("/") if part]
    if not parts or any(part in (".", "..") for part in parts):
        return ""
    return "/" + "/".join(parts)


def _validate_setting_value(key: str, value, default):
    """Return value if it passes validation; else default.
    Never raises — logs a WARNING on mismatch so operators can trace it."""
    spec = SETTINGS_VALIDATORS.get(key)
    if spec is None or value is None:
        return value
    log = logging.getLogger(__name__)
    kind = spec[0]
    if kind == 'int':
        _, lo, hi = spec
        try:
            iv = int(value)
        except (TypeError, ValueError):
            log.warning("settings[%s]: %r is not an integer; using default %r",
                        key, value, default)
            return default
        if not (lo <= iv <= hi):
            log.warning("settings[%s]: %d out of range [%d, %d]; using default %r",
                        key, iv, lo, hi, default)
            return default
        return str(iv)
    if kind == 'enum':
        _, allowed = spec
        if str(value) not in allowed:
            log.warning("settings[%s]: %r not in allowed set %s; using default %r",
                        key, value, sorted(allowed), default)
            return default
        return value
    if kind == 'bool':
        if str(value).lower() in ('true', 'false'):
            return str(value).lower()
        log.warning("settings[%s]: %r is not a bool-like string; using default %r",
                    key, value, default)
        return default
    if kind == 'url_base':
        normalized = normalize_url_base(value)
        if normalized or value in ("", None, "/"):
            return normalized
        log.warning("settings[%s]: %r is not a valid URL base; using default %r",
                    key, value, default)
        return default
    return value


# ── Encryption migrations ────────────────────────────────────────────────────

def migrate_encrypt_settings_secrets():
    """Encrypt any plaintext values for keys in SETTINGS_SECRET_KEYS.

    Idempotent: enc:v1: values are skipped, empty values are skipped.
    Atomic: runs in a single get_db() transaction; if any UPDATE
    raises, the whole batch rolls back and existing plaintext stays
    plaintext (operator can investigate before retry).

    No-op when the cipher is unavailable — plaintext stays plaintext;
    a single WARNING line records the skip so operators see why
    encryption-at-rest hasn't kicked in yet.

    Returns the count of rows updated. Never raises through to the
    caller — startup must keep going either way.
    """
    log = logging.getLogger(__name__)
    from security import (
        secret_cipher_loaded, encrypt_secret, is_encrypted_secret,
    )
    if not secret_cipher_loaded():
        log.warning(
            "settings secrets migration skipped: secret cipher unavailable; "
            "set MANGARR_SECRET_KEY or ensure /config is writable",
        )
        return 0
    placeholders = ",".join("?" * len(SETTINGS_SECRET_KEYS))
    updated = 0
    try:
        with get_db() as db:
            rows = db.execute(
                f"SELECT key, value FROM settings WHERE key IN ({placeholders})",
                tuple(SETTINGS_SECRET_KEYS),
            ).fetchall()
            for row in rows:
                v = row['value']
                if not v or is_encrypted_secret(v):
                    continue
                ct = encrypt_secret(v)
                db.execute(
                    "UPDATE settings SET value=? WHERE key=?", (ct, row['key']),
                )
                updated += 1
        if updated:
            log.info("encrypted %d settings secret(s) at rest", updated)
        return updated
    except Exception as e:
        log.error(
            "settings secrets migration failed; rolled back: %s: %s",
            type(e).__name__, e,
        )
        return 0


def migrate_encrypt_table_column_secrets():
    """Encrypt plaintext values in TABLE_SECRET_COLUMNS at rest.

    Idempotent: enc:v1: values are skipped, NULL / empty values are
    skipped. Atomic: one get_db() transaction per table; a failure in
    one table rolls back only that table — siblings remain unchanged
    (operator can investigate one table without losing progress on
    another).

    No-op when the cipher isn't loaded. Returns a dict of
    {table_name: rows_updated}. Never raises through to the caller.
    """
    log = logging.getLogger(__name__)
    from security import (
        secret_cipher_loaded, encrypt_secret, is_encrypted_secret,
    )
    if not secret_cipher_loaded():
        log.warning(
            "indexer/download-client secrets migration skipped: "
            "secret cipher unavailable",
        )
        return {t: 0 for t in TABLE_SECRET_COLUMNS}

    totals: dict[str, int] = {}
    for table, (col, _label) in TABLE_SECRET_COLUMNS.items():
        updated = 0
        try:
            with get_db() as db:
                rows = db.execute(
                    f"SELECT id, {col} FROM {table} WHERE {col} IS NOT NULL AND {col}!=''"
                ).fetchall()
                for row in rows:
                    v = row[col]
                    if not v or is_encrypted_secret(v):
                        continue
                    ct = encrypt_secret(v)
                    db.execute(
                        f"UPDATE {table} SET {col}=? WHERE id=?", (ct, row['id']),
                    )
                    updated += 1
            totals[table] = updated
            if updated:
                log.info("encrypted %d %s.%s value(s) at rest", updated, table, col)
        except Exception as e:
            log.error(
                "%s.%s migration failed; rolled back: %s: %s",
                table, col, type(e).__name__, e,
            )
            totals[table] = 0
    return totals


def migrate_encrypt_notification_connection_secrets():
    """Encrypt per-provider secret fields inside every row's
    notification_connections.settings JSON blob.

    Idempotent: enc:v1: values are skipped, NULL / empty values are
    skipped, unknown (non-secret) JSON keys are preserved unchanged.

    Atomicity model: per-row. A malformed JSON payload or a single
    row's encrypt failure logs an error and leaves that row unchanged
    — siblings still migrate. The alternative (one all-or-nothing
    transaction) would let a single corrupt row block encryption on a
    fleet of healthy ones; correctness per-row is the safer default
    here because each row is semantically independent.

    No-op when the cipher isn't loaded. Returns the count of rows
    actually updated. Never raises through to the caller.
    """
    import json as _json
    log = logging.getLogger(__name__)
    from security import secret_cipher_loaded, encrypt_if_cipher_available
    if not secret_cipher_loaded():
        log.warning(
            "notification_connections secrets migration skipped: "
            "secret cipher unavailable",
        )
        return 0
    updated = 0
    try:
        with get_db() as db:
            rows = db.execute(
                "SELECT id, name, type, settings FROM notification_connections"
            ).fetchall()
        # Process per-row outside the big SELECT cursor so each UPDATE
        # runs in its own short transaction; a single bad row doesn't
        # roll back the rest.
        for row in rows:
            ctype = (row['type'] or '').strip()
            secret_keys = NOTIFICATION_SECRET_KEYS_BY_TYPE.get(ctype)
            if not secret_keys:
                continue
            raw = row['settings'] or '{}'
            try:
                blob = _json.loads(raw)
            except Exception:
                log.warning(
                    "notification_connections id=%s (%s/%s) has malformed JSON — skipping",
                    row['id'], ctype, row['name'],
                )
                continue
            if not isinstance(blob, dict):
                continue
            changed = False
            for k in secret_keys:
                v = blob.get(k)
                if not v or not isinstance(v, str):
                    continue
                new_v = encrypt_if_cipher_available(v)
                if new_v != v:
                    blob[k] = new_v
                    changed = True
            if not changed:
                continue
            try:
                new_blob = _json.dumps(blob)
                with get_db() as db2:
                    db2.execute(
                        "UPDATE notification_connections SET settings=? WHERE id=?",
                        (new_blob, row['id']),
                    )
                updated += 1
            except Exception as e:
                log.error(
                    "notification_connections id=%s encrypt failed (%s): %s — "
                    "row unchanged",
                    row['id'], type(e).__name__, e,
                )
        if updated:
            log.info(
                "encrypted secret fields in %d notification_connections row(s)",
                updated,
            )
        return updated
    except Exception as e:
        log.error(
            "notification_connections migration failed: %s: %s",
            type(e).__name__, e,
        )
        return 0
