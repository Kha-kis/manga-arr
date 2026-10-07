"""Issue #382: local volume coverage must prevent duplicate loose-chapter jobs."""

import asyncio
import json
import sqlite3
import zipfile
from collections.abc import Generator, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest


@dataclass
class CoverageSync:
    db_path: Path
    library: Path
    feed: dict[float, str] = field(default_factory=dict)

    def volume(
        self,
        number: float,
        *,
        status: str = "downloaded",
        local: bool = True,
        special: bool = False,
        monitored: bool = True,
    ) -> int:
        path = self.library / f"volume-{number}.cbz"
        if local:
            with zipfile.ZipFile(path, "w") as cbz:
                cbz.writestr("0001.png", b"local page")
        with sqlite3.connect(self.db_path) as db:
            cur = db.execute(
                "INSERT INTO volumes(series_id, volume_num, status, import_path,"
                " is_special, monitored) VALUES(7,?,?,?,?,?)",
                (number, status, str(path), int(special), int(monitored)),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    def chapter(
        self,
        number: float,
        *,
        volume_id: int | None = None,
        title: str | None = None,
        status: str = "wanted",
        monitored: bool = True,
        available: bool = True,
    ) -> None:
        if available:
            self.feed[number] = f"Ch.{number}"
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "INSERT INTO chapters(series_id, chapter_num, volume_id, title,"
                " status, monitored) VALUES(7,?,?,?,?,?)",
                (number, volume_id, title, status, int(monitored)),
            )

    def mapping(self, mapping: dict[str, Any] | str) -> None:
        value = json.dumps(mapping) if isinstance(mapping, dict) else mapping
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE series SET chapter_vol_map=? WHERE id=7", (value,))

    def rows(self, query: str) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(query).fetchall()]

    def sync(self) -> tuple[int, int]:
        from routers import suwayomi_ as swy

        series = self.rows("SELECT * FROM series WHERE id=7")[0]
        return asyncio.run(swy._suwayomi_sync_series({}, series))

    def queued_chapters(self) -> list[float]:
        return [
            row["chapter_num"]
            for row in self.rows(
                "SELECT chapter_num FROM suwayomi_downloads"
                " WHERE chapter_num IS NOT NULL ORDER BY chapter_num"
            )
        ]

    async def gql(
        self, _client: dict, query: str, variables: dict | None = None
    ) -> dict:
        if "fetchChapters" in query:
            return {
                "fetchChapters": {
                    "chapters": [
                        {
                            "id": index,
                            "chapterNumber": number,
                            "name": name,
                            "sourceOrder": index,
                            "isDownloaded": False,
                        }
                        for index, (number, name) in enumerate(self.feed.items(), 1)
                    ]
                }
            }
        if "enqueueChapterDownloads" in query:
            assert variables is not None and variables["ids"]
            return {"enqueueChapterDownloads": {"clientMutationId": None}}
        if "startDownloader" in query:
            return {"startDownloader": {"clientMutationId": None}}
        raise AssertionError(f"Unexpected GraphQL operation: {query}")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[CoverageSync]:
    import main
    import security
    import shared
    from routers import suwayomi_ as swy

    db_path = tmp_path / "coverage.db"
    monkeypatch.setattr(main, "DB_PATH", str(db_path))
    monkeypatch.setattr(shared, "DB_PATH", str(db_path))
    monkeypatch.setattr(main, "CONFIG", {})
    monkeypatch.setattr(shared, "CONFIG", {})
    monkeypatch.setattr(security, "_SECRET_CIPHER", None)
    security.load_or_create_secret_cipher(str(tmp_path / "keys"))
    main.init_db()
    main.load_config()
    library = tmp_path / "library"
    library.mkdir()
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO download_clients(id, name, type, host, enabled)"
            " VALUES(1, 'swy', 'suwayomi', 'http://swy.invalid', 1)"
        )
        db.execute(
            "INSERT INTO series(id, title, search_pattern, status, mangadex_id,"
            " total_volumes, total_chapters)"
            " VALUES(7, 'Coverage', 'Coverage', 'FINISHED', 'stored-id', 3, 30)"
        )
        db.execute(
            "INSERT INTO suwayomi_sources(series_id, source_id, source_name, suwayomi_manga_id)"
            " VALUES(7, 'md', 'MangaDex', 101)"
        )
    upstream = CoverageSync(db_path, library)
    monkeypatch.setattr(swy, "_gql", upstream.gql)
    yield upstream


def test_sync_does_not_queue_unlinked_chapter_inside_local_volume(
    env: CoverageSync,
) -> None:
    volume_id = env.volume(1)
    env.chapter(1, volume_id=volume_id, status="downloaded")
    env.chapter(10, volume_id=volume_id, status="downloaded")
    env.chapter(5)
    env.chapter(11)
    before = env.rows("SELECT * FROM chapters WHERE chapter_num=5")
    series_before = env.rows("SELECT * FROM series WHERE id=7")
    volumes_before = env.rows("SELECT * FROM volumes")

    result = env.sync()

    assert env.queued_chapters() == [11.0]
    assert result == (0, 1)
    assert env.rows("SELECT * FROM chapters WHERE chapter_num=5") == before
    assert env.rows("SELECT * FROM volumes") == volumes_before
    series_after = env.rows("SELECT * FROM series WHERE id=7")[0]
    series_before[0]["suwayomi_id"] = (
        101  # Existing grab linkage, not coverage ownership.
    )
    assert series_after == series_before[0]


@pytest.mark.parametrize("anchors", ["mapped", "linked", "mixed"])
def test_partial_coverage_keeps_missing_middle_volumes_and_unknown_edges(
    env: CoverageSync, anchors: str
) -> None:
    first = env.volume(1, monitored=False)
    env.volume(2, status="wanted", monitored=False)
    third = env.volume(3)
    mapping: dict[str, Any] = {"8": 2}
    if anchors in ("mapped", "mixed"):
        mapping.update({"001": 1, "10.0": "1", "21": 3, "30": 3})
    if anchors in ("linked", "mixed"):
        for number, volume in ((1, first), (10, first), (21, third), (30, third)):
            env.chapter(number, volume_id=volume, status="downloaded")
    env.mapping(mapping)
    for number in (0, 5, 8, 10.5, 11, 15, 25, 31):
        env.chapter(number)
    env.chapter(6, title="Bonus special")

    # Chapter 8 contradicts volume 1's interval, so chapter 5 stays eligible.
    # Volume 3's independent 21..30 interval still covers chapter 25.
    assert env.sync() == (0, 8)
    assert env.queued_chapters() == [0.0, 5.0, 6.0, 8.0, 10.5, 11.0, 15.0, 31.0]


def test_single_map_anchor_only_covers_exact_assignments(env: CoverageSync) -> None:
    env.volume(1)
    env.mapping({"10": 1, "5.5": 1})
    for number in (1, 5, 5.25, 5.5, 10, 11):
        env.chapter(number)

    assert env.sync() == (0, 4)
    assert env.queued_chapters() == [1.0, 5.0, 5.25, 11.0]


@pytest.mark.parametrize(
    "status,local,special",
    [
        ("downloaded", False, False),
        ("wanted", True, False),
        ("grabbed", True, False),
        ("downloaded", True, True),
    ],
)
def test_no_actual_mainline_local_volume_does_not_suppress_chapters(
    env: CoverageSync, status: str, local: bool, special: bool
) -> None:
    env.volume(1, status=status, local=local, special=special, monitored=False)
    env.mapping({"1": 1, "10": 1})
    for number in (1, 5, 10, 11):
        env.chapter(number)

    assert env.sync() == (0, 4)
    assert env.queued_chapters() == [1.0, 5.0, 10.0, 11.0]


def test_no_volumes_and_existing_chapter_monitoring_are_preserved(
    env: CoverageSync,
) -> None:
    env.chapter(1)
    env.chapter(2, monitored=False)
    env.chapter(3, status="grabbed")
    env.chapter(4, status="downloaded")
    env.chapter(5, available=False)
    before = env.rows("SELECT * FROM chapters WHERE chapter_num!=1")

    assert env.sync() == (0, 1)
    assert env.queued_chapters() == [1.0]
    assert env.rows("SELECT * FROM chapters WHERE chapter_num!=1") == before


def test_new_chapters_still_discover_and_dispatch_above_local_coverage(
    env: CoverageSync,
) -> None:
    owned = env.volume(1)
    wanted = env.volume(2, status="wanted")
    env.chapter(1, volume_id=owned, status="downloaded")
    env.chapter(10, volume_id=owned, status="downloaded")
    env.chapter(12, volume_id=wanted)
    env.chapter(8, monitored=False)
    env.feed.update({5: "Ch.5", 8: "Ch.8", 11: "Ch.11", 12: "Vol.2 Ch.12"})
    with sqlite3.connect(env.db_path) as db:
        db.execute("UPDATE series SET status='RELEASING' WHERE id=7")
    existing = env.rows("SELECT id, chapter_num, volume_id, monitored FROM chapters")

    assert env.sync() == (1, 1)
    assert env.queued_chapters() == [11.0]
    assert env.rows(
        "SELECT volume_num FROM suwayomi_downloads WHERE volume_num IS NOT NULL"
    ) == [{"volume_num": 2.0}]
    assert (
        env.rows(
            "SELECT id, chapter_num, volume_id, monitored FROM chapters WHERE chapter_num IN (1,8,10,12)"
        )
        == existing
    )
    discovered = env.rows(
        "SELECT chapter_num, status, volume_id FROM chapters WHERE chapter_num IN (5,11) ORDER BY chapter_num"
    )
    assert discovered == [
        {"chapter_num": 5.0, "status": "wanted", "volume_id": None},
        {"chapter_num": 11.0, "status": "grabbed", "volume_id": None},
    ]


@pytest.mark.parametrize(
    "mapping",
    [
        "{broken",
        "[]",
        "null",
        '{"5": "bad", "NaN": 1, "Infinity": 1}',
        '{"1": 1, "10": 1, "5": null}',
        '{"1": 1, "10": 1, "5": 1, "05": 2}',
    ],
)
def test_invalid_or_ambiguous_mapping_keeps_unproven_chapter(
    env: CoverageSync, mapping: str
) -> None:
    env.volume(1)
    env.mapping(mapping)
    env.chapter(5)

    assert env.sync() == (0, 1)
    assert env.queued_chapters() == [5.0]


def test_legacy_downloaded_volume_with_no_path_does_not_prove_coverage(
    env: CoverageSync,
) -> None:
    owned = env.volume(1)
    env.chapter(1, volume_id=owned, status="downloaded")
    env.chapter(10, volume_id=owned, status="downloaded")
    env.chapter(5)
    with sqlite3.connect(env.db_path) as db:
        db.execute("UPDATE volumes SET import_path=NULL")

    assert env.sync() == (0, 1)
    assert env.queued_chapters() == [5.0]


def test_fractional_volume_covers_only_exactly_mapped_chapters(
    env: CoverageSync,
) -> None:
    env.volume(1.5)
    env.mapping({"1": 1.5, "10": 1.5})
    for number in (1, 5, 10):
        env.chapter(number)

    assert env.sync() == (0, 1)
    assert env.queued_chapters() == [5.0]


def test_link_map_conflict_cannot_establish_local_range(env: CoverageSync) -> None:
    owned = env.volume(1)
    env.volume(2, status="wanted", monitored=False)
    env.chapter(1, volume_id=owned, status="downloaded")
    env.chapter(10, volume_id=owned, status="downloaded")
    env.mapping({"10": 2})
    env.chapter(5)

    assert env.sync() == (0, 1)
    assert env.queued_chapters() == [5.0]


def test_other_series_local_volume_does_not_suppress_chapters(
    env: CoverageSync,
) -> None:
    owned = env.volume(1)
    with sqlite3.connect(env.db_path) as db:
        db.execute(
            "INSERT INTO series(id, title, search_pattern) VALUES(8, 'Other', 'Other')"
        )
        db.execute("UPDATE volumes SET series_id=8 WHERE id=?", (owned,))
    env.mapping({"1": 1, "10": 1})
    env.chapter(5)

    assert env.sync() == (0, 1)
    assert env.queued_chapters() == [5.0]


@pytest.mark.parametrize("kind", ["empty-file", "empty-directory", "chapter-directory"])
def test_local_path_requires_actual_content(env: CoverageSync, kind: str) -> None:
    env.volume(1)
    env.mapping({"1": 1, "10": 1})
    env.chapter(5)
    path = env.library / "local-content"
    if kind == "empty-file":
        path.touch()
    else:
        path.mkdir()
        if kind == "chapter-directory":
            with zipfile.ZipFile(path / "chapter.cbz", "w") as cbz:
                cbz.writestr("0001.png", b"local page")
    with sqlite3.connect(env.db_path) as db:
        db.execute("UPDATE volumes SET import_path=?", (str(path),))

    expected = [] if kind == "chapter-directory" else [5.0]
    assert env.sync() == (0, len(expected))
    assert env.queued_chapters() == expected


@pytest.mark.parametrize(
    "interior", ["other-volume", "map-link-conflict", "unknown", "special-link"]
)
def test_interior_contradiction_must_not_prove_unmapped_coverage(
    env: CoverageSync, interior: str
) -> None:
    owned = env.volume(1)
    other = env.volume(
        2, status="wanted", monitored=False, special=interior == "special-link"
    )
    mapping: dict[str, Any] = {"1": 1, "10": 1}
    if interior == "other-volume":
        mapping["5"] = 2
    elif interior == "map-link-conflict":
        mapping["5"] = 2
        env.chapter(5, volume_id=owned, status="downloaded")
    elif interior == "unknown":
        mapping["5"] = None
    else:
        env.chapter(5, volume_id=other, status="downloaded")
    env.mapping(mapping)
    env.chapter(6)
    result = env.sync()
    assert (result, env.queued_chapters()) == ((0, 1), [6.0])


@pytest.mark.parametrize("kind", ["arbitrary-file", "cover-only", "metadata-only"])
def test_non_content_paths_must_not_suppress_real_sync(
    env: CoverageSync, kind: str
) -> None:
    env.volume(1)
    env.mapping({"1": 1, "10": 1})
    env.chapter(5)
    path = env.library / "not-volume-content"
    if kind == "arbitrary-file":
        path.write_text("not manga content")
    else:
        path.mkdir()
        (path / ("cover.jpg" if kind == "cover-only" else "ComicInfo.xml")).write_bytes(
            b"nonempty"
        )
    with sqlite3.connect(env.db_path) as db:
        db.execute("UPDATE volumes SET import_path=?", (str(path),))
    result = env.sync()
    assert (result, env.queued_chapters()) == ((0, 1), [5.0])


class GuardedRow(Mapping[str, Any]):
    """Make the repository's row-lifetime contract observable in tests."""

    def __init__(self, row: sqlite3.Row, active: list[bool]) -> None:
        self.row = row
        self.active = active

    def __getitem__(self, key: str) -> Any:
        assert self.active[0], "sqlite row accessed after get_db exit"
        return self.row[key]

    def __iter__(self) -> Iterator[str]:
        assert self.active[0], "sqlite row accessed after get_db exit"
        return iter(self.row.keys())

    def __len__(self) -> int:
        return len(self.row)


def test_rows_are_materialized_inside_db_context(
    env: CoverageSync, monkeypatch: pytest.MonkeyPatch
) -> None:
    from routers import suwayomi_ as swy

    owned = env.volume(1)
    env.chapter(1, volume_id=owned, status="downloaded")
    env.chapter(10, volume_id=owned, status="downloaded")
    real_get_db = swy.get_db

    @contextmanager
    def guarded_db() -> Generator[sqlite3.Connection, None, None]:
        active = [True]
        with real_get_db() as db:
            db.row_factory = lambda cursor, row: GuardedRow(
                sqlite3.Row(cursor, row), active
            )
            try:
                yield db
            finally:
                active[0] = False

    monkeypatch.setattr(swy, "get_db", guarded_db)
    assert swy._suwayomi_local_chapter_coverage(7, '{"5": 1}', [(5.0, None)]) == {5.0}
    assert swy._suwayomi_local_chapter_coverage(7, None, [(6.0, None)]) == {6.0}

    # Isolate sync's row lifetime from the separately owned grab/source code.
    dispatches: list[tuple[str, float]] = []

    async def find_manga(*_args: Any, **_kwargs: Any) -> int:
        return 101

    async def grab_volume(_series_id: int, number: float) -> bool:
        dispatches.append(("volume", number))
        return True

    async def grab_chapter(_series_id: int, number: float) -> bool:
        dispatches.append(("chapter", number))
        return True

    monkeypatch.setattr(
        swy, "_get_series_source", lambda *_args: {"source_name": "MangaDex"}
    )
    monkeypatch.setattr(swy, "find_or_add_manga", find_manga)
    monkeypatch.setattr(swy, "suwayomi_grab", grab_volume)
    monkeypatch.setattr(swy, "suwayomi_chapter_grab", grab_chapter)
    wanted = env.volume(2, status="wanted")
    env.chapter(12, volume_id=wanted)
    env.feed[12] = "Vol.2 Ch.12"
    env.chapter(5)
    env.chapter(11)
    assert env.sync() == (1, 1)
    assert dispatches == [("volume", 2.0), ("chapter", 11.0)]


def test_newly_discovered_named_special_is_not_range_inferred(
    env: CoverageSync,
) -> None:
    env.volume(1)
    env.mapping({"1": 1, "10": 1})
    env.feed[5] = "Bonus special"
    with sqlite3.connect(env.db_path) as db:
        db.execute("UPDATE series SET status='RELEASING' WHERE id=7")
    result = env.sync()
    assert (result, env.queued_chapters()) == ((0, 1), [5.0])
    assert env.rows("SELECT title FROM chapters WHERE chapter_num=5") == [
        {"title": "Bonus special"}
    ]


@pytest.mark.parametrize(
    "name,title,queued",
    [
        ("Ch.5", None, []),
        ("Chapter 005", None, []),
        ("5.0", None, []),
        ("Chapter 6", "Chapter 6", [5.0]),
        ("Chapter 5: Bonus special", "Chapter 5: Bonus special", [5.0]),
    ],
)
def test_discovery_preserves_meaningful_names_but_not_plain_number_labels(
    env: CoverageSync, name: str, title: str | None, queued: list[float]
) -> None:
    env.volume(1)
    env.mapping({"1": 1, "10": 1})
    env.feed[5] = name
    with sqlite3.connect(env.db_path) as db:
        db.execute("UPDATE series SET status='RELEASING' WHERE id=7")

    assert env.sync() == (0, len(queued))
    assert env.queued_chapters() == queued
    assert env.rows("SELECT title FROM chapters WHERE chapter_num=5") == [
        {"title": title}
    ]


@pytest.mark.parametrize("title", [None, "Operator-owned title"])
def test_existing_chapter_metadata_is_not_overwritten_by_feed_names(
    env: CoverageSync, title: str | None
) -> None:
    env.volume(1)
    env.mapping({"1": 1, "10": 1})
    env.chapter(5, title=title)
    env.feed[5] = "Bonus special"
    with sqlite3.connect(env.db_path) as db:
        db.execute("UPDATE series SET status='RELEASING' WHERE id=7")
    before = env.rows("SELECT id, title, volume_id, monitored FROM chapters")

    assert env.sync() == (0, 1)
    assert env.queued_chapters() == [5.0]
    assert env.rows("SELECT id, title, volume_id, monitored FROM chapters") == before
