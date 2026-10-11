"""Independent loose library files survive volume deletion and rescan (#421)."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any
import zipfile

import pytest


def archive(path: Path, page: bytes = b"page") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as cbz:
        cbz.writestr("001.png", page)


@pytest.fixture
def state_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import main
    import shared

    db_path = tmp_path / "state.db"
    root = tmp_path / "library"
    library = root / "State Series"
    library.mkdir(parents=True)
    monkeypatch.setattr(main, "DB_PATH", str(db_path))
    monkeypatch.setattr(shared, "DB_PATH", str(db_path))
    monkeypatch.setattr(main, "CONFIG", {})
    monkeypatch.setattr(shared, "CONFIG", {})
    main.init_db()
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO root_folders(id,path,is_default) VALUES(1,?,1)", (str(root),)
        )
        db.execute(
            "INSERT INTO series(id,title,search_pattern,root_folder_id,folder_name,"
            "anilist_id,mangadex_id,chapter_vol_map,total_volumes,total_chapters)"
            " VALUES(1,'State Series','State Series',1,'State Series',123,'public-id',?,1,4)",
            (json.dumps({str(ch): 1 for ch in range(1, 5)}),),
        )
        db.execute(
            "INSERT INTO volumes(id,series_id,volume_num,status) VALUES(11,1,1,'wanted')"
        )
        db.execute(
            "INSERT INTO series_metadata_fields(series_id,field_name,value_json,selected_source,locked,selected_at)"
            " VALUES(1,'title','\"State Series\"','manual',1,'2026-10-10')"
        )
        db.execute(
            "INSERT INTO series_metadata_sources(series_id,source,status,failure_count)"
            " VALUES(1,'anilist','failed',3)"
        )
        for ch in range(1, 5):
            db.execute(
                "INSERT INTO chapters(id,series_id,volume_id,chapter_num,status,monitored,"
                "torrent_name,indexer,protocol,client,quality)"
                " VALUES(?,1,11,?,'wanted',1,'Original chapter','Original indexer','ddl','suwayomi','cbz')",
                (ch, ch),
            )
    return {"db_path": db_path, "library": library, "tmp": tmp_path}


def rows(env: dict[str, Any], table: str) -> list[dict[str, Any]]:
    assert table in {
        "series",
        "chapters",
        "volumes",
        "history",
        "series_metadata_fields",
        "series_metadata_sources",
    }
    with sqlite3.connect(env["db_path"]) as db:
        db.row_factory = sqlite3.Row
        return [
            dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")
        ]


def complete_merge(env: dict[str, Any]) -> Path:
    """Real chapter imports and completed volume job, followed by normal cascade."""
    import shared
    from file_mutation_lock import file_mutation_guard
    from routers import suwayomi_ as swy
    from volumes import _cascade_chapters

    source = env["tmp"] / "source" / "mangas" / "Source" / "State Series"
    client = {"download_path": str(env["tmp"] / "source"), "merge_chapters": 1}
    chapters = {}
    for ch in range(1, 5):
        archive(source / f"Group_Chapter {ch}.cbz", f"page-{ch}".encode())
        chapters[ch] = {
            "id": ch,
            "chapterNumber": ch,
            "name": f"Chapter {ch}",
            "scanlator": "Group",
            "isDownloaded": True,
        }
        path, _ = swy._import_suwayomi_chapter_files(
            client,
            1,
            float(ch),
            swy_title="State Series",
            chapter_ids=[ch],
            source_chapters=chapters,
            source_display_name="Source",
        )
        assert path is not None
        with sqlite3.connect(env["db_path"]) as db:
            db.execute(
                "UPDATE chapters SET status='downloaded',import_path=? WHERE id=?",
                (path, ch),
            )
    with sqlite3.connect(env["db_path"]) as db:
        db.execute(
            "INSERT INTO suwayomi_downloads(id,series_id,volume_num,suwayomi_manga_id,chapter_ids,status,total)"
            " VALUES(1,1,1,999,'[1,2,3,4]','queued',4)"
        )
    job = {"id": 1, "series_id": 1, "chapter_num": None, "volume_num": 1}
    with file_mutation_guard(shared.DB_PATH) as guard:
        merged, _ = swy._complete_suwayomi_job_files(
            client,
            job,
            [1, 2, 3, 4],
            chapters,
            "State Series",
            guard,
            source_display_name="Source",
        )
    assert merged is not None
    with sqlite3.connect(env["db_path"]) as db:
        _cascade_chapters(db, 1, [11], "downloaded", import_path=merged)
    return Path(merged)


def test_delete_after_completed_merge_preserves_independent_loose_files(state_env):
    import volume_file_deletion
    import rescan

    merged = complete_merge(state_env)
    loose = sorted(state_env["library"].glob("* Ch*.cbz"))
    hashes = [hashlib.sha256(path.read_bytes()).digest() for path in loose]
    before_series = rows(state_env, "series")
    before_chapters = rows(state_env, "chapters")
    assert all(ch["import_path"] == str(merged) for ch in before_chapters)

    result = volume_file_deletion.delete_volume_file(1, 11)

    assert result.status == "complete"
    assert not merged.exists()
    assert rows(state_env, "volumes")[0]["status"] == "wanted"
    assert rows(state_env, "series") == before_series
    for before, after, path in zip(before_chapters, rows(state_env, "chapters"), loose):
        expected = {**before, "import_path": str(path)}
        assert after == expected
    assert [hashlib.sha256(path.read_bytes()).digest() for path in loose] == hashes
    assert rescan.rescan_series_folder(1)["recovered"] == 0
    assert rows(state_env, "chapters") == [
        {**before, "import_path": str(path)}
        for before, path in zip(before_chapters, loose)
    ]
    assert rescan.rescan_series_folder(1)["recovered"] == 0


def test_rescan_recovers_existing_loose_chapters_without_metadata_or_owner_changes(
    state_env,
):
    import rescan

    for ch in range(1, 5):
        archive(state_env["library"] / f"State Series Ch{ch:03d}.cbz")
    before_series = rows(state_env, "series")
    before_history = rows(state_env, "history")
    before_chapters = rows(state_env, "chapters")
    before_metadata = {
        table: rows(state_env, table)
        for table in ("series_metadata_fields", "series_metadata_sources")
    }

    result = rescan.rescan_series_folder(1)

    assert result["recovered"] == 4
    assert result["created"] == 0
    assert rows(state_env, "series") == before_series
    assert rows(state_env, "history") == before_history
    assert {
        table: rows(state_env, table) for table in before_metadata
    } == before_metadata
    assert rows(state_env, "volumes")[0]["status"] == "wanted"
    for before, after in zip(before_chapters, rows(state_env, "chapters")):
        path = (
            state_env["library"] / f"State Series Ch{before['chapter_num']:03.0f}.cbz"
        )
        assert after["status"] == "downloaded"
        assert after["import_path"] == str(path)
        for key in (
            "id",
            "volume_id",
            "monitored",
            "torrent_name",
            "indexer",
            "protocol",
            "client",
        ):
            assert after[key] == before[key]


@pytest.mark.parametrize("operation", ["delete", "rescan"])
@pytest.mark.parametrize(
    "problem",
    [
        "missing",
        "empty",
        "directory",
        "symlink",
        "parent-symlink",
        "outside",
        "foreign",
        "ambiguous",
        "volume",
        "pack",
        "private",
        "hardlink",
        "conflicting-number",
        "conflicting-full-number",
        "conflicting-full-number-case",
        "range",
    ],
)
def test_unproven_files_do_not_authorize_downloaded_chapter(
    state_env, operation, problem
):
    import rescan
    import volume_file_deletion

    library = state_env["library"]
    path = library / "State Series Ch001.cbz"
    volume = library / "State Series v01.cbz"
    archive(volume)
    if problem == "empty":
        path.touch()
    elif problem == "directory":
        path.mkdir()
    elif problem == "symlink":
        outside = state_env["tmp"] / "foreign.cbz"
        archive(outside)
        path.symlink_to(outside)
    elif problem == "parent-symlink":
        outside = state_env["tmp"] / "foreign"
        archive(outside / path.name)
        (library / "linked").symlink_to(outside, target_is_directory=True)
        path = library / "linked" / path.name
    elif problem == "outside":
        path = state_env["tmp"] / path.name
        archive(path)
    elif problem == "foreign":
        archive(library / "Unrelated Ch001.cbz")
    elif problem == "ambiguous":
        archive(path)
        path = library / "alternative.cbz"
        archive(path)
    elif problem == "volume":
        path = volume
    elif problem == "hardlink":
        path.hardlink_to(volume)
    elif problem == "conflicting-number":
        path = library / "State Series Ch001 Ch002.cbz"
        archive(path)
    elif problem in ("conflicting-full-number", "conflicting-full-number-case"):
        title = "STATE SERIES" if problem.endswith("-case") else "State Series"
        path = library / f"{title} Chapter 2 Ch001.cbz"
        archive(path)
    elif problem == "range":
        path = library / "State Series Ch001-004.cbz"
        archive(path)
    elif problem == "pack":
        archive(path)
        with sqlite3.connect(state_env["db_path"]) as db:
            db.execute(
                "INSERT INTO volumes(series_id,volume_num,pack_type,status,import_path)"
                " VALUES(1,NULL,'complete','downloaded',?)",
                (str(path),),
            )
    elif problem == "private":
        path = library / ".mangarr-staging-unowned" / path.name
        archive(path)
    with sqlite3.connect(state_env["db_path"]) as db:
        db.execute(
            "UPDATE volumes SET status='downloaded',import_path=? WHERE id=11",
            (str(volume),),
        )
        db.execute(
            "UPDATE chapters SET status=?,import_path=? WHERE id=1",
            (
                "downloaded" if operation == "delete" else "wanted",
                None if problem == "foreign" else str(path),
            ),
        )
    if operation == "delete":
        assert volume_file_deletion.delete_volume_file(1, 11).status == "complete"
    else:
        # Avoid volume recovery/cascade; only the loose-file path is under test.
        if problem != "hardlink":
            with sqlite3.connect(state_env["db_path"]) as db:
                db.execute(
                    "UPDATE volumes SET status='wanted',import_path=NULL WHERE id=11"
                )
            volume.unlink()
        rescan.rescan_series_folder(1)
    assert rows(state_env, "chapters")[0]["status"] == "wanted"


@pytest.mark.parametrize("monitored", [True, False])
def test_delete_mixed_independent_and_volume_backed_chapters(state_env, monitored):
    import volume_file_deletion

    merged = complete_merge(state_env)
    (state_env["library"] / "State Series Ch002.cbz").unlink()
    with sqlite3.connect(state_env["db_path"]) as db:
        db.execute("UPDATE chapters SET monitored=? WHERE id IN (1,2)", (monitored,))
    before = rows(state_env, "chapters")
    assert volume_file_deletion.delete_volume_file(1, 11).status == "complete"
    after = rows(state_env, "chapters")
    assert after[0] == {
        **before[0],
        "import_path": str(state_env["library"] / "State Series Ch001.cbz"),
    }
    assert after[1]["status"] == ("wanted" if monitored else "downloaded")
    assert after[1]["import_path"] == (None if monitored else str(merged))


@pytest.mark.parametrize("monitored", [True, False])
@pytest.mark.parametrize("linked", [True, False])
def test_rescan_existing_rows_respects_identity_not_monitoring_and_is_idempotent(
    state_env, monitored, linked
):
    import rescan

    path = state_env["library"] / "State Series Ch001.cbz"
    archive(path)
    with sqlite3.connect(state_env["db_path"]) as db:
        db.execute(
            "UPDATE chapters SET monitored=?,volume_id=? WHERE id=1",
            (monitored, 11 if linked else None),
        )
    before_series = rows(state_env, "series")
    assert rescan.rescan_series_folder(1)["recovered"] == 1
    first = rows(state_env, "chapters")
    assert first[0]["status"] == "downloaded"
    assert first[0]["monitored"] == monitored
    assert first[0]["volume_id"] == (11 if linked else None)
    assert rescan.rescan_series_folder(1)["recovered"] == 0
    assert rows(state_env, "chapters") == first
    assert rows(state_env, "series") == before_series


def test_persisted_volume_and_chapter_filename_recovers_only_chapter(state_env):
    import rescan

    path = state_env["library"] / "State Series v02 Ch001.cbz"
    archive(path)
    with sqlite3.connect(state_env["db_path"]) as db:
        db.execute("UPDATE chapters SET import_path=? WHERE id=1", (str(path),))
    before_series = rows(state_env, "series")
    before_volumes = rows(state_env, "volumes")
    result = rescan.rescan_series_folder(1)
    assert result["recovered"] == 1
    assert result["created"] == 0
    assert rows(state_env, "volumes") == before_volumes
    assert rows(state_env, "series") == before_series


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "grabbed"),
        ("import_path", "/winner.cbz"),
        ("download_id", "winner"),
        ("chapter_num", 9),
        ("volume_id", None),
        ("monitored", 0),
    ],
)
@pytest.mark.parametrize("operation", ["delete", "rescan"])
def test_concurrent_chapter_change_preserves_winner(
    state_env, monkeypatch, operation, field, value
):
    import rescan
    import volume_file_deletion

    path = state_env["library"] / "State Series Ch001.cbz"
    archive(path)
    volume = state_env["library"] / "State Series v01.cbz"
    if operation == "delete":
        archive(volume)
        with sqlite3.connect(state_env["db_path"]) as db:
            db.execute(
                "UPDATE volumes SET status='downloaded',import_path=? WHERE id=11",
                (str(volume),),
            )
            db.execute(
                "UPDATE chapters SET status='downloaded',import_path=? WHERE id=1",
                (str(volume),),
            )
        module, name = volume_file_deletion, "inspect_volume_file_deletion"
    else:
        module, name = rescan, "build_filesystem_inventory"
    original = getattr(module, name)
    winner = []

    def raced(*args):
        result = original(*args)
        with sqlite3.connect(state_env["db_path"]) as db:
            db.execute(f"UPDATE chapters SET {field}=? WHERE id=1", (value,))
        winner.extend(rows(state_env, "chapters"))
        return result

    monkeypatch.setattr(module, name, raced)
    if operation == "delete":
        assert volume_file_deletion.delete_volume_file(1, 11).status == "changed"
        assert volume.exists()
    else:
        rescan.rescan_series_folder(1)
    assert rows(state_env, "chapters")[0] == winner[0]


@pytest.mark.parametrize("kind", ["volume", "chapter"])
def test_chapter_number_in_series_title_is_not_file_identity(state_env, kind):
    import rescan

    with sqlite3.connect(state_env["db_path"]) as db:
        db.execute("UPDATE series SET title='Chapter 13' WHERE id=1")
    path = state_env["library"] / (
        "Chapter 13 v01.cbz" if kind == "volume" else "Chapter 13 Ch001.cbz"
    )
    archive(path)
    result = rescan.rescan_series_folder(1)
    assert result["recovered"] == 1
    table = "volumes" if kind == "volume" else "chapters"
    assert rows(state_env, table)[0]["status"] == "downloaded"
    assert rows(state_env, table)[0]["import_path"] == str(path)


@pytest.mark.parametrize("title", ["Other Title", "chapter 13"])
def test_persisted_volume_identity_survives_title_edit(state_env, title):
    import rescan

    path = state_env["library"] / "Chapter 13 v01.cbz"
    archive(path)
    with sqlite3.connect(state_env["db_path"]) as db:
        db.execute("UPDATE series SET title=? WHERE id=1", (title,))
        db.execute(
            "UPDATE volumes SET status='downloaded',import_path=?,quality='cbz' WHERE id=11",
            (str(path),),
        )
        db.execute(
            "UPDATE chapters SET status='downloaded',import_path=?",
            (str(path),),
        )
    before = {
        table: rows(state_env, table) for table in ("series", "volumes", "chapters")
    }
    assert rescan.rescan_series_folder(1)["recovered"] == 0
    assert {table: rows(state_env, table) for table in before} == before


@pytest.mark.parametrize("operation", ["delete", "rescan"])
@pytest.mark.parametrize("title", ["Other Title", "chapter 13"])
def test_persisted_independent_chapter_identity_survives_title_edit(
    state_env, operation, title
):
    import rescan
    import volume_file_deletion

    path = state_env["library"] / "Chapter 13 Ch001.cbz"
    volume = state_env["library"] / "State Series v01.cbz"
    archive(path)
    if operation == "delete":
        archive(volume)
    with sqlite3.connect(state_env["db_path"]) as db:
        db.execute("UPDATE series SET title=? WHERE id=1", (title,))
        db.execute(
            "UPDATE chapters SET status=?,import_path=? WHERE id=1",
            ("downloaded" if operation == "delete" else "wanted", str(path)),
        )
        if operation == "delete":
            db.execute(
                "UPDATE volumes SET status='downloaded',import_path=? WHERE id=11",
                (str(volume),),
            )
    before = rows(state_env, "chapters")[0]
    before_series = rows(state_env, "series")
    if operation == "delete":
        assert volume_file_deletion.delete_volume_file(1, 11).status == "complete"
    else:
        assert rescan.rescan_series_folder(1)["recovered"] == 1
    assert rows(state_env, "chapters")[0] == {**before, "status": "downloaded"}
    assert rows(state_env, "series") == before_series
    assert path.exists()


@pytest.mark.parametrize("untracked", [False, True])
@pytest.mark.parametrize("conflict", [False, True])
def test_supported_dotted_title_volume_inventory(
    state_env, monkeypatch, untracked, conflict
):
    import files
    import rescan
    import shared

    monkeypatch.setitem(shared.CONFIG, "file_format", "{Series.Title} v{Volume:02d}")
    filename = files.build_filename("Chapter 13", 1, "original.cbz")
    assert filename == "Chapter.13 v01.cbz"
    if conflict:
        filename = filename.replace(" v01", " Chapter 2 v01")
    path = state_env["library"] / filename
    archive(path)
    with sqlite3.connect(state_env["db_path"]) as db:
        db.execute("UPDATE series SET title='Chapter 13' WHERE id=1")
        if untracked:
            db.execute("UPDATE chapters SET volume_id=NULL")
            db.execute("DELETE FROM volumes")
    before_chapters = rows(state_env, "chapters")
    result = rescan.rescan_series_folder(1)
    assert result["created"] == int(untracked and not conflict)
    assert result["recovered"] == int(not untracked and not conflict)
    expected_chapters = [
        {**chapter, "status": "downloaded"}
        if not conflict and not untracked
        else chapter
        for chapter in before_chapters
    ]
    assert rows(state_env, "chapters") == expected_chapters
    if not conflict:
        assert rows(state_env, "volumes")[0]["import_path"] == str(path)
        assert rows(state_env, "volumes")[0]["status"] == "downloaded"


@pytest.mark.parametrize("operation", ["delete", "rescan"])
@pytest.mark.parametrize(
    "marker", ["Chapter 13", "Episode 2", "Ep 2", "Ch2", "C02", "第2話"]
)
@pytest.mark.parametrize(
    "identity",
    [
        "current",
        "historical",
        "historical-words",
        "conflict",
        "dotted-conflict",
        "unowned",
    ],
)
def test_persisted_marker_title_has_bounded_terminal_chapter_identity(
    state_env, operation, marker, identity
):
    import rescan
    import volume_file_deletion

    title = marker if identity == "current" else "Other Title"
    filename = f"{marker} Ch001.cbz"
    if identity == "historical-words":
        filename = f"The {marker} Story Ch001.cbz"
    if identity in ("conflict", "dotted-conflict"):
        title = "State Series"
        prefix = "State.Series" if identity == "dotted-conflict" else title
        filename = f"{prefix} {marker} Ch001.cbz"
    path = state_env["library"] / filename
    volume = state_env["library"] / "State Series v01.cbz"
    archive(path)
    if operation == "delete":
        archive(volume)
    with sqlite3.connect(state_env["db_path"]) as db:
        db.execute("UPDATE series SET title=? WHERE id=1", (title,))
        db.execute(
            "UPDATE chapters SET status=?,import_path=? WHERE id=1",
            (
                "downloaded" if operation == "delete" else "wanted",
                None if identity == "unowned" else str(path),
            ),
        )
        if operation == "delete":
            db.execute(
                "UPDATE volumes SET status='downloaded',import_path=? WHERE id=11",
                (str(volume),),
            )
    before_series = rows(state_env, "series")
    before_chapters = rows(state_env, "chapters")
    accepted = identity in ("current", "historical", "historical-words")
    if operation == "delete":
        assert volume_file_deletion.delete_volume_file(1, 11).status == "complete"
    else:
        result = rescan.rescan_series_folder(1)
        assert result["recovered"] == int(accepted)
        assert result["created"] == 0
    after = rows(state_env, "chapters")
    assert after[0]["status"] == ("downloaded" if accepted else "wanted")
    if accepted:
        assert after[0] == {**before_chapters[0], "status": "downloaded"}
    assert rows(state_env, "series") == before_series
    assert path.exists()


@pytest.mark.parametrize("operation", ["delete", "rescan"])
@pytest.mark.parametrize(
    "prefix",
    ["Episode 2 Ep 3", "Ch002", "Chapter 2-3", "Ep 2-3", "C02-03", "第2話-第3話"],
)
def test_historical_persisted_suffix_does_not_erase_ambiguous_prefix(
    state_env, operation, prefix
):
    import rescan
    import volume_file_deletion

    path = state_env["library"] / f"{prefix} Ch001.cbz"
    volume = state_env["library"] / "State Series v01.cbz"
    archive(path)
    if operation == "delete":
        archive(volume)
    with sqlite3.connect(state_env["db_path"]) as db:
        db.execute("UPDATE series SET title='Other Title' WHERE id=1")
        db.execute(
            "UPDATE chapters SET status=?,import_path=? WHERE id=1",
            ("downloaded" if operation == "delete" else "wanted", str(path)),
        )
        if operation == "delete":
            db.execute(
                "UPDATE volumes SET status='downloaded',import_path=? WHERE id=11",
                (str(volume),),
            )
    if operation == "delete":
        assert volume_file_deletion.delete_volume_file(1, 11).status == "complete"
    else:
        assert rescan.rescan_series_folder(1)["recovered"] == 0
    assert rows(state_env, "chapters")[0]["status"] == "wanted"


@pytest.mark.parametrize("operation", ["delete", "rescan"])
@pytest.mark.parametrize(
    "ownership", ["wanted", "missing-path", "untracked", "alternate"]
)
@pytest.mark.parametrize("alias", [True, False])
def test_discovered_volume_inode_cannot_be_independent_chapter(
    state_env, monkeypatch, operation, ownership, alias
):
    import rescan
    import volume_file_deletion

    volume = state_env["library"] / "State Series v01.cbz"
    discovered = volume
    persisted = None
    status = "wanted"
    if ownership in ("untracked", "alternate"):
        archive(volume, b"owned volume")
        persisted = str(volume)
        status = "downloaded"
        discovered = state_env["library"] / (
            "State Series v02.cbz"
            if ownership == "untracked"
            else "State Series v01 alternate.cbz"
        )
    elif ownership == "missing-path":
        persisted = str(state_env["library"] / "missing.cbz")
        status = "downloaded"
    archive(discovered, b"discovered volume")
    chapter = state_env["library"] / "State Series Ch001.cbz"
    if alias:
        chapter.hardlink_to(discovered)
    else:
        archive(chapter, b"independent chapter")
    with sqlite3.connect(state_env["db_path"]) as db:
        db.execute(
            "UPDATE volumes SET status=?,import_path=? WHERE id=11", (status, persisted)
        )
        db.execute(
            "UPDATE chapters SET status=?,import_path=? WHERE id=1",
            ("downloaded" if operation == "delete" else "wanted", str(chapter)),
        )
    observed = []
    if operation == "delete":
        original = volume_file_deletion.inspect_volume_file_deletion

        def capture(*args):
            inspection = original(*args)
            assert inspection is not None
            observed.append(inspection.independent_chapters)
            return inspection

        monkeypatch.setattr(
            volume_file_deletion, "inspect_volume_file_deletion", capture
        )
        assert volume_file_deletion.delete_volume_file(1, 11).status == "complete"
    else:
        original = rescan.build_filesystem_inventory

        def capture(*args):
            inventory = original(*args)
            observed.append(inventory.files_by_chapter)
            return inventory

        monkeypatch.setattr(rescan, "build_filesystem_inventory", capture)
        rescan.rescan_series_folder(1)
    assert (1 in observed[0]) == (not alias)
    if operation == "delete" or not alias:
        assert rows(state_env, "chapters")[0]["status"] == (
            "wanted" if alias else "downloaded"
        )
    assert chapter.exists()
    if operation == "delete":
        # The discovery is observation-only; untracked/alternate files are never deleted.
        assert discovered.exists()
        with rescan.get_db() as db:
            snapshot = rescan.snapshot_series_rescan(db, 1)
        assert snapshot is not None
        assert (1 in rescan.build_filesystem_inventory(snapshot).files_by_chapter) == (
            not alias
        )


@pytest.mark.parametrize("operation", ["delete", "rescan"])
@pytest.mark.parametrize("kind", ["rescan", "import", "publication", "deletion"])
def test_writer_rechecks_late_competing_fence(state_env, monkeypatch, operation, kind):
    import rescan
    import volume_file_deletion

    archive(state_env["library"] / "State Series Ch001.cbz")
    if operation == "delete":
        merged = complete_merge(state_env)
        module, name = volume_file_deletion, "inspect_volume_file_deletion"
    else:
        merged = None
        module, name = rescan, "build_filesystem_inventory"
    original = getattr(module, name)
    before = rows(state_env, "chapters")

    def raced(*args):
        result = original(*args)
        with sqlite3.connect(state_env["db_path"]) as db:
            if kind == "rescan":
                db.execute(
                    "INSERT INTO rescan_file_operations(operation_token,series_id,volume_id,"
                    "source_path,destination_path,expected_volume_json,expected_context_json,"
                    "fingerprints_json,carriers_json,state) VALUES('late',1,11,'/source','/dest','{}','{}','{}','{}','prepared')"
                )
            elif kind == "deletion":
                db.execute(
                    "INSERT INTO volume_file_deletions(volume_id,series_id,state,target_path,parent_path,"
                    "claim_path,target_present,series_title) VALUES(12,1,'active','/foreign','/','/claim',0,'State Series')"
                )
            else:
                db.execute(
                    "INSERT INTO import_queue(id,series_id,status,lease_owner) VALUES(71,1,?,?)",
                    (
                        "importing" if kind == "import" else "pending",
                        "winner" if kind == "import" else None,
                    ),
                )
                if kind == "publication":
                    db.execute(
                        "INSERT INTO import_publications(queue_id,state,owner_token,series_id,dst_dir,import_mode,"
                        "staging_dir,queue_snapshot_json,series_tags_json,queue_status)"
                        " VALUES(71,'prepared','winner',1,'/dest','copy','/staging','{}','[]','pending')"
                    )
        return result

    monkeypatch.setattr(module, name, raced)
    if operation == "delete":
        assert (
            volume_file_deletion.delete_volume_file(1, 11).status
            == "import_in_progress"
        )
        assert merged is not None and merged.exists()
    else:
        assert rescan.rescan_series_folder(1)["recovered"] == 0
    assert rows(state_env, "chapters") == before


@pytest.mark.parametrize("operation", ["delete", "rescan"])
@pytest.mark.parametrize("change", ["root", "parent"])
def test_inventory_cannot_outlive_series_root_or_parent_owner(
    state_env, monkeypatch, operation, change
):
    import rescan
    import volume_file_deletion

    archive(state_env["library"] / "State Series Ch001.cbz")
    if operation == "delete":
        merged = complete_merge(state_env)
        module, name = volume_file_deletion, "inspect_volume_file_deletion"
    else:
        merged = None
        module, name = rescan, "build_filesystem_inventory"
    original = getattr(module, name)
    before = rows(state_env, "chapters")

    def raced(*args):
        result = original(*args)
        with sqlite3.connect(state_env["db_path"]) as db:
            if change == "root":
                db.execute("UPDATE root_folders SET path='/winner' WHERE id=1")
            else:
                db.execute("UPDATE volumes SET download_id='winner' WHERE id=11")
        return result

    monkeypatch.setattr(module, name, raced)
    if operation == "delete":
        assert volume_file_deletion.delete_volume_file(1, 11).status == "changed"
        assert merged is not None and merged.exists()
    else:
        assert rescan.rescan_series_folder(1)["recovered"] == 0
    assert rows(state_env, "chapters") == before


def test_recycled_series_deletion_preserves_independent_chapter(state_env):
    import rescan
    import shared
    import volume_file_deletion

    merged = complete_merge(state_env)
    with sqlite3.connect(state_env["db_path"]) as db:
        db.execute("UPDATE series SET deleted_at='2026-10-10' WHERE id=1")
    before_series = rows(state_env, "series")
    before_chapters = rows(state_env, "chapters")
    with shared.get_db() as db:
        assert rescan.snapshot_series_rescan(db, 1) is None

    result = volume_file_deletion.delete_volume_file(1, 11)

    assert result.status == "complete"
    assert not merged.exists()
    assert rows(state_env, "series") == before_series
    assert rows(state_env, "chapters") == [
        {
            **chapter,
            "import_path": str(
                state_env["library"] / f"State Series Ch{number:03d}.cbz"
            ),
        }
        for number, chapter in enumerate(before_chapters, 1)
    ]


@pytest.mark.parametrize("deleted_at", [None, "2026-10-10"])
def test_deletion_refuses_concurrent_recycle_state_change(
    state_env, monkeypatch, deleted_at
):
    import volume_file_deletion

    merged = complete_merge(state_env)
    with sqlite3.connect(state_env["db_path"]) as db:
        db.execute("UPDATE series SET deleted_at=? WHERE id=1", (deleted_at,))
    before = rows(state_env, "chapters")
    before_history = rows(state_env, "history")
    original = volume_file_deletion.inspect_volume_file_deletion

    def raced(*args):
        result = original(*args)
        with sqlite3.connect(state_env["db_path"]) as db:
            db.execute(
                "UPDATE series SET deleted_at=? WHERE id=1",
                ("2026-10-10" if deleted_at is None else None,),
            )
        return result

    monkeypatch.setattr(volume_file_deletion, "inspect_volume_file_deletion", raced)
    assert volume_file_deletion.delete_volume_file(1, 11).status == "changed"
    assert merged.exists()
    assert rows(state_env, "chapters") == before
    assert rows(state_env, "history") == before_history


@pytest.mark.parametrize("operation", ["delete", "rescan"])
def test_independent_file_checks_never_run_under_sqlite_writer(
    state_env, monkeypatch, operation
):
    import rescan
    import shared
    import volume_file_deletion

    if operation == "delete":
        complete_merge(state_env)
    else:
        archive(state_env["library"] / "State Series Ch001.cbz")
    active = []
    observed = []
    real_get_db = shared.get_db
    real_lstat = rescan.os.lstat

    @contextmanager
    def tracked_get_db():
        with real_get_db() as db:
            active.append(db)
            try:
                yield db
            finally:
                active.remove(db)

    def checked_lstat(*args, **kwargs):
        assert not any(db.in_transaction for db in active)
        observed.append(args[0])
        return real_lstat(*args, **kwargs)

    monkeypatch.setattr(rescan, "get_db", tracked_get_db)
    monkeypatch.setattr(volume_file_deletion, "get_db", tracked_get_db)
    monkeypatch.setattr(rescan.os, "lstat", checked_lstat)
    if operation == "delete":
        assert volume_file_deletion.delete_volume_file(1, 11).status == "complete"
    else:
        assert rescan.rescan_series_folder(1)["recovered"] == 1
    assert observed


@pytest.fixture
def deleted_volume_alias(state_env: dict[str, Any]) -> dict[str, Any]:
    import volume_file_deletion

    volume = state_env["library"] / "State Series v01.cbz"
    alias = state_env["library"] / "State Series Ch001.cbz"
    archive(volume, b"page" + b"x" * (2 * 1024 * 1024))
    alias.hardlink_to(volume)
    with sqlite3.connect(state_env["db_path"]) as db:
        db.execute(
            "UPDATE volumes SET status='downloaded',import_path=? WHERE id=11",
            (str(volume),),
        )
        db.execute(
            "UPDATE chapters SET status='downloaded',import_path=? WHERE id=1",
            (str(volume),),
        )
    assert volume_file_deletion.delete_volume_file(1, 11).status == "complete"
    assert not volume.exists()
    assert alias.stat().st_nlink == 1
    assert rows(state_env, "chapters")[0]["status"] == "wanted"
    with sqlite3.connect(state_env["db_path"]) as db:
        db.row_factory = sqlite3.Row
        journal = dict(db.execute("SELECT * FROM volume_file_deletions").fetchone())
    assert journal["state"] == "completed"
    assert journal["target_present"] == 1
    return {**state_env, "alias": alias, "journal": journal}


@pytest.mark.parametrize("identity", ["canonical", "persisted"])
def test_completed_deletion_alias_stays_wanted_after_rescan_and_restart(
    deleted_volume_alias: dict[str, Any], identity: str
) -> None:
    import rescan

    env = deleted_volume_alias
    alias = env["alias"]
    if identity == "persisted":
        alias = alias.rename(env["library"] / "Historical Ch001.cbz")
        with sqlite3.connect(env["db_path"]) as db:
            db.execute("UPDATE chapters SET import_path=? WHERE id=1", (str(alias),))
    before = {
        table: rows(env, table)
        for table in (
            "series",
            "chapters",
            "volumes",
            "history",
            "series_metadata_fields",
            "series_metadata_sources",
        )
    }
    digest = hashlib.sha256(alias.read_bytes()).hexdigest()
    for _ in range(2):
        assert rescan.rescan_series_folder(1) == {
            "found": 0,
            "recovered": 0,
            "missing": 0,
            "lost": 0,
            "created": 0,
        }
        assert {table: rows(env, table) for table in before} == before
    restarted = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json,sys,shared,rescan; shared.DB_PATH=sys.argv[1]; "
            "shared.CONFIG={}; print(json.dumps(rescan.rescan_series_folder(1)))",
            str(env["db_path"]),
        ],
        env={**os.environ, "PYTHONPATH": str(Path(rescan.__file__).parent)},
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert json.loads(restarted.stdout)["recovered"] == 0
    assert {table: rows(env, table) for table in before} == before
    assert hashlib.sha256(alias.read_bytes()).hexdigest() == digest
    with sqlite3.connect(env["db_path"]) as db:
        db.row_factory = sqlite3.Row
        assert (
            dict(db.execute("SELECT * FROM volume_file_deletions").fetchone())
            == env["journal"]
        )


@pytest.mark.parametrize(
    "control",
    [
        "identical-copy",
        "source-hardlink",
        "changed-digest",
        "changed-size",
        "changed-mtime",
        "foreign-series",
        "missing-target",
        "incomplete-stat",
        "no-journal",
    ],
)
def test_completed_deletion_does_not_blacklist_independent_chapters(
    deleted_volume_alias: dict[str, Any], control: str
) -> None:
    import rescan

    env = deleted_volume_alias
    alias = env["alias"]
    old = alias.stat()
    if control in ("identical-copy", "source-hardlink"):
        source = env["tmp"] / "independent.cbz"
        if control == "identical-copy":
            source.write_bytes(alias.read_bytes())
            os.utime(source, ns=(old.st_atime_ns, old.st_mtime_ns))
        else:
            archive(source, b"independent chapter")
        alias.unlink()
        if control == "source-hardlink":
            alias.hardlink_to(source)
            assert alias.stat().st_nlink == 2
        else:
            source.rename(alias)
            assert (
                hashlib.sha256(alias.read_bytes()).hexdigest()
                == env["journal"]["target_sha256"]
            )
        assert alias.stat().st_ino != old.st_ino
    elif control == "changed-digest":
        with alias.open("r+b") as stream:
            stream.seek(-1, os.SEEK_END)
            stream.write(b"!")
        os.utime(alias, ns=(old.st_atime_ns, old.st_mtime_ns))
        current = alias.stat()
        assert (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
        ) == (
            old.st_dev,
            old.st_ino,
            old.st_size,
            old.st_mtime_ns,
        )
        assert (
            hashlib.sha256(alias.read_bytes()).hexdigest()
            != env["journal"]["target_sha256"]
        )
    elif control == "changed-size":
        with alias.open("ab") as stream:
            stream.write(b"new content")
    elif control == "changed-mtime":
        os.utime(alias, ns=(old.st_atime_ns, old.st_mtime_ns + 1))
    else:
        with sqlite3.connect(env["db_path"]) as db:
            if control == "foreign-series":
                db.execute("UPDATE volume_file_deletions SET series_id=2")
            elif control == "missing-target":
                db.execute("UPDATE volume_file_deletions SET target_present=0")
            elif control == "incomplete-stat":
                db.execute("UPDATE volume_file_deletions SET target_inode=NULL")
            else:
                db.execute("DELETE FROM volume_file_deletions")
    before_series = rows(env, "series")
    assert rescan.rescan_series_folder(1)["recovered"] == 1
    assert rows(env, "chapters")[0]["import_path"] == str(alias)
    assert rows(env, "chapters")[0]["status"] == "downloaded"
    assert rows(env, "series") == before_series
    assert rescan.rescan_series_folder(1)["recovered"] == 0


@pytest.mark.parametrize(
    "failure",
    ["open", "read", "path-swap", "symlink-swap", "mutation", "missing-digest"],
)
def test_matching_deletion_evidence_verification_failure_refuses_chapter_visibly(
    deleted_volume_alias: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    import rescan

    env = deleted_volume_alias
    alias = env["alias"]
    expected_inode = alias.stat().st_ino
    replacement = env["tmp"] / "replacement.cbz"
    archive(replacement, b"independent")
    real_open, real_read = os.open, os.read
    triggered = []

    def raced_open(path, flags, *args, **kwargs):
        if failure == "open" and os.fspath(path) in (str(alias), alias.name):
            triggered.append(True)
            raise PermissionError("simulated unreadable candidate")
        return real_open(path, flags, *args, **kwargs)

    def raced_read(descriptor: int, size: int) -> bytes:
        if os.fstat(descriptor).st_ino == expected_inode:
            assert 0 < size <= 1024 * 1024
            if not triggered:
                triggered.append(True)
                if failure == "read":
                    raise OSError("simulated hash read failure")
                if failure in ("path-swap", "symlink-swap"):
                    alias.unlink()
                    if failure == "path-swap":
                        replacement.rename(alias)
                    else:
                        alias.symlink_to(replacement)
                elif failure == "mutation":
                    old = os.fstat(descriptor)
                    with alias.open("r+b") as stream:
                        stream.seek(-1, os.SEEK_END)
                        stream.write(b"!")
                    os.utime(alias, ns=(old.st_atime_ns, old.st_mtime_ns))
        return real_read(descriptor, size)

    if failure == "missing-digest":
        with sqlite3.connect(env["db_path"]) as db:
            db.execute("UPDATE volume_file_deletions SET target_sha256=NULL")
    monkeypatch.setattr(rescan.os, "open", raced_open)
    monkeypatch.setattr(rescan.os, "read", raced_read)
    assert rescan.rescan_series_folder(1)["recovered"] == 0
    assert rows(env, "chapters")[0]["status"] == "wanted"
    assert "verification refused" in caplog.text.lower()
    if failure != "missing-digest":
        assert triggered


def test_completed_deletion_hash_streams_outside_sqlite_writer(
    deleted_volume_alias: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import rescan

    env = deleted_volume_alias
    inode = env["alias"].stat().st_ino
    real_read = os.read
    sizes = []

    def checked_read(descriptor: int, size: int) -> bytes:
        if os.fstat(descriptor).st_ino == inode:
            sizes.append(size)
            assert 0 < size <= 1024 * 1024
            with sqlite3.connect(env["db_path"], timeout=0.05) as writer:
                writer.execute("BEGIN IMMEDIATE")
                writer.rollback()
        return real_read(descriptor, size)

    monkeypatch.setattr(rescan.os, "read", checked_read)
    assert rescan.rescan_series_folder(1)["recovered"] == 0
    assert len(sizes) >= 3
    assert rows(env, "chapters")[0]["status"] == "wanted"


def test_completed_deletion_journal_change_after_inventory_fences_recovery(
    deleted_volume_alias: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import rescan

    env = deleted_volume_alias
    with sqlite3.connect(env["db_path"]) as db:
        db.execute("UPDATE volume_file_deletions SET target_present=0")
    original = rescan.build_filesystem_inventory

    def raced(snapshot):
        inventory = original(snapshot)
        assert 1 in inventory.files_by_chapter
        with sqlite3.connect(env["db_path"]) as db:
            db.execute("UPDATE volume_file_deletions SET target_present=1")
        return inventory

    monkeypatch.setattr(rescan, "build_filesystem_inventory", raced)
    assert rescan.rescan_series_folder(1)["recovered"] == 0
    assert rows(env, "chapters")[0]["status"] == "wanted"
