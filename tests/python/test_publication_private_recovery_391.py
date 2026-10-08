"""Replacement, collision and replay evidence for private FILE publications."""

import asyncio
import errno
import json
import os
import sqlite3
from pathlib import Path

import pytest

from test_publication_nfs_file_claims_391 import (
    _prepare_overwrite_publication,
    _seed_queue,
    _unsupported_renameat2,
    _zip,
    journal_env,
)

__all__ = ["journal_env"]


def _changed(env, series_id):
    with sqlite3.connect(env["db_path"]) as db:
        db.execute("UPDATE volumes SET quality='reassigned' WHERE series_id=?", (series_id,))


def _state(env):
    with sqlite3.connect(env["db_path"]) as db:
        return db.execute("SELECT state,queue_snapshot_json FROM import_publications").fetchone()


@pytest.mark.parametrize("unsupported", [False, True], ids=["native", "nfs"])
def test_rollback_capture_race_restores_replacement_and_retains_original(
    journal_env, monkeypatch, unsupported
):
    import import_publication as publication
    import private_file_claim as claims

    pid, sid, _, final, stage, _ = _prepare_overwrite_publication(journal_env, monkeypatch)
    original = final.read_bytes()
    if unsupported:
        _unsupported_renameat2(monkeypatch, errno.EINVAL)
    assert publication.publish_publication(pid, "owner")
    _changed(journal_env, sid)
    replacement = final.parent / "external.cbz"
    _zip(replacement, b"unrelated replacement")
    winner = replacement.read_bytes()
    real_capture = claims.claim_into_empty
    replaced = []

    def capture(guard, carrier, parent_fd, name):
        if carrier.binding.purpose == "rollback" and not replaced:
            os.replace(replacement, final)
            replaced.append(True)
        return real_capture(guard, carrier, parent_fd, name)

    monkeypatch.setattr(claims, "claim_into_empty", capture)
    assert not asyncio.run(publication.complete_publication(pid, "owner"))
    assert replaced
    assert final.read_bytes() == winner
    assert stage.is_file()
    state, snapshot = _state(journal_env)
    assert state == "published"
    assert json.loads(snapshot)["_file_publication"]["decision"] == "compensate"
    with sqlite3.connect(journal_env["db_path"]) as db:
        proof = db.execute("SELECT final_claim_carrier_json FROM import_publication_files").fetchone()[0]
        assert db.execute("SELECT COUNT(*) FROM history").fetchone() == (0,)
    record = claims.CarrierRecord.from_json(proof)
    assert (Path(record.carrier_path) / "artifact").read_bytes() == original


@pytest.mark.parametrize("unsupported", [False, True], ids=["native", "nfs"])
def test_same_inode_unacknowledged_restore_refuses_and_retains(journal_env, monkeypatch, unsupported):
    import import_publication as publication
    import private_file_claim as claims

    pid, sid, _, final, stage, _ = _prepare_overwrite_publication(journal_env, monkeypatch)
    original = final.read_bytes()
    if unsupported:
        _unsupported_renameat2(monkeypatch, errno.EINVAL)
    assert publication.publish_publication(pid, "owner")
    _changed(journal_env, sid)
    real_link = claims.link_private_regular
    attempted = []

    def link(guard, carrier, parent_fd, name, expected):
        if carrier.binding.purpose == "original" and not attempted:
            os.link("artifact", name, src_dir_fd=carrier.fd, dst_dir_fd=parent_fd)
            attempted.append(True)
        return real_link(guard, carrier, parent_fd, name, expected)

    monkeypatch.setattr(claims, "link_private_regular", link)
    assert not asyncio.run(publication.complete_publication(pid, "owner"))
    assert attempted
    assert final.read_bytes() == original
    assert stage.is_file()
    state, snapshot = _state(journal_env)
    assert state == "published"
    assert json.loads(snapshot)["_file_publication"]["decision"] == "compensate"
    assert not asyncio.run(publication.complete_publication(pid, "owner"))
    assert final.read_bytes() == original
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM history").fetchone() == (0,)


@pytest.mark.parametrize("unsupported", [False, True], ids=["native", "nfs"])
def test_source_capture_race_preserves_changed_shared_source(journal_env, monkeypatch, unsupported):
    import import_execute
    import private_file_claim as claims

    qid, _, sources, finals = _seed_queue(journal_env, file_count=1, mode="move")
    replacement = sources[0].parent / "external.cbz"
    _zip(replacement, b"new unrelated source")
    winner = replacement.read_bytes()
    if unsupported:
        _unsupported_renameat2(monkeypatch, errno.EINVAL)
    real_capture = claims.claim_into_empty
    replaced = []

    def capture(guard, carrier, parent_fd, name):
        if carrier.binding.purpose == "source" and not replaced:
            os.replace(replacement, carrier.record.origin_path)
            replaced.append(True)
        return real_capture(guard, carrier, parent_fd, name)

    monkeypatch.setattr(claims, "claim_into_empty", capture)
    assert asyncio.run(import_execute._execute_import(qid))
    assert replaced
    assert sources[0].read_bytes() == winner
    assert finals[0].is_file()
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute("SELECT state FROM import_publications").fetchone() == ("deleted",)
        assert db.execute("SELECT cleanup_state FROM import_publication_files").fetchone() == ("replaced",)
    roots = list(journal_env["library_root"].rglob(".mangarr-claims")) + list(journal_env["source_root"].rglob(".mangarr-claims"))
    assert roots and all({p.name for p in root.iterdir()} == {"owner.json"} for root in roots)


@pytest.mark.parametrize("purpose", ["publication", "original"])
def test_changed_private_artifact_is_retained_not_reproved(journal_env, monkeypatch, purpose):
    import import_publication as publication
    import private_file_claim as claims

    pid, _, _, _, stage, _ = _prepare_overwrite_publication(journal_env, monkeypatch)
    _unsupported_renameat2(monkeypatch, errno.EINVAL)
    assert publication.publish_publication(pid, "owner")
    _, snapshot = _state(journal_env)
    entry = next(iter(json.loads(snapshot)["_file_publication"]["artifacts"].values()))
    record = claims.CarrierRecord.from_json(entry[purpose])
    artifact = Path(record.carrier_path) / "artifact"
    artifact.write_bytes(b"changed private artifact")
    assert not asyncio.run(publication.complete_publication(pid, "owner"))
    assert artifact.read_bytes() == b"changed private artifact"
    assert stage.is_file()
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM history").fetchone() == (0,)
        assert db.execute("SELECT status FROM volumes").fetchone() == ("grabbed",)


@pytest.mark.parametrize("unsupported", [False, True], ids=["native", "nfs"])
@pytest.mark.parametrize("decided", [False, True], ids=["pending", "compensation-decided"])
def test_compensation_replays_durable_original_intent_before_capture(journal_env, monkeypatch, unsupported, decided):
    import import_publication as publication
    import private_file_claim as claims

    pid, sid, _, final, stage, _ = _prepare_overwrite_publication(journal_env, monkeypatch)
    original = final.read_bytes()
    if unsupported:
        _unsupported_renameat2(monkeypatch, errno.EINVAL)
    real_capture = claims.claim_into_empty
    interrupted = []

    def capture(guard, carrier, parent_fd, name):
        if carrier.binding.purpose == "original" and not interrupted:
            interrupted.append(True)
            raise OSError(errno.EIO, "injected before original capture")
        return real_capture(guard, carrier, parent_fd, name)

    monkeypatch.setattr(claims, "claim_into_empty", capture)
    assert not publication.publish_publication(pid, "owner")
    assert interrupted and stage.is_file()
    _changed(journal_env, sid)
    if decided:
        with sqlite3.connect(journal_env["db_path"]) as db:
            snapshot = json.loads(db.execute("SELECT queue_snapshot_json FROM import_publications").fetchone()[0])
            snapshot["_file_publication"]["decision"] = "compensate"
            db.execute("UPDATE import_publications SET queue_snapshot_json=?", (json.dumps(snapshot),))
    assert not asyncio.run(publication.complete_publication(pid, "owner"))
    assert final.read_bytes() == original
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute("SELECT state,result_ok FROM import_publications").fetchone() == ("finalized", 0)
        assert db.execute("SELECT COUNT(*) FROM history").fetchone() == (0,)
    roots = list(journal_env["library_root"].rglob(".mangarr-claims"))
    assert roots and all({p.name for p in root.iterdir()} == {"owner.json"} for root in roots)


def test_unowned_private_publish_entry_cannot_use_the_legacy_delete_path(journal_env, monkeypatch):
    import import_publication as publication
    from shared import get_db

    pid, _, _, final, stage, _ = _prepare_overwrite_publication(journal_env, monkeypatch)
    original = final.read_bytes()
    staged = stage.read_bytes()
    with get_db() as db:
        record = publication.load_publication(db, publication_id=pid)
    assert record is not None
    with pytest.raises(publication.PublicationBlocked):
        publication._publish_prepared_file(record, record.files[0], "unowned")
    assert final.read_bytes() == original
    assert stage.read_bytes() == staged


def test_legacy_source_cleanup_entry_cannot_delete_before_domain_decision(journal_env, monkeypatch):
    import import_execute
    import import_publication as publication
    from shared import get_db

    qid, _, sources, _ = _seed_queue(journal_env, file_count=1, mode="move")
    async def defer(*_args, **_kwargs):
        return False
    monkeypatch.setattr(import_execute, "complete_publication", defer)
    assert not asyncio.run(import_execute._execute_import(qid))
    with sqlite3.connect(journal_env["db_path"]) as db:
        pid = db.execute("SELECT id FROM import_publications").fetchone()[0]
    source = sources[0]
    original = source.read_bytes()
    with get_db() as db:
        record = publication.load_publication(db, publication_id=pid)
    assert record is not None
    outcome = publication._cleanup_move_source(pid, record.files[0], "unowned")
    assert outcome.state == "blocked"
    assert source.read_bytes() == original


@pytest.mark.parametrize("unsupported", [False, True], ids=["native-consumed", "nfs-unacknowledged-link"])
def test_compensation_after_unacknowledged_publish_uses_only_positive_native_proof(journal_env, monkeypatch, unsupported):
    import import_publication as publication
    import private_file_claim as claims

    pid, sid, _, final, stage, _ = _prepare_overwrite_publication(journal_env, monkeypatch)
    original = final.read_bytes()
    incoming = stage.read_bytes()
    interrupted = []
    if unsupported:
        _unsupported_renameat2(monkeypatch, errno.EINVAL)
        real_link = claims.link_private_regular

        def link(guard, carrier, parent_fd, name, expected):
            receipt = real_link(guard, carrier, parent_fd, name, expected)
            if carrier.binding.purpose == "publication" and not interrupted:
                interrupted.append(True)
                raise OSError(errno.EIO, "injected before publication receipt")
            return receipt

        monkeypatch.setattr(claims, "link_private_regular", link)
    else:
        real_fsync = publication._fsync_renamed_directories

        def fsync(source, destination):
            real_fsync(source, destination)
            if os.path.basename(source) == "artifact" and destination == str(final) and not interrupted:
                interrupted.append(True)
                raise OSError(errno.EIO, "injected before native publication receipt")

        monkeypatch.setattr(publication, "_fsync_renamed_directories", fsync)
    assert not publication.publish_publication(pid, "owner")
    assert interrupted and final.read_bytes() == incoming
    _changed(journal_env, sid)
    with sqlite3.connect(journal_env["db_path"]) as db:
        snapshot = json.loads(db.execute("SELECT queue_snapshot_json FROM import_publications").fetchone()[0])
        snapshot["_file_publication"]["decision"] = "compensate"
        db.execute("UPDATE import_publications SET queue_snapshot_json=?", (json.dumps(snapshot),))
    assert not asyncio.run(publication.complete_publication(pid, "owner"))
    assert final.read_bytes() == (incoming if unsupported else original)
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute("SELECT state FROM import_publications").fetchone() == ("publishing" if unsupported else "finalized",)
        assert db.execute("SELECT COUNT(*) FROM history").fetchone() == (0,)
    if unsupported:
        assert stage.is_file()
        assert not asyncio.run(publication.complete_publication(pid, "owner"))


def test_staging_public_name_replacement_is_not_deleted_by_postcommit_gc(journal_env, monkeypatch):
    import import_publication as publication

    pid, _, _, _, stage, _ = _prepare_overwrite_publication(journal_env, monkeypatch)
    assert publication.publish_publication(pid, "owner")
    # An ordinary media-group writer can replace a public directory entry
    # without entering or modifying the original 0700 staging directory.
    namespace = stage.parent.parent
    os.chmod(namespace.parent, 0o770)
    real_verify = publication._verify_stage_directory
    moved = namespace.with_name(namespace.name + "-external-move") / stage.parent.name
    replaced = []

    def verify(record):
        fd = real_verify(record)
        with sqlite3.connect(journal_env["db_path"]) as db:
            state = db.execute("SELECT state FROM import_publications").fetchone()[0]
        if state == "cleaning" and not replaced:
            os.rename(namespace, moved.parent)
            namespace.mkdir(mode=0o700)
            stage.parent.mkdir(mode=0o700)
            (stage.parent / stage.name).write_bytes(b"unrelated public-name replacement")
            replaced.append(True)
        return fd

    monkeypatch.setattr(publication, "_verify_stage_directory", verify)
    result = asyncio.run(publication.complete_publication(pid, "owner"))
    assert replaced
    assert stage.is_file(), "postcommit GC deleted the unrelated public-name replacement"
    assert stage.read_bytes() == b"unrelated public-name replacement"
    assert (moved / stage.name).is_file()
    assert not result
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute("SELECT state FROM import_publications").fetchone() == ("cleaning",)


def test_initial_staging_collision_is_never_removed_or_adopted(journal_env):
    import import_publication as publication
    from import_lease import claim_import_queue_row
    from import_plan import _plan_import
    from shared import get_db

    qid, _, _, _ = _seed_queue(journal_env, file_count=1)
    owner = "staging-collision-owner"
    with get_db() as db:
        assert claim_import_queue_row(db, qid, owner, lease_seconds=120)
        plan = _plan_import(db, qid, owner, {}, {}, set(), "copy", lease_seconds=120)
    assert plan is not None
    root = Path(plan.dst_dir)
    root.mkdir(parents=True)
    collision = Path(publication.deterministic_staging_dir(str(root), qid, owner))
    collision.mkdir(mode=0o700)
    occupant = collision / "unrelated.cbz"
    occupant.write_bytes(b"unrelated staging-name occupant")
    with pytest.raises(publication.PublicationBlocked):
        publication.initialize_publication_filesystem(plan, owner)
    assert occupant.read_bytes() == b"unrelated staging-name occupant"
