"""NFS pack lifecycle qualification at real queue/recovery boundaries."""

from __future__ import annotations

import errno
import json
import sqlite3
import subprocess
import zipfile
from pathlib import Path

import pytest

from import_pack_cleanup import _rename_noreplace as _NATIVE_RENAME
from test_import_pack_cleanup_durability import (
    _PackEnv,
    _pack_paths,
    _probe_writer,
    _terminal_queue,
    _write_cbz,
    pack_env,  # noqa: F401
)


@pytest.fixture(params=[errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP])
def unsupported_rename(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> int:
    import import_pack_cleanup

    error = int(request.param)

    def reject(source: str, destination: str) -> None:
        raise OSError(error, "injected unsupported directory rename", destination)

    monkeypatch.setattr(import_pack_cleanup, "_rename_noreplace", reject)
    return error


def _queue_images(env: _PackEnv, download_id: str) -> int | None:
    import import_queue
    import main

    source = env["tmp_path"] / download_id
    chapter = source / "Pack Series c001"
    chapter.mkdir(parents=True)
    (chapter / "001.jpg").write_bytes(b"page-one")
    with main.get_db() as db:
        queue_id, _ = import_queue._queue_import(
            db,
            1,
            download_id,
            "Pack Series c001",
            "magnet:" + download_id,
            None,
            str(source),
        )
    return queue_id


def _expire(env: _PackEnv) -> None:
    with sqlite3.connect(env["db_path"]) as db:
        db.execute(
            "UPDATE import_pack_cleanup_reservations"
            " SET expires_at=datetime('now', '-1 second')"
        )


def test_generated_queue_attach_without_rename_noreplace(
    pack_env: _PackEnv,
    unsupported_rename: int,
) -> None:
    queue_id = _queue_images(pack_env, "nfs-generated")
    assert queue_id is not None
    with sqlite3.connect(pack_env["db_path"]) as db:
        rows = db.execute(
            "SELECT src_path FROM import_queue_files WHERE queue_id=?",
            (queue_id,),
        ).fetchall()
        assert len(rows) == 1
        canonical, _ = _pack_paths("nfs-generated")
        ownership = json.loads(
            db.execute(
                "SELECT directory_ownership_json FROM import_pack_cleanup_reservations"
            ).fetchone()[0]
        )
        actual = Path(ownership["canonical_directory"]["path"])
        assert Path(rows[0][0]).parent == actual
        assert (
            actual == Path(ownership["placement_carrier"]["carrier_path"]) / "artifact"
        )
        assert not canonical.exists()
        assert Path(rows[0][0]).is_file()
        assert db.execute(
            "SELECT purpose,queue_id FROM import_pack_cleanup_reservations"
        ).fetchone() == ("queueing", queue_id)
        assert db.execute(
            "SELECT COUNT(*) FROM import_pack_cleanup_reservations WHERE purpose='cleanup'"
        ).fetchone() == (0,)


def test_terminal_pack_detach_without_rename_noreplace(
    pack_env: _PackEnv,
    unsupported_rename: int,
) -> None:
    import import_pack_cleanup

    queue_id = _terminal_queue(pack_env["db_path"], "nfs-terminal")
    canonical, _ = _pack_paths("nfs-terminal")
    canonical.mkdir(parents=True)
    (canonical / "page.cbz").write_bytes(b"page")
    assert import_pack_cleanup.cleanup_terminal_pack_staging(
        queue_id,
        "nfs-terminal",
        download_client_id=None,
        protocol=None,
    )
    assert not canonical.exists()
    assert (
        import_pack_cleanup.recover_pack_cleanup_state()
        == import_pack_cleanup.PackCleanupRecovery()
    )


def test_abandoned_queue_recovery_without_rename_noreplace(
    pack_env: _PackEnv,
    unsupported_rename: int,
) -> None:
    import import_pack_cleanup
    import main

    with main.get_db() as db:
        owner = import_pack_cleanup.reserve_pack_queue_creation(
            db,
            "nfs-abandoned",
            download_client_id=None,
            protocol=None,
        )
    assert owner is not None
    _, private = _pack_paths("nfs-abandoned", owner)
    private.mkdir(parents=True)
    (private / "page.cbz").write_bytes(b"page")
    _expire(pack_env)
    result = import_pack_cleanup.recover_pack_cleanup_state()
    assert result.reservations_recovered == 1
    assert result.tombstones_removed == 1
    assert not private.exists()
    assert (
        import_pack_cleanup.recover_pack_cleanup_state()
        == import_pack_cleanup.PackCleanupRecovery()
    )


def test_legacy_tracked_tombstone_cleanup_without_rename_noreplace(
    pack_env: _PackEnv,
    unsupported_rename: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_pack_cleanup

    # Create an actual legacy journal through the native path, then replay on
    # an unsupported filesystem. No invented tombstone SQL fixture is needed.
    with monkeypatch.context() as native:
        native.setattr(import_pack_cleanup, "_rename_noreplace", _NATIVE_RENAME)
        native.setattr(
            import_pack_cleanup,
            "_remove_tracked_tombstone",
            lambda row, **kwargs: False,
        )
        queue_id = _terminal_queue(pack_env["db_path"], "nfs-tracked")
        canonical, _ = _pack_paths("nfs-tracked")
        canonical.mkdir(parents=True)
        (canonical / "page.cbz").write_bytes(b"page")
        assert not import_pack_cleanup.cleanup_terminal_pack_staging(
            queue_id,
            "nfs-tracked",
            download_client_id=None,
            protocol=None,
        )
    result = import_pack_cleanup.recover_pack_cleanup_state()
    assert result.tombstones_removed == 1
    assert result.tombstones_retained == 0
    assert not canonical.exists()


def test_partial_canonical_attach_failure_retains_reservation_and_private_proof(
    pack_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_pack_cleanup
    import import_queue

    private_paths: list[Path] = []

    def partial(download_id: str, owner: str, **kwargs: object) -> str:
        canonical, private = _pack_paths(download_id, owner)
        with sqlite3.connect(pack_env["db_path"]) as db:
            ownership = json.loads(
                db.execute(
                    "SELECT directory_ownership_json FROM import_pack_cleanup_reservations"
                ).fetchone()[0]
            )
        private_paths.append(Path(ownership["private_directory"]["path"]))
        canonical.mkdir(mode=0o700)
        import_pack_cleanup._write_pack_owner_marker(
            str(canonical),
            import_pack_cleanup.DownloadIdentity(None, None, download_id),
            owner,
        )
        (canonical / "partial.cbz").write_bytes(b"partial")
        _probe_writer(pack_env["db_path"], "writer-during-partial-attach")
        raise OSError(errno.EIO, "injected partial attachment barrier failure")

    monkeypatch.setattr(import_queue, "durably_attach_pack_queue_directory", partial)
    assert _queue_images(pack_env, "partial-attach") is None
    with sqlite3.connect(pack_env["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM import_queue").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM import_queue_files").fetchone() == (0,)
        assert db.execute(
            "SELECT purpose FROM import_pack_cleanup_reservations"
        ).fetchone() == ("cleanup",)
    assert private_paths[0].is_dir()
    assert list(private_paths[0].glob("*.cbz"))


def test_abandoned_private_and_canonical_recovery_never_releases_partial_fence(
    pack_env: _PackEnv,
) -> None:
    import import_pack_cleanup
    import main

    with main.get_db() as db:
        owner = import_pack_cleanup.reserve_pack_queue_creation(
            db,
            "two-trees",
            download_client_id=None,
            protocol=None,
        )
    assert owner is not None
    canonical, private = _pack_paths("two-trees", owner)
    private.mkdir(parents=True)
    canonical.mkdir()
    (private / "private.cbz").write_bytes(b"private")
    (canonical / "unrelated.cbz").write_bytes(b"unrelated")
    _expire(pack_env)
    import_pack_cleanup.recover_pack_cleanup_state()
    assert (canonical / "unrelated.cbz").read_bytes() == b"unrelated"
    assert (private / "private.cbz").read_bytes() == b"private"
    with main.get_db() as db:
        assert (
            import_pack_cleanup.reserve_pack_queue_creation(
                db,
                "two-trees",
                download_client_id=None,
                protocol=None,
            )
            is None
        )


def test_lost_attach_reservation_keeps_partial_tree_and_private_proof(
    pack_env: _PackEnv,
) -> None:
    import import_pack_cleanup
    import import_queue
    import main

    with main.get_db() as db:
        owner = import_pack_cleanup.reserve_pack_queue_creation(
            db,
            "lost-partial",
            download_client_id=None,
            protocol=None,
        )
        assert owner is not None
        assert import_pack_cleanup.begin_pack_queue_attachment(
            db,
            "lost-partial",
            owner,
            download_client_id=None,
            protocol=None,
        )
        canonical, private = _pack_paths("lost-partial", owner)
        private.mkdir(parents=True)
        canonical.mkdir()
        (private / "private.cbz").write_bytes(b"private")
        (canonical / "partial.cbz").write_bytes(b"partial")
        _expire(pack_env)
        heartbeat = import_queue._PackQueueHeartbeat(
            db, "lost-partial", None, None, owner
        )
        with pytest.raises(import_queue._PackQueueReservationLost):
            heartbeat.checkpoint(force=True)
    assert (private / "private.cbz").read_bytes() == b"private"
    assert (canonical / "partial.cbz").read_bytes() == b"partial"
    with sqlite3.connect(pack_env["db_path"]) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM import_pack_cleanup_reservations"
        ).fetchone() == (1,)


def test_nested_zip_wrapper_queue_attach_without_rename_noreplace(
    pack_env: _PackEnv,
    unsupported_rename: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_queue
    import main

    source = pack_env["tmp_path"] / "zip-wrapper"
    source.mkdir()
    for name, part in (("one.zip", "scene.rar"), ("two.zip", "scene.r00")):
        with zipfile.ZipFile(source / name, "w") as archive:
            archive.writestr(part, b"opaque split archive")

    def extract(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert command[:3] == ["unrar", "x", "-o+"]
        nested = Path(command[-1]) / "nested"
        nested.mkdir(parents=True)
        _write_cbz(nested / "Pack Series c001.cbz")
        _probe_writer(pack_env["db_path"], "writer-during-extract")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(import_queue.shutil, "which", lambda command: None)
    monkeypatch.setattr(
        import_queue,
        "_run_pack_extractor",
        lambda command, heartbeat, output_fd: extract(command),
    )
    with main.get_db() as db:
        queue_id, _ = import_queue._queue_import(
            db,
            1,
            "nested-wrapper",
            "Pack Series c001",
            "magnet:nested-wrapper",
            None,
            str(source),
        )
    assert queue_id is not None
    canonical, _ = _pack_paths("nested-wrapper")
    with sqlite3.connect(pack_env["db_path"]) as db:
        row = db.execute(
            "SELECT src_path FROM import_queue_files WHERE queue_id=?", (queue_id,)
        ).fetchone()
        assert row is not None
        ownership = json.loads(
            db.execute(
                "SELECT directory_ownership_json FROM import_pack_cleanup_reservations"
            ).fetchone()[0]
        )
        actual = Path(ownership["canonical_directory"]["path"])
        assert (
            Path(row[0]) == actual / "split-rar/group-1/out/nested/Pack Series c001.cbz"
        )
        assert not canonical.exists()
        assert Path(row[0]).is_file()


@pytest.mark.parametrize("same_owner_marker", [False, True])
def test_occupied_canonical_is_not_adopted_even_with_matching_owner_marker(
    pack_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
    same_owner_marker: bool,
) -> None:
    import import_pack_cleanup

    owner = "occupied-owner"
    monkeypatch.setattr(
        import_pack_cleanup.secrets, "token_urlsafe", lambda length: owner
    )
    canonical, private = _pack_paths("occupied-canonical", owner)
    canonical.mkdir(parents=True)
    if same_owner_marker:
        import_pack_cleanup._write_pack_owner_marker(
            str(canonical),
            import_pack_cleanup.DownloadIdentity(None, None, "occupied-canonical"),
            owner,
        )
    before = {p.name: p.read_bytes() for p in canonical.iterdir()}
    assert _queue_images(pack_env, "occupied-canonical") is None
    assert {p.name: p.read_bytes() for p in canonical.iterdir()} == before
    with sqlite3.connect(pack_env["db_path"]) as db:
        ownership = json.loads(
            db.execute(
                "SELECT directory_ownership_json FROM import_pack_cleanup_reservations"
            ).fetchone()[0]
        )
        assert Path(ownership["private_directory"]["path"]).is_dir()
        assert db.execute("SELECT COUNT(*) FROM import_queue_files").fetchone() == (0,)
        assert db.execute(
            "SELECT COUNT(*) FROM import_pack_cleanup_reservations"
        ).fetchone() == (1,)


def test_ambiguous_network_eexist_after_attach_retains_journal_for_replay(
    pack_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_pack_cleanup

    def ambiguous(source: str, destination: str) -> None:
        _NATIVE_RENAME(source, destination)
        raise FileExistsError(errno.EEXIST, "injected lost rename reply", destination)

    with monkeypatch.context() as fault:
        fault.setattr(import_pack_cleanup, "_rename_noreplace", ambiguous)
        assert _queue_images(pack_env, "ambiguous-attach") is None
    canonical, _ = _pack_paths("ambiguous-attach")
    assert canonical.is_dir()
    with sqlite3.connect(pack_env["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM import_queue_files").fetchone() == (0,)
        assert db.execute(
            "SELECT COUNT(*) FROM import_pack_cleanup_reservations"
        ).fetchone() == (1,)
    _expire(pack_env)
    replay = import_pack_cleanup.recover_pack_cleanup_state()
    assert replay.reservations_recovered == 1
    assert replay.tombstones_removed == 1
    assert not canonical.exists()


def test_terminal_cleanup_does_not_delete_an_occupied_tombstone(
    pack_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_pack_cleanup

    monkeypatch.setattr(
        import_pack_cleanup.secrets, "token_urlsafe", lambda length: "occupied-cleanup"
    )
    queue_id = _terminal_queue(pack_env["db_path"], "occupied-tombstone")
    canonical, _ = _pack_paths("occupied-tombstone")
    canonical.mkdir(parents=True)
    (canonical / "owned.cbz").write_bytes(b"owned")
    tombstone = canonical.with_name(canonical.name + ".cleanup-occupied-cleanup")
    tombstone.mkdir()
    (tombstone / "unrelated.cbz").write_bytes(b"unrelated")
    assert not import_pack_cleanup.cleanup_terminal_pack_staging(
        queue_id,
        "occupied-tombstone",
        download_client_id=None,
        protocol=None,
    )
    assert (canonical / "owned.cbz").read_bytes() == b"owned"
    assert (tombstone / "unrelated.cbz").read_bytes() == b"unrelated"


def test_changed_source_at_terminal_detach_is_never_deleted(
    pack_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_pack_cleanup

    queue_id = _terminal_queue(pack_env["db_path"], "source-racer")
    canonical, _ = _pack_paths("source-racer")
    canonical.mkdir(parents=True)
    (canonical / "owned.cbz").write_bytes(b"owned")
    original = canonical.with_name(canonical.name + ".retained-original")

    def replace_source(source: str, destination: str) -> None:
        Path(source).rename(original)
        Path(source).mkdir()
        (Path(source) / "unrelated.cbz").write_bytes(b"unrelated")
        _NATIVE_RENAME(source, destination)

    monkeypatch.setattr(import_pack_cleanup, "_rename_noreplace", replace_source)
    assert not import_pack_cleanup.cleanup_terminal_pack_staging(
        queue_id,
        "source-racer",
        download_client_id=None,
        protocol=None,
    )
    assert (original / "owned.cbz").read_bytes() == b"owned"
    # It is acceptable to retain a mismatched private claim for intervention,
    # but neither the moved replacement nor the original may be discarded.
    assert any(
        p.read_bytes() == b"unrelated"
        for p in pack_env["pack_root"].rglob("unrelated.cbz")
    )
