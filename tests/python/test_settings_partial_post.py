"""Regression coverage for partial-safe ``POST /settings`` persistence."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from httpx import Response


@dataclass(frozen=True)
class _ProcessState:
    main_config: dict[str, str]
    main_values: dict[str, str]
    shared_config: dict[str, str]
    shared_values: dict[str, str]
    secret_cipher: object
    root_log_level: int


_process_state_baseline: _ProcessState | None = None

_CLEARABLE_NON_SECRET_SETTINGS = (
    "torrent_save_path",
    "file_format",
    "chapter_format",
    "folder_format",
    "quality_cutoff",
    "ignored_words",
    "preferred_words",
    "required_words",
    "preferred_groups",
    "blocked_groups",
    "komga_url",
    "komga_library_id",
)

_SUPPORTED_DDL_LANGUAGES = (
    "en",
    "ja",
    "zh-hant",
    "zh-hans",
    "ko",
    "fr",
    "de",
    "es",
    "pt-br",
    "it",
    "pl",
    "ru",
)

_SUPPORTED_QUALITY_CUTOFFS = ("", "pdf", "epub", "cbr", "cbz", "rar", "zip", "mobi")


@pytest.fixture
def env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[Mapping[str, str]]:
    """Run the settings route against an isolated DB and encryption key."""
    global _process_state_baseline

    import main
    import security
    import shared

    process_state = _ProcessState(
        main_config=cast(dict[str, str], main.CONFIG),
        main_values=dict(main.CONFIG),
        shared_config=cast(dict[str, str], shared.CONFIG),
        shared_values=dict(shared.CONFIG),
        secret_cipher=security._SECRET_CIPHER,
        root_log_level=logging.getLogger().level,
    )
    if _process_state_baseline is None:
        _process_state_baseline = process_state
    else:
        assert main.CONFIG is _process_state_baseline.main_config
        assert main.CONFIG == _process_state_baseline.main_values
        assert shared.CONFIG is _process_state_baseline.shared_config
        assert shared.CONFIG == _process_state_baseline.shared_values
        assert security._SECRET_CIPHER is _process_state_baseline.secret_cipher
        assert logging.getLogger().level == _process_state_baseline.root_log_level

    db_path = str(tmp_path / "settings.db")
    key_dir = str(tmp_path / "config")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(shared, "DB_PATH", db_path)
    monkeypatch.delenv("MANGARR_SECRET_KEY", raising=False)
    monkeypatch.setattr(security, "_SECRET_CIPHER", None)
    try:
        security.load_or_create_secret_cipher(key_dir)
        main.init_db()
        main.load_config()
        main.ensure_api_key()
        yield {"db_path": db_path}
    finally:
        process_state.main_config.clear()
        process_state.main_config.update(process_state.main_values)
        main.CONFIG = process_state.main_config
        process_state.shared_config.clear()
        process_state.shared_config.update(process_state.shared_values)
        shared.CONFIG = process_state.shared_config
        security._SECRET_CIPHER = process_state.secret_cipher
        logging.getLogger().setLevel(process_state.root_log_level)


def _client() -> TestClient:
    import main

    return TestClient(main.app)


def _csrf(tag: str) -> dict[str, dict[str, str]]:
    token = f"csrf-settings-{tag}-" + "x" * 30
    return {
        "cookies": {"csrftoken": token},
        "headers": {"X-CSRFToken": token},
    }


def _seed(db_path: str, values: Mapping[str, str]) -> None:
    import main

    with sqlite3.connect(db_path) as db:
        db.executemany(
            "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
            values.items(),
        )
    main.load_config()


def _read(db_path: str, *keys: str) -> dict[str, str]:
    placeholders = ",".join("?" for _ in keys)
    with sqlite3.connect(db_path) as db:
        return dict(
            db.execute(
                f"SELECT key,value FROM settings WHERE key IN ({placeholders})",
                keys,
            )
        )


def _post(
    data: Mapping[str, str],
    *,
    tag: str,
    htmx: bool = False,
) -> Response:
    csrf = _csrf(tag)
    headers = dict(csrf["headers"])
    if htmx:
        headers["HX-Request"] = "true"
    client = _client()
    client.cookies.update(csrf["cookies"])
    return client.post(
        "/settings",
        data={"csrf_token": headers["X-CSRFToken"], **data},
        headers=headers,
        follow_redirects=False,
    )


def test_one_field_partial_post_preserves_every_unrelated_setting(
    env: Mapping[str, str],
) -> None:
    from security import encrypt_if_cipher_available

    seeded = {
        "category": "seeded-category",
        "min_seeders": "12",
        "torrent_save_path": "/seeded/downloads",
        "import_mode": "move",
        "remove_completed": "true",
        "minimum_free_space_mb": "9876",
        "propers_and_repacks": "do_not_upgrade",
        "file_format": "seeded-file",
        "chapter_format": "seeded-chapter",
        "folder_format": "seeded-folder",
        "ddl_grab_mode": "only",
        "ddl_language": "fr",
        "suwayomi_check_interval": "43200",
        "rss_interval": "1800",
        "grab_delay_minutes": "45",
        "quality_cutoff": "epub",
        "ignored_words": "seeded-ignore",
        "preferred_words": "seeded-prefer",
        "required_words": "seeded-require",
        "preferred_groups": "seeded-group",
        "blocked_groups": "seeded-block",
        "google_books_api_key": cast(str, encrypt_if_cipher_available("seeded-google")),
        "komga_url": "http://seeded-komga:25600",
        "komga_user": cast(str, encrypt_if_cipher_available("seeded-user")),
        "komga_pass": cast(str, encrypt_if_cipher_available("seeded-pass")),
        "komga_library_id": "seeded-library",
        "komga_scan_enabled": "true",
    }
    _seed(env["db_path"], seeded)

    response = _post({"category": "updated-category"}, tag="one-field")

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?saved=1"
    actual = _read(env["db_path"], *seeded)
    assert actual["category"] == "updated-category"
    assert {key: actual[key] for key in seeded if key != "category"} == {
        key: value for key, value in seeded.items() if key != "category"
    }


def test_all_visible_media_controls_persist_with_validation(
    env: Mapping[str, str],
) -> None:
    response = _post(
        {
            "category": "light-novels",
            "min_seeders": "7",
            "torrent_save_path": "  /downloads/incoming  ",
            "import_mode": "copy",
            "remove_completed": "true",
            "minimum_free_space_mb": "10000001",
            "propers_and_repacks": "do_not_prefer",
            "file_format": "  {Series Title} v{Volume:02d}  ",
            "chapter_format": "  {Series Title} c{Chapter:04d}  ",
            "folder_format": "  {Series Title} ({Year})  ",
            "ddl_grab_mode": "prefer",
            "ddl_language": "de",
            "suwayomi_check_interval": "7200",
        },
        tag="media",
    )

    assert response.status_code == 303
    assert _read(
        env["db_path"],
        "category",
        "min_seeders",
        "torrent_save_path",
        "import_mode",
        "remove_completed",
        "minimum_free_space_mb",
        "propers_and_repacks",
        "file_format",
        "chapter_format",
        "folder_format",
        "ddl_grab_mode",
        "ddl_language",
        "suwayomi_check_interval",
    ) == {
        "category": "light-novels",
        "min_seeders": "7",
        "torrent_save_path": "/downloads/incoming",
        "import_mode": "copy",
        "remove_completed": "true",
        "minimum_free_space_mb": "10000000",
        "propers_and_repacks": "do_not_prefer",
        "file_format": "{Series Title} v{Volume:02d}",
        "chapter_format": "{Series Title} c{Chapter:04d}",
        "folder_format": "{Series Title} ({Year})",
        "ddl_grab_mode": "prefer",
        "ddl_language": "de",
        "suwayomi_check_interval": "7200",
    }


def test_all_visible_general_controls_persist_with_clamping(
    env: Mapping[str, str],
) -> None:
    response = _post(
        {
            "rss_interval": "30",
            "grab_delay_minutes": "20000",
            "quality_cutoff": "cbz",
            "ignored_words": "raw,webtoon",
            "preferred_words": "official",
            "required_words": "english",
            "preferred_groups": "Group A",
            "blocked_groups": "Group B",
        },
        tag="general",
    )

    assert response.status_code == 303
    assert _read(
        env["db_path"],
        "rss_interval",
        "grab_delay_minutes",
        "quality_cutoff",
        "ignored_words",
        "preferred_words",
        "required_words",
        "preferred_groups",
        "blocked_groups",
    ) == {
        "rss_interval": "60",
        "grab_delay_minutes": "10080",
        "quality_cutoff": "cbz",
        "ignored_words": "raw,webtoon",
        "preferred_words": "official",
        "required_words": "english",
        "preferred_groups": "Group A",
        "blocked_groups": "Group B",
    }


def test_all_visible_metadata_controls_persist_and_encrypt_secrets(
    env: Mapping[str, str],
) -> None:
    import main

    csrf = _csrf("metadata")
    token = csrf["headers"]["X-CSRFToken"]
    body = urlencode(
        [
            ("csrf_token", token),
            ("google_books_api_key", "google-secret"),
            ("komga_url", "http://komga:25600"),
            ("komga_user", "komga-user"),
            ("komga_pass", "komga-pass"),
            ("komga_library_id", "library-id"),
            ("komga_scan_enabled", "0"),
            ("komga_scan_enabled", "1"),
        ]
    )
    client = _client()
    client.cookies.update(csrf["cookies"])
    response = client.post(
        "/settings",
        content=body,
        headers={
            **csrf["headers"],
            "Content-Type": "application/x-www-form-urlencoded",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    stored = _read(
        env["db_path"],
        "google_books_api_key",
        "komga_url",
        "komga_user",
        "komga_pass",
        "komga_library_id",
        "komga_scan_enabled",
    )
    assert stored["google_books_api_key"].startswith("enc:v1:")
    assert stored["komga_user"].startswith("enc:v1:")
    assert stored["komga_pass"].startswith("enc:v1:")
    assert stored["komga_url"] == "http://komga:25600"
    assert stored["komga_library_id"] == "library-id"
    assert stored["komga_scan_enabled"] == "true"
    assert main.get_cfg("google_books_api_key") == "google-secret"
    assert main.get_cfg("komga_user") == "komga-user"
    assert main.get_cfg("komga_pass") == "komga-pass"


def test_blank_secrets_keep_existing_and_torrent_path_can_be_cleared(
    env: Mapping[str, str],
) -> None:
    from security import encrypt_if_cipher_available

    seeded = {
        "google_books_api_key": cast(str, encrypt_if_cipher_available("keep-google")),
        "komga_user": cast(str, encrypt_if_cipher_available("keep-user")),
        "komga_pass": cast(str, encrypt_if_cipher_available("keep-pass")),
        "torrent_save_path": "/downloads/to-clear",
    }
    _seed(env["db_path"], seeded)

    response = _post(
        {
            "google_books_api_key": "   ",
            "komga_user": "",
            "komga_pass": "",
            "torrent_save_path": "   ",
        },
        tag="blank-secret-clear-path",
    )

    assert response.status_code == 303
    actual = _read(env["db_path"], *seeded)
    assert actual["google_books_api_key"] == seeded["google_books_api_key"]
    assert actual["komga_user"] == seeded["komga_user"]
    assert actual["komga_pass"] == seeded["komga_pass"]
    assert actual["torrent_save_path"] == ""


def test_all_user_clearable_non_secret_fields_persist_empty_values(
    env: Mapping[str, str],
) -> None:
    import main

    seeded = {
        key: f"seeded-{key}"
        for key in _CLEARABLE_NON_SECRET_SETTINGS
        if key != "quality_cutoff"
    }
    seeded["quality_cutoff"] = "cbz"
    seeded["category"] = "seeded-category"
    _seed(env["db_path"], seeded)

    response = _post(
        {
            **dict.fromkeys(_CLEARABLE_NON_SECRET_SETTINGS, ""),
            "category": "",
        },
        tag="clear-non-secret-fields",
    )

    assert response.status_code == 303
    actual = _read(
        env["db_path"],
        *_CLEARABLE_NON_SECRET_SETTINGS,
        "category",
    )
    assert {
        key: actual[key] for key in _CLEARABLE_NON_SECRET_SETTINGS
    } == dict.fromkeys(
        _CLEARABLE_NON_SECRET_SETTINGS,
        "",
    )
    assert actual["category"] == "seeded-category"
    assert {
        key: main.CONFIG[key] for key in _CLEARABLE_NON_SECRET_SETTINGS
    } == dict.fromkeys(_CLEARABLE_NON_SECRET_SETTINGS, "")
    assert main.CONFIG["category"] == "seeded-category"


def test_hidden_checkbox_false_value_persists(env: Mapping[str, str]) -> None:
    _seed(env["db_path"], {"komga_scan_enabled": "true"})

    response = _post(
        {"komga_scan_enabled": "0"},
        tag="checkbox-false",
    )

    assert response.status_code == 303
    assert _read(env["db_path"], "komga_scan_enabled") == {
        "komga_scan_enabled": "false"
    }


@pytest.mark.parametrize("language", _SUPPORTED_DDL_LANGUAGES)
def test_every_template_supported_ddl_language_persists(
    env: Mapping[str, str],
    language: str,
) -> None:
    import main

    response = _post({"ddl_language": language}, tag=f"ddl-language-{language}")

    assert response.status_code == 303
    assert _read(env["db_path"], "ddl_language") == {"ddl_language": language}
    assert main.CONFIG["ddl_language"] == language


@pytest.mark.parametrize("quality_cutoff", _SUPPORTED_QUALITY_CUTOFFS)
def test_every_supported_quality_cutoff_persists(
    env: Mapping[str, str],
    quality_cutoff: str,
) -> None:
    import main

    _seed(env["db_path"], {"quality_cutoff": "cbz"})
    response = _post(
        {"quality_cutoff": quality_cutoff},
        tag=f"quality-cutoff-{quality_cutoff or 'empty'}",
    )

    assert response.status_code == 303
    assert _read(env["db_path"], "quality_cutoff") == {"quality_cutoff": quality_cutoff}
    assert main.CONFIG["quality_cutoff"] == quality_cutoff


def test_invalid_language_and_quality_use_safe_defaults(
    env: Mapping[str, str],
) -> None:
    import main
    import shared

    _seed(
        env["db_path"],
        {
            "ddl_language": "fr",
            "quality_cutoff": "cbz",
        },
    )

    response = _post(
        {
            "ddl_language": "../../unsupported",
            "quality_cutoff": "executable",
        },
        tag="invalid-language-quality",
    )

    assert response.status_code == 303
    assert _read(env["db_path"], "ddl_language", "quality_cutoff") == {
        "ddl_language": "en",
        "quality_cutoff": "",
    }
    assert main.CONFIG["ddl_language"] == "en"
    assert main.CONFIG["quality_cutoff"] == ""
    assert shared.CONFIG["ddl_language"] == "en"
    assert shared.CONFIG["quality_cutoff"] == ""


def test_invalid_submitted_values_keep_existing_validation_behavior(
    env: Mapping[str, str],
) -> None:
    _seed(
        env["db_path"],
        {
            "rss_interval": "1800",
            "suwayomi_check_interval": "43200",
        },
    )

    response = _post(
        {
            "min_seeders": "-9",
            "import_mode": "invalid",
            "minimum_free_space_mb": "invalid",
            "propers_and_repacks": "invalid",
            "ddl_grab_mode": "invalid",
            "suwayomi_check_interval": "invalid",
            "rss_interval": "invalid",
            "grab_delay_minutes": "invalid",
        },
        tag="invalid-values",
    )

    assert response.status_code == 303
    assert _read(
        env["db_path"],
        "min_seeders",
        "import_mode",
        "minimum_free_space_mb",
        "propers_and_repacks",
        "ddl_grab_mode",
        "suwayomi_check_interval",
        "rss_interval",
        "grab_delay_minutes",
    ) == {
        "min_seeders": "0",
        "import_mode": "hardlink",
        "minimum_free_space_mb": "0",
        "propers_and_repacks": "prefer_and_upgrade",
        "ddl_grab_mode": "fallback",
        "suwayomi_check_interval": "43200",
        "rss_interval": "1800",
        "grab_delay_minutes": "0",
    }


def test_htmx_response_keeps_toast_contract(env: Mapping[str, str]) -> None:
    assert env["db_path"]
    response = _post(
        {"rss_interval": "1200"},
        tag="htmx",
        htmx=True,
    )

    assert response.status_code == 200
    assert response.content == b""
    assert json.loads(response.headers["HX-Trigger"]) == {
        "showToast": {"msg": "Settings saved", "type": "success"}
    }


def test_zz_fixture_mutation_probe(env: Mapping[str, str]) -> None:
    import main
    import security
    import shared

    assert env["db_path"]
    main.CONFIG["__settings_fixture_probe__"] = "main"
    shared.CONFIG["__settings_fixture_probe__"] = "shared"
    security._SECRET_CIPHER = object()
    logging.getLogger().setLevel(logging.CRITICAL)


def test_zzz_fixture_restores_process_globals_after_prior_test() -> None:
    import main
    import security
    import shared

    assert _process_state_baseline is not None
    assert main.CONFIG is _process_state_baseline.main_config
    assert main.CONFIG == _process_state_baseline.main_values
    assert shared.CONFIG is _process_state_baseline.shared_config
    assert shared.CONFIG == _process_state_baseline.shared_values
    assert security._SECRET_CIPHER is _process_state_baseline.secret_cipher
    assert logging.getLogger().level == _process_state_baseline.root_log_level
