"""Controlled namespace birth is distinct from proven legacy recovery."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from file_mutation_lock import file_mutation_guard
import private_file_claim as claims
from test_private_file_claim import claim_env as claim_env
from test_volume_file_deletion_journal import deletion_env as deletion_env


def _ownership(db_path: str, parent: Path) -> str:
    with sqlite3.connect(db_path) as db:
        value = db.execute(
            "SELECT ownership_json FROM file_claim_namespaces WHERE parent_path=?",
            (str(parent),),
        ).fetchone()
    assert value is not None and isinstance(value[0], str)
    return value[0]


def _legacy_proof(db_path: str, parent: Path) -> str:
    """Create an exact v1 marker/record as written by the pre-receipt helper."""
    value = json.loads(_ownership(db_path, parent))
    value.pop("creation_boundary", None)
    value["version"] = 1
    payload = {k: v for k, v in value.items() if k != "marker_fingerprint"}
    marker = parent / ".mangarr-claims" / "owner.json"
    marker.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    st = marker.stat()
    value["marker_fingerprint"] = {
        "dev": st.st_dev,
        "inode": st.st_ino,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "sha256": hashlib.sha256(marker.read_bytes()).hexdigest(),
    }
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE file_claim_namespaces SET ownership_json=? WHERE parent_path=?",
            (encoded, str(parent)),
        )
    return encoded


@pytest.mark.parametrize("mode", [0o770, 0o777, 0o2770, 0o757, 0o775])
@pytest.mark.parametrize("pending", [False, True])
def test_shared_unknown_or_null_refuses_before_mkdir_or_marker(
    claim_env: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    mode: int,
    pending: bool,
) -> None:
    db_path, parent = claim_env
    parent.chmod(mode)
    if pending:
        with sqlite3.connect(db_path) as db:
            db.execute(
                "INSERT INTO file_claim_namespaces(parent_path) VALUES(?)",
                (str(parent),),
            )
    writes: list[str] = []
    real_mkdir, real_write = os.mkdir, os.write

    def mkdir(*args: Any, **kwargs: Any) -> None:
        writes.append("mkdir")
        real_mkdir(*args, **kwargs)

    def write(*args: Any, **kwargs: Any) -> int:
        writes.append("write")
        return real_write(*args, **kwargs)

    with file_mutation_guard(db_path) as guard:
        monkeypatch.setattr(os, "mkdir", mkdir)
        monkeypatch.setattr(os, "write", write)
        with pytest.raises(claims.PrivateClaimError):
            with claims.ensure_namespace(guard, str(parent)):
                pytest.fail("shared-parent initial inode was blessed")
    assert writes == []
    assert not (parent / ".mangarr-claims").exists()
    assert parent.stat().st_mode & 0o7777 == mode


@pytest.mark.parametrize("mode", [0o700, 0o750, 0o755, 0o2750])
def test_controlled_birth_receipt_survives_later_shared_parent(
    claim_env: tuple[str, Path], mode: int
) -> None:
    db_path, parent = claim_env
    parent.chmod(mode)
    with file_mutation_guard(db_path) as guard:
        with claims.ensure_namespace(guard, str(parent)):
            pass
        encoded = _ownership(db_path, parent)
        value = json.loads(encoded)
        assert value["version"] == 2
        assert value["creation_boundary"] == {
            "kind": "exclusive_parent",
            "parent_uid": os.geteuid(),
            "parent_mode": mode,
        }
        parent.chmod(0o770)
        with claims.ensure_namespace(guard, str(parent)) as namespace:
            binding = claims.ClaimBinding(
                "pack", "original-owner", None, "pack_cleanup"
            )
            with claims.allocate_carrier(
                namespace, binding, str(parent / "source")
            ) as carrier:
                discarded = replace(carrier.record, phase="discarded")
            claims.gc_discarded_carrier(namespace, binding, discarded)
            assert os.listdir(namespace.fd) == ["owner.json"]
        assert _ownership(db_path, parent) == encoded
        assert parent.stat().st_mode & 0o777 == 0o770


def test_exclusive_v1_allocation_does_not_invent_historical_receipt(
    claim_env: tuple[str, Path],
) -> None:
    db_path, parent = claim_env
    with file_mutation_guard(db_path) as guard:
        with claims.ensure_namespace(guard, str(parent)):
            pass
        encoded = _legacy_proof(db_path, parent)
        marker = (parent / ".mangarr-claims" / "owner.json").read_bytes()
        with claims.ensure_namespace(guard, str(parent)) as namespace:
            binding = claims.ClaimBinding("deletion", "legacy", 1, "delete")
            with claims.allocate_carrier(
                namespace, binding, str(parent / "source")
            ) as carrier:
                discarded = replace(carrier.record, phase="discarded")
            claims.gc_discarded_carrier(namespace, binding, discarded)
        assert _ownership(db_path, parent) == encoded
        assert (parent / ".mangarr-claims" / "owner.json").read_bytes() == marker


def test_shared_v1_recovery_and_gc_preserved_but_new_allocation_refused(
    claim_env: tuple[str, Path],
) -> None:
    db_path, parent = claim_env
    with file_mutation_guard(db_path) as guard:
        with claims.ensure_namespace(guard, str(parent)):
            pass
        encoded = _legacy_proof(db_path, parent)
        binding = claims.ClaimBinding("deletion", "legacy", 1, "delete")
        with claims.ensure_namespace(guard, str(parent)) as namespace:
            with claims.allocate_carrier(
                namespace, binding, str(parent / "source")
            ) as carrier:
                record = carrier.record
        parent.chmod(0o770)
        with claims.open_namespace(guard, str(parent), encoded) as namespace:
            with claims.open_carrier(namespace, binding, record) as carrier:
                carrier.verify()
            claims.gc_discarded_carrier(
                namespace, binding, replace(record, phase="discarded")
            )
            with pytest.raises(claims.PrivateClaimError):
                with claims.allocate_carrier(
                    namespace, binding, str(parent / "another")
                ):
                    pytest.fail("READY v1 is not a controlled-birth receipt")
        assert _ownership(db_path, parent) == encoded
        assert {p.name for p in (parent / ".mangarr-claims").iterdir()} == {
            "owner.json"
        }


@pytest.mark.parametrize(
    "bad",
    [
        True,
        None,
        {},
        {"kind": "exclusive_parent", "parent_uid": True, "parent_mode": 0o700},
        {"kind": "exclusive_parent", "parent_uid": 1.0, "parent_mode": 0o700},
        {"kind": "exclusive_parent", "parent_uid": 1, "parent_mode": True},
        {"kind": "exclusive_parent", "parent_uid": 1, "parent_mode": 448.0},
        {"kind": "exclusive_parent", "parent_uid": 1, "parent_mode": 0o770},
        {"kind": "exclusive_parent", "parent_uid": 1, "parent_mode": 0o100700},
        {"kind": "shared_parent", "parent_uid": 1, "parent_mode": 0o700},
    ],
)
def test_invalid_receipt_cannot_authorize_open(
    claim_env: tuple[str, Path], bad: object
) -> None:
    db_path, parent = claim_env
    with file_mutation_guard(db_path) as guard:
        with claims.ensure_namespace(guard, str(parent)):
            pass
        value = json.loads(_ownership(db_path, parent))
        value["version"] = 2
        value["creation_boundary"] = bad
        with pytest.raises(claims.PrivateClaimError):
            with claims.open_namespace(guard, str(parent), json.dumps(value)):
                pytest.fail("malformed receipt accepted")


@pytest.mark.parametrize(
    "bad",
    [
        "duplicate",
        "boolean-version",
        "unknown-version",
        "missing-receipt",
        "unknown-field",
    ],
)
def test_namespace_version_and_receipt_fields_remain_strict(
    claim_env: tuple[str, Path], bad: str
) -> None:
    db_path, parent = claim_env
    with file_mutation_guard(db_path) as guard:
        with claims.ensure_namespace(guard, str(parent)):
            pass
        encoded = _ownership(db_path, parent)
        value = json.loads(encoded)
        if bad == "duplicate":
            encoded = encoded.replace(
                '"kind":"exclusive_parent"',
                '"kind":"exclusive_parent","kind":"exclusive_parent"',
            )
        else:
            if bad == "boolean-version":
                value["version"] = True
            elif bad == "unknown-version":
                value["version"] = 3
            elif bad == "missing-receipt":
                value.pop("creation_boundary")
            else:
                value["creation_boundary"]["invented"] = True
            encoded = json.dumps(value)
        with pytest.raises(claims.PrivateClaimError):
            with claims.open_namespace(guard, str(parent), encoded):
                pytest.fail("malformed namespace authority accepted")
