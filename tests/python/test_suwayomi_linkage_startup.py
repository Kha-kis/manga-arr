"""Startup must not infer a MangaDex feed from a different source's manga ID."""

import asyncio
from collections.abc import Iterator
import json
from pathlib import Path
import sqlite3
from typing import Any

import pytest
from starlette.requests import Request

import schema
import shared
from routers import suwayomi_ as swy

pytestmark = pytest.mark.usefixtures("linkage_db")


@pytest.fixture
def linkage_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "linkage.db"
    monkeypatch.setattr(shared, "DB_PATH", str(path))
    monkeypatch.setattr(shared, "CONFIG", {})

    async def no_provider(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("source linkage/startup must not contact a provider")

    monkeypatch.setattr(swy, "_gql", no_provider)
    schema.init_db()
    with shared.get_db() as db:
        db.execute(
            "INSERT INTO series(id,title,search_pattern,mangadex_id,mal_id)"
            " VALUES(1,'Orbit','Orbit','metadata-md-uuid',77)"
        )
        db.execute(
            "INSERT INTO volumes(id,series_id,volume_num,status) VALUES(1,1,1,'wanted')"
        )
        db.execute(
            "INSERT INTO chapters(id,series_id,volume_id,chapter_num,status)"
            " VALUES(1,1,1,1,'wanted')"
        )
    yield path
    with shared.get_db() as db:
        assert db.execute("PRAGMA integrity_check").fetchall()[0][0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def series() -> dict[str, Any]:
    with shared.get_db() as db:
        return dict(db.execute("SELECT * FROM series WHERE id=1").fetchone())


def sources() -> list[dict[str, Any]]:
    with shared.get_db() as db:
        return [
            dict(row)
            for row in db.execute(
                "SELECT * FROM suwayomi_sources WHERE series_id=1 ORDER BY id"
            )
        ]


def content_rows() -> dict[str, list[tuple[Any, ...]]]:
    with shared.get_db() as db:
        return {
            table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
            for table in ("volumes", "chapters")
        }


def link(source_id: str, source_name: str, manga_id: int) -> None:
    payload = json.dumps(
        {
            "source_id": source_id,
            "source_name": source_name,
            "suwayomi_manga_id": manga_id,
        }
    ).encode()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": payload, "more_body": False}

    request = Request({"type": "http", "method": "POST", "path": "/"}, receive)
    response = asyncio.run(swy.link_series_to_source(1, request))
    assert json.loads(bytes(response.body)) == {"ok": True}


def unlink(source_id: str) -> None:
    response = asyncio.run(swy.unlink_source(1, source_id))
    assert json.loads(bytes(response.body)) == {"ok": True}


@pytest.mark.parametrize("source_name", ["Manga Plus", "Other Fansubs"])
def test_alternative_link_restart_unlink_cannot_become_mangadex(
    source_name: str,
) -> None:
    original = series()
    content = content_rows()
    link("numeric-alternative", source_name, 200)
    linked = sources()
    assert series()["mangadex_id"] == original["mangadex_id"]
    schema.init_db()
    after_startup = sources()
    assert (
        asyncio.run(swy.find_or_add_manga({}, 1, "Orbit", source_name=source_name))
        == 200
    )
    unlink("numeric-alternative")
    schema.init_db()
    schema.init_db()
    assert sources() == []
    assert after_startup == linked
    assert series() == original
    fallback = swy._get_series_source(1, series())
    assert fallback is not None and fallback["suwayomi_manga_id"] is None
    assert content_rows() == content


@pytest.mark.parametrize("source_name", ["MangaDex", "Manga Plus"])
def test_explicit_unlink_before_restart_clears_only_removed_cached_identity(
    source_name: str,
) -> None:
    original = series()
    link("numeric-source", source_name, 200)
    unlink("numeric-source")
    assert series() == original
    schema.init_db()
    assert sources() == []
    fallback = swy._get_series_source(1, series())
    assert fallback is not None and fallback["suwayomi_manga_id"] is None


def test_truly_unlinked_legacy_mangadex_identity_keeps_fallback_and_backfill() -> None:
    with shared.get_db() as db:
        db.execute("UPDATE series SET suwayomi_id=101,ddl_language='fr' WHERE id=1")
    original = series()
    assert swy._get_series_source(1, original) == {
        "source_name": "MangaDex",
        "source_id": "mangadex",
        "suwayomi_manga_id": 101,
    }
    schema.init_db()
    linked = sources()
    assert len(linked) == 1
    assert linked[0]["source_id"] == "mangadex"
    assert linked[0]["suwayomi_manga_id"] == 101
    assert linked[0]["source_lang"] == "fr"
    schema.init_db()
    assert sources() == linked
    assert series() == original
    unlink("mangadex")
    schema.init_db()
    assert sources() == []
    assert series()["suwayomi_id"] is None


def test_existing_numeric_mangadex_link_is_not_duplicated_on_restart() -> None:
    link("numeric-mangadex", "MangaDex", 101)
    original = series()
    linked = sources()
    schema.init_db()
    schema.init_db()
    assert sources() == linked
    assert series() == original


def test_unlink_preserves_other_source_rows_and_their_cached_identity() -> None:
    link("alternative", "Manga Plus", 200)
    link("numeric-mangadex", "MangaDex", 101)
    original = series()
    retained = sources()[1:]
    unlink("alternative")
    assert series() == original
    assert sources() == retained
    schema.init_db()
    assert sources() == retained
    assert swy._get_series_source(1, series()) == retained[0]


def test_unlink_current_source_does_not_reassign_other_source_identity() -> None:
    link("numeric-mangadex", "MangaDex", 101)
    link("alternative", "Manga Plus", 200)
    retained = sources()[:1]
    unlink("alternative")
    assert series()["suwayomi_id"] is None
    assert sources() == retained
    schema.init_db()
    assert sources() == retained
    assert swy._get_series_source(1, series()) == retained[0]


def test_unknown_unlink_does_not_clear_legacy_cached_identity() -> None:
    with shared.get_db() as db:
        db.execute("UPDATE series SET suwayomi_id=101 WHERE id=1")
    original = series()
    unlink("never-linked")
    assert series() == original
    schema.init_db()
    assert sources()[0]["suwayomi_manga_id"] == 101


def test_unlink_delete_failure_rolls_back_cached_id_and_source_row() -> None:
    link("alternative", "Manga Plus", 200)
    original = series()
    linked = sources()
    with shared.get_db() as db:
        db.execute(
            "CREATE TRIGGER refuse_unlink BEFORE DELETE ON suwayomi_sources"
            " BEGIN SELECT CASE"
            " WHEN (SELECT suwayomi_id FROM series WHERE id=OLD.series_id) IS NULL"
            " THEN RAISE(ABORT,'injected unlink failure')"
            " ELSE RAISE(ABORT,'cached identity was not cleared before delete')"
            " END; END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected unlink failure"):
        unlink("alternative")
    assert series() == original
    assert sources() == linked


def test_nullable_source_row_blocks_inference_without_clearing_unrelated_id() -> None:
    with shared.get_db() as db:
        db.execute("UPDATE series SET suwayomi_id=101 WHERE id=1")
        db.execute(
            "INSERT INTO suwayomi_sources(series_id,source_id,source_name)"
            " VALUES(1,'alternative','Manga Plus')"
        )
    original = series()
    linked = sources()
    schema.init_db()
    assert sources() == linked
    unlink("alternative")
    assert series() == original


def test_ambiguous_historical_alias_is_not_automatically_reclassified() -> None:
    link("alternative", "Manga Plus", 200)
    with shared.get_db() as db:
        db.execute(
            "INSERT INTO suwayomi_sources(series_id,source_id,source_name,"
            "suwayomi_manga_id) VALUES(1,'mangadex','MangaDex',200)"
        )
    linked = sources()
    schema.init_db()
    assert sources() == linked
    unlink("alternative")
    assert sources() == linked[1:]
    assert series()["suwayomi_id"] is None
    schema.init_db()
    assert sources() == linked[1:]
