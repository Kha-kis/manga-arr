"""Reciprocal admission is checked by the actual short writer, not a pre-read."""

import sqlite3
import zipfile

import pytest

from test_rescan_recovery_protocol import _fixture
from test_rescan_transactions import rescan_env as rescan_env
from test_import_publication_journal import journal_env as journal_env, _seed_queue


ACTIVE = ["prepared", "published", "db_committed", "rollback"]


def _journal(db_path, series_id=7, state="prepared", source="/source"):
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO rescan_file_operations(operation_token,series_id,volume_id,"
            "source_path,destination_path,expected_volume_json,expected_context_json,"
            "fingerprints_json,carriers_json,state) VALUES('fence',?,99,?,?,'{}','{}','{}','{}',?)",
            (series_id, source, source, state),
        )


@pytest.mark.parametrize("state", ACTIVE)
def test_active_series_blocks_import_claim_on_separate_connection(rescan_env, state):
    import import_lease
    import shared

    with sqlite3.connect(rescan_env["db_path"]) as db:
        db.execute(
            "INSERT INTO import_queue(id,series_id,download_id,status) VALUES(1,7,'test','pending')"
        )
    _journal(rescan_env["db_path"], state=state)
    with shared.get_db() as db:
        assert not import_lease.claim_import_queue_row(db, 1, "worker")
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert db.execute(
            "SELECT status,lease_owner FROM import_queue WHERE id=1"
        ).fetchone() == ("pending", None)


@pytest.mark.parametrize("state", ACTIVE)
def test_active_series_blocks_delete_reservation_without_reset(rescan_env, state):
    import volume_file_deletion

    path, volume_id, _, _ = _fixture(rescan_env)
    original = path.read_bytes()
    _journal(rescan_env["db_path"], state=state)
    reservation = volume_file_deletion.reserve_volume_file_deletion(7, volume_id)
    assert reservation.status == "import_in_progress"
    assert path.read_bytes() == original
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert db.execute(
            "SELECT status,import_path FROM volumes WHERE id=?", (volume_id,)
        ).fetchone() == ("downloaded", str(path))
        assert db.execute("SELECT COUNT(*) FROM volume_file_deletions").fetchone() == (
            0,
        )


@pytest.mark.parametrize("state", ACTIVE)
def test_active_series_reconciliation_does_not_reset_missing_or_counts(
    rescan_env, state
):
    import rescan

    path, volume_id, _, _ = _fixture(rescan_env)
    path.unlink()
    _journal(rescan_env["db_path"], state=state, source=str(path))
    with sqlite3.connect(rescan_env["db_path"]) as db:
        before = db.execute("SELECT * FROM volumes WHERE id=?", (volume_id,)).fetchone()
        series_before = db.execute("SELECT * FROM series WHERE id=7").fetchone()
    result = rescan.rescan_series_folder(7)
    assert result["missing"] == 0
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert (
            db.execute("SELECT * FROM volumes WHERE id=?", (volume_id,)).fetchone()
            == before
        )
        assert db.execute("SELECT * FROM series WHERE id=7").fetchone() == series_before


@pytest.mark.parametrize("state", ACTIVE)
def test_soft_deleted_series_retains_journal_and_hard_purge_is_fenced(
    rescan_env, state
):
    import shared
    from routers import series_

    _journal(rescan_env["db_path"], state=state)
    with sqlite3.connect(rescan_env["db_path"]) as db:
        db.execute("UPDATE series SET deleted_at=CURRENT_TIMESTAMP WHERE id=7")
    with shared.get_db() as db:
        result = series_._prepare_hard_delete_series(db, 7)
    assert result["status"] == "import_in_progress"
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM series WHERE id=7").fetchone() == (1,)
        assert db.execute("SELECT state FROM rescan_file_operations").fetchone() == (
            state,
        )


def test_adoption_of_former_mapping_cannot_bypass_path_journal(rescan_env):
    import library_scan

    path, _, _, _ = _fixture(rescan_env)
    _journal(rescan_env["db_path"], source=str(path))
    with sqlite3.connect(rescan_env["db_path"]) as db:
        db.execute("DELETE FROM volumes WHERE series_id=7")
        db.execute("DELETE FROM series WHERE id=7")
    result = library_scan.adopt_unmapped_folder(1, str(path.parent))
    assert not result.ok
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM series").fetchone() == (0,)
    assert path.is_file()


@pytest.mark.parametrize("state", ACTIVE)
def test_publication_reservation_rechecks_rescan_on_writer(journal_env, state, monkeypatch):
    import import_publication
    import shared
    from import_lease import claim_import_queue_row, IMPORT_LEASE_SECONDS
    from import_plan import _plan_import

    queue_id, series_id, sources, _ = _seed_queue(journal_env, file_count=1)
    original = sources[0].read_bytes()
    with shared.get_db() as db:
        assert claim_import_queue_row(db, queue_id, "owner")
        plan = _plan_import(
            db,
            queue_id,
            "owner",
            {},
            {},
            set(),
            "copy",
            lease_seconds=IMPORT_LEASE_SECONDS,
        )
    assert plan is not None
    real_fingerprint = import_publication._regular_fingerprint
    hits = []

    def fingerprint(path, *, include_hash, heartbeat=None):
        result = real_fingerprint(path, include_hash=include_hash, heartbeat=heartbeat)
        if path == str(sources[0]) and include_hash is True:
            with sqlite3.connect(journal_env["db_path"]) as db:
                assert db.execute("SELECT COUNT(*) FROM import_publications").fetchone() == (0,)
            _journal(journal_env["db_path"], series_id=series_id, state=state)
            hits.append(state)
        return result

    monkeypatch.setattr(import_publication, "_regular_fingerprint", fingerprint)
    with pytest.raises(import_publication.PublicationOwnershipLost):
        import_publication.initialize_publication_filesystem(plan, "owner")
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM import_publications").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM file_claim_namespaces").fetchone() == (0,)
    assert hits == [state]
    assert sources[0].read_bytes() == original


def test_real_import_binding_fences_public_and_cached_rescan(rescan_env, monkeypatch):
    import rescan
    import rescan_file_recovery as recovery
    import shared
    from import_lease import claim_import_queue_row, IMPORT_LEASE_SECONDS
    from import_plan import _plan_import
    from import_publication import initialize_publication_filesystem

    path, _, target, context = _fixture(rescan_env)
    original = path.read_bytes()
    source = rescan_env["library_root"].parent / "incoming.cbz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("001.jpg", b"incoming volume two")
    with sqlite3.connect(rescan_env["db_path"]) as db:
        db.execute(
            "INSERT INTO volumes(series_id,volume_num,status,download_id)"
            " VALUES(7,2,'grabbed','incoming')"
        )
        # Explicit local manual import; no synthesized rescan operation or receipt.
        cursor = db.execute(
            "INSERT INTO import_queue(series_id,download_id,torrent_name,torrent_url,"
            "src_dir,status,respect_grab_claims)"
            " VALUES(7,'incoming','incoming v02','local:manual',?,'pending',0)",
            (str(source.parent),),
        )
        queue_id = cursor.lastrowid
        db.execute(
            "INSERT INTO import_queue_files(queue_id,filename,src_path,proposed_volume,"
            "file_type,proposed_import_kind,status)"
            " VALUES(?,?,?,2,'volume','volume','pending')",
            (queue_id, "Race Manga v02.cbz", str(source)),
        )
    assert queue_id is not None
    owner = "opposite-order-owner"
    with shared.get_db() as db:
        assert claim_import_queue_row(db, queue_id, owner)
        plan = _plan_import(
            db, queue_id, owner, {}, {}, set(), "copy",
            lease_seconds=IMPORT_LEASE_SECONDS,
        )
    assert plan is not None
    initialize_publication_filesystem(plan, owner)
    assert rescan._current_enrichment_context(target) == context
    with sqlite3.connect(rescan_env["db_path"]) as db:
        before = db.execute("SELECT * FROM volumes WHERE series_id=7 ORDER BY id").fetchall()
        publication_before = db.execute("SELECT * FROM import_publications").fetchall()

    real_competing = recovery.competing_for_series
    real_reserve = recovery._reserve
    hits = []
    reservations = []

    def competing(db, series_id):
        result = real_competing(db, series_id)
        hits.append((series_id, db.in_transaction, result))
        return result

    def reserve(*args):
        result = real_reserve(*args)
        reservations.append(result)
        return result

    monkeypatch.setattr(recovery, "competing_for_series", competing)
    monkeypatch.setattr(recovery, "_reserve", reserve)
    result = rescan.rescan_series_folder(7)
    assert hits == [(7, True, True)]
    assert result["created"] == result["recovered"] == result["missing"] == 0
    hits.clear()
    recovery.enrich_target(target, context)
    assert hits == [(7, True, True)]
    assert reservations == [None]
    assert path.read_bytes() == original
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM rescan_file_operations").fetchone() == (0,)
        assert db.execute("SELECT * FROM volumes WHERE series_id=7 ORDER BY id").fetchall() == before
        assert db.execute("SELECT * FROM import_publications").fetchall() == publication_before


@pytest.mark.parametrize(
    "status,owner",
    [("importing", None), ("importing", "expired"), ("pending", "expired")],
)
def test_rescan_rejects_importing_or_unrecovered_prejournal_owner(
    rescan_env, status, owner
):
    import rescan_file_recovery as recovery

    path, _, target, context = _fixture(rescan_env)
    original = path.read_bytes()
    with sqlite3.connect(rescan_env["db_path"]) as db:
        db.execute(
            "INSERT INTO import_queue(series_id,download_id,status,lease_owner,lease_expires_at) VALUES(7,'test',?,?, '2000-01-01')",
            (status, owner),
        )
    recovery.enrich_target(target, context)
    assert path.read_bytes() == original
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM rescan_file_operations").fetchone() == (
            0,
        )


def test_reconciliation_cannot_recover_file_reserved_for_deletion(rescan_env):
    import rescan
    import volume_file_deletion

    path, volume_id, _, _ = _fixture(rescan_env)
    reservation = volume_file_deletion.reserve_volume_file_deletion(7, volume_id)
    assert reservation.status == "reserved"
    assert path.is_file()
    result = rescan.rescan_series_folder(7)
    assert result["recovered"] == 0
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert db.execute(
            "SELECT status,import_path FROM volumes WHERE id=?", (volume_id,)
        ).fetchone() == ("wanted", None)


def test_import_claim_writer_rechecks_journal_created_after_its_identity_read(
    rescan_env, monkeypatch
):
    import import_lease
    import shared

    with sqlite3.connect(rescan_env["db_path"]) as db:
        db.execute(
            "INSERT INTO import_queue(id,series_id,download_id,status) VALUES(1,7,'test','pending')"
        )
    real_modifier = import_lease._lease_modifier

    def competing_writer(seconds):
        _journal(rescan_env["db_path"])
        return real_modifier(seconds)

    monkeypatch.setattr(import_lease, "_lease_modifier", competing_writer)
    with shared.get_db() as db:
        assert not import_lease.claim_import_queue_row(db, 1, "worker")
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert db.execute(
            "SELECT status,lease_owner FROM import_queue WHERE id=1"
        ).fetchone() == ("pending", None)
