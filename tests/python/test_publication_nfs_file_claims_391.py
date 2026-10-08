"""Deferred #378 publication acceptance; restore after clean #387 merge.

Six parametrized nodes, with original fixture/helper bodies and assertions.
Run from the publication worktree with PYTHONPATH=app:tests/python.
"""


from __future__ import annotations


import asyncio
import ctypes
import errno
import os
import shutil
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import conftest  # noqa: F401


UNSUPPORTED = [errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP]



def _unsupported_renameat2(
    monkeypatch: pytest.MonkeyPatch,
    error: int,
    *,
    source_contains: str | None = None,
) -> None:
    """Inject a real syscall errno, optionally only at a restoration boundary."""
    real_libc = ctypes.CDLL(None, use_errno=True)
    real_rename = real_libc.renameat2
    real_rename.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    real_rename.restype = ctypes.c_int

    class Rename:
        def __call__(
            self,
            source_fd: int,
            source: bytes,
            destination_fd: int,
            destination: bytes,
            flags: int,
        ) -> int:
            if source_contains is None or source_contains in os.fsdecode(source):
                ctypes.set_errno(error)
                return -1
            return real_rename(source_fd, source, destination_fd, destination, flags)

    libc = SimpleNamespace(renameat2=Rename())
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: libc)



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
    """Explicit internal local/manual fixture; no downloader acquisition occurred."""
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
            " torrent_url,volume_num,src_dir,status,respect_grab_claims)"
            " VALUES(?,'journal-download','Journal batch','magnet:journal',"
            " NULL,?,'pending',0)",
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



publication_tests = SimpleNamespace(
    _prepare_overwrite_publication=_prepare_overwrite_publication,
    _seed_queue=_seed_queue,
)


@pytest.mark.parametrize("error", UNSUPPORTED)
def test_overwrite_workflow_completes_with_unsupported_renameat2(
    journal_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, error: int
) -> None:
    import import_publication

    publication_id, series_id, source, final, stage, _ = (
        publication_tests._prepare_overwrite_publication(journal_env, monkeypatch)
    )
    source_bytes = source.read_bytes()
    staged_bytes = stage.read_bytes()
    _unsupported_renameat2(monkeypatch, error)

    published = import_publication.publish_publication(publication_id, "nfs-owner")

    assert source.read_bytes() == source_bytes
    if not published:
        assert stage.read_bytes() == staged_bytes
        with sqlite3.connect(journal_env["db_path"]) as db:
            assert db.execute(
                "SELECT COUNT(*) FROM history WHERE series_id=?"
                " AND event_type='imported'",
                (series_id,),
            ).fetchone() == (0,)
    assert published, "prepared overwrite is still blocked by unsupported claims"
    assert final.read_bytes() == staged_bytes



@pytest.mark.parametrize("error", UNSUPPORTED)
def test_move_workflow_removes_only_recorded_source_after_commit(
    journal_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, error: int
) -> None:
    import import_execute

    queue_id, series_id, sources, finals = publication_tests._seed_queue(
        journal_env, mode="move", file_count=1
    )
    source_bytes = sources[0].read_bytes()
    _unsupported_renameat2(monkeypatch, error)

    asyncio.run(import_execute._execute_import(queue_id))

    assert finals[0].is_file()
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM history WHERE series_id=? AND event_type='imported'",
            (series_id,),
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT status FROM volumes WHERE series_id=?", (series_id,)
        ).fetchone() == ("downloaded",)
        row = db.execute(
            "SELECT p.state,f.cleanup_state FROM import_publications p"
            " JOIN import_publication_files f ON f.publication_id=p.id"
        ).fetchone()
    assert row is not None
    if sources[0].exists():
        assert sources[0].read_bytes() == source_bytes
        assert row == ("cleaning", "blocked")
    assert not sources[0].exists(), "committed move still retains unsupported source"
    assert row == ("deleted", "deleted")
