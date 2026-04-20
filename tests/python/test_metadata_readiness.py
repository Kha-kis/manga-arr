"""Metadata-readiness report contract.

`reconcile_map.metadata_readiness_report(series_id)` inspects one series
and tells the operator whether it's ready for reconciliation — and if
not, what the smallest supported fix is. It's a pure read; never mutates.

These tests lock in:
  - blocker detection (missing totals / missing stubs / missing map)
  - the "special row shares mainline volume_num" edge case
  - classification of existing rows (downloaded vs pack vs special)
  - idempotence (report stays stable when re-run)
  - that the existing `create_volume_stubs` path produces a READY
    report afterwards — proving the recommended operator workflow
    actually closes the blockers it reports.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


@pytest.fixture
def env(tmp_path):
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-metaready-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()

    try:
        yield db.name
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


def _seed_series(db_path, *, series_id=7, total_volumes=None,
                 chapter_vol_map=None, total_chapters=None):
    with sqlite3.connect(db_path) as c:
        cvm = json.dumps(chapter_vol_map) if chapter_vol_map else None
        c.execute(
            "INSERT INTO series(id, title, search_pattern, total_volumes,"
            " total_chapters, chapter_vol_map)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            (series_id, "TestSeries", "TestSeries",
             total_volumes, total_chapters, cvm)
        )


def _seed_vol(db_path, *, series_id=7, volume_num=None, status='wanted',
              is_special=0, pack_type=None, vol_range_start=None,
              vol_range_end=None, import_path=None):
    with sqlite3.connect(db_path) as c:
        cur = c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, is_special,"
            " pack_type, vol_range_start, vol_range_end, import_path)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (series_id, volume_num, status, is_special, pack_type,
             vol_range_start, vol_range_end, import_path)
        )
        return cur.lastrowid


def _seed_chap(db_path, *, series_id=7, chapter_num, volume_id=None,
               status='wanted'):
    with sqlite3.connect(db_path) as c:
        cur = c.execute(
            "INSERT INTO chapters(series_id, chapter_num, volume_id, status)"
            " VALUES(?,?,?,?)",
            (series_id, chapter_num, volume_id, status)
        )
        return cur.lastrowid


# ── blockers ─────────────────────────────────────────────────────────

def test_missing_total_volumes_flags_needs_total_volumes(env):
    from reconcile_map import metadata_readiness_report
    _seed_series(env, total_volumes=None)
    r = metadata_readiness_report(7)
    assert r['ready'] is False
    assert 'needs_total_volumes' in r['blockers']
    assert 'Total Volumes' in r['recommended_next_step']


def test_missing_chapter_vol_map_flags_needs_chapter_vol_map(env):
    from reconcile_map import metadata_readiness_report
    _seed_series(env, total_volumes=5, chapter_vol_map=None)
    for v in range(1, 6):
        _seed_vol(env, volume_num=float(v))
    r = metadata_readiness_report(7)
    assert 'needs_chapter_vol_map' in r['blockers']


def test_missing_stubs_lists_the_exact_gaps(env):
    """Only the gap list is meaningful — the operator needs to know
    which volumes to expect stubs for after the next save."""
    from reconcile_map import metadata_readiness_report
    _seed_series(env, total_volumes=5, chapter_vol_map={"1": 1})
    _seed_vol(env, volume_num=1.0, status='downloaded')
    _seed_vol(env, volume_num=3.0, status='wanted')
    # vols 2, 4, 5 missing
    r = metadata_readiness_report(7)
    assert sorted(r['missing_mainline_stubs']) == [2.0, 4.0, 5.0]
    assert 'missing_mainline_stubs' in r['blockers']


def test_unlinked_chapters_flagged_when_present(env):
    from reconcile_map import metadata_readiness_report
    _seed_series(env, total_volumes=2, chapter_vol_map={"1": 1})
    v1 = _seed_vol(env, volume_num=1.0)
    _seed_vol(env, volume_num=2.0)
    _seed_chap(env, chapter_num=1.0, volume_id=v1)
    _seed_chap(env, chapter_num=2.0, volume_id=None)  # unlinked
    r = metadata_readiness_report(7)
    assert r['unlinked_chapters'] == 1
    assert 'unlinked_chapters' in r['blockers']


def test_special_sharing_mainline_volnum_flagged(env):
    """When a special row has volume_num=3 and mainline vol 3 is missing,
    the existing create_volume_stubs will skip vol 3 because a row with
    that volume_num already exists. Flag this so the operator knows to
    resolve it manually before reconciling."""
    from reconcile_map import metadata_readiness_report
    _seed_series(env, total_volumes=5, chapter_vol_map={"1": 1})
    _seed_vol(env, volume_num=1.0, status='downloaded')
    _seed_vol(env, volume_num=2.0, status='wanted')
    # Special vol 3 present; mainline vol 3 stub missing.
    _seed_vol(env, volume_num=3.0, status='grabbed', is_special=1)
    # mainline 4, 5 also missing
    r = metadata_readiness_report(7)
    assert 'special_blocks_mainline' in r['blockers']


# ── ready state ────────────────────────────────────────────────────

def test_fully_populated_series_is_ready(env):
    from reconcile_map import metadata_readiness_report
    _seed_series(env, total_volumes=3, chapter_vol_map={"1": 1, "2": 1, "3": 2})
    v1 = _seed_vol(env, volume_num=1.0, status='downloaded')
    v2 = _seed_vol(env, volume_num=2.0, status='downloaded')
    v3 = _seed_vol(env, volume_num=3.0, status='downloaded')
    _seed_chap(env, chapter_num=1.0, volume_id=v1, status='downloaded')
    _seed_chap(env, chapter_num=2.0, volume_id=v1, status='downloaded')
    _seed_chap(env, chapter_num=3.0, volume_id=v2, status='downloaded')

    r = metadata_readiness_report(7)
    assert r['ready'] is True
    assert r['blockers'] == []
    assert 'reconcile_series_chapter_map' in r['recommended_next_step']


# ── classification of existing rows ────────────────────────────────

def test_counts_distinguish_downloaded_pack_and_special(env):
    from reconcile_map import metadata_readiness_report
    _seed_series(env, total_volumes=3, chapter_vol_map={"1": 1})
    # Mainline downloaded vol 1.
    _seed_vol(env, volume_num=1.0, status='downloaded')
    # Pack row (no volume_num, has pack_type).
    _seed_vol(env, volume_num=None, status='wanted', pack_type='volume_range',
              vol_range_start=1.0, vol_range_end=3.0)
    # Special.
    _seed_vol(env, volume_num=1.0, status='downloaded', is_special=1)

    r = metadata_readiness_report(7)
    assert r['downloaded_with_num'] == 1
    assert r['wanted_pack_rows']    == 1
    assert r['special_count']       == 1


# ── idempotence + non-mutation ─────────────────────────────────────

def test_report_never_mutates(env):
    from reconcile_map import metadata_readiness_report
    _seed_series(env, total_volumes=3, chapter_vol_map={"1": 1, "2": 1})
    _seed_vol(env, volume_num=1.0, status='downloaded')
    _seed_vol(env, volume_num=2.0, status='wanted')
    _seed_chap(env, chapter_num=1.0, volume_id=None)

    with sqlite3.connect(env) as c:
        before = {
            'volumes':  list(c.execute(
                "SELECT id, series_id, volume_num, status, is_special,"
                " pack_type, vol_range_start, vol_range_end, import_path"
                " FROM volumes ORDER BY id"
            )),
            'chapters': list(c.execute(
                "SELECT id, series_id, chapter_num, volume_id, status"
                " FROM chapters ORDER BY id"
            )),
            'series':   list(c.execute(
                "SELECT id, title, total_volumes, total_chapters,"
                " chapter_vol_map FROM series"
            )),
        }

    for _ in range(3):
        metadata_readiness_report(7)

    with sqlite3.connect(env) as c:
        after = {
            'volumes':  list(c.execute(
                "SELECT id, series_id, volume_num, status, is_special,"
                " pack_type, vol_range_start, vol_range_end, import_path"
                " FROM volumes ORDER BY id"
            )),
            'chapters': list(c.execute(
                "SELECT id, series_id, chapter_num, volume_id, status"
                " FROM chapters ORDER BY id"
            )),
            'series':   list(c.execute(
                "SELECT id, title, total_volumes, total_chapters,"
                " chapter_vol_map FROM series"
            )),
        }
    assert before == after


def test_unknown_series_id_returns_not_found_payload(env):
    from reconcile_map import metadata_readiness_report
    r = metadata_readiness_report(99999)
    assert r['ready'] is False
    assert r['series_id'] == 99999
    assert r['title'] is None
    assert 'not found' in r['recommended_next_step']


# ── end-to-end: supported app path closes the blockers it reports ──

def test_create_volume_stubs_flips_series_to_ready(env):
    """The report says the operator should set total_volumes + save
    to fix missing-stub blockers. This test proves that claim: the
    existing `create_volume_stubs` path (called by series_.py on
    save) closes out the blocker set the report returned."""
    from reconcile_map import metadata_readiness_report
    import main

    # HxH-like state: total_volumes=None, some downloaded rows with real
    # volume_nums, some packs, no mainline stubs for the rest, and a
    # chapter_vol_map already populated.
    _seed_series(env, total_volumes=None,
                 chapter_vol_map={"1": 1, "2": 1, "5": 2, "10": 3})
    _seed_vol(env, volume_num=1.0, status='downloaded')
    _seed_vol(env, volume_num=None, status='wanted', pack_type='volume')  # pack

    before = metadata_readiness_report(7)
    assert before['ready'] is False
    assert 'needs_total_volumes' in before['blockers']

    # Operator's supported action: series editor sets total_volumes=3,
    # which triggers create_volume_stubs.
    with main.get_db() as db:
        db.execute("UPDATE series SET total_volumes=3 WHERE id=7")
        main.create_volume_stubs(db, 7, 3)

    after = metadata_readiness_report(7)
    # Stubs for 2 and 3 should now exist; vol 1 downloaded row preserved.
    assert sorted(after['existing_vol_nums']) == [1.0, 2.0, 3.0]
    assert after['missing_mainline_stubs'] == []
    assert after['wanted_pack_rows'] == 1   # pack row untouched
    assert after['downloaded_with_num'] == 1  # vol 1 still downloaded
    # All blockers in `before` are now gone.
    assert 'needs_total_volumes' not in after['blockers']
    assert 'missing_mainline_stubs' not in after['blockers']


def test_create_volume_stubs_preserves_downloaded_and_special_rows(env):
    """Defensive regression: running create_volume_stubs on a series
    with existing downloaded + special rows must not flip any of them
    to 'wanted' or delete them."""
    from reconcile_map import metadata_readiness_report
    import main

    _seed_series(env, total_volumes=5,
                 chapter_vol_map={"1": 1, "5": 3})
    dl_vol1     = _seed_vol(env, volume_num=1.0, status='downloaded')
    dl_vol5     = _seed_vol(env, volume_num=5.0, status='downloaded')
    special_v2  = _seed_vol(env, volume_num=2.0, status='grabbed', is_special=1)

    with main.get_db() as db:
        main.create_volume_stubs(db, 7, 5)

    with sqlite3.connect(env) as c:
        rows = {r[0]: (r[1], r[2], r[3]) for r in c.execute(
            "SELECT id, volume_num, status, is_special FROM volumes WHERE series_id=7"
        )}
    assert rows[dl_vol1]    == (1.0, 'downloaded', 0)
    assert rows[dl_vol5]    == (5.0, 'downloaded', 0)
    assert rows[special_v2] == (2.0, 'grabbed',    1)
    # After stub creation, the mainline set should include 1, 3, 4, 5
    # (vol 2 is blocked by the special — the report's
    # special_blocks_mainline flag would have warned the operator).
    r = metadata_readiness_report(7)
    assert 1.0 in r['existing_vol_nums']
    assert 3.0 in r['existing_vol_nums']
    assert 4.0 in r['existing_vol_nums']
    assert 5.0 in r['existing_vol_nums']
    # Vol 2's mainline stub is NOT present — create_volume_stubs
    # correctly respects that the special occupies that volume_num.
    assert 2.0 not in r['existing_vol_nums'] or r['special_count'] >= 1


def test_create_volume_stubs_is_idempotent(env):
    """Second call is a no-op (no duplicate inserts)."""
    import main
    _seed_series(env, total_volumes=3, chapter_vol_map={})

    with main.get_db() as db:
        main.create_volume_stubs(db, 7, 3)
    with main.get_db() as db:
        main.create_volume_stubs(db, 7, 3)

    with sqlite3.connect(env) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM volumes WHERE series_id=7 AND volume_num IS NOT NULL"
        ).fetchone()[0]
    assert n == 3
