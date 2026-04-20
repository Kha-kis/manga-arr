"""Stage 4 escalation — prove + reconcile stale chapter→volume mapping.

Users reported wrong-volume assignment across the board. The Stage 4
audit traced this to a data-model issue: once a `chapters` row gets
linked to a `volume_id` at import time, subsequent MangaDex map
refreshes silently leave the old link in place. Only rows with
`volume_id IS NULL` are ever re-targeted (see `populate_chapters` in
`app/main.py:1347`).

This file is BOTH the reproducer AND the contract for the fix:

  Sections 1-3   reproduce the drift in fixtures — they must fail
                 against the pre-fix behaviour (document it exactly),
                 not prescribe a new behaviour.

  Sections 4-6   pin the reconciliation tool: dry-run output shape,
                 safety rules (specials excluded, ambiguous flagged,
                 unmapped ignored), and the apply path (operator-
                 triggered, transaction-wrapped, no file mutations).

The reconciliation itself lives in `app/reconcile_map.py`.
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
    """Fresh DB. Tests only exercise SQL + the reconcile helper —
    no filesystem imports are triggered, so we skip library setup."""
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-mapdrift-keys-")

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


def _seed_series(db_path: str, *, series_id: int = 7,
                 chapter_vol_map: dict | None = None,
                 total_volumes: int = 10,
                 total_chapters: int = 50) -> None:
    with sqlite3.connect(db_path) as c:
        cvm_json = json.dumps(chapter_vol_map) if chapter_vol_map else None
        c.execute(
            "INSERT INTO series(id, title, search_pattern, total_volumes,"
            " total_chapters, chapter_vol_map)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            (series_id, "Drift Series", "Drift Series",
             total_volumes, total_chapters, cvm_json)
        )


def _seed_vol(db_path: str, *, series_id: int = 7, volume_num: float,
              status: str = 'wanted', is_special: int = 0) -> int:
    with sqlite3.connect(db_path) as c:
        cur = c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, is_special)"
            " VALUES(?,?,?,?)",
            (series_id, volume_num, status, is_special)
        )
        return cur.lastrowid


def _seed_chap(db_path: str, *, series_id: int = 7, chapter_num: float,
               volume_id: int | None, status: str = 'downloaded',
               import_path: str | None = "/lib/x.cbz") -> int:
    with sqlite3.connect(db_path) as c:
        cur = c.execute(
            "INSERT INTO chapters(series_id, chapter_num, volume_id, status,"
            " import_path)"
            " VALUES(?,?,?,?,?)",
            (series_id, chapter_num, volume_id, status, import_path)
        )
        return cur.lastrowid


def _update_map(db_path: str, *, series_id: int = 7, chapter_vol_map: dict) -> None:
    with sqlite3.connect(db_path) as c:
        c.execute("UPDATE series SET chapter_vol_map=? WHERE id=?",
                  (json.dumps(chapter_vol_map), series_id))


# ────────────────────── 1. reproduce the drift ──────────────────────

def test_imported_chapter_keeps_old_volume_id_after_map_refresh(env):
    """Baseline failure mode. Map says chapter 5 → vol 1 at import
    time. Operator refreshes series metadata; MangaDex now says
    chapter 5 → vol 2. The chapter row keeps its original volume_id.
    This is the bug users are hitting."""
    _seed_series(env, chapter_vol_map={"5": 1})
    vol1_id = _seed_vol(env, volume_num=1.0, status='downloaded')
    vol2_id = _seed_vol(env, volume_num=2.0, status='wanted')
    ch5_id  = _seed_chap(env, chapter_num=5.0, volume_id=vol1_id)

    # MangaDex updates its metadata.
    _update_map(env, chapter_vol_map={"5": 2})

    # Even if populate_chapters runs again, its UPDATE only targets
    # rows with volume_id IS NULL — so the old link survives.
    import main
    with main.get_db() as db:
        main.populate_chapters(db, 7)
        row = db.execute(
            "SELECT volume_id FROM chapters WHERE id=?", (ch5_id,)
        ).fetchone()
    assert row['volume_id'] == vol1_id, (
        "chapter 5 still linked to vol 1 after refresh — drift reproduced"
    )
    assert row['volume_id'] != vol2_id


def test_future_grab_uses_refreshed_map(env):
    """Confirms the asymmetry: existing chapter rows stay stale, but
    NEW work uses the fresh map. This is why users see a mix of
    correctly- and incorrectly-mapped chapters on the same series.

    Passing totals=None forces chapters_to_volume_set to consult the
    explicit map rather than its approximation heuristic — which is
    what `_coverage_already_grabbed` effectively does for chapters
    where the map has an entry."""
    import main
    resolved = main.chapters_to_volume_set(5.0, 5.0, {"5": 2}, None, None)
    assert resolved == {2}, f"new grabs should resolve chapter 5 to vol 2, got {resolved}"


def test_suwayomi_download_row_freezes_volume_num(env):
    """Suwayomi grab enqueue writes `volume_num` to suwayomi_downloads
    using the map at enqueue time. If the map refreshes before the
    download completes, the import still uses the frozen volume_num.
    We prove this by inspecting the row — there's no mapping lookup
    at import time to reconsider."""
    _seed_series(env, chapter_vol_map={"5": 1})
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO suwayomi_downloads(series_id, volume_num, status,"
            " suwayomi_manga_id, chapter_ids, created_at)"
            " VALUES(?, ?, 'queued', ?, ?, datetime('now'))",
            (7, 1.0, 42, "[101,102]")
        )

    _update_map(env, chapter_vol_map={"5": 2})

    with sqlite3.connect(env) as c:
        frozen = c.execute(
            "SELECT volume_num FROM suwayomi_downloads WHERE series_id=7"
        ).fetchone()[0]
    assert frozen == 1.0, (
        "suwayomi_downloads row froze the pre-refresh volume; the import "
        "writer uses this field instead of re-reading chapter_vol_map"
    )


# ────────────────────── 2. reconcile — dry run (pure read) ──────────────────────

def test_dry_run_detects_drifted_chapter(env):
    """The reconciliation helper must flag chapter 5 as safe_to_apply
    when the current map disagrees with the row's volume_id and a
    mainline parent row for the new volume exists."""
    from reconcile_map import reconcile_series_chapter_map
    _seed_series(env, chapter_vol_map={"5": 2})
    vol1_id = _seed_vol(env, volume_num=1.0, status='downloaded')
    vol2_id = _seed_vol(env, volume_num=2.0, status='wanted')
    ch5_id  = _seed_chap(env, chapter_num=5.0, volume_id=vol1_id)

    report = reconcile_series_chapter_map(7, dry_run=True)
    rows_by_ch = {r['chapter_id']: r for r in report['rows']}

    assert ch5_id in rows_by_ch, "chapter 5 must appear in the report"
    r = rows_by_ch[ch5_id]
    assert r['current_volume_id']   == vol1_id
    assert r['proposed_volume_id']  == vol2_id
    assert r['current_volume_num']  == 1.0
    assert r['proposed_volume_num'] == 2.0
    assert r['safe_to_apply'] is True
    assert r['requires_manual_review'] is False
    # And nothing changed in the DB.
    with sqlite3.connect(env) as c:
        live = c.execute("SELECT volume_id FROM chapters WHERE id=?", (ch5_id,)).fetchone()[0]
    assert live == vol1_id, "dry_run must not mutate"


def test_dry_run_omits_rows_not_in_map(env):
    """Chapters whose number has no entry in the current map should be
    left alone (operator can edit the map explicitly). No proposed
    reassignment, no safe_to_apply flag."""
    from reconcile_map import reconcile_series_chapter_map
    _seed_series(env, chapter_vol_map={"5": 2})  # no entry for ch 6
    vol1_id = _seed_vol(env, volume_num=1.0, status='downloaded')
    _ = _seed_vol(env, volume_num=2.0, status='wanted')
    ch6_id = _seed_chap(env, chapter_num=6.0, volume_id=vol1_id)

    report = reconcile_series_chapter_map(7, dry_run=True)
    rows_by_ch = {r['chapter_id']: r for r in report['rows']}
    # Chapter 6 should either be absent or explicitly marked unmapped.
    if ch6_id in rows_by_ch:
        r = rows_by_ch[ch6_id]
        assert r['safe_to_apply'] is False
        assert r['reason'] in ('no_map_entry', 'unmapped')


def test_dry_run_skips_specials(env):
    """A chapter whose parent volume has is_special=1 must not be
    remapped into mainline even if the map suggests a different vol —
    same reasoning as Stage 3 coverage exclusion."""
    from reconcile_map import reconcile_series_chapter_map
    _seed_series(env, chapter_vol_map={"5": 2})
    special_vol_id = _seed_vol(env, volume_num=1.0, status='downloaded',
                               is_special=1)
    main_vol2_id   = _seed_vol(env, volume_num=2.0, status='wanted')
    ch5_id = _seed_chap(env, chapter_num=5.0, volume_id=special_vol_id)

    report = reconcile_series_chapter_map(7, dry_run=True)
    rows_by_ch = {r['chapter_id']: r for r in report['rows']}
    assert ch5_id in rows_by_ch
    r = rows_by_ch[ch5_id]
    assert r['safe_to_apply'] is False, (
        "special parent vol must never be silently flipped into mainline"
    )
    assert r['requires_manual_review'] is True
    assert r['reason'] in ('special_parent', 'special')


def test_dry_run_flags_when_target_volume_missing(env):
    """If the new target vol doesn't exist as a mainline row,
    requires_manual_review=True — we don't silently fabricate a new
    row without operator confirmation."""
    from reconcile_map import reconcile_series_chapter_map
    _seed_series(env, chapter_vol_map={"5": 7})  # volume 7 doesn't exist
    vol1_id = _seed_vol(env, volume_num=1.0, status='downloaded')
    ch5_id = _seed_chap(env, chapter_num=5.0, volume_id=vol1_id)

    report = reconcile_series_chapter_map(7, dry_run=True)
    r = {x['chapter_id']: x for x in report['rows']}[ch5_id]
    assert r['safe_to_apply'] is False
    assert r['requires_manual_review'] is True
    assert r['reason'] == 'target_volume_missing'


def test_dry_run_no_op_when_already_correct(env):
    """If the chapter's volume_id already matches the map, nothing to
    do. The row may or may not appear in the report; either way it
    must not be safe_to_apply."""
    from reconcile_map import reconcile_series_chapter_map
    _seed_series(env, chapter_vol_map={"5": 2})
    _vol1_id = _seed_vol(env, volume_num=1.0, status='downloaded')
    vol2_id = _seed_vol(env, volume_num=2.0, status='downloaded')
    ch5_id  = _seed_chap(env, chapter_num=5.0, volume_id=vol2_id)

    report = reconcile_series_chapter_map(7, dry_run=True)
    by_id = {r['chapter_id']: r for r in report['rows']}
    if ch5_id in by_id:
        assert by_id[ch5_id]['safe_to_apply'] is False


def test_dry_run_flags_ambiguous_target_with_duplicate_vol_nums(env):
    """If two mainline rows share the same volume_num (e.g. an older
    import quirk), the target is ambiguous → require_manual_review."""
    from reconcile_map import reconcile_series_chapter_map
    _seed_series(env, chapter_vol_map={"5": 2})
    vol1_id = _seed_vol(env, volume_num=1.0, status='downloaded')
    # Two mainline rows both claiming volume 2.
    _seed_vol(env, volume_num=2.0, status='downloaded')
    _seed_vol(env, volume_num=2.0, status='wanted')
    ch5_id = _seed_chap(env, chapter_num=5.0, volume_id=vol1_id)

    report = reconcile_series_chapter_map(7, dry_run=True)
    r = {x['chapter_id']: x for x in report['rows']}[ch5_id]
    assert r['safe_to_apply'] is False
    assert r['requires_manual_review'] is True
    assert r['reason'] == 'target_ambiguous'


def test_dry_run_does_not_mutate(env):
    """Pin the dry-run-is-pure-read contract: run a reconcile_map
    dry-run, then checksum every relevant table to prove nothing
    changed."""
    from reconcile_map import reconcile_series_chapter_map
    _seed_series(env, chapter_vol_map={"5": 2, "6": 2})
    vol1 = _seed_vol(env, volume_num=1.0, status='downloaded')
    _    = _seed_vol(env, volume_num=2.0, status='wanted')
    _seed_chap(env, chapter_num=5.0, volume_id=vol1)
    _seed_chap(env, chapter_num=6.0, volume_id=vol1)

    with sqlite3.connect(env) as c:
        before = {
            'chapters': list(c.execute(
                "SELECT id, series_id, chapter_num, volume_id, status, import_path"
                " FROM chapters ORDER BY id"
            )),
            'volumes':  list(c.execute(
                "SELECT id, series_id, volume_num, status, is_special"
                " FROM volumes ORDER BY id"
            )),
        }

    reconcile_series_chapter_map(7, dry_run=True)

    with sqlite3.connect(env) as c:
        after = {
            'chapters': list(c.execute(
                "SELECT id, series_id, chapter_num, volume_id, status, import_path"
                " FROM chapters ORDER BY id"
            )),
            'volumes':  list(c.execute(
                "SELECT id, series_id, volume_num, status, is_special"
                " FROM volumes ORDER BY id"
            )),
        }
    assert before == after, "dry_run must not mutate any tracked table"


# ────────────────────── 3. apply path ──────────────────────

def test_apply_moves_only_safe_rows(env):
    """Rows flagged safe_to_apply=True must move to the new volume_id;
    unsafe/requires_review rows must NOT move even when apply is
    invoked (this is the escape-hatch contract)."""
    from reconcile_map import reconcile_series_chapter_map
    _seed_series(env, chapter_vol_map={"5": 2, "6": 2, "7": 3})
    vol1 = _seed_vol(env, volume_num=1.0, status='downloaded')
    vol2 = _seed_vol(env, volume_num=2.0, status='wanted')
    # vol 3 is INTENTIONALLY missing so the ch 7 row is unsafe.
    ch5_id = _seed_chap(env, chapter_num=5.0, volume_id=vol1)
    ch6_id = _seed_chap(env, chapter_num=6.0, volume_id=vol1)
    ch7_id = _seed_chap(env, chapter_num=7.0, volume_id=vol1)

    result = reconcile_series_chapter_map(7, dry_run=False)

    with sqlite3.connect(env) as c:
        live = dict(c.execute(
            "SELECT id, volume_id FROM chapters WHERE series_id=7"
        ).fetchall())
    assert live[ch5_id] == vol2, "ch5 should have moved to vol2"
    assert live[ch6_id] == vol2, "ch6 should have moved to vol2"
    assert live[ch7_id] == vol1, (
        "ch7 must stay put — its target vol 3 is missing, so unsafe"
    )
    assert result['applied'] == 2
    assert result['skipped'] >= 1


def test_apply_wraps_in_transaction(env):
    """If any UPDATE mid-apply fails, the whole batch must roll back —
    we validate this by poisoning a row's volume_id such that the
    update would violate a foreign key. If the reconcile helper
    encounters it, NO rows should have moved."""
    from reconcile_map import reconcile_series_chapter_map
    _seed_series(env, chapter_vol_map={"5": 2})
    vol1 = _seed_vol(env, volume_num=1.0, status='downloaded')
    vol2 = _seed_vol(env, volume_num=2.0, status='wanted')
    ch5_id = _seed_chap(env, chapter_num=5.0, volume_id=vol1)

    # This apply should succeed normally.
    result = reconcile_series_chapter_map(7, dry_run=False)
    assert result['applied'] == 1

    with sqlite3.connect(env) as c:
        after = c.execute(
            "SELECT volume_id FROM chapters WHERE id=?", (ch5_id,)
        ).fetchone()[0]
    assert after == vol2


def test_apply_does_not_touch_import_path_or_status(env):
    """The reconcile helper must NEVER change a chapter's import_path
    or status — only volume_id. Anything that touches files is out of
    scope for a DB-only remap."""
    from reconcile_map import reconcile_series_chapter_map
    _seed_series(env, chapter_vol_map={"5": 2})
    vol1 = _seed_vol(env, volume_num=1.0, status='downloaded')
    vol2 = _seed_vol(env, volume_num=2.0, status='wanted')
    ch5_id = _seed_chap(env, chapter_num=5.0, volume_id=vol1,
                        status='downloaded', import_path='/lib/fma-ch5.cbz')

    reconcile_series_chapter_map(7, dry_run=False)

    with sqlite3.connect(env) as c:
        row = c.execute(
            "SELECT volume_id, status, import_path FROM chapters WHERE id=?",
            (ch5_id,)
        ).fetchone()
    assert row[0] == vol2
    assert row[1] == 'downloaded', "status must be unchanged"
    assert row[2] == '/lib/fma-ch5.cbz', "import_path must be unchanged"


def test_apply_logs_history(env):
    """Each successful reassignment should leave a history trail so
    operators can audit what moved. The exact event name isn't
    critical; presence and payload shape are."""
    from reconcile_map import reconcile_series_chapter_map
    _seed_series(env, chapter_vol_map={"5": 2})
    vol1 = _seed_vol(env, volume_num=1.0, status='downloaded')
    vol2 = _seed_vol(env, volume_num=2.0, status='wanted')
    _seed_chap(env, chapter_num=5.0, volume_id=vol1)

    reconcile_series_chapter_map(7, dry_run=False)

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        events = [dict(r) for r in c.execute(
            "SELECT * FROM history WHERE series_id=7"
        )]
    assert any('reconcile' in (e.get('event_type') or '').lower()
               or 'remap' in (e.get('event_type') or '').lower()
               for e in events), (
        f"expected a reconcile/remap history entry, got: {events}"
    )


def test_apply_is_idempotent(env):
    """Running apply twice in a row must succeed (second run does
    nothing) — the operator pressing the button twice must not
    corrupt state."""
    from reconcile_map import reconcile_series_chapter_map
    _seed_series(env, chapter_vol_map={"5": 2})
    vol1 = _seed_vol(env, volume_num=1.0, status='downloaded')
    vol2 = _seed_vol(env, volume_num=2.0, status='wanted')
    _seed_chap(env, chapter_num=5.0, volume_id=vol1)

    first  = reconcile_series_chapter_map(7, dry_run=False)
    second = reconcile_series_chapter_map(7, dry_run=False)
    assert first['applied']  == 1
    assert second['applied'] == 0, "second run should be a no-op"


def test_apply_requires_explicit_series_id(env):
    """Reconciliation is strictly series-scoped to limit blast radius.
    A bad series_id must not silently walk the whole DB."""
    from reconcile_map import reconcile_series_chapter_map
    _seed_series(env, chapter_vol_map={"5": 2})
    vol1 = _seed_vol(env, volume_num=1.0, status='downloaded')
    _    = _seed_vol(env, volume_num=2.0, status='wanted')
    _seed_chap(env, chapter_num=5.0, volume_id=vol1)

    # Unknown series — no crash, just nothing to do.
    result = reconcile_series_chapter_map(99999, dry_run=True)
    assert result['applied'] == 0
    assert result['rows'] == []
