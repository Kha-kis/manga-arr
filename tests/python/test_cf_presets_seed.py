"""Tests for built-in CF library + profile presets seed (PR #127).

A fresh Mangarr install must come up with a TRaSH-Guides-style starter
setup so users get usable scoring out of the box. The seed runs only on
fresh installs (no existing custom_formats AND no existing profiles) —
upgrades from earlier versions are not touched.

Behavior pinned here:
  - Fresh install: 11 built-in CFs + 4 profiles, with scores wired
  - "Best Available" is the only is_default=1 profile
  - Upgrade install (CFs already exist): seed is skipped entirely
  - Upgrade install (no CFs but profiles exist): seed is skipped
  - Hard rejects (-10000) wired to the right (profile, CF) pairs
"""
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401


@pytest.fixture
def freshdb_paths():
    """Return paths but DON'T init the DB — tests need to control whether
    init_db sees a virgin file or one we pre-populated to simulate
    upgrade scenarios."""
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-presets-keys-")
    yield {'db_path': db.name, 'key_dir': key_dir}
    for ext in ("", "-wal", "-shm"):
        p = db.name + ext
        if os.path.exists(p):
            os.unlink(p)


def _init_with(db_path, key_dir):
    """Run init_db with the given paths; restore module state on exit."""
    import main, shared, security
    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db_path
    shared.DB_PATH = db_path
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    return (orig_main_db, orig_shared_db)


def _restore(originals):
    import main, shared
    main.DB_PATH, shared.DB_PATH = originals


# ───────────────────── Fresh-install seed ─────────────────────


def test_fresh_install_seeds_full_cf_library(freshdb_paths):
    """A virgin DB must come up with the full built-in CF library."""
    from cf_presets import BUILTIN_CUSTOM_FORMATS
    originals = _init_with(freshdb_paths['db_path'], freshdb_paths['key_dir'])
    try:
        with sqlite3.connect(freshdb_paths['db_path']) as c:
            names = {r[0] for r in c.execute("SELECT name FROM custom_formats")}
        assert len(names) == len(BUILTIN_CUSTOM_FORMATS), (
            f"expected {len(BUILTIN_CUSTOM_FORMATS)} built-in CFs, got {len(names)}"
        )
        for cf in BUILTIN_CUSTOM_FORMATS:
            assert cf['name'] in names, f"missing CF: {cf['name']}"
    finally:
        _restore(originals)


def test_fresh_install_seeds_four_profile_presets(freshdb_paths):
    """A virgin DB must come up with the four profile presets and exactly
    one of them must be is_default=1 (must be Best Available)."""
    from cf_presets import PROFILE_PRESETS
    originals = _init_with(freshdb_paths['db_path'], freshdb_paths['key_dir'])
    try:
        with sqlite3.connect(freshdb_paths['db_path']) as c:
            rows = c.execute(
                "SELECT name, is_default FROM quality_profiles ORDER BY id"
            ).fetchall()
        names = [r[0] for r in rows]
        assert len(rows) == len(PROFILE_PRESETS), (
            f"expected {len(PROFILE_PRESETS)} profiles, got {len(rows)}: {names}"
        )
        for preset in PROFILE_PRESETS:
            assert preset['name'] in names, f"missing preset: {preset['name']}"
        defaults = [r[0] for r in rows if r[1] == 1]
        assert defaults == ['Best Available'], (
            f"exactly one profile must be is_default; got: {defaults}"
        )
    finally:
        _restore(originals)


def test_fresh_install_wires_scores_from_presets(freshdb_paths):
    """Each preset's `scores` dict must materialize as join-table rows
    with the correct integer values."""
    from cf_presets import PROFILE_PRESETS
    originals = _init_with(freshdb_paths['db_path'], freshdb_paths['key_dir'])
    try:
        with sqlite3.connect(freshdb_paths['db_path']) as c:
            for preset in PROFILE_PRESETS:
                pid = c.execute(
                    "SELECT id FROM quality_profiles WHERE name=?",
                    (preset['name'],)
                ).fetchone()[0]
                for cf_name, expected_score in preset['scores'].items():
                    if expected_score == 0:
                        continue
                    cf_id_row = c.execute(
                        "SELECT id FROM custom_formats WHERE name=?", (cf_name,)
                    ).fetchone()
                    assert cf_id_row is not None, (
                        f"preset '{preset['name']}' references CF '{cf_name}' "
                        "that isn't in the built-in library"
                    )
                    score_row = c.execute(
                        "SELECT score FROM quality_profile_custom_formats"
                        " WHERE profile_id=? AND format_id=?",
                        (pid, cf_id_row[0])
                    ).fetchone()
                    assert score_row is not None, (
                        f"preset '{preset['name']}' missing score for '{cf_name}'"
                    )
                    assert score_row[0] == expected_score, (
                        f"preset '{preset['name']}' / '{cf_name}': "
                        f"expected {expected_score}, got {score_row[0]}"
                    )
    finally:
        _restore(originals)


def test_fresh_install_hard_reject_wiring(freshdb_paths):
    """The hard-reject pairs are the most consequential semantics here —
    pin them explicitly so a careless preset edit can't silently flip
    a 'reject scanlations' profile into 'allow scanlations'."""
    originals = _init_with(freshdb_paths['db_path'], freshdb_paths['key_dir'])
    try:
        with sqlite3.connect(freshdb_paths['db_path']) as c:
            def score(profile_name, cf_name):
                row = c.execute(
                    "SELECT score FROM quality_profile_custom_formats qpcf"
                    " JOIN quality_profiles qp ON qp.id = qpcf.profile_id"
                    " JOIN custom_formats cf ON cf.id = qpcf.format_id"
                    " WHERE qp.name=? AND cf.name=?",
                    (profile_name, cf_name)
                ).fetchone()
                return row[0] if row else None

            assert score('Official Digital Only', 'Quality Scanlation') == -10000
            assert score('Official Digital Only', 'Japanese Raw') == -10000
            assert score('Scanlations OK', 'Japanese Raw') == -10000
            assert score('Japanese Raw', 'Official Digital') == -10000
            assert score('Japanese Raw', 'Quality Scanlation') == -10000
    finally:
        _restore(originals)


def test_default_profile_uses_new_upgrade_columns(freshdb_paths):
    """The Best Available preset must wire cutoff_format_score and
    min_upgrade_format_score (PR #124 columns) — those control the
    upgrade engine and 0 would mean 'never upgrade'."""
    originals = _init_with(freshdb_paths['db_path'], freshdb_paths['key_dir'])
    try:
        with sqlite3.connect(freshdb_paths['db_path']) as c:
            row = c.execute(
                "SELECT cutoff_format_score, min_upgrade_format_score"
                " FROM quality_profiles WHERE name='Best Available'"
            ).fetchone()
        assert row is not None
        assert row[0] == 10000, "cutoff_format_score must be 10000 (unbounded)"
        assert row[1] == 10, "min_upgrade_format_score must be 10 (loop prevention)"
    finally:
        _restore(originals)


# ───────────────────── Upgrade-install: don't clobber ─────────────────────


def test_upgrade_install_skips_seed_when_cfs_exist(freshdb_paths):
    """If custom_formats already has any row, init_db must not seed the
    library or the four presets — that would clobber user customizations
    on existing installs."""
    db_path = freshdb_paths['db_path']
    key_dir = freshdb_paths['key_dir']

    # First init: virgin DB → seed runs
    originals = _init_with(db_path, key_dir)
    try:
        with sqlite3.connect(db_path) as c:
            cf_count_before = c.execute(
                "SELECT COUNT(*) FROM custom_formats"
            ).fetchone()[0]
            profile_count_before = c.execute(
                "SELECT COUNT(*) FROM quality_profiles"
            ).fetchone()[0]
        assert cf_count_before > 0
        assert profile_count_before == 4

        # Second init on same DB: should be a no-op (seed gates on emptiness)
        import main
        main.init_db()
        with sqlite3.connect(db_path) as c:
            cf_count_after = c.execute(
                "SELECT COUNT(*) FROM custom_formats"
            ).fetchone()[0]
            profile_count_after = c.execute(
                "SELECT COUNT(*) FROM quality_profiles"
            ).fetchone()[0]
        assert cf_count_after == cf_count_before, "second init_db must not re-seed CFs"
        assert profile_count_after == profile_count_before, (
            "second init_db must not duplicate profiles"
        )
    finally:
        _restore(originals)


def test_upgrade_install_skips_seed_when_profiles_exist_but_cfs_dont(freshdb_paths):
    """Edge case: an old install that wiped its CFs but kept profiles.
    The seed gate requires BOTH tables to be empty, otherwise we'd add
    profiles with names that may collide with user names — skip the
    library to be safe and let the legacy 'Any Quality' fallback kick
    in only if there are also no profiles."""
    import main, shared, security

    main.DB_PATH = freshdb_paths['db_path']
    shared.DB_PATH = freshdb_paths['db_path']
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(freshdb_paths['key_dir'])

    # Pre-populate just the schema so we can manually seed a profile
    # before running the seed pass.
    main.init_db()  # fresh install — seeds 4 profiles + 11 CFs
    # Wipe just CFs to simulate the edge case
    with sqlite3.connect(freshdb_paths['db_path']) as c:
        c.execute("DELETE FROM custom_formats")
        cf_count = c.execute("SELECT COUNT(*) FROM custom_formats").fetchone()[0]
        prof_count_before = c.execute(
            "SELECT COUNT(*) FROM quality_profiles"
        ).fetchone()[0]
    assert cf_count == 0
    assert prof_count_before == 4

    # Re-init — must NOT re-seed CFs (profiles exist)
    main.init_db()
    with sqlite3.connect(freshdb_paths['db_path']) as c:
        cf_count_after = c.execute(
            "SELECT COUNT(*) FROM custom_formats"
        ).fetchone()[0]
        prof_count_after = c.execute(
            "SELECT COUNT(*) FROM quality_profiles"
        ).fetchone()[0]
    assert cf_count_after == 0, (
        "must not re-seed CF library when profiles already exist"
    )
    assert prof_count_after == prof_count_before, "profile count must be stable"


def test_legacy_fallback_when_both_tables_empty_and_seed_disabled(freshdb_paths):
    """If the CF-library import ever breaks at runtime (e.g. someone deletes
    cf_presets.py), init_db must still produce a working install with at
    least the legacy 'Any Quality' profile. Smoke-tested by verifying
    the elif branch wiring: seed `custom_formats` directly so the gate
    fails on the CF check, then verify the elif still fires."""
    db_path = freshdb_paths['db_path']
    key_dir = freshdb_paths['key_dir']

    import main, shared, security
    main.DB_PATH = db_path
    shared.DB_PATH = db_path
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)

    main.init_db()  # full seed
    # Now delete profiles only — keep CFs so the gate hits the legacy path
    with sqlite3.connect(db_path) as c:
        c.execute("DELETE FROM quality_profile_custom_formats")
        c.execute("DELETE FROM quality_profiles")
    main.init_db()
    with sqlite3.connect(db_path) as c:
        names = [r[0] for r in c.execute(
            "SELECT name FROM quality_profiles"
        ).fetchall()]
    # CFs already exist so the new seed is gated off; legacy elif fires
    assert names == ['Any Quality'], (
        f"legacy fallback must produce a single 'Any Quality' profile; got {names}"
    )


# ───────────────────── Preset data integrity ─────────────────────


def test_every_score_references_an_existing_cf():
    """Lint test on the data: each preset's score keys must match a
    name in BUILTIN_CUSTOM_FORMATS so the seed actually wires them."""
    from cf_presets import BUILTIN_CUSTOM_FORMATS, PROFILE_PRESETS
    cf_names = {cf['name'] for cf in BUILTIN_CUSTOM_FORMATS}
    for preset in PROFILE_PRESETS:
        for cf_name in preset['scores']:
            assert cf_name in cf_names, (
                f"preset '{preset['name']}' references CF '{cf_name}' "
                "but no built-in CF has that name"
            )


def test_exactly_one_preset_is_default():
    """Exactly one preset must be is_default — the seed assumes this and
    a constraint violation would be silent."""
    from cf_presets import PROFILE_PRESETS
    defaults = [p for p in PROFILE_PRESETS if p['is_default']]
    assert len(defaults) == 1, (
        f"expected exactly one default preset; got {len(defaults)}"
    )
    assert defaults[0]['name'] == 'Best Available'
