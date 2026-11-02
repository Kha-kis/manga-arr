"""Exact lexical equivalence and deterministic ASCII scan-cost regression."""

from __future__ import annotations

import builtins
from typing import cast

import pytest

from file_identity import InvalidIdentity
import file_identity_store as store


def _legacy_text(value: object, *, empty: bool = False) -> str:
    if type(value) is not str or (not empty and not value):
        raise InvalidIdentity("identity text has an invalid type or is empty")
    result = cast(str, value)
    if "\x00" in result or any(0xD800 <= ord(c) <= 0xDFFF for c in result):
        raise InvalidIdentity("identity text contains invalid characters")
    return result


def _assert_equivalent(value: object, *, empty: bool = False) -> None:
    try:
        expected = _legacy_text(value, empty=empty)
    except InvalidIdentity as expected_error:
        with pytest.raises(InvalidIdentity) as actual:
            store._text(value, empty=empty)
        assert type(actual.value) is type(expected_error)
        assert str(actual.value) == str(expected_error)
    else:
        result = store._text(value, empty=empty)
        assert result is value and result is expected


@pytest.mark.parametrize(
    "value",
    ["actual ASCII text", "".join(chr(code) for code in range(1, 128))],
    ids=["ordinary", "all-non-NUL-ASCII"],
)
def test_ascii_validation_avoids_python_character_scan(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    calls: list[str] = []

    def counted(character: str) -> int:
        calls.append(character)
        return builtins.ord(character)

    monkeypatch.setattr(store, "ord", counted, raising=False)
    assert store._text(value) is value
    assert calls == []


@pytest.mark.parametrize("empty", [False, True])
def test_all_ascii_single_and_pair_values_match_legacy(empty: bool) -> None:
    _assert_equivalent("", empty=empty)
    for first in range(128):
        character = chr(first)
        for value in (
            character,
            character + "tail",
            "head" + character + "tail",
            "head" + character,
        ):
            _assert_equivalent(value, empty=empty)
        for second in range(128):
            _assert_equivalent(character + chr(second), empty=empty)


@pytest.mark.parametrize("position", ["prefix", "middle", "end"])
@pytest.mark.parametrize("empty", [False, True])
def test_all_surrogates_match_legacy(position: str, empty: bool) -> None:
    for code in range(0xD800, 0xE000):
        surrogate = chr(code)
        value = {
            "prefix": surrogate + "tail",
            "middle": "head" + surrogate + "tail",
            "end": "head" + surrogate,
        }[position]
        _assert_equivalent(value, empty=empty)


@pytest.mark.parametrize(
    "value",
    [
        "\x80",
        "\u00e9",
        "\ud7ff",
        "\ue000",
        "\uffff",
        "\U00010000",
        "\U0001f600",
        "\U0010ffff",
    ],
)
def test_non_ascii_retains_exact_original_scan(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    text = "prefix" + value + "suffix"
    calls: list[str] = []

    def counted(character: str) -> int:
        calls.append(character)
        return builtins.ord(character)

    monkeypatch.setattr(store, "ord", counted, raising=False)
    _assert_equivalent(text)
    assert calls == list(text)


@pytest.mark.parametrize(
    "value", ["\x00ASCII", "ASCII\x00tail", "\x00\u00e9", "\ud800\x00"]
)
@pytest.mark.parametrize("empty", [False, True])
def test_nul_refuses_before_any_surrogate_scan(
    monkeypatch: pytest.MonkeyPatch, value: str, empty: bool
) -> None:
    calls: list[str] = []

    def counted(character: str) -> int:
        calls.append(character)
        return builtins.ord(character)

    monkeypatch.setattr(store, "ord", counted, raising=False)
    _assert_equivalent(value, empty=empty)
    assert calls == []


@pytest.mark.parametrize("empty", [False, True])
def test_exact_type_and_empty_semantics_match_legacy(empty: bool) -> None:
    class StringSubclass(str):
        def isascii(self) -> bool:
            raise AssertionError("a string subclass must be rejected before inspection")

    values: list[object] = [
        None,
        False,
        True,
        0,
        1,
        1.25,
        b"ASCII",
        bytearray(b"ASCII"),
        memoryview(b"ASCII"),
        [],
        {},
        object(),
        StringSubclass(""),
        StringSubclass("ASCII"),
        StringSubclass("\u00e9"),
        "",
        "ASCII",
        "\u00e9",
    ]
    for value in values:
        _assert_equivalent(value, empty=empty)
