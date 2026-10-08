"""Private staging birth, partial writes, and cleanup authority."""

import asyncio
import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import zipfile

import pytest

from test_import_publication_journal import (
    _prepare_overwrite_publication,
    _seed_queue,
    journal_env,  # noqa: F401
)


def _snapshot(env):
    with sqlite3.connect(env["db_path"]) as db:
        row = db.execute("SELECT id,state,queue_snapshot_json,staging_dir FROM import_publications").fetchone()
    assert row is not None
    return row, json.loads(row[2])


def _load(pid):
    from import_publication import load_publication
    from shared import get_db
    with get_db() as db:
        record = load_publication(db, publication_id=pid)
    assert record is not None
    return record


def test_stage_binding_is_durable_before_first_archive_write(journal_env, monkeypatch):
    import import_execute
    import import_staging

    qid, _, _, _ = _seed_queue(journal_env, file_count=1)
    real_stage = import_staging._ImportStaging.stage
    observed = []

    def stage(self, source, final):
        row, snapshot = _snapshot(journal_env)
        proof = snapshot["_publication_staging"]
        assert row[1] == "staging"
        assert proof["phase"] == "ready"
        assert proof["carrier"]["binding"]["operation_key"] == str(row[0])
        assert proof["carrier"]["binding"]["purpose"] == "staging"
        assert proof["carrier"]["carrier_path"] == row[3]
        observed.append(True)
        return real_stage(self, source, final)

    monkeypatch.setattr(import_staging._ImportStaging, "stage", stage)
    assert asyncio.run(import_execute._execute_import(qid))
    assert observed
    roots = list(journal_env["library_root"].rglob(".mangarr-claims"))
    assert roots and all({p.name for p in root.iterdir()} == {"owner.json"} for root in roots)


def test_stage_allocation_has_durable_batch_intent(journal_env, monkeypatch):
    import import_execute
    import private_file_claim as claims

    qid, _, _, _ = _seed_queue(journal_env, file_count=1)
    real_allocate = claims.allocate_carrier
    observed = []

    @contextmanager
    def allocate(namespace, binding, origin, fingerprint=None):
        if binding.purpose == "staging":
            row, snapshot = _snapshot(journal_env)
            assert row[1] == "staging"
            assert snapshot["_publication_staging"]["phase"] == "allocating"
            assert snapshot["_publication_staging"]["carrier"] is None
            observed.append(True)
        with real_allocate(namespace, binding, origin, fingerprint) as carrier:
            yield carrier

    monkeypatch.setattr(claims, "allocate_carrier", allocate)
    assert asyncio.run(import_execute._execute_import(qid))
    assert observed


@pytest.mark.parametrize("scratch", ["partial.cbr", ".mangarr-cow-interrupted"])
def test_partial_stage_children_abort_through_private_carrier(journal_env, monkeypatch, scratch):
    import import_publication as publication

    pid, _, source, _, stage, _ = _prepare_overwrite_publication(journal_env, monkeypatch)
    row, snapshot = _snapshot(journal_env)
    assert snapshot["_publication_staging"]["carrier"]["carrier_path"] == str(stage.parent)
    original = source.read_bytes()
    (stage.parent / scratch).write_bytes(b"partial transform")
    with sqlite3.connect(journal_env["db_path"]) as db:
        db.execute("UPDATE import_publications SET state='staging' WHERE id=?", (pid,))
    assert publication.remove_staging_directory(_load(pid))
    assert publication.abort_staging_publication(pid, release_queue=False)
    assert source.read_bytes() == original
    assert not stage.parent.exists()
    assert {p.name for p in stage.parent.parent.iterdir()} == {"owner.json"}


def test_v0_stage_current_stats_do_not_authorize_public_root_cleanup(journal_env, monkeypatch):
    import import_publication as publication

    pid, _, _, _, stage, _ = _prepare_overwrite_publication(journal_env, monkeypatch)
    row, snapshot = _snapshot(journal_env)
    snapshot.pop("_publication_staging", None)
    with sqlite3.connect(journal_env["db_path"]) as db:
        db.execute("UPDATE import_publications SET state='staging',queue_snapshot_json=? WHERE id=?", (json.dumps(snapshot), pid))
    assert not publication.remove_staging_directory(_load(pid))
    assert stage.is_file()
    assert _snapshot(journal_env)[0][1] == "staging"


def test_real_compressed_rar_transforms_through_inherited_carrier_fd(journal_env):
    import import_execute

    qid, _, sources, _ = _seed_queue(journal_env, file_count=1)
    compressed = base64.b64decode(
        "UmFyIRoHAQAzkrXlCgEFBgAFAQGAgAD9y50EJQIDC4UBBJwBtIMCM4Xr04AFAQcwMDEucG5nCgMTgfnGamccjgDFHYI2ZmQj+CP+0TYWcitqqm4BZKP4JxA5JCASEEkJDAhYkSIJOwcQCBpCQwEWLAhgQoWJFGB8YKYd9PvfWe5P5gXmRGZYWEj7RAAEVcjR2Pn+O5z986mOoSkTcjn2xTFd8C4Y0JrJtzaRPZt5YSY8W1IPpUdLSX/uzvqnj+eQKqwStSCUHXdWUQMFBAA="
    )
    source = sources[0].with_suffix(".cbr")
    sources[0].rename(source)
    source.write_bytes(compressed)
    with sqlite3.connect(journal_env["db_path"]) as db:
        db.execute("UPDATE import_queue_files SET src_path=?,filename=? WHERE queue_id=?",
                   (str(source), source.name, qid))
    assert asyncio.run(import_execute._execute_import(qid))
    with sqlite3.connect(journal_env["db_path"]) as db:
        final = Path(db.execute("SELECT final_path FROM import_publication_files").fetchone()[0])
        assert db.execute("SELECT state FROM import_publications").fetchone() == ("deleted",)
        assert db.execute("SELECT COUNT(*) FROM history WHERE event_type='imported'").fetchone() == (1,)
    assert source.read_bytes() == compressed
    assert final.suffix == ".cbz"
    with zipfile.ZipFile(final) as archive:
        assert archive.testzip() is None
        assert archive.read("001.png").startswith(b"\x89PNG")
        assert "ComicInfo.xml" in archive.namelist()
    roots = list(journal_env["library_root"].rglob(".mangarr-claims"))
    assert roots and all({p.name for p in root.iterdir()} == {"owner.json"} for root in roots)


def test_failed_partial_cow_aborts_without_touching_source(journal_env, monkeypatch):
    import import_execute
    import import_staging
    qid, _, sources, _ = _seed_queue(journal_env, file_count=1, mode="hardlink")
    original = sources[0].read_bytes()
    real_write = os.write
    injected = []

    def write(fd, data):
        if ".mangarr-cow-" in os.readlink(f"/proc/self/fd/{fd}") and not injected:
            real_write(fd, data[:3])
            injected.append(True)
            raise OSError("injected after partial COW write")
        return real_write(fd, data)

    monkeypatch.setattr(import_staging.os, "write", write)
    assert not asyncio.run(import_execute._execute_import(qid))
    assert injected
    assert sources[0].read_bytes() == original
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM import_publications").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM history WHERE event_type='imported'").fetchone() == (0,)
    roots = list(journal_env["library_root"].rglob(".mangarr-claims"))
    assert roots and all({p.name for p in root.iterdir()} == {"owner.json"} for root in roots)


def test_failed_transform_process_discards_partial_private_output(journal_env, monkeypatch):
    import import_execute
    import import_staging

    qid, _, sources, _ = _seed_queue(journal_env, file_count=1)
    original = sources[0].read_bytes()
    # Honest child-process fault injection after a real private output write;
    # no filesystem/converter syscall replacement in the application process.
    prelude = """
import json, os, sys
p = json.loads(sys.stdin.read())
fd = os.open('partial.cbz', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=p['fd'])
os.write(fd, b'PK incomplete archive')
os.fsync(fd)
os.close(fd)
os._exit(42)
"""
    monkeypatch.setattr(import_staging, "_PINNED_TRANSFORM_WORKER", prelude)
    assert not asyncio.run(import_execute._execute_import(qid))
    assert sources[0].read_bytes() == original
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM import_publications").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM history WHERE event_type='imported'").fetchone() == (0,)
    roots = list(journal_env["library_root"].rglob(".mangarr-claims"))
    assert roots and all({p.name for p in root.iterdir()} == {"owner.json"} for root in roots)
