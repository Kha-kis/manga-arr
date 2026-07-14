"""PR 7: SETTINGS_VALIDATORS rejects typed-setting rows whose value
fails type/range/enum checks, falling back to the ENV_DEFAULTS
default. Prior behaviour silently accepted any string from the DB
and let it blow up at the first int() or enum check deep in the
application."""
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


@pytest.fixture
def env():
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-settings-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()

    try:
        yield db.name
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


def _set(db_path, key, value):
    with sqlite3.connect(db_path) as c:
        c.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
            (key, value)
        )


# ── int range validation ──────────────────────────────────────────────────────

def test_valid_int_value_passes_through(env):
    import main
    _set(env, 'rss_interval', '600')
    main.load_config()
    assert main.CONFIG['rss_interval'] == '600'


def test_garbage_int_falls_back_to_default(env):
    import main
    _set(env, 'rss_interval', 'not-a-number')
    main.load_config()
    # default is '900' per ENV_DEFAULTS
    assert main.CONFIG['rss_interval'] == '900'


def test_out_of_range_int_falls_back(env):
    import main
    _set(env, 'rss_interval', '1')  # below minimum of 30
    main.load_config()
    assert main.CONFIG['rss_interval'] == '900'


def test_negative_int_falls_back(env):
    import main
    _set(env, 'blocklist_ttl_days', '-5')
    main.load_config()
    assert main.CONFIG['blocklist_ttl_days'] == '90'


# ── enum validation ──────────────────────────────────────────────────────────

def test_valid_enum_value_passes_through(env):
    import main
    _set(env, 'import_mode', 'move')
    main.load_config()
    assert main.CONFIG['import_mode'] == 'move'


def test_invalid_enum_falls_back_to_default(env):
    import main
    _set(env, 'import_mode', 'hardlinkk')  # typo
    main.load_config()
    assert main.CONFIG['import_mode'] == 'hardlink'


def test_log_level_env_default_is_validated(monkeypatch, env):
    import main
    monkeypatch.setenv('MANGARR_LOG_LEVEL', 'DEBUG')
    main.load_config()
    assert main.CONFIG['log_level'] == 'DEBUG'


def test_invalid_log_level_env_falls_back(monkeypatch, env):
    import main
    monkeypatch.setenv('MANGARR_LOG_LEVEL', 'TRACE')
    main.load_config()
    assert main.CONFIG['log_level'] == 'INFO'


def test_invalid_log_level_falls_back_to_env_default(monkeypatch, env):
    import main
    monkeypatch.setenv('MANGARR_LOG_LEVEL', 'WARNING')
    _set(env, 'log_level', 'TRACE')
    main.load_config()
    assert main.CONFIG['log_level'] == 'WARNING'


def test_quality_cutoff_empty_string_is_valid(env):
    import main
    _set(env, 'quality_cutoff', '')
    main.load_config()
    assert main.CONFIG['quality_cutoff'] == ''


def test_quality_cutoff_invalid_enum_falls_back(env):
    import main
    _set(env, 'quality_cutoff', 'pdfz')
    main.load_config()
    # Default per ENV_DEFAULTS is ''
    assert main.CONFIG['quality_cutoff'] == ''


@pytest.mark.parametrize("mode", ["fallback", "prefer", "only", "off"])
def test_ddl_grab_mode_valid_values_pass_through(env, mode):
    import main
    _set(env, 'ddl_grab_mode', mode)
    main.load_config()
    assert main.CONFIG['ddl_grab_mode'] == mode


def test_ddl_grab_mode_invalid_value_falls_back(env):
    import main
    _set(env, 'ddl_grab_mode', 'trackers-maybe')
    main.load_config()
    assert main.CONFIG['ddl_grab_mode'] == 'fallback'


# ── bool validation ──────────────────────────────────────────────────────────

def test_valid_bool_true_passes(env):
    import main
    _set(env, 'komga_scan_enabled', 'true')
    main.load_config()
    assert main.CONFIG['komga_scan_enabled'] == 'true'


def test_valid_bool_false_passes(env):
    import main
    _set(env, 'remove_completed', 'false')
    main.load_config()
    assert main.CONFIG['remove_completed'] == 'false'


def test_garbage_bool_falls_back(env):
    import main
    _set(env, 'remove_completed', 'yes')
    main.load_config()
    assert main.CONFIG['remove_completed'] == 'false'


# ── URL base validation ──────────────────────────────────────────────────────

def test_url_base_normalizes_missing_leading_slash(env):
    import main
    _set(env, 'url_base', 'mangarr/')
    main.load_config()
    assert main.CONFIG['url_base'] == '/mangarr'


def test_url_base_rejects_absolute_url(env):
    import main
    _set(env, 'url_base', 'https://example.invalid/mangarr')
    main.load_config()
    assert main.CONFIG['url_base'] == ''


def test_url_base_env_default_is_normalized(monkeypatch, env):
    import main
    monkeypatch.setenv('MANGARR_URL_BASE', 'mangarr/')
    main.load_config()
    assert main.CONFIG['url_base'] == '/mangarr'


# ── unvalidated keys pass through unchanged ─────────────────────────────────

def test_non_validated_key_passes_through_any_value(env):
    import main
    # file_format has no validator entry — accept any string
    _set(env, 'file_format', '{Series} v{Volume:02d}')
    main.load_config()
    assert main.CONFIG['file_format'] == '{Series} v{Volume:02d}'


# ── direct validator helper ─────────────────────────────────────────────────

def test_validator_helper_returns_default_on_garbage():
    from main import _validate_setting_value
    assert _validate_setting_value('rss_interval', 'abc', '900') == '900'


def test_validator_helper_returns_value_on_pass():
    from main import _validate_setting_value
    assert _validate_setting_value('rss_interval', '600', '900') == '600'


def test_validator_helper_ignores_unknown_key():
    from main import _validate_setting_value
    # 'made_up_key' has no validator entry — value passes through
    assert _validate_setting_value('made_up_key', 'literally-anything', '') == 'literally-anything'
