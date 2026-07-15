"""Release-version consistency checks."""

import asyncio
import json
import re
from pathlib import Path

from main import app
from routers.api_v1 import api_v1_system_status
from routers import system
from version import APP_VERSION


SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_app_version_is_semver():
    assert SEMVER.fullmatch(APP_VERSION)
    assert (REPO_ROOT / "app/VERSION").read_text().strip() == APP_VERSION


def test_openapi_uses_canonical_version():
    assert app.version == APP_VERSION
    assert app.openapi()["info"]["version"] == APP_VERSION


def test_system_update_status_uses_canonical_version():
    assert system.APP_VERSION == APP_VERSION
    assert system.build_update_status()["currentVersion"] == APP_VERSION


def test_system_status_api_uses_canonical_version():
    response = asyncio.run(api_v1_system_status())
    assert json.loads(response.body)["version"] == APP_VERSION


def test_release_docs_name_current_version():
    for path in ("README.md", "CHANGELOG.md"):
        assert APP_VERSION in (REPO_ROOT / path).read_text()
