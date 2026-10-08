"""Path metadata calls must not run while generated-queue SQL owns the writer."""

from __future__ import annotations

import errno
import os
import sqlite3
import zipfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from test_import_pack_cleanup_durability import _PackEnv, pack_env  # noqa: F401
from test_import_pack_nfs_lifecycle import _queue_images


def test_generated_queue_path_metadata_keeps_second_writer_responsive(
    pack_env: _PackEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    import import_pack_cleanup as cleanup
    import import_queue

    real_lstat = os.lstat
    real_get_db = cleanup.get_db
    real_commit = cleanup._commit_generated_pack_queue
    active_connections: list[sqlite3.Connection] = []
    targets: dict[str, str] = {}
    probes: list[tuple[str, bool, str | None]] = []

    @contextmanager
    def observe_connection() -> Generator[sqlite3.Connection, None, None]:
        with real_get_db() as db:
            active_connections.append(db)
            try:
                yield db
            finally:
                active_connections.remove(db)

    def observe_lstat(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
    ) -> os.stat_result:
        label = targets.get(os.fsdecode(path))
        if label is not None and dir_fd is None:
            owns_writer = any(db.in_transaction for db in active_connections)
            error = None
            # The contender is independent of the app connection and guard. Its
            # transaction is rolled back without altering application records.
            contender = sqlite3.connect(pack_env["db_path"], timeout=0)
            try:
                contender.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                error = str(exc)
            finally:
                contender.rollback()
                contender.close()
            probes.append((label, owns_writer, error))
        return real_lstat(path, dir_fd=dir_fd)

    def observe_commit(
        guard: Any,
        reservation: Any,
        values: tuple[object, ...],
        file_rows: list[tuple[object, ...]],
        **kwargs: Any,
    ) -> int:
        ownership = cleanup._ownership(reservation)
        assert ownership is not None and ownership.canonical_directory is not None
        physical = ownership.canonical_directory.path
        assert physical != reservation.pack_path
        targets[physical] = "lstat(physical_directory)"
        targets[reservation.pack_path] = "lexists(logical_canonical)"
        try:
            return real_commit(guard, reservation, values, file_rows, **kwargs)
        finally:
            targets.clear()

    def unsupported(source: str, destination: str) -> None:
        raise OSError(errno.EINVAL, "unsupported directory NOREPLACE", destination)

    monkeypatch.setattr(cleanup, "get_db", observe_connection)
    monkeypatch.setattr(cleanup, "_rename_noreplace", unsupported)
    monkeypatch.setattr(import_queue, "_commit_generated_pack_queue", observe_commit)
    monkeypatch.setattr(os, "lstat", observe_lstat)
    queue_id = _queue_images(pack_env, "metadata-writer-boundary")

    # Keep real queue admission, complete rows and generated payload as controls;
    # collecting a failed probe must not abort or bypass the production commit.
    assert queue_id is not None
    with sqlite3.connect(pack_env["db_path"]) as db:
        assert db.execute("SELECT id,status FROM import_queue").fetchall() == [
            (queue_id, "pending")
        ]
        rows = db.execute("SELECT queue_id,src_path FROM import_queue_files").fetchall()
        assert len(rows) == 1 and rows[0][0] == queue_id
        assert db.execute(
            "SELECT queue_id FROM import_pack_cleanup_reservations"
        ).fetchall() == [(queue_id,)]
    with zipfile.ZipFile(Path(rows[0][1])) as archive:
        assert archive.read("001.jpg") == b"page-one"
    assert {label for label, _, _ in probes} == {
        "lstat(physical_directory)",
        "lexists(logical_canonical)",
    }
    assert probes and any(
        not owns_writer and error is None for _, owns_writer, error in probes
    )
    assert all(error is None for _, _, error in probes), probes
