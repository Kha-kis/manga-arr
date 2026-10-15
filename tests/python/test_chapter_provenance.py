"""Provenance for managed Suwayomi chapter copies (issue #422, part 1).

Ownership evidence is recorded only for a library copy the chapter import
itself created and verified, from a chapter selected by its own Suwayomi
identity, with the full client/manga/source/chapter context known. Nothing
here cleans anything up. Coverage:
  - a new verified copy records full context and a content fingerprint
  - reused destination, source-only path, legacy filename matching, missing
    source context and missing chapter rows record nothing
  - copy, verification and DB failures roll back only the copy this import
    created and leave chapter state and Suwayomi downloads untouched
  - repeated imports do not re-record; a change after verification voids it
"""
import asyncio
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


_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63f8cf000000030001fe79bff70000000049454e44ae42"
    "6082"
)
SOURCE_ID = "2131019126180322627"
CHAPTER_ID = 4242
CHAPTER_NAME = "Episode 12"
SOURCE_FILE = f"Scans_{CHAPTER_NAME}.cbz"


def _make_cbz(path, page_count=3):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for i in range(page_count):
            zf.writestr(f"{i + 1:04d}.png", _TINY_PNG)
    return str(path)


def _sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


@pytest.fixture
def owned_copy():
    import chapter_provenance

    owner = chapter_provenance._OwnedCopy()
    try:
        yield owner
    finally:
        owner.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    import main
    import security
    import shared

    db = tempfile.NamedTemporaryFile(suffix=".db", dir=tmp_path, delete=False)
    db.close()
    os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-provenance-keys-")
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
    state = {"library": True}

    def _fake_dir(_db, sid):
        if not state["library"]:
            return None
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
            "INSERT INTO series(id, title, search_pattern)"
            " VALUES(7, 'Freezing', 'Freezing')"
        )
    manga_dir = swy_root / "mangas" / "Weeb Central (EN)" / "Freezing"
    source = _make_cbz(manga_dir / SOURCE_FILE)
    try:
        yield {
            "db": db.name,
            "lib": lib_root / "series-7",
            "source": source,
            "source_sha": _sha(source),
            "swy_root": swy_root,
            "state": state,
        }
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db


def _queue(env, *, chapter_row=True, job_id=None):
    with sqlite3.connect(env["db"]) as c:
        if chapter_row:
            c.execute(
                "INSERT OR IGNORE INTO chapters(series_id, chapter_num, status)"
                " VALUES(7, 12.0, 'wanted')"
            )
        c.execute(
            "INSERT INTO suwayomi_downloads"
            " (series_id, suwayomi_manga_id, chapter_ids, chapter_num, status,"
            "  progress, total, created_at)"
            " VALUES(7, 99, ?, 12.0, 'queued', 0, 1, datetime('now'))",
            (json.dumps([CHAPTER_ID]),),
        )


def _gql(*, source_id=SOURCE_ID, identified=True):
    async def _fake(_c, _query, _vars=None):
        node = {"id": CHAPTER_ID, "isDownloaded": True, "chapterNumber": 12.0}
        if identified:
            node.update({"name": CHAPTER_NAME, "scanlator": "Scans"})
        source = {"displayName": "Weeb Central (EN)"}
        if source_id is not None:
            source["id"] = source_id
        return {"manga": {"title": "Freezing", "source": source,
                          "chapters": {"nodes": [node]}}}
    return _fake


def _client(env):
    return {"id": 1, "type": "suwayomi", "host": "http://user:secret@Swy.Local:4567/",
            "download_path": str(env["swy_root"])}


def _run(env, **gql_kwargs):
    from routers import suwayomi_ as swy

    with patch.object(swy, "_gql", new=_gql(**gql_kwargs)), \
         patch.object(swy, "get_suwayomi_client", new=lambda _db: _client(env)):
        asyncio.run(swy.check_suwayomi_jobs())


def _rows(env):
    with sqlite3.connect(env["db"]) as c:
        c.row_factory = sqlite3.Row
        prov = [dict(r) for r in c.execute("SELECT * FROM chapter_file_provenance")]
        ch = c.execute(
            "SELECT id, status, import_path FROM chapters WHERE chapter_num=12.0"
        ).fetchone()
        jobs = [dict(r) for r in c.execute(
            "SELECT status, error FROM suwayomi_downloads ORDER BY id")]
    return prov, (dict(ch) if ch else None), jobs


def _library_copy(env):
    return str(env["lib"] / "Freezing Ch012.cbz")


def _source_untouched(env):
    return os.path.exists(env["source"]) and _sha(env["source"]) == env["source_sha"]


def _fail_settlement(monkeypatch, before_raise=lambda: None):
    """Fail the settlement transaction, not the unit's pre-copy check.

    Each attempt calls the availability check twice: once before the copy and
    once inside the settlement transaction. Only the second call fails.
    """
    import routers.suwayomi_ as swy

    calls = {"n": 0}

    def _fail(*a, **k):
        calls["n"] += 1
        if calls["n"] % 2 == 0:
            before_raise()
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(swy, "assert_library_file_mutation_available", _fail)
    return calls


# ── recorded ────────────────────────────────────────────────────────────────

def test_new_verified_copy_records_full_context_and_fingerprint(env):
    _queue(env)
    _run(env)
    prov, ch, jobs = _rows(env)
    dest = _library_copy(env)
    assert jobs[0]["status"] == "completed"
    assert ch["status"] == "downloaded" and ch["import_path"] == dest
    assert len(prov) == 1
    row = prov[0]
    st = os.stat(dest)
    assert row["chapter_id"] == ch["id"]
    assert row["library_path"] == dest
    assert row["sha256"] == _sha(dest) == env["source_sha"]
    assert (row["size_bytes"], row["file_dev"], row["file_inode"], row["mtime_ns"]) == (
        st.st_size, str(st.st_dev), str(st.st_ino), st.st_mtime_ns)
    assert row["suwayomi_client_id"] == 1
    assert row["suwayomi_server"] == "http://swy.local:4567"
    assert "secret" not in row["suwayomi_server"]
    assert row["suwayomi_manga_id"] == 99
    assert row["suwayomi_source_id"] == SOURCE_ID
    assert row["suwayomi_chapter_id"] == CHAPTER_ID
    assert row["suwayomi_job_id"] is not None
    assert _source_untouched(env)


# ── not recorded ────────────────────────────────────────────────────────────

def test_reused_destination_gains_no_ownership(env):
    dest = _make_cbz(env["lib"] / "Freezing Ch012.cbz", page_count=5)
    before = _sha(dest)
    _queue(env)
    _run(env)
    prov, ch, jobs = _rows(env)
    assert jobs[0]["status"] == "completed"
    assert ch["import_path"] == dest
    assert prov == []
    assert _sha(dest) == before
    assert _source_untouched(env)


def test_source_only_path_without_library_records_nothing(env):
    env["state"]["library"] = False
    _queue(env)
    _run(env)
    prov, ch, jobs = _rows(env)
    assert jobs[0]["status"] == "completed"
    assert ch["import_path"] == env["source"]
    assert prov == []
    assert _source_untouched(env)


def test_legacy_filename_match_records_nothing(env):
    # Without name/scanlator the importer falls back to filename matching.
    legacy = os.path.join(os.path.dirname(env["source"]), "Vol.1 Ch.12.cbz")
    os.replace(env["source"], legacy)
    _queue(env)
    _run(env, identified=False)
    prov, ch, jobs = _rows(env)
    assert jobs[0]["status"] == "completed"
    assert ch["status"] == "downloaded"
    assert prov == []


def test_missing_source_identity_records_nothing(env):
    _queue(env)
    _run(env, source_id=None)
    prov, ch, jobs = _rows(env)
    assert jobs[0]["status"] == "completed"
    assert ch["import_path"] == _library_copy(env)
    assert prov == []


def test_missing_chapter_row_records_nothing(env):
    _queue(env, chapter_row=False)
    _run(env)
    prov, ch, jobs = _rows(env)
    assert jobs[0]["status"] == "completed"
    assert ch is None
    assert prov == []


def test_oversized_copy_imports_without_evidence(env, monkeypatch):
    import chapter_provenance

    monkeypatch.setattr(chapter_provenance, "FINGERPRINT_MAX_BYTES", 10)
    _queue(env)
    _run(env)
    prov, ch, jobs = _rows(env)
    assert jobs[0]["status"] == "completed"
    assert _sha(_library_copy(env)) == env["source_sha"]
    assert prov == []


# ── failures roll back only what this import created ────────────────────────

def test_copy_failure_removes_partial_copy_and_settles_nothing(env, monkeypatch):
    import chapter_provenance

    def _boom(fd):
        raise OSError("disk full")

    monkeypatch.setattr(chapter_provenance.os, "fsync", _boom)
    _queue(env)
    _run(env)
    prov, ch, jobs = _rows(env)
    assert jobs[0]["status"] == "error"
    assert ch["status"] == "wanted" and ch["import_path"] is None
    assert not os.path.exists(_library_copy(env))
    assert prov == []
    assert _source_untouched(env)


def test_verification_failure_removes_copy(env, monkeypatch):
    import chapter_provenance

    def _bad(path):
        raise chapter_provenance.ProvenanceError("changed while hashing")

    monkeypatch.setattr(chapter_provenance, "fingerprint", _bad)
    _queue(env)
    _run(env)
    prov, ch, jobs = _rows(env)
    assert jobs[0]["status"] == "error"
    assert ch["status"] == "wanted"
    assert not os.path.exists(_library_copy(env))
    assert prov == []
    assert _source_untouched(env)


def test_db_failure_rolls_back_copy_and_state_then_retry_records(env, monkeypatch):
    import chapter_provenance

    real_record = chapter_provenance.record

    def _fail(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(chapter_provenance, "record", _fail)
    _queue(env)
    _run(env)
    prov, ch, jobs = _rows(env)
    assert jobs[0]["status"] == "error"
    assert ch["status"] == "wanted" and ch["import_path"] is None
    assert not os.path.exists(_library_copy(env))
    assert prov == []
    assert _source_untouched(env)

    monkeypatch.setattr(chapter_provenance, "record", real_record)
    _queue(env)
    _run(env)
    prov, ch, jobs = _rows(env)
    assert jobs[-1]["status"] == "completed"
    assert ch["status"] == "downloaded"
    assert len(prov) == 1 and prov[0]["sha256"] == env["source_sha"]


def test_db_failure_never_removes_a_reused_destination(env, monkeypatch):
    dest = _make_cbz(env["lib"] / "Freezing Ch012.cbz", page_count=5)
    before = _sha(dest)

    # Force the settlement transaction itself to fail for a reused file.
    calls = _fail_settlement(monkeypatch)
    _queue(env)
    _run(env)
    prov, _ch, jobs = _rows(env)
    assert calls["n"] == 6
    assert jobs[0]["status"] == "error"
    assert os.path.exists(dest) and _sha(dest) == before
    assert prov == []


# ── repeated import and late change ─────────────────────────────────────────

def test_repeated_import_does_not_rerecord(env):
    _queue(env)
    _run(env)
    first, _ch, _jobs = _rows(env)
    _queue(env)
    _run(env)
    second, ch, jobs = _rows(env)
    assert [j["status"] for j in jobs] == ["completed", "completed"]
    assert len(second) == 1
    assert second[0]["id"] == first[0]["id"]
    assert second[0]["suwayomi_job_id"] == first[0]["suwayomi_job_id"]
    assert ch["import_path"] == _library_copy(env)


def test_change_after_copy_before_commit_records_nothing(env, monkeypatch):
    import routers.suwayomi_ as swy

    real = swy._chapter_ownership_evidence

    def _tamper(c, job, chapter_ids, chapter_import, source_id):
        with open(chapter_import.path, "ab") as fh:
            fh.write(b"x")
        return real(c, job, chapter_ids, chapter_import, source_id)

    monkeypatch.setattr(swy, "_chapter_ownership_evidence", _tamper)
    _queue(env)
    _run(env)
    prov, ch, jobs = _rows(env)
    assert jobs[0]["status"] == "completed"
    assert ch["status"] == "downloaded"
    assert prov == []
    assert os.path.exists(_library_copy(env))


def test_change_after_commit_voids_the_record(env, monkeypatch):
    import chapter_provenance

    monkeypatch.setattr(chapter_provenance, "same_file", lambda path, ev: False)
    _queue(env)
    _run(env)
    prov, ch, jobs = _rows(env)
    assert jobs[0]["status"] == "completed"
    assert ch["status"] == "downloaded"
    assert prov == []


# ── helpers ─────────────────────────────────────────────────────────────────

def test_copy_new_file_refuses_existing_destination(tmp_path):
    import chapter_provenance

    src = _make_cbz(tmp_path / "src.cbz")
    dst = _make_cbz(tmp_path / "dst.cbz", page_count=7)
    before = _sha(dst)
    with pytest.raises(FileExistsError):
        chapter_provenance.copy_new_file(src, dst)
    assert _sha(dst) == before


def test_remove_if_unchanged_keeps_a_replaced_file(tmp_path, owned_copy):
    import chapter_provenance

    src = _make_cbz(tmp_path / "src.cbz")
    dst = str(tmp_path / "dst.cbz")
    evidence, verified = chapter_provenance.copy_new_file(src, dst, _owner=owned_copy)
    assert verified
    os.unlink(dst)
    _make_cbz(dst, page_count=9)
    assert chapter_provenance.remove_if_unchanged(dst, evidence, _owner=owned_copy) is False
    assert os.path.exists(dst)


@pytest.mark.parametrize(
    "client,expected",
    [
        ({"id": 1, "host": "http://user:pw@Swy.Local:4567/"}, "http://swy.local:4567"),
        ({"id": 1, "host": "https://swy.example/base/?x=1"}, "https://swy.example/base"),
        ({"id": 1, "host": "http://swy", "port": 4567}, "http://swy:4567"),
        ({"id": 1, "host": "http://[2001:DB8::1]:80"}, "http://[2001:db8::1]:80"),
        ({"id": 1, "host": "http://[2001:db8::1:80]"}, "http://[2001:db8::1:80]"),
    ],
)
def test_suwayomi_server_identity_has_no_credentials(client, expected):
    import chapter_provenance

    assert chapter_provenance.suwayomi_server(client) == expected


@pytest.mark.parametrize(
    "client,manga,source,chapter",
    [
        (None, 1, "s", 1),
        ({"id": 0, "host": "http://swy"}, 1, "s", 1),
        ({"id": 1, "host": "http://swy"}, None, "s", 1),
        ({"id": 1, "host": "http://swy"}, 1, "", 1),
        ({"id": 1, "host": "http://swy"}, 1, None, 1),
        ({"id": 1, "host": "http://swy"}, 1, "s", True),
        ({"id": 1, "host": ""}, 1, "s", 1),
    ],
)
def test_incomplete_source_identity_is_refused(client, manga, source, chapter):
    import chapter_provenance

    assert chapter_provenance.source_identity(client, manga, source, chapter) is None


# ── review follow-ups ───────────────────────────────────────────────────────

def test_malformed_client_url_never_fails_the_import(env):
    from routers import suwayomi_ as swy

    _queue(env)
    bad = {"id": 1, "type": "suwayomi", "host": "http://swy.local:notaport",
           "download_path": str(env["swy_root"])}
    with patch.object(swy, "_gql", new=_gql()), \
         patch.object(swy, "get_suwayomi_client", new=lambda _db: bad):
        asyncio.run(swy.check_suwayomi_jobs())
    prov, ch, jobs = _rows(env)
    assert jobs[0]["status"] == "completed"
    assert ch["status"] == "downloaded" and ch["import_path"] == _library_copy(env)
    assert prov == []


def test_source_changing_during_copy_is_refused(env, monkeypatch):
    import chapter_provenance

    real = chapter_provenance._write_all
    calls = {"n": 0}

    def _grow_source(fd, chunk):
        calls["n"] += 1
        if calls["n"] == 1:
            # Suwayomi rewrites bytes the copy has already read.
            with open(env["source"], "r+b") as fh:
                fh.write(b"X")
        real(fd, chunk)

    monkeypatch.setattr(chapter_provenance, "_write_all", _grow_source)
    _queue(env)
    _run(env)
    prov, ch, jobs = _rows(env)
    # The torn attempt is refused and removed; the retry copies the settled file.
    assert calls["n"] >= 2
    assert jobs[0]["status"] == "completed"
    assert len(prov) == 1
    assert prov[0]["sha256"] == _sha(env["source"]) == _sha(_library_copy(env))
    assert prov[0]["sha256"] != env["source_sha"]


def test_oversized_copy_is_rolled_back_on_db_failure(env, monkeypatch):
    import chapter_provenance

    monkeypatch.setattr(chapter_provenance, "FINGERPRINT_MAX_BYTES", 10)
    copied = []
    calls = _fail_settlement(
        monkeypatch, lambda: copied.append(os.path.exists(_library_copy(env))))
    _queue(env)
    _run(env)
    prov, ch, jobs = _rows(env)
    assert calls["n"] == 6 and copied == [True, True, True]
    assert jobs[0]["status"] == "error"
    assert ch["status"] == "wanted"
    assert not os.path.exists(_library_copy(env))
    assert prov == []
    assert _source_untouched(env)


def test_empty_placeholder_overwrite_records_nothing(env):
    env["lib"].mkdir(exist_ok=True)
    open(_library_copy(env), "wb").close()
    _queue(env)
    _run(env)
    prov, ch, jobs = _rows(env)
    assert jobs[0]["status"] == "completed"
    assert _sha(_library_copy(env)) == env["source_sha"]
    assert prov == []


def test_destination_created_by_another_writer_is_reused(env, monkeypatch):
    import chapter_provenance

    def _race(src, dst, **_kwargs):
        _make_cbz(dst, page_count=6)
        raise FileExistsError(dst)

    monkeypatch.setattr(chapter_provenance, "copy_new_file", _race)
    _queue(env)
    _run(env)
    prov, ch, jobs = _rows(env)
    assert jobs[0]["status"] == "completed"
    assert ch["import_path"] == _library_copy(env)
    assert os.path.exists(_library_copy(env))
    assert prov == []


def test_revalidation_failure_alone_keeps_import_without_evidence(env, monkeypatch):
    import chapter_provenance

    real = chapter_provenance.fingerprint
    calls = {"n": 0}

    def _second_fails(path):
        calls["n"] += 1
        if calls["n"] == 2:  # 1 = copy verification, 2 = pre-commit revalidation
            raise chapter_provenance.ProvenanceError("changed while hashing")
        return real(path)

    monkeypatch.setattr(chapter_provenance, "fingerprint", _second_fails)
    _queue(env)
    _run(env)
    prov, ch, jobs = _rows(env)
    assert calls["n"] == 2
    assert jobs[0]["status"] == "completed"
    assert ch["import_path"] == _library_copy(env)
    assert os.path.exists(_library_copy(env))
    assert prov == []


def test_downloaded_chapter_with_other_path_keeps_old_file(env):
    old = _make_cbz(env["lib"] / "old Ch012.cbz", page_count=4)
    before = _sha(old)
    with sqlite3.connect(env["db"]) as c:
        c.execute(
            "INSERT INTO chapters(series_id, chapter_num, status, import_path)"
            " VALUES(7, 12.0, 'downloaded', ?)", (old,)
        )
    _queue(env)
    _run(env)
    prov, ch, jobs = _rows(env)
    assert jobs[0]["status"] == "completed"
    assert ch["import_path"] == _library_copy(env)
    assert len(prov) == 1 and prov[0]["library_path"] == _library_copy(env)
    assert os.path.exists(old) and _sha(old) == before


# ── second review ───────────────────────────────────────────────────────────

def _swap_destination_on_first_write(monkeypatch, dst, replace):
    import chapter_provenance

    real = chapter_provenance._write_all
    calls = {"n": 0}

    def _swap(fd, chunk):
        calls["n"] += 1
        if calls["n"] == 1:
            os.unlink(dst)
            replace(dst)
        real(fd, chunk)

    monkeypatch.setattr(chapter_provenance, "_write_all", _swap)
    return calls


@pytest.mark.parametrize("limit", [10, 4 * 1024 * 1024 * 1024])
def test_copy_never_writes_a_regular_file_swapped_in(tmp_path, monkeypatch, limit):
    import chapter_provenance

    monkeypatch.setattr(chapter_provenance, "FINGERPRINT_MAX_BYTES", limit)
    src = _make_cbz(tmp_path / "src.cbz")
    dst = str(tmp_path / "dst.cbz")
    _swap_destination_on_first_write(
        monkeypatch, dst, lambda p: _make_cbz(p, page_count=6))
    with pytest.raises(chapter_provenance.ProvenanceError):
        chapter_provenance.copy_new_file(src, dst)
    assert _sha(dst) == _sha(_make_cbz(tmp_path / "ref.cbz", page_count=6))


@pytest.mark.parametrize("limit", [10, 4 * 1024 * 1024 * 1024])
def test_copy_never_writes_through_a_symlink_swapped_in(tmp_path, monkeypatch, limit):
    import chapter_provenance

    monkeypatch.setattr(chapter_provenance, "FINGERPRINT_MAX_BYTES", limit)
    src = _make_cbz(tmp_path / "src.cbz")
    dst = str(tmp_path / "dst.cbz")
    target = _make_cbz(tmp_path / "target.cbz", page_count=8)
    before = _sha(target)
    _swap_destination_on_first_write(monkeypatch, dst, lambda p: os.symlink(target, p))
    with pytest.raises(chapter_provenance.ProvenanceError):
        chapter_provenance.copy_new_file(src, dst)
    assert os.path.islink(dst)
    assert _sha(target) == before


def test_oversized_settlement_failure_keeps_a_swapped_in_file(env, monkeypatch):
    import chapter_provenance

    monkeypatch.setattr(chapter_provenance, "FINGERPRINT_MAX_BYTES", 10)
    dest = _library_copy(env)
    foreign = {}

    def _swap():
        if not foreign:
            os.unlink(dest)
            foreign["sha"] = _sha(_make_cbz(dest, page_count=6))

    _fail_settlement(monkeypatch, _swap)
    _queue(env)
    _run(env)
    prov, ch, jobs = _rows(env)
    assert jobs[0]["status"] == "error"
    assert ch["status"] == "wanted"
    assert foreign and os.path.exists(dest) and _sha(dest) == foreign["sha"]
    assert prov == []
    assert _source_untouched(env)


def test_unsigned_64bit_inode_is_recorded_losslessly(env, monkeypatch):
    import chapter_provenance

    real = chapter_provenance._identity

    def _nfs_fileid(st):
        dev, inode, size, mtime_ns = real(st)
        return dev, inode + 2**63, size, mtime_ns

    monkeypatch.setattr(chapter_provenance, "_identity", _nfs_fileid)
    _queue(env)
    _run(env)
    prov, ch, jobs = _rows(env)
    assert jobs[0]["status"] == "completed"
    assert ch["status"] == "downloaded" and ch["import_path"] == _library_copy(env)
    assert len(prov) == 1
    assert prov[0]["file_inode"] == str(os.stat(_library_copy(env)).st_ino + 2**63)


def test_fingerprint_stops_reading_at_its_bound(tmp_path, monkeypatch):
    import chapter_provenance

    monkeypatch.setattr(chapter_provenance, "FINGERPRINT_MAX_BYTES", 8)
    monkeypatch.setattr(chapter_provenance, "_HASH_CHUNK_SIZE", 4)
    path = tmp_path / "growing.bin"
    path.write_bytes(b"abcd")
    real_read = os.read
    consumed = {"n": 0}

    def _read(fd, n):
        data = real_read(fd, n)
        if consumed["n"] == 0:
            with open(path, "ab") as fh:
                fh.write(b"x" * 1024)
        consumed["n"] += len(data)
        return data

    monkeypatch.setattr(chapter_provenance.os, "read", _read)
    with pytest.raises(chapter_provenance.ProvenanceError):
        chapter_provenance.fingerprint(str(path))
    assert consumed["n"] <= 9


def test_db_replaced_during_ownership_evidence_never_receives_settlement(env, monkeypatch):
    import routers.suwayomi_ as swy
    from file_mutation_lock import FileMutationLockError, file_mutation_guard

    _queue(env)
    foreign = env["db"] + ".foreign"
    original = env["db"] + ".original"
    with sqlite3.connect(env["db"]) as db, sqlite3.connect(foreign) as other:
        db.backup(other)
        other.execute("CREATE TABLE foreign_marker(value TEXT)")
        other.execute("INSERT INTO foreign_marker VALUES('must remain unchanged')")
        db.row_factory = sqlite3.Row
        job = dict(db.execute("SELECT * FROM suwayomi_downloads").fetchone())
    foreign_sha = _sha(foreign)
    real = swy._chapter_ownership_evidence
    observed = []

    def replace_after_hash(*args):
        result = real(*args)
        assert result[0] is not None
        os.replace(env["db"], original)
        os.replace(foreign, env["db"])
        observed.append(True)
        return result

    monkeypatch.setattr(swy, "_chapter_ownership_evidence", replace_after_hash)
    with pytest.raises(FileMutationLockError):
        with file_mutation_guard(env["db"]) as guard:
            swy._complete_suwayomi_job_files(
                _client(env), job, [CHAPTER_ID],
                {CHAPTER_ID: {"name": CHAPTER_NAME, "scanlator": "Scans",
                              "chapterNumber": 12.0, "isDownloaded": True}},
                "Freezing", guard, source_display_name="Weeb Central (EN)",
                source_id=SOURCE_ID,
            )
    assert observed == [True]
    assert _sha(env["db"]) == foreign_sha
    prov, chapter, jobs = _rows(env)
    assert chapter is not None
    assert prov == [] and chapter["status"] == "wanted"
    assert jobs[0]["status"] == "queued"
    with sqlite3.connect(original) as db:
        assert db.execute("SELECT status FROM chapters").fetchone()[0] == "wanted"
        assert db.execute("SELECT COUNT(*) FROM chapter_file_provenance").fetchone()[0] == 0
    assert not os.path.lexists(_library_copy(env))
    assert _source_untouched(env)


@pytest.mark.parametrize("cleanup", ["copy_failure", "settlement_failure"])
@pytest.mark.parametrize("replacement", ["regular", "symlink"])
@pytest.mark.parametrize("limit", [10, 10_000_000])
def test_rollback_preserves_entry_replaced_at_capture_boundary(
    tmp_path, monkeypatch, cleanup, replacement, limit, owned_copy,
):
    import chapter_provenance as provenance

    src = _make_cbz(tmp_path / "source.cbz")
    dst = str(tmp_path / "copy.cbz")
    displaced = str(tmp_path / "displaced-owned.cbz")
    foreign = tmp_path / "foreign.cbz"
    foreign.write_bytes(b"unrelated file must survive")
    foreign_bytes = foreign.read_bytes()
    monkeypatch.setattr(provenance, "FINGERPRINT_MAX_BYTES", limit)
    evidence = None
    if cleanup == "settlement_failure":
        evidence, _verified = provenance.copy_new_file(src, dst, _owner=owned_copy)
    else:
        real_write = provenance._write_all

        def fail_after_write(fd, data):
            real_write(fd, data)
            raise OSError("injected copy failure")

        monkeypatch.setattr(provenance, "_write_all", fail_after_write)

    real_rename, real_unlink = os.rename, os.unlink
    swapped = []

    def swap_public_entry(path, parent_fd=None):
        candidate = os.fspath(path)
        if parent_fd is not None and not os.path.isabs(candidate):
            candidate = os.path.join(os.readlink(f"/proc/self/fd/{parent_fd}"), candidate)
        if candidate == dst and not swapped:
            os.replace(dst, displaced)
            if replacement == "symlink":
                os.symlink(foreign, dst)
            else:
                os.replace(foreign, dst)
            swapped.append(True)

    def rename(source, destination, *args, **kwargs):
        swap_public_entry(source, kwargs.get("src_dir_fd"))
        return real_rename(source, destination, *args, **kwargs)

    def unlink(path, *args, **kwargs):
        swap_public_entry(path, kwargs.get("dir_fd"))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(provenance.os, "rename", rename)
    monkeypatch.setattr(provenance.os, "unlink", unlink)
    if cleanup == "copy_failure":
        with pytest.raises(OSError, match="injected copy failure"):
            provenance.copy_new_file(src, dst)
    else:
        assert evidence is not None
        assert provenance.remove_if_unchanged(dst, evidence, _owner=owned_copy) is False
    assert swapped == [True]
    assert os.path.lexists(dst)
    if replacement == "symlink":
        assert os.path.islink(dst) and os.readlink(dst) == str(foreign)
        assert foreign.read_bytes() == foreign_bytes
    else:
        with open(dst, "rb") as fh:
            assert fh.read() == foreign_bytes
    assert _sha(src) == _sha(displaced)


@pytest.mark.parametrize("replacement", ["regular", "symlink"])
@pytest.mark.parametrize("refusal", ["collision", "unsupported"])
def test_rollback_retains_captured_foreign_entry_when_restore_refused(
    tmp_path, monkeypatch, caplog, replacement, refusal, owned_copy,
):
    import errno
    import chapter_provenance as provenance
    import import_publication

    src = _make_cbz(tmp_path / "source.cbz")
    dst = tmp_path / "copy.cbz"
    evidence, _verified = provenance.copy_new_file(src, str(dst), _owner=owned_copy)
    foreign = tmp_path / "foreign.cbz"
    foreign.write_bytes(b"captured foreign bytes")
    displaced = tmp_path / "displaced-owned.cbz"
    real_rename = os.rename
    captured = []

    def rename(source, destination, *args, **kwargs):
        candidate = os.fspath(source)
        if "src_dir_fd" in kwargs:
            candidate = os.path.join(
                os.readlink(f"/proc/self/fd/{kwargs['src_dir_fd']}"), candidate,
            )
        if candidate == str(dst) and not captured:
            os.replace(dst, displaced)
            if replacement == "symlink":
                os.symlink(foreign, dst)
            else:
                os.replace(foreign, dst)
            result = real_rename(source, destination, *args, **kwargs)
            captured.append(True)
            if refusal == "collision":
                dst.write_bytes(b"new public occupant")
            return result
        return real_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(provenance.os, "rename", rename)
    if refusal == "unsupported":
        def unsupported(source, destination):
            raise OSError(errno.EOPNOTSUPP, "no atomic restore")

        monkeypatch.setattr(import_publication, "_rename_noreplace", unsupported)
    assert provenance.remove_if_unchanged(str(dst), evidence, _owner=owned_copy) is False
    assert captured == [True]
    if refusal == "collision":
        assert dst.read_bytes() == b"new public occupant"
    else:
        assert not os.path.lexists(dst)
    carriers = list(tmp_path.glob(".mangarr-claim-chapter-rollback-*"))
    assert len(carriers) == 1
    artifact = carriers[0] / "artifact"
    if replacement == "symlink":
        assert artifact.is_symlink() and os.readlink(artifact) == str(foreign)
        assert foreign.read_bytes() == b"captured foreign bytes"
    else:
        assert artifact.read_bytes() == b"captured foreign bytes"
    receipt = json.loads((carriers[0] / "receipt.json").read_text())
    assert receipt["origin"] == str(dst)
    assert receipt["expected"][:2] == [evidence.dev, evidence.inode]
    assert str(carriers[0]) in caplog.text
    assert _sha(src) == _sha(displaced)


def test_partial_copy_with_refused_rollback_is_never_reused_on_retry(env, monkeypatch):
    import chapter_provenance as provenance

    real_write, real_fsync = provenance._write_all, os.fsync
    writes = []

    def partial_write(fd, chunk):
        real_write(fd, chunk[:1])
        writes.append(True)
        raise OSError("injected partial write")

    def refuse_rollback_barrier(fd):
        if writes:
            raise OSError("injected rollback barrier failure")
        real_fsync(fd)

    monkeypatch.setattr(provenance, "_write_all", partial_write)
    monkeypatch.setattr(provenance.os, "fsync", refuse_rollback_barrier)
    _queue(env)
    _run(env)
    assert writes
    prov, chapter, jobs = _rows(env)
    assert chapter is not None
    assert jobs[0]["status"] == "error"
    assert chapter["status"] == "wanted" and chapter["import_path"] is None
    assert prov == []
    with open(_library_copy(env), "rb") as partial, open(env["source"], "rb") as source:
        assert partial.read() == source.read(1)

    # Even after the transient barrier error ends, the failed public copy is
    # not a reusable destination; recovery of the pending receipt is explicit.
    monkeypatch.setattr(provenance, "_write_all", real_write)
    monkeypatch.setattr(provenance.os, "fsync", real_fsync)
    _queue(env)
    _run(env)
    prov, chapter, jobs = _rows(env)
    assert chapter is not None
    assert [job["status"] for job in jobs] == ["error", "error"]
    assert chapter["status"] == "wanted" and chapter["import_path"] is None
    assert prov == [] and _source_untouched(env)


@pytest.mark.parametrize("helper", ["pending", "rollback"])
@pytest.mark.parametrize("failure", ["existing_receipt", "chmod_refusal", "nonprivate_mode"])
def test_initialization_refusal_preserves_unknown_receipt_and_directory(
    tmp_path, monkeypatch, helper, failure, owned_copy,
):
    import errno
    import chapter_provenance as provenance

    foreign = tmp_path / "preexisting-directory"
    foreign.mkdir(mode=0o700)
    if failure == "nonprivate_mode":
        foreign.chmod(0o775)
    original_mode = foreign.stat().st_mode
    receipt_bytes = b"unrelated existing recovery authority"
    (foreign / "receipt.json").write_bytes(receipt_bytes)
    (foreign / "keep.bin").write_bytes(b"unrelated existing content")
    destination = tmp_path / "copy.cbz"
    source = tmp_path / "original-source.cbz"
    source.write_bytes(b"owned copy remains untouched")
    evidence, _verified = provenance.copy_new_file(str(source), str(destination), _owner=owned_copy)
    displaced = tmp_path / "displaced-fresh-directory"
    prefix = f".mangarr-claim-chapter-{helper}-"
    real_open, real_chmod = os.open, os.fchmod
    opened = []

    def replace_before_open(path, flags, *args, **kwargs):
        if isinstance(path, str) and path.startswith(prefix) and flags & os.O_DIRECTORY and not opened:
            public = tmp_path / path
            os.rename(public, displaced)
            os.rename(foreign, public)
            opened.append(public)
        return real_open(path, flags, *args, **kwargs)

    def chmod(fd, mode):
        if opened and failure == "chmod_refusal":
            raise PermissionError(errno.EPERM, "injected initialization chmod refusal")
        return real_chmod(fd, mode)

    monkeypatch.setattr(provenance.os, "open", replace_before_open)
    monkeypatch.setattr(provenance.os, "fchmod", chmod)
    error = None
    try:
        if helper == "pending":
            with provenance._pending_copy(str(destination)):
                pytest.fail("initialization unexpectedly accepted foreign receipt")
        else:
            assert provenance.remove_if_unchanged(str(destination), evidence, _owner=owned_copy) is False
    except (OSError, provenance.ProvenanceError) as exc:
        error = exc
    assert len(opened) == 1
    assert (opened[0] / "receipt.json").read_bytes() == receipt_bytes
    assert (opened[0] / "keep.bin").read_bytes() == b"unrelated existing content"
    assert opened[0].stat().st_mode == original_mode
    assert sorted(path.name for path in opened[0].iterdir()) == ["keep.bin", "receipt.json"]
    assert list(displaced.iterdir()) == []
    assert destination.read_bytes() == b"owned copy remains untouched"
    if helper == "pending":
        # The created-directory snapshot now permits refusal before chmod or
        # exclusive receipt creation; cleanup must preserve that root error.
        assert isinstance(error, provenance.ProvenanceError)
        assert "private directory identity/permissions changed" in str(error)
    else:
        assert error is None


@pytest.mark.parametrize("error_name", ["EIO", "EROFS"])
def test_rollback_closes_directory_fds_when_owned_receipt_cleanup_fails(
    tmp_path, monkeypatch, error_name, owned_copy,
):
    import errno
    import chapter_provenance as provenance

    destination = tmp_path / "owned-copy.cbz"
    source = tmp_path / "source.cbz"
    source.write_bytes(b"owned copy bytes")
    evidence, _verified = provenance.copy_new_file(str(source), str(destination), _owner=owned_copy)
    real_open, real_unlink = os.open, os.unlink
    opened = []
    error_number = getattr(errno, error_name)

    def record_directory_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if flags & os.O_DIRECTORY:
            opened.append(fd)
        return fd

    def refuse_receipt_cleanup(path, *args, **kwargs):
        if path == "receipt.json":
            raise OSError(error_number, "injected owned receipt cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(provenance.os, "open", record_directory_open)
    monkeypatch.setattr(provenance.os, "unlink", refuse_receipt_cleanup)
    try:
        with pytest.raises(OSError) as error:
            provenance.remove_if_unchanged(str(destination), evidence, _owner=owned_copy)
        assert error.value.errno == error_number
        assert len(opened) == 2
        still_open = []
        for fd in opened:
            try:
                os.fstat(fd)
            except OSError as exc:
                assert exc.errno == errno.EBADF
            else:
                still_open.append(fd)
        assert still_open == [], "receipt cleanup leaked parent/carrier directory FDs"
        assert not destination.exists()
        carriers = list(tmp_path.glob(".mangarr-claim-chapter-rollback-*"))
        assert len(carriers) == 1 and (carriers[0] / "receipt.json").is_file()
        assert not (carriers[0] / "artifact").exists()
    finally:
        # Close descriptors leaked by the RED baseline, never leave QA leaks.
        for fd in opened:
            try:
                os.close(fd)
            except OSError:
                pass


@pytest.mark.parametrize("failure,limit", [("stream", 10), ("stream", 10_000_000), ("verification", 10_000_000)])
def test_created_inode_stays_pinned_through_failure_capture(tmp_path, monkeypatch, failure, limit):
    import errno
    import chapter_provenance as provenance

    source = _make_cbz(tmp_path / "source.cbz")
    destination = str(tmp_path / "copy.cbz")
    monkeypatch.setattr(provenance, "FINGERPRINT_MAX_BYTES", limit)
    real_open, real_capture = os.open, provenance._remove_captured
    created = []
    observed = []

    def open_file(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if os.fspath(path) == destination and flags & os.O_CREAT:
            created.append((fd, os.fstat(fd)))
        return fd

    def capture(*args, **kwargs):
        fd, original = created[0]
        try:
            held = os.fstat(fd)
            observed.append((held.st_dev, held.st_ino) == (original.st_dev, original.st_ino))
        except OSError:
            observed.append(False)
        return real_capture(*args, **kwargs)

    def fail(*args):
        raise provenance.ProvenanceError("injected copy boundary failure")

    monkeypatch.setattr(provenance.os, "open", open_file)
    monkeypatch.setattr(provenance, "_remove_captured", capture)
    monkeypatch.setattr(provenance, "_write_all" if failure == "stream" else "fingerprint", fail)
    with pytest.raises(provenance.ProvenanceError, match="copy boundary failure"):
        provenance.copy_new_file(source, destination)
    assert observed == [True], "created inode was unpinned before rollback capture"
    assert not os.path.lexists(destination)
    with pytest.raises(OSError) as closed:
        os.fstat(created[0][0])
    assert closed.value.errno == errno.EBADF


def test_unpinned_file_evidence_never_authorizes_deletion(tmp_path):
    import chapter_provenance as provenance

    source = _make_cbz(tmp_path / "source.cbz")
    destination = str(tmp_path / "copy.cbz")
    evidence, _verified = provenance.copy_new_file(source, destination)
    before = _sha(destination)
    assert provenance.remove_if_unchanged(destination, evidence) is False
    assert _sha(destination) == before


@pytest.mark.parametrize("settlement_failure", [False, True])
@pytest.mark.parametrize("limit", [10, 10_000_000])
def test_route_pins_created_inode_until_settlement_then_closes(env, monkeypatch, settlement_failure, limit):
    import errno
    import chapter_provenance as provenance
    import routers.suwayomi_ as swy

    destination = _library_copy(env)
    monkeypatch.setattr(provenance, "FINGERPRINT_MAX_BYTES", limit)
    real_open, real_evidence, real_record = os.open, swy._chapter_ownership_evidence, provenance.record
    created = []
    observed = []

    def open_file(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if os.fspath(path) == destination and flags & os.O_CREAT:
            created.append((fd, os.fstat(fd)))
        return fd

    def observe():
        fd, original = created[-1]
        try:
            held = os.fstat(fd)
            observed.append((held.st_dev, held.st_ino) == (original.st_dev, original.st_ino))
        except OSError:
            observed.append(False)

    def evidence(*args):
        observe()
        return real_evidence(*args)

    def record(*args, **kwargs):
        observe()
        return real_record(*args, **kwargs)

    monkeypatch.setattr(provenance.os, "open", open_file)
    monkeypatch.setattr(swy, "_chapter_ownership_evidence", evidence)
    monkeypatch.setattr(provenance, "record", record)
    if settlement_failure:
        _fail_settlement(monkeypatch, observe)
    _queue(env)
    _run(env)
    assert observed and all(observed), "settlement used tuple evidence after the owned FD closed"
    for fd, _original in created:
        with pytest.raises(OSError) as closed:
            os.fstat(fd)
        assert closed.value.errno == errno.EBADF
    _prov, chapter, jobs = _rows(env)
    assert chapter is not None
    assert jobs[0]["status"] == ("error" if settlement_failure else "completed")
    assert chapter["status"] == ("wanted" if settlement_failure else "downloaded")
    assert _source_untouched(env)


def test_copy_refuses_a_symlink_source_before_any_public_creation(tmp_path):
    import chapter_provenance as provenance

    foreign = _make_cbz(tmp_path / "foreign.cbz")
    source = tmp_path / "selected.cbz"
    source.symlink_to(foreign)
    destination = str(tmp_path / "copy.cbz")
    before = _sha(foreign)
    with pytest.raises((OSError, provenance.ProvenanceError)):
        provenance.copy_new_file(str(source), destination)
    assert not os.path.lexists(destination)
    assert _sha(foreign) == before


def test_source_symlink_swap_after_selection_never_records_foreign_bytes(env, monkeypatch):
    from pathlib import Path
    import routers.suwayomi_ as swy

    source = Path(env["source"])
    held_original = source.with_name("held-original.cbz")
    foreign = source.with_name("foreign.cbz")
    _make_cbz(foreign, page_count=8)
    foreign_sha = _sha(foreign)
    real = swy._source_chapter_cbz
    swapped = []

    def select_then_swap(*args, **kwargs):
        selected = real(*args, **kwargs)
        if selected and not swapped:
            source.rename(held_original)
            source.symlink_to(foreign)
            swapped.append(True)
        return selected

    monkeypatch.setattr(swy, "_source_chapter_cbz", select_then_swap)
    _queue(env)
    _run(env)
    provenance, chapter, jobs = _rows(env)
    assert swapped == [True]
    assert jobs[0]["status"] == "error"
    assert chapter is not None and chapter["status"] == "wanted"
    assert provenance == [] and not os.path.lexists(_library_copy(env))
    assert _sha(held_original) == env["source_sha"]
    assert _sha(foreign) == foreign_sha


def test_copy_size_bound_uses_the_open_source_descriptor(tmp_path, monkeypatch):
    import chapter_provenance as provenance

    source = _make_cbz(tmp_path / "source.cbz")
    destination = str(tmp_path / "copy.cbz")
    real = os.path.getsize
    monkeypatch.setattr(provenance, "FINGERPRINT_MAX_BYTES", 10)
    monkeypatch.setattr(provenance.os.path, "getsize", lambda path: 0 if path == source else real(path))
    evidence, verified = provenance.copy_new_file(source, destination)
    assert verified is False and evidence.sha256 == ""
    assert _sha(destination) == _sha(source)


@pytest.mark.parametrize("kind", ["fifo", "directory"])
def test_nonregular_source_is_refused_without_blocking(tmp_path, kind):
    import subprocess

    source = tmp_path / "selected.cbz"
    destination = tmp_path / "copy.cbz"
    if kind == "fifo":
        os.mkfifo(source)
    else:
        source.mkdir()
    script = """
import sys
import chapter_provenance as provenance
try:
    provenance.copy_new_file(sys.argv[1], sys.argv[2])
except (OSError, provenance.ProvenanceError):
    pass
else:
    raise AssertionError('nonregular source was accepted')
"""
    try:
        result = subprocess.run(
            [sys.executable, "-B", "-c", script, str(source), str(destination)],
            capture_output=True, text=True, timeout=3,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("source open blocked before regular-file validation")
    assert result.returncode == 0, result.stderr
    assert not os.path.lexists(destination)
    assert source.exists()


def test_live_pin_prevents_recreated_entry_from_becoming_owned(tmp_path, owned_copy):
    import chapter_provenance as provenance

    source = _make_cbz(tmp_path / "source.cbz")
    destination = str(tmp_path / "copy.cbz")
    evidence, _verified = provenance.copy_new_file(source, destination, _owner=owned_copy)
    assert provenance.fingerprint(destination) == evidence
    os.unlink(destination)
    foreign_bytes = b"f" * evidence.size
    with open(destination, "wb") as foreign:
        foreign.write(foreign_bytes)
    os.utime(destination, ns=(evidence.mtime_ns, evidence.mtime_ns))
    # The original allocation remains live after unlink; it cannot be reused
    # by the native recreated entry while that descriptor is still held.
    assert owned_copy.matches((evidence.dev, evidence.inode))
    assert os.stat(destination).st_ino != evidence.inode
    assert provenance.remove_if_unchanged(destination, evidence, _owner=owned_copy) is False
    with open(destination, "rb") as foreign:
        assert foreign.read() == foreign_bytes
    assert _sha(source) == evidence.sha256
