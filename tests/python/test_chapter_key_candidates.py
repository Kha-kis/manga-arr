"""Bug #1 fix: _chapter_key_candidates must generate zero-padded forms
so cvm lookups succeed against upstream data that stores chapter keys
as fixed-width strings (e.g. "021" for chapter 21).

Real-session context: series 44 ('Though I Am an Inept Villainess')
had its entire 35-chapter cvm keyed as "021", "022", etc. The old
implementation only generated "21", "21.0", "21" as candidates, so
every chapter row returned no_map_entry despite the cvm being fully
populated and the chapters already correctly linked.
"""
import sys

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


def _candidates(ch: float) -> list[str]:
    from reconcile_map import _chapter_key_candidates
    return _chapter_key_candidates(ch)


def test_integer_chapter_generates_padded_variants():
    cands = _candidates(21.0)
    # All five forms should be present; order starts with the canonical
    # non-padded variants so hotter paths match first.
    assert '21' in cands
    assert '21.0' in cands
    assert '021' in cands
    # "01" (2-digit) is useful for chapters 1..9 where sources use two
    # digits; harmless and small for 21.
    assert '21'[:2] in cands or '21' in cands


def test_single_digit_chapter_has_two_and_three_digit_padded_forms():
    cands = _candidates(1.0)
    assert '1' in cands
    assert '1.0' in cands
    assert '01' in cands
    assert '001' in cands


def test_lookup_succeeds_against_zero_padded_cvm():
    # Reproduces the series-44 shape: cvm keys are '021'-style.
    from reconcile_map import _lookup_target_vol_num
    cvm = {f"{i:03d}": ((i - 1) // 5) + 1 for i in range(1, 36)}
    # Sanity: keys are indeed zero-padded.
    assert '021' in cvm and '21' not in cvm
    # Lookup for integer-valued chapter must resolve.
    assert _lookup_target_vol_num(21.0, cvm) == 5.0
    assert _lookup_target_vol_num(1.0, cvm) == 1.0
    assert _lookup_target_vol_num(35.0, cvm) == 7.0


def test_lookup_still_succeeds_on_bare_and_dotted_keys():
    # Non-padded cvm (the common case) must keep working.
    from reconcile_map import _lookup_target_vol_num
    assert _lookup_target_vol_num(10.0, {'10': 2}) == 2.0
    assert _lookup_target_vol_num(10.0, {'10.0': 2}) == 2.0


def test_decimal_chapter_not_padded():
    # 1.5 should not gain "001" / "01" candidates — those are only
    # meaningful for integer chapter numbers.
    cands = _candidates(1.5)
    assert cands == ['1.5']


def test_candidates_are_deduplicated():
    # Ensure identical forms aren't emitted twice (stable ordering,
    # small return list).
    cands = _candidates(5.0)
    assert len(cands) == len(set(cands))
