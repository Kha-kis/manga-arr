"""Opt-in quarantine of loose chapters a merged volume holds (issue #422, part 2b).

Coverage:
  - off by default; when on, loose chapters proven inside their volume move
    to ``.mangarr-quarantine/<series>/`` and the chapters point at the volume
  - each eligibility refusal leaves the file and its row untouched: changed
    loose file, re-downloaded chapter with different pages, changed volume,
    no assembly record, chapter outside the volume, other Suwayomi context
  - a crash after each phase recovers (rollback before commit, forward after),
    including the link fallback interrupted between link and unlink
  - a commit that loses its chapter CAS or fails rolls back; a taken name on
    rollback keeps the file and fences the series
  - restore moves it back and repoints the chapter; refused when the name is
    taken or the quarantined file changed
  - cross-filesystem refused before anything moves; both move paths work
  - the job path quarantines after a merged volume import; Suwayomi's own
    downloads are untouched
"""
import errno
import hashlib
import os
import sqlite3
import stat
import sys
import tempfile
import zipfile

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401

SOURCE_ID = "2131019126180322627"
SERVER = "http://swy.local:4567"
PAGES = {501: (1.0, [b"one-a" * 50, b"one-b" * 50]), 502: (2.0, [b"two-a" * 50, b"two-b" * 50, b"two-c" * 50])}


def _cbz(path, pages):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for i, data in enumerate(pages):
            zf.writestr(f"{i + 1:04d}.png", data)
    return str(path)


def _sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _ident(cid, source_id=SOURCE_ID):
    import chapter_provenance

    return chapter_provenance.SourceIdentity(1, SERVER, 99, source_id, cid)


@pytest.fixture
def lib(tmp_path, monkeypatch):
    """A series with two loose chapters (with provenance) and their volume."""
    import chapter_provenance
    import main
    import security
    import shared
    import volume_assembly

    db = tempfile.NamedTemporaryFile(suffix=".db", dir=tmp_path, delete=False)
    db.close()
    os.unlink(db.name)
    orig = main.DB_PATH, shared.DB_PATH
    main.DB_PATH = shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(tempfile.mkdtemp(prefix="mangarr-cleanup-keys-"))
    main.init_db()
    main.load_config()
    monkeypatch.setitem(shared.CONFIG, "cleanup_loose_chapters", "true")

    root = tmp_path / "library"
    series_dir = root / "Freezing"
    swy = tmp_path / "swy"
    loose = {cid: _cbz(series_dir / f"Freezing Ch{int(n):03d}.cbz", p) for cid, (n, p) in PAGES.items()}
    sources = {cid: _cbz(swy / f"Chapter {int(n)}.cbz", p) for cid, (n, p) in PAGES.items()}
    volume_path = str(series_dir / "Freezing v01.cbz")
    result = volume_assembly.assemble(
        [volume_assembly.AssemblySource(sources[c], _ident(c)) for c in PAGES], volume_path
    )
    assert result.assembly is not None
    with sqlite3.connect(db.name) as c:
        c.row_factory = sqlite3.Row
        c.execute("INSERT INTO series(id, title, search_pattern) VALUES(7, 'Freezing', 'Freezing')")
        vid = c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, import_path, client)"
            " VALUES(7, 1.0, 'downloaded', ?, 'suwayomi')",
            (volume_path,),
        ).lastrowid
        for cid, (num, _pages) in PAGES.items():
            c.execute(
                "INSERT INTO chapters(series_id, volume_id, chapter_num, status, monitored,"
                " import_path, size_bytes, quality, client)"
                " VALUES(7, ?, ?, 'downloaded', 1, ?, ?, 'cbz', 'suwayomi')",
                (vid, num, loose[cid], os.path.getsize(loose[cid])),
            )
            assert chapter_provenance.record(
                c, series_id=7, chapter_num=num, path=loose[cid],
                evidence=chapter_provenance.fingerprint(loose[cid]), source=_ident(cid), job_id=None,
            )
        assert volume_assembly.record(
            c, series_id=7, volume_num=1.0, path=volume_path, assembly=result.assembly, job_id=None
        )
    try:
        yield {
            "db": db.name, "root": root, "dir": series_dir, "loose": loose, "sources": sources,
            "volume": volume_path, "vid": vid, "q": root / ".mangarr-quarantine" / "7",
        }
    finally:
        main.DB_PATH, shared.DB_PATH = orig


def _cleanup(lib):
    import shared
    import volume_chapter_cleanup
    from file_mutation_lock import file_mutation_guard

    with file_mutation_guard(shared.DB_PATH) as guard:
        return volume_chapter_cleanup.cleanup_volume(guard, 7, lib["vid"])


def _chapters(lib):
    with sqlite3.connect(lib["db"]) as c:
        c.row_factory = sqlite3.Row
        return {r["chapter_num"]: dict(r) for r in c.execute("SELECT * FROM chapters WHERE series_id=7")}


def _rows(lib):
    with sqlite3.connect(lib["db"]) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT * FROM chapter_quarantine ORDER BY id")]


def _provenance(lib):
    with sqlite3.connect(lib["db"]) as c:
        return {r[0] for r in c.execute("SELECT library_path FROM chapter_file_provenance")}


def _no_carrier(lib):
    import volume_chapter_cleanup

    return not any(n.startswith(volume_chapter_cleanup.CARRIER_PREFIX) for n in os.listdir(lib["dir"]))


def _untouched(lib, before):
    assert _chapters(lib) == before
    assert all(os.path.isfile(p) for p in lib["loose"].values())
    assert _rows(lib) == []
    assert _no_carrier(lib)


def _fenced(lib):
    import import_publication

    with sqlite3.connect(lib["db"]) as c:
        try:
            import_publication.assert_library_file_mutation_available(c, 7)
        except import_publication.PublicationBlocked:
            return True
    return False


# ── quarantined ─────────────────────────────────────────────────────────────


def test_off_by_default(lib, monkeypatch):
    import shared

    monkeypatch.setitem(shared.CONFIG, "cleanup_loose_chapters", "false")
    before = _chapters(lib)
    assert _cleanup(lib).quarantined == 0
    _untouched(lib, before)


def test_proven_loose_chapters_are_quarantined_and_repointed(lib):
    volume_sha = _sha(lib["volume"])
    loose_sha = {cid: _sha(p) for cid, p in lib["loose"].items()}
    summary = _cleanup(lib)
    assert summary.quarantined == 2 and not summary.refused
    rows = _rows(lib)
    assert [r["state"] for r in rows] == ["quarantined", "quarantined"]
    for row, cid in zip(rows, PAGES):
        assert not os.path.exists(row["original_path"])
        assert row["quarantine_path"] == str(lib["q"] / f"{os.path.basename(lib['loose'][cid])}.{row['id']}")
        assert _sha(row["quarantine_path"]) == loose_sha[cid]
        assert os.stat(row["quarantine_path"]).st_nlink == 1
    for ch in _chapters(lib).values():
        assert ch["status"] == "downloaded" and ch["import_path"] == lib["volume"]
        assert ch["size_bytes"] is None and ch["quality"] is None and ch["monitored"] == 1
    assert _provenance(lib) == set()
    assert _sha(lib["volume"]) == volume_sha
    assert _no_carrier(lib) and not _fenced(lib)
    for d in (lib["q"], lib["q"].parent):
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    assert {p.name for p in lib["q"].iterdir()} == {os.path.basename(r["quarantine_path"]) for r in rows}


def test_link_fallback_moves_leave_one_name(lib, monkeypatch):
    import import_publication

    def _unsupported(_src, dst):
        raise OSError(errno.ENOTSUP, "atomic no-replace rename is unsupported", dst)

    monkeypatch.setattr(import_publication, "_rename_noreplace", _unsupported)
    assert _cleanup(lib).quarantined == 2
    for row in _rows(lib):
        assert os.stat(row["quarantine_path"]).st_nlink == 1
    assert _no_carrier(lib)


def test_device_number_change_after_remount_still_quarantines(lib):
    """A remount (NFS, btrfs subvolume) gives new st_dev; content proof still holds."""
    with sqlite3.connect(lib["db"]) as c:
        assert c.execute("UPDATE chapter_file_provenance SET file_dev='999999'").rowcount == 2
        assert c.execute("UPDATE volume_assembly SET file_dev='999999'").rowcount == 1
    summary = _cleanup(lib)
    assert summary.quarantined == 2 and not summary.refused and not summary.skipped
    assert {ch["import_path"] for ch in _chapters(lib).values()} == {lib["volume"]}


# ── refused ─────────────────────────────────────────────────────────────────


def test_modified_loose_file_is_skipped(lib):
    with open(lib["loose"][501], "ab") as fh:
        fh.write(b"x")
    before = _chapters(lib)
    summary = _cleanup(lib)
    assert summary.quarantined == 1
    assert "no longer matches its provenance" in summary.skipped["suwayomi chapter 501"]
    assert os.path.isfile(lib["loose"][501]) and _chapters(lib)[1.0] == before[1.0]


def test_redownloaded_chapter_with_other_pages_is_skipped(lib):
    """Same Suwayomi chapter id, provenance consistent, pages differ."""
    import chapter_provenance

    _cbz(lib["loose"][501], [b"redrawn page"])
    with sqlite3.connect(lib["db"]) as c:
        c.row_factory = sqlite3.Row
        c.execute("UPDATE chapters SET size_bytes=? WHERE chapter_num=1.0", (os.path.getsize(lib["loose"][501]),))
        chapter_provenance.record(
            c, series_id=7, chapter_num=1.0, path=lib["loose"][501],
            evidence=chapter_provenance.fingerprint(lib["loose"][501]), source=_ident(501), job_id=None,
        )
    summary = _cleanup(lib)
    assert summary.skipped["suwayomi chapter 501"] == "loose pages differ from the volume's page range"
    assert os.path.isfile(lib["loose"][501])


def test_modified_volume_skips_everything(lib):
    with zipfile.ZipFile(lib["volume"], "a") as zf:
        zf.writestr("extra.txt", b"x")
    before = _chapters(lib)
    summary = _cleanup(lib)
    assert summary.skipped == {"volume": "volume file no longer matches its assembly record"}
    _untouched(lib, before)


def test_volume_without_assembly_record_skips_everything(lib):
    with sqlite3.connect(lib["db"]) as c:
        c.execute("DELETE FROM volume_assembly")
    before = _chapters(lib)
    assert _cleanup(lib).skipped == {"volume": "no assembly record for this volume file"}
    _untouched(lib, before)


def test_chapter_outside_the_volume_is_skipped(lib):
    with sqlite3.connect(lib["db"]) as c:
        c.execute("UPDATE chapters SET volume_id=NULL WHERE chapter_num=2.0")
    summary = _cleanup(lib)
    assert summary.skipped["suwayomi chapter 502"] == "chapter does not belong to this volume"
    assert os.path.isfile(lib["loose"][502])


def test_other_suwayomi_context_is_skipped(lib):
    with sqlite3.connect(lib["db"]) as c:
        c.execute("UPDATE chapter_file_provenance SET suwayomi_source_id='other'"
                  " WHERE suwayomi_chapter_id=501")
    summary = _cleanup(lib)
    assert "no single provenance record" in summary.skipped["suwayomi chapter 501"]
    assert os.path.isfile(lib["loose"][501])


def test_cross_filesystem_quarantine_refuses_the_run(lib, monkeypatch):
    import volume_chapter_cleanup

    def _exdev(_src, dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link", dst)

    monkeypatch.setattr(volume_chapter_cleanup, "_move_noreplace", _exdev)
    before = _chapters(lib)
    summary = _cleanup(lib)
    assert summary.refused.startswith("quarantine move refused")
    _untouched(lib, before)


# ── crash recovery ──────────────────────────────────────────────────────────


class _Crash(BaseException):
    """Simulates the process dying: not caught by any recovery handler."""


def _crash_at(monkeypatch, name):
    import volume_chapter_cleanup

    def _die(*_a, **_k):
        raise _Crash(name)

    monkeypatch.setattr(volume_chapter_cleanup, name, _die)


def _replay_all():
    import volume_chapter_cleanup

    return [volume_chapter_cleanup.replay(i) for i in volume_chapter_cleanup.active_ids()]


def test_crash_after_reserve_rolls_back(lib, monkeypatch):
    _crash_at(monkeypatch, "_capture")
    before = _chapters(lib)
    with pytest.raises(_Crash):
        _cleanup(lib)
    assert _rows(lib)[0]["state"] == "reserved" and _fenced(lib)
    monkeypatch.undo()
    assert _replay_all() == ["rolled_back"]
    assert _chapters(lib) == before and os.path.isfile(lib["loose"][501])
    assert _rows(lib)[0]["state"] == "rolled_back" and not _fenced(lib)


def test_crash_after_capture_rolls_back(lib, monkeypatch):
    _crash_at(monkeypatch, "_commit")
    before = _chapters(lib)
    with pytest.raises(_Crash):
        _cleanup(lib)
    row = _rows(lib)[0]
    assert row["state"] == "captured" and not os.path.exists(row["original_path"])
    monkeypatch.undo()
    assert _replay_all() == ["rolled_back"]
    assert _chapters(lib) == before
    assert _sha(lib["loose"][501]) == row["sha256"] and _no_carrier(lib)
    assert _provenance(lib) == set(lib["loose"].values())


def test_crash_after_commit_moves_forward(lib, monkeypatch):
    _crash_at(monkeypatch, "_transfer")
    with pytest.raises(_Crash):
        _cleanup(lib)
    row = _rows(lib)[0]
    assert row["state"] == "committed" and _chapters(lib)[1.0]["import_path"] == lib["volume"]
    monkeypatch.undo()
    assert _replay_all() == ["quarantined"]
    row = _rows(lib)[0]
    assert row["state"] == "quarantined" and _sha(row["quarantine_path"]) == row["sha256"]
    assert _no_carrier(lib)


def test_crash_between_link_and_unlink_keeps_one_file(lib, monkeypatch):
    import volume_chapter_cleanup

    def _link_then_die(source, destination):
        os.link(source, destination)
        raise _Crash("after link")

    monkeypatch.setattr(volume_chapter_cleanup, "_move_noreplace", _link_then_die)
    monkeypatch.setattr(volume_chapter_cleanup, "_probe", lambda *_a: None)
    with pytest.raises(_Crash):
        _cleanup(lib)
    row = _rows(lib)[0]
    assert os.stat(row["quarantine_path"]).st_nlink == 2
    monkeypatch.undo()
    assert _replay_all() == ["quarantined"]
    assert os.stat(row["quarantine_path"]).st_nlink == 1 and _no_carrier(lib)


def test_commit_losing_its_chapter_cas_rolls_back(lib, monkeypatch):
    import volume_chapter_cleanup

    real = volume_chapter_cleanup._capture

    def _capture_then_touch_row(row, guard):
        real(row, guard)
        with sqlite3.connect(lib["db"]) as c:
            c.execute("UPDATE chapters SET monitored=0 WHERE id=?", (row["chapter_id"],))

    monkeypatch.setattr(volume_chapter_cleanup, "_capture", _capture_then_touch_row)
    summary = _cleanup(lib)
    assert summary.quarantined == 0
    assert [r["state"] for r in _rows(lib)][:1] == ["rolled_back"]
    assert all(os.path.isfile(p) for p in lib["loose"].values()) and _no_carrier(lib)


def test_bytes_changed_in_place_before_capture_roll_back(lib, monkeypatch):
    """Same inode, size and mtime, different bytes: caught after capture."""
    import volume_chapter_cleanup

    real = volume_chapter_cleanup._reserve

    def _reserve_then_rewrite(candidate, series_id):
        row_id = real(candidate, series_id)
        st = os.stat(candidate.path)
        with open(candidate.path, "r+b") as fh:
            fh.seek(st.st_size - 1)
            last = fh.read(1)
            fh.seek(st.st_size - 1)
            fh.write(b"\x00" if last != b"\x00" else b"\x01")
        assert os.stat(candidate.path).st_size == st.st_size
        os.utime(candidate.path, ns=(st.st_atime_ns, st.st_mtime_ns))
        return row_id

    monkeypatch.setattr(volume_chapter_cleanup, "_reserve", _reserve_then_rewrite)
    before = _chapters(lib)
    assert _cleanup(lib).quarantined == 0
    assert {r["state"] for r in _rows(lib)} == {"rolled_back"}
    assert _chapters(lib) == before and all(os.path.isfile(p) for p in lib["loose"].values())
    assert _no_carrier(lib)


def test_commit_failing_inside_its_transaction_rolls_back_now(lib, monkeypatch):
    """The DB stays ``captured``; the file goes straight back, no fence left."""
    import volume_chapter_cleanup

    real = volume_chapter_cleanup._advance

    def _fail_commit(row, db=None, **changes):
        result = real(row, db, **changes)
        if db is not None and changes.get("state") == "committed":
            raise sqlite3.OperationalError("disk I/O error")
        return result

    monkeypatch.setattr(volume_chapter_cleanup, "_advance", _fail_commit)
    before = _chapters(lib)
    assert _cleanup(lib).quarantined == 0
    assert _chapters(lib) == before and all(os.path.isfile(p) for p in lib["loose"].values())
    assert {r["state"] for r in _rows(lib)} == {"rolled_back"} and not _fenced(lib)
    assert _provenance(lib) == set(lib["loose"].values()) and _no_carrier(lib)


def test_volume_changed_before_commit_rolls_back(lib, monkeypatch):
    import volume_chapter_cleanup

    real = volume_chapter_cleanup._capture

    def _capture_then_touch_volume(row, guard):
        real(row, guard)
        os.utime(lib["volume"], ns=(1, 1))

    monkeypatch.setattr(volume_chapter_cleanup, "_capture", _capture_then_touch_volume)
    before = _chapters(lib)
    assert _cleanup(lib).quarantined == 0
    assert _chapters(lib) == before and all(os.path.isfile(p) for p in lib["loose"].values())
    assert {r["state"] for r in _rows(lib)} == {"rolled_back"}


def test_loose_file_not_beside_its_volume_is_skipped(lib):
    import chapter_provenance

    moved = str(lib["dir"] / "older" / os.path.basename(lib["loose"][502]))
    os.makedirs(os.path.dirname(moved))
    os.rename(lib["loose"][502], moved)
    with sqlite3.connect(lib["db"]) as c:
        c.row_factory = sqlite3.Row
        c.execute("DELETE FROM chapter_file_provenance WHERE suwayomi_chapter_id=502")
        c.execute("UPDATE chapters SET import_path=? WHERE chapter_num=2.0", (moved,))
        chapter_provenance.record(
            c, series_id=7, chapter_num=2.0, path=moved,
            evidence=chapter_provenance.fingerprint(moved), source=_ident(502), job_id=None,
        )
    summary = _cleanup(lib)
    assert summary.quarantined == 1
    assert summary.skipped["suwayomi chapter 502"] == "loose file is not beside its volume"
    assert os.path.isfile(moved)


def test_real_cross_device_errors_are_not_swallowed(lib, monkeypatch):
    import import_publication

    def _exdev(_src, dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link", dst)

    monkeypatch.setattr(import_publication, "_rename_noreplace", _exdev)
    before = _chapters(lib)
    assert _cleanup(lib).refused.startswith("quarantine move refused")
    _untouched(lib, before)

    def _unsupported(_src, dst):
        raise OSError(errno.ENOTSUP, "unsupported", dst)

    monkeypatch.setattr(import_publication, "_rename_noreplace", _unsupported)
    def _link_exdev(_src, dst, **_k):
        raise OSError(errno.EXDEV, "Invalid cross-device link", dst)

    monkeypatch.setattr(os, "link", _link_exdev)
    assert _cleanup(lib).refused.startswith("quarantine move refused")
    _untouched(lib, before)


def test_crash_after_carrier_recorded_leaves_no_carrier(lib, monkeypatch):
    import volume_chapter_cleanup

    real_rename = os.rename

    def _die_on_capture(src, dst, *a, **k):
        if os.path.basename(os.path.dirname(dst)).startswith(volume_chapter_cleanup.CARRIER_PREFIX):
            raise _Crash("before rename")
        return real_rename(src, dst, *a, **k)

    monkeypatch.setattr(os, "rename", _die_on_capture)
    with pytest.raises(_Crash):
        _cleanup(lib)
    row = _rows(lib)[0]
    assert row["state"] == "reserved" and os.path.isdir(row["carrier_path"])
    monkeypatch.undo()
    assert _replay_all() == ["rolled_back"]
    assert _no_carrier(lib) and os.path.isfile(lib["loose"][501])


def test_active_row_blocks_series_purge(lib, monkeypatch):
    from routers import series_

    _crash_at(monkeypatch, "_transfer")
    with pytest.raises(_Crash):
        _cleanup(lib)
    c = sqlite3.connect(lib["db"], isolation_level=None)
    c.row_factory = sqlite3.Row
    try:
        c.execute("UPDATE series SET deleted_at=CURRENT_TIMESTAMP WHERE id=7")
        result = series_._prepare_hard_delete_series(c, 7, remove_files=True)
    finally:
        c.close()
    status = result["status"] if isinstance(result, dict) else result.status
    assert status == "import_in_progress"


def test_taken_name_on_rollback_keeps_the_file_and_fences(lib, monkeypatch):
    _crash_at(monkeypatch, "_commit")
    with pytest.raises(_Crash):
        _cleanup(lib)
    row = _rows(lib)[0]
    with open(row["original_path"], "wb") as fh:
        fh.write(b"someone else")
    monkeypatch.undo()
    assert _replay_all() == ["blocked"]
    row = _rows(lib)[0]
    assert row["state"] == "captured" and "original name is taken" in row["diagnostic"]
    kept = os.path.join(row["carrier_path"], os.path.basename(row["original_path"]))
    assert _sha(kept) == row["sha256"]
    with open(row["original_path"], "rb") as fh:
        assert fh.read() == b"someone else"
    assert _fenced(lib)
    os.unlink(row["original_path"])
    assert _replay_all() == ["rolled_back"] and not _fenced(lib)


def test_active_row_fences_rescan_and_imports(lib, monkeypatch):
    import rescan
    import rescan_file_recovery

    _crash_at(monkeypatch, "_transfer")
    with pytest.raises(_Crash):
        _cleanup(lib)
    with sqlite3.connect(lib["db"]) as c:
        assert rescan_file_recovery.competing_for_series(c, 7)
    assert _fenced(lib)
    assert rescan._is_private_inventory_directory(os.path.basename(_rows(lib)[0]["carrier_path"]))
    assert rescan._is_private_inventory_directory(".mangarr-quarantine")


# ── restore ─────────────────────────────────────────────────────────────────


def test_restore_moves_back_and_repoints(lib):
    import volume_chapter_cleanup

    _cleanup(lib)
    row = _rows(lib)[0]
    assert volume_chapter_cleanup.restore(row["id"], 7) == ("restored", "")
    assert _sha(row["original_path"]) == row["sha256"]
    assert not os.path.exists(row["quarantine_path"])
    ch = _chapters(lib)[1.0]
    assert ch["import_path"] == row["original_path"] and ch["size_bytes"] == row["size_bytes"]
    assert _rows(lib)[0]["state"] == "restored" and not _fenced(lib)


def test_restore_refused_when_name_taken(lib):
    import volume_chapter_cleanup

    _cleanup(lib)
    row = _rows(lib)[0]
    with open(row["original_path"], "wb") as fh:
        fh.write(b"new file")
    outcome, diagnostic = volume_chapter_cleanup.restore(row["id"], 7)
    assert outcome == "terminal" and "original name is taken" in diagnostic
    assert _rows(lib)[0]["state"] == "quarantined" and _sha(row["quarantine_path"]) == row["sha256"]
    with open(row["original_path"], "rb") as fh:
        assert fh.read() == b"new file"


def test_restore_refused_when_quarantined_file_changed(lib):
    import volume_chapter_cleanup

    _cleanup(lib)
    row = _rows(lib)[0]
    with open(row["quarantine_path"], "ab") as fh:
        fh.write(b"x")
    outcome, diagnostic = volume_chapter_cleanup.restore(row["id"], 7)
    assert outcome == "terminal" and "changed" in diagnostic
    assert not os.path.exists(row["original_path"]) and _rows(lib)[0]["state"] == "quarantined"


def test_restore_with_quarantined_file_missing_lifts_the_fence(lib):
    import volume_chapter_cleanup

    _cleanup(lib)
    row = _rows(lib)[0]
    os.unlink(row["quarantine_path"])
    outcome, diagnostic = volume_chapter_cleanup.restore(row["id"], 7)
    assert outcome == "terminal" and "missing" in diagnostic
    assert _rows(lib)[0]["state"] == "quarantined" and not _fenced(lib)


def test_restore_move_failure_lifts_the_fence(lib, monkeypatch):
    import import_publication
    import volume_chapter_cleanup

    _cleanup(lib)
    row = _rows(lib)[0]

    def _exdev(_src, dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link", dst)

    monkeypatch.setattr(import_publication, "_rename_noreplace", _exdev)
    outcome, diagnostic = volume_chapter_cleanup.restore(row["id"], 7)
    assert outcome == "terminal" and "cross-device" in diagnostic
    assert _rows(lib)[0]["state"] == "quarantined" and not _fenced(lib)
    assert _sha(row["quarantine_path"]) == row["sha256"] and not os.path.exists(row["original_path"])


def test_restore_refused_after_the_volume_was_deleted(lib):
    import volume_chapter_cleanup

    _cleanup(lib)
    row = _rows(lib)[0]
    with sqlite3.connect(lib["db"]) as c:
        c.execute("UPDATE chapters SET status='wanted', import_path=NULL WHERE id=?", (row["chapter_id"],))
        c.execute("UPDATE volumes SET status='wanted', import_path=NULL")
    outcome, diagnostic = volume_chapter_cleanup.restore(row["id"], 7)
    assert outcome == "terminal" and "no longer points at its volume" in diagnostic
    assert _rows(lib)[0]["state"] == "quarantined" and not os.path.exists(row["original_path"])


def test_restore_of_another_series_row_is_refused(lib):
    import volume_chapter_cleanup

    _cleanup(lib)
    row = _rows(lib)[0]
    assert volume_chapter_cleanup.restore(row["id"], 8)[0] == "terminal"
    assert _rows(lib)[0]["state"] == "quarantined"


def test_crash_during_restore_after_link_completes(lib, monkeypatch):
    import volume_chapter_cleanup

    _cleanup(lib)
    row = _rows(lib)[0]

    def _link_then_die(source, destination):
        os.link(source, destination)
        raise _Crash("after link")

    monkeypatch.setattr(volume_chapter_cleanup, "_move_noreplace", _link_then_die)
    with pytest.raises(_Crash):
        volume_chapter_cleanup.restore(row["id"], 7)
    monkeypatch.undo()
    assert _replay_all() == ["restored"]
    assert os.stat(row["original_path"]).st_nlink == 1
    assert not os.path.exists(row["quarantine_path"])


def test_series_purge_leaves_quarantine_rows_and_files(lib):
    _cleanup(lib)
    with sqlite3.connect(lib["db"]) as c:
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("DELETE FROM chapters")
        c.execute("DELETE FROM volumes")
        c.execute("DELETE FROM series WHERE id=7")
    rows = _rows(lib)
    assert len(rows) == 2 and all(os.path.isfile(r["quarantine_path"]) for r in rows)


# ── job path ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("enabled", [True, False], ids=["on", "off"])
def test_merged_volume_job_quarantines_its_loose_chapters(tmp_path, monkeypatch, enabled):
    import chapter_provenance
    import shared
    import test_volume_assembly as tva

    env_gen = tva.env.__wrapped__(tmp_path, monkeypatch)
    env = next(env_gen)
    try:
        monkeypatch.setitem(shared.CONFIG, "cleanup_loose_chapters", "true" if enabled else "false")
        tva._queue(env)
        loose = {}
        with sqlite3.connect(env["db"]) as c:
            c.row_factory = sqlite3.Row
            vid = c.execute("SELECT id FROM volumes WHERE series_id=7 AND volume_num=1.0").fetchone()[0]
            for cid, num, *pages in tva.CHAPTERS:
                loose[cid] = _cbz(env["lib"] / f"Freezing Ch{int(num):03d}.cbz", pages)
                c.execute(
                    "INSERT INTO chapters(series_id, volume_id, chapter_num, status, monitored,"
                    " import_path, client) VALUES(7, ?, ?, 'downloaded', 1, ?, 'suwayomi')",
                    (vid, num, loose[cid]),
                )
                assert chapter_provenance.record(
                    c, series_id=7, chapter_num=num, path=loose[cid],
                    evidence=chapter_provenance.fingerprint(loose[cid]),
                    source=chapter_provenance.SourceIdentity(1, SERVER, 99, tva.SOURCE_ID, cid),
                    job_id=None,
                )
        sources = {cid: _sha(p) for cid, p in env["sources"].items()}
        tva._run(env)
        _asm, _chs, vol, jobs = tva._rows(env)
        assert jobs == [{"status": "completed", "error": None}] and vol["status"] == "downloaded"
        with sqlite3.connect(env["db"]) as c:
            states = [r[0] for r in c.execute("SELECT state FROM chapter_quarantine")]
            paths = {r[0] for r in c.execute("SELECT import_path FROM chapters WHERE series_id=7")}
        if enabled:
            assert states == ["quarantined", "quarantined"]
            assert not any(os.path.exists(p) for p in loose.values())
            assert paths == {vol["import_path"]}
        else:
            assert states == [] and all(os.path.isfile(p) for p in loose.values())
        assert {cid: _sha(p) for cid, p in env["sources"].items()} == sources
    finally:
        env_gen.close()
