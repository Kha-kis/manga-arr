"""Tests for M2: partial-import atomicity.

Two layers:
  1. _ImportStaging helper primitives (hardlink/copy/move, commit/rollback,
     CBR→CBZ rename, staging-dir hygiene)
  2. _execute_import integration — exercises the SAVEPOINT + staging
     commit/rollback decision end-to-end against a real sqlite DB with
     real source/destination files.
"""
import asyncio
import hashlib
import os
import sqlite3
import tempfile

import pytest


# ───────────────────── fixtures ─────────────────────

@pytest.fixture
def dst_dir(tmp_path):
    """Per-test destination root (simulates /manga/<series>)."""
    d = tmp_path / "Series"
    d.mkdir()
    return str(d)


def _make_src(tmp_path, name: str, body: bytes = b"CBZ-PAYLOAD") -> str:
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    p = src / name
    p.write_bytes(body)
    return str(p)


def _digest(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# ───────────────────── hardlink mode ─────────────────────

def test_stage_and_commit_hardlink(tmp_path, dst_dir):
    """hardlink mode: commit_all places hardlinks at final paths and
    leaves source untouched (same inode, still at original location)."""
    import main

    s1 = _make_src(tmp_path, "v01.cbz", b"A")
    s2 = _make_src(tmp_path, "v02.cbz", b"B")
    staging = main._ImportStaging(dst_dir, queue_id=1, import_mode="hardlink")
    f1 = os.path.join(dst_dir, "v01.cbz")
    f2 = os.path.join(dst_dir, "v02.cbz")

    staging.stage(s1, f1)
    staging.stage(s2, f2)
    staging.commit_all()

    # Final paths exist.
    assert os.path.isfile(f1) and os.path.isfile(f2)
    # Sources still exist (hardlink preserves them).
    assert os.path.isfile(s1) and os.path.isfile(s2)
    # Same inode => hardlink, not copy.
    assert os.stat(f1).st_ino == os.stat(s1).st_ino
    assert os.stat(f2).st_ino == os.stat(s2).st_ino
    # Staging dir cleaned up.
    assert not os.path.isdir(staging.staging_dir)


def test_rollback_hardlink_leaves_source_intact(tmp_path, dst_dir):
    """If we stage two files then rollback, the destination must be empty
    of both — and the hardlinked source files must still exist unchanged."""
    import main

    s1 = _make_src(tmp_path, "v01.cbz", b"A")
    s2 = _make_src(tmp_path, "v02.cbz", b"B")
    f1 = os.path.join(dst_dir, "v01.cbz")
    f2 = os.path.join(dst_dir, "v02.cbz")

    staging = main._ImportStaging(dst_dir, queue_id=2, import_mode="hardlink")
    staging.stage(s1, f1)
    staging.stage(s2, f2)
    staging.rollback()

    assert not os.path.exists(f1)
    assert not os.path.exists(f2)
    assert os.path.isfile(s1) and os.path.isfile(s2)
    assert not os.path.isdir(staging.staging_dir)


# ───────────────────── copy mode ─────────────────────

def test_stage_and_commit_copy(tmp_path, dst_dir):
    """copy mode: final file exists with identical content, source intact."""
    import main

    s1 = _make_src(tmp_path, "v01.cbz", b"HELLO")
    staging = main._ImportStaging(dst_dir, queue_id=3, import_mode="copy")
    f1 = os.path.join(dst_dir, "v01.cbz")
    staging.stage(s1, f1)
    staging.commit_all()

    assert _digest(f1) == _digest(s1)
    assert os.path.isfile(s1)
    # copy mode must NOT share inode with source
    assert os.stat(f1).st_ino != os.stat(s1).st_ino


def test_rollback_copy_does_not_leak_partial_files(tmp_path, dst_dir):
    import main

    s1 = _make_src(tmp_path, "v01.cbz", b"A" * 1024)
    staging = main._ImportStaging(dst_dir, queue_id=4, import_mode="copy")
    f1 = os.path.join(dst_dir, "v01.cbz")
    staging.stage(s1, f1)
    staging.rollback()

    assert not os.path.exists(f1)
    assert os.path.isfile(s1)  # source intact
    # dst_dir must be completely free of our staging
    assert os.listdir(dst_dir) == []


# ───────────────────── move mode ─────────────────────

def test_stage_and_commit_move_deletes_source(tmp_path, dst_dir):
    """move mode: source is deleted AFTER commit_all. Before commit, source
    must still exist (that's what makes rollback safe)."""
    import main

    s1 = _make_src(tmp_path, "v01.cbz", b"MOVE-ME")
    staging = main._ImportStaging(dst_dir, queue_id=5, import_mode="move")
    f1 = os.path.join(dst_dir, "v01.cbz")

    staging.stage(s1, f1)
    # Crucial: source MUST still exist after staging. This is the whole
    # point of copy-to-staging for move mode.
    assert os.path.isfile(s1), \
        "source was deleted during stage(); rollback would lose data"

    staging.commit_all()
    assert os.path.isfile(f1)
    assert not os.path.exists(s1), "source must be removed after commit_all"


def test_rollback_move_preserves_source(tmp_path, dst_dir):
    """The key test for M2: a mid-batch failure with move mode MUST leave
    source files untouched so the user can retry."""
    import main

    s1 = _make_src(tmp_path, "v01.cbz", b"A")
    s2 = _make_src(tmp_path, "v02.cbz", b"B")
    f1 = os.path.join(dst_dir, "v01.cbz")
    f2 = os.path.join(dst_dir, "v02.cbz")

    staging = main._ImportStaging(dst_dir, queue_id=6, import_mode="move")
    staging.stage(s1, f1)
    staging.stage(s2, f2)
    # Batch would fail now — rollback.
    staging.rollback()

    assert not os.path.exists(f1) and not os.path.exists(f2)
    # Both source files must survive
    assert os.path.isfile(s1) and os.path.isfile(s2)
    assert os.listdir(dst_dir) == []


# ───────────────────── in-staging rename (CBR→CBZ) ─────────────────────

def test_rename_updates_final_path(tmp_path, dst_dir):
    """_maybe_convert_to_cbz can rename a staged file (.cbr → .cbz). The
    helper's rename() must update tracking so commit_all uses the new
    basename as the final destination."""
    import main

    s1 = _make_src(tmp_path, "vol1.cbr", b"RARSIG")
    staging = main._ImportStaging(dst_dir, queue_id=7, import_mode="copy")
    f1_cbr = os.path.join(dst_dir, "vol1.cbr")
    stage_cbr = staging.stage(s1, f1_cbr)

    # Simulate CBR→CBZ rewriting the staged file
    stage_cbz = stage_cbr[:-4] + ".cbz"
    os.rename(stage_cbr, stage_cbz)
    new_final = staging.rename(stage_cbr, stage_cbz)
    assert new_final.endswith(".cbz")

    staging.commit_all()
    assert os.path.isfile(os.path.join(dst_dir, "vol1.cbz"))
    assert not os.path.exists(os.path.join(dst_dir, "vol1.cbr"))


def test_rename_on_unknown_path_raises(dst_dir):
    import main
    staging = main._ImportStaging(dst_dir, queue_id=8, import_mode="copy")
    with pytest.raises(ValueError):
        staging.rename("/does/not/exist", "/also/not")
    staging.rollback()


# ───────────────────── batch semantics ─────────────────────

def test_mid_batch_failure_leaves_destination_clean(tmp_path, dst_dir):
    """Simulates the spec's headline scenario: file 3 of 5 fails during
    staging. The other 4 must not appear at final destination. All 5
    source files must survive."""
    import main

    srcs = [_make_src(tmp_path, f"v{i:02d}.cbz", f"#{i}".encode()) for i in range(1, 6)]
    finals = [os.path.join(dst_dir, f"v{i:02d}.cbz") for i in range(1, 6)]

    staging = main._ImportStaging(dst_dir, queue_id=9, import_mode="move")
    # Stage files 1, 2 successfully
    staging.stage(srcs[0], finals[0])
    staging.stage(srcs[1], finals[1])
    # File 3: point at a non-existent source so stage() raises
    with pytest.raises(FileNotFoundError):
        staging.stage("/nonexistent/file3.cbz", finals[2])
    # We haven't staged 4 or 5 because of the early failure
    staging.rollback()

    # None of the 5 final paths exist
    for f in finals:
        assert not os.path.exists(f), f"{f} leaked to dst"
    # All 5 sources still exist
    for s in srcs:
        assert os.path.isfile(s), f"source {s} lost during rollback"
    # Staging dir cleaned up
    assert not os.path.isdir(staging.staging_dir)
    # dst_dir itself is empty (no leftover staging, no leftover files)
    assert os.listdir(dst_dir) == []


def test_single_file_happy_path(tmp_path, dst_dir):
    """Baseline: a one-file batch still works end-to-end — stage, commit,
    final path exists with correct content, staging dir removed."""
    import main

    s1 = _make_src(tmp_path, "Vol 01.cbz", b"one-file-payload")
    staging = main._ImportStaging(dst_dir, queue_id=10, import_mode="copy")
    f1 = os.path.join(dst_dir, "Vol 01.cbz")

    staging.stage(s1, f1)
    staging.commit_all()

    assert os.path.isfile(f1)
    assert os.path.isfile(s1)  # copy mode
    with open(f1, "rb") as fh:
        assert fh.read() == b"one-file-payload"
    assert not os.path.isdir(staging.staging_dir)


def test_staging_dir_is_under_dst_dir(tmp_path, dst_dir):
    """Staging must live UNDER dst_dir so os.replace into dst_dir is
    atomic (same filesystem, guaranteed). Also: the staging basename
    starts with '.' so it's hidden from library scanners."""
    import main

    staging = main._ImportStaging(dst_dir, queue_id=11, import_mode="copy")
    try:
        parent = os.path.dirname(staging.staging_dir)
        assert os.path.realpath(parent) == os.path.realpath(dst_dir)
        assert os.path.basename(staging.staging_dir).startswith(".mangarr-staging-")
    finally:
        staging.rollback()


def test_staging_cleaned_up_on_both_commit_and_rollback(tmp_path, dst_dir):
    import main

    # Success path
    s1 = _make_src(tmp_path, "a.cbz", b"A")
    ok_staging = main._ImportStaging(dst_dir, queue_id=12, import_mode="copy")
    ok_staging.stage(s1, os.path.join(dst_dir, "a.cbz"))
    ok_staging.commit_all()
    assert not os.path.isdir(ok_staging.staging_dir)

    # Failure path
    s2 = _make_src(tmp_path, "b.cbz", b"B")
    bad_staging = main._ImportStaging(dst_dir, queue_id=13, import_mode="copy")
    bad_staging.stage(s2, os.path.join(dst_dir, "b.cbz"))
    bad_staging.rollback()
    assert not os.path.isdir(bad_staging.staging_dir)

    # dst_dir should have only the successful commit artifact.
    remaining = sorted(os.listdir(dst_dir))
    assert remaining == ["a.cbz"], f"unexpected leftovers: {remaining}"


# ═══════════════════════════════════════════════════════════════════════
# Integration: drive _execute_import against a real sqlite DB + real files
# ═══════════════════════════════════════════════════════════════════════

def _run(coro):
    """Run a coroutine in a fresh event loop; restore a default afterwards
    so subsequent tests that use asyncio.get_event_loop() still work."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


@pytest.fixture
def exec_env(tmp_path, monkeypatch):
    """Set up a fresh DB + library root + source dir and yield everything
    _execute_import needs to run against a real sqlite3 file."""
    import main
    import shared

    db_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_tmp.close()
    os.unlink(db_tmp.name)
    monkeypatch.setattr(main, "DB_PATH", db_tmp.name)
    monkeypatch.setattr(shared, "DB_PATH", db_tmp.name)
    main.init_db()
    main.load_config()

    # Stub fire-and-forget side-effects so the integration test stays
    # hermetic and fast. None of these are what we're testing here.
    async def _noop_async(*a, **kw):
        return None
    monkeypatch.setattr(main, "notify_discord", _noop_async)
    monkeypatch.setattr(main, "trigger_komga_scan", _noop_async)
    monkeypatch.setattr(main, "broadcast_queue_event", _noop_async)
    # log_event is NOT stubbed any more: after the log_event write-lock
    # fix, in-transaction call sites thread db=db through so log_event
    # no longer opens a second contending sqlite connection. Integration
    # tests now run in well under a second each, not ~15s.

    library_root = tmp_path / "library"
    library_root.mkdir()
    src_root = tmp_path / "downloads"
    src_root.mkdir()

    # Library destination now resolves through a root folder (PR C
    # removed the save_path fallback). Seed a default folder pointing
    # at our tmp library_root so the import pipeline has somewhere to
    # place files.
    with sqlite3.connect(db_tmp.name) as c:
        c.execute(
            "INSERT OR REPLACE INTO root_folders(id, path, label, is_default)"
            " VALUES(1, ?, 'Manga', 1)",
            (str(library_root),)
        )

    # save_path is kept as a belt-and-braces default for legacy paths
    # (e.g. first-run bootstrap); with the root folder in place the
    # import pipeline never reads it.
    monkeypatch.setitem(main.CONFIG, "save_path", str(library_root))
    import shared as _s
    monkeypatch.setitem(_s.CONFIG, "save_path", str(library_root))

    yield {
        "db_path": db_tmp.name,
        "library_root": str(library_root),
        "src_root": str(src_root),
        "series_title": "Test Series",
        "dst_dir": str(library_root / "Test Series"),  # main.sanitize_filename("Test Series")
    }

    for ext in ("", "-wal", "-shm"):
        p = db_tmp.name + ext
        if os.path.exists(p):
            os.unlink(p)


def _seed_series(db_path, title="Test Series"):
    """Seed a series row that references root_folder_id=1 (set up by the
    exec_env fixture). PR B made root_folder_id non-optional at
    creation; PR C removed the save_path fallback downstream, so tests
    need a valid folder to pass."""
    with sqlite3.connect(db_path) as c:
        c.execute(
            "INSERT INTO series(title, search_pattern, monitored, root_folder_id)"
            " VALUES(?,?,1,1)",
            (title, title.lower().replace(" ", "-")),
        )
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]


def _seed_queue(db_path, series_id, file_count, src_root, import_mode=None):
    """Create a pending queue row plus `file_count` volume-file rows.
    Creates real source files in src_root so the copy will actually work.
    Returns (queue_id, [src_paths], [filenames])."""
    src_paths = []
    filenames = []
    for i in range(1, file_count + 1):
        name = f"Test Series v{i:02d}.cbz"
        p = os.path.join(src_root, name)
        with open(p, "wb") as f:
            f.write(f"content-{i}".encode() * 100)   # distinct content per file
        src_paths.append(p)
        filenames.append(name)

    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        c.execute(
            "INSERT INTO import_queue(series_id, download_id, torrent_name,"
            " torrent_url, volume_num, src_dir, status) VALUES(?,?,?,?,?,?,'pending')",
            (series_id, "dl-test", "Test Series batch",
             "magnet:test", 1.0, src_root),
        )
        qid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        for i, (src, fname) in enumerate(zip(src_paths, filenames), start=1):
            dst_placeholder = os.path.join(
                src_root, fname,  # real dst_path is computed by _execute_import
            )
            c.execute(
                "INSERT INTO import_queue_files(queue_id, filename, src_path,"
                " dst_path, proposed_volume, file_type, status)"
                " VALUES(?,?,?,?,?,'volume','pending')",
                (qid, fname, src, dst_placeholder, float(i)),
            )
        c.commit()
    return qid, src_paths, filenames


def _file_rows(db_path, queue_id):
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        return c.execute(
            "SELECT id, filename, status, dst_path FROM import_queue_files"
            " WHERE queue_id=? ORDER BY id", (queue_id,)
        ).fetchall()


def _queue_row(db_path, queue_id):
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        return c.execute(
            "SELECT * FROM import_queue WHERE id=?", (queue_id,)
        ).fetchone()


# ───────────── Test 1: mid-batch failure rolls back filesystem + DB ─────────────

def test_execute_import_rolls_back_entire_batch_on_staging_failure(exec_env, monkeypatch):
    """Multi-file queue, copy mode, file 3 fails during staging.

    Expected:
      - no final files at dst_dir (files 1-2 rolled back, files 4-5 never started)
      - source files all intact (copy mode + rollback)
      - staging dir is removed
      - the one failing file is marked 'failed'; other files revert to 'pending'
      - queue ends 'failed' (imported_count == 0, any_error True)
    """
    import main
    monkeypatch.setitem(main.CONFIG, "import_mode", "copy")
    monkeypatch.setitem(main.CONFIG, "save_path", exec_env["library_root"])

    sid = _seed_series(exec_env["db_path"])
    qid, srcs, fnames = _seed_queue(
        exec_env["db_path"], sid, file_count=5,
        src_root=exec_env["src_root"],
    )

    # Wrap _ImportStaging.stage so the 3rd call raises. This simulates a
    # mid-staging filesystem failure (disk full, permission denied, ...).
    real_stage = main._ImportStaging.stage
    call_count = {"n": 0}
    def _flaky_stage(self, src, final_path):
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise OSError("simulated stage failure on file 3")
        return real_stage(self, src, final_path)
    monkeypatch.setattr(main._ImportStaging, "stage", _flaky_stage)

    result = _run(main._execute_import(qid))

    # ── filesystem ──
    assert result is not True, "execute_import should not report full success"
    for name in fnames:
        assert not os.path.exists(os.path.join(exec_env["dst_dir"], name)), \
            f"{name} leaked to final dst"
    for src in srcs:
        assert os.path.isfile(src), f"source {src} lost after rollback"
    # No .mangarr-staging-* directory left behind
    if os.path.isdir(exec_env["dst_dir"]):
        remaining = os.listdir(exec_env["dst_dir"])
        assert all(not n.startswith(".mangarr-staging-") for n in remaining), \
            f"staging dir left behind: {remaining}"

    # ── DB ──
    rows = _file_rows(exec_env["db_path"], qid)
    assert len(rows) == 5
    # File 3 (the one that failed stage()) is marked 'failed'
    failed = [r for r in rows if r["status"] == "failed"]
    assert len(failed) == 1, \
        f"expected exactly 1 failed row, got: {[(r['filename'], r['status']) for r in rows]}"
    assert failed[0]["filename"] == fnames[2]
    # Files 1,2 were staged/DB-written but rolled back — they're pending again.
    # Files 4,5 never reached the stage() call in the first place and stay pending.
    pending = [r for r in rows if r["status"] == "pending"]
    assert len(pending) == 4

    # Queue row ends 'failed'
    queue = _queue_row(exec_env["db_path"], qid)
    assert queue["status"] == "failed", f"queue ended in {queue['status']!r}"


# ───────────── Test 2: successful multi-file batch ─────────────

def test_execute_import_happy_path_multi_file(exec_env, monkeypatch):
    """All 3 files succeed. Final files exist with correct content;
    sources intact (copy mode); staging dir cleaned; queue row and
    queue_files rows deleted (current behaviour for status='imported')."""
    import main
    monkeypatch.setitem(main.CONFIG, "import_mode", "copy")
    monkeypatch.setitem(main.CONFIG, "save_path", exec_env["library_root"])

    sid = _seed_series(exec_env["db_path"])
    qid, srcs, fnames = _seed_queue(
        exec_env["db_path"], sid, file_count=3,
        src_root=exec_env["src_root"],
    )

    result = _run(main._execute_import(qid))
    # (_execute_import's return value is True only on full clean import.)
    assert result is True, "multi-file happy path should return True"

    # Every final file exists with the right content
    for src, fname in zip(srcs, fnames):
        final = os.path.join(exec_env["dst_dir"], fname)
        assert os.path.isfile(final), f"missing final: {final}"
        assert _digest(final) == _digest(src)
        assert os.path.isfile(src), "copy mode must preserve source"

    # Staging dir cleaned up
    remaining = os.listdir(exec_env["dst_dir"])
    assert all(not n.startswith(".mangarr-staging-") for n in remaining), \
        f"staging dir left behind: {remaining}"

    # On status='imported', current behaviour deletes queue + queue_files
    # (main.py does this explicitly). Verify both rows are gone.
    queue = _queue_row(exec_env["db_path"], qid)
    assert queue is None, "successful import should delete import_queue row"
    assert _file_rows(exec_env["db_path"], qid) == [], \
        "successful import should delete import_queue_files rows"

    # Volumes were written: 3 'downloaded' rows for this series
    with sqlite3.connect(exec_env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        vols = c.execute(
            "SELECT volume_num, status, import_path FROM volumes"
            " WHERE series_id=? ORDER BY volume_num", (sid,)
        ).fetchall()
    assert len(vols) == 3
    for i, v in enumerate(vols, start=1):
        assert v["status"] == "downloaded"
        assert v["import_path"].endswith(f"v{i:02d}.cbz")


# ───────────── Test 3: move mode — failed batch preserves sources ─────────────

def test_execute_import_move_mode_failed_batch_preserves_sources(exec_env, monkeypatch):
    """Move mode + mid-batch failure: the critical M2 guarantee.
    Sources MUST survive the rollback — otherwise the user loses data
    with no way to retry. No final files should leak."""
    import main
    monkeypatch.setitem(main.CONFIG, "import_mode", "move")
    monkeypatch.setitem(main.CONFIG, "save_path", exec_env["library_root"])

    sid = _seed_series(exec_env["db_path"])
    qid, srcs, fnames = _seed_queue(
        exec_env["db_path"], sid, file_count=4,
        src_root=exec_env["src_root"],
    )

    real_stage = main._ImportStaging.stage
    call_count = {"n": 0}
    def _flaky_stage(self, src, final_path):
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise OSError("simulated move-mode stage failure on file 3")
        return real_stage(self, src, final_path)
    monkeypatch.setattr(main._ImportStaging, "stage", _flaky_stage)

    _run(main._execute_import(qid))

    # All 4 source files still exist — move deletion was deferred to
    # commit_all, which never ran because of the rollback.
    for src in srcs:
        assert os.path.isfile(src), \
            f"move mode lost source {src} during rollback (DATA LOSS)"
    # No final files at destination
    for name in fnames:
        assert not os.path.exists(os.path.join(exec_env["dst_dir"], name))
    # Queue ended 'failed'
    assert _queue_row(exec_env["db_path"], qid)["status"] == "failed"


# ───────────── Test 4: destination-overwrite behaviour ─────────────

def test_successful_import_overwrites_existing_final_file(exec_env, monkeypatch):
    """If the final destination already has a file (e.g. a prior import of
    the same volume), a successful import replaces it in place."""
    import main
    monkeypatch.setitem(main.CONFIG, "import_mode", "copy")
    monkeypatch.setitem(main.CONFIG, "save_path", exec_env["library_root"])

    # Pre-create dst_dir with an existing file at the final path
    os.makedirs(exec_env["dst_dir"], exist_ok=True)
    preexisting = os.path.join(exec_env["dst_dir"], "Test Series v01.cbz")
    with open(preexisting, "wb") as f:
        f.write(b"OLD-CONTENT")
    assert _digest(preexisting) == _digest(preexisting)  # sanity

    sid = _seed_series(exec_env["db_path"])
    qid, srcs, _ = _seed_queue(
        exec_env["db_path"], sid, file_count=1,
        src_root=exec_env["src_root"],
    )

    result = _run(main._execute_import(qid))
    assert result is True

    # File at final path is NEW content, not OLD.
    assert os.path.isfile(preexisting)
    with open(preexisting, "rb") as f:
        final_bytes = f.read()
    assert final_bytes != b"OLD-CONTENT"
    # And it matches what was at source
    assert _digest(preexisting) == _digest(srcs[0])


def test_failed_batch_does_not_clobber_existing_final_file(exec_env, monkeypatch):
    """If the batch fails, any pre-existing file at the final path must
    survive untouched. Since the file op lands in staging first and is
    never renamed, the final path is never opened for write."""
    import main
    monkeypatch.setitem(main.CONFIG, "import_mode", "copy")
    monkeypatch.setitem(main.CONFIG, "save_path", exec_env["library_root"])

    # Pre-create a file at the final path of file 1.
    os.makedirs(exec_env["dst_dir"], exist_ok=True)
    preexisting = os.path.join(exec_env["dst_dir"], "Test Series v01.cbz")
    with open(preexisting, "wb") as f:
        f.write(b"ORIGINAL-DO-NOT-CLOBBER")

    sid = _seed_series(exec_env["db_path"])
    qid, srcs, fnames = _seed_queue(
        exec_env["db_path"], sid, file_count=3,
        src_root=exec_env["src_root"],
    )

    # Fail on file 3 so the whole batch rolls back
    real_stage = main._ImportStaging.stage
    n = {"c": 0}
    def _flaky_stage(self, src, final_path):
        n["c"] += 1
        if n["c"] == 3:
            raise OSError("simulated failure")
        return real_stage(self, src, final_path)
    monkeypatch.setattr(main._ImportStaging, "stage", _flaky_stage)

    _run(main._execute_import(qid))

    # The pre-existing file at final path is UNCHANGED
    assert os.path.isfile(preexisting)
    with open(preexisting, "rb") as f:
        assert f.read() == b"ORIGINAL-DO-NOT-CLOBBER", \
            "rollback allowed a pre-existing final file to be clobbered"
