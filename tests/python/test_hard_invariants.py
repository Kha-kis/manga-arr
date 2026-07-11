"""Tripwire tests for the silent-correctness invariants documented in
CLAUDE.md.

These don't prove the codebase is bug-free — they fence off the specific
regression classes that have caused real production bugs and that pass
unit tests but break at runtime.

If a test here fails, READ the failure message. The fix is almost never
"loosen the test"; it's "use the documented helper instead of the raw
operation that triggered the failure."
"""
import ast
import asyncio
import pathlib
import re
import subprocess
import sys
from unittest.mock import patch

APP_DIR = pathlib.Path(__file__).resolve().parents[2] / "app"
ROUTERS_DIR = APP_DIR / "routers"

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401,E402


# ───────────────────── source hygiene invariants ─────────────────────


def _app_python_files() -> list[pathlib.Path]:
    return sorted(APP_DIR.rglob("*.py"))


def test_app_code_does_not_use_direct_print_calls():
    """Production code should use structured log_event()/logging paths.

    Direct print() calls disappear in normal container operation and make
    failures harder to diagnose. This keeps the print-to-log_event migration
    from drifting backwards.
    """
    offenders = []
    for path in _app_python_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                rel = path.relative_to(APP_DIR.parent).as_posix()
                offenders.append(f"{rel}:{node.lineno}")

    assert not offenders, (
        "direct print() calls found in app code; use log_event() or logging:\n"
        + "\n".join(offenders)
    )


def test_app_code_has_no_stale_audit_markers():
    """Keep stale audit breadcrumbs out of production app code."""
    stale_markers = ("TO" "DO", "FIX" "ME", "HA" "CK")
    marker_re = re.compile(r"\b(" + "|".join(stale_markers) + r")\b")
    offenders = []
    for path in _app_python_files():
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if marker_re.search(line):
                rel = path.relative_to(APP_DIR.parent).as_posix()
                offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, "stale source markers found:\n" + "\n".join(offenders)


# ───────────────────── int(vol_num) silent truncation ─────────────────────

# CLAUDE.md hard invariant: never int(vol_num) for display or search.
# Display must use vol_num_to_display (e.g. 3.5 → "3½"); search must use
# vol_num_to_search (e.g. 3.5 → "3.5"). int(3.5) = 3 silently misses
# every half-volume release on the indexer (PR #102).
#
# These six occurrences are the only allowed `int(vol_num)` in app/:
#
#   app/shared.py:250  — inside vol_num_to_display itself (computing base)
#   app/shared.py:274  — inside vol_num_to_search itself (computing base)
#   app/parsing.py:173 — inside _parse_vol_suffix (parser internal)
#   app/routers/series_core.py:24 — chapter→volume bucketing in
#       _chapter_map_to_ranges, where bucketing into integer volume
#       slots is the intentional behavior (not a search query)
#   app/routers/series_.py:46 — chapter→volume bucketing for the
#       chapter_vol_map text export (duplicate of series_core; kept
#       for backward compatibility during migration)
#   app/routers/series_detail.py:29 — chapter→volume bucketing in
#       _chapter_map_to_ranges (extracted from series_ during refactoring;
#       same purpose as series_core.py:24)
#
# A new occurrence anywhere else is almost certainly a search-query bug
# of the PR #102 class. Bump the limit only after confirming the new
# site is one of the legitimate helper-internal uses above; document
# the new line in this comment block.

_ALLOWED_INT_VOL_NUM_OCCURRENCES = 6


def test_int_vol_num_count_is_pinned():
    """Tripwire: any new `int(vol_num)` site must be reviewed against the
    half-volume-search silent-truncation bug class (PR #102)."""
    result = subprocess.run(
        ['grep', '-rnE', r'int\(vol_num\)', str(APP_DIR)],
        capture_output=True, text=True,
    )
    # grep returns exit 1 when no matches — that's fine, count = 0
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    count = len(lines)
    assert count <= _ALLOWED_INT_VOL_NUM_OCCURRENCES, (
        f"\n\n  HARD INVARIANT TRIPWIRE: int(vol_num) count is now {count}, "
        f"was {_ALLOWED_INT_VOL_NUM_OCCURRENCES}.\n\n"
        f"  Each occurrence must be either:\n"
        f"    1. INSIDE the body of vol_num_to_display / vol_num_to_search\n"
        f"       (computing the integer base of the float — fine), or\n"
        f"    2. INSIDE _parse_vol_suffix in parsing.py\n"
        f"       (internal parser helper — fine), or\n"
        f"    3. Bucketing chapters into integer volume slots for the\n"
        f"       chapter_vol_map text export (display-only — fine).\n\n"
        f"  Anywhere else, int(vol_num) silently drops fractional volumes:\n"
        f"      int(3.5) = 3   ← misses every half-volume release\n"
        f"  Use vol_num_to_search() for indexer queries (returns '3.5') or\n"
        f"  vol_num_to_display() for UI (returns '3½'). See CLAUDE.md and\n"
        f"  PR #102 for the bug class.\n\n"
        f"  Current sites:\n    " + "\n    ".join(lines) + "\n\n"
        f"  If the new site is legitimate (one of the helper-internal\n"
        f"  uses above), bump _ALLOWED_INT_VOL_NUM_OCCURRENCES in this\n"
        f"  test file and document the new line in the comment block.\n"
    )


# ───────────────────── Prowlarr categories invariant ─────────────────────

# CLAUDE.md hard invariant: Prowlarr manga uses categories 7000+7010+7020
# (Books/General, Mags, EBook). Nyaa manga lives in 7000 specifically.
# The default that ships in `indexers` table DDL must contain those three.

def test_indexers_default_categories_includes_manga_set():
    """The DDL default for indexers.categories must include 7000, 7010,
    and 7020 — manga search queries are silently empty without them."""
    schema_path = APP_DIR / "schema.py"
    if not schema_path.exists():
        return  # skip if schema lives elsewhere later
    text = schema_path.read_text()
    # Find `categories TEXT ... DEFAULT '...'` for the indexers table
    m = re.search(
        r"categories\s+TEXT\s+(?:NOT\s+NULL\s+)?DEFAULT\s+'([^']+)'",
        text,
    )
    assert m is not None, (
        "indexers.categories default not found in schema.py — "
        "either it was removed (categories must have a DDL default) "
        "or this test needs to be updated to match the new shape."
    )
    default_value = m.group(1)
    for required in ('7000', '7010', '7020'):
        assert required in default_value, (
            f"indexers.categories default {default_value!r} is missing "
            f"the {required} category. Manga lives in 7000+7010+7020 "
            f"(Books/General, Mags, EBook). Without all three, RSS and "
            f"search silently return zero matches for some indexers."
        )


# ───────────────────── seen-table dedup columns ─────────────────────

# CLAUDE.md hard invariant: two-layer grab dedup — torrent_url AND
# release_guid. The seen table must carry release_guid (PR #104) so
# the cross-URL check can fire.

def test_seen_table_has_release_guid_column():
    """Schema migration must keep `release_guid` on the seen table —
    its absence reverts dedup to URL-only (PR #104 regression class)."""
    schema_path = APP_DIR / "schema.py"
    text = schema_path.read_text()
    # Both the add_col call and the seen_new DDL must reference release_guid
    assert "release_guid" in text, (
        "seen.release_guid column is missing from schema.py. This was added "
        "in PR #104 to dedup mirrored releases (same content, different URL). "
        "Without it, URL-only dedup lets Prowlarr mirrors silently grab the "
        "same torrent twice."
    )
    assert "add_col('seen'" in text and "release_guid" in text, (
        "release_guid must be added via add_col on the seen table"
    )


# ───────────────────── route order: literal before parameterized ─────────────

# CLAUDE.md hard invariant: "literal paths must precede parameterized siblings
# in the same module. Starlette first-match wins. Grep before adding any new
# path." Bug class: /import-lists/{list_id} declared before /import-lists/sync
# means /import-lists/sync is unreachable — the parameterized route eats it.
#
# This tripwire walks every app/routers/*.py, parses the decorator paths in
# source order, and asserts that no earlier declaration would shadow a later
# one with the same HTTP method.

_DECORATOR_RE = re.compile(
    r'^\s*@router\.(get|post|patch|delete|put)\(\s*["\']([^"\']+)["\']',
    re.MULTILINE,
)


def _earlier_shadows_later(earlier_path: str, later_path: str) -> bool:
    """True if a request to later_path would be matched by earlier_path
    (treating {param} in earlier as a wildcard segment). When that's true,
    later_path is unreachable in source order."""
    e_segs = earlier_path.strip('/').split('/')
    l_segs = later_path.strip('/').split('/')
    if len(e_segs) != len(l_segs):
        return False
    for e, l in zip(e_segs, l_segs):
        if e == l:
            continue
        if e.startswith('{') and e.endswith('}'):
            continue  # earlier matches anything → including the literal in later
        return False
    return True


def test_no_route_order_violations():
    """Tripwire: in each router module, no earlier @router decorator may
    shadow a later one with the same HTTP method.

    The pattern that caused real bugs: a parameterized path declared before
    a literal sibling renders the literal unreachable. Starlette doesn't
    warn about this — the route just silently never fires."""
    violations = []
    for router_file in sorted(ROUTERS_DIR.glob("*.py")):
        if router_file.name.startswith('_'):
            continue
        content = router_file.read_text()
        decls = []
        for m in _DECORATOR_RE.finditer(content):
            line = content[:m.start()].count('\n') + 1
            method = m.group(1).upper()
            path = m.group(2)
            decls.append((line, method, path))

        for i in range(len(decls)):
            e_line, e_method, e_path = decls[i]
            for j in range(i + 1, len(decls)):
                l_line, l_method, l_path = decls[j]
                if e_method != l_method:
                    continue
                if _earlier_shadows_later(e_path, l_path):
                    violations.append(
                        f"{router_file.name}:{l_line}  "
                        f"{l_method} {l_path!r} is shadowed by "
                        f"{e_path!r} at line {e_line}"
                    )

    assert not violations, (
        "\n\n  ROUTE ORDER VIOLATIONS — these routes are unreachable:\n\n    "
        + "\n    ".join(violations)
        + "\n\n  Starlette dispatches first-match-wins on registration order. "
        + "When a parameterized path (`/foo/{id}`) is declared before a "
        + "literal sibling (`/foo/sync`), the literal never matches.\n\n"
        + "  Fix: move the literal-path decorator above the parameterized one "
        + "in the same file. Add a comment if the order is non-obvious so a "
        + "future refactor doesn't re-sort them.\n"
    )


# ───────────────────── Prowlarr per-indexer enable filter ─────────────────────

# CLAUDE.md hard invariant (and the user-facing promise of the indexers
# integration): toggling an indexer OFF in Prowlarr's own UI must stop
# Mangarr from polling it via RSS or search. The filter that enforces
# this lives at routers/indexers.py:483 inside _get_prowlarr_indexers:
#
#     for idx in indexers:
#         if not idx.get('enable', True):
#             continue
#
# A future refactor that drops or inverts this line would silently start
# pulling from Prowlarr-disabled indexers — exactly the kind of bug the
# user wouldn't notice until unwanted releases started showing up. This
# behavioral test mocks Prowlarr's /api/v1/indexer response and asserts
# the filter still fires.


class _MockProwlarrResponse:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        return self._json


def _mock_prowlarr_client(indexers_response):
    """Returns a stand-in for httpx.AsyncClient whose .get() always
    returns indexers_response from /api/v1/indexer."""

    class _C:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return _MockProwlarrResponse(200, indexers_response)

    return _C


def test_prowlarr_filter_skips_disabled_subindexers():
    """Tripwire: _get_prowlarr_indexers must filter out sub-indexers where
    Prowlarr reports `enable: false`. If this check is removed, Mangarr
    starts polling indexers the user explicitly disabled in Prowlarr's UI.
    """
    from routers.indexers import _get_prowlarr_indexers

    # Three sub-indexers: one enabled, one disabled, one with enable missing
    # (Prowlarr default is enabled — we should INCLUDE these).
    fake_response = [
        {
            'id': 1, 'name': 'NyaaActive', 'enable': True,
            'protocol': 'torrent',
            'capabilities': {'categories': [{'id': 7000}, {'id': 7020}]},
        },
        {
            'id': 2, 'name': 'NyaaDisabled', 'enable': False,
            'protocol': 'torrent',
            'capabilities': {'categories': [{'id': 7000}]},
        },
        {
            'id': 3, 'name': 'AnimeBytesNoEnableKey',
            # 'enable' key intentionally absent — must default to True
            'protocol': 'torrent',
            'capabilities': {'categories': [{'id': 7000}, {'id': 7010}]},
        },
    ]

    with patch('httpx.AsyncClient', new=_mock_prowlarr_client(fake_response)):
        result = asyncio.run(
            _get_prowlarr_indexers('http://prowlarr.test', 'fake-key', [7000, 7010, 7020])
        )

    names = [name for _id, name, _proto in result]
    assert 'NyaaActive' in names, "enabled sub-indexer must be included"
    assert 'AnimeBytesNoEnableKey' in names, (
        "missing-enable-key sub-indexer must default to enabled (Prowlarr's "
        "own default behavior)"
    )
    assert 'NyaaDisabled' not in names, (
        "Prowlarr-disabled sub-indexer leaked through the filter at "
        "routers/indexers.py:483 — RSS and search would silently pull "
        "from indexers the user has explicitly disabled in Prowlarr's UI. "
        "Re-add `if not idx.get('enable', True): continue` to "
        "_get_prowlarr_indexers."
    )


def test_prowlarr_filter_skips_subindexers_without_manga_categories():
    """Companion check: sub-indexers whose declared categories don't intersect
    with the requested manga set must also be skipped. CLAUDE.md hard
    invariant: 'Prowlarr manga categories: 7000 + 7010 + 7020.' A non-manga
    indexer (movies-only, music-only) would otherwise be polled and return
    junk for every search."""
    from routers.indexers import _get_prowlarr_indexers

    fake_response = [
        {
            'id': 10, 'name': 'MangaIndexer', 'enable': True,
            'protocol': 'torrent',
            'capabilities': {'categories': [{'id': 7000}]},
        },
        {
            'id': 11, 'name': 'MoviesOnlyIndexer', 'enable': True,
            'protocol': 'torrent',
            'capabilities': {'categories': [{'id': 2000}, {'id': 2010}]},
        },
        {
            'id': 12, 'name': 'NoCapsListed', 'enable': True,
            'protocol': 'torrent',
            'capabilities': {},  # no categories listed — included (unknown caps)
        },
    ]

    with patch('httpx.AsyncClient', new=_mock_prowlarr_client(fake_response)):
        result = asyncio.run(
            _get_prowlarr_indexers('http://prowlarr.test', 'fake-key', [7000, 7010, 7020])
        )

    names = {name for _id, name, _proto in result}
    assert 'MangaIndexer' in names
    assert 'MoviesOnlyIndexer' not in names, (
        "non-manga indexer leaked through category-intersection filter at "
        "routers/indexers.py:486"
    )
    assert 'NoCapsListed' in names, (
        "indexer with no declared capabilities must be included (we don't "
        "know what it supports, give it the benefit of the doubt)"
    )
