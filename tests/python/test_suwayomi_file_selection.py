"""Exact chapter identity and complete Suwayomi volume assembly regressions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
from pathlib import Path
import sqlite3
from typing import Any
import zipfile

from fastapi.testclient import TestClient
import pytest

from routers import suwayomi_ as swy


def cbz(path: Path, page: bytes = b"page") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.png", page)


@pytest.mark.parametrize(
    "name,number",
    [
        ("ComicDom_Vol.3 Ch.17.cbz", 17),
        ("unofficial_Ch. 17.cbz", 17),
        ("Official_Chapter 17.cbz", 17),
        ("Alpha_# 17.cbz", 17),
        ("Unknown_Mission 126.cbz", 126),
        ("Official_quest 81.cbz", 81),
        ("Official_Chime 14.cbz", 14),
        ("Delta_Chapter 17.5.cbz", 17.5),
    ],
)
def test_reported_chapter_names(tmp_path: Path, name: str, number: float) -> None:
    cbz(tmp_path / name)
    assert swy._chapter_cbz(str(tmp_path), number) == str(tmp_path / name)


@pytest.mark.parametrize(
    "name,number",
    [
        ("Group_Ch.17.5.cbz", 17),
        ("Group_Ch.170.cbz", 17),
        ("Group_Ch.117.cbz", 17),
        ("Group_Ch.17.55.cbz", 17.5),
        ("Group_Ch.17x5.cbz", 17.5),
        ("Group_Chapter 10 - The 2nd Battle.cbz", 2),
        ("Group_Chapter 10 - Ch.17.cbz", 17),
        ("Group_Chapter 10 - Extra_Chapter 17.cbz", 17),
        ("Group_Mission 10 - Extra_Chapter 17.cbz", 17),
        ("Group_Chapter 10.5.1 - Extra_Chapter 17.cbz", 17),
        ("Group_Mission 17 - Extra.cbz", 17),
        ("Group_Mission 10 - Extra_Mission 17.cbz", 17),
        ("Group_Two Words 17.cbz", 17),
        ("Mission 17.cbz", 17),
        ("Group_Chapter 17.5.1.cbz", 17.5),
    ],
)
def test_no_numeric_prefix_or_title_number_matches(
    tmp_path: Path, name: str, number: float
) -> None:
    cbz(tmp_path / name)
    assert swy._chapter_cbz(str(tmp_path), number) is None


@pytest.mark.parametrize("reverse", [False, True])
def test_identity_strength_before_filename_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reverse: bool
) -> None:
    names = [
        "A_Mission 17.cbz",
        "A_Ch.17 - Extra story 2.cbz",
        "Long Scanlator_Chapter 17.cbz",
    ]
    for name in names:
        cbz(tmp_path / name)
    monkeypatch.setattr(
        swy.os, "listdir", lambda _: list(reversed(names)) if reverse else names
    )
    assert swy._chapter_cbz(str(tmp_path), 17) == str(tmp_path / names[2])


def test_duplicate_tie_is_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = ["Delta_Chapter 90.cbz", "Alpha_# 90.cbz"]
    for name in names:
        cbz(tmp_path / name)
    monkeypatch.setattr(swy.os, "listdir", lambda _: list(reversed(names)))
    first = swy._chapter_cbz(str(tmp_path), 90)
    monkeypatch.setattr(swy.os, "listdir", lambda _: names)
    assert swy._chapter_cbz(str(tmp_path), 90) == first


@pytest.mark.parametrize("merge", [True, False])
@pytest.mark.parametrize(
    "name",
    [
        "Group_Mission 10 - Extra_Chapter 17.cbz",
        "Group_Chapter 10.5.1 - Extra_Chapter 17.cbz",
    ],
)
def test_secondary_title_identity_cannot_complete_volume(
    env: ImportEnv, merge: bool, name: str
) -> None:
    env.client["merge_chapters"] = merge
    cbz(env.manga_dir / name, b"not-chapter-17")
    env.queue([node(17, 17)])
    before = env.row("volumes")
    env.process()
    assert env.row("suwayomi_downloads")["status"] == "error"
    assert env.row("volumes") == before
    assert not env.library.exists()


@pytest.mark.parametrize("requested,other", [(1, 1.5), (1.5, 1.55)])
def test_volume_label_has_exact_numeric_boundary(
    tmp_path: Path, requested: float, other: float
) -> None:
    cbz(tmp_path / f"Vol.{other} Ch.1.cbz")
    assert swy._vol_chapter_cbzs(str(tmp_path), requested) == []


def test_cbz_named_directory_is_not_a_chapter(tmp_path: Path) -> None:
    (tmp_path / "Official_Chapter 17.cbz").mkdir()
    assert swy._chapter_cbz(str(tmp_path), 17) is None
    (tmp_path / "Vol.1 Ch.17.cbz").mkdir()
    assert swy._vol_chapter_cbzs(str(tmp_path), 1) == []


@dataclass
class ImportEnv:
    db_path: Path
    manga_dir: Path
    library: Path
    client: dict[str, Any]
    nodes: list[dict[str, Any]] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    enqueued: list[list[int]] = field(default_factory=list)

    def configure_grab(self, chapter_map: str, *, map_source: str = "mangadex") -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE volumes SET status='wanted' WHERE id=1")
            db.execute(
                "UPDATE series SET chapter_vol_map=?,chapter_map_source=? WHERE id=1",
                (chapter_map, map_source),
            )
            db.execute(
                "INSERT INTO download_clients(id,name,type,host,enabled,download_path,merge_chapters)"
                " VALUES(1,'swy','suwayomi','http://swy.invalid',1,?,?)",
                (
                    self.client["download_path"],
                    int(self.client.get("merge_chapters", True)),
                ),
            )
            db.execute(
                "INSERT INTO suwayomi_sources(series_id,source_id,source_name,suwayomi_manga_id)"
                " VALUES(1,'source','Source',999)"
            )

    async def gql(
        self,
        _client: dict[str, Any],
        query: str,
        _variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.queries.append(query)
        # Mirror GraphQL: fields not requested are not returned.
        nodes = [dict(node) for node in self.nodes]
        if "chapterNumber" not in query:
            for node in nodes:
                node.pop("chapterNumber", None)
        if "fetchChapters" in query:
            assert _variables == {"mid": 999}
            return {"fetchChapters": {"chapters": nodes}}
        if "enqueueChapterDownloads" in query:
            assert _variables is not None
            self.enqueued.append(list(_variables["ids"]))
            return {"enqueueChapterDownloads": {"clientMutationId": None}}
        assert "manga(id:" in query, f"Unexpected GraphQL operation: {query}"
        return {"manga": {"title": "Selection", "chapters": {"nodes": nodes}}}

    def row(self, table: str) -> dict[str, Any]:
        assert table in {"suwayomi_downloads", "volumes", "series", "chapters"}
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(f"SELECT * FROM {table} WHERE id=1").fetchone()
        assert row is not None
        return dict(row)

    def queue(
        self,
        nodes: list[dict[str, Any]],
        ids: list[int] | None = None,
        chapter: float | None = None,
    ) -> None:
        self.nodes = nodes
        ids = ids if ids is not None else [node["id"] for node in nodes]
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "INSERT INTO suwayomi_downloads(id, series_id, volume_num, chapter_num,"
                " suwayomi_manga_id, chapter_ids, status, total) VALUES(1,1,1,?,999,?,'queued',?)",
                (chapter, json.dumps(ids), len(ids)),
            )

    def process(self) -> None:
        asyncio.run(
            swy._process_suwayomi_job(self.client, self.row("suwayomi_downloads"))
        )


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ImportEnv:
    import main
    import security
    import shared

    db_path = tmp_path / "selection.db"
    monkeypatch.setattr(main, "DB_PATH", str(db_path))
    monkeypatch.setattr(shared, "DB_PATH", str(db_path))
    monkeypatch.setattr(main, "CONFIG", {})
    monkeypatch.setattr(shared, "CONFIG", {})
    monkeypatch.setattr(security, "_SECRET_CIPHER", None)
    security.load_or_create_secret_cipher(str(tmp_path / "keys"))
    main.init_db()
    main.load_config()
    library = tmp_path / "library"
    monkeypatch.setattr(main, "_series_library_dir", lambda db, sid: str(library))
    manga_dir = tmp_path / "swy" / "mangas" / "Source" / "Selection"
    manga_dir.mkdir(parents=True)
    result = ImportEnv(
        db_path, manga_dir, library, {"download_path": str(tmp_path / "swy")}
    )
    monkeypatch.setattr(swy, "_gql", result.gql)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO series(id,title,search_pattern,anilist_id,mangadex_id,monitored,chapter_vol_map)"
            " VALUES(1,'Selection','Selection',123,'provider-id',0,?)",
            ('{"45.1":1,"45.2":1}',),
        )
        db.execute(
            "INSERT INTO volumes(id,series_id,volume_num,status,quality,size_bytes,monitored,torrent_name)"
            " VALUES(1,1,1,'grabbed','local-quality',42,0,'original-release')"
        )
        db.execute(
            "INSERT INTO chapters(id,series_id,chapter_num,status,monitored) VALUES(1,1,17,'grabbed',0)"
        )
    return result


def node(cid: int, number: Any) -> dict[str, Any]:
    return {"id": cid, "chapterNumber": number, "isDownloaded": True}


@pytest.mark.parametrize("merge", [True, False])
def test_job_assembles_only_queued_chapters_once_in_numeric_order(
    env: ImportEnv, merge: bool
) -> None:
    env.client["merge_chapters"] = merge
    for name, page in [
        ("Delta_Chapter 10.cbz", b"ten"),
        ("Delta_Chapter 2.cbz", b"two"),
        ("Alpha_# 2.cbz", b"two"),
        ("Official_quest 2.5.cbz", b"two-and-half"),
        ("Vol.1 Ch.99.cbz", b"not-in-job"),
    ]:
        cbz(env.manga_dir / name, page)
    env.queue(
        [node(10, 10), node(2, 2), node(22, 2), node(25, 2.5), node(99, 99)],
        [10, 2, 22, 25],
    )
    before_series = env.row("series")
    before_chapter = env.row("chapters")
    env.process()
    job, volume = env.row("suwayomi_downloads"), env.row("volumes")
    assert job["status"] == "completed", job["error"]
    assert job["chapter_ids"] == "[10, 2, 22, 25]"
    assert job["progress"] == 4
    assert env.queries and len(env.queries) == 1
    assert env.row("series") == before_series
    assert env.row("chapters") == before_chapter
    assert (
        volume["quality"],
        volume["size_bytes"],
        volume["monitored"],
        volume["torrent_name"],
    ) == ("local-quality", 42, 0, "original-release")
    assert volume["status"] == "downloaded"
    if merge:
        with zipfile.ZipFile(volume["import_path"]) as archive:
            assert archive.namelist() == ["0001.png", "0002.png", "0003.png"]
            assert [archive.read(name) for name in archive.namelist()] == [
                b"two",
                b"two-and-half",
                b"ten",
            ]
    else:
        copied = sorted(Path(volume["import_path"]).glob("*.cbz"))
        assert len(copied) == 3
        assert not any("99" in path.name for path in copied)


@pytest.mark.parametrize("merge", [True, False])
@pytest.mark.parametrize(
    "problem",
    ["missing-file", "unknown-number", "invalid-number", "empty-job", "split"],
)
def test_incomplete_job_cannot_mutate_library_or_complete(
    env: ImportEnv, merge: bool, problem: str
) -> None:
    env.client["merge_chapters"] = merge
    cbz(env.manga_dir / "Vol.1 Ch.1.cbz", b"one")
    cbz(env.manga_dir / "Official_Chapter 45.cbz", b"whole")
    nodes = [node(1, 1), node(2, 2)]
    if problem == "unknown-number":
        nodes[1]["chapterNumber"] = None
    elif problem == "invalid-number":
        nodes[1]["chapterNumber"] = "NaN"
    elif problem == "empty-job":
        nodes = []
    elif problem == "split":
        nodes = [node(451, 45.1), node(452, 45.2)]
    env.queue(nodes)
    before = env.row("volumes")
    env.process()
    assert env.row("suwayomi_downloads")["status"] == "error"
    assert env.row("volumes") == before
    assert not env.library.exists()
    assert len(env.queries) == 1


def test_missing_queued_id_stays_pending_without_import(env: ImportEnv) -> None:
    cbz(env.manga_dir / "Vol.1 Ch.1.cbz")
    env.queue([node(1, 1)], [1, 2])
    before = env.row("volumes")
    env.process()
    assert env.row("suwayomi_downloads")["status"] == "queued"
    assert env.row("suwayomi_downloads")["progress"] == 1
    assert env.row("volumes") == before
    assert not env.library.exists()


def test_whole_source_job_does_not_use_metadata_split_map(env: ImportEnv) -> None:
    """Assembly only: this manually queued job does not prove grab eligibility."""
    cbz(env.manga_dir / "Official_Chapter 45.cbz", b"whole-45")
    env.queue([node(45, 45)])
    env.process()
    assert env.row("suwayomi_downloads")["status"] == "completed"
    with zipfile.ZipFile(env.row("volumes")["import_path"]) as archive:
        assert [archive.read(name) for name in archive.namelist()] == [b"whole-45"]


@pytest.mark.parametrize(
    "source_name,chapter_map,can_queue",
    [
        ("Chapter 45", '{"45.1":1,"45.2":1}', False),
        ("Chapter 45", '{"45":1,"45.1":1,"45.2":1}', False),
        ("Chapter 45", '{"45.1":1,"45.2":2}', False),
        ("Vol.2 Chapter 45", '{"45.1":1,"45.2":1}', False),
        ("Vol.1 Chapter 45", '{"45.1":1,"45.2":1}', True),
        ("Chapter 45", '{"45":1}', True),
    ],
)
def test_real_grab_then_process_whole_source_with_split_metadata(
    env: ImportEnv, source_name: str, chapter_map: str, can_queue: bool
) -> None:
    cbz(env.manga_dir / "Official_Chapter 45.cbz", b"whole-45")
    env.nodes = [
        {**node(4500, 45), "name": source_name, "sourceOrder": 1},
    ]
    env.configure_grab(
        chapter_map, map_source="manual" if chapter_map == '{"45":1}' else "mangadex"
    )
    before_chapter = env.row("chapters")
    before_series = env.row("series")
    before_volume = env.row("volumes")
    assert before_series["chapter_vol_map"] == chapter_map
    assert asyncio.run(swy.suwayomi_grab(1, 1)) is can_queue
    if not can_queue:
        # The live feed cannot prove volume membership from split-map numbers.
        # Exercise the real poll too: it must not invent a job or import content.
        asyncio.run(swy.check_suwayomi_jobs())
        with sqlite3.connect(env.db_path) as db:
            assert (
                db.execute("SELECT COUNT(*) FROM suwayomi_downloads").fetchone()[0] == 0
            )
        assert env.enqueued == []
        assert len(env.queries) == 1
        assert env.row("series") == before_series
        assert env.row("chapters") == before_chapter
        assert env.row("volumes") == before_volume
        assert not env.library.exists()
        return
    job = env.row("suwayomi_downloads")
    assert json.loads(job["chapter_ids"]) == [4500]
    assert job["status"] == "queued"
    assert env.enqueued == [[4500]]
    asyncio.run(swy.check_suwayomi_jobs())
    assert env.row("suwayomi_downloads")["status"] == "completed"
    volume = env.row("volumes")
    assert volume["status"] == "downloaded"
    assert volume["quality"] == "local-quality"
    assert volume["size_bytes"] == 42
    assert volume["monitored"] == 0
    assert len(env.queries) == 3
    assert env.row("chapters") == before_chapter
    after_series = env.row("series")
    assert after_series["suwayomi_id"] == 999
    assert {
        key: value for key, value in after_series.items() if key != "suwayomi_id"
    } == {key: value for key, value in before_series.items() if key != "suwayomi_id"}
    with zipfile.ZipFile(volume["import_path"]) as archive:
        assert archive.namelist() == ["0001.png"]
        assert archive.read("0001.png") == b"whole-45"


@pytest.mark.parametrize("merge", [True, False])
def test_edit_post_replaces_split_map_for_real_whole_source_grab(
    env: ImportEnv, merge: bool
) -> None:
    import main

    env.client["merge_chapters"] = merge
    env.configure_grab('{"45.1":1,"45.2":1}')
    env.nodes = [{**node(4500, 45), "name": "Chapter 45", "sourceOrder": 1}]
    cbz(env.manga_dir / "Official_Chapter 45.cbz", b"whole-45")
    before_series = env.row("series")
    before_chapter = env.row("chapters")
    before_volume = env.row("volumes")
    client = TestClient(main.app)
    token = "test-csrf-" + "a" * 32
    client.cookies.set("csrftoken", token)

    # The separate Chapter Map editor does not replace the selector's map.
    response = client.post(
        "/series/1/chapter-map",
        json={"overrides": {"45": 1}},
        headers={"X-CSRFToken": token},
    )
    assert response.status_code == 200
    with sqlite3.connect(env.db_path) as db:
        assert db.execute(
            "SELECT chapter,volume_num FROM series_chapter_overrides WHERE series_id=1"
        ).fetchall() == [("45", 1.0)]
    assert env.row("series") == before_series
    assert asyncio.run(swy.suwayomi_grab(1, 1)) is False
    asyncio.run(swy.check_suwayomi_jobs())
    assert env.enqueued == []
    assert env.row("volumes") == before_volume
    with sqlite3.connect(env.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM suwayomi_downloads").fetchone()[0] == 0
    assert not env.library.exists()

    # One line represents volume 1; replace, rather than augment, split entries.
    response = client.post(
        "/series/1/edit",
        data={"chapter_map_text": "45", "csrf_token": token},
        headers={"X-CSRFToken": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/series/1"
    edited_series = env.row("series")
    assert json.loads(edited_series["chapter_vol_map"]) == {"45": 1}
    assert edited_series["chapter_map_source"] == "manual"
    assert edited_series["chapter_map_updated_at"]
    map_fields = {"chapter_vol_map", "chapter_map_source", "chapter_map_updated_at"}
    assert {k: v for k, v in edited_series.items() if k not in map_fields} == {
        k: v for k, v in before_series.items() if k not in map_fields
    }
    with sqlite3.connect(env.db_path) as db:
        value, source, locked = db.execute(
            "SELECT value_json,selected_source,locked FROM series_metadata_fields"
            " WHERE series_id=1 AND field_name='chapter_vol_map'"
        ).fetchone()
    assert json.loads(value) == {"45": 1}
    assert (source, locked) == ("manual", 1)
    assert env.row("chapters") == before_chapter
    assert env.row("volumes") == before_volume

    assert asyncio.run(swy.suwayomi_grab(1, 1)) is True
    assert env.enqueued == [[4500]]
    assert json.loads(env.row("suwayomi_downloads")["chapter_ids"]) == [4500]
    asyncio.run(swy.check_suwayomi_jobs())
    assert env.row("suwayomi_downloads")["status"] == "completed"
    volume = env.row("volumes")
    assert volume["status"] == "downloaded"
    assert (volume["quality"], volume["size_bytes"], volume["monitored"]) == (
        "local-quality",
        42,
        0,
    )
    after_series = env.row("series")
    assert after_series["suwayomi_id"] == 999
    assert {k: v for k, v in after_series.items() if k != "suwayomi_id"} == {
        k: v for k, v in edited_series.items() if k != "suwayomi_id"
    }
    assert env.row("chapters") == before_chapter
    output = Path(volume["import_path"])
    if not merge:
        assert [path.name for path in output.iterdir()] == ["Official_Chapter 45.cbz"]
        output /= "Official_Chapter 45.cbz"
    with zipfile.ZipFile(output) as archive:
        assert [archive.read(name) for name in archive.namelist()] == [b"whole-45"]


@pytest.mark.parametrize("merge", [True, False])
@pytest.mark.parametrize(
    "mapping", ["chapter-map", "mangadex-cache", "chapter-map-invalid-source"]
)
def test_real_grab_cannot_publish_subset_of_split_metadata(
    env: ImportEnv, merge: bool, mapping: str
) -> None:
    env.client["merge_chapters"] = merge
    env.configure_grab(
        "{}" if mapping == "mangadex-cache" else '{"43":1,"44.1":1,"44.2":1}'
    )
    if mapping != "chapter-map":
        with sqlite3.connect(env.db_path) as db:
            db.executemany(
                "INSERT INTO mangadex_chapters(mangadex_chapter_id,series_id,chapter_num,volume_num)"
                " VALUES(?,1,?,1)",
                [("md-43", 43)]
                if mapping == "chapter-map-invalid-source"
                else [("md-43", 43), ("md-44.1", 44.1), ("md-44.2", 44.2)],
            )
    env.nodes = [
        {**node(4300, 43), "name": "Chapter 43", "sourceOrder": 2},
        {**node(4400, 44), "name": "Chapter 44", "sourceOrder": 1},
    ]
    if mapping == "chapter-map-invalid-source":
        env.nodes.append({**node(4500, "unknown"), "name": "Special", "sourceOrder": 0})
    cbz(env.manga_dir / "Official_Chapter 43.cbz", b"chapter-43")
    cbz(env.manga_dir / "Official_Chapter 44.cbz", b"whole-44-not-proven-equivalent")
    before_series = env.row("series")
    before_chapter = env.row("chapters")
    before_volume = env.row("volumes")
    grabbed = asyncio.run(swy.suwayomi_grab(1, 1))
    asyncio.run(swy.check_suwayomi_jobs())
    assert env.row("volumes") == before_volume
    assert grabbed is False
    with sqlite3.connect(env.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM suwayomi_downloads").fetchone()[0] == 0
    assert env.enqueued == []
    assert len(env.queries) == 1
    assert env.row("series") == before_series
    assert env.row("chapters") == before_chapter
    assert not env.library.exists()


@pytest.mark.parametrize("mapping", ["chapter-map", "mangadex-cache"])
@pytest.mark.parametrize("merge", [True, False])
@pytest.mark.parametrize("missing_file", [True, False])
def test_real_split_grab_preserves_ids_and_requires_exact_files(
    env: ImportEnv, mapping: str, merge: bool, missing_file: bool
) -> None:
    env.client["merge_chapters"] = merge
    env.configure_grab('{"45.1":1,"45.2":1}' if mapping == "chapter-map" else "{}")
    if mapping == "mangadex-cache":
        with sqlite3.connect(env.db_path) as db:
            db.executemany(
                "INSERT INTO mangadex_chapters(mangadex_chapter_id,series_id,chapter_num,volume_num)"
                " VALUES(?,1,?,1)",
                [("md-45.1", 45.1), ("md-45.2", 45.2)],
            )
    env.nodes = [
        {**node(451, 45.1), "name": "Chapter 45.1", "sourceOrder": 2},
        {**node(452, 45.2), "name": "Chapter 45.2", "sourceOrder": 1},
    ]
    cbz(env.manga_dir / "Vol.1 Ch.45.1.cbz", b"part-one")
    cbz(env.manga_dir / "Vol.1 Ch.45.cbz", b"not-an-exact-part")
    if not missing_file:
        cbz(env.manga_dir / "Official_Chapter 45.2.cbz", b"part-two")
    assert asyncio.run(swy.suwayomi_grab(1, 1)) is True
    assert json.loads(env.row("suwayomi_downloads")["chapter_ids"]) == [451, 452]
    before_volume = env.row("volumes")
    asyncio.run(swy.check_suwayomi_jobs())
    job, volume = env.row("suwayomi_downloads"), env.row("volumes")
    assert json.loads(job["chapter_ids"]) == [451, 452]
    assert env.enqueued == [[451, 452]]
    if missing_file:
        assert job["status"] == "error"
        assert volume == before_volume
        assert not env.library.exists()
    else:
        assert job["status"] == "completed"
        assert volume["status"] == "downloaded"
        if merge:
            with zipfile.ZipFile(volume["import_path"]) as archive:
                assert [archive.read(name) for name in archive.namelist()] == [
                    b"part-one",
                    b"part-two",
                ]
        else:
            assert sorted(
                path.name for path in Path(volume["import_path"]).iterdir()
            ) == ["Official_Chapter 45.2.cbz", "Vol.1 Ch.45.1.cbz"]


def test_actual_chapter_job_imports_non_mangadex_name(env: ImportEnv) -> None:
    cbz(env.manga_dir / "unofficial_Ch. 17.cbz", b"seventeen")
    env.queue([node(17, 17)], chapter=17)
    env.process()
    assert env.row("suwayomi_downloads")["status"] == "completed"
    chapter = env.row("chapters")
    assert chapter["status"] == "downloaded"
    assert chapter["monitored"] == 0
    with zipfile.ZipFile(chapter["import_path"]) as archive:
        assert archive.read("001.png") == b"seventeen"


@pytest.mark.parametrize("number", [True, -1, float("inf"), "", {}, "not-a-number"])
def test_invalid_source_numbers_fail_without_import(
    env: ImportEnv, number: Any
) -> None:
    cbz(env.manga_dir / "Vol.1 Ch.1.cbz")
    env.queue([node(1, 1), node(2, number)])
    before = env.row("volumes")
    env.process()
    assert env.row("suwayomi_downloads")["status"] == "error"
    assert env.row("volumes") == before
    assert not env.library.exists()


def test_mixed_split_and_whole_job_cannot_guess_coverage(env: ImportEnv) -> None:
    cbz(env.manga_dir / "Official_Chapter 45.cbz", b"whole")
    env.queue([node(45, 45), node(451, 45.1), node(452, 45.2)])
    env.process()
    assert env.row("suwayomi_downloads")["status"] == "error"
    assert not env.library.exists()


def test_chapter_symlink_cannot_select_outside_file(tmp_path: Path) -> None:
    outside = tmp_path / "outside.cbz"
    cbz(outside)
    manga_dir = tmp_path / "manga"
    manga_dir.mkdir()
    (manga_dir / "Vol.1 Ch.17.cbz").symlink_to(outside)
    assert swy._chapter_cbz(str(manga_dir), 17) is None
    assert swy._vol_chapter_cbzs(str(manga_dir), 1) == []


def test_volume_named_variants_deduplicate_and_sort_numerically(tmp_path: Path) -> None:
    for name in [
        "Group_Vol.1 Chapter 10.cbz",
        "Group_Vol.1 Ch.2.cbz",
        "Other_Vol.1 # 2.cbz",
    ]:
        cbz(tmp_path / name)
    paths = swy._vol_chapter_cbzs(str(tmp_path), 1)
    assert len(paths) == 2
    assert Path(paths[0]).name in {"Group_Vol.1 Ch.2.cbz", "Other_Vol.1 # 2.cbz"}
    assert Path(paths[1]).name == "Group_Vol.1 Chapter 10.cbz"


@pytest.mark.parametrize("merge", [True, False])
def test_incomplete_selection_preserves_already_downloaded_content(
    env: ImportEnv, merge: bool
) -> None:
    env.client["merge_chapters"] = merge
    env.library.mkdir()
    existing = env.library / "Selection v01.cbz"
    cbz(existing, b"local-downloaded-content")
    original = existing.read_bytes()
    cbz(env.manga_dir / "Vol.1 Ch.1.cbz", b"source-partial")
    with sqlite3.connect(env.db_path) as db:
        db.execute(
            "UPDATE volumes SET status='downloaded',import_path=?,imported_at='2026-01-01' WHERE id=1",
            (str(existing),),
        )
    env.queue([node(1, 1), node(2, 2)])
    before = env.row("volumes")
    env.process()
    assert env.row("suwayomi_downloads")["status"] == "error"
    assert env.row("volumes") == before
    assert existing.read_bytes() == original
    assert list(env.library.iterdir()) == [existing]
