"""Tripwire tests for the silent-correctness invariants documented in
CLAUDE.md.

These don't prove the codebase is bug-free — they fence off the specific
regression classes that have caused real production bugs and that pass
unit tests but break at runtime.

If a test here fails, READ the failure message. The fix is almost never
"loosen the test"; it's "use the documented helper instead of the raw
operation that triggered the failure."
"""
import pathlib
import re
import subprocess

APP_DIR = pathlib.Path(__file__).resolve().parents[2] / "app"


# ───────────────────── int(vol_num) silent truncation ─────────────────────

# CLAUDE.md hard invariant: never int(vol_num) for display or search.
# Display must use vol_num_to_display (e.g. 3.5 → "3½"); search must use
# vol_num_to_search (e.g. 3.5 → "3.5"). int(3.5) = 3 silently misses
# every half-volume release on the indexer (PR #102).
#
# These four occurrences are the only allowed `int(vol_num)` in app/:
#
#   app/shared.py:250  — inside vol_num_to_display itself (computing base)
#   app/shared.py:274  — inside vol_num_to_search itself (computing base)
#   app/parsing.py:173 — inside _parse_vol_suffix (parser internal)
#   app/routers/series_.py:46 — chapter→volume bucketing for the
#       chapter_vol_map text export, where bucketing into integer volume
#       slots is the intentional behavior (not a search query)
#
# A new occurrence anywhere else is almost certainly a search-query bug
# of the PR #102 class. Bump the limit only after confirming the new
# site is one of the legitimate helper-internal uses above; document
# the new line in this comment block.

_ALLOWED_INT_VOL_NUM_OCCURRENCES = 4


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
