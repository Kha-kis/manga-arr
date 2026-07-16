"""Field-level metadata provenance and candidate selection coverage."""

from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def provenance_db(tmp_path, monkeypatch):
    import main
    import security
    import shared

    db_path = tmp_path / "metadata-provenance.db"
    monkeypatch.setattr(main, "DB_PATH", str(db_path))
    monkeypatch.setattr(shared, "DB_PATH", str(db_path))
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(str(tmp_path / "keys"))
    main.init_db()
    main.load_config()
    main.ensure_api_key()
    with shared.get_db() as db:
        db.execute(
            "INSERT INTO series(id,title,search_pattern,total_volumes,"
            " vol_count_source,total_chapters,chapter_count_source)"
            " VALUES(7,'Existing Title','Existing Title',12,'manual',90,'anilist')"
        )
    yield db_path
    security._SECRET_CIPHER = None


def _state(series_id: int, field_name: str) -> dict:
    from metadata_provenance import get_metadata_field_states

    return next(
        item
        for item in get_metadata_field_states(series_id)
        if item["field_name"] == field_name
    )


def test_backfill_preserves_manual_ownership_and_cascades(provenance_db):
    import shared
    from metadata_provenance import backfill_metadata_provenance

    with shared.get_db() as db:
        backfill_metadata_provenance(db)

    volume_state = _state(7, "total_volumes")
    assert volume_state["value"] == 12
    assert volume_state["selected_source"] == "manual"
    assert volume_state["locked"] is True
    assert volume_state["candidates"][0]["source"] == "manual"

    with shared.get_db() as db:
        db.execute("DELETE FROM series WHERE id=7")
        fields = db.execute(
            "SELECT COUNT(*) FROM series_metadata_fields WHERE series_id=7"
        ).fetchone()[0]
        candidates = db.execute(
            "SELECT COUNT(*) FROM series_metadata_candidates WHERE series_id=7"
        ).fetchone()[0]
    assert fields == 0
    assert candidates == 0


def test_unlock_allows_provider_candidate_to_replace_manual_value(provenance_db):
    import shared
    from metadata_provenance import (
        backfill_metadata_provenance,
        record_metadata_candidates,
        set_metadata_field_lock,
    )

    with shared.get_db() as db:
        backfill_metadata_provenance(db)
    record_metadata_candidates(7, "mangaupdates", {"total_volumes": 14})

    locked = _state(7, "total_volumes")
    assert locked["pending"] is False
    set_metadata_field_lock(7, "total_volumes", False)
    unlocked = _state(7, "total_volumes")
    assert unlocked["locked"] is False
    assert unlocked["recommended"]["source"] == "mangaupdates"
    assert unlocked["pending"] is True
    assert unlocked["conflict"] is False


def test_candidate_apply_guards_decreases_and_records_selection(provenance_db):
    import shared
    from metadata_provenance import (
        apply_metadata_candidate,
        backfill_metadata_provenance,
        record_metadata_candidates,
        set_metadata_field_lock,
    )

    with shared.get_db() as db:
        backfill_metadata_provenance(db)
    set_metadata_field_lock(7, "total_volumes", False)
    record_metadata_candidates(7, "anilist", {"total_volumes": 10})

    with pytest.raises(ValueError, match="explicit confirmation"):
        apply_metadata_candidate(7, "total_volumes", "anilist")
    result = apply_metadata_candidate(
        7, "total_volumes", "anilist", allow_decrease=True
    )

    assert result == {
        "field_name": "total_volumes",
        "source": "anilist",
        "value": 10,
    }
    with sqlite3.connect(provenance_db) as db:
        db.row_factory = sqlite3.Row
        series = db.execute(
            "SELECT total_volumes,vol_count_source FROM series WHERE id=7"
        ).fetchone()
        selected = db.execute(
            "SELECT selected_source,locked FROM series_metadata_fields"
            " WHERE series_id=7 AND field_name='total_volumes'"
        ).fetchone()
    assert dict(series) == {"total_volumes": 10, "vol_count_source": "anilist"}
    assert dict(selected) == {"selected_source": "anilist", "locked": 0}


def test_safe_apply_skips_provider_conflicts(provenance_db):
    import shared
    from metadata_provenance import (
        apply_recommended_candidates,
        backfill_metadata_provenance,
        record_metadata_candidates,
        set_metadata_field_lock,
    )

    with shared.get_db() as db:
        backfill_metadata_provenance(db)
    set_metadata_field_lock(7, "total_volumes", False)
    record_metadata_candidates(7, "anilist", {"total_volumes": 13})
    record_metadata_candidates(7, "mangaupdates", {"total_volumes": 14})

    state = _state(7, "total_volumes")
    assert state["conflict"] is True
    result = apply_recommended_candidates(7)
    assert result["applied"] == []
    assert result["skipped"] == [{"field_name": "total_volumes", "reason": "conflict"}]


def test_preview_refresh_records_candidates_without_mutating_series(provenance_db):
    import metadata_service as service

    record = {
        "anilist_id": 123,
        "mal_id": 456,
        "title": "Existing Title",
        "romaji_title": "Existing Title Romaji",
        "aliases": ["Provider Alternate"],
        "genres": ["Drama"],
        "cover_url": "https://example.test/cover.jpg",
        "status": "FINISHED",
        "volumes": 14,
        "chapters": 100,
        "pub_year": 2024,
        "description": "Provider description",
    }
    cover = AsyncMock(return_value=(True, None))
    with (
        patch.object(service, "anilist_search", AsyncMock(return_value=[record])),
        patch.object(service, "fetch_mu_metadata", AsyncMock(return_value=None)),
        patch.object(service, "refresh_mangadex_map", AsyncMock(return_value=True)),
        patch.object(service, "refresh_series_cover", cover),
    ):
        result = asyncio.run(
            service.refresh_series_metadata(
                7,
                force=True,
                include_manifest=False,
                reason="preview",
                apply_changes=False,
            )
        )

    assert result["ok"] is True, result
    assert result["applied"] is False
    cover.assert_not_awaited()
    with sqlite3.connect(provenance_db) as db:
        db.row_factory = sqlite3.Row
        series = db.execute(
            "SELECT title,anilist_id,total_volumes,total_chapters,description,"
            " metadata_status,last_metadata_refresh,metadata_last_attempt"
            " FROM series WHERE id=7"
        ).fetchone()
        candidate = db.execute(
            "SELECT value_json FROM series_metadata_candidates"
            " WHERE series_id=7 AND field_name='description' AND source='anilist'"
        ).fetchone()
        aliases = db.execute(
            "SELECT COUNT(*) FROM series_aliases WHERE series_id=7"
        ).fetchone()[0]
    assert dict(series) == {
        "title": "Existing Title",
        "anilist_id": None,
        "total_volumes": 12,
        "total_chapters": 90,
        "description": None,
        "metadata_status": "pending",
        "last_metadata_refresh": None,
        "metadata_last_attempt": None,
    }
    assert candidate["value_json"] == '"Provider description"'
    assert aliases == 0


def test_series_detail_and_htmx_route_render_source_panel(provenance_db):
    import main
    import shared
    from fastapi.testclient import TestClient
    from metadata_provenance import (
        backfill_metadata_provenance,
        record_metadata_candidates,
    )

    with shared.get_db() as db:
        backfill_metadata_provenance(db)
    record_metadata_candidates(7, "mangaupdates", {"total_volumes": 14})

    with TestClient(main.app) as client:
        detail = client.get("/series/7")
        partial = client.get(
            "/api/series/7/metadata-sources",
            headers={
                "X-Api-Key": main.get_cfg("api_key"),
                "HX-Request": "true",
            },
        )

    assert detail.status_code == 200
    assert 'id="metadata-sources-panel"' in detail.text
    assert 'hx-post="/series/7/metadata/preview"' in detail.text
    assert partial.status_code == 200
    assert "MangaUpdates" in partial.text


def test_htmx_unlock_and_accept_candidate_routes(provenance_db):
    import main
    import shared
    from fastapi.testclient import TestClient
    from metadata_provenance import (
        backfill_metadata_provenance,
        record_metadata_candidates,
    )

    with shared.get_db() as db:
        backfill_metadata_provenance(db)
    record_metadata_candidates(7, "mangaupdates", {"total_volumes": 14})
    token = "csrf-metadata-" + "x" * 32
    headers = {"HX-Request": "true", "X-CSRFToken": token}

    with TestClient(main.app) as client:
        unlocked = client.post(
            "/series/7/metadata/lock",
            data={"field_name": "total_volumes", "locked": "0"},
            headers=headers,
            cookies={"csrftoken": token},
        )
        applied = client.post(
            "/series/7/metadata/apply-candidate",
            data={
                "field_name": "total_volumes",
                "source": "mangaupdates",
                "allow_decrease": "0",
            },
            headers=headers,
            cookies={"csrftoken": token},
        )

    assert unlocked.status_code == 200
    assert "Total Volumes unlocked" in unlocked.headers["HX-Trigger"]
    assert applied.status_code == 200
    assert "Selected mangaupdates" in applied.headers["HX-Trigger"]
    with sqlite3.connect(provenance_db) as db:
        row = db.execute(
            "SELECT total_volumes,vol_count_source FROM series WHERE id=7"
        ).fetchone()
    assert row == (14, "mangaupdates")


def test_plain_preview_route_uses_candidate_only_refresh(provenance_db):
    import main
    import metadata_service
    from fastapi.testclient import TestClient

    token = "csrf-preview-" + "x" * 32
    refresh = AsyncMock(return_value={"ok": True, "errors": []})
    with (
        patch.object(metadata_service, "refresh_series_metadata", refresh),
        TestClient(main.app) as client,
    ):
        response = client.post(
            "/series/7/metadata/preview",
            headers={"X-CSRFToken": token},
            cookies={"csrftoken": token},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/series/7?")
    refresh.assert_awaited_once_with(
        7,
        force=True,
        include_manifest=False,
        reason="preview",
        apply_changes=False,
    )


def test_anilist_apply_preserves_locked_core_field(provenance_db):
    import metadata_service
    from metadata_provenance import record_manual_metadata

    with sqlite3.connect(provenance_db) as db:
        db.execute("UPDATE series SET description='Operator description' WHERE id=7")
    record_manual_metadata(7, {"description": "Operator description"})
    record = {
        "anilist_id": 123,
        "mal_id": None,
        "cover_url": None,
        "status": "RELEASING",
        "description": "Provider description",
        "pub_year": None,
        "volumes": 12,
        "chapters": 90,
    }
    metadata_service._apply_anilist_record(7, record)

    with sqlite3.connect(provenance_db) as db:
        description = db.execute(
            "SELECT description FROM series WHERE id=7"
        ).fetchone()[0]
        candidate = db.execute(
            "SELECT value_json FROM series_metadata_candidates"
            " WHERE series_id=7 AND field_name='description' AND source='anilist'"
        ).fetchone()[0]
    assert description == "Operator description"
    assert candidate == '"Provider description"'


def test_selected_value_does_not_overwrite_fresh_provider_candidate(provenance_db):
    import metadata_service

    with sqlite3.connect(provenance_db) as db:
        db.execute("UPDATE series SET vol_count_source='anilist' WHERE id=7")
    record = {
        "anilist_id": 123,
        "mal_id": None,
        "cover_url": None,
        "status": "RELEASING",
        "description": None,
        "pub_year": None,
        "volumes": 10,
        "chapters": 90,
    }
    metadata_service._apply_anilist_record(7, record)

    with sqlite3.connect(provenance_db) as db:
        db.row_factory = sqlite3.Row
        selected = db.execute(
            "SELECT value_json,selected_source FROM series_metadata_fields"
            " WHERE series_id=7 AND field_name='total_volumes'"
        ).fetchone()
        candidate = db.execute(
            "SELECT value_json FROM series_metadata_candidates"
            " WHERE series_id=7 AND field_name='total_volumes' AND source='anilist'"
        ).fetchone()
    assert dict(selected) == {"value_json": "12", "selected_source": "anilist"}
    assert candidate["value_json"] == "10"


def test_mangaupdates_records_but_does_not_apply_locked_fields(provenance_db):
    import metadata_enrichment
    from metadata_provenance import set_metadata_field_lock

    with sqlite3.connect(provenance_db) as db:
        db.execute("UPDATE series SET vol_count_source='anilist',mu_id=NULL WHERE id=7")
    set_metadata_field_lock(7, "total_volumes", True)
    set_metadata_field_lock(7, "mu_id", True)
    with patch.object(
        metadata_enrichment,
        "mu_search",
        AsyncMock(
            return_value=[
                {
                    "title": "Existing Title",
                    "mu_id": "provider-id",
                    "volumes": 14,
                }
            ]
        ),
    ):
        result = asyncio.run(metadata_enrichment.fetch_mu_metadata(7, "Existing Title"))

    assert result["updated_vols"] is False
    with sqlite3.connect(provenance_db) as db:
        db.row_factory = sqlite3.Row
        series = db.execute(
            "SELECT mu_id,total_volumes FROM series WHERE id=7"
        ).fetchone()
        candidates = db.execute(
            "SELECT field_name,value_json FROM series_metadata_candidates"
            " WHERE series_id=7 AND source='mangaupdates' ORDER BY field_name"
        ).fetchall()
    assert dict(series) == {"mu_id": None, "total_volumes": 12}
    assert [tuple(row) for row in candidates] == [
        ("mu_id", '"provider-id"'),
        ("total_volumes", "14"),
    ]


def test_mangadex_records_but_does_not_apply_locked_map_or_id(provenance_db):
    import metadata_enrichment
    from metadata_provenance import record_manual_metadata

    original_map = {"1": 1}
    record_manual_metadata(
        7,
        {"mangadex_id": None, "chapter_vol_map": original_map},
    )
    with sqlite3.connect(provenance_db) as db:
        db.execute(
            "UPDATE series SET chapter_vol_map=?,chapter_map_source='manual' WHERE id=7",
            ('{"1":1}',),
        )
    provider_map = {"1": 1, "2": 1}
    with (
        patch.object(
            metadata_enrichment,
            "fetch_mangadex_id",
            AsyncMock(return_value=("provider-mdx", {})),
        ),
        patch.object(
            metadata_enrichment,
            "fetch_chapter_volume_map",
            AsyncMock(return_value=provider_map),
        ),
        patch.object(
            metadata_enrichment,
            "_validate_chapter_map",
            return_value=True,
        ),
    ):
        result = asyncio.run(metadata_enrichment.refresh_mangadex_map(7))

    assert result is True
    with sqlite3.connect(provenance_db) as db:
        db.row_factory = sqlite3.Row
        series = db.execute(
            "SELECT mangadex_id,chapter_vol_map,chapter_map_source"
            " FROM series WHERE id=7"
        ).fetchone()
        candidates = db.execute(
            "SELECT field_name,source FROM series_metadata_candidates"
            " WHERE series_id=7 AND source='mangadex' ORDER BY field_name"
        ).fetchall()
    assert dict(series) == {
        "mangadex_id": None,
        "chapter_vol_map": '{"1":1}',
        "chapter_map_source": "manual",
    }
    assert [tuple(row) for row in candidates] == [
        ("chapter_vol_map", "mangadex"),
        ("mangadex_id", "mangadex"),
    ]
