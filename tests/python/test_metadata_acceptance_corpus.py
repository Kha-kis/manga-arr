"""Release qualification for representative catalogue metadata shapes."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


CASES = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "metadata_acceptance.json").read_text()
)


@pytest.fixture
def metadata_db(tmp_path, monkeypatch):
    import main
    import security
    import shared

    db_path = tmp_path / "metadata-acceptance.db"
    key_dir = tmp_path / "keys"
    monkeypatch.setattr(main, "DB_PATH", str(db_path))
    monkeypatch.setattr(shared, "DB_PATH", str(db_path))
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(str(key_dir))
    main.init_db()
    yield db_path
    security._SECRET_CIPHER = None


def _seed_series(db_path: Path, values: dict) -> None:
    series = {
        "id": 7,
        "title": values["title"],
        "search_pattern": values["title"],
        "anilist_id": None,
        "monitored": 1,
        **values,
    }
    columns = ",".join(series)
    placeholders = ",".join("?" for _ in series)
    with sqlite3.connect(db_path) as db:
        db.execute(
            f"INSERT INTO series({columns}) VALUES({placeholders})",
            tuple(series.values()),
        )


@pytest.mark.parametrize(
    "case",
    CASES["catalogue_cases"],
    ids=lambda case: case["name"],
)
def test_catalogue_metadata_acceptance_cases(metadata_db, case):
    from metadata_service import _apply_anilist_record

    _seed_series(metadata_db, case["series"])
    with sqlite3.connect(metadata_db) as db:
        for volume_num in case.get("downloaded_volumes", []):
            db.execute(
                "INSERT INTO volumes(series_id,volume_num,status) VALUES(?,?,'downloaded')",
                (7, volume_num),
            )
        for chapter_num in case.get("downloaded_chapters", []):
            db.execute(
                "INSERT INTO chapters(series_id,chapter_num,status) VALUES(?,?,'downloaded')",
                (7, chapter_num),
            )

    changed = _apply_anilist_record(7, case["record"])

    with sqlite3.connect(metadata_db) as db:
        db.row_factory = sqlite3.Row
        series = dict(db.execute("SELECT * FROM series WHERE id=7").fetchone())
        integer_volume_rows = db.execute(
            "SELECT COUNT(*) FROM volumes WHERE series_id=7"
            " AND volume_num=CAST(volume_num AS INTEGER)"
        ).fetchone()[0]

    expected = case["expected"]
    for field in (
        "total_volumes",
        "total_chapters",
        "vol_count_source",
        "chapter_count_source",
        "status",
        "update_strategy",
    ):
        assert series[field] == expected[field]
    assert integer_volume_rows == expected["integer_volume_rows"]
    assert "cover_url" in changed
    assert "description" in changed


def test_alternate_titles_and_genres_are_curated(metadata_db):
    from metadata_service import _store_aliases_and_genres

    case = CASES["alias_case"]
    _seed_series(
        metadata_db,
        {
            "title": case["main_title"],
            "edition_type": "standard",
            "status": "RELEASING",
            "update_strategy": "always",
            "total_volumes": None,
            "total_chapters": None,
            "vol_count_source": "anilist",
            "chapter_count_source": "anilist",
        },
    )

    result = _store_aliases_and_genres(7, case["main_title"], case["record"])

    with sqlite3.connect(metadata_db) as db:
        aliases = [
            row[0]
            for row in db.execute(
                "SELECT alias FROM series_aliases WHERE series_id=7 ORDER BY alias COLLATE NOCASE"
            )
        ]
        genres = [
            row[0]
            for row in db.execute(
                "SELECT tag FROM series_tags WHERE series_id=7 ORDER BY tag"
            )
        ]
    assert aliases == case["expected_aliases"]
    assert genres == case["expected_genres"]
    assert result == {"aliases": 2, "genres": 3}


@pytest.mark.parametrize(
    "case",
    CASES["mangaupdates_cases"],
    ids=lambda case: case["name"],
)
def test_mangaupdates_conflict_policy(metadata_db, case):
    from metadata_enrichment import fetch_mu_metadata

    title = "Provider Conflict Series"
    _seed_series(
        metadata_db,
        {
            "title": title,
            "edition_type": case["edition_type"],
            "status": "RELEASING",
            "update_strategy": "always",
            "total_volumes": case["current_volumes"],
            "total_chapters": 50,
            "vol_count_source": case["current_source"],
            "chapter_count_source": "anilist",
        },
    )
    result = {
        "title": title,
        "mu_id": "provider-conflict-series",
        "volumes": case["incoming_volumes"],
    }

    with patch(
        "metadata_enrichment.mu_search",
        new=AsyncMock(return_value=[result]),
    ):
        summary = asyncio.run(fetch_mu_metadata(7, title))

    with sqlite3.connect(metadata_db) as db:
        db.row_factory = sqlite3.Row
        series = dict(
            db.execute(
                "SELECT mu_id,total_volumes,vol_count_source FROM series WHERE id=7"
            ).fetchone()
        )
    assert series["mu_id"] == "provider-conflict-series"
    assert series["total_volumes"] == case["expected_volumes"]
    assert series["vol_count_source"] == case["expected_source"]
    assert summary["updated_vols"] is case["expected_updated"]
