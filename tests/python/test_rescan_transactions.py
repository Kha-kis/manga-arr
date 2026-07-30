"""Transaction, race, and event-loop invariants for library rescans."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
import zipfile
from contextlib import contextmanager
from typing import Any

import pytest
from starlette.requests import Request


@pytest.fixture
def rescan_env(tmp_path, monkeypatch):
    import main
    import rescan
    import shared

    db_path = tmp_path / "mangarr.db"
    library_root = tmp_path / "library"
    series_dir = library_root / "Race Manga"
    series_dir.mkdir(parents=True)

    original_main_config = dict(main.CONFIG)
    original_shared_config = dict(shared.CONFIG)
    main.CONFIG.clear()
    shared.CONFIG.clear()
    main.CONFIG["folder_format"] = ""
    shared.CONFIG["folder_format"] = ""
    try:
        monkeypatch.setattr(main, "DB_PATH", str(db_path))
        monkeypatch.setattr(shared, "DB_PATH", str(db_path))
        main.init_db()
        with sqlite3.connect(db_path) as db:
            db.execute(
                "INSERT INTO root_folders(id,path,label,is_default)"
                " VALUES(1,?,'Library',1)",
                (str(library_root),),
            )
            db.execute(
                "INSERT INTO series(id,title,search_pattern,root_folder_id,"
                " folder_name,monitor_mode,monitored)"
                " VALUES(7,'Race Manga','Race Manga',1,'Race Manga','missing',1)"
            )
        yield {
            "db_path": str(db_path),
            "library_root": library_root,
            "series_dir": series_dir,
        }
    finally:
        main.CONFIG.clear()
        main.CONFIG.update(original_main_config)
        shared.CONFIG.clear()
        shared.CONFIG.update(original_shared_config)


def _insert_volume(
    db_path: str,
    volume_num: float,
    status: str,
    **values: Any,
) -> int:
    columns = ["series_id", "volume_num", "status", *values]
    params = [7, volume_num, status, *values.values()]
    placeholders = ",".join("?" for _ in params)
    with sqlite3.connect(db_path) as db:
        cursor = db.execute(
            f"INSERT INTO volumes({','.join(columns)}) VALUES({placeholders})",
            params,
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid


def test_filesystem_inventory_does_not_block_unrelated_writer(rescan_env, monkeypatch):
    import rescan

    _insert_volume(rescan_env["db_path"], 1.0, "wanted")
    volume_path = rescan_env["series_dir"] / "Race Manga v01.cbz"
    volume_path.write_bytes(b"not-a-real-archive")

    entered = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []
    real_walk = os.walk

    def pause() -> None:
        entered.set()
        if not release.wait(timeout=10):
            raise TimeoutError("inventory checkpoint was not released")

    def paused_walk(path):
        pause()
        yield from real_walk(path)

    monkeypatch.setattr(rescan.os, "walk", paused_walk)
    monkeypatch.setattr(rescan, "quality_from_filename", lambda path: "cbz")

    def run_rescan() -> None:
        try:
            rescan.rescan_series_folder(7)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run_rescan)
    thread.start()
    assert entered.wait(timeout=10), "rescan never reached inventory"

    started = time.monotonic()
    try:
        with sqlite3.connect(rescan_env["db_path"], timeout=1.0) as db:
            db.execute(
                "INSERT INTO events(event_type,series_id,message)"
                " VALUES('probe',7,'writer completed')"
            )
    finally:
        release.set()
        thread.join(timeout=10)
    elapsed = time.monotonic() - started

    assert not thread.is_alive()
    assert not errors
    assert elapsed < 0.75
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='probe'"
            ).fetchone()[0]
            == 1
        )


def test_no_filesystem_helper_runs_during_rescan_write_transaction(
    rescan_env, monkeypatch
):
    import rescan
    import shared

    _insert_volume(rescan_env["db_path"], 1.0, "wanted")
    _insert_volume(
        rescan_env["db_path"],
        2.0,
        "downloaded",
        import_path=str(rescan_env["series_dir"] / "missing-v02.cbz"),
    )
    with sqlite3.connect(rescan_env["db_path"]) as db:
        db.execute(
            "INSERT INTO volumes(series_id,volume_num,status,pack_type,import_path)"
            " VALUES(7,NULL,'downloaded','complete',?)",
            (str(rescan_env["series_dir"] / "missing-pack.cbz"),),
        )
    (rescan_env["series_dir"] / "Race Manga v01.cbz").write_bytes(b"archive")

    active: list[sqlite3.Connection] = []
    observations: list[str] = []
    real_get_db = shared.get_db
    real_walk = os.walk
    real_isdir = os.path.isdir
    real_exists = os.path.exists
    real_stat = os.stat
    real_inject = rescan.inject_comicinfo

    @contextmanager
    def tracked_get_db():
        with real_get_db() as db:
            active.append(db)
            try:
                yield db
            finally:
                active.remove(db)

    def assert_no_write(name: str) -> None:
        observations.append(name)
        assert not any(db.in_transaction for db in active), name

    def checked_walk(path):
        assert_no_write("walk")
        yield from real_walk(path)

    def checked_isdir(path):
        assert_no_write("isdir")
        return real_isdir(path)

    def checked_exists(path):
        assert_no_write("exists")
        return real_exists(path)

    def checked_stat(path, *args, **kwargs):
        assert_no_write("stat")
        return real_stat(path, *args, **kwargs)

    def checked_quality(path):
        assert_no_write("quality")
        return "cbz"

    def checked_inject(path, xml):
        assert_no_write("inject")
        return real_inject(path, xml)

    monkeypatch.setattr(rescan, "get_db", tracked_get_db)
    monkeypatch.setattr(rescan.os, "walk", checked_walk)
    monkeypatch.setattr(rescan.os.path, "isdir", checked_isdir)
    monkeypatch.setattr(rescan.os.path, "exists", checked_exists)
    monkeypatch.setattr(rescan.os, "stat", checked_stat)
    monkeypatch.setattr(rescan, "quality_from_filename", checked_quality)
    monkeypatch.setattr(rescan, "inject_comicinfo", checked_inject)

    result = rescan.rescan_series_folder(7)

    assert result == {
        "found": 1,
        "recovered": 1,
        "missing": 2,
        "lost": 0,
        "created": 0,
    }
    assert {"walk", "isdir", "exists", "stat", "quality", "inject"} <= set(
        observations
    )
    assert not active


def test_stat_failure_does_not_confirm_volume_or_complete_pack(
    rescan_env, monkeypatch
):
    import rescan
    import shared

    _insert_volume(rescan_env["db_path"], 1.0, "wanted")
    with sqlite3.connect(rescan_env["db_path"]) as db:
        pack_id = db.execute(
            "INSERT INTO volumes(series_id,volume_num,status,pack_type)"
            " VALUES(7,NULL,'grabbed','complete')"
        ).lastrowid
    assert pack_id is not None
    volume_path = rescan_env["series_dir"] / "Race Manga v01.cbz"
    volume_path.write_bytes(b"disappearing")
    real_stat = os.stat

    def disappearing_stat(path, *args, **kwargs):
        if os.fspath(path) == str(volume_path):
            raise FileNotFoundError(path)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(rescan.os, "stat", disappearing_stat)
    with shared.get_db() as db:
        snapshot = rescan.snapshot_series_rescan(db, 7)
    assert snapshot is not None

    inventory = rescan.build_filesystem_inventory(snapshot)
    with shared.get_db() as db:
        reconciliation = rescan.reconcile_series_inventory(db, snapshot, inventory)

    assert inventory.on_disk == frozenset()
    assert inventory.any_library_files is False
    assert reconciliation.result["recovered"] == 0
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert db.execute(
            "SELECT status FROM volumes WHERE id=?", (pack_id,)
        ).fetchone()[0] == "grabbed"


def test_downloaded_pack_quality_is_backfilled(rescan_env):
    import rescan

    pack_path = rescan_env["series_dir"] / "Race Manga Complete.cbz"
    pack_path.write_bytes(b"archive")
    with sqlite3.connect(rescan_env["db_path"]) as db:
        pack_id = db.execute(
            "INSERT INTO volumes(series_id,volume_num,status,pack_type,"
            " import_path,quality) VALUES(7,NULL,'downloaded','complete',?,NULL)",
            (str(pack_path),),
        ).lastrowid
    assert pack_id is not None

    result = rescan.rescan_series_folder(7)

    assert result["missing"] == 0
    with sqlite3.connect(rescan_env["db_path"]) as db:
        quality = db.execute(
            "SELECT quality FROM volumes WHERE id=?", (pack_id,)
        ).fetchone()[0]
    assert quality == "cbz"


def test_rescan_walks_series_directory_once(rescan_env, monkeypatch):
    import rescan

    _insert_volume(rescan_env["db_path"], 1.0, "wanted")
    for volume_num in (1, 2, 3):
        (rescan_env["series_dir"] / f"Race Manga v{volume_num:02}.cbz").write_bytes(
            b"archive"
        )

    walk_count = 0
    real_walk = os.walk

    def counted_walk(path):
        nonlocal walk_count
        walk_count += 1
        yield from real_walk(path)

    monkeypatch.setattr(rescan.os, "walk", counted_walk)
    monkeypatch.setattr(rescan, "quality_from_filename", lambda path: "cbz")

    result = rescan.rescan_series_folder(7)

    assert walk_count == 1
    assert result["found"] == 3
    assert result["recovered"] == 1
    assert result["created"] == 2


def test_missing_reset_cas_preserves_concurrent_status_and_skips_cascade(
    rescan_env,
):
    import rescan
    import shared

    volume_id = _insert_volume(
        rescan_env["db_path"],
        1.0,
        "downloaded",
        import_path="/old/import.cbz",
        torrent_name="old release",
    )
    with sqlite3.connect(rescan_env["db_path"]) as db:
        db.execute(
            "INSERT INTO chapters(series_id,volume_id,chapter_num,status,monitored)"
            " VALUES(7,?,1,'downloaded',1)",
            (volume_id,),
        )

    with shared.get_db() as db:
        snapshot = rescan.snapshot_series_rescan(db, 7)
    assert snapshot is not None
    inventory = rescan.build_filesystem_inventory(snapshot)
    with sqlite3.connect(rescan_env["db_path"]) as db:
        db.execute(
            "UPDATE volumes SET status='grabbed',import_path='/concurrent/import.cbz',"
            " torrent_name='concurrent release' WHERE id=?",
            (volume_id,),
        )
    with shared.get_db() as db:
        reconciliation = rescan.reconcile_series_inventory(db, snapshot, inventory)

    assert reconciliation.result["missing"] == 0
    with sqlite3.connect(rescan_env["db_path"]) as db:
        db.row_factory = sqlite3.Row
        volume = db.execute(
            "SELECT status,import_path,torrent_name FROM volumes WHERE id=?",
            (volume_id,),
        ).fetchone()
        chapter_status = db.execute(
            "SELECT status FROM chapters WHERE volume_id=?", (volume_id,)
        ).fetchone()[0]
        history_count = db.execute(
            "SELECT COUNT(*) FROM history WHERE event_type='file_deleted'"
        ).fetchone()[0]
    assert dict(volume) == {
        "status": "grabbed",
        "import_path": "/concurrent/import.cbz",
        "torrent_name": "concurrent release",
    }
    assert chapter_status == "downloaded"
    assert history_count == 0


def test_missing_reset_cas_preserves_concurrent_owner_change(rescan_env):
    """An owner-only reassignment invalidates the parent and chapter snapshot."""
    import rescan
    import shared

    volume_id = _insert_volume(
        rescan_env["db_path"],
        1.0,
        "downloaded",
        import_path="/old/import.cbz",
        download_id="owned-download",
        download_client_id=101,
    )
    with sqlite3.connect(rescan_env["db_path"]) as db:
        db.execute(
            "INSERT INTO chapters("
            "series_id,volume_id,chapter_num,status,monitored,download_id,"
            "download_client_id"
            ") VALUES(7,?,1,'downloaded',1,'owned-download',101)",
            (volume_id,),
        )

    with shared.get_db() as db:
        snapshot = rescan.snapshot_series_rescan(db, 7)
    assert snapshot is not None
    inventory = rescan.build_filesystem_inventory(snapshot)
    with sqlite3.connect(rescan_env["db_path"]) as db:
        db.execute(
            "UPDATE volumes SET download_client_id=102 WHERE id=?",
            (volume_id,),
        )

    with shared.get_db() as db:
        reconciliation = rescan.reconcile_series_inventory(db, snapshot, inventory)

    assert reconciliation.result["missing"] == 0
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert db.execute(
            "SELECT status,import_path,download_id,download_client_id"
            " FROM volumes WHERE id=?",
            (volume_id,),
        ).fetchone() == (
            "downloaded",
            "/old/import.cbz",
            "owned-download",
            102,
        )
        assert db.execute(
            "SELECT status,download_id,download_client_id FROM chapters"
            " WHERE volume_id=?",
            (volume_id,),
        ).fetchone() == ("downloaded", "owned-download", 101)
        assert db.execute(
            "SELECT COUNT(*) FROM history WHERE event_type='file_deleted'"
        ).fetchone() == (0,)


def test_missing_reset_clears_owner_and_records_original_owner(rescan_env):
    """A won reset clears acquisition ownership from parent and children."""
    import rescan

    volume_id = _insert_volume(
        rescan_env["db_path"],
        1.0,
        "downloaded",
        import_path="/missing/import.cbz",
        download_id="owned-download",
        download_client_id=101,
        torrent_name="Owned release",
    )
    with sqlite3.connect(rescan_env["db_path"]) as db:
        db.execute(
            "INSERT INTO chapters("
            "series_id,volume_id,chapter_num,status,monitored,download_id,"
            "download_client_id"
            ") VALUES(7,?,1,'downloaded',1,'owned-download',101)",
            (volume_id,),
        )

    result = rescan.rescan_series_folder(7)

    assert result["missing"] == 1
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert db.execute(
            "SELECT status,download_id,download_client_id FROM volumes"
            " WHERE id=?",
            (volume_id,),
        ).fetchone() == ("wanted", None, None)
        assert db.execute(
            "SELECT status,download_id,download_client_id FROM chapters"
            " WHERE volume_id=?",
            (volume_id,),
        ).fetchone() == ("wanted", None, None)
        history_data = db.execute(
            "SELECT data FROM history WHERE event_type='file_deleted'"
        ).fetchone()[0]
    assert json.loads(history_data) == {"download_client_id": 101}


def test_recovery_cas_preserves_concurrent_import(rescan_env, monkeypatch):
    import rescan
    import shared

    volume_id = _insert_volume(rescan_env["db_path"], 1.0, "wanted")
    (rescan_env["series_dir"] / "Race Manga v01.cbz").write_bytes(b"archive")
    monkeypatch.setattr(rescan, "quality_from_filename", lambda path: "cbz")

    with shared.get_db() as db:
        snapshot = rescan.snapshot_series_rescan(db, 7)
    assert snapshot is not None
    inventory = rescan.build_filesystem_inventory(snapshot)
    with sqlite3.connect(rescan_env["db_path"]) as db:
        db.execute(
            "UPDATE volumes SET status='downloaded',"
            " import_path='/concurrent/import.cbz',size_bytes=999 WHERE id=?",
            (volume_id,),
        )
    with shared.get_db() as db:
        reconciliation = rescan.reconcile_series_inventory(db, snapshot, inventory)

    assert reconciliation.result["recovered"] == 0
    with sqlite3.connect(rescan_env["db_path"]) as db:
        row = db.execute(
            "SELECT status,import_path,size_bytes,quality FROM volumes WHERE id=?",
            (volume_id,),
        ).fetchone()
    assert row == ("downloaded", "/concurrent/import.cbz", 999, None)


def test_recovery_cas_loser_never_converts_winners_cbr(rescan_env):
    import rescan
    import shared

    volume_id = _insert_volume(rescan_env["db_path"], 1.0, "wanted")
    cbr_path = rescan_env["series_dir"] / "Race Manga v01.cbr"
    original_bytes = b"winner-owned-cbr"
    cbr_path.write_bytes(original_bytes)

    with shared.get_db() as db:
        snapshot = rescan.snapshot_series_rescan(db, 7)
    assert snapshot is not None
    inventory = rescan.build_filesystem_inventory(snapshot)
    with sqlite3.connect(rescan_env["db_path"]) as db:
        db.execute(
            "UPDATE volumes SET status='downloaded',import_path=? WHERE id=?",
            (str(cbr_path), volume_id),
        )
    with shared.get_db() as db:
        reconciliation = rescan.reconcile_series_inventory(db, snapshot, inventory)
    rescan.enrich_reconciled_files(reconciliation)

    assert reconciliation.result["recovered"] == 0
    assert cbr_path.is_file()
    assert cbr_path.read_bytes() == original_bytes
    assert not cbr_path.with_suffix(".cbz").exists()
    with sqlite3.connect(rescan_env["db_path"]) as db:
        row = db.execute(
            "SELECT status,import_path FROM volumes WHERE id=?", (volume_id,)
        ).fetchone()
    assert row == ("downloaded", str(cbr_path))


@pytest.mark.parametrize("existing_stub", [True, False])
def test_successful_recovery_and_discovery_convert_and_inject_comicinfo(
    rescan_env, monkeypatch, existing_stub
):
    import rescan
    import shared

    if existing_stub:
        _insert_volume(rescan_env["db_path"], 1.0, "wanted")
    cbr_path = rescan_env["series_dir"] / "Race Manga v01.cbr"
    cbr_path.write_bytes(b"rar-source")

    def fake_convert(staged_path):
        converted = os.path.splitext(staged_path)[0] + ".cbz"
        with zipfile.ZipFile(converted, "w") as archive:
            archive.writestr("001.jpg", b"page")
        return converted

    def hardlink_must_not_run(*args, **kwargs):
        raise AssertionError("enrichment must not depend on hard links")

    monkeypatch.setattr(rescan, "detect_file_type_magic", lambda path: "cbr")
    monkeypatch.setattr(rescan, "convert_cbr_to_cbz", fake_convert)
    monkeypatch.setattr(rescan.os, "link", hardlink_must_not_run)

    result = rescan.rescan_series_folder(7)

    assert result["recovered" if existing_stub else "created"] == 1
    cbz_path = cbr_path.with_suffix(".cbz")
    assert cbz_path.is_file()
    assert not cbr_path.exists()
    with zipfile.ZipFile(cbz_path) as archive:
        comicinfo = archive.read("ComicInfo.xml").decode()
    assert "<Series>Race Manga</Series>" in comicinfo
    assert "<Volume>1</Volume>" in comicinfo
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert db.execute(
            "SELECT import_path FROM volumes"
            " WHERE series_id=7 AND volume_num=1"
        ).fetchone()[0] == str(cbz_path)
    cbr_path.write_bytes(b"stale-cbr")

    with shared.get_db() as db:
        snapshot = rescan.snapshot_series_rescan(db, 7)
    assert snapshot is not None
    inventory = rescan.build_filesystem_inventory(snapshot)
    assert inventory.files_by_volume[1.0].path == str(cbz_path)


def test_enrichment_does_not_overwrite_replacement_after_final_validation(
    rescan_env, monkeypatch
):
    import rescan

    source_path = rescan_env["series_dir"] / "Race Manga v01.cbz"
    with zipfile.ZipFile(source_path, "w") as archive:
        archive.writestr("001.jpg", b"original")
    _insert_volume(rescan_env["db_path"], 1.0, "wanted")
    replacement = rescan_env["series_dir"] / "winner.tmp"
    winner_bytes = b"filesystem-winner"
    replacement.write_bytes(winner_bytes)
    real_claim = rescan._claim_exact_path
    replaced = False

    def replace_then_claim(path, fingerprint):
        nonlocal replaced
        if path == str(source_path) and not replaced:
            replaced = True
            os.replace(replacement, source_path)
        return real_claim(path, fingerprint)

    monkeypatch.setattr(rescan, "_claim_exact_path", replace_then_claim)

    result = rescan.rescan_series_folder(7)

    assert result["recovered"] == 1
    assert replaced
    assert source_path.read_bytes() == winner_bytes
    assert not list(rescan_env["series_dir"].glob(".mangarr-claim-*"))


@pytest.mark.parametrize("cas_outcome", ["lost", "exception"])
def test_cbr_cas_failure_compensates_publication(
    rescan_env, monkeypatch, cas_outcome
):
    import rescan

    volume_id = _insert_volume(rescan_env["db_path"], 1.0, "wanted")
    cbr_path = rescan_env["series_dir"] / "Race Manga v01.cbr"
    original_bytes = b"rar-source"
    cbr_path.write_bytes(original_bytes)

    def fake_convert(staged_path):
        converted = os.path.splitext(staged_path)[0] + ".cbz"
        with zipfile.ZipFile(converted, "w") as archive:
            archive.writestr("001.jpg", b"page")
        return converted

    def cas_result(*args, **kwargs):
        if cas_outcome == "exception":
            raise RuntimeError("injected CAS failure")
        return False

    monkeypatch.setattr(rescan, "detect_file_type_magic", lambda path: "cbr")
    monkeypatch.setattr(rescan, "convert_cbr_to_cbz", fake_convert)
    monkeypatch.setattr(rescan, "_cas_converted_volume", cas_result)

    result = rescan.rescan_series_folder(7)

    assert result["recovered"] == 1
    assert cbr_path.read_bytes() == original_bytes
    assert not cbr_path.with_suffix(".cbz").exists()
    assert not list(rescan_env["series_dir"].glob(".mangarr-claim-*"))
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert db.execute(
            "SELECT status,import_path FROM volumes WHERE id=?",
            (volume_id,),
        ).fetchone() == ("downloaded", str(cbr_path))


def test_systemic_noreplace_unavailability_skips_before_source_claim(
    rescan_env, monkeypatch
):
    import rescan

    volume_id = _insert_volume(rescan_env["db_path"], 1.0, "wanted")
    cbr_path = rescan_env["series_dir"] / "Race Manga v01.cbr"
    original_bytes = b"rar-source"
    cbr_path.write_bytes(original_bytes)
    calls = 0

    def unavailable(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise OSError("renameat2 unavailable")

    monkeypatch.setattr(rescan, "_rename_noreplace", unavailable)

    result = rescan.rescan_series_folder(7)

    assert result["recovered"] == 1
    assert calls == 1
    assert cbr_path.read_bytes() == original_bytes
    assert not cbr_path.with_suffix(".cbz").exists()
    assert not list(rescan_env["series_dir"].glob(".mangarr-claim-*"))
    assert not list(
        rescan_env["series_dir"].glob(".mangarr-noreplace-probe-*")
    )
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert db.execute(
            "SELECT status,import_path FROM volumes WHERE id=?",
            (volume_id,),
        ).fetchone() == ("downloaded", str(cbr_path))


def test_publication_primitive_failure_restores_source(rescan_env, monkeypatch):
    import rescan

    volume_id = _insert_volume(rescan_env["db_path"], 1.0, "wanted")
    cbr_path = rescan_env["series_dir"] / "Race Manga v01.cbr"
    original_bytes = b"rar-source"
    cbr_path.write_bytes(original_bytes)

    def fake_convert(staged_path):
        converted = os.path.splitext(staged_path)[0] + ".cbz"
        with zipfile.ZipFile(converted, "w") as archive:
            archive.writestr("001.jpg", b"page")
        return converted

    real_rename_noreplace = rescan._rename_noreplace

    def fail_staged_publication(source, destination):
        if ".mangarr-rescan-" in source:
            return False
        return real_rename_noreplace(source, destination)

    monkeypatch.setattr(rescan, "detect_file_type_magic", lambda path: "cbr")
    monkeypatch.setattr(rescan, "convert_cbr_to_cbz", fake_convert)
    monkeypatch.setattr(rescan, "_rename_noreplace", fail_staged_publication)

    result = rescan.rescan_series_folder(7)

    assert result["recovered"] == 1
    assert cbr_path.read_bytes() == original_bytes
    assert not cbr_path.with_suffix(".cbz").exists()
    assert not list(rescan_env["series_dir"].glob(".mangarr-claim-*"))
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert db.execute(
            "SELECT status,import_path FROM volumes WHERE id=?",
            (volume_id,),
        ).fetchone() == ("downloaded", str(cbr_path))


def test_artifact_cleanup_exception_still_restores_source(rescan_env, monkeypatch):
    import rescan

    volume_id = _insert_volume(rescan_env["db_path"], 1.0, "wanted")
    cbr_path = rescan_env["series_dir"] / "Race Manga v01.cbr"
    original_bytes = b"rar-source"
    cbr_path.write_bytes(original_bytes)

    def fake_convert(staged_path):
        converted = os.path.splitext(staged_path)[0] + ".cbz"
        with zipfile.ZipFile(converted, "w") as archive:
            archive.writestr("001.jpg", b"page")
        return converted

    real_restore = rescan._restore_claim
    restoration_attempted = False

    def track_restore(claim):
        nonlocal restoration_attempted
        restoration_attempted = True
        return real_restore(claim)

    def cleanup_failure(*args, **kwargs):
        raise RuntimeError("injected artifact cleanup failure")

    monkeypatch.setattr(rescan, "detect_file_type_magic", lambda path: "cbr")
    monkeypatch.setattr(rescan, "convert_cbr_to_cbz", fake_convert)
    monkeypatch.setattr(rescan, "_cas_converted_volume", lambda *args: False)
    monkeypatch.setattr(rescan, "_remove_exact_artifact", cleanup_failure)
    monkeypatch.setattr(rescan, "_restore_claim", track_restore)

    result = rescan.rescan_series_folder(7)

    assert result["recovered"] == 1
    assert restoration_attempted
    assert cbr_path.read_bytes() == original_bytes
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert db.execute(
            "SELECT status,import_path FROM volumes WHERE id=?",
            (volume_id,),
        ).fetchone() == ("downloaded", str(cbr_path))


def test_claim_restoration_does_not_overwrite_winner(rescan_env):
    import rescan

    source_path = rescan_env["series_dir"] / "Race Manga v01.cbz"
    source_path.write_bytes(b"original")
    fingerprint = rescan._fingerprint(os.stat(source_path))
    claim = rescan._claim_exact_path(str(source_path), fingerprint)
    assert claim is not None
    winner_bytes = b"winner"
    source_path.write_bytes(winner_bytes)

    restored = rescan._restore_claim(claim)

    assert restored is False
    assert source_path.read_bytes() == winner_bytes
    assert os.path.exists(claim.claimed_path)
    rescan._discard_claim(claim)


def test_rescan_never_rewrites_archive_after_reconciliation(rescan_env, monkeypatch):
    import rescan

    archive_path = rescan_env["series_dir"] / "Race Manga v01.cbz"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("001.jpg", b"page")
    original_bytes = archive_path.read_bytes()
    _insert_volume(rescan_env["db_path"], 1.0, "wanted")
    monkeypatch.setattr(rescan, "quality_from_filename", lambda path: "cbz")
    real_reconcile = rescan.reconcile_series_inventory

    def reconcile_then_change(db, snapshot, inventory):
        reconciliation = real_reconcile(db, snapshot, inventory)
        db.execute("UPDATE series SET title='Concurrent Title' WHERE id=7")
        db.execute("INSERT INTO series_tags(series_id,tag) VALUES(7,'concurrent-tag')")
        db.execute(
            "UPDATE volumes SET import_path='/concurrent/import.cbz'"
            " WHERE series_id=7 AND volume_num=1"
        )
        return reconciliation

    monkeypatch.setattr(
        rescan,
        "reconcile_series_inventory",
        reconcile_then_change,
    )

    result = rescan.rescan_series_folder(7)

    assert result["recovered"] == 1
    assert archive_path.read_bytes() == original_bytes
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert db.execute("SELECT title FROM series WHERE id=7").fetchone()[0] == (
            "Concurrent Title"
        )
        assert (
            db.execute("SELECT tag FROM series_tags WHERE series_id=7").fetchone()[0]
            == "concurrent-tag"
        )
        assert (
            db.execute(
                "SELECT import_path FROM volumes WHERE series_id=7 AND volume_num=1"
            ).fetchone()[0]
            == "/concurrent/import.cbz"
        )


def test_pack_cascade_skips_volume_whose_cas_lost(rescan_env, monkeypatch):
    import rescan
    import shared

    volume_id = _insert_volume(rescan_env["db_path"], 1.0, "wanted")
    with sqlite3.connect(rescan_env["db_path"]) as db:
        pack_id = db.execute(
            "INSERT INTO volumes(series_id,volume_num,status,pack_type)"
            " VALUES(7,NULL,'grabbed','complete')"
        ).lastrowid
        db.execute(
            "INSERT INTO chapters(series_id,volume_id,chapter_num,status,monitored)"
            " VALUES(7,?,1,'wanted',1)",
            (volume_id,),
        )
    (rescan_env["series_dir"] / "Race Manga v01.cbz").write_bytes(b"archive")
    monkeypatch.setattr(rescan, "quality_from_filename", lambda path: "cbz")

    with shared.get_db() as db:
        snapshot = rescan.snapshot_series_rescan(db, 7)
    assert snapshot is not None
    inventory = rescan.build_filesystem_inventory(snapshot)
    with sqlite3.connect(rescan_env["db_path"]) as db:
        db.execute(
            "UPDATE volumes SET status='grabbed',torrent_name='concurrent import'"
            " WHERE id=?",
            (volume_id,),
        )
    with shared.get_db() as db:
        reconciliation = rescan.reconcile_series_inventory(db, snapshot, inventory)

    assert reconciliation.result["recovered"] == 1
    with sqlite3.connect(rescan_env["db_path"]) as db:
        volume_status = db.execute(
            "SELECT status FROM volumes WHERE id=?", (volume_id,)
        ).fetchone()[0]
        pack_status = db.execute(
            "SELECT status FROM volumes WHERE id=?", (pack_id,)
        ).fetchone()[0]
        chapter_status = db.execute(
            "SELECT status FROM chapters WHERE volume_id=?", (volume_id,)
        ).fetchone()[0]
    assert volume_status == "grabbed"
    assert pack_status == "downloaded"
    assert chapter_status == "wanted"


@pytest.mark.parametrize("race", ["folder", "root", "delete", "soft_delete"])
def test_series_path_identity_race_aborts_reconciliation(rescan_env, monkeypatch, race):
    import rescan
    import shared

    (rescan_env["series_dir"] / "Race Manga v05.cbz").write_bytes(b"archive")
    monkeypatch.setattr(rescan, "quality_from_filename", lambda path: "cbz")
    with shared.get_db() as db:
        snapshot = rescan.snapshot_series_rescan(db, 7)
    assert snapshot is not None
    inventory = rescan.build_filesystem_inventory(snapshot)

    with sqlite3.connect(rescan_env["db_path"]) as db:
        if race == "folder":
            db.execute("UPDATE series SET folder_name='Moved Manga' WHERE id=7")
        elif race == "root":
            db.execute(
                "UPDATE root_folders SET path=? WHERE id=1",
                (str(rescan_env["library_root"] / "new-root"),),
            )
        elif race == "delete":
            db.execute("DELETE FROM series WHERE id=7")
        else:
            db.execute("UPDATE series SET deleted_at=datetime('now') WHERE id=7")

    with shared.get_db() as db:
        reconciliation = rescan.reconcile_series_inventory(db, snapshot, inventory)

    assert reconciliation.result == {
        "found": 1,
        "recovered": 0,
        "missing": 0,
        "lost": 0,
        "created": 0,
    }
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM volumes WHERE series_id=7 AND volume_num=5"
            ).fetchone()[0]
            == 0
        )


def test_concurrent_unmatched_reconciliation_creates_one_stub(rescan_env, monkeypatch):
    import rescan
    import shared

    (rescan_env["series_dir"] / "Race Manga v04.cbz").write_bytes(b"archive")
    monkeypatch.setattr(rescan, "quality_from_filename", lambda path: "cbz")
    with shared.get_db() as db:
        snapshot = rescan.snapshot_series_rescan(db, 7)
    assert snapshot is not None
    inventory = rescan.build_filesystem_inventory(snapshot)

    barrier = threading.Barrier(2)
    created: list[int] = []
    errors: list[BaseException] = []

    def reconcile() -> None:
        try:
            barrier.wait(timeout=5)
            with shared.get_db() as db:
                outcome = rescan.reconcile_series_inventory(db, snapshot, inventory)
            created.append(outcome.result["created"])
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=reconcile) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert sorted(created) == [0, 1]
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM volumes WHERE series_id=7 AND volume_num=4"
            ).fetchone()[0]
            == 1
        )


def test_writer_refreshes_monitor_mode_before_creating_stub(rescan_env, monkeypatch):
    import rescan
    import shared

    (rescan_env["series_dir"] / "Race Manga v04.cbz").write_bytes(b"archive")
    monkeypatch.setattr(rescan, "quality_from_filename", lambda path: "cbz")
    with shared.get_db() as db:
        snapshot = rescan.snapshot_series_rescan(db, 7)
    assert snapshot is not None
    inventory = rescan.build_filesystem_inventory(snapshot)
    with sqlite3.connect(rescan_env["db_path"]) as db:
        db.execute("UPDATE series SET monitor_mode='none' WHERE id=7")

    with shared.get_db() as db:
        reconciliation = rescan.reconcile_series_inventory(db, snapshot, inventory)

    assert reconciliation.result["created"] == 1
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert db.execute(
            "SELECT monitored FROM volumes WHERE series_id=7 AND volume_num=4"
        ).fetchone()[0] == 0


def test_missing_chapter_cascade_uses_preindexed_rows_once(
    rescan_env, monkeypatch
):
    import rescan
    import shared

    volume_ids = [
        _insert_volume(
            rescan_env["db_path"],
            float(volume_num),
            "downloaded",
            import_path=f"/missing/v{volume_num}.cbz",
        )
        for volume_num in range(1, 9)
    ]
    with sqlite3.connect(rescan_env["db_path"]) as db:
        for volume_index, volume_id in enumerate(volume_ids, start=1):
            for chapter_num in range(1, 4):
                db.execute(
                    "INSERT INTO chapters(series_id,volume_id,chapter_num,"
                    " status,monitored) VALUES(7,?,?,'downloaded',1)",
                    (volume_id, volume_index * 10 + chapter_num),
                )
    with shared.get_db() as db:
        snapshot = rescan.snapshot_series_rescan(db, 7)
    assert snapshot is not None
    considered: list[int] = []
    real_cascade = rescan._cascade_chapter_snapshot

    def checked_cascade(db, chapters_by_volume, *, status, volume_ids, clear_grab=False):
        for volume_id in volume_ids:
            considered.extend(
                int(chapter["id"])
                for chapter in chapters_by_volume.get(volume_id, ())
            )
        return real_cascade(
            db,
            chapters_by_volume,
            status=status,
            volume_ids=volume_ids,
            clear_grab=clear_grab,
        )

    monkeypatch.setattr(rescan, "_cascade_chapter_snapshot", checked_cascade)
    inventory = rescan.build_filesystem_inventory(snapshot)

    with shared.get_db() as db:
        reconciliation = rescan.reconcile_series_inventory(db, snapshot, inventory)

    assert reconciliation.result["missing"] == len(volume_ids)
    assert len(considered) == len(set(considered)) == len(volume_ids) * 3


def test_snapshot_contains_only_plain_data_after_connection_exit(rescan_env):
    import rescan
    import shared

    _insert_volume(rescan_env["db_path"], 1.0, "wanted")
    with shared.get_db() as db:
        snapshot = rescan.snapshot_series_rescan(db, 7)
    assert snapshot is not None

    assert isinstance(snapshot.series, dict)
    assert all(isinstance(row, dict) for row in snapshot.numbered)
    assert all(isinstance(row, dict) for row in snapshot.packs)
    assert all(isinstance(row, dict) for row in snapshot.chapters)
    assert all(
        isinstance(row, dict)
        for rows in snapshot.chapters_by_volume.values()
        for row in rows
    )


def test_snapshot_uses_consistent_short_read_transaction(rescan_env):
    import rescan
    import shared

    statements: list[str] = []
    with shared.get_db() as db:
        db.set_trace_callback(statements.append)
        assert not db.in_transaction
        snapshot = rescan.snapshot_series_rescan(db, 7)
        assert not db.in_transaction

    assert snapshot is not None
    assert statements[0] == "BEGIN"
    assert statements[-1] == "COMMIT"


def test_full_rescan_closes_id_snapshot_connection_and_offloads(monkeypatch):
    import main
    import routers.series_ as series_router

    state = {"open": False}
    worker_threads: list[int] = []
    event_thread = threading.get_ident()

    class Rows:
        def fetchall(self):
            return [{"id": 11}, {"id": 12}]

    class FakeDb:
        def execute(self, sql):
            assert "SELECT id FROM series" in sql
            assert "deleted_at IS NULL" in sql
            return Rows()

    @contextmanager
    def fake_get_db():
        state["open"] = True
        try:
            yield FakeDb()
        finally:
            state["open"] = False

    def fake_rescan(series_id):
        assert not state["open"]
        worker_threads.append(threading.get_ident())
        return {"found": 1, "recovered": 0, "missing": 0, "lost": 0, "created": 0}

    monkeypatch.setattr(series_router, "get_db", fake_get_db)
    monkeypatch.setattr(main, "rescan_series_folder", fake_rescan)
    monkeypatch.setattr(main, "log_event", lambda *args, **kwargs: None)

    asyncio.run(series_router._rescan_all_impl())

    assert len(worker_threads) == 2
    assert all(thread_id != event_thread for thread_id in worker_threads)
    assert state["open"] is False


def test_single_rescan_route_offloads_and_preserves_plain_fallback(monkeypatch):
    import main
    import routers.series_ as series_router

    worker_threads: list[int] = []
    event_thread = threading.get_ident()

    def fake_rescan(series_id):
        worker_threads.append(threading.get_ident())
        return {"found": 0, "recovered": 0, "missing": 0, "lost": 0, "created": 0}

    monkeypatch.setattr(main, "rescan_series_folder", fake_rescan)
    monkeypatch.setattr(main, "log_event", lambda *args, **kwargs: None)
    request = Request({"type": "http", "method": "POST", "headers": []})

    response = asyncio.run(series_router.rescan_series(request, 7))

    assert response.status_code == 303
    assert response.headers["location"] == "/series/7"
    assert worker_threads and worker_threads[0] != event_thread


def test_adoption_route_offloads_from_event_loop(monkeypatch):
    import routers.api_v1 as api_v1
    from library_scan import AdoptUnmappedFolderResult

    worker_threads: list[int] = []
    event_thread = threading.get_ident()

    def fake_adopt(*args, **kwargs):
        worker_threads.append(threading.get_ident())
        return AdoptUnmappedFolderResult(
            True,
            200,
            payload={"series": {}, "rescan": {"created": 0}},
        )

    body = json.dumps({"path": "/library/Adopt Me"}).encode()
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/rootfolder/1/unmappedfolders/adopt",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"",
        },
        receive,
    )
    monkeypatch.setattr(api_v1, "adopt_unmapped_folder", fake_adopt)

    response = asyncio.run(api_v1.api_v1_root_folder_adopt_unmapped(request, 1))

    assert response.status_code == 200
    assert worker_threads and worker_threads[0] != event_thread


def test_adoption_scans_before_writer_and_reconciles_without_filesystem_io(
    rescan_env, monkeypatch
):
    import library_scan
    import rescan
    import shared

    target = rescan_env["library_root"] / "Adopt Me"
    target.mkdir()
    (target / "Adopt Me v01.cbz").write_bytes(b"archive")

    active: list[sqlite3.Connection] = []
    real_get_db = shared.get_db
    real_build = library_scan.build_filesystem_inventory
    real_reconcile = library_scan.reconcile_series_inventory
    real_enrich = library_scan.enrich_reconciled_files
    real_flock = library_scan.fcntl.flock
    real_isdir = os.path.isdir
    real_realpath = os.path.realpath
    phases: list[str] = []

    @contextmanager
    def tracked_get_db():
        with real_get_db() as db:
            active.append(db)
            try:
                yield db
            finally:
                active.remove(db)

    def checked_build(snapshot):
        phases.append("inventory")
        assert not active
        return real_build(snapshot)

    def checked_reconcile(db, snapshot, inventory):
        phases.append("reconcile")
        assert db.in_transaction
        return real_reconcile(db, snapshot, inventory)

    def checked_enrich(reconciliation):
        phases.append("enrich")
        assert not any(db.in_transaction for db in active)
        return real_enrich(reconciliation)

    def checked_flock(fd, operation):
        assert not any(db.in_transaction for db in active)
        return real_flock(fd, operation)

    def checked_isdir(path):
        assert not any(db.in_transaction for db in active)
        return real_isdir(path)

    def checked_realpath(path):
        assert not any(db.in_transaction for db in active)
        return real_realpath(path)

    monkeypatch.setattr(library_scan, "get_db", tracked_get_db)
    monkeypatch.setattr(library_scan, "build_filesystem_inventory", checked_build)
    monkeypatch.setattr(library_scan, "reconcile_series_inventory", checked_reconcile)
    monkeypatch.setattr(library_scan, "enrich_reconciled_files", checked_enrich)
    monkeypatch.setattr(library_scan.fcntl, "flock", checked_flock)
    monkeypatch.setattr(library_scan.os.path, "isdir", checked_isdir)
    monkeypatch.setattr(library_scan.os.path, "realpath", checked_realpath)
    monkeypatch.setattr(rescan, "quality_from_filename", lambda path: "cbz")

    result = library_scan.adopt_unmapped_folder(1, str(target))

    assert result.ok
    assert result.payload is not None
    assert result.payload["rescan"]["created"] == 1
    assert phases == ["inventory", "reconcile", "enrich"]
    with sqlite3.connect(rescan_env["db_path"]) as db:
        row = db.execute(
            "SELECT v.status,v.import_path FROM volumes v"
            " JOIN series s ON s.id=v.series_id WHERE s.title='Adopt Me'"
        ).fetchone()
    assert row == ("downloaded", str(target / "Adopt Me v01.cbz"))
