"""Verified disappearance reconciles observations, never acquisition intent (#444)."""

from __future__ import annotations

import errno
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any

import pytest

from test_loose_chapter_state import (
    archive,
    complete_merge,
    rows,
    state_env as state_env,
)


def missing(env: dict[str, Any], *, linked: bool = False) -> Path:
    path = env["library"] / "State Series Ch001.cbz"
    archive(path)
    with sqlite3.connect(env["db_path"]) as db:
        db.execute(
            "UPDATE chapters SET status='downloaded',import_path=?,volume_id=?,"
            "size_bytes=123,imported_at='2026-10-10' WHERE id=1",
            (str(path), 11 if linked else None),
        )
    path.unlink()
    return path


def protected(env: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        table: rows(env, table)
        for table in (
            "series",
            "history",
            "series_metadata_fields",
            "series_metadata_sources",
        )
    }


@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize("monitored", [False, True])
def test_missing_independent_observation_and_repeat(state_env, linked, monitored):
    import rescan

    missing(state_env, linked=linked)
    with sqlite3.connect(state_env["db_path"]) as db:
        db.execute("UPDATE chapters SET monitored=? WHERE id=1", (monitored,))
    before = rows(state_env, "chapters")
    unchanged = protected(state_env)
    result = rescan.rescan_series_folder(1)
    expected = {
        **before[0],
        "status": "wanted",
        "import_path": None,
        "quality": None,
        "size_bytes": None,
        "imported_at": None,
    }
    assert rows(state_env, "chapters") == [expected, *before[1:]]
    assert result["missing"] == 1
    assert protected(state_env) == unchanged
    assert rescan.rescan_series_folder(1)["missing"] == 0
    assert rows(state_env, "chapters") == [expected, *before[1:]]


@pytest.mark.parametrize("mapped", [False, True])
def test_surviving_volume_requires_mapping_and_verified_current_file(state_env, mapped):
    import rescan

    volume = complete_merge(state_env)
    path = missing(state_env, linked=True)
    if not mapped:
        with sqlite3.connect(state_env["db_path"]) as db:
            db.execute("UPDATE series SET chapter_vol_map='{}' WHERE id=1")
    before = rows(state_env, "chapters")
    unchanged = protected(state_env)
    volumes = rows(state_env, "volumes")
    content = volume.read_bytes()
    result = rescan.rescan_series_folder(1)
    expected = {
        **before[0],
        "status": "downloaded" if mapped else "wanted",
        "import_path": str(volume) if mapped else None,
        "size_bytes": None,
        "quality": None,
        "imported_at": None,
    }
    assert rows(state_env, "chapters")[0] == expected
    assert result["missing"] == 1
    assert protected(state_env) == unchanged
    assert rows(state_env, "volumes") == volumes
    assert volume.read_bytes() == content and not path.exists()
    assert rescan.rescan_series_folder(1)["missing"] == 0


def test_surviving_loose_observation_is_unchanged(state_env):
    import rescan

    path = missing(state_env)
    archive(path)
    before = rows(state_env, "chapters")
    assert rescan.rescan_series_folder(1)["missing"] == 0
    assert rows(state_env, "chapters") == before
    assert path.exists()


@pytest.mark.parametrize(
    "problem",
    [
        "unavailable",
        "permission",
        "symlink",
        "parent-symlink",
        "outside",
        "directory",
        "ambiguous",
        "no-path",
        "range",
    ],
)
def test_unproven_absence_does_not_erase_observation(state_env, monkeypatch, problem):
    import rescan

    path = missing(state_env)
    if problem == "unavailable":
        state_env["library"].rename(state_env["tmp"] / "offline")
    elif problem == "permission":
        original = os.open

        def denied(name, *args, **kwargs):
            if os.fspath(name) in (str(state_env["library"]), "State Series"):
                raise PermissionError(errno.EACCES, "offline fixture")
            return original(name, *args, **kwargs)

        monkeypatch.setattr(os, "open", denied)
    elif problem == "symlink":
        path.symlink_to(state_env["tmp"] / "missing.cbz")
    elif problem == "parent-symlink":
        state_env["library"].rename(state_env["tmp"] / "linked")
        state_env["library"].symlink_to(
            state_env["tmp"] / "linked", target_is_directory=True
        )
    elif problem == "outside":
        with sqlite3.connect(state_env["db_path"]) as db:
            db.execute(
                "UPDATE chapters SET import_path=? WHERE id=1",
                (str(state_env["tmp"] / path.name),),
            )
    elif problem == "directory":
        path.mkdir()
    elif problem == "ambiguous":
        archive(state_env["library"] / "State Series Chapter 1.cbz")
    elif problem == "no-path":
        with sqlite3.connect(state_env["db_path"]) as db:
            db.execute("UPDATE chapters SET import_path=NULL WHERE id=1")
    else:
        with sqlite3.connect(state_env["db_path"]) as db:
            db.execute("UPDATE chapters SET chapter_range_end=2 WHERE id=1")
    before = rows(state_env, "chapters")
    rescan.rescan_series_folder(1)
    assert rows(state_env, "chapters") == before


@pytest.mark.parametrize("race", ["return", "directory-replaced", "volume-replaced"])
def test_file_and_directory_return_races_refuse_stale_absence(
    state_env, monkeypatch, race
):
    import rescan

    volume = complete_merge(state_env) if race == "volume-replaced" else None
    path = missing(state_env, linked=volume is not None)
    before = rows(state_env, "chapters")
    original = rescan.build_filesystem_inventory

    def raced(snapshot):
        inventory = original(snapshot)
        if race == "return":
            archive(path)
        elif race == "directory-replaced":
            state_env["library"].rename(state_env["tmp"] / "original")
            state_env["library"].mkdir()
        else:
            assert volume is not None
            volume.unlink()
            archive(volume, b"different volume")
        return inventory

    monkeypatch.setattr(rescan, "build_filesystem_inventory", raced)
    rescan.rescan_series_folder(1)
    assert rows(state_env, "chapters")[0] == before[0]


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "grabbed"),
        ("import_path", "/winner.cbz"),
        ("download_id", "winner"),
        ("monitored", 0),
        ("volume_id", 11),
    ],
)
def test_competing_observation_writer_wins(state_env, monkeypatch, field, value):
    import rescan

    missing(state_env)
    original = rescan.build_filesystem_inventory
    winner = []

    def raced(snapshot):
        inventory = original(snapshot)
        with sqlite3.connect(state_env["db_path"]) as db:
            db.execute(f"UPDATE chapters SET {field}=? WHERE id=1", (value,))
        winner.extend(rows(state_env, "chapters"))
        return inventory

    monkeypatch.setattr(rescan, "build_filesystem_inventory", raced)
    rescan.rescan_series_folder(1)
    assert rows(state_env, "chapters") == winner


def test_live_filesystem_owner_refuses_missing_reconciliation(state_env):
    import rescan
    from file_mutation_lock import FileMutationBusy, file_mutation_guard

    missing(state_env)
    before = rows(state_env, "chapters")
    with file_mutation_guard(str(state_env["db_path"])):
        with pytest.raises(FileMutationBusy):
            rescan.rescan_series_folder(1)
    assert rows(state_env, "chapters") == before


def test_inventory_without_owned_revalidation_cannot_erase_observations(state_env):
    import rescan

    missing(state_env)
    before = rows(state_env, "chapters")
    with rescan.get_db() as db:
        snapshot = rescan.snapshot_series_rescan(db, 1)
    assert snapshot is not None
    inventory = rescan.build_filesystem_inventory(snapshot)
    with rescan.get_db() as db:
        result = rescan.reconcile_series_inventory(db, snapshot, inventory)
    assert result.result["missing"] == 0
    assert rows(state_env, "chapters") == before


@pytest.mark.parametrize(
    "problem", ["empty-loose", "unsupported", "nested", "shared-path", "bad-map"]
)
def test_unsupported_or_ambiguous_missing_identity_refuses(state_env, problem):
    import rescan

    path = missing(state_env)
    with sqlite3.connect(state_env["db_path"]) as db:
        if problem == "empty-loose":
            path.touch()
        elif problem == "unsupported":
            path = path.with_suffix(".txt")
            path.write_bytes(b"not manga")
            db.execute("UPDATE chapters SET import_path=? WHERE id=1", (str(path),))
        elif problem == "nested":
            path = path.parent / "chapters" / path.name
            path.parent.mkdir()
            db.execute("UPDATE chapters SET import_path=? WHERE id=1", (str(path),))
        elif problem == "shared-path":
            db.execute("UPDATE chapters SET import_path=? WHERE id=2", (str(path),))
        else:
            db.execute("UPDATE series SET chapter_vol_map='broken-json' WHERE id=1")
    before = rows(state_env, "chapters")
    rescan.rescan_series_folder(1)
    assert rows(state_env, "chapters") == before


@pytest.mark.parametrize(
    "problem", ["empty", "symlink", "permission", "outside", "null-path", "empty-path"]
)
def test_unproven_backing_volume_does_not_replace_observation(
    state_env, monkeypatch, problem
):
    import rescan

    volume = complete_merge(state_env)
    missing(state_env, linked=True)
    if problem == "empty":
        volume.write_bytes(b"")
    elif problem == "symlink":
        volume.unlink()
        target = state_env["tmp"] / "outside.cbz"
        archive(target)
        volume.symlink_to(target)
    elif problem == "outside":
        target = state_env["tmp"] / volume.name
        target.write_bytes(volume.read_bytes())
        with sqlite3.connect(state_env["db_path"]) as db:
            db.execute("UPDATE volumes SET import_path=? WHERE id=11", (str(target),))
    elif problem in ("null-path", "empty-path"):
        with sqlite3.connect(state_env["db_path"]) as db:
            db.execute(
                "UPDATE volumes SET import_path=? WHERE id=11",
                (None if problem == "null-path" else "",),
            )
    else:
        original = os.open

        def denied(name, *args, **kwargs):
            if os.fspath(name) == volume.name:
                raise PermissionError(errno.EACCES, "unavailable volume")
            return original(name, *args, **kwargs)

        monkeypatch.setattr(os, "open", denied)
    before = rows(state_env, "chapters")
    rescan.rescan_series_folder(1)
    assert rows(state_env, "chapters")[0] == before[0]


@pytest.mark.parametrize("change", ["parent", "mapping", "root"])
def test_late_coverage_or_root_writer_is_not_overwritten(
    state_env, monkeypatch, change
):
    import rescan

    complete_merge(state_env)
    missing(state_env, linked=True)
    before = rows(state_env, "chapters")
    original = rescan.build_filesystem_inventory

    def raced(snapshot):
        inventory = original(snapshot)
        with sqlite3.connect(state_env["db_path"]) as db:
            if change == "parent":
                db.execute("UPDATE volumes SET download_id='winner' WHERE id=11")
            elif change == "mapping":
                db.execute("UPDATE series SET chapter_vol_map='{}' WHERE id=1")
            else:
                db.execute("UPDATE root_folders SET path='/winner' WHERE id=1")
        return inventory

    monkeypatch.setattr(rescan, "build_filesystem_inventory", raced)
    rescan.rescan_series_folder(1)
    assert rows(state_env, "chapters")[0] == before[0]


@pytest.mark.parametrize("kind", ["rescan", "import", "publication", "deletion"])
def test_late_operation_fences_block_absence_update(state_env, monkeypatch, kind):
    import rescan

    missing(state_env)
    before = rows(state_env, "chapters")
    original = rescan.build_filesystem_inventory

    def raced(snapshot):
        inventory = original(snapshot)
        with sqlite3.connect(state_env["db_path"]) as db:
            if kind == "rescan":
                db.execute(
                    "INSERT INTO rescan_file_operations(operation_token,series_id,volume_id,source_path,destination_path,expected_volume_json,expected_context_json,fingerprints_json,carriers_json,state) VALUES('late',1,11,'/source','/dest','{}','{}','{}','{}','prepared')"
                )
            elif kind == "deletion":
                db.execute(
                    "INSERT INTO volume_file_deletions(volume_id,series_id,state,target_path,parent_path,claim_path,target_present,series_title) VALUES(12,1,'active','/foreign','/','/claim',0,'State Series')"
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
                        "INSERT INTO import_publications(queue_id,state,owner_token,series_id,dst_dir,import_mode,staging_dir,queue_snapshot_json,series_tags_json,queue_status) VALUES(71,'prepared','winner',1,'/dest','copy','/staging','{}','[]','pending')"
                    )
        return inventory

    monkeypatch.setattr(rescan, "build_filesystem_inventory", raced)
    assert rescan.rescan_series_folder(1)["missing"] == 0
    assert rows(state_env, "chapters") == before


@pytest.mark.parametrize(
    "mapping",
    [
        {"1.0": 1},
        {"001": 1},
        {"1": "1.0"},
        {"1": 1, "1.0": "1.0"},
        {"1.000": 1},
        {"1e0": "01"},
        {" 1 ": "1.0"},
    ],
)
def test_missing_backing_coverage_accepts_existing_map_formats(state_env, mapping):
    import rescan

    volume = complete_merge(state_env)
    missing(state_env, linked=True)
    with sqlite3.connect(state_env["db_path"]) as db:
        db.execute(
            "UPDATE series SET chapter_vol_map=? WHERE id=1", (json.dumps(mapping),)
        )
    before = rows(state_env, "chapters")[0]
    assert rescan.rescan_series_folder(1)["missing"] == 1
    assert rows(state_env, "chapters")[0] == {
        **before,
        "import_path": str(volume),
        "size_bytes": None,
        "quality": None,
        "imported_at": None,
    }


def test_missing_backing_coverage_does_not_round_fractional_chapter_key(state_env):
    import rescan

    volume = complete_merge(state_env)
    missing(state_env, linked=True)
    number = 1.23456789
    path = state_env["library"] / f"State Series Ch{number}.cbz"
    with sqlite3.connect(state_env["db_path"]) as db:
        db.execute(
            "UPDATE chapters SET chapter_num=?,import_path=? WHERE id=1",
            (number, str(path)),
        )
        db.execute(
            "UPDATE series SET chapter_vol_map=? WHERE id=1",
            (json.dumps({str(number): 1}),),
        )
    before = rows(state_env, "chapters")[0]
    assert rescan.rescan_series_folder(1)["missing"] == 1
    assert rows(state_env, "chapters")[0] == {
        **before,
        "import_path": str(volume),
        "size_bytes": None,
        "quality": None,
        "imported_at": None,
    }


@pytest.mark.parametrize("mapping", [{"1": 1, "1.0": 2}, {"1.0": 2, "1": 1}])
def test_missing_conflicting_equivalent_map_keys_defer_unchanged(state_env, mapping):
    import rescan

    complete_merge(state_env)
    rescan.rescan_series_folder(1)
    missing(state_env, linked=True)
    with sqlite3.connect(state_env["db_path"]) as db:
        db.execute(
            "UPDATE series SET chapter_vol_map=? WHERE id=1", (json.dumps(mapping),)
        )
    before = rows(state_env, "chapters")
    assert rescan.rescan_series_folder(1)["missing"] == 0
    assert rows(state_env, "chapters") == before


@pytest.mark.parametrize(
    "assigned", [None, True, False, "invalid", "NaN", "Infinity", "-Infinity"]
)
@pytest.mark.parametrize("alias", [False, True])
def test_missing_unknown_equivalent_assignment_defers(state_env, assigned, alias):
    import rescan

    complete_merge(state_env)
    rescan.rescan_series_folder(1)
    missing(state_env, linked=True)
    mapping = {"1": 1, "1.0": assigned} if alias else {"1": assigned}
    with sqlite3.connect(state_env["db_path"]) as db:
        db.execute(
            "UPDATE series SET chapter_vol_map=? WHERE id=1", (json.dumps(mapping),)
        )
    before = rows(state_env, "chapters")
    assert rescan.rescan_series_folder(1)["missing"] == 0
    assert rows(state_env, "chapters") == before


def test_verified_custom_backing_does_not_reset_parent(state_env, monkeypatch):
    import main
    import shared
    import rescan

    volume = complete_merge(state_env)
    rescan.rescan_series_folder(1)
    custom = volume.with_name("State Series Book 01.cbz")
    volume.rename(custom)
    monkeypatch.setitem(main.CONFIG, "file_format", "{Series Title} Book {Volume:02d}")
    monkeypatch.setitem(
        shared.CONFIG, "file_format", "{Series Title} Book {Volume:02d}"
    )
    # Chapter naming remains an explicitly independent managed path.
    monkeypatch.setitem(main.CONFIG, "chapter_format", "{Series Title} Ch{Chapter:03d}")
    monkeypatch.setitem(
        shared.CONFIG, "chapter_format", "{Series Title} Ch{Chapter:03d}"
    )
    with sqlite3.connect(state_env["db_path"]) as db:
        db.execute(
            "UPDATE volumes SET import_path=?,client='qbittorrent' WHERE id=11",
            (str(custom),),
        )
    missing(state_env, linked=True)
    before = rows(state_env, "volumes")
    content = custom.read_bytes()
    result = rescan.rescan_series_folder(1)
    assert rows(state_env, "volumes") == before
    assert rows(state_env, "chapters")[0]["import_path"] == str(custom)
    assert rows(state_env, "chapters")[0]["status"] == "downloaded"
    assert result["missing"] == 1 and result["found"] == 1
    assert custom.read_bytes() == content


@pytest.mark.parametrize(
    "chapter_range,covers", [("010-020", False), ("001-004", True)]
)
def test_range_ambiguity_only_blocks_covered_missing_chapter(
    state_env, chapter_range, covers
):
    import rescan

    missing(state_env)
    path = state_env["library"] / f"State Series Ch{chapter_range}.cbz"
    archive(path)
    before = rows(state_env, "chapters")[0]
    result = rescan.rescan_series_folder(1)
    after = rows(state_env, "chapters")[0]
    assert result["missing"] == (0 if covers else 1)
    if covers:
        assert after == before
    else:
        assert after["status"] == "wanted" and after["import_path"] is None
    assert path.exists()


@pytest.mark.parametrize("colliding_format", [False, True])
def test_recorded_volume_chapter_marker_is_verified_as_backing(
    state_env, monkeypatch, colliding_format
):
    import main
    import shared
    import rescan

    volume = complete_merge(state_env)
    rescan.rescan_series_folder(1)
    preferred = volume.with_name("State Series v01 Ch001.cbz")
    volume.rename(preferred)
    with sqlite3.connect(state_env["db_path"]) as db:
        db.execute("UPDATE volumes SET import_path=? WHERE id=11", (str(preferred),))
    if colliding_format:
        for config in (main.CONFIG, shared.CONFIG):
            monkeypatch.setitem(
                config, "chapter_format", "{Series Title} v{Volume:02d} Ch{Chapter:03d}"
            )
    missing(state_env, linked=True)
    before = rows(state_env, "volumes")
    assert rescan.rescan_series_folder(1)["missing"] == 1
    assert rows(state_env, "chapters")[0]["import_path"] == str(preferred)
    assert rows(state_env, "chapters")[0]["status"] == "downloaded"
    assert rows(state_env, "volumes") == before and preferred.exists()


def test_busy_direct_worker_reports_unexecuted_scan(state_env):
    import rescan
    from file_mutation_lock import FileMutationBusy, file_mutation_guard

    missing(state_env)
    before = rows(state_env, "chapters")
    with file_mutation_guard(str(state_env["db_path"])):
        with pytest.raises(FileMutationBusy):
            rescan.rescan_series_folder(1)
    assert rows(state_env, "chapters") == before


@pytest.mark.parametrize("htmx", [False, True])
def test_busy_manual_request_waits_and_completes_after_release(
    state_env, monkeypatch, htmx
):
    import main
    import rescan
    from file_mutation_lock import file_mutation_guard
    from routers import series_ as route
    from starlette.requests import Request

    missing(state_env)
    attempted = threading.Event()

    def worker(sid):
        try:
            return rescan.rescan_series_folder(sid)
        finally:
            attempted.set()

    monkeypatch.setattr(main, "rescan_series_folder", worker)
    monkeypatch.setattr(main, "log_event", lambda *args, **kwargs: None)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "headers": [(b"hx-request", b"true")] if htmx else [],
        }
    )

    async def run():
        with file_mutation_guard(str(state_env["db_path"])):
            task = asyncio.create_task(route.rescan_series(request, 1))
            assert await asyncio.to_thread(attempted.wait, 5)
            await asyncio.sleep(0.05)
            assert not task.done(), "busy scan was reported as completed"
        response = await asyncio.wait_for(task, 3)
        assert response.status_code == (200 if htmx else 303)
        if htmx:
            assert "nothing changed" not in response.headers["HX-Trigger"]

    asyncio.run(run())
    assert rows(state_env, "chapters")[0]["status"] == "wanted"


def test_busy_full_scan_does_not_advance_before_retry(state_env, monkeypatch):
    import main
    import rescan
    from file_mutation_lock import file_mutation_guard
    from routers import series_ as route

    missing(state_env)
    with sqlite3.connect(state_env["db_path"]) as db:
        db.execute(
            "INSERT INTO series(id,title,search_pattern,root_folder_id,folder_name) VALUES(2,'Other','Other',1,'Other')"
        )
    (state_env["library"].parent / "Other").mkdir()
    attempted = threading.Event()
    visits = []

    def worker(sid):
        visits.append(sid)
        try:
            return rescan.rescan_series_folder(sid)
        finally:
            attempted.set()

    monkeypatch.setattr(main, "rescan_series_folder", worker)
    monkeypatch.setattr(main, "log_event", lambda *args, **kwargs: None)

    async def run():
        with file_mutation_guard(str(state_env["db_path"])):
            task = asyncio.create_task(route._rescan_all_impl())
            assert await asyncio.to_thread(attempted.wait, 5)
            await asyncio.sleep(0.05)
            assert not task.done() and visits == [1]
        await asyncio.wait_for(task, 3)

    asyncio.run(run())
    assert visits == [1, 1, 2]
    assert rows(state_env, "chapters")[0]["status"] == "wanted"


def test_busy_async_retry_is_promptly_cancellable(state_env):
    import rescan
    from file_mutation_lock import file_mutation_guard
    from rescan_file_recovery import rescan_series_in_thread

    missing(state_env)
    before = rows(state_env, "chapters")
    attempted = threading.Event()

    def worker(sid):
        try:
            return rescan.rescan_series_folder(sid)
        finally:
            attempted.set()

    async def run():
        with file_mutation_guard(str(state_env["db_path"])):
            task = asyncio.create_task(rescan_series_in_thread(1, worker))
            assert await asyncio.to_thread(attempted.wait, 5)
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 0.5)

    asyncio.run(run())
    assert rows(state_env, "chapters") == before


@pytest.mark.parametrize("backed", [False, True])
def test_htmx_missing_observation_summary_is_truthful(state_env, monkeypatch, backed):
    import main
    import rescan
    from routers import series_ as route
    from starlette.requests import Request

    if backed:
        complete_merge(state_env)
        rescan.rescan_series_folder(1)
    missing(state_env, linked=backed)
    monkeypatch.setattr(main, "log_event", lambda *args, **kwargs: None)
    request = Request(
        {"type": "http", "method": "POST", "headers": [(b"hx-request", b"true")]}
    )
    response = asyncio.run(route.rescan_series(request, 1))
    message = json.loads(response.headers["HX-Trigger"])["showToast"]["msg"]
    assert response.status_code == 200
    assert "nothing changed" not in message
    if backed:
        assert rows(state_env, "chapters")[0]["status"] == "downloaded"
        assert "reset to wanted" not in message
    else:
        assert rows(state_env, "chapters")[0]["status"] == "wanted"
        assert "reset to wanted" in message or "missing file observation" in message


@pytest.mark.parametrize("writer", ["parent", "chapter"])
def test_custom_backing_cas_loser_cannot_contribute_file_count(
    state_env, monkeypatch, writer
):
    import rescan

    volume = complete_merge(state_env)
    rescan.rescan_series_folder(1)
    custom = volume.with_name("State Series Book 01.cbz")
    volume.rename(custom)
    with sqlite3.connect(state_env["db_path"]) as db:
        db.execute(
            "UPDATE volumes SET import_path=?,client='qbittorrent' WHERE id=11",
            (str(custom),),
        )
    missing(state_env, linked=True)
    original = rescan.build_filesystem_inventory
    winner = []

    def raced(snapshot):
        inventory = original(snapshot)
        with sqlite3.connect(state_env["db_path"]) as db:
            db.execute(
                f"UPDATE {'volumes' if writer == 'parent' else 'chapters'} SET download_id='winner' WHERE id=?",
                (11 if writer == "parent" else 1,),
            )
        winner.extend(rows(state_env, "chapters"))
        return inventory

    monkeypatch.setattr(rescan, "build_filesystem_inventory", raced)
    unchanged = protected(state_env)
    result = rescan.rescan_series_folder(1)
    assert result["found"] == 0
    assert rows(state_env, "chapters")[0] == winner[0]
    # Legacy volume-only missing reconciliation is outside this correction;
    # no unaccepted chapter backing proof may create local metadata credit.
    assert rows(state_env, "series") == unchanged["series"]
    assert (
        rows(state_env, "series_metadata_fields") == unchanged["series_metadata_fields"]
    )
    assert custom.exists()
