"""Fallback placement uses proven private carriers, not public mkdir identity guesses."""

from __future__ import annotations

import errno
import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from file_mutation_lock import file_mutation_guard
import private_file_claim as claims
from test_import_pack_cleanup_durability import (
    _PackEnv,
    _pack_paths,
    _terminal_queue,
    pack_env as pack_env,
)
from test_import_pack_nfs_lifecycle import _queue_images, _expire


def _provision(env: _PackEnv) -> None:
    parent = env["pack_root"]
    parent.mkdir(mode=0o750)
    with file_mutation_guard(env["db_path"]) as guard:
        with claims.ensure_namespace(guard, str(parent)):
            pass
    parent.chmod(0o770)


@pytest.mark.parametrize("intent,expected", [(None, 1), (True, 1), (False, 0)])
def test_generated_queue_preserves_current_acquisition_policy(
    pack_env: _PackEnv, intent: bool | None, expected: int
) -> None:
    import import_queue
    import main

    source = pack_env["tmp_path"] / "policy-images"
    chapter = source / "Pack Series c001"
    chapter.mkdir(parents=True)
    (chapter / "001.jpg").write_bytes(b"page-one")
    with main.get_db() as db:
        queue_id, _ = import_queue._queue_import(
            db,
            1,
            "policy-images",
            "Pack Series c001",
            "magnet:policy-images",
            None,
            str(source),
            respect_grab_claims=intent,
        )
    assert queue_id is not None
    with sqlite3.connect(pack_env["db_path"]) as db:
        assert db.execute(
            "SELECT respect_grab_claims FROM import_queue WHERE id=?", (queue_id,)
        ).fetchone() == (expected,)


@pytest.mark.parametrize("operation", ["attach", "abandoned", "terminal"])
@pytest.mark.parametrize("shared_parent", [False, True])
def test_legacy_first_observation_requires_exclusive_parent(
    pack_env: _PackEnv, operation: str, shared_parent: bool
) -> None:
    import import_pack_cleanup as cleanup
    import main
    from private_pack_claim import PackProofError

    _provision(pack_env)
    if not shared_parent:
        pack_env["pack_root"].chmod(0o750)
    download = f"legacy-{operation}"
    if operation == "terminal":
        queue_id = _terminal_queue(pack_env["db_path"], download)
        source, _ = _pack_paths(download)
    else:
        with main.get_db() as db:
            owner = cleanup.reserve_pack_queue_creation(
                db, download, download_client_id=None, protocol=None
            )
            assert owner is not None
            assert cleanup.begin_pack_queue_attachment(
                db, download, owner, download_client_id=None, protocol=None
            )
        _, source = _pack_paths(download, owner)
    # An entry writer can move this pre-existing app-owned inode into the
    # expected name without writing its private contents or forging a marker.
    source.mkdir(mode=0o700)
    (source / "unmarked.cbz").write_bytes(b"existing private data")
    before = source.stat().st_ino
    if operation == "attach":
        if shared_parent:
            with pytest.raises(PackProofError, match="exclusive"):
                cleanup.durably_attach_pack_queue_directory(
                    download, owner, download_client_id=None, protocol=None
                )
        else:
            actual = cleanup.durably_attach_pack_queue_directory(
                download, owner, download_client_id=None, protocol=None
            )
            assert Path(actual).stat().st_ino == before
    elif operation == "abandoned":
        _expire(pack_env)
        result = cleanup.recover_pack_cleanup_state()
        assert result.reservations_recovered == (0 if shared_parent else 1)
    else:
        assert cleanup.cleanup_terminal_pack_staging(
            queue_id, download, download_client_id=None, protocol=None
        ) is (not shared_parent)
    if shared_parent:
        assert source.stat().st_ino == before
        assert {p.name for p in source.iterdir()} == {"unmarked.cbz"}
        assert (source / "unmarked.cbz").read_bytes() == b"existing private data"
    elif operation != "attach":
        assert not source.exists()


@pytest.mark.parametrize("error", [errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP])
def test_generated_fallback_stays_private_until_terminal_own_carrier_gc(
    pack_env: _PackEnv, monkeypatch: pytest.MonkeyPatch, error: int
) -> None:
    import import_pack_cleanup as cleanup

    _provision(pack_env)
    canonical, _ = _pack_paths("private-placement")
    public_mkdir: list[str] = []
    original = os.mkdir

    def mkdir(name, mode=0o777, *, dir_fd=None):
        if name == canonical.name:
            public_mkdir.append(str(name))
        original(name, mode, dir_fd=dir_fd)

    def unsupported(source: str, destination: str) -> None:
        raise OSError(error, "unsupported", destination)

    monkeypatch.setattr(os, "mkdir", mkdir)
    monkeypatch.setattr(cleanup, "_rename_noreplace", unsupported)
    queue_id = _queue_images(pack_env, "private-placement")
    assert queue_id is not None
    assert public_mkdir == [], "public mkdir has no atomic fresh-inode proof"
    assert not canonical.exists()
    with sqlite3.connect(pack_env["db_path"]) as db:
        src_dir = db.execute(
            "SELECT src_dir FROM import_queue WHERE id=?", (queue_id,)
        ).fetchone()[0]
        assert src_dir == str(pack_env["tmp_path"] / "private-placement")
        logical, encoded = db.execute(
            "SELECT pack_path,directory_ownership_json FROM import_pack_cleanup_reservations"
        ).fetchone()
        assert logical == str(canonical)
        ownership = json.loads(encoded)
        placement = claims.CarrierRecord.from_json(
            json.dumps(ownership["placement_carrier"])
        )
        assert placement.binding.purpose == "pack_cleanup"
        assert placement.binding.operation_key == ownership["artifact_owner_token"]
        actual = Path(placement.carrier_path) / "artifact"
        assert ownership["canonical_directory"]["path"] == str(actual)
        rows = db.execute(
            "SELECT src_path FROM import_queue_files WHERE queue_id=?", (queue_id,)
        ).fetchall()
        assert len(rows) == 1 and Path(rows[0][0]).parent == actual
        assert Path(rows[0][0]).is_file()
        assert not list(actual.rglob(".mangarr-claims"))
        db.execute("UPDATE import_queue SET status='failed' WHERE id=?", (queue_id,))
    assert cleanup.cleanup_terminal_pack_staging(
        queue_id, "private-placement", download_client_id=None, protocol=None
    )
    assert not actual.exists() and not Path(placement.carrier_path).exists()
    namespace = pack_env["pack_root"] / ".mangarr-claims"
    assert {p.name for p in namespace.iterdir()} == {"owner.json"}
    assert cleanup.recover_pack_cleanup_state() == cleanup.PackCleanupRecovery()


def test_unprovisioned_shared_parent_refuses_before_any_pack_artifact(
    pack_env: _PackEnv,
) -> None:
    pack_env["pack_root"].mkdir(mode=0o770)
    pack_env["pack_root"].chmod(0o770)
    assert _queue_images(pack_env, "unprovisioned") is None
    assert list(pack_env["pack_root"].iterdir()) == []
    with sqlite3.connect(pack_env["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM import_queue").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM import_queue_files").fetchone() == (0,)


def test_fresh_cache_root_cannot_bootstrap_through_a_shared_parent(
    pack_env: _PackEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    import import_pipeline

    parent = pack_env["tmp_path"] / "shared-cache-parent"
    parent.mkdir(mode=0o770)
    parent.chmod(0o770)
    root = parent / "fresh-cache"
    monkeypatch.setattr(import_pipeline, "PACK_STAGING_ROOT", str(root))
    assert _queue_images(pack_env, "cache-bootstrap") is None
    assert not root.exists(), "fresh cache mkdir also lacks atomic inode birth proof"
    with sqlite3.connect(pack_env["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM import_queue").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM import_queue_files").fetchone() == (0,)


@pytest.mark.parametrize("phase", ["discarding", "discarded", "post-gc"])
def test_private_placement_durable_discard_replays_without_carrier_residue(
    pack_env: _PackEnv, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    import import_pack_cleanup as cleanup

    _provision(pack_env)

    def unsupported(_source: str, destination: str) -> None:
        raise OSError(errno.EINVAL, "unsupported", destination)

    monkeypatch.setattr(cleanup, "_rename_noreplace", unsupported)
    queue_id = _queue_images(pack_env, "placement-crash")
    assert queue_id is not None
    with sqlite3.connect(pack_env["db_path"]) as db:
        db.execute("UPDATE import_queue SET status='failed' WHERE id=?", (queue_id,))
    save, gc = cleanup._save_ownership, cleanup.gc_discarded_carrier

    def crash(guard, reservation, ownership, **kwargs):
        result = save(guard, reservation, ownership, **kwargs)
        if (
            ownership.placement_carrier is not None
            and ownership.placement_carrier.phase == phase
        ):
            raise RuntimeError("crash after durable placement disposition")
        return result

    def after_gc(*args, **kwargs):
        gc(*args, **kwargs)
        raise RuntimeError("crash after durable placement disposition")

    with monkeypatch.context() as fault:
        fault.setattr(cleanup, "_save_ownership", crash)
        if phase == "post-gc":
            fault.setattr(cleanup, "gc_discarded_carrier", after_gc)
        with pytest.raises(RuntimeError, match="durable placement disposition"):
            cleanup.cleanup_terminal_pack_staging(
                queue_id, "placement-crash", download_client_id=None, protocol=None
            )
    _expire(pack_env)
    assert cleanup.recover_pack_cleanup_state().reservations_recovered == 1
    assert {p.name for p in (pack_env["pack_root"] / ".mangarr-claims").iterdir()} == {
        "owner.json"
    }
    assert cleanup.recover_pack_cleanup_state() == cleanup.PackCleanupRecovery()


def test_private_fallback_queue_commit_rollback_recovers_without_nested_namespace(
    pack_env: _PackEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    import import_pack_cleanup as cleanup

    _provision(pack_env)

    def unsupported(source: str, destination: str) -> None:
        raise OSError(errno.EINVAL, "unsupported", destination)

    original = sqlite3.connect

    class FailedConnection(sqlite3.Connection):
        def executemany(self, sql, parameters):
            if sql.startswith("INSERT INTO import_queue_files"):
                raise OSError(errno.EIO, "queue decision failed")
            return super().executemany(sql, parameters)

    def connect(*args, **kwargs):
        kwargs["factory"] = FailedConnection
        return original(*args, **kwargs)

    monkeypatch.setattr(cleanup, "_rename_noreplace", unsupported)
    with monkeypatch.context() as fault:
        fault.setattr(sqlite3, "connect", connect)
        with pytest.raises(OSError, match="queue decision failed"):
            _queue_images(pack_env, "private-rollback")
    with original(pack_env["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM import_queue").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM import_queue_files").fetchone() == (0,)
    _expire(pack_env)
    assert cleanup.recover_pack_cleanup_state().reservations_recovered == 1
    assert {p.name for p in (pack_env["pack_root"] / ".mangarr-claims").iterdir()} == {
        "owner.json"
    }


@pytest.mark.parametrize("shared_parent", [False, True])
def test_other_uid_public_substitution_never_receives_pack_marker_or_output(
    pack_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
    shared_parent: bool,
) -> None:
    """Retarget the immutable preidentity repro's removed public-mkdir hook."""
    import import_pack_cleanup as cleanup

    _provision(pack_env)
    parent = pack_env["pack_root"]
    if not shared_parent:
        parent.chmod(0o750)
    victim = parent / "preexisting-app-directory"
    victim.mkdir(mode=0o700)
    victim_inode = victim.stat().st_ino
    canonical, _ = _pack_paths("other-uid-placement")
    attempts: list[subprocess.CompletedProcess[str]] = []

    def unsupported(source: str, destination: str) -> None:
        script = "import os,sys; print(os.geteuid(),flush=True); os.rename('/attack/'+sys.argv[1], '/attack/'+sys.argv[2])"
        child = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--pull=never",
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--pids-limit=16",
                "--memory=128m",
                "--memory-swap=128m",
                "--cpus=1",
                "--user",
                f"{os.geteuid() + 1}:{os.getegid()}",
                "--mount",
                f"type=bind,src={parent},dst=/attack",
                "python:3.11-slim",
                "python",
                "-c",
                script,
                victim.name,
                canonical.name,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert child.stdout.strip() == str(os.geteuid() + 1)
        attempts.append(child)
        raise OSError(errno.EOPNOTSUPP, "unsupported", destination)

    monkeypatch.setattr(cleanup, "_rename_noreplace", unsupported)
    queue_id = _queue_images(pack_env, "other-uid-placement")
    assert len(attempts) == 1
    if shared_parent:
        assert attempts[0].returncode == 0, attempts[0].stderr
        assert canonical.stat().st_ino == victim_inode
        assert canonical.stat().st_uid == os.geteuid()
        assert list(canonical.iterdir()) == [], "victim received marker/output"
        assert queue_id is None
        with sqlite3.connect(pack_env["db_path"]) as db:
            assert db.execute("SELECT COUNT(*) FROM import_queue").fetchone() == (0,)
            assert db.execute("SELECT COUNT(*) FROM import_queue_files").fetchone() == (
                0,
            )
            assert db.execute(
                "SELECT COUNT(*) FROM import_pack_cleanup_reservations"
            ).fetchone() == (1,)
    else:
        assert attempts[0].returncode != 0 and "PermissionError" in attempts[0].stderr
        assert victim.stat().st_ino == victim_inode and list(victim.iterdir()) == []
        assert queue_id is not None and not canonical.exists()


def test_late_occupied_logical_canonical_refuses_private_queue_decision(
    pack_env: _PackEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    import import_pack_cleanup as cleanup
    import import_queue

    _provision(pack_env)
    canonical, _ = _pack_paths("late-canonical")
    original = cleanup._commit_generated_pack_queue

    def unsupported(source: str, destination: str) -> None:
        raise OSError(errno.EINVAL, "unsupported", destination)

    def collide(guard, reservation, values, file_rows, **kwargs):
        canonical.mkdir(mode=0o700)
        return original(guard, reservation, values, file_rows, **kwargs)

    monkeypatch.setattr(cleanup, "_rename_noreplace", unsupported)
    monkeypatch.setattr(import_queue, "_commit_generated_pack_queue", collide)
    assert _queue_images(pack_env, "late-canonical") is None
    assert list(canonical.iterdir()) == []
    with sqlite3.connect(pack_env["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM import_queue").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM import_queue_files").fetchone() == (0,)
