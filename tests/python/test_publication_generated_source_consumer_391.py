"""Actual FILE callers consume admitted PACK bytes and retired-queue authority."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import sqlite3
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from file_mutation_lock import file_mutation_guard
from test_import_pack_cleanup_durability import _PackEnv, pack_env  # noqa: F401
from test_import_publication_journal import _seed_queue, journal_env  # noqa: F401
from test_publication_nfs_file_claims_391 import _unsupported_renameat2


def _generated_queue(
    env: _PackEnv, monkeypatch: pytest.MonkeyPatch, *, private: bool,
    mixed: bool = False,
) -> tuple[int, Path, Path | None]:
    import import_download
    import import_execute
    import import_queue
    import main
    import private_file_claim as claims
    import shared

    for config in (main.CONFIG, shared.CONFIG):
        config["import_mode"] = "move"
        config["remove_completed"] = "false"
        config["minimum_free_space_mb"] = "0"
    monkeypatch.setattr(import_execute, "_IMPORT_SEM", None)

    async def no_network(*_args: object, **_kwargs: object) -> None:
        pass

    monkeypatch.setattr(import_execute, "broadcast_queue_event", no_network)
    monkeypatch.setattr(import_download, "dispatch_download_notification", no_network)
    root = env["pack_root"]
    root.mkdir(mode=0o750)
    with file_mutation_guard(env["db_path"]) as guard:
        with claims.ensure_namespace(guard, str(root)):
            pass
    root.chmod(0o770)
    if private:
        _unsupported_renameat2(monkeypatch, errno.EINVAL)
    source = env["tmp_path"] / "consumer-generated"
    chapter = source / "Pack Series c001"
    chapter.mkdir(parents=True)
    (chapter / "001.jpg").write_bytes(b"admitted page bytes")
    ordinary = source / "Pack Series c002.cbz" if mixed else None
    if ordinary is not None:
        with zipfile.ZipFile(ordinary, "w") as archive:
            archive.writestr("001.jpg", b"ordinary page bytes")
    with main.get_db() as db:
        # Internal-local explicit manual selection, not unknown acquisition policy.
        queue_id, _ = import_queue._queue_import(
            db, 1, "consumer-generated", "Pack Series c001", "magnet:consumer-generated",
            None, str(source), respect_grab_claims=False,
        )
    assert queue_id is not None
    with sqlite3.connect(env["db_path"]) as db:
        if ordinary is not None:
            # Image discovery queues generated leaves only. This internal-local
            # manual builder explicitly selects its regular sibling before plan.
            db.execute(
                "INSERT INTO import_queue_files(queue_id,filename,src_path,"
                "proposed_chapter,file_type,proposed_import_kind,status)"
                " VALUES(?,?,?,2,'chapter','chapter','pending')",
                (queue_id, ordinary.name, str(ordinary)),
            )
        rows = db.execute("SELECT src_path FROM import_queue_files WHERE queue_id=?",
                          (queue_id,)).fetchall()
    generated = [Path(row[0]) for row in rows if Path(row[0]) != ordinary]
    assert len(generated) == 1
    return queue_id, generated[0], ordinary


@pytest.mark.parametrize("private", [False, True], ids=["native", "private"])
def test_stage_reads_pinned_pack_fd_and_refuses_directory_replacement(
    pack_env: _PackEnv, monkeypatch: pytest.MonkeyPatch, private: bool,
) -> None:
    import import_execute
    import private_pack_source
    from import_staging import _ImportStaging

    queue_id, source, _ = _generated_queue(pack_env, monkeypatch, private=private)
    original = source.read_bytes()
    preserved = pack_env["tmp_path"] / "preserved-admitted-directory"
    actual_open = private_pack_source._open_pack_file_source
    opened: list[int] = []
    copied: list[bytes] = []

    @contextmanager
    def replace_after_open(*args: Any, **kwargs: Any):
        with actual_open(*args, **kwargs) as held:
            assert held is not None
            fd, _, _ = held
            opened.append(fd)
            source.parent.rename(preserved)
            source.parent.mkdir(mode=0o755)
            source.write_bytes(b"foreign replacement bytes")
            assert os.pread(fd, len(original) + 1, 0) == original
            try:
                yield held
            finally:
                # The caller's actual pinned copy exists before adapter exit
                # notices the replacement and before any transform is allowed.
                with sqlite3.connect(pack_env["db_path"]) as db:
                    stage = Path(db.execute("SELECT stage_path FROM import_publication_files").fetchone()[0])
                copied.append(stage.read_bytes())

    def forbidden_transform(self: _ImportStaging, path: str) -> str:
        pytest.fail("replacement was transformed before pinned-source revalidation")

    monkeypatch.setattr(private_pack_source, "_open_pack_file_source", replace_after_open)
    monkeypatch.setattr(_ImportStaging, "prepare_for_mutation", forbidden_transform)
    assert not asyncio.run(import_execute._guarded_execute_import(queue_id))
    assert len(opened) == 1 and copied == [original]
    with pytest.raises(OSError):
        os.fstat(opened[0])
    assert source.read_bytes() == b"foreign replacement bytes"
    assert (preserved / source.name).read_bytes() == original
    with sqlite3.connect(pack_env["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM history WHERE event_type='imported'").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM chapters WHERE status='downloaded'").fetchone() == (0,)
    assert list(pack_env["library"].rglob("*.cbz")) == []


@pytest.mark.parametrize("private", [False, True], ids=["native", "private"])
def test_retired_queue_pack_delegation_replays_once_without_nested_namespace(
    pack_env: _PackEnv, monkeypatch: pytest.MonkeyPatch, private: bool,
) -> None:
    import import_execute
    import import_publication as publication

    queue_id, source, _ = _generated_queue(pack_env, monkeypatch, private=private)
    original = source.read_bytes()
    actual_cleanup = publication._cleanup_source_private
    interrupted: list[int] = []

    def interrupt_before_delegation(record, file, guard, owner):
        if not interrupted:
            with sqlite3.connect(pack_env["db_path"], timeout=0) as db:
                db.execute("BEGIN IMMEDIATE")
                assert db.execute("SELECT status FROM import_queue WHERE id=?", (queue_id,)).fetchone() == ("imported",)
                # Honest receipt-loss injection after actual Phase3. Normal
                # journal retirement is atomic with finalization, not Phase3.
                db.execute("DELETE FROM import_queue_files WHERE queue_id=?", (queue_id,))
                db.execute("DELETE FROM import_queue WHERE id=?", (queue_id,))
            interrupted.append(record.publication_id)
            raise OSError(errno.EIO, "injected postcommit before delegated disposition")
        return actual_cleanup(record, file, guard, owner)

    monkeypatch.setattr(publication, "_cleanup_source_private", interrupt_before_delegation)
    assert not asyncio.run(import_execute._guarded_execute_import(queue_id))
    assert len(interrupted) == 1 and source.read_bytes() == original
    with sqlite3.connect(pack_env["db_path"]) as db:
        assert db.execute("SELECT state FROM import_publications").fetchone() == ("cleaning",)
        snapshot = json.loads(db.execute("SELECT queue_snapshot_json FROM import_publications").fetchone()[0])
        assert {origin["kind"] for origin in snapshot["_pack_source_origins"]["files"].values()} == {"pack"}
        assert db.execute("SELECT COUNT(*) FROM history WHERE event_type='imported'").fetchone() == (1,)
    assert not (source.parent / ".mangarr-claims").exists()
    monkeypatch.setattr(publication, "_cleanup_source_private", actual_cleanup)
    assert asyncio.run(publication.complete_publication(interrupted[0]))
    assert asyncio.run(publication.complete_publication(interrupted[0]))
    assert not source.parent.exists()
    with sqlite3.connect(pack_env["db_path"]) as db:
        assert db.execute("SELECT state,pack_cleanup_state FROM import_publications").fetchone() == ("deleted", "complete")
        assert db.execute("SELECT cleanup_state FROM import_publication_files").fetchone() == ("not_applicable",)
        assert db.execute("SELECT COUNT(*) FROM history WHERE event_type='imported'").fetchone() == (1,)
        parents = [Path(row[0]) for row in db.execute("SELECT parent_path FROM file_claim_namespaces")]
    assert parents and all({entry.name for entry in (parent / ".mangarr-claims").iterdir()} == {"owner.json"} for parent in parents)


@pytest.mark.parametrize("private", [False, True], ids=["native", "private"])
def test_mixed_pack_and_nonpack_keep_distinct_postcommit_source_authority(
    pack_env: _PackEnv, monkeypatch: pytest.MonkeyPatch, private: bool,
) -> None:
    import import_execute

    queue_id, generated, ordinary = _generated_queue(pack_env, monkeypatch, private=private, mixed=True)
    assert ordinary is not None and ordinary.exists()
    assert asyncio.run(import_execute._guarded_execute_import(queue_id))
    assert not generated.parent.exists() and not ordinary.exists()
    with sqlite3.connect(pack_env["db_path"]) as db:
        snapshot = json.loads(db.execute("SELECT queue_snapshot_json FROM import_publications").fetchone()[0])
        kinds = snapshot["_pack_source_origins"]["files"]
        states = {kinds[str(file_id)]["kind"]: state for file_id, state in db.execute("SELECT file_id,cleanup_state FROM import_publication_files")}
        assert states == {"pack": "not_applicable", "nonpack": "deleted"}
        receipts = db.execute("SELECT data FROM history WHERE event_type='imported'").fetchall()
        assert len(receipts) == 1 and json.loads(receipts[0][0])["count"] == 2
        assert db.execute("SELECT COUNT(*) FROM chapters WHERE status='downloaded'").fetchone() == (2,)


@pytest.mark.parametrize("capture", ["flat", "private"])
def test_captured_file_source_recovery_precedes_missing_pack_origin(
    journal_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capture: str,
) -> None:
    import import_execute
    import import_publication as publication

    queue_id, _, sources, finals = _seed_queue(journal_env, file_count=1, mode="move")
    original = sources[0].read_bytes()
    interrupted: list[bool] = []
    if capture == "flat":
        actual_rename = publication._rename_noreplace

        def interrupt_after_native_capture(source: str, target: str) -> None:
            actual_rename(source, target)
            if source == str(sources[0]) and not interrupted:
                interrupted.append(True)
                raise OSError(errno.EIO, "injected after actual native source capture")

        monkeypatch.setattr(publication, "_rename_noreplace", interrupt_after_native_capture)
    else:
        _unsupported_renameat2(monkeypatch, errno.EINVAL)
        actual_discard = publication._discard_private

        def interrupt_before_discard(record, file, guard, owner, purpose):
            if purpose == "source" and not interrupted:
                interrupted.append(True)
                raise OSError(errno.EIO, "injected after verified private source capture")
            return actual_discard(record, file, guard, owner, purpose)

        monkeypatch.setattr(publication, "_discard_private", interrupt_before_discard)
    assert not asyncio.run(import_execute._execute_import(queue_id))
    assert interrupted == [True] and finals[0].exists() and not sources[0].exists()
    with sqlite3.connect(journal_env["db_path"]) as db:
        pid, encoded = db.execute("SELECT id,queue_snapshot_json FROM import_publications").fetchone()
        snapshot = json.loads(encoded)
        snapshot.pop("_pack_source_origins")
        db.execute("UPDATE import_publications SET queue_snapshot_json=?", (json.dumps(snapshot),))
        flat, encoded_carrier = db.execute("SELECT source_claim_path,source_claim_carrier_json FROM import_publication_files").fetchone()
    if capture == "flat":
        assert Path(flat).read_bytes() == original and encoded_carrier is None
        monkeypatch.setattr(publication, "_rename_noreplace", actual_rename)
    else:
        import private_file_claim as claims
        record = claims.CarrierRecord.from_json(encoded_carrier)
        assert (Path(record.carrier_path) / "artifact").read_bytes() == original
        monkeypatch.setattr(publication, "_discard_private", actual_discard)
    assert asyncio.run(publication.complete_publication(pid))
    assert asyncio.run(publication.complete_publication(pid))
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute("SELECT state FROM import_publications").fetchone() == ("deleted",)
        assert db.execute("SELECT cleanup_state FROM import_publication_files").fetchone() == ("deleted",)
        assert db.execute("SELECT COUNT(*) FROM history WHERE event_type='imported'").fetchone() == (1,)
