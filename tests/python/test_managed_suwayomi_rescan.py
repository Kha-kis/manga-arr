"""Managed assembled Suwayomi files use recorded absence, not client identity."""

from __future__ import annotations

import errno
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from test_loose_chapter_state import (
    archive,
    complete_merge,
    rows,
    state_env as state_env,
)


@pytest.fixture
def assembled_env(state_env: dict[str, Any]) -> dict[str, Any]:
    merged = complete_merge(state_env)
    # Model the completed Suwayomi row explicitly, not a client-less import.
    with sqlite3.connect(state_env["db_path"]) as db:
        db.execute("UPDATE volumes SET client='suwayomi' WHERE id=11")
    return {**state_env, "merged": merged}


def file_bytes(env: dict[str, Any]) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in env["tmp"].rglob("*.cbz")}


@pytest.mark.parametrize("surviving", [(), (1,), (1, 2, 3, 4)])
def test_missing_assembled_volume_resets_only_children_without_loose_files(
    assembled_env: dict[str, Any], surviving: tuple[int, ...]
) -> None:
    import rescan

    env = assembled_env
    env["merged"].unlink()
    for number in range(1, 5):
        if number not in surviving:
            (env["library"] / f"State Series Ch{number:03d}.cbz").unlink()
    protected = {
        table: rows(env, table)
        for table in ("series", "series_metadata_fields", "series_metadata_sources")
    }
    with sqlite3.connect(env["db_path"]) as db:
        provenance = db.execute(
            "SELECT * FROM chapter_file_provenance ORDER BY id"
        ).fetchall()
    content = file_bytes(env)
    result = rescan.rescan_series_folder(1)

    assert result == {
        "found": 0,
        "recovered": len(surviving),
        "missing": 1,
        "lost": 0,
        "created": 0,
    }
    volume = rows(env, "volumes")[0]
    assert volume["status"] == "wanted" and volume["import_path"] is None
    assert volume["client"] is None
    for chapter in rows(env, "chapters"):
        number = int(chapter["chapter_num"])
        if number in surviving:
            assert chapter["status"] == "downloaded"
            assert chapter["import_path"] == str(
                env["library"] / f"State Series Ch{number:03d}.cbz"
            )
            assert chapter["client"] == "suwayomi"
        else:
            assert chapter["status"] == "wanted"
            assert chapter["client"] is None
            # Preserve the existing missing-volume cascade metadata contract.
            assert chapter["import_path"] == str(env["merged"])
    assert {table: rows(env, table) for table in protected} == protected
    with sqlite3.connect(env["db_path"]) as db:
        assert (
            db.execute("SELECT * FROM chapter_file_provenance ORDER BY id").fetchall()
            == provenance
        )
    history = rows(env, "history")
    assert sum(row["event_type"] == "file_deleted" for row in history) == 1
    settled = (rows(env, "volumes"), rows(env, "chapters"), history)
    assert rescan.rescan_series_folder(1) == {
        "found": 0,
        "recovered": 0,
        "missing": 0,
        "lost": 0,
        "created": 0,
    }
    assert (
        rows(env, "volumes"),
        rows(env, "chapters"),
        rows(env, "history"),
    ) == settled
    assert file_bytes(env) == content


def test_missing_assembled_volume_preserves_unmonitored_child(
    assembled_env: dict[str, Any],
) -> None:
    import rescan

    env = assembled_env
    for path in env["library"].glob("*.cbz"):
        path.unlink()
    with sqlite3.connect(env["db_path"]) as db:
        db.execute("UPDATE chapters SET monitored=0 WHERE id=2")
    before = rows(env, "chapters")[1]

    assert rescan.rescan_series_folder(1)["missing"] == 1
    assert rows(env, "volumes")[0]["status"] == "wanted"
    assert rows(env, "chapters")[1] == before


def test_missing_loose_child_with_missing_parent_defers_until_absence_is_proven(
    assembled_env: dict[str, Any],
) -> None:
    import rescan

    env = assembled_env
    loose = env["library"] / "State Series Ch002.cbz"
    with sqlite3.connect(env["db_path"]) as db:
        db.execute("UPDATE chapters SET import_path=? WHERE id=2", (str(loose),))
        db.execute("UPDATE chapters SET monitored=0 WHERE id=4")
    env["merged"].unlink()
    loose.unlink()
    before = rows(env, "chapters")
    content = file_bytes(env)

    result = rescan.rescan_series_folder(1)

    chapters = rows(env, "chapters")
    assert result["missing"] == 1 and result["recovered"] == 3
    assert rows(env, "volumes")[0]["status"] == "wanted"
    assert chapters[1] == before[1]
    assert chapters[3] == {
        **before[3],
        "import_path": str(env["library"] / "State Series Ch004.cbz"),
    }
    assert file_bytes(env) == content
    assert rescan.rescan_series_folder(1)["missing"] == 1
    assert rows(env, "chapters")[1] == {
        **before[1],
        "status": "wanted",
        "import_path": None,
        "size_bytes": None,
        "quality": None,
        "imported_at": None,
    }
    assert rescan.rescan_series_folder(1)["missing"] == 0


@pytest.mark.parametrize("observation", ["external", "ambiguous", "inaccessible"])
@pytest.mark.parametrize("client", ["suwayomi", "qbittorrent"])
@pytest.mark.parametrize("status", ["downloaded", "wanted"])
def test_missing_parent_preserves_unknown_independent_child_observation(
    assembled_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    observation: str,
    client: str,
    status: str,
) -> None:
    import rescan

    env = assembled_env
    canonical = env["library"] / "State Series Ch002.cbz"
    if observation == "external":
        independent = env["tmp"] / "external" / canonical.name
    elif observation == "ambiguous":
        independent = env["library"] / "State Series Alternate Ch002.cbz"
    else:
        independent = env["library"] / "nested" / canonical.name
    independent.parent.mkdir(exist_ok=True)
    canonical.rename(independent)
    if observation == "ambiguous":
        archive(canonical, b"alternate")
    for number in (1, 3, 4):
        (env["library"] / f"State Series Ch{number:03d}.cbz").unlink()
    env["merged"].unlink()
    with sqlite3.connect(env["db_path"]) as db:
        db.execute(
            "UPDATE chapters SET import_path=?,status=?,grabbed_at='2026-10-10',"
            "imported_at='2026-10-11',size_bytes=123,download_id='independent-job',"
            "release_group='Independent group' WHERE id=2",
            (str(independent), status),
        )
        db.execute("UPDATE volumes SET client=? WHERE id=11", (client,))
    before = rows(env, "chapters")[1]
    expected = (
        before
        if client == "suwayomi"
        else {
            **before,
            "status": "wanted",
            "grabbed_at": None,
            "torrent_name": None,
            "torrent_url": None,
            "indexer": None,
            "protocol": None,
            "client": None,
            "download_id": None,
            "download_client_id": None,
            "release_group": None,
        }
    )
    content = file_bytes(env)
    if observation == "inaccessible":
        real_lstat = os.lstat

        def denied(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
            if os.fspath(path) in (str(independent), str(independent.parent)):
                raise PermissionError(errno.EACCES, "independent directory unavailable")
            return real_lstat(path, *args, **kwargs)

        monkeypatch.setattr(rescan.os, "lstat", denied)

    assert rescan.rescan_series_folder(1) == {
        "found": 0,
        "recovered": 0,
        "missing": 1,
        "lost": 0,
        "created": 0,
    }
    assert rows(env, "volumes")[0]["status"] == "wanted"
    assert rows(env, "chapters")[1] == expected
    assert [chapter["status"] for chapter in rows(env, "chapters")] == [
        "wanted",
        expected["status"],
        "wanted",
        "wanted",
    ]
    assert rescan.rescan_series_folder(1)["missing"] == 0
    assert rows(env, "chapters")[1] == expected
    assert {path: path.read_bytes() for path in content} == content


def test_missing_managed_parent_resets_child_without_recorded_path(
    assembled_env: dict[str, Any],
) -> None:
    import rescan

    env = assembled_env
    for path in env["library"].glob("*.cbz"):
        path.unlink()
    with sqlite3.connect(env["db_path"]) as db:
        db.execute("UPDATE chapters SET import_path=NULL WHERE id=2")

    assert rescan.rescan_series_folder(1)["missing"] == 1
    chapter = rows(env, "chapters")[1]
    assert chapter["status"] == "wanted"
    assert chapter["import_path"] is None and chapter["client"] is None


def test_restored_managed_volume_and_repeat_do_not_change_source_bytes(
    assembled_env: dict[str, Any],
) -> None:
    import rescan

    env = assembled_env
    merged = env["merged"]
    content = merged.read_bytes()
    for path in env["library"].glob("*.cbz"):
        path.unlink()
    assert rescan.rescan_series_folder(1)["missing"] == 1
    source = file_bytes(env)
    merged.write_bytes(content)

    result = rescan.rescan_series_folder(1)

    assert result == {
        "found": 1,
        "recovered": 1,
        "missing": 0,
        "lost": 0,
        "created": 0,
    }
    assert rows(env, "volumes")[0]["status"] == "downloaded"
    assert all(chapter["status"] == "downloaded" for chapter in rows(env, "chapters"))
    settled = (rows(env, "volumes"), rows(env, "chapters"), rows(env, "history"))
    assert rescan.rescan_series_folder(1)["recovered"] == 0
    assert (
        rows(env, "volumes"),
        rows(env, "chapters"),
        rows(env, "history"),
    ) == settled
    assert {path: path.read_bytes() for path in source} == source


def test_reported_sequential_deletion_and_restore(
    assembled_env: dict[str, Any],
) -> None:
    import rescan

    env = assembled_env
    content = file_bytes(env)
    protected = {
        table: rows(env, table)
        for table in ("series", "series_metadata_fields", "series_metadata_sources")
    }
    for number in (3, 4):
        (env["library"] / f"State Series Ch{number:03d}.cbz").unlink()

    assert rescan.rescan_series_folder(1) == {
        "found": 1,
        "recovered": 2,
        "missing": 0,
        "lost": 0,
        "created": 0,
    }
    chapters = rows(env, "chapters")
    assert [chapter["status"] for chapter in chapters] == ["downloaded"] * 4
    assert [chapter["import_path"] for chapter in chapters[2:]] == [
        str(env["merged"])
    ] * 2

    env["merged"].unlink()
    assert rescan.rescan_series_folder(1) == {
        "found": 0,
        "recovered": 0,
        "missing": 1,
        "lost": 0,
        "created": 0,
    }
    assert rows(env, "volumes")[0]["status"] == "wanted"
    assert [chapter["status"] for chapter in rows(env, "chapters")] == [
        "downloaded",
        "downloaded",
        "wanted",
        "wanted",
    ]

    (env["library"] / "State Series Ch002.cbz").unlink()
    assert rescan.rescan_series_folder(1) == {
        "found": 0,
        "recovered": 0,
        "missing": 1,
        "lost": 0,
        "created": 0,
    }
    chapters = rows(env, "chapters")
    assert [chapter["status"] for chapter in chapters] == [
        "downloaded",
        "wanted",
        "wanted",
        "wanted",
    ]
    assert chapters[1]["import_path"] is None
    assert chapters[1]["client"] == "suwayomi"
    assert {path: path.read_bytes() for path in content if path.exists()} == {
        path: data for path, data in content.items() if path.exists()
    }

    for path, data in content.items():
        if not path.exists():
            path.write_bytes(data)
    assert rescan.rescan_series_folder(1) == {
        "found": 1,
        "recovered": 4,
        "missing": 0,
        "lost": 0,
        "created": 0,
    }
    assert rows(env, "volumes")[0]["status"] == "downloaded"
    for chapter in rows(env, "chapters"):
        assert chapter["status"] == "downloaded"
        assert chapter["import_path"] == str(
            env["library"] / f"State Series Ch{int(chapter['chapter_num']):03d}.cbz"
        )
    assert {table: rows(env, table) for table in protected} == protected
    assert file_bytes(env) == content
    assert rescan.rescan_series_folder(1) == {
        "found": 1,
        "recovered": 0,
        "missing": 0,
        "lost": 0,
        "created": 0,
    }
    assert sum(row["event_type"] == "file_deleted" for row in rows(env, "history")) == 1
