"""Local-filesystem fault injection for private-stage NFS publication."""

from __future__ import annotations

import asyncio
import ctypes
import errno
import hashlib
import os
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import test_import_publication_journal as journal_tests
from test_import_publication_journal import journal_env  # noqa: F401


def _unsupported_libc(monkeypatch: pytest.MonkeyPatch, error: int | None) -> None:
    import import_publication

    class Rename:
        def __call__(self, *args: object) -> int:
            ctypes.set_errno(error or errno.ENOSYS)
            return -1

    libc = SimpleNamespace() if error is None else SimpleNamespace(renameat2=Rename())
    monkeypatch.setattr(import_publication.ctypes, "CDLL", lambda *a, **kw: libc)


def _journal(env: dict[str, Path]) -> tuple[object, ...]:
    with sqlite3.connect(env["db_path"]) as db:
        row = db.execute(
            "SELECT p.id,p.state,p.staging_dir,f.stage_path,f.final_path,"
            " f.staged_sha256,f.cleanup_state,p.diagnostic"
            " FROM import_publications p JOIN import_publication_files f"
            " ON f.publication_id=p.id ORDER BY f.file_id LIMIT 1"
        ).fetchone()
    assert row is not None
    return row


def _assert_not_committed(env: dict[str, Path], series_id: int) -> None:
    with sqlite3.connect(env["db_path"]) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM history WHERE series_id=? AND event_type='imported'",
            (series_id,),
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT status,import_path FROM volumes WHERE series_id=?", (series_id,)
        ).fetchone() == ("grabbed", None)


def _replay() -> int:
    from import_publication import ReplaySummary

    return cast(ReplaySummary, journal_tests._run_replay()).blocked


@pytest.mark.parametrize("mode", ["copy", "hardlink"])
@pytest.mark.parametrize("error", [errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, None])
def test_private_stage_pipeline_completes_without_publication_unlink(
    journal_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    error: int | None,
) -> None:
    import import_execute
    import import_publication

    queue_id, series_id, sources, finals = journal_tests._seed_queue(
        journal_env, mode=mode
    )
    original = [source.read_bytes() for source in sources]
    _unsupported_libc(monkeypatch, error)
    real_fsync = import_publication._fsync_directory
    real_link = os.link
    real_unlink = os.unlink
    real_phase3 = import_publication.claim_publication_phase3
    events: list[tuple[str, str]] = []
    linked = False
    phase3_started = False

    def link(source: str, destination: str, **kwargs: Any) -> None:
        nonlocal linked
        real_link(source, destination, **kwargs)
        if kwargs.get("follow_symlinks") is False:
            linked = True
            events.append(("link", destination))
            assert Path(source).is_file()

    def fsync(path: str) -> None:
        real_fsync(path)
        if linked and not phase3_started:
            events.append(("fsync", path))

    def unlink(path: str, *args: Any, **kwargs: Any) -> None:
        if linked:
            assert _journal(journal_env)[1] in ("db_committed", "cleaning", "deleted")
        real_unlink(path, *args, **kwargs)

    def phase3(db: sqlite3.Connection, publication_id: int, owner_token: str) -> bool:
        nonlocal phase3_started
        row = _journal(journal_env)
        assert row[1] == "published"
        assert Path(str(row[3])).is_file()
        assert Path(str(row[4])).is_file()
        assert events[:3] == [
            ("link", str(finals[0])),
            ("fsync", str(finals[0].parent)),
            ("fsync", str(Path(str(row[3])).parent)),
        ]
        result = real_phase3(db, publication_id, owner_token)
        phase3_started = True
        return result

    monkeypatch.setattr(os, "link", link)
    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(import_publication, "_fsync_directory", fsync)
    monkeypatch.setattr(import_publication, "claim_publication_phase3", phase3)

    assert asyncio.run(import_execute._execute_import(queue_id))
    row = _journal(journal_env)
    assert row[1] == "deleted"
    assert not Path(str(row[2])).exists()
    assert [source.read_bytes() for source in sources] == original
    assert hashlib.sha256(finals[0].read_bytes()).hexdigest() == row[5]
    for final in finals:
        with zipfile.ZipFile(final) as archive:
            assert archive.testzip() is None
    journal_tests._assert_exactly_once(journal_env, series_id)


@pytest.mark.parametrize("same_inode", [False, True])
def test_incoming_destination_winner_is_retained_and_replay_blocks(
    journal_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    same_inode: bool,
) -> None:
    import import_execute
    import import_publication

    queue_id, series_id, sources, finals = journal_tests._seed_queue(
        journal_env, file_count=1
    )
    original = sources[0].read_bytes()
    inserted = False
    winner = b"unrelated incoming winner"

    def occupied(source: str, destination: str) -> None:
        nonlocal inserted, winner
        if not inserted:
            inserted = True
            if same_inode:
                os.link(source, destination, follow_symlinks=False)
                winner = Path(source).read_bytes()
            else:
                Path(destination).write_bytes(winner)
        raise OSError(errno.EOPNOTSUPP, "injected unsupported rename", destination)

    monkeypatch.setattr(import_publication, "_rename_noreplace", occupied)
    assert not asyncio.run(import_execute._execute_import(queue_id))
    row = _journal(journal_env)
    stage = Path(str(row[3]))
    stage_bytes = stage.read_bytes()
    assert row[1] == "publishing"
    assert finals[0].read_bytes() == winner
    if same_inode:
        assert os.path.samefile(stage, finals[0])
    _assert_not_committed(journal_env, series_id)
    assert _replay() == 1
    assert stage.read_bytes() == stage_bytes
    assert finals[0].read_bytes() == winner
    assert sources[0].read_bytes() == original
    _assert_not_committed(journal_env, series_id)


@pytest.mark.parametrize("error", [errno.EPERM, errno.EXDEV, errno.EACCES, errno.EIO])
def test_private_publisher_does_not_link_for_other_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: int
) -> None:
    import import_publication

    stage = tmp_path / "stage"
    final = tmp_path / "final"
    stage.write_bytes(b"stage")
    _unsupported_libc(monkeypatch, error)

    def forbidden_link(*args: object, **kwargs: object) -> None:
        pytest.fail("non-unsupported errors must not trigger linking")

    monkeypatch.setattr(os, "link", forbidden_link)
    with pytest.raises(OSError) as raised:
        import_publication._publish_absent_stage(str(stage), str(final))
    assert raised.value.errno == error
    assert stage.read_bytes() == b"stage"
    assert not final.exists()


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_private_publisher_refuses_nonregular_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    import import_publication

    target = tmp_path / "target"
    target.write_bytes(b"target")
    stage = tmp_path / "stage"
    final = tmp_path / "final"
    if kind == "directory":
        stage.mkdir()
    else:
        stage.symlink_to(target)
    _unsupported_libc(monkeypatch, errno.EOPNOTSUPP)
    with pytest.raises((OSError, import_publication.PublicationBlocked)):
        import_publication._publish_absent_stage(str(stage), str(final))
    assert os.path.lexists(stage)
    assert not os.path.lexists(final)
    assert target.read_bytes() == b"target"


def test_private_publisher_still_fails_closed_off_linux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import import_publication

    stage = tmp_path / "stage"
    final = tmp_path / "final"
    stage.write_bytes(b"stage")
    monkeypatch.setattr(import_publication.sys, "platform", "unsupported")
    with pytest.raises(OSError):
        import_publication._publish_absent_stage(str(stage), str(final))
    assert stage.read_bytes() == b"stage"
    assert not final.exists()


def test_native_private_publication_preserves_fsync_order(
    journal_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import import_execute
    import import_publication

    queue_id, _, _, finals = journal_tests._seed_queue(journal_env, file_count=1)
    real_rename = import_publication._rename_noreplace
    real_fsync = import_publication._fsync_directory
    events: list[tuple[str, str]] = []
    stage_dir = ""

    def rename(source: str, destination: str) -> None:
        nonlocal stage_dir
        stage_dir = str(Path(source).parent)
        real_rename(source, destination)
        assert not os.path.lexists(source)
        events.append(("rename", destination))

    def fsync(path: str) -> None:
        real_fsync(path)
        if stage_dir:
            events.append(("fsync", path))

    def forbidden_link(*args: object, **kwargs: object) -> None:
        pytest.fail("native publication must not invoke the fallback")

    monkeypatch.setattr(import_publication, "_rename_noreplace", rename)
    monkeypatch.setattr(import_publication, "_fsync_directory", fsync)
    monkeypatch.setattr(os, "link", forbidden_link)
    assert asyncio.run(import_execute._execute_import(queue_id))
    assert events[:3] == [
        ("rename", str(finals[0])),
        ("fsync", str(finals[0].parent)),
        ("fsync", stage_dir),
    ]


def test_sigkill_after_private_link_retains_both_names_and_blocks_replay(
    journal_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    queue_id, series_id, sources, finals = journal_tests._seed_queue(
        journal_env, file_count=1
    )
    original = sources[0].read_bytes()
    injection = """
if crash_kind == "nfs_link":
    import errno
    def unsupported(src, dst):
        raise OSError(errno.EOPNOTSUPP, "injected unsupported rename", dst)
    import_publication._rename_noreplace = unsupported
    real_link = os.link
    def link_and_die(src, dst, **kwargs):
        real_link(src, dst, **kwargs)
        die()
    os.link = link_and_die
"""
    monkeypatch.setattr(
        journal_tests,
        "_CRASH_CHILD",
        journal_tests._CRASH_CHILD.replace(
            "asyncio.run(import_execute._execute_import(queue_id))",
            injection + "\nasyncio.run(import_execute._execute_import(queue_id))",
        ),
    )
    journal_tests._crash_worker(journal_env, queue_id, kind="nfs_link")
    row = _journal(journal_env)
    stage = Path(str(row[3]))
    content = stage.read_bytes()
    assert row[1] == "publishing"
    assert finals[0].read_bytes() == content
    assert os.path.samefile(stage, finals[0])
    _assert_not_committed(journal_env, series_id)
    assert _replay() == 1
    assert stage.read_bytes() == content
    assert finals[0].read_bytes() == content
    assert sources[0].read_bytes() == original
    _assert_not_committed(journal_env, series_id)


@pytest.mark.parametrize("failed_barrier", [1, 2])
def test_private_link_directory_fsync_failure_blocks_without_cleanup(
    journal_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    failed_barrier: int,
) -> None:
    import import_execute
    import import_publication

    queue_id, series_id, _, finals = journal_tests._seed_queue(
        journal_env, file_count=1
    )
    _unsupported_libc(monkeypatch, errno.EOPNOTSUPP)
    real_link = os.link
    real_fsync = import_publication._fsync_directory
    linked = False
    barriers = 0

    def link(source: str, destination: str, **kwargs: Any) -> None:
        nonlocal linked
        real_link(source, destination, **kwargs)
        linked = True

    def fsync(path: str) -> None:
        nonlocal barriers
        if linked:
            barriers += 1
            if barriers == failed_barrier:
                raise OSError(errno.EIO, "injected directory fsync failure", path)
        real_fsync(path)

    monkeypatch.setattr(os, "link", link)
    monkeypatch.setattr(import_publication, "_fsync_directory", fsync)
    assert not asyncio.run(import_execute._execute_import(queue_id))
    row = _journal(journal_env)
    stage = Path(str(row[3]))
    assert linked and barriers == failed_barrier
    content = stage.read_bytes()
    assert finals[0].read_bytes() == content
    _assert_not_committed(journal_env, series_id)
    assert _replay() == 1
    assert stage.read_bytes() == content
    assert finals[0].read_bytes() == content
    _assert_not_committed(journal_env, series_id)


def test_move_publishes_but_unsupported_source_claim_remains_blocked(
    journal_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import import_execute

    queue_id, _, sources, finals = journal_tests._seed_queue(
        journal_env, mode="move", file_count=1
    )
    original = sources[0].read_bytes()
    _unsupported_libc(monkeypatch, errno.EOPNOTSUPP)
    assert not asyncio.run(import_execute._execute_import(queue_id))
    row = _journal(journal_env)
    assert row[1] == "cleaning"
    assert row[6] == "blocked"
    assert sources[0].read_bytes() == original
    assert hashlib.sha256(finals[0].read_bytes()).hexdigest() == row[5]
    assert _replay() == 1
    assert sources[0].read_bytes() == original
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM import_queue").fetchone() == (1,)


def test_move_completes_when_only_private_publication_needs_fallback(
    journal_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import import_execute
    import import_publication

    queue_id, series_id, sources, finals = journal_tests._seed_queue(
        journal_env, mode="move", file_count=1
    )
    real_rename = import_publication._rename_noreplace

    def unsupported_stage_only(source: str, destination: str) -> None:
        if Path(source).parent.name.startswith(".mangarr-publication-"):
            raise OSError(
                errno.EOPNOTSUPP, "unsupported stage publication", destination
            )
        real_rename(source, destination)

    monkeypatch.setattr(import_publication, "_rename_noreplace", unsupported_stage_only)
    assert asyncio.run(import_execute._execute_import(queue_id))
    assert not sources[0].exists()
    assert finals[0].is_file()
    assert _journal(journal_env)[1] == "deleted"
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM history WHERE series_id=? AND event_type='imported'",
            (series_id,),
        ).fetchone() == (1,)


def test_unsupported_overwrite_does_not_use_private_stage_fallback(
    journal_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import import_publication

    publication_id, series_id, source, final, stage, claim = (
        journal_tests._prepare_overwrite_publication(journal_env, monkeypatch)
    )
    original = final.read_bytes()
    source_bytes = source.read_bytes()
    stage_bytes = stage.read_bytes()
    _unsupported_libc(monkeypatch, errno.EOPNOTSUPP)
    assert not import_publication.publish_publication(publication_id, "nfs-owner")
    assert final.read_bytes() == original
    assert stage.read_bytes() == stage_bytes
    assert source.read_bytes() == source_bytes
    assert not claim.exists()
    _assert_not_committed(journal_env, series_id)


@pytest.mark.parametrize("kind", ["file", "hardlink", "directory", "symlink"])
def test_private_link_claim_never_replaces_an_occupied_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    import import_publication

    stage = tmp_path / "stage"
    final = tmp_path / "final"
    stage.write_bytes(b"stage")
    if kind == "file":
        final.write_bytes(b"winner")
    elif kind == "hardlink":
        os.link(stage, final)
    elif kind == "directory":
        final.mkdir()
    else:
        final.symlink_to(stage)
    before = os.lstat(final)
    _unsupported_libc(monkeypatch, errno.EOPNOTSUPP)
    with pytest.raises(FileExistsError):
        import_publication._publish_absent_stage(str(stage), str(final))
    assert os.lstat(final) == before
    assert stage.read_bytes() == b"stage"
    if kind == "file":
        assert final.read_bytes() == b"winner"


@pytest.mark.parametrize("error", [errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, None])
def test_shared_path_adapter_still_refuses_unsupported_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: int | None
) -> None:
    import import_publication

    source = tmp_path / "shared-source"
    claim = tmp_path / "shared-claim"
    source.write_bytes(b"source")
    _unsupported_libc(monkeypatch, error)
    with pytest.raises(OSError):
        import_publication._rename_noreplace(str(source), str(claim))
    assert source.read_bytes() == b"source"
    assert not claim.exists()


@pytest.mark.parametrize("error", [errno.EPERM, errno.EXDEV])
def test_link_failure_retains_stage_without_rollback_or_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: int
) -> None:
    import import_publication

    stage = tmp_path / "stage"
    final = tmp_path / "final"
    stage.write_bytes(b"stage")
    _unsupported_libc(monkeypatch, errno.EOPNOTSUPP)

    def denied_link(*args: object, **kwargs: object) -> None:
        raise OSError(error, "injected link failure", str(final))

    monkeypatch.setattr(os, "link", denied_link)
    with pytest.raises(OSError) as raised:
        import_publication._publish_absent_stage(str(stage), str(final))
    assert raised.value.errno == error
    assert stage.read_bytes() == b"stage"
    assert not final.exists()
