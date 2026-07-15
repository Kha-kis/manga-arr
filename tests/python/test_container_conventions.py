from pathlib import Path

import pytest

from docker_entrypoint import parse_umask
from config import ENV_ALIASES, ENV_DEFAULTS


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0022", 0o022), ("0002", 0o002), ("077", 0o077), ("0777", 0o777)],
)
def test_container_umask_parser_accepts_octal_modes(value, expected):
    assert parse_umask(value) == expected


@pytest.mark.parametrize("value", ["", "22", "0o22", "0088", "1000", "abcd"])
def test_container_umask_parser_rejects_ambiguous_values(value):
    with pytest.raises(ValueError):
        parse_umask(value)


def test_image_exposes_supported_cli_and_applies_umask_entrypoint():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    assert "COPY bin/mangarr /usr/local/bin/mangarr" in dockerfile
    assert 'ENTRYPOINT ["python", "/app/docker_entrypoint.py"]' in dockerfile
    assert "MANGARR_UMASK=0022" in dockerfile


def test_project_metadata_declares_python_support_and_cli():
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    assert 'requires-python = ">=3.11"' in pyproject
    assert 'mangarr = "cli:main"' in pyproject


def test_public_application_environment_names_use_one_prefix():
    configured = [env_name for env_name, _default in ENV_DEFAULTS.values() if env_name]
    assert configured
    assert all(env_name.startswith("MANGARR_") for env_name in configured)
    assert "MANGA_SAVE_PATH" in ENV_ALIASES["save_path"]
    assert "RSS_INTERVAL" in ENV_ALIASES["rss_interval"]
