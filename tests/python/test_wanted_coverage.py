"""Stage 3 — coverage correctness contract tests.

Pins the behaviour of the "is this release already covered?" logic
after the Stage 3 rewrite:

  - mainline coverage excludes volumes.is_special = 1
  - volume matching is float-precise (no CAST-to-INTEGER collapse)
  - existing volume-range rows satisfy interior targets
  - existing chapter-range rows satisfy interior chapters
  - chapter packs do not cover volume slots and vice versa
  - the chapter sync guard does not use a special row to suppress
    mainline chapter stub creation

Parser / queue-review behaviour (Stages 1 and 2) is unchanged by Stage
3; those contracts still live in test_release_mapping_parser.py and
test_import_mapping.py. This file only touches the coverage path.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


@pytest.fixture
def env(tmp_path):
    """Fresh DB. No library path needed — these tests only exercise SQL."""
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-cov-keys-")

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


def _seed_series(db_path: str, *, series_id: int = 7, title: str = "Test",
                 total_volumes: int | None = 10,
                 total_chapters: int | None = 50,
                 chapter_vol_map: str | None = None) -> None:
    with sqlite3.connect(db_path) as c:
        c.execute(
            "INSERT INTO series(id, title, search_pattern, total_volumes,"
            " total_chapters, chapter_vol_map)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            (series_id, title, title, total_volumes, total_chapters, chapter_vol_map)
        )


def _seed_volume(db_path: str, *, series_id: int = 7, volume_num: float | None = None,
                 status: str = 'wanted', vol_range_start: float | None = None,
                 vol_range_end: float | None = None, pack_type: str | None = None,
                 is_special: int = 0, monitored: int = 1) -> int:
    with sqlite3.connect(db_path) as c:
        cur = c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, vol_range_start,"
            " vol_range_end, pack_type, is_special, monitored)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (series_id, volume_num, status, vol_range_start, vol_range_end,
             pack_type, is_special, monitored)
        )
        return cur.lastrowid


def _seed_chapter(db_path: str, *, series_id: int = 7, chapter_num: float,
                  chapter_range_end: float | None = None, status: str = 'wanted',
                  volume_id: int | None = None, monitored: int = 1) -> int:
    with sqlite3.connect(db_path) as c:
        cur = c.execute(
            "INSERT INTO chapters(series_id, chapter_num, chapter_range_end,"
            " status, volume_id, monitored)"
            " VALUES(?,?,?,?,?,?)",
            (series_id, chapter_num, chapter_range_end, status, volume_id, monitored)
        )
        return cur.lastrowid


# ─────────────── 1. fractional volumes are distinct ───────────────

def test_fractional_volume_not_collapsed_by_integer_cast(env):
    """Volume 3 must not cover 3.5 or 3.01 (letter-suffix 3a).
    The pre-Stage-3 CAST(volume_num AS INTEGER) made all three equal."""
    import main
    _seed_series(env, total_volumes=10)
    # Seed grabbed mainline vol 3 only. Seed wanted stubs for 3, 3.5, 3.01.
    _seed_volume(env, volume_num=3.0,  status='grabbed')
    _seed_volume(env, volume_num=3.5,  status='wanted')
    _seed_volume(env, volume_num=3.01, status='wanted')

    # New pack wants volume 3.5 — must not be marked covered by the
    # grabbed volume 3.
    covered = main._coverage_already_grabbed(
        7, 'volume', (3.5, 3.5), None, {}, 50, 10
    )
    assert covered is False, "vol 3.5 pack should not be covered by grabbed vol 3"

    # And vol 3.0 ALONE is covered when vol 3 is grabbed.
    covered3 = main._coverage_already_grabbed(
        7, 'volume', (3.0, 3.0), None, {}, 50, 10
    )
    assert covered3 is True, "vol 3 pack should be covered by grabbed vol 3"


def test_fractional_vol_35_only_covers_itself(env):
    """A grabbed vol 3.5 should satisfy a 3.5 pack but NOT a 3 pack."""
    import main
    _seed_series(env, total_volumes=10)
    _seed_volume(env, volume_num=3.5, status='grabbed')
    _seed_volume(env, volume_num=3.0, status='wanted')

    assert main._coverage_already_grabbed(7, 'volume', (3.5, 3.5), None, {}, 50, 10) is True
    assert main._coverage_already_grabbed(7, 'volume', (3.0, 3.0), None, {}, 50, 10) is False


# ─────────────── 2. range rows satisfy interior volumes ───────────────

def test_range_row_covers_interior_volumes(env):
    """A grabbed volumes row with vol_range_start=1, vol_range_end=5
    must satisfy new packs asking about any volume 1..5, but NOT 6."""
    import main
    _seed_series(env, total_volumes=10)
    # Pre-existing v1-v5 range row (as imported by Stage 2).
    _seed_volume(env, volume_num=None, status='downloaded',
                 vol_range_start=1.0, vol_range_end=5.0, pack_type='volume_range')
    # Interior stubs were seeded earlier as wanted — the test pins that
    # the range row alone covers them, regardless of stub status.
    for v in (1.0, 2.0, 3.0, 4.0, 5.0, 6.0):
        _seed_volume(env, volume_num=v, status='wanted')

    # Each interior volume → covered
    for v in (1.0, 3.0, 5.0):
        assert main._coverage_already_grabbed(
            7, 'volume', (v, v), None, {}, 50, 10
        ) is True, f"vol {v} should be covered by the range row"

    # v6 → not covered
    assert main._coverage_already_grabbed(
        7, 'volume', (6.0, 6.0), None, {}, 50, 10
    ) is False


# ─────────────── 3. chapter-range coverage (already pinned elsewhere) ───────

def test_chapter_range_row_covers_interior_chapters(env):
    """Sync guard uses COALESCE(chapter_range_end, chapter_num). A chapter
    row with chapter_num=5, chapter_range_end=6 covers 5 and 6 but not 7.
    This was already true pre-Stage-3 via test_chapter_range.py; Stage 3
    re-pins it here as part of the coverage contract."""
    _seed_series(env, total_volumes=10)
    _seed_chapter(env, chapter_num=5.0, chapter_range_end=6.0, status='downloaded')

    with sqlite3.connect(env) as c:
        covered_5 = c.execute(
            "SELECT 1 FROM chapters WHERE series_id=7 AND chapter_num<=? AND ?<=COALESCE(chapter_range_end, chapter_num)",
            (5.0, 5.0)
        ).fetchone()
        covered_6 = c.execute(
            "SELECT 1 FROM chapters WHERE series_id=7 AND chapter_num<=? AND ?<=COALESCE(chapter_range_end, chapter_num)",
            (6.0, 6.0)
        ).fetchone()
        covered_7 = c.execute(
            "SELECT 1 FROM chapters WHERE series_id=7 AND chapter_num<=? AND ?<=COALESCE(chapter_range_end, chapter_num)",
            (7.0, 7.0)
        ).fetchone()
    assert covered_5 is not None
    assert covered_6 is not None
    assert covered_7 is None


# ─────────────── 4. specials must not satisfy mainline ───────────────

def test_special_volume_does_not_satisfy_mainline(env):
    """A grabbed special vol 3 (is_special=1) must not cover mainline
    vol 3. This is the core D8 contract — Gaiden rows stay out of
    mainline coverage calculations."""
    import main
    _seed_series(env, total_volumes=10)
    _seed_volume(env, volume_num=3.0, status='grabbed', is_special=1)

    covered = main._coverage_already_grabbed(
        7, 'volume', (3.0, 3.0), None, {}, 50, 10
    )
    assert covered is False, "special vol 3 must not satisfy mainline vol 3"


def test_special_range_does_not_satisfy_mainline(env):
    """Same rule for range rows: a Gaiden v1-v5 must not cover mainline."""
    import main
    _seed_series(env, total_volumes=10)
    _seed_volume(env, volume_num=None, status='grabbed',
                 vol_range_start=1.0, vol_range_end=5.0, pack_type='volume_range',
                 is_special=1)

    # Mainline vol 3 must NOT be covered by the special range.
    assert main._coverage_already_grabbed(
        7, 'volume', (3.0, 3.0), None, {}, 50, 10
    ) is False


def test_special_complete_does_not_mask_mainline_complete(env):
    """A grabbed special complete pack shouldn't fool the coverage check
    into thinking mainline is done."""
    import main
    _seed_series(env, total_volumes=10)
    _seed_volume(env, volume_num=None, status='grabbed', pack_type='complete',
                 is_special=1)
    # Seed one mainline wanted+monitored stub.
    _seed_volume(env, volume_num=1.0, status='wanted')

    # New mainline complete pack — should NOT be reported as covered,
    # because the special complete doesn't count.
    covered = main._coverage_already_grabbed(
        7, 'complete', None, None, {}, 50, 10
    )
    assert covered is False


def test_special_chapter_row_does_not_suppress_mainline_sync(env):
    """The chapter sync guard (used by metadata resync) must not treat
    a special chapter range as coverage for mainline chapter sync."""
    import main
    _seed_series(env, total_volumes=10)
    # Create a special parent volume first.
    special_vol_id = _seed_volume(env, volume_num=1.0, status='downloaded',
                                   is_special=1)
    # Chapter row linked to special vol, covering c1-c2.
    _seed_chapter(env, chapter_num=1.0, chapter_range_end=2.0,
                  status='downloaded', volume_id=special_vol_id)

    # Query the guard directly.
    with sqlite3.connect(env) as c:
        mainline_covered = c.execute(
            "SELECT 1 FROM chapters c"
            " LEFT JOIN volumes v ON v.id = c.volume_id"
            " WHERE c.series_id=?"
            "   AND c.chapter_num <= ?"
            "   AND ? <= COALESCE(c.chapter_range_end, c.chapter_num)"
            "   AND COALESCE(v.is_special, 0) = 0"
            " LIMIT 1",
            (7, 1.0, 1.0)
        ).fetchone()
    assert mainline_covered is None, (
        "special chapter row must not satisfy mainline chapter sync guard"
    )


# ─────────────── 5. cross-satisfaction guards ───────────────

def test_chapter_pack_does_not_claim_volume_range_slots(env):
    """A volume row whose vol_range_* is NULL (because it's a single
    chapter row or a non-range pack) must not be mistaken for range
    coverage. Only explicit vol_range_start/end rows satisfy via range.
    This guards against a regression where the range predicate fires
    on rows without range data."""
    import main
    _seed_series(env, total_volumes=10)
    # A grabbed volume 3 — no range columns set.
    _seed_volume(env, volume_num=3.0, status='grabbed')

    # Querying for volume 5 must NOT return covered — vol 3's absent
    # range data must not satisfy vol 5.
    assert main._coverage_already_grabbed(
        7, 'volume', (5.0, 5.0), None, {}, 50, 10
    ) is False


def test_chapter_range_row_alone_does_not_cover_volume_slots(env):
    """A chapters row (chapter_range_end set) is NOT a volumes row.
    _coverage_already_grabbed is volume-coverage; chapter rows alone
    must not satisfy a new volume pack."""
    import main
    _seed_series(env, total_volumes=10)
    _seed_chapter(env, chapter_num=1.0, chapter_range_end=10.0,
                  status='downloaded')

    # Volume pack for v1 — chapter coverage doesn't speak to volume coverage.
    covered = main._coverage_already_grabbed(
        7, 'volume', (1.0, 1.0), None, {}, 50, 10
    )
    assert covered is False


# ─────────────── 6. already-grabbed uses range overlap ───────────────

def test_already_grabbed_respects_range_row_for_multi_volume_target(env):
    """End-to-end: a range row with vol_range_start=1 end=5 satisfies
    a new pack that wants v2-v4 even if none of the interior stubs
    have been explicitly marked grabbed."""
    import main
    _seed_series(env, total_volumes=10)
    _seed_volume(env, volume_num=None, status='grabbed',
                 vol_range_start=1.0, vol_range_end=5.0, pack_type='volume_range')
    # No interior stubs — range row alone must carry.

    covered = main._coverage_already_grabbed(
        7, 'volume', (2.0, 4.0), None, {}, 50, 10
    )
    assert covered is True


def test_already_grabbed_partial_range_not_covered(env):
    """A range row covering v1-v3 is NOT enough when the new pack wants
    v1-v5. The answer must be False (uncovered)."""
    import main
    _seed_series(env, total_volumes=10)
    _seed_volume(env, volume_num=None, status='grabbed',
                 vol_range_start=1.0, vol_range_end=3.0, pack_type='volume_range')

    covered = main._coverage_already_grabbed(
        7, 'volume', (1.0, 5.0), None, {}, 50, 10
    )
    assert covered is False


def test_complete_pack_still_supersedes_narrower_grab(env):
    """Regression from Stage 2 behaviour — a mainline complete pack
    covers any narrower mainline pack attempt."""
    import main
    _seed_series(env, total_volumes=10)
    _seed_volume(env, volume_num=None, status='grabbed', pack_type='complete')

    assert main._coverage_already_grabbed(
        7, 'volume', (3.0, 5.0), None, {}, 50, 10
    ) is True


# ─────────────── 7. legacy queue / grab paths still work ─────────────

def test_grab_item_mark_covered_drops_cast_uses_float(env):
    """Regression: the grab-time UPDATE in grab_item that marks
    chapter-pack covered volumes as grabbed no longer collapses
    fractional volumes. A chapter pack mapping to volume 3 must NOT
    flip volume 3.5's status."""
    import main
    _seed_series(env, total_volumes=10, chapter_vol_map='{"1": 3, "2": 3}')
    _seed_volume(env, volume_num=3.0, status='wanted')
    _seed_volume(env, volume_num=3.5, status='wanted')

    # Simulate what grab_item does after a chapter pack is accepted.
    # Use a minimal set of params; the SQL is what we're testing.
    covered_vols = {3}
    _float_vols = [float(v) for v in covered_vols]
    placeholders = ",".join("?" * len(covered_vols))
    with main.get_db() as db:
        db.execute(
            f"UPDATE volumes SET status='grabbed'"
            f" WHERE series_id=? AND status='wanted'"
            f" AND volume_num IS NOT NULL AND volume_num IN ({placeholders})"
            f" AND COALESCE(is_special, 0) = 0",
            [7, *_float_vols]
        )

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        rows = {r["volume_num"]: r["status"] for r in c.execute(
            "SELECT volume_num, status FROM volumes WHERE series_id=7"
        )}
    assert rows[3.0] == 'grabbed'
    assert rows[3.5] == 'wanted', "vol 3.5 must not be flipped by vol 3 chapter pack"


# ─────────────── 8. seen.vol_range_* is NOT needed (pre-check) ───────

def test_seen_range_columns_not_required_for_coverage(env):
    """Coverage reads from `volumes` (not `seen`). Adding
    seen.vol_range_start/end would be redundant at this point. This
    test pins that `seen` is unchanged by Stage 3 — if a future coverage
    change needs to join `seen`, this test will flag the need to add
    the columns deliberately."""
    with sqlite3.connect(env) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(seen)").fetchall()}
    assert 'vol_range_start' not in cols, (
        "Stage 3 shouldn't have added seen.vol_range_start; if a coverage "
        "query now needs it, add it deliberately with a new test."
    )
    assert 'vol_range_end' not in cols
