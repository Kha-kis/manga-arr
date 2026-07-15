"""Canonical Mangarr release version."""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


_VERSION_FILE = Path(__file__).with_name("VERSION")
try:
    APP_VERSION = _VERSION_FILE.read_text(encoding="ascii").strip()
except FileNotFoundError:
    try:
        APP_VERSION = version("mangarr")
    except PackageNotFoundError as exc:
        raise RuntimeError("Mangarr version metadata is unavailable") from exc

if not APP_VERSION:
    raise RuntimeError("app/VERSION must contain a release version")
