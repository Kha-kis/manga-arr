"""Bug #2 fix: _trim_cvm_to_vol_range drops entries targeting vols
beyond the series' total_volumes, preventing MangaDex continuous-
numbering contamination from driving populate_chapters to create
phantom chapter rows.

Live-session context: JoJo P3/P4/P5 were each contaminated with cvm
entries whose target vol exceeded the part's volume count — because
MangaDex catalogues individual JoJo parts under separate UUIDs but
numbers chapters continuously across the whole series. Each refresh
was re-writing the bad data. This test file pins the fix so future
refreshes drop the offending entries before populate_chapters runs.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


# ───────────────────────── unit tests: the helper ─────────────────────────────

def test_trim_drops_entries_above_total_volumes():
    from main import _trim_cvm_to_vol_range
    mapping = {'1': 1, '2': 1, '10': 2, '50': 5, '200': 16, '300': 28}
    trimmed = _trim_cvm_to_vol_range(mapping, total_volumes=16, source='test')
    assert trimmed == {'1': 1, '2': 1, '10': 2, '50': 5, '200': 16}
    assert '300' not in trimmed  # vol 28 is out of range


def test_trim_keeps_boundary_entries():
    from main import _trim_cvm_to_vol_range
    mapping = {'1': 1, '100': 16}  # vol 16 == total_volumes
    trimmed = _trim_cvm_to_vol_range(mapping, total_volumes=16, source='test')
    assert trimmed == mapping


def test_trim_no_bound_returns_original():
    from main import _trim_cvm_to_vol_range
    mapping = {'1': 1, '10': 30}
    # total_volumes unknown → pass everything through
    for tv in (None, 0):
        assert _trim_cvm_to_vol_range(mapping, total_volumes=tv, source='test') == mapping


def test_trim_empty_map_returns_empty():
    from main import _trim_cvm_to_vol_range
    assert _trim_cvm_to_vol_range({}, total_volumes=10, source='test') == {}


def test_trim_preserves_non_numeric_values_for_later_validator():
    # Defensive: if a caller passed a garbage map, don't drop entries we
    # can't classify — let _validate_chapter_map reject the whole thing.
    from main import _trim_cvm_to_vol_range
    mapping = {'1': 1, '2': 'oops', '3': None, '4': 20}
    trimmed = _trim_cvm_to_vol_range(mapping, total_volumes=10, source='test')
    assert trimmed == {'1': 1, '2': 'oops', '3': None}
    # '4': 20 dropped; 'oops' and None preserved so the validator sees them


def test_trim_reproduces_jojo_p3_shape():
    # Mirrors the P3 contamination pattern: cvm holds in-range entries
    # plus entries targeting vols beyond total_volumes. Verify every
    # in-range entry survives and every out-of-range entry is dropped.
    from main import _trim_cvm_to_vol_range
    mapping = {}
    # In-range: chapters 1..152 distributed across vols 1..16
    for ch in range(1, 153):
        mapping[str(ch)] = (ch - 1) // 10 + 1  # vols 1..16
    # Contamination: chapters 153..265 targeting vols 17..28
    for ch in range(153, 266):
        mapping[str(ch)] = (ch - 1) // 10 + 1  # vols 17..27
    before = len(mapping)
    trimmed = _trim_cvm_to_vol_range(mapping, total_volumes=16, source='test')
    assert all(int(v) <= 16 for v in trimmed.values())
    assert len(trimmed) < before
    # Every chapter whose original target was <= 16 must survive
    for k, v in mapping.items():
        if int(v) <= 16:
            assert k in trimmed, f"in-range entry {k}:{v} was wrongly trimmed"
