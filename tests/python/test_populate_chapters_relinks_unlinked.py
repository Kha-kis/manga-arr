"""Bug #4 fix: populate_chapters must be able to re-link an existing
unlinked chapter row (volume_id IS NULL) when a cvm refresh gives it
a target volume, even when the coverage guard would otherwise treat
the row as "covered."

Live-session context: JoJo P4 had chapters 172-174 sitting with
status='downloaded' but volume_id=NULL. When we added cvm entries
pointing them at vol 18, populate_chapters returned 0 and left them
unlinked — the coverage guard short-circuited on the existing row.
We had to finish the job via reconcile_chapter_vol apply.
"""
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


@pytest.fixture
def env():
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-pop-keys-")

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


def _seed(db_path: str, *, cvm: dict, vols: list, chapters: list):
    """vols: list of volume_num floats. chapters: list of (ch_num, vol_id_or_None, status)."""
    import json
    with sqlite3.connect(db_path) as c:
        c.execute(
            "INSERT INTO series(id, title, search_pattern, total_volumes,"
            " total_chapters, chapter_vol_map) VALUES(?, ?, ?, ?, ?, ?)",
            (7, "S", "S", max([int(v) for v in vols] or [0]), len(chapters), json.dumps(cvm))
        )
        vid = {}
        for v in vols:
            cur = c.execute(
                "INSERT INTO volumes(series_id, volume_num, status, monitored)"
                " VALUES(?, ?, 'wanted', 1)",
                (7, float(v))
            )
            vid[float(v)] = cur.lastrowid
        for ch_num, vol_num, status in chapters:
            vol_id = vid[float(vol_num)] if vol_num is not None else None
            c.execute(
                "INSERT INTO chapters(series_id, volume_id, chapter_num, status, monitored)"
                " VALUES(?, ?, ?, ?, 1)",
                (7, vol_id, float(ch_num), status)
            )


def test_populate_chapters_links_existing_unlinked_downloaded_row(env):
    import main
    # Pre-state: vols 1 and 2 exist. Chapter 5 is downloaded but unlinked.
    # cvm says ch 5 → vol 2. Prior bug: populate_chapters returned 0 and
    # chapter stayed unlinked.
    _seed(
        env,
        cvm={'5': 2, '1': 1, '2': 1, '3': 1, '4': 1},
        vols=[1.0, 2.0],
        chapters=[(5, None, 'downloaded')],
    )

    with main.get_db() as db:
        created = main.populate_chapters(db, 7)
        link = db.execute(
            "SELECT volume_id FROM chapters WHERE series_id=7 AND chapter_num=5"
        ).fetchone()
        vol2_id = db.execute(
            "SELECT id FROM volumes WHERE series_id=7 AND volume_num=2.0"
        ).fetchone()['id']

    # populate_chapters didn't "create" (existing row) — but it linked.
    # The created-count may be 0 or higher depending on other inserts.
    assert link['volume_id'] == vol2_id, (
        "downloaded-but-unlinked chapter should be linked to its cvm target "
        "instead of being skipped by the coverage guard"
    )


def test_coverage_guard_still_suppresses_duplicate_against_pack(env):
    # Regression guard: a c001-002 pack row should still prevent
    # populate_chapters from creating a duplicate chapter-1 row.
    import json
    import main
    with sqlite3.connect(env) as c:
        c.execute(
            "INSERT INTO series(id, title, search_pattern, total_volumes,"
            " total_chapters, chapter_vol_map) VALUES(?, ?, ?, ?, ?, ?)",
            (8, "P", "P", 1, 2, json.dumps({'1': 1, '2': 1}))
        )
        cur = c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, monitored)"
            " VALUES(?, 1.0, 'downloaded', 1)", (8,)
        )
        vol_id = cur.lastrowid
        # A pack row covering chapters 1-2.
        c.execute(
            "INSERT INTO chapters(series_id, volume_id, chapter_num,"
            " chapter_range_end, status, monitored) VALUES(?, ?, 1.0, 2.0, 'downloaded', 1)",
            (8, vol_id)
        )

    with main.get_db() as db:
        main.populate_chapters(db, 8)
        ch_rows = db.execute(
            "SELECT chapter_num, chapter_range_end FROM chapters WHERE series_id=8"
        ).fetchall()
    # Exactly the one pack row should remain — no duplicate chapter-1 or -2 stubs.
    assert len(ch_rows) == 1
    assert ch_rows[0]['chapter_range_end'] == 2.0


def test_populate_chapters_still_inserts_brand_new_chapter(env):
    # Regression guard: for a truly new chapter (no row at all),
    # populate_chapters must still INSERT.
    import main
    _seed(
        env,
        cvm={'1': 1, '2': 1},
        vols=[1.0],
        chapters=[],  # empty
    )
    with main.get_db() as db:
        created = main.populate_chapters(db, 7)
        rows = db.execute(
            "SELECT chapter_num FROM chapters WHERE series_id=7 ORDER BY chapter_num"
        ).fetchall()
    assert created == 2
    assert [r['chapter_num'] for r in rows] == [1.0, 2.0]
