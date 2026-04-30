"""Tests for vol_num_to_search — the indexer-search-friendly volume
formatter.

CLAUDE.md flags `int(vol_num)` as a hard invariant violation: floats like
3.5 represent half-volumes that real torrent titles use as "v3.5" or
"vol 3.5". Truncating to `int(3.5) = 3` silently misses those releases.
This helper is the search-side counterpart to vol_num_to_display.
"""
import sys

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401

from shared import vol_num_to_search


def test_none_returns_empty_string():
    assert vol_num_to_search(None) == ""


def test_integer_volume_returns_int_string():
    assert vol_num_to_search(3) == "3"
    assert vol_num_to_search(3.0) == "3"
    assert vol_num_to_search(0) == "0"
    assert vol_num_to_search(99) == "99"


def test_half_volume_keeps_decimal():
    """The bug we're fixing: 3.5 must become "3.5", not "3"."""
    assert vol_num_to_search(3.5) == "3.5"
    assert vol_num_to_search(0.5) == "0.5"
    assert vol_num_to_search(99.5) == "99.5"


def test_quarter_volume_keeps_decimal():
    assert vol_num_to_search(3.25) == "3.25"
    assert vol_num_to_search(3.75) == "3.75"


def test_subvol_marker_truncates_to_int():
    """Sub-volume markers (3.01='3a', 3.02='3b') don't appear in real
    indexer titles — searching the integer base widens results."""
    assert vol_num_to_search(3.01) == "3"
    assert vol_num_to_search(3.02) == "3"
    assert vol_num_to_search(3.03) == "3"
    assert vol_num_to_search(3.04) == "3"


def test_unknown_decimal_truncates_to_int():
    """Decimals indexers don't typically use (e.g., 3.14) widen to int."""
    assert vol_num_to_search(3.14) == "3"
    assert vol_num_to_search(3.99) == "3"


def test_invalid_input_falls_back_to_str():
    """Mirrors vol_num_to_display: garbage in → str(garbage) out, no crash."""
    assert vol_num_to_search("not-a-number") == "not-a-number"


def test_search_query_is_substring_of_real_indexer_titles():
    """Smoke check: the formatted output should match how torrents are
    actually titled. Half-volumes show up as "v3.5" or "vol 3.5" in
    indexer responses, so query "vol 3.5" must hit those."""
    sample_indexer_titles = [
        "Series Name v3.5 [Group]",
        "Series Name vol 3.5 (2024)",
        "[Group] Series Name 03.5",
    ]
    search_term = vol_num_to_search(3.5)
    assert all(search_term in t for t in sample_indexer_titles)
