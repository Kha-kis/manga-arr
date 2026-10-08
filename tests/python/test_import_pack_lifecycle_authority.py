"""Pack ownership and queue decision contracts before shared-carrier integration."""

from __future__ import annotations

import os
import sqlite3
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from test_import_pack_cleanup_durability import (
    _PackEnv,
    _pack_paths,
    _terminal_queue,
    _write_cbz,
    pack_env,  # noqa: F401
)
from test_import_pack_nfs_lifecycle import _expire, _queue_images


def _generate_on_db(
    env: _PackEnv, db: sqlite3.Connection, download_id: str
) -> int | None:
    import import_queue

    source = env["tmp_path"] / download_id
    chapter = source / "Pack Series c001"
    chapter.mkdir(parents=True)
    (chapter / "001.jpg").write_bytes(b"page-one")
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


def _expired_attachment(env: _PackEnv, download_id: str) -> None:
    import import_pack_cleanup
    import main

    with main.get_db() as db:
        owner = import_pack_cleanup.reserve_pack_queue_creation(
            db, download_id, download_client_id=None, protocol=None
        )
        assert owner is not None
        assert import_pack_cleanup.begin_pack_queue_attachment(
            db, download_id, owner, download_client_id=None, protocol=None
        )
    _expire(env)


def test_generated_queue_is_committed_before_return_to_caller(
    pack_env: _PackEnv,
) -> None:
    import main

    with main.get_db() as caller:
        queue_id = _generate_on_db(pack_env, caller, "commit-before-return")
        assert queue_id is not None
        with sqlite3.connect(pack_env["db_path"]) as observer:
            assert observer.execute(
                "SELECT COUNT(*) FROM import_queue WHERE id=?", (queue_id,)
            ).fetchone() == (1,)
            paths = observer.execute(
                "SELECT src_path FROM import_queue_files WHERE queue_id=?", (queue_id,)
            ).fetchall()
            assert len(paths) == 1
            assert Path(paths[0][0]).is_file()


def test_file_row_insert_failure_cannot_be_committed_as_partial_queue(
    pack_env: _PackEnv,
) -> None:
    import main

    with sqlite3.connect(pack_env["db_path"]) as db:
        db.execute(
            "CREATE TRIGGER refuse_pack_file BEFORE INSERT ON import_queue_files"
            " BEGIN SELECT RAISE(ABORT, 'injected file row failure'); END"
        )
    # Catching a statement error must not leave queue inserts for the outer
    # context's ordinary commit to publish without any file rows.
    with main.get_db() as caller:
        with pytest.raises(sqlite3.IntegrityError, match="injected file row failure"):
            _generate_on_db(pack_env, caller, "file-row-failure")
    with sqlite3.connect(pack_env["db_path"]) as observer:
        assert observer.execute("SELECT COUNT(*) FROM import_queue").fetchone() == (0,)
        assert observer.execute(
            "SELECT COUNT(*) FROM import_queue_files"
        ).fetchone() == (0,)
        canonical, _ = _pack_paths("file-row-failure")
        if canonical.exists():
            assert observer.execute(
                "SELECT purpose FROM import_pack_cleanup_reservations"
            ).fetchone() == ("cleanup",)


def test_interrupted_file_insert_cannot_be_committed_as_partial_queue(
    pack_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main

    original_connect = sqlite3.connect

    class InterruptedConnection(sqlite3.Connection):
        def executemany(self, sql: str, parameters: Any, /) -> sqlite3.Cursor:
            if sql.startswith("INSERT INTO import_queue_files"):
                raise KeyboardInterrupt("injected queue insertion interruption")
            return super().executemany(sql, parameters)

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs["factory"] = InterruptedConnection
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    with main.get_db() as caller:
        with pytest.raises(KeyboardInterrupt, match="injected queue insertion"):
            _generate_on_db(pack_env, caller, "interrupted-insert")
    with original_connect(pack_env["db_path"]) as observer:
        assert observer.execute("SELECT COUNT(*) FROM import_queue").fetchone() == (0,)
        assert observer.execute(
            "SELECT COUNT(*) FROM import_queue_files"
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "after_commit", [False, True], ids=["before", "ambiguous-after"]
)
def test_queue_commit_fault_is_resolved_inside_queue_creation(
    pack_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
    after_commit: bool,
) -> None:
    import main

    events: list[str] = []
    injected = False
    original_connect = sqlite3.connect

    class FaultConnection(sqlite3.Connection):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.queue_inserted = False
            self.set_authorizer(self.authorize)

        def authorize(
            self,
            action: int,
            first: str | None,
            second: str | None,
            database: str | None,
            trigger: str | None,
        ) -> int:
            if action == sqlite3.SQLITE_INSERT and first == "import_queue":
                self.queue_inserted = True
            return sqlite3.SQLITE_OK

        def commit(self) -> None:
            nonlocal injected
            if self.queue_inserted and not injected:
                injected = True
                events.append("queue-commit")
                if after_commit:
                    super().commit()
                raise sqlite3.OperationalError("injected queue commit outcome")
            super().commit()

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs["factory"] = FaultConnection
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    try:
        with main.get_db() as caller:
            _generate_on_db(pack_env, caller, "commit-fault")
            events.append("returned")
    except sqlite3.Error:
        events.append("raised")
    assert injected
    if "returned" in events:
        assert events.index("queue-commit") < events.index("returned")
    with original_connect(pack_env["db_path"]) as observer:
        expected = 1 if after_commit else 0
        assert observer.execute("SELECT COUNT(*) FROM import_queue").fetchone() == (
            expected,
        )
        assert observer.execute(
            "SELECT COUNT(*) FROM import_queue_files"
        ).fetchone() == (expected,)
        canonical, _ = _pack_paths("commit-fault")
        if canonical.exists():
            # An uncertain outcome cannot leave a tree without a durable fence
            # or an authoritative, complete queued decision.
            assert expected or observer.execute(
                "SELECT COUNT(*) FROM import_pack_cleanup_reservations"
                " WHERE purpose='cleanup'"
            ).fetchone() == (1,)


def test_expired_cleanup_reservation_still_blocks_pack_consumers(
    pack_env: _PackEnv,
) -> None:
    import import_pack_cleanup
    import main

    _expired_attachment(pack_env, "expired-pack-fence")
    with main.get_db() as db:
        assert import_pack_cleanup.cleanup_reservation_blocks(
            db, "expired-pack-fence", download_client_id=None, protocol=None
        )


@pytest.mark.parametrize("matching", [True, False], ids=["same-identity", "unrelated"])
def test_queue_admission_respects_expired_pack_cleanup_fence(
    pack_env: _PackEnv,
    matching: bool,
) -> None:
    import import_lease
    import main

    _expired_attachment(pack_env, "expired-pack-fence")
    download_id = "expired-pack-fence" if matching else "unrelated-pack"
    queue_id = _terminal_queue(pack_env["db_path"], download_id)
    with main.get_db() as db:
        db.execute("UPDATE import_queue SET status='pending' WHERE id=?", (queue_id,))
        db.commit()
        claimed = import_lease.claim_import_queue_row(db, queue_id, "consumer")
        assert claimed is not matching


def test_image_generation_does_not_modify_or_attach_replaced_private_directory(
    pack_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_queue

    original_pack = import_queue._pack_image_dir_to_cbz
    replaced: list[Path] = []
    displaced = pack_env["tmp_path"] / "displaced-private"

    def race(source: str, target: str, checkpoint: Callable[[], None]) -> int | None:
        private = Path(os.path.realpath(os.path.dirname(target)))
        private.mkdir(parents=True, exist_ok=True)
        private.rename(displaced)
        private.mkdir(mode=0o700)
        (private / "unrelated.cbz").write_bytes(b"must not modify")
        replaced.append(private)
        return original_pack(source, target, checkpoint)

    monkeypatch.setattr(import_queue, "_pack_image_dir_to_cbz", race)
    queue_id = _queue_images(pack_env, "generator-path-racer")
    assert replaced[0].is_dir(), "unrelated replacement was moved or removed"
    assert {p.name: p.read_bytes() for p in replaced[0].iterdir()} == {
        "unrelated.cbz": b"must not modify"
    }
    assert queue_id is None
    with sqlite3.connect(pack_env["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM import_queue").fetchone() == (0,)


def test_native_generated_attachment_keeps_native_first_path(
    pack_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_pack_cleanup

    original_rename = import_pack_cleanup._rename_noreplace
    sources: list[Path] = []

    def record(source: str, destination: str) -> None:
        sources.append(Path(source))
        original_rename(source, destination)

    monkeypatch.setattr(import_pack_cleanup, "_rename_noreplace", record)
    queue_id = _queue_images(pack_env, "native-compatibility")
    assert queue_id is not None
    canonical, _ = _pack_paths("native-compatibility")
    assert len(sources) == 1
    assert not sources[0].exists()
    with zipfile.ZipFile(canonical / "Pack Series c001.cbz") as archive:
        assert archive.read("001.jpg") == b"page-one"


def test_non_generated_queue_keeps_existing_no_pack_reservation_behavior(
    pack_env: _PackEnv,
) -> None:
    import import_queue
    import main

    source = pack_env["tmp_path"] / "Pack Series c001.cbz"
    _write_cbz(source)
    with main.get_db() as db:
        queue_id, _ = import_queue._queue_import(
            db,
            1,
            "plain-cbz",
            "Pack Series c001",
            "magnet:plain-cbz",
            None,
            str(source),
        )
    assert queue_id is not None
    assert source.is_file()
    canonical, _ = _pack_paths("plain-cbz")
    assert not canonical.exists()
    with sqlite3.connect(pack_env["db_path"]) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM import_pack_cleanup_reservations"
        ).fetchone() == (0,)


def test_genuine_legacy_missing_tombstone_journal_remains_recoverable(
    pack_env: _PackEnv,
) -> None:
    import import_pack_cleanup

    queue_id = _terminal_queue(pack_env["db_path"], "legacy-missing")
    canonical, _ = _pack_paths("legacy-missing")
    canonical.parent.mkdir(parents=True)
    tombstone = canonical.with_name(canonical.name + ".cleanup-pre-upgrade-owner")
    # Insert only pre-upgrade fields: new live-flow writers cannot accidentally
    # supply ownership proof and turn this into a disguised versioned fixture.
    with sqlite3.connect(pack_env["db_path"]) as db:
        db.execute(
            "INSERT INTO import_pack_cleanup_tombstones("
            "tombstone_path, download_identity_key, normalized_download_id,"
            " download_id, queue_id, pack_path) VALUES(?,?,?,?,?,?)",
            (
                str(tombstone),
                '["legacy","unknown","legacy-missing"]',
                "legacy-missing",
                "legacy-missing",
                queue_id,
                str(canonical),
            ),
        )
    recovery = import_pack_cleanup.recover_pack_cleanup_state()
    assert recovery.tombstones_removed == 1
    assert recovery.tombstones_retained == 0
    with sqlite3.connect(pack_env["db_path"]) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM import_pack_cleanup_tombstones"
        ).fetchone() == (0,)
