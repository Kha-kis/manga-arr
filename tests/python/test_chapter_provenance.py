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
        st.st_size, st.st_dev, st.st_ino, st.st_mtime_ns)
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

    def _fail(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    # Force the settlement transaction itself to fail for a reused file.
    import routers.suwayomi_ as swy
    monkeypatch.setattr(swy, "assert_library_file_mutation_available", _fail)
    _queue(env)
    _run(env)
    prov, _ch, jobs = _rows(env)
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


def test_remove_if_unchanged_keeps_a_replaced_file(tmp_path):
    import chapter_provenance

    src = _make_cbz(tmp_path / "src.cbz")
    dst = str(tmp_path / "dst.cbz")
    evidence, verified = chapter_provenance.copy_new_file(src, dst)
    assert verified
    os.unlink(dst)
    _make_cbz(dst, page_count=9)
    assert chapter_provenance.remove_if_unchanged(dst, evidence) is False
    assert os.path.exists(dst)


@pytest.mark.parametrize(
    "client,expected",
    [
        ({"id": 1, "host": "http://user:pw@Swy.Local:4567/"}, "http://swy.local:4567"),
        ({"id": 1, "host": "https://swy.example/base/?x=1"}, "https://swy.example/base"),
        ({"id": 1, "host": "http://swy", "port": 4567}, "http://swy:4567"),
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
    import routers.suwayomi_ as swy

    monkeypatch.setattr(chapter_provenance, "FINGERPRINT_MAX_BYTES", 10)

    def _fail(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(swy, "assert_library_file_mutation_available", _fail)
    _queue(env)
    _run(env)
    prov, ch, jobs = _rows(env)
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

    def _race(src, dst):
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
