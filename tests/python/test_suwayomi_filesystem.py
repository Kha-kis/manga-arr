"""Hermetic filesystem tests for Suwayomi import paths.

Builds tiny CBZ fixtures in tmpdirs and exercises the actual disk-touching
helpers in app/routers/suwayomi_.py:

  - _swy_library_base / _find_suwayomi_manga_dir   — directory resolution
  - _vol_chapter_cbzs / _chapter_cbz                — file selection by name
  - _ch_sort_key                                    — page ordering
  - _merge_cbzs                                     — multi-cbz merge
  - _import_suwayomi_volume / _import_suwayomi_chapter — full import flow

All paths are inside pytest's tmp_path. No real media folders touched.
"""
import asyncio
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any
import xml.etree.ElementTree as ET
import zipfile

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


# ───────────────────────── tiny PNG (1×1 transparent) ────────────────────────

_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63f8cf000000030001fe79bff70000000049454e44ae42"
    "6082"
)


def _make_cbz(path: str, page_count: int = 2) -> str:
    """Write a CBZ with `page_count` 1×1 PNGs at deterministic names."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for i in range(page_count):
            zf.writestr(f"{i+1:04d}.png", _TINY_PNG)
    return path


# ───────────────────────── library base resolution ───────────────────────────

def test_swy_library_base_returns_none_when_unset():
    from routers.suwayomi_ import _swy_library_base
    assert _swy_library_base({}) is None
    assert _swy_library_base({"download_path": ""}) is None
    assert _swy_library_base({"download_path": "   "}) is None


def test_find_manga_dir_exact_match(tmp_path):
    from routers.suwayomi_ import _find_suwayomi_manga_dir
    base = tmp_path / "swy"
    (base / "mangas" / "MangaDex" / "Vinland Saga").mkdir(parents=True)
    found = _find_suwayomi_manga_dir({"download_path": str(base)}, "Vinland Saga")
    assert found == str(base / "mangas" / "MangaDex" / "Vinland Saga")


def test_find_manga_dir_normalised_match(tmp_path):
    """Suwayomi may sanitize titles to dir names; matcher should fuzz them."""
    from routers.suwayomi_ import _find_suwayomi_manga_dir
    base = tmp_path / "swy"
    (base / "mangas" / "MangaDex" / "Berserk_Black_Swordsman").mkdir(parents=True)
    found = _find_suwayomi_manga_dir(
        {"download_path": str(base)}, "Berserk: Black Swordsman"
    )
    assert found is not None
    assert found.endswith("Berserk_Black_Swordsman")


def test_find_manga_dir_returns_none_when_no_match(tmp_path):
    from routers.suwayomi_ import _find_suwayomi_manga_dir
    base = tmp_path / "swy"
    (base / "mangas" / "MangaDex" / "Other Title").mkdir(parents=True)
    assert _find_suwayomi_manga_dir({"download_path": str(base)}, "Vinland Saga") is None


def test_find_manga_dir_returns_none_when_no_mangas_root(tmp_path):
    """If `<base>/mangas` doesn't exist, fail safely (no crash)."""
    from routers.suwayomi_ import _find_suwayomi_manga_dir
    base = tmp_path / "swy-empty"
    base.mkdir()
    assert _find_suwayomi_manga_dir({"download_path": str(base)}, "X") is None


# ───────────────────────── chapter file selection ────────────────────────────

def test_vol_chapter_cbzs_filters_by_volume(tmp_path):
    from routers.suwayomi_ import _vol_chapter_cbzs
    manga_dir = tmp_path / "manga"
    manga_dir.mkdir()
    _make_cbz(str(manga_dir / "Vol.1 Ch.1.cbz"))
    _make_cbz(str(manga_dir / "Vol.1 Ch.2.cbz"))
    _make_cbz(str(manga_dir / "Vol.2 Ch.3.cbz"))
    found = _vol_chapter_cbzs(str(manga_dir), 1.0)
    assert len(found) == 2
    assert all("Vol.1" in os.path.basename(p) for p in found)


def test_vol_chapter_cbzs_ignores_non_cbz_files(tmp_path):
    from routers.suwayomi_ import _vol_chapter_cbzs
    manga_dir = tmp_path / "manga"
    manga_dir.mkdir()
    _make_cbz(str(manga_dir / "Vol.1 Ch.1.cbz"))
    (manga_dir / "Vol.1 Ch.2.txt").write_text("not a cbz")
    (manga_dir / "Vol.1 Ch.3.zip").write_text("also not")
    found = _vol_chapter_cbzs(str(manga_dir), 1.0)
    assert len(found) == 1


def test_vol_chapter_cbzs_returns_sorted_by_chapter_num(tmp_path):
    from routers.suwayomi_ import _vol_chapter_cbzs
    manga_dir = tmp_path / "manga"
    manga_dir.mkdir()
    _make_cbz(str(manga_dir / "Vol.1 Ch.10.cbz"))
    _make_cbz(str(manga_dir / "Vol.1 Ch.2.cbz"))
    _make_cbz(str(manga_dir / "Vol.1 Ch.1.cbz"))
    found = [os.path.basename(p) for p in _vol_chapter_cbzs(str(manga_dir), 1.0)]
    # _ch_sort_key parses Ch.N — must order numerically, not lexically.
    assert found == ["Vol.1 Ch.1.cbz", "Vol.1 Ch.2.cbz", "Vol.1 Ch.10.cbz"]


def test_vol_chapter_cbzs_handles_decimal_volume(tmp_path):
    from routers.suwayomi_ import _vol_chapter_cbzs
    manga_dir = tmp_path / "manga"
    manga_dir.mkdir()
    _make_cbz(str(manga_dir / "Vol.1.5 Ch.1.cbz"))
    _make_cbz(str(manga_dir / "Vol.1 Ch.1.cbz"))
    found = [os.path.basename(p) for p in _vol_chapter_cbzs(str(manga_dir), 1.5)]
    assert found == ["Vol.1.5 Ch.1.cbz"]


def test_chapter_cbz_finds_specific_chapter(tmp_path):
    from routers.suwayomi_ import _chapter_cbz
    manga_dir = tmp_path / "manga"
    manga_dir.mkdir()
    _make_cbz(str(manga_dir / "Vol.1 Ch.5.cbz"))
    found = _chapter_cbz(str(manga_dir), 5.0)
    assert found is not None
    assert found.endswith("Vol.1 Ch.5.cbz")


def test_chapter_cbz_returns_none_when_missing(tmp_path):
    from routers.suwayomi_ import _chapter_cbz
    manga_dir = tmp_path / "manga"
    manga_dir.mkdir()
    _make_cbz(str(manga_dir / "Vol.1 Ch.1.cbz"))
    assert _chapter_cbz(str(manga_dir), 99.0) is None


# ───────────────────────── _merge_cbzs ───────────────────────────────────────

def test_merge_cbzs_concatenates_pages(tmp_path):
    from routers.suwayomi_ import _merge_cbzs
    manga_dir = tmp_path / "manga"
    manga_dir.mkdir()
    ch1 = _make_cbz(str(manga_dir / "Vol.1 Ch.1.cbz"), page_count=3)
    ch2 = _make_cbz(str(manga_dir / "Vol.1 Ch.2.cbz"), page_count=2)
    out = str(tmp_path / "library" / "merged.cbz")

    size = _merge_cbzs([ch1, ch2], out)
    assert size > 0
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    # 3 + 2 = 5 pages, renamed sequentially.
    assert len(names) == 5
    assert names == ["0001.png", "0002.png", "0003.png", "0004.png", "0005.png"]


def test_merge_cbzs_skips_dotfiles(tmp_path):
    """macOS .DS_Store etc. inside archives must not become pages."""
    from routers.suwayomi_ import _merge_cbzs
    src = str(tmp_path / "src.cbz")
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr(".DS_Store", b"junk")
        zf.writestr("0001.png", _TINY_PNG)
        zf.writestr("__MACOSX/._meta", b"more junk")
    out = str(tmp_path / "out.cbz")
    _merge_cbzs([src], out)
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert names == ["0001.png"]


def test_merge_cbzs_returns_zero_and_cleans_up_on_failure(tmp_path):
    """Bad input archive → return 0, no orphan output file."""
    from routers.suwayomi_ import _merge_cbzs
    bad = str(tmp_path / "bad.cbz")
    with open(bad, "wb") as f:
        f.write(b"this is not a zip")
    out = str(tmp_path / "out.cbz")
    size = _merge_cbzs([bad], out)
    assert size == 0
    assert not os.path.exists(out), "failed merge left an orphan output file"


def test_merge_cbzs_handles_only_image_extensions(tmp_path):
    """Non-image entries in the source archive must not appear as pages."""
    from routers.suwayomi_ import _merge_cbzs
    src = str(tmp_path / "src.cbz")
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("0001.png",      _TINY_PNG)
        zf.writestr("notes.txt",     b"chapter notes")
        zf.writestr("0002.jpeg",     _TINY_PNG)
        zf.writestr("metadata.xml",  b"<x/>")
    out = str(tmp_path / "out.cbz")
    _merge_cbzs([src], out)
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert names == ["0001.png", "0002.jpeg"]


# ───────────────────────── _import_suwayomi_volume (full flow) ───────────────

@pytest.fixture
def import_env(tmp_path, monkeypatch):
    """Fresh DB + tmp Suwayomi library root + tmp Mangarr library root."""
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False, dir=tmp_path)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-swyfs-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()

    swy_root = tmp_path / "swy"
    lib_root = tmp_path / "library"
    lib_root.mkdir()

    # Stub _series_library_dir so we don't need to set up root_folders.
    def _fake_dir(_db, sid):
        d = lib_root / f"series-{sid}"
        d.mkdir(exist_ok=True)
        return str(d)
    monkeypatch.setattr(main, "_series_library_dir", _fake_dir)

    try:
        yield {
            "db_path":  db.name,
            "swy_root": swy_root,
            "lib_root": lib_root,
            "client":   {"download_path": str(swy_root)},
        }
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


def test_import_volume_merges_chapters_into_one_cbz(import_env):
    """Default merge=True path: produces single output CBZ in series dir."""
    import asyncio
    from routers.suwayomi_ import _import_suwayomi_volume

    db_path  = import_env["db_path"]
    swy_root = import_env["swy_root"]
    with sqlite3.connect(db_path) as c:
        c.execute("INSERT INTO series(id, title, search_pattern)"
                  " VALUES(7, 'Test Series', 'Test Series')")

    # Build a Suwayomi-style download dir with two chapters in vol 1.
    manga_dir = swy_root / "mangas" / "MangaDex" / "Test Series"
    manga_dir.mkdir(parents=True)
    _make_cbz(str(manga_dir / "Vol.1 Ch.1.cbz"), page_count=2)
    _make_cbz(str(manga_dir / "Vol.1 Ch.2.cbz"), page_count=3)

    path, size = asyncio.run(_import_suwayomi_volume(
        import_env["client"], 7, 1.0, swy_title="Test Series"
    ))
    assert path is not None
    assert size > 0
    assert os.path.basename(path) == "Test Series v01.cbz"
    with zipfile.ZipFile(path) as zf:
        assert len([name for name in zf.namelist() if name.endswith(".png")]) == 5


@pytest.fixture
def merged_volume_env(import_env: dict[str, Any]) -> dict[str, Any]:
    with sqlite3.connect(import_env["db_path"]) as db:
        db.execute(
            "INSERT INTO series(id,title,search_pattern,description,status,pub_year,"
            "total_volumes,total_chapters,anilist_id,mal_id,mangadex_id)"
            " VALUES(7,?,?,?,?,?,?,?,?,?,?)",
            ("Manual & Current", "Old Provider Title", "Persisted summary", "FINISHED",
             2004, 1, 8, 41734, 100, "46fb8129-fa17-4532-8686-0b9dbdde0236"),
        )
        db.execute(
            "INSERT INTO series_metadata_fields"
            "(series_id,field_name,value_json,selected_source,locked,selected_at)"
            " VALUES(7,'title','\"Manual & Current\"','manual',1,'2026-10-10')"
        )
        db.execute(
            "INSERT INTO series_metadata_sources(series_id,source,status,failure_count)"
            " VALUES(7,'anilist','failed',3)"
        )
        before = {
            table: db.execute(f"SELECT * FROM {table}").fetchall()
            for table in ("series", "volumes", "chapters", "series_metadata_fields",
                          "series_metadata_sources")
        }
    manga_dir = import_env["swy_root"] / "mangas" / "MangaDex" / "Old Provider Title"
    manga_dir.mkdir(parents=True)
    sources: list[Path] = []
    pages: list[bytes] = []
    for chapter in range(1, 9):
        source = manga_dir / f"Vol.1 Ch.{chapter}.cbz"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr(
                "ComicInfo.xml",
                "<ComicInfo><Series>Wrong Chapter Title</Series>"
                "<Volume>99</Volume><Number>999</Number></ComicInfo>",
            )
            for page in (1, 2):
                image = _TINY_PNG + bytes((chapter, page))
                pages.append(image)
                archive.writestr(f"{page:04d}.png", image)
        sources.append(source)
    return {
        **import_env, "sources": sources, "pages": pages, "before": before,
        "source_hashes": [hashlib.sha256(path.read_bytes()).digest() for path in sources],
    }


def test_import_merged_volume_regenerates_current_series_comicinfo(
    merged_volume_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    import comicinfo
    from routers.suwayomi_ import _import_suwayomi_volume

    env = merged_volume_env
    rewrite = comicinfo._rewrite_with_comicinfo

    def rewrite_without_db_writer(path: str, xml: str) -> None:
        with sqlite3.connect(env["db_path"], timeout=0) as writer:
            writer.execute("BEGIN IMMEDIATE")
            writer.rollback()
        rewrite(path, xml)

    monkeypatch.setattr(comicinfo, "_rewrite_with_comicinfo", rewrite_without_db_writer)
    path, size = asyncio.run(_import_suwayomi_volume(
        env["client"], 7, 1.0, swy_title="Old Provider Title",
    ))
    assert path is not None
    assert size == os.path.getsize(path)
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        assert archive.namelist().count("ComicInfo.xml") == 1
        xml = ET.fromstring(archive.read("ComicInfo.xml"))
        assert xml.findtext("Series") == "Manual & Current"
        assert xml.findtext("Volume") == "1"
        assert xml.find("Number") is None
        assert xml.findtext("Summary") == "Persisted summary"
        assert xml.findtext("Year") == "2004"
        assert xml.findtext("Count") == "1"
        images = [name for name in archive.namelist() if name.endswith(".png")]
        assert images == [f"{page:04d}.png" for page in range(1, 17)]
        assert [archive.read(name) for name in images] == env["pages"]
    assert [hashlib.sha256(source.read_bytes()).digest() for source in env["sources"]] == env["source_hashes"]
    with sqlite3.connect(env["db_path"]) as db:
        for table, before in env["before"].items():
            assert db.execute(f"SELECT * FROM {table}").fetchall() == before


@pytest.mark.parametrize("failure", ["metadata", "rewrite"])
def test_import_merged_volume_comicinfo_failure_is_nonfatal(
    merged_volume_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    import comicinfo
    from routers.suwayomi_ import _import_suwayomi_volume

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("synthetic enrichment failure")

    monkeypatch.setattr(
        comicinfo,
        "build_comicinfo_xml" if failure == "metadata" else "_rewrite_with_comicinfo",
        fail,
    )
    env = merged_volume_env
    path, size = asyncio.run(_import_suwayomi_volume(
        env["client"], 7, 1.0, swy_title="Old Provider Title",
    ))
    assert path is not None
    assert size == os.path.getsize(path) > 0
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        assert [archive.read(name) for name in archive.namelist()] == env["pages"]
    assert [hashlib.sha256(source.read_bytes()).digest() for source in env["sources"]] == env["source_hashes"]
    with sqlite3.connect(env["db_path"]) as db:
        errors = db.execute("SELECT message FROM events WHERE event_type='error'").fetchall()
        assert any("synthetic enrichment failure" in row[0] for row in errors)


def test_import_merged_fractional_volume_passes_unmodified_metadata_context(
    import_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    import comicinfo
    from routers.suwayomi_ import _import_suwayomi_volume

    with sqlite3.connect(import_env["db_path"]) as db:
        db.execute(
            "INSERT INTO series(id,title,search_pattern) VALUES(7,'Test Series','Test Series')"
        )
    source = Path(_make_cbz(str(
        import_env["swy_root"] / "mangas" / "MangaDex" / "Test Series" / "Vol.1.5 Ch.1.cbz"
    )))
    original = source.read_bytes()
    contexts: list[float | None] = []
    build = comicinfo.build_comicinfo_xml

    def record_context(
        series: dict[str, Any], volume_num: float | None = None,
        chapter_num: float | None = None, tags: list[str] | None = None,
    ) -> str:
        contexts.append(volume_num)
        return build(series, volume_num=volume_num, chapter_num=chapter_num, tags=tags)

    monkeypatch.setattr(comicinfo, "build_comicinfo_xml", record_context)
    path, size = asyncio.run(_import_suwayomi_volume(
        import_env["client"], 7, 1.5, swy_title="Test Series",
    ))
    assert path is not None
    assert os.path.basename(path) == "Test Series v1.5.cbz"
    assert size == os.path.getsize(path)
    assert contexts == [1.5]
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        xml = ET.fromstring(archive.read("ComicInfo.xml"))
        assert xml.findtext("Series") == "Test Series"
        assert xml.find("Number") is None
        assert [archive.read(name) for name in archive.namelist() if name.endswith(".png")] == [_TINY_PNG] * 2
    assert source.read_bytes() == original


def test_import_volume_idempotent_when_output_exists(import_env):
    """If output file exists with size>0, return it unchanged (no re-merge)."""
    import asyncio
    from routers.suwayomi_ import _import_suwayomi_volume

    db_path  = import_env["db_path"]
    swy_root = import_env["swy_root"]
    lib_root = import_env["lib_root"]
    with sqlite3.connect(db_path) as c:
        c.execute("INSERT INTO series(id, title, search_pattern)"
                  " VALUES(7, 'Test Series', 'Test Series')")

    manga_dir = swy_root / "mangas" / "MangaDex" / "Test Series"
    manga_dir.mkdir(parents=True)
    _make_cbz(str(manga_dir / "Vol.1 Ch.1.cbz"))

    # Pre-existing output: contents and mtime must remain.
    pre = lib_root / "series-7" / "Test Series v01.cbz"
    pre.parent.mkdir(parents=True, exist_ok=True)
    pre.write_bytes(b"PRE-EXISTING-CONTENT-NOT-A-VALID-CBZ")
    pre_size = pre.stat().st_size

    path, size = asyncio.run(_import_suwayomi_volume(
        import_env["client"], 7, 1.0, swy_title="Test Series"
    ))
    assert path == str(pre)
    assert size == pre_size
    assert pre.read_bytes() == b"PRE-EXISTING-CONTENT-NOT-A-VALID-CBZ"


def test_import_volume_returns_none_when_no_chapters(import_env):
    """No chapters for the requested volume → (None, 0), no output file."""
    import asyncio
    from routers.suwayomi_ import _import_suwayomi_volume

    db_path  = import_env["db_path"]
    swy_root = import_env["swy_root"]
    lib_root = import_env["lib_root"]
    with sqlite3.connect(db_path) as c:
        c.execute("INSERT INTO series(id, title, search_pattern)"
                  " VALUES(7, 'Test Series', 'Test Series')")
    manga_dir = swy_root / "mangas" / "MangaDex" / "Test Series"
    manga_dir.mkdir(parents=True)
    _make_cbz(str(manga_dir / "Vol.5 Ch.1.cbz"))  # different volume

    path, size = asyncio.run(_import_suwayomi_volume(
        import_env["client"], 7, 1.0, swy_title="Test Series"
    ))
    assert (path, size) == (None, 0)
    # Library dir should be empty (no garbage output).
    assert list((lib_root / "series-7").iterdir()) == []


def test_import_volume_returns_none_when_series_missing(import_env):
    """Unknown series_id → (None, 0), no crash."""
    import asyncio
    from routers.suwayomi_ import _import_suwayomi_volume
    path, size = asyncio.run(_import_suwayomi_volume(
        import_env["client"], 999, 1.0, swy_title="Whatever"
    ))
    assert (path, size) == (None, 0)


def test_import_volume_returns_none_when_manga_dir_missing(import_env):
    """Series exists but the Suwayomi download dir doesn't → (None, 0)."""
    import asyncio
    from routers.suwayomi_ import _import_suwayomi_volume
    db_path = import_env["db_path"]
    with sqlite3.connect(db_path) as c:
        c.execute("INSERT INTO series(id, title, search_pattern)"
                  " VALUES(7, 'Missing Manga', 'Missing Manga')")
    # Library base exists but the manga dir doesn't.
    (import_env["swy_root"] / "mangas" / "MangaDex").mkdir(parents=True)
    path, size = asyncio.run(_import_suwayomi_volume(
        import_env["client"], 7, 1.0, swy_title="Missing Manga"
    ))
    assert (path, size) == (None, 0)


def test_import_chapter_copies_single_cbz(import_env):
    """Chapter import copies the source CBZ into the series library dir
    with a normalised filename."""
    import asyncio
    from routers.suwayomi_ import _import_suwayomi_chapter

    db_path  = import_env["db_path"]
    swy_root = import_env["swy_root"]
    lib_root = import_env["lib_root"]
    with sqlite3.connect(db_path) as c:
        c.execute("INSERT INTO series(id, title, search_pattern)"
                  " VALUES(7, 'Test Series', 'Test Series')")
    manga_dir = swy_root / "mangas" / "MangaDex" / "Test Series"
    manga_dir.mkdir(parents=True)
    src = _make_cbz(str(manga_dir / "Vol.1 Ch.42.cbz"))

    path, size = asyncio.run(_import_suwayomi_chapter(
        import_env["client"], 7, 42.0, swy_title="Test Series"
    ))
    assert path is not None
    assert os.path.basename(path) == "Test Series Ch042.cbz"
    assert os.path.getsize(path) == os.path.getsize(src)


def test_import_chapter_idempotent_when_output_exists(import_env):
    """Pre-existing output with size>0 is returned unchanged."""
    import asyncio
    from routers.suwayomi_ import _import_suwayomi_chapter

    db_path  = import_env["db_path"]
    swy_root = import_env["swy_root"]
    lib_root = import_env["lib_root"]
    with sqlite3.connect(db_path) as c:
        c.execute("INSERT INTO series(id, title, search_pattern)"
                  " VALUES(7, 'Test Series', 'Test Series')")
    (swy_root / "mangas" / "MangaDex" / "Test Series").mkdir(parents=True)
    _make_cbz(str(swy_root / "mangas" / "MangaDex" / "Test Series" / "Vol.1 Ch.5.cbz"))

    series_dir = lib_root / "series-7"
    series_dir.mkdir()
    pre = series_dir / "Test Series Ch005.cbz"
    pre.write_bytes(b"PRE-EXISTING")

    path, size = asyncio.run(_import_suwayomi_chapter(
        import_env["client"], 7, 5.0, swy_title="Test Series"
    ))
    assert path == str(pre)
    assert pre.read_bytes() == b"PRE-EXISTING"
