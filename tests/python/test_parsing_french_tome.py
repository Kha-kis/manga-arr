"""Regression coverage for French tome markers and language tags."""

from __future__ import annotations

# pyright: reportMissingTypeStubs=false

import sys

import pytest

sys.path.insert(0, "app")

from parsing import (  # noqa: E402
    extract_chapter_num,
    extract_volume_num,
    extract_volume_range,
    is_foreign_language,
    matches,
)


RC4_MANGA_FR_RELEASE = (
    "One-Punch Man T21 (One-Murata) [2020] [Digital-1920] "
    "[Manga FR] [PapriKa+] (cbz)"
)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        (RC4_MANGA_FR_RELEASE, 21.0),
        ("One-Punch Man - T29 (cbz)", 29.0),
        ("One-Punch Man T 21 (cbz)", 21.0),
        ("One-Punch Man T.21 (cbz)", 21.0),
        ("One-Punch Man Tome 21 (cbz)", 21.0),
    ],
)
def test_french_tome_marker_is_a_volume_without_a_ghost_chapter(
    title: str, expected: float
) -> None:
    assert extract_volume_num(title) == expected
    assert extract_volume_range(title) is None
    assert extract_chapter_num(title) is None


@pytest.mark.parametrize(
    "title",
    [
        RC4_MANGA_FR_RELEASE,
        "One-Punch Man - T29 (cbz)",
        "One-Punch Man T 21 (cbz)",
        "One-Punch Man T.21 (cbz)",
        "One-Punch Man Tome 21 (cbz)",
    ],
)
def test_french_tome_marker_is_stripped_from_fuzzy_series_match(title: str) -> None:
    assert matches("One-Punch Man", title, threshold=1.0)


@pytest.mark.parametrize(
    "title",
    [
        "Manga Name ST21",
        "Manga Name T2100",
        "Manga Name T21X",
        "Manga Name Tome21",
        "Manga Name T21-Pro",
        "Manga Name T21-23",
    ],
)
def test_french_tome_marker_rejects_embedded_and_model_numbers(title: str) -> None:
    assert extract_volume_num(title) is None
    assert extract_chapter_num(title) is None


def test_model_number_is_not_stripped_from_fuzzy_series_match() -> None:
    assert not matches("Manga Model", "Manga Model T21-Pro", threshold=1.0)


@pytest.mark.parametrize(
    "title",
    [
        RC4_MANGA_FR_RELEASE,
        "One-Punch Man T21 [Manga FR]",
        "One-Punch Man T21 [Manga French]",
        "One-Punch Man T21 [Manga Français]",
        "One-Punch Man T21 [FR]",
        "One-Punch Man T21 French",
    ],
)
def test_french_language_markers_are_rejected(title: str) -> None:
    assert is_foreign_language(title)


@pytest.mark.parametrize(
    "title",
    [
        "Frieren T21",
        "Manga Freedom T21",
        "One-Punch Man T21 Manga FR",
        "One-Punch Man T21 [Manga FRE]",
        "One-Punch Man T21 [Manga FR-HD]",
        "One-Punch Man T21 [MangaFR]",
    ],
)
def test_french_language_marker_boundaries_remain_conservative(title: str) -> None:
    assert not is_foreign_language(title)


def test_existing_year_decimal_fraction_and_range_parsing_is_preserved() -> None:
    assert extract_volume_num("One.Punch.Man.v13.2018.F.digital.aKraa") == 13.0
    assert extract_chapter_num("Manga.Name.Ch.25.2025.Digital") == 25.0
    assert extract_volume_num("Manga Name v10.5") == 10.5
    assert extract_volume_num("Manga Name v3½") == 3.5
    assert extract_volume_range("Manga Name v1.5-v2.5") == (1.5, 2.5)
