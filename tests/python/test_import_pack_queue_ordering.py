"""Complete provisional rows need revalidation and exact SQL receipt CAS."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from test_import_pack_cleanup_durability import _PackEnv, pack_env  # noqa: F401
from test_import_pack_nfs_lifecycle import _expire, _queue_images
from test_import_pack_private_placement import _provision
from test_import_pack_protocol import _fallback
from test_import_pack_provisional_queue_fence import (
    _KILL_AFTER_QUEUE_COMMIT,
    test_crash_after_complete_queue_link_blocks_matching_claim_before_promotion as _crash,
)


@pytest.mark.parametrize("version", [1, 2, 3])
def test_queue_receipt_versions_roundtrip_without_legacy_fabrication(
    version: int,
) -> None:
    from private_pack_claim import PackOwnership

    ownership = PackOwnership("identity", "original-owner", version=version)
    if version == 3:
        ownership = replace(ownership, queue_receipt_sha256="a" * 64, phase="queued")
    encoded = ownership.to_json()
    assert PackOwnership.from_json(encoded) == ownership
    assert ("queue_receipt_sha256" in json.loads(encoded)) == (version == 3)


@pytest.mark.parametrize("receipt", [None, True, "", "a" * 63, "A" * 64, "x" * 64])
def test_v3_queued_proof_refuses_missing_or_malformed_receipt(receipt: object) -> None:
    from private_pack_claim import PackOwnership, PackProofError

    value = json.loads(PackOwnership("identity", "original-owner", version=2).to_json())
    value.update(version=3, phase="queued", queue_receipt_sha256=receipt)
    with pytest.raises(PackProofError):
        PackOwnership.from_json(json.dumps(value))


@pytest.mark.parametrize("edit", ["unchanged", "proposal", "lease", "domain"])
def test_real_crash_replay_promotes_only_unchanged_provisional_rows(
    pack_env: _PackEnv, edit: str
) -> None:
    import import_pack_cleanup as cleanup

    _crash(pack_env)
    with sqlite3.connect(pack_env["db_path"]) as db:
        queue_id, encoded = db.execute(
            "SELECT queue_id,directory_ownership_json FROM import_pack_cleanup_reservations"
        ).fetchone()
        if edit == "proposal":
            db.execute(
                "UPDATE import_queue_files SET dst_path=dst_path||'.user-edit' WHERE queue_id=?",
                (queue_id,),
            )
        elif edit == "lease":
            db.execute(
                "UPDATE import_queue SET lease_owner='successor',lease_expires_at=datetime('now','+1 hour') WHERE id=?",
                (queue_id,),
            )
        elif edit == "domain":
            db.execute(
                "INSERT INTO history(event_type,series_id,download_id) VALUES('imported',1,'linked-provisional-crash')"
            )
        before_queue = db.execute(
            "SELECT * FROM import_queue WHERE id=?", (queue_id,)
        ).fetchall()
        before_files = db.execute(
            "SELECT * FROM import_queue_files WHERE queue_id=? ORDER BY id", (queue_id,)
        ).fetchall()
    _expire(pack_env)
    cleanup.recover_pack_cleanup_state()
    with sqlite3.connect(pack_env["db_path"]) as db:
        purpose, proof = db.execute(
            "SELECT purpose,directory_ownership_json FROM import_pack_cleanup_reservations"
        ).fetchone()
        ownership = json.loads(proof)
        assert (
            db.execute("SELECT * FROM import_queue WHERE id=?", (queue_id,)).fetchall()
            == before_queue
        )
        assert (
            db.execute(
                "SELECT * FROM import_queue_files WHERE queue_id=? ORDER BY id",
                (queue_id,),
            ).fetchall()
            == before_files
        )
    if edit == "unchanged":
        assert purpose == "queueing" and ownership["phase"] == "attached"
        assert ownership["version"] == 3
        assert len(ownership["queue_receipt_sha256"]) == 64
    else:
        assert purpose == "cleanup", "edited provisional rows were promoted"
        assert ownership["phase"] == "queued"
        assert proof == encoded, "receipt/proof was overwritten after mismatch"


@pytest.mark.parametrize("edit", [None, "proposal", "lease", "domain"])
def test_uncertain_provisional_commit_still_validates_and_cancels_replacement(
    pack_env: _PackEnv, monkeypatch: pytest.MonkeyPatch, edit: str | None
) -> None:
    _provision(pack_env)
    _fallback(monkeypatch)
    real_connect = sqlite3.connect
    displaced = pack_env["tmp_path"] / "prevalidation-original"
    replaced: list[Path] = []
    receipts: list[str] = []

    class UncertainConnection(sqlite3.Connection):
        queue_inserted = False

        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            cursor = super().execute(sql, parameters)
            if sql.startswith("INSERT INTO import_queue("):
                self.queue_inserted = True
            return cursor

        def commit(self) -> None:
            super().commit()
            if self.queue_inserted and not replaced:
                actual = Path(
                    self.execute("SELECT src_path FROM import_queue_files").fetchone()[
                        0
                    ]
                ).parent
                actual.rename(displaced)
                actual.mkdir(mode=0o755)
                (actual / "unrelated.cbz").write_bytes(b"retain unrelated")
                replaced.append(actual)
                receipts.append(
                    self.execute(
                        "SELECT directory_ownership_json FROM import_pack_cleanup_reservations"
                    ).fetchone()[0]
                )
                if edit == "proposal":
                    self.execute(
                        "UPDATE import_queue_files SET dst_path=dst_path||'.user-edit'"
                    )
                elif edit == "lease":
                    self.execute(
                        "UPDATE import_queue SET lease_owner='successor',lease_expires_at=datetime('now','+1 hour')"
                    )
                elif edit == "domain":
                    self.execute(
                        "INSERT INTO history(event_type,series_id,download_id) VALUES('imported',1,'uncertain-replacement')"
                    )
                super().commit()
                raise sqlite3.OperationalError(
                    "ambiguous successful provisional COMMIT"
                )

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs["factory"] = UncertainConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    assert _queue_images(pack_env, "uncertain-replacement") is None
    assert (
        replaced and (replaced[0] / "unrelated.cbz").read_bytes() == b"retain unrelated"
    )
    assert list(displaced.glob("*.cbz"))
    with real_connect(pack_env["db_path"]) as db:
        expected = 0 if edit is None else 1
        assert db.execute("SELECT COUNT(*) FROM import_queue").fetchone() == (expected,)
        assert db.execute("SELECT COUNT(*) FROM import_queue_files").fetchone() == (
            expected,
        )
        purpose, queue_id, encoded = db.execute(
            "SELECT purpose,queue_id,directory_ownership_json FROM import_pack_cleanup_reservations"
        ).fetchone()
        assert purpose == "cleanup"
        if edit is None:
            assert queue_id is None and json.loads(encoded)["phase"] == "retained"
        else:
            assert queue_id is not None and encoded == receipts[0]
            if edit == "proposal":
                assert (
                    db.execute("SELECT dst_path FROM import_queue_files")
                    .fetchone()[0]
                    .endswith(".user-edit")
                )
            elif edit == "lease":
                assert db.execute(
                    "SELECT lease_owner FROM import_queue"
                ).fetchone() == ("successor",)
            else:
                assert db.execute(
                    "SELECT COUNT(*) FROM history WHERE event_type='imported'"
                ).fetchone() == (1,)


def test_uncertain_successful_promotion_is_resolved_without_reset(
    pack_env: _PackEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_connect = sqlite3.connect
    hits: list[bool] = []

    class UncertainPromotion(sqlite3.Connection):
        promoted = False

        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            result = super().execute(sql, parameters)
            if "SET purpose='queueing'" in sql:
                self.promoted = True
            return result

        def commit(self) -> None:
            super().commit()
            if self.promoted and not hits:
                hits.append(True)
                raise sqlite3.OperationalError("ambiguous successful promotion COMMIT")

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs["factory"] = UncertainPromotion
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    queue_id = _queue_images(pack_env, "uncertain-promotion")
    assert hits and queue_id is not None
    with real_connect(pack_env["db_path"]) as db:
        purpose, linked, encoded = db.execute(
            "SELECT purpose,queue_id,directory_ownership_json FROM import_pack_cleanup_reservations"
        ).fetchone()
        assert (purpose, linked) == ("queueing", queue_id)
        assert json.loads(encoded)["phase"] == "attached"
        assert db.execute(
            "SELECT COUNT(*) FROM import_queue_files WHERE queue_id=?", (queue_id,)
        ).fetchone() == (1,)


@pytest.mark.parametrize("outcome", ["before", "unreadable", "malformed", "successor"])
def test_uncertain_promotion_requires_a_readable_matching_decision(
    pack_env: _PackEnv, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    import import_pack_cleanup as cleanup

    real_connect = sqlite3.connect
    hits: list[bool] = []

    class FaultPromotion(sqlite3.Connection):
        promoted = False

        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            if (
                outcome == "unreadable"
                and hits
                and "SELECT * FROM import_pack_cleanup_reservations" in sql
            ):
                raise sqlite3.OperationalError("promotion receipt unreadable")
            cursor = super().execute(sql, parameters)
            if "SET purpose='queueing'" in sql:
                self.promoted = True
            return cursor

        def commit(self) -> None:
            if self.promoted and not hits:
                hits.append(True)
                if outcome != "before":
                    super().commit()
                    if outcome == "malformed":
                        super().execute(
                            "UPDATE import_pack_cleanup_reservations SET directory_ownership_json='{}'"
                        )
                        super().commit()
                    elif outcome == "successor":
                        super().execute(
                            "UPDATE import_pack_cleanup_reservations SET owner_token='new-winner'"
                        )
                        super().commit()
                raise sqlite3.OperationalError("uncertain promotion")
            super().commit()

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs["factory"] = FaultPromotion
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    if outcome == "before":
        queue_id = _queue_images(pack_env, "promotion-outcome")
        assert queue_id is not None
    else:
        with pytest.raises(cleanup._PackQueueDecisionUnresolved):
            _queue_images(pack_env, "promotion-outcome")
    assert hits
    with real_connect(pack_env["db_path"]) as db:
        purpose, owner, encoded = db.execute(
            "SELECT purpose,owner_token,directory_ownership_json FROM import_pack_cleanup_reservations"
        ).fetchone()
        assert db.execute("SELECT COUNT(*) FROM import_queue").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM import_queue_files").fetchone() == (1,)
        if outcome == "before":
            assert purpose == "cleanup" and json.loads(encoded)["phase"] == "queued"
        else:
            assert purpose == "queueing", "uncertainty reset a committed admission"
            if outcome == "malformed":
                assert encoded == "{}"
            else:
                assert json.loads(encoded)["phase"] == "attached"
            if outcome == "successor":
                assert owner == "new-winner", "uncertainty overwrote a later owner"


def test_linked_provisional_queue_cannot_freeze_forward_source_origin(
    pack_env: _PackEnv,
) -> None:
    import shared
    from file_mutation_lock import file_mutation_guard
    from private_pack_claim import PackProofError
    from private_pack_source import _freeze_pack_file_origin

    _crash(pack_env)
    with shared.get_db() as db:
        queue = dict(
            db.execute("SELECT * FROM import_queue ORDER BY id LIMIT 1").fetchone()
        )
        file_id, src_path = db.execute(
            "SELECT id,src_path FROM import_queue_files WHERE queue_id=?",
            (queue["id"],),
        ).fetchone()
    original = Path(src_path).read_bytes()
    with file_mutation_guard(shared.DB_PATH) as guard, pytest.raises(PackProofError):
        _freeze_pack_file_origin(guard, queue, file_id, src_path)
    assert Path(src_path).read_bytes() == original


@pytest.mark.parametrize("version", [1, 2])
def test_provable_legacy_queued_journal_replays_without_invented_receipt(
    pack_env: _PackEnv, version: int
) -> None:
    import import_pack_cleanup as cleanup
    import shared
    from file_mutation_lock import file_mutation_guard
    from private_pack_claim import PackOwnership

    if version == 1:
        # The unchanged generic crash control forces private placement. Use its
        # same real SIGKILL checkpoint with native rename for the historical v1.
        hook = "import_pack_cleanup._rename_noreplace = unsupported"
        assert _KILL_AFTER_QUEUE_COMMIT.count(hook) == 1
        script = _KILL_AFTER_QUEUE_COMMIT.replace(hook, "")
        root = Path(__file__).resolve().parents[2]
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            [
                str(root / "app"),
                str(root / "tests" / "python"),
                environment.get("PYTHONPATH", ""),
            ]
        )
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                json.dumps({key: str(value) for key, value in pack_env.items()}),
            ],
            cwd=root,
            env=environment,
            timeout=30,
            capture_output=True,
            text=True,
        )
        assert child.returncode == -signal.SIGKILL, child.stdout + child.stderr
    else:
        _crash(pack_env)
    with shared.get_db() as db:
        identity = db.execute(
            "SELECT download_identity_key FROM import_pack_cleanup_reservations"
        ).fetchone()[0]
    with file_mutation_guard(shared.DB_PATH) as guard:
        reservation = cleanup._read_reservation(identity)
        assert reservation is not None
        # Historical native v1 did not allocate a build carrier. Settle the
        # actual modern empty carrier using its proof before constructing v1.
        if version == 1:
            reservation = cleanup._discard_pack_placement(
                guard, reservation, empty_only=True
            )
        ownership = cleanup._ownership(reservation)
        assert ownership is not None
        legacy = replace(ownership, version=version, queue_receipt_sha256=None)
        with shared.get_db() as db:
            db.execute(
                "UPDATE import_pack_cleanup_reservations SET directory_ownership_json=?",
                (legacy.to_json(),),
            )
    _expire(pack_env)
    cleanup.recover_pack_cleanup_state()
    with shared.get_db() as db:
        purpose, encoded = db.execute(
            "SELECT purpose,directory_ownership_json FROM import_pack_cleanup_reservations"
        ).fetchone()
    assert purpose == "queueing"
    promoted = PackOwnership.from_json(encoded)
    assert promoted.version == version and promoted.phase == "attached"
    assert promoted.queue_receipt_sha256 is None
    assert "queue_receipt_sha256" not in json.loads(encoded)
    with shared.get_db() as db:
        source = db.execute(
            "SELECT src_path FROM import_queue_files WHERE queue_id=?",
            (reservation.queue_id,),
        ).fetchone()[0]
    with zipfile.ZipFile(source) as archive:
        assert archive.read("001.jpg") == b"page-one"


@pytest.mark.parametrize(
    "after_commit", [False, True], ids=["before", "ambiguous-after"]
)
def test_cancel_commit_uncertainty_never_resets_an_unproven_decision(
    pack_env: _PackEnv, monkeypatch: pytest.MonkeyPatch, after_commit: bool
) -> None:
    import import_pack_cleanup as cleanup

    real_connect = sqlite3.connect
    moved = pack_env["tmp_path"] / "cancellation-original"
    replacements: list[Path] = []
    hits: list[bool] = []

    class CancelFault(sqlite3.Connection):
        cancelled = False

        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            result = super().execute(sql, parameters)
            if sql.startswith("DELETE FROM import_queue WHERE"):
                self.cancelled = True
            return result

        def executemany(self, sql: str, parameters: Any) -> sqlite3.Cursor:
            if sql.startswith("INSERT INTO import_queue_files"):
                actual = Path(parameters[0][2]).parent
                actual.rename(moved)
                actual.mkdir(mode=0o700)
                (actual / "unrelated").write_bytes(b"unrelated")
                replacements.append(actual)
            return super().executemany(sql, parameters)

        def commit(self) -> None:
            if self.cancelled and not hits:
                hits.append(True)
                if after_commit:
                    super().commit()
                raise sqlite3.OperationalError("uncertain cancellation commit")
            super().commit()

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs["factory"] = CancelFault
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    if after_commit:
        assert _queue_images(pack_env, "uncertain-cancel") is None
    else:
        with pytest.raises(cleanup._PackQueueDecisionUnresolved):
            _queue_images(pack_env, "uncertain-cancel")
    assert hits and (replacements[0] / "unrelated").read_bytes() == b"unrelated"
    assert list(moved.glob("*.cbz"))
    with real_connect(pack_env["db_path"]) as db:
        expected = 0 if after_commit else 1
        assert db.execute("SELECT COUNT(*) FROM import_queue").fetchone() == (expected,)
        assert db.execute("SELECT COUNT(*) FROM import_queue_files").fetchone() == (
            expected,
        )
        purpose, queue_id, encoded = db.execute(
            "SELECT purpose,queue_id,directory_ownership_json FROM import_pack_cleanup_reservations"
        ).fetchone()
        assert purpose == "cleanup"
        assert (queue_id is None) == after_commit
        assert json.loads(encoded)["phase"] == (
            "retained" if after_commit else "queued"
        )
