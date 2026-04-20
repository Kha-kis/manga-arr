"""Contract tests for release-title parsing — Stage 1 of the mapping audit.

Pins the behaviour of the title-parsing primitives in ``app.main``:

  - ``extract_volume_num``
  - ``extract_volume_range``
  - ``extract_chapter_num``
  - ``extract_chapter_range``
  - ``detect_pack_type``
  - ``is_complete_pack``
  - ``is_special_release``  (new in Stage 1, detection-only)

These tests encode the "intended behaviour" section of the mapping audit.
They exist *before* the parser fixes so failures are meaningful: each red
test maps directly to a documented audit defect (D1…D15).

What this file DOES NOT cover:
  - import queue writes / review UI (Stage 2)
  - wanted/missing coverage SQL (Stage 3)
  - provider alignment (Stage 4)
Those layers still read parser output, but the contract of the parser is
fixed here.

Notation used in tests:
  - `None` for "no single value" — a range, or nothing detected.
  - `(start, end)` floats for ranges.
  - ``detect_pack_type`` returns 'complete' | 'chapter' | 'volume'.
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401

from main import (  # type: ignore[import-not-found]
    detect_pack_type,
    extract_chapter_num,
    extract_chapter_range,
    extract_volume_num,
    extract_volume_range,
    is_complete_pack,
    is_special_release,
)


# ─────────────────────────── 1. single volumes ─────────────────────────────

@pytest.mark.parametrize("title,expected", [
    ("Manga Name v01.cbz",                 1),
    ("Manga Name Vol. 1.cbz",              1),
    ("Manga Name Vol.1.cbz",               1),   # no space
    ("Manga Name Vol 1.cbz",               1),   # no dot
    ("Manga Name Volume 01 [Group].cbz",   1),
    ("Manga Name v_001 [Colored]",         1),
    ("Manga Name Volume III",              3),
    ("漫画 第1巻",                          1),
    ("만화 1권",                             1),
])
def test_single_volume_parses(title: str, expected: int) -> None:
    assert extract_volume_num(title) == expected


# ─────────────── 2. no ghost chapter from volume files (D1, D3) ────────────

@pytest.mark.parametrize("title", [
    "Manga Name v01.cbz",
    "Manga Name Vol. 1.cbz",
    "Manga Name Vol. 1-3.cbz",
    "Manga Name Volume 01 [Group].cbz",
    "Manga Name v_001 [Colored]",
    "Manga Name v01-v03.cbz",
    "Collector's Edition Vol. 1",
    "20th Century Boys Vol. 1",
    "Manga Name Volume III",
    # The bare-number fallback was poisoning these with a ghost chapter
    # pulled from the series title or year. extract_chapter_num must
    # keep its hands off anything with a volume marker present.
])
def test_volume_file_has_no_ghost_chapter(title: str) -> None:
    assert extract_chapter_num(title) is None, (
        f"extract_chapter_num({title!r}) must not return a ghost chapter; "
        f"got {extract_chapter_num(title)}"
    )


# ─────────────────────────── 3. single chapters ────────────────────────────

@pytest.mark.parametrize("title,expected", [
    ("Manga Name c001.cbz",           1.0),
    ("Manga Name Ch. 1.cbz",          1.0),
    ("Manga Name Ch.1.cbz",           1.0),
    ("Manga Name Chapter 1.cbz",      1.0),
    ("Manga Name Chapter 001.5.cbz",  1.5),
    ("Manga Name c1000",              1000.0),
    ("Manga Name Ch.10.5",            10.5),
    ("漫画 第3話",                     3.0),
])
def test_single_chapter_parses(title: str, expected: float) -> None:
    assert extract_chapter_num(title) == expected


# ─────────────────────────── 4. chapter ranges ─────────────────────────────

@pytest.mark.parametrize("title,expected", [
    ("Manga Name c001-002.cbz",  (1.0, 2.0)),
    ("Manga Name Ch. 1-2.cbz",   (1.0, 2.0)),
    ("Manga Name Ch.1-2.cbz",    (1.0, 2.0)),
    ("Manga Name c1-c2.cbz",     (1.0, 2.0)),
    ("Manga Name Chapters 1-10", (1.0, 10.0)),
    ("Manga Name Chapter 5-15",  (5.0, 15.0)),
    # Long-running shounen releases legitimately exceed the old 200
    # cap. Chapter-prefix requirement still protects against year
    # ranges (tested below).
    ("Jujutsu Kaisen c001-c267", (1.0, 267.0)),
    ("Naruto c001-c460",         (1.0, 460.0)),
    ("One Piece c001-c1089",     (1.0, 1089.0)),
])
def test_chapter_range_parses(title: str, expected: tuple[float, float]) -> None:
    assert extract_chapter_range(title) == expected


@pytest.mark.parametrize("title", [
    # These MUST still be rejected: no chapter prefix means the
    # bare-number pattern in the volume-range parser handles them
    # (or rejects them entirely). This guards against the old 200-cap
    # fix re-introducing year-range false positives.
    "Manga Name (2010-2020) complete",
    "Manga Name 2010-2020",
    "Manga Name - 1990-2010",
])
def test_chapter_range_still_rejects_year_ranges(title: str) -> None:
    assert extract_chapter_range(title) is None


@pytest.mark.parametrize("title", [
    # Spans > 2000 chapters are rejected — no real manga publishes in
    # a single pack that long; pair is almost certainly an ID / date
    # munging artifact.
    "Manga Name c001-c5000",
    "Manga Name c10-c9999",
])
def test_chapter_range_rejects_absurd_spans(title: str) -> None:
    assert extract_chapter_range(title) is None


@pytest.mark.parametrize("title", [
    "Manga Name c001-002.cbz",
    "Manga Name Ch. 1-2.cbz",
    "Manga Name c1-c2.cbz",
    "Manga Name Chapters 1-10",
])
def test_chapter_range_suppresses_single_chapter(title: str) -> None:
    """A release that is a chapter range must NOT also yield a single chapter.
    Callers that read chapter_num independently were importing the wrong
    row when the range slipped through (D2, D3)."""
    assert extract_chapter_num(title) is None, (
        f"chapter-range title {title!r} leaked a single chapter "
        f"value {extract_chapter_num(title)!r}"
    )


@pytest.mark.parametrize("title", [
    "Manga Name c001-002.cbz",
    "Manga Name c001-c100.cbz",
    "Manga Name c1-c2.cbz",
])
def test_chapter_range_is_not_a_volume_range(title: str) -> None:
    """extract_volume_range was also matching chapter-prefix ranges (D4),
    which let chapter packs pretend to cover volume slots. That pattern
    belongs to extract_chapter_range now."""
    assert extract_volume_range(title) is None, (
        f"chapter-range title {title!r} produced volume_range "
        f"{extract_volume_range(title)!r}"
    )


# ─────────────────────────── 5. volume ranges ──────────────────────────────

@pytest.mark.parametrize("title,expected", [
    ("Manga Name v01-v03.cbz",   (1.0, 3.0)),
    ("Manga Name Vol. 1-3.cbz",  (1.0, 3.0)),
    ("Manga Name vol.1-vol.5",   (1.0, 5.0)),
    ("Manga Name Volume 1-10",   (1.0, 10.0)),
])
def test_volume_range_parses(title: str, expected: tuple[float, float]) -> None:
    assert extract_volume_range(title) == expected


@pytest.mark.parametrize("title", [
    "Manga Name v01-v03.cbz",
    "Manga Name Vol. 1-3.cbz",
    "Manga Name Volume 1-10",
])
def test_volume_range_suppresses_single_volume(title: str) -> None:
    """A release that is a volume range must NOT also yield a single
    volume number. Symmetric to the chapter-range rule (D9)."""
    assert extract_volume_num(title) is None, (
        f"volume-range title {title!r} leaked a single volume "
        f"value {extract_volume_num(title)!r}"
    )


@pytest.mark.parametrize("title", [
    "Manga Name v01-v03.cbz",
    "Manga Name Vol. 1-3.cbz",
])
def test_volume_range_yields_no_chapter(title: str) -> None:
    """Volume ranges must not produce chapter extractions either."""
    assert extract_chapter_num(title) is None
    assert extract_chapter_range(title) is None


# ─────────────── 6. ranges are exclusive across vol/chapter ────────────────

@pytest.mark.parametrize("title", [
    "Manga Name c001-c100.cbz",
    "Manga Name Ch. 1-10",
    "Manga Name chapters 1-100",
])
def test_chapter_ranges_never_yield_volume_range(title: str) -> None:
    assert extract_volume_range(title) is None
    assert extract_chapter_range(title) is not None


@pytest.mark.parametrize("title", [
    "Manga Name v01-v03.cbz",
    "Manga Name vol.1-vol.5",
])
def test_volume_ranges_never_yield_chapter_range(title: str) -> None:
    assert extract_chapter_range(title) is None
    assert extract_volume_range(title) is not None


# ─────────────────────────── 7. complete packs ─────────────────────────────

@pytest.mark.parametrize("title,total_vols,expected", [
    ("Manga Name Complete Series",       None, True),
    ("Manga Name complete collection",   None, True),
    ("Manga Name (2012-2021) complete",  None, True),    # year span
    ("Manga Name v01-v10",               10,   True),     # 100% of 10
    ("Manga Name v01-v09",               10,   True),     # 90% of 10
    ("Manga Name v01-v03",               10,   False),    # 30%
])
def test_is_complete_pack(title: str, total_vols: int | None, expected: bool) -> None:
    assert is_complete_pack(title, total_vols) is expected


@pytest.mark.parametrize("title,vol_range,total_vols,expected", [
    ("Manga Name Complete Series",    None,        None, 'complete'),
    ("Manga Name v01-10 Complete",    (1.0, 10.0), 10,   'complete'),  # D15
    ("Manga Name v01-v05",            (1.0, 5.0),  10,   'volume'),
    ("Manga Name Chapters 1-10",      None,        10,   'chapter'),   # D5
    ("Manga Name c1-c2.cbz",          (1.0, 2.0),  10,   'chapter'),   # D5
    ("Manga Name Part 5 Vol 17",      None,        20,   'volume'),    # D6
    ("Manga Name v01.cbz",            None,        10,   'volume'),
])
def test_detect_pack_type(title: str, vol_range, total_vols: int | None, expected: str) -> None:
    assert detect_pack_type(title, vol_range, total_vols) == expected


# ──────────── 8. special / side-story detection (detection-only) ────────────

@pytest.mark.parametrize("title", [
    "Manga Name Special",
    "Manga Name Extra",
    "Manga Name Oneshot",
    "Manga Name One-shot",
    "Manga Name Bonus Chapter",
    "Manga Name Omake",
    "Tomioka Giyuu Gaiden c001-002",
    "Demon Slayer Side Story - Stories of Water and Flame c001-002",
    "Kimetsu no Yaiba Side Story",
    "Manga Name Sidestory Vol 1",
    "漫画 外伝 c001",                  # Japanese gaiden marker
])
def test_special_release_detected(title: str) -> None:
    """Stage 1 is detection-only. A True here says "operator should
    review this" — it does not yet change DB behaviour. Stage 2 wires
    this into a dedicated queue-review category and Stage 3 excludes
    specials from mainline coverage."""
    assert is_special_release(title) is True, (
        f"{title!r} should be flagged as a special/side-story release"
    )


@pytest.mark.parametrize("title", [
    "Manga Name v01.cbz",
    "Manga Name c001.cbz",
    "Manga Name Vol. 1-3.cbz",
    "Manga Name Complete Series",
])
def test_mainline_release_not_flagged_special(title: str) -> None:
    assert is_special_release(title) is False


# ────────────────────── 9. omnibus / title-number ambiguity ─────────────────

def test_omnibus_does_not_parse_as_chapter() -> None:
    """D7: 'Manga Name Omnibus 1' was pulling chap=1 out of the bare
    number fallback. Minimum bar: extract_chapter_num returns None.
    Preferred: extract_volume_num returns 1 (omnibus is volume-level).
    We assert the minimum; the preferred is stronger in audit Stage 2."""
    assert extract_chapter_num("Manga Name Omnibus 1") is None
    # Preferred behaviour — not a hard requirement for Stage 1, but pin
    # it here so Stage 2 has a failing test if someone regresses:
    assert extract_volume_num("Manga Name Omnibus 1") == 1


def test_three_in_one_edition_does_not_parse_as_chapter_3() -> None:
    """'3-in-1 Edition Vol. 1' was returning chap=3 from the leading
    digit. After D1, the has_vol guard catches Vol. 1 and suppresses
    the bare-number fallback."""
    title = "Manga Name 3-in-1 Edition Vol. 1"
    assert extract_volume_num(title) == 1
    assert extract_chapter_num(title) is None


def test_series_title_with_digit_does_not_leak_into_chapter() -> None:
    """20th Century Boys Vol. 1 was returning chap=20 (digit in series
    title). Guarded by D1."""
    title = "20th Century Boys Vol. 1"
    assert extract_volume_num(title) == 1
    assert extract_chapter_num(title) is None


def test_part_5_vol_17_classifies_as_volume() -> None:
    """D6: detect_pack_type was returning 'chapter' because '17'
    tripped the bare-number heuristic. With vol_num known, the heuristic
    must defer."""
    title = "Manga Name Part 5 Vol 17"
    assert extract_volume_num(title) == 17
    assert detect_pack_type(title, None, 20) == 'volume'


# ─────────────────── 10. resolution / filesize / year ignored ───────────────

@pytest.mark.parametrize("title,expected", [
    ("Manga Name v01 720p [Group]", 1),
    ("Manga Name v01 1080p",        1),
    ("Manga Name v01 300MB",        1),
    ("Manga Name v01 2GB",          1),
    ("Manga Name v01 (2015)",       1),
])
def test_resolution_filesize_year_do_not_poison_volume(title: str, expected: int) -> None:
    assert extract_volume_num(title) == expected


@pytest.mark.parametrize("title", [
    "Manga Name v01 720p [Group]",
    "Manga Name v01 1080p",
    "Manga Name v01 300MB",
    "Manga Name v01 (2015)",
])
def test_resolution_filesize_year_do_not_invent_chapter(title: str) -> None:
    assert extract_chapter_num(title) is None


def test_year_span_triggers_complete() -> None:
    """(2012-2021) complete — multi-year span should be treated as a
    complete-series pack even if no vol/chapter numbers present."""
    title = "Manga Name (2012-2021) complete"
    assert is_complete_pack(title) is True
    assert detect_pack_type(title, None, 10) == 'complete'


# ────────────── 11. fractional / letter-suffix parser behaviour ─────────────

@pytest.mark.parametrize("title,expected", [
    ("Manga Name v3a.cbz",  3.01),
    ("Manga Name v3b",      3.02),
    ("Manga Name v3½",      3.5),
    ("Manga Name v3¼",      3.25),
    ("Manga Name v3¾",      3.75),
    ("Manga Name Ch.3a",    3.01),
    ("Manga Name Ch.3½",    3.5),
])
def test_fractional_and_letter_suffixes(title: str, expected: float) -> None:
    """Parser produces the fractional value. The review UI's
    integer-only form field TRUNCATES this on round-trip — that's a
    Stage 2 defect (D11) and is NOT addressed here. This test pins the
    parser half of the contract so Stage 2 has a fixed target."""
    fn = extract_chapter_num if "Ch" in title else extract_volume_num
    assert fn(title) == pytest.approx(expected)


# ──────────── regression: bracket ranges should be volume, not chapter ──────

def test_bracket_range_is_volume_range_not_chapter() -> None:
    """'Manga Name [001-038]' is typically a scan-pack volume range.
    It should parse as a volume range and NOT leak a chapter value."""
    title = "Manga Name [001-038]"
    assert extract_volume_range(title) == (1.0, 38.0)
    assert extract_chapter_range(title) is None
    assert extract_chapter_num(title) is None
