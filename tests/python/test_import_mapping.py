"""Stage 2 — import queue / review / import contract tests.

These tests pin the end-to-end behaviour of the mapping pipeline after
Stage 2 wiring:

  1. prepare_import_queue (_queue_import) populates the new range /
     pack-type / is_special columns from Stage 1 parser output.
  2. The review form round-trips ranges and fractional volume values
     without truncation (audit D11).
  3. _execute_import trusts the explicit columns, falls back only for
     pre-Stage-2 queue rows.

Coverage SQL is NOT changed in Stage 2, so these tests only cover
schema / queue / review / import — not wanted/missing.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

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
def env(tmp_path):
    """Fresh DB + temp src/library dirs, pointed at main.load_config()."""
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-importmap-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()
    main.ensure_api_key()

    src_root = tmp_path / "src"
    lib_root = tmp_path / "library"
    src_root.mkdir(); lib_root.mkdir()

    with sqlite3.connect(db.name) as c:
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('save_path', ?)",
                  (str(lib_root),))
        c.execute(
            "INSERT OR REPLACE INTO root_folders(id, path, label, is_default)"
            " VALUES(1, ?, 'Manga', 1)",
            (str(lib_root),),
        )
    main.load_config()

    try:
        yield {"db_path": db.name, "src_root": src_root, "lib_root": lib_root}
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


def _seed_series(db_path: str, *, series_id: int = 7, title: str = "Test Series",
                 total_volumes: int | None = None) -> None:
    with sqlite3.connect(db_path) as c:
        c.execute(
            "INSERT INTO series(id, title, search_pattern, total_volumes, root_folder_id)"
            " VALUES(?, ?, ?, ?, 1)",
            (series_id, title, title, total_volumes),
        )


def _run_queue_import(db_path: str, *, series_id: int, torrent_name: str,
                      content_path: str, download_id: str = "dlid-test",
                      volume_num: float | None = None) -> int:
    """Invoke main._queue_import under a real get_db transaction and
    return the resulting queue_id."""
    import main
    with main.get_db() as db:
        qid, _ = main._queue_import(
            db, series_id, download_id, torrent_name, None, volume_num, content_path
        )
    return qid


def _make_rar_stub(path: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"Rar!\x1a\x07\x00mangarr-test")
    return path


def _post_review(client, qid: int, data: dict) -> "object":
    """POST the review form with a CSRF handshake so the middleware lets
    us through on non-/api/ routes. Returns the response."""
    # Any GET to a CSRF-protected path establishes the csrftoken cookie.
    client.get("/queue")
    token = client.cookies.get("csrftoken") or ""
    return client.post(
        f"/import/{qid}/process",
        data=data,
        headers={"X-CSRFToken": token},
    )


# ─────────────── 1. schema migration ────────────────────────────────

def test_schema_has_new_import_queue_files_columns(env):
    """init_db must add all Stage 2 columns; defaults where specified."""
    with sqlite3.connect(env["db_path"]) as c:
        cols = {r[1]: (r[2], r[4]) for r in c.execute(
            "PRAGMA table_info(import_queue_files)"
        ).fetchall()}
    for name in (
        "proposed_volume_range_start",
        "proposed_volume_range_end",
        "proposed_chapter_range_end",
        "proposed_pack_type",
        "proposed_is_special",
    ):
        assert name in cols, f"missing column {name}"
    assert cols["proposed_is_special"][1] in ("0", 0), \
        f"proposed_is_special default should be 0, got {cols['proposed_is_special'][1]!r}"


def test_schema_has_volumes_is_special(env):
    with sqlite3.connect(env["db_path"]) as c:
        cols = {r[1]: (r[2], r[4]) for r in c.execute(
            "PRAGMA table_info(volumes)"
        ).fetchall()}
    assert "is_special" in cols
    assert cols["is_special"][1] in ("0", 0)


# ─────────────── 2. queue-creation: chapter range ───────────────────

def test_queue_import_detects_chapter_range(env):
    """A c001-002.cbz release must land as one file row with
    proposed_chapter=1.0 and proposed_chapter_range_end=2.0."""
    _seed_series(env["db_path"])
    src_dir = env["src_root"] / "c001-002-release"
    src_dir.mkdir()
    _make_zip(str(src_dir / "Series - c001-002.cbz"))

    qid = _run_queue_import(
        env["db_path"], series_id=7,
        torrent_name="Series - c001-002",
        content_path=str(src_dir),
    )
    assert qid is not None

    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT proposed_volume, proposed_chapter, proposed_chapter_range_end,"
            " proposed_pack_type, file_type"
            " FROM import_queue_files WHERE queue_id=?", (qid,)
        ).fetchone()
    assert row["file_type"] == "chapter"
    assert row["proposed_volume"] is None
    assert row["proposed_chapter"] == 1.0
    assert row["proposed_chapter_range_end"] == 2.0
    assert row["proposed_pack_type"] == "chapter_range"


def test_queue_import_uses_chapter_format_for_chapter_filename(env):
    """Chapter-only imports must build their destination from chapter_format."""
    import main

    _seed_series(env["db_path"])
    with sqlite3.connect(env["db_path"]) as c:
        c.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES('chapter_format', ?)",
            ("{Series Title} c{Chapter:04d}",),
        )
    main.load_config()

    src_dir = env["src_root"] / "chapter-release"
    src_dir.mkdir()
    _make_zip(str(src_dir / "Series - c001.cbz"))

    qid = _run_queue_import(
        env["db_path"], series_id=7,
        torrent_name="Series - c001",
        content_path=str(src_dir),
    )

    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT filename, dst_path, proposed_chapter, file_type"
            " FROM import_queue_files WHERE queue_id=?", (qid,)
        ).fetchone()
    assert row["file_type"] == "chapter"
    assert row["proposed_chapter"] == 1.0
    assert row["filename"] == "Test Series c0001.cbz"
    assert row["dst_path"].endswith("Test Series c0001.cbz")


def test_queue_import_does_not_apply_volume_template_to_chapter(env):
    """When no chapter_format exists, a volume-only file_format must not
    produce filenames with unresolved {Volume} tokens for chapter imports."""
    import main

    _seed_series(env["db_path"])
    with sqlite3.connect(env["db_path"]) as c:
        c.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES('file_format', ?)",
            ("{Series Title} v{Volume:02d}",),
        )
        c.execute("DELETE FROM settings WHERE key='chapter_format'")
    main.load_config()

    src_dir = env["src_root"] / "chapter-no-format"
    src_dir.mkdir()
    _make_zip(str(src_dir / "Series - c001.cbz"))

    qid = _run_queue_import(
        env["db_path"], series_id=7,
        torrent_name="Series - c001",
        content_path=str(src_dir),
    )

    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT filename, dst_path, proposed_chapter, file_type"
            " FROM import_queue_files WHERE queue_id=?", (qid,)
        ).fetchone()
    assert row["file_type"] == "chapter"
    assert row["proposed_chapter"] == 1.0
    assert row["filename"] == "Series - c001.cbz"
    assert "{Volume" not in row["dst_path"]


def test_import_plan_repairs_legacy_chapter_filename_tokens(env):
    """Failed queue rows created before the fix may already contain a
    persisted volume-template filename. Retry planning must repair those."""
    import main
    from import_plan import _plan_import

    _seed_series(env["db_path"])
    with sqlite3.connect(env["db_path"]) as c:
        c.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES('file_format', ?)",
            ("{Series Title} v{Volume:02d}",),
        )
        c.execute("DELETE FROM settings WHERE key='chapter_format'")
    main.load_config()

    src_dir = env["src_root"] / "legacy-token"
    src_dir.mkdir()
    src_path = _make_zip(str(src_dir / "Series - c001.cbz"))
    with sqlite3.connect(env["db_path"]) as c:
        c.execute(
            "INSERT INTO import_queue(series_id, download_id, torrent_name,"
            " torrent_url, volume_num, src_dir, status)"
            " VALUES(7, 'legacy-dl', 'Series - c001', 'magnet:x', NULL, ?, 'pending')",
            (str(src_dir),),
        )
        qid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute(
            "INSERT INTO import_queue_files(queue_id, filename, src_path, dst_path,"
            " proposed_chapter, file_type, status)"
            " VALUES(?,?,?,?,1.0,'chapter','pending')",
            (
                qid,
                "Test Series v{Volume:02d}.cbz",
                src_path,
                str(env["lib_root"] / "Test Series" / "Test Series v{Volume:02d}.cbz"),
            ),
        )

    with main.get_db() as db:
        plan = _plan_import(db, qid, {}, {}, set(), "copy")

    assert plan is not None
    assert plan.files[0].filename == "Series - c001.cbz"
    assert "{Volume" not in plan.files[0].dst_path
    with sqlite3.connect(env["db_path"]) as c:
        filename = c.execute(
            "SELECT filename FROM import_queue_files WHERE queue_id=?", (qid,)
        ).fetchone()[0]
    assert filename == "Series - c001.cbz"


def test_execute_import_skips_lower_quality_existing_volume(env):
    """A retry must not downgrade an already-downloaded volume."""
    import main

    _seed_series(env["db_path"])
    existing = env["lib_root"] / "Test Series" / "Test Series v01.cbz"
    _make_zip(str(existing))
    src_dir = env["src_root"] / "lower-quality-dup"
    src_dir.mkdir()
    src_path = _make_rar_stub(str(src_dir / "Test Series v01.cbr"))

    with sqlite3.connect(env["db_path"]) as c:
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, quality, import_path)"
            " VALUES(7, 1.0, 'downloaded', 'cbz', ?)",
            (str(existing),),
        )
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, download_id, pack_type)"
            " VALUES(7, NULL, 'grabbed', 'dl-lower', 'volume')",
        )
        c.execute(
            "INSERT INTO import_queue(series_id, download_id, torrent_name,"
            " torrent_url, src_dir, status)"
            " VALUES(7, 'dl-lower', 'Test Series v01', 'magnet:lower', ?, 'pending')",
            (str(src_dir),),
        )
        qid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute(
            "INSERT INTO import_queue_files(queue_id, filename, src_path, dst_path,"
            " proposed_volume, file_type, status)"
            " VALUES(?,?,?,?,1.0,'volume','pending')",
            (
                qid,
                "Test Series v01.cbr",
                src_path,
                str(env["lib_root"] / "Test Series" / "Test Series v01.cbr"),
            ),
        )

    assert asyncio.run(main._guarded_execute_import(qid))
    downgraded = env["lib_root"] / "Test Series" / "Test Series v01.cbr"
    converted = env["lib_root"] / "Test Series" / "Test Series v01.cbz"
    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        vol = c.execute(
            "SELECT status, quality, import_path FROM volumes"
            " WHERE series_id=7 AND volume_num=1.0"
        ).fetchone()
        placeholder = c.execute(
            "SELECT 1 FROM volumes WHERE series_id=7 AND download_id='dl-lower'"
            " AND volume_num IS NULL"
        ).fetchone()
        queue = c.execute("SELECT 1 FROM import_queue WHERE id=?", (qid,)).fetchone()
    assert vol["status"] == "downloaded"
    assert vol["quality"] == "cbz"
    assert vol["import_path"] == str(existing)
    assert not downgraded.exists()
    assert converted.exists()  # the original existing CBZ is still present
    assert placeholder is None
    assert queue is None


def test_execute_import_mixed_pack_skips_duplicate_and_imports_wanted(env):
    """When a pack contains one duplicate and one wanted volume, import only
    the wanted file and remove the transient grabbed pack placeholder."""
    import main

    _seed_series(env["db_path"])
    series_dir = env["lib_root"] / "Test Series"
    existing = series_dir / "Test Series v01.cbz"
    _make_zip(str(existing))
    src_dir = env["src_root"] / "mixed-pack"
    src_dir.mkdir()
    v1_src = _make_rar_stub(str(src_dir / "Test Series v01.cbr"))
    v2_src = _make_rar_stub(str(src_dir / "Test Series v02.cbr"))

    with sqlite3.connect(env["db_path"]) as c:
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, quality, import_path)"
            " VALUES(7, 1.0, 'downloaded', 'cbz', ?)",
            (str(existing),),
        )
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status)"
            " VALUES(7, 2.0, 'wanted')",
        )
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, download_id, pack_type)"
            " VALUES(7, NULL, 'grabbed', 'dl-mixed', 'complete')",
        )
        c.execute(
            "INSERT INTO import_queue(series_id, download_id, torrent_name,"
            " torrent_url, src_dir, status)"
            " VALUES(7, 'dl-mixed', 'Test Series pack', 'magnet:mixed', ?, 'pending')",
            (str(src_dir),),
        )
        qid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        for fname, src, vol in (
            ("Test Series v01.cbr", v1_src, 1.0),
            ("Test Series v02.cbr", v2_src, 2.0),
        ):
            c.execute(
                "INSERT INTO import_queue_files(queue_id, filename, src_path,"
                " dst_path, proposed_volume, file_type, status)"
                " VALUES(?,?,?,?,?,'volume','pending')",
                (
                    qid,
                    fname,
                    src,
                    str(series_dir / fname),
                    vol,
                ),
            )

    assert asyncio.run(main._guarded_execute_import(qid))
    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        rows = {
            r["volume_num"]: dict(r)
            for r in c.execute(
                "SELECT volume_num, status, quality, import_path, download_id"
                " FROM volumes WHERE series_id=7 AND volume_num IS NOT NULL"
            )
        }
        placeholder = c.execute(
            "SELECT 1 FROM volumes WHERE series_id=7 AND download_id='dl-mixed'"
            " AND volume_num IS NULL"
        ).fetchone()
        queue = c.execute("SELECT 1 FROM import_queue WHERE id=?", (qid,)).fetchone()

    assert rows[1.0]["quality"] == "cbz"
    assert rows[1.0]["import_path"] == str(existing)
    assert rows[2.0]["status"] == "downloaded"
    assert rows[2.0]["quality"] == "cbr"
    assert rows[2.0]["download_id"] == "dl-mixed"
    assert os.path.exists(rows[2.0]["import_path"])
    assert not (series_dir / "Test Series v01.cbr").exists()
    assert placeholder is None
    assert queue is None


# ─────────────── 3. review round-trip overrides chapter range ─────────

def test_review_override_chapter_range_round_trip(env):
    """Operator in review changes c001-002 → c005-006; the final
    chapters row carries chapter_num=5, chapter_range_end=6."""
    _seed_series(env["db_path"])
    src_dir = env["src_root"] / "ch-range"
    src_dir.mkdir()
    _make_zip(str(src_dir / "Series - c001-002.cbz"))

    qid = _run_queue_import(
        env["db_path"], series_id=7,
        torrent_name="Series - c001-002",
        content_path=str(src_dir),
    )

    # Submit the review form with an override to 5-6.
    import main
    from fastapi.testclient import TestClient

    with sqlite3.connect(env["db_path"]) as c:
        fid = c.execute(
            "SELECT id FROM import_queue_files WHERE queue_id=?", (qid,)
        ).fetchone()[0]
    with TestClient(main.app, follow_redirects=False) as client:
        r = _post_review(client, qid, {
            f"vol_{fid}":      "",
            f"vol_end_{fid}":  "",
            f"chap_{fid}":     "5",
            f"chap_end_{fid}": "6",
            f"pack_{fid}":     "chapter_range",
            f"spec_{fid}":     "",
        })
        assert r.status_code in (200, 303)

    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        rows = list(c.execute(
            "SELECT chapter_num, chapter_range_end, status FROM chapters"
            " WHERE series_id=7 ORDER BY chapter_num"
        ))
    assert len(rows) == 1, f"expected 1 chapter row, got {[dict(r) for r in rows]}"
    r = rows[0]
    assert r["chapter_num"] == 5.0
    assert r["chapter_range_end"] == 6.0
    assert r["status"] == "downloaded"


# ─────────────── 4. queue-creation: volume range ────────────────────

def test_queue_import_detects_volume_range(env):
    """v01-v03.cbz release must populate proposed_volume_range_start/end
    and NOT set proposed_volume (it's a range, not a single)."""
    _seed_series(env["db_path"], total_volumes=10)
    src_dir = env["src_root"] / "v01-v03-release"
    src_dir.mkdir()
    _make_zip(str(src_dir / "Series - v01-v03.cbz"))

    qid = _run_queue_import(
        env["db_path"], series_id=7,
        torrent_name="Series - v01-v03",
        content_path=str(src_dir),
    )
    assert qid is not None

    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT proposed_volume, proposed_volume_range_start,"
            " proposed_volume_range_end, proposed_pack_type, file_type"
            " FROM import_queue_files WHERE queue_id=?", (qid,)
        ).fetchone()
    assert row["file_type"] == "volume"
    assert row["proposed_volume"] is None
    assert row["proposed_volume_range_start"] == 1.0
    assert row["proposed_volume_range_end"]   == 3.0
    assert row["proposed_pack_type"] == "volume_range"


def test_volume_range_imports_one_range_row(env):
    """End-to-end: v01-v03.cbz → one volumes row with vol_range_start=1,
    vol_range_end=3, pack_type='volume_range'."""
    _seed_series(env["db_path"], total_volumes=10)
    src_dir = env["src_root"] / "v-range"
    src_dir.mkdir()
    src = _make_zip(str(src_dir / "Series - v01-v03.cbz"))

    qid = _run_queue_import(
        env["db_path"], series_id=7,
        torrent_name="Series - v01-v03",
        content_path=str(src_dir),
    )

    import main
    asyncio.run(main._execute_import(qid))

    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        rows = list(c.execute(
            "SELECT volume_num, vol_range_start, vol_range_end, pack_type, status"
            " FROM volumes WHERE series_id=7 ORDER BY id"
        ))
    # One volumes row representing the whole range.
    assert len(rows) == 1, f"expected 1 volumes row, got {[dict(r) for r in rows]}"
    r = rows[0]
    assert r["volume_num"] is None
    assert r["vol_range_start"] == 1.0
    assert r["vol_range_end"]   == 3.0
    assert r["pack_type"] in ("volume", "volume_range")
    assert r["status"] == "downloaded"


# ─────────────── 5. single Vol. 1 regression ────────────────────────

def test_single_vol_1_imports_one_volume_row_no_chapter_row(env):
    """Regression for D1 + D11: Vol. 1.cbz does NOT leak a chapter row."""
    _seed_series(env["db_path"], total_volumes=10)
    src_dir = env["src_root"] / "single-vol"
    src_dir.mkdir()
    _make_zip(str(src_dir / "Series - Vol. 1.cbz"))

    qid = _run_queue_import(
        env["db_path"], series_id=7,
        torrent_name="Series - Vol. 1",
        content_path=str(src_dir),
        volume_num=1.0,
    )

    import main
    asyncio.run(main._execute_import(qid))

    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        vols = list(c.execute(
            "SELECT volume_num, vol_range_start, status FROM volumes"
            " WHERE series_id=7 ORDER BY volume_num"
        ))
        chaps = list(c.execute(
            "SELECT chapter_num FROM chapters WHERE series_id=7"
        ))
    assert len(vols) == 1 and vols[0]["volume_num"] == 1.0
    assert vols[0]["status"] == "downloaded"
    assert vols[0]["vol_range_start"] is None
    assert chaps == [], f"Vol. 1 leaked a chapter row: {[dict(c) for c in chaps]}"


def test_single_volume_release_overrides_opaque_payload_chapter_number(env):
    """Scene payload names like o01nep071.pdf must not override the grabbed volume."""
    import main

    _seed_series(env["db_path"], title="One Piece", total_volumes=120)
    with sqlite3.connect(env["db_path"]) as c:
        c.execute(
            "INSERT OR REPLACE INTO settings(key, value)"
            " VALUES('file_format', '{Series Title} v{Volume:02d}')"
        )
    main.load_config()
    src_dir = env["src_root"] / "opaque-single-volume"
    src_dir.mkdir()
    (src_dir / "o01nep071.pdf").write_bytes(b"%PDF-1.4\n")

    qid = _run_queue_import(
        env["db_path"], series_id=7,
        torrent_name="VIZ Media - One Piece Vol 109 2025 HYBRID MANGA eBook-21A1",
        content_path=str(src_dir),
        volume_num=109.0,
    )

    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT filename, proposed_volume, proposed_chapter, file_type"
            " FROM import_queue_files WHERE queue_id=?",
            (qid,),
        ).fetchone()
    assert row["file_type"] == "volume"
    assert row["proposed_volume"] == 109.0
    assert row["proposed_chapter"] is None
    assert row["filename"] == "One Piece v109.pdf"


def test_queue_import_expands_zip_wrapped_split_rar_payload(env, monkeypatch):
    """Outer ZIP split-RAR parts should queue the unpacked manga payload."""
    import main
    import import_queue
    import import_pipeline

    _seed_series(env["db_path"], title="One Piece", total_volumes=120)
    with sqlite3.connect(env["db_path"]) as c:
        c.execute(
            "INSERT OR REPLACE INTO settings(key, value)"
            " VALUES('file_format', '{Series Title} v{Volume:02d}')"
        )
    main.load_config()
    monkeypatch.setattr(
        import_pipeline,
        "PACK_STAGING_ROOT",
        str(env["src_root"] / "pack-staging"),
    )
    src_dir = env["src_root"] / "zip-split-rar"
    src_dir.mkdir()
    with zipfile.ZipFile(src_dir / "part-a.zip", "w") as zf:
        zf.writestr("scene.rar", b"rar-head")
    with zipfile.ZipFile(src_dir / "part-b.zip", "w") as zf:
        zf.writestr("scene.r00", b"rar-tail")

    def fake_unrar(args, **kwargs):
        out_dir = Path(args[-1])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "o01nep071.pdf").write_bytes(b"%PDF-1.4\n")
        return object()

    monkeypatch.setattr(import_queue.subprocess, "run", fake_unrar)

    qid = _run_queue_import(
        env["db_path"], series_id=7,
        torrent_name="VIZ Media - One Piece Vol 109 2025 HYBRID MANGA eBook-21A1",
        content_path=str(src_dir),
        volume_num=109.0,
    )

    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT src_path, filename, proposed_volume, proposed_chapter, file_type"
            " FROM import_queue_files WHERE queue_id=?",
            (qid,),
        ).fetchone()
    assert row["src_path"].endswith("o01nep071.pdf")
    assert row["filename"] == "One Piece v109.pdf"
    assert row["file_type"] == "volume"
    assert row["proposed_volume"] == 109.0
    assert row["proposed_chapter"] is None


# ─────────────── 6. complete pack ───────────────────────────────────

def test_complete_pack_persists_pack_type(env):
    """Release title 'v01-10 Complete' with total_volumes=10 classifies
    as complete and the proposed_pack_type column carries 'complete'."""
    _seed_series(env["db_path"], total_volumes=10)
    src_dir = env["src_root"] / "complete"
    src_dir.mkdir()
    _make_zip(str(src_dir / "Series - v01-10 Complete.cbz"))

    qid = _run_queue_import(
        env["db_path"], series_id=7,
        torrent_name="Series - v01-10 Complete",
        content_path=str(src_dir),
    )

    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT proposed_pack_type FROM import_queue_files WHERE queue_id=?",
            (qid,)
        ).fetchone()
    assert row["proposed_pack_type"] == "complete"


# ─────────────── 7. special / side-story persistence ───────────────

def test_special_release_flags_is_special(env):
    """Gaiden c001-002 release — proposed_is_special=1 at queue time;
    the resulting chapters row belongs to a volume row carrying
    is_special=1 if a parent volume exists, OR the chapters path simply
    preserves the flag through the queue row for Stage 3 coverage."""
    _seed_series(env["db_path"])
    src_dir = env["src_root"] / "gaiden"
    src_dir.mkdir()
    _make_zip(str(src_dir / "Series - Gaiden c001-002.cbz"))

    qid = _run_queue_import(
        env["db_path"], series_id=7,
        torrent_name="Tomioka Giyuu Gaiden c001-002",
        content_path=str(src_dir),
    )

    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT proposed_is_special FROM import_queue_files WHERE queue_id=?",
            (qid,)
        ).fetchone()
    assert row["proposed_is_special"] == 1


# ─────────────── 8. fractional volume round-trip (D11) ─────────────

def test_fractional_volume_survives_review_round_trip(env):
    """3a → proposed_volume=3.01 at queue time; operator review keeps
    3.01 on submit; final volumes row has volume_num=3.01 (not 3)."""
    _seed_series(env["db_path"], total_volumes=10)
    src_dir = env["src_root"] / "frac"
    src_dir.mkdir()
    _make_zip(str(src_dir / "Series - v3a.cbz"))

    qid = _run_queue_import(
        env["db_path"], series_id=7,
        torrent_name="Series - v3a",
        content_path=str(src_dir),
        volume_num=3.01,
    )

    with sqlite3.connect(env["db_path"]) as c:
        fid = c.execute(
            "SELECT id FROM import_queue_files WHERE queue_id=?", (qid,)
        ).fetchone()[0]
        proposed = c.execute(
            "SELECT proposed_volume FROM import_queue_files WHERE id=?", (fid,)
        ).fetchone()[0]
    assert proposed == pytest.approx(3.01)

    # Submit the review with the same "3a" value.
    import main
    from fastapi.testclient import TestClient
    with TestClient(main.app, follow_redirects=False) as client:
        r = _post_review(client, qid, {f"vol_{fid}": "3a", f"pack_{fid}": "volume"})
        assert r.status_code in (200, 303)

    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        rows = list(c.execute(
            "SELECT volume_num, status FROM volumes WHERE series_id=7"
        ))
    assert len(rows) == 1
    assert rows[0]["volume_num"] == pytest.approx(3.01)


# ─────────────── 9. skip still skips ───────────────────────────────

def test_skip_still_skips(env):
    """Regression: skip_{id} on the review form keeps the file out of
    both volumes and chapters tables."""
    _seed_series(env["db_path"], total_volumes=10)
    src_dir = env["src_root"] / "skip"
    src_dir.mkdir()
    _make_zip(str(src_dir / "Series - Vol. 1.cbz"))

    qid = _run_queue_import(
        env["db_path"], series_id=7,
        torrent_name="Series - Vol. 1",
        content_path=str(src_dir),
        volume_num=1.0,
    )
    with sqlite3.connect(env["db_path"]) as c:
        fid = c.execute(
            "SELECT id FROM import_queue_files WHERE queue_id=?", (qid,)
        ).fetchone()[0]

    import main
    from fastapi.testclient import TestClient
    with TestClient(main.app, follow_redirects=False) as client:
        _post_review(client, qid, {f"skip_{fid}": "1", f"vol_{fid}": "1"})

    # When the only file in a queue is skipped, _execute_import treats
    # the queue as fully handled and cleans up the queue + files rows.
    # The important assertion is that no downloaded volume/chapter was
    # created — the file was never actually imported.
    with sqlite3.connect(env["db_path"]) as c:
        vol_rows = list(c.execute(
            "SELECT id FROM volumes WHERE series_id=7 AND status='downloaded'"
        ))
        chap_rows = list(c.execute(
            "SELECT id FROM chapters WHERE series_id=7 AND status='downloaded'"
        ))
    assert vol_rows == [], "skipped file must not create a downloaded volume row"
    assert chap_rows == [], "skipped file must not create a downloaded chapter row"


# ─────────────── 10. conflict → needs_review ───────────────────────

def test_conflicting_vol_and_chap_marks_needs_review(env):
    """Submitting both a volume AND a chapter number on the same file,
    with pack_type left on 'auto', must mark the file needs_review
    rather than silently picking one shape."""
    _seed_series(env["db_path"], total_volumes=10)
    src_dir = env["src_root"] / "conflict"
    src_dir.mkdir()
    _make_zip(str(src_dir / "Series - unknown.cbz"))

    qid = _run_queue_import(
        env["db_path"], series_id=7,
        torrent_name="Series - unknown",
        content_path=str(src_dir),
    )
    with sqlite3.connect(env["db_path"]) as c:
        fid = c.execute(
            "SELECT id FROM import_queue_files WHERE queue_id=?", (qid,)
        ).fetchone()[0]

    import main
    from fastapi.testclient import TestClient
    with TestClient(main.app, follow_redirects=False) as client:
        _post_review(client, qid, {f"vol_{fid}": "1", f"chap_{fid}": "5", f"pack_{fid}": ""})

    with sqlite3.connect(env["db_path"]) as c:
        status = c.execute(
            "SELECT status FROM import_queue_files WHERE id=?", (fid,)
        ).fetchone()[0]
        vols = list(c.execute(
            "SELECT volume_num FROM volumes WHERE series_id=7 AND status='downloaded'"
        ))
        chs = list(c.execute(
            "SELECT chapter_num FROM chapters WHERE series_id=7"
        ))
    assert status == "needs_review", (
        f"expected needs_review for conflict, got {status}"
    )
    assert vols == [] and chs == [], (
        "conflict row must not import as either volume or chapter"
    )


# ─────────────── 11. legacy queue rows still work ─────────────────

def test_legacy_queue_row_without_new_columns_still_imports(env):
    """Simulate a queue row written before the Stage 2 migration — it
    has proposed_volume set but all new columns NULL. _execute_import
    must still process it via the legacy fallback path."""
    _seed_series(env["db_path"], total_volumes=10)
    src_dir = env["src_root"] / "legacy"
    src_dir.mkdir()
    src = _make_zip(str(src_dir / "legacy-vol1.cbz"))

    with sqlite3.connect(env["db_path"]) as c:
        cur = c.execute(
            "INSERT INTO import_queue(series_id, torrent_name, src_dir, status, created_at,"
            " download_id)"
            " VALUES(7, 'legacy', ?, 'pending', datetime('now'), 'legacy-dlid')",
            (str(src_dir),)
        )
        qid = cur.lastrowid
        c.execute(
            "INSERT INTO import_queue_files(queue_id, filename, src_path, dst_path,"
            " proposed_volume, file_type, status)"
            " VALUES(?, ?, ?, ?, 1.0, 'volume', 'pending')",
            (qid, "legacy-vol1.cbz", src,
             str(env["lib_root"] / "Test Series" / "legacy-vol1.cbz"))
        )

    import main
    asyncio.run(main._execute_import(qid))

    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        vols = list(c.execute(
            "SELECT volume_num, status FROM volumes WHERE series_id=7"
        ))
    assert len(vols) == 1
    assert vols[0]["volume_num"] == 1.0
    assert vols[0]["status"] == "downloaded"


# ─────────────── 12. pack completion Row access ─────────────────────

def test_mark_downloaded_volume_pack_handles_sqlite_row(env, monkeypatch):
    """_mark_downloaded must use sqlite3.Row bracket access for pack rows.

    sqlite3.Row has no .get(); this pins the volume-pack path that reads
    vol_range_start/end from the grabbed pack row.
    """
    _seed_series(env["db_path"])
    import import_download

    def _close_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(import_download.asyncio, "create_task", _close_task)

    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, source_url,"
            " download_id, pack_type, vol_range_start, vol_range_end)"
            " VALUES(7, NULL, 'grabbed', 'http://example/pack',"
            " 'dl-pack', 'volume', 1.0, 3.0)"
        )
        for vol in (1.0, 2.0, 3.0):
            c.execute(
                "INSERT INTO volumes(series_id, volume_num, status)"
                " VALUES(7, ?, 'grabbed')",
                (vol,),
            )

        assert import_download._mark_downloaded(
            c, 7, None, "http://example/pack"
        )

        rows = list(
            c.execute(
                "SELECT volume_num, status FROM volumes"
                " WHERE series_id=7 AND volume_num IS NOT NULL"
                " ORDER BY volume_num"
            )
        )

    assert [r["volume_num"] for r in rows] == [1.0, 2.0, 3.0]
    assert {r["status"] for r in rows} == {"downloaded"}


def test_mark_downloaded_chapter_pack_marks_placeholder_downloaded(env, monkeypatch):
    """Chapter-pack imports create chapter rows, but the pack placeholder
    must not remain grabbed after import completes."""
    _seed_series(env["db_path"])
    import import_download

    def _close_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(import_download.asyncio, "create_task", _close_task)

    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        c.execute(
            "INSERT INTO seen(torrent_url, torrent_name, series_id, protocol, client,"
            " download_id)"
            " VALUES('http://example/chapter-pack', 'Series c001', 7, 'torrent',"
            " 'qbittorrent', 'dl-chapter')"
        )
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, source_url,"
            " download_id, pack_type, torrent_name)"
            " VALUES(7, NULL, 'grabbed', 'http://example/chapter-pack',"
            " 'dl-chapter', 'chapter', 'Series c001')"
        )

        assert import_download._mark_downloaded(
            c, 7, None, "http://example/chapter-pack"
        )
        row = c.execute(
            "SELECT status, torrent_name, client FROM volumes"
            " WHERE series_id=7 AND download_id='dl-chapter'"
        ).fetchone()

    assert row["status"] == "downloaded"
    assert row["torrent_name"] == "Series c001"
    assert row["client"] == "qbittorrent"


# ─────────────── 13. review UI exposes new fields ─────────────────

def test_review_template_renders_range_and_pack_inputs(env):
    """Queue table partial should render the new range inputs + pack
    type select + special checkbox. Checks the rendered HTML so a future
    template refactor can't silently drop them."""
    _seed_series(env["db_path"])
    src_dir = env["src_root"] / "uirender"
    src_dir.mkdir()
    _make_zip(str(src_dir / "Series - c001-002.cbz"))

    qid = _run_queue_import(
        env["db_path"], series_id=7,
        torrent_name="Series - c001-002",
        content_path=str(src_dir),
    )
    # Mark the row as needing review so the modal renders.
    with sqlite3.connect(env["db_path"]) as c:
        c.execute(
            "UPDATE import_queue_files SET status='needs_review' WHERE queue_id=?",
            (qid,)
        )
        # Create a grabbed volume placeholder so the queue_table row is built.
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, download_id,"
            " torrent_name, grabbed_at, client)"
            " VALUES(7, 1.0, 'grabbed', 'dlid-test', 'Series - c001-002',"
            " datetime('now'), 'qbittorrent')"
        )

    import main
    from fastapi.testclient import TestClient
    with TestClient(main.app, follow_redirects=False) as client:
        r = client.get("/queue")
    assert r.status_code == 200
    html = r.text
    # Range inputs
    assert 'name="vol_end_' in html
    assert 'name="chap_end_' in html
    # Pack type select
    assert 'name="pack_' in html
    assert '>vol range<' in html or 'value="volume_range"' in html
    # Special checkbox
    assert 'name="spec_' in html
    # Volume input should NOT use step="1" (would truncate 3a/3.5)
    assert 'name="vol_' in html
    # If the vol input has step="1" we regressed D11. The new form uses
    # type="text" + pattern, so no step attribute should pair with vol_.
    import re as _re
    bad = _re.search(r'name="vol_\d+"[^>]*step="1"', html)
    assert bad is None, f"vol input still uses step='1' (truncates fractions): {bad.group(0)!r}"
