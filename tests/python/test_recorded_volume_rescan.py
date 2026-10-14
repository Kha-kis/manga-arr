"""Recorded files are not missing merely because a filename parser misses them."""

from __future__ import annotations

import errno
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
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


@pytest.mark.parametrize("drift", [False, True, "initial_hardlink"])
@pytest.mark.parametrize("safe", [None, "loose", "unparsed"])
def test_quarantined_carrier_cannot_confirm_complete_pack(
    recorded_env, monkeypatch, drift, safe
):
    import rescan

    path = recorded_env["recorded"]
    if not drift:
        path = path.with_name("State Series v02.cbz")
        recorded_env["recorded"].rename(path)
    elif drift == "initial_hardlink":
        os.link(path, path.with_name("State Series Alias.cbz"))
    with sqlite3.connect(recorded_env["db_path"]) as db:
        db.execute("UPDATE volumes SET import_path=? WHERE id=11", (str(path),))
        db.execute(
            "INSERT INTO volumes(id,series_id,volume_num,status,quality) VALUES(12,1,3,'wanted','cbz')"
        )
        db.execute(
            "INSERT INTO volumes(id,series_id,pack_type,status,quality) VALUES(13,1,'complete','grabbed','cbz')"
        )
        db.execute(
            "INSERT INTO chapters(id,series_id,volume_id,chapter_num,status,monitored,quality) VALUES(7,1,12,7,'wanted',1,'cbz')"
        )
    if safe:
        archive(
            recorded_env["library"]
            / (
                "State Series Ch099.cbz"
                if safe == "loose"
                else "State Series Extra.cbz"
            )
        )
    before = state(recorded_env)
    real_build = rescan.build_filesystem_inventory

    def changed(snapshot):
        inventory = real_build(snapshot)
        if drift is True:
            os.link(path, path.with_name("State Series Alias.cbz"))
        return inventory

    monkeypatch.setattr(rescan, "build_filesystem_inventory", changed)
    result = rescan.rescan_series_folder(1)
    assert result["found"] == 0 and result["created"] == 0 and result["missing"] == 0
    if safe is None:
        assert result["recovered"] == 0 and state(recorded_env) == before
    else:
        assert result["recovered"] == 1
        assert [row["status"] for row in rows(recorded_env, "volumes")] == [
            "downloaded"
        ] * 3
        assert rows(recorded_env, "chapters")[0]["status"] == "downloaded"
        for table in (
            "series",
            "history",
            "series_metadata_fields",
            "series_metadata_sources",
        ):
            assert rows(recorded_env, table) == before[table]


@pytest.mark.parametrize(
    "problem",
    [
        "contradictory",
        "hardlink",
        "shared",
        "late_owner",
        "late_parsed_owner",
        "late_case_owner",
        "context",
        "unrevalidated",
        "mismatch",
        "healthy",
    ],
)
def test_missing_chapter_backing_requires_current_recorded_safety(
    recorded_env, monkeypatch, problem
):
    import rescan
    import shared

    path = recorded_env["recorded"]
    if problem in ("contradictory", "late_parsed_owner"):
        alternate = path.with_name(
            "State Series v02.cbz"
            if problem == "contradictory"
            else "State Series v01.cbz"
        )
        path.rename(alternate)
        path = alternate
    if problem == "hardlink":
        os.link(path, path.with_name("State Series Alias.cbz"))
    with sqlite3.connect(recorded_env["db_path"]) as db:
        db.execute("UPDATE volumes SET import_path=? WHERE id=11", (str(path),))
        db.execute(
            "INSERT INTO chapters(id,series_id,volume_id,chapter_num,status,import_path,quality)"
            " VALUES(1,1,11,1,'downloaded',?,'cbz')",
            (str(recorded_env["library"] / "State Series Ch001.cbz"),),
        )
        if problem == "shared":
            db.execute(
                "INSERT INTO volumes(id,series_id,volume_num,status,import_path,quality) VALUES(12,1,2,'downloaded',?,'cbz')",
                (str(path),),
            )
    before = state(recorded_env)
    real_build = rescan.build_filesystem_inventory

    def changed(snapshot):
        inventory = real_build(snapshot)
        if problem in ("late_owner", "late_case_owner", "late_parsed_owner"):
            owner_path = f"{path.parent}/./{path.name}"
            if problem == "late_case_owner":
                owner_path = f"{path.parent}/./{path.name.lower()}"
            with sqlite3.connect(recorded_env["db_path"]) as db:
                db.execute(
                    "INSERT INTO volumes(id,series_id,volume_num,status,import_path,quality) VALUES(12,1,2,'downloaded',?,'cbz')",
                    (owner_path,),
                )
            before.update(state(recorded_env))
        return inventory

    real_reconcile = rescan.reconcile_series_inventory

    def changed_context(db, snapshot, inventory):
        if problem == "context":
            monkeypatch.setitem(
                shared.CONFIG, "file_format", "{Series Title} Revised {Volume:02d}"
            )
        elif problem == "unrevalidated":
            inventory = replace(inventory, recorded_volumes_revalidated=False)
        elif problem == "mismatch":
            proof = inventory.recorded_volumes[11]
            assert proof.file is not None
            inventory = replace(
                inventory,
                recorded_volumes={
                    11: replace(
                        proof, file=replace(proof.file, inode=proof.file.inode + 1)
                    )
                },
            )
        return real_reconcile(db, snapshot, inventory)

    monkeypatch.setattr(rescan, "build_filesystem_inventory", changed)
    monkeypatch.setattr(rescan, "reconcile_series_inventory", changed_context)
    result = rescan.rescan_series_folder(1)
    if problem == "healthy":
        assert result["missing"] == 1 and result["found"] == 1
        assert rows(recorded_env, "chapters")[0]["import_path"] == str(path)
        assert rows(recorded_env, "volumes") == before["volumes"]
    else:
        assert result["missing"] == 0 and result["found"] == 0
        assert state(recorded_env) == before


@pytest.mark.parametrize("late", [False, True])
def test_casefolded_logical_alias_refuses_absence(recorded_env, monkeypatch, late):
    import rescan

    recorded_env["recorded"].unlink()
    before = {}

    def insert_alias():
        path = recorded_env["recorded"]
        with sqlite3.connect(recorded_env["db_path"]) as db:
            db.execute(
                "INSERT INTO volumes(id,series_id,volume_num,status,import_path,quality) VALUES(12,1,2,'downloaded',?,'cbz')",
                (f"{path.parent}/./{path.name.lower()}",),
            )
        before.update(state(recorded_env))

    if not late:
        insert_alias()
    real_build = rescan.build_filesystem_inventory

    def changed(snapshot):
        inventory = real_build(snapshot)
        if late:
            insert_alias()
        return inventory

    monkeypatch.setattr(rescan, "build_filesystem_inventory", changed)
    assert_deferred(recorded_env, before)


def traced_owner_lookups(monkeypatch) -> list[str]:
    import rescan

    queries: list[str] = []
    real_get_db = rescan.get_db

    @contextmanager
    def traced():
        with real_get_db() as db:

            def capture(query):
                if (
                    "FROM volumes WHERE import_path IS NOT NULL" in query
                    and "UNION ALL" in query
                ):
                    queries.append(query)

            db.set_trace_callback(capture)
            yield db

    monkeypatch.setattr(rescan, "get_db", traced)
    return queries


@pytest.mark.parametrize("noise", [0, 200])
@pytest.mark.parametrize("evidence", ["present", "unknown"])
def test_noop_recorded_proofs_do_not_scan_global_owners(
    recorded_env, monkeypatch, noise, evidence
):
    import rescan

    with sqlite3.connect(recorded_env["db_path"]) as db:
        db.execute(
            "INSERT INTO series(id,title,search_pattern) VALUES(2,'Other','Other')"
        )
        db.executemany(
            "INSERT INTO volumes(series_id,volume_num,status,import_path) VALUES(2,?,'wanted',?)",
            (
                (number, f"/private-other/v{number}.cbz")
                for number in range(1, noise + 1)
            ),
        )
    if evidence == "unknown":
        recorded_env["recorded"].write_bytes(b"")
    queries = traced_owner_lookups(monkeypatch)
    for _ in range(3):
        assert rescan.rescan_series_folder(1)["missing"] == 0
    assert queries == []


def test_eligible_absences_batch_one_fresh_owner_lookup(recorded_env, monkeypatch):
    import rescan

    recorded_env["recorded"].unlink()
    with sqlite3.connect(recorded_env["db_path"]) as db:
        db.execute(
            "INSERT INTO volumes(id,series_id,volume_num,status,import_path,quality) VALUES(12,1,2,'downloaded',?,'cbz')",
            (str(recorded_env["library"] / "State Series Book 02.cbz"),),
        )
    queries = traced_owner_lookups(monkeypatch)
    assert rescan.rescan_series_folder(1)["missing"] == 2
    assert len(queries) == 1


def test_owner_lookup_is_fresh_after_existing_path_changes(recorded_env, monkeypatch):
    import rescan

    path = recorded_env["recorded"]
    path.unlink()
    with sqlite3.connect(recorded_env["db_path"]) as db:
        db.execute(
            "INSERT INTO volumes(id,series_id,volume_num,status,import_path,quality) VALUES(12,1,2,'wanted','/private-other.cbz','cbz')"
        )
    queries = traced_owner_lookups(monkeypatch)
    assert rescan.rescan_series_folder(1)["missing"] == 1
    with sqlite3.connect(recorded_env["db_path"]) as db:
        db.execute(
            "UPDATE volumes SET status='downloaded',import_path=? WHERE id=11",
            (str(path),),
        )
        db.execute("UPDATE volumes SET import_path=? WHERE id=12", (str(path),))
    before = state(recorded_env)
    assert rescan.rescan_series_folder(1)["missing"] == 0
    assert state(recorded_env) == before
    with sqlite3.connect(recorded_env["db_path"]) as db:
        db.execute("UPDATE volumes SET import_path='/private-other.cbz' WHERE id=12")
    assert rescan.rescan_series_folder(1)["missing"] == 1
    assert len(queries) == 2  # The middle, UNKNOWN snapshot must not trigger a lookup.


def test_owner_snapshot_changed_in_current_writer_defers_backing(
    recorded_env, monkeypatch
):
    import rescan

    path = recorded_env["recorded"]
    with sqlite3.connect(recorded_env["db_path"]) as db:
        db.execute(
            "INSERT INTO volumes(id,series_id,volume_num,status,import_path,quality) VALUES(12,1,2,'wanted','/private-other.cbz','cbz')"
        )
        db.execute(
            "INSERT INTO chapters(id,series_id,volume_id,chapter_num,status,import_path,quality) VALUES(1,1,11,1,'downloaded',?,'cbz')",
            (str(recorded_env["library"] / "State Series Ch001.cbz"),),
        )
    real_owners = rescan._recorded_volume_path_owners
    before = {}

    def changed(db, *args):
        owners = real_owners(db, *args)
        db.execute("UPDATE volumes SET import_path=? WHERE id=12", (str(path),))
        before.update(state(recorded_env))
        before["volumes"][1]["import_path"] = str(
            path
        )  # Uncommitted current-writer winner.
        return owners

    monkeypatch.setattr(rescan, "_recorded_volume_path_owners", changed)
    result = rescan.rescan_series_folder(1)
    assert result["missing"] == 0 and result["found"] == 0
    assert state(recorded_env) == before


def test_owner_change_between_eligible_rows_invalidates_writer_lookup(
    recorded_env, monkeypatch
):
    import rescan

    recorded_env["recorded"].unlink()
    second = str(recorded_env["library"] / "State Series Book 02.cbz")
    with sqlite3.connect(recorded_env["db_path"]) as db:
        db.execute(
            "INSERT INTO volumes(id,series_id,volume_num,status,import_path,quality) VALUES(12,1,2,'downloaded',?,'cbz')",
            (second,),
        )
    real_reset = rescan._reset_missing_volume

    def changed(db, row):
        reset = real_reset(db, row)
        if reset and row["id"] == 11:
            db.execute(
                "INSERT INTO volumes(id,series_id,volume_num,status,import_path) VALUES(13,1,3,'wanted',?)",
                (second,),
            )
        return reset

    queries = traced_owner_lookups(monkeypatch)
    monkeypatch.setattr(rescan, "_reset_missing_volume", changed)
    result = rescan.rescan_series_folder(1)
    assert result["missing"] == 1
    assert rows(recorded_env, "volumes")[1]["status"] == "downloaded"
    assert len(rows(recorded_env, "history")) == 1
    assert len(queries) == 1


@pytest.mark.parametrize("owner_series", [1, 2])
@pytest.mark.parametrize("alias", [False, True])
@pytest.mark.parametrize("safe", [None, "loose", "unparsed"])
def test_late_recorded_carrier_owner_cannot_confirm_complete_pack(
    recorded_env, monkeypatch, owner_series, alias, safe
):
    import rescan

    path = recorded_env["recorded"]
    content = path.read_bytes()
    if safe:
        archive(
            recorded_env["library"]
            / (
                "State Series Ch099.cbz"
                if safe == "loose"
                else "State Series Extra.cbz"
            )
        )
    with sqlite3.connect(recorded_env["db_path"]) as db:
        db.execute(
            "INSERT INTO volumes(id,series_id,volume_num,status,quality) VALUES(12,1,3,'wanted','cbz')"
        )
        db.execute(
            "INSERT INTO volumes(id,series_id,pack_type,status,quality) VALUES(13,1,'complete','grabbed','cbz')"
        )
        db.execute(
            "INSERT INTO chapters(id,series_id,volume_id,chapter_num,status,monitored,quality) VALUES(7,1,12,7,'wanted',1,'cbz')"
        )
        if owner_series == 2:
            db.execute(
                "INSERT INTO series(id,title,search_pattern) VALUES(2,'Other','Other')"
            )
    before = {}
    real_build = rescan.build_filesystem_inventory

    def changed(snapshot):
        inventory = real_build(snapshot)
        proof = inventory.recorded_volumes[11]
        assert proof.file is not None  # A genuine PRESENT carrier before the DB change.
        owner_path = f"{path.parent}/./{path.name.lower()}" if alias else str(path)
        with sqlite3.connect(recorded_env["db_path"]) as db:
            db.execute(
                "INSERT INTO volumes(id,series_id,volume_num,status,import_path,quality) VALUES(14,?,4,'downloaded',?,'cbz')",
                (owner_series, owner_path),
            )
        before.update(state(recorded_env))
        return inventory

    monkeypatch.setattr(rescan, "build_filesystem_inventory", changed)
    queries = traced_owner_lookups(monkeypatch)
    result = rescan.rescan_series_folder(1)
    assert len(queries) == 1
    if safe:
        assert result["recovered"] == 1
        assert {row["id"]: row["status"] for row in rows(recorded_env, "volumes")} == {
            11: "downloaded",
            12: "downloaded",
            13: "downloaded",
            14: "downloaded",
        }
        assert rows(recorded_env, "chapters")[0]["status"] == "downloaded"
        for table in (
            "series",
            "history",
            "series_metadata_fields",
            "series_metadata_sources",
        ):
            assert rows(recorded_env, table) == before[table]
        assert path.read_bytes() == content
        return
    assert result["recovered"] == 0, {
        "result": result,
        "volume_statuses": {
            row["id"]: row["status"] for row in rows(recorded_env, "volumes")
        },
        "chapter_statuses": {
            row["id"]: row["status"] for row in rows(recorded_env, "chapters")
        },
    }
    assert result["found"] == result["created"] == result["missing"] == 0
    assert state(recorded_env) == before
    assert path.read_bytes() == content


@pytest.mark.parametrize("problem", ["unknown", "changed_pack"])
def test_unusable_complete_pack_does_not_request_global_owners(
    recorded_env, monkeypatch, problem
):
    import rescan

    with sqlite3.connect(recorded_env["db_path"]) as db:
        db.execute(
            "INSERT INTO volumes(id,series_id,pack_type,status,quality) VALUES(13,1,'complete','grabbed','cbz')"
        )
    if problem == "unknown":
        recorded_env["recorded"].write_bytes(b"")
    before = state(recorded_env)
    real_build = rescan.build_filesystem_inventory

    def changed(snapshot):
        inventory = real_build(snapshot)
        if problem == "changed_pack":
            with sqlite3.connect(recorded_env["db_path"]) as db:
                db.execute("UPDATE volumes SET quality='pdf' WHERE id=13")
            before.update(state(recorded_env))
        return inventory

    queries = traced_owner_lookups(monkeypatch)
    monkeypatch.setattr(rescan, "build_filesystem_inventory", changed)
    assert_deferred(recorded_env, before)
    assert queries == []


def test_present_carrier_two_complete_packs_reuse_fresh_writer_batch(
    recorded_env, monkeypatch
):
    import rescan

    with sqlite3.connect(recorded_env["db_path"]) as db:
        db.execute(
            "INSERT INTO volumes(id,series_id,volume_num,status,quality) VALUES(12,1,3,'wanted','cbz')"
        )
        for pack in (13, 14):
            db.execute(
                "INSERT INTO volumes(id,series_id,pack_type,status,quality) VALUES(?,1,'complete','grabbed','cbz')",
                (pack,),
            )
        db.execute(
            "INSERT INTO chapters(id,series_id,volume_id,chapter_num,status,monitored,quality) VALUES(7,1,12,7,'wanted',1,'cbz')"
        )
    queries = traced_owner_lookups(monkeypatch)
    result = rescan.rescan_series_folder(1)
    assert result["recovered"] == 2
    assert result["found"] == result["created"] == result["missing"] == 0
    assert {row["status"] for row in rows(recorded_env, "volumes")} == {"downloaded"}
    assert rows(recorded_env, "chapters")[0]["status"] == "downloaded"
    assert len(queries) == 1


@pytest.mark.parametrize("changed_literal", [False, True])
def test_trigger_literal_credit_cannot_mask_equal_count_owner_write(
    recorded_env, monkeypatch, changed_literal
):
    import rescan

    with sqlite3.connect(recorded_env["db_path"]) as db:
        db.execute(
            "INSERT INTO volumes(id,series_id,volume_num,status,quality) VALUES(12,1,3,'wanted','cbz')"
        )
        for pack in (13, 14):
            db.execute(
                "INSERT INTO volumes(id,series_id,pack_type,status,quality) VALUES(?,1,'complete','grabbed','cbz')",
                (pack,),
            )
        db.execute(
            "INSERT INTO volumes(id,series_id,volume_num,status,import_path,quality) VALUES(15,1,4,'downloaded','/private-other.cbz','cbz')"
        )
        db.execute(
            "INSERT INTO chapters(id,series_id,volume_id,chapter_num,status,monitored,quality) VALUES(7,1,12,7,'wanted',1,'cbz')"
        )
        if changed_literal:
            sql = db.execute(
                "SELECT sql FROM sqlite_master WHERE name='trg_vol_downloaded_cascade'"
            ).fetchone()[0]
            assert "NEW.status = 'downloaded'" in sql
            db.execute("DROP TRIGGER trg_vol_downloaded_cascade")
            db.execute(sql.replace("'downloaded'", "'DOWNLOADED'", 1))
        db.execute("""CREATE TRIGGER unexpected_carrier_owner AFTER UPDATE OF status ON volumes
            WHEN NEW.id=12 AND NEW.status='downloaded' AND OLD.status != 'downloaded'
            BEGIN UPDATE volumes SET import_path=(SELECT import_path FROM volumes WHERE id=11)
            WHERE id=15; END""")
    queries = traced_owner_lookups(monkeypatch)
    result = rescan.rescan_series_folder(1)
    volumes = {row["id"]: row for row in rows(recorded_env, "volumes")}
    assert volumes[15]["import_path"] == str(recorded_env["recorded"])
    assert volumes[13]["status"] == "downloaded"
    assert volumes[14]["status"] == "grabbed", {
        "result": result,
        "second_pack": volumes[14]["status"],
    }
    assert result["recovered"] == 1
    assert result["found"] == result["created"] == result["missing"] == 0
    assert len(queries) == 1


@pytest.mark.parametrize("catalog", ["main", "temp"])
def test_trigger_catalog_interaction_cannot_replace_canonical_status_credit(
    recorded_env, monkeypatch, catalog
):
    import rescan

    with sqlite3.connect(recorded_env["db_path"]) as db:
        canonical = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='trg_vol_downloaded_cascade'"
        ).fetchone()[0]
        db.execute(
            "INSERT INTO volumes(id,series_id,volume_num,status,quality) VALUES(12,1,3,'wanted','cbz')"
        )
        for pack in (13, 14):
            db.execute(
                "INSERT INTO volumes(id,series_id,pack_type,status,quality) VALUES(?,1,'complete','grabbed','cbz')",
                (pack,),
            )
        db.execute(
            "INSERT INTO chapters(id,series_id,volume_id,chapter_num,status,monitored,quality) VALUES(7,1,12,7,'wanted',1,'cbz')"
        )
    real_reconcile = rescan.reconcile_series_inventory

    def interacting_trigger(db, snapshot, inventory):
        temp = "TEMP " if catalog == "temp" else ""
        table = 'main."VoLuMeS"' if catalog == "temp" else '"VoLuMeS"'
        db.execute(f"""CREATE {temp}TRIGGER before_carrier_owner BEFORE UPDATE OF status ON {table}
            WHEN NEW.status='downloaded' AND NEW.id=12
            BEGIN UPDATE chapters SET status='downloaded',
                import_path=(SELECT import_path FROM volumes WHERE id=11) WHERE id=7; END""")
        schema = "sqlite_temp_master" if catalog == "temp" else "sqlite_master"
        assert db.execute(
            f"SELECT 1 FROM {schema} WHERE type='trigger' AND name='before_carrier_owner' AND tbl_name='volumes' COLLATE NOCASE"
        ).fetchone()
        assert (
            db.execute(
                "SELECT sql FROM sqlite_master WHERE name='trg_vol_downloaded_cascade'"
            ).fetchone()[0]
            == canonical
        )
        return real_reconcile(db, snapshot, inventory)

    queries = traced_owner_lookups(monkeypatch)
    monkeypatch.setattr(rescan, "reconcile_series_inventory", interacting_trigger)
    result = rescan.rescan_series_folder(1)
    chapter = rows(recorded_env, "chapters")[0]
    assert chapter["status"] == "downloaded" and chapter["volume_id"] == 12
    assert chapter["import_path"] == str(recorded_env["recorded"])
    volumes = {row["id"]: row for row in rows(recorded_env, "volumes")}
    assert volumes[13]["status"] == "downloaded"
    assert volumes[14]["status"] == "grabbed", {
        "result": result,
        "second_pack": volumes[14]["status"],
    }
    assert result["recovered"] == 1
    assert result["found"] == result["created"] == result["missing"] == 0
    assert len(queries) == 1


@pytest.mark.parametrize("owner_series", [1, 2])
@pytest.mark.parametrize("safe", [False, True])
def test_late_recorded_carrier_owner_cannot_confirm_range_pack(
    recorded_env, monkeypatch, owner_series, safe
):
    import rescan

    path = recorded_env["recorded"].with_name("State Series v01.cbz")
    recorded_env["recorded"].rename(path)
    content = path.read_bytes()
    if safe:
        archive(path.with_name("State Series v02.cbz"))
    with sqlite3.connect(recorded_env["db_path"]) as db:
        db.execute("UPDATE volumes SET import_path=? WHERE id=11", (str(path),))
        db.execute(
            "INSERT INTO volumes(id,series_id,volume_num,status,quality) VALUES(12,1,3,'wanted','cbz')"
        )
        db.execute(
            "INSERT INTO volumes(id,series_id,pack_type,vol_range_start,vol_range_end,status,quality) VALUES(13,1,'volume',1,3,'grabbed','cbz')"
        )
        db.execute(
            "INSERT INTO chapters(id,series_id,volume_id,chapter_num,status,monitored,quality) VALUES(7,1,12,7,'wanted',1,'cbz')"
        )
        if owner_series == 2:
            db.execute(
                "INSERT INTO series(id,title,search_pattern) VALUES(2,'Other','Other')"
            )
    before = {}
    real_build = rescan.build_filesystem_inventory

    def changed(snapshot):
        inventory = real_build(snapshot)
        assert inventory.recorded_volumes[11].file is not None
        assert 1 in inventory.on_disk
        owner_path = (
            str(path) if owner_series == 1 else f"{path.parent}/./{path.name.lower()}"
        )
        with sqlite3.connect(recorded_env["db_path"]) as db:
            db.execute(
                "INSERT INTO volumes(id,series_id,volume_num,status,import_path,quality) VALUES(14,?,4,'downloaded',?,'cbz')",
                (owner_series, owner_path),
            )
        before.update(state(recorded_env))
        return inventory

    monkeypatch.setattr(rescan, "build_filesystem_inventory", changed)
    result = rescan.rescan_series_folder(1)
    if safe:
        assert (
            result["recovered"] == 1 and result["found"] == 1 and result["created"] == 1
        )
        assert rows(recorded_env, "chapters")[0]["status"] == "downloaded"
        assert (
            next(row for row in rows(recorded_env, "volumes") if row["id"] == 13)[
                "status"
            ]
            == "downloaded"
        )
    else:
        assert result["recovered"] == 0, {
            "result": result,
            "volumes": {
                row["id"]: row["status"] for row in rows(recorded_env, "volumes")
            },
            "chapters": {
                row["id"]: row["status"] for row in rows(recorded_env, "chapters")
            },
        }
        assert result["found"] == result["created"] == result["missing"] == 0
        assert state(recorded_env) == before
    assert path.read_bytes() == content
