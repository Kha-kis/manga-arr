"""Regression guards for the UI audit remediation work."""
from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TEMPLATES = REPO_ROOT / "app" / "templates"
STATIC = REPO_ROOT / "app" / "static"

RAW_COLOR_RE = re.compile(r"(#[0-9a-fA-F]{3,8}\b|rgba?\()", re.IGNORECASE)
LITERAL_Z_RE = re.compile(r"z-index\s*:(?!\s*var\(--z-)[^;]+", re.IGNORECASE)
LITERAL_TYPE_RE = re.compile(r"font-size\s*:(?!\s*var\()[^;]+", re.IGNORECASE)
LITERAL_RADIUS_RE = re.compile(r"border-radius\s*:(?!\s*var\()[^;]+", re.IGNORECASE)
LITERAL_PX_RE = re.compile(r":\s*[^;]*\b\d+px\b", re.IGNORECASE)
STATUS_EMOJI_RE = re.compile(r"[✅❌⚠✓✕✖✗]")

ICON_BUTTON_MARKERS = (
    "btn-icon",
    "btn-close",
    "series-inline-icon-button",
)


def _template_paths() -> list[pathlib.Path]:
    return sorted(TEMPLATES.rglob("*.html"))


def _strip_jinja_comments(text: str) -> str:
    return re.sub(r"{#.*?#}", "", text, flags=re.DOTALL)


def test_templates_do_not_use_inline_style_attributes():
    offenders = []
    for path in _template_paths():
        text = path.read_text()
        if "style=" in text:
            offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert not offenders, "inline style attributes found: " + ", ".join(offenders)


def test_page_templates_use_design_tokens_for_visual_values():
    offenders = []
    for path in _template_paths():
        if path.name == "base.html":
            continue
        text = _strip_jinja_comments(path.read_text())
        for lineno, line in enumerate(text.splitlines(), start=1):
            if (
                RAW_COLOR_RE.search(line)
                or LITERAL_Z_RE.search(line)
                or LITERAL_TYPE_RE.search(line)
                or LITERAL_RADIUS_RE.search(line)
                or LITERAL_PX_RE.search(line)
            ):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, "hardcoded visual values found:\n" + "\n".join(offenders)


def test_templates_do_not_use_emoji_status_icons():
    offenders = []
    for path in _template_paths():
        text = _strip_jinja_comments(path.read_text())
        for lineno, line in enumerate(text.splitlines(), start=1):
            if STATUS_EMOJI_RE.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, "emoji status icons found:\n" + "\n".join(offenders)


def test_icon_only_buttons_have_accessible_names():
    offenders = []
    for path in _template_paths():
        lines = path.read_text().splitlines()
        for idx, line in enumerate(lines):
            if "<button" not in line:
                continue
            window = [line]
            cursor = idx + 1
            while ">" not in "\n".join(window) and cursor < len(lines):
                window.append(lines[cursor])
                cursor += 1
            button_open = "\n".join(window)
            if not any(marker in button_open for marker in ICON_BUTTON_MARKERS):
                continue
            if "aria-label=" not in button_open and "aria-labelledby=" not in button_open:
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{idx + 1}: "
                    f"{button_open.strip()}"
                )
    assert not offenders, "icon-only buttons without accessible names:\n" + "\n".join(offenders)


def test_static_directory_contains_only_loaded_vendored_assets():
    allowed = {"PROVENANCE.md", "htmx.min.js", "alpine.min.js"}
    found = {
        path.relative_to(STATIC).as_posix()
        for path in STATIC.rglob("*")
        if path.is_file()
    }
    assert found == allowed
