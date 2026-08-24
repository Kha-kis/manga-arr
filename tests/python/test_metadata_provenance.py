"""Field-level metadata provenance and candidate selection coverage."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from contextlib import contextmanager
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


def _record_selection(
    field_name: str,
    value,
    source: str,
    *,
    locked: bool = False,
) -> None:
    from metadata_provenance import record_metadata_selections

    record_metadata_selections(
        7,
        {field_name: value},
        {field_name: source},
        locks={field_name: locked},
    )


def _audit_series_column_updates(db_path, column: str) -> None:
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE series_update_audit"
            "(column_name TEXT NOT NULL, observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        db.execute(
            f"CREATE TRIGGER audit_{column}_updates AFTER UPDATE OF {column} ON series "
            "BEGIN INSERT INTO series_update_audit(column_name) "
            f"VALUES('{column}'); END"
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
    assert volume_state["alternative_count"] == 0

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
    assert locked["alternative_count"] == 1
    set_metadata_field_lock(7, "total_volumes", False)
    unlocked = _state(7, "total_volumes")
    assert unlocked["locked"] is False
    assert unlocked["recommended"]["source"] == "mangaupdates"
    assert unlocked["pending"] is True
    assert unlocked["conflict"] is False


def test_manual_title_unlock_behavior_remains_compatible(provenance_db):
    from metadata_provenance import (
        record_manual_metadata,
        record_metadata_candidates,
        set_metadata_field_lock,
    )

    record_manual_metadata(7, {"title": "Existing Title"})
    record_metadata_candidates(7, "anilist", {"title": "Provider Title"})

    set_metadata_field_lock(7, "title", False)
    state = _state(7, "title")
    assert state["selected_source"] == "manual"
    assert state["locked"] is False
    assert state["recommended"]["source"] == "anilist"
    assert state["pending"] is True
    assert state["conflict"] is False
    assert {candidate["source"] for candidate in state["candidates"]} == {
        "anilist",
        "manual",
    }


def test_unlocked_local_count_candidates_keep_global_priority(provenance_db):
    from metadata_provenance import (
        record_metadata_candidates,
        record_metadata_selections,
    )

    with sqlite3.connect(provenance_db) as db:
        db.execute(
            "UPDATE series SET vol_count_source='local',"
            " chapter_count_source='local' WHERE id=7"
        )
    record_metadata_selections(
        7,
        {"total_volumes": 12, "total_chapters": 90},
        {"total_volumes": "local", "total_chapters": "local"},
        locks={"total_volumes": False, "total_chapters": False},
    )
    record_metadata_candidates(
        7,
        "local",
        {"total_volumes": 12, "total_chapters": 90},
    )
    record_metadata_candidates(
        7,
        "anilist",
        {"total_volumes": 14, "total_chapters": 100},
    )

    for field_name in ("total_volumes", "total_chapters"):
        state = _state(7, field_name)
        assert state["selected_source"] == "local"
        assert state["locked"] is False
        assert state["recommended"]["source"] == "local"
        assert state["recommended"]["is_current"] is True
        assert state["pending"] is False
        assert state["conflict"] is True


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


def test_api_title_equal_provider_reports_and_safely_reconciles_source_drift(
    provenance_db,
):
    import shared
    from metadata_provenance import (
        apply_recommended_candidates,
        build_metadata_repair_report,
        record_initial_title,
        record_metadata_candidates,
    )

    with shared.get_db() as db:
        record_initial_title(
            7,
            "Existing Title",
            "api",
            locked=False,
            db=db,
        )
        db.execute(
            "UPDATE series_metadata_fields SET selected_at='2000-01-01T00:00:00+00:00'"
            " WHERE series_id=7 AND field_name='title'"
        )
    record_metadata_candidates(7, "anilist", {"title": "Existing Title"})
    _audit_series_column_updates(provenance_db, "title")

    state = _state(7, "title")
    report = build_metadata_repair_report(7)

    assert state["pending"] is False
    assert state["recommended"]["source"] == "anilist"

    result = apply_recommended_candidates(7)
    reconciled_state = _state(7, "title")

    assert reconciled_state["selected_source"] == "anilist"
    assert state["source_drift"] is True
    assert report["pending_count"] == 0
    assert report["source_drift_count"] == 1
    assert result == {
        "applied": [],
        "reconciled": [
            {
                "field_name": "title",
                "source": "anilist",
                "value": "Existing Title",
            }
        ],
        "skipped": [],
    }
    with sqlite3.connect(provenance_db) as db:
        db.row_factory = sqlite3.Row
        series = db.execute("SELECT title FROM series WHERE id=7").fetchone()
        selected = db.execute(
            "SELECT value_json,selected_source,locked,selected_at"
            " FROM series_metadata_fields"
            " WHERE series_id=7 AND field_name='title'"
        ).fetchone()
        value_writes = db.execute(
            "SELECT COUNT(*) FROM series_update_audit WHERE column_name='title'"
        ).fetchone()[0]
    assert series["title"] == "Existing Title"
    assert dict(selected) == {
        "value_json": '"Existing Title"',
        "selected_source": "anilist",
        "locked": 0,
        "selected_at": selected["selected_at"],
    }
    assert selected["selected_at"] != "2000-01-01T00:00:00+00:00"
    assert value_writes == 0


def test_equal_provider_value_reconciles_stale_legacy_ownership(provenance_db):
    from metadata_provenance import (
        apply_recommended_candidates,
        record_metadata_candidates,
    )

    _record_selection("title", "Existing Title", "legacy")
    record_metadata_candidates(7, "mangaupdates", {"title": "Existing Title"})

    result = apply_recommended_candidates(7)

    assert result["applied"] == []
    assert result["reconciled"] == [
        {
            "field_name": "title",
            "source": "mangaupdates",
            "value": "Existing Title",
        }
    ]
    assert _state(7, "title")["selected_source"] == "mangaupdates"


@pytest.mark.parametrize(
    (
        "selected_source",
        "locked",
        "candidates",
        "recommended_source",
        "conflict",
    ),
    [
        pytest.param(
            "api",
            True,
            [("anilist", "Existing Title")],
            "anilist",
            False,
            id="locked",
        ),
        pytest.param(
            "legacy",
            False,
            [
                ("mangaupdates", "Existing Title"),
                ("anilist", "Different Title"),
            ],
            "mangaupdates",
            True,
            id="provider-conflict",
        ),
        pytest.param(
            "api",
            False,
            [("manual", "Existing Title")],
            "manual",
            False,
            id="unlocked-manual-relinquishment",
        ),
        pytest.param(
            "api",
            False,
            [("local", "Existing Title")],
            "local",
            False,
            id="unlocked-local-title-relinquishment",
        ),
    ],
)
def test_ineligible_equal_value_candidate_does_not_reconcile_source_drift(
    provenance_db,
    selected_source,
    locked,
    candidates,
    recommended_source,
    conflict,
):
    from metadata_provenance import (
        apply_recommended_candidates,
        record_metadata_candidates,
    )

    _record_selection("title", "Existing Title", selected_source, locked=locked)
    for source, value in candidates:
        record_metadata_candidates(7, source, {"title": value})

    state = _state(7, "title")
    result = apply_recommended_candidates(7)

    assert state["recommended"]["source"] == recommended_source
    assert state["pending"] is False
    assert state["conflict"] is conflict
    assert state["source_drift"] is False
    assert result["reconciled"] == []
    assert _state(7, "title")["selected_source"] == selected_source


def test_equal_value_providers_reconcile_using_existing_recommendation_priority(
    provenance_db,
):
    from metadata_provenance import (
        apply_recommended_candidates,
        record_metadata_candidates,
    )

    _record_selection("title", "Existing Title", "legacy")
    record_metadata_candidates(7, "anilist", {"title": "Existing Title"})
    record_metadata_candidates(7, "mangaupdates", {"title": "Existing Title"})

    state = _state(7, "title")
    result = apply_recommended_candidates(7)

    assert state["conflict"] is False
    assert [candidate["source"] for candidate in state["candidates"]] == [
        "mangaupdates",
        "anilist",
    ]
    assert state["recommended"]["source"] == "mangaupdates"
    assert state["source_drift"] is True
    assert result["reconciled"][0]["source"] == "mangaupdates"
    assert _state(7, "title")["selected_source"] == "mangaupdates"


@pytest.mark.parametrize(
    ("field_name", "source_column", "value"),
    [
        ("total_volumes", "vol_count_source", 12),
        ("total_chapters", "chapter_count_source", 90),
    ],
)
def test_equal_local_count_reconciliation_updates_only_the_source_column(
    provenance_db,
    field_name,
    source_column,
    value,
):
    from metadata_provenance import (
        apply_recommended_candidates,
        record_metadata_candidates,
    )

    with sqlite3.connect(provenance_db) as db:
        db.execute(f"UPDATE series SET {source_column}='legacy' WHERE id=7")
    _record_selection(field_name, value, "legacy")
    record_metadata_candidates(7, "anilist", {field_name: value})
    record_metadata_candidates(7, "local", {field_name: value})
    _audit_series_column_updates(provenance_db, field_name)

    state = _state(7, field_name)
    result = apply_recommended_candidates(7)

    assert state["recommended"]["source"] == "local"
    assert state["source_drift"] is True
    assert result["applied"] == []
    assert result["reconciled"] == [
        {"field_name": field_name, "source": "local", "value": value}
    ]
    with sqlite3.connect(provenance_db) as db:
        selected_source = db.execute(
            f"SELECT {source_column} FROM series WHERE id=7"
        ).fetchone()[0]
        value_writes = db.execute(
            "SELECT COUNT(*) FROM series_update_audit WHERE column_name=?",
            (field_name,),
        ).fetchone()[0]
        volume_stubs = db.execute(
            "SELECT COUNT(*) FROM volumes WHERE series_id=7"
        ).fetchone()[0]
    assert selected_source == "local"
    assert value_writes == 0
    assert volume_stubs == 0


def test_explicit_equal_map_candidate_reconciles_without_value_write_or_side_effects(
    provenance_db,
):
    from metadata_provenance import (
        apply_metadata_candidate,
        record_metadata_candidates,
    )

    current_map = {"1": 1}
    with sqlite3.connect(provenance_db) as db:
        db.execute(
            "UPDATE series SET chapter_vol_map=?,"
            " chapter_map_source='legacy',"
            " chapter_map_updated_at='2000-01-01T00:00:00+00:00' WHERE id=7",
            ('{"1":1}',),
        )
    _record_selection("chapter_vol_map", current_map, "legacy")
    record_metadata_candidates(7, "mangadex", {"chapter_vol_map": current_map})
    _audit_series_column_updates(provenance_db, "chapter_vol_map")

    result = apply_metadata_candidate(7, "chapter_vol_map", "mangadex")

    assert result == {
        "field_name": "chapter_vol_map",
        "source": "mangadex",
        "value": current_map,
    }
    with sqlite3.connect(provenance_db) as db:
        db.row_factory = sqlite3.Row
        series = db.execute(
            "SELECT chapter_vol_map,chapter_map_source,chapter_map_updated_at"
            " FROM series WHERE id=7"
        ).fetchone()
        selection = db.execute(
            "SELECT value_json,selected_source,locked FROM series_metadata_fields"
            " WHERE series_id=7 AND field_name='chapter_vol_map'"
        ).fetchone()
        value_writes = db.execute(
            "SELECT COUNT(*) FROM series_update_audit"
            " WHERE column_name='chapter_vol_map'"
        ).fetchone()[0]
        chapter_stubs = db.execute(
            "SELECT COUNT(*) FROM chapters WHERE series_id=7"
        ).fetchone()[0]
    assert dict(series) == {
        "chapter_vol_map": '{"1":1}',
        "chapter_map_source": "mangadex",
        "chapter_map_updated_at": "2000-01-01T00:00:00+00:00",
    }
    assert dict(selection) == {
        "value_json": '{"1":1}',
        "selected_source": "mangadex",
        "locked": 0,
    }
    assert value_writes == 0
    assert chapter_stubs == 0


def test_source_drift_reconciliation_syncs_stale_value_json_from_series(
    provenance_db,
):
    import shared
    from metadata_provenance import (
        apply_recommended_candidates,
        record_metadata_candidates,
    )

    _record_selection("title", "Old Title", "api")
    with shared.get_db() as db:
        db.execute(
            "UPDATE series_metadata_fields SET selected_at='2000-01-01T00:00:00+00:00'"
            " WHERE series_id=7 AND field_name='title'"
        )
    record_metadata_candidates(7, "anilist", {"title": "Existing Title"})

    state = _state(7, "title")
    result = apply_recommended_candidates(7)

    assert state["value"] == "Existing Title"
    assert state["pending"] is False
    assert state["source_drift"] is True
    assert result["reconciled"][0]["source"] == "anilist"
    with sqlite3.connect(provenance_db) as db:
        db.row_factory = sqlite3.Row
        series = db.execute("SELECT title FROM series WHERE id=7").fetchone()
        selection = db.execute(
            "SELECT value_json,selected_source,selected_at"
            " FROM series_metadata_fields"
            " WHERE series_id=7 AND field_name='title'"
        ).fetchone()
    assert series["title"] == "Existing Title"
    assert selection["value_json"] == '"Existing Title"'
    assert selection["selected_source"] == "anilist"
    assert selection["selected_at"] != "2000-01-01T00:00:00+00:00"


def test_value_changing_safe_apply_remains_applied_and_keeps_existing_side_effects(
    provenance_db,
):
    from metadata_provenance import (
        apply_recommended_candidates,
        record_metadata_candidates,
    )

    with sqlite3.connect(provenance_db) as db:
        db.execute("UPDATE series SET vol_count_source='legacy' WHERE id=7")
    _record_selection("total_volumes", 12, "legacy")
    record_metadata_candidates(7, "mangaupdates", {"total_volumes": 14})

    result = apply_recommended_candidates(7)

    assert result == {
        "applied": [
            {
                "field_name": "total_volumes",
                "source": "mangaupdates",
                "value": 14,
            }
        ],
        "reconciled": [],
        "skipped": [],
    }
    with sqlite3.connect(provenance_db) as db:
        series = db.execute(
            "SELECT total_volumes,vol_count_source FROM series WHERE id=7"
        ).fetchone()
        volume_stubs = db.execute(
            "SELECT COUNT(*) FROM volumes WHERE series_id=7"
        ).fetchone()[0]
    assert series == (14, "mangaupdates")
    assert volume_stubs == 14


@pytest.mark.parametrize(
    ("initial_source", "concurrent_source", "concurrent_value"),
    [
        ("anilist", "mangaupdates", "Provider Title"),
        ("mangaupdates", "anilist", "Conflicting Title"),
        ("anilist", "anilist", "Replacement Title"),
    ],
    ids=[
        "higher-priority-source",
        "provider-conflict",
        "same-source-candidate-value",
    ],
)
def test_pending_safe_apply_revalidates_concurrent_provider_changes(
    provenance_db,
    monkeypatch,
    initial_source,
    concurrent_source,
    concurrent_value,
):
    import metadata_provenance

    _record_selection("title", "Existing Title", "api")
    metadata_provenance.record_metadata_candidates(
        7,
        initial_source,
        {"title": "Provider Title"},
    )
    original_states = metadata_provenance.get_metadata_field_states

    def snapshot_then_change(series_id):
        states = original_states(series_id)
        metadata_provenance.record_metadata_candidates(
            7,
            concurrent_source,
            {"title": concurrent_value},
        )
        return states

    monkeypatch.setattr(
        metadata_provenance,
        "get_metadata_field_states",
        snapshot_then_change,
    )

    result = metadata_provenance.apply_recommended_candidates(7)

    assert result == {
        "applied": [],
        "reconciled": [],
        "skipped": [{"field_name": "title", "reason": "stale"}],
    }
    with sqlite3.connect(provenance_db) as db:
        db.row_factory = sqlite3.Row
        series = db.execute("SELECT title FROM series WHERE id=7").fetchone()
        selected = db.execute(
            "SELECT value_json,selected_source,locked FROM series_metadata_fields"
            " WHERE series_id=7 AND field_name='title'"
        ).fetchone()
    assert series["title"] == "Existing Title"
    assert dict(selected) == {
        "value_json": '"Existing Title"',
        "selected_source": "api",
        "locked": 0,
    }


def test_pending_safe_apply_becoming_equal_is_reconciled_from_current_state(
    provenance_db,
    monkeypatch,
):
    import metadata_provenance

    _record_selection("title", "Existing Title", "api")
    metadata_provenance.record_metadata_candidates(
        7,
        "anilist",
        {"title": "Provider Title"},
    )
    _audit_series_column_updates(provenance_db, "title")
    original_states = metadata_provenance.get_metadata_field_states

    def snapshot_then_match_candidate(series_id):
        states = original_states(series_id)
        with sqlite3.connect(provenance_db) as db:
            db.execute("UPDATE series SET title='Provider Title' WHERE id=7")
            db.execute(
                "UPDATE series_metadata_fields SET value_json='\"Provider Title\"'"
                " WHERE series_id=7 AND field_name='title'"
            )
            db.execute("DELETE FROM series_update_audit")
        return states

    monkeypatch.setattr(
        metadata_provenance,
        "get_metadata_field_states",
        snapshot_then_match_candidate,
    )

    result = metadata_provenance.apply_recommended_candidates(7)

    assert result == {
        "applied": [],
        "reconciled": [
            {
                "field_name": "title",
                "source": "anilist",
                "value": "Provider Title",
            }
        ],
        "skipped": [],
    }
    with sqlite3.connect(provenance_db) as db:
        db.row_factory = sqlite3.Row
        series = db.execute("SELECT title FROM series WHERE id=7").fetchone()
        selected = db.execute(
            "SELECT value_json,selected_source,locked FROM series_metadata_fields"
            " WHERE series_id=7 AND field_name='title'"
        ).fetchone()
        value_writes = db.execute(
            "SELECT COUNT(*) FROM series_update_audit WHERE column_name='title'"
        ).fetchone()[0]
    assert series["title"] == "Provider Title"
    assert dict(selected) == {
        "value_json": '"Provider Title"',
        "selected_source": "anilist",
        "locked": 0,
    }
    assert value_writes == 0


@pytest.mark.parametrize(
    "concurrent_change",
    [
        "candidate-value",
        "series-value",
        "provider-conflict",
        "recommended-source",
        "manual-lock",
    ],
)
def test_safe_source_reconciliation_revalidates_concurrent_changes(
    provenance_db,
    monkeypatch,
    concurrent_change,
):
    import metadata_provenance

    _record_selection("title", "Existing Title", "api")
    metadata_provenance.record_metadata_candidates(
        7,
        "anilist",
        {"title": "Existing Title"},
    )
    original_states = metadata_provenance.get_metadata_field_states

    def snapshot_then_change(series_id):
        states = original_states(series_id)
        with sqlite3.connect(provenance_db) as db:
            if concurrent_change == "candidate-value":
                db.execute(
                    "UPDATE series_metadata_candidates SET value_json='\"Changed Title\"'"
                    " WHERE series_id=7 AND field_name='title' AND source='anilist'"
                )
            elif concurrent_change == "series-value":
                db.execute("UPDATE series SET title='Concurrent Title' WHERE id=7")
                db.execute(
                    "UPDATE series_metadata_fields"
                    " SET value_json='\"Concurrent Title\"'"
                    " WHERE series_id=7 AND field_name='title'"
                )
            elif concurrent_change == "provider-conflict":
                db.execute(
                    "INSERT INTO series_metadata_candidates"
                    "(series_id,field_name,source,value_json,fetched_at)"
                    " VALUES(7,'title','mangaupdates','\"Different Title\"','now')"
                )
            elif concurrent_change == "recommended-source":
                db.execute(
                    "INSERT INTO series_metadata_candidates"
                    "(series_id,field_name,source,value_json,fetched_at)"
                    " VALUES(7,'title','mangaupdates','\"Existing Title\"','now')"
                )
            else:
                db.execute(
                    "UPDATE series_metadata_fields"
                    " SET selected_source='manual',locked=1"
                    " WHERE series_id=7 AND field_name='title'"
                )
        return states

    monkeypatch.setattr(
        metadata_provenance,
        "get_metadata_field_states",
        snapshot_then_change,
    )

    result = metadata_provenance.apply_recommended_candidates(7)

    assert result == {
        "applied": [],
        "reconciled": [],
        "skipped": [{"field_name": "title", "reason": "stale"}],
    }
    with sqlite3.connect(provenance_db) as db:
        db.row_factory = sqlite3.Row
        series = db.execute("SELECT title FROM series WHERE id=7").fetchone()
        selected = db.execute(
            "SELECT value_json,selected_source,locked FROM series_metadata_fields"
            " WHERE series_id=7 AND field_name='title'"
        ).fetchone()
    expected_title = (
        "Concurrent Title" if concurrent_change == "series-value" else "Existing Title"
    )
    expected_source = "manual" if concurrent_change == "manual-lock" else "api"
    expected_value_json = (
        '"Concurrent Title"'
        if concurrent_change == "series-value"
        else '"Existing Title"'
    )
    assert series["title"] == expected_title
    assert dict(selected) == {
        "value_json": expected_value_json,
        "selected_source": expected_source,
        "locked": int(concurrent_change == "manual-lock"),
    }


def test_explicit_equal_candidate_cannot_overwrite_concurrent_manual_ownership(
    provenance_db,
    monkeypatch,
):
    import metadata_provenance

    _record_selection("title", "Existing Title", "api")
    metadata_provenance.record_metadata_candidates(
        7,
        "anilist",
        {"title": "Existing Title"},
    )
    before_selection_write = threading.Event()
    resume_application = threading.Event()
    first_manual_attempt = threading.Event()
    application_finished = threading.Event()
    manual_first_outcome: list[str] = []
    application_errors: list[BaseException] = []
    original_get_db = metadata_provenance.get_db

    @contextmanager
    def synchronized_get_db():
        with original_get_db() as db:
            def trace(statement):
                if (
                    statement.startswith("INSERT INTO series_metadata_fields")
                    and not before_selection_write.is_set()
                ):
                    before_selection_write.set()
                    assert resume_application.wait(5)

            db.set_trace_callback(trace)
            yield db
            db.set_trace_callback(None)

    monkeypatch.setattr(metadata_provenance, "get_db", synchronized_get_db)

    def apply_candidate():
        try:
            metadata_provenance.apply_metadata_candidate(7, "title", "anilist")
        except BaseException as exc:
            application_errors.append(exc)
        finally:
            application_finished.set()

    def write_manual_selection():
        db = sqlite3.connect(provenance_db, timeout=0)
        try:
            db.execute("PRAGMA busy_timeout=0")
            try:
                db.execute("BEGIN IMMEDIATE")
                manual_first_outcome.append("committed")
            except sqlite3.OperationalError as exc:
                assert "locked" in str(exc).lower()
                manual_first_outcome.append("locked")
                db.rollback()
            first_manual_attempt.set()
            if manual_first_outcome == ["locked"]:
                assert application_finished.wait(5)
                db.execute("PRAGMA busy_timeout=5000")
                db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE series SET title='Manual Title' WHERE id=7")
            db.execute(
                "UPDATE series_metadata_fields"
                " SET value_json='\"Manual Title\"',selected_source='manual',locked=1"
                " WHERE series_id=7 AND field_name='title'"
            )
            db.commit()
        finally:
            db.close()

    application = threading.Thread(target=apply_candidate, daemon=True)
    manual_writer = threading.Thread(target=write_manual_selection, daemon=True)
    application.start()
    assert before_selection_write.wait(5)
    manual_writer.start()
    assert first_manual_attempt.wait(5)
    resume_application.set()
    application.join(5)
    manual_writer.join(5)

    assert not application.is_alive()
    assert not manual_writer.is_alive()
    assert application_errors == []
    with sqlite3.connect(provenance_db) as db:
        db.row_factory = sqlite3.Row
        series = db.execute("SELECT title FROM series WHERE id=7").fetchone()
        selected = db.execute(
            "SELECT value_json,selected_source,locked FROM series_metadata_fields"
            " WHERE series_id=7 AND field_name='title'"
        ).fetchone()
    assert series["title"] == "Manual Title"
    assert dict(selected) == {
        "value_json": '"Manual Title"',
        "selected_source": "manual",
        "locked": 1,
    }


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


def test_htmx_source_panel_distinguishes_title_source_drift_from_pending(
    provenance_db,
):
    import main
    from fastapi.testclient import TestClient
    from metadata_provenance import record_metadata_candidates

    _record_selection("title", "Existing Title", "api")
    record_metadata_candidates(7, "anilist", {"title": "Existing Title"})

    with TestClient(main.app) as client:
        response = client.get(
            "/api/series/7/metadata-sources",
            headers={
                "X-Api-Key": main.get_cfg("api_key"),
                "HX-Request": "true",
            },
        )

    assert response.status_code == 200
    assert "1 source drift" in response.text.lower()
    assert "1 pending" not in response.text.lower()
    assert 'hx-post="/series/7/metadata/apply-safe"' in response.text


def test_htmx_apply_safe_reports_reconciled_title_ownership(provenance_db):
    import json

    import main
    from fastapi.testclient import TestClient
    from metadata_provenance import record_metadata_candidates

    _record_selection("title", "Existing Title", "api")
    record_metadata_candidates(7, "anilist", {"title": "Existing Title"})
    token = "csrf-source-drift-" + "x" * 32

    with TestClient(main.app, cookies={"csrftoken": token}) as client:
        response = client.post(
            "/series/7/metadata/apply-safe",
            headers={"HX-Request": "true", "X-CSRFToken": token},
        )

    assert response.status_code == 200
    toast = json.loads(response.headers["HX-Trigger"])["showToast"]
    assert toast == {
        "msg": "Reconciled 1 metadata source(s)",
        "type": "success",
    }
    assert "source drift" not in response.text.lower()
    assert 'hx-post="/series/7/metadata/apply-safe"' not in response.text
    assert _state(7, "title")["selected_source"] == "anilist"


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


def test_anilist_apply_preserves_provider_source_lock(provenance_db):
    import metadata_service
    import shared
    from metadata_provenance import (
        backfill_metadata_provenance,
        set_metadata_field_lock,
    )

    with shared.get_db() as db:
        backfill_metadata_provenance(db)
    set_metadata_field_lock(7, "total_chapters", True)
    record = {
        "anilist_id": 123,
        "mal_id": None,
        "cover_url": None,
        "status": "RELEASING",
        "description": None,
        "pub_year": None,
        "volumes": 12,
        "chapters": 100,
    }

    metadata_service._apply_anilist_record(7, record)

    with sqlite3.connect(provenance_db) as db:
        db.row_factory = sqlite3.Row
        series = db.execute(
            "SELECT total_chapters FROM series WHERE id=7"
        ).fetchone()
        selected = db.execute(
            "SELECT selected_source,locked FROM series_metadata_fields"
            " WHERE series_id=7 AND field_name='total_chapters'"
        ).fetchone()
    assert series["total_chapters"] == 90
    assert dict(selected) == {"selected_source": "anilist", "locked": 1}


def test_rescan_records_local_candidate_without_overwriting_locked_count(
    provenance_db, tmp_path
):
    import rescan
    import shared
    from metadata_provenance import backfill_metadata_provenance

    library_dir = tmp_path / "library" / "Existing Title"
    library_dir.mkdir(parents=True)
    (library_dir / "Existing Title v14.cbz").write_bytes(b"not-a-real-archive")
    with shared.get_db() as db:
        backfill_metadata_provenance(db)
    with patch.object(rescan, "_series_library_dir", return_value=str(library_dir)):
        result = rescan.rescan_series_folder(7)

    assert result["created"] == 1
    with sqlite3.connect(provenance_db) as db:
        db.row_factory = sqlite3.Row
        series = db.execute(
            "SELECT total_volumes,vol_count_source FROM series WHERE id=7"
        ).fetchone()
        selected = db.execute(
            "SELECT selected_source,locked FROM series_metadata_fields"
            " WHERE series_id=7 AND field_name='total_volumes'"
        ).fetchone()
        candidate = db.execute(
            "SELECT value_json FROM series_metadata_candidates"
            " WHERE series_id=7 AND field_name='total_volumes' AND source='local'"
        ).fetchone()
    assert dict(series) == {"total_volumes": 12, "vol_count_source": "manual"}
    assert dict(selected) == {"selected_source": "manual", "locked": 1}
    assert candidate["value_json"] == "14"


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


def test_mangaupdates_refresh_uses_stored_identity_for_same_title_results(
    provenance_db,
):
    import metadata_enrichment

    with sqlite3.connect(provenance_db) as db:
        db.execute(
            "UPDATE series SET mu_id='stored-id',vol_count_source='anilist' WHERE id=7"
        )
    with patch.object(
        metadata_enrichment,
        "mu_search",
        AsyncMock(
            return_value=[
                {
                    "title": "Existing Title",
                    "mu_id": "different-id",
                    "volumes": 20,
                },
                {
                    "title": "Existing Title",
                    "mu_id": "stored-id",
                    "volumes": 14,
                },
            ]
        ),
    ):
        result = asyncio.run(metadata_enrichment.fetch_mu_metadata(7, "Existing Title"))

    assert result == {"mu_id": "stored-id", "volumes": 14, "updated_vols": True}
    with sqlite3.connect(provenance_db) as db:
        db.row_factory = sqlite3.Row
        series = db.execute(
            "SELECT mu_id,total_volumes,vol_count_source FROM series WHERE id=7"
        ).fetchone()
        candidates = db.execute(
            "SELECT field_name,value_json FROM series_metadata_candidates"
            " WHERE series_id=7 AND source='mangaupdates' ORDER BY field_name"
        ).fetchall()
    assert dict(series) == {
        "mu_id": "stored-id",
        "total_volumes": 14,
        "vol_count_source": "mangaupdates",
    }
    assert [tuple(row) for row in candidates] == [
        ("mu_id", '"stored-id"'),
        ("total_volumes", "14"),
    ]


def test_mangaupdates_refresh_preserves_cache_when_stored_identity_is_absent(
    provenance_db,
):
    import metadata_enrichment

    with sqlite3.connect(provenance_db) as db:
        db.execute(
            "UPDATE series SET mu_id='stored-id',vol_count_source='anilist' WHERE id=7"
        )
    with patch.object(
        metadata_enrichment,
        "mu_search",
        AsyncMock(
            return_value=[
                {
                    "title": "Existing Title",
                    "mu_id": "different-id",
                    "volumes": 20,
                }
            ]
        ),
    ):
        result = asyncio.run(metadata_enrichment.fetch_mu_metadata(7, "Existing Title"))

    assert result is None
    with sqlite3.connect(provenance_db) as db:
        db.row_factory = sqlite3.Row
        series = db.execute(
            "SELECT mu_id,total_volumes,vol_count_source FROM series WHERE id=7"
        ).fetchone()
        candidate_count = db.execute(
            "SELECT COUNT(*) FROM series_metadata_candidates"
            " WHERE series_id=7 AND source='mangaupdates'"
        ).fetchone()[0]
    assert dict(series) == {
        "mu_id": "stored-id",
        "total_volumes": 12,
        "vol_count_source": "anilist",
    }
    assert candidate_count == 0


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
