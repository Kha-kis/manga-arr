"""Tests for manga-native custom-format spec types (PR #126).

Sonarr-style CFs match TV-style metadata (release groups, indexers,
language, raw title). Manga has its own structural fields the upstream
CFs miss:

  edition_is — strict enum match against the edition that
               files.detect_edition_type() returns. Includes the four
               Japanese print formats added in this PR (tankobon /
               bunkoban / kanzenban / aizoban).

  source_is  — Official Digital / Scanlation / Raw. Inferred from
               publisher patterns, fan-group patterns, and language
               signals.

The point of strict enums (vs the legacy free-text edition_contains) is
that user CFs survive renames in the underlying detection patterns —
they bind to the *classification* output, not to a substring in the
title.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401


@pytest.fixture
def env(tmp_path):
    """Fresh DB; CF evaluator is pure-Python so most tests don't need it,
    but the route-level smoke tests do."""
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-cfspec-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()
    main.ensure_api_key()
    try:
        yield {'db_path': db.name}
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


# ───────────────────── Japanese edition patterns ─────────────────────


def test_detect_edition_recognises_japanese_print_formats():
    """Tankobon / bunkoban / kanzenban / aizoban must each map to their
    own edition value (not silently fold into 'omnibus' or 'special')."""
    from files import detect_edition_type
    assert detect_edition_type("Berserk Tankobon Vol.1 [LuCaZ]") == 'tankobon'
    assert detect_edition_type("Death Note Kanzenban v01") == 'kanzenban'
    assert detect_edition_type("Lone Wolf Bunkoban Vol 1") == 'bunkoban'
    assert detect_edition_type("Vagabond Aizoban Vol.1") == 'aizoban'


def test_detect_edition_recognises_japanese_kanji():
    """The kanji forms appear in actual Japanese-source torrents."""
    from files import detect_edition_type
    assert detect_edition_type("ベルセルク 単行本 v01") == 'tankobon'
    assert detect_edition_type("Death Note 完全版 v01") == 'kanzenban'
    assert detect_edition_type("バガボンド 文庫版 v01") == 'bunkoban'
    assert detect_edition_type("ヴィンランド 愛蔵版 v01") == 'aizoban'


def test_aizoban_does_not_steal_special_match():
    """'Special edition' should still classify as 'special' even if the
    title also has the word aizoban — first-match wins, but the patterns
    must order such that the more-specific Japanese print form claims it
    when it's the genuine edition signal. Smoke test that 'special' on
    its own is unaffected."""
    from files import detect_edition_type
    assert detect_edition_type("One Piece Special Edition Vol.1") == 'special'


# ───────────────────── edition_is evaluator ─────────────────────


def test_edition_is_matches_detected_edition():
    """An edition_is spec passes only when the detected edition equals
    the spec value."""
    from routers.custom_formats import evaluate_custom_format
    specs = [{"type": "edition_is", "value": "omnibus"}]
    assert evaluate_custom_format(specs, "Naruto 3-in-1 Vol.1", 0, 0) is True
    assert evaluate_custom_format(specs, "Naruto Deluxe Vol.1", 0, 0) is False


def test_edition_is_defaults_undetected_to_single():
    """When no edition keyword is present the title classifies as 'single'.
    A spec for value=single must match plain titles."""
    from routers.custom_formats import evaluate_custom_format
    specs = [{"type": "edition_is", "value": "single"}]
    assert evaluate_custom_format(specs, "Naruto Vol.1 [LuCaZ]", 0, 0) is True
    assert evaluate_custom_format(specs, "Naruto Omnibus Vol.1", 0, 0) is False


def test_edition_is_negate_inverts_match():
    """negate flag flips the verdict (used for 'not omnibus' style CFs)."""
    from routers.custom_formats import evaluate_custom_format
    specs = [{"type": "edition_is", "value": "omnibus", "negate": True}]
    assert evaluate_custom_format(specs, "Naruto Omnibus Vol.1", 0, 0) is False
    assert evaluate_custom_format(specs, "Naruto Vol.1", 0, 0) is True


def test_edition_is_japanese_formats_routable():
    """Each Japanese edition value must be addressable by an edition_is
    spec — the whole point of adding them as enum values."""
    from routers.custom_formats import evaluate_custom_format
    for ed in ("tankobon", "kanzenban", "bunkoban", "aizoban"):
        specs = [{"type": "edition_is", "value": ed}]
        assert evaluate_custom_format(specs, f"Series {ed.title()} Vol.1", 0, 0) is True, ed
        assert evaluate_custom_format(specs, "Series Vol.1", 0, 0) is False, ed


# ───────────────────── source_is evaluator ─────────────────────


def test_source_is_official_digital_for_known_publisher():
    """Title containing a licensed publisher name → official_digital."""
    from routers.custom_formats import evaluate_custom_format
    specs = [{"type": "source_is", "value": "official_digital"}]
    assert evaluate_custom_format(
        specs, "One Piece Vol.1 [Viz Media]", 0, 0
    ) is True
    assert evaluate_custom_format(
        specs, "One Piece Vol.1 (Kodansha)", 0, 0
    ) is True
    assert evaluate_custom_format(
        specs, "One Piece Vol.1 [LuCaZ]", 0, 0
    ) is False


def test_source_is_scanlation_for_known_fan_group():
    """A known fan-scanlation group tag classifies as scanlation, even
    without an explicit release_group field."""
    from routers.custom_formats import evaluate_custom_format
    specs = [{"type": "source_is", "value": "scanlation"}]
    assert evaluate_custom_format(
        specs, "One Piece Vol.1 [LuCaZ]", 0, 0
    ) is True
    assert evaluate_custom_format(
        specs, "One Piece Vol.1 [Stick]", 0, 0
    ) is True


def test_source_is_scanlation_via_release_group_field():
    """If the parser pulled out a release_group, that alone is enough
    to call it scanlation (vs. an untagged official rip)."""
    from routers.custom_formats import evaluate_custom_format
    specs = [{"type": "source_is", "value": "scanlation"}]
    assert evaluate_custom_format(
        specs, "Some Series Vol.1", 0, 0,
        release_group="UnknownFanGroup",
    ) is True


def test_source_is_raw_for_japanese_only():
    """No publisher, no group, language=ja → raw."""
    from routers.custom_formats import evaluate_custom_format
    specs = [{"type": "source_is", "value": "raw"}]
    assert evaluate_custom_format(
        specs, "ベルセルク Vol.1 (Japanese)", 0, 0
    ) is True
    # Japanese title with publisher → official_digital, not raw
    assert evaluate_custom_format(
        specs, "Berserk Vol.1 (Japanese) [Kodansha]", 0, 0
    ) is False


def test_source_is_publisher_wins_over_fan_group():
    """If a title carries both a publisher and a fan-group tag, the
    publisher signal wins — that's how downloaders re-tag licensed rips
    and we shouldn't penalise them as fan releases."""
    from routers.custom_formats import detect_source_type
    assert detect_source_type("One Piece Vol.1 [Viz Media] (LuCaZ)") == 'official_digital'


def test_source_is_negate_inverts_match():
    from routers.custom_formats import evaluate_custom_format
    specs = [{"type": "source_is", "value": "official_digital", "negate": True}]
    assert evaluate_custom_format(
        specs, "One Piece Vol.1 [Viz Media]", 0, 0
    ) is False
    assert evaluate_custom_format(
        specs, "One Piece Vol.1 [LuCaZ]", 0, 0
    ) is True


# ───────────────────── Help-text exposure ─────────────────────


def test_create_modal_lists_new_spec_types(env):
    """Discoverability: the help text on /custom-formats must mention
    edition_is + source_is so users know the new spec types exist."""
    from fastapi.testclient import TestClient
    import main
    r = TestClient(main.app).get("/custom-formats")
    assert r.status_code == 200, r.text
    body = r.text
    assert 'edition_is' in body, "edition_is must appear in help text"
    assert 'source_is' in body, "source_is must appear in help text"
    # Enum values must be discoverable too
    assert 'tankobon' in body, "Japanese formats must appear in enum hint"
    assert 'official_digital' in body, "source enum must appear in hint"


# ───────────────────── Spec-list registration ─────────────────────


def test_new_specs_registered_in_spec_types_list():
    """The SPEC_TYPES list is what the rest of the codebase (and any
    future picker UI) introspects to know which types are valid. Both
    new types must appear there."""
    from routers.custom_formats import SPEC_TYPES
    assert 'edition_is' in SPEC_TYPES
    assert 'source_is' in SPEC_TYPES
