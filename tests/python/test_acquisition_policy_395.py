"""Acquisition intent survives disposable history and import recovery."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from test_cross_client_download_ownership import (
    _Response,
    ownership_env as ownership_env,
)
from test_grab_monitoring_380 import (
    _archive,
    commit_pack_env as commit_pack_env,
    release,
)


@pytest.fixture(autouse=True)
def _restore_prior_secret_cipher(monkeypatch: pytest.MonkeyPatch) -> None:
    import security

    # Imported ownership fixtures assign this cache directly; pytest restores it.
    monkeypatch.setattr(security, "_SECRET_CIPHER", security._SECRET_CIPHER)


def _grab(protocol: str, *, manual: bool = False) -> None:
    import grab_core
    from clients import GrabResult

    client = "sabnzbd" if protocol == "nzb" else "qbittorrent"
    download_id = "NZO-pack" if protocol == "nzb" else "PACK-HASH"
    assert isinstance(grab_core.grab_url, AsyncMock)
    grab_core.grab_url.return_value = GrabResult(True, client, download_id, True, 7)
    item = release("Test Series v01-v03")
    item["protocol"] = protocol
    assert asyncio.run(grab_core.grab_item(item, 1, respect_monitoring=not manual))


def _delete_history() -> None:
    import shared
    from routers import api_v1

    with shared.get_db() as db:
        ids = [
            row[0]
            for row in db.execute("SELECT id FROM history WHERE event_type='grabbed'")
        ]
    for history_id in ids:
        response = asyncio.run(api_v1.api_v1_history_delete(history_id))
        assert response.status_code == 200
        assert json.loads(bytes(response.body))["ok"] is True
    with shared.get_db() as db:
        assert (
            db.execute("SELECT 1 FROM history WHERE event_type='grabbed'").fetchone()
            is None
        )


def _prepare(paths: dict[str, Path]) -> dict[int, tuple[Path, bytes, dict[str, Any]]]:
    import shared
    from files import build_filename
    from rescan import _series_library_dir

    protected = {}
    with shared.get_db() as db:
        directory = _series_library_dir(db, 1)
        assert directory is not None
        for num in (1, 2, 3):
            _archive(paths["downloads"] / f"Test Series v0{num}.cbz")
        for num in (2, 3):
            destination = Path(directory) / build_filename(
                "Test Series", float(num), f"Test Series v0{num}.cbz"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(destination, "w") as archive:
                archive.writestr("001.jpg", f"protected page {num}".encode())
            db.execute(
                "UPDATE volumes SET status='downloaded',quality=NULL,import_path=?,"
                "source_url='https://other',download_id='other-id',"
                "download_client_id=42,protocol='torrent' WHERE id=?",
                (str(destination), num),
            )
            row = dict(
                db.execute("SELECT * FROM volumes WHERE id=?", (num,)).fetchone()
            )
            protected[num] = (destination, destination.read_bytes(), row)
    return protected


def _discover(
    paths: dict[str, Path], protocol: str, monkeypatch: pytest.MonkeyPatch
) -> int:
    import import_discovery as discovery
    import shared

    if protocol == "nzb":
        ids = discovery._sab_process_sync(
            {"NZO-pack": {"storage": str(paths["downloads"])}},
            {"NZO-pack"},
            "http://sab.invalid",
            download_client_id=7,
            include_legacy_ownerless=False,
        )
    else:

        class Client:
            def __init__(self, **kwargs: object) -> None:
                pass

            async def __aenter__(self) -> Client:
                return self

            async def __aexit__(self, *args: object) -> None:
                pass

            async def post(self, *args: object, **kwargs: object) -> _Response:
                return _Response(text="Ok.")

            async def get(self, *args: object, **kwargs: object) -> _Response:
                return _Response(
                    data=[
                        {
                            "hash": "pack-hash",
                            "name": "Test Series v01-v03",
                            "progress": 1.0,
                            "content_path": str(paths["downloads"]),
                        }
                    ]
                )

        ids = []
        monkeypatch.setattr(discovery.httpx, "AsyncClient", Client)
        monkeypatch.setattr(discovery, "schedule_import_worker", ids.append)
        asyncio.run(
            discovery._poll_qbit_partition(
                discovery._ClientPollPartition(7, "fixture", "qbittorrent", False),
                {"host": "http://qbit.invalid", "category": "manga"},
            )
        )
    assert len(ids) == 1
    with shared.get_db() as db:
        assert tuple(db.execute("SELECT COUNT(*) FROM import_queue").fetchone()) == (1,)
    return ids[0]


def _queue(paths: dict[str, Path], protocol: str, *, manual: bool = False) -> int:
    import import_queue
    import shared

    with shared.get_db() as db:
        queue_id, review = import_queue._queue_import(
            db,
            1,
            "NZO-pack" if protocol == "nzb" else "PACK-HASH",
            "Test Series v01-v03",
            "https://indexer.test/release",
            None,
            str(paths["downloads"]),
            download_client_id=7,
            protocol=protocol,
            respect_grab_claims=False if manual else None,
        )
    assert queue_id is not None and not review
    return queue_id


def _assert_protected(protected: dict[int, tuple[Path, bytes, dict[str, Any]]]) -> None:
    import shared

    with shared.get_db() as db:
        for num, (destination, before_bytes, before_row) in protected.items():
            assert destination.read_bytes() == before_bytes
            assert (
                dict(db.execute("SELECT * FROM volumes WHERE id=?", (num,)).fetchone())
                == before_row
            )


@pytest.mark.parametrize("protocol", ["torrent", "nzb"])
@pytest.mark.parametrize("boundary", ["before_queue", "after_queue"])
def test_history_cleanup_and_restart_preserve_automatic_nonclaims(
    commit_pack_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    boundary: str,
) -> None:
    import import_execute
    import shared

    protected = _prepare(commit_pack_env)
    _grab(protocol)
    if boundary == "before_queue":
        _delete_history()
    queue_id = _discover(commit_pack_env, protocol, monkeypatch)
    if boundary == "after_queue":
        _delete_history()
        # Queue policy remains authoritative after acquisition cleanup too.
        with shared.get_db() as db:
            db.execute("DELETE FROM seen")
    with shared.get_db() as db:
        assert tuple(
            db.execute(
                "SELECT respect_grab_claims FROM import_queue WHERE id=?", (queue_id,)
            ).fetchone()
        ) == (1,)
    if boundary == "after_queue":
        script = """
import asyncio, sys
import conftest
import main, shared, import_execute
main.DB_PATH = shared.DB_PATH = sys.argv[1]
for cfg in (main.CONFIG, shared.CONFIG):
    cfg.update(save_path=sys.argv[2], import_mode='copy', remove_completed='false')
assert asyncio.run(import_execute._execute_import(int(sys.argv[3])))
"""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(commit_pack_env["db_path"]),
                str(commit_pack_env["library"]),
                str(queue_id),
            ],
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    (
                        str(Path(__file__).parents[2] / "app"),
                        str(Path(__file__).parent),
                    )
                ),
            },
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert asyncio.run(import_execute._execute_import(queue_id))
    _assert_protected(protected)
    with shared.get_db() as db:
        snapshot = json.loads(
            db.execute(
                "SELECT queue_snapshot_json FROM import_publications WHERE queue_id=?",
                (queue_id,),
            ).fetchone()[0]
        )
    assert snapshot["respect_grab_claims"] == 1
    assert snapshot["_respect_grab_claims"] is True


@pytest.mark.parametrize("protocol", ["torrent", "nzb"])
@pytest.mark.parametrize("manual", ["grab", "queue", "mapping"])
def test_explicit_manual_intent_and_only_selected_mapping_survive_cleanup(
    commit_pack_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    manual: str,
) -> None:
    import import_execute
    import shared

    protected = _prepare(commit_pack_env)
    if manual != "queue":
        _grab(protocol, manual=manual == "grab")
    _delete_history()
    queue_id = _queue(commit_pack_env, protocol, manual=manual == "queue")
    with shared.get_db() as db:
        file_id = db.execute(
            "SELECT id FROM import_queue_files WHERE queue_id=? AND proposed_volume=2",
            (queue_id,),
        ).fetchone()[0]
    assert asyncio.run(
        import_execute._execute_import(
            queue_id,
            volume_overrides={file_id: 2.0} if manual == "mapping" else None,
        )
    )
    destination = protected[2][0]
    with zipfile.ZipFile(destination) as archive:
        assert archive.read("001.jpg") == b"page"
    assert destination.read_bytes() != protected[2][1]
    if manual == "mapping":
        _assert_protected({3: protected[3]})


def test_schema_nullable_checked_policy_and_idempotent_upgrade(
    commit_pack_env: dict[str, Path],
) -> None:
    import main
    import shared

    _prepare(commit_pack_env)
    _grab("nzb")
    with shared.get_db() as db:
        for table in ("seen", "import_queue"):
            columns = {row[1]: row for row in db.execute(f"PRAGMA table_info({table})")}
            assert "respect_grab_claims" in columns
            assert columns["respect_grab_claims"][3:5] == (0, None)
        db.execute("UPDATE seen SET respect_grab_claims=0")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE seen SET respect_grab_claims=2")
        db.execute(
            "INSERT INTO import_queue(series_id,respect_grab_claims) VALUES(1,NULL)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE import_queue SET respect_grab_claims=-1")
    main.init_db()
    with shared.get_db() as db:
        assert tuple(db.execute("SELECT respect_grab_claims FROM seen").fetchone()) == (
            0,
        )
        assert tuple(
            db.execute("SELECT respect_grab_claims FROM import_queue").fetchone()
        ) == (None,)


@pytest.mark.parametrize("manual", [False, True])
def test_grab_policy_commits_before_disposable_history_write(
    commit_pack_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    manual: bool,
) -> None:
    import grab_core
    import shared

    def failed_history(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected history failure")

    monkeypatch.setattr(grab_core, "add_history", failed_history)
    with pytest.raises(RuntimeError, match="injected history failure"):
        _grab("nzb", manual=manual)
    with shared.get_db() as db:
        assert tuple(db.execute("SELECT respect_grab_claims FROM seen").fetchone()) == (
            int(not manual),
        )
        assert (
            db.execute("SELECT 1 FROM history WHERE event_type='grabbed'").fetchone()
            is None
        )


def test_untracked_acceptance_is_constrained_even_for_manual_selection(
    commit_pack_env: dict[str, Path],
) -> None:
    import grab_core
    import shared
    from clients import GrabResult

    assert isinstance(grab_core.grab_url, AsyncMock)
    grab_core.grab_url.return_value = GrabResult(False, "qbittorrent", None, True, 7)
    assert not asyncio.run(
        grab_core.grab_item(release("Test Series v01-v03"), 1, respect_monitoring=False)
    )
    with shared.get_db() as db:
        assert tuple(
            db.execute("SELECT download_id,respect_grab_claims FROM seen").fetchone()
        ) == (None, 1)


def test_manual_claim_lost_during_acceptance_retains_constraint(
    commit_pack_env: dict[str, Path],
) -> None:
    import grab_core
    import shared
    from clients import GrabResult

    async def accepted(*args: object, **kwargs: object) -> GrabResult:
        with shared.get_db() as db:
            db.execute(
                "UPDATE volumes SET status='grabbed',download_client_id=42,download_id='other' WHERE id=1"
            )
        return GrabResult(True, "sabnzbd", "late-id", True, 7)

    assert isinstance(grab_core.grab_url, AsyncMock)
    grab_core.grab_url.side_effect = accepted
    item = release("Test Series v01")
    item["protocol"] = "nzb"
    assert not asyncio.run(grab_core.grab_item(item, 1, respect_monitoring=False))
    _delete_history()
    with shared.get_db() as db:
        assert tuple(db.execute("SELECT respect_grab_claims FROM seen").fetchone()) == (
            1,
        )
        assert tuple(
            db.execute(
                "SELECT download_client_id,download_id FROM volumes WHERE id=1"
            ).fetchone()
        ) == (42, "other")


def test_seen_collision_during_acceptance_does_not_acquire_or_borrow_manual_claims(
    commit_pack_env: dict[str, Path],
) -> None:
    import grab_core
    import shared
    from clients import GrabResult

    with shared.get_db() as db:
        before = [tuple(row) for row in db.execute("SELECT * FROM volumes ORDER BY id")]

    async def accepted(*args: object, **kwargs: object) -> GrabResult:
        with shared.get_db() as db:
            db.execute(
                "INSERT INTO seen(torrent_url,series_id,download_id,download_client_id,protocol,"
                "respect_grab_claims) VALUES('https://indexer.test/release',1,'other',42,'nzb',0)"
            )
        return GrabResult(True, "sabnzbd", "NZO-pack", True, 7)

    assert isinstance(grab_core.grab_url, AsyncMock)
    grab_core.grab_url.side_effect = accepted
    item = release("Test Series v01-v03")
    item["protocol"] = "nzb"
    assert not asyncio.run(grab_core.grab_item(item, 1, respect_monitoring=False))
    with shared.get_db() as db:
        assert [
            tuple(row) for row in db.execute("SELECT * FROM volumes ORDER BY id")
        ] == before
        assert tuple(
            db.execute(
                "SELECT download_id,download_client_id,respect_grab_claims FROM seen"
            ).fetchone()
        ) == ("other", 42, 0)


def test_manual_torrent_discovery_copies_intent_across_hash_case_normalization(
    commit_pack_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shared

    _prepare(commit_pack_env)
    _grab("torrent", manual=True)
    _delete_history()
    queue_id = _discover(commit_pack_env, "torrent", monkeypatch)
    with shared.get_db() as db:
        assert tuple(
            db.execute(
                "SELECT download_id,respect_grab_claims FROM import_queue WHERE id=?",
                (queue_id,),
            ).fetchone()
        ) == ("pack-hash", 0)


def test_legacy_manual_evidence_is_frozen_before_publication_and_log_cleanup(
    commit_pack_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_execute
    import import_publication as publication
    import shared

    protected = _prepare(commit_pack_env)
    _grab("nzb", manual=True)
    queue_id = _discover(commit_pack_env, "nzb", monkeypatch)
    with shared.get_db() as db:
        db.execute("UPDATE seen SET respect_grab_claims=NULL")
        db.execute("UPDATE import_queue SET respect_grab_claims=NULL")

    async def defer(*args: object, **kwargs: object) -> bool:
        return False

    with monkeypatch.context() as patch:
        patch.setattr(import_execute, "complete_publication", defer)
        assert not asyncio.run(import_execute._execute_import(queue_id))
    _delete_history()
    with shared.get_db() as db:
        db.execute("DELETE FROM seen")
        row = db.execute(
            "SELECT id,queue_snapshot_json FROM import_publications WHERE queue_id=?",
            (queue_id,),
        ).fetchone()
        assert json.loads(row[1])["respect_grab_claims"] == 0
    assert asyncio.run(publication.complete_publication(row[0], "replay-owner"))
    for destination, before_bytes, _ in protected.values():
        with zipfile.ZipFile(destination) as archive:
            assert archive.read("001.jpg") == b"page"
        assert destination.read_bytes() != before_bytes


def test_upgrade_keeps_legacy_rows_unknown_without_history_backfill(
    commit_pack_env: dict[str, Path],
) -> None:
    import main
    import shared

    _grab("nzb", manual=True)
    with shared.get_db() as db:
        db.execute("INSERT INTO import_queue(series_id) VALUES(1)")
        for table in ("seen", "import_queue"):
            db.execute(f"ALTER TABLE {table} DROP COLUMN respect_grab_claims")
    main.init_db()
    with shared.get_db() as db:
        for table in ("seen", "import_queue"):
            assert tuple(
                db.execute(f"SELECT respect_grab_claims FROM {table}").fetchone()
            ) == (None,)


def test_queue_policy_read_is_atomic_without_writer_held_filesystem_io(
    commit_pack_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_queue
    import import_execute
    import import_publication

    _prepare(commit_pack_env)
    _grab("nzb")
    _delete_history()
    reads = []
    real_policy = import_queue.acquisition_policy

    def policy(db: sqlite3.Connection, **kwargs: Any) -> int | None:
        assert db.in_transaction
        reads.append(True)
        return real_policy(db, **kwargs)

    monkeypatch.setattr(import_queue, "acquisition_policy", policy)
    calls = []
    for name in ("scandir", "rename", "link"):
        real = getattr(os, name)

        def checked(
            *args: Any, _name: str = name, _real: Any = real, **kwargs: Any
        ) -> Any:
            with sqlite3.connect(commit_pack_env["db_path"], timeout=0) as db:
                db.execute("BEGIN IMMEDIATE")
                db.rollback()
            calls.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(os, name, checked)
    real_rename = import_publication._rename_noreplace

    def checked_native_rename(*args: Any, **kwargs: Any) -> None:
        with sqlite3.connect(commit_pack_env["db_path"], timeout=0) as db:
            db.execute("BEGIN IMMEDIATE")
            db.rollback()
        calls.append("native_rename")
        real_rename(*args, **kwargs)

    monkeypatch.setattr(import_publication, "_rename_noreplace", checked_native_rename)
    queue_id = _queue(commit_pack_env, "nzb")
    assert reads == [True]
    assert asyncio.run(import_execute._execute_import(queue_id))
    assert "scandir" in calls and "native_rename" in calls


@pytest.mark.parametrize(
    "data,protected",
    [
        ('{"respect_monitoring":false}', False),
        ('{"respect_monitoring":true}', True),
        ('{"respect_monitoring":false,"claim_lost":true}', True),
        ('{"respect_monitoring":0}', True),
        ("{}", True),
        ("broken", True),
        ('{"respect_monitoring":false,"respect_monitoring":false}', True),
        (None, True),
    ],
)
def test_legacy_history_is_exact_explicit_and_conservative(
    commit_pack_env: dict[str, Path],
    data: str | None,
    protected: bool,
) -> None:
    import import_plan
    import shared

    _grab("nzb")
    with shared.get_db() as db:
        db.execute("UPDATE seen SET respect_grab_claims=NULL")
        if data is None:
            db.execute("DELETE FROM history")
        else:
            db.execute("UPDATE history SET data=? WHERE event_type='grabbed'", (data,))
        queue = {
            "series_id": 1,
            "download_id": "NZO-pack",
            "download_client_id": 7,
            "download_protocol": "nzb",
            "torrent_url": "https://indexer.test/release",
        }
        assert import_plan._automatic_grab_import(db, queue) is protected


@pytest.mark.parametrize(
    "collision", ["owner", "source", "protocol", "nzb_case", "conflict"]
)
def test_manual_history_cannot_lend_authority_to_another_identity(
    commit_pack_env: dict[str, Path],
    collision: str,
) -> None:
    import import_plan
    import shared

    _grab("nzb", manual=True)
    with shared.get_db() as db:
        db.execute("UPDATE seen SET respect_grab_claims=NULL")
        queue = {
            "series_id": 1,
            "download_id": "NZO-pack",
            "download_client_id": 7,
            "download_protocol": "nzb",
            "torrent_url": "https://indexer.test/release",
        }
        if collision == "conflict":
            db.execute(
                "INSERT INTO history(event_type,series_id,download_id,download_client_id,"
                "protocol,torrent_url,data) VALUES('grabbed',1,'NZO-pack',7,'nzb',?,'{}')",
                (queue["torrent_url"],),
            )
        else:
            field, value = {
                "owner": ("download_client_id", 8),
                "source": ("torrent_url", "other"),
                "protocol": ("download_protocol", "torrent"),
                "nzb_case": ("download_id", "nzo-pack"),
            }[collision]
            queue[field] = value
        assert import_plan._automatic_grab_import(db, queue) is True


@pytest.mark.parametrize("state", ["prepared", "publishing", "published"])
@pytest.mark.parametrize("entry", ["complete", "publish"])
def test_ambiguous_old_manual_snapshot_retains_files_and_domain_rows(
    commit_pack_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    entry: str,
) -> None:
    import import_execute
    import import_publication as publication
    import shared

    protected = _prepare(commit_pack_env)
    _grab("nzb")
    queue_id = _discover(commit_pack_env, "nzb", monkeypatch)

    async def defer(*args: object, **kwargs: object) -> bool:
        return False

    with monkeypatch.context() as patch:
        patch.setattr(import_execute, "complete_publication", defer)
        assert not asyncio.run(import_execute._execute_import(queue_id))
    if state == "published":
        with shared.get_db() as db:
            publication_id = db.execute(
                "SELECT id FROM import_publications WHERE queue_id=?", (queue_id,)
            ).fetchone()[0]
        assert publication.publish_publication(publication_id, "setup-publish-owner")
    with shared.get_db() as db:
        row = db.execute(
            "SELECT id,queue_snapshot_json FROM import_publications WHERE queue_id=?",
            (queue_id,),
        ).fetchone()
        publication_id = row[0]
        snapshot = json.loads(row[1])
        snapshot.pop("respect_grab_claims", None)
        snapshot["_respect_grab_claims"] = False
        db.execute(
            "UPDATE import_publications SET queue_snapshot_json=?,state=?,operation_owner=NULL,"
            "operation_expires_at=NULL WHERE id=?",
            (json.dumps(snapshot), state, publication_id),
        )
        db.execute("UPDATE import_queue SET respect_grab_claims=NULL")
        db.execute("UPDATE seen SET respect_grab_claims=NULL")
        db.execute("DELETE FROM history WHERE event_type='grabbed'")
        domain_before = [
            tuple(row) for row in db.execute("SELECT * FROM volumes ORDER BY id")
        ]
        child_before = [
            tuple(row) for row in db.execute("SELECT * FROM import_publication_files")
        ]
    before_files = {
        str(path): path.read_bytes()
        for path in commit_pack_env["library"].rglob("*")
        if path.is_file()
    }
    if entry == "publish":
        assert not publication.publish_publication(publication_id, "retry-owner")
    else:
        assert not asyncio.run(
            publication.complete_publication(publication_id, "retry-owner")
        )
    assert before_files == {
        str(path): path.read_bytes()
        for path in commit_pack_env["library"].rglob("*")
        if path.is_file()
    }
    with shared.get_db() as db:
        assert [
            tuple(row) for row in db.execute("SELECT * FROM volumes ORDER BY id")
        ] == domain_before
        assert [
            tuple(row) for row in db.execute("SELECT * FROM import_publication_files")
        ] == child_before
        header = db.execute(
            "SELECT state,queue_snapshot_json,diagnostic FROM import_publications WHERE id=?",
            (publication_id,),
        ).fetchone()
        assert header[0] == state
        assert json.loads(header[1]) == snapshot
        assert "manual" in header[2]
    _assert_protected(protected)


def test_old_terminal_receipt_is_not_reinterpreted_or_rewritten(
    commit_pack_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_execute
    import import_publication as publication
    import shared

    _prepare(commit_pack_env)
    _grab("nzb")
    queue_id = _discover(commit_pack_env, "nzb", monkeypatch)
    assert asyncio.run(import_execute._execute_import(queue_id))
    _delete_history()
    with shared.get_db() as db:
        row = db.execute(
            "SELECT id,queue_snapshot_json FROM import_publications WHERE queue_id=?",
            (queue_id,),
        ).fetchone()
        snapshot = json.loads(row[1])
        snapshot.pop("respect_grab_claims")
        snapshot["_respect_grab_claims"] = False
        db.execute(
            "UPDATE import_publications SET queue_snapshot_json=? WHERE id=?",
            (json.dumps(snapshot), row[0]),
        )
        db.execute("DELETE FROM seen")
        before = tuple(
            db.execute(
                "SELECT state,queue_snapshot_json,result_ok,result_imported_count,result_queue_status,diagnostic FROM import_publications WHERE id=?",
                (row[0],),
            ).fetchone()
        )
    assert asyncio.run(publication.complete_publication(row[0], process_terminal=False))
    with shared.get_db() as db:
        assert (
            tuple(
                db.execute(
                    "SELECT state,queue_snapshot_json,result_ok,result_imported_count,result_queue_status,diagnostic FROM import_publications WHERE id=?",
                    (row[0],),
                ).fetchone()
            )
            == before
        )
