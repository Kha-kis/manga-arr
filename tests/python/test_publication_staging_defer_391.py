"""Live staging ownership is distinct from ambiguous recovery authority."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

import test_import_publication_journal as journal_tests
from test_import_publication_journal import journal_env  # noqa: F401


@pytest.mark.parametrize(
    ("case", "expected_deferred"),
    (
        ("owned-live-operation", 1),
        ("mismatched-live-queue", 0),
        ("unknown-stage-live-queue", 0),
        ("unknown-stage-live-operation", 0),
        ("unknown-stage-no-live-owner", 0),
    ),
)
def test_staging_replay_defers_only_with_proven_live_owner(
    journal_env: dict[str, Path], case: str, expected_deferred: int,
) -> None:
    import import_publication
    import main
    from import_lease import IMPORT_LEASE_SECONDS, claim_import_queue_row
    from import_plan import _plan_import

    queue_id, _, _, finals = journal_tests._seed_queue(journal_env, file_count=1)
    owner = "staging-defer-owner"
    with main.get_db() as db:
        assert claim_import_queue_row(db, queue_id, owner)
        plan = _plan_import(db, queue_id, owner, {}, {}, set(), "copy",
                            lease_seconds=IMPORT_LEASE_SECONDS)
    assert plan is not None
    stage, _ = import_publication.initialize_publication_filesystem(plan, owner)
    sentinel = Path(stage) / "retained.cbz"
    sentinel.write_bytes(b"live or ambiguous stage must remain")
    with sqlite3.connect(journal_env["db_path"]) as db:
        publication_id, snapshot = db.execute(
            "SELECT id,queue_snapshot_json FROM import_publications WHERE queue_id=?",
            (queue_id,),
        ).fetchone()
        if case.startswith("unknown-stage"):
            value = json.loads(snapshot)
            value.pop("_publication_staging")
            db.execute("UPDATE import_publications SET queue_snapshot_json=? WHERE id=?",
                       (json.dumps(value), publication_id))
        if case.endswith("live-operation"):
            db.execute("UPDATE import_queue SET lease_expires_at=datetime('now','-1 second') WHERE id=?",
                       (queue_id,))
            db.execute("UPDATE import_publications SET operation_owner='live-recovery',"
                       "operation_expires_at=datetime('now','+5 minutes') WHERE id=?",
                       (publication_id,))
        elif case == "mismatched-live-queue":
            db.execute("UPDATE import_queue SET lease_owner='unrelated-successor' WHERE id=?",
                       (queue_id,))
        elif case == "unknown-stage-no-live-owner":
            db.execute("UPDATE import_queue SET lease_expires_at=datetime('now','-1 second') WHERE id=?",
                       (queue_id,))

    summary = asyncio.run(import_publication.replay_import_publications(include_terminal=False))
    assert summary.examined == 1
    assert summary.deferred == expected_deferred
    assert summary.blocked == 1 - expected_deferred
    assert summary.aborted_staging == 0
    assert sentinel.read_bytes() == b"live or ambiguous stage must remain"
    assert not finals[0].exists()
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute("SELECT state FROM import_publications WHERE id=?", (publication_id,)).fetchone() == ("staging",)
        assert db.execute("SELECT COUNT(*) FROM history WHERE event_type='imported'").fetchone() == (0,)
