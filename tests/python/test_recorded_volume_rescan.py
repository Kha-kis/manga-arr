"""Recorded files are not missing merely because a filename parser misses them."""

from __future__ import annotations

import errno
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from test_loose_chapter_state import archive, rows, state_env as state_env


@pytest.fixture
def recorded_env(state_env, monkeypatch) -> dict[str, Any]:
    import main
    import shared

    path = state_env["library"] / "State Series Book 01.cbz"
    archive(path)
    for config in (main.CONFIG, shared.CONFIG):
        monkeypatch.setitem(config, "file_format", "{Series Title} Book {Volume:02d}")
    with sqlite3.connect(state_env["db_path"]) as db:
        db.execute("DELETE FROM chapters")
        db.execute(
            "UPDATE volumes SET status='downloaded',import_path=?,client='qbittorrent',"
            "quality='cbz',size_bytes=123,imported_at='2026-10-10',"
            "download_id='original-job',torrent_name='Original release',"
            "indexer='Original indexer',protocol='torrent' WHERE id=11",
            (str(path),),
        )
    return {**state_env, "recorded": path}


def state(env: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        table: rows(env, table)
        for table in (
            "series",
            "volumes",
            "chapters",
            "history",
            "series_metadata_fields",
            "series_metadata_sources",
        )
    }


def assert_deferred(env: dict[str, Any], before: dict[str, Any]) -> None:
    import rescan

    result = rescan.rescan_series_folder(1)
    assert result["missing"] == 0
    assert result["recovered"] == 0 and result["created"] == 0
    assert result["found"] == 0  # No custom-file inventory/count injection.
    assert state(env) == before


@pytest.mark.parametrize("children", [False, True])
def test_custom_recorded_file_preserves_parent_children_and_repeat(
    recorded_env, children
):
    if children:
        with sqlite3.connect(recorded_env["db_path"]) as db:
            db.execute(
                "INSERT INTO chapters(id,series_id,volume_id,chapter_num,status,"
                "import_path,quality) VALUES(1,1,11,1,'downloaded',?,'cbz')",
                (str(recorded_env["recorded"]),),
            )
    before = state(recorded_env)
    content = recorded_env["recorded"].read_bytes()
    assert_deferred(recorded_env, before)
    assert_deferred(recorded_env, before)
    assert recorded_env["recorded"].read_bytes() == content


@pytest.mark.parametrize(
    "problem",
    [
        "permission",
        "unavailable",
        "symlink",
        "ancestor_symlink",
        "empty",
        "directory",
        "outside",
        "nested",
        "unsupported",
        "shared_numbered",
        "shared_pack",
        "shared_chapter",
        "hardlink",
        "conflicting_number",
    ],
)
def test_unknown_recorded_evidence_does_not_reset_or_create_credit(
    recorded_env, monkeypatch, problem
):
    import rescan

    path = recorded_env["recorded"]
    if problem == "permission":
        real_open = rescan.os.open

        def denied(name, *args, **kwargs):
            if name in (str(path), path.name):
                raise PermissionError(errno.EACCES, "private fixture denied")
            return real_open(name, *args, **kwargs)

        monkeypatch.setattr(rescan.os, "open", denied)
    elif problem == "unavailable":

        @contextmanager
        def unavailable(root):
            raise OSError(errno.ENODEV, "private fixture unavailable")
            yield  # pragma: no cover

        monkeypatch.setattr(rescan, "_open_rescan_directory", unavailable)
    elif problem == "symlink":
        target = recorded_env["tmp"] / "outside.cbz"
        path.rename(target)
        path.symlink_to(target)
    elif problem == "ancestor_symlink":
        actual = recorded_env["tmp"] / "actual-library"
        recorded_env["library"].rename(actual)
        recorded_env["library"].symlink_to(actual, target_is_directory=True)
    elif problem == "empty":
        path.write_bytes(b"")
    elif problem == "directory":
        path.unlink()
        path.mkdir()
    elif problem in ("outside", "nested", "unsupported", "conflicting_number"):
        destination = {
            "outside": recorded_env["tmp"] / "outside.cbz",
            "nested": recorded_env["library"] / "nested" / path.name,
            "unsupported": path.with_suffix(".txt"),
            "conflicting_number": path.with_name("State Series v02.cbz"),
        }[problem]
        destination.parent.mkdir(parents=True, exist_ok=True)
        path.rename(destination)
        with sqlite3.connect(recorded_env["db_path"]) as db:
            db.execute(
                "UPDATE volumes SET import_path=? WHERE id=11", (str(destination),)
            )
    elif problem in ("shared_numbered", "shared_pack"):
        with sqlite3.connect(recorded_env["db_path"]) as db:
            db.execute(
                "INSERT INTO volumes(id,series_id,volume_num,status,import_path,quality)"
                " VALUES(12,1,?,'downloaded',?,'cbz')",
                (2 if problem == "shared_numbered" else None, str(path)),
            )
    elif problem == "shared_chapter":
        with sqlite3.connect(recorded_env["db_path"]) as db:
            db.execute(
                "INSERT INTO chapters(id,series_id,chapter_num,status,import_path,quality)"
                " VALUES(1,1,99,'downloaded',?,'cbz')",
                (str(path),),
            )
    elif problem == "hardlink":
        os.link(path, path.with_name("State Series Alternate.cbz"))
    before = state(recorded_env)
    assert_deferred(recorded_env, before)


@pytest.mark.parametrize(
    "race", ["removed", "replaced", "returned", "directory", "permission", "template"]
)
def test_recorded_evidence_changes_after_inventory_defer(
    recorded_env, monkeypatch, race
):
    import rescan

    path = recorded_env["recorded"]
    if race in ("returned", "template"):
        path.unlink()
    before = state(recorded_env)
    real_build = rescan.build_filesystem_inventory

    def changed(snapshot):
        inventory = real_build(snapshot)
        if race in ("removed", "replaced", "returned"):
            if race != "returned":
                path.unlink()
            if race != "removed":
                archive(path, b"replacement")
        elif race == "directory":
            recorded_env["library"].rename(recorded_env["tmp"] / "old-directory")
            recorded_env["library"].mkdir()
        elif race == "permission":
            real_open = rescan.os.open

            def denied(name, *args, **kwargs):
                if name in (str(path), path.name):
                    raise PermissionError(errno.EACCES, "private permission loss")
                return real_open(name, *args, **kwargs)

            monkeypatch.setattr(rescan.os, "open", denied)
        else:
            import main
            import shared

            for config in (main.CONFIG, shared.CONFIG):
                monkeypatch.setitem(
                    config, "file_format", "{Series Title} New {Volume:02d}"
                )
        return inventory

    monkeypatch.setattr(rescan, "build_filesystem_inventory", changed)
    assert_deferred(recorded_env, before)


def test_verified_managed_absence_retains_existing_reset_contract(recorded_env):
    import rescan

    recorded_env["recorded"].unlink()
    result = rescan.rescan_series_folder(1)
    assert result["missing"] == 1 and result["created"] == 0
    assert rows(recorded_env, "volumes")[0]["status"] == "wanted"
    assert rows(recorded_env, "history")[0]["event_type"] == "file_deleted"
    assert rescan.rescan_series_folder(1)["missing"] == 0


def test_recognized_recorded_file_keeps_existing_inventory_precedence(recorded_env):
    import rescan

    path = recorded_env["recorded"].with_name("State Series v01.cbz")
    recorded_env["recorded"].rename(path)
    with sqlite3.connect(recorded_env["db_path"]) as db:
        db.execute("UPDATE volumes SET import_path=? WHERE id=11", (str(path),))
    before = state(recorded_env)
    result = rescan.rescan_series_folder(1)
    assert result["found"] == 1 and result["missing"] == 0
    assert state(recorded_env) == before


@pytest.mark.parametrize(
    "writer",
    [
        "parent",
        "new_owner",
        "new_owner_alias",
        "new_pack",
        "new_chapter",
        "root",
        "title",
    ],
)
def test_concurrent_database_writer_cannot_authorize_reset(
    recorded_env, monkeypatch, writer
):
    import rescan

    recorded_env["recorded"].unlink()
    real_build = rescan.build_filesystem_inventory
    winner = {}

    def changed(snapshot):
        inventory = real_build(snapshot)
        with sqlite3.connect(recorded_env["db_path"]) as db:
            if writer == "parent":
                db.execute("UPDATE volumes SET download_id='writer-wins' WHERE id=11")
            elif writer in ("new_owner", "new_owner_alias", "new_pack"):
                path = str(recorded_env["recorded"])
                if writer == "new_owner_alias":
                    path = (
                        f"{recorded_env['library']}/./{recorded_env['recorded'].name}"
                    )
                db.execute(
                    "INSERT INTO volumes(id,series_id,volume_num,status,import_path,quality)"
                    " VALUES(12,1,?,'downloaded',?,'cbz')",
                    (
                        2 if writer == "new_owner" else None,
                        path,
                    ),
                )
            elif writer == "new_chapter":
                db.execute(
                    "INSERT INTO chapters(id,series_id,chapter_num,status,import_path,quality)"
                    " VALUES(1,1,99,'downloaded',?,'cbz')",
                    (str(recorded_env["recorded"]),),
                )
            elif writer == "title":
                db.execute("UPDATE series SET title='Changed title' WHERE id=1")
            else:
                db.execute(
                    "UPDATE root_folders SET path=? WHERE id=1",
                    (str(recorded_env["tmp"]),),
                )
        winner.update(state(recorded_env))
        return inventory

    monkeypatch.setattr(rescan, "build_filesystem_inventory", changed)
    assert_deferred(recorded_env, winner)


def test_direct_inventory_without_owned_revalidation_cannot_reset(recorded_env):
    import rescan

    recorded_env["recorded"].unlink()
    before = state(recorded_env)
    with rescan.get_db() as db:
        snapshot = rescan.snapshot_series_rescan(db, 1)
    assert snapshot is not None
    inventory = rescan.build_filesystem_inventory(snapshot)
    with rescan.get_db() as db:
        result = rescan.reconcile_series_inventory(db, snapshot, inventory)
    assert result.result["missing"] == 0
    assert state(recorded_env) == before


@pytest.mark.parametrize(
    "kind", ["rescan", "import", "publication", "deletion", "completed"]
)
def test_late_operation_and_completed_deletion_fences(recorded_env, monkeypatch, kind):
    import rescan

    recorded_env["recorded"].unlink()
    before = state(recorded_env)
    real_build = rescan.build_filesystem_inventory

    def fenced(snapshot):
        inventory = real_build(snapshot)
        with sqlite3.connect(recorded_env["db_path"]) as db:
            if kind == "rescan":
                db.execute(
                    "INSERT INTO rescan_file_operations(operation_token,series_id,volume_id,"
                    "source_path,destination_path,expected_volume_json,expected_context_json,"
                    "fingerprints_json,carriers_json,state)"
                    " VALUES('late',1,11,'/source','/dest','{}','{}','{}','{}','prepared')"
                )
            elif kind in ("deletion", "completed"):
                db.execute(
                    "INSERT INTO volume_file_deletions(volume_id,series_id,state,target_path,"
                    "parent_path,claim_path,target_present,series_title)"
                    " VALUES(11,1,?,'/private-target','/','/private-claim',1,'State Series')",
                    ("active" if kind == "deletion" else "completed",),
                )
            else:
                db.execute(
                    "INSERT INTO import_queue(id,series_id,status,lease_owner)"
                    " VALUES(71,1,?,?)",
                    (
                        "importing" if kind == "import" else "pending",
                        "winner" if kind == "import" else None,
                    ),
                )
                if kind == "publication":
                    db.execute(
                        "INSERT INTO import_publications(queue_id,state,owner_token,series_id,"
                        "dst_dir,import_mode,staging_dir,queue_snapshot_json,series_tags_json,queue_status)"
                        " VALUES(71,'prepared','winner',1,'/dest','copy','/staging','{}','[]','pending')"
                    )
        return inventory

    monkeypatch.setattr(rescan, "build_filesystem_inventory", fenced)
    assert_deferred(recorded_env, before)


def test_old_completed_deletion_does_not_blacklist_replacement(recorded_env):
    with sqlite3.connect(recorded_env["db_path"]) as db:
        db.execute(
            "INSERT INTO volume_file_deletions(volume_id,series_id,state,target_path,"
            "parent_path,claim_path,target_present,series_title)"
            " VALUES(11,1,'completed',?,'/','/old-claim',1,'State Series')",
            (str(recorded_env["recorded"]),),
        )
    before = state(recorded_env)
    assert_deferred(recorded_env, before)


@pytest.mark.parametrize("status", ["wanted", "grabbed"])
def test_recorded_guard_does_not_change_discovery_or_recovery(recorded_env, status):
    import rescan

    path = recorded_env["recorded"].with_name("State Series v01.cbz")
    recorded_env["recorded"].rename(path)
    with sqlite3.connect(recorded_env["db_path"]) as db:
        db.execute(
            "UPDATE volumes SET status=?,import_path=NULL WHERE id=11", (status,)
        )
    result = rescan.rescan_series_folder(1)
    assert result["found"] == 1 and result["recovered"] == 1
    assert result["missing"] == 0 and result["created"] == 0
    assert rows(recorded_env, "volumes")[0]["status"] == "downloaded"


def test_no_recorded_path_retains_existing_missing_behavior(recorded_env):
    import rescan

    recorded_env["recorded"].unlink()
    with sqlite3.connect(recorded_env["db_path"]) as db:
        db.execute("UPDATE volumes SET import_path=NULL WHERE id=11")
    assert rescan.rescan_series_folder(1)["missing"] == 1


def test_missing_historical_path_with_current_template_candidate_defers(recorded_env):
    old_path = recorded_env["library"] / "State Series Historical Book.cbz"
    with sqlite3.connect(recorded_env["db_path"]) as db:
        db.execute("UPDATE volumes SET import_path=? WHERE id=11", (str(old_path),))
    assert_deferred(recorded_env, state(recorded_env))


def test_exact_conflicting_path_quarantine_does_not_hide_unrelated_discovery(
    recorded_env,
):
    import rescan

    conflict = recorded_env["recorded"].with_name("State Series v02.cbz")
    recorded_env["recorded"].rename(conflict)
    archive(recorded_env["library"] / "State Series v03.cbz")
    with sqlite3.connect(recorded_env["db_path"]) as db:
        db.execute("UPDATE volumes SET import_path=? WHERE id=11", (str(conflict),))
    result = rescan.rescan_series_folder(1)
    assert result["missing"] == 0
    assert result["created"] == 1 and result["found"] == 1
    assert [
        (row["volume_num"], row["status"]) for row in rows(recorded_env, "volumes")
    ] == [(1, "downloaded"), (3, "downloaded")]


def test_template_change_after_revalidation_defers_reset(recorded_env, monkeypatch):
    import shared
    import rescan

    recorded_env["recorded"].unlink()
    before = state(recorded_env)
    real_reconcile = rescan.reconcile_series_inventory

    def changed(db, snapshot, inventory):
        assert inventory.recorded_volumes_revalidated
        monkeypatch.setitem(
            shared.CONFIG, "file_format", "{Series Title} Revised {Volume:02d}"
        )
        return real_reconcile(db, snapshot, inventory)

    monkeypatch.setattr(rescan, "reconcile_series_inventory", changed)
    assert_deferred(recorded_env, before)


@pytest.mark.parametrize(
    "filename", ["State Series Book 01.CBZ", "state series Book 01.cbz"]
)
def test_case_variant_current_template_candidate_defers(recorded_env, filename):
    original = recorded_env["recorded"]
    alternate = original.with_name(filename)
    content = original.read_bytes()
    original.rename(alternate)
    before = state(recorded_env)
    assert_deferred(recorded_env, before)
    assert alternate.read_bytes() == content and not original.exists()


def test_unrelated_case_variant_book_does_not_block_verified_absence(recorded_env):
    import rescan

    recorded_env["recorded"].unlink()
    unrelated = recorded_env["recorded"].with_name("state series Book 02.CBZ")
    archive(unrelated)
    content = unrelated.read_bytes()
    result = rescan.rescan_series_folder(1)
    assert result["missing"] == 1
    assert result["found"] == 0 and result["created"] == 0
    assert rows(recorded_env, "volumes")[0]["status"] == "wanted"
    assert unrelated.read_bytes() == content


@pytest.mark.parametrize("suffix", [".zip", ".cbr", ".pdf"])
def test_supported_format_current_template_candidate_defers(recorded_env, suffix):
    original = recorded_env["recorded"]
    alternate = original.with_suffix(suffix)
    content = original.read_bytes()
    original.rename(alternate)
    before = state(recorded_env)
    assert_deferred(recorded_env, before)
    assert alternate.read_bytes() == content and not original.exists()


def test_unrelated_supported_format_book_does_not_block_verified_absence(recorded_env):
    import rescan

    recorded_env["recorded"].unlink()
    unrelated = recorded_env["recorded"].with_name("State Series Book 02.zip")
    archive(unrelated)
    content = unrelated.read_bytes()
    result = rescan.rescan_series_folder(1)
    assert result["missing"] == 1
    assert result["found"] == 0 and result["created"] == 0
    assert rows(recorded_env, "volumes")[0]["status"] == "wanted"
    assert unrelated.read_bytes() == content
