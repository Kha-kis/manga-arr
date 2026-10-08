"""Whole-guard queue authority and versioned directory-carrier replay."""

from __future__ import annotations

import errno
import json
import os
import sqlite3
from pathlib import Path

import pytest

from file_mutation_lock import FileMutationBusy, file_mutation_guard
from test_import_pack_cleanup_durability import (
    _PackEnv,
    _pack_paths,
    _terminal_queue,
    _probe_writer,
    pack_env,
)  # noqa: F401
from test_import_pack_nfs_lifecycle import _queue_images, _expire


def _fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    import import_pack_cleanup

    def unsupported(source: str, target: str) -> None:
        raise OSError(errno.EINVAL, "unsupported", target)

    monkeypatch.setattr(import_pack_cleanup, "_rename_noreplace", unsupported)


def test_reservation_cas_does_not_bypass_live_guard(pack_env: _PackEnv) -> None:
    import import_pack_cleanup
    import main

    with file_mutation_guard(pack_env["db_path"]), main.get_db() as db:
        assert (
            import_pack_cleanup.reserve_pack_queue_creation(
                db, "excluded-cas", download_client_id=None, protocol=None
            )
            is None
        )
    with sqlite3.connect(pack_env["db_path"]) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM import_pack_cleanup_reservations"
        ).fetchone() == (0,)


def test_final_queue_commit_retains_same_guard(
    pack_env: _PackEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = sqlite3.connect
    checks: list[bool] = []

    class ObservedConnection(sqlite3.Connection):
        queue_inserted = False

        def execute(self, sql: str, parameters=()):
            if sql.startswith("INSERT INTO import_queue("):
                self.queue_inserted = True
            return super().execute(sql, parameters)

        def commit(self) -> None:
            if self.in_transaction and self.queue_inserted:
                with pytest.raises(FileMutationBusy):
                    with file_mutation_guard(pack_env["db_path"]):
                        pass
                checks.append(True)
            super().commit()
            self.queue_inserted = False

    def connect(*args, **kwargs):
        kwargs["factory"] = ObservedConnection
        return original(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    assert _queue_images(pack_env, "guarded-commit") is not None
    assert checks


def test_attachment_barrier_failure_replays_proven_private_placement(
    pack_env: _PackEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    import import_pack_cleanup

    _fallback(monkeypatch)
    original = import_pack_cleanup.inventory_tree
    flushes = 0

    def fail(*args, **kwargs):
        nonlocal flushes
        if not kwargs.get("flush"):
            return original(*args, **kwargs)
        flushes += 1
        if flushes != 2:
            return original(*args, **kwargs)
        _probe_writer(pack_env["db_path"], "writer-during-link")
        raise OSError(errno.EIO, "attachment barrier reply lost")

    def retain(*args, **kwargs):
        raise OSError(errno.EIO, "private cleanup unavailable")

    with monkeypatch.context() as fault:
        fault.setattr(import_pack_cleanup, "inventory_tree", fail)
        fault.setattr(import_pack_cleanup.shutil, "rmtree", retain)
        assert _queue_images(pack_env, "partial-link") is None
    assert import_pack_cleanup.inventory_tree is original
    with sqlite3.connect(pack_env["db_path"]) as db:
        row = db.execute(
            "SELECT directory_ownership_json FROM import_pack_cleanup_reservations"
        ).fetchone()
        assert row is not None
        ownership = json.loads(row[0])
        assert ownership["private_directory"] and ownership["canonical_directory"]
        assert ownership["placement_carrier"]
        assert db.execute("SELECT COUNT(*) FROM import_queue_files").fetchone() == (0,)
    _expire(pack_env)
    result = import_pack_cleanup.recover_pack_cleanup_state()
    assert result.reservations_recovered == 1
    canonical, _ = _pack_paths("partial-link")
    assert not canonical.exists()
    assert (
        import_pack_cleanup.recover_pack_cleanup_state()
        == import_pack_cleanup.PackCleanupRecovery()
    )
    namespace = pack_env["pack_root"] / ".mangarr-claims"
    assert {p.name for p in namespace.iterdir()} == {"owner.json"}


def test_terminal_success_preserves_original_provenance_against_recreation(
    pack_env: _PackEnv,
) -> None:
    import import_pack_cleanup

    queue_id = _queue_images(pack_env, "terminal-recreated")
    assert queue_id is not None
    canonical, _ = _pack_paths("terminal-recreated")
    marker = (canonical / "mangarr-pack-owner").read_bytes()
    with sqlite3.connect(pack_env["db_path"]) as db:
        db.execute("UPDATE import_queue SET status='failed' WHERE id=?", (queue_id,))
    assert import_pack_cleanup.cleanup_terminal_pack_staging(
        queue_id, "terminal-recreated", download_client_id=None, protocol=None
    )
    canonical.mkdir()
    (canonical / "mangarr-pack-owner").write_bytes(marker)
    (canonical / "unrelated.cbz").write_bytes(b"unrelated")
    assert not import_pack_cleanup.cleanup_terminal_pack_staging(
        queue_id, "terminal-recreated", download_client_id=None, protocol=None
    )
    assert (canonical / "unrelated.cbz").read_bytes() == b"unrelated"


def test_terminal_capture_checks_original_owner_after_executor_change(
    pack_env: _PackEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    import import_pack_cleanup

    queue_id = _queue_images(pack_env, "original-owner")
    assert queue_id is not None
    with sqlite3.connect(pack_env["db_path"]) as db:
        original = json.loads(
            db.execute(
                "SELECT directory_ownership_json FROM import_pack_cleanup_reservations"
            ).fetchone()[0]
        )["artifact_owner_token"]
        db.execute("UPDATE import_queue SET status='failed' WHERE id=?", (queue_id,))

    def crash(*args, **kwargs):
        raise RuntimeError("crash before journal transfer")

    with monkeypatch.context() as fault:
        fault.setattr(import_pack_cleanup, "_record_owned_tombstone", crash)
        with pytest.raises(RuntimeError, match="crash before"):
            import_pack_cleanup.cleanup_terminal_pack_staging(
                queue_id, "original-owner", download_client_id=None, protocol=None
            )
    with sqlite3.connect(pack_env["db_path"]) as db:
        executor, encoded = db.execute(
            "SELECT owner_token,directory_ownership_json FROM import_pack_cleanup_reservations"
        ).fetchone()
        assert executor != original
        assert json.loads(encoded)["artifact_owner_token"] == original
    _expire(pack_env)
    assert import_pack_cleanup.recover_pack_cleanup_state().tombstones_removed == 1


def test_normal_cleanup_does_not_accumulate_carriers(pack_env: _PackEnv) -> None:
    import import_pack_cleanup

    for index in range(100):
        download = f"bounded-{index}"
        queue_id = _terminal_queue(pack_env["db_path"], download)
        canonical, _ = _pack_paths(download)
        canonical.mkdir(parents=True)
        (canonical / "page.cbz").write_bytes(b"page")
        assert import_pack_cleanup.cleanup_terminal_pack_staging(
            queue_id, download, download_client_id=None, protocol=None
        )
    assert {p.name for p in pack_env["pack_root"].iterdir()} == {".mangarr-claims"}
    assert {p.name for p in (pack_env["pack_root"] / ".mangarr-claims").iterdir()} == {
        "owner.json"
    }


@pytest.mark.parametrize("private_placement", [False, True], ids=["native", "private"])
def test_canonical_replacement_during_queue_insert_rolls_back(
    pack_env: _PackEnv, monkeypatch: pytest.MonkeyPatch, private_placement: bool
) -> None:
    if private_placement:
        _fallback(monkeypatch)
    original = sqlite3.connect
    displaced = pack_env["tmp_path"] / "committed-original"
    replaced: list[Path] = []

    class ReplacedConnection(sqlite3.Connection):
        def executemany(self, sql, parameters):
            if sql.startswith("INSERT INTO import_queue_files"):
                actual = Path(parameters[0][2]).parent
                actual.rename(displaced)
                actual.mkdir()
                (actual / "unrelated").write_bytes(b"leave alone")
                replaced.append(actual)
            return super().executemany(sql, parameters)

    def connect(*args, **kwargs):
        kwargs["factory"] = ReplacedConnection
        return original(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    assert _queue_images(pack_env, "commit-replacement") is None
    with original(pack_env["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM import_queue").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM import_queue_files").fetchone() == (0,)
        assert db.execute(
            "SELECT COUNT(*) FROM import_pack_cleanup_reservations"
        ).fetchone() == (1,)
    assert (replaced[0] / "unrelated").read_bytes() == b"leave alone"
    assert list(displaced.glob("*.cbz"))


def test_discard_replay_refuses_a_changed_pack_marker(
    pack_env: _PackEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    import import_pack_cleanup

    queue_id = _queue_images(pack_env, "discard-marker")
    assert queue_id is not None
    with sqlite3.connect(pack_env["db_path"]) as db:
        db.execute("UPDATE import_queue SET status='failed' WHERE id=?", (queue_id,))

    def interrupt(path, **kwargs):
        raise OSError(errno.EIO, "discard interrupted")

    with monkeypatch.context() as fault:
        fault.setattr(import_pack_cleanup.shutil, "rmtree", interrupt)
        assert not import_pack_cleanup.cleanup_terminal_pack_staging(
            queue_id, "discard-marker", download_client_id=None, protocol=None
        )
    with sqlite3.connect(pack_env["db_path"]) as db:
        path, encoded = db.execute(
            "SELECT tombstone_path,carrier_json FROM import_pack_cleanup_tombstones"
        ).fetchone()
        assert json.loads(encoded)["carrier"]["phase"] == "discarding"
    marker = Path(path) / "mangarr-pack-owner"
    marker.write_bytes(b"different owner")
    result = import_pack_cleanup.recover_pack_cleanup_state()
    assert result.tombstones_retained == 1
    assert marker.read_bytes() == b"different owner"


def test_pre_move_error_cannot_release_an_unremoved_private_proof(
    pack_env: _PackEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    import import_pack_cleanup

    def fail(*args, **kwargs):
        raise OSError(errno.EIO, "native reply failure before move")

    monkeypatch.setattr(import_pack_cleanup, "_rename_noreplace", fail)
    monkeypatch.setattr(import_pack_cleanup.shutil, "rmtree", fail)
    assert _queue_images(pack_env, "pre-move-error") is None
    with sqlite3.connect(pack_env["db_path"]) as db:
        row = db.execute(
            "SELECT directory_ownership_json FROM import_pack_cleanup_reservations"
        ).fetchone()
        assert row is not None
        proof = json.loads(row[0])["private_directory"]
        assert proof is not None and Path(proof["path"]).is_dir()
        assert db.execute("SELECT COUNT(*) FROM import_queue").fetchone() == (0,)


def test_direct_attachment_cas_respects_shared_guard(pack_env: _PackEnv) -> None:
    import import_pack_cleanup
    import main

    with main.get_db() as db:
        owner = import_pack_cleanup.reserve_pack_queue_creation(
            db, "guarded-cas", download_client_id=None, protocol=None
        )
    assert owner is not None
    with file_mutation_guard(pack_env["db_path"]), main.get_db() as db:
        assert not import_pack_cleanup.begin_pack_queue_attachment(
            db, "guarded-cas", owner, download_client_id=None, protocol=None
        )
    with sqlite3.connect(pack_env["db_path"]) as db:
        assert db.execute(
            "SELECT purpose FROM import_pack_cleanup_reservations"
        ).fetchone() == ("queueing",)


@pytest.mark.parametrize("condition", ["proven", "changed", "empty-recreation"])
def test_genuine_v0_tombstone_requires_independent_file_evidence(
    pack_env: _PackEnv, condition: str
) -> None:
    import import_pack_cleanup
    from private_pack_claim import open_directory, fingerprint_at
    from download_identity import DownloadIdentity, download_identity_key

    download = "genuine-v0"
    queue_id = _terminal_queue(pack_env["db_path"], download)
    canonical, _ = _pack_paths(download)
    canonical.parent.mkdir(parents=True)
    tombstone = canonical.with_name(canonical.name + ".cleanup-pre-upgrade")
    tombstone.mkdir()
    page = tombstone / "page.cbz"
    page.write_bytes(b"known original app output")
    with open_directory(str(tombstone)) as fd:
        expected = fingerprint_at(fd, "page.cbz")
    if condition == "changed":
        page.write_bytes(b"unrelated replacement")
    elif condition == "empty-recreation":
        tombstone.rename(pack_env["tmp_path"] / "old-v0")
        tombstone.mkdir()
    with sqlite3.connect(pack_env["db_path"]) as db:
        cur = db.execute(
            "INSERT INTO import_publications(queue_id,state,owner_token,series_id,dst_dir,"
            "import_mode,staging_dir,queue_snapshot_json,series_tags_json,queue_status)"
            " VALUES(?,'finalized','legacy-executor',1,?,'copy',?,'{}','[]','failed')",
            (
                queue_id,
                str(pack_env["library"]),
                str(pack_env["tmp_path"] / "old-stage"),
            ),
        )
        publication = cur.lastrowid
        db.execute(
            "INSERT INTO import_publication_files(publication_id,ordinal,file_id,src_path,filename,dst_path,"
            "import_kind,file_type,is_special,has_volume_range,is_legacy_chapter_stub,is_legacy_chapter_recheck,"
            "plan_status,source_dev,source_inode,source_size,source_mtime_ns,source_sha256,cleanup_state)"
            " VALUES(?,0,1,?,'page.cbz',?,'volume','volume',0,0,0,0,'ready',?,?,?,?,?,'not_applicable')",
            (
                publication,
                str(canonical / "page.cbz"),
                str(pack_env["library"] / "page.cbz"),
                expected.dev,
                expected.inode,
                expected.size,
                expected.mtime_ns,
                expected.sha256,
            ),
        )
        db.execute(
            "INSERT INTO import_pack_cleanup_tombstones(tombstone_path,download_identity_key,"
            "normalized_download_id,download_id,queue_id,pack_path) VALUES(?,?,?,?,?,?)",
            (
                str(tombstone),
                download_identity_key(DownloadIdentity(None, None, download)),
                download,
                download,
                queue_id,
                str(canonical),
            ),
        )
    result = import_pack_cleanup.recover_pack_cleanup_state()
    assert result.tombstones_removed == int(condition == "proven")
    assert result.tombstones_retained == int(condition != "proven")
    assert tombstone.exists() is (condition != "proven")
    if condition == "changed":
        assert page.read_bytes() == b"unrelated replacement"


@pytest.mark.parametrize(
    "missing_private",
    [False, True],
    ids=["partial-canonical", "missing-private-refused"],
)
def test_nested_partial_attach_recovery_never_authorizes_private_missing_files(
    pack_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
    missing_private: bool,
) -> None:
    import import_pack_cleanup
    import main

    _fallback(monkeypatch)
    with main.get_db() as db:
        owner = import_pack_cleanup.reserve_pack_queue_creation(
            db, "nested-partial", download_client_id=None, protocol=None
        )
        assert owner is not None
        assert import_pack_cleanup.begin_pack_queue_attachment(
            db, "nested-partial", owner, download_client_id=None, protocol=None
        )
    canonical, private = _pack_paths("nested-partial", owner)
    private.mkdir(parents=True)
    (private / "first").mkdir()
    (private / "later").mkdir()
    (private / "first" / "page.cbz").write_bytes(b"first")
    (private / "later" / "page.cbz").write_bytes(b"later")
    # Exact pre-placement v1 journal: interrupted after the first public child
    # mkdir but before its first link. Preserve recovery of these existing trees.
    from private_pack_claim import (
        PackOwnership,
        directory_proof,
        inventory_tree,
        open_directory,
    )
    from download_identity import DownloadIdentity, download_identity_key

    canonical.mkdir()
    (canonical / "first").mkdir()
    identity = DownloadIdentity(None, None, "nested-partial")
    import_pack_cleanup._write_pack_owner_marker(str(private), identity, owner)
    import_pack_cleanup._write_pack_owner_marker(str(canonical), identity, owner)
    with open_directory(str(private)) as fd:
        private_proof = directory_proof(str(private), fd)
        inventory = inventory_tree(fd, flush=True)
    with open_directory(str(canonical)) as fd:
        canonical_proof = directory_proof(str(canonical), fd)
    encoded = PackOwnership(
        download_identity_key(identity),
        owner,
        phase="attaching",
        private_directory=private_proof,
        canonical_directory=canonical_proof,
        inventory=inventory,
    ).to_json()
    with sqlite3.connect(pack_env["db_path"]) as db:
        db.execute(
            "UPDATE import_pack_cleanup_reservations SET directory_ownership_json=?",
            (encoded,),
        )
    assert (canonical / "first").is_dir() and not (canonical / "later").exists()
    if missing_private:
        (private / "first" / "page.cbz").unlink()
        (canonical / "later").mkdir()
    _expire(pack_env)
    result = import_pack_cleanup.recover_pack_cleanup_state()
    assert result.reservations_recovered == int(not missing_private)
    assert private.exists() is missing_private
    assert canonical.exists() is missing_private


@pytest.mark.parametrize("stage", ["private", "canonical"])
def test_fresh_directory_replacement_before_open_is_never_marked_or_modified(
    pack_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    _fallback(monkeypatch)
    canonical, _ = _pack_paths("creation-racer")
    original = os.open
    replacements: list[Path] = []

    def race(path, flags, *args, **kwargs):
        if not replacements and flags & os.O_DIRECTORY:
            candidate = (
                Path(os.readlink(f"/proc/self/fd/{kwargs['dir_fd']}")) / path
                if "dir_fd" in kwargs
                else Path(path)
            )
            if candidate.name == "artifact" and (
                (stage == "private" and "dir_fd" in kwargs)
                or (stage == "canonical" and "dir_fd" not in kwargs)
            ):
                candidate.rename(pack_env["tmp_path"] / "fresh-original")
                candidate.mkdir(mode=0o700)
                (candidate / "unrelated").write_bytes(b"leave alone")
                replacements.append(candidate)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", race)
    assert _queue_images(pack_env, "creation-racer") is None
    assert replacements and {
        p.name: p.read_bytes() for p in replacements[0].iterdir()
    } == {"unrelated": b"leave alone"}
    with sqlite3.connect(pack_env["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM import_queue").fetchone() == (0,)


@pytest.mark.parametrize("phase", ["allocated", "claiming", "claimed"])
def test_durable_capture_checkpoint_replays_without_operation_residue(
    pack_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    import import_pack_cleanup

    queue_id = _queue_images(pack_env, "capture-checkpoint")
    assert queue_id is not None
    with sqlite3.connect(pack_env["db_path"]) as db:
        db.execute("UPDATE import_queue SET status='failed' WHERE id=?", (queue_id,))
    original = import_pack_cleanup._save_ownership

    def crash(guard, reservation, ownership, **kwargs):
        updated = original(guard, reservation, ownership, **kwargs)
        if ownership.claims and ownership.claims[-1].carrier.phase == phase:
            raise RuntimeError(f"crash after durable {phase}")
        return updated

    with monkeypatch.context() as fault:
        fault.setattr(import_pack_cleanup, "_save_ownership", crash)
        with pytest.raises(RuntimeError, match="crash after durable"):
            import_pack_cleanup.cleanup_terminal_pack_staging(
                queue_id, "capture-checkpoint", download_client_id=None, protocol=None
            )
    _expire(pack_env)
    assert import_pack_cleanup.recover_pack_cleanup_state().tombstones_removed == 1
    canonical, _ = _pack_paths("capture-checkpoint")
    assert not canonical.exists()
    assert {p.name for p in (pack_env["pack_root"] / ".mangarr-claims").iterdir()} == {
        "owner.json"
    }


def test_post_commit_private_cleanup_error_keeps_known_queue_and_fence(
    pack_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_pack_cleanup

    def fail(*args, **kwargs):
        raise OSError(
            errno.EIO, "private cleanup failed after authoritative queue COMMIT"
        )

    with monkeypatch.context() as fault:
        fault.setattr(import_pack_cleanup, "gc_discarded_carrier", fail)
        queue_id = _queue_images(pack_env, "post-commit-cleanup")
    assert queue_id is not None
    with sqlite3.connect(pack_env["db_path"]) as db:
        row = db.execute(
            "SELECT purpose,queue_id,directory_ownership_json FROM import_pack_cleanup_reservations"
        ).fetchone()
        assert row[:2] == ("cleanup", queue_id)
        assert json.loads(row[2])["phase"] == "queued"
        assert db.execute(
            "SELECT COUNT(*) FROM import_queue_files WHERE queue_id=?", (queue_id,)
        ).fetchone() == (1,)
    _expire(pack_env)
    assert import_pack_cleanup.recover_pack_cleanup_state().reservations_recovered == 1
    with sqlite3.connect(pack_env["db_path"]) as db:
        assert db.execute(
            "SELECT purpose FROM import_pack_cleanup_reservations"
        ).fetchone() == ("queueing",)


def test_private_mkdir_marker_crash_is_not_mistaken_for_a_legacy_owned_tree(
    pack_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_pack_cleanup
    import main

    def crash(*args, **kwargs):
        raise KeyboardInterrupt("crash between private mkdir and marker")

    with monkeypatch.context() as fault:
        fault.setattr(import_pack_cleanup, "_write_pack_owner_marker", crash)
        with pytest.raises(KeyboardInterrupt, match="between private mkdir"):
            _queue_images(pack_env, "marker-gap")
    with sqlite3.connect(pack_env["db_path"]) as db:
        owner, encoded = db.execute(
            "SELECT owner_token,directory_ownership_json FROM import_pack_cleanup_reservations"
        ).fetchone()
    path = str(
        Path(json.loads(encoded)["placement_carrier"]["carrier_path"]) / "artifact"
    )
    assert Path(path).is_dir() and not list(Path(path).iterdir())
    with main.get_db() as db:
        assert not import_pack_cleanup.release_pack_queue_creation(
            db, "marker-gap", owner, download_client_id=None, protocol=None, commit=True
        )
    _expire(pack_env)
    assert import_pack_cleanup.recover_pack_cleanup_state().reservations_recovered == 0
    assert Path(path).is_dir()


@pytest.mark.parametrize("field", ["operation_key", "source_path"])
def test_reservation_claim_binding_cannot_authorize_another_operation_or_parent(
    pack_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    import import_pack_cleanup
    from dataclasses import replace
    from download_identity import DownloadIdentity, download_identity_key
    from private_pack_claim import PackProofError

    queue_id = _queue_images(pack_env, "strict-claim-binding")
    assert queue_id is not None
    with sqlite3.connect(pack_env["db_path"]) as db:
        db.execute("UPDATE import_queue SET status='failed' WHERE id=?", (queue_id,))

    def crash(*args, **kwargs):
        raise RuntimeError("stop before carrier journal transfer")

    with monkeypatch.context() as fault:
        fault.setattr(import_pack_cleanup, "_record_owned_tombstone", crash)
        with pytest.raises(RuntimeError, match="before carrier"):
            import_pack_cleanup.cleanup_terminal_pack_staging(
                queue_id, "strict-claim-binding", download_client_id=None, protocol=None
            )
    reservation = import_pack_cleanup._read_reservation(
        download_identity_key(DownloadIdentity(None, None, "strict-claim-binding"))
    )
    assert reservation is not None and reservation.directory_ownership_json is not None
    value = json.loads(reservation.directory_ownership_json)
    claim = value["claims"][0]
    if field == "operation_key":
        claim["carrier"]["binding"]["operation_key"] = "unrelated-operation"
    else:
        other = str(pack_env["library"] / "unrelated-tree")
        claim["source_directory"]["path"] = claim["carrier"]["origin_path"] = other
    with pytest.raises(PackProofError):
        import_pack_cleanup._ownership(
            replace(reservation, directory_ownership_json=json.dumps(value))
        )
    assert not (pack_env["library"] / ".mangarr-claims").exists()


def test_invalid_tombstone_origin_is_rejected_before_namespace_creation(
    pack_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_pack_cleanup

    queue_id = _queue_images(pack_env, "invalid-tombstone-origin")
    assert queue_id is not None
    with sqlite3.connect(pack_env["db_path"]) as db:
        db.execute("UPDATE import_queue SET status='failed' WHERE id=?", (queue_id,))
    with monkeypatch.context() as fault:

        def interrupt(*args, **kwargs):
            raise OSError(errno.EIO, "interrupt private discard")

        fault.setattr(import_pack_cleanup.shutil, "rmtree", interrupt)
        assert not import_pack_cleanup.cleanup_terminal_pack_staging(
            queue_id, "invalid-tombstone-origin", download_client_id=None, protocol=None
        )
    with sqlite3.connect(pack_env["db_path"]) as db:
        path, encoded = db.execute(
            "SELECT tombstone_path,carrier_json FROM import_pack_cleanup_tombstones"
        ).fetchone()
        value = json.loads(encoded)
        other = str(pack_env["library"] / "unrelated-tree")
        value["source_directory"]["path"] = value["carrier"]["origin_path"] = other
        db.execute(
            "UPDATE import_pack_cleanup_tombstones SET carrier_json=?",
            (json.dumps(value),),
        )
    assert import_pack_cleanup.recover_pack_cleanup_state().tombstones_retained == 1
    assert Path(path).is_dir()
    assert not (pack_env["library"] / ".mangarr-claims").exists()


@pytest.mark.parametrize(
    "evidence",
    ["deleted", "missing", "not-applicable", "different-fingerprint", "uncommitted"],
)
@pytest.mark.parametrize("private_placement", [False, True], ids=["native", "private"])
def test_terminal_inventory_allows_only_committed_matching_missing_paths(
    pack_env: _PackEnv,
    evidence: str,
    monkeypatch: pytest.MonkeyPatch,
    private_placement: bool,
) -> None:
    import import_pack_cleanup
    from private_pack_claim import open_directory, fingerprint_at

    if private_placement:
        _fallback(monkeypatch)
    queue_id = _queue_images(pack_env, "committed-missing")
    assert queue_id is not None
    with sqlite3.connect(pack_env["db_path"]) as db:
        page = Path(
            db.execute(
                "SELECT src_path FROM import_queue_files WHERE queue_id=?", (queue_id,)
            ).fetchone()[0]
        )
    canonical = page.parent
    with open_directory(str(canonical)) as fd:
        expected = fingerprint_at(fd, page.name)
    with sqlite3.connect(pack_env["db_path"]) as db:
        db.execute("UPDATE import_queue SET status='failed' WHERE id=?", (queue_id,))
        file_id = db.execute(
            "SELECT id FROM import_queue_files WHERE queue_id=?", (queue_id,)
        ).fetchone()[0]
        cur = db.execute(
            "INSERT INTO import_publications(queue_id,state,owner_token,series_id,dst_dir,"
            "import_mode,staging_dir,queue_snapshot_json,series_tags_json,queue_status)"
            " VALUES(?,?,'publication-executor',1,?,'copy',?,'{}','[]','failed')",
            (
                queue_id,
                "published" if evidence == "uncommitted" else "finalized",
                str(pack_env["library"]),
                str(pack_env["tmp_path"] / "publication-stage"),
            ),
        )
        cleanup = (
            "not_applicable"
            if evidence == "not-applicable"
            else "missing"
            if evidence == "missing"
            else "deleted"
        )
        db.execute(
            "INSERT INTO import_publication_files(publication_id,ordinal,file_id,src_path,filename,dst_path,"
            "import_kind,file_type,is_special,has_volume_range,is_legacy_chapter_stub,is_legacy_chapter_recheck,"
            "plan_status,source_dev,source_inode,source_size,source_mtime_ns,source_sha256,cleanup_state)"
            " VALUES(?,0,?,?,?,?, 'chapter','chapter',0,0,0,0,'ready',?,?,?,?,?,?)",
            (
                cur.lastrowid,
                file_id,
                str(page),
                page.name,
                str(pack_env["library"] / page.name),
                expected.dev,
                expected.inode,
                expected.size,
                expected.mtime_ns,
                "0" * 64 if evidence == "different-fingerprint" else expected.sha256,
                cleanup,
            ),
        )
    page.unlink()
    authorized = evidence in ("deleted", "missing")
    from download_identity import DownloadIdentity, download_identity_key

    reservation = import_pack_cleanup._read_reservation(
        download_identity_key(DownloadIdentity(None, None, "committed-missing"))
    )
    assert reservation is not None
    ownership = import_pack_cleanup._ownership(reservation)
    assert ownership is not None and ownership.inventory is not None
    assert import_pack_cleanup._committed_missing(reservation, ownership.inventory) == (
        frozenset({page.name}) if authorized else frozenset()
    )
    assert (
        import_pack_cleanup.cleanup_terminal_pack_staging(
            queue_id, "committed-missing", download_client_id=None, protocol=None
        )
        is authorized
    )
    assert canonical.exists() is not authorized
