"""Exact chapter identity and complete Suwayomi volume assembly regressions."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
from typing import Any
import zipfile

from fastapi.testclient import TestClient
import pytest

from routers import suwayomi_ as swy
from test_suwayomi_local_coverage import GuardedRow


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
        ("Chime Scanlations_Chapter 17.cbz", 17),
        ("Mission Scanlations_Chapter 17.cbz", 17),
        ("Quest Scanlations_Chapter 17.cbz", 17),
        ("Chapterhouse_Chapter 17.cbz", 17),
        ("Volcano_Chapter 17.cbz", 17),
        ("Chapter 10 - Extra_Chapter 17.cbz", 10),
        ("Vol.1 Chapter 10 - Extra_Chapter 17.cbz", 10),
        ("Group_Chapter 10 - Extra_Chapter 17.cbz", 10),
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
        ("Chapter 10.5.1 - Extra_Chapter 17.cbz", 17),
        ("Ch.10.5.1 - Extra_Chapter 17.cbz", 17),
        ("# 10.5.1 - Extra_Chapter 17.cbz", 17),
        ("Vol.1.2.3 - Extra_Chapter 17.cbz", 17),
        ("Vol.1 Chapter 10.5.1 - Extra_Chapter 17.cbz", 17),
        ("Chapter17 - Extra_Chapter 17.cbz", 17),
        ("Chapter unknown_Chapter 17.cbz", 17),
        (" Ch.unknown_Chapter 17.cbz", 17),
        ("# unknown_Mission 17.cbz", 17),
        ("Vol.unknown_Mission 17.cbz", 17),
    ],
)
def test_no_numeric_prefix_or_title_number_matches(
    tmp_path: Path, name: str, number: float
) -> None:
    cbz(tmp_path / name)
    assert swy._chapter_cbz(str(tmp_path), number) is None


@pytest.mark.parametrize("label", ["Mission", "Chime", "quest"])
@pytest.mark.parametrize("separator", [" ", ""])
def test_bare_custom_title_cannot_match_secondary(
    tmp_path: Path, label: str, separator: str
) -> None:
    name = f"{label}{separator}10 - Extra_Chapter 17.cbz"
    cbz(tmp_path / name, b"not-chapter-17")
    assert swy._chapter_cbz(str(tmp_path), 17) is None


@pytest.mark.parametrize("label", ["Mission", "Chime", "quest"])
@pytest.mark.parametrize("merge", [True, False])
def test_bare_custom_title_cannot_complete_volume(
    env: ImportEnv, label: str, merge: bool
) -> None:
    env.client["merge_chapters"] = merge
    cbz(env.manga_dir / f"{label} 10 - Extra_Chapter 17.cbz", b"chapter-ten")
    env.queue([node(17, 17)])
    before = env.row("volumes")
    env.process()
    assert env.row("suwayomi_downloads")["status"] == "error"
    assert env.row("volumes") == before
    assert not env.library.exists()


@pytest.mark.parametrize(
    "prefix",
    ["Episode 10.5.1 - Extra", "Scene10x5", "Part -10", "Act .5", "Team7", "2000"],
)
def test_numeric_scanlator_prefix_is_ambiguous(tmp_path: Path, prefix: str) -> None:
    cbz(tmp_path / f"{prefix}_Chapter 17.cbz")
    assert swy._chapter_cbz(str(tmp_path), 17) is None


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
        "Chapter 10.5.1 - Extra_Chapter 17.cbz",
        "Ch.10.5.1 - Extra_Chapter 17.cbz",
        "# 10.5.1 - Extra_Chapter 17.cbz",
        "Vol.1.2.3 - Extra_Chapter 17.cbz",
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
        for field_name in ("name", "scanlator"):
            if field_name not in query:
                for node in nodes:
                    node.pop(field_name, None)
        if "fetchChapters" in query:
            assert _variables == {"mid": 999}
            return {"fetchChapters": {"chapters": nodes}}
        if "enqueueChapterDownloads" in query:
            assert _variables is not None
            self.enqueued.append(list(_variables["ids"]))
            return {"enqueueChapterDownloads": {"clientMutationId": None}}
        assert "manga(id:" in query, f"Unexpected GraphQL operation: {query}"
        return {
            "manga": {
                "title": "Selection",
                "source": {"displayName": "Source"},
                "chapters": {"nodes": nodes},
            }
        }

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
        [
            {**node(10, 10), "name": "Chapter 10", "scanlator": "Delta"},
            {**node(2, 2), "name": "# 2", "scanlator": "Alpha"},
            {**node(22, 2), "name": "Chapter 2", "scanlator": "Delta"},
            {**node(25, 2.5), "name": "quest 2.5", "scanlator": "Official"},
            {**node(99, 99), "name": "Vol.1 Ch.99", "scanlator": None},
        ],
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
    env.queue([{**node(45, 45), "name": "Chapter 45", "scanlator": "Official"}])
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
    cbz(env.manga_dir / f"Official_{source_name}.cbz", b"whole-45")
    env.nodes = [
        {
            **node(4500, 45),
            "name": source_name,
            "scanlator": "Official",
            "sourceOrder": 1,
        },
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
    env.nodes = [
        {
            **node(4500, 45),
            "name": "Chapter 45",
            "scanlator": "Official",
            "sourceOrder": 1,
        }
    ]
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


@pytest.mark.parametrize("merge", [True, False])
@pytest.mark.parametrize("bad", [{"unknown": 1}, {"99": "unknown"}])
def test_malformed_authoritative_map_cannot_enable_cached_subset(
    env: ImportEnv, merge: bool, bad: dict[str, Any]
) -> None:
    env.client["merge_chapters"] = merge
    env.configure_grab(
        json.dumps({"43": 1, "44.1": 1, "44.2": 1, **bad}), map_source="manual"
    )
    with sqlite3.connect(env.db_path) as db:
        db.execute(
            "INSERT INTO mangadex_chapters(mangadex_chapter_id,series_id,chapter_num,volume_num)"
            " VALUES('cached-43',1,43,1)"
        )
    env.nodes = [
        {**node(4300, 43), "name": "Chapter 43", "sourceOrder": 2},
        {**node(4400, 44), "name": "Chapter 44", "sourceOrder": 1},
    ]
    cbz(env.manga_dir / "Official_Chapter 43.cbz", b"chapter-43-only")
    cbz(env.manga_dir / "Official_Chapter 44.cbz", b"whole-44")
    before_series = env.row("series")
    before_volume = env.row("volumes")
    grabbed = asyncio.run(swy.suwayomi_grab(1, 1))
    asyncio.run(swy.check_suwayomi_jobs())
    with sqlite3.connect(env.db_path) as db:
        jobs = db.execute(
            "SELECT status,chapter_ids FROM suwayomi_downloads"
        ).fetchall()
    assert (grabbed, jobs, env.row("volumes")["status"]) == (
        False,
        [],
        before_volume["status"],
    )
    assert env.row("series") == before_series
    assert env.enqueued == []
    assert not env.library.exists()


@pytest.mark.parametrize(
    "chapter_map",
    [
        "not-json",
        "[]",
        "null",
        "false",
        '"map"',
        '{"43":true}',
        '{"43":null}',
        '{"43":[]}',
        '{"43":{}}',
        '{"43":"NaN"}',
        '{"43":Infinity}',
        '{"43":-1}',
        '{"NaN":1}',
        '{"Infinity":1}',
        '{"-1":1}',
        '{"43":1,"unknown":2}',
        '{"43":1,"99":false}',
    ],
)
def test_invalid_map_is_not_membership_proof(env: ImportEnv, chapter_map: str) -> None:
    env.configure_grab(chapter_map)
    with sqlite3.connect(env.db_path) as db:
        db.execute(
            "INSERT INTO mangadex_chapters(mangadex_chapter_id,series_id,chapter_num,volume_num)"
            " VALUES('cached-43',1,43,1)"
        )
    assert swy._chapters_for_volume([node(4300, 43)], 1, 1) == []


@pytest.mark.parametrize("chapter_map", [None, "", "{}"])
def test_empty_map_retains_cached_membership(
    env: ImportEnv, chapter_map: str | None
) -> None:
    env.configure_grab("{}")
    with sqlite3.connect(env.db_path) as db:
        db.execute("UPDATE series SET chapter_vol_map=? WHERE id=1", (chapter_map,))
        db.execute(
            "INSERT INTO mangadex_chapters(mangadex_chapter_id,series_id,chapter_num,volume_num)"
            " VALUES('cached-43',1,43,1)"
        )
    chapters = [node(4300, 43)]
    assert swy._chapters_for_volume(chapters, 1, 1) == chapters


@pytest.mark.parametrize("mode", ["series-map", "cached-map"])
def test_selector_rows_materialized_inside_context(
    env: ImportEnv, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    env.configure_grab('{"43":1}' if mode == "series-map" else "{}")
    with sqlite3.connect(env.db_path) as db:
        db.execute(
            "INSERT INTO mangadex_chapters(mangadex_chapter_id,series_id,chapter_num,volume_num)"
            " VALUES('cached-43',1,43,1)"
        )
    real_get_db = swy.get_db

    @contextmanager
    def guarded_db() -> Generator[sqlite3.Connection, None, None]:
        active = [True]
        with real_get_db() as db:

            def row_factory(
                cursor: sqlite3.Cursor, row: tuple[Any, ...]
            ) -> GuardedRow | dict[str, Any]:
                result = sqlite3.Row(cursor, row)
                # Isolate the cached read from the series-map lifetime check.
                if (
                    mode == "cached-map"
                    and cursor.description[0][0] == "chapter_vol_map"
                ):
                    return dict(result)
                return GuardedRow(result, active)

            db.row_factory = row_factory
            try:
                yield db
            finally:
                active[0] = False

    monkeypatch.setattr(swy, "get_db", guarded_db)
    chapters = [node(4300, 43)]
    assert swy._chapters_for_volume(chapters, 1, 1) == chapters


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
        {
            **node(451, 45.1),
            "name": "Chapter 45.1",
            "scanlator": None,
            "sourceOrder": 2,
        },
        {
            **node(452, 45.2),
            "name": "Chapter 45.2",
            "scanlator": "Official",
            "sourceOrder": 1,
        },
    ]
    cbz(env.manga_dir / "Chapter 45.1.cbz", b"part-one")
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
            ) == ["Chapter 45.1.cbz", "Official_Chapter 45.2.cbz"]


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


def prepare_source_chapter(env: ImportEnv, number: float = 49) -> None:
    from metadata_provenance import record_manual_metadata

    with sqlite3.connect(env.db_path) as db:
        db.execute(
            "UPDATE chapters SET chapter_num=?,quality='local-quality',size_bytes=42,"
            " torrent_name='original-release' WHERE id=1",
            (number,),
        )
        db.execute("UPDATE series SET chapter_map_source='manual' WHERE id=1")
        record_manual_metadata(
            1, {"title": "Selection", "chapter_vol_map": {"45.1": 1, "45.2": 1}}, db=db
        )


@pytest.mark.parametrize(
    "scanlator,name,filename",
    [
        (
            "_Alpha Team_ Beta",
            "Vol.10 Ch.49 - Extra Story",
            "_Alpha Team_ Beta_Vol.10 Ch.49 - Extra Story.cbz",
        ),
        ("Team7_2000", "Chapter 49", "Team7_2000_Chapter 49.cbz"),
        (None, "Chapter 49", "Chapter 49.cbz"),
        ("", "Chapter 49", "_Chapter 49.cbz"),
        ("[Alpha]", "Chapter 49", "[Alpha]_Chapter 49.cbz"),
        (None, "Chapitre 49 \u00e9", "Chapitre 49 \u00e9.cbz"),
        (
            None,
            ' ..Chapter 49\x00\x1f\x7f"*/:<>?\\|.. ',
            "Chapter 49" + "_" * 12 + ".cbz",
        ),
        (None, "x" * 239 + "\u00e9", "x" * 239 + ".cbz"),
    ],
)
def test_source_evidence_completes_exact_chapter_job(
    env: ImportEnv, scanlator: str | None, name: str, filename: str
) -> None:
    prepare_source_chapter(env)
    source = env.manga_dir / filename
    cbz(source, b"source-chapter-49")
    # An unrelated same-number release must not win the legacy ranking.
    if scanlator != "_Alpha Team_ Beta":
        cbz(env.manga_dir / "A_# 49.cbz", b"wrong-release")
    env.queue([{**node(605, 49), "name": name, "scanlator": scanlator}], chapter=49)
    before_series, before_volume = env.row("series"), env.row("volumes")
    before_chapter = env.row("chapters")
    with sqlite3.connect(env.db_path) as db:
        before_settings = db.execute("SELECT * FROM settings").fetchall()
        before_provenance = db.execute(
            "SELECT * FROM series_metadata_fields"
        ).fetchall()
    env.process()
    job, chapter = env.row("suwayomi_downloads"), env.row("chapters")
    assert job["status"] == "completed", job["error"]
    assert job["chapter_ids"] == "[605]"
    assert job["progress"] == 1
    assert len(env.queries) == 1
    assert env.row("series") == before_series
    assert env.row("volumes") == before_volume
    for key in ("monitored", "quality", "size_bytes", "torrent_name"):
        assert chapter[key] == before_chapter[key]
    with sqlite3.connect(env.db_path) as db:
        assert db.execute("SELECT * FROM settings").fetchall() == before_settings
        assert (
            db.execute("SELECT * FROM series_metadata_fields").fetchall()
            == before_provenance
        )
    assert chapter["status"] == "downloaded"
    with zipfile.ZipFile(chapter["import_path"]) as archive:
        assert archive.read("001.png") == b"source-chapter-49"
    assert "name scanlator" in env.queries[0]
    assert source.is_file()


@pytest.mark.parametrize(
    "name,expected",
    [
        ("\u00e9" * 121, "\u00e9" * 120),
        ("x" * 239 + "\u00e9", "x" * 239),
        ("\U0001f600" * 61, "\U0001f600" * 60),
    ],
)
def test_source_basename_caps_utf8_without_splitting_codepoint(
    name: str, expected: str
) -> None:
    assert (
        swy._source_chapter_basename({"name": name, "scanlator": None})
        == expected + ".cbz"
    )


def test_source_utf8_truncation_collision_refuses_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chapters = {
        cid: {**node(cid, 49), "name": "\u00e9" * 120 + suffix, "scanlator": None}
        for cid, suffix in ((605, "A"), (606, "B"))
    }
    monkeypatch.setattr(swy, "_chapter_files", lambda _: ["\u00e9" * 120 + ".cbz"])
    assert swy._source_chapter_cbz(str(tmp_path), 49, [605], chapters) is None


@pytest.mark.parametrize(
    "name,scanlator,filename",
    [("(invalid)", None, "(invalid).cbz"), (". ", "Alpha", "Alpha_.cbz")],
)
def test_empty_other_source_name_cannot_hide_basename_collision(
    env: ImportEnv, name: str, scanlator: str | None, filename: str
) -> None:
    prepare_source_chapter(env)
    cbz(env.manga_dir / filename, b"ambiguous-source-chapter")
    cbz(env.manga_dir / "A_# 49.cbz", b"forbidden-generic-fallback")
    env.queue(
        [
            {**node(605, 49), "name": name, "scanlator": scanlator},
            {**node(606, 50), "name": "", "scanlator": scanlator},
        ],
        [605],
        chapter=49,
    )
    before = {table: env.row(table) for table in ("series", "volumes", "chapters")}
    env.process()
    assert env.row("suwayomi_downloads")["status"] == "error"
    assert {table: env.row(table) for table in before} == before
    assert not env.library.exists()


@pytest.mark.parametrize(
    "problem",
    [
        "wrong-scanlator",
        "wrong-name",
        "unrelated-id",
        "missing-id",
        "number-mismatch",
        "decimal-mismatch",
        "missing-number",
        "invalid-number",
        "collision",
        "truncation-collision",
        "symlink",
        "directory",
        "malformed-name",
        "empty-name",
        "malformed-scanlator",
        "partial-malformed",
        "multiple-queued-ids",
        "surrogate",
    ],
)
def test_source_evidence_refuses_unproven_chapter_without_fallback(
    env: ImportEnv, problem: str
) -> None:
    prepare_source_chapter(env)
    filename = "Alpha_Chapter 49.cbz"
    chapter = {**node(605, 49), "name": "Chapter 49", "scanlator": "Alpha"}
    nodes = [chapter]
    ids = [605]
    if problem == "wrong-scanlator":
        chapter["scanlator"] = "Beta"
    elif problem == "wrong-name":
        chapter["name"] = "Chapter 49 - Other"
    elif problem == "unrelated-id":
        chapter["scanlator"] = "Beta"
        nodes.append({**node(606, 49), "name": "Chapter 49", "scanlator": "Alpha"})
    elif problem == "missing-id":
        chapter["id"] = 606
    elif problem == "number-mismatch":
        chapter["chapterNumber"] = 48
    elif problem == "decimal-mismatch":
        chapter["chapterNumber"] = 49.5
    elif problem == "missing-number":
        chapter.pop("chapterNumber")
    elif problem == "invalid-number":
        chapter["chapterNumber"] = True
    elif problem == "collision":
        chapter["name"] = "Chapter 49?"
        nodes.append({**node(606, 50), "name": "Chapter 49*", "scanlator": "Alpha"})
        filename = "Alpha_Chapter 49_.cbz"
    elif problem == "truncation-collision":
        chapter.update(name="x" * 240 + "A", scanlator=None)
        nodes.append({**node(606, 50), "name": "x" * 240 + "B", "scanlator": None})
        filename = "x" * 240 + ".cbz"
    elif problem == "malformed-name":
        chapter["name"] = None
    elif problem == "empty-name":
        chapter["name"] = ""
        filename = "Alpha_.cbz"
    elif problem == "malformed-scanlator":
        chapter["scanlator"] = ["Alpha"]
    elif problem == "partial-malformed":
        chapter.pop("scanlator")
        chapter["name"] = 49
    elif problem == "multiple-queued-ids":
        nodes.append({**node(606, 49), "name": "Chapter 49", "scanlator": "Alpha"})
        ids.append(606)
    elif problem == "surrogate":
        chapter["name"] = "Chapter 49\ud800"
    source = env.manga_dir / filename
    if problem == "directory":
        source.mkdir()
    elif problem == "symlink":
        outside = env.manga_dir.parent / "outside.cbz"
        cbz(outside)
        source.symlink_to(outside)
    else:
        cbz(source, b"wrong-release")
    cbz(env.manga_dir / "A_# 49.cbz", b"generic-fallback-must-not-win")
    env.queue(nodes, ids, chapter=49)
    before = {table: env.row(table) for table in ("series", "volumes", "chapters")}
    env.process()
    assert env.row("suwayomi_downloads")["status"] == (
        "queued" if problem == "missing-id" else "error"
    )
    assert {table: env.row(table) for table in before} == before
    assert not env.library.exists()


@pytest.mark.parametrize("evidence", [{}, {"name": "Chapter 49"}, {"scanlator": None}])
@pytest.mark.parametrize("problem", ["mismatch", "missing", "null", "boolean"])
def test_missing_filename_evidence_still_requires_exact_source_number(
    env: ImportEnv, evidence: dict[str, Any], problem: str
) -> None:
    prepare_source_chapter(env)
    cbz(env.manga_dir / "Alpha_Chapter 49.cbz", b"unproven-chapter")
    chapter = {**node(605, 49), **evidence}
    if problem == "missing":
        chapter.pop("chapterNumber")
    else:
        chapter["chapterNumber"] = {"mismatch": 48, "null": None, "boolean": True}[
            problem
        ]
    env.queue([chapter], chapter=49)
    before = {table: env.row(table) for table in ("series", "volumes", "chapters")}
    env.process()
    assert env.row("suwayomi_downloads")["status"] == "error"
    assert {table: env.row(table) for table in before} == before
    assert not env.library.exists()


@pytest.mark.parametrize("evidence", [{}, {"name": "Chapter 49"}, {"scanlator": None}])
def test_missing_source_fields_retain_cached_legacy_chapter(
    env: ImportEnv, evidence: dict[str, Any]
) -> None:
    prepare_source_chapter(env)
    cbz(env.manga_dir / "Alpha_Chapter 49.cbz", b"legacy")
    env.queue([{**node(605, 49), **evidence}], chapter=49)
    env.process()
    assert env.row("suwayomi_downloads")["status"] == "completed"
    # The existing cached destination remains untouched on another import.
    Path(env.row("chapters")["import_path"]).write_bytes(b"cached-library-file")
    env.process()
    assert (
        Path(env.row("chapters")["import_path"]).read_bytes() == b"cached-library-file"
    )


def test_missing_source_evidence_does_not_relax_filename_parser(env: ImportEnv) -> None:
    prepare_source_chapter(env)
    filename = "_Alpha Team_ Beta_Vol.10 Ch.49 - Extra Story.cbz"
    cbz(env.manga_dir / filename)
    assert swy._chapter_file_identity(filename) is None
    assert swy._chapter_cbz(str(env.manga_dir), 49) is None
    env.queue([node(605, 49)], chapter=49)
    env.process()
    assert env.row("suwayomi_downloads")["status"] == "error"
    assert not env.library.exists()


def test_source_evidence_decimal_chapter_does_not_select_whole(env: ImportEnv) -> None:
    prepare_source_chapter(env, 49.5)
    cbz(env.manga_dir / "Team7_Chapter 49.cbz", b"whole")
    cbz(env.manga_dir / "Team7_Chapter 49.5.cbz", b"decimal")
    env.queue(
        [{**node(605, 49.5), "name": "Chapter 49.5", "scanlator": "Team7"}],
        chapter=49.5,
    )
    env.process()
    assert env.row("suwayomi_downloads")["status"] == "completed"
    with zipfile.ZipFile(env.row("chapters")["import_path"]) as archive:
        assert archive.read("001.png") == b"decimal"


@pytest.mark.parametrize("merge", [True, False])
def test_source_volume_evidence_cannot_substitute_unrelated_filename(
    env: ImportEnv, merge: bool
) -> None:
    env.client["merge_chapters"] = merge
    cbz(env.manga_dir / "Alpha_Chapter 49.cbz", b"legacy-volume")
    env.queue([{**node(605, 49), "name": "Different title", "scanlator": "Team7"}])
    before = env.row("volumes")
    env.process()
    assert env.row("suwayomi_downloads")["status"] == "error"
    assert env.row("volumes") == before
    assert not env.library.exists()


def poll_volume(env: ImportEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(swy, "get_suwayomi_client", lambda db: env.client)
    asyncio.run(swy.check_suwayomi_jobs())


def volume_observations(env: ImportEnv) -> dict[str, Any]:
    with sqlite3.connect(env.db_path) as db:
        return {
            "series": env.row("series"),
            "chapters": env.row("chapters"),
            "settings": db.execute("SELECT * FROM settings").fetchall(),
            "provenance": db.execute("SELECT * FROM series_metadata_fields").fetchall(),
        }


@pytest.mark.parametrize("merge", [True, False])
@pytest.mark.parametrize(
    "scanlator,name,filename",
    [
        (
            "_Alpha Team_ Beta",
            "Vol.10 Ch.49 - Extra Story",
            "_Alpha Team_ Beta_Vol.10 Ch.49 - Extra Story.cbz",
        ),
        ("Team7_2000", "Chapter 49", "Team7_2000_Chapter 49.cbz"),
        (None, "Chapter 49", "Chapter 49.cbz"),
        ("", "Chapter 49", "_Chapter 49.cbz"),
        (None, "Chapitre 49 \u00e9", "Chapitre 49 \u00e9.cbz"),
        (
            None,
            ' ..Chapter 49\x00\x1f\x7f"*/:<>?\\|.. ',
            "Chapter 49" + "_" * 12 + ".cbz",
        ),
        (None, "x" * 239 + "\u00e9", "x" * 239 + ".cbz"),
        (None, "x" * 236 + "\u00e9" * 3, "x" * 236 + "\u00e9" * 2 + ".cbz"),
    ],
)
def test_source_volume_poll_imports_only_exact_queued_file(
    env: ImportEnv,
    monkeypatch: pytest.MonkeyPatch,
    merge: bool,
    scanlator: str | None,
    name: str,
    filename: str,
) -> None:
    env.client["merge_chapters"] = merge
    prepare_source_chapter(env)
    source = env.manga_dir / filename
    cbz(source, b"queued-source")
    cbz(env.manga_dir / "A_# 49.cbz", b"unrelated-source")
    env.queue(
        [
            {**node(605, 49), "name": name, "scanlator": scanlator},
            {**node(606, 49), "name": "# 49", "scanlator": "A"},
        ],
        [605],
    )
    before = volume_observations(env)
    before_volume = env.row("volumes")
    original = source.read_bytes()
    poll_volume(env, monkeypatch)
    job, volume = env.row("suwayomi_downloads"), env.row("volumes")
    assert (job["status"], job["progress"], job["chapter_ids"]) == (
        "completed",
        1,
        "[605]",
    )
    assert volume["status"] == "downloaded"
    assert volume_observations(env) == before
    for key in ("quality", "size_bytes", "monitored", "torrent_name"):
        assert volume[key] == before_volume[key]
    output = Path(volume["import_path"])
    if not merge:
        assert [path.name for path in output.iterdir()] == [filename]
        output /= filename
    with zipfile.ZipFile(output) as archive:
        assert [archive.read(page) for page in archive.namelist()] == [b"queued-source"]
    assert source.read_bytes() == original
    assert len(env.queries) == 1
    assert "name scanlator" in env.queries[0]


@pytest.mark.parametrize("merge", [True, False])
@pytest.mark.parametrize(
    "problem",
    [
        "missing-file",
        "wrong-scanlator",
        "wrong-name",
        "missing-name",
        "missing-scanlator",
        "null-name",
        "empty-name",
        "invalid-name",
        "invalid-scanlator",
        "partial-malformed",
        "surrogate",
        "collision",
        "queued-collision",
        "truncation-collision",
        "utf8-collision",
        "empty-other-collision",
        "duplicate-missing-file",
        "duplicate-invalid-name",
        "missing-number",
        "invalid-number",
        "boolean-number",
        "negative-number",
        "decimal-file-missing",
        "directory",
        "symlink",
        "dangling-symlink",
        "fifo",
        "boolean-queued-id",
        "boolean-source-id",
        "zero-id",
        "truthy-downloaded",
    ],
)
def test_source_volume_poll_refuses_unproven_set_before_writes(
    env: ImportEnv,
    monkeypatch: pytest.MonkeyPatch,
    merge: bool,
    problem: str,
) -> None:
    env.client["merge_chapters"] = merge
    prepare_source_chapter(env)
    chapter = {**node(605, 49), "name": "Chapter 49", "scanlator": "Alpha"}
    nodes = [chapter]
    ids: list[int] = [605]
    filename = "Alpha_Chapter 49.cbz"
    if problem == "wrong-scanlator":
        chapter["scanlator"] = "Beta"
    elif problem == "wrong-name":
        chapter["name"] = "Chapter 49 - Other"
    elif problem in {"missing-name", "missing-scanlator", "missing-number"}:
        chapter.pop(
            {
                "missing-name": "name",
                "missing-scanlator": "scanlator",
                "missing-number": "chapterNumber",
            }[problem]
        )
    elif problem in {"null-name", "empty-name", "invalid-name"}:
        chapter["name"] = {"null-name": None, "empty-name": "", "invalid-name": 49}[
            problem
        ]
    elif problem == "invalid-scanlator":
        chapter["scanlator"] = ["Alpha"]
    elif problem == "partial-malformed":
        chapter.pop("scanlator")
        chapter["name"] = 49
    elif problem == "surrogate":
        chapter["name"] = "Chapter 49\ud800"
    elif problem in {"collision", "queued-collision"}:
        chapter["name"] = "Chapter 49 - X?"
        nodes.append(
            {
                **node(606, 50),
                "name": "Chapter 49 - X*",
                "scanlator": "Alpha",
                "isDownloaded": problem == "queued-collision",
            }
        )
        filename = "Alpha_Chapter 49 - X_.cbz"
        if problem == "queued-collision":
            ids.append(606)
    elif problem in {"truncation-collision", "utf8-collision"}:
        prefix = "x" * 240 if problem == "truncation-collision" else "\u00e9" * 120
        chapter.update(name=prefix + "A", scanlator=None)
        nodes.append({**node(606, 50), "name": prefix + "B", "scanlator": None})
        filename = prefix + ".cbz"
    elif problem == "empty-other-collision":
        chapter.update(name="(invalid)", scanlator=None)
        nodes.append({**node(606, 50), "name": "", "scanlator": None})
        filename = "(invalid).cbz"
    elif problem in {"duplicate-missing-file", "duplicate-invalid-name"}:
        nodes.append(
            {
                **node(606, 49),
                "name": "Chapter 49" if problem == "duplicate-missing-file" else None,
                "scanlator": "Beta",
            }
        )
        ids.append(606)
    elif problem in {"invalid-number", "boolean-number", "negative-number"}:
        chapter["chapterNumber"] = {
            "invalid-number": "NaN",
            "boolean-number": True,
            "negative-number": -1,
        }[problem]
    elif problem == "decimal-file-missing":
        chapter.update(chapterNumber=49.5, name="Chapter 49.5")
        cbz(env.manga_dir / "A_# 49.5.cbz", b"forbidden-decimal-substitute")
    elif problem == "boolean-queued-id":
        chapter["id"] = 1
        ids = [True]
    elif problem == "boolean-source-id":
        chapter["id"] = True
        ids = [1]
    elif problem == "zero-id":
        chapter["id"] = 0
        ids = [0]
    elif problem == "truthy-downloaded":
        chapter["isDownloaded"] = "yes"
    source = env.manga_dir / filename
    if problem == "directory":
        source.mkdir()
    elif problem in {"symlink", "dangling-symlink"}:
        outside = env.manga_dir.parent / "outside.cbz"
        if problem == "symlink":
            cbz(outside, b"outside-source")
        source.symlink_to(outside)
    elif problem == "fifo":
        os.mkfifo(source)
    elif problem == "utf8-collision":
        # ZFS formD can refuse this basename after normalization. Exercise the
        # collision with an inventory containing the exact 240-byte basename.
        monkeypatch.setattr(
            swy, "_chapter_files", lambda path: [filename, "A_# 49.cbz"]
        )
    elif problem != "missing-file":
        cbz(source, b"unproven-source")
    cbz(env.manga_dir / "A_# 49.cbz", b"forbidden-substitute")
    env.queue(nodes, ids)
    before = volume_observations(env)
    before_volume = env.row("volumes")
    poll_volume(env, monkeypatch)
    job = env.row("suwayomi_downloads")
    assert job["status"] == "error"
    assert job["progress"] == len(ids)
    assert json.loads(job["chapter_ids"]) == ids
    assert volume_observations(env) == before
    assert env.row("volumes") == before_volume
    assert not env.library.exists()


@pytest.mark.parametrize("merge", [True, False])
@pytest.mark.parametrize("reverse", [True, False])
def test_source_volume_poll_deduplicates_only_complete_queued_variants(
    env: ImportEnv,
    monkeypatch: pytest.MonkeyPatch,
    merge: bool,
    reverse: bool,
) -> None:
    env.client["merge_chapters"] = merge
    chapters = [
        {**node(10, 10), "name": "Ten", "scanlator": "Team7"},
        {**node(2, 2), "name": "Chapter 2", "scanlator": "Long Group"},
        {**node(22, "2.00"), "name": "Two", "scanlator": "Team7"},
        {**node(25, "2.5"), "name": "Two and half", "scanlator": None},
    ]
    for filename, page in [
        ("Team7_Ten.cbz", b"ten"),
        ("Long Group_Chapter 2.cbz", b"other-queued-two"),
        ("Team7_Two.cbz", b"two"),
        ("Two and half.cbz", b"two-and-half"),
        ("A_# 2.cbz", b"unrelated-two"),
        ("Vol.1 Ch.99.cbz", b"not-in-job"),
    ]:
        cbz(env.manga_dir / filename, page)
    ids = [10, 2, 22, 25]
    if reverse:
        chapters.reverse()
        ids.reverse()
    env.queue(chapters, ids)
    original_listdir = os.listdir
    monkeypatch.setattr(
        os,
        "listdir",
        lambda path: (
            list(reversed(original_listdir(path)))
            if reverse
            else original_listdir(path)
        ),
    )
    assert [
        Path(path).name
        for path in swy._source_volume_cbzs(
            str(env.manga_dir), ids, {chapter["id"]: chapter for chapter in chapters}
        )
    ] == ["Team7_Two.cbz", "Two and half.cbz", "Team7_Ten.cbz"]
    poll_volume(env, monkeypatch)
    job, volume = env.row("suwayomi_downloads"), env.row("volumes")
    assert job["status"] == "completed"
    assert job["progress"] == 4
    assert json.loads(job["chapter_ids"]) == ids
    output = Path(volume["import_path"])
    if merge:
        with zipfile.ZipFile(output) as archive:
            assert [archive.read(page) for page in archive.namelist()] == [
                b"two",
                b"two-and-half",
                b"ten",
            ]
    else:
        assert sorted(path.name for path in output.iterdir()) == [
            "Team7_Ten.cbz",
            "Team7_Two.cbz",
            "Two and half.cbz",
        ]
        for filename, page in [
            ("Team7_Ten.cbz", b"ten"),
            ("Team7_Two.cbz", b"two"),
            ("Two and half.cbz", b"two-and-half"),
        ]:
            with zipfile.ZipFile(output / filename) as archive:
                assert archive.read("001.png") == page


@pytest.mark.parametrize("merge", [True, False])
@pytest.mark.parametrize("problem", ["missing-id", "not-downloaded"])
def test_source_volume_poll_keeps_incomplete_job_pending(
    env: ImportEnv,
    monkeypatch: pytest.MonkeyPatch,
    merge: bool,
    problem: str,
) -> None:
    env.client["merge_chapters"] = merge
    nodes = [{**node(605, 49), "name": "Chapter 49", "scanlator": "Alpha"}]
    if problem == "not-downloaded":
        nodes.append(
            {
                **node(606, 50),
                "name": "Chapter 50",
                "scanlator": "Alpha",
                "isDownloaded": False,
            }
        )
    cbz(env.manga_dir / "Alpha_Chapter 49.cbz")
    env.queue(nodes, [605, 606])
    before = env.row("volumes")
    poll_volume(env, monkeypatch)
    job = env.row("suwayomi_downloads")
    assert (job["status"], job["progress"]) == ("queued", 1)
    assert env.row("volumes") == before
    assert not env.library.exists()


@pytest.mark.parametrize("merge", [True, False])
@pytest.mark.parametrize("problem", ["none", "missing-file", "invalid-name"])
def test_source_volume_poll_preserves_cache_without_bypassing_coverage(
    env: ImportEnv,
    monkeypatch: pytest.MonkeyPatch,
    merge: bool,
    problem: str,
) -> None:
    env.client["merge_chapters"] = merge
    chapters = [
        {**node(2, 2), "name": "Chapter 2", "scanlator": "Alpha"},
        {**node(10, 10), "name": "Ten", "scanlator": "Team7"},
    ]
    cbz(env.manga_dir / "Alpha_Chapter 2.cbz", b"new-two")
    if problem != "missing-file":
        cbz(env.manga_dir / "Team7_Ten.cbz", b"new-ten")
    if problem == "invalid-name":
        chapters[1]["name"] = None
    destination = env.library / "Selection v01.cbz" if merge else env.library / "v01"
    files = (
        [destination]
        if merge
        else [destination / "Alpha_Chapter 2.cbz", destination / "Team7_Ten.cbz"]
    )
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"cached-library-content")
    original = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}
    with sqlite3.connect(env.db_path) as db:
        db.execute(
            "UPDATE volumes SET status='downloaded',import_path=?,imported_at='2026-01-01' WHERE id=1",
            (str(destination),),
        )
    env.queue(chapters)
    before = env.row("volumes")
    poll_volume(env, monkeypatch)
    assert env.row("suwayomi_downloads")["status"] == (
        "completed" if problem == "none" else "error"
    )
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files
    } == original
    assert set(env.library.rglob("*.cbz")) == set(files)
    if problem != "none":
        assert env.row("volumes") == before


@pytest.mark.parametrize("merge", [True, False])
@pytest.mark.parametrize(
    "numbers,valid",
    [
        ([Decimal("2.00"), Decimal("10.0")], True),
        ([10, 2], False),
        ([2], False),
        ([], False),
        ([2, float("nan")], False),
    ],
)
def test_source_volume_import_checks_supplied_numbers_before_cache(
    env: ImportEnv,
    merge: bool,
    numbers: list[float | Decimal],
    valid: bool,
) -> None:
    env.client["merge_chapters"] = merge
    chapters = {
        2: {**node(2, 2), "name": "Chapter 2", "scanlator": "Alpha"},
        10: {**node(10, 10), "name": "Chapter 10", "scanlator": "Alpha"},
    }
    cbz(env.manga_dir / "Alpha_Chapter 2.cbz", b"two")
    cbz(env.manga_dir / "Alpha_Chapter 10.cbz", b"ten")
    destination = env.library / "Selection v01.cbz" if merge else env.library / "v01"
    cached = (
        [destination]
        if merge
        else [destination / "Alpha_Chapter 2.cbz", destination / "Alpha_Chapter 10.cbz"]
    )
    for path in cached:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"cached-content")
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in cached}
    result = asyncio.run(
        swy._import_suwayomi_volume(
            env.client,
            1,
            1,
            chapter_ids=[2, 10],
            chapter_nums=numbers,
            source_chapters=chapters,
        )
    )
    assert result == (
        (str(destination), sum(path.stat().st_size for path in cached))
        if valid
        else (None, 0)
    )
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in cached
    } == before


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
