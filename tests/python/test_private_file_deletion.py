"""Actual deletion/replay boundaries for unsupported file claims."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from test_nfs_private_file_workflows import _unsupported_renameat2
from test_volume_file_deletion_journal import deletion_env as deletion_env


class SimulatedCrash(BaseException):
    pass


def _reserve() -> int:
    import volume_file_deletion as deletion

    reservation = deletion.reserve_volume_file_deletion(1, 11)
    assert reservation.journal_id is not None
    return reservation.journal_id


def _record(env: dict[str, object]):
    import private_file_claim as claims

    with sqlite3.connect(str(env["db_path"])) as db:
        encoded = db.execute(
            "SELECT claim_carrier_json FROM volume_file_deletions"
        ).fetchone()[0]
    assert encoded is not None, (
        "filesystem action had no durable private carrier record"
    )
    return claims.CarrierRecord.from_json(encoded)


@pytest.mark.parametrize("fallback", [False, True])
def test_pre_epoch_timestamp_deletes_and_replays_once(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    fallback: bool,
) -> None:
    import errno
    import volume_file_deletion as deletion

    source = Path(str(deletion_env["file_path"]))
    os.utime(source, ns=(-1_000_000_000, -1_000_000_000))
    assert source.stat().st_mtime_ns == -1_000_000_000
    journal_id = _reserve()
    if fallback:
        _unsupported_renameat2(monkeypatch, errno.EOPNOTSUPP)
    outcome = deletion.replay_volume_file_deletion(journal_id)
    journal = deletion._load_journal(journal_id)
    assert journal is not None
    assert outcome == "completed", journal.diagnostic
    record = _record(deletion_env)
    assert record.artifact_fingerprint is not None
    assert record.artifact_fingerprint.mtime_ns == -1_000_000_000
    assert record.phase == "discarded"
    assert not source.exists()
    assert not Path(journal.claim_path).exists()
    assert not Path(record.carrier_path).exists()
    assert deletion.replay_volume_file_deletion(journal_id) == "terminal"
    with sqlite3.connect(str(deletion_env["db_path"])) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM history WHERE event_type='file_deleted'"
        ).fetchone() == (1,)


@pytest.mark.parametrize("boundary", ["capture", "unlink", "gc"])
def test_crash_replay_settles_once_and_gcs_owned_carrier(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    import errno
    import volume_file_deletion as deletion

    journal_id = _reserve()
    _unsupported_renameat2(monkeypatch, errno.EINVAL)
    name = {"capture": "rename", "unlink": "unlink", "gc": "rmdir"}[boundary]
    real = getattr(os, name)
    hit = False

    def crash(*args, **kwargs):
        nonlocal hit
        # Ignore the marker removal: deletion of artifact is the destructive edge.
        selected = name != "unlink" or args[0] == "artifact"
        if selected and not hit:
            hit = True
            _record(deletion_env)
            result = real(*args, **kwargs)
            raise SimulatedCrash(result)
        return real(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, name, crash)
        with pytest.raises(SimulatedCrash):
            deletion.replay_volume_file_deletion(journal_id)
    assert hit
    assert deletion.replay_volume_file_deletion(journal_id) == "completed"
    assert deletion.replay_volume_file_deletion(journal_id) == "terminal"
    assert not Path(str(deletion_env["file_path"])).exists()
    assert sorted(
        p.name
        for p in Path(str(deletion_env["library_root"]), ".mangarr-claims").iterdir()
    ) == ["owner.json"]
    with sqlite3.connect(str(deletion_env["db_path"])) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM history WHERE event_type='file_deleted'"
        ).fetchone() == (1,)


def test_source_replacement_is_restored_never_deleted_by_failed_capture(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import errno
    import volume_file_deletion as deletion

    journal_id = _reserve()
    source = Path(str(deletion_env["file_path"]))
    original = source.read_bytes()
    real = os.rename
    _unsupported_renameat2(monkeypatch, errno.EINVAL)

    def raced(src, dst, **kwargs):
        _record(deletion_env)
        real(source, source.with_name("retained-original"))
        source.write_bytes(b"unrelated replacement")
        return real(src, dst, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "rename", raced)
        assert deletion.replay_volume_file_deletion(journal_id) == "blocked"
    assert source.read_bytes() == b"unrelated replacement"
    assert source.with_name("retained-original").read_bytes() == original
    assert deletion.replay_volume_file_deletion(journal_id) == "blocked"
    assert source.read_bytes() == b"unrelated replacement"
    assert _record(deletion_env).restore_receipt is not None


def test_recreated_public_target_keeps_private_claim_until_safe_retry(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import errno
    import volume_file_deletion as deletion

    journal_id = _reserve()
    source = Path(str(deletion_env["file_path"]))
    original = source.read_bytes()
    real = os.rename
    _unsupported_renameat2(monkeypatch, errno.EINVAL)

    def recreate(src, dst, **kwargs):
        result = real(src, dst, **kwargs)
        source.write_bytes(b"new public target")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(os, "rename", recreate)
        assert deletion.replay_volume_file_deletion(journal_id) == "blocked"
    assert source.read_bytes() == b"new public target"
    assert Path(_record(deletion_env).carrier_path, "artifact").read_bytes() == original
    source.unlink()  # Synthetic replacement removed explicitly by its test owner.
    assert deletion.replay_volume_file_deletion(journal_id) == "completed"


def test_restore_crash_before_receipt_refuses_same_inode_eexist_on_replay(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import errno
    import private_file_claim as claims
    import volume_file_deletion as deletion

    journal_id = _reserve()
    journal = deletion._load_journal(journal_id)
    assert journal is not None
    os.rename(journal.target_path, journal.claim_path)
    Path(journal.claim_path).write_bytes(b"legacy changed bytes")
    _unsupported_renameat2(monkeypatch, errno.EINVAL)
    real = claims.link_private_regular

    def crash(*args, **kwargs):
        real(*args, **kwargs)
        raise SimulatedCrash("link succeeded before durable receipt")

    with monkeypatch.context() as patch:
        patch.setattr(claims, "link_private_regular", crash)
        with pytest.raises(SimulatedCrash):
            deletion.replay_volume_file_deletion(journal_id)
    assert _record(deletion_env).phase == "restoring"
    assert _record(deletion_env).restore_receipt is None
    assert deletion.replay_volume_file_deletion(journal_id) == "blocked"
    assert Path(journal.target_path).read_bytes() == b"legacy changed bytes"
    assert (
        Path(_record(deletion_env).carrier_path, "artifact").read_bytes()
        == b"legacy changed bytes"
    )
    with sqlite3.connect(str(deletion_env["db_path"])) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM history WHERE event_type='file_deleted'"
        ).fetchone() == (0,)


def test_foreign_binding_record_is_refused_before_filesystem_action(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import errno
    import volume_file_deletion as deletion

    journal_id = _reserve()
    _unsupported_renameat2(monkeypatch, errno.EINVAL)

    def crash(*args, **kwargs):
        raise SimulatedCrash("before capture")

    with monkeypatch.context() as patch:
        patch.setattr(os, "rename", crash)
        with pytest.raises(SimulatedCrash):
            deletion.replay_volume_file_deletion(journal_id)
    record = json.loads(_record(deletion_env).to_json())
    record["binding"]["operation_key"] = "another journal"
    with sqlite3.connect(str(deletion_env["db_path"])) as db:
        db.execute(
            "UPDATE volume_file_deletions SET claim_carrier_json=?",
            (json.dumps(record),),
        )
    before = Path(str(deletion_env["file_path"])).read_bytes()
    assert deletion.replay_volume_file_deletion(journal_id) == "blocked"
    assert Path(str(deletion_env["file_path"])).read_bytes() == before


@pytest.mark.parametrize("legacy", [False, True])
def test_native_deletion_recaptures_flat_claim_before_private_discard(
    deletion_env: dict[str, object],
    legacy: bool,
) -> None:
    import volume_file_deletion as deletion

    journal_id = _reserve()
    journal = deletion._load_journal(journal_id)
    assert journal is not None
    if legacy:
        os.rename(journal.target_path, journal.claim_path)
    assert deletion.replay_volume_file_deletion(journal_id) == "completed"
    assert deletion.replay_volume_file_deletion(journal_id) == "terminal"
    assert _record(deletion_env).phase == "discarded"
    assert _record(deletion_env).origin_path == journal.claim_path
    assert not Path(journal.claim_path).exists()
    with sqlite3.connect(str(deletion_env["db_path"])) as db:
        assert db.execute("SELECT COUNT(*) FROM file_claim_namespaces").fetchone() == (
            1,
        )
        assert db.execute(
            "SELECT COUNT(*) FROM history WHERE event_type='file_deleted'"
        ).fetchone() == (1,)
    assert os.listdir(Path(str(deletion_env["library_root"]), ".mangarr-claims")) == [
        "owner.json"
    ]


def test_durable_carrier_write_failure_never_moves_public_source(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import errno
    import volume_file_deletion as deletion

    journal_id = _reserve()
    source = Path(str(deletion_env["file_path"]))
    before = source.read_bytes()
    _unsupported_renameat2(monkeypatch, errno.EINVAL)

    def failed_record(*args, **kwargs):
        raise sqlite3.OperationalError("injected durable carrier write failure")

    monkeypatch.setattr(deletion, "_store_carrier", failed_record)
    assert deletion.replay_volume_file_deletion(journal_id) == "blocked"
    assert source.read_bytes() == before
    with sqlite3.connect(str(deletion_env["db_path"])) as db:
        assert db.execute(
            "SELECT state,claim_carrier_json FROM volume_file_deletions"
        ).fetchone() == ("active", None)
        assert db.execute(
            "SELECT COUNT(*) FROM history WHERE event_type='file_deleted'"
        ).fetchone() == (0,)


def test_changed_private_artifact_is_retained_when_public_restore_is_occupied(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import errno
    import volume_file_deletion as deletion

    journal_id = _reserve()
    real = os.rename
    _unsupported_renameat2(monkeypatch, errno.EINVAL)

    def captured(*args, **kwargs):
        real(*args, **kwargs)
        raise SimulatedCrash("after capture")

    with monkeypatch.context() as patch:
        patch.setattr(os, "rename", captured)
        with pytest.raises(SimulatedCrash):
            deletion.replay_volume_file_deletion(journal_id)
    artifact = Path(_record(deletion_env).carrier_path, "artifact")
    artifact.write_bytes(b"changed private bytes")
    source = Path(str(deletion_env["file_path"]))
    source.write_bytes(b"unrelated public bytes")
    assert deletion.replay_volume_file_deletion(journal_id) == "blocked"
    assert artifact.read_bytes() == b"changed private bytes"
    assert source.read_bytes() == b"unrelated public bytes"


def test_changed_restored_public_file_refuses_private_cleanup_on_replay(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import errno
    import volume_file_deletion as deletion

    journal_id = _reserve()
    journal = deletion._load_journal(journal_id)
    assert journal is not None
    os.rename(journal.target_path, journal.claim_path)
    Path(journal.claim_path).write_bytes(b"changed legacy bytes")
    _unsupported_renameat2(monkeypatch, errno.EINVAL)
    real = deletion._store_carrier

    def restored(*args, **kwargs):
        result = real(*args, **kwargs)
        if args[2].phase == "restored":
            raise SimulatedCrash("after durable restore receipt")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(deletion, "_store_carrier", restored)
        with pytest.raises(SimulatedCrash):
            deletion.replay_volume_file_deletion(journal_id)
    source = Path(journal.target_path)
    source.unlink()
    source.write_bytes(b"replaced restored file")
    assert deletion.replay_volume_file_deletion(journal_id) == "blocked"
    assert (
        Path(_record(deletion_env).carrier_path, "artifact").read_bytes()
        == b"changed legacy bytes"
    )
    assert source.read_bytes() == b"replaced restored file"


def test_discard_intent_write_failure_retains_claim_and_no_audit(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import errno
    import volume_file_deletion as deletion

    journal_id = _reserve()
    _unsupported_renameat2(monkeypatch, errno.EINVAL)
    real = deletion._store_carrier

    def failed_discard(*args, **kwargs):
        if args[2].phase == "discarding":
            raise sqlite3.OperationalError("injected discard intent failure")
        return real(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(deletion, "_store_carrier", failed_discard)
        assert deletion.replay_volume_file_deletion(journal_id) == "blocked"
    assert _record(deletion_env).phase == "claimed"
    assert (
        Path(_record(deletion_env).carrier_path, "artifact").read_bytes()
        == b"journal-volume-payload"
    )
    assert deletion.replay_volume_file_deletion(journal_id) == "completed"


def test_contending_fallback_replay_never_rewrites_live_owner_record(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import errno
    import volume_file_deletion as deletion

    journal_id = _reserve()
    _unsupported_renameat2(monkeypatch, errno.EINVAL)
    entered = threading.Event()
    release = threading.Event()
    real = os.rename
    calls = []

    def paused(*args, **kwargs):
        calls.append(args)
        entered.set()
        assert release.wait(10), "owner was not released"
        return real(*args, **kwargs)

    monkeypatch.setattr(os, "rename", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        owner = pool.submit(deletion.replay_volume_file_deletion, journal_id)
        try:
            assert entered.wait(10), "owner did not reach private capture"
            with sqlite3.connect(str(deletion_env["db_path"])) as db:
                before = db.execute(
                    "SELECT claim_carrier_json,diagnostic,updated_at FROM volume_file_deletions"
                ).fetchone()
            assert deletion.replay_volume_file_deletion(journal_id) == "blocked"
            with sqlite3.connect(str(deletion_env["db_path"])) as db:
                assert (
                    db.execute(
                        "SELECT claim_carrier_json,diagnostic,updated_at FROM volume_file_deletions"
                    ).fetchone()
                    == before
                )
            assert len(calls) == 1
        finally:
            release.set()
        assert owner.result(timeout=10) == "completed"


def test_fallback_workflow_performs_no_filesystem_io_under_sqlite_writer(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import errno
    import private_file_claim as claims
    import volume_file_deletion as deletion

    journal_id = _reserve()
    _unsupported_renameat2(monkeypatch, errno.EINVAL)
    real_get_db = deletion.get_db
    connections = []

    @contextmanager
    def tracked_db():
        with real_get_db() as db:
            connections.append(db)
            try:
                yield db
            finally:
                connections.remove(db)

    def checked(action):
        def run(*args, **kwargs):
            assert not any(db.in_transaction for db in connections), (
                "FS under SQLite writer"
            )
            return action(*args, **kwargs)

        return run

    monkeypatch.setattr(deletion, "get_db", tracked_db)
    monkeypatch.setattr(claims, "get_db", tracked_db)
    for name in (
        "open",
        "stat",
        "lstat",
        "fstat",
        "fsync",
        "mkdir",
        "fchmod",
        "rename",
        "link",
        "unlink",
        "rmdir",
        "read",
        "write",
    ):
        monkeypatch.setattr(os, name, checked(getattr(os, name)))
    assert deletion.replay_volume_file_deletion(journal_id) == "completed"


def test_carrier_record_on_missing_target_journal_is_refused_conservatively(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import errno
    import volume_file_deletion as deletion

    journal_id = _reserve()
    _unsupported_renameat2(monkeypatch, errno.EINVAL)

    def crash(*_args, **_kwargs):
        raise SimulatedCrash("before capture")

    with monkeypatch.context() as patch:
        patch.setattr(os, "rename", crash)
        with pytest.raises(SimulatedCrash):
            deletion.replay_volume_file_deletion(journal_id)
    with sqlite3.connect(str(deletion_env["db_path"])) as db:
        db.execute("UPDATE volume_file_deletions SET target_present=0")
    assert deletion.replay_volume_file_deletion(journal_id) == "blocked"
    assert (
        Path(str(deletion_env["file_path"])).read_bytes() == b"journal-volume-payload"
    )


@pytest.mark.parametrize("legacy", [False, True])
def test_shared_matching_flat_claim_replacement_is_preserved_before_cleanup(
    deletion_env: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    legacy: bool,
) -> None:
    import volume_file_deletion as deletion

    journal_id = _reserve()
    journal = deletion._load_journal(journal_id)
    assert journal is not None
    if legacy:
        os.rename(journal.target_path, journal.claim_path)
    saved = Path(journal.claim_path).with_name("retained-recorded-flat-claim")
    real = deletion._regular_fingerprint
    replaced = False

    def replace_after_check(path):
        nonlocal replaced
        fingerprint = real(path)
        if path == journal.claim_path and not replaced:
            replaced = True
            os.rename(path, saved)
            Path(path).write_bytes(b"unrelated shared-flat replacement")
        return fingerprint

    monkeypatch.setattr(deletion, "_regular_fingerprint", replace_after_check)
    assert deletion.replay_volume_file_deletion(journal_id) == "blocked"
    assert replaced
    assert saved.read_bytes() == b"journal-volume-payload"
    assert (
        Path(journal.target_path).read_bytes() == b"unrelated shared-flat replacement"
    )
    assert _record(deletion_env).restore_receipt is not None
    with sqlite3.connect(str(deletion_env["db_path"])) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM history WHERE event_type='file_deleted'"
        ).fetchone() == (0,)
