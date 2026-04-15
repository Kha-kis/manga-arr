"""Tests for M2: partial-import atomicity.

Focus on the _ImportStaging helper primitive. The helper is what makes
batch rollback possible: every file op lands under a per-batch staging
directory, and commit_all / rollback decide whether the batch commits
or disappears.

We exercise the helper directly (hardlink, copy, move; success and
failure paths). Integration with _execute_import's SAVEPOINT + commit
decision is tested separately via its own path.
"""
import hashlib
import os

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
