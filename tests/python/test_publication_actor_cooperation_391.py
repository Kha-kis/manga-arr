"""Real manual and Suwayomi callers respect FILE owners and reservations."""

import asyncio
import json
import sqlite3

import pytest
from starlette.requests import Request

from test_publication_nfs_file_claims_391 import (
    _prepare_overwrite_publication,
    _seed_queue,
    _zip,
    journal_env,
)
from test_suwayomi_file_selection import cbz, env as suwayomi_env

__all__ = ["journal_env", "suwayomi_env"]


def _request(data):
    async def receive():
        return {"type": "http.request", "body": json.dumps(data).encode(), "more_body": False}
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []}, receive)


@pytest.mark.parametrize("automatic", [False, True], ids=["selected", "auto-match"])
@pytest.mark.parametrize("held", [False, True], ids=["durable-fence", "held-owner"])
def test_actual_manual_entrypoints_refuse_publication_owner(journal_env, monkeypatch, automatic, held):
    import main
    import shared
    from file_mutation_lock import file_mutation_guard
    from routers import import_ as routes

    pid, sid, _, final, _, _ = _prepare_overwrite_publication(journal_env, monkeypatch)
    original = final.read_bytes()
    with sqlite3.connect(journal_env["db_path"]) as db:
        title = db.execute("SELECT title FROM series WHERE id=?", (sid,)).fetchone()[0]
        before = db.execute("SELECT * FROM volumes WHERE series_id=?", (sid,)).fetchall()
        if held:
            # Only the owner guard, not the reservation query, protects this case.
            db.execute("UPDATE import_publications SET state='finalized' WHERE id=?", (pid,))
            db.execute("UPDATE import_queue SET status='pending',lease_owner=NULL,lease_expires_at=NULL")
    scan = journal_env["source_root"] / "manual"
    scan.mkdir()
    source = scan / f"{title} v01.cbz"
    _zip(source, b"manual incoming bytes")
    source_bytes = source.read_bytes()
    for config in (main.CONFIG, shared.CONFIG):
        config["import_mode"] = "copy"
    async def no_scan():
        pass
    monkeypatch.setattr(main, "trigger_komga_scan", no_scan)
    route = routes.manual_import_auto if automatic else routes.manual_import_process
    data = {"path": str(scan), "remove_source": True} if automatic else {"entries": [{"path": str(source), "series_id": sid, "volume_num": 1}]}
    if held:
        with file_mutation_guard(str(journal_env["db_path"])):
            response = asyncio.run(route(_request(data)))
    else:
        response = asyncio.run(route(_request(data)))
    body = json.loads(bytes(response.body))
    assert body["imported"] == 0
    assert final.read_bytes() == original
    assert source.read_bytes() == source_bytes
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute("SELECT * FROM volumes WHERE series_id=?", (sid,)).fetchall() == before
        assert db.execute("SELECT COUNT(*) FROM history").fetchone() == (0,)


@pytest.mark.parametrize("chapter", [False, True], ids=["volume", "chapter"])
@pytest.mark.parametrize("held", [False, True], ids=["durable-fence", "held-owner"])
def test_actual_suwayomi_file_callers_refuse_publication_owner(suwayomi_env, monkeypatch, chapter, held):
    import import_execute
    import shared
    from file_mutation_lock import file_mutation_guard
    from routers import suwayomi_ as routes

    env = suwayomi_env
    source = env.manga_dir / "Official_Chapter 1.cbz"
    cbz(source)
    with shared.get_db() as db:
        db.execute("INSERT OR REPLACE INTO root_folders(id,path) VALUES(1,?)", (str(env.library),))
        db.execute("UPDATE series SET root_folder_id=1 WHERE id=1")
        db.execute("INSERT INTO import_queue(series_id,torrent_name,src_dir,status,respect_grab_claims) VALUES(1,'391 reserved',?,'pending',0)", (str(env.manga_dir),))
        qid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("INSERT INTO import_queue_files(queue_id,filename,src_path,proposed_volume,file_type,proposed_import_kind,status) VALUES(?,'Selection v01.cbz',?,1,'volume','volume','pending')", (qid, str(source)))
    async def defer(*args, **kwargs):
        return False
    monkeypatch.setattr(import_execute, "complete_publication", defer)
    assert not asyncio.run(import_execute._execute_import(qid))
    with shared.get_db() as db:
        assert db.execute("SELECT state FROM import_publications").fetchone()[0] == "prepared"
        if held:
            db.execute("UPDATE import_publications SET state='finalized'")
            db.execute("UPDATE import_queue SET status='pending',lease_owner=NULL,lease_expires_at=NULL")
    before = sorted(str(path.relative_to(env.library)) for path in env.library.rglob("*"))
    async def call():
        if chapter:
            return await routes._import_suwayomi_chapter(env.client, 1, 1, swy_title="Selection")
        return await routes._import_suwayomi_volume(env.client, 1, 1, swy_title="Selection", chapter_nums=[1])
    if held:
        with file_mutation_guard(str(env.db_path)):
            result = asyncio.run(call())
    else:
        result = asyncio.run(call())
    assert result == (None, 0)
    assert sorted(str(path.relative_to(env.library)) for path in env.library.rglob("*")) == before


def test_importer_cannot_claim_queue_during_another_filesystem_owner(journal_env):
    import import_execute
    from file_mutation_lock import file_mutation_guard

    qid, _, _, _ = _seed_queue(journal_env, file_count=1)
    with sqlite3.connect(journal_env["db_path"]) as db:
        before = db.execute("SELECT * FROM import_queue WHERE id=?", (qid,)).fetchone()
    with file_mutation_guard(str(journal_env["db_path"])):
        assert not asyncio.run(import_execute._guarded_execute_import(qid))
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute("SELECT * FROM import_queue WHERE id=?", (qid,)).fetchone() == before
        assert db.execute("SELECT COUNT(*) FROM import_publications").fetchone() == (0,)


@pytest.mark.parametrize("chapter", [False, True], ids=["volume", "chapter"])
def test_suwayomi_job_audit_keeps_owner_and_quality_outside_writer(suwayomi_env, monkeypatch, chapter):
    import main
    from file_mutation_lock import FileMutationBusy, file_mutation_guard
    from test_suwayomi_file_selection import node

    env = suwayomi_env
    cbz(env.manga_dir / "Official_Chapter 17.cbz")
    env.queue([node(17, 17)], chapter=17 if chapter else None)
    observations = []
    quality = main.quality_from_filename
    history = main.add_history

    def assert_owner():
        with pytest.raises(FileMutationBusy):
            with file_mutation_guard(str(env.db_path)):
                pass
        observations.append("owner")

    def inspect_quality(path):
        assert_owner()
        with sqlite3.connect(env.db_path, timeout=0) as writer:
            writer.execute("BEGIN IMMEDIATE")
            writer.rollback()
        observations.append("writer-free")
        return quality(path)

    def inspect_history(*args, **kwargs):
        assert_owner()
        return history(*args, **kwargs)

    monkeypatch.setattr(main, "quality_from_filename", inspect_quality)
    monkeypatch.setattr(main, "add_history", inspect_history)
    env.process()
    assert env.row("suwayomi_downloads")["status"] == "completed"
    assert observations == ["owner", "writer-free", "owner"]
