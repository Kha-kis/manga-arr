"""Cached identity is not a new MangaDex provider observation."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, tzinfo
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
import zipfile

import httpx
import pytest

from test_metadata_provenance import provenance_db as provenance_db

OLD = "2026-01-01T00:00:00+00:00"
NOW = "2026-01-02T00:00:00+00:00"
MDX = "stored-mdx"
MAP = {"1": 1, "2": 1, "3": 2, "4": 2}


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    import metadata_provenance
    import metadata_state

    value = {"now": OLD}

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> FrozenDatetime:
            instant = cls.fromisoformat(value["now"])
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(metadata_provenance, "_now", lambda: value["now"])
    monkeypatch.setattr(metadata_state, "datetime", FrozenDatetime)
    return value


def seed(*, complete: bool, locked: bool, lookup: bool, resolved: bool = True) -> None:
    import metadata_provenance as provenance
    import metadata_state as state
    from shared import get_db

    with get_db() as db:
        db.execute(
            "UPDATE series SET anilist_id=123,mangadex_id=?,mal_id=?,mu_id=?,"
            " total_volumes=2,total_chapters=4,chapter_vol_map=?,"
            " chapter_map_source='mangadex',chapter_map_updated_at=?,"
            " last_metadata_refresh=?,metadata_status='healthy',"
            " description='Cached description',cover_url='https://example.test/cover.jpg'"
            " WHERE id=7",
            (
                MDX if resolved else None,
                None if lookup else 456,
                None if lookup else "789",
                json.dumps(MAP, sort_keys=True),
                OLD,
                OLD,
            ),
        )
        if complete:
            provenance.backfill_metadata_provenance(db)
            if resolved:
                provenance.record_metadata_candidates(
                    7, "mangadex", {"mangadex_id": MDX}, confidence=0.25, db=db
                )
        if complete or locked:
            provenance.record_metadata_selections(
                7,
                {"mangadex_id": MDX if resolved else None},
                {"mangadex_id": "api"},
                locks={"mangadex_id": locked},
                db=db,
            )
    state.mark_source_success(7, state.SOURCE_CHAPTER_MAP)
    with get_db() as db:
        db.execute(
            "UPDATE series_metadata_sources SET failure_count=2"
            " WHERE series_id=7 AND source='chapter_map'"
        )


def snapshot() -> dict[str, Any]:
    from shared import get_db

    with get_db() as db:
        series = db.execute("SELECT * FROM series WHERE id=7").fetchone()
        assert series is not None
        return {
            "series": dict(series),
            "fields": [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM series_metadata_fields ORDER BY series_id,field_name"
                )
            ],
            "candidates": [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM series_metadata_candidates ORDER BY series_id,field_name,source"
                )
            ],
            "volumes": [
                dict(row) for row in db.execute("SELECT * FROM volumes ORDER BY id")
            ],
            "chapters": [
                dict(row) for row in db.execute("SELECT * FROM chapters ORDER BY id")
            ],
        }


def candidate(
    field: str, value: Any, source: str = "mangadex", confidence: float = 1.0
) -> dict[str, Any]:
    return {
        "series_id": 7,
        "field_name": field,
        "source": source,
        "value_json": json.dumps(value, sort_keys=True, separators=(",", ":")),
        "confidence": confidence,
        "fetched_at": NOW,
    }


def selection(field: str, value: Any, source: str = "mangadex") -> dict[str, Any]:
    return {
        "series_id": 7,
        "field_name": field,
        "value_json": json.dumps(value, sort_keys=True, separators=(",", ":")),
        "selected_source": source,
        "locked": 0,
        "selected_at": NOW,
    }


def replaced(
    rows: list[dict[str, Any]],
    updates: list[dict[str, Any]],
    *,
    candidates: bool = False,
) -> list[dict[str, Any]]:
    keys = (
        ("series_id", "field_name", "source")
        if candidates
        else ("series_id", "field_name")
    )
    result = {tuple(row[key] for key in keys): row for row in [*rows, *updates]}
    return [result[key] for key in sorted(result)]


def detail_payload() -> dict[str, Any]:
    return {
        "result": "ok",
        "data": {
            "id": MDX,
            "type": "manga",
            "attributes": {
                "title": {"en": "Existing Title"},
                "links": {"al": "123", "mal": "900", "mu": "z"},
            },
        },
    }


def transport(
    monkeypatch: pytest.MonkeyPatch,
    library: Path,
    *,
    detail: str = "timeout",
    aggregate: str = "failure",
    fallback: str | None = None,
) -> None:
    import rescan

    library.mkdir()
    if fallback == "cbz":
        with zipfile.ZipFile(library / "cached.cbz", "w") as archive:
            for chapter, volume in MAP.items():
                archive.writestr(
                    f"Title - c{chapter} (v{volume}) - p1.jpg", b"local page"
                )
    monkeypatch.setattr(rescan, "_series_library_dir", lambda *_: str(library))
    original = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.mangadex.org":
            if request.url.path.endswith("/aggregate"):
                if aggregate == "failure":
                    return httpx.Response(503, json={"result": "error"})
                mapping = (
                    MAP
                    if aggregate == "usable"
                    else {"1": 1}
                    if aggregate == "sparse"
                    else {}
                )
                volumes: dict[str, Any] = {}
                for chapter, volume in mapping.items():
                    volumes.setdefault(str(volume), {"chapters": {}})["chapters"][
                        chapter
                    ] = {}
                return httpx.Response(200, json={"result": "ok", "volumes": volumes})
            payload = detail_payload()
            if request.url.path == "/manga":
                if detail == "unresolved":
                    return httpx.Response(503, json={"result": "error"})
                return httpx.Response(200, json={"data": [payload["data"]]})
            assert request.url.path == f"/manga/{MDX}"
            if detail == "timeout":
                raise httpx.ReadTimeout("offline detail", request=request)
            if detail == "invalid_json":
                return httpx.Response(200, content=b"not json")
            if detail == "http_error":
                return httpx.Response(503, json=payload)
            if detail == "wrong_id":
                payload["data"]["id"] = "another-mdx"
            elif detail == "wrong_type":
                payload["data"]["type"] = "chapter"
            elif detail == "missing_type":
                payload["data"].pop("type")
            elif detail == "error_payload":
                payload["result"] = "error"
            elif detail == "malformed_result":
                payload["result"] = []
            elif detail == "malformed_data":
                payload["data"] = []
            elif detail == "malformed_attributes":
                payload["data"]["attributes"] = []
            elif detail == "malformed_links":
                payload["data"]["attributes"]["links"] = ["mal", "900"]
            elif detail == "malformed_link_values":
                payload["data"]["attributes"]["links"]["mu"] = {"id": "z"}
            elif detail == "malformed_root":
                return httpx.Response(200, json=[])
            elif detail == "empty_links":
                payload["data"]["attributes"]["links"] = {}
            elif detail == "null_links":
                payload["data"]["attributes"]["links"] = None
            elif detail == "missing_links":
                payload["data"]["attributes"].pop("links")
            return httpx.Response(200, json=payload)
        assert request.url.host == "kitsu.io"
        if fallback == "empty":
            return httpx.Response(200, json={"data": []})
        if fallback != "kitsu":
            return httpx.Response(503, json={"error": "offline"})
        if request.url.path.endswith("/manga"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "7", "attributes": {"canonicalTitle": "Existing Title"}}
                    ]
                },
            )
        assert request.url.path.endswith("/chapters")
        return httpx.Response(
            200,
            json={
                "data": [
                    {"attributes": {"number": chapter, "volumeNumber": volume}}
                    for chapter, volume in MAP.items()
                ],
                "links": {},
            },
        )

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return original(*args, **kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "AsyncClient", client)


@pytest.mark.parametrize("complete", [False, True])
@pytest.mark.parametrize("locked", [False, True])
@pytest.mark.parametrize("apply_changes", [False, True])
@pytest.mark.parametrize("lookup", [False, True])
def test_failed_stored_identity_keeps_full_provenance(
    provenance_db: Path,
    clock: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    complete: bool,
    locked: bool,
    apply_changes: bool,
    lookup: bool,
) -> None:
    import metadata_enrichment as enrichment
    import metadata_state as state

    seed(complete=complete, locked=locked, lookup=lookup)
    before = snapshot()
    transport(monkeypatch, tmp_path / "library")
    clock["now"] = NOW
    assert (
        asyncio.run(enrichment.refresh_mangadex_map(7, apply_changes=apply_changes))
        is False
    )
    assert snapshot() == before
    source = state.get_source_states(7)[0]
    assert source["status"] == "degraded"
    assert source["last_success_at"] == OLD
    assert source["failure_count"] == 3
    assert (
        source["next_retry_at"]
        == (datetime.fromisoformat(NOW) + timedelta(minutes=20)).isoformat()
    )


@pytest.mark.parametrize(
    "detail",
    [
        "http_error",
        "wrong_id",
        "wrong_type",
        "missing_type",
        "error_payload",
        "malformed_result",
        "malformed_root",
        "malformed_data",
        "malformed_attributes",
        "malformed_links",
        "malformed_link_values",
        "invalid_json",
    ],
)
@pytest.mark.parametrize("aggregate", ["failure", "empty"])
@pytest.mark.parametrize("apply_changes", [False, True])
def test_unverified_detail_links_are_never_observations(
    provenance_db: Path,
    clock: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    detail: str,
    aggregate: str,
    apply_changes: bool,
) -> None:
    import metadata_enrichment as enrichment

    seed(complete=True, locked=False, lookup=True)
    before = snapshot()
    transport(monkeypatch, tmp_path / "library", detail=detail, aggregate=aggregate)
    clock["now"] = NOW
    assert (
        asyncio.run(enrichment.refresh_mangadex_map(7, apply_changes=apply_changes))
        is False
    )
    after = snapshot()
    expected = deepcopy(before)
    if aggregate == "empty":
        expected["candidates"] = replaced(
            before["candidates"], [candidate("mangadex_id", MDX)], candidates=True
        )
        if apply_changes:
            expected["fields"] = replaced(
                before["fields"], [selection("mangadex_id", MDX)]
            )
    assert after == expected


@pytest.mark.parametrize(
    "evidence",
    [
        "resolution",
        "detail",
        "empty_links",
        "null_links",
        "missing_links",
        "aggregate_empty",
        "aggregate_sparse",
        "aggregate_usable",
    ],
)
@pytest.mark.parametrize("locked", [False, True])
@pytest.mark.parametrize("apply_changes", [False, True])
def test_fresh_mangadex_evidence_records_identity_with_existing_locks(
    provenance_db: Path,
    clock: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    evidence: str,
    locked: bool,
    apply_changes: bool,
) -> None:
    import metadata_enrichment as enrichment
    import metadata_state as state

    identity_lookup = not evidence.startswith("aggregate_")
    seed(
        complete=True,
        locked=locked,
        lookup=identity_lookup,
        resolved=evidence != "resolution",
    )
    before = snapshot()
    aggregate = (
        evidence.removeprefix("aggregate_") if not identity_lookup else "failure"
    )
    transport(
        monkeypatch,
        tmp_path / "library",
        detail=evidence,
        aggregate=aggregate,
        fallback="empty" if aggregate in {"empty", "sparse"} else None,
    )
    clock["now"] = NOW
    usable = evidence == "aggregate_usable"
    assert (
        asyncio.run(enrichment.refresh_mangadex_map(7, apply_changes=apply_changes))
        is usable
    )
    after = snapshot()
    observations = [candidate("mangadex_id", MDX)]
    selections = [] if locked or not apply_changes else [selection("mangadex_id", MDX)]
    expected_series = deepcopy(before["series"])
    if apply_changes and not locked:
        expected_series["mangadex_id"] = MDX
    if evidence in {"resolution", "detail"}:
        observations += [candidate("mal_id", 900), candidate("mu_id", "35")]
        if apply_changes:
            selections += [selection("mal_id", 900), selection("mu_id", "35")]
            expected_series.update(mal_id=900, mu_id="35")
    if usable:
        observations.append(candidate("chapter_vol_map", MAP))
        if apply_changes:
            selections.append(selection("chapter_vol_map", MAP))
            expected_series["chapter_map_updated_at"] = NOW
    assert after["series"] == expected_series
    assert after["fields"] == replaced(before["fields"], selections)
    assert after["candidates"] == replaced(
        before["candidates"], observations, candidates=True
    )
    source = state.get_source_states(7)[0]
    assert source["last_success_at"] == (OLD if identity_lookup else NOW)
    assert source["failure_count"] == (3 if identity_lookup else 0)
    assert bool(source["next_retry_at"]) is identity_lookup


@pytest.mark.parametrize("complete", [False, True])
@pytest.mark.parametrize("apply_changes", [False, True])
def test_failed_identity_resolution_creates_no_provenance(
    provenance_db: Path,
    clock: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    complete: bool,
    apply_changes: bool,
) -> None:
    import metadata_enrichment as enrichment

    seed(complete=complete, locked=False, lookup=True, resolved=False)
    before = snapshot()
    transport(monkeypatch, tmp_path / "library", detail="unresolved")
    clock["now"] = NOW
    assert (
        asyncio.run(enrichment.refresh_mangadex_map(7, apply_changes=apply_changes))
        is False
    )
    assert snapshot() == before


@pytest.mark.parametrize("fallback", ["kitsu", "cbz"])
@pytest.mark.parametrize("lookup", [False, True])
@pytest.mark.parametrize("locked", [False, True])
@pytest.mark.parametrize("apply_changes", [False, True])
def test_fallback_records_only_its_own_map_not_cached_mangadex_identity(
    provenance_db: Path,
    clock: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fallback: str,
    lookup: bool,
    locked: bool,
    apply_changes: bool,
) -> None:
    import metadata_enrichment as enrichment
    import metadata_state as state

    seed(complete=True, locked=locked, lookup=lookup)
    before = snapshot()
    transport(monkeypatch, tmp_path / "library", fallback=fallback)
    clock["now"] = NOW
    assert (
        asyncio.run(enrichment.refresh_mangadex_map(7, apply_changes=apply_changes))
        is True
    )
    after = snapshot()
    expected_series = deepcopy(before["series"])
    if apply_changes:
        expected_series.update(chapter_map_source=fallback, chapter_map_updated_at=NOW)
    assert after["series"] == expected_series
    assert after["fields"] == replaced(
        before["fields"],
        [selection("chapter_vol_map", MAP, fallback)] if apply_changes else [],
    )
    assert after["candidates"] == replaced(
        before["candidates"],
        [candidate("chapter_vol_map", MAP, fallback, 0.8)],
        candidates=True,
    )
    source = state.get_source_states(7)[0]
    assert source["status"] == "healthy"
    assert source["last_success_at"] == NOW
    assert source["failure_count"] == 0
    assert source["next_retry_at"] is None


def test_real_refresh_total_failure_preserves_complete_cache_and_provenance(
    provenance_db: Path,
    clock: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import metadata_enrichment as enrichment
    from metadata import MetadataProviderError
    import metadata_provenance as provenance
    import metadata_service as service
    import metadata_state as state
    from shared import get_db

    seed(complete=True, locked=False, lookup=True)
    with get_db() as db:
        db.execute("UPDATE series SET vol_count_source='anilist' WHERE id=7")
        provenance.record_metadata_selections(
            7, {"total_volumes": 2}, {"total_volumes": "anilist"}, db=db
        )
        provenance.record_manual_metadata(
            7,
            {
                "title": "Existing Title",
                "description": "Cached description",
                "chapter_vol_map": MAP,
            },
            db=db,
        )
        db.execute("UPDATE series SET chapter_map_source='manual' WHERE id=7")
    for source in (state.SOURCE_ANILIST, state.SOURCE_MANGAUPDATES, state.SOURCE_COVER):
        state.mark_source_success(7, source)
    with get_db() as db:
        db.execute(
            "UPDATE series_metadata_sources SET failure_count=2 WHERE series_id=7"
        )
    before = snapshot()
    transport(monkeypatch, tmp_path / "library")
    monkeypatch.setattr(
        service,
        "fetch_anilist_by_id",
        AsyncMock(side_effect=MetadataProviderError("AniList offline")),
    )
    monkeypatch.setattr(
        enrichment,
        "mu_search",
        AsyncMock(side_effect=MetadataProviderError("MangaUpdates offline")),
    )
    monkeypatch.setattr(
        service,
        "download_cover",
        AsyncMock(
            return_value={"ok": False, "status": "failed", "error": "cover offline"}
        ),
    )
    monkeypatch.setattr(service, "COVERS_DIR", str(tmp_path / "covers"))
    clock["now"] = NOW
    result = asyncio.run(
        service.refresh_series_metadata(7, force=True, include_manifest=False)
    )
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["changed_fields"] == []
    after = snapshot()
    expected_series = deepcopy(before["series"])
    expected_series.update(
        metadata_status="failed",
        metadata_last_attempt=NOW,
        metadata_error=after["series"]["metadata_error"],
    )
    assert "AniList offline" in expected_series["metadata_error"]
    assert "MangaUpdates offline" in expected_series["metadata_error"]
    assert "cover offline" in expected_series["metadata_error"]
    assert after == {**before, "series": expected_series}
    assert after["series"]["last_metadata_refresh"] == OLD
    sources = state.get_source_states(7)
    assert {row["source"] for row in sources} == {
        "anilist",
        "mangaupdates",
        "chapter_map",
        "cover",
    }
    for row in sources:
        assert row["status"] == "degraded"
        assert row["last_attempt_at"] == NOW
        assert row["last_success_at"] == OLD
        assert row["failure_count"] == 3
        assert (
            row["next_retry_at"]
            == (datetime.fromisoformat(NOW) + timedelta(minutes=20)).isoformat()
        )
        assert row["error"]
