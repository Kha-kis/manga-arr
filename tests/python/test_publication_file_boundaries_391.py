"""Physical ownership and decision boundaries for the #391 publication unit."""

import asyncio
import errno
import sqlite3
from pathlib import Path

import pytest

from test_publication_nfs_file_claims_391 import (
    _prepare_overwrite_publication,
    _seed_queue,
    _unsupported_renameat2,
    journal_env,
)

__all__ = ["journal_env"]


@pytest.mark.parametrize("unsupported", [False, True], ids=["native", "nfs"])
def test_original_and_stage_survive_until_batch_database_decision(
    journal_env, monkeypatch, unsupported
):
    import import_publication
    from private_file_claim import CarrierRecord

    publication_id, _, _, final, stage, flat_claim = _prepare_overwrite_publication(
        journal_env, monkeypatch
    )
    original = final.read_bytes()
    staged = stage.read_bytes()
    if unsupported:
        _unsupported_renameat2(monkeypatch, errno.EINVAL)

    assert import_publication.publish_publication(publication_id, "boundary-owner")
    assert stage.read_bytes() == staged
    assert final.read_bytes() == staged
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute(
            "SELECT state FROM import_publications WHERE id=?", (publication_id,)
        ).fetchone() == ("published",)
        proof = db.execute(
            "SELECT final_claim_carrier_json FROM import_publication_files"
            " WHERE publication_id=?",
            (publication_id,),
        ).fetchone()[0]
        assert db.execute("SELECT COUNT(*) FROM history").fetchone() == (0,)
    assert proof is not None
    carrier = CarrierRecord.from_json(proof)
    assert carrier.phase == "claimed"
    assert (Path(carrier.carrier_path) / "artifact").read_bytes() == original
    assert not flat_claim.exists()


@pytest.mark.parametrize("unsupported", [False, True], ids=["native", "nfs"])
def test_published_observation_change_compensates_without_success_receipts(
    journal_env, monkeypatch, unsupported
):
    import import_publication

    publication_id, series_id, source, final, _, _ = _prepare_overwrite_publication(
        journal_env, monkeypatch
    )
    original = final.read_bytes()
    source_bytes = source.read_bytes()
    if unsupported:
        _unsupported_renameat2(monkeypatch, errno.EINVAL)
    assert import_publication.publish_publication(publication_id, "boundary-owner")
    with sqlite3.connect(journal_env["db_path"]) as db:
        db.row_factory = sqlite3.Row
        db.execute(
            "UPDATE volumes SET quality='external ownership observation'"
            " WHERE series_id=?",
            (series_id,),
        )
        expected = dict(
            db.execute("SELECT * FROM volumes WHERE series_id=?", (series_id,))
            .fetchone()
        )

    assert not asyncio.run(
        import_publication.complete_publication(publication_id, "boundary-owner")
    )
    assert final.read_bytes() == original
    assert source.read_bytes() == source_bytes
    with sqlite3.connect(journal_env["db_path"]) as db:
        db.row_factory = sqlite3.Row
        assert dict(
            db.execute("SELECT * FROM volumes WHERE series_id=?", (series_id,))
            .fetchone()
        ) == expected
        assert db.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM import_publication_success_effects"
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM import_publication_notifications"
        ).fetchone()[0] == 0


def test_publication_mutation_runs_inside_nonexpiring_guard(journal_env, monkeypatch):
    import import_execute
    import import_publication
    from file_mutation_lock import FileMutationBusy, file_mutation_guard

    queue_id, _, _, _ = _seed_queue(journal_env, file_count=1)
    observed = []
    real_rename = import_publication._rename_noreplace

    def rename(source, destination):
        try:
            with file_mutation_guard(str(journal_env["db_path"])):
                observed.append(False)
        except FileMutationBusy:
            observed.append(True)
        return real_rename(source, destination)

    monkeypatch.setattr(import_publication, "_rename_noreplace", rename)
    assert asyncio.run(import_execute._execute_import(queue_id))
    assert observed and all(observed)


def test_quality_detection_does_not_run_under_sqlite_writer(journal_env, monkeypatch):
    import import_commit
    import import_execute

    queue_id, _, _, _ = _seed_queue(journal_env, file_count=1)
    observed = []
    real_quality = import_commit.quality_from_filename

    def quality(path):
        with sqlite3.connect(journal_env["db_path"], timeout=0) as writer:
            try:
                writer.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError:
                observed.append(False)
            else:
                observed.append(True)
                writer.rollback()
        return real_quality(path)

    monkeypatch.setattr(import_commit, "quality_from_filename", quality)
    assert asyncio.run(import_execute._execute_import(queue_id))
    assert observed and all(observed)


def test_planning_quality_detection_does_not_run_under_sqlite_writer(journal_env, monkeypatch):
    import import_execute
    import import_plan

    qid, _, _, _ = _seed_queue(journal_env, file_count=1)
    quality = import_plan.quality_from_filename
    observed = []

    def inspect(path):
        with sqlite3.connect(journal_env["db_path"], timeout=0) as writer:
            writer.execute("BEGIN IMMEDIATE")
            writer.rollback()
        observed.append(True)
        return quality(path)

    monkeypatch.setattr(import_plan, "quality_from_filename", inspect)
    assert asyncio.run(import_execute._execute_import(qid))
    assert observed


def test_monitor_and_provider_display_are_not_admission_revocation(
    journal_env, monkeypatch
):
    import import_publication

    publication_id, series_id, _, final, stage, _ = _prepare_overwrite_publication(
        journal_env, monkeypatch
    )
    staged = stage.read_bytes()
    assert import_publication.publish_publication(publication_id, "boundary-owner")
    with sqlite3.connect(journal_env["db_path"]) as db:
        db.execute("UPDATE volumes SET monitored=0 WHERE series_id=?", (series_id,))
        db.execute("UPDATE series SET title='New display title' WHERE id=?", (series_id,))
    assert asyncio.run(
        import_publication.complete_publication(publication_id, "boundary-owner")
    )
    assert final.read_bytes() == staged
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute(
            "SELECT status,monitored FROM volumes WHERE series_id=?", (series_id,)
        ).fetchone() == ("downloaded", 0)


def test_no_sqlite_writer_spans_physical_publish(journal_env, monkeypatch):
    import import_execute
    import import_publication

    queue_id, _, _, _ = _seed_queue(journal_env, file_count=1)
    observed = []
    real_rename = import_publication._rename_noreplace

    def rename(source, destination):
        with sqlite3.connect(journal_env["db_path"], timeout=0) as writer:
            writer.execute("BEGIN IMMEDIATE")
            observed.append(True)
            writer.rollback()
        return real_rename(source, destination)

    monkeypatch.setattr(import_publication, "_rename_noreplace", rename)
    assert asyncio.run(import_execute._execute_import(queue_id))
    assert observed


@pytest.mark.parametrize("column", ["proposed_volume_range_start", "proposed_volume_range_end"])
def test_mapping_range_changes_revoke_the_whole_published_batch(journal_env, monkeypatch, column):
    import import_publication as publication

    pid, _, _, final, _, _ = _prepare_overwrite_publication(journal_env, monkeypatch)
    original = final.read_bytes()
    assert publication.publish_publication(pid, "owner")
    with sqlite3.connect(journal_env["db_path"]) as db:
        db.execute(f"UPDATE import_queue_files SET {column}=25")
    assert not asyncio.run(publication.complete_publication(pid, "owner"))
    assert final.read_bytes() == original
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM history").fetchone() == (0,)


def test_another_series_new_path_claim_revokes_publication(journal_env, monkeypatch):
    import import_publication as publication

    pid, _, _, final, _, _ = _prepare_overwrite_publication(journal_env, monkeypatch)
    original = final.read_bytes()
    assert publication.publish_publication(pid, "owner")
    with sqlite3.connect(journal_env["db_path"]) as db:
        db.execute("INSERT INTO series(title,search_pattern) VALUES('Unrelated ownership','Unrelated ownership')")
        sid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("INSERT INTO volumes(series_id,volume_num,status,import_path) VALUES(?,1,'downloaded',?)", (sid, str(final)))
        unrelated = db.execute("SELECT * FROM volumes WHERE series_id=?", (sid,)).fetchone()
    assert not asyncio.run(publication.complete_publication(pid, "owner"))
    assert final.read_bytes() == original
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute("SELECT * FROM volumes WHERE series_id=?", (sid,)).fetchone() == unrelated
        assert db.execute("SELECT COUNT(*) FROM history").fetchone() == (0,)


def test_acquisition_metadata_change_cannot_supply_late_success_metadata(journal_env, monkeypatch):
    import import_publication as publication

    pid, sid, _, final, _, _ = _prepare_overwrite_publication(journal_env, monkeypatch)
    original = final.read_bytes()
    assert publication.publish_publication(pid, "owner")
    with sqlite3.connect(journal_env["db_path"]) as db:
        db.execute("INSERT INTO seen(series_id,torrent_url,download_id,torrent_name,indexer,respect_grab_claims) VALUES(?,'magnet:journal','journal-download','New acquisition','changed metadata',0)", (sid,))
    assert not asyncio.run(publication.complete_publication(pid, "owner"))
    assert final.read_bytes() == original
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM history").fetchone() == (0,)
