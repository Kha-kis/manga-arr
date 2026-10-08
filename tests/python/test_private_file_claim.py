"""Private namespace ownership, capture, restoration, and bounded GC."""

from __future__ import annotations

import errno
import hashlib
import importlib
import os
import sqlite3
import stat
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from test_volume_file_deletion_journal import deletion_env as deletion_env


def _module() -> ModuleType:
    try:
        return importlib.import_module("private_file_claim")
    except ModuleNotFoundError:
        pytest.fail("durable private-file claim helper is not implemented")


@pytest.fixture
def claim_env(deletion_env: dict[str, object]) -> tuple[str, Path]:
    return str(deletion_env["db_path"]), Path(str(deletion_env["library_root"]))


def _fingerprint(module: ModuleType, path: Path) -> Any:
    st = path.stat()
    return module.FullFileFingerprint(
        st.st_dev,
        st.st_ino,
        st.st_size,
        st.st_mtime_ns,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )


@pytest.mark.parametrize("mtime_ns", [-1_000_000_000, -1, 0, 1])
def test_fingerprint_accepts_signed_integer_timestamps(mtime_ns: int) -> None:
    import private_file_claim as claims

    value = {"dev": 1, "inode": 2, "size": 3, "mtime_ns": mtime_ns, "sha256": "0" * 64}
    fingerprint = claims.FullFileFingerprint.from_value(value)
    assert fingerprint.mtime_ns == mtime_ns


@pytest.mark.parametrize("mtime_ns", [True, False, 1.0, -1.0, "-1", None, []])
def test_fingerprint_rejects_noninteger_timestamps(mtime_ns: object) -> None:
    import private_file_claim as claims

    value = {"dev": 1, "inode": 2, "size": 3, "mtime_ns": mtime_ns, "sha256": "0" * 64}
    with pytest.raises(claims.PrivateClaimError):
        claims.FullFileFingerprint.from_value(value)


@pytest.mark.parametrize("field", ["dev", "inode", "size"])
@pytest.mark.parametrize("invalid", [-1, True, False, 1.0, "1", None])
def test_fingerprint_other_integers_remain_nonnegative_and_strict(
    field: str,
    invalid: object,
) -> None:
    import private_file_claim as claims

    value: dict[str, object] = {
        "dev": 1,
        "inode": 2,
        "size": 3,
        "mtime_ns": -1,
        "sha256": "0" * 64,
    }
    value[field] = invalid
    with pytest.raises(claims.PrivateClaimError):
        claims.FullFileFingerprint.from_value(value)


def _binding(module: ModuleType) -> Any:
    return module.ClaimBinding("deletion", "synthetic-operation", 11, "delete")


def _registry(db_path: str) -> str | None:
    with sqlite3.connect(db_path) as db:
        row = db.execute("SELECT ownership_json FROM file_claim_namespaces").fetchone()
    assert row is not None
    return row[0]


def test_namespace_never_adopts_an_unregistered_root(
    claim_env: tuple[str, Path],
) -> None:
    from file_mutation_lock import file_mutation_guard

    db_path, parent = claim_env
    module = _module()
    root = parent / ".mangarr-claims"
    root.mkdir(mode=0o700)
    (root / "owner.json").write_bytes(b"unrelated marker")
    with file_mutation_guard(db_path) as guard:
        with pytest.raises(module.PrivateClaimError):
            with module.ensure_namespace(guard, str(parent)):
                pytest.fail("unregistered namespace adopted")
    assert (root / "owner.json").read_bytes() == b"unrelated marker"


def test_namespace_reuses_ready_proof_and_rejects_pending_creation(
    claim_env: tuple[str, Path],
) -> None:
    from file_mutation_lock import file_mutation_guard

    db_path, parent = claim_env
    module = _module()
    with file_mutation_guard(db_path) as guard:
        with module.ensure_namespace(guard, str(parent)) as namespace:
            identity = os.fstat(namespace.fd).st_ino
            namespace.verify()
        ready = _registry(db_path)
        assert ready is not None
        with module.ensure_namespace(guard, str(parent)) as reopened:
            assert os.fstat(reopened.fd).st_ino == identity
        with sqlite3.connect(db_path) as db:
            db.execute("UPDATE file_claim_namespaces SET ownership_json=NULL")
        with pytest.raises(module.PrivateClaimError):
            with module.ensure_namespace(guard, str(parent)):
                pytest.fail("pending registry row was adopted")
    assert _registry(db_path) is None
    assert (parent / ".mangarr-claims").stat().st_ino == identity


def test_namespace_creation_barrier_failure_retains_pending_proof_gap(
    claim_env: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from file_mutation_lock import file_mutation_guard

    db_path, parent = claim_env
    module = _module()
    real_fsync = os.fsync
    failed = False

    def fail_once(fd: int) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError(errno.EIO, "injected creation barrier failure")
        real_fsync(fd)

    with file_mutation_guard(db_path) as guard:
        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", fail_once)
            with pytest.raises(OSError):
                with module.ensure_namespace(guard, str(parent)):
                    pytest.fail("unflushed namespace became ready")
        assert _registry(db_path) is None
        with pytest.raises(module.PrivateClaimError):
            with module.ensure_namespace(guard, str(parent)):
                pytest.fail("creation gap was inferred as ownership")
    assert (parent / ".mangarr-claims").is_dir()


@pytest.mark.parametrize("changed", ["namespace", "carrier", "marker"])
def test_replaced_ownership_proof_refuses_capture(
    claim_env: tuple[str, Path],
    changed: str,
) -> None:
    from file_mutation_lock import file_mutation_guard

    db_path, parent = claim_env
    module = _module()
    source = parent / "source"
    source.write_bytes(b"original")
    with file_mutation_guard(db_path) as guard:
        with module.ensure_namespace(guard, str(parent)) as namespace:
            with module.allocate_carrier(
                namespace, _binding(module), str(source), _fingerprint(module, source)
            ) as carrier:
                path = Path(
                    namespace.path
                    if changed == "namespace"
                    else carrier.record.carrier_path
                )
                if changed == "marker":
                    marker = path / "owner.json"
                    marker.rename(path / "old-marker")
                    marker.write_bytes(b"replacement")
                else:
                    path.rename(path.with_name(path.name + "-original"))
                    path.mkdir(mode=0o700)
                    (path / "unrelated").write_bytes(b"replacement")
                carrier.record = replace(carrier.record, phase="claiming")
                with pytest.raises(module.PrivateClaimError):
                    module.claim_into_empty(
                        guard, carrier, namespace.parent_fd, source.name
                    )
    assert source.read_bytes() == b"original"


def test_occupied_private_child_is_never_overwritten(
    claim_env: tuple[str, Path],
) -> None:
    from file_mutation_lock import file_mutation_guard

    db_path, parent = claim_env
    module = _module()
    source = parent / "source"
    source.write_bytes(b"original")
    with file_mutation_guard(db_path) as guard:
        with module.ensure_namespace(guard, str(parent)) as namespace:
            with module.allocate_carrier(
                namespace, _binding(module), str(source), _fingerprint(module, source)
            ) as carrier:
                Path(carrier.artifact_path).write_bytes(b"collision")
                carrier.record = replace(carrier.record, phase="claiming")
                with pytest.raises((FileExistsError, module.PrivateClaimError)):
                    module.claim_into_empty(
                        guard, carrier, namespace.parent_fd, source.name
                    )
                assert Path(carrier.artifact_path).read_bytes() == b"collision"
    assert source.read_bytes() == b"original"


def test_replacement_during_capture_is_retained_not_deleted(
    claim_env: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from file_mutation_lock import file_mutation_guard

    db_path, parent = claim_env
    module = _module()
    source = parent / "source"
    source.write_bytes(b"original")
    real_rename = os.rename
    with file_mutation_guard(db_path) as guard:
        with module.ensure_namespace(guard, str(parent)) as namespace:
            with module.allocate_carrier(
                namespace, _binding(module), str(source), _fingerprint(module, source)
            ) as carrier:
                carrier.record = replace(carrier.record, phase="claiming")

                def raced(src: Any, dst: Any, **kwargs: Any) -> None:
                    real_rename(source, parent / "original-retained")
                    source.write_bytes(b"replacement")
                    real_rename(src, dst, **kwargs)

                monkeypatch.setattr(os, "rename", raced)
                with pytest.raises(module.PrivateClaimError):
                    module.claim_into_empty(
                        guard, carrier, namespace.parent_fd, source.name
                    )
                assert Path(carrier.artifact_path).read_bytes() == b"replacement"
    assert (parent / "original-retained").read_bytes() == b"original"


def test_pinned_namespace_does_not_retarget_capture_to_replacement_root(
    claim_env: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from file_mutation_lock import file_mutation_guard

    db_path, parent = claim_env
    module = _module()
    source = parent / "source"
    source.write_bytes(b"original")
    real_rename = os.rename
    with file_mutation_guard(db_path) as guard:
        with module.ensure_namespace(guard, str(parent)) as namespace:
            with module.allocate_carrier(
                namespace, _binding(module), str(source), _fingerprint(module, source)
            ) as carrier:
                root = Path(namespace.path)
                original = root.with_name("retained-namespace")
                carrier.record = replace(carrier.record, phase="claiming")

                def raced(src: Any, dst: Any, **kwargs: Any) -> None:
                    real_rename(root, original)
                    root.mkdir(mode=0o700)
                    (root / "unrelated").write_bytes(b"untouched")
                    real_rename(src, dst, **kwargs)

                monkeypatch.setattr(os, "rename", raced)
                with pytest.raises(module.PrivateClaimError):
                    module.claim_into_empty(
                        guard, carrier, namespace.parent_fd, source.name
                    )
                assert (
                    original / Path(carrier.record.carrier_path).name / "artifact"
                ).read_bytes() == b"original"
                assert sorted(p.name for p in root.iterdir()) == ["unrelated"]
                assert (root / "unrelated").read_bytes() == b"untouched"


def test_atomic_private_restore_refuses_same_inode_eexist(
    claim_env: tuple[str, Path],
) -> None:
    from file_mutation_lock import file_mutation_guard

    db_path, parent = claim_env
    module = _module()
    source = parent / "source"
    source.write_bytes(b"original")
    expected = _fingerprint(module, source)
    with file_mutation_guard(db_path) as guard:
        with module.ensure_namespace(guard, str(parent)) as namespace:
            with module.allocate_carrier(
                namespace, _binding(module), str(source), expected
            ) as carrier:
                carrier.record = replace(carrier.record, phase="claiming")
                module.claim_into_empty(
                    guard, carrier, namespace.parent_fd, source.name
                )
                carrier.record = replace(carrier.record, phase="restoring")
                receipt = module.link_private_regular(
                    guard, carrier, namespace.parent_fd, source.name, expected
                )
                assert receipt.destination_path == str(source)
                with pytest.raises(FileExistsError):
                    module.link_private_regular(
                        guard, carrier, namespace.parent_fd, source.name, expected
                    )
                assert (
                    source.read_bytes()
                    == Path(carrier.artifact_path).read_bytes()
                    == b"original"
                )


@pytest.mark.parametrize("obstruction", ["occupied", "changed"])
def test_failed_restore_or_discard_retains_private_artifact(
    claim_env: tuple[str, Path],
    obstruction: str,
) -> None:
    from file_mutation_lock import file_mutation_guard

    db_path, parent = claim_env
    module = _module()
    source = parent / "source"
    source.write_bytes(b"original")
    expected = _fingerprint(module, source)
    with file_mutation_guard(db_path) as guard:
        with module.ensure_namespace(guard, str(parent)) as namespace:
            with module.allocate_carrier(
                namespace, _binding(module), str(source), expected
            ) as carrier:
                carrier.record = replace(carrier.record, phase="claiming")
                module.claim_into_empty(
                    guard, carrier, namespace.parent_fd, source.name
                )
                if obstruction == "occupied":
                    source.write_bytes(b"unrelated")
                    carrier.record = replace(carrier.record, phase="restoring")
                    with pytest.raises(FileExistsError):
                        module.link_private_regular(
                            guard, carrier, namespace.parent_fd, source.name, expected
                        )
                    assert source.read_bytes() == b"unrelated"
                else:
                    Path(carrier.artifact_path).write_bytes(b"changed")
                    carrier.record = replace(carrier.record, phase="discarding")
                    with pytest.raises(module.PrivateClaimError):
                        module.discard_private_regular(guard, carrier, expected)
                assert Path(carrier.artifact_path).exists()


def test_discarded_gc_is_bounded_and_recovers_interrupted_marker_removal(
    claim_env: tuple[str, Path],
) -> None:
    from file_mutation_lock import file_mutation_guard

    db_path, parent = claim_env
    module = _module()
    with file_mutation_guard(db_path) as guard:
        with module.ensure_namespace(guard, str(parent)) as namespace:
            root_inode = os.fstat(namespace.fd).st_ino
            for number in range(100):
                source = parent / f"source-{number}"
                source.write_bytes(b"original")
                expected = _fingerprint(module, source)
                with module.allocate_carrier(
                    namespace, _binding(module), str(source), expected
                ) as carrier:
                    carrier.record = replace(carrier.record, phase="claiming")
                    module.claim_into_empty(
                        guard, carrier, namespace.parent_fd, source.name
                    )
                    carrier.record = replace(carrier.record, phase="discarding")
                    module.discard_private_regular(guard, carrier, expected)
                    discarded = replace(carrier.record, phase="discarded")
                    if number == 0:
                        os.unlink("owner.json", dir_fd=carrier.fd)
                module.gc_discarded_carrier(namespace, _binding(module), discarded)
                module.gc_discarded_carrier(namespace, _binding(module), discarded)
            assert os.fstat(namespace.fd).st_ino == root_inode
            assert os.listdir(namespace.fd) == ["owner.json"]


def test_gc_requires_discarded_phase_and_no_unexpected_entries(
    claim_env: tuple[str, Path],
) -> None:
    from file_mutation_lock import file_mutation_guard

    db_path, parent = claim_env
    module = _module()
    with file_mutation_guard(db_path) as guard:
        with module.ensure_namespace(guard, str(parent)) as namespace:
            with module.allocate_carrier(
                namespace, _binding(module), str(parent / "source")
            ) as carrier:
                record = carrier.record
                with pytest.raises(module.PrivateClaimError):
                    module.gc_discarded_carrier(namespace, _binding(module), record)
                Path(record.carrier_path, "unrelated").write_bytes(b"untouched")
                with pytest.raises(module.PrivateClaimError):
                    module.gc_discarded_carrier(
                        namespace, _binding(module), replace(record, phase="discarded")
                    )
                assert (
                    Path(record.carrier_path, "unrelated").read_bytes() == b"untouched"
                )


def test_carrier_parser_rejects_duplicate_unknown_version_and_wrong_binding(
    claim_env: tuple[str, Path],
) -> None:
    from file_mutation_lock import file_mutation_guard

    db_path, parent = claim_env
    module = _module()
    binding = _binding(module)
    with file_mutation_guard(db_path) as guard:
        with module.ensure_namespace(guard, str(parent)) as namespace:
            with module.allocate_carrier(
                namespace, binding, str(parent / "source")
            ) as carrier:
                encoded = carrier.record.to_json()
                assert module.CarrierRecord.from_json(encoded) == carrier.record
                with pytest.raises(module.PrivateClaimError):
                    module.CarrierRecord.from_json(encoded[:-1] + ',"version":1}')
                with pytest.raises(module.PrivateClaimError):
                    module.CarrierRecord.from_json(
                        encoded.replace('"version":1', '"version":99')
                    )
                wrong = replace(binding, operation_key="another-operation")
                with pytest.raises(module.PrivateClaimError):
                    with module.open_carrier(namespace, wrong, carrier.record):
                        pytest.fail("carrier from another operation opened")


@pytest.mark.parametrize("window", ["captured", "unlinked", "link-barrier"])
def test_durable_record_reopens_each_file_mutation_crash_window(
    claim_env: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    window: str,
) -> None:
    from file_mutation_lock import file_mutation_guard

    db_path, parent = claim_env
    module = _module()
    source = parent / "source"
    source.write_bytes(b"original")
    expected = _fingerprint(module, source)
    with file_mutation_guard(db_path) as guard:
        with module.ensure_namespace(guard, str(parent)) as namespace:
            with module.allocate_carrier(
                namespace, _binding(module), str(source), expected
            ) as carrier:
                carrier.record = replace(carrier.record, phase="claiming")
                module.claim_into_empty(
                    guard, carrier, namespace.parent_fd, source.name
                )
                if window == "unlinked":
                    carrier.record = replace(carrier.record, phase="discarding")
                    module.discard_private_regular(guard, carrier, expected)
                elif window == "link-barrier":
                    carrier.record = replace(carrier.record, phase="restoring")
                    with monkeypatch.context() as patch:

                        def failed_barrier(fd: int) -> None:
                            raise OSError(
                                errno.EIO, "injected post-link barrier failure"
                            )

                        patch.setattr(os, "fsync", failed_barrier)
                        with pytest.raises(OSError):
                            module.link_private_regular(
                                guard,
                                carrier,
                                namespace.parent_fd,
                                source.name,
                                expected,
                            )
                    assert source.read_bytes() == b"original"
                encoded = carrier.record.to_json()
    # A fresh capability and descriptors simulate the next process; ownership
    # comes from the serialized record, never an inode-equality guess.
    with file_mutation_guard(db_path) as guard:
        with module.ensure_namespace(guard, str(parent)) as namespace:
            with module.open_carrier(
                namespace, _binding(module), module.CarrierRecord.from_json(encoded)
            ) as carrier:
                if window == "link-barrier":
                    with pytest.raises(FileExistsError):
                        module.link_private_regular(
                            guard, carrier, namespace.parent_fd, source.name, expected
                        )
                    assert Path(carrier.artifact_path).read_bytes() == b"original"
                else:
                    carrier.record = replace(carrier.record, phase="discarding")
                    module.discard_private_regular(guard, carrier, expected)
                    discarded = replace(carrier.record, phase="discarded")
            if window != "link-barrier":
                module.gc_discarded_carrier(namespace, _binding(module), discarded)
                assert os.listdir(namespace.fd) == ["owner.json"]


def test_capture_barrier_failure_retains_both_durable_claim_and_bytes(
    claim_env: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from file_mutation_lock import file_mutation_guard

    db_path, parent = claim_env
    module = _module()
    source = parent / "source"
    source.write_bytes(b"original")
    with file_mutation_guard(db_path) as guard:
        with module.ensure_namespace(guard, str(parent)) as namespace:
            with module.allocate_carrier(
                namespace, _binding(module), str(source), _fingerprint(module, source)
            ) as carrier:
                carrier.record = replace(carrier.record, phase="claiming")
                with monkeypatch.context() as patch:

                    def fail(fd: int) -> None:
                        raise OSError(errno.EIO, "capture barrier failed")

                    patch.setattr(os, "fsync", fail)
                    with pytest.raises(OSError):
                        module.claim_into_empty(
                            guard, carrier, namespace.parent_fd, source.name
                        )
                assert not source.exists()
                assert Path(carrier.artifact_path).read_bytes() == b"original"
                carrier.verify()


@pytest.mark.parametrize(
    "bad",
    ["unknown-field", "boolean-version", "relative-origin", "bad-digest", "bad-phase"],
)
def test_malformed_durable_carrier_never_authorizes_reopen(
    claim_env: tuple[str, Path],
    bad: str,
) -> None:
    import json
    from file_mutation_lock import file_mutation_guard

    db_path, parent = claim_env
    module = _module()
    with file_mutation_guard(db_path) as guard:
        with module.ensure_namespace(guard, str(parent)) as namespace:
            with module.allocate_carrier(
                namespace, _binding(module), str(parent / "source")
            ) as carrier:
                value = json.loads(carrier.record.to_json())
                if bad == "unknown-field":
                    value["extra"] = 1
                elif bad == "boolean-version":
                    value["version"] = True
                elif bad == "relative-origin":
                    value["origin_path"] = "source"
                elif bad == "bad-digest":
                    value["marker_fingerprint"]["sha256"] = "not-a-digest"
                else:
                    value["phase"] = "guessed-owned"
                with pytest.raises(module.PrivateClaimError):
                    module.CarrierRecord.from_json(json.dumps(value))


def test_namespace_sql_never_holds_writer_during_filesystem_io(
    claim_env: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from file_mutation_lock import file_mutation_guard

    db_path, parent = claim_env
    module = _module()
    real_get_db = module.get_db
    connections: list[sqlite3.Connection] = []

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
                "filesystem IO under SQLite writer"
            )
            return action(*args, **kwargs)

        return run

    monkeypatch.setattr(module, "get_db", tracked_db)
    for name in (
        "open",
        "stat",
        "lstat",
        "fstat",
        "fsync",
        "mkdir",
        "fchmod",
        "read",
        "write",
        "unlink",
        "rmdir",
    ):
        monkeypatch.setattr(os, name, checked(getattr(os, name)))
    with file_mutation_guard(db_path) as guard:
        with module.ensure_namespace(guard, str(parent)) as namespace:
            namespace.verify()
        with module.ensure_namespace(guard, str(parent)) as namespace:
            namespace.verify()


def test_private_namespace_under_setgid_media_parent_preserves_parent_mode(
    claim_env: tuple[str, Path],
) -> None:
    from file_mutation_lock import file_mutation_guard

    db_path, parent = claim_env
    module = _module()
    parent.chmod(0o2770)
    before = parent.stat()
    with file_mutation_guard(db_path) as guard:
        with module.ensure_namespace(guard, str(parent)) as namespace:
            assert stat.S_IMODE(os.fstat(namespace.fd).st_mode) == 0o700
            with module.allocate_carrier(
                namespace, _binding(module), str(parent / "source")
            ) as carrier:
                assert stat.S_IMODE(os.fstat(carrier.fd).st_mode) == 0o700
    after = parent.stat()
    assert (
        after.st_dev,
        after.st_ino,
        after.st_uid,
        after.st_gid,
        stat.S_IMODE(after.st_mode),
    ) == (
        before.st_dev,
        before.st_ino,
        before.st_uid,
        before.st_gid,
        0o2770,
    )


def test_capture_and_discard_flush_pinned_directories_before_return(
    claim_env: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from file_mutation_lock import file_mutation_guard

    db_path, parent = claim_env
    module = _module()
    source = parent / "source"
    source.write_bytes(b"original")
    expected = _fingerprint(module, source)
    with file_mutation_guard(db_path) as guard:
        with module.ensure_namespace(guard, str(parent)) as namespace:
            with module.allocate_carrier(
                namespace, _binding(module), str(source), expected
            ) as carrier:
                calls = []
                real_rename, real_unlink, real_fsync = os.rename, os.unlink, os.fsync

                def renamed(*args, **kwargs):
                    real_rename(*args, **kwargs)
                    calls.append("capture")

                def unlinked(*args, **kwargs):
                    real_unlink(*args, **kwargs)
                    calls.append("discard")

                def flushed(fd):
                    real_fsync(fd)
                    calls.append(("flush", fd))

                monkeypatch.setattr(os, "rename", renamed)
                monkeypatch.setattr(os, "unlink", unlinked)
                monkeypatch.setattr(os, "fsync", flushed)
                carrier.record = replace(carrier.record, phase="claiming")
                module.claim_into_empty(
                    guard, carrier, namespace.parent_fd, source.name
                )
                carrier.record = replace(carrier.record, phase="discarding")
                module.discard_private_regular(guard, carrier, expected)
                assert calls == [
                    "capture",
                    ("flush", carrier.fd),
                    ("flush", namespace.parent_fd),
                    "discard",
                    ("flush", carrier.fd),
                ]
