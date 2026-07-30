"""Focused durability and replay tests for the import publication journal."""

from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import threading
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401, E402


def _zip(path: Path, payload: bytes) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("page.bin", payload)


@pytest.fixture
def journal_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import import_download
    import import_execute
    import main
    import security
    import shared

    db_path = tmp_path / "journal.db"
    library_root = tmp_path / "library"
    source_root = tmp_path / "downloads"
    key_root = tmp_path / "keys"
    library_root.mkdir()
    source_root.mkdir()
    key_root.mkdir()

    original_main_db = main.DB_PATH
    original_shared_db = shared.DB_PATH
    original_main_config = dict(main.CONFIG)
    original_shared_config = dict(shared.CONFIG)
    original_sem = import_execute._IMPORT_SEM
    original_secret_cipher = security._SECRET_CIPHER
    main.DB_PATH = str(db_path)
    shared.DB_PATH = str(db_path)
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(str(key_root))
    main.init_db()
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT OR REPLACE INTO root_folders(id,path,label,is_default)"
            " VALUES(1,?,'Test',1)",
            (str(library_root),),
        )
    main.load_config()
    main.CONFIG["remove_completed"] = "false"
    shared.CONFIG["remove_completed"] = "false"
    import_execute._IMPORT_SEM = None

    async def _noop(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(import_execute, "broadcast_queue_event", _noop)
    monkeypatch.setattr(import_download, "dispatch_download_notification", _noop)

    try:
        yield {
            "db_path": db_path,
            "library_root": library_root,
            "source_root": source_root,
        }
    finally:
        main.DB_PATH = original_main_db
        shared.DB_PATH = original_shared_db
        main.CONFIG.clear()
        main.CONFIG.update(original_main_config)
        shared.CONFIG.clear()
        shared.CONFIG.update(original_shared_config)
        import_execute._IMPORT_SEM = original_sem
        security._SECRET_CIPHER = original_secret_cipher
        shutil.rmtree(key_root)


def _set_mode(mode: str) -> None:
    import main
    import shared

    main.CONFIG["import_mode"] = mode
    shared.CONFIG["import_mode"] = mode


def _seed_queue(
    env: dict[str, Path],
    *,
    file_count: int = 2,
    mode: str = "copy",
    source_paths: list[Path] | None = None,
    needs_review: bool = False,
) -> tuple[int, int, list[Path], list[Path]]:
    _set_mode(mode)
    db_path = env["db_path"]
    source_root = env["source_root"]
    library_root = env["library_root"]
    title = f"Journal Series {os.urandom(3).hex()}"
    destination_dir = library_root / title

    sources = source_paths or []
    if not sources:
        for index in range(file_count):
            source = source_root / f"source-{index + 1}.cbz"
            _zip(source, f"payload-{index + 1}".encode())
            sources.append(source)
    finals = [
        destination_dir / f"Journal v{index + 1:02d}.cbz" for index in range(file_count)
    ]

    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO series(title,search_pattern,root_folder_id) VALUES(?,?,1)",
            (title, title),
        )
        series_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
        for index in range(file_count):
            db.execute(
                "INSERT INTO volumes(series_id,volume_num,status,download_id)"
                " VALUES(?,?,'grabbed','journal-download')",
                (series_id, float(index + 1)),
            )
        db.execute(
            "INSERT INTO import_queue(series_id,download_id,torrent_name,"
            " torrent_url,volume_num,src_dir,status)"
            " VALUES(?,'journal-download','Journal batch','magnet:journal',"
            " NULL,?,'pending')",
            (series_id, str(source_root)),
        )
        queue_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
        for index, source in enumerate(sources):
            status = (
                "needs_review"
                if needs_review and index == file_count - 1
                else "pending"
            )
            db.execute(
                "INSERT INTO import_queue_files(queue_id,filename,src_path,"
                " proposed_volume,file_type,proposed_import_kind,status)"
                " VALUES(?,?,?,?,?,'volume',?)",
                (
                    queue_id,
                    finals[index].name,
                    str(source),
                    float(index + 1),
                    "volume",
                    status,
                ),
            )
    return queue_id, series_id, sources, finals


def _prepare_overwrite_publication(
    env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[int, int, Path, Path, Path, Path]:
    """Stop one import at its prepared barrier with an existing destination."""
    queue_id, series_id, sources, finals = _seed_queue(env, file_count=1)
    finals[0].parent.mkdir()
    _zip(finals[0], b"old-destination")

    import import_execute

    async def _defer_publication(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        return False

    monkeypatch.setattr(
        import_execute,
        "complete_publication",
        _defer_publication,
    )
    assert not asyncio.run(import_execute._execute_import(queue_id))

    with sqlite3.connect(env["db_path"]) as db:
        row = db.execute(
            """
            SELECT p.id, p.state, f.stage_path, f.final_claim_path
            FROM import_publications AS p
            JOIN import_publication_files AS f ON f.publication_id=p.id
            WHERE p.queue_id=?
            """,
            (queue_id,),
        ).fetchone()
    assert row is not None
    assert row[1] == "prepared"
    return (
        int(row[0]),
        series_id,
        sources[0],
        finals[0],
        Path(row[2]),
        Path(row[3]),
    )


def _seed_minimal_publications(
    env: dict[str, Path],
    states: list[str],
    *,
    first_queue_id: int = 10_000,
) -> list[int]:
    publication_ids: list[int] = []
    dst_dir = env["library_root"] / "minimal-publications"
    with sqlite3.connect(env["db_path"]) as db:
        for offset, state in enumerate(states):
            queue_id = first_queue_id + offset
            terminal = state in ("finalized", "deleted")
            db.execute(
                """
                INSERT INTO import_publications(
                    queue_id, state, owner_token, series_id, dst_dir,
                    import_mode, staging_dir, queue_snapshot_json,
                    series_snapshot_json, series_tags_json, queue_status,
                    queue_download_id, pack_cleanup_state
                ) VALUES(?, ?, 'minimal-owner', 1, ?, 'copy', ?,
                         ?, NULL, '[]', 'importing', ?, ?)
                """,
                (
                    queue_id,
                    state,
                    str(dst_dir),
                    str(dst_dir / f".mangarr-publication-{queue_id}"),
                    f'{{"id":{queue_id}}}',
                    f"download-{queue_id}" if terminal else None,
                    "pending" if terminal else "retained",
                ),
            )
            publication_ids.append(
                int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
            )
    return publication_ids


def _run_replay() -> object:
    from import_publication import replay_import_publications

    return asyncio.run(replay_import_publications(max_rows=100))


def _assert_exactly_once(env: dict[str, Path], series_id: int) -> None:
    with sqlite3.connect(env["db_path"]) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM history"
                " WHERE series_id=? AND event_type='imported'",
                (series_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM volumes"
                " WHERE series_id=? AND status='downloaded'",
                (series_id,),
            ).fetchone()[0]
            == 2
        )
        assert db.execute(
            "SELECT state FROM import_publications ORDER BY id DESC LIMIT 1"
        ).fetchone() == ("deleted",)


_CRASH_CHILD = textwrap.dedent(
    """
    import asyncio
    import os
    import signal

    import import_execute
    import import_pack_cleanup
    import import_pipeline
    import import_publication
    import shared

    shared.DB_PATH = os.environ["JOURNAL_DB"]
    shared.CONFIG["import_mode"] = os.environ["JOURNAL_MODE"]
    shared.CONFIG["remove_completed"] = "false"
    if pack_root := os.environ.get("JOURNAL_PACK_ROOT"):
        import_pipeline.PACK_STAGING_ROOT = pack_root
    queue_id = int(os.environ["JOURNAL_QUEUE_ID"])
    crash_kind = os.environ["JOURNAL_CRASH_KIND"]
    crash_index = int(os.environ.get("JOURNAL_CRASH_INDEX", "1"))
    crash_when = os.environ.get("JOURNAL_CRASH_WHEN", "before")

    def die():
        os.kill(os.getpid(), signal.SIGKILL)

    if crash_kind == "replace":
        real_replace = import_publication._rename_noreplace
        calls = 0
        def replace(src, dst):
            global calls
            calls += 1
            if calls == crash_index and crash_when == "before":
                die()
            real_replace(src, dst)
            if calls == crash_index and crash_when == "after":
                die()
        import_publication._rename_noreplace = replace
    elif crash_kind == "fsync":
        real_barrier = import_execute.commit_prepared_barrier
        real_fsync = import_publication._fsync_directory
        def barrier(*args, **kwargs):
            real_barrier(*args, **kwargs)
            calls = 0
            def fsync(path):
                nonlocal calls
                calls += 1
                if calls == crash_index and crash_when == "before":
                    die()
                real_fsync(path)
                if calls == crash_index and crash_when == "after":
                    die()
            import_publication._fsync_directory = fsync
        import_execute.commit_prepared_barrier = barrier
    elif crash_kind == "phase3_before":
        def claim(*args, **kwargs):
            die()
        import_publication.claim_publication_phase3 = claim
    elif crash_kind == "phase3_during":
        def committed(*args, **kwargs):
            die()
        import_publication.mark_publication_db_committed = committed
    elif crash_kind == "phase3_after":
        async def notify(*args, **kwargs):
            die()
        import_publication._dispatch_journal_notification = notify
    elif crash_kind == "unlink":
        real_unlink = import_publication.os.unlink
        calls = 0
        def unlink(path, *args, **kwargs):
            global calls
            calls += 1
            if calls == crash_index and crash_when == "before":
                die()
            real_unlink(path, *args, **kwargs)
            if calls == crash_index and crash_when == "after":
                die()
        import_publication.os.unlink = unlink
    elif crash_kind == "pack_cleanup_before":
        def cleanup(*args, **kwargs):
            die()
        import_pack_cleanup.cleanup_terminal_pack_staging = cleanup
    elif crash_kind == "pack_cleanup_tombstone":
        def remove_tombstone(*args, **kwargs):
            die()
        import_pack_cleanup._remove_tracked_tombstone = remove_tombstone

    asyncio.run(import_execute._execute_import(queue_id))
    """
)

_REPLAY_CHILD = textwrap.dedent(
    """
    import asyncio
    import os
    import time

    import import_publication
    import shared

    shared.DB_PATH = os.environ["JOURNAL_DB"]
    start_file = os.environ["JOURNAL_START_FILE"]
    while not os.path.exists(start_file):
        time.sleep(0.01)
    summary = asyncio.run(
        import_publication.replay_import_publications(max_rows=None)
    )
    print(summary)
    """
)


def _crash_worker(
    env: dict[str, Path],
    queue_id: int,
    *,
    kind: str,
    mode: str = "copy",
    index: int = 1,
    when: str = "before",
) -> None:
    child_env = dict(os.environ)
    child_env.update(
        {
            "PYTHONPATH": str(Path.cwd() / "app"),
            "JOURNAL_DB": str(env["db_path"]),
            "JOURNAL_QUEUE_ID": str(queue_id),
            "JOURNAL_MODE": mode,
            "JOURNAL_CRASH_KIND": kind,
            "JOURNAL_CRASH_INDEX": str(index),
            "JOURNAL_CRASH_WHEN": when,
        }
    )
    if pack_root := env.get("pack_root"):
        child_env["JOURNAL_PACK_ROOT"] = str(pack_root)
    result = subprocess.run(
        [sys.executable, "-c", _CRASH_CHILD],
        env=child_env,
        check=False,
        timeout=30,
    )
    assert result.returncode == -signal.SIGKILL
    # A crashed process retains no authority. Advance its persisted operation
    # lease so replay exercises the durable expired-owner takeover path.
    with sqlite3.connect(env["db_path"]) as db:
        db.execute(
            "UPDATE import_publications"
            " SET operation_expires_at=datetime('now', '-1 second')"
            " WHERE operation_owner IS NOT NULL"
        )


def test_atomic_noreplace_fails_closed_off_linux(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_publication

    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"source")
    monkeypatch.setattr(import_publication.sys, "platform", "unsupported")

    with pytest.raises(OSError) as raised:
        import_publication._rename_noreplace(str(source), str(destination))

    assert raised.value.errno == import_publication.errno.ENOTSUP
    assert source.read_bytes() == b"source"
    assert not destination.exists()


def test_nested_destination_entries_are_durable_before_move_source_cleanup(
    journal_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_id, _, sources, _ = _seed_queue(
        journal_env,
        file_count=1,
        mode="move",
    )
    nested_root = journal_env["library_root"] / "new-parent" / "new-root"
    with sqlite3.connect(journal_env["db_path"]) as db:
        db.execute(
            "UPDATE root_folders SET path=? WHERE id=1",
            (str(nested_root),),
        )
        title = str(
            db.execute(
                "SELECT series.title FROM series"
                " JOIN import_queue ON import_queue.series_id=series.id"
                " WHERE import_queue.id=?",
                (queue_id,),
            ).fetchone()[0]
        )

    import import_execute
    import import_publication

    real_fsync = import_publication._fsync_directory
    real_cleanup = import_publication._cleanup_move_source
    events: list[tuple[str, str]] = []

    def _fsync(path: str) -> None:
        events.append(("fsync", os.path.abspath(path)))
        real_fsync(path)

    def _cleanup(
        publication_id: int,
        file_record: import_publication.PublicationFile,
        owner_token: str,
    ) -> import_publication.CleanupOutcome:
        events.append(("source_cleanup", str(sources[0])))
        return real_cleanup(publication_id, file_record, owner_token)

    monkeypatch.setattr(import_publication, "_fsync_directory", _fsync)
    monkeypatch.setattr(import_publication, "_cleanup_move_source", _cleanup)

    assert asyncio.run(import_execute._execute_import(queue_id))
    first_cleanup = next(
        index
        for index, event in enumerate(events)
        if event[0] == "source_cleanup"
    )
    destination_dir = nested_root / title
    required_barriers = (
        journal_env["library_root"],
        journal_env["library_root"] / "new-parent",
        nested_root,
        destination_dir,
    )
    for directory in required_barriers:
        assert ("fsync", str(directory)) in events[:first_cleanup]
    assert not sources[0].exists()
    assert (destination_dir / "Journal v01.cbz").is_file()


@pytest.mark.parametrize(
    "queue_mutation",
    (
        "status='pending'",
        "lease_owner='successor-owner'",
        "lease_expires_at=datetime('now', '-1 second')",
    ),
)
def test_publication_creation_cas_rejects_stale_queue_snapshot(
    journal_env: dict[str, Path],
    queue_mutation: str,
) -> None:
    queue_id, _, _, _ = _seed_queue(journal_env, file_count=1)
    owner = "publication-cas-owner"

    import import_publication
    import main
    from import_lease import IMPORT_LEASE_SECONDS, claim_import_queue_row
    from import_plan import _plan_import

    with main.get_db() as db:
        assert claim_import_queue_row(db, queue_id, owner)
        plan = _plan_import(
            db,
            queue_id,
            owner,
            {},
            {},
            set(),
            "copy",
            lease_seconds=IMPORT_LEASE_SECONDS,
        )
    assert plan is not None
    staging_dir, source_fingerprints = (
        import_publication.initialize_publication_filesystem(plan, owner)
    )
    with sqlite3.connect(journal_env["db_path"]) as db:
        db.execute(
            f"UPDATE import_queue SET {queue_mutation} WHERE id=?",
            (queue_id,),
        )

    with pytest.raises(import_publication.PublicationOwnershipLost):
        with main.get_db() as db:
            import_publication.create_publication(
                db,
                plan,
                owner,
                staging_dir,
                source_fingerprints,
            )
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM import_publications WHERE queue_id=?",
            (queue_id,),
        ).fetchone() == (0,)
    shutil.rmtree(staging_dir)


def test_stale_publication_loser_cannot_remove_successor_staging(
    journal_env: dict[str, Path],
) -> None:
    queue_id, _, _, _ = _seed_queue(journal_env, file_count=1)
    stale_owner = "stale-publication-owner"
    successor_owner = "successor-publication-owner"

    import import_publication
    import main
    from import_lease import IMPORT_LEASE_SECONDS, claim_import_queue_row
    from import_plan import _ImportPlan, _plan_import

    def _claim_plan(owner: str) -> _ImportPlan:
        with main.get_db() as db:
            assert claim_import_queue_row(db, queue_id, owner)
            plan = _plan_import(
                db,
                queue_id,
                owner,
                {},
                {},
                set(),
                "copy",
                lease_seconds=IMPORT_LEASE_SECONDS,
            )
        assert plan is not None
        return plan

    stale_plan = _claim_plan(stale_owner)
    stale_staging, stale_sources = (
        import_publication.initialize_publication_filesystem(
            stale_plan,
            stale_owner,
        )
    )
    with sqlite3.connect(journal_env["db_path"]) as db:
        db.execute(
            "UPDATE import_queue SET status='pending', lease_owner=NULL,"
            " lease_expires_at=NULL WHERE id=?",
            (queue_id,),
        )

    successor_plan = _claim_plan(successor_owner)
    successor_staging, successor_sources = (
        import_publication.initialize_publication_filesystem(
            successor_plan,
            successor_owner,
        )
    )
    successor_sentinel = Path(successor_staging) / "successor.sentinel"
    successor_sentinel.write_bytes(b"successor")
    with main.get_db() as db:
        successor_publication = import_publication.create_publication(
            db,
            successor_plan,
            successor_owner,
            successor_staging,
            successor_sources,
        )

    assert stale_staging != successor_staging
    with pytest.raises(import_publication.PublicationOwnershipLost):
        with main.get_db() as db:
            import_publication.create_publication(
                db,
                stale_plan,
                stale_owner,
                stale_staging,
                stale_sources,
            )
    shutil.rmtree(stale_staging)

    assert successor_sentinel.read_bytes() == b"successor"
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute(
            "SELECT id, owner_token, staging_dir FROM import_publications"
            " WHERE queue_id=?",
            (queue_id,),
        ).fetchone() == (
            successor_publication,
            successor_owner,
            successor_staging,
        )


def test_claim_restore_fsyncs_rename_directories_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_publication

    claim_dir = tmp_path / "claim-dir"
    original_dir = tmp_path / "original-dir"
    claim_dir.mkdir()
    original_dir.mkdir()
    claim = claim_dir / "artifact.claim"
    original = original_dir / "artifact.cbz"
    claim.write_bytes(b"old")
    real_rename = import_publication._rename_noreplace
    real_fsync = import_publication._fsync_directory
    calls: list[tuple[str, str, str | None]] = []

    def _rename(source: str, destination: str) -> None:
        calls.append(("rename", source, destination))
        real_rename(source, destination)

    def _fsync(path: str) -> None:
        calls.append(("fsync", os.path.abspath(path), None))
        real_fsync(path)

    monkeypatch.setattr(import_publication, "_rename_noreplace", _rename)
    monkeypatch.setattr(import_publication, "_fsync_directory", _fsync)

    assert import_publication._restore_claim_without_clobber(
        str(claim),
        str(original),
    )
    assert calls == [
        ("rename", str(claim), str(original)),
        ("fsync", str(original_dir), None),
        ("fsync", str(claim_dir), None),
    ]


def test_claim_restore_surfaces_directory_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_publication

    claim = tmp_path / "artifact.claim"
    original = tmp_path / "artifact.cbz"
    claim.write_bytes(b"old")

    def _fail_fsync(path: str) -> None:
        raise OSError(import_publication.errno.EIO, "fsync failed", path)

    monkeypatch.setattr(import_publication, "_fsync_directory", _fail_fsync)

    with pytest.raises(OSError, match="fsync failed"):
        import_publication._restore_claim_without_clobber(
            str(claim),
            str(original),
        )
    assert original.read_bytes() == b"old"
    assert not claim.exists()


def test_overwrite_publish_fsync_order_precedes_old_claim_unlink(
    journal_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_publication

    publication_id, _, _, final, stage, claim = _prepare_overwrite_publication(
        journal_env,
        monkeypatch,
    )
    destination_dir = str(final.parent)
    staging_dir = str(stage.parent)
    real_rename = import_publication._rename_noreplace
    real_fsync = import_publication._fsync_directory
    real_unlink = import_publication.os.unlink
    calls: list[tuple[str, str, str | None]] = []

    def _rename(source: str, destination: str) -> None:
        calls.append(("rename", source, destination))
        real_rename(source, destination)

    def _fsync(path: str) -> None:
        calls.append(("fsync", os.path.abspath(path), None))
        real_fsync(path)

    def _unlink(path: str, *args: object, **kwargs: object) -> None:
        calls.append(("unlink", os.path.abspath(path), None))
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(import_publication, "_rename_noreplace", _rename)
    monkeypatch.setattr(import_publication, "_fsync_directory", _fsync)
    monkeypatch.setattr(import_publication.os, "unlink", _unlink)

    assert import_publication.publish_publication(
        publication_id,
        "fsync-order-owner",
    )
    assert calls == [
        ("rename", str(final), str(claim)),
        ("fsync", destination_dir, None),
        ("rename", str(stage), str(final)),
        ("fsync", destination_dir, None),
        ("fsync", staging_dir, None),
        ("unlink", str(claim), None),
        ("fsync", destination_dir, None),
    ]


@pytest.mark.parametrize("failed_barrier", (1, 2, 3, 4))
def test_overwrite_fsync_failure_is_replayable_at_every_barrier(
    journal_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    failed_barrier: int,
) -> None:
    import import_publication

    publication_id, series_id, source, final, _, _ = _prepare_overwrite_publication(
        journal_env,
        monkeypatch,
    )
    old_digest = hashlib.sha256(final.read_bytes()).digest()
    real_fsync = import_publication._fsync_directory
    calls = 0

    def _fail_selected_fsync(path: str) -> None:
        nonlocal calls
        calls += 1
        if calls == failed_barrier:
            raise OSError(import_publication.errno.EIO, "injected fsync failure", path)
        real_fsync(path)

    monkeypatch.setattr(
        import_publication,
        "_fsync_directory",
        _fail_selected_fsync,
    )
    assert not import_publication.publish_publication(
        publication_id,
        "fsync-failure-owner",
    )

    monkeypatch.setattr(import_publication, "_fsync_directory", real_fsync)
    replayed = _run_replay()
    assert replayed.completed == 1
    assert source.is_file()
    assert final.is_file()
    assert hashlib.sha256(final.read_bytes()).digest() != old_digest
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute(
            "SELECT state FROM import_publications WHERE id=?",
            (publication_id,),
        ).fetchone() == ("deleted",)
        assert db.execute(
            "SELECT COUNT(*) FROM history WHERE series_id=? AND event_type='imported'",
            (series_id,),
        ).fetchone() == (1,)


@pytest.mark.parametrize("barrier", (1, 2, 3, 4))
def test_sigkill_after_each_overwrite_fsync_barrier_replays(
    journal_env: dict[str, Path],
    barrier: int,
) -> None:
    queue_id, series_id, sources, finals = _seed_queue(
        journal_env,
        file_count=1,
    )
    finals[0].parent.mkdir()
    _zip(finals[0], b"old-destination")
    old_digest = hashlib.sha256(finals[0].read_bytes()).digest()

    _crash_worker(
        journal_env,
        queue_id,
        kind="fsync",
        index=barrier,
        when="after",
    )

    replayed = _run_replay()
    assert replayed.completed == 1
    assert sources[0].is_file()
    assert finals[0].is_file()
    assert hashlib.sha256(finals[0].read_bytes()).digest() != old_digest
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM history WHERE series_id=? AND event_type='imported'",
            (series_id,),
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    ("index", "when"),
    ((1, "before"), (1, "after"), (2, "before"), (2, "after")),
)
def test_sigkill_before_and_after_each_replace_rolls_forward(
    journal_env: dict[str, Path],
    index: int,
    when: str,
) -> None:
    queue_id, series_id, sources, finals = _seed_queue(journal_env)
    _crash_worker(
        journal_env,
        queue_id,
        kind="replace",
        index=index,
        when=when,
    )

    summary = _run_replay()
    assert summary.completed == 1
    assert all(final.is_file() for final in finals)
    assert all(source.is_file() for source in sources)
    _assert_exactly_once(journal_env, series_id)


@pytest.mark.parametrize(
    "kind",
    ("phase3_before", "phase3_during", "phase3_after"),
)
def test_sigkill_around_phase3_commit_is_atomic_and_replayable(
    journal_env: dict[str, Path],
    kind: str,
) -> None:
    queue_id, series_id, _, finals = _seed_queue(journal_env)
    _crash_worker(journal_env, queue_id, kind=kind)

    _run_replay()
    assert all(final.is_file() for final in finals)
    _assert_exactly_once(journal_env, series_id)


@pytest.mark.parametrize("when", ("before", "after"))
def test_sigkill_around_move_source_unlink_is_replayable(
    journal_env: dict[str, Path],
    when: str,
) -> None:
    queue_id, series_id, sources, finals = _seed_queue(
        journal_env,
        mode="move",
    )
    _crash_worker(
        journal_env,
        queue_id,
        kind="unlink",
        mode="move",
        when=when,
    )

    _run_replay()
    assert all(final.is_file() for final in finals)
    assert all(not source.exists() for source in sources)
    _assert_exactly_once(journal_env, series_id)


def test_active_journal_retains_pack_until_replay_terminal_cleanup(
    journal_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_execute
    import import_pack_cleanup
    import import_pipeline

    pack_root = journal_env["db_path"].with_suffix(".packs")
    pack_root.mkdir()
    journal_env["pack_root"] = pack_root
    monkeypatch.setattr(import_pipeline, "PACK_STAGING_ROOT", str(pack_root))
    pack_dir = Path(
        import_pack_cleanup.pack_queue_creation_paths(
            "journal-download",
            "test-owner",
            download_client_id=None,
            protocol=None,
        )[0]
    )
    pack_dir.mkdir()
    (pack_dir / "generated.cbz").write_bytes(b"generated")
    queue_id, series_id, _, finals = _seed_queue(journal_env, file_count=1)
    _crash_worker(
        journal_env,
        queue_id,
        kind="replace",
        index=1,
        when="before",
    )

    assert not import_execute._cleanup_pack_staging_if_safe(
        queue_id,
        "journal-download",
        "crashed-owner",
    )
    assert pack_dir.is_dir()

    replayed = _run_replay()
    assert replayed.completed == 1
    assert finals[0].is_file()
    assert not pack_dir.exists()
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute(
            "SELECT state, pack_cleanup_state FROM import_publications"
        ).fetchone() == ("deleted", "complete")
        assert db.execute(
            "SELECT COUNT(*) FROM history WHERE series_id=? AND event_type='imported'",
            (series_id,),
        ).fetchone() == (1,)
    assert not list(pack_root.glob("queue-*.cleanup-*"))


@pytest.mark.parametrize(
    "kind",
    ("pack_cleanup_before", "pack_cleanup_tombstone"),
)
def test_crash_after_publication_finalize_replays_pack_cleanup_snapshot(
    journal_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    import import_pack_cleanup
    import import_pipeline

    pack_root = journal_env["db_path"].with_suffix(".packs")
    pack_root.mkdir()
    journal_env["pack_root"] = pack_root
    monkeypatch.setattr(import_pipeline, "PACK_STAGING_ROOT", str(pack_root))
    pack_dir = Path(
        import_pack_cleanup.pack_queue_creation_paths(
            "journal-download",
            "test-owner",
            download_client_id=None,
            protocol=None,
        )[0]
    )
    pack_dir.mkdir()
    (pack_dir / "generated.cbz").write_bytes(b"generated")
    queue_id, series_id, _, finals = _seed_queue(journal_env, file_count=1)

    _crash_worker(journal_env, queue_id, kind=kind)
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute(
            "SELECT state, queue_download_id, pack_cleanup_state"
            " FROM import_publications"
        ).fetchone() == ("deleted", "journal-download", "pending")
        assert (
            db.execute(
                "SELECT 1 FROM import_queue WHERE id=?",
                (queue_id,),
            ).fetchone()
            is None
        )

    replayed = _run_replay()
    assert replayed.completed == 1
    assert finals[0].is_file()
    assert not pack_dir.exists()
    assert not list(pack_root.glob("queue-*.cleanup-*"))
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute(
            "SELECT state, pack_cleanup_state FROM import_publications"
        ).fetchone() == ("deleted", "complete")
        assert db.execute(
            "SELECT COUNT(*) FROM import_pack_cleanup_tombstones"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT COUNT(*) FROM history WHERE series_id=? AND event_type='imported'",
            (series_id,),
        ).fetchone() == (1,)


def test_missing_or_wrong_stage_and_final_blocks_without_phase3(
    journal_env: dict[str, Path],
) -> None:
    queue_id, series_id, _, finals = _seed_queue(journal_env, file_count=2)
    _crash_worker(
        journal_env,
        queue_id,
        kind="replace",
        index=1,
        when="before",
    )
    with sqlite3.connect(journal_env["db_path"]) as db:
        stage_paths = [
            Path(row[0])
            for row in db.execute(
                "SELECT stage_path FROM import_publication_files ORDER BY ordinal"
            ).fetchall()
        ]
    stage_paths[0].unlink()
    finals[0].parent.mkdir(exist_ok=True)
    finals[0].write_bytes(b"wrong-final")
    stage_paths[1].write_bytes(b"wrong-stage")

    summary = _run_replay()
    assert summary.blocked == 1
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert (
            db.execute("SELECT state FROM import_publications").fetchone()[0]
            == "publishing"
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM history WHERE series_id=?",
                (series_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT status FROM import_queue WHERE id=?",
                (queue_id,),
            ).fetchone()[0]
            == "importing"
        )


@pytest.mark.parametrize("prepared_existing", (False, True))
def test_destination_appearing_or_replaced_after_prepared_is_retained(
    journal_env: dict[str, Path],
    prepared_existing: bool,
) -> None:
    queue_id, series_id, _, finals = _seed_queue(journal_env, file_count=1)
    prepared_payload = b"prepared-existing"
    if prepared_existing:
        finals[0].parent.mkdir()
        finals[0].write_bytes(prepared_payload)

    _crash_worker(
        journal_env,
        queue_id,
        kind="replace",
        index=1,
        when="before",
    )
    late_payload = (
        b"replacement-after-prepared"
        if prepared_existing
        else b"appeared-after-prepared"
    )
    replacement = finals[0].with_suffix(".late")
    replacement.write_bytes(late_payload)
    os.replace(replacement, finals[0])

    summary = _run_replay()
    assert summary.blocked == 1
    assert finals[0].read_bytes() == late_payload
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute("SELECT state FROM import_publications").fetchone() == (
            "publishing",
        )
        assert db.execute(
            "SELECT COUNT(*) FROM history WHERE series_id=?",
            (series_id,),
        ).fetchone() == (0,)
        barrier = db.execute(
            "SELECT final_expected_absent, prepared_final_sha256"
            " FROM import_publication_files"
        ).fetchone()
    assert barrier[0] == int(not prepared_existing)
    assert (barrier[1] is not None) is prepared_existing


def test_live_publishing_owner_defers_without_filesystem_work(
    journal_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_id, _, _, finals = _seed_queue(journal_env, file_count=1)
    _crash_worker(
        journal_env,
        queue_id,
        kind="replace",
        index=1,
        when="before",
    )
    with sqlite3.connect(journal_env["db_path"]) as db:
        db.execute(
            "UPDATE import_publications"
            " SET operation_owner='live-owner',"
            " operation_expires_at=datetime('now', '+5 minutes')"
        )

    import import_publication

    monkeypatch.setattr(
        import_publication,
        "_publish_prepared_file",
        lambda *args: pytest.fail(
            "filesystem publication ran under another live owner"
        ),
    )
    assert not import_publication.publish_publication(1, "other-owner")
    assert not finals[0].exists()
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute("SELECT diagnostic FROM import_publications").fetchone() == (
            "",
        )


def test_concurrent_replay_defers_to_live_staging_worker(
    journal_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay must not remove a staging tree while its queue lease is live."""
    queue_id, series_id, _, finals = _seed_queue(journal_env, file_count=1)

    import import_execute
    import import_publication

    original_stage_files = import_execute._stage_files

    async def _scenario() -> object:
        staging_started = asyncio.Event()
        release_staging = asyncio.Event()

        async def _paused_stage_files(*args: object, **kwargs: object) -> object:
            staging_started.set()
            await release_staging.wait()
            return await original_stage_files(*args, **kwargs)

        monkeypatch.setattr(import_execute, "_stage_files", _paused_stage_files)
        monkeypatch.setattr(import_publication, "_replay_lock", None)
        monkeypatch.setattr(
            import_publication,
            "remove_staging_directory",
            lambda _publication: pytest.fail(
                "replay touched a live worker's staging directory"
            ),
        )

        worker = asyncio.create_task(import_execute._execute_import(queue_id))
        try:
            await asyncio.wait_for(staging_started.wait(), timeout=5)
            with sqlite3.connect(journal_env["db_path"]) as db:
                queue = db.execute(
                    "SELECT status, lease_owner,"
                    " lease_expires_at > CURRENT_TIMESTAMP"
                    " FROM import_queue WHERE id=?",
                    (queue_id,),
                ).fetchone()
                assert queue is not None
                assert queue[0] == "importing"
                assert queue[1]
                assert queue[2] == 1
                assert db.execute(
                    "SELECT state, operation_owner FROM import_publications"
                    " WHERE queue_id=?",
                    (queue_id,),
                ).fetchone() == ("staging", None)

            summary = await import_publication.replay_import_publications(
                max_rows=100,
                include_terminal=False,
            )
            assert summary.examined == 1
            assert summary.deferred == 1
            assert summary.aborted_staging == 0
        finally:
            release_staging.set()

        assert await asyncio.wait_for(worker, timeout=10)
        return summary

    summary = asyncio.run(_scenario())
    assert summary.deferred == 1
    assert finals[0].is_file()
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM history"
            " WHERE series_id=? AND event_type='imported'",
            (series_id,),
        ).fetchone() == (1,)


def test_expired_publishing_owner_is_taken_over(
    journal_env: dict[str, Path],
) -> None:
    queue_id, series_id, _, finals = _seed_queue(journal_env, file_count=1)
    _crash_worker(
        journal_env,
        queue_id,
        kind="replace",
        index=1,
        when="before",
    )
    with sqlite3.connect(journal_env["db_path"]) as db:
        db.execute(
            "UPDATE import_publications"
            " SET operation_owner='expired-owner',"
            " operation_expires_at=datetime('now', '-1 second')"
        )

    summary = _run_replay()
    assert summary.completed == 1
    assert finals[0].is_file()
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM history WHERE series_id=? AND event_type='imported'",
            (series_id,),
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT state, diagnostic FROM import_publications"
        ).fetchone() == ("deleted", "")


def test_replaced_move_source_is_retained_by_exact_identity_cleanup(
    journal_env: dict[str, Path],
) -> None:
    queue_id, _, sources, _ = _seed_queue(
        journal_env,
        file_count=1,
        mode="move",
    )
    _crash_worker(
        journal_env,
        queue_id,
        kind="unlink",
        mode="move",
        when="before",
    )
    replacement = sources[0].with_suffix(".replacement")
    replacement.write_bytes(b"new download content")
    os.replace(replacement, sources[0])

    _run_replay()
    assert sources[0].read_bytes() == b"new download content"
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute(
            "SELECT cleanup_state FROM import_publication_files"
        ).fetchone() == ("deleted",)


def test_mutated_move_source_is_restored_and_retained_by_hash(
    journal_env: dict[str, Path],
) -> None:
    queue_id, _, sources, finals = _seed_queue(
        journal_env,
        file_count=1,
        mode="move",
    )
    _crash_worker(
        journal_env,
        queue_id,
        kind="replace",
        mode="move",
        index=2,
        when="before",
    )
    sources[0].write_bytes(b"mutated-in-place-after-staging")

    summary = _run_replay()
    assert summary.completed == 1
    assert finals[0].is_file()
    assert sources[0].read_bytes() == b"mutated-in-place-after-staging"
    with sqlite3.connect(journal_env["db_path"]) as db:
        row = db.execute(
            "SELECT source_sha256, cleanup_state FROM import_publication_files"
        ).fetchone()
    assert len(row[0]) == 64
    assert row[1] == "replaced"


def test_cleaning_live_owner_defers_and_expired_owner_is_taken_over(
    journal_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_id, _, sources, _ = _seed_queue(
        journal_env,
        file_count=1,
        mode="move",
    )
    _crash_worker(
        journal_env,
        queue_id,
        kind="unlink",
        mode="move",
        when="before",
    )
    with sqlite3.connect(journal_env["db_path"]) as db:
        db.execute(
            "UPDATE import_publications"
            " SET operation_owner='live-cleaner',"
            " operation_expires_at=datetime('now', '+5 minutes')"
        )

    import import_publication

    real_cleanup = import_publication.cleanup_publication_filesystem
    monkeypatch.setattr(
        import_publication,
        "cleanup_publication_filesystem",
        lambda *args: pytest.fail("cleanup ran under another live owner"),
    )
    deferred = _run_replay()
    assert deferred.deferred == 1
    assert deferred.blocked == 0
    with sqlite3.connect(journal_env["db_path"]) as db:
        db.execute(
            "UPDATE import_publications"
            " SET operation_expires_at=datetime('now', '-1 second')"
        )
    monkeypatch.setattr(
        import_publication,
        "cleanup_publication_filesystem",
        real_cleanup,
    )
    completed = _run_replay()
    assert completed.completed == 1
    assert not sources[0].exists()


def test_hardlink_import_copy_on_write_does_not_rewrite_torrent_inode(
    journal_env: dict[str, Path],
) -> None:
    queue_id, _, sources, finals = _seed_queue(
        journal_env,
        file_count=1,
        mode="hardlink",
    )
    before = hashlib.sha256(sources[0].read_bytes()).digest()
    source_stat = sources[0].stat()

    import import_execute

    assert asyncio.run(import_execute._execute_import(queue_id))
    assert hashlib.sha256(sources[0].read_bytes()).digest() == before
    assert sources[0].stat().st_ino == source_stat.st_ino
    assert finals[0].stat().st_ino != source_stat.st_ino


def test_source_equal_destination_move_retains_published_file(
    journal_env: dict[str, Path],
) -> None:
    title = "Same Path"
    destination_dir = journal_env["library_root"] / title
    destination_dir.mkdir()
    source = destination_dir / "Same Path v01.cbz"
    _zip(source, b"same-path")
    _set_mode("move")
    with sqlite3.connect(journal_env["db_path"]) as db:
        db.execute(
            "INSERT INTO series(title,search_pattern,root_folder_id) VALUES(?,?,1)",
            (title, title),
        )
        series_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
        db.execute(
            "INSERT INTO volumes(series_id,volume_num,status,download_id)"
            " VALUES(?,1,'grabbed','same-path')",
            (series_id,),
        )
        db.execute(
            "INSERT INTO import_queue(series_id,download_id,torrent_name,"
            " torrent_url,volume_num,src_dir,status)"
            " VALUES(?,'same-path','Same Path','magnet:same',1,?,'pending')",
            (series_id, str(destination_dir)),
        )
        queue_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
        db.execute(
            "INSERT INTO import_queue_files(queue_id,filename,src_path,"
            " proposed_volume,file_type,status)"
            " VALUES(?,?,?,1,'volume','pending')",
            (queue_id, source.name, str(source)),
        )

    import import_execute

    assert asyncio.run(import_execute._execute_import(queue_id))
    assert source.is_file()
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute(
            "SELECT cleanup_state FROM import_publication_files"
        ).fetchone() == ("replaced",)


def test_partial_plan_round_trip_journals_needs_review_and_ranges(
    journal_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_id, series_id, _, _ = _seed_queue(
        journal_env,
        file_count=2,
        needs_review=True,
    )
    import clients
    import import_execute
    import main
    import shared
    from import_publication import load_publication
    from shared import get_db

    for config in (main.CONFIG, shared.CONFIG):
        config["remove_completed"] = "true"
        config["komga_scan_enabled"] = "false"
    with sqlite3.connect(journal_env["db_path"]) as db:
        db.execute(
            "INSERT INTO download_clients("
            " id,name,type,host,username,password,enabled,priority"
            ") VALUES(1122001,'Journal qBit','qbittorrent',"
            " 'https://qbit.test','user','password',1,1)"
        )
        db.execute(
            "UPDATE volumes SET protocol='torrent', client='qbittorrent'"
            " WHERE series_id=? AND download_id='journal-download'",
            (series_id,),
        )
        db.execute(
            "INSERT INTO seen(torrent_url,torrent_name,series_id,protocol,"
            " client,download_id) VALUES('magnet:journal','Journal batch',?,"
            " 'torrent','qbittorrent','journal-download')",
            (series_id,),
        )

    async def _must_not_remove(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        raise AssertionError("partial import removed its downloader item")

    monkeypatch.setattr(clients, "qbit_remove", _must_not_remove)
    assert asyncio.run(import_execute._execute_import(queue_id))
    with get_db() as db:
        publication = load_publication(db, queue_id=queue_id)
    assert publication is not None
    assert publication.state == "finalized"
    assert [record.plan.plan_status for record in publication.files] == [
        "ready",
        "needs_review",
    ]
    assert all(not isinstance(record, sqlite3.Row) for record in publication.files)
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute(
            "SELECT status FROM import_queue WHERE id=?",
            (queue_id,),
        ).fetchone() == ("partial",)
        assert db.execute(
            "SELECT effect_type FROM import_publication_success_effects"
            " ORDER BY effect_type"
        ).fetchall() == [("cover",)]


def test_removal_protocol_prefers_persisted_queue_snapshot(
    journal_env: dict[str, Path],
) -> None:
    """Mutable client/evidence drift cannot replace the accepted protocol."""
    import import_publication
    from shared import get_db

    with sqlite3.connect(journal_env["db_path"]) as db:
        db.execute(
            "INSERT INTO download_clients("
            "id,name,type,host,password,enabled,priority"
            ") VALUES(701001,'Persisted SAB','sabnzbd',"
            "'https://sab.invalid','secret',1,1)"
        )
        db.execute(
            """
            INSERT INTO import_publications(
                queue_id, state, owner_token, series_id, dst_dir,
                import_mode, staging_dir, queue_snapshot_json,
                series_snapshot_json, series_tags_json, queue_status,
                queue_download_id, queue_download_client_id
            ) VALUES(
                701001, 'finalized', 'owner', 1, ?, 'copy', ?,
                '{"id":701001,"download_protocol":"nzb"}',
                NULL, '[]', 'imported', 'NZO-Persisted', 701001
            )
            """,
            (
                str(journal_env["library_root"]),
                str(journal_env["library_root"] / ".mangarr-publication-701001"),
            ),
        )
        db.execute(
            "INSERT INTO seen("
            "torrent_url,torrent_name,protocol,download_id,download_client_id"
            ") VALUES('magnet:misleading','Misleading','torrent',"
            "'nzo-persisted',701001)"
        )
        publication_id = int(
            db.execute("SELECT last_insert_rowid()").fetchone()[0]
        )

    with get_db() as db:
        identity = import_publication._resolve_removal_client_identity(
            db,
            publication_id,
            "NZO-Persisted",
        )
    assert identity is not None
    assert identity.client_id == 701001
    assert identity.client_type == "sabnzbd"
    assert identity.protocol == "nzb"


@pytest.mark.parametrize(
    ("protocol", "client_type", "stored_id", "requested_id", "resolves"),
    [
        ("torrent", "qbittorrent", "abcdef", "ABCDEF", True),
        ("nzb", "sabnzbd", "NZO-Case", "nzo-case", False),
    ],
)
def test_removal_legacy_protocol_evidence_uses_downloader_id_semantics(
    journal_env: dict[str, Path],
    protocol: str,
    client_type: str,
    stored_id: str,
    requested_id: str,
    resolves: bool,
) -> None:
    import import_publication
    from shared import get_db

    client_id = 702001 if protocol == "torrent" else 702002
    queue_id = client_id
    with sqlite3.connect(journal_env["db_path"]) as db:
        db.execute(
            "INSERT INTO download_clients("
            "id,name,type,host,password,enabled,priority"
            ") VALUES(?,?,?,?,?,1,1)",
            (
                client_id,
                f"Legacy {client_type}",
                client_type,
                f"https://{client_type}.invalid",
                "secret",
            ),
        )
        db.execute(
            """
            INSERT INTO import_publications(
                queue_id, state, owner_token, series_id, dst_dir,
                import_mode, staging_dir, queue_snapshot_json,
                series_snapshot_json, series_tags_json, queue_status,
                queue_download_id, queue_download_client_id
            ) VALUES(
                ?, 'finalized', 'owner', 1, ?, 'copy', ?,
                ?, NULL, '[]', 'imported', ?, ?
            )
            """,
            (
                queue_id,
                str(journal_env["library_root"]),
                str(
                    journal_env["library_root"]
                    / f".mangarr-publication-{queue_id}"
                ),
                f'{{"id":{queue_id}}}',
                requested_id,
                client_id,
            ),
        )
        publication_id = int(
            db.execute("SELECT last_insert_rowid()").fetchone()[0]
        )
        db.execute(
            "INSERT INTO seen("
            "torrent_url,torrent_name,protocol,download_id,download_client_id"
            ") VALUES(?,?,?,?,?)",
            (
                f"https://evidence.invalid/{client_id}",
                "Legacy evidence",
                protocol,
                stored_id,
                client_id,
            ),
        )

    with get_db() as db:
        identity = import_publication._resolve_removal_client_identity(
            db,
            publication_id,
            requested_id,
        )
    assert (identity is not None) is resolves


def test_terminal_pack_failure_does_not_starve_notification_outbox(
    journal_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_id, _, _, _ = _seed_queue(journal_env, file_count=1)
    with sqlite3.connect(journal_env["db_path"]) as db:
        db.execute("UPDATE import_queue SET volume_num=1 WHERE id=?", (queue_id,))
        db.execute(
            """
            INSERT INTO notification_connections(
                name, type, enabled, settings, on_download
            ) VALUES('cleanup-observer', 'webhook', 1, '{}', 1)
            """
        )

    import import_commit
    import import_download
    import import_execute
    import import_pack_cleanup
    from routers import notification_connections

    monkeypatch.setattr(
        import_commit,
        "_mark_downloaded",
        lambda *args, **kwargs: import_download.DownloadNotificationIntent(
            title="Journal Series",
            label="Vol 1",
            cover_url="",
        ),
    )
    notification_started = threading.Event()
    attempts = 0

    async def _send(
        connection: dict[str, object],
        message: str,
        event: str = "",
        embed: dict | None = None,
    ) -> tuple[bool, str]:
        del connection, message, event, embed
        nonlocal attempts
        attempts += 1
        notification_started.set()
        return True, "sent"

    def _blocked_pack_cleanup(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        assert notification_started.wait(5), "notification was starved by pack cleanup"
        return False

    monkeypatch.setattr(
        notification_connections,
        "send_connection",
        _send,
    )
    monkeypatch.setattr(
        import_pack_cleanup,
        "cleanup_terminal_pack_staging",
        _blocked_pack_cleanup,
    )
    monkeypatch.setattr(
        import_execute,
        "cleanup_terminal_pack_staging",
        lambda *args, **kwargs: False,
    )

    assert asyncio.run(import_execute._execute_import(queue_id))
    assert attempts == 1
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute(
            "SELECT state, pack_cleanup_state, notification_state"
            " FROM import_publications"
        ).fetchone() == ("deleted", "pending", "dispatched")
        assert db.execute(
            "SELECT state, attempt_count, last_error"
            " FROM import_publication_notifications"
        ).fetchone() == ("dispatched", 1, "")
        assert db.execute(
            "SELECT state, completion_reason, attempt_count"
            " FROM import_publication_notification_deliveries"
        ).fetchone() == ("completed", "delivered", 1)


def test_partial_failure_second_attempt_retries_only_failed_connection(
    journal_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_id, _, _, _ = _seed_queue(journal_env, file_count=1)
    with sqlite3.connect(journal_env["db_path"]) as db:
        db.execute("UPDATE import_queue SET volume_num=1 WHERE id=?", (queue_id,))
        for name in ("success", "failure"):
            db.execute(
                """
                INSERT INTO notification_connections(
                    name, type, enabled, settings, on_download
                ) VALUES(?, 'webhook', 1, '{}', 1)
                """,
                (name,),
            )

    import import_commit
    import import_download
    import import_execute
    from import_publication import replay_import_publications
    from routers import notification_connections

    monkeypatch.setattr(
        import_commit,
        "_mark_downloaded",
        lambda *args, **kwargs: import_download.DownloadNotificationIntent(
            title="Journal Series",
            label="Vol 1",
            cover_url="",
        ),
    )
    attempts = {"success": 0, "failure": 0}

    async def _send(
        connection: dict[str, object],
        message: str,
        event: str = "",
        embed: dict | None = None,
    ) -> tuple[bool, str]:
        del message, event, embed
        name = str(connection["name"])
        attempts[name] += 1
        return name == "success" or attempts[name] == 2, "provider unavailable"

    monkeypatch.setattr(notification_connections, "send_connection", _send)

    assert asyncio.run(import_execute._execute_import(queue_id))
    assert attempts == {"success": 1, "failure": 1}
    with sqlite3.connect(journal_env["db_path"]) as db:
        outbox = db.execute(
            "SELECT state, attempt_count, next_attempt_at, last_error"
            " FROM import_publication_notifications"
        ).fetchone()
        deliveries = db.execute(
            """
            SELECT connection_name, state, completion_reason, attempt_count,
                   next_attempt_at, last_error
            FROM import_publication_notification_deliveries
            ORDER BY connection_name
            """
        ).fetchall()
    assert outbox is not None
    assert outbox[:2] == ("pending", 2)
    assert outbox[2] is not None
    assert outbox[3] == "NotificationDeliveryError"
    assert deliveries[0][:4] == ("failure", "pending", None, 1)
    assert deliveries[0][4] is not None
    assert deliveries[0][5] == "provider_rejected"
    assert deliveries[1] == ("success", "completed", "delivered", 1, None, "")

    with sqlite3.connect(journal_env["db_path"]) as db:
        db.execute(
            "UPDATE import_publication_notification_deliveries"
            " SET next_attempt_at=datetime('now', '-1 second')"
            " WHERE connection_name='failure'"
        )
    replayed = asyncio.run(replay_import_publications(max_rows=None))
    assert replayed.completed == 1
    assert attempts == {"success": 1, "failure": 2}
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute(
            "SELECT state, attempt_count, next_attempt_at, last_error"
            " FROM import_publication_notifications"
        ).fetchone() == ("dispatched", 3, None, "")
        assert db.execute(
            """
            SELECT connection_name, state, completion_reason, attempt_count
            FROM import_publication_notification_deliveries
            ORDER BY connection_name
            """
        ).fetchall() == [
            ("failure", "completed", "delivered", 2),
            ("success", "completed", "delivered", 1),
        ]


def test_orphaned_terminal_notification_children_remain_replayable(
    journal_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_id, _, sources, finals = _seed_queue(
        journal_env,
        file_count=1,
        mode="move",
    )
    with sqlite3.connect(journal_env["db_path"]) as db:
        db.execute("UPDATE import_queue SET volume_num=1 WHERE id=?", (queue_id,))
        db.execute(
            """
            INSERT INTO notification_connections(
                name, type, enabled, settings, on_download
            ) VALUES('orphan-replay', 'webhook', 1, '{}', 1)
            """
        )

    import import_commit
    import import_download
    import import_execute
    from import_publication import replay_import_publications
    from routers import notification_connections

    attempts = 0

    async def fail_once(
        connection: dict[str, object],
        message: str,
        event: str = "",
        embed: dict | None = None,
    ) -> tuple[bool, str]:
        del connection, message, event, embed
        nonlocal attempts
        attempts += 1
        return attempts == 2, "provider unavailable"

    monkeypatch.setattr(
        import_commit,
        "_mark_downloaded",
        lambda *args, **kwargs: import_download.DownloadNotificationIntent(
            title="Journal Series",
            label="Vol 1",
            cover_url="",
        ),
    )
    monkeypatch.setattr(notification_connections, "send_connection", fail_once)
    assert asyncio.run(import_execute._execute_import(queue_id))
    assert finals[0].is_file()
    assert not sources[0].exists()
    with sqlite3.connect(journal_env["db_path"]) as db:
        publication = db.execute("SELECT id, state FROM import_publications").fetchone()
        outbox = db.execute(
            "SELECT state, attempt_count, next_attempt_at, last_error,"
            " idempotency_key FROM import_publication_notifications"
        ).fetchone()
        delivery = db.execute(
            "SELECT state, attempt_count, next_attempt_at, last_error"
            " FROM import_publication_notification_deliveries"
        ).fetchone()
        assert (
            db.execute("SELECT 1 FROM import_queue WHERE id=?", (queue_id,)).fetchone()
            is None
        )
    assert publication[1] == "deleted"
    assert outbox[:2] == ("pending", 1)
    assert outbox[2] is not None
    assert outbox[3] == "NotificationDeliveryError"
    assert outbox[4] == f"mangarr-import-publication:{publication[0]}"
    assert delivery[:2] == ("pending", 1)
    assert delivery[2] is not None
    assert delivery[3] == "provider_rejected"

    with sqlite3.connect(journal_env["db_path"]) as db:
        db.execute(
            "UPDATE import_publication_notification_deliveries"
            " SET next_attempt_at=datetime('now', '-1 second')"
        )
        db.execute(
            "DELETE FROM import_publications WHERE id=?",
            (publication[0],),
        )
        assert db.execute(
            "SELECT state FROM import_publication_notifications WHERE publication_id=?",
            (publication[0],),
        ).fetchone() == ("pending",)
        assert db.execute(
            "SELECT state FROM import_publication_notification_deliveries"
            " WHERE publication_id=?",
            (publication[0],),
        ).fetchone() == ("pending",)
    replayed = asyncio.run(replay_import_publications(max_rows=None))
    assert replayed.completed == 1
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute(
            "SELECT state, attempt_count, last_error"
            " FROM import_publication_notifications"
        ).fetchone() == ("dispatched", 2, "")
        assert db.execute(
            "SELECT state, completion_reason, attempt_count, last_error"
            " FROM import_publication_notification_deliveries"
        ).fetchone() == ("completed", "delivered", 2, "")


def test_notification_connection_leases_are_independent_and_exclusive(
    journal_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_id, _, _, _ = _seed_queue(journal_env, file_count=1)
    with sqlite3.connect(journal_env["db_path"]) as db:
        db.execute("UPDATE import_queue SET volume_num=1 WHERE id=?", (queue_id,))
        for name in ("leased", "available"):
            db.execute(
                """
                INSERT INTO notification_connections(
                    name, type, enabled, settings, on_download
                ) VALUES(?, 'webhook', 1, '{}', 1)
                """,
                (name,),
            )

    import import_commit
    import import_download
    import import_execute
    from import_publication import _dispatch_journal_notification
    from routers import notification_connections

    async def initial_failure(
        connection: dict[str, object],
        message: str,
        event: str = "",
        embed: dict | None = None,
    ) -> tuple[bool, str]:
        del connection, message, event, embed
        return False, "initial failure"

    monkeypatch.setattr(
        import_commit,
        "_mark_downloaded",
        lambda *args, **kwargs: import_download.DownloadNotificationIntent(
            title="Journal Series",
            label="Vol 1",
            cover_url="",
        ),
    )
    monkeypatch.setattr(
        notification_connections,
        "send_connection",
        initial_failure,
    )
    assert asyncio.run(import_execute._execute_import(queue_id))
    with sqlite3.connect(journal_env["db_path"]) as db:
        publication_id = db.execute("SELECT id FROM import_publications").fetchone()[0]
        db.execute(
            "UPDATE import_publication_notification_deliveries"
            " SET state='dispatching', operation_owner='abandoned',"
            " operation_expires_at=datetime('now', '+5 minutes'),"
            " next_attempt_at=NULL WHERE connection_name='leased'"
        )
        db.execute(
            "UPDATE import_publication_notification_deliveries"
            " SET next_attempt_at=datetime('now', '-1 second')"
            " WHERE connection_name='available'"
        )

    calls = {"leased": 0, "available": 0}
    release = asyncio.Event()
    leased_started = asyncio.Event()

    async def concurrent_dispatch(
        connection: dict[str, object],
        message: str,
        event: str = "",
        embed: dict | None = None,
    ) -> tuple[bool, str]:
        del message, event, embed
        name = str(connection["name"])
        calls[name] += 1
        if name == "leased":
            leased_started.set()
            await release.wait()
        return True, "sent"

    async def scenario() -> None:
        monkeypatch.setattr(
            notification_connections,
            "send_connection",
            concurrent_dispatch,
        )
        assert not await _dispatch_journal_notification(
            publication_id,
            "available-dispatcher",
        )
        assert calls == {"leased": 0, "available": 1}
        with sqlite3.connect(journal_env["db_path"]) as db:
            db.execute(
                "UPDATE import_publication_notification_deliveries"
                " SET operation_expires_at=datetime('now', '-1 second')"
                " WHERE connection_name='leased'"
            )
        first = asyncio.create_task(
            _dispatch_journal_notification(publication_id, "dispatcher-one")
        )
        await leased_started.wait()
        assert not await _dispatch_journal_notification(
            publication_id,
            "dispatcher-two",
        )
        release.set()
        assert await first

    asyncio.run(scenario())
    assert calls == {"leased": 1, "available": 1}
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute(
            "SELECT state, operation_owner, operation_expires_at"
            " FROM import_publication_notifications"
        ).fetchone() == ("dispatched", None, None)
        assert db.execute(
            """
            SELECT connection_name, state, completion_reason, attempt_count
            FROM import_publication_notification_deliveries
            ORDER BY connection_name
            """
        ).fetchall() == [
            ("available", "completed", "delivered", 2),
            ("leased", "completed", "delivered", 2),
        ]


def test_notification_with_no_enabled_subscribed_provider_completes_immediately(
    journal_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_id, _, _, _ = _seed_queue(journal_env, file_count=1)
    with sqlite3.connect(journal_env["db_path"]) as db:
        db.execute("UPDATE import_queue SET volume_num=1 WHERE id=?", (queue_id,))
        db.execute(
            """
            INSERT INTO notification_connections(
                name, type, enabled, settings, on_download
            ) VALUES('disabled', 'webhook', 0, '{}', 1)
            """
        )
        db.execute(
            """
            INSERT INTO notification_connections(
                name, type, enabled, settings, on_download
            ) VALUES('unsubscribed', 'webhook', 1, '{}', 0)
            """
        )

    import import_commit
    import import_download
    import import_execute
    from routers import notification_connections

    monkeypatch.setattr(
        import_commit,
        "_mark_downloaded",
        lambda *args, **kwargs: import_download.DownloadNotificationIntent(
            title="Journal Series",
            label="Vol 1",
            cover_url="",
        ),
    )
    monkeypatch.setattr(
        notification_connections,
        "send_connection",
        lambda *args, **kwargs: pytest.fail("non-snapshotted provider was called"),
    )

    assert asyncio.run(import_execute._execute_import(queue_id))
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute(
            "SELECT notification_state FROM import_publications"
        ).fetchone() == ("dispatched",)
        assert db.execute(
            "SELECT state, attempt_count, dispatched_at"
            " FROM import_publication_notifications"
        ).fetchone()[:2] == ("dispatched", 0)
        assert db.execute(
            "SELECT COUNT(*) FROM import_publication_notification_deliveries"
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    ("connection_change", "completion_reason"),
    (
        ("delete", "connection_deleted"),
        ("disable", "connection_disabled"),
    ),
)
def test_failed_delivery_settles_when_connection_is_deleted_or_disabled(
    journal_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    connection_change: str,
    completion_reason: str,
) -> None:
    queue_id, _, _, _ = _seed_queue(journal_env, file_count=1)
    with sqlite3.connect(journal_env["db_path"]) as db:
        db.execute("UPDATE import_queue SET volume_num=1 WHERE id=?", (queue_id,))
        db.execute(
            """
            INSERT INTO notification_connections(
                name, type, enabled, settings, on_download
            ) VALUES('mutable', 'webhook', 1, '{}', 1)
            """
        )
        connection_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])

    import import_commit
    import import_download
    import import_execute
    from import_publication import replay_import_publications
    from routers import notification_connections

    monkeypatch.setattr(
        import_commit,
        "_mark_downloaded",
        lambda *args, **kwargs: import_download.DownloadNotificationIntent(
            title="Journal Series",
            label="Vol 1",
            cover_url="",
        ),
    )
    calls = 0

    async def _fail(
        connection: dict[str, object],
        message: str,
        event: str = "",
        embed: dict | None = None,
    ) -> tuple[bool, str]:
        del connection, message, event, embed
        nonlocal calls
        calls += 1
        return False, "provider unavailable"

    monkeypatch.setattr(notification_connections, "send_connection", _fail)
    assert asyncio.run(import_execute._execute_import(queue_id))
    assert calls == 1

    with sqlite3.connect(journal_env["db_path"]) as db:
        if connection_change == "delete":
            db.execute(
                "DELETE FROM notification_connections WHERE id=?",
                (connection_id,),
            )
        else:
            db.execute(
                "UPDATE notification_connections SET enabled=0 WHERE id=?",
                (connection_id,),
            )
        db.execute(
            "UPDATE import_publication_notification_deliveries"
            " SET next_attempt_at=datetime('now', '-1 second')"
        )

    replayed = asyncio.run(replay_import_publications(max_rows=None))
    assert replayed.completed == 1
    assert calls == 1
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute(
            """
            SELECT connection_id, connection_name, connection_type, state,
                   completion_reason, attempt_count, last_error
            FROM import_publication_notification_deliveries
            """
        ).fetchone() == (
            connection_id,
            "mutable",
            "webhook",
            "completed",
            completion_reason,
            2,
            "",
        )
        assert db.execute(
            "SELECT state, attempt_count, last_error"
            " FROM import_publication_notifications"
        ).fetchone() == ("dispatched", 2, "")


def test_fresh_schema_has_per_connection_notification_delivery_state(
    journal_env: dict[str, Path],
) -> None:
    with sqlite3.connect(journal_env["db_path"]) as db:
        columns = {
            str(row[1])
            for row in db.execute(
                "PRAGMA table_info(import_publication_notification_deliveries)"
            ).fetchall()
        }
        indexes = {
            str(row[1])
            for row in db.execute(
                "PRAGMA index_list(import_publication_notification_deliveries)"
            ).fetchall()
        }
        version = int(db.execute("PRAGMA user_version").fetchone()[0])

    assert {
        "publication_id",
        "connection_id",
        "connection_name",
        "connection_type",
        "state",
        "completion_reason",
        "operation_owner",
        "operation_expires_at",
        "attempt_count",
        "next_attempt_at",
        "last_error",
        "completed_at",
    } <= columns
    assert "settings" not in columns
    assert "idx_import_publication_notification_deliveries_due" in indexes
    assert version == 5


def test_v3_notification_migration_snapshots_ids_and_display_metadata_only(
    journal_env: dict[str, Path],
) -> None:
    publication_id = _seed_minimal_publications(
        journal_env,
        ["deleted"],
        first_queue_id=30_000,
    )[0]
    secret_canary = "PLAINTEXT-NOTIFICATION-SECRET-CANARY"
    with sqlite3.connect(journal_env["db_path"]) as db:
        db.execute(
            """
            INSERT INTO notification_connections(
                name, type, enabled, settings, on_download
            ) VALUES('migration-target', 'webhook', 1, ?, 1)
            """,
            (f'{{"token":"{secret_canary}"}}',),
        )
        connection_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
        db.execute(
            """
            UPDATE import_publications
            SET notification_state='pending',
                notification_title='Migrated title',
                notification_label='Migrated label',
                notification_cover_url='https://example.invalid/cover.jpg'
            WHERE id=?
            """,
            (publication_id,),
        )
        db.execute("DROP TABLE import_publication_notification_deliveries")
        db.execute("DROP TABLE import_publication_notifications")
        db.execute("PRAGMA user_version=3")

    import main

    main.init_db()

    with sqlite3.connect(journal_env["db_path"]) as db:
        db.row_factory = sqlite3.Row
        parent = dict(
            db.execute(
                "SELECT * FROM import_publication_notifications WHERE publication_id=?",
                (publication_id,),
            ).fetchone()
        )
        delivery = dict(
            db.execute(
                "SELECT * FROM import_publication_notification_deliveries"
                " WHERE publication_id=?",
                (publication_id,),
            ).fetchone()
        )
        version = int(db.execute("PRAGMA user_version").fetchone()[0])

    assert parent["state"] == "pending"
    assert delivery["connection_id"] == connection_id
    assert delivery["connection_name"] == "migration-target"
    assert delivery["connection_type"] == "webhook"
    assert delivery["state"] == "pending"
    assert "settings" not in delivery
    assert secret_canary not in repr(delivery)
    assert version == 5


def test_startup_drain_ignores_terminal_work_and_pages_past_blocked_ids(
    journal_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_publication

    terminal_ids = _seed_minimal_publications(
        journal_env,
        ["finalized", "deleted", "finalized"],
    )
    active_ids = _seed_minimal_publications(
        journal_env,
        ["prepared"] * 105,
        first_queue_id=20_000,
    )
    calls: list[tuple[int, bool]] = []

    async def _blocked(
        publication_id: int,
        owner_token: str | None = None,
        *,
        process_terminal: bool = True,
    ) -> bool:
        del owner_token
        calls.append((publication_id, process_terminal))
        return False

    monkeypatch.setattr(import_publication, "complete_publication", _blocked)
    monkeypatch.setattr(import_publication, "_replay_lock", None)

    summary = asyncio.run(
        import_publication.drain_active_import_publications(page_size=100)
    )

    assert summary.examined == 105
    assert summary.blocked == 105
    assert summary.last_id == active_ids[-1]
    assert [publication_id for publication_id, _ in calls] == active_ids
    assert all(not process_terminal for _, process_terminal in calls)
    assert set(terminal_ids).isdisjoint(publication_id for publication_id, _ in calls)


def test_replay_cancellation_settles_only_one_inflight_id(
    journal_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_publication

    publication_ids = _seed_minimal_publications(
        journal_env,
        ["prepared", "prepared"],
    )
    monkeypatch.setattr(import_publication, "_replay_lock", None)

    async def _scenario() -> list[int]:
        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[int] = []

        async def _complete(
            publication_id: int,
            owner_token: str | None = None,
            *,
            process_terminal: bool = True,
        ) -> bool:
            del owner_token, process_terminal
            calls.append(publication_id)
            started.set()
            await release.wait()
            return True

        monkeypatch.setattr(
            import_publication,
            "complete_publication",
            _complete,
        )
        replay = asyncio.create_task(
            import_publication.replay_import_publications(
                max_rows=100,
                include_terminal=False,
            )
        )
        await started.wait()
        replay.cancel()
        await asyncio.sleep(0)
        assert not replay.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await replay
        return calls

    assert asyncio.run(_scenario()) == [publication_ids[0]]


def test_replay_all_snapshot_and_keyset_reach_ids_after_first_hundred(
    journal_env: dict[str, Path],
) -> None:
    from import_publication import pending_publication_ids, replay_import_publications

    dst_dir = journal_env["library_root"] / "bulk-replay"
    with sqlite3.connect(journal_env["db_path"]) as db:
        db.execute(
            "INSERT INTO series(title,search_pattern,root_folder_id)"
            " VALUES('Bulk Replay','Bulk Replay',1)"
        )
        series_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        for index in range(105):
            db.execute(
                "INSERT INTO import_queue(series_id,download_id,torrent_name,"
                " src_dir,status) VALUES(?,?,?,?, 'importing')",
                (
                    series_id,
                    f"bulk-{index}",
                    f"Bulk {index}",
                    str(journal_env["source_root"]),
                ),
            )
            queue_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            staging_dir = dst_dir / f".mangarr-publication-{queue_id}"
            db.execute(
                """
                INSERT INTO import_publications(
                    queue_id, state, owner_token, series_id, dst_dir,
                    import_mode, staging_dir, queue_snapshot_json,
                    series_snapshot_json, series_tags_json, queue_status
                ) VALUES(?, 'staging', 'bulk-owner', ?, ?, 'copy', ?,
                         ?, NULL, '[]', 'importing')
                """,
                (
                    queue_id,
                    series_id,
                    str(dst_dir),
                    str(staging_dir),
                    f'{{"id":{queue_id}}}',
                ),
            )
        first_page = pending_publication_ids(db, 100)
        second_page = pending_publication_ids(
            db,
            100,
            after_id=first_page[-1],
        )

    assert len(first_page) == 100
    assert len(second_page) == 5
    assert second_page[0] > first_page[-1]
    summary = asyncio.run(replay_import_publications(max_rows=None))
    assert summary.examined == 105
    assert summary.aborted_staging == 105
    assert summary.last_id == second_page[-1]


def test_concurrent_replay_produces_one_domain_history(
    journal_env: dict[str, Path],
) -> None:
    queue_id, series_id, _, _ = _seed_queue(journal_env)
    _crash_worker(
        journal_env,
        queue_id,
        kind="replace",
        index=1,
        when="after",
    )
    from import_publication import replay_import_publications

    async def replay_twice() -> None:
        await asyncio.gather(
            replay_import_publications(max_rows=100),
            replay_import_publications(max_rows=100),
        )

    asyncio.run(replay_twice())
    _assert_exactly_once(journal_env, series_id)


def test_subprocess_concurrent_replay_has_one_history_and_no_false_block(
    journal_env: dict[str, Path],
) -> None:
    queue_id, series_id, _, _ = _seed_queue(journal_env)
    _crash_worker(
        journal_env,
        queue_id,
        kind="replace",
        index=1,
        when="after",
    )
    start_file = journal_env["db_path"].with_suffix(".start")
    child_env = dict(os.environ)
    child_env.update(
        {
            "PYTHONPATH": str(Path.cwd() / "app"),
            "JOURNAL_DB": str(journal_env["db_path"]),
            "JOURNAL_START_FILE": str(start_file),
        }
    )
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", _REPLAY_CHILD],
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    start_file.write_text("go")
    results = [worker.communicate(timeout=30) for worker in workers]

    assert [worker.returncode for worker in workers] == [0, 0], results
    _assert_exactly_once(journal_env, series_id)
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM import_publication_files"
            " WHERE publish_state='blocked'"
        ).fetchone() == (0,)
        assert db.execute("SELECT diagnostic FROM import_publications").fetchone() == (
            "",
        )
