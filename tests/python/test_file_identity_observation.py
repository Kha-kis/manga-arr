"""Read-only ownership acquisition must preserve live local SQLite state."""

from __future__ import annotations

import importlib
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import zipfile
from dataclasses import FrozenInstanceError, asdict, dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from file_identity import (
    IdentityUnavailable,
    InvalidIdentity,
    NativeHandle,
    OwnerRef,
    RoleKey,
)
from file_identity_consumers import (
    InventoryBudget,
    ReferenceClosure,
    RoleSpec,
    collect_references,
)
from file_mutation_lock import FileMutationLockError, file_mutation_guard
from test_file_identity_schema import ledger_path as ledger_path
from test_pack_publication_integration import (
    other_uid_python_image as other_uid_python_image,
)


def observer() -> ModuleType:
    try:
        return importlib.import_module("file_identity_recovery")
    except ModuleNotFoundError as exc:
        if exc.name != "file_identity_recovery":
            raise
        pytest.fail("Task4b1 readonly observation foundation is not implemented")


def test_empty_real_schema_with_live_guard_is_complete(ledger_path: Path) -> None:
    module = observer()
    with file_mutation_guard(str(ledger_path)) as guard:
        result = module.inspect_ownership(
            guard, str(ledger_path.parent), InventoryBudget()
        )
        assert result.version == 1
        assert result.unresolved == ()
        assert result.closure.snapshots == result.observed == ()
        assert module.OwnershipInventory.from_json(result.to_json()) == result


def test_live_committed_wal_is_not_ignored(ledger_path: Path) -> None:
    module = observer()
    writer = sqlite3.connect(ledger_path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "INSERT INTO file_claim_namespaces VALUES(?,?)",
            (str(ledger_path.parent / "media"), '{"invalid":true}'),
        )
        writer.commit()
        # A WAL-aware read must reject this committed supported malformed row;
        # an immutable/raw-copy shortcut would miss it and report empty success.
        with file_mutation_guard(str(ledger_path)) as guard:
            from file_identity import InvalidIdentity

            with pytest.raises(InvalidIdentity):
                module.inspect_ownership(
                    guard, str(ledger_path.parent), InventoryBudget()
                )
    finally:
        writer.close()


def role(path: Path, *, kind: str = "file", policy: str | None = None) -> RoleSpec:
    return RoleSpec(
        RoleKey(None, "test_observation", kind, None),
        str(path),
        None,
        None,
        "intent",
        policy or ("parent_identity" if kind == "directory" else kind),
        OwnerRef("namespace", (str(path.parent),)),
    )


class MediaProxy:
    """Real syscalls by default; hooks only inject local races/boundary evidence."""

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)


@pytest.fixture
def fs_observer(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = observer()
    assert hasattr(module, "_observe_role"), "readonly filesystem acquisition missing"
    monkeypatch.setattr(
        module, "capture_native", lambda fd: NativeHandle(1, b"simulated-current-role")
    )
    return module


def capture(
    module: ModuleType,
    db_path: Path,
    scope: Path,
    spec: RoleSpec,
    *,
    budget: InventoryBudget = InventoryBudget(),
    declared: frozenset[str] | None = None,
) -> Any:
    before = set(os.listdir("/proc/self/fd"))
    with file_mutation_guard(str(db_path)) as guard:
        result = module._observe_role(
            guard,
            spec,
            str(scope),
            declared or frozenset({spec.path}),
            module._ObservationBudget(budget),
        )
    assert set(os.listdir("/proc/self/fd")) == before
    return result


def test_fs_stable_content_and_constraints_are_observation_not_proof(
    fs_observer: ModuleType, ledger_path: Path, tmp_path: Path
) -> None:
    media = tmp_path / "media"
    media.mkdir()
    path = media / "file.cbz"
    path.write_bytes(b"real-disposable-content" * 100_000)
    spec = role(path)
    before = (path.read_bytes(), path.stat())
    result = capture(fs_observer, ledger_path, media, spec)
    assert result.outcome == "observed"
    constraints = json.loads(result.constraints_json)
    assert constraints["kind"] == "readonly_observation"
    assert constraints["expected"] == "undeclared"
    assert constraints["current"]["inode"] == path.stat().st_ino
    assert constraints["content"] == {
        "size": len(before[0]),
        "sha256": hashlib.sha256(before[0]).hexdigest(),
    }
    assert before[0] == path.read_bytes()
    assert before[1].st_mode == path.stat().st_mode
    assert before[1].st_nlink == path.stat().st_nlink
    with pytest.raises(FrozenInstanceError):
        result.outcome = "absent"
    with pytest.raises(InvalidIdentity):
        replace(result, constraints_json='{"kind":"object_proof"}')


@pytest.mark.parametrize("native_supported", [True, False])
def test_fs_known_absence_and_native_unsupported_are_complete_observations(
    fs_observer: ModuleType,
    ledger_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_supported: bool,
) -> None:
    media = tmp_path / "media"
    media.mkdir()
    path = media / "leaf"
    if native_supported:
        result = capture(fs_observer, ledger_path, media, role(path))
        assert result.outcome == "absent"
        assert json.loads(result.constraints_json)["current"] is None
    else:
        path.write_bytes(b"current bytes, not past proof")

        def unsupported(fd: int) -> NativeHandle:
            raise IdentityUnavailable("simulated unsupported native capture")

        monkeypatch.setattr(fs_observer, "capture_native", unsupported)
        result = capture(fs_observer, ledger_path, media, role(path))
        assert result.outcome == "native_unsupported"
        assert (
            json.loads(result.constraints_json)["content"]["size"]
            == path.stat().st_size
        )
    assert not result.unresolved
    assert result.native is None


def test_fs_missing_ancestor_is_not_fabricated_leaf_absence(
    fs_observer: ModuleType, ledger_path: Path, tmp_path: Path
) -> None:
    media = tmp_path / "media"
    media.mkdir()
    result = capture(fs_observer, ledger_path, media, role(media / "missing" / "leaf"))
    assert result.unresolved and result.outcome != "absent"


@pytest.mark.parametrize("location", ["scope", "ancestor", "leaf"])
def test_fs_symlinks_are_never_followed(
    fs_observer: ModuleType, ledger_path: Path, tmp_path: Path, location: str
) -> None:
    media = tmp_path / "media"
    media.mkdir()
    folder = media / "folder"
    folder.mkdir()
    leaf = folder / "leaf"
    leaf.write_bytes(b"keep")
    target = {"scope": media, "ancestor": folder, "leaf": leaf}[location]
    moved = target.with_name(target.name + "-original")
    target.rename(moved)
    target.symlink_to(moved, target_is_directory=location != "leaf")
    result = capture(fs_observer, ledger_path, media, role(leaf))
    assert result.unresolved and result.outcome == "unsafe_path"


@pytest.mark.parametrize("change", ["leaf", "ancestor", "scope", "fifo"])
def test_fs_held_name_replacement_during_hash_is_not_complete(
    fs_observer: ModuleType,
    ledger_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    media = tmp_path / "media"
    media.mkdir()
    folder = media / "folder"
    folder.mkdir()
    path = folder / "leaf"
    path.write_bytes(b"a" * (1024**2 + 7))

    class Race(MediaProxy):
        changed = False

        def read(self, fd: int, size: int) -> bytes:
            data = os.read(fd, size)
            if not self.changed:
                self.changed = True
                target = {
                    "leaf": path,
                    "fifo": path,
                    "ancestor": folder,
                    "scope": media,
                }[change]
                target.rename(target.with_name(target.name + "-original"))
                if change == "fifo":
                    os.mkfifo(path)
                elif change == "leaf":
                    path.write_bytes(b"replacement")
                else:
                    target.mkdir()
            return data

    monkeypatch.setattr(fs_observer, "_media", Race())
    result = capture(fs_observer, ledger_path, media, role(path))
    assert result.outcome == "changed" and result.unresolved


@pytest.mark.parametrize("bad", ["mode", "hardlink", "uid"])
def test_fs_marker_policy_mismatch_preserves_object(
    fs_observer: ModuleType,
    ledger_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad: str,
) -> None:
    media = tmp_path / "media"
    media.mkdir()
    path = media / "owner.json"
    path.write_bytes(b"marker")
    path.chmod(0o600)
    if bad == "mode":
        path.chmod(0o644)
    elif bad == "hardlink":
        os.link(path, media / "alias")
    else:

        class ForeignUID(MediaProxy):
            def geteuid(self) -> int:
                return os.geteuid() + 1

        monkeypatch.setattr(fs_observer, "_media", ForeignUID())
    before = path.stat()
    result = capture(fs_observer, ledger_path, media, role(path, kind="marker"))
    assert result.outcome == "policy_mismatch" and result.unresolved
    assert path.read_bytes() == b"marker"
    assert (
        path.stat().st_mode == before.st_mode
        and path.stat().st_nlink == before.st_nlink
    )


@pytest.mark.parametrize("boundary", ["nested", "unsupported", "drift"])
def test_fs_mount_boundary_has_no_same_device_fallback(
    fs_observer: ModuleType,
    ledger_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    media = tmp_path / "media"
    media.mkdir()
    folder = media / "nested"
    folder.mkdir()
    leaf = folder / "leaf"
    leaf.write_bytes(b"read")
    real_mount_id = fs_observer._mount_id
    calls: dict[int, int] = {}

    def mount_id(fd: int) -> int:
        inode = os.fstat(fd).st_ino
        calls[inode] = calls.get(inode, 0) + 1
        if boundary == "unsupported":
            raise IdentityUnavailable("simulated unknown boundary")
        value = real_mount_id(fd)
        if inode == folder.stat().st_ino and (boundary == "nested" or calls[inode] > 1):
            return value + 1
        return value

    monkeypatch.setattr(fs_observer, "_mount_id", mount_id)
    result = capture(fs_observer, ledger_path, media, role(leaf))
    assert result.unresolved
    assert result.outcome == (
        "boundary_unknown" if boundary == "unsupported" else "cross_device"
    )


def test_fs_enumeration_detects_unknown_descendants_and_changed_childset(
    fs_observer: ModuleType,
    ledger_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media = tmp_path / "media"
    media.mkdir()
    private = media / "private"
    private.mkdir(mode=0o700)
    nested = private / "unknown"
    nested.mkdir()
    (nested / "leaf").write_bytes(b"unowned")
    spec = role(private, kind="directory", policy="private_directory")
    result = capture(fs_observer, ledger_path, media, spec)
    assert result.outcome == "unknown_children" and result.unresolved
    assert {v["path"] for v in json.loads(result.constraints_json)["children"]} == {
        str(nested),
        str(nested / "leaf"),
    }
    (nested / "leaf").unlink()
    nested.rmdir()

    class EnumerationRace(MediaProxy):
        def scandir(self, fd: int) -> Any:
            iterator = os.scandir(fd)

            class Changed:
                def __enter__(self) -> Any:
                    return iterator.__enter__()

                def __exit__(self, *args: object) -> Any:
                    (private / "late-child").write_bytes(b"must prevent complete")
                    return iterator.__exit__(*args)

            return Changed()

    monkeypatch.setattr(fs_observer, "_media", EnumerationRace())
    changed = capture(fs_observer, ledger_path, media, spec)
    assert changed.outcome == "changed" and changed.unresolved


@pytest.mark.parametrize("limit", ["content", "objects"])
def test_fs_budgets_refuse_before_unbounded_work(
    fs_observer: ModuleType, ledger_path: Path, tmp_path: Path, limit: str
) -> None:
    media = tmp_path / "media"
    media.mkdir()
    leaf = media / "file"
    leaf.write_bytes(b"xx")
    if limit == "content":
        spec = role(leaf)
        budget = InventoryBudget(max_hash_bytes=1)
    else:
        spec = role(media, kind="directory", policy="private_directory")
        media.chmod(0o700)
        budget = InventoryBudget(max_objects=1)
    result = capture(fs_observer, ledger_path, media, spec, budget=budget)
    assert result.outcome == "budget_exceeded" and result.unresolved


def test_fs_guard_is_not_bypassed_for_closed_or_cross_thread_use(
    fs_observer: ModuleType, ledger_path: Path, tmp_path: Path
) -> None:
    spec = role(tmp_path / "missing")
    errors: list[BaseException] = []
    with file_mutation_guard(str(ledger_path)) as guard:

        def worker() -> None:
            try:
                fs_observer._observe_role(
                    guard,
                    spec,
                    str(tmp_path),
                    frozenset(),
                    fs_observer._ObservationBudget(InventoryBudget()),
                )
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], FileMutationLockError)
    with pytest.raises(FileMutationLockError):
        fs_observer._observe_role(
            guard,
            spec,
            str(tmp_path),
            frozenset(),
            fs_observer._ObservationBudget(InventoryBudget()),
        )


def test_fs_real_namespace_producer_roles_keep_exact_legacy_and_distinct_handles(
    fs_observer: ModuleType,
    ledger_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from private_file_claim import ensure_namespace

    media = tmp_path / "media"
    media.mkdir()
    with file_mutation_guard(str(ledger_path)) as guard:
        with ensure_namespace(guard, str(media)):
            pass
    db = sqlite3.connect(ledger_path)
    try:
        closure = collect_references(db, str(media), InventoryBudget())
    finally:
        db.close()
    handles = {
        Path(spec.path).stat().st_ino: NativeHandle(
            1, f"simulated-independent-{i}".encode()
        )
        for i, spec in enumerate(closure.roles)
    }
    monkeypatch.setattr(
        fs_observer, "capture_native", lambda fd: handles[os.fstat(fd).st_ino]
    )
    declared = frozenset(spec.path for spec in closure.roles)
    results = [
        capture(fs_observer, ledger_path, media, spec, declared=declared)
        for spec in closure.roles
    ]
    assert len(results) == 3 and all(r.outcome == "observed" for r in results)
    assert len({r.native for r in results}) == 3
    assert all(r.spec.legacy_json == closure.roles[0].legacy_json for r in results)
    assert all(
        json.loads(r.constraints_json)["expected"] == "unverified_legacy"
        for r in results
    )


def test_fs_directory_childset_change_during_native_capture_is_unresolved(
    fs_observer: ModuleType,
    ledger_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media = tmp_path / "media"
    media.mkdir(mode=0o700)

    def capture_and_change(fd: int) -> NativeHandle:
        (media / "late-child").write_bytes(b"new child after enumeration")
        return NativeHandle(1, b"simulated-racing-directory")

    monkeypatch.setattr(fs_observer, "capture_native", capture_and_change)
    result = capture(
        fs_observer,
        ledger_path,
        media,
        role(media, kind="directory", policy="private_directory"),
    )
    assert result.outcome == "changed" and result.unresolved


def test_fs_fifo_swap_between_stat_and_open_never_opens_a_readable_fifo(
    fs_observer: ModuleType,
    ledger_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media = tmp_path / "media"
    media.mkdir()
    leaf = media / "leaf"
    leaf.write_bytes(b"regular before stat")

    class FIFOBeforeOpen(MediaProxy):
        def open(self, path: str, flags: int, **kwargs: Any) -> int:
            if path == "leaf":
                leaf.rename(media / "original")
                os.mkfifo(leaf)
                assert flags & os.O_PATH
            return os.open(path, flags, **kwargs)

    monkeypatch.setattr(fs_observer, "_media", FIFOBeforeOpen())
    result = capture(fs_observer, ledger_path, media, role(leaf))
    assert result.outcome == "changed" and result.unresolved


def test_fs_content_budget_is_not_consumed_by_json_metadata(
    fs_observer: ModuleType, ledger_path: Path, tmp_path: Path
) -> None:
    media = tmp_path / "media"
    media.mkdir()
    leaf = media / "one-byte"
    leaf.write_bytes(b"a")
    result = capture(
        fs_observer,
        ledger_path,
        media,
        role(leaf),
        budget=InventoryBudget(max_hash_bytes=1),
    )
    assert result.outcome == "observed" and not result.unresolved


def test_wire_real_closure_roundtrip_and_exact_legacy(
    fs_observer: ModuleType, ledger_path: Path, tmp_path: Path
) -> None:
    from private_file_claim import ensure_namespace

    media = tmp_path / "media"
    media.mkdir()
    with file_mutation_guard(str(ledger_path)) as guard:
        with ensure_namespace(guard, str(media)):
            pass
    db = sqlite3.connect(ledger_path)
    try:
        legacy = db.execute(
            "SELECT ownership_json FROM file_claim_namespaces"
        ).fetchone()[0]
        exact = "  " + legacy + "\n"
        db.execute("UPDATE file_claim_namespaces SET ownership_json=?", (exact,))
        db.commit()
        closure = collect_references(db, str(media), InventoryBudget())
    finally:
        db.close()
    declared = frozenset(spec.path for spec in closure.roles)
    observed = tuple(
        capture(fs_observer, ledger_path, media, spec, declared=declared)
        for spec in closure.roles
    )
    inventory = fs_observer.OwnershipInventory.create(
        str(media), closure, observed, (), InventoryBudget()
    )
    encoded = inventory.to_json()
    assert fs_observer.OwnershipInventory.from_json(encoded) == inventory
    assert all(r.spec.legacy_json == exact for r in inventory.observed)
    value = json.loads(encoded)
    digest = value.pop("sha256")
    assert (
        hashlib.sha256(
            json.dumps(
                value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode()
        ).hexdigest()
        == digest
    )
    assert "\n" not in encoded
    with pytest.raises(FrozenInstanceError):
        inventory.version = 2


def empty_wire(module: ModuleType, ledger_path: Path) -> Any:
    db = sqlite3.connect(ledger_path)
    try:
        closure = collect_references(db, str(ledger_path.parent), InventoryBudget())
    finally:
        db.close()
    return module.OwnershipInventory.create(
        str(ledger_path.parent), closure, (), (), InventoryBudget()
    )


@pytest.mark.parametrize(
    "defect",
    [
        "duplicate",
        "nan",
        "infinity",
        "version_bool",
        "version_unknown",
        "extra",
        "digest",
        "budget_bool",
        "closure_extra",
        "unresolved_unknown",
        "depth",
        "collections",
        "size",
        "truncated",
        "parent_alias",
    ],
)
def test_wire_parser_rejects_malformed_tampered_and_oversized_documents(
    fs_observer: ModuleType, ledger_path: Path, defect: str
) -> None:
    inventory = empty_wire(fs_observer, ledger_path)
    value = json.loads(inventory.to_json())
    if defect == "duplicate":
        encoded = inventory.to_json().replace('"version":1', '"version":1,"version":1')
    elif defect in {"nan", "infinity"}:
        encoded = inventory.to_json().replace(
            '"version":1', '"version":' + {"nan": "NaN", "infinity": "Infinity"}[defect]
        )
    elif defect == "depth":
        encoded = "[" * 35 + "0" + "]" * 35
    elif defect == "collections":
        encoded = "[" + ",".join("0" for _ in range(10001)) + "]"
    elif defect == "size":
        encoded = '"' + "x" * (16 * 1024**2) + '"'
    elif defect == "truncated":
        encoded = inventory.to_json()[:-1]
    else:
        if defect == "version_bool":
            value["version"] = True
        elif defect == "version_unknown":
            value["version"] = 2
        elif defect == "extra":
            value["extra"] = None
        elif defect == "digest":
            value["sha256"] = "0" * 64
        elif defect == "budget_bool":
            value["budget"]["max_rows"] = True
        elif defect == "closure_extra":
            value["closure"]["extra"] = []
        elif defect == "unresolved_unknown":
            value["unresolved"] = ["made_up"]
        elif defect == "parent_alias":
            value["parent"] += "/../alias"
        encoded = json.dumps(value)
    with pytest.raises(InvalidIdentity):
        fs_observer.OwnershipInventory.from_json(encoded)


def test_wire_cannot_omit_roles_or_drop_unresolved_failures(
    fs_observer: ModuleType, ledger_path: Path, tmp_path: Path
) -> None:
    from private_file_claim import ensure_namespace

    media = tmp_path / "media"
    media.mkdir()
    with file_mutation_guard(str(ledger_path)) as guard:
        with ensure_namespace(guard, str(media)):
            pass
    db = sqlite3.connect(ledger_path)
    try:
        closure = collect_references(db, str(media), InventoryBudget())
    finally:
        db.close()
    with pytest.raises(InvalidIdentity):
        fs_observer.OwnershipInventory.create(
            str(media), closure, (), (), InventoryBudget()
        )
    incomplete = fs_observer.OwnershipInventory.create(
        str(media), closure, (), ("evidence_budget_exceeded",), InventoryBudget()
    )
    assert incomplete.unresolved == ("evidence_budget_exceeded",)


@pytest.mark.parametrize("text", ["a", "\x7f", "\u00e9", "\U0001f30a", "\\", "\n", '"'])
def test_wire_size_is_exact_before_escaped_string_allocation(
    fs_observer: ModuleType, text: str
) -> None:
    size = len(json.dumps(text, ensure_ascii=True))
    assert fs_observer._wire_size(text, size) == size
    with pytest.raises(InvalidIdentity):
        fs_observer._wire_size(text, size - 1)


@pytest.fixture
def namespace_wire(
    fs_observer: ModuleType, ledger_path: Path, tmp_path: Path
) -> dict[str, Any]:
    from private_file_claim import ensure_namespace

    media = tmp_path / "media"
    media.mkdir()
    with file_mutation_guard(str(ledger_path)) as guard:
        with ensure_namespace(guard, str(media)):
            pass
    db = sqlite3.connect(ledger_path)
    try:
        closure = collect_references(db, str(media), InventoryBudget())
    finally:
        db.close()
    declared = frozenset(spec.path for spec in closure.roles)
    observed = tuple(
        capture(fs_observer, ledger_path, media, spec, declared=declared)
        for spec in closure.roles
    )
    result = fs_observer.OwnershipInventory.create(
        str(media), closure, observed, (), InventoryBudget()
    )
    return dict(json.loads(result.to_json()))


@pytest.mark.parametrize(
    "defect",
    [
        "outcome",
        "native_bool",
        "native_extra",
        "native_absence",
        "constraints_extra",
        "constraints_policy",
        "constraints_mode_bool",
        "directory_mutable",
        "constraints_kind",
        "constraints_expected",
        "spec_extra",
        "role_kind",
        "owner_bool",
        "duplicate_role",
        "raw_bool",
        "raw_extra",
        "role_omitted",
        "constraints_null_present",
        "closure_roles_omitted",
    ],
)
def test_wire_resigned_semantic_tampering_is_rejected(
    fs_observer: ModuleType, namespace_wire: dict[str, Any], defect: str
) -> None:
    value = namespace_wire
    observed = value["observed"][0]
    constraints = json.loads(observed["constraints_json"])
    if defect == "outcome":
        observed["outcome"] = "arbitrary"
    elif defect == "native_bool":
        observed["native"]["handle_type"] = True
    elif defect == "native_extra":
        observed["native"]["extra"] = None
    elif defect == "native_absence":
        observed["outcome"] = "absent"
    elif defect == "constraints_extra":
        constraints["extra"] = None
    elif defect == "constraints_policy":
        constraints["policy"] = ["satisfied"]
    elif defect == "constraints_mode_bool":
        constraints["current"]["mode"] = True
    elif defect == "directory_mutable":
        constraints["current"]["nlink"] = 2
    elif defect == "constraints_kind":
        constraints["kind"] = "object_proof"
    elif defect == "constraints_expected":
        constraints["expected"] = "undeclared"
    elif defect == "spec_extra":
        observed["spec"]["extra"] = None
    elif defect == "role_kind":
        observed["spec"]["role"]["kind"] = "unknown"
    elif defect == "owner_bool":
        observed["spec"]["owner"]["key"] = [True]
    elif defect == "duplicate_role":
        value["observed"].append(observed.copy())
    elif defect == "raw_bool":
        value["closure"]["raw_rows"][0]["values"][0][1] = True
    elif defect == "raw_extra":
        value["closure"]["raw_rows"][0]["extra"] = None
    elif defect == "role_omitted":
        value["observed"].pop()
    elif defect == "constraints_null_present":
        constraints["current"] = None
    elif defect == "closure_roles_omitted":
        value["closure"]["roles"] = []
        value["observed"] = []
    observed["constraints_json"] = json.dumps(
        constraints, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    value.pop("sha256")
    value["sha256"] = hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()
    with pytest.raises(InvalidIdentity):
        fs_observer.OwnershipInventory.from_json(json.dumps(value))


def test_fs_hash_chunks_bounded_and_read_failure_closes_fds(
    fs_observer: ModuleType,
    ledger_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media = tmp_path / "media"
    media.mkdir()
    leaf = media / "leaf"
    leaf.write_bytes(b"x" * (1024**2 + 3))
    lengths: list[int] = []

    class Reads(MediaProxy):
        def read(self, fd: int, size: int) -> bytes:
            lengths.append(size)
            return os.read(fd, size)

    monkeypatch.setattr(fs_observer, "_media", Reads())
    assert capture(fs_observer, ledger_path, media, role(leaf)).outcome == "observed"
    assert lengths == [1024**2, 3]

    class Unreadable(MediaProxy):
        def read(self, fd: int, size: int) -> bytes:
            raise PermissionError("simulated unreadable content")

    monkeypatch.setattr(fs_observer, "_media", Unreadable())
    assert capture(fs_observer, ledger_path, media, role(leaf)).outcome == "unreadable"


def test_wire_inventory_content_cannot_exceed_declared_operator_budget(
    fs_observer: ModuleType, namespace_wire: dict[str, Any]
) -> None:
    inventory = fs_observer.OwnershipInventory.from_json(json.dumps(namespace_wire))
    with pytest.raises(InvalidIdentity):
        fs_observer.OwnershipInventory.create(
            inventory.parent,
            inventory.closure,
            inventory.observed,
            (),
            InventoryBudget(max_hash_bytes=1),
        )


@pytest.mark.parametrize("argument", ["closure", "budget", "observed", "unresolved"])
def test_wire_factory_rejects_wrong_public_input_types(
    fs_observer: ModuleType, ledger_path: Path, argument: str
) -> None:
    inventory = empty_wire(fs_observer, ledger_path)
    values = dict(
        parent=inventory.parent,
        closure=inventory.closure,
        observed=(),
        unresolved=(),
        budget=InventoryBudget(),
    )
    values[argument] = None
    with pytest.raises(InvalidIdentity):
        fs_observer.OwnershipInventory.create(**values)


def test_fs_missing_leaf_does_not_survive_ancestor_name_replacement(
    fs_observer: ModuleType,
    ledger_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media = tmp_path / "media"
    media.mkdir()
    folder = media / "ancestor"
    folder.mkdir()
    anchor = folder / "parent"
    anchor.mkdir()
    missing = 0

    class MissingRace(MediaProxy):
        def stat(self, path: str, **kwargs: Any) -> os.stat_result:
            nonlocal missing
            try:
                return os.stat(path, **kwargs)
            except FileNotFoundError:
                if path == "leaf":
                    missing += 1
                    if missing == 2:
                        folder.rename(media / "old-parent")
                        folder.mkdir()
                        anchor.mkdir()
                        (anchor / "leaf").write_bytes(b"replacement is not absent")
                raise

    monkeypatch.setattr(fs_observer, "_media", MissingRace())
    result = capture(fs_observer, ledger_path, media, role(anchor / "leaf"))
    assert result.outcome == "changed" and result.unresolved


@pytest.mark.parametrize("unknown_private", [False, True])
def test_fs_real_namespace_public_context_is_not_private_descendant_ownership(
    fs_observer: ModuleType,
    ledger_path: Path,
    tmp_path: Path,
    unknown_private: bool,
) -> None:
    from private_file_claim import ensure_namespace
    from test_file_identity_inventory import all_rows

    media = tmp_path / "series"
    media.mkdir()
    public = media / "unrelated-public.cbz"
    public.write_bytes(b"unrelated archive is not namespace-owned")
    unrelated = media / "unrelated-directory"
    unrelated.mkdir()
    (unrelated / "another-archive.cbz").write_bytes(b"not a private subtree")
    with file_mutation_guard(str(ledger_path)) as guard:
        with ensure_namespace(guard, str(media)):
            pass
    private = media / ".mangarr-claims"
    if unknown_private:
        (private / "undeclared-private-child").write_bytes(
            b"unregistered, retain unresolved"
        )
    db = sqlite3.connect(ledger_path)
    try:
        closure = collect_references(db, str(media), InventoryBudget())
        rows_before = all_rows(db)
    finally:
        db.close()
    before = {
        str(p.relative_to(media)): (
            p.stat().st_mode,
            p.stat().st_uid,
            p.stat().st_nlink,
            p.read_bytes() if p.is_file() else None,
        )
        for p in [media, *media.rglob("*")]
    }
    declared = frozenset(spec.path for spec in closure.roles)
    results = {
        spec.role.purpose: capture(
            fs_observer, ledger_path, media, spec, declared=declared
        )
        for spec in closure.roles
    }
    parent = results["namespace_parent"]
    assert parent.spec.policy == "exclusive_parent"
    assert parent.outcome == "observed" and not parent.unresolved
    assert json.loads(parent.constraints_json)["children"] == []
    root = results["namespace_root"]
    assert root.outcome == ("unknown_children" if unknown_private else "observed")
    assert root.unresolved is unknown_private
    assert json.loads(root.constraints_json)["children"] == (
        [
            {
                "path": str(private / "undeclared-private-child"),
                "outcome": "unknown_children",
            }
        ]
        if unknown_private
        else []
    )
    assert before == {
        str(p.relative_to(media)): (
            p.stat().st_mode,
            p.stat().st_uid,
            p.stat().st_nlink,
            p.read_bytes() if p.is_file() else None,
        )
        for p in [media, *media.rglob("*")]
    }
    db = sqlite3.connect(ledger_path)
    try:
        assert all_rows(db) == rows_before
    finally:
        db.close()


def test_fs_unknown_noncanonical_child_is_unresolved_not_a_parser_crash(
    fs_observer: ModuleType, ledger_path: Path, tmp_path: Path
) -> None:
    media = tmp_path / "private"
    media.mkdir(mode=0o700)
    (media / "unsupported\\name").write_bytes(b"unknown child, retain")
    result = capture(
        fs_observer,
        ledger_path,
        media,
        role(media, kind="directory", policy="private_directory"),
    )
    assert result.outcome == "unsafe_path" and result.unresolved


def test_fs_child_evidence_is_not_charged_twice_again_in_role_envelope(
    fs_observer: ModuleType, ledger_path: Path, tmp_path: Path
) -> None:
    media = tmp_path / "private"
    media.mkdir(mode=0o700)
    (media / "unknown").write_bytes(b"unknown")
    spec = role(media, kind="directory", policy="private_directory")
    first = capture(fs_observer, ledger_path, media, spec)
    size = len(json.dumps(first.constraints_json, ensure_ascii=True).encode())
    counter = fs_observer._ObservationBudget(
        InventoryBudget(), metadata=16 * 1024**2 - 4096 - size
    )
    with file_mutation_guard(str(ledger_path)) as guard:
        result = fs_observer._observe_role(
            guard, spec, str(media), frozenset({spec.path}), counter
        )
    assert result.outcome == "unknown_children"
    assert counter.metadata == 16 * 1024**2 - 4096


@dataclass(frozen=True)
class ProducerMedia:
    db_path: Path
    scope: Path
    library: Path
    source: Path
    incoming: Path


@pytest.fixture
def producer_media(
    ledger_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> ProducerMedia:
    import import_pipeline

    scope = tmp_path / "media"
    library = scope / "library" / "Matrix"
    incoming = scope / "incoming"
    library.mkdir(parents=True)
    incoming.mkdir()
    source = library / "Matrix v01.cbz"
    write_archive(source, b"original library bytes")
    monkeypatch.setattr(import_pipeline, "PACK_STAGING_ROOT", str(scope / "packs"))
    db = sqlite3.connect(ledger_path)
    try:
        root_id = db.execute(
            "INSERT INTO root_folders(path,label,is_default) VALUES(?,'Matrix',1)",
            (str(library.parent),),
        ).lastrowid
        db.execute(
            "INSERT INTO series(id,title,search_pattern,folder_name,root_folder_id) VALUES(7,'Matrix','Matrix','Matrix',?)",
            (root_id,),
        )
        db.execute(
            "INSERT INTO volumes(id,series_id,volume_num,status,import_path) VALUES(1,7,1,'downloaded',?)",
            (str(source),),
        )
        db.commit()
    finally:
        db.close()
    with file_mutation_guard(str(ledger_path)):
        pass
    return ProducerMedia(ledger_path, scope, library, source, incoming)


@pytest.mark.parametrize("bootstrap_root", [False, True], ids=["fresh", "bootstrap"])
def test_producer_media_preserves_existing_root_and_binds_matrix_library(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bootstrap_root: bool,
) -> None:
    import shared

    bootstrap = tmp_path / "bootstrap-library"
    # Initialize the real ledger before requesting the real producer fixture.
    with monkeypatch.context() as config:
        config.setitem(
            cast(dict[str, object], shared.CONFIG),
            "save_path",
            str(bootstrap) if bootstrap_root else "",
        )
        path = cast(Path, request.getfixturevalue("ledger_path"))
    db = sqlite3.connect(path)
    try:
        before = db.execute("SELECT * FROM root_folders ORDER BY id").fetchall()
        if bootstrap_root:
            assert db.execute(
                "SELECT id,path,label,is_default FROM root_folders"
            ).fetchall() == [(1, str(bootstrap), "Manga", 1)]
        else:
            assert before == []
    finally:
        db.close()

    env = cast(ProducerMedia, request.getfixturevalue("producer_media"))
    db = sqlite3.connect(path)
    try:
        roots = db.execute("SELECT * FROM root_folders ORDER BY id").fetchall()
        assert len(roots) == len(before) + 1
        assert roots[: len(before)] == before
        assert db.execute(
            """SELECT s.id,s.title,s.folder_name,r.path,r.label,v.import_path
            FROM series s JOIN root_folders r ON r.id=s.root_folder_id
            JOIN volumes v ON v.series_id=s.id WHERE s.id=7 AND v.id=1"""
        ).fetchall() == [
            (7, "Matrix", "Matrix", str(env.library.parent), "Matrix", str(env.source))
        ]
    finally:
        db.close()
    assert env.db_path == path
    assert env.library == tmp_path / "media" / "library" / "Matrix"
    with zipfile.ZipFile(env.source) as archive:
        assert archive.read("001.jpg") == b"original library bytes"


def write_archive(path: Path, payload: bytes) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("001.jpg", payload)


def producer_queue(env: ProducerMedia) -> int:
    import import_queue

    source = env.incoming / "Matrix v02.cbz"
    write_archive(source, b"queued source")
    db = sqlite3.connect(env.db_path)
    db.row_factory = sqlite3.Row
    try:
        queue_id, _ = import_queue._queue_import(
            db,
            7,
            "matrix-queue",
            "Matrix v02",
            "magnet:matrix-queue",
            2,
            str(env.incoming),
            download_client_id=1,
            protocol="torrent",
            respect_grab_claims=False,
        )
        assert queue_id is not None
        db.commit()
        return queue_id
    finally:
        db.close()


def producer_rescan(env: ProducerMedia, state: str) -> int:
    import private_file_claim as claims
    import rescan
    import rescan_file_recovery as recovery

    db = sqlite3.connect(env.db_path)
    db.row_factory = sqlite3.Row
    try:
        volume = dict(db.execute(rescan._ENRICHMENT_VOLUME_SELECT, (1,)).fetchone())
    finally:
        db.close()
    before = env.source.stat()
    target = rescan._EnrichmentTarget(
        volume,
        1,
        str(env.source),
        rescan.FileFingerprint(
            before.st_size, before.st_mtime_ns, before.st_dev, before.st_ino
        ),
    )
    context = rescan._current_enrichment_context(target)
    assert context is not None
    operation = recovery._reserve(
        target,
        context,
        recovery.fingerprint_path(str(env.source)),
        str(env.library / "converted.cbz"),
    )
    assert operation is not None
    with file_mutation_guard(str(env.db_path)) as guard:
        if state in {"rollback", "rolled_back"}:
            recovery._store(operation, state="rollback")
            if state == "rolled_back":
                assert recovery._settle(operation, guard) == "rolled_back"
        elif state != "prepared":
            recovery._allocate(operation, guard, "stage")
            stage = recovery._record(operation, "stage")
            assert stage is not None
            write_archive(Path(stage.carrier_path) / "artifact", b"prepared stage")
            operation.fingerprints["stage"] = asdict(
                recovery.fingerprint_path(stage.carrier_path + "/artifact")
            )
            recovery._store(operation)
            recovery._capture_source(operation, guard)
            recovery._allocate(operation, guard, "publication")
            with recovery._open(operation, guard, "publication") as carrier:
                write_archive(Path(carrier.artifact_path), b"prepared stage")
                fingerprint = claims.fingerprint_regular(carrier)
                operation.fingerprints["publication"] = asdict(fingerprint)
                recovery._phase(operation, "publication", carrier, "restoring")
                receipt = claims.link_private_regular(
                    guard,
                    carrier,
                    carrier.namespace.parent_fd,
                    "converted.cbz",
                    fingerprint,
                )
                operation.row["publication_receipt_json"] = json.dumps(
                    asdict(receipt), sort_keys=True, separators=(",", ":")
                )
                recovery._store(operation, state="published")
            if state in {"db_committed", "completed"}:
                assert recovery._commit(operation, guard)
            if state == "completed":
                assert recovery._settle(operation, guard) == "completed"
    assert operation.row["state"] == state
    return int(operation.row["id"])


def producer_deletion(env: ProducerMedia, phase: str) -> int:
    import private_file_claim as claims
    import volume_file_deletion as deletion

    if phase == "missing":
        env.source.unlink()
    reservation = deletion.reserve_volume_file_deletion(7, 1)
    assert reservation.status == "reserved", reservation
    journal_id = reservation.journal_id
    assert journal_id is not None
    if phase in {"active", "missing"}:
        return journal_id
    journal = deletion._load_journal(journal_id)
    assert journal is not None and journal.fingerprint is not None
    with file_mutation_guard(str(env.db_path)) as guard:
        with claims.ensure_namespace(guard, str(env.library)) as namespace:
            with claims.allocate_carrier(
                namespace,
                deletion._private_binding(journal),
                str(env.source),
                deletion._full_fingerprint(journal.fingerprint),
            ) as carrier:
                record = replace(carrier.record, phase="claiming")
                journal = deletion._store_carrier(journal, guard, record)
                carrier.record = record
                if phase != "claiming":
                    claims.claim_into_empty(
                        guard, carrier, namespace.parent_fd, env.source.name
                    )
                    record = replace(record, phase="claimed")
                    journal = deletion._store_carrier(journal, guard, record)
                    carrier.record = record
                if phase in {"discarded", "completed"}:
                    record = replace(record, phase="discarding")
                    journal = deletion._store_carrier(journal, guard, record)
                    carrier.record = record
                    assert journal.fingerprint is not None
                    claims.discard_private_regular(
                        guard, carrier, deletion._full_fingerprint(journal.fingerprint)
                    )
                    record = replace(record, phase="discarded")
                    journal = deletion._store_carrier(journal, guard, record)
                    carrier.record = record
        if phase == "completed":
            assert deletion._replay_private_claim(journal, guard) == "completed"
    return journal_id


def producer_publication(env: ProducerMedia, queue_id: int, state: str) -> int:
    import import_publication as publication
    from import_plan import _FilePlan, _ImportPlan

    owner = "a" * 32
    db = sqlite3.connect(env.db_path)
    db.row_factory = sqlite3.Row
    try:
        db.execute(
            "UPDATE import_queue SET status='importing',lease_owner=?,lease_expires_at=datetime('now','+1 hour') WHERE id=?",
            (owner, queue_id),
        )
        db.commit()
        queue = dict(
            db.execute("SELECT * FROM import_queue WHERE id=?", (queue_id,)).fetchone()
        )
        file = dict(
            db.execute(
                "SELECT * FROM import_queue_files WHERE queue_id=?", (queue_id,)
            ).fetchone()
        )
        series = dict(db.execute("SELECT * FROM series WHERE id=7").fetchone())
    finally:
        db.close()
    final = env.library / "Matrix v02.cbz"
    file_plan = _FilePlan(
        file["id"],
        file["src_path"],
        file["filename"],
        str(final),
        "volume",
        "cbz",
        2,
        None,
        None,
        None,
        None,
        None,
        0,
        None,
        False,
        False,
        False,
        "ready",
        "",
    )
    plan = _ImportPlan(
        queue, series, [], str(env.library), "copy", None, [file_plan], 7
    )
    stage_dir, _ = publication.initialize_publication_filesystem(plan, owner)
    db = sqlite3.connect(env.db_path)
    try:
        publication_id = int(
            db.execute(
                "SELECT id FROM import_publications WHERE queue_id=?", (queue_id,)
            ).fetchone()[0]
        )
        if state != "staging":
            stage_path = Path(stage_dir) / final.name
            # Fixture crash checkpoint: real producer created/bound the carrier;
            # real regular fingerprints are recorded for this state projection.
            write_archive(stage_path, b"prepared publication")
            fingerprint = publication._regular_fingerprint(
                str(stage_path), include_hash=True
            )
            if state in {
                "published",
                "db_committed",
                "cleaning",
                "finalized",
                "deleted",
            }:
                final.write_bytes(stage_path.read_bytes())
            db.execute(
                "UPDATE import_publication_files SET stage_state='staged',staged_dev=?,staged_inode=?,staged_size=?,staged_mtime_ns=?,staged_sha256=? WHERE publication_id=?",
                (
                    fingerprint.dev,
                    fingerprint.inode,
                    fingerprint.size,
                    fingerprint.mtime_ns,
                    fingerprint.sha256,
                    publication_id,
                ),
            )
            db.execute(
                "UPDATE import_publications SET state=? WHERE id=?",
                (state, publication_id),
            )
            if state in {
                "published",
                "db_committed",
                "cleaning",
                "finalized",
                "deleted",
            }:
                db.execute(
                    "UPDATE import_publication_files SET publish_state='published' WHERE publication_id=?",
                    (publication_id,),
                )
            if state in {"finalized", "deleted"}:
                db.execute(
                    "UPDATE import_queue SET status='imported',lease_owner=NULL,lease_expires_at=NULL WHERE id=?",
                    (queue_id,),
                )
                db.execute(
                    "UPDATE import_queue_files SET status='imported' WHERE queue_id=?",
                    (queue_id,),
                )
        db.commit()
    finally:
        db.close()
    return publication_id


def producer_pack(
    env: ProducerMedia, phase: str, *, tombstone: bool = False
) -> tuple[str, int | None]:
    import import_pack_cleanup as packs
    import private_file_claim as claims
    from private_pack_claim import (
        PackDirectoryClaim,
        directory_proof,
        inventory_tree,
        open_directory,
    )

    download_id = "matrix-generated"
    db = sqlite3.connect(env.db_path)
    db.row_factory = sqlite3.Row
    try:
        owner = packs.reserve_pack_queue_creation(
            db, download_id, download_client_id=1, protocol="torrent"
        )
        assert owner is not None
    finally:
        db.close()
    reservation = packs._pack_reservation_for_owner(
        download_id, owner, download_client_id=1, protocol="torrent"
    )
    if phase == "intent":
        return reservation.download_identity_key, None
    with file_mutation_guard(str(env.db_path)) as guard:
        fd, proof = packs._prepare_pack_private_directory(
            guard, download_id, owner, download_client_id=1, protocol="torrent"
        )
        try:
            if phase == "building":
                return reservation.download_identity_key, None
            write_archive(Path(proof.path) / "Matrix v02.cbz", b"generated pack source")
            inventory = inventory_tree(fd)
        finally:
            os.close(fd)
        reservation = packs._read_reservation(reservation.download_identity_key)
        assert reservation is not None
        ownership = packs._ownership(reservation)
        assert ownership is not None
        if tombstone:
            # Set up the actual native-placement checkpoint with an ordinary
            # local rename, not a renameat2/native-capability trial.
            os.rename(proof.path, reservation.pack_path)
            proof = replace(proof, path=reservation.pack_path)
            placement = ownership.placement_carrier
            assert placement is not None
            with claims.ensure_namespace(guard, str(env.scope / "packs")) as namespace:
                claims.gc_discarded_carrier(
                    namespace, placement.binding, replace(placement, phase="discarded")
                )
            ownership = replace(ownership, placement_carrier=None)
        db = sqlite3.connect(env.db_path)
        db.row_factory = sqlite3.Row
        try:
            assert packs.begin_pack_queue_attachment(
                db,
                download_id,
                owner,
                download_client_id=1,
                protocol="torrent",
                _guard=guard,
            )
        finally:
            db.close()
        reservation = packs._read_reservation(reservation.download_identity_key)
        assert reservation is not None
        ownership = replace(
            ownership,
            phase="ready",
            private_directory=None,
            canonical_directory=proof,
            inventory=inventory,
        )
        reservation = packs._save_ownership(guard, reservation, ownership)
        if phase == "ready":
            return reservation.download_identity_key, None
        source = Path(proof.path) / "Matrix v02.cbz"
        queue_id = packs._commit_generated_pack_queue(
            guard,
            reservation,
            (
                7,
                download_id,
                1,
                "torrent",
                "Matrix v02",
                "magnet:matrix-generated",
                2,
                proof.path,
            ),
            [
                (
                    source.name,
                    str(source),
                    str(env.library / source.name),
                    2,
                    None,
                    None,
                    None,
                    None,
                    None,
                    0,
                    "volume",
                    None,
                    "cbz",
                    "pending",
                )
            ],
            respect_grab_claims=False,
        )
        if phase == "queued":
            return reservation.download_identity_key, queue_id
        packs._finish_generated_pack_queue(guard, reservation, queue_id)
        reservation = packs._read_reservation(reservation.download_identity_key)
        assert reservation is not None
        ownership = packs._ownership(reservation)
        assert ownership is not None and ownership.phase == "attached"
        if phase == "retained":
            packs._save_ownership(
                guard,
                reservation,
                replace(
                    ownership, phase="retained", retained_reason="fixture-retained"
                ),
            )
        if tombstone:
            db = sqlite3.connect(env.db_path)
            try:
                db.execute(
                    "UPDATE import_queue SET status='failed' WHERE id=?", (queue_id,)
                )
                db.execute(
                    "UPDATE import_queue_files SET status='failed' WHERE queue_id=?",
                    (queue_id,),
                )
                db.commit()
            finally:
                db.close()
            with open_directory(proof.path) as directory:
                proof = directory_proof(proof.path, directory)
            with claims.ensure_namespace(guard, str(env.scope / "packs")) as namespace:
                binding = claims.ClaimBinding(
                    "pack", ownership.artifact_owner_token, None, "pack_cleanup"
                )
                with claims.allocate_carrier(namespace, binding, proof.path) as carrier:
                    record = replace(carrier.record, phase="claiming")
                    claim = PackDirectoryClaim(
                        record, proof, inventory, ownership.artifact_owner_token
                    )
                    ownership = replace(ownership, phase="cleaning", claims=(claim,))
                    reservation = packs._save_ownership(
                        guard,
                        reservation,
                        ownership,
                        tombstone_path=carrier.artifact_path,
                    )
                    carrier.record = record
                    claims.claim_into_empty(
                        guard, carrier, namespace.parent_fd, Path(proof.path).name
                    )
                    claim = replace(claim, carrier=replace(record, phase="claimed"))
                    ownership = replace(ownership, claims=(claim,))
                    reservation = packs._save_ownership(guard, reservation, ownership)
                    carrier.record = claim.carrier
            assert reservation.tombstone_path is not None
            assert packs._record_owned_tombstone(
                reservation, reservation.tombstone_path
            )
        return reservation.download_identity_key, queue_id


def captured_fixture(env: ProducerMedia) -> tuple[ReferenceClosure, object]:
    from test_file_identity_inventory import all_rows

    db = sqlite3.connect(env.db_path)
    try:
        closure = collect_references(db, str(env.scope), InventoryBudget())
        before = all_rows(db)
        assert not db.in_transaction
    finally:
        db.close()
    return closure, before


def media_snapshot(scope: Path) -> dict[str, tuple[object, ...]]:
    return {
        str(path.relative_to(scope)): (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_uid,
            info.st_gid,
            info.st_nlink,
            info.st_mtime_ns,
            info.st_ctime_ns,
            path.read_bytes() if path.is_file() else None,
        )
        for path in [scope, *sorted(scope.rglob("*"))]
        for info in [path.lstat()]
    }


def control_snapshot(db_path: Path) -> dict[str, object]:
    # Dedicated no-SQL process: raw DB reads never release this process's locks.
    program = "import json,pathlib,sys; p=pathlib.Path(sys.argv[1]); names=[p.name,p.name+'-wal',p.name+'-shm','.'+p.name+'.file-mutation.lock']; print(json.dumps({n:[s.st_mode,s.st_uid,s.st_gid,s.st_nlink,f.read_bytes().hex()] for n in names if (f:=p.parent/n).exists() for s in [f.lstat()]}))"
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", program, str(db_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return dict(json.loads(result.stdout))


def observe_fixture(
    module: ModuleType,
    env: ProducerMedia,
    closure: ReferenceClosure,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, ...]:
    identities = {
        (info.st_dev, info.st_ino): NativeHandle(
            1, f"simulated-matrix-{index}".encode()
        )
        for index, path in enumerate(
            sorted({r.path for r in closure.roles if Path(r.path).exists()})
        )
        for info in [Path(path).stat()]
    }
    monkeypatch.setattr(
        module,
        "capture_native",
        lambda fd: identities[(os.fstat(fd).st_dev, os.fstat(fd).st_ino)],
    )
    declared = frozenset(r.path for r in closure.roles)
    counter = module._ObservationBudget(InventoryBudget())
    descriptors = set(os.listdir("/proc/self/fd"))
    before = media_snapshot(env.scope)
    controls = control_snapshot(env.db_path)
    with file_mutation_guard(str(env.db_path)) as guard:
        observed = tuple(
            module._observe_role(guard, role, str(env.scope), declared, counter)
            for role in closure.roles
        )
    assert set(os.listdir("/proc/self/fd")) == descriptors
    assert media_snapshot(env.scope) == before
    assert control_snapshot(env.db_path) == controls
    return observed


MATRIX = (
    [("namespace", phase) for phase in ("ready", "marker_absent")]
    + [
        ("rescan", phase)
        for phase in (
            "prepared",
            "published",
            "db_committed",
            "rollback",
            "rolled_back",
            "completed",
        )
    ]
    + [
        ("deletion", phase)
        for phase in (
            "active",
            "missing",
            "claiming",
            "claimed",
            "discarded",
            "completed",
        )
    ]
    + [
        ("publication", phase)
        for phase in (
            "staging",
            "prepared",
            "publishing",
            "published",
            "db_committed",
            "cleaning",
            "finalized",
            "deleted",
        )
    ]
    + [("queue_source", phase) for phase in ("pending", "imported", "failed")]
    + [
        ("pack_reservation", phase)
        for phase in ("intent", "building", "ready", "queued", "attached", "retained")
    ]
    + [("pack_tombstone", "claimed")]
)


@pytest.mark.parametrize(("domain", "phase"), MATRIX)
def test_fs_all_seven_producer_domain_state_matrix(
    fs_observer: ModuleType,
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    domain: str,
    phase: str,
) -> None:
    import private_file_claim as claims
    from test_file_identity_inventory import all_rows

    env = producer_media
    if domain == "namespace":
        with file_mutation_guard(str(env.db_path)) as guard:
            with claims.ensure_namespace(guard, str(env.library)):
                pass
        if phase == "marker_absent":
            (env.library / ".mangarr-claims" / "owner.json").unlink()
    elif domain == "rescan":
        producer_rescan(env, phase)
    elif domain == "deletion":
        producer_deletion(env, phase)
    elif domain in {"pack_reservation", "pack_tombstone"}:
        producer_pack(env, phase, tombstone=domain == "pack_tombstone")
    else:
        queue_id = producer_queue(env)
        if domain == "publication":
            producer_publication(env, queue_id, phase)
        else:
            db = sqlite3.connect(env.db_path)
            try:
                db.execute(
                    "UPDATE import_queue SET status=? WHERE id=?", (phase, queue_id)
                )
                db.commit()
            finally:
                db.close()
    closure, before_rows = captured_fixture(env)
    assert domain in {s.owner.domain for s in closure.snapshots}
    observations = observe_fixture(fs_observer, env, closure, monkeypatch)
    assert tuple(r.spec for r in observations) == closure.roles
    assert all(
        r.outcome in {"observed", "absent", "unreadable"} for r in observations
    ), [(r.spec.role.purpose, r.outcome) for r in observations]
    for record in observations:
        path = Path(record.spec.path)
        fields = json.loads(record.constraints_json)
        if path.exists():
            assert record.outcome == "observed"
            assert fields["current"]["dev"] == path.stat().st_dev
            assert fields["current"]["inode"] == path.stat().st_ino
        elif path.parent.exists():
            assert record.outcome == "absent" and not record.unresolved
        else:
            assert record.outcome == "unreadable" and record.unresolved
    unresolved = tuple(
        sorted({"role_" + r.outcome for r in observations if r.unresolved})
    )
    report = fs_observer.OwnershipInventory.create(
        str(env.scope), closure, observations, unresolved, InventoryBudget()
    )
    assert fs_observer.OwnershipInventory.from_json(report.to_json()) == report
    db = sqlite3.connect(env.db_path)
    try:
        assert all_rows(db) == before_rows
    finally:
        db.close()


def test_fs_same_path_different_owners_survive_wire_without_global_deduplication(
    fs_observer: ModuleType,
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = producer_media
    queue_id = producer_queue(env)
    producer_publication(env, queue_id, "prepared")
    closure, _ = captured_fixture(env)
    observed = observe_fixture(fs_observer, env, closure, monkeypatch)
    by_path: dict[str, list[Any]] = {}
    for record in observed:
        by_path.setdefault(record.spec.path, []).append(record)
    same = next(
        records
        for records in by_path.values()
        if {r.spec.owner.domain for r in records} >= {"publication", "queue_source"}
        and records[0].spec.role.kind == "file"
        and Path(records[0].spec.path).is_file()
    )
    assert len({r.spec.owner for r in same}) >= 2
    assert all(r.outcome == "observed" for r in same)
    assert len({r.native for r in same}) == 1
    report = fs_observer.OwnershipInventory.create(
        str(env.scope), closure, observed, (), InventoryBudget()
    )
    decoded = fs_observer.OwnershipInventory.from_json(report.to_json())
    assert tuple((r.spec.owner, r.spec.role) for r in decoded.observed) == tuple(
        (r.spec.owner, r.spec.role) for r in observed
    )


@pytest.mark.parametrize("terminal", ["finalized", "deleted"])
def test_fs_terminal_frozen_origin_keeps_old_and_replacement_generations_distinct(
    fs_observer: ModuleType,
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    terminal: str,
) -> None:
    import import_pack_cleanup as packs
    import private_file_claim as claims
    from private_pack_claim import PackOwnership, directory_proof, inventory_tree

    env = producer_media
    key, queue_id = producer_pack(env, "attached")
    assert queue_id is not None
    producer_publication(env, queue_id, terminal)
    old_reservation = packs._read_reservation(key)
    assert old_reservation is not None
    old = packs._ownership(old_reservation)
    assert old is not None and old.canonical_directory is not None
    old_path = Path(old.canonical_directory.path) / "Matrix v02.cbz"
    old_bytes = old_path.read_bytes()
    with file_mutation_guard(str(env.db_path)) as guard:
        new_owner = "c" * 32
        origin = old_reservation.pack_path + ".owner-" + new_owner
        with claims.ensure_namespace(guard, str(env.scope / "packs")) as namespace:
            with claims.allocate_carrier(
                namespace,
                claims.ClaimBinding("pack", new_owner, None, "pack_cleanup"),
                origin,
            ) as carrier:
                directory = Path(carrier.artifact_path)
                directory.mkdir(mode=0o700)
                packs._write_pack_owner_marker(
                    str(directory),
                    packs._pack_identity(
                        old_reservation.download_id,
                        download_client_id=1,
                        protocol="torrent",
                    ),
                    new_owner,
                )
                write_archive(directory / "Matrix v02.cbz", b"replacement generation")
                fd = os.open(
                    directory,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                )
                try:
                    proof = directory_proof(str(directory), fd)
                    inventory = inventory_tree(fd)
                finally:
                    os.close(fd)
                replacement = PackOwnership(
                    key,
                    new_owner,
                    phase="retained",
                    canonical_directory=proof,
                    inventory=inventory,
                    retained_reason="later-generation",
                    version=2,
                    placement_carrier=replace(carrier.record, phase="claimed"),
                )
                packs._save_ownership(guard, old_reservation, replacement)
    closure, _ = captured_fixture(env)
    publication = next(s for s in closure.snapshots if s.owner.domain == "publication")
    frozen = json.loads(publication.rows_json)["root"]["row"]["queue_snapshot_json"]
    frozen_origins = json.loads(frozen)["_pack_source_origins"]["files"]
    frozen_text = next(iter(frozen_origins.values()))["ownership_json"]
    assert (
        PackOwnership.from_json(frozen_text).artifact_owner_token
        == old.artifact_owner_token
    )
    assert replacement.artifact_owner_token != old.artifact_owner_token
    observed = observe_fixture(fs_observer, env, closure, monkeypatch)
    old_files = [r for r in observed if r.spec.path == str(old_path)]
    new_files = [
        r for r in observed if r.spec.path == str(directory / "Matrix v02.cbz")
    ]
    assert old_files and new_files
    assert all(r.outcome == "observed" for r in [*old_files, *new_files])
    assert {r.spec.owner.domain for r in old_files} >= {"publication", "queue_source"}
    assert {r.spec.owner.domain for r in new_files} == {"pack_reservation"}
    assert {r.native for r in old_files}.isdisjoint({r.native for r in new_files})
    assert all(
        json.loads(r.constraints_json)["expected"] == "unverified_legacy"
        for r in new_files
    )
    assert old_path.read_bytes() == old_bytes
    assert (
        next(iter(json.loads(frozen)["_pack_source_origins"]["files"].values()))[
            "ownership_json"
        ]
        == frozen_text
    )
    assert not any(r.unresolved for r in observed)
    report = fs_observer.OwnershipInventory.create(
        str(env.scope), closure, observed, (), InventoryBudget()
    )
    assert fs_observer.OwnershipInventory.from_json(report.to_json()) == report


def test_fs_scanned_sql_metadata_budget_stops_before_materializing_keyset_page(
    ledger_path: Path,
) -> None:
    """A real64-row page must not retain64 large SQL payloads before charging."""
    db = sqlite3.connect(ledger_path)
    try:
        db.executemany(
            "INSERT INTO series(id,title,search_pattern) VALUES(?,?,'')",
            ((i, "x" * (512 * 1024)) for i in range(1, 65)),
        )
        db.commit()
    finally:
        db.close()
    materialized: list[int] = []
    cursors: list[Any] = []

    class CountedCursor(sqlite3.Cursor):
        tracked: bool = False
        closed: bool = False

        def fetchmany(self, size: int | None = None) -> list[Any]:
            result = super().fetchmany(self.arraysize if size is None else size)
            if self.tracked:
                materialized.extend([1] * len(result))
            return result

        def __next__(self) -> Any:
            result = super().__next__()
            if self.tracked:
                materialized.append(1)
            return result

        def close(self) -> None:
            self.closed = True
            super().close()

    class CountedConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = ()) -> CountedCursor:
            cursor = self.cursor(factory=CountedCursor)
            cursor.tracked = sql.startswith("SELECT * FROM series ")
            if cursor.tracked:
                cursors.append(cursor)
            return cursor.execute(sql, parameters)

    counted = sqlite3.connect(ledger_path, factory=CountedConnection)
    try:
        counted.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 16 * 1024**2)
        with pytest.raises(InvalidIdentity, match="budget"):
            collect_references(
                counted,
                str(ledger_path.parent),
                InventoryBudget(max_hash_bytes=2 * 1024**2),
            )
        assert len(materialized) <= 5, (
            "metadata cap applied only after retaining an entire64-row page"
        )
        assert cursors and all(cursor.closed for cursor in cursors)
        assert not counted.in_transaction and counted.total_changes == 0
        assert counted.execute("SELECT count(*) FROM series").fetchone()[0] == 64
    finally:
        counted.close()


def test_fs_ordinary_large_sql_evidence_has_no_small_per_row_limit(
    ledger_path: Path,
) -> None:
    db = sqlite3.connect(ledger_path)
    try:
        db.execute(
            "INSERT INTO series(id,title,search_pattern) VALUES(1,?,'')",
            ("x" * (8 * 1024**2),),
        )
        db.commit()
        closure = collect_references(
            db, str(ledger_path.parent), InventoryBudget(max_hash_bytes=16 * 1024**2)
        )
        assert not closure.roles
    finally:
        db.close()


def test_fs_report_metadata_is_reserved_before_hashing_unreportable_role(
    fs_observer: ModuleType,
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import private_file_claim as claims

    env = producer_media
    with file_mutation_guard(str(env.db_path)) as guard:
        with claims.ensure_namespace(guard, str(env.library)):
            pass
    db = sqlite3.connect(env.db_path)
    try:
        old = db.execute("SELECT ownership_json FROM file_claim_namespaces").fetchone()[
            0
        ]
        exact = " " * (2 * 1024**2) + old
        db.execute("UPDATE file_claim_namespaces SET ownership_json=?", (exact,))
        db.commit()
    finally:
        db.close()
    closure, _ = captured_fixture(env)
    reads: list[int] = []

    class Reads(MediaProxy):
        def read(self, fd: int, count: int) -> bytes:
            reads.append(count)
            return os.read(fd, count)

    monkeypatch.setattr(fs_observer, "_media", Reads())
    with file_mutation_guard(str(env.db_path)) as guard:
        report = fs_observer._observe_closure(
            guard, str(env.scope), closure, InventoryBudget()
        )
    assert "evidence_budget_exceeded" in report.unresolved
    assert not reads, "read content for a role whose preserved evidence cannot fit"
    assert len(report.observed) < len(closure.roles)
    assert report.closure == closure
    assert all(r.legacy_json == exact for r in report.closure.roles)
    assert len(report.to_json().encode()) <= 16 * 1024**2


@pytest.mark.parametrize(
    "which", ["content_exact", "content_over", "objects_exact", "objects_over"]
)
def test_fs_shared_observation_budget_from_real_prepared_rescan(
    fs_observer: ModuleType, producer_media: ProducerMedia, which: str
) -> None:
    env = producer_media
    producer_rescan(env, "prepared")
    closure, _ = captured_fixture(env)
    size = env.source.stat().st_size
    budget = InventoryBudget(
        max_objects=1 if which == "objects_over" else len(closure.roles),
        max_hash_bytes=size - 1 if which == "content_over" else size,
    )
    with file_mutation_guard(str(env.db_path)) as guard:
        report = fs_observer._observe_closure(guard, str(env.scope), closure, budget)
    if which.endswith("exact"):
        assert not report.unresolved
        assert (
            sum(
                json.loads(r.constraints_json)["content"]["size"]
                for r in report.observed
                if json.loads(r.constraints_json)["content"] is not None
            )
            == size
        )
    else:
        assert report.unresolved
    assert len(report.to_json().encode()) <= 16 * 1024**2


def test_fs_real_foreign_uid_marker_is_preserved(
    fs_observer: ModuleType, ledger_path: Path, other_uid_python_image: str
) -> None:
    shared = ledger_path.parent / "foreign-marker"
    shared.mkdir(mode=0o770)
    shared.chmod(0o770)
    marker = shared / "marker"

    def actor(script: str) -> str:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--pull=never",
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--pids-limit=16",
                "--memory=128m",
                "--memory-swap=128m",
                "--cpus=1",
                "--user",
                f"{os.geteuid() + 1}:{os.getegid()}",
                "--mount",
                f"type=bind,src={shared},dst=/attack",
                "--entrypoint=python",
                other_uid_python_image,
                "-c",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    assert actor(
        "import os; from pathlib import Path; p=Path('/attack/marker'); "
        "p.write_bytes(b'foreign-marker-bytes'); p.chmod(0o600); print(os.geteuid())"
    ) == str(os.geteuid() + 1)
    before = marker.stat()
    result = capture(fs_observer, ledger_path, shared, role(marker, kind="marker"))
    assert result.outcome == "policy_mismatch"
    assert marker.stat() == before
    assert (
        actor(
            "from pathlib import Path; print(Path('/attack/marker').read_bytes().hex())"
        )
        == b"foreign-marker-bytes".hex()
    )


def assert_sqlite_writer_status(db_path: Path, *, blocked: bool) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            "import sqlite3,sys; db=sqlite3.connect(sys.argv[1],timeout=0); "
            "\ntry:\n db.execute('BEGIN IMMEDIATE')"
            "\nexcept sqlite3.OperationalError as exc:\n print(exc.sqlite_errorcode)"
            "\nelse:\n print('writer-acquired')"
            "\nfinally:\n db.close()",
            str(db_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    expected = str(sqlite3.SQLITE_BUSY) if blocked else "writer-acquired"
    assert result.stdout.strip() == expected, result.stdout


@pytest.mark.parametrize(
    ("control", "alias", "race"),
    [
        (control, alias, False)
        for control in ("db", "wal", "shm", "journal")
        for alias in (False, True)
    ]
    + [("guard", False, False), ("db", True, True), ("shm", True, True)],
)
def test_fs_sqlite_control_inodes_refused_before_media_open(
    fs_observer: ModuleType,
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control: str,
    alias: bool,
    race: bool,
) -> None:
    suffix = {"db": "", "wal": "-wal", "shm": "-shm", "journal": "-journal"}
    with file_mutation_guard(str(ledger_path)) as guard:
        db = sqlite3.connect(ledger_path, timeout=0)
        try:
            # The raced DB case isolates DB POSIX locks in rollback mode;
            # the raced SHM case isolates WAL's shared-memory writer lock.
            if control != "journal" and not (control == "db" and race):
                assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
            else:
                assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO series(id,title,search_pattern) VALUES(999,'pending','')"
            )
            source = (
                Path(guard._lock_path)
                if control == "guard"
                else Path(str(ledger_path) + suffix[control])
            )
            assert source.is_file()
            target = ledger_path.parent / "media-alias" if alias else source
            replacement = ledger_path.parent / "reserved-replacement"
            if race:
                target.write_bytes(b"ordinary media before name swap")
                os.link(source, replacement)
            elif alias:
                os.link(source, target)
            inode = (source.stat().st_dev, source.stat().st_ino)
            attempts: list[int] = []

            class Opens(MediaProxy):
                def open(self, path: str, flags: int, **kwargs: Any) -> int:
                    if race and path == target.name and flags & os.O_PATH:
                        os.replace(replacement, target)
                    try:
                        info = os.stat(path, follow_symlinks=False, **kwargs)
                    except FileNotFoundError:
                        info = None
                    if info is not None and (info.st_dev, info.st_ino) == inode:
                        attempts.append(flags)
                        assert flags & os.O_PATH, (
                            "attempted unmanaged readable SQLite/control FD"
                        )
                    if path.startswith("/proc/self/fd/"):
                        held = os.fstat(int(path.rsplit("/", 1)[1]))
                        assert (held.st_dev, held.st_ino) != inode, (
                            "attempted readable control reopen"
                        )
                    return os.open(path, flags, **kwargs)

                def read(self, fd: int, count: int) -> bytes:
                    pytest.fail("SQLite/control bytes must not be read as media")

            before = control_snapshot(ledger_path)
            descriptors = set(os.listdir("/proc/self/fd"))
            assert_sqlite_writer_status(ledger_path, blocked=True)
            monkeypatch.setattr(fs_observer, "_media", Opens())
            try:
                result = fs_observer._observe_role(
                    guard,
                    role(target),
                    str(ledger_path.parent),
                    frozenset({str(target)}),
                    fs_observer._ObservationBudget(InventoryBudget()),
                )
                assert result.outcome == "unsafe_path"
                if race:
                    assert len(attempts) == 1 and attempts[0] & os.O_PATH
                else:
                    assert not attempts, "stable reserved inode opened even with O_PATH"
                assert json.loads(result.constraints_json)["content"] is None
                guard.verify()
                assert control_snapshot(ledger_path) == before
                assert set(os.listdir("/proc/self/fd")) == descriptors
            finally:
                assert_sqlite_writer_status(ledger_path, blocked=True)
        finally:
            db.rollback()
            db.close()
        assert_sqlite_writer_status(ledger_path, blocked=False)


def test_fs_ordinary_archive_hardlinks_remain_supported(
    fs_observer: ModuleType,
    ledger_path: Path,
) -> None:
    source = ledger_path.parent / "ordinary.cbz"
    write_archive(source, b"ordinary archive")
    alias = ledger_path.parent / "ordinary-alias.cbz"
    os.link(source, alias)
    result = capture(fs_observer, ledger_path, ledger_path.parent, role(alias))
    assert result.outcome == "observed"
    assert json.loads(result.constraints_json)["current"]["nlink"] == 2
    assert alias.read_bytes() == source.read_bytes()
