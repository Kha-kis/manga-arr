"""Generated sources use durable admission, not fresh pathname fingerprints."""

from __future__ import annotations

import copy
import importlib
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from file_mutation_lock import file_mutation_guard
from private_file_claim import FullFileFingerprint
from private_pack_claim import PackOwnership, PackProofError
from test_import_pack_cleanup_durability import _PackEnv, pack_env  # noqa: F401
from test_import_pack_nfs_lifecycle import _queue_images
from test_import_pack_private_placement import _provision
from test_import_pack_protocol import _fallback


@pytest.fixture(params=[False, True], ids=["native", "private"])
def admitted_queue(
    pack_env: _PackEnv, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> tuple[_PackEnv, dict[str, Any], dict[str, Any]]:
    import import_lease
    import main

    _provision(pack_env)
    if request.param:
        _fallback(monkeypatch)
    queue_id = _queue_images(pack_env, "source-adapter")
    assert queue_id is not None
    with file_mutation_guard(pack_env["db_path"]), main.get_db() as db:
        assert import_lease.claim_import_queue_row(db, queue_id, "source-executor")
        queue = dict(
            db.execute("SELECT * FROM import_queue WHERE id=?", (queue_id,)).fetchone()
        )
        file = dict(
            db.execute(
                "SELECT * FROM import_queue_files WHERE queue_id=?", (queue_id,)
            ).fetchone()
        )
    return pack_env, queue, file


def _adapter() -> Any:
    try:
        return importlib.import_module("private_pack_source")
    except ModuleNotFoundError as exc:
        if exc.name != "private_pack_source":
            raise
        pytest.fail("generated-pack source admission adapter is not implemented")


def _freeze(
    adapter: Any, guard: Any, queue: dict[str, Any], file: dict[str, Any]
) -> dict[str, Any]:
    origin = adapter._freeze_pack_file_origin(
        guard, queue, file["id"], file["src_path"]
    )
    queue["_pack_source_origins"] = {"version": 1, "files": {str(file["id"]): origin}}
    # Exercise the existing publication JSON boundary, not an in-memory token.
    queue.update(json.loads(json.dumps(queue)))
    return queue["_pack_source_origins"]["files"][str(file["id"])]


def test_admission_records_inventory_before_payload_reads(
    admitted_queue: tuple[_PackEnv, dict[str, Any], dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, queue, file = admitted_queue
    adapter = _adapter()
    info = os.stat(file["src_path"])
    real_read = os.read

    def read(fd: int, count: int) -> bytes:
        current = os.fstat(fd)
        assert (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino), (
            "payload read before durable admission"
        )
        return real_read(fd, count)

    monkeypatch.setattr(os, "read", read)
    with file_mutation_guard(env["db_path"]) as guard:
        origin = _freeze(adapter, guard, queue, file)
        assert origin["kind"] == "pack" and origin["file_id"] == file["id"]
        ownership = PackOwnership.from_json(origin["ownership_json"])
        assert ownership.inventory is not None
        assert (
            FullFileFingerprint.from_value(origin["source_fingerprint"])
            == ownership.inventory.files[origin["relative_path"]]
        )
        assert origin["artifact_owner_token"] == ownership.artifact_owner_token
        assert origin["src_path"] == file["src_path"]
        guard.verify()


def test_pinned_source_fd_is_borrowed_with_guard_and_alias(
    admitted_queue: tuple[_PackEnv, dict[str, Any], dict[str, Any]],
) -> None:
    env, queue, file = admitted_queue
    adapter = _adapter()
    expected = Path(file["src_path"]).read_bytes()
    with file_mutation_guard(env["db_path"]) as guard:
        origin = _freeze(adapter, guard, queue, file)
        with adapter._open_pack_file_source(
            guard, queue, file["id"], file["src_path"], origin
        ) as source:
            assert source is not None
            fd, alias, fingerprint = source
            assert os.read(fd, len(expected) + 1) == expected
            assert Path(alias).read_bytes() == expected
            assert fingerprint == FullFileFingerprint.from_value(
                origin["source_fingerprint"]
            )
        with pytest.raises(OSError):
            os.fstat(fd)
        guard.verify()
        assert os.fstat(guard.subprocess_fd).st_ino > 0


@pytest.mark.parametrize(
    "during_read", [False, True], ids=["before-open", "during-read"]
)
def test_directory_replacement_never_authorizes_foreign_bytes(
    admitted_queue: tuple[_PackEnv, dict[str, Any], dict[str, Any]], during_read: bool
) -> None:
    env, queue, file = admitted_queue
    adapter = _adapter()
    source = Path(file["src_path"])
    preserved = env["tmp_path"] / "preserved-source"
    expected = source.read_bytes()

    def replace_directory() -> None:
        source.parent.rename(preserved)
        source.parent.mkdir(mode=0o755)
        source.write_bytes(b"foreign replacement")

    with file_mutation_guard(env["db_path"]) as guard:
        origin = _freeze(adapter, guard, queue, file)
        if not during_read:
            replace_directory()
        with pytest.raises(PackProofError):
            with adapter._open_pack_file_source(
                guard, queue, file["id"], file["src_path"], origin
            ) as held:
                assert during_read and held is not None
                replace_directory()
                assert os.read(held[0], len(expected) + 1) == expected
        assert source.read_bytes() == b"foreign replacement"
        assert (preserved / source.name).read_bytes() == expected
        guard.verify()


def test_provisional_matching_queue_is_not_forward_admission_authority(
    admitted_queue: tuple[_PackEnv, dict[str, Any], dict[str, Any]],
) -> None:
    env, queue, file = admitted_queue
    adapter = _adapter()
    with file_mutation_guard(env["db_path"]) as guard:
        with sqlite3.connect(env["db_path"]) as db:
            encoded = db.execute(
                "SELECT directory_ownership_json FROM import_pack_cleanup_reservations"
            ).fetchone()[0]
            ownership = replace(PackOwnership.from_json(encoded), phase="queued")
            db.execute(
                "UPDATE import_pack_cleanup_reservations SET purpose='cleanup',directory_ownership_json=?",
                (ownership.to_json(),),
            )
        with pytest.raises(PackProofError):
            _freeze(adapter, guard, queue, file)
        guard.verify()


@pytest.mark.parametrize("mutation", ["owner", "file", "fingerprint", "unknown-kind"])
def test_malformed_origin_refuses_nonpack_fallback(
    admitted_queue: tuple[_PackEnv, dict[str, Any], dict[str, Any]], mutation: str
) -> None:
    env, queue, file = admitted_queue
    adapter = _adapter()
    with file_mutation_guard(env["db_path"]) as guard:
        origin = copy.deepcopy(_freeze(adapter, guard, queue, file))
        if mutation == "owner":
            origin["artifact_owner_token"] = "successor"
        elif mutation == "file":
            origin["file_id"] += 1
        elif mutation == "fingerprint":
            origin["source_fingerprint"]["sha256"] = "0" * 64
        else:
            origin["kind"] = "unknown"
        queue["_pack_source_origins"]["files"][str(file["id"])] = origin
        with pytest.raises(PackProofError):
            with adapter._open_pack_file_source(
                guard, queue, file["id"], file["src_path"], origin
            ):
                pytest.fail("malformed generated origin was admitted")


def test_missing_generated_ownership_is_not_ordinary_source(
    admitted_queue: tuple[_PackEnv, dict[str, Any], dict[str, Any]],
) -> None:
    env, queue, file = admitted_queue
    adapter = _adapter()
    with file_mutation_guard(env["db_path"]) as guard:
        with sqlite3.connect(env["db_path"]) as db:
            db.execute("DELETE FROM import_pack_cleanup_reservations")
        with pytest.raises(PackProofError):
            _freeze(adapter, guard, queue, file)


def _publication(
    env: _PackEnv,
    queue: dict[str, Any],
    file: dict[str, Any],
    origin: dict[str, Any],
    source_fingerprint: FullFileFingerprint | None = None,
) -> int:
    fingerprint = source_fingerprint or FullFileFingerprint.from_value(
        origin["source_fingerprint"]
    )
    with sqlite3.connect(env["db_path"]) as db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        cursor = db.execute(
            "INSERT INTO import_publications(queue_id,state,owner_token,series_id,"
            "dst_dir,import_mode,staging_dir,queue_snapshot_json,series_tags_json,"
            "queue_status,pack_cleanup_state,queue_download_id,queue_download_client_id)"
            " VALUES(?,'cleaning',?,1,?,'move',?,?,'[]','imported','pending',?,?)",
            (
                queue["id"],
                queue["lease_owner"],
                str(env["library"]),
                str(env["tmp_path"] / "publication-stage"),
                json.dumps(queue),
                queue["download_id"],
                queue["download_client_id"],
            ),
        )
        publication_id = cursor.lastrowid
        assert publication_id is not None
        db.execute(
            "INSERT INTO import_publication_files(publication_id,ordinal,file_id,"
            "src_path,filename,dst_path,import_kind,file_type,is_special,has_volume_range,"
            "is_legacy_chapter_stub,is_legacy_chapter_recheck,plan_status,source_dev,"
            "source_inode,source_size,source_mtime_ns,source_sha256,source_claim_path)"
            " VALUES(?,0,?,?,?,?,'chapter','chapter',0,0,0,0,'ready',?,?,?,?,?,?)",
            (
                publication_id,
                file["id"],
                file["src_path"],
                file["filename"],
                file["dst_path"],
                fingerprint.dev,
                fingerprint.inode,
                fingerprint.size,
                fingerprint.mtime_ns,
                fingerprint.sha256,
                file["src_path"] + ".source-claim",
            ),
        )
    return publication_id


def test_retired_queue_delegation_uses_durable_original_owner(
    admitted_queue: tuple[_PackEnv, dict[str, Any], dict[str, Any]],
) -> None:
    env, queue, file = admitted_queue
    adapter = _adapter()
    expected = Path(file["src_path"]).read_bytes()
    with file_mutation_guard(env["db_path"]) as guard:
        origin = _freeze(adapter, guard, queue, file)
        publication_id = _publication(env, queue, file, origin)
        with sqlite3.connect(env["db_path"]) as db:
            db.execute(
                "DELETE FROM import_queue_files WHERE queue_id=?", (queue["id"],)
            )
            db.execute("DELETE FROM import_queue WHERE id=?", (queue["id"],))
            db.execute(
                "UPDATE import_pack_cleanup_reservations SET owner_token='new-executor'"
            )
            stored = json.loads(
                db.execute(
                    "SELECT queue_snapshot_json FROM import_publications WHERE id=?",
                    (publication_id,),
                ).fetchone()[0]
            )
        recorded = stored["_pack_source_origins"]["files"][str(file["id"])]
        for _ in range(2):
            assert (
                adapter._pack_file_cleanup_delegated(
                    guard,
                    stored,
                    file["id"],
                    file["src_path"],
                    recorded,
                    FullFileFingerprint.from_value(recorded["source_fingerprint"]),
                    publication_id=publication_id,
                )
                is True
            )
        assert Path(file["src_path"]).read_bytes() == expected
        assert not Path(file["src_path"] + ".source-claim").exists()
        guard.verify()


@pytest.mark.parametrize(
    "changed", ["snapshot", "fingerprint", "authority", "captured-flat"]
)
def test_delegation_refuses_mismatched_journal_or_existing_capture(
    admitted_queue: tuple[_PackEnv, dict[str, Any], dict[str, Any]], changed: str
) -> None:
    env, queue, file = admitted_queue
    adapter = _adapter()
    with file_mutation_guard(env["db_path"]) as guard:
        origin = _freeze(adapter, guard, queue, file)
        publication_id = _publication(env, queue, file, origin)
        with sqlite3.connect(env["db_path"]) as db:
            if changed == "snapshot":
                db.execute("UPDATE import_publications SET queue_snapshot_json='{}'")
            elif changed == "fingerprint":
                db.execute(
                    "UPDATE import_publication_files SET source_sha256=?", ("0" * 64,)
                )
            elif changed == "authority":
                db.execute("DELETE FROM import_pack_cleanup_reservations")
        if changed == "captured-flat":
            Path(file["src_path"] + ".source-claim").write_bytes(b"retained capture")
        with pytest.raises(PackProofError):
            adapter._pack_file_cleanup_delegated(
                guard,
                queue,
                file["id"],
                file["src_path"],
                origin,
                FullFileFingerprint.from_value(origin["source_fingerprint"]),
                publication_id=publication_id,
            )
        assert Path(file["src_path"]).is_file()


def test_mixed_batch_nonpack_is_explicit_and_persisted(
    admitted_queue: tuple[_PackEnv, dict[str, Any], dict[str, Any]],
) -> None:
    env, queue, file = admitted_queue
    adapter = _adapter()
    ordinary = Path(queue["src_dir"]) / "ordinary.cbz"
    ordinary.write_bytes(b"ordinary download")
    with file_mutation_guard(env["db_path"]) as guard:
        _freeze(adapter, guard, queue, file)
        with sqlite3.connect(env["db_path"]) as db:
            cursor = db.execute(
                "INSERT INTO import_queue_files(queue_id,filename,src_path,dst_path) VALUES(?,?,?,?)",
                (
                    queue["id"],
                    ordinary.name,
                    str(ordinary),
                    str(env["library"] / ordinary.name),
                ),
            )
            file_id = cursor.lastrowid
            assert file_id is not None
        origin = adapter._freeze_pack_file_origin(guard, queue, file_id, str(ordinary))
        assert origin["kind"] == "nonpack"
        queue["_pack_source_origins"]["files"][str(file_id)] = origin
        with adapter._open_pack_file_source(
            guard, queue, file_id, str(ordinary), origin
        ) as source:
            assert source is None
        assert len(queue["_pack_source_origins"]["files"]) == 2
        assert ordinary.read_bytes() == b"ordinary download"


def test_manual_null_download_identity_remains_proved_nonpack(
    pack_env: _PackEnv,
) -> None:
    import import_lease
    import main
    from private_pack_claim import fingerprint_at

    adapter = _adapter()
    source = pack_env["tmp_path"] / "manual.cbz"
    source.write_bytes(b"manual source")
    with file_mutation_guard(pack_env["db_path"]) as guard, main.get_db() as db:
        cursor = db.execute(
            "INSERT INTO import_queue(series_id,torrent_name,src_dir) VALUES(1,'manual',?)",
            (str(source.parent),),
        )
        queue_id = cursor.lastrowid
        assert queue_id is not None
        cursor = db.execute(
            "INSERT INTO import_queue_files(queue_id,filename,src_path,dst_path) VALUES(?,?,?,?)",
            (
                queue_id,
                source.name,
                str(source),
                str(pack_env["library"] / source.name),
            ),
        )
        file_id = cursor.lastrowid
        assert file_id is not None
        db.commit()
        assert import_lease.claim_import_queue_row(db, queue_id, "manual-owner")
        queue = dict(
            db.execute("SELECT * FROM import_queue WHERE id=?", (queue_id,)).fetchone()
        )
        file = dict(
            db.execute(
                "SELECT * FROM import_queue_files WHERE id=?", (file_id,)
            ).fetchone()
        )
        db.commit()
        origin = _freeze(adapter, guard, queue, file)
        assert origin["kind"] == "nonpack" and origin["download_id"] is None
        with adapter._open_pack_file_source(
            guard, queue, file_id, str(source), origin
        ) as held:
            assert held is None
        fd = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fingerprint = fingerprint_at(fd, source.name)
        finally:
            os.close(fd)
        publication_id = _publication(pack_env, queue, file, origin, fingerprint)
        assert (
            adapter._pack_file_cleanup_delegated(
                guard,
                queue,
                file_id,
                str(source),
                origin,
                fingerprint,
                publication_id=publication_id,
            )
            is False
        )
        assert source.read_bytes() == b"manual source"
