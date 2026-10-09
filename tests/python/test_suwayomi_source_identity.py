"""Job-scoped Suwayomi directory identity, independent of current linkage."""

import asyncio
from pathlib import Path
import sqlite3
from typing import Any
import zipfile

import pytest

from routers import suwayomi_ as swy
from test_suwayomi_directory_selection import ImportEnv, import_env, manga_dir


def archive(path: Path, page: bytes) -> None:
    with zipfile.ZipFile(path, "w") as cbz:
        cbz.writestr("0001.png", page)


def process(
    env: ImportEnv,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    source: object,
    title: str = "Orbit",
) -> list[str]:
    env.client["merge_chapters"] = int(kind != "volume-copy")
    with sqlite3.connect(env.db_path) as db:
        # Relinking after enqueue must not change the job's import identity.
        db.execute("UPDATE series SET suwayomi_id=200 WHERE id=1")
        db.execute(
            "INSERT INTO suwayomi_sources(series_id,source_id,source_name,suwayomi_manga_id)"
            " VALUES(1,'current','Current (FR)',200)"
        )
        db.execute(
            "INSERT INTO suwayomi_downloads(id,series_id,volume_num,chapter_num,"
            "suwayomi_manga_id,chapter_ids,status,total)"
            " VALUES(1,1,1,?,999,'[901]','queued',1)",
            (1 if kind == "chapter" else None,),
        )
    queries: list[str] = []

    async def gql(
        _client: dict[str, Any], query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        assert variables == {"mid": 999}
        assert "manga(id:" in query
        queries.append(query)
        return {
            "manga": {
                "title": title,
                "source": source,
                "chapters": {
                    "nodes": [
                        {
                            "id": 901,
                            "chapterNumber": 1,
                            "isDownloaded": True,
                            "name": "Vol.1 Ch.1",
                            "scanlator": None,
                        }
                    ]
                },
            }
        }

    monkeypatch.setattr(swy, "_gql", gql)
    asyncio.run(swy._process_suwayomi_job(env.client, env.row("suwayomi_downloads")))
    return queries


@pytest.mark.parametrize("kind", ["chapter", "volume-merge", "volume-copy"])
@pytest.mark.parametrize(
    "stale_present", [False, True], ids=["control", "same-title-stale"]
)
def test_job_source_wins_over_stale_and_current_relink(
    import_env: ImportEnv,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    stale_present: bool,
) -> None:
    env = import_env
    expected = manga_dir(env.base, "Queued (EN)", "Orbit") / "Vol.1 Ch.1.cbz"
    archive(expected, b"queued-source")
    if stale_present:
        archive(
            manga_dir(env.base, "Current (FR)", "Orbit") / expected.name,
            b"wrong-source",
        )
    queries = process(env, monkeypatch, kind, {"displayName": "Queued (EN)"})
    assert env.row("suwayomi_downloads")["status"] == "completed"
    row = env.row("chapters" if kind == "chapter" else "volumes")
    assert row["status"] == "downloaded"
    output = Path(row["import_path"])
    if kind == "volume-copy":
        output /= expected.name
    with zipfile.ZipFile(output) as cbz:
        assert cbz.read(cbz.namelist()[0]) == b"queued-source"
    assert len(queries) == 1
    assert "displayName" in queries[0]
    assert expected.exists()
    assert env.row("series")["suwayomi_id"] == 200


@pytest.mark.parametrize("kind", ["chapter", "volume-merge", "volume-copy"])
@pytest.mark.parametrize(
    "problem",
    [
        "stale-only",
        "missing-title",
        "wrong-language",
        "source-symlink",
        "manga-symlink",
    ],
)
def test_job_never_falls_back_outside_exact_source(
    import_env: ImportEnv, monkeypatch: pytest.MonkeyPatch, kind: str, problem: str
) -> None:
    env = import_env
    stale = manga_dir(env.base, "Current (FR)", "Orbit") / "Vol.1 Ch.1.cbz"
    archive(stale, b"wrong-source")
    if problem == "missing-title":
        manga_dir(env.base, "Queued (EN)", "Other")
    elif problem == "wrong-language":
        archive(
            manga_dir(env.base, "Queued (FR)", "Orbit") / stale.name, b"wrong-language"
        )
    elif problem == "source-symlink":
        (env.base / "mangas" / "Queued (EN)").symlink_to(
            stale.parent.parent, target_is_directory=True
        )
    elif problem == "manga-symlink":
        parent = env.base / "mangas" / "Queued (EN)"
        parent.mkdir()
        (parent / "Orbit").symlink_to(stale.parent, target_is_directory=True)
    env.library.mkdir()
    retained = env.library / "retained.cbz"
    retained.write_bytes(b"unchanged")
    before = {table: env.row(table) for table in ("chapters", "volumes")}
    original = stale.read_bytes()
    process(env, monkeypatch, kind, {"displayName": "Queued (EN)"})
    assert env.row("suwayomi_downloads")["status"] == "error"
    assert {table: env.row(table) for table in before} == before
    assert list(env.library.iterdir()) == [retained]
    assert retained.read_bytes() == b"unchanged"
    assert stale.read_bytes() == original


@pytest.mark.parametrize(
    "source",
    [
        None,
        {},
        [],
        "Queued (EN)",
        {"displayName": None},
        {"displayName": 123},
        {"displayName": ""},
        {"displayName": "  "},
    ],
)
@pytest.mark.parametrize("kind", ["chapter", "volume-copy"])
def test_completed_job_missing_or_malformed_source_fails_closed(
    import_env: ImportEnv, monkeypatch: pytest.MonkeyPatch, source: object, kind: str
) -> None:
    env = import_env
    archive(manga_dir(env.base, "Queued (EN)", "Orbit") / "Vol.1 Ch.1.cbz", b"unproven")
    process(env, monkeypatch, kind, source)
    assert env.row("suwayomi_downloads")["status"] == "error"
    assert env.row("chapters")["status"] == "grabbed"
    assert env.row("volumes")["status"] == "grabbed"
    assert not env.library.exists()


@pytest.mark.parametrize(
    ("source", "source_folder", "title", "title_folder"),
    [
        ("Alias (ALL)", "Alias (ALL)", "Orbit", "Orbit"),
        ("Queued (FR)", "Queued (FR)", "Orbit", "Orbit"),
        (
            "../Queued/Source (EN)",
            "_Queued_Source (EN)",
            "../Orbit/Parts",
            "_Orbit_Parts",
        ),
        ("/absolute/Source", "_absolute_Source", "Orbit", "Orbit"),
        ("Queued\\Source (EN)", "Queued_Source (EN)", "Orbit", "Orbit"),
        ("\u6f2b\u753b (JA)", "\u6f2b\u753b (JA)", "\u6f2b\u753b", "\u6f2b\u753b"),
    ],
)
def test_source_display_name_and_title_use_upstream_safe_path(
    import_env: ImportEnv,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    source_folder: str,
    title: str,
    title_folder: str,
) -> None:
    env = import_env
    archive(
        manga_dir(env.base, source_folder, title_folder) / "Vol.1 Ch.1.cbz", b"safe"
    )
    process(env, monkeypatch, "chapter", {"displayName": source}, title)
    assert env.row("suwayomi_downloads")["status"] == "completed"
    assert Path(env.row("chapters")["import_path"]).is_relative_to(env.library)


@pytest.mark.parametrize("kind", ["chapter", "volume"])
def test_legacy_unique_global_helper_remains_compatible(
    import_env: ImportEnv, kind: str
) -> None:
    env = import_env
    archive(manga_dir(env.base, "Legacy", "Orbit") / "Vol.1 Ch.1.cbz", b"legacy")
    operation = (
        swy._import_suwayomi_chapter
        if kind == "chapter"
        else swy._import_suwayomi_volume
    )
    path, size = asyncio.run(operation(env.client, 1, 1))
    assert path is not None
    assert size > 0
    assert Path(path).is_relative_to(env.library)


def test_job_title_alias_fallback_stays_within_source(
    import_env: ImportEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = import_env
    # The live title's exact directory outside the source must not outrank
    # the metadata alias inside the job source.
    archive(
        manga_dir(env.base, "Current (FR)", "Live Title") / "Vol.1 Ch.1.cbz", b"wrong"
    )
    archive(manga_dir(env.base, "Queued (EN)", "Orbit") / "Vol.1 Ch.1.cbz", b"alias")
    process(env, monkeypatch, "chapter", {"displayName": "Queued (EN)"}, "Live Title")
    assert env.row("suwayomi_downloads")["status"] == "completed"
    with zipfile.ZipFile(env.row("chapters")["import_path"]) as cbz:
        assert cbz.read("0001.png") == b"alias"


@pytest.mark.parametrize("kind", ["chapter", "volume-merge"])
def test_missing_source_cannot_complete_from_cached_output(
    import_env: ImportEnv, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    env = import_env
    archive(manga_dir(env.base, "Queued (EN)", "Orbit") / "Vol.1 Ch.1.cbz", b"source")
    cached = env.library / ("Orbit Ch001.cbz" if kind == "chapter" else "Orbit v01.cbz")
    cached.parent.mkdir()
    cached.write_bytes(b"historical-output")
    process(env, monkeypatch, kind, None)
    assert env.row("suwayomi_downloads")["status"] == "error"
    assert env.row("chapters")["status"] == "grabbed"
    assert env.row("volumes")["status"] == "grabbed"
    assert cached.read_bytes() == b"historical-output"
    assert list(env.library.iterdir()) == [cached]


@pytest.mark.parametrize("source", ["../outside", "/outside", "Queued (en)", "\ud800"])
def test_source_path_and_normalized_labels_cannot_select_raw_paths(
    import_env: ImportEnv, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    env = import_env
    archive(manga_dir(env.base, "Queued (EN)", "Orbit") / "Vol.1 Ch.1.cbz", b"unproven")
    outside = env.base / "outside" / "Orbit"
    outside.mkdir(parents=True)
    archive(outside / "Vol.1 Ch.1.cbz", b"outside")
    process(env, monkeypatch, "chapter", {"displayName": source})
    assert env.row("suwayomi_downloads")["status"] == "error"
    assert not env.library.exists()
