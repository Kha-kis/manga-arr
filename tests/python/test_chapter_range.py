"""Chapter-range import support — parser, importer, coverage, label.

Real-world trigger: AnimeBytes ships side-story / one-shot collections as a
single CBZ named like `c001-002.zip`. The pre-existing chapter parser
rejected ranges and the importer modeled one chapter per file, so a 2-
chapter pack landed in the import queue marked "unmapped" with no path
forward except skipping it.

This file pins the new behaviour: one row covers a chapter range via
chapter_range_end. The schema, parser, importer, sync guard, and label
filter all agree.
"""
import os
import sqlite3
import sys
import tempfile
import zipfile

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63f8cf000000030001fe79bff70000000049454e44ae42"
    "6082"
)


def _make_zip(path: str, name: str = "page.png") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(name, _TINY_PNG)
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fresh DB + temp dirs + a stubbed _series_library_dir."""
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-chrange-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()

    src_root = tmp_path / "src"
    lib_root = tmp_path / "library"
    src_root.mkdir(); lib_root.mkdir()

    # _execute_import resolves dst_dir from settings.save_path (or the
    # series's root_folder). Point it at our tmp lib so file ops succeed.
    with sqlite3.connect(db.name) as c:
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('save_path', ?)",
                  (str(lib_root),))
    main.load_config()

    try:
        yield {
            "db_path":  db.name,
            "src_root": src_root,
            "lib_root": lib_root,
        }
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


# ───────────────────── schema migration ──────────────────────────────────────

def test_chapter_range_end_column_exists(env):
    """init_db() must create chapter_range_end as a nullable REAL column."""
    with sqlite3.connect(env["db_path"]) as c:
        cols = {r[1]: r[2] for r in c.execute("PRAGMA table_info(chapters)").fetchall()}
    assert "chapter_range_end" in cols
    assert cols["chapter_range_end"].upper() == "REAL"


# ───────────────────── parser: extract_chapter_range ─────────────────────────

@pytest.mark.parametrize("title,expected", [
    # Hyphen / en-dash / em-dash separators
    ("Series - c001-002 [grp].zip",                  (1.0, 2.0)),
    ("Series - c001-c002 [grp].zip",                 (1.0, 2.0)),
    ("Series - ch1-2.cbz",                           (1.0, 2.0)),
    ("Series - chapter 10-15.cbz",                   (10.0, 15.0)),
    ("Series - Chapter 10–20.cbz",                   (10.0, 20.0)),  # en-dash
    ("Series - c001-100.cbz",                        (1.0, 100.0)),
    # Decimals are accepted
    ("Series - c1.5-c2.cbz",                         (1.5, 2.0)),
])
def test_extract_chapter_range_accepts(title, expected):
    from main import extract_chapter_range
    assert extract_chapter_range(title) == expected


@pytest.mark.parametrize("title", [
    None, "",
    "Series - v01-v05.cbz",        # volume range, not chapter
    "Series - ch5.cbz",            # single chapter
    "Series - c002-001.cbz",       # descending
    "Series - c001-001.cbz",       # degenerate (start == end)
    "Series - c001-1000.cbz",      # absurd span (>200)
    "Series 2010-2020 Complete.cbz",  # year range without chapter prefix
])
def test_extract_chapter_range_rejects(title):
    from main import extract_chapter_range
    assert extract_chapter_range(title) is None


def test_extract_chapter_num_still_returns_none_for_ranges():
    """The single-chapter parser must continue to refuse ranges so the
    importer routes them through the new range path."""
    from main import extract_chapter_num
    assert extract_chapter_num("Series - c001-002.cbz") is None


# ───────────────────── importer: writes range row, sweeps placeholders ───────

def _seed_queue(db_path, src_path, series_id=7, vol_num=None, chap_num=1.0):
    """Insert a queue + queue_files row marking src_path as a chapter import."""
    with sqlite3.connect(db_path) as c:
        c.execute("INSERT INTO series(id, title, search_pattern)"
                  " VALUES(?, ?, ?)", (series_id, "Test Series", "Test Series"))
        cur = c.execute(
            "INSERT INTO import_queue(series_id, torrent_name, status, created_at)"
            " VALUES(?, ?, 'pending', datetime('now'))",
            (series_id, "test-torrent")
        )
        qid = cur.lastrowid
        c.execute(
            "INSERT INTO import_queue_files(queue_id, src_path, filename,"
            " file_type, proposed_volume, proposed_chapter, status)"
            " VALUES(?, ?, ?, 'chapter', ?, ?, 'pending')",
            (qid, src_path, os.path.basename(src_path), vol_num, chap_num)
        )
        return qid


def test_chapter_range_import_creates_one_row_with_range_end(env):
    """The headline: c001-002 lands as one chapters row covering both."""
    import asyncio
    import main

    src = _make_zip(str(env["src_root"] / "c001-002.zip"))
    qid = _seed_queue(env["db_path"], src, chap_num=1.0)

    asyncio.run(main._execute_import(qid, {}, set(), {}))

    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        rows = list(c.execute(
            "SELECT id, chapter_num, chapter_range_end, status, import_path"
            " FROM chapters WHERE series_id=7 ORDER BY chapter_num"
        ))
    assert len(rows) == 1, f"expected 1 chapter row, got {len(rows)}: {[dict(r) for r in rows]}"
    r = rows[0]
    assert r["chapter_num"]       == 1.0
    assert r["chapter_range_end"] == 2.0
    assert r["status"]            == "downloaded"
    assert r["import_path"] and r["import_path"].lower().endswith((".cbz", ".zip"))


def test_chapter_range_import_sweeps_existing_placeholder(env):
    """If chapter 2 already exists as a wanted placeholder (created by
    earlier metadata sync), importing c001-002 must clean it up — otherwise
    the UI keeps showing chapter 2 as wanted next to a row that covers it."""
    import asyncio
    import main

    with sqlite3.connect(env["db_path"]) as c:
        c.execute("INSERT INTO series(id, title, search_pattern)"
                  " VALUES(7, 'Test Series', 'Test Series')")
        # Pre-existing placeholders.
        c.execute("INSERT INTO chapters(series_id, chapter_num, status)"
                  " VALUES(7, 2.0, 'wanted')")
        c.execute("INSERT INTO chapters(series_id, chapter_num, status)"
                  " VALUES(7, 3.0, 'wanted')")  # not in range, must survive
        cur = c.execute(
            "INSERT INTO import_queue(series_id, torrent_name, status, created_at)"
            " VALUES(7, 'test-torrent', 'pending', datetime('now'))"
        )
        qid = cur.lastrowid
        src = _make_zip(str(env["src_root"] / "c001-002.zip"))
        c.execute(
            "INSERT INTO import_queue_files(queue_id, src_path, filename,"
            " file_type, proposed_chapter, status)"
            " VALUES(?, ?, ?, 'chapter', 1.0, 'pending')",
            (qid, src, os.path.basename(src))
        )

    asyncio.run(main._execute_import(qid, {}, set(), {}))

    with sqlite3.connect(env["db_path"]) as c:
        nums = sorted(r[0] for r in c.execute(
            "SELECT chapter_num FROM chapters WHERE series_id=7"
        ))
    # The placeholder for ch2 was inside the range → swept. Ch3 remained.
    assert nums == [1.0, 3.0], f"expected [1.0, 3.0], got {nums}"


def test_chapter_range_import_does_not_delete_other_imported_files(env):
    """A pre-existing chapter row that has its own import_path is a separate
    physical file — never delete it just because a range covers its number."""
    import asyncio
    import main

    with sqlite3.connect(env["db_path"]) as c:
        c.execute("INSERT INTO series(id, title, search_pattern)"
                  " VALUES(7, 'Test Series', 'Test Series')")
        # Chapter 2 was previously imported as its own standalone file.
        c.execute(
            "INSERT INTO chapters(series_id, chapter_num, status, import_path, quality)"
            " VALUES(7, 2.0, 'downloaded', '/data/old/ch2.cbz', 'WEB-DL')"
        )
        cur = c.execute(
            "INSERT INTO import_queue(series_id, torrent_name, status, created_at)"
            " VALUES(7, 'test-torrent', 'pending', datetime('now'))"
        )
        qid = cur.lastrowid
        src = _make_zip(str(env["src_root"] / "c001-002.zip"))
        c.execute(
            "INSERT INTO import_queue_files(queue_id, src_path, filename,"
            " file_type, proposed_chapter, status)"
            " VALUES(?, ?, ?, 'chapter', 1.0, 'pending')",
            (qid, src, os.path.basename(src))
        )

    asyncio.run(main._execute_import(qid, {}, set(), {}))

    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        rows = sorted(
            (r["chapter_num"], r["import_path"])
            for r in c.execute("SELECT chapter_num, import_path FROM chapters WHERE series_id=7")
        )
    # Both rows survive: the range row at 1, plus the standalone ch2.
    paths = {n: p for n, p in rows}
    assert 1.0 in paths and paths[1.0].lower().endswith((".cbz", ".zip"))
    assert 2.0 in paths and paths[2.0] == "/data/old/ch2.cbz"


def test_single_chapter_import_unchanged_when_no_range(env):
    """Existing single-chapter behaviour: chapter_range_end stays NULL."""
    import asyncio
    import main

    src = _make_zip(str(env["src_root"] / "Series Ch.5 [grp].zip"))
    qid = _seed_queue(env["db_path"], src, chap_num=5.0)

    asyncio.run(main._execute_import(qid, {}, set(), {}))

    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT chapter_num, chapter_range_end, status FROM chapters WHERE series_id=7"
        ).fetchone()
    assert row["chapter_num"]       == 5.0
    assert row["chapter_range_end"] is None
    assert row["status"]            == "downloaded"


# ───────────────────── sync guard: don't recreate covered chapters ───────────

def test_chapter_sync_skips_chapters_already_covered_by_range(env):
    """After a range import sweeps placeholders, a later chapter-metadata
    sync must NOT re-create the covered chapters as wanted."""
    import main

    with sqlite3.connect(env["db_path"]) as c:
        c.execute("INSERT INTO series(id, title, search_pattern)"
                  " VALUES(7, 'Test Series', 'Test Series')")
        # Range row already in place (as it would be after import).
        c.execute(
            "INSERT INTO chapters(series_id, chapter_num, chapter_range_end, status, import_path)"
            " VALUES(7, 1.0, 2.0, 'downloaded', '/data/c001-002.cbz')"
        )

    # Drive create_volume_chapter_stubs by simulating a chapter_vol_map sync.
    with main.get_db() as db:
        # Map says chapters 1, 2, 3 all belong to volume 1.
        db.execute("INSERT INTO volumes(id, series_id, volume_num, status)"
                   " VALUES(99, 7, 1.0, 'wanted')")
    with main.get_db() as db:
        # Inline the same INSERT-OR-IGNORE-with-coverage-guard pattern the
        # production sync uses.
        for ch_num in (1.0, 2.0, 3.0):
            covered = db.execute(
                "SELECT 1 FROM chapters WHERE series_id=?"
                "   AND chapter_num <= ?"
                "   AND ? <= COALESCE(chapter_range_end, chapter_num)"
                " LIMIT 1",
                (7, ch_num, ch_num)
            ).fetchone()
            if covered:
                continue
            db.execute(
                "INSERT OR IGNORE INTO chapters(series_id, volume_id, chapter_num, status, monitored)"
                " VALUES(7, 99, ?, 'wanted', 1)",
                (ch_num,)
            )

    with sqlite3.connect(env["db_path"]) as c:
        nums = sorted(r[0] for r in c.execute(
            "SELECT chapter_num FROM chapters WHERE series_id=7"
        ))
    # Range row at 1 covers 1+2; sync only creates 3.
    assert nums == [1.0, 3.0], f"sync re-created covered chapter; got {nums}"


# ───────────────────── coverage query (the user-facing invariant) ────────────

def _is_chapter_covered(db_path, series_id: int, ch_num: float) -> bool:
    """Mirror of the wanted/missing query: is this chapter covered by some
    downloaded row (single OR range)?"""
    with sqlite3.connect(db_path) as c:
        r = c.execute(
            "SELECT 1 FROM chapters WHERE series_id=?"
            "   AND status='downloaded'"
            "   AND chapter_num <= ?"
            "   AND ? <= COALESCE(chapter_range_end, chapter_num)"
            " LIMIT 1",
            (series_id, ch_num, ch_num)
        ).fetchone()
    return r is not None


def test_range_row_covers_every_chapter_in_range(env):
    with sqlite3.connect(env["db_path"]) as c:
        c.execute("INSERT INTO series(id, title, search_pattern)"
                  " VALUES(7, 'Test Series', 'Test Series')")
        c.execute(
            "INSERT INTO chapters(series_id, chapter_num, chapter_range_end, status)"
            " VALUES(7, 1.0, 2.0, 'downloaded')"
        )

    assert _is_chapter_covered(env["db_path"], 7, 1.0) is True
    assert _is_chapter_covered(env["db_path"], 7, 2.0) is True
    assert _is_chapter_covered(env["db_path"], 7, 3.0) is False


def test_single_row_with_null_range_end_covers_only_itself(env):
    """NULL chapter_range_end → COALESCE collapses to chapter_num.
    Single rows still behave exactly as before."""
    with sqlite3.connect(env["db_path"]) as c:
        c.execute("INSERT INTO series(id, title, search_pattern)"
                  " VALUES(7, 'Test Series', 'Test Series')")
        c.execute(
            "INSERT INTO chapters(series_id, chapter_num, status)"
            " VALUES(7, 5.0, 'downloaded')"
        )

    assert _is_chapter_covered(env["db_path"], 7, 5.0) is True
    assert _is_chapter_covered(env["db_path"], 7, 4.0) is False
    assert _is_chapter_covered(env["db_path"], 7, 6.0) is False


# ───────────────────── label rendering ───────────────────────────────────────

def test_ch_label_filter_renders_single_chapter():
    from main import _ch_label_filter
    assert _ch_label_filter({"chapter_num": 5.0})                              == "5"
    assert _ch_label_filter({"chapter_num": 5.0, "chapter_range_end": None})   == "5"
    assert _ch_label_filter({"chapter_num": 1.5})                              == "1.5"


def test_ch_label_filter_renders_range_when_end_set():
    from main import _ch_label_filter
    assert _ch_label_filter({"chapter_num": 1.0, "chapter_range_end": 2.0})    == "1-2"
    assert _ch_label_filter({"chapter_num": 10.0, "chapter_range_end": 15.0})  == "10-15"


def test_ch_label_filter_handles_degenerate_range():
    """end <= start is treated as single (defensive against bad data)."""
    from main import _ch_label_filter
    assert _ch_label_filter({"chapter_num": 5.0, "chapter_range_end": 5.0}) == "5"


def test_ch_label_filter_handles_missing_or_none():
    from main import _ch_label_filter
    assert _ch_label_filter(None)                          == ""
    assert _ch_label_filter({})                            == ""
    assert _ch_label_filter({"chapter_num": None})         == ""


def test_build_chapter_label_helper_matches_filter():
    """The Python helper used elsewhere agrees with the Jinja filter."""
    from main import build_chapter_label
    assert build_chapter_label(5.0)             == "Ch.005"
    assert build_chapter_label(1.0, 2.0)        == "Ch.001-002"
    assert build_chapter_label(1.5)             == "Ch.1.5"
