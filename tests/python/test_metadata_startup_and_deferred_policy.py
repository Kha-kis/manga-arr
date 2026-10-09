"""Startup core selection and deferred-map aggregate regressions for PR 411."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Any
from unittest.mock import AsyncMock

import pytest
import httpx

from test_metadata_retry_policy import (
    _map_snapshot,
    _source_state,
    offline_providers as offline_providers,
    policy_db as policy_db,
)


@pytest.fixture
def selection_clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Delegate date arithmetic to SQLite with a fixed selection-time clock."""
    import metadata_state
    import shared
    import tasks

    original_get_db = shared.get_db
    with sqlite3.connect(":memory:") as clock_db:

        def fixed_datetime(*args: Any) -> str | None:
            values = tuple(
                "2026-10-09T12:00:00+00:00" if value == "now" else value
                for value in args
            )
            query = "SELECT datetime(" + ",".join("?" for _ in values) + ")"
            return clock_db.execute(query, values).fetchone()[0]

        @contextmanager
        def get_db_at_fixed_time() -> Iterator[sqlite3.Connection]:
            with original_get_db() as db:
                db.create_function("datetime", -1, fixed_datetime)
                yield db

        monkeypatch.setattr(tasks, "get_db", get_db_at_fixed_time)
        monkeypatch.setattr(metadata_state, "get_db", get_db_at_fixed_time)
        yield


@pytest.fixture
def startup_boundaries(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    import tasks
    from routers import mangadex_

    monkeypatch.setattr(tasks.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(tasks, "_mdx_backoff_active", lambda: False)
    manifest = AsyncMock(return_value={"added": 0, "updated": 0, "total": 0})
    monkeypatch.setattr(mangadex_, "sync_mangadex_chapters", manifest)
    return manifest


def _aggregate_state() -> dict[str, Any]:
    from shared import get_db

    with get_db() as db:
        row = db.execute(
            "SELECT metadata_status,metadata_error,metadata_last_attempt,"
            " last_metadata_refresh FROM series WHERE id=7"
        ).fetchone()
    assert row is not None
    return dict(row)


def _seed_manifest() -> None:
    from shared import get_db

    with get_db() as db:
        db.execute(
            "INSERT INTO mangadex_chapters"
            " (series_id,mangadex_chapter_id,chapter_num,language)"
            " VALUES(7,'cached-manifest',1,'en')"
        )


@pytest.mark.parametrize(
    ("strategy", "last_success", "monitored", "deleted", "eligible"),
    [
        ("once", "2026-10-01T12:00:00+00:00", 1, False, False),
        ("once", None, 1, False, False),
        ("throttled", "2026-10-03T12:00:00+00:00", 1, False, False),
        ("throttled", "2026-10-02T12:00:01+00:00", 1, False, False),
        ("throttled", "2026-10-02T12:00:00+00:00", 1, False, True),
        ("throttled", None, 1, False, True),
        ("throttled", "invalid timestamp", 1, False, True),
        ("always", "2026-10-09T12:00:00+00:00", 1, False, True),
        (None, "2026-10-09T12:00:00+00:00", 1, False, True),
        ("always", "2026-10-09T12:00:00+00:00", 0, False, True),
        ("once", None, 0, False, False),
        ("always", None, 1, True, False),
    ],
)
def test_startup_core_backfill_respects_strategy_without_changing_monitor_policy(
    policy_db: Path,
    offline_providers: AsyncMock,
    startup_boundaries: AsyncMock,
    selection_clock: None,
    strategy: str | None,
    last_success: str | None,
    monitored: int,
    deleted: bool,
    eligible: bool,
) -> None:
    import tasks
    from metadata_state import metadata_retry_candidates
    from shared import get_db

    _seed_manifest()
    with get_db() as db:
        db.execute(
            "UPDATE series SET update_strategy=?,last_metadata_refresh=?,monitored=?,"
            " deleted_at=?,metadata_status='degraded',metadata_error='stored map failure'"
            " WHERE id=7",
            (strategy, last_success, monitored, "2026-10-01" if deleted else None),
        )
        db.execute(
            "INSERT INTO series_metadata_sources"
            " (series_id,source,status,next_retry_at,failure_count,error)"
            " VALUES(7,'chapter_map','failed','2099-01-01T00:00:00+00:00',3,"
            " 'stored map failure')"
        )
        db.execute(
            "INSERT INTO series_metadata_sources(series_id,source,status,next_retry_at)"
            " VALUES(7,'mangaupdates','failed','2000-01-01T00:00:00+00:00')"
        )
    before = _aggregate_state()
    source = _source_state()
    snapshot = _map_snapshot()
    assert metadata_retry_candidates() == ([7] if eligible and monitored else [])

    asyncio.run(tasks._backfill_metadata_loop())

    after = _aggregate_state()
    if eligible:
        assert after["metadata_last_attempt"] is not None
        assert after["last_metadata_refresh"] != before["last_metadata_refresh"]
    else:
        assert after == before
    assert _source_state() == source
    assert _map_snapshot() == snapshot
    offline_providers.assert_not_awaited()


@pytest.mark.parametrize(
    ("missing", "mu_success", "eligible"),
    [
        ("map", None, True),
        ("mangadex_id", None, True),
        ("mu_id", None, True),
        ("mu_id", "2026-10-08T12:00:00+00:00", False),
        ("mu_id", "2026-09-09T12:00:00+00:00", True),
        (None, None, False),
    ],
)
def test_startup_core_backfill_preserves_missing_identity_and_mu_cache_boundaries(
    policy_db: Path,
    offline_providers: AsyncMock,
    startup_boundaries: AsyncMock,
    selection_clock: None,
    monkeypatch: pytest.MonkeyPatch,
    missing: str | None,
    mu_success: str | None,
    eligible: bool,
) -> None:
    import metadata_enrichment
    import tasks
    from shared import get_db

    _seed_manifest()
    monkeypatch.setattr(
        metadata_enrichment,
        "fetch_mangadex_id",
        AsyncMock(return_value=("mdx-1", {})),
    )
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get.return_value = httpx.Response(
        200, json={"data": {"attributes": {"links": {}}}}
    )
    monkeypatch.setattr(metadata_enrichment.httpx, "AsyncClient", lambda **_: client)
    with get_db() as db:
        db.execute(
            "UPDATE series SET chapter_vol_map=?,mangadex_id=?,mu_id=? WHERE id=7",
            (
                None if missing == "map" else '{"1":1,"2":1,"3":2,"4":2}',
                None if missing == "mangadex_id" else "mdx-1",
                None if missing == "mu_id" else "789",
            ),
        )
        if mu_success:
            db.execute(
                "INSERT INTO series_metadata_sources"
                " (series_id,source,status,last_success_at)"
                " VALUES(7,'mangaupdates','healthy',?)",
                (mu_success,),
            )
    before = _aggregate_state()
    asyncio.run(tasks._backfill_metadata_loop())
    if eligible:
        assert _aggregate_state()["metadata_last_attempt"] is not None
    else:
        assert _aggregate_state() == before
        offline_providers.assert_not_awaited()


@pytest.mark.parametrize("strategy", ["once", "throttled"])
def test_startup_manifest_observation_remains_independent_of_core_strategy(
    policy_db: Path,
    offline_providers: AsyncMock,
    startup_boundaries: AsyncMock,
    selection_clock: None,
    strategy: str,
) -> None:
    import tasks
    from shared import get_db

    with get_db() as db:
        db.execute(
            "UPDATE series SET update_strategy=?,last_metadata_refresh=?,"
            " chapter_vol_map=? WHERE id=7",
            (strategy, "2026-10-09T12:00:00+00:00", '{"1":1,"3":2}'),
        )

    async def observe_manifest(series_id: int) -> dict[str, int]:
        assert series_id == 7
        _seed_manifest()
        return {"added": 1, "updated": 0, "total": 1}

    startup_boundaries.side_effect = observe_manifest
    before = _aggregate_state()
    asyncio.run(tasks._backfill_metadata_loop())
    assert _aggregate_state() == before
    with get_db() as db:
        assert db.execute("SELECT count(*) FROM mangadex_chapters").fetchone()[0] == 1
    offline_providers.assert_not_awaited()


@pytest.mark.parametrize("apply_changes", [False, True])
@pytest.mark.parametrize(
    ("status", "error"),
    [
        ("failed", "saved map failure"),
        ("failed", None),
        ("degraded", "saved map failure"),
        ("healthy", None),
        (None, None),
    ],
)
def test_deferred_map_keeps_aggregate_source_warning_and_raw_api_status(
    policy_db: Path,
    offline_providers: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    apply_changes: bool,
    status: str | None,
    error: str | None,
) -> None:
    import metadata_service as service
    from metadata_provenance import record_manual_metadata
    from shared import get_db
    from routers.api_v1 import api_v1_series_detail

    with get_db() as db:
        db.execute(
            "UPDATE series SET chapter_vol_map=?,chapter_map_source='manual',"
            " metadata_status='degraded',metadata_error='previous aggregate error'"
            " WHERE id=7",
            ('{"1":1,"2":1,"3":2,"4":2}',),
        )
        if status:
            db.execute(
                "INSERT INTO series_metadata_sources"
                " (series_id,source,status,next_retry_at,failure_count,error,details)"
                " VALUES(7,'chapter_map',?,'2099-01-01T00:00:00+00:00',5,?,?)",
                (status, error, '{"preserved_entries":4}'),
            )
    record_manual_metadata(7, {"chapter_vol_map": {"1": 1, "2": 1, "3": 2, "4": 2}})
    if status is None:
        original_due = service.source_retry_due

        def source_disappeared(series_id: int, source: str) -> bool:
            return False if source == "chapter_map" else original_due(series_id, source)

        monkeypatch.setattr(service, "source_retry_due", source_disappeared)
    aggregate = _aggregate_state()
    source = _source_state()
    snapshot = _map_snapshot()

    result = asyncio.run(
        service.refresh_series_metadata(
            7, include_manifest=False, apply_changes=apply_changes
        )
    )

    degraded = status in {"failed", "degraded"}
    expected = "degraded" if degraded else "healthy"
    after = _aggregate_state()
    if apply_changes:
        payload = json.loads(bytes(asyncio.run(api_v1_series_detail(7)).body))
        assert payload["metadataStatus"] == expected
    assert result["status"] == expected
    if status:
        assert result["sources"]["chapter_map"] == status
    else:
        assert "chapter_map" not in result["sources"]
    if degraded:
        assert any(
            "chapter map" in warning and "defer" in warning
            for warning in result["warnings"]
        )
        if error:
            assert any(error in warning for warning in result["warnings"])
    else:
        assert result["warnings"] == []
    if apply_changes:
        assert after["metadata_status"] == expected
        assert bool(after["metadata_error"]) == degraded
    else:
        assert after == aggregate
    assert _source_state() == source
    assert _map_snapshot() == snapshot
    offline_providers.assert_not_awaited()
