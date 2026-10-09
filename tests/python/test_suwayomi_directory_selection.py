"""Hermetic directory identity tests, independent of chapter/volume parsing."""

import asyncio
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
from typing import Any
import zipfile

import pytest

from routers import suwayomi_ as swy


def manga_dir(base: Path, source: str, title: str) -> Path:
    path = base / "mangas" / source / title
    path.mkdir(parents=True)
    return path


def find(base: Path, *titles: str) -> str | None:
    return swy._find_suwayomi_manga_dir({"download_path": str(base)}, *titles)


@pytest.fixture(params=[False, True], ids=["forward", "reverse"])
def enumeration_order(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    reverse = bool(request.param)
    real_listdir = os.listdir
    real_scandir = os.scandir

    def listdir(path: str | os.PathLike[str]) -> list[str]:
        return sorted(real_listdir(path), reverse=reverse)

    @contextmanager
    def scandir(path: str | os.PathLike[str]) -> Iterator[Iterator[os.DirEntry[str]]]:
        with real_scandir(path) as entries:
            ordered = sorted(entries, key=lambda entry: entry.name, reverse=reverse)
        yield iter(ordered)

    monkeypatch.setattr(os, "listdir", listdir)
    monkeypatch.setattr(os, "scandir", scandir)


def test_duplicate_exact_cannot_downgrade_to_unique_alias(
    tmp_path: Path, enumeration_order: None
) -> None:
    manga_dir(tmp_path, "SourceA", "Orbit")
    manga_dir(tmp_path, "SourceB", "Orbit")
    manga_dir(tmp_path, "SourceC", "Alias")
    assert find(tmp_path, "Orbit", "Alias") is None


@pytest.mark.parametrize("same_source", [False, True])
def test_duplicate_normalized_equality_is_not_first_match(
    tmp_path: Path, enumeration_order: None, same_source: bool
) -> None:
    manga_dir(tmp_path, "SourceA", "ORBIT_PARTS")
    manga_dir(tmp_path, "SourceA" if same_source else "SourceB", "orbit-parts")
    assert find(tmp_path, "Orbit: Parts") is None


def test_safe_aliases_are_one_tier_not_argument_order(
    tmp_path: Path, enumeration_order: None
) -> None:
    manga_dir(tmp_path, "SourceA", "AliasA")
    manga_dir(tmp_path, "SourceB", "AliasB")
    assert find(tmp_path, "Missing", "AliasA", "AliasB") is None
    assert find(tmp_path, "Missing", "AliasB", "AliasA") is None


def test_case_collision_without_exact_source_basename_refuses(
    tmp_path: Path, enumeration_order: None
) -> None:
    manga_dir(tmp_path, "SourceA", "ORBIT")
    manga_dir(tmp_path, "SourceA", "orbit")
    assert find(tmp_path, "Orbit") is None


def test_unique_exact_beats_lower_case_and_substring_variants(
    tmp_path: Path, enumeration_order: None
) -> None:
    expected = manga_dir(tmp_path, "SourceB", "Orbit")
    manga_dir(tmp_path, "SourceA", "ORBIT")
    manga_dir(tmp_path, "SourceA", "Orbit Extended")
    assert find(tmp_path, "Orbit") == str(expected)


@pytest.mark.parametrize(
    ("title", "basename"),
    [
        ("Source:Title", "Source_Title"),
        ("Source/Title", "Source_Title"),
        (" .Source Title. ", "Source Title"),
        ("a" * 241, "a" * 240),
        ("\u6f2b" * 81, "\u6f2b" * 80),
        ("a" * 239 + "\U0001f600", "a" * 239),
    ],
    ids=[
        "colon",
        "slash",
        "trim",
        "ascii-truncate",
        "utf8-truncate",
        "partial-codepoint",
    ],
)
def test_safe_source_basename_precedes_metadata_exact(
    tmp_path: Path, enumeration_order: None, title: str, basename: str
) -> None:
    manga_dir(tmp_path, "SourceA", "Metadata Title")
    expected = manga_dir(tmp_path, "SourceB", basename)
    assert find(tmp_path, title, "Metadata Title") == str(expected)


@pytest.mark.parametrize(
    ("title", "basename"),
    [
        ("Orbit\\Parts", "Orbit_Parts"),
        ('Orbit "Parts"?*<>|\x00\x1f\x7f', "Orbit _Parts_________"),
        ("\u6f2b\u753b", "\u6f2b\u753b"),
        ("\U0001f600", "\U0001f600"),
        (" . ", "(invalid)"),
    ],
    ids=[
        "backslash",
        "fat-invalid-characters",
        "unicode",
        "emoji",
        "upstream-invalid-name",
    ],
)
def test_upstream_sanitized_and_unicode_exact_names(
    tmp_path: Path, title: str, basename: str
) -> None:
    expected = manga_dir(tmp_path, "Source", basename)
    assert find(tmp_path, title) == str(expected)


def test_truncated_source_basename_collision_cannot_use_shorter_alias(
    tmp_path: Path,
) -> None:
    manga_dir(tmp_path, "SourceA", "a" * 240)
    manga_dir(tmp_path, "SourceB", "a" * 240)
    manga_dir(tmp_path, "SourceC", "Alias")
    assert find(tmp_path, "a" * 240 + "different", "Alias") is None


@pytest.mark.parametrize("title", ["", "!!!", "\u6f2b\u753b", "\U0001f600"])
def test_empty_normalization_does_not_match_arbitrary_directory(
    tmp_path: Path, title: str
) -> None:
    manga_dir(tmp_path, "Source", "Anything")
    manga_dir(tmp_path, "Source", "???")
    assert find(tmp_path, title) is None


@pytest.mark.parametrize(
    ("title", "folder"),
    [("Ore", "Borealis"), ("Orbit", "Orbit Extended"), ("Orbit Extended", "Orbit")],
)
def test_unique_substring_is_not_directory_identity(
    tmp_path: Path, title: str, folder: str
) -> None:
    manga_dir(tmp_path, "Source", folder)
    assert find(tmp_path, title) is None


@pytest.mark.parametrize(
    ("titles", "basename"),
    [
        (("Missing", "Alias"), "Alias"),
        (("Missing", "Alias: Parts"), "Alias_Parts"),
        (("Orbit: Parts",), "orbit-parts"),
        (("", "Orbit"), "Orbit"),
        (("Legacy: Title",), "Legacy: Title"),
    ],
)
def test_unique_legitimate_alias_and_normalized_matches_retained(
    tmp_path: Path, enumeration_order: None, titles: tuple[str, ...], basename: str
) -> None:
    expected = manga_dir(tmp_path, "Source", basename)
    assert find(tmp_path, *titles) == str(expected)


@pytest.mark.parametrize("title", ["../Escape", ".", "..", "Orbit/Parts"])
def test_raw_components_cannot_select_non_direct_manga_directory(
    tmp_path: Path, title: str
) -> None:
    manga_dir(tmp_path, "Source", "Orbit/Parts")
    (tmp_path / "mangas" / "Escape").mkdir()
    assert find(tmp_path, title) is None


def test_absolute_title_cannot_select_external_directory(tmp_path: Path) -> None:
    base = tmp_path / "swy"
    (base / "mangas" / "Source").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    assert find(base, str(outside)) is None


@pytest.mark.parametrize("level", ["mangas", "source", "manga"])
def test_child_symlink_cannot_select_external_directory(
    tmp_path: Path, level: str
) -> None:
    base = tmp_path / "swy"
    outside = tmp_path / "outside"
    target = outside if level == "manga" else outside / "Orbit"
    target.mkdir(parents=True)
    if level == "mangas":
        source = outside / "Source"
        source.mkdir()
        (source / "Orbit").mkdir()
        link = base / "mangas"
    elif level == "source":
        link = base / "mangas" / "Source"
    else:
        link = base / "mangas" / "Source" / "Orbit"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    assert find(base, "Orbit") is None


def test_symlink_alias_does_not_hide_unique_real_directory(tmp_path: Path) -> None:
    expected = manga_dir(tmp_path, "Source", "Orbit")
    (tmp_path / "mangas" / "AliasSource").symlink_to(
        expected.parent, target_is_directory=True
    )
    assert find(tmp_path, "Orbit") == str(expected)


def test_configured_base_symlink_is_trusted_anchor(tmp_path: Path) -> None:
    base = tmp_path / "real"
    manga_dir(base, "Source", "Orbit")
    alias = tmp_path / "configured"
    alias.symlink_to(base, target_is_directory=True)
    assert find(alias, "Orbit") == str(alias / "mangas" / "Source" / "Orbit")


def test_directory_scan_error_cannot_choose_partial_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manga_dir(tmp_path, "SourceA", "Orbit")
    failing = manga_dir(tmp_path, "SourceB", "Other").parent
    original = os.scandir

    def scandir(
        path: str | os.PathLike[str],
    ) -> AbstractContextManager[Iterator[os.DirEntry[str]]]:
        if os.fspath(path) == str(failing):
            raise PermissionError("synthetic unreadable source")
        return original(path)

    monkeypatch.setattr(os, "scandir", scandir)
    assert find(tmp_path, "Orbit") is None


def cbz(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("0001.png", b"synthetic-page")


@dataclass
class ImportEnv:
    base: Path
    library: Path
    db_path: Path
    client: dict[str, Any]

    def row(self, table: str) -> dict[str, Any]:
        assert table in {"series", "chapters", "volumes", "suwayomi_downloads"}
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            result = db.execute(f"SELECT * FROM {table} WHERE id=1").fetchone()
        assert result is not None
        return dict(result)


@pytest.fixture
def import_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ImportEnv:
    import main
    import security
    import shared

    db_path = tmp_path / "directory.db"
    monkeypatch.setattr(main, "DB_PATH", str(db_path))
    monkeypatch.setattr(shared, "DB_PATH", str(db_path))
    monkeypatch.setattr(main, "CONFIG", {})
    monkeypatch.setattr(shared, "CONFIG", {})
    monkeypatch.setattr(security, "_SECRET_CIPHER", None)
    security.load_or_create_secret_cipher(str(tmp_path / "keys"))
    main.init_db()
    main.load_config()
    library = tmp_path / "library"
    monkeypatch.setattr(main, "_series_library_dir", lambda *_: str(library))
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO series(id,title,search_pattern,anilist_id,monitored) VALUES(1,'Orbit','Orbit',123,0)"
        )
        db.execute(
            "INSERT INTO chapters(id,series_id,chapter_num,status,monitored) VALUES(1,1,1,'grabbed',0)"
        )
        db.execute(
            "INSERT INTO volumes(id,series_id,volume_num,status,monitored) VALUES(1,1,1,'grabbed',0)"
        )
    return ImportEnv(
        tmp_path / "swy", library, db_path, {"download_path": str(tmp_path / "swy")}
    )


@pytest.mark.parametrize("kind", ["chapter", "volume-merge", "volume-copy"])
@pytest.mark.parametrize("cached", [False, True], ids=["new-output", "cached-output"])
@pytest.mark.parametrize(
    "problem",
    ["safe-alias-duplicate", "normalized-duplicate", "source-symlink", "manga-symlink"],
)
def test_real_job_refuses_directory_before_output_or_completion(
    import_env: ImportEnv,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    cached: bool,
    problem: str,
) -> None:
    env = import_env
    env.client["merge_chapters"] = int(kind != "volume-copy")
    filename = "Alpha_Chapter 1.cbz"
    if problem.endswith("duplicate"):
        titles = (
            ("Orbit_Parts", "Orbit:Parts")
            if problem == "safe-alias-duplicate"
            else ("ORBIT", "orbit")
        )
        sources = [manga_dir(env.base, "Source", title) for title in titles]
        if problem == "safe-alias-duplicate":
            with sqlite3.connect(env.db_path) as db:
                db.execute("UPDATE series SET title='Orbit:Parts' WHERE id=1")
    else:
        outside = env.base.parent / "outside"
        outside.mkdir()
        if problem == "source-symlink":
            target = outside / "Orbit"
            target.mkdir()
            link = env.base / "mangas" / "Source"
        else:
            target = outside
            link = env.base / "mangas" / "Source" / "Orbit"
        link.parent.mkdir(parents=True)
        link.symlink_to(outside, target_is_directory=True)
        sources = [target]
    for source in sources:
        cbz(source / filename)
    source_bytes = {str(source): (source / filename).read_bytes() for source in sources}
    import main

    safe_title = main.sanitize_filename(env.row("series")["title"])
    output = env.library / (
        f"{safe_title} Ch001.cbz" if kind == "chapter" else f"{safe_title} v01.cbz"
    )
    if kind == "volume-copy":
        output = env.library / "v01" / filename
    if cached:
        output.parent.mkdir(parents=True)
        output.write_bytes(b"preserved-cache")
    before_files = {
        str(path.relative_to(env.library)): path.read_bytes()
        for path in env.library.rglob("*")
        if path.is_file()
    }
    before_rows = {table: env.row(table) for table in ("series", "chapters", "volumes")}
    with sqlite3.connect(env.db_path) as db:
        db.execute(
            "INSERT INTO suwayomi_downloads(id,series_id,volume_num,chapter_num,suwayomi_manga_id,chapter_ids,status,total)"
            " VALUES(1,1,1,?,999,'[901]','queued',1)",
            (1 if kind == "chapter" else None,),
        )

    async def gql(
        _client: dict[str, Any], query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        assert "manga(id:" in query
        assert variables == {"mid": 999}
        return {
            "manga": {
                "title": "Missing" if problem == "safe-alias-duplicate" else "Orbit",
                "source": {"displayName": "Source"},
                "chapters": {
                    "nodes": [
                        {
                            "id": 901,
                            "chapterNumber": 1,
                            "isDownloaded": True,
                            "name": "Chapter 1",
                            "scanlator": "Alpha",
                        }
                    ]
                },
            }
        }

    monkeypatch.setattr(swy, "_gql", gql)
    asyncio.run(swy._process_suwayomi_job(env.client, env.row("suwayomi_downloads")))
    job = env.row("suwayomi_downloads")
    assert job["status"] == "error"
    assert job["chapter_ids"] == "[901]"
    assert job["suwayomi_manga_id"] == 999
    assert job["error"]
    assert {table: env.row(table) for table in before_rows} == before_rows
    assert {
        str(path.relative_to(env.library)): path.read_bytes()
        for path in env.library.rglob("*")
        if path.is_file()
    } == before_files
    assert {
        str(source): (source / filename).read_bytes() for source in sources
    } == source_bytes
    if not cached:
        assert not env.library.exists()


@pytest.mark.parametrize("kind", ["chapter", "volume"])
def test_legacy_import_cannot_bypass_duplicate_directory(
    import_env: ImportEnv, kind: str
) -> None:
    for source in ("SourceA", "SourceB"):
        cbz(manga_dir(import_env.base, source, "Orbit") / "Vol.1 Ch.1.cbz")
    operation = (
        swy._import_suwayomi_chapter
        if kind == "chapter"
        else swy._import_suwayomi_volume
    )
    assert asyncio.run(operation(import_env.client, 1, 1)) == (None, 0)
    assert not import_env.library.exists()
