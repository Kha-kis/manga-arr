"""Automatic retry policy and escaped chapter-map failure regressions."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def policy_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    import main
    import security
    import shared

    path = tmp_path / "retry-policy.db"
    monkeypatch.setattr(main, "DB_PATH", str(path))
    monkeypatch.setattr(shared, "DB_PATH", str(path))
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(str(tmp_path / "keys"))
    main.init_db()
    with shared.get_db() as db:
        db.execute(
            "INSERT INTO series(id,title,search_pattern,anilist_id,mal_id,mu_id,"
            " mangadex_id,total_volumes,total_chapters,vol_count_source,"
            " status,monitored,metadata_status,update_strategy)"
            " VALUES(7,'Policy Fixture','Policy Fixture',123,456,'789','mdx-1',"
            " 2,4,'manual','RELEASING',1,'healthy','always')"
        )
    try:
        yield path
    finally:
        security._SECRET_CIPHER = None


@pytest.fixture
def offline_providers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    import metadata_enrichment as enrichment
    import metadata_service as service
    import rescan

    record = {
        "anilist_id": 123,
        "mal_id": 456,
        "title": "Policy Fixture",
        "romaji_title": "Policy Fixture",
        "aliases": [],
        "genres": [],
        "cover_url": "https://example.test/cover.jpg",
        "status": "RELEASING",
        "format": "MANGA",
        "volumes": 2,
        "chapters": 4,
        "pub_year": 2020,
        "description": "Fixture description",
        "source": "anilist",
    }
    monkeypatch.setattr(service, "fetch_anilist_by_id", AsyncMock(return_value=record))
    monkeypatch.setattr(service, "fetch_mu_metadata", AsyncMock(return_value=None))
    monkeypatch.setattr(
        service, "refresh_series_cover", AsyncMock(return_value=(True, None))
    )
    provider_map = AsyncMock(
        return_value=enrichment._ChapterMapResult({"1": 1, "2": 1, "3": 2, "4": 2})
    )
    monkeypatch.setattr(enrichment, "_fetch_chapter_volume_map_result", provider_map)
    monkeypatch.setattr(
        enrichment,
        "_fetch_kitsu_chapter_map_result",
        AsyncMock(return_value=enrichment._ChapterMapResult()),
    )
    monkeypatch.setattr(rescan, "_series_library_dir", lambda *_: str(tmp_path))
    return provider_map


def _source_state() -> dict[str, Any] | None:
    from shared import get_db

    with get_db() as db:
        row = db.execute(
            "SELECT * FROM series_metadata_sources"
            " WHERE series_id=7 AND source='chapter_map'"
        ).fetchone()
    return dict(row) if row else None


def _map_snapshot() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from shared import get_db

    with get_db() as db:
        row = db.execute(
            "SELECT chapter_vol_map,chapter_map_source,chapter_map_updated_at,"
            " total_volumes,total_chapters FROM series WHERE id=7"
        ).fetchone()
        fields = db.execute(
            "SELECT * FROM series_metadata_fields WHERE series_id=7"
            " AND field_name='chapter_vol_map'"
        ).fetchall()
    assert row is not None
    return dict(row), [dict(field) for field in fields]


@pytest.mark.parametrize("reason", ["manual", "scheduled", "retry", "preview"])
def test_nonforced_map_refresh_preserves_future_backoff_when_mu_is_due(
    policy_db: Path, offline_providers: AsyncMock, reason: str
) -> None:
    import metadata_service as service
    from metadata_state import metadata_retry_candidates
    from shared import get_db

    with get_db() as db:
        db.execute("UPDATE series SET vol_count_source='anilist' WHERE id=7")
        db.execute(
            "INSERT INTO series_metadata_sources"
            " (series_id,source,status,last_attempt_at,next_retry_at,failure_count,error)"
            " VALUES(7,'chapter_map','failed',datetime('now','-1 hour'),"
            " datetime('now','+1 day'),9,'cached failure')"
        )
        db.execute(
            "INSERT INTO series_metadata_sources(series_id,source,status,next_retry_at)"
            " VALUES(7,'mangaupdates','failed',datetime('now','-1 minute'))"
        )
    before = _source_state()
    snapshot = _map_snapshot()
    assert metadata_retry_candidates() == [7]

    result = asyncio.run(
        service.refresh_series_metadata(
            7,
            force=False,
            include_manifest=False,
            reason=reason,
            apply_changes=reason != "preview",
        )
    )

    assert result["ok"] is True
    assert _source_state() == before
    assert _map_snapshot() == snapshot
    offline_providers.assert_not_awaited()


def test_forced_map_refresh_can_override_future_backoff(
    policy_db: Path, offline_providers: AsyncMock
) -> None:
    import metadata_service as service
    from shared import get_db

    with get_db() as db:
        db.execute(
            "INSERT INTO series_metadata_sources(series_id,source,status,next_retry_at)"
            " VALUES(7,'chapter_map','failed',datetime('now','+1 day'))"
        )
    result = asyncio.run(
        service.refresh_series_metadata(7, force=True, include_manifest=False)
    )
    state = _source_state()
    assert result["ok"] is True
    assert state is not None
    assert state["status"] == "healthy"
    assert state["next_retry_at"] is None
    assert state["failure_count"] == 0
    offline_providers.assert_awaited_once_with("mdx-1")


@pytest.mark.parametrize("pending", [True, False])
def test_automatic_retry_excludes_manual_only_even_for_initial_pending(
    policy_db: Path, pending: bool
) -> None:
    from metadata_state import metadata_retry_candidates
    from shared import get_db

    with get_db() as db:
        db.execute(
            "UPDATE series SET update_strategy='once',metadata_status=? WHERE id=7",
            ("pending" if pending else "degraded",),
        )
        if not pending:
            db.execute(
                "INSERT INTO series_metadata_sources"
                " (series_id,source,status,next_retry_at)"
                " VALUES(7,'chapter_map','degraded',datetime('now','-1 minute'))"
            )
    assert metadata_retry_candidates() == []


@pytest.mark.parametrize("force", [False, True])
def test_direct_initial_refresh_is_allowed_for_manual_only(
    policy_db: Path, offline_providers: AsyncMock, force: bool
) -> None:
    import metadata_service as service
    from shared import get_db

    with get_db() as db:
        db.execute(
            "UPDATE series SET update_strategy='once',metadata_status='pending' WHERE id=7"
        )
    result = asyncio.run(
        service.refresh_series_metadata(7, force=force, include_manifest=False)
    )
    state = _source_state()
    assert result["ok"] is True
    assert state is not None
    assert state["status"] == "healthy"
    offline_providers.assert_awaited_once_with("mdx-1")


@pytest.mark.parametrize(
    ("strategy", "last_success", "expected"),
    [
        ("throttled", "datetime('now','-6 days')", []),
        ("throttled", "datetime('now','-7 days')", [7]),
        ("throttled", "NULL", [7]),
        ("throttled", "'invalid timestamp'", [7]),
        ("always", "datetime('now')", [7]),
        (None, "datetime('now')", [7]),
    ],
)
def test_automatic_retry_honors_existing_weekly_success_throttle(
    policy_db: Path, strategy: str | None, last_success: str, expected: list[int]
) -> None:
    from metadata_state import metadata_retry_candidates
    from shared import get_db

    with get_db() as db:
        db.execute(
            f"UPDATE series SET update_strategy=?,last_metadata_refresh={last_success}"
            " WHERE id=7",
            (strategy,),
        )
        db.execute(
            "INSERT INTO series_metadata_sources(series_id,source,status,next_retry_at)"
            " VALUES(7,'chapter_map','failed',datetime('now','-1 minute'))"
        )
    assert metadata_retry_candidates() == expected


@pytest.mark.parametrize(
    ("cache_source", "raw_cache", "usable_cache"),
    [
        (None, None, False),
        ("legacy", '{"1":1,"2":1,"3":2,"4":2}', True),
        ("manual", '{"1":1,"2":1,"3":2,"4":2}', True),
        ("legacy", "invalid JSON", False),
        ("legacy", "[]", False),
        ("legacy", "{}", False),
    ],
)
@pytest.mark.parametrize("prior_success", [False, True])
def test_escaped_map_exception_records_terminal_retry_preserving_ownership(
    policy_db: Path,
    offline_providers: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    cache_source: str | None,
    raw_cache: str | None,
    usable_cache: bool,
    prior_success: bool,
) -> None:
    import metadata_enrichment as enrichment
    import metadata_service as service
    from metadata_provenance import record_metadata_selections
    from shared import get_db

    if cache_source:
        with get_db() as db:
            db.execute(
                "UPDATE series SET chapter_vol_map=?,chapter_map_source=? WHERE id=7",
                (raw_cache, cache_source),
            )
        if usable_cache:
            record_metadata_selections(
                7,
                {"chapter_vol_map": {"1": 1, "2": 1, "3": 2, "4": 2}},
                {"chapter_vol_map": cache_source},
            )
    old_success = "2026-01-01T00:00:00+00:00" if prior_success else None
    if prior_success:
        with get_db() as db:
            db.execute(
                "INSERT INTO series_metadata_sources"
                " (series_id,source,status,last_success_at,failure_count)"
                " VALUES(7,'chapter_map','degraded',?,2)",
                (old_success,),
            )
    snapshot = _map_snapshot()
    offline_providers.return_value = enrichment._ChapterMapResult()

    def unavailable_directory(_directory: str) -> dict[str, int]:
        raise PermissionError("local map fallback unavailable")

    monkeypatch.setattr(enrichment, "_extract_map_from_cbzs", unavailable_directory)
    result = asyncio.run(
        service.refresh_series_metadata(7, force=False, include_manifest=False)
    )
    state = _source_state()
    assert result["sources"]["chapter_map"] == "failed"
    assert state is not None
    assert state["status"] == (
        "degraded" if usable_cache or prior_success else "failed"
    )
    assert state["failure_count"] == (3 if prior_success else 1)
    assert state["last_success_at"] == old_success
    assert "PermissionError" in state["error"]
    assert datetime.fromisoformat(state["next_retry_at"]) > datetime.now(timezone.utc)
    assert _map_snapshot() == snapshot


def test_map_cancellation_is_not_recorded_as_provider_failure(
    policy_db: Path, offline_providers: AsyncMock
) -> None:
    import metadata_service as service

    offline_providers.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.refresh_series_metadata(7, include_manifest=False))
    state = _source_state()
    assert state is not None
    assert state["status"] == "refreshing"
    assert state["failure_count"] == 0
