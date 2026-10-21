"""PR491 expected-correctness wire/stat regressions; no recovery authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import file_identity_recovery as observation
from file_identity import IdentityUnavailable, InvalidIdentity, OwnerRef
from test_file_identity_observation import (
    ProducerMedia,
    fs_observer as fs_observer,
    namespace_wire as namespace_wire,
    producer_media as producer_media,
    producer_publication,
    producer_queue,
)
from test_file_identity_observation_sql import inspect, namespace as namespace
from test_file_identity_schema import ledger_path as ledger_path


def resign(value: dict[str, Any]) -> str:
    value.pop("sha256", None)
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    value["sha256"] = hashlib.sha256(encoded.encode()).hexdigest()
    return json.dumps(value)


@pytest.mark.parametrize(
    "outcome",
    [
        "policy_mismatch",
        "boundary_unknown",
        "cross_device",
        "unsafe_path",
        "changed",
        "unreadable",
        "budget_exceeded",
        "unknown_children",
    ],
)
def test_wire_rejects_complete_evidence_relabelled_as_unresolved(
    namespace_wire: dict[str, Any],
    outcome: str,
) -> None:
    value = namespace_wire
    record = next(r for r in value["observed"] if r["spec"]["role"]["kind"] == "marker")
    fields = json.loads(record["constraints_json"])
    assert record["native"] and fields["current"] and fields["content"]
    assert fields["policy"] == "satisfied" and fields["children"] == []
    record["outcome"] = outcome
    value["unresolved"] = ["role_" + outcome]
    with pytest.raises(InvalidIdentity):
        observation.OwnershipInventory.from_json(resign(value))


@pytest.mark.parametrize(
    "outcome",
    [
        "policy_mismatch",
        "boundary_unknown",
        "cross_device",
        "unsafe_path",
        "changed",
        "unreadable",
        "budget_exceeded",
    ],
)
def test_exception_outcome_emitter_shape_remains_roundtrippable(
    ledger_path: Path,
    namespace: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> str:
        raise observation._ObservationFailure(outcome)

    monkeypatch.setattr(observation, "_hash", refuse)
    result = inspect(ledger_path, namespace)
    record = next(r for r in result.observed if r.spec.role.kind == "marker")
    fields = json.loads(record.constraints_json)
    assert record.outcome == outcome and "role_" + outcome in result.unresolved
    assert record.native is None
    assert fields["current"] is fields["content"] is None and fields["children"] == []
    assert fields["policy"] == (
        "mismatch" if outcome == "policy_mismatch" else "satisfied"
    )
    assert fields["boundary"] == (
        "cross_device" if outcome == "cross_device" else "verified"
    )


@pytest.mark.parametrize("native_supported", [True, False])
def test_unknown_children_retains_real_positive_evidence(
    ledger_path: Path,
    namespace: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_supported: bool,
) -> None:
    if not native_supported:

        def unsupported(_fd: int) -> None:
            raise IdentityUnavailable("fixture unsupported native capture")

        monkeypatch.setattr(observation, "capture_native", unsupported)
    child = namespace / ".mangarr-claims" / "unclaimed"
    child.write_bytes(b"not declared")
    result = inspect(ledger_path, namespace)
    record = next(r for r in result.observed if r.spec.role.purpose == "namespace_root")
    fields = json.loads(record.constraints_json)
    assert record.outcome == "unknown_children"
    assert (record.native is not None) is native_supported
    assert fields["current"] is not None and fields["content"] is None
    assert fields["policy"] == "satisfied" and fields["boundary"] == "verified"
    assert [c["path"] for c in fields["children"]] == [str(child)]
    assert "role_unknown_children" in result.unresolved


@pytest.fixture
def producer_wire(
    producer_media: ProducerMedia, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    from file_identity import NativeHandle

    env = producer_media
    producer_publication(env, producer_queue(env), "prepared")
    monkeypatch.setattr(
        observation, "capture_native", lambda _fd: NativeHandle(1, b"fixture-current")
    )
    result = inspect(env.db_path, env.scope)
    assert result.unresolved == ()
    assert result.closure.raw_rows and result.closure.dependencies
    return json.loads(result.to_json())


@pytest.mark.parametrize(
    "omission",
    ["raw_rows", "dependencies", "both", "owner_root", "context", "root_value"],
)
def test_wire_rejects_resigned_incomplete_or_inconsistent_sql_closure(
    producer_wire: dict[str, Any],
    omission: str,
) -> None:
    value = producer_wire
    closure = value["closure"]
    if omission in {"raw_rows", "both"}:
        closure["raw_rows"] = []
    if omission in {"dependencies", "both"}:
        closure["dependencies"] = []
    if omission in {"owner_root", "root_value"}:
        root = json.loads(closure["snapshots"][0]["rows_json"])["root"]
        raw = next(
            r
            for r in closure["raw_rows"]
            if (r["table"], r["key"]) == (root["table"], root["key"])
        )
        if omission == "owner_root":
            closure["raw_rows"].remove(raw)
        else:
            column = next(
                k for k, v in raw["values"] if k != "parent_path" and isinstance(v, str)
            )
            raw["values"] = [
                [k, v + " " if k == column else v] for k, v in raw["values"]
            ]
    if omission == "context":
        assert any(r["table"] == "series" for r in closure["raw_rows"])
        closure["raw_rows"] = [r for r in closure["raw_rows"] if r["table"] != "series"]
    with pytest.raises(InvalidIdentity):
        observation.OwnershipInventory.from_json(resign(value))


def test_real_producer_wire_and_required_fields_control(
    producer_wire: dict[str, Any],
) -> None:
    encoded = resign(producer_wire)
    assert observation.OwnershipInventory.from_json(encoded).unresolved == ()
    del producer_wire["closure"]["raw_rows"]
    with pytest.raises(InvalidIdentity):
        observation.OwnershipInventory.from_json(resign(producer_wire))


@pytest.mark.parametrize("conflicting", [False, True])
def test_wire_rejects_duplicate_raw_table_keys(
    producer_wire: dict[str, Any], conflicting: bool
) -> None:
    rows = producer_wire["closure"]["raw_rows"]
    duplicate = json.loads(json.dumps(rows[0]))
    if conflicting:
        duplicate["values"].append(
            ["zz_extra_scalar", "different row, same primary key"]
        )
    rows.append(duplicate)
    rows.sort(
        key=lambda row: (row["table"], json.dumps(row["key"], separators=(",", ":")))
    )
    with pytest.raises(InvalidIdentity):
        observation.OwnershipInventory.from_json(resign(producer_wire))


def test_wire_preserves_additional_scalar_columns(
    producer_wire: dict[str, Any],
) -> None:
    row = producer_wire["closure"]["raw_rows"][0]
    assert row["table"] == "file_claim_namespaces"
    row["values"].append(["recovery_event_id", 777])
    result = observation.OwnershipInventory.from_json(resign(producer_wire))
    assert dict(result.closure.raw_rows[0].values)["recovery_event_id"] == 777


@pytest.mark.parametrize("field", ["dev", "inode"])
@pytest.mark.parametrize("number", [2**63, 2**64 - 1])
def test_wire_accepts_full_unsigned_stat_identity(
    namespace_wire: dict[str, Any],
    field: str,
    number: int,
) -> None:
    record = namespace_wire["observed"][0]
    fields = json.loads(record["constraints_json"])
    fields["current"][field] = number
    record["constraints_json"] = json.dumps(
        fields, sort_keys=True, separators=(",", ":")
    )
    result = observation.OwnershipInventory.from_json(resign(namespace_wire))
    assert json.loads(result.observed[0].constraints_json)["current"][field] == number


@pytest.mark.parametrize("field", ["dev", "inode"])
@pytest.mark.parametrize("number", [-1, True, 2**64])
def test_wire_unsigned_stat_bounds_remain_strict(
    namespace_wire: dict[str, Any],
    field: str,
    number: int,
) -> None:
    record = namespace_wire["observed"][0]
    fields = json.loads(record["constraints_json"])
    fields["current"][field] = number
    record["constraints_json"] = json.dumps(
        fields, sort_keys=True, separators=(",", ":")
    )
    with pytest.raises(InvalidIdentity):
        observation.OwnershipInventory.from_json(resign(namespace_wire))


def test_stat_unsigned_range_does_not_change_sql_owner_or_signed_time_contract(
    namespace_wire: dict[str, Any],
) -> None:
    valid = observation.OwnershipInventory.from_json(resign(namespace_wire))
    with pytest.raises(InvalidIdentity):
        replace(valid.observed[0].spec, owner=OwnerRef("rescan", (2**63,)))
    record = next(
        r for r in namespace_wire["observed"] if r["spec"]["role"]["kind"] == "marker"
    )
    fields = json.loads(record["constraints_json"])
    fields["current"]["mtime_ns"] = fields["current"]["ctime_ns"] = -1
    record["constraints_json"] = json.dumps(
        fields, sort_keys=True, separators=(",", ":")
    )
    assert (
        observation.OwnershipInventory.from_json(resign(namespace_wire)).unresolved
        == ()
    )
