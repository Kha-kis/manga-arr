"""Owned rescan protocol controls with actual SQLite and filesystem actions."""

from pathlib import Path
import sqlite3
import os
import zipfile

import pytest

from test_rescan_transactions import _insert_volume, rescan_env as rescan_env


def _fixture(env):
    import rescan

    path = Path(env["series_dir"]) / "Race Manga v01.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.jpg", b"original")
    volume_id = _insert_volume(env["db_path"], 1, "wanted")
    with rescan.get_db() as db:
        snapshot = rescan.snapshot_series_rescan(db, 7)
    assert snapshot is not None
    inventory = rescan.build_filesystem_inventory(snapshot)
    with rescan.get_db() as db:
        reconciliation = rescan.reconcile_series_inventory(db, snapshot, inventory)
    target = reconciliation.enrichment_targets[0]
    context = rescan._current_enrichment_context(target)
    assert context is not None
    return path, volume_id, target, context


def _rows(env):
    with sqlite3.connect(env["db_path"]) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM rescan_file_operations")]


def test_success_keeps_original_and_stage_until_full_decision(rescan_env, monkeypatch):
    import rescan_file_recovery as recovery

    path, _, target, context = _fixture(rescan_env)
    original = path.read_bytes()
    real_commit = recovery._commit
    seen = []

    def commit(operation, guard):
        source = recovery._record(operation, "source")
        stage = recovery._record(operation, "stage")
        assert source is not None and stage is not None
        assert (Path(source.carrier_path) / "artifact").read_bytes() == original
        assert (Path(stage.carrier_path) / "artifact").is_file()
        assert _rows(rescan_env)[0]["state"] == "published"
        seen.append(True)
        return real_commit(operation, guard)

    monkeypatch.setattr(recovery, "_commit", commit)
    recovery.enrich_target(target, context)
    assert seen == [True]
    assert _rows(rescan_env)[0]["state"] == "completed"
    assert [p.name for p in (path.parent / ".mangarr-claims").iterdir()] == [
        "owner.json"
    ]
    with zipfile.ZipFile(path) as archive:
        assert archive.read("001.jpg") == b"original"
        assert "ComicInfo.xml" in archive.namelist()


@pytest.mark.parametrize("boundary", ["capture", "publish", "commit"])
def test_exception_recovery_preserves_original_or_committed_publication(
    rescan_env, monkeypatch, boundary
):
    import rescan_file_recovery as recovery

    path, _, target, context = _fixture(rescan_env)
    original = path.read_bytes()
    hook = {"capture": "_capture_source", "publish": "_publish", "commit": "_commit"}[
        boundary
    ]
    real = getattr(recovery, hook)

    def fail(*args):
        real(*args)
        raise RuntimeError("injected after durable boundary")

    monkeypatch.setattr(recovery, hook, fail)
    with pytest.raises(RuntimeError, match="injected after durable boundary"):
        recovery.enrich_target(target, context)
    assert path.is_file()
    if boundary == "commit":
        assert _rows(rescan_env)[0]["state"] == "completed"
        with zipfile.ZipFile(path) as archive:
            assert "ComicInfo.xml" in archive.namelist()
    else:
        assert path.read_bytes() == original
        assert _rows(rescan_env)[0]["state"] == "rolled_back"


def test_link_result_gap_retains_both_files_without_receipt_synthesis(
    rescan_env, monkeypatch
):
    import private_file_claim as claims
    import rescan_file_recovery as recovery
    from test_rescan_nfs_recovery import _unsupported_renameat2
    import errno

    _unsupported_renameat2(monkeypatch, errno.EOPNOTSUPP)

    path, _, target, context = _fixture(rescan_env)
    original = path.read_bytes()
    real_link = claims.link_private_regular

    def unacknowledged(*args):
        real_link(*args)
        raise RuntimeError("injected after link before journal receipt")

    monkeypatch.setattr(claims, "link_private_regular", unacknowledged)
    with pytest.raises(RuntimeError, match="injected after link"):
        recovery.enrich_target(target, context)
    row = _rows(rescan_env)[0]
    assert row["state"] == "rollback"
    assert row["publication_receipt_json"] is None
    public = path.read_bytes()
    assert public != original
    source = recovery._record(recovery.load_operation(row["id"]), "source")
    assert source is not None
    assert (Path(source.carrier_path) / "artifact").read_bytes() == original
    assert recovery.replay_rescan_file_operation(row["id"]) == "blocked"
    assert path.read_bytes() == public
    assert (Path(source.carrier_path) / "artifact").read_bytes() == original


@pytest.mark.parametrize("fallback", [False, True])
def test_namespace_and_filesystem_work_do_not_hold_sqlite_writer(
    rescan_env, monkeypatch, fallback
):
    import private_file_claim as claims
    import rescan_file_recovery as recovery

    if fallback:
        from test_rescan_nfs_recovery import _unsupported_renameat2
        import errno

        _unsupported_renameat2(monkeypatch, errno.EOPNOTSUPP)

    _, _, target, context = _fixture(rescan_env)
    checkpoints = []
    for name in (
        "allocate_carrier",
        "claim_into_empty",
        "link_private_regular",
        "discard_private_regular",
        "gc_discarded_carrier",
    ):
        real = getattr(claims, name)

        def probe(*args, _real=real, _name=name, **kwargs):
            with sqlite3.connect(rescan_env["db_path"], timeout=0.05) as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT INTO events(event_type,message) VALUES('probe',?)", (_name,)
                )
            checkpoints.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(claims, name, probe)
    recovery.enrich_target(target, context)
    assert _rows(rescan_env)[0]["state"] == "completed"
    expected = {
        "allocate_carrier",
        "claim_into_empty",
        "discard_private_regular",
        "gc_discarded_carrier",
    }
    if fallback:
        expected.add("link_private_regular")
    assert set(checkpoints) == expected


def test_full_context_cas_loser_keeps_new_metadata_and_original(
    rescan_env, monkeypatch
):
    import rescan_file_recovery as recovery

    path, _, target, context = _fixture(rescan_env)
    original = path.read_bytes()
    real_publish = recovery._publish

    def change_context(*args):
        result = real_publish(*args)
        with sqlite3.connect(rescan_env["db_path"]) as db:
            db.execute("UPDATE series SET title='new owner title' WHERE id=7")
        return result

    monkeypatch.setattr(recovery, "_publish", change_context)
    recovery.enrich_target(target, context)
    assert path.read_bytes() == original
    assert _rows(rescan_env)[0]["state"] == "rolled_back"
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert db.execute("SELECT title FROM series WHERE id=7").fetchone() == (
            "new owner title",
        )


def test_capture_postcheck_restores_changed_race_winner_without_publishing(
    rescan_env, monkeypatch
):
    import private_file_claim as claims
    import rescan_file_recovery as recovery

    path, _, target, context = _fixture(rescan_env)
    replacement = path.parent / "winner.tmp"
    replacement.write_bytes(b"winner")
    real = claims.claim_into_empty

    def replace_then_capture(*args):
        replacement.replace(path)
        return real(*args)

    monkeypatch.setattr(claims, "claim_into_empty", replace_then_capture)
    with pytest.raises(
        claims.PrivateClaimError, match="differs from recorded full fingerprint"
    ):
        recovery.enrich_target(target, context)
    assert path.read_bytes() == b"winner"
    assert _rows(rescan_env)[0]["state"] == "rolled_back"


def test_cbr_cleanup_failure_still_restores_source_but_keeps_active_journal(
    rescan_env, monkeypatch
):
    import rescan
    import rescan_file_recovery as recovery

    path, _, target, context = _fixture(rescan_env)

    # The conversion target differs from the source, so restoration need not
    # remove a possibly changed destination to keep the original available.
    def convert(staged):
        converted = str(Path(staged).with_suffix(".new.cbz"))
        Path(converted).write_bytes(Path(staged).read_bytes())
        return converted

    source = path.with_suffix(".cbr")
    path.rename(source)
    with sqlite3.connect(rescan_env["db_path"]) as db:
        db.execute(
            "UPDATE volumes SET import_path=? WHERE id=?",
            (str(source), target.volume["id"]),
        )
    target = rescan._EnrichmentTarget(
        {**target.volume, "import_path": str(source)},
        target.volume_num,
        str(source),
        rescan._fingerprint(source.stat()),
    )
    original = source.read_bytes()
    monkeypatch.setattr(rescan, "detect_file_type_magic", lambda path: "cbr")
    monkeypatch.setattr(rescan, "convert_cbr_to_cbz", convert)
    monkeypatch.setattr(recovery, "_commit", lambda *args: False)
    monkeypatch.setattr(
        recovery,
        "_remove_publication",
        lambda *args: (_ for _ in ()).throw(RuntimeError("cleanup failure")),
    )
    recovery.enrich_target(target, context)
    assert source.read_bytes() == original
    assert path.is_file()
    assert _rows(rescan_env)[0]["state"] == "rollback"
    assert "cleanup failure" in _rows(rescan_env)[0]["diagnostic"]


def test_unrecorded_allocation_gap_is_not_gc_or_adoption_authority(
    rescan_env, monkeypatch
):
    import rescan_file_recovery as recovery

    path, _, target, context = _fixture(rescan_env)
    original = path.read_bytes()
    real_save = recovery._save_record

    def fail(operation, purpose, record):
        if purpose == "stage":
            raise RuntimeError("injected allocation journal gap")
        return real_save(operation, purpose, record)

    monkeypatch.setattr(recovery, "_save_record", fail)
    with pytest.raises(RuntimeError, match="allocation journal gap"):
        recovery.enrich_target(target, context)
    carriers = [p for p in (path.parent / ".mangarr-claims").iterdir() if p.is_dir()]
    assert len(carriers) == 1
    marker = (carriers[0] / "owner.json").read_bytes()
    row = _rows(rescan_env)[0]
    assert row["state"] == "prepared"
    assert recovery.replay_rescan_file_operation(row["id"]) == "blocked"
    assert (carriers[0] / "owner.json").read_bytes() == marker
    assert path.read_bytes() == original


def test_failed_receipt_writer_does_not_authorize_in_memory_receipt(
    rescan_env, monkeypatch
):
    import rescan_file_recovery as recovery

    path, _, target, context = _fixture(rescan_env)
    original = path.read_bytes()
    real_store = recovery._store

    def fail_receipt(operation, **kwargs):
        if kwargs.get("state") == "published":
            raise sqlite3.OperationalError("injected receipt transaction failure")
        return real_store(operation, **kwargs)

    monkeypatch.setattr(recovery, "_store", fail_receipt)
    with pytest.raises(sqlite3.OperationalError, match="receipt transaction"):
        recovery.enrich_target(target, context)
    row = _rows(rescan_env)[0]
    assert row["state"] == "rollback"
    assert row["publication_receipt_json"] is None
    public = path.read_bytes()
    assert public != original
    operation = recovery.load_operation(row["id"])
    record = recovery._record(operation, "source")
    assert record is not None
    assert (Path(record.carrier_path) / "artifact").read_bytes() == original
    assert recovery.replay_rescan_file_operation(row["id"]) == "blocked"
    assert path.read_bytes() == public


def test_gc_requires_already_committed_discarded_record(rescan_env, monkeypatch):
    import private_file_claim as claims
    import rescan_file_recovery as recovery

    _, _, target, context = _fixture(rescan_env)
    real_gc = claims.gc_discarded_carrier
    seen = []

    def check(namespace, binding, record):
        operation = recovery.load_operation(_rows(rescan_env)[0]["id"])
        durable = recovery._record(operation, binding.purpose)
        assert durable is not None and durable.phase == "discarded"
        assert durable == record
        seen.append(binding.purpose)
        return real_gc(namespace, binding, record)

    monkeypatch.setattr(claims, "gc_discarded_carrier", check)
    recovery.enrich_target(target, context)
    assert set(seen) == {"source", "stage", "publication"}
    assert _rows(rescan_env)[0]["state"] == "completed"


def test_staging_copy_must_prove_original_bytes_before_comicinfo(
    rescan_env, monkeypatch
):
    import rescan_file_recovery as recovery

    path, _, target, context = _fixture(rescan_env)
    original = path.read_bytes()
    other = path.parent / "other.tmp"
    with zipfile.ZipFile(other, "w") as archive:
        archive.writestr("001.jpg", b"wrong file")
    real_copy = recovery.shutil.copy2
    monkeypatch.setattr(
        recovery.shutil,
        "copy2",
        lambda source, destination: real_copy(other, destination),
    )
    recovery.enrich_target(target, context)
    assert path.read_bytes() == original
    assert _rows(rescan_env) == []


def test_root_identity_change_after_publication_loses_context_cas(
    rescan_env, monkeypatch
):
    import rescan_file_recovery as recovery

    path, _, target, context = _fixture(rescan_env)
    original = path.read_bytes()
    real_publish = recovery._publish

    def change_identity(*args):
        result = real_publish(*args)
        with sqlite3.connect(rescan_env["db_path"]) as db:
            db.execute("UPDATE root_folders SET path='/new-root' WHERE id=1")
        return result

    monkeypatch.setattr(recovery, "_publish", change_identity)
    recovery.enrich_target(target, context)
    assert path.read_bytes() == original
    assert _rows(rescan_env)[0]["state"] == "rolled_back"
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert db.execute("SELECT path FROM root_folders WHERE id=1").fetchone() == (
            "/new-root",
        )


def test_changed_publication_before_decision_is_not_committed(rescan_env, monkeypatch):
    import rescan_file_recovery as recovery

    path, _, target, context = _fixture(rescan_env)
    original = path.read_bytes()
    winner = path.parent / "winner.tmp"
    winner.write_bytes(b"unrelated winner")
    real_publish = recovery._publish

    def replace_publication(*args):
        receipt = real_publish(*args)
        winner.replace(receipt.destination_path)
        return receipt

    monkeypatch.setattr(recovery, "_publish", replace_publication)
    recovery.enrich_target(target, context)
    assert path.read_bytes() == b"unrelated winner"
    row = _rows(rescan_env)[0]
    assert row["state"] == "rollback"
    source = recovery._record(recovery.load_operation(row["id"]), "source")
    assert source is not None
    assert (Path(source.carrier_path) / "artifact").read_bytes() == original
    assert recovery.replay_rescan_file_operation(row["id"]) == "blocked"
    assert path.read_bytes() == b"unrelated winner"


@pytest.mark.parametrize("fallback", [False, True])
def test_publication_copy_preserves_prepared_file_permissions(
    rescan_env, monkeypatch, fallback
):
    import rescan_file_recovery as recovery
    import stat
    import errno
    from test_rescan_nfs_recovery import _unsupported_renameat2

    path, _, target, context = _fixture(rescan_env)
    path.chmod(0o664)
    original_group = path.stat().st_gid
    if fallback:
        _unsupported_renameat2(monkeypatch, errno.EOPNOTSUPP)
    recovery.enrich_target(target, context)
    assert _rows(rescan_env)[0]["state"] == "completed"
    assert stat.S_IMODE(path.stat().st_mode) == 0o664
    assert path.stat().st_gid == original_group


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("same_inode", [False, True])
def test_publication_collision_never_adopts_or_unlinks_winner(
    rescan_env, monkeypatch, fallback, same_inode
):
    import errno
    import rescan
    import rescan_file_recovery as recovery
    from test_rescan_nfs_recovery import _unsupported_renameat2

    path, _, target, context = _fixture(rescan_env)
    original = path.read_bytes()
    if fallback:
        _unsupported_renameat2(monkeypatch, errno.EOPNOTSUPP)
    real_rename = rescan._rename_noreplace
    winners = []

    def occupy(source, destination):
        if same_inode:
            os.link(source, destination)
        else:
            Path(destination).write_bytes(b"unrelated collision winner")
        winners.append(Path(destination).read_bytes())
        return real_rename(source, destination)

    monkeypatch.setattr(rescan, "_rename_noreplace", occupy)
    with pytest.raises(FileExistsError):
        recovery.enrich_target(target, context)
    row = _rows(rescan_env)[0]
    assert row["state"] == "rollback"
    assert row["publication_receipt_json"] is None
    assert path.read_bytes() == winners[0]
    operation = recovery.load_operation(row["id"])
    source = recovery._record(operation, "source")
    publication = recovery._record(operation, "publication")
    assert source is not None and publication is not None
    assert (Path(source.carrier_path) / "artifact").read_bytes() == original
    prepared = (Path(publication.carrier_path) / "artifact").read_bytes()
    assert recovery.replay_rescan_file_operation(row["id"]) == "blocked"
    assert path.read_bytes() == winners[0]
    assert (Path(source.carrier_path) / "artifact").read_bytes() == original
    assert (Path(publication.carrier_path) / "artifact").read_bytes() == prepared


@pytest.mark.parametrize("same_inode", [False, True])
def test_source_restore_collision_retains_original_and_winner(
    rescan_env, monkeypatch, same_inode
):
    import private_file_claim as claims
    import rescan_file_recovery as recovery

    path, _, target, context = _fixture(rescan_env)
    original = path.read_bytes()
    monkeypatch.setattr(recovery, "_commit", lambda *args: False)
    real_link = claims.link_private_regular

    def occupy(guard, carrier, parent_fd, name, expected):
        if carrier.record.binding.purpose == "source":
            if same_inode:
                os.link(carrier.artifact_path, path)
            else:
                path.write_bytes(b"unrelated restore winner")
        return real_link(guard, carrier, parent_fd, name, expected)

    monkeypatch.setattr(claims, "link_private_regular", occupy)
    recovery.enrich_target(target, context)
    row = _rows(rescan_env)[0]
    assert row["state"] == "rollback"
    source = recovery._record(recovery.load_operation(row["id"]), "source")
    assert source is not None and source.restore_receipt is None
    assert path.read_bytes() == (
        original if same_inode else b"unrelated restore winner"
    )
    assert (Path(source.carrier_path) / "artifact").read_bytes() == original
    assert recovery.replay_rescan_file_operation(row["id"]) == "blocked"
    assert path.read_bytes() == (
        original if same_inode else b"unrelated restore winner"
    )
    assert (Path(source.carrier_path) / "artifact").read_bytes() == original
