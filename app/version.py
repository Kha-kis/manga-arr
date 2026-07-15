"""Canonical Mangarr release version."""

from pathlib import Path


APP_VERSION = Path(__file__).with_name("VERSION").read_text(encoding="ascii").strip()

if not APP_VERSION:
    raise RuntimeError("app/VERSION must contain a release version")
