"""Volume assembly evidence for merged Suwayomi volumes (issue #422, part 2a).

A merged volume is staged privately, finished there, and published to its
absent final name. Evidence is recorded only for a volume this import
created from chapters selected by their own Suwayomi identity, with the full
context known and every chapter's pages verified in the published bytes.
Nothing here cleans anything up. Coverage:
  - a new volume records the file fingerprint, and per chapter in order the
    source hash and the page range it produced, which re-verifies
  - reused volume, non-merge mode, legacy matching, missing context,
    oversized, changing or empty sources record nothing
  - pages that differ after finishing record nothing
  - a final volume above the evidence bound, unknown context or pages that
    cannot be verified still import, without evidence; a stage that changed
    after finishing is still never published
  - page verification reads bounded chunks, never a whole chapter
  - an existing final name is never replaced; failures leave no stage
  - the link fallback publishes one name; a change after commit voids
"""
import asyncio
import errno
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import zipfile
from unittest.mock import patch

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


SOURCE_ID = "2131019126180322627"
CHAPTERS = [(501, 1.0, b"one-a", b"one-b"), (502, 2.0, b"two-a", b"two-b", b"two-c")]
VOLUME = "Freezing v01.cbz"


def _make_cbz(path, pages):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for i, data in enumerate(pages):
            zf.writestr(f"{i + 1:04d}.png", data)
    return str(path)


def _sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


@pytest.fixture
def env(tmp_path, monkeypatch):
    import main
    import security
    import shared

    db = tempfile.NamedTemporaryFile(suffix=".db", dir=tmp_path, delete=False)
    db.close()
    os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-assembly-keys-")
    orig_main_db, orig_shared_db = main.DB_PATH, shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()

    swy_root = tmp_path / "swy"
    lib_root = tmp_path / "library"
    lib_root.mkdir()

    def _fake_dir(_db, sid):
        d = lib_root / f"series-{sid}"
        d.mkdir(exist_ok=True)
        return str(d)

    monkeypatch.setattr(main, "_series_library_dir", _fake_dir)
    from routers import suwayomi_ as swy

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(swy._aio, "sleep", _no_sleep)

    with sqlite3.connect(db.name) as c:
        c.execute(
            "INSERT INTO download_clients(id, name, type, host, enabled, download_path)"
            " VALUES(1, 'swy', 'suwayomi', 'http://swy.local:4567', 1, ?)",
            (str(swy_root),),
        )
        c.execute(
            "INSERT INTO series(id, title, search_pattern) VALUES(7, 'Freezing', 'Freezing')"
        )
    manga_dir = swy_root / "mangas" / "Weeb Central (EN)" / "Freezing"
    sources = {
        cid: _make_cbz(manga_dir / f"Scans_Chapter {int(num)}.cbz", pages)
        for cid, num, *pages in CHAPTERS
    }
    try:
        yield {
            "db": db.name,
            "lib": lib_root / "series-7",
            "manga_dir": manga_dir,
            "sources": sources,
            "merge": True,
        }
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db


def _queue(env):
    with sqlite3.connect(env["db"]) as c:
        c.execute(
            "INSERT OR IGNORE INTO volumes(series_id, volume_num, status)"
            " VALUES(7, 1.0, 'wanted')"
        )
        c.execute(
            "INSERT INTO suwayomi_downloads"
            " (series_id, suwayomi_manga_id, chapter_ids, volume_num, status,"
            "  progress, total, created_at)"
            " VALUES(7, 99, ?, 1.0, 'queued', 0, 2, datetime('now'))",
            (json.dumps([cid for cid, *_ in CHAPTERS]),),
        )


def _gql(*, source_id=SOURCE_ID):
    async def _fake(_c, _query, _vars=None):
        nodes = [
            {"id": cid, "isDownloaded": True, "chapterNumber": num,
             "name": f"Chapter {int(num)}", "scanlator": "Scans"}
            for cid, num, *_ in CHAPTERS
        ]
        source = {"displayName": "Weeb Central (EN)"}
        if source_id is not None:
            source["id"] = source_id
        return {"manga": {"title": "Freezing", "source": source,
                          "chapters": {"nodes": nodes}}}
    return _fake


def _client(env):
    return {"id": 1, "type": "suwayomi", "host": "http://user:secret@Swy.Local:4567/",
            "download_path": str(env["swy_root"]) if "swy_root" in env
            else str(env["manga_dir"].parents[2]),
            "merge_chapters": 1 if env["merge"] else 0}


def _run(env, **gql_kwargs):
    from routers import suwayomi_ as swy

    with patch.object(swy, "_gql", new=_gql(**gql_kwargs)), \
         patch.object(swy, "get_suwayomi_client", new=lambda _db: _client(env)):
        asyncio.run(swy.check_suwayomi_jobs())


def _rows(env):
    with sqlite3.connect(env["db"]) as c:
        c.row_factory = sqlite3.Row
        asm = [dict(r) for r in c.execute("SELECT * FROM volume_assembly")]
        chs = [dict(r) for r in c.execute(
            "SELECT * FROM volume_assembly_chapters ORDER BY assembly_id, position")]
        vol = c.execute("SELECT id, status, import_path FROM volumes WHERE volume_num=1.0").fetchone()
        jobs = [dict(r) for r in c.execute("SELECT status, error FROM suwayomi_downloads")]
    return asm, chs, (dict(vol) if vol else None), jobs


def _volume(env):
    return str(env["lib"] / VOLUME)


def _no_stage(env):
    import volume_assembly

    return not any(
        n.startswith(volume_assembly.STAGE_PREFIX) for n in os.listdir(env["lib"])
    )


# ── recorded ────────────────────────────────────────────────────────────────


def test_new_volume_records_fingerprint_and_ordered_page_ranges(env):
    import volume_assembly

    _queue(env)
    _run(env)
    asm, chs, vol, jobs = _rows(env)
    path = _volume(env)
    assert jobs == [{"status": "completed", "error": None}]
    assert vol["status"] == "downloaded" and vol["import_path"] == path
    assert _no_stage(env)
    assert len(asm) == 1
    a = asm[0]
    st = os.stat(path)
    assert a["volume_id"] == vol["id"] and a["library_path"] == path
    assert (a["file_dev"], a["file_inode"]) == (str(st.st_dev), str(st.st_ino))
    assert a["size_bytes"] == st.st_size and a["mtime_ns"] == st.st_mtime_ns
    assert a["sha256"] == _sha(path)
    assert (a["suwayomi_client_id"], a["suwayomi_server"], a["suwayomi_manga_id"],
            a["suwayomi_source_id"]) == (1, "http://swy.local:4567", 99, SOURCE_ID)
    assert [(c["position"], c["suwayomi_chapter_id"], c["first_page"], c["last_page"])
            for c in chs] == [(0, 501, 1, 2), (1, 502, 3, 5)]
    with zipfile.ZipFile(path) as archive:
        assert "ComicInfo.xml" in archive.namelist()
        for c, (cid, _num, *pages) in zip(chs, CHAPTERS):
            assert c["source_sha256"] == _sha(env["sources"][cid])
            assert c["source_size_bytes"] == os.path.getsize(env["sources"][cid])
            assert c["pages_sha256"] == volume_assembly.page_digest(pages)
            assert volume_assembly.range_digest(
                archive, c["first_page"], c["last_page"]
            ) == c["pages_sha256"]


def test_loose_chapter_pages_match_their_recorded_range(env):
    """The check PR B will make: loose chapter images vs the claimed range."""
    import volume_assembly

    _queue(env)
    _run(env)
    _asm, chs, _vol, _jobs = _rows(env)
    for c in chs:
        src = env["sources"][c["suwayomi_chapter_id"]]
        with zipfile.ZipFile(src) as loose:
            pages = [loose.read(n) for n in volume_assembly.image_members(loose.namelist())]
        assert volume_assembly.page_digest(pages) == c["pages_sha256"]


def test_link_fallback_publishes_one_name_and_records(env, monkeypatch):
    import import_publication

    def _unsupported(_src, dst):
        raise OSError(errno.ENOTSUP, "atomic no-replace rename is unsupported", dst)

    monkeypatch.setattr(import_publication, "_rename_noreplace", _unsupported)
    _queue(env)
    _run(env)
    asm, _chs, vol, _jobs = _rows(env)
    assert vol["status"] == "downloaded"
    assert os.stat(_volume(env)).st_nlink == 1
    assert _no_stage(env)
    assert len(asm) == 1


# ── not recorded ────────────────────────────────────────────────────────────


def test_reused_volume_records_nothing_and_is_untouched(env):
    env["lib"].mkdir(parents=True, exist_ok=True)
    existing = _make_cbz(env["lib"] / VOLUME, [b"someone-else"])
    before = _sha(existing)
    _queue(env)
    _run(env)
    asm, chs, vol, _jobs = _rows(env)
    assert vol["status"] == "downloaded" and vol["import_path"] == existing
    assert _sha(existing) == before
    assert asm == [] and chs == []


def test_non_merge_mode_records_nothing(env):
    env["merge"] = False
    _queue(env)
    _run(env)
    asm, _chs, vol, _jobs = _rows(env)
    assert vol["status"] == "downloaded"
    assert os.path.isdir(vol["import_path"])
    assert asm == []


def test_missing_source_context_records_nothing(env):
    _queue(env)
    _run(env, source_id=None)
    asm, _chs, vol, _jobs = _rows(env)
    assert vol["status"] == "downloaded" and os.path.isfile(_volume(env))
    assert asm == []


def test_legacy_filename_selection_carries_no_evidence(env):
    import chapter_provenance
    from routers import suwayomi_ as swy

    os.rename(env["sources"][501], env["manga_dir"] / "Vol.1 Ch.1.cbz")
    os.rename(env["sources"][502], env["manga_dir"] / "Vol.1 Ch.2.cbz")
    ident = chapter_provenance.SourceIdentity(1, "http://swy.local:4567", 99, SOURCE_ID, 501)
    result = swy._import_suwayomi_volume_result(
        _client(env), 7, 1.0, swy_title="Freezing", source_identity=lambda _cid: ident,
    )
    assert result.path == _volume(env) and result.assembly is None


def test_oversized_source_records_nothing(env, monkeypatch):
    import chapter_provenance

    monkeypatch.setattr(chapter_provenance, "FINGERPRINT_MAX_BYTES", 10)
    _queue(env)
    _run(env)
    asm, _chs, vol, _jobs = _rows(env)
    assert vol["status"] == "downloaded"
    assert asm == []


def test_source_changing_while_read_records_nothing(env, monkeypatch):
    import volume_assembly

    real = volume_assembly._hash_open_file

    def _hash_then_touch(fh):
        digest = real(fh)
        os.utime(fh.fileno(), ns=(1, 1))
        return digest

    monkeypatch.setattr(volume_assembly, "_hash_open_file", _hash_then_touch)
    _queue(env)
    _run(env)
    asm, _chs, vol, _jobs = _rows(env)
    assert vol["status"] == "downloaded"
    assert asm == []


def test_chapter_without_pages_records_nothing(env, caplog):
    _make_cbz(env["sources"][501], [])
    _queue(env)
    _run(env)
    asm, _chs, vol, _jobs = _rows(env)
    assert vol["status"] == "downloaded"
    assert "attempt=" not in caplog.text  # settled first time, not via a retry
    with zipfile.ZipFile(_volume(env)) as archive:
        assert sorted(n for n in archive.namelist() if n.endswith(".png")) == [
            "0001.png", "0002.png", "0003.png"]
    assert asm == []


def test_pages_changed_while_finishing_record_nothing(env, monkeypatch):
    import main

    def _tamper(stage, *_a, **_k):
        with zipfile.ZipFile(stage, "a") as zf:
            zf.writestr("0006.png", b"extra")

    monkeypatch.setattr(main, "_try_inject_comicinfo", _tamper)
    _queue(env)
    _run(env)
    asm, _chs, vol, _jobs = _rows(env)
    assert vol["status"] == "downloaded"
    assert asm == []


@pytest.mark.parametrize("source_id", [SOURCE_ID, None], ids=["known", "unknown"])
def test_volume_above_evidence_bound_still_imports(env, monkeypatch, source_id):
    """Small compressed chapters expand past the bound in the stored volume."""
    import chapter_provenance

    for cid, *_ in CHAPTERS:
        with zipfile.ZipFile(env["sources"][cid], "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("0001.png", bytes([cid % 256]) * 200)
    monkeypatch.setattr(chapter_provenance, "FINGERPRINT_MAX_BYTES", 500)
    assert all(os.path.getsize(p) <= 500 for p in env["sources"].values())
    _queue(env)
    _run(env, source_id=source_id)
    asm, _chs, vol, jobs = _rows(env)
    assert jobs == [{"status": "completed", "error": None}]
    assert vol["status"] == "downloaded" and vol["import_path"] == _volume(env)
    assert os.path.getsize(_volume(env)) > 500
    with zipfile.ZipFile(_volume(env)) as archive:
        assert sorted(n for n in archive.namelist() if n.endswith(".png")) == [
            "0001.png", "0002.png"]
    assert _no_stage(env)
    assert asm == []


def test_unknown_context_never_hashes_the_volume(env, monkeypatch):
    import volume_assembly

    def _refuse(path, **_k):
        raise AssertionError(f"evidence hash without context: {path}")

    monkeypatch.setattr(volume_assembly, "fingerprint", _refuse)
    _queue(env)
    _run(env, source_id=None)
    asm, _chs, vol, _jobs = _rows(env)
    assert vol["status"] == "downloaded" and os.path.isfile(_volume(env))
    assert asm == []


def test_unverifiable_pages_still_import(env, monkeypatch):
    import main

    def _cover(stage, *_a, **_k):
        with zipfile.ZipFile(stage, "a") as zf:
            zf.writestr("cover.jpg", b"not a numbered page")

    monkeypatch.setattr(main, "_try_inject_comicinfo", _cover)
    _queue(env)
    _run(env)
    asm, _chs, vol, jobs = _rows(env)
    assert jobs == [{"status": "completed", "error": None}]
    assert vol["status"] == "downloaded" and os.path.isfile(_volume(env))
    assert asm == []


@pytest.mark.parametrize("source_id", [SOURCE_ID, None], ids=["known", "unknown"])
def test_stage_changed_after_finishing_is_never_published(env, monkeypatch, source_id):
    import volume_assembly

    real = volume_assembly._file_identity
    calls = []

    def _identity_then_touch(path):
        found = real(path)
        if not calls:
            os.utime(path, ns=(1, 1))
        calls.append(path)
        return found

    monkeypatch.setattr(volume_assembly, "_file_identity", _identity_then_touch)
    _queue(env)
    _run(env, source_id=source_id)
    asm, _chs, vol, jobs = _rows(env)
    assert not os.path.exists(_volume(env))
    assert _no_stage(env)
    assert asm == [] and vol["status"] == "wanted"
    assert jobs[0]["status"] == "error"


def test_page_verification_holds_one_bounded_chunk(tmp_path):
    import gc
    import tracemalloc

    import chapter_provenance
    import volume_assembly

    page_size = 1024 * 1024
    path = str(tmp_path / "volume.cbz")
    pages = [os.urandom(page_size) for _ in range(16)]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        for number, data in enumerate(pages, 1):
            zf.writestr(f"{number:04d}.png", data)
    ident = chapter_provenance.SourceIdentity(1, "http://swy.local:4567", 99, SOURCE_ID, 501)
    chapters = (volume_assembly.ChapterPages(
        0, ident, "0" * 64, 1, 1, 16, volume_assembly.page_digest(pages)),)
    del pages
    gc.collect()
    tracemalloc.start()
    try:
        verified = volume_assembly._verify_pages(path, chapters)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert verified
    # The chapter is 16 MiB decoded; verification keeps about one chunk.
    assert peak < 4 * page_size


def test_change_after_commit_voids_the_record(env, monkeypatch):
    import volume_assembly

    real = volume_assembly.record

    def _record_then_touch(db, **kw):
        found = real(db, **kw)
        os.utime(kw["path"], ns=(1, 1))
        return found

    monkeypatch.setattr(volume_assembly, "record", _record_then_touch)
    _queue(env)
    _run(env)
    asm, chs, vol, _jobs = _rows(env)
    assert vol["status"] == "downloaded"
    assert asm == [] and chs == []


def test_record_requires_the_volume_row_settled_to_that_path(env):
    import volume_assembly

    _queue(env)
    _run(env)
    asm, _chs, _vol, _jobs = _rows(env)
    with sqlite3.connect(env["db"]) as c:
        c.row_factory = sqlite3.Row
        c.execute("DELETE FROM volume_assembly")
        assembly = volume_assembly.VolumeEvidence(
            volume_assembly.FileEvidence(1, 2, 3, 4, "0" * 64),
            (volume_assembly.ChapterPages(
                0, __import__("chapter_provenance").SourceIdentity(
                    1, "http://swy.local:4567", 99, SOURCE_ID, 501),
                "0" * 64, 3, 1, 1, "0" * 64),),
        )
        assert volume_assembly.record(
            c, series_id=7, volume_num=1.0, path=_volume(env) + ".other",
            assembly=assembly, job_id=None,
        ) is None
    assert asm  # the real import did record


def test_malformed_evidence_never_fails_the_import_transaction(env):
    import chapter_provenance
    import volume_assembly

    ident = chapter_provenance.SourceIdentity(1, "http://swy.local:4567", 99, SOURCE_ID, 501)
    bad = volume_assembly.VolumeEvidence(
        volume_assembly.FileEvidence(1, 2, 3, 4, "a" * 64),
        (volume_assembly.ChapterPages(0, ident, "b" * 64, 3, 1, 0, "c" * 64),),
    )
    with sqlite3.connect(env["db"]) as c:
        c.row_factory = sqlite3.Row
        c.execute("INSERT INTO volumes(series_id, volume_num, status, import_path)"
                  " VALUES(7, 1.0, 'downloaded', '/v.cbz')")
        assert volume_assembly.record(
            c, series_id=7, volume_num=1.0, path="/v.cbz", assembly=bad, job_id=None,
        ) is None
        # Even an insert the schema refuses leaves the caller's transaction usable.
        orig = volume_assembly._well_formed
        volume_assembly._well_formed = lambda _a: True
        try:
            assert volume_assembly.record(
                c, series_id=7, volume_num=1.0, path="/v.cbz", assembly=bad, job_id=None,
            ) is None
        finally:
            volume_assembly._well_formed = orig
        c.execute("UPDATE volumes SET quality='kept' WHERE volume_num=1.0")
    with sqlite3.connect(env["db"]) as c:
        assert c.execute("SELECT quality FROM volumes").fetchone() == ("kept",)
        assert c.execute("SELECT count(*) FROM volume_assembly").fetchone() == (0,)


# ── publication safety ──────────────────────────────────────────────────────


def test_final_name_created_meanwhile_is_never_replaced(env, monkeypatch):
    import main

    def _race(stage, *_a, **_k):
        _make_cbz(env["lib"] / VOLUME, [b"raced-in"])

    monkeypatch.setattr(main, "_try_inject_comicinfo", _race)
    _queue(env)
    _run(env)
    asm, _chs, vol, jobs = _rows(env)
    with zipfile.ZipFile(_volume(env)) as archive:
        assert archive.read("0001.png") == b"raced-in"
    assert _no_stage(env)
    assert asm == [] and vol["status"] == "wanted"
    assert jobs[0]["status"] == "error"


def test_empty_existing_volume_is_preserved_not_overwritten(env):
    env["lib"].mkdir(parents=True, exist_ok=True)
    (env["lib"] / VOLUME).write_bytes(b"")
    _queue(env)
    _run(env)
    asm, _chs, vol, jobs = _rows(env)
    assert (env["lib"] / VOLUME).read_bytes() == b""
    assert _no_stage(env)
    assert asm == [] and vol["status"] == "wanted"
    assert jobs[0]["status"] == "error"


def test_merge_failure_publishes_nothing_and_leaves_no_stage(env):
    with open(env["sources"][502], "wb") as fh:
        fh.write(b"not a zip")
    _queue(env)
    _run(env)
    asm, _chs, vol, jobs = _rows(env)
    assert not os.path.exists(_volume(env))
    assert _no_stage(env)
    assert asm == [] and vol["status"] == "wanted"
    assert jobs[0]["status"] == "error"


def test_stage_lives_in_a_private_directory(env, monkeypatch):
    import main

    seen = {}

    def _inspect(stage, *_a, **_k):
        carrier = os.path.dirname(stage)
        seen["mode"] = os.stat(carrier).st_mode & 0o7777
        seen["parent"] = os.path.dirname(carrier)

    monkeypatch.setattr(main, "_try_inject_comicinfo", _inspect)
    _queue(env)
    _run(env)
    assert seen == {"mode": 0o700, "parent": str(env["lib"])}
    assert sorted(os.listdir(env["lib"])) == [VOLUME]


def test_unexpected_entry_in_stage_directory_is_retained(env, monkeypatch, caplog):
    import main
    import volume_assembly

    def _litter(stage, *_a, **_k):
        with open(os.path.join(os.path.dirname(stage), "foreign"), "w") as fh:
            fh.write("x")

    monkeypatch.setattr(main, "_try_inject_comicinfo", _litter)
    _queue(env)
    _run(env)
    _asm, _chs, vol, _jobs = _rows(env)
    assert vol["status"] == "downloaded"
    kept = [n for n in os.listdir(env["lib"]) if n.startswith(volume_assembly.STAGE_PREFIX)]
    assert len(kept) == 1
    assert os.listdir(env["lib"] / kept[0]) == ["foreign"]
    assert "retained for review" in caplog.text


def test_stage_is_private_to_rescan_inventory():
    import rescan
    import volume_assembly

    assert rescan._is_private_inventory_directory(volume_assembly.STAGE_PREFIX + "x.cbz")
