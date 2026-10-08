"""Caller refusal controls for persisted per-file source admission."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path

import pytest

from import_plan import _ImportPlan
from import_staging import _ImportStaging, _StageOutcome
from test_import_publication_journal import (
    _seed_queue,
    journal_env,  # noqa: F401
)


@pytest.mark.parametrize("damage", ("missing", "boolean-version", "missing-file", "malformed-pack"))
def test_stage_reads_durable_source_origin_and_refuses_unknown_admission(
    journal_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, damage: str,
) -> None:
    import import_execute

    queue_id, series_id, sources, finals = _seed_queue(journal_env, file_count=1)
    original = sources[0].read_bytes()
    actual_stage_files = import_execute._stage_files
    observed: list[int] = []

    async def damage_receipt_then_stage(
        plan: _ImportPlan, staging: _ImportStaging,
    ) -> list[_StageOutcome]:
        # Mutate the durable receipt at the real post-binding boundary; every
        # subsequent stage/publication/abort operation remains the actual code.
        with sqlite3.connect(journal_env["db_path"]) as db:
            publication_id, encoded = db.execute(
                "SELECT id,queue_snapshot_json FROM import_publications WHERE queue_id=?",
                (queue_id,),
            ).fetchone()
            snapshot = json.loads(encoded)
            assert snapshot["_publication_staging"]["phase"] == "ready"
            if damage == "missing":
                snapshot.pop("_pack_source_origins", None)
            elif damage == "boolean-version":
                snapshot["_pack_source_origins"] = {"version": True, "files": {}}
            elif damage == "missing-file":
                snapshot["_pack_source_origins"] = {"version": 1, "files": {}}
            else:
                snapshot["_pack_source_origins"] = {
                    "version": 1, "files": {str(plan.files[0].file_id): {"kind": "pack"}},
                }
            db.execute("UPDATE import_publications SET queue_snapshot_json=? WHERE id=?",
                       (json.dumps(snapshot), publication_id))
        observed.append(publication_id)
        return await actual_stage_files(plan, staging)

    monkeypatch.setattr(import_execute, "_stage_files", damage_receipt_then_stage)
    assert not asyncio.run(import_execute._execute_import(queue_id))
    assert len(observed) == 1
    assert sources[0].read_bytes() == original
    assert not finals[0].exists()
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM history WHERE series_id=? AND event_type='imported'",
                          (series_id,)).fetchone() == (0,)
        assert db.execute("SELECT status,download_id FROM volumes WHERE series_id=?",
                          (series_id,)).fetchone() == ("wanted", None)


def test_explicit_local_nonpack_import_keeps_ordinary_source_behavior(
    journal_env: dict[str, Path],
) -> None:
    import import_execute

    queue_id, series_id, sources, finals = _seed_queue(journal_env, file_count=1)
    original = sources[0].read_bytes()
    assert asyncio.run(import_execute._execute_import(queue_id))
    assert sources[0].read_bytes() == original
    assert finals[0].is_file()
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM history WHERE series_id=? AND event_type='imported'",
                          (series_id,)).fetchone() == (1,)


def test_real_prepublication_stage_collision_releases_owned_grab_without_import(
    journal_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_execute
    from import_publication import open_publication_stage

    queue_id, series_id, sources, finals = _seed_queue(journal_env, file_count=1)
    original = sources[0].read_bytes()
    actual_stage_files = import_execute._stage_files

    async def collide_then_stage(
        plan: _ImportPlan, staging: _ImportStaging,
    ) -> list[_StageOutcome]:
        assert staging.publication_id is not None and staging.owner_token is not None
        with open_publication_stage(staging.publication_id, staging.owner_token) as (_, carrier):
            fd = os.open(os.path.basename(plan.files[0].dst_path),
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=carrier.fd)
            try:
                os.write(fd, b"owned partial stage")
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(carrier.fd)
        return await actual_stage_files(plan, staging)

    monkeypatch.setattr(import_execute, "_stage_files", collide_then_stage)
    assert not asyncio.run(import_execute._execute_import(queue_id))
    assert sources[0].read_bytes() == original
    assert not finals[0].exists()
    with sqlite3.connect(journal_env["db_path"]) as db:
        assert db.execute("SELECT status,download_id FROM volumes WHERE series_id=?",
                          (series_id,)).fetchone() == ("wanted", None)
        assert db.execute("SELECT COUNT(*) FROM history WHERE series_id=? AND event_type='imported'",
                          (series_id,)).fetchone() == (0,)
