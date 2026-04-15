"""Tests for M3: user-regex ReDoS protection.

Two layers:
  1. security.compile_user_regex() / safe_regex_search() primitives
     — length cap, nested-quantifier detection, invalid-pattern rejection
  2. Integration with the 5 regex evaluation sites:
       custom_formats._spec_matches       (3 spec types)
       release_profiles._profile_term_match
       release_profiles._profile_pref_match
     — catastrophic pattern doesn't hang; harmless pattern still matches;
       one bad spec doesn't break the rest of a format's evaluation.
"""
import re
import sys
import time

import pytest

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401


# ───────────────────── primitive: safe_regex_search ─────────────────────

def test_harmless_regex_matches():
    from security import safe_regex_search
    assert safe_regex_search(r"Volume", "Series Volume 01.cbz", re.IGNORECASE) is True
    assert safe_regex_search(r"Volume", "Series Chapter 01.cbz", re.IGNORECASE) is False


def test_anchored_regex_works():
    from security import safe_regex_search
    assert safe_regex_search(r"^Series", "Series Volume 01", re.IGNORECASE) is True
    assert safe_regex_search(r"^Series", "The Series", re.IGNORECASE) is False


def test_character_class_regex_works():
    from security import safe_regex_search
    # Bounded character class + quantifier — safe, no nesting
    assert safe_regex_search(r"v[0-9]+", "Vol v01", re.IGNORECASE) is True


def test_empty_pattern_returns_none():
    from security import safe_regex_search
    assert safe_regex_search("", "anything") is None
    assert safe_regex_search("   ", "anything") is None


def test_malformed_pattern_returns_none():
    from security import safe_regex_search
    # Each of these is a parse error
    for bad in ["(unclosed", ")", "[", "*malformed"]:
        assert safe_regex_search(bad, "anything") is None, f"expected None for {bad!r}"


def test_over_long_pattern_rejected():
    from security import safe_regex_search
    # 300-char pattern is over the 256 limit
    assert safe_regex_search("a" * 300, "aaaa") is None


def test_catastrophic_pattern_rejected_fast():
    """The classic ReDoS shape (a+)+$ must be rejected BEFORE evaluation,
    in <50ms. Pre-fix, against the 30-char input below, this would have
    run for seconds."""
    from security import safe_regex_search
    t0 = time.monotonic()
    result = safe_regex_search(r"(a+)+$", "a" * 30 + "!")
    elapsed = time.monotonic() - t0
    assert result is None, "catastrophic pattern should be rejected"
    assert elapsed < 0.1, f"rejection took {elapsed*1000:.1f}ms — should be near-instant"


def test_various_nested_unbounded_patterns_rejected():
    """Several shapes that classic ReDoS analysis flags."""
    from security import safe_regex_search
    for pat in [
        r"(a+)+",
        r"(a*)*",
        r"(a+)*",
        r"(a*)+",
        r"([abc]+)+",
        r"(.+)*",
        r"(.*)+",
        r"(x+y+)+",
    ]:
        assert safe_regex_search(pat, "aaaa") is None, f"expected rejection for {pat!r}"


def test_non_nested_quantifier_still_accepted():
    """Non-nested quantifiers are safe and must not be rejected."""
    from security import safe_regex_search
    # These all quantify at only one level — not the nested-unbounded shape.
    assert safe_regex_search(r"a+", "aaaa") is True
    assert safe_regex_search(r"a*b", "aaab") is True
    assert safe_regex_search(r"v[0-9]+\.cbz", "vol v01.cbz", re.IGNORECASE) is True
    assert safe_regex_search(r"(cbz|cbr)", "Series.cbz", re.IGNORECASE) is True


def test_input_length_cap_applied_to_match():
    """A pattern scanning a huge input must not loop forever. The
    helper truncates text to 2048 chars before matching."""
    from security import safe_regex_search
    # Harmless pattern, enormous input. Must complete fast.
    big = "x" * 200_000
    t0 = time.monotonic()
    safe_regex_search(r"x+$", big)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.25, f"{elapsed*1000:.1f}ms — input cap didn't apply"


# ───────────────────── primitive: compile_user_regex ─────────────────────

def test_compile_user_regex_raises_on_unsafe():
    from security import compile_user_regex, UnsafeRegexError
    with pytest.raises(UnsafeRegexError, match="nested unbounded"):
        compile_user_regex(r"(a+)+")
    with pytest.raises(UnsafeRegexError, match="empty"):
        compile_user_regex("")
    with pytest.raises(UnsafeRegexError, match="too long"):
        compile_user_regex("a" * 300)
    with pytest.raises(UnsafeRegexError, match="invalid"):
        compile_user_regex("(unclosed")


def test_compile_user_regex_returns_pattern_on_safe():
    from security import compile_user_regex
    import re as _re
    p = compile_user_regex(r"v[0-9]+", _re.IGNORECASE)
    assert p.search("Vol V01.cbz") is not None


# ───────────────────── integration: custom_formats._spec_matches ─────────────────────

def test_custom_format_spec_matches_with_harmless_regex():
    from routers.custom_formats import evaluate_custom_format
    specs = [{"type": "release_title_contains", "value": "Volume", "negate": False}]
    assert evaluate_custom_format(specs, "Series Volume 01.cbz", 0, 0, "", "", "") is True
    assert evaluate_custom_format(specs, "Series Chapter 01.cbz", 0, 0, "", "", "") is False


def test_custom_format_spec_fallback_on_catastrophic_pattern():
    """A catastrophic regex in a custom-format spec is rejected by the
    helper, the spec falls back to substring match, and the format
    continues to be evaluated without hanging."""
    from routers.custom_formats import evaluate_custom_format
    specs = [{"type": "release_title_contains", "value": r"(a+)+$", "negate": False}]
    # Substring fallback: does "(a+)+$" appear as a literal substring in the title? No.
    t0 = time.monotonic()
    result = evaluate_custom_format(specs, "Series Volume 01.cbz", 0, 0, "", "", "")
    elapsed = time.monotonic() - t0
    assert elapsed < 0.1, f"{elapsed*1000:.1f}ms — spec with bad regex is slow"
    # With substring fallback, the literal pattern string is not in the
    # title, so the spec returns False.
    assert result is False


def test_custom_format_mixed_good_and_bad_specs():
    """When a format has a mix of good and bad specs, the bad one falls
    back to substring (i.e. the format keeps working), and the good
    ones still evaluate normally."""
    from routers.custom_formats import evaluate_custom_format
    specs = [
        {"type": "release_title_contains", "value": r"(a+)+",   "negate": False},  # bad
        {"type": "release_title_contains", "value": r"Volume",  "negate": False},  # good
    ]
    # Bad regex falls back to substring: "(a+)+" not in title → False → spec short-circuits
    assert evaluate_custom_format(specs, "Series Volume 01.cbz", 0, 0, "", "", "") is False

    # Same format but swap the bad one to its literal in the title so it
    # "matches" via substring; the good one matches normally.
    specs2 = [
        {"type": "release_title_contains", "value": r"(a+)+",   "negate": False},
        {"type": "release_title_contains", "value": r"Volume",  "negate": False},
    ]
    # Title deliberately contains the literal "(a+)+" so the substring
    # fallback finds it, and "Volume" matches the good regex.
    assert evaluate_custom_format(specs2, "Series (a+)+ Volume 01.cbz", 0, 0, "", "", "") is True


def test_custom_format_malformed_regex_does_not_crash():
    """An unclosed paren in a spec value must not raise."""
    from routers.custom_formats import evaluate_custom_format
    specs = [{"type": "release_title_contains", "value": "(unclosed", "negate": False}]
    # Should not raise; falls back to substring (which won't match "(unclosed")
    assert evaluate_custom_format(specs, "Series Volume 01.cbz", 0, 0, "", "", "") is False


# ───────────────────── integration: release_profiles ─────────────────────

def test_release_profile_is_regex_harmless_still_matches():
    from routers.release_profiles import _profile_term_match
    term = {"term": r"v[0-9]+", "is_regex": True}
    assert _profile_term_match(term, "series v01.cbz") is True


def test_release_profile_is_regex_catastrophic_rejected_fast():
    """A profile term flagged as is_regex=True with a catastrophic value
    must not hang the scoring path."""
    from routers.release_profiles import _profile_term_match
    term = {"term": r"(a+)+$", "is_regex": True}
    t0 = time.monotonic()
    # Substring fallback on "(a+)+$" in a long 'a' string returns False
    # (the literal pattern string is not in the input). The key is that
    # the call returns fast.
    _profile_term_match(term, "a" * 500)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.1, f"{elapsed*1000:.1f}ms — profile term with bad regex"


def test_release_profile_preferred_regex_still_works():
    from routers.release_profiles import _profile_pref_match
    pref = {"term": r"\[v[0-9]\]", "is_regex": True}
    assert _profile_pref_match(pref, "series [v2] repack") is True
    assert _profile_pref_match(pref, "series nominal") is False


def test_release_profile_preferred_catastrophic_rejected_fast():
    from routers.release_profiles import _profile_pref_match
    pref = {"term": r"(a*)*x", "is_regex": True}
    t0 = time.monotonic()
    _profile_pref_match(pref, "a" * 300)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.1, f"{elapsed*1000:.1f}ms — preferred term with bad regex"


# ───────────────────── regression guard ─────────────────────

def test_no_naked_re_search_at_user_regex_sites():
    """Guard: the 5 known regex evaluation sites must route through
    safe_regex_search, not bare re.search. Catches a future change
    that forgets the helper."""
    import pathlib
    files = [
        pathlib.Path(__file__).resolve().parents[2] / "app" / "routers" / "custom_formats.py",
        pathlib.Path(__file__).resolve().parents[2] / "app" / "routers" / "release_profiles.py",
    ]
    for fp in files:
        text = fp.read_text()
        # Allow re.search in other contexts, but flag if value/title/term
        # are fed into it without safe_regex_search on the line above.
        # Conservative heuristic: no unqualified `re.search(` where the
        # first arg is a user-controlled field we know about.
        banned = [
            "re.search(value, title",      # custom_formats old pattern
            "re.search(t, title_lower",    # release_profiles _profile_term_match old
            "re.search(term, title_lower", # release_profiles _profile_pref_match old
        ]
        for phrase in banned:
            assert phrase not in text, \
                f"{fp.name} still contains legacy {phrase!r} — should use safe_regex_search"
