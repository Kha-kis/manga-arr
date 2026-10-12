"""Automatic matching must pass SQLite-backed series identity to the provider."""

from collections.abc import Iterator
from pathlib import Path
import sqlite3
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import schema
import shared
from routers import suwayomi_ as swy


MD_UUID = "46fb8129-fa17-4532-8686-0b9dbdde0236"


@pytest.fixture
def match_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setattr(shared, "DB_PATH", str(tmp_path / "match.db"))
    monkeypatch.setattr(shared, "CONFIG", {})

    async def no_network(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("matching regression must not contact a provider")

    monkeypatch.setattr(swy, "_gql", no_network)
    schema.init_db()
    with shared.get_db() as db:
        db.execute(
            "INSERT INTO series(id,title,search_pattern,mangadex_id)"
            " VALUES(1,'Orbit','Orbit',?)",
            (MD_UUID,),
        )
        db.execute(
            "INSERT INTO download_clients(id,name,type,host,port)"
            " VALUES(1,'Isolated Suwayomi','suwayomi','example.invalid',4567)"
        )
        row = db.execute("SELECT * FROM series WHERE id=1").fetchone()
        assert isinstance(row, sqlite3.Row)
        assert not hasattr(row, "get")
    app = FastAPI()
    app.include_router(swy.router)
    with TestClient(app) as client:
        yield client
    with shared.get_db() as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    (
        "series_language",
        "configured_language",
        "uuid",
        "source_name",
        "expected_language",
    ),
    [
        ("fr", "de", MD_UUID, None, "fr"),
        (None, "de", MD_UUID, None, "de"),
        (None, None, MD_UUID, None, "en"),
        ("", None, None, None, "en"),
        (None, "es", "", None, "es"),
        ("ja", "en", MD_UUID, "Manga Plus", "ja"),
    ],
)
def test_match_preserves_provider_identity_and_language(
    match_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    series_language: str | None,
    configured_language: str | None,
    uuid: str | None,
    source_name: str | None,
    expected_language: str,
) -> None:
    if configured_language is not None:
        shared.CONFIG["ddl_language"] = configured_language
    with shared.get_db() as db:
        db.execute(
            "UPDATE series SET ddl_language=?,mangadex_id=? WHERE id=1",
            (series_language, uuid),
        )
        if source_name:
            db.executemany(
                "INSERT INTO suwayomi_sources(series_id,source_id,source_name,priority)"
                " VALUES(1,?,?,?)",
                [("other", "Other Source", 10), ("selected", source_name, 0)],
            )
        before = tuple(db.execute("SELECT * FROM series WHERE id=1").fetchone())
        links = [tuple(row) for row in db.execute("SELECT * FROM suwayomi_sources")]
    calls: list[dict[str, Any]] = []

    async def fake_provider(
        client: dict[str, Any],
        series_id: int,
        title: str,
        lang: str,
        mangadex_uuid: str | None = None,
        source_name: str = "MangaDex",
    ) -> int:
        calls.append(
            dict(
                client_id=client["id"],
                series_id=series_id,
                title=title,
                lang=lang,
                mangadex_uuid=mangadex_uuid,
                source_name=source_name,
            )
        )
        return 41734

    monkeypatch.setattr(swy, "find_or_add_manga", fake_provider)
    response = match_client.post("/api/series/1/suwayomi/match")
    assert response.status_code == 200, response.json()
    assert response.json() == {"ok": True, "suwayomi_id": 41734}
    assert calls == [
        dict(
            client_id=1,
            series_id=1,
            title="Orbit",
            lang=expected_language,
            mangadex_uuid=uuid,
            source_name=source_name or "MangaDex",
        )
    ]
    with shared.get_db() as db:
        assert tuple(db.execute("SELECT * FROM series WHERE id=1").fetchone()) == before
        assert [
            tuple(row) for row in db.execute("SELECT * FROM suwayomi_sources")
        ] == links


@pytest.mark.parametrize("missing", ["series", "client"])
def test_match_missing_data_still_returns_404(
    match_client: TestClient, missing: str
) -> None:
    with shared.get_db() as db:
        db.execute(
            "DELETE FROM series"
            if missing == "series"
            else "DELETE FROM download_clients"
        )
    response = match_client.post("/api/series/1/suwayomi/match")
    assert response.status_code == 404
    assert response.json() == {"ok": False, "message": "Not found"}


def test_match_provider_error_preserves_json_contract(
    match_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failed_provider(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("isolated provider failure")

    monkeypatch.setattr(swy, "find_or_add_manga", failed_provider)
    response = match_client.post("/api/series/1/suwayomi/match")
    assert response.status_code == 500
    assert response.json() == {"ok": False, "message": "isolated provider failure"}


def test_match_reuses_selected_link_without_provider_lookup(
    match_client: TestClient,
) -> None:
    with shared.get_db() as db:
        db.execute(
            "INSERT INTO suwayomi_sources(series_id,source_id,source_name,suwayomi_manga_id)"
            " VALUES(1,'selected','Manga Plus',987)"
        )
    response = match_client.post("/api/series/1/suwayomi/match")
    assert response.status_code == 200, response.json()
    assert response.json() == {"ok": True, "suwayomi_id": 987}
    with shared.get_db() as db:
        assert (
            db.execute("SELECT mangadex_id FROM series WHERE id=1").fetchone()[0]
            == MD_UUID
        )
        assert db.execute("SELECT COUNT(*) FROM suwayomi_sources").fetchone()[0] == 1
