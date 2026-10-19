"""Allocation diagnostics must not change durable recovery authority."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
from typing import Any

import pytest

import private_file_claim as claims
import rescan_file_recovery as recovery
from file_mutation_lock import file_mutation_guard
from test_rescan_recovery_protocol import _fixture, _rows
from test_rescan_transactions import rescan_env as rescan_env


@pytest.mark.parametrize("best_effort", [False, True])
@pytest.mark.parametrize("long_cause", [False, True])
def test_namespace_failure_keeps_cause_and_series_fence(
    rescan_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    best_effort: bool,
    long_cause: bool,
) -> None:
    import rescan

    path, _, target, context = _fixture(rescan_env)
    original = path.read_bytes()
    cause = "namespace ownership verification failed"
    if long_cause:
        cause += "; allocation:source; " + "x" * 10000

    def fail_namespace(*args: Any, **kwargs: Any) -> Any:
        raise claims.PrivateClaimError(cause)

    with monkeypatch.context() as patch:
        patch.setattr(claims, "ensure_namespace", fail_namespace)
        if best_effort:
            rescan.enrich_reconciled_files(
                rescan._Reconciliation(rescan._empty_result(), (target,))
            )
        else:
            with pytest.raises(claims.PrivateClaimError, match="ownership"):
                recovery.enrich_target(target, context)
    row = _rows(rescan_env)[0]
    assert row["state"] == "prepared"
    assert row["diagnostic"].startswith("unrecorded allocation:stage; retained")
    assert "namespace ownership verification failed" in row["diagnostic"]
    assert len(row["diagnostic"]) <= 1100
    assert set(json.loads(row["carriers_json"]).values()) == {None}
    assert row["publication_receipt_json"] is None
    for _ in range(5):
        assert recovery.replay_rescan_file_operation(row["id"]) == "blocked"
        current = _rows(rescan_env)[0]
        assert current["diagnostic"] == row["diagnostic"]
        assert current["carriers_json"] == row["carriers_json"]
        assert current["state"] == "prepared"
    with rescan.get_db() as db:
        assert recovery.active_for_series(db, 7)
        assert recovery.active_for_path(db, str(path.parent))
    assert (
        recovery._reserve(
            target, context, recovery.fingerprint_path(str(path)), str(path)
        )
        is None
    )
    with sqlite3.connect(rescan_env["db_path"]) as db:
        db.execute("UPDATE volumes SET status='wanted',import_path=NULL")
    rescan.rescan_series_folder(7)
    with sqlite3.connect(rescan_env["db_path"]) as db:
        assert db.execute("SELECT status,import_path FROM volumes").fetchone() == (
            "wanted",
            None,
        )
        assert db.execute("SELECT count(*) FROM file_claim_namespaces").fetchone() == (
            0,
        )
    assert len(_rows(rescan_env)) == 1
    assert not (path.parent / ".mangarr-claims").exists()
    assert path.read_bytes() == original


def test_failed_carrier_save_never_persists_in_memory_authority(
    rescan_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _, target, context = _fixture(rescan_env)
    original = path.read_bytes()
    real_store = recovery._store

    def fail_save(operation: recovery.Operation, **kwargs: Any) -> None:
        if operation.carriers["stage"] is not None:
            raise sqlite3.OperationalError("carrier allocation save failed")
        real_store(operation, **kwargs)

    monkeypatch.setattr(recovery, "_store", fail_save)
    with pytest.raises(sqlite3.OperationalError, match="allocation save failed"):
        recovery.enrich_target(target, context)
    row = _rows(rescan_env)[0]
    assert row["state"] == "prepared"
    assert json.loads(row["carriers_json"])["stage"] is None
    retained = {
        p: p.read_bytes() for p in (path.parent / ".mangarr-claims").rglob("owner.json")
    }
    assert len(retained) == 2
    assert "carrier allocation save failed" in row["diagnostic"]
    for _ in range(3):
        assert recovery.replay_rescan_file_operation(row["id"]) == "blocked"
        assert _rows(rescan_env)[0]["diagnostic"] == row["diagnostic"]
    assert all(p.read_bytes() == data for p, data in retained.items())
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "diagnostic", ["allocating:stage", "unrecorded allocation:stage; retained"]
)
def test_legacy_gap_still_blocks_without_namespace_adoption(
    rescan_env: dict[str, Any], diagnostic: str
) -> None:
    path, _, target, context = _fixture(rescan_env)
    original = path.read_bytes()
    operation = recovery._reserve(
        target, context, recovery.fingerprint_path(str(path)), str(path)
    )
    assert operation is not None
    recovery._store(operation, diagnostic=diagnostic)
    for _ in range(3):
        assert recovery.replay_rescan_file_operation(operation.row["id"]) == "blocked"
    row = _rows(rescan_env)[0]
    assert row["state"] == "prepared"
    assert row["diagnostic"] == "unrecorded allocation:stage; retained"
    assert set(json.loads(row["carriers_json"]).values()) == {None}
    assert not (path.parent / ".mangarr-claims").exists()
    assert path.read_bytes() == original


def test_unrelated_settle_error_is_not_replaced_by_allocation_cause(
    rescan_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _, target, context = _fixture(rescan_env)
    operation = recovery._reserve(
        target, context, recovery.fingerprint_path(str(path)), str(path)
    )
    assert operation is not None

    def fail_settle(*args: Any) -> str:
        raise claims.PrivateClaimError("unrelated settlement failure")

    monkeypatch.setattr(recovery, "_settle", fail_settle)
    with file_mutation_guard(rescan_env["db_path"]) as guard:
        assert recovery._settle_safely(operation, guard) == "blocked"
    assert _rows(rescan_env)[0]["diagnostic"] == "unrelated settlement failure"


@pytest.mark.parametrize("purpose", ["stage", "source", "publication", "rollback"])
def test_allocation_annotation_keeps_purpose_and_original_exception(
    rescan_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, purpose: str
) -> None:
    path, _, target, context = _fixture(rescan_env)
    operation = recovery._reserve(
        target, context, recovery.fingerprint_path(str(path)), str(path)
    )
    assert operation is not None
    operation.fingerprints["publication"] = operation.fingerprints["source"]
    recovery._store(operation)
    original = claims.PrivateClaimError("namespace allocation refused")

    def fail_namespace(*args: Any, **kwargs: Any) -> Any:
        raise original

    monkeypatch.setattr(claims, "ensure_namespace", fail_namespace)
    with file_mutation_guard(rescan_env["db_path"]) as guard:
        with pytest.raises(claims.PrivateClaimError) as caught:
            recovery._allocate(operation, guard, purpose)
        assert caught.value is original
        assert _rows(rescan_env)[0]["diagnostic"].startswith(f"allocating:{purpose};")
        assert recovery._settle_safely(operation, guard) == "blocked"
    row = _rows(rescan_env)[0]
    assert row["diagnostic"] == (
        f"unrecorded allocation:{purpose}; retained; "
        "cause=PrivateClaimError: namespace allocation refused"
    )
    assert row["state"] == "prepared"
    assert set(json.loads(row["carriers_json"]).values()) == {None}


def test_annotation_write_failure_does_not_mask_original_allocation_error(
    rescan_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _, target, context = _fixture(rescan_env)
    original_bytes = path.read_bytes()
    real_store = recovery._store
    original = claims.PrivateClaimError("namespace allocation refused")

    def fail_namespace(*args: Any, **kwargs: Any) -> Any:
        raise original

    def fail_annotation(operation: recovery.Operation, **kwargs: Any) -> None:
        if "; cause=" in (kwargs.get("diagnostic") or ""):
            raise sqlite3.OperationalError("diagnostic write failed")
        real_store(operation, **kwargs)

    monkeypatch.setattr(claims, "ensure_namespace", fail_namespace)
    monkeypatch.setattr(recovery, "_store", fail_annotation)
    with pytest.raises(claims.PrivateClaimError) as caught:
        recovery.enrich_target(target, context)
    assert caught.value is original
    row = _rows(rescan_env)[0]
    assert row["state"] == "prepared"
    assert row["diagnostic"] == "unrecorded allocation:stage; retained"
    assert set(json.loads(row["carriers_json"]).values()) == {None}
    assert not (path.parent / ".mangarr-claims").exists()
    assert path.read_bytes() == original_bytes
    assert len(_rows(rescan_env)) == 1


def test_hard_kill_allocation_save_gap_retains_unrecorded_carrier(
    rescan_env: dict[str, Any],
) -> None:
    from test_rescan_nfs_recovery import _seed_source

    _, path, original = _seed_source(rescan_env, "cbz")
    root = Path(__file__).resolve().parents[2]
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os, signal, sys
import rescan, rescan_file_recovery as recovery, shared
shared.DB_PATH = sys.argv[1]
shared.CONFIG.clear()
shared.CONFIG['folder_format'] = ''
def kill_before_save(*args):
    os.kill(os.getpid(), signal.SIGKILL)
recovery._save_record = kill_before_save
rescan.rescan_series_folder(7)
""",
            rescan_env["db_path"],
        ],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "app")},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == -signal.SIGKILL, (child.stdout, child.stderr)
    row = _rows(rescan_env)[0]
    assert row["diagnostic"] == "allocating:stage"
    retained = {
        p: p.read_bytes() for p in (path.parent / ".mangarr-claims").rglob("owner.json")
    }
    assert len(retained) == 2
    for _ in range(3):
        assert recovery.replay_rescan_file_operation(row["id"]) == "blocked"
    after = _rows(rescan_env)[0]
    assert after["state"] == "prepared"
    assert after["diagnostic"] == "unrecorded allocation:stage; retained"
    assert set(json.loads(after["carriers_json"]).values()) == {None}
    assert all(p.read_bytes() == data for p, data in retained.items())
    assert path.read_bytes() == original
